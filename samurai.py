"""
Сканер «Самурай»: продажа путов (naked put) по ТЗ, как в Option Samurai.

Фильтры:
  1. экспирация опциона раньше ближайшего отчёта компании;
  2. капитализация выше $2 млрд;
  3. EMA 30 выше EMA 50 и EMA 30 выше EMA 500 (дневки);
  4. MACD (12/26/9) выше сигнальной линии;
  5. рекомендации аналитиков — Buy или Strong Buy (средняя оценка Yahoo ≤ 2.5);
  6. объём опционов за день выше 5000 контрактов (коллы + путы, экспирации до 60 дней);
  7. IV Rank выше 60;
  8. вероятность, что пут истечёт без стоимости, выше 70%;
  9. до экспирации 28–45 дней;
 10. спред бид/аск меньше $0.50.
По каждой акции берётся пут с лучшей годовой доходностью среди прошедших 8–10.
Доходность = бид / (страйк − бид), как в Option Samurai; годовая = × 365 / дней.

IV Rank. У Yahoo нет истории подразумеваемой волатильности, поэтому:
  • если задан EULERPOOL_KEY — берём IV Rank у Eulerpool;
  • иначе, если накоплено ≥ IVR_MIN_DAYS дней своей истории IV (cache/iv_history.json) — считаем сами;
  • иначе — замена «≈»: где текущая IV30 в годовом диапазоне исторической волатильности (HV20) акции.

Запуск — дважды в день по будням (.github/workflows/samurai.yml): ~10:30 и ~15:00 по Нью-Йорку.
  python samurai.py                    — полный прогон, Telegram, cache/samurai.json
  python samurai.py --no-tg            — без Telegram
  python samurai.py --tickers AAPL,NVDA --no-tg
  python samurai.py --slot             — запуск по расписанию: сам решает, утренний это прогон или вечерний
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "samurai.json"
IV_FILE = BASE / "cache" / "iv_history.json"
VOL_FILE = BASE / "cache" / "samurai_vol.json"
INFO_FILE = BASE / "cache" / "samurai_info.json"
SITE_URL = "https://luxeon87-21q.github.io/ob-bot/#samurai"
ET = "America/New_York"
log = logging.getLogger("ob_bot.samurai")


def _env(name, default):
    v = os.environ.get(name)
    return default if v in (None, "") else type(default)(v)


@dataclass
class SParams:
    mcap_min: float = 2e9
    ema_fast: int = 30
    ema_mid: int = 50
    ema_slow: int = 500
    analyst_max: float = 2.5      # средняя оценка Yahoo: 1 Strong Buy … 2 Buy … 3 Hold; ≤ 2.5 — Buy и выше
    opt_vol_min: int = 5000
    vol_days: int = 60            # объём считаем по экспирациям до N дней
    ivr_min: float = 60
    ivr_min_days: int = 60        # сколько дней своей истории IV нужно, чтобы считать IV Rank самим
    prob_min: float = 0.70
    dte_min: int = 28
    dte_max: int = 45
    spread_max: float = 0.50
    bid_min: float = 0.10
    rate: float = 0.04
    top: int = 20

    @classmethod
    def from_env(cls) -> "SParams":
        p = cls()
        for k, v in asdict(p).items():
            setattr(p, k, _env("SAM_" + k.upper(), v))
        return p


# ======================================================================== расчёты
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def technicals(df: pd.DataFrame, P: SParams) -> Optional[dict]:
    c = df["close"].dropna()
    if len(c) < P.ema_slow:
        return None
    e30, e50, e500 = ema(c, P.ema_fast).iloc[-1], ema(c, P.ema_mid).iloc[-1], ema(c, P.ema_slow).iloc[-1]
    macd = ema(c, 12) - ema(c, 26)
    sig = ema(macd, 9)
    lr = np.log(c / c.shift(1))
    hv = (lr.rolling(20).std() * math.sqrt(252)).dropna().tail(252)
    return dict(close=float(c.iloc[-1]), ema30=float(e30), ema50=float(e50), ema500=float(e500),
                macd=float(macd.iloc[-1]), macd_sig=float(sig.iloc[-1]),
                ok=bool(e30 > e50 and e30 > e500 and macd.iloc[-1] > sig.iloc[-1]),
                hv20=float(hv.iloc[-1]) if len(hv) else None,
                hv_min=float(hv.min()) if len(hv) else None, hv_max=float(hv.max()) if len(hv) else None)


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prob_otm_put(S: float, K: float, T: float, iv: float, r: float) -> Optional[float]:
    """Вероятность, что в экспирацию цена выше страйка (пут истечёт без стоимости), логнормально по IV."""
    if not (S > 0 and K > 0 and T > 0 and iv and iv > 0.01):
        return None
    d2 = (math.log(S / K) + (r - iv * iv / 2) * T) / (iv * math.sqrt(T))
    return ncdf(d2)


def iv_rank_own(hist: Dict[str, float], iv: float, min_days: int) -> Optional[float]:
    vals = list(hist.values())
    if len(vals) < min_days:
        return None
    lo, hi = min(vals + [iv]), max(vals + [iv])
    return 100.0 if hi <= lo else (iv - lo) / (hi - lo) * 100


def iv_rank_proxy(iv: float, hv_min: Optional[float], hv_max: Optional[float]) -> Optional[float]:
    """Замена: где текущая IV в годовом диапазоне исторической волатильности (0–100, может выйти за края)."""
    if iv is None or hv_min is None or hv_max is None or hv_max <= hv_min:
        return None
    return (iv - hv_min) / (hv_max - hv_min) * 100


def eulerpool_ranks(tickers: List[str]) -> Dict[str, float]:
    """IV Rank у Eulerpool, если задан ключ. Формат ответа проверяется при первом запуске с ключом."""
    key = os.environ.get("EULERPOOL_KEY", "")
    if not key or not tickers:
        return {}
    url = os.environ.get("EULERPOOL_URL", "https://api.eulerpool.com/v1/volatility/{ticker}/iv-rank")
    out = {}
    for t in tickers:
        try:
            r = requests.get(url.format(ticker=t), headers={"Authorization": f"Bearer {key}"}, timeout=20)
            j = r.json()
            v = None
            for k in ("iv_rank", "ivRank", "iv_rank_30d", "rank"):
                if isinstance(j, dict) and j.get(k) is not None:
                    v = float(j[k])
                    break
                if isinstance(j, dict) and isinstance(j.get("data"), dict) and j["data"].get(k) is not None:
                    v = float(j["data"][k])
                    break
            if v is not None:
                out[t] = v * 100 if v <= 1.0 else v
            elif len(out) == 0:
                log.warning("Eulerpool %s: непонятный ответ %s", t, str(j)[:300])
        except Exception as e:
            log.warning("Eulerpool %s: %s", t, e)
        time.sleep(0.2)
    return out


def _num(x) -> Optional[float]:
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except Exception:
        return None


# ======================================================================== кэши
def _load(f: Path) -> dict:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(f: Path, d: dict) -> None:
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")


def analyst_and_earnings(tickers: List[str], today: str) -> Dict[str, dict]:
    """Рекомендации аналитиков и дата отчёта — раз в день (кэш cache/samurai_info.json)."""
    import yfinance as yf
    import seasonal
    cache = _load(INFO_FILE)
    todo = [t for t in tickers if cache.get(t, {}).get("date") != today]
    log.info("аналитики/отчёты: запрашиваю %d из %d", len(todo), len(tickers))
    for t in todo:
        rec = {"date": today}
        for attempt in range(2):
            try:
                info = yf.Ticker(t).info or {}
                rec.update(rec_mean=_num(info.get("recommendationMean")), rec_key=info.get("recommendationKey"),
                           analysts=info.get("numberOfAnalystOpinions"), mcap=_num(info.get("marketCap")),
                           name=info.get("shortName"))
                break
            except Exception as e:
                rec["error"] = str(e)[:120]
                time.sleep(3)
        ed = seasonal.next_earnings(t)
        rec["earnings"] = ed.isoformat() if ed else None
        cache[t] = rec
        time.sleep(0.3)
    _save(INFO_FILE, cache)
    return cache


# ======================================================================== опционы
def options_for(t: str, tech: dict, info: dict, P: SParams, today: pd.Timestamp,
                prev_vol: Optional[int], iv_hist: Dict[str, float]) -> dict:
    import yfinance as yf
    tk = yf.Ticker(t)
    exps = list(tk.options or ())
    if not exps:
        raise RuntimeError("пустой список экспираций")
    S = tech["close"]
    d0 = today.normalize()
    near = [e for e in exps if 0 <= (pd.Timestamp(e) - d0).days <= P.vol_days]
    earn = info.get("earnings")
    vol = 0
    iv30, iv30_gap = None, 1e9
    puts = []
    for e in near:
        dte = (pd.Timestamp(e) - d0).days
        ch = tk.option_chain(e)
        for side in (ch.calls, ch.puts):
            if side is not None and "volume" in side:
                vol += int(pd.to_numeric(side["volume"], errors="coerce").fillna(0).sum())
        # IV30: экспирация ближе всего к 30 дням, страйк ближе всего к цене, среднее колла и пута
        if abs(dte - 30) < iv30_gap and dte >= 7:
            ivs = []
            for side in (ch.calls, ch.puts):
                if side is not None and len(side):
                    row = side.iloc[(side["strike"] - S).abs().argsort()[:1]]
                    v = _num(row["impliedVolatility"].iloc[0])
                    if v and 0.02 < v < 5:
                        ivs.append(v)
            if ivs:
                iv30, iv30_gap = float(np.mean(ivs)), abs(dte - 30)
        if not (P.dte_min <= dte <= P.dte_max):
            continue
        before = bool(earn and e < earn)
        for _, r in ch.puts.iterrows():
            K, bid, ask = _num(r.get("strike")), _num(r.get("bid")), _num(r.get("ask"))
            iv = _num(r.get("impliedVolatility"))
            if not K or K >= S or not bid or not ask or bid < P.bid_min or ask < bid:
                continue
            prob = prob_otm_put(S, K, dte / 365, iv or 0, P.rate)
            ret = bid / (K - bid) * 100
            puts.append(dict(exp=e, dte=dte, strike=K, bid=bid, ask=ask, spread=round(ask - bid, 2),
                             iv=iv, prob=prob, ret=ret, ann=ret * 365 / dte, otm=(K / S - 1) * 100,
                             oi=int(_num(r.get("openInterest")) or 0), volume=int(_num(r.get("volume")) or 0),
                             before_earn=before))
    vol_use = max(vol, prev_vol or 0)
    return dict(vol=vol, vol_use=vol_use, iv30=iv30, puts=puts)


def pick(c: dict, P: SParams) -> Optional[dict]:
    ok = [p for p in c["puts"] if p["prob"] is not None and p["prob"] >= P.prob_min
          and p["spread"] < P.spread_max and (p["before_earn"] or not c.get("earnings"))]
    if not ok:
        return None
    return max(ok, key=lambda p: p["ann"])


def why_not(c: dict, P: SParams) -> str:
    ps = c["puts"]
    if not ps:
        return f"нет путов ниже цены с бидом ≥ {P.bid_min} на {P.dte_min}–{P.dte_max} дней"
    if c.get("earnings") and not any(p["before_earn"] for p in ps):
        return f"все экспирации {P.dte_min}–{P.dte_max} дн. после отчёта {c['earnings']}"
    if not any(p["prob"] is not None and p["prob"] >= P.prob_min for p in ps):
        return f"нет страйка с вероятностью ≥ {P.prob_min:.0%}"
    return f"спред бид/аск ≥ {P.spread_max}"


# ======================================================================== прогон
def run(P: SParams, tickers: Optional[List[str]] = None) -> dict:
    import seasonal
    now = pd.Timestamp.now(tz=ET)
    today = now.tz_localize(None)
    tstr = str(today.date())
    funnel = {}
    names = {}
    if tickers is None:
        u = seasonal.all_us_stocks()
        funnel["all"] = len(u)
        if u["mcap"].notna().sum():
            u = u[u["mcap"] >= P.mcap_min]
        names = dict(zip(u["ticker"], u["name"]))
        tickers = u["ticker"].tolist()
        funnel["mcap"] = len(tickers)
    hist = seasonal.download(sorted(set(tickers)), 5)        # 5 лет — чтобы EMA 500 сошлась, как в TradingView
    funnel["history"] = len(hist)

    tech = {}
    for t, df in hist.items():
        try:
            x = technicals(df.dropna(subset=["close"]), P)
        except Exception as e:
            log.warning("%s: %s", t, e)
            continue
        if x and x["ok"]:
            tech[t] = x
    funnel["technicals"] = len(tech)
    log.info("EMA/MACD прошли %d из %d", len(tech), len(hist))

    info = analyst_and_earnings(sorted(tech), tstr)
    good = []
    for t in sorted(tech):
        i = info.get(t, {})
        if i.get("mcap") is not None and i["mcap"] < P.mcap_min:
            continue
        if i.get("rec_mean") is None or i["rec_mean"] > P.analyst_max:
            continue
        good.append(t)
    funnel["analysts"] = len(good)
    log.info("аналитики Buy и выше: %d", len(good))

    vols = _load(VOL_FILE)
    ivh = _load(IV_FILE)
    time.sleep(30 if len(hist) > 100 else 0)       # после массовой загрузки Yahoo временно режет опционы
    cands: Dict[str, dict] = {}

    def one(t):
        prev = vols.get(t, {})
        prev_vol = prev.get("vol") if prev.get("date") and prev["date"] < tstr else None
        for attempt in range(3):
            try:
                c = options_for(t, tech[t], info.get(t, {}), P, today, prev_vol, ivh.get(t, {}))
                c.update(ticker=t, **{k: tech[t][k] for k in ("close", "hv20", "hv_min", "hv_max")},
                         earnings=info.get(t, {}).get("earnings"), rec_mean=info[t].get("rec_mean"),
                         rec_key=info[t].get("rec_key"), analysts=info[t].get("analysts"),
                         mcap=info[t].get("mcap"), name=info[t].get("name") or names.get(t))
                cands[t] = c
                return True
            except Exception as e:
                log.warning("%s (попытка %d): %s", t, attempt + 1, e)
                time.sleep(4 * (attempt + 1))
        return False

    for t in good:
        one(t)
        time.sleep(0.4)
    for rnd in range(2):
        miss = [t for t in good if t not in cands]
        if not miss:
            break
        time.sleep(60)
        for t in miss:
            one(t)
    funnel["options_ok"] = len(cands)
    funnel["options_failed"] = [t for t in good if t not in cands]

    # объём и история IV
    for t, c in cands.items():
        if c["vol"]:
            old = vols.get(t, {})
            if old.get("date") != tstr or c["vol"] > old.get("vol", 0):
                vols[t] = {"date": tstr, "vol": c["vol"]}
        if c["iv30"]:
            h = ivh.setdefault(t, {})
            h[tstr] = round(c["iv30"], 4)
            if len(h) > 400:
                for k in sorted(h)[:-400]:
                    h.pop(k)
    _save(VOL_FILE, vols)
    _save(IV_FILE, ivh)

    ep = eulerpool_ranks(sorted(cands))
    rows, rest = [], []
    for t, c in cands.items():
        if t in ep:
            c["ivr"], c["ivr_src"] = ep[t], "eulerpool"
        else:
            own = {k: v for k, v in ivh.get(t, {}).items() if k != tstr}
            r = iv_rank_own(own, c["iv30"], P.ivr_min_days) if c["iv30"] else None
            if r is not None:
                c["ivr"], c["ivr_src"] = r, "own"
            else:
                c["ivr"], c["ivr_src"] = iv_rank_proxy(c["iv30"], c["hv_min"], c["hv_max"]), "hv"
        if c["vol_use"] < P.opt_vol_min:
            c["reason"] = f"объём опционов {c['vol_use']:,} < {P.opt_vol_min:,}".replace(",", " ")
        elif c["ivr"] is None or c["ivr"] < P.ivr_min:
            c["reason"] = ("IV Rank н/д" if c["ivr"] is None else f"IV Rank {c['ivr']:.0f} < {P.ivr_min:.0f}")
        else:
            best = pick(c, P)
            if best:
                c["best"] = best
            else:
                c["reason"] = why_not(c, P)
        n_puts = len(c["puts"])
        c["puts"] = sorted([p for p in c["puts"] if p["prob"] and p["prob"] >= P.prob_min - 0.1],
                           key=lambda p: -p["ann"])[:8]
        c["n_puts"] = n_puts
        (rows if c.get("best") else rest).append(c)
    funnel["final"] = len(rows)
    rows.sort(key=lambda c: -c["best"]["ann"])
    rest.sort(key=lambda c: c["ticker"])
    return dict(made=pd.Timestamp.now(tz="UTC").isoformat(), et=now.strftime("%Y-%m-%d %H:%M"),
                params=asdict(P), funnel=funnel, rows=rows, rest=rest,
                iv_days=max((len(v) for v in ivh.values()), default=0), eulerpool=bool(ep))


# ======================================================================== сообщение
def _f(x, d=2):
    return "—" if x is None else f"{x:,.{d}f}".replace(",", " ")


def ddmm(iso: Optional[str]) -> str:
    return f"{iso[8:10]}.{iso[5:7]}" if iso else "н/д"


def build_message(res: dict, top: int = 20) -> str:
    P = res["params"]
    fn = res["funnel"]
    src = ("Eulerpool" if res.get("eulerpool") else
           f"своя история IV ({res['iv_days']} дн.)" if res["iv_days"] >= P["ivr_min_days"] else
           "≈ по исторической волатильности (своя история IV копится)")
    head = [f"🗡 <b>Самурай — продажа путов</b> · {res['et'][8:10]}.{res['et'][5:7]} {res['et'][11:]} NY",
            f"<i>Кап. ≥ $2 млрд · EMA30 &gt; EMA50 и EMA500 · MACD &gt; сигнальной · аналитики Buy+ · "
            f"объём опц. ≥ {P['opt_vol_min']} · IVR ≥ {P['ivr_min']:.0f} · вероятность ≥ {P['prob_min']:.0%} · "
            f"{P['dte_min']}–{P['dte_max']} дн. до отчёта · спред &lt; ${P['spread_max']}</i>",
            f"Воронка: {fn.get('mcap', fn.get('history'))} → EMA/MACD {fn['technicals']} → аналитики {fn['analysts']} "
            f"→ итог <b>{fn['final']}</b>. IV Rank: {src}."]
    if not res["rows"]:
        return "\n".join(head + ["", "Подходящих путов нет."])
    lines = head + [""]
    for c in res["rows"][:top]:
        b = c["best"]
        ivr = f"{'≈' if c['ivr_src'] == 'hv' else ''}{c['ivr']:.0f}"
        lines.append(
            f"<b>{c['ticker']}</b> {_f(c['close'])} · пут <b>{_f(b['strike'], 1 if b['strike'] % 1 else 0)}</b> "
            f"({b['otm']:+.0f}%) до {ddmm(b['exp'])} ({b['dte']} дн.)\n"
            f"   бид {_f(b['bid'])} / аск {_f(b['ask'])} · вероятность {b['prob'] * 100:.0f}% · "
            f"доход {b['ret']:.2f}% (<b>{b['ann']:.1f}% год.</b>)\n"
            f"   IVR {ivr} · объём {c['vol_use']:,} · отчёт {ddmm(c.get('earnings'))}".replace(",", " "))
    if len(res["rows"]) > top:
        lines.append(f"\n…ещё {len(res['rows']) - top} — на сайте")
    lines.append(f"\n🌐 {SITE_URL}\n⚠️ Продажа пута — обязательство купить акции по страйку. Не инвестиционная рекомендация.")
    return "\n".join(lines)


# ======================================================================== запуск
def slot_now(now: pd.Timestamp) -> Optional[str]:
    """Окна запуска по Нью-Йорку: утро 10:20–11:50, вечер 14:40–15:50 (пн–пт)."""
    if now.weekday() >= 5:
        return None
    m = now.hour * 60 + now.minute
    if 620 <= m <= 710:
        return "am"
    if 880 <= m <= 950:
        return "pm"
    return None


def main():
    import bot
    import tg
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tg", action="store_true")
    ap.add_argument("--tickers")
    ap.add_argument("--out")
    ap.add_argument("--slot", action="store_true", help="запуск по расписанию: только в окна утра/вечера, один раз")
    a = ap.parse_args()
    P = SParams.from_env()
    gh_out = os.environ.get("GITHUB_OUTPUT")

    slot_id = None
    if a.slot:
        now = pd.Timestamp.now(tz=ET)
        s = slot_now(now)
        slot_id = f"{now.date()}-{s}" if s else None
        done = _load(OUT_FILE).get("slot")
        if not s or done == slot_id:
            log.info("не время запуска или уже посчитано (%s, сейчас %s NY) — пропускаю", done, now.strftime("%H:%M"))
            if gh_out:
                with open(gh_out, "a") as fh:
                    fh.write("skip=true\n")
            return

    tickers = [x.strip().upper() for x in a.tickers.split(",")] if a.tickers else None
    res = run(P, tickers)
    res["slot"] = slot_id
    out = BASE / a.out if a.out else OUT_FILE
    _save(out, res)
    msg = build_message(res, P.top)
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if a.no_tg or not token or a.tickers:
        print(msg)
        return
    k = tg.broadcast_one(token, tg.load(), msg, key="samurai")
    log.info("самурай разослан подписчикам: %d", k)


if __name__ == "__main__":
    main()
