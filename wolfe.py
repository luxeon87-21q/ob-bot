"""
Волны Вульфа (Bill Wolfe) на 4H свечах — по правилам из книги
L. B. Raschke, L. Connors «Street Smarts», глава 15 «Wolfe Waves».

Бычья волна (счёт начинается с вершины):
  2 — вершина;
  3 — дно первого снижения после 2;
  1 — дно перед вершиной 2. Точка 3 обязана быть НИЖЕ точки 1;
  4 — вершина отскока после 3. Точка 4 должна быть ВЫШЕ точки 1 (и ниже 2);
  «Нельзя искать волну, пока точки 1, 2, 3 и 4 не сформированы».
  Линия 1–3, продлённая вправо, показывает зону точки 5.
  5 — дно после 4 у линии 1–3 (часто с небольшим проколом линии).
  Вход — на РАЗВОРОТЕ от точки 5 (не на касании), стоп — сразу под точкой 5.
  Цель — EPA (Estimated Price at Arrival): линия 1–4, продлённая вправо.
Медвежья — зеркально.

Что добавлено к правилам книги, чтобы робот не ловил шум:
  • развороты зигзага подтверждаются `pivot` свечами с каждой стороны,
    а колено меньше `min_leg` × ATR(14) не считается волной (Рашке: волна
    чётко видна только при хорошей волатильности);
  • линии 1–3 и 2–4 сходятся (клин) — так выглядят все примеры главы;
  • разворот = закрытие свечи выше максимума свечи точки 5 (для медвежьей —
    ниже минимума);
  • прокол линии 1–3 глубже `max_over` × ATR — это уже пробой, а не точка 5;
  • EPA должна быть дальше цены входа (иначе цели уже нет).
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
    line13: float             # линия 1–3 на свече точки 5
    target: float             # цель: линия 1–4 (EPA) в момент ETA
    eta: Optional[pd.Timestamp]
    eta_bars: float           # через сколько свечей от сигнала ETA
    p5: float = 0.0           # экстремум точки 5
    p5_idx: int = 0
    p5_time: Optional[pd.Timestamp] = None
    stop: float = 0.0         # стоп — сразу за точкой 5
    epa: float = 0.0          # линия 1–4 на свече сигнала (ближайшая цель)

    def line(self, a: int, b: int, x: float) -> float:
        """Значение прямой через точки a и b (1..4) на свече x."""
        xa, xb = self.idx[a - 1], self.idx[b - 1]
        ya, yb = self.price[a - 1], self.price[b - 1]
        return ya + (yb - ya) * (x - xa) / (xb - xa)


@dataclass
class WolfeEvent:
    type: str                 # "wolfe"
    w: Wolfe
    bar_time: pd.Timestamp    # свеча разворота от точки 5 (сигнал)
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
    return _alternate(raw)


def _alternate(points):
    piv = []
    for p in points:
        if piv and piv[-1][1] == p[1]:            # два максимума подряд — оставляем более крайний
            if (p[1] == "H" and p[2] > piv[-1][2]) or (p[1] == "L" and p[2] < piv[-1][2]):
                piv[-1] = p
        else:
            piv.append(p)
    return piv


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(abs(high - prev), abs(low - prev)))
    return pd.Series(tr).rolling(n, min_periods=1).mean().values


def swings(high, low, L: int, a: np.ndarray, min_leg: float):
    """Зигзаг без мелких колен: колено короче min_leg × ATR выбрасывается."""
    piv = zigzag(high, low, L)
    if min_leg <= 0:
        return piv
    out = []
    for p in piv:
        if out and out[-1][1] == p[1]:
            out = _alternate(out + [p])
        elif out and abs(p[2] - out[-1][2]) < min_leg * a[p[0]]:
            continue                               # мелкий откат — не волна
        else:
            out.append(p)
    return out


def _check(p) -> bool:
    """Бычья структура 1–4 по Рашке (медвежья проверяется на зеркальных ценах)."""
    (i1, _, y1), (i2, _, y2), (i3, _, y3), (i4, _, y4) = p
    if not (y3 < y1 and y1 < y4 < y2):            # 3 ниже 1; 4 выше 1 и ниже 2
        return False
    s13 = (y3 - y1) / (i3 - i1)
    s24 = (y4 - y2) / (i4 - i2)
    return s24 < s13                               # клин: 2–4 падает круче 1–3


def _scan(high, low, close, a, p, L, max_wait, tol, max_over):
    """Ищет точку 5 и разворот от неё для бычьей фигуры p. Возвращает (j, i5) или None."""
    n = len(close)
    (i1, _, y1), (i2, _, y2), (i3, _, y3), (i4, _, y4) = p
    l13 = lambda x: y1 + (y3 - y1) * (x - i1) / (i3 - i1)
    confirm = i4 + L                               # точка 4 известна только теперь
    last = min(n - 1, i4 + int(max(L + 3, max_wait * max(i3 - i2, i4 - i3))))
    i5 = None
    for j in range(i4 + 1, last + 1):
        if i5 is None and high[j] > y4:             # ушли выше 4 до точки 5 — фигуры нет
            return None
        if i5 is None or low[j] < low[i5]:
            if low[j] <= l13(j) + tol * a[j]:       # дошли до линии 1–3
                if low[j] < l13(j) - max_over * a[j]:
                    return None                    # глубокий пробой линии, а не точка 5
                i5 = j
                continue
        if i5 is not None and j >= confirm and close[j] > high[i5]:
            return j, i5                           # разворот: закрытие выше свечи точки 5
    return None


def detect(df: pd.DataFrame, pivot: int = 4, max_wait: float = 2.5, min_leg: float = 1.0,
           tol: float = 0.25, max_over: float = 1.5) -> List[WolfeEvent]:
    """Сигналы «разворот от точки 5» по всей истории df."""
    high, low, close = (df[c].values.astype(float) for c in ("high", "low", "close"))
    a = atr(high, low, close)
    events: List[WolfeEvent] = []
    for side in ("bull", "bear"):
        # медвежья волна = бычья на перевёрнутом графике
        H, Lo, C = (high, low, close) if side == "bull" else (-low, -high, -close)
        piv = swings(H, Lo, pivot, a, min_leg)
        sg = 1 if side == "bull" else -1
        for k in range(3, len(piv)):
            p = piv[k - 3:k + 1]
            if p[0][1] != "L" or not _check(p):
                continue
            hit = _scan(H, Lo, C, a, p, pivot, max_wait, tol, max_over)
            if not hit:
                continue
            j, i5 = hit
            idx = [q[0] for q in p]
            y = [q[2] for q in p]
            s13 = (y[2] - y[0]) / (idx[2] - idx[0])
            s14 = (y[3] - y[0]) / (idx[3] - idx[0])
            s24 = (y[3] - y[1]) / (idx[3] - idx[1])
            epa = y[0] + s14 * (j - idx[0])
            if epa <= C[j]:
                continue                           # цель уже позади — сигнала нет
            x_eta = (y[1] - y[0] + s13 * idx[0] - s24 * idx[1]) / (s13 - s24)
            if x_eta <= j:
                x_eta = j + max(1, idx[3] - idx[0])
            w = Wolfe(side, idx, [sg * v for v in y], [df.index[i] for i in idx],
                      line13=sg * (y[0] + s13 * (i5 - idx[0])),
                      target=sg * (y[0] + s14 * (x_eta - idx[0])), eta=None, eta_bars=x_eta - j,
                      p5=sg * Lo[i5], p5_idx=i5, p5_time=df.index[i5],
                      stop=sg * (Lo[i5] - 0.1 * a[i5]), epa=sg * epa)
            events.append(WolfeEvent("wolfe", w, df.index[j], j, float(close[j]),
                                     float(high[j]), float(low[j])))
    events.sort(key=lambda e: e.bar_idx)
    return events
