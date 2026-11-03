"""
Отчёты: календарь на неделю и «перекос» опционов — сколько закладывают опционы против того,
сколько акция обычно проходит после отчёта.

Для каждой акции из списка опционного скринера (cache/opt_universe.json), у которой отчёт
в ближайшие WINDOW дней:
  • дата и время: до открытия (BMO) или после закрытия (AMC) — по Yahoo;
  • заложенный ход — ATM-стрэддл (колл + пут на ближайшем к цене страйке) первой экспирации
    не раньше дня реакции, в % от цены. Это обычная оценка «expected move»;
  • история — ход закрытие → закрытие в день реакции на прошлых отчётах (до 40 отчётов ≈ 10 лет):
    BMO — закрытие дня отчёта к закрытию накануне, AMC — закрытие следующего дня к закрытию дня отчёта;
  • перекос — заложенный ход / средний исторический. < 0,85 — опционы дешевле истории
    (интереснее покупка волатильности), > 1,15 — дороже (интереснее продажа);
  • «шире заложенного» — доля прошлых отчётов, где ход был больше сегодняшнего заложенного;
    «внутри 1,2×» — доля, где ход уместился в 1,2 заложенного (короткие страйки кондора);
  • EPS: оценка аналитиков и сколько из последних 8 отчётов было лучше оценки;
  • OB — по акции был сигнал ордер-блока 4H за последние дни (cache/signals.jsonl).

Важно: это история и цены опционов, а не прогноз направления.

Запуск (по будням через ≈20 минут после открытия — бид/аск уже живые, .github/workflows/earnings.yml):
  python earnings.py                       — полный прогон, Telegram, cache/earnings.json
  python earnings.py --no-tg               — без Telegram
  python earnings.py --tickers AMD,DOCN --no-tg --out cache/earnings_test.json
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "earnings.json"
UNIVERSE_FILE = BASE / "cache" / "opt_universe.json"
SIGNALS_FILE = BASE / "cache" / "signals.jsonl"
ET = "America/New_York"
WINDOW = 7            # дней вперёд
HIST_MAX = 40         # прошлых отчётов в истории (≈10 лет)
MIN_HIST = 4          # меньше — перекос не считаем
CHEAP, RICH = 0.85, 1.15
MAX_EXP_DAYS = 4      # экспирация дальше дня реакции — в стрэддле лишние дни обычной волатильности, перекос не считаем
STALE_SHARE = 0.4     # стрэддл по последним сделкам дешевле 0,4 × истории — цена старая, перекос не считаем
SITE_URL = "https://luxeon87-21q.github.io/ob-bot/#earnings"
log = logging.getLogger("ob_bot.earnings")


# ======================================================================== чистые функции
def session_of(ts) -> Optional[str]:
    """BMO — до открытия, AMC — после закрытия, DMH — во время торгов, None — время неизвестно
    (Yahoo ставит полночь, когда час не подтверждён)."""
    if ts is None:
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert(ET)
    hm = t.hour * 60 + t.minute
    if hm == 0:
        return None
    if hm < 9 * 60 + 30:
        return "BMO"
    if hm >= 16 * 60:
        return "AMC"
    return "DMH"


def next_bday(d: date) -> date:
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def reaction_day(d: date, session: Optional[str]) -> date:
    """День, в который рынок реагирует на отчёт: BMO и во время торгов — тот же день, иначе — следующий."""
    return d if session in ("BMO", "DMH") else next_bday(d)


def reaction_moves(closes: pd.Series, events: Sequence[Tuple[date, Optional[str]]]) -> List[Tuple[str, float]]:
    """Ход закрытие → закрытие (в %) на прошлых отчётах. closes — дневные закрытия с индексом-датами.
    Отчёты с неизвестным временем пропускаем: нельзя понять, какой день считать."""
    if closes is None or closes.empty:
        return []
    s = closes.dropna().copy()
    s.index = pd.DatetimeIndex([pd.Timestamp(x).date() for x in s.index])
    s = s[~s.index.duplicated(keep="last")].sort_index()
    days = list(s.index.date)
    pos = {d: i for i, d in enumerate(days)}
    out = []
    for d, ses in events:
        if ses is None or d not in pos:
            continue
        i = pos[d]
        if ses in ("BMO", "DMH"):
            if i == 0:
                continue
            a, b = s.iloc[i - 1], s.iloc[i]
        else:
            if i + 1 >= len(days):
                continue
            a, b = s.iloc[i], s.iloc[i + 1]
        if a > 0:
            out.append((d.isoformat(), round((b / a - 1) * 100, 2)))
    return out


def _px(row) -> Tuple[Optional[float], str]:
    """Цена опциона: середина бид/аск, если оба есть, иначе последняя сделка."""
    bid, ask, last = (float(row.get(k) or 0) for k in ("bid", "ask", "lastPrice"))
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2, "mid"
    if last > 0:
        return last, "last"
    return None, ""


def straddle(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> Optional[dict]:
    """ATM-стрэддл: страйк, общий для коллов и путов, ближайший к цене."""
    if calls is None or puts is None or calls.empty or puts.empty or not spot:
        return None
    common = sorted(set(calls["strike"]).intersection(puts["strike"]), key=lambda k: abs(k - spot))
    for k in common[:3]:
        c = calls[calls["strike"] == k].iloc[0]
        p = puts[puts["strike"] == k].iloc[0]
        cp, cs = _px(c)
        pp, ps = _px(p)
        if cp is None or pp is None:
            continue
        px = cp + pp
        return dict(strike=float(k), price=round(px, 2), pct=round(px / spot * 100, 2),
                    src="mid" if cs == ps == "mid" else "last")
    return None


def stats(moves: Sequence[Tuple[str, float]], implied: Optional[float]) -> dict:
    a = np.abs([m for _, m in moves])
    out = dict(hist_n=int(a.size), hist_avg=None, hist_med=None, hist_avg8=None, last_move=None,
               last_date=None, ratio=None, over=None, inside12=None)
    if not a.size:
        return out
    out.update(hist_avg=round(float(a.mean()), 2), hist_med=round(float(np.median(a)), 2),
               hist_avg8=round(float(a[-8:].mean()), 2), last_move=moves[-1][1], last_date=moves[-1][0])
    if implied and a.size >= MIN_HIST:
        out["ratio"] = round(implied / float(a.mean()), 2)
        out["over"] = round(float((a > implied).mean()), 2)
        out["inside12"] = round(float((a <= 1.2 * implied).mean()), 2)
    return out


def quality(implied: Optional[float], st: Optional[dict], hist_avg: Optional[float]) -> Optional[str]:
    """Почему заложенному ходу нельзя верить для сравнения: far — экспирация через несколько дней после
    реакции (стрэддл завышен), stale — по последним сделкам и подозрительно дёшево. None — всё в порядке."""
    if implied is None or not st:
        return None
    if (st.get("exp_days") or 0) > MAX_EXP_DAYS:
        return "far"
    if st.get("src") == "last" and hist_avg and implied < STALE_SHARE * hist_avg:
        return "stale"
    return None


def verdict(ratio: Optional[float]) -> Optional[str]:
    if ratio is None:
        return None
    return "cheap" if ratio < CHEAP else "rich" if ratio > RICH else "fair"


VERDICT_RU = {"cheap": "опционы дешевле истории", "rich": "опционы дороже истории", "fair": "≈ как история"}


def ob_marks(path: Path = SIGNALS_FILE, days: int = 7) -> Dict[str, dict]:
    """Последний сигнал ордер-блока 4H по каждой акции за последние days дней."""
    out: Dict[str, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-5000:]
    except Exception:
        return out
    since = pd.Timestamp.now(tz=ET) - pd.Timedelta(days=days)
    for ln in lines:
        try:
            s = json.loads(ln)
            if s.get("type") == "wolfe" or pd.Timestamp(s["bar_time"]) < since:
                continue
            out[s["ticker"]] = dict(side=s.get("side"), type=s.get("type"), bar_time=s["bar_time"])
        except Exception:
            continue
    return out


# ======================================================================== Yahoo
def _cal_date(ticker: str, today: date, end: date) -> Optional[date]:
    import yfinance as yf
    cal = yf.Ticker(ticker).calendar
    ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not ed:
        return None
    ed = ed if isinstance(ed, (list, tuple)) else [ed]
    ds = sorted(pd.Timestamp(x).date() for x in ed)
    ds = [x for x in ds if today <= x <= end]
    return ds[0] if ds else None


def _col(df: pd.DataFrame, *keys) -> Optional[str]:
    for c in df.columns:
        lc = str(c).lower()
        if all(k in lc for k in keys):
            return c
    return None


def _num(x) -> Optional[float]:
    try:
        v = float(x)
        return None if np.isnan(v) else v
    except Exception:
        return None


def details(ticker: str, d_cal: date, today: date) -> dict:
    """Время отчёта, история ходов, стрэддл и EPS по одной акции."""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    rec = dict(ticker=ticker, date=d_cal.isoformat(), session=None, eps_est=None, beats=None, beat_n=0)

    events: List[Tuple[date, Optional[str]]] = []
    try:
        ed = tk.get_earnings_dates(limit=HIST_MAX + 8)
    except Exception as e:
        log.debug("даты отчётов %s: %s", ticker, e)
        ed = None
    if ed is not None and not ed.empty:
        est_c, rep_c = _col(ed, "estimate"), _col(ed, "reported")
        ed = ed.sort_index()
        surprises = []
        for ts, row in ed.iterrows():
            t = pd.Timestamp(ts)
            t = t.tz_convert(ET) if t.tzinfo is not None else t
            d, ses = t.date(), session_of(t)
            rep = _num(row.get(rep_c)) if rep_c else None
            est = _num(row.get(est_c)) if est_c else None
            if abs((d - d_cal).days) <= 3 and d >= today - timedelta(days=1):
                rec.update(date=d.isoformat(), session=ses, eps_est=est)      # предстоящий (или сегодняшний) отчёт
            elif d < today and rep is not None:
                events.append((d, ses))
                if est is not None:
                    surprises.append(rep > est)
        last8 = surprises[-8:]
        rec.update(beats=sum(last8) if last8 else None, beat_n=len(last8))
    events = events[-HIST_MAX:]

    hist = tk.history(period="11y", interval="1d", auto_adjust=False)
    closes = hist["Close"] if hist is not None and not hist.empty else pd.Series(dtype=float)
    spot = float(closes.iloc[-1]) if len(closes) else None
    moves = reaction_moves(closes, events)
    rec.update(price=round(spot, 2) if spot else None, moves=moves[-12:])

    # стрэддл: первая экспирация не раньше дня реакции
    d_rep = date.fromisoformat(rec["date"])
    react = reaction_day(d_rep, rec["session"] or "AMC")
    rec["react"] = react.isoformat()
    imp = None
    try:
        exps = [e for e in (tk.options or ()) if date.fromisoformat(e) >= react]
        if exps and spot:
            ch = tk.option_chain(exps[0])
            imp = straddle(ch.calls, ch.puts, spot)
            if imp:
                imp["exp"] = exps[0]
                imp["exp_days"] = (date.fromisoformat(exps[0]) - react).days
    except Exception as e:
        log.debug("опционы %s: %s", ticker, e)
    rec["implied"] = imp["pct"] if imp else None
    rec["straddle"] = imp
    st = stats(moves, rec["implied"])
    note = quality(rec["implied"], imp, st["hist_avg"])
    if note:
        st.update(ratio=None, over=None, inside12=None)
    rec.update(st, note=note, verdict=verdict(st["ratio"]))
    return rec


def _retry(fn, *a, tries=2):
    for k in range(tries):
        try:
            return fn(*a)
        except Exception as e:
            if k == tries - 1:
                log.debug("%s%s: %s", fn.__name__, a[:1], e)
            time.sleep(1.5)
    return None


def run(tickers: Iterable[str], vol: Dict[str, int], window: int = WINDOW, workers: int = 6,
        budget_s: float = 1500) -> dict:
    now = pd.Timestamp.now(tz=ET)
    today = now.date()
    end = today + timedelta(days=window)
    tickers = list(dict.fromkeys(tickers))
    t0 = time.time()

    found: Dict[str, date] = {}
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(_retry, _cal_date, t.replace(".", "-"), today, end): t for t in tickers}
        for f in as_completed(futs):
            d = f.result()
            if d:
                found[futs[f]] = d
    log.info("отчёты в ближайшие %d дн.: %d из %d акций (%.0f с)", window, len(found), len(tickers), time.time() - t0)

    obs = ob_marks()
    rows, fail = [], 0
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(_retry, details, t.replace(".", "-"), d, today): t for t, d in found.items()}
        for f in as_completed(futs):
            t, r = futs[f], f.result()
            if not r:
                fail += 1
                continue
            r["ticker"] = t
            r["vol"] = vol.get(t)
            r["ob"] = obs.get(t)
            rows.append(r)
            if time.time() - t0 > budget_s:
                log.warning("лимит времени — остальные акции пропущены")
                for g in futs:
                    g.cancel()
                break
    rows.sort(key=lambda r: (r["date"], {"BMO": 0, "DMH": 1, "AMC": 2}.get(r["session"], 3), -(r.get("vol") or 0)))
    log.info("посчитано %d, ошибок %d", len(rows), fail)
    return dict(updated=now.isoformat(), today=today.isoformat(), window=window,
                checked=len(tickers), found=len(found), rows=rows)


# ======================================================================== сообщение
def _n(x, d=1) -> str:
    return "—" if x is None else f"{x:.{d}f}".replace(".", ",")


def tradable_today(res: dict) -> Tuple[List[dict], List[dict]]:
    """Отчёты, которые можно сыграть сегодняшними опционами: сегодня после закрытия и завтра до открытия."""
    today = date.fromisoformat(res["today"])
    nxt = next_bday(today).isoformat()
    tod = [r for r in res["rows"] if r["date"] == res["today"] and r["session"] in ("AMC", None)]
    tom = [r for r in res["rows"] if r["date"] == nxt and r["session"] in ("BMO", "DMH", None)]
    return tod, tom


def _line(r: dict) -> str:
    tv = html.escape(r["ticker"].replace("-", "."))
    head = f'<a href="https://www.tradingview.com/chart/?symbol={tv}&amp;interval=D"><b>{html.escape(r["ticker"])}</b></a>'
    if r.get("session") is None:
        head += " (время ?)"
    if r.get("ob"):
        head += " 📦" + ("🟢" if r["ob"]["side"] == "bull" else "🔴")
    imp = f"±{_n(r.get('implied'))}%" if r.get("implied") else "опционы н/д"
    if r.get("note") == "far":
        imp = "≈" + imp + " (дальняя экспирация)"
    elif r.get("note") == "stale":
        imp += " (цена старая)"
    hist = f"история ±{_n(r.get('hist_avg'))}% ({r['hist_n']})" if r.get("hist_n") else "истории нет"
    s = f"{head}: {imp} · {hist}"
    v = r.get("verdict")
    if v == "cheap":
        s += " · 🟢 <b>дешевле</b>"
    elif v == "rich":
        s += " · 🔴 <b>дороже</b>"
    if r.get("over") is not None:
        s += f"\n   шире заложенного: {round(r['over'] * 100)}% · внутри 1,2×: {round(r['inside12'] * 100)}%"
    if r.get("eps_est") is not None:
        s += f" · EPS {_n(r['eps_est'], 2)}"
        if r.get("beat_n"):
            s += f" (лучше оценки {r['beats']}/{r['beat_n']})"
    return s


def build_message(res: dict, top: int = 20) -> str:
    tod, tom = tradable_today(res)
    if not tod and not tom:
        return ""
    far = sum(r.get("note") == "far" for r in tod + tom)
    tod = [r for r in tod if r.get("note") != "far"]          # месячная экспирация — не сыграть сам отчёт
    tom = [r for r in tom if r.get("note") != "far"]
    if not tod and not tom:
        return ""
    by_vol = lambda xs: sorted(xs, key=lambda r: -(r.get("vol") or 0))[:top]
    nd = next_bday(date.fromisoformat(res["today"]))
    out = ["📊 <b>Отчёты — что можно сыграть сегодняшними опционами</b>",
           "Заложенный ход (стрэддл) против среднего хода на прошлых отчётах. "
           "🟢 дешевле истории — интереснее покупка волатильности, 🔴 дороже — продажа."]
    if tod:
        out.append(f"\n🌙 <b>Сегодня после закрытия</b>")
        out += [_line(r) for r in by_vol(tod)]
    if tom:
        out.append(f"\n🌅 <b>{nd.strftime('%d.%m')} до открытия</b>")
        out += [_line(r) for r in by_vol(tom)]
    if far:
        out.append(f"\nЕщё {far} — только с месячной экспирацией (ход в опционах не выделить), они на сайте.")
    out.append(f"\n📦 — сигнал ордер-блока 4H за неделю. Вся неделя: {SITE_URL}")
    out.append("⚠️ История и цены опционов, не прогноз направления. Не инвестиционная рекомендация.")
    return "\n".join(out)


# ======================================================================== запуск
def universe(tickers_arg: str) -> Tuple[List[str], Dict[str, int]]:
    try:
        u = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        u = {}
    vol = {k: int(v) for k, v in (u.get("vol") or {}).items()}
    if tickers_arg:
        return [t.strip().upper() for t in tickers_arg.split(",") if t.strip()], vol
    return list(u.get("tickers") or []), vol


def main():
    import bot
    import tg
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tg", action="store_true")
    ap.add_argument("--if-new", action="store_true", help="пропустить, если сегодня уже считали")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--window", type=int, default=WINDOW)
    ap.add_argument("--out", default=str(OUT_FILE))
    a = ap.parse_args()
    out = Path(a.out)
    now = pd.Timestamp.now(tz=ET)
    if a.if_new and not a.tickers:
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        if prev.get("today") == now.date().isoformat() and prev.get("live"):
            log.info("за сегодня уже посчитано по живым ценам — пропускаю")
            gh = os.environ.get("GITHUB_OUTPUT")
            if gh:
                with open(gh, "a") as f:
                    f.write("skip=true\n")
            return
    tickers, vol = universe(a.tickers)
    if not tickers:
        log.error("список акций пуст: нет cache/opt_universe.json")
        sys.exit(1)
    res = run(tickers, vol, window=a.window)
    msg = build_message(res)
    res["msg"] = msg
    hm = now.hour * 60 + now.minute
    res["live"] = now.weekday() < 5 and 9 * 60 + 40 <= hm <= 16 * 60     # бид/аск живые (идут торги)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if msg and not a.no_tg and not a.tickers and token:
        k = tg.broadcast_one(token, tg.load(), msg, key="earnings")
        log.info("разослано подписчикам: %d", k)
    else:
        print(msg or "сегодня после закрытия и завтра до открытия отчётов нет")


if __name__ == "__main__":
    main()
