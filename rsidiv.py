"""
Бычья дивергенция RSI(9) с ценой на 4H свечах основной сессии — только крупные ETF (список ETFS ниже).

Основа — стандартный индикатор TradingView «RSI divergence indicator» (Regular Bullish):
  минимумы ищутся НА RSI (ta.pivotlow: lbL свечей слева, lbR справа), цена берётся low той же свечи;
  дивергенция — RSI в новом минимуме выше, чем в предыдущем, а low цены — ниже.

Плюс условия Denis:
  1. Предыдущий минимум RSI (A) — ниже 30.
  2. Новый минимум RSI (B) — выше A (сам B может быть где угодно, хоть выше 30).
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
        if not (r[a] < level and dist <= max_bars and r[m] > r[a] and l[m] < l[a]):
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
