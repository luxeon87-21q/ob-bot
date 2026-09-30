"""
Волны Вульфа (Bill Wolfe) на 4H свечах.

Бычья волна (разворот вверх):
  1 — минимум, 2 — максимум, 3 — минимум ниже 1, 4 — максимум ниже 2, но выше 1;
  линии 1–3 и 2–4 сходятся вправо (падающий клин: линия 2–4 круче).
  5 — цена доходит до продолжения линии 1–3 («зона точки 5»): тут и шлём сигнал.
  Цель — линия 1–4 в момент пересечения линий 1–3 и 2–4 (ETA).
Медвежья — зеркально.

Точки ищутся зигзагом по разворотам (pivot) с длиной `pivot` свечей с каждой стороны.
Точка 4 подтверждается через `pivot` свечей после неё — раньше сигнал не приходит.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass
class Wolfe:
    side: str                 # bull / bear
    idx: List[int]            # индексы свечей точек 1..4
    price: List[float]        # цены точек 1..4
    times: List[pd.Timestamp]
    line13: float             # значение линии 1–3 на свече сигнала
    target: float             # цель: линия 1–4 в момент ETA
    eta: Optional[pd.Timestamp]
    eta_bars: float           # через сколько свечей от сигнала ETA

    def line(self, a: int, b: int, x: float) -> float:
        """Значение прямой через точки a и b (1..4) на свече x."""
        xa, xb = self.idx[a - 1], self.idx[b - 1]
        ya, yb = self.price[a - 1], self.price[b - 1]
        return ya + (yb - ya) * (x - xa) / (xb - xa)


@dataclass
class WolfeEvent:
    type: str                 # "wolfe"
    w: Wolfe
    bar_time: pd.Timestamp
    bar_idx: int
    close: float
    high: float
    low: float


def zigzag(high: np.ndarray, low: np.ndarray, L: int):
    """Чередующиеся развороты [(idx, 'H'|'L', price)], каждый подтверждён L свечами справа."""
    n = len(high)
    raw = []
    for i in range(L, n - L):
        hwin, lwin = high[i - L:i + L + 1], low[i - L:i + L + 1]
        if high[i] == hwin.max() and high[i] > high[i - L:i].max():
            raw.append((i, "H", float(high[i])))
        if low[i] == lwin.min() and low[i] < low[i - L:i].min():
            raw.append((i, "L", float(low[i])))
    piv = []
    for p in raw:
        if piv and piv[-1][1] == p[1]:            # два максимума подряд — оставляем более крайний
            if (p[1] == "H" and p[2] > piv[-1][2]) or (p[1] == "L" and p[2] < piv[-1][2]):
                piv[-1] = p
        else:
            piv.append(p)
    return piv


def _check(side: str, p) -> bool:
    (i1, _, y1), (i2, _, y2), (i3, _, y3), (i4, _, y4) = p
    if side == "bull":
        ok = y3 < y1 and y3 < y4 < y2 and y4 > y1
    else:
        ok = y3 > y1 and y2 < y4 < y3 and y4 < y1
    if not ok:
        return False
    s13 = (y3 - y1) / (i3 - i1)
    s24 = (y4 - y2) / (i4 - i2)
    # линии сходятся вправо (клин): у бычьей 2–4 падает круче 1–3, у медвежьей 2–4 растёт круче 1–3
    if side == "bull" and not s24 < s13:
        return False
    if side == "bear" and not s24 > s13:
        return False
    # грубая симметрия: волны не отличаются по длительности больше чем в 4 раза
    d12, d23, d34 = i2 - i1, i3 - i2, i4 - i3
    return max(d12, d23, d34) <= 4 * min(d12, d23, d34)


def detect(df: pd.DataFrame, pivot: int = 4, max_wait: float = 2.0) -> List[WolfeEvent]:
    """События «цена дошла до линии 1–3» (зона точки 5) по всей истории df."""
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    piv = zigzag(high, low, pivot)
    events: List[WolfeEvent] = []
    for k in range(3, len(piv)):
        p = piv[k - 3:k + 1]
        side = "bull" if p[0][1] == "L" else "bear"
        if not _check(side, p):
            continue
        idx = [q[0] for q in p]
        price = [q[2] for q in p]
        w = Wolfe(side, idx, price, [df.index[i] for i in idx], 0.0, 0.0, None, 0.0)
        i3, i4 = idx[2], idx[3]
        # точка 5 должна прийти не позже, чем через max_wait × (длительность 3→4) свечей
        last = min(n - 1, i4 + int(max(2, max_wait * (i4 - i3))))
        confirm = i4 + pivot                            # когда точка 4 стала известна
        for j in range(i4 + 1, last + 1):
            l13 = w.line(1, 3, j)
            if side == "bull":
                if high[j] > price[3]:                  # ушли выше точки 4 — фигура сломана
                    break
                hit = low[j] <= l13
            else:
                if low[j] < price[3]:
                    break
                hit = high[j] >= l13
            if not hit:
                continue
            at = max(j, confirm)
            if at >= n:
                break
            if side == "bull" and high[j:at + 1].max() > price[3]:
                break
            if side == "bear" and low[j:at + 1].min() < price[3]:
                break
            # ETA — пересечение линий 1–3 и 2–4; цель — линия 1–4 в этот момент
            s13 = (price[2] - price[0]) / (idx[2] - idx[0])
            s24 = (price[3] - price[1]) / (idx[3] - idx[1])
            x_eta = ((price[1] - price[0] + s13 * idx[0] - s24 * idx[1]) / (s13 - s24)
                     if s13 != s24 else None)
            if x_eta is None or x_eta <= at:
                x_eta = at + max(1, idx[3] - idx[0])   # линии почти параллельны — ориентир на длину фигуры
            w.line13 = l13
            w.target = w.line(1, 4, x_eta)
            w.eta_bars = x_eta - at
            w.eta = None
            events.append(WolfeEvent("wolfe", w, df.index[at], at, float(close[at]),
                                     float(high[at]), float(low[at])))
            break
    return events
