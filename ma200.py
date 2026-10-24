"""
«Своя» скользящая: у каждой акции ищем среднюю, от которой цена исторически отскакивает,
и показываем, насколько цена сейчас к ней близко (в ATR и %).

Перебор: EMA и SMA 100 / 150 / 200, отдельно на неделях (W) и днях (D).

Касание (по истории):
  • цена подходит сверху — предыдущие APPROACH свечей закрывались выше средней;
  • средняя растёт (MA сейчас выше, чем SLOPE свечей назад);
  • минимум свечи дошёл до средней: Low ≤ MA + 0,25 ATR.
  Следующее касание считается только после того, как цена снова ушла от средней дальше 1,5 ATR.
Отскок — после касания за HOLD свечей цена поднялась на 2 ATR от минимума свечи касания
  раньше, чем закрылась ниже MA − 1 ATR. Иначе — пробой (не отскок).
«Своя» средняя — с лучшей долей отскоков (сглаженно: (отскоки+1)/(касания+2)), при минимуме касаний
  MIN_TOUCH и доле отскоков ≥ MIN_WIN.

Сигнал — цена (закрытие) сейчас не дальше 1,5 ATR над своей средней (или под ней не глубже 1 ATR).
ATR(14) — как в TradingView (RMA), считается на том же таймфрейме. Неделя — пятничные недели из дневок,
текущая незакрытая неделя участвует только в расчёте расстояния «сейчас».

Отбор: все акции США (цена ≥ $5, оборот ≥ $5 млн в день) + крупные ETF, OI опционов ≥ 1000.

Запуск (по будням после закрытия, .github/workflows/ma200.yml):
  python ma200.py                    — полный прогон, Telegram (только новые подходы), cache/ma200.json
  python ma200.py --no-tg            — без Telegram
  python ma200.py --tickers CAT,FTNT,ABBV --no-tg --out cache/ma200_test.json
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "ma200.json"
SENT_FILE = BASE / "cache" / "ma200_sent.json"
ET = "America/New_York"
TFS = ("W", "D")
MAS = [(k, n) for n in (100, 150, 200) for k in ("EMA", "SMA")]
ETFS = ["SPY", "QQQ", "IWM", "DIA", "RSP", "SMH", "SOXX", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP",
        "XLU", "XLB", "XLRE", "XLC", "XBI", "IBB", "KRE", "XHB", "ITB", "GLD", "SLV", "GDX", "TLT", "EEM",
        "EFA", "FXI", "KWEB", "ARKK", "IGV", "XOP", "XME", "TAN", "URA", "IYR", "VNQ", "MDY"]
log = logging.getLogger("ob_bot.ma200")


def _env(name, default, cast=float):
    v = os.environ.get(name)
    return default if v in (None, "") else cast(v)


@dataclass
class Params:
    atr_len: int = 14
    touch_atr: float = 0.25    # касание: Low ≤ MA + 0,25 ATR
    rearm_atr: float = 1.5     # новое касание — после ухода от средней дальше 1,5 ATR
    bounce_atr: float = 2.0    # отскок: рост на 2 ATR от минимума свечи касания
    break_atr: float = 1.0     # пробой: закрытие ниже MA − 1 ATR
    zone_atr: float = 1.5      # сигнал: цена не дальше 1,5 ATR над средней
    below_atr: float = 1.0     # … или под ней не глубже 1 ATR
    rearm_sent: float = 3.0    # повторный сигнал в Telegram — после ухода дальше 3 ATR
    min_touch_w: int = 3
    min_touch_d: int = 5
    min_win: float = 0.7
    min_above: float = 0.7    # сейчас: доля закрытий над средней за последние 26 недель / 60 дней
    years: int = 21
    min_price: float = 5.0
    min_dvol: float = 5e6
    oi_min: int = 1000
    top: int = 40

    @classmethod
    def from_env(cls) -> "Params":
        p = cls()
        for k, v in asdict(p).items():
            setattr(p, k, _env("MA200_" + k.upper(), v, type(v)))
        return p


# свечей на таймфрейм: подход сверху, наклон средней, сколько ждать отскока
TFP = {"W": dict(approach=4, slope=8, hold=13, recent=26), "D": dict(approach=10, slope=20, hold=40, recent=60)}


# ======================================================================== индикаторы
def rma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def atr(h, l, c, n: int) -> np.ndarray:
    tr = np.empty(len(h))
    if len(h):
        tr[0] = h[0] - l[0]
        pc = c[:-1]
        tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    return rma(tr, n)


def moving(c: pd.Series, kind: str, n: int) -> np.ndarray:
    if kind == "EMA":
        m = c.ewm(span=n, adjust=False).mean().to_numpy().copy()
        m[:n - 1] = np.nan
        return m
    return c.rolling(n).mean().to_numpy().copy()


def touches(df: pd.DataFrame, ma: np.ndarray, at: np.ndarray, P: Params, tf: str, closed: int) -> List[dict]:
    """Касания средней сверху и их исход. closed — сколько свечей закрыто (последняя может быть незакрытой)."""
    T = TFP[tf]
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    n = len(c)
    ap, sl = T["approach"], T["slope"]
    with np.errstate(invalid="ignore"):
        above = np.nan_to_num(c > ma).astype(np.int64)
        far = np.nan_to_num(c > ma + P.rearm_atr * at).astype(np.int64)
        near = l <= ma + P.touch_atr * at
        rising = np.zeros(n, bool)
        rising[sl:] = ma[sl:] > ma[:-sl]
    ca = np.concatenate([[0], np.cumsum(above)])
    cf = np.concatenate([[0], np.cumsum(far)])
    idx = np.arange(n)
    came = np.zeros(n, bool)
    came[ap:] = (ca[idx[ap:]] - ca[idx[ap:] - ap]) == ap          # предыдущие ap свечей закрылись выше MA
    cand = np.nonzero(near & came & rising & (at > 0))[0]
    cand = cand[cand < closed]
    out = []
    last = -1
    for i in cand:
        if last >= 0 and cf[i] - cf[last + 1] == 0:                  # после прошлого касания не уходила далеко
            continue
        last = i
        m, a = ma[i], at[i]
        end = min(closed, i + T["hold"] + 1)
        res, bars = None, None
        for j in range(i, end):
            if j > i and c[j] < ma[j] - P.break_atr * at[j]:
                res = False
                break
            if h[j] >= l[i] + P.bounce_atr * a:
                res, bars = True, int(j - i)
                break
        if res is None and i + T["hold"] < closed:
            res = False
        out.append(dict(i=int(i), date=str(df.index[i].date()), ok=res, bars=bars,
                        depth=round(float((m - l[i]) / a), 2),
                        rise=round(float(h[i:end].max() / l[i] - 1) * 100, 1)))
    # Пробои, которые не попали в касания (гэп вниз, медленное сползание, повторный тест без ухода вверх):
    # закрытие ниже MA − 1 ATR, хотя недавно (2·APPROACH свечей) цена закрывалась выше средней.
    with np.errstate(invalid="ignore"):
        brk = c < ma - P.break_atr * at
    covered = [(t["i"], t["i"] + T["hold"]) for t in out if t["ok"] is False]
    for j in np.nonzero(brk)[0]:
        if j >= closed or j < 2 * ap or brk[j - 1] or np.isnan(ma[j]):
            continue
        if ca[j] - ca[j - 2 * ap] == 0:                                # выше средней давно не была
            continue
        if any(a0 <= j <= b0 for a0, b0 in covered):
            continue
        out.append(dict(i=int(j), date=str(df.index[j].date()), ok=False, bars=None, brk=True,
                        depth=round(float((ma[j] - l[j]) / at[j]), 2), rise=0.0))
    out.sort(key=lambda t: t["i"])
    return out


def evaluate(df: pd.DataFrame, P: Params, tf: str, closed: Optional[int] = None) -> Optional[dict]:
    """Лучшая («своя») средняя на таймфрейме и где цена сейчас относительно неё."""
    closed = len(df) if closed is None else closed
    if closed < 260:
        return None
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    at = atr(h, l, c, P.atr_len)
    min_touch = P.min_touch_w if tf == "W" else P.min_touch_d
    best, cands = None, []
    for kind, n in MAS:
        ma = moving(df["close"], kind, n)
        ts = touches(df, ma, at, P, tf, closed)
        done = [t for t in ts if t["ok"] is not None]
        wins = sum(t["ok"] for t in done)
        if len(done) < min_touch:
            continue
        win = wins / len(done)
        score = (wins + 1) / (len(done) + 2)
        cands.append(f"{kind}{n} {wins}/{len(done)}")
        if win >= P.min_win and (best is None or score > best["score"]
                                 or (score == best["score"] and len(done) > best["n"])):
            best = dict(kind=kind, len=n, ma=ma, touches=ts, n=len(done), wins=wins, win=win, score=score)
    if best is None:
        return None
    ma, a, cl = best["ma"][-1], at[-1], c[-1]
    if np.isnan(ma) or np.isnan(a) or a <= 0:
        return None
    # закономерность действует сейчас: средняя растёт и цена в основном держится над ней
    L = TFP[tf]["recent"]
    with np.errstate(invalid="ignore"):
        above_share = float(np.mean(c[-L:] > best["ma"][-L:]))
    rising_now = best["ma"][-1] > best["ma"][-1 - TFP[tf]["slope"]]
    if not rising_now or above_share < P.min_above:
        return None
    done = [t for t in best["touches"] if t["ok"] is not None]
    ok = [t for t in done if t["ok"]]
    slope = (best["ma"][-1] / best["ma"][-1 - TFP[tf]["slope"]] - 1) * 100
    return dict(ma=f"{best['kind']} {best['len']}", ma_val=round(float(ma), 4), price=round(float(cl), 4),
                atr=round(float(a), 4), atr_pct=round(float(a / cl * 100), 2),
                dist_atr=round(float((cl - ma) / a), 2), dist_pct=round(float((cl / ma - 1) * 100), 2),
                low_atr=round(float((l[-1] - ma) / a), 2), slope=round(float(slope), 2),
                n=best["n"], wins=best["wins"], win=round(best["win"], 3),
                rise_avg=round(float(np.mean([t["rise"] for t in ok])), 1) if ok else None,
                bars_avg=round(float(np.mean([t["bars"] for t in ok])), 1) if ok else None,
                depth_med=round(float(np.median([t["depth"] for t in done])), 2),
                last=[[t["date"], t["ok"], t["rise"]] for t in best["touches"][-6:]],
                above=round(above_share, 2), others=cands, bar=str(df.index[-1].date()))


# ======================================================================== свечи
OHLCV = dict(open="first", high="max", low="min", close="last", volume="sum")


def _left(bucket_end: date, now: pd.Timestamp) -> bool:
    today = now.date()
    if bucket_end < today:
        return False
    if today.weekday() < 5 and (now.hour, now.minute) < (16, 15):
        return True
    d = today + timedelta(days=1)
    while d <= bucket_end:
        if d.weekday() < 5:
            return True
        d += timedelta(days=1)
    return False


def weekly(df: pd.DataFrame, now: pd.Timestamp):
    """Недели (пт) и число закрытых недель."""
    w = df.resample("W-FRI").agg(OHLCV).dropna(subset=["open", "close"])
    closed = len(w) - (1 if len(w) and _left(w.index[-1].date(), now) else 0)
    return w, closed


def daily(df: pd.DataFrame, now: pd.Timestamp):
    closed = len(df) - (1 if len(df) and _left(df.index[-1].date(), now) else 0)
    return df, closed


# ======================================================================== прогон
def run(P: Params, tickers: Optional[List[str]] = None, now: Optional[pd.Timestamp] = None,
        batch: int = 80) -> dict:
    import options
    import seasonal
    now = now or pd.Timestamp.now(tz=ET)
    explicit = tickers is not None
    SP = seasonal.Params(min_price=P.min_price, min_dvol=P.min_dvol)
    if tickers is None:
        u = seasonal.all_us_stocks()
        tickers = sorted(set(seasonal.prefilter(u, SP)) | set(ETFS))
        log.info("акций США: %d, после отсева по цене/обороту (+ETF): %d", len(u), len(tickers))
    rows: List[dict] = []
    checked = {tf: 0 for tf in TFS}
    loaded = 0
    for b in range(0, len(tickers), batch):
        part = tickers[b:b + batch]
        hist = seasonal.download(part, P.years)
        loaded += len(hist)
        for t, d in hist.items():
            if not explicit and t not in ETFS and not seasonal.liquid(d, SP):
                continue
            d = d[(d["open"] > 0) & (d["low"] > 0)]
            for tf, (f, closed) in (("W", weekly(d, now)), ("D", daily(d, now))):
                try:
                    r = evaluate(f, P, tf, closed)
                except Exception as e:
                    log.debug("%s %s: %s", t, tf, e)
                    r = None
                if closed >= 260:
                    checked[tf] += 1
                if r:
                    rows.append(dict(r, ticker=t, tf=tf, etf=t in ETFS))
        log.info("MA: %d/%d, закономерностей %d", min(b + batch, len(tickers)), len(tickers), len(rows))
    if not explicit and loaded < 0.5 * len(tickers):
        raise RuntimeError(f"история загрузилась только по {loaded} из {len(tickers)} акций")
    for r in rows:
        r["zone"] = -P.below_atr <= r["dist_atr"] <= P.zone_atr

    oi_dropped = []
    if P.oi_min and rows:
        found = sorted({r["ticker"] for r in rows if r["zone"]})
        try:
            oi = options.refresh(found, budget_s=600)
        except Exception as e:
            log.warning("OI: %s", e)
            oi = options.load()
        keep = []
        for r in rows:
            r["oi"] = options.oi_of(oi, r["ticker"])
            if not r["zone"] or options.passes(oi, r["ticker"], P.oi_min):
                keep.append(r)
            elif r["ticker"] not in oi_dropped:
                oi_dropped.append(r["ticker"])
        rows = keep
    rows.sort(key=lambda r: (not r["zone"], r["dist_atr"]))
    return dict(updated=pd.Timestamp.now(tz="UTC").isoformat(), today=now.date().isoformat(),
                params=asdict(P), mas=[f"{k} {n}" for k, n in MAS], checked=checked, rows=rows,
                oi_dropped=oi_dropped)


# ======================================================================== Telegram
TF_NAME = {"W": "Недели", "D": "Дни"}


def key_of(r: dict) -> str:
    return f"{r['ticker']}|{r['tf']}|{r['ma']}"


def build_message(res: dict, rows: List[dict], top: int = 40) -> str:
    P = res["params"]
    lines = ["📈 <b>Своя скользящая: цена подошла к средней</b>",
             f"<i>Средняя, от которой акция исторически отскакивала (EMA/SMA 100–200), цена ближе "
             f"{P['zone_atr']:g} ATR. «Отскоки» — сколько касаний из скольких закончились ростом на "
             f"{P['bounce_atr']:g} ATR.</i>"]
    for tf in TFS:
        rs = sorted([r for r in rows if r["tf"] == tf], key=lambda r: (-r["win"], r["dist_atr"]))
        if not rs:
            continue
        lines += ["", f"<b>{TF_NAME[tf]}</b>"]
        for r in rs[:top]:
            tv = r["ticker"].replace("-", ".")
            link = (f'<a href="https://www.tradingview.com/chart/?symbol={html.escape(tv)}&amp;interval={tf}">'
                    f'<b>{html.escape(r["ticker"])}</b></a>')
            past = ", ".join(f"{d[8:10]}.{d[5:7]}.{d[2:4]}" for d, ok, _ in r["last"][-3:] if ok)
            lines.append(f"• {link} {r['price']:.2f} · <b>{r['ma']}</b> {r['ma_val']:.2f} · "
                         f"{r['dist_atr']:+.1f} ATR ({r['dist_pct']:+.1f}%)\n"
                         f"   отскоков {r['wins']}/{r['n']} · ср. рост {r['rise_avg'] or 0:.0f}% · "
                         f"ATR {r['atr_pct']:.1f}%" + (f" · были: {past}" if past else ""))
        if len(rs) > top:
            lines.append(f"   … и ещё {len(rs) - top} — на сайте")
    lines += ["", "<i>Полный список — вкладка «Скользящие» на сайте. Не инвестиционная рекомендация.</i>"]
    return "\n".join(lines)


def load_sent() -> Dict[str, str]:
    try:
        return json.loads(SENT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_sent(sent: Dict[str, str]) -> None:
    SENT_FILE.parent.mkdir(exist_ok=True)
    SENT_FILE.write_text(json.dumps(sent, separators=(",", ":")), encoding="utf-8")


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
    ap.add_argument("--if-new", action="store_true", help="пропустить, если сегодня после закрытия уже считали")
    a = ap.parse_args()
    now = pd.Timestamp.now(tz=ET)
    if a.if_new:
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        if prev.get("today") == now.date().isoformat() and prev.get("after_close"):
            log.info("за сегодня уже посчитано — пропускаю")
            out = os.environ.get("GITHUB_OUTPUT")
            if out:
                with open(out, "a") as f:
                    f.write("skip=true\n")
            return
    P = Params.from_env()
    tickers = [x.strip().upper() for x in a.tickers.split(",")] if a.tickers else None
    res = run(P, tickers, now)
    res["after_close"] = (now.hour, now.minute) >= (16, 15)
    sent = {} if a.tickers else load_sent()
    for r in res["rows"]:
        r["new"] = r["zone"] and key_of(r) not in sent
    out = BASE / a.out if a.out else OUT_FILE
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    zone = [r for r in res["rows"] if r["zone"]]
    log.info("закономерностей %d, у средней сейчас %d, новых %d", len(res["rows"]), len(zone),
             sum(r["new"] for r in res["rows"]))
    if a.tickers:
        print(build_message(res, zone, P.top))
        return
    fresh = [r for r in zone if r["new"]]
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if fresh and not a.no_tg and token:
        k = tg.broadcast_one(token, tg.load(), build_message(res, fresh, P.top), key="ma200")
        log.info("разослано подписчикам: %d", k)
    else:
        print(build_message(res, fresh, P.top) if fresh else "новых подходов нет")
    # кто ушёл от средней дальше 3 ATR — снова может прислать сигнал при следующем подходе
    for r in res["rows"]:
        k = key_of(r)
        if r["zone"]:
            sent.setdefault(k, res["today"])
        elif r["dist_atr"] > P.rearm_sent or r["dist_atr"] < -P.below_atr - 1:
            sent.pop(k, None)
    save_sent(sent)


if __name__ == "__main__":
    main()
