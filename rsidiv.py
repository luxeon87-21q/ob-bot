"""
Бычья дивергенция RSI(9) с ценой на 4H свечах основной сессии — только крупные ETF (список ETFS ниже).

Основа — стандартный индикатор TradingView «RSI divergence indicator» (Regular Bullish):
  минимумы ищутся НА RSI (ta.pivotlow: lbL свечей слева, lbR справа), цена берётся low той же свечи;
  дивергенция — RSI в новом минимуме выше, чем в предыдущем, а low цены — ниже.

Плюс условия Denis:
  1. Предыдущий минимум RSI (A) — ниже 30.
  2. Новый минимум RSI (B) — тоже ниже 30, но выше A (например, A = 25, B = 29).
  3. low цены на свече B ниже low на свече A.
  4. Между A и B не больше `max_bars` 4H баров (по умолчанию 15).
  5. RSI(9) вместо 14.

Как подтверждается минимум B — несколько вариантов, каждый даёт свой сигнал с меткой:
  «tv»    — как в индикаторе: lbR (5) свечей справа RSI не опускался ниже B. Совпадает с TradingView,
            но сигнал на 5 свечей (2,5 дня) позже минимума;
  «rev»   — разворотный бар: минимумы 2 предыдущих свечей ниже минимума текущей, текущая растущая
            (закрытие выше открытия и выше прошлого закрытия), RSI после B не обновлял минимум;
  «break» — закрытие выше максимумов 2 предыдущих свечей, RSI после B не обновлял минимум.
Ранние варианты (rev, break) ждут подтверждения не дольше lbR свечей после B.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd

# Сканируются только крупные ETF (не акции): индексы, сектора, отрасли, сырьё, облигации, страны
# и самые ликвидные ETF с плечом. Свой список — переменная RSIDIV_TICKERS (через запятую).
ETFS = ["SPY", "QQQ", "IWM", "DIA", "RSP", "MDY",
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
        "SMH", "SOXX", "IGV", "XBI", "IBB", "KRE", "XHB", "ITB", "XOP", "XME", "TAN", "URA", "ARKK",
        "IYR", "VNQ", "GLD", "SLV", "GDX", "USO", "TLT", "HYG", "EEM", "EFA", "FXI", "KWEB",
        "TQQQ", "SOXL", "TNA", "LABU"]

VARIANTS = {"tv": "как в индикаторе TV", "rev": "разворотный бар", "break": "закрытие выше 2 максимумов"}


@dataclass
class DivEvent:
    type: str                # "rsidiv"
    variant: str             # tv / rev / break
    bar_time: pd.Timestamp   # свеча подтверждения (сигнал)
    bar_idx: int
    close: float
    a_idx: int
    a_time: pd.Timestamp
    a_rsi: float
    a_low: float
    b_idx: int
    b_time: pd.Timestamp
    b_rsi: float
    b_low: float
    rsi: float               # RSI на свече сигнала
    bars: int                # расстояние A→B в 4H барах


def rsi(close: np.ndarray, n: int = 9) -> np.ndarray:
    """RSI Уайлдера (как ta.rsi в TradingView)."""
    d = np.diff(close, prepend=close[0])
    up = pd.Series(np.where(d > 0, d, 0.0)).ewm(alpha=1 / n, adjust=False).mean().values
    dn = pd.Series(np.where(d < 0, -d, 0.0)).ewm(alpha=1 / n, adjust=False).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100 - 100 / (1 + up / dn)
    r = np.where(dn == 0, 100.0, r)
    r[:n] = np.nan                                   # прогрев
    return r


def _left_low(r: np.ndarray, m: int, lbL: int) -> bool:
    """RSI на свече m ниже всех lbL свечей слева."""
    if m < lbL:
        return False
    w = r[m - lbL:m]
    return not np.isnan(r[m]) and not np.isnan(w).any() and r[m] < w.min()


def _candle_ok(variant: str, o, h, l, c, i: int) -> bool:
    if i < 2:
        return False
    if variant == "rev":
        return l[i - 1] < l[i] and l[i - 2] < l[i] and c[i] > o[i] and c[i] > c[i - 1]
    if variant == "break":
        return c[i] > max(h[i - 1], h[i - 2])
    return False


def detect(df: pd.DataFrame, period: int = 9, level: float = 30.0, max_bars: int = 15,
           lbL: int = 5, lbR: int = 5, variants=("tv", "rev", "break")) -> List[DivEvent]:
    o, h, l, c = (df[k].values.astype(float) for k in ("open", "high", "low", "close"))
    n = len(c)
    r = rsi(c, period)
    # полностью подтверждённые минимумы RSI (как ta.pivotlow) — кандидаты в A
    full = [m for m in range(lbL, n - lbR) if _left_low(r, m, lbL) and r[m] <= r[m + 1:m + lbR + 1].min()]
    events: List[DivEvent] = []
    for m in range(lbL, n):
        if not _left_low(r, m, lbL):
            continue
        # A — предыдущий подтверждённый минимум RSI (как valuewhen(plFound, …, 1))
        prev = [a for a in full if a < m]
        if not prev:
            continue
        a = prev[-1]
        dist = m - a
        if not (r[a] < r[m] < level and dist <= max_bars and l[m] < l[a]):
            continue
        for v in variants:
            sig = None
            for i in range(m + 1, min(n, m + lbR + 1)):
                if r[i] < r[m]:                      # RSI ушёл ниже — минимум B не состоялся
                    break
                if a + lbR > i:                      # A ещё не был подтверждён на этой свече
                    continue
                if v == "tv":
                    if i == m + lbR:
                        sig = i
                elif _candle_ok(v, o, h, l, c, i):
                    sig = i
                if sig is not None:
                    break
            if sig is None:
                continue
            events.append(DivEvent("rsidiv", v, df.index[sig], sig, float(c[sig]),
                                   a, df.index[a], float(r[a]), float(l[a]),
                                   m, df.index[m], float(r[m]), float(l[m]), float(r[sig]), dist))
    events.sort(key=lambda e: (e.bar_idx, e.variant))
    return events


# ---------------------------------------------------------------- проверочный прогон
STOP_ATR = (0.0, 0.5, 1.0, 1.5)       # стоп: минимум B минус k × ATR(14)
HOLD = 20                              # держим не дольше 20 свечей 4H (~10 торговых дней)


def _atr(h, l, c, n=14):
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(abs(h - prev), abs(l - prev)))
    return pd.Series(tr).rolling(n, min_periods=1).mean().values


def _trade(h, l, c, i, stop):
    """Вход по закрытию свечи i; выход по стопу или через HOLD свечей. Возвращает (доходность %, стоп?)."""
    for j in range(i + 1, i + HOLD + 1):
        if l[j] <= stop:
            o = min(stop, float(c[j - 1]))      # гэп ниже стопа — выходим хуже
            return (o / c[i] - 1) * 100, True
    return (c[i + HOLD] / c[i] - 1) * 100, False


def _stats(vals):
    if not vals:
        return dict(n=0)
    v = np.array(vals, float)
    return dict(n=len(v), win=round(float((v > 0).mean() * 100), 1), avg=round(float(v.mean()), 2),
                med=round(float(np.median(v)), 2))


KEEP_SIGNALS = 4000     # сколько последних сигналов класть в файл для сайта (сводка — по всем)


def report(tickers=None, out=None, recent_bars: int = 20, stocks: bool = False) -> dict:
    """Прогон на всей истории (~2 года 4H): ETF (и акции, если stocks) — все сигналы, что было с ценой
    после них, сравнение со случайным входом и разные стопы."""
    import json
    from pathlib import Path
    import data
    tickers = list(tickers or ETFS)
    if stocks:
        base = Path(__file__).resolve().parent
        tickers += [t for t in data.load_universe("sp500,nasdaq100,file", base / "tickers.txt") if t not in tickers]
    etfset = set(ETFS)
    bars = data.update_bars(tickers)
    rows, last, base_by = [], {}, {}
    rng = np.random.default_rng(7)
    for t, df in bars.items():
        h, l, c = (df[k].values.astype(float) for k in ("high", "low", "close"))
        a = _atr(h, l, c)
        r = rsi(c)
        last[t] = dict(bar=df.index[-1].isoformat(), close=round(float(c[-1]), 2), rsi=round(float(r[-1]), 1), bars=len(df))
        # случайный вход: любая свеча этого ETF (та же доходность через HOLD свечей)
        fwd = (c[HOLD:] / c[:-HOLD] - 1) * 100
        base_by[t] = fwd[30:]
        for e in detect(df):
            i = e.bar_idx
            res = dict(ticker=t, variant=e.variant, bar_time=e.bar_time.isoformat(), close=round(e.close, 2),
                       a_time=e.a_time.isoformat(), a_rsi=round(e.a_rsi, 1), a_low=round(e.a_low, 2),
                       b_time=e.b_time.isoformat(), b_rsi=round(e.b_rsi, 1), b_low=round(e.b_low, 2),
                       bars=e.bars, recent=i >= len(df) - recent_bars, atr=round(float(a[i]), 3), etf=t in etfset)
            for k in (5, 10, 20):
                res[f"ret{k}"] = round((c[i + k] / e.close - 1) * 100, 2) if i + k < len(c) else None
            res["max20"] = round((h[i + 1:i + 21].max() / e.close - 1) * 100, 2) if i + 1 < len(c) else None
            if i + HOLD < len(c):
                res["base20"] = round(float(base_by[t].mean()), 2)        # средний ход этого ETF за 20 свечей
                res["trades"] = {}
                for k in STOP_ATR:
                    stop = e.b_low - k * a[i]
                    ret, hit = _trade(h, l, c, i, stop)
                    risk = (e.close - stop) / e.close * 100
                    res["trades"][str(k)] = dict(ret=round(ret, 2), stop=hit, risk=round(risk, 2),
                                                 R=round(ret / risk, 2) if risk > 0 else None)
            rows.append(res)
    rows.sort(key=lambda r: r["bar_time"])

    def summarize(rr):
        done = [x for x in rr if "trades" in x]
        out_ = dict(signals=len(rr), closed=len(done), hold20=_stats([x["ret20"] for x in done]))
        # случайный вход по тем же ETF: средний ход за 20 свечей, взвешенный как сигналы
        out_["random20"] = _stats([float(v) for x in done for v in rng.choice(base_by[x["ticker"]], 20)])
        if done:
            out_["edge20"] = round(out_["hold20"]["avg"] - out_["random20"]["avg"], 2)
        out_["stops"] = {}
        for k in STOP_ATR:
            tr = [x["trades"][str(k)] for x in done]
            st = _stats([x["ret"] for x in tr])
            if tr:
                st["stopped"] = round(100 * sum(x["stop"] for x in tr) / len(tr), 1)
                st["risk"] = round(float(np.mean([x["risk"] for x in tr])), 2)
                st["avgR"] = round(float(np.mean([x["R"] for x in tr if x["R"] is not None])), 2)
            out_["stops"][f"B-{k}ATR"] = st
        return out_

    summ = {v: summarize([x for x in rows if v == "all" or x["variant"] == v]) for v in ["all"] + list(VARIANTS)}
    # одна дивергенция — один сигнал (самое раннее подтверждение), чтобы не считать её трижды
    first = {}
    for x in rows:
        first.setdefault((x["ticker"], x["b_time"]), x)
    uniq = list(first.values())
    summ["unique"] = summarize(uniq)
    for kind, flag in (("etf", True), ("stock", False)):
        summ["unique_" + kind] = summarize([x for x in uniq if x["etf"] == flag])
        summ["tv_" + kind] = summarize([x for x in rows if x["variant"] == "tv" and x["etf"] == flag])
    # для сайта — последние сигналы, из сделок только стоп под B
    keep = []
    for x in rows[-KEEP_SIGNALS:]:
        y = {k: v for k, v in x.items() if k != "trades"}
        if "trades" in x:
            y["trades"] = {"0.0": x["trades"]["0.0"]}
        keep.append(y)
    res = dict(updated=pd.Timestamp.now(tz="America/New_York").isoformat(), tickers=len(tickers),
               loaded=len(bars), etfs=sum(t in etfset for t in bars), stocks=sum(t not in etfset for t in bars),
               hold=HOLD, total_signals=len(rows), summary=summ, signals=keep,
               last={k: v for k, v in last.items() if k in etfset})
    if out:
        Path(out).write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":"), default=float), encoding="utf-8")
    return res


if __name__ == "__main__":
    import argparse
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Дивергенция RSI(9) — проверочный прогон")
    ap.add_argument("--tickers", default="", help="через запятую; по умолчанию — список ETFS")
    ap.add_argument("--stocks", action="store_true", help="плюс акции S&P 500 + Nasdaq-100 + tickers.txt")
    ap.add_argument("--out", default="cache/rsidiv_test.json")
    a = ap.parse_args()
    r = report([x.strip().upper() for x in a.tickers.split(",") if x.strip()] or None, a.out, stocks=a.stocks)
    print(r["summary"])
    print("сигналов:", r["total_signals"])
