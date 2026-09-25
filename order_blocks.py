"""
Порт индикатора "Order Block Detector [LuxAlgo]" (Pine Script v5) на Python.

Оригинал: © LuxAlgo, лицензия CC BY-NC-SA 4.0 (только некоммерческое использование).

Логика 1-в-1 с Pine:
  upper/lower   = ta.highest(length) / ta.lowest(length)          (high/low последних length баров, включая текущий)
  os            = high[length] > upper ? 0 : low[length] < lower ? 1 : os[1]
  phv           = ta.pivothigh(volume, length, length)             (пик объёма на баре length назад)
  бычий OB      = phv and os == 1 -> top = hl2[length], btm = low[length]
  медвежий OB   = phv and os == 0 -> top = high[length], btm = hl2[length]
  пробитие (mitigation):
      Wick  : бычий — lowest(low, length)  < btm ; медвежий — highest(high, length)  > top
      Close : бычий — lowest(close, length) < btm ; медвежий — highest(close, length) > top

Дополнительно (в оригинале нет, нужно для бота):
  "касание" — бар зашёл в зону OB, при этом предыдущий бар был вне зоны
      бычий:   low[i]  <= top  и low[i-1]  > top
      медвежий: high[i] >= btm и high[i-1] < btm
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass
class OrderBlock:
    side: str                 # 'bull' | 'bear'
    top: float
    btm: float
    ob_time: pd.Timestamp     # время свечи ордер-блока (как left у box в Pine)
    detected_time: pd.Timestamp  # время бара, на котором OB подтвердился (через length баров)
    detected_idx: int

    @property
    def avg(self) -> float:
        return (self.top + self.btm) / 2


@dataclass
class Event:
    type: str                 # 'formed' | 'touched' | 'mitigated'
    ob: OrderBlock
    bar_time: pd.Timestamp
    bar_idx: int
    close: float
    high: float
    low: float


def _is_pivot_high(vol: np.ndarray, c: int, length: int) -> bool:
    """ta.pivothigh(volume, length, length): бар c выше всех length баров слева и справа."""
    if c - length < 0 or c + length >= len(vol):
        return False
    v = vol[c]
    if not np.isfinite(v):
        return False
    left = vol[c - length:c]
    right = vol[c + 1:c + length + 1]
    if not (np.all(np.isfinite(left)) and np.all(np.isfinite(right))):
        return False
    return bool(v > left.max() and v > right.max())


def detect(df: pd.DataFrame, length: int = 5, mitigation: str = "Wick",
           track_last: int = 3):
    """
    Прогоняет индикатор по всей истории.

    df: колонки open, high, low, close, volume; индекс — время открытия бара, по возрастанию.
    track_last: как bull_ext_last / bear_ext_last в Pine — сколько последних OB каждой стороны
                "видно" на графике. Касания/пробития сообщаются только для них.
                0 = следить за всеми непробитыми.

    Возвращает (events, active_bull, active_bear); списки активных OB — новые первыми.
    """
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    t = df.index
    n = len(df)
    use_close = mitigation.lower() == "close"

    bulls: List[OrderBlock] = []
    bears: List[OrderBlock] = []
    events: List[Event] = []
    os_ = 0

    def ev(kind, ob, i):
        events.append(Event(kind, ob, t[i], i, c[i], h[i], l[i]))

    for i in range(n):
        if i < length:
            continue
        win = slice(i - length + 1, i + 1)
        upper = h[win].max()
        lower = l[win].min()
        if use_close:
            target_bull = c[win].min()
            target_bear = c[win].max()
        else:
            target_bull = lower
            target_bear = upper

        k = i - length
        if h[k] > upper:
            os_ = 0
        elif l[k] < lower:
            os_ = 1

        # --- касания/пробития уже существующих OB (до добавления новых) ---
        visible_bull = bulls if track_last <= 0 else bulls[:track_last]
        visible_bear = bears if track_last <= 0 else bears[:track_last]

        keep = []
        for ob in bulls:
            if target_bull < ob.btm:
                if ob in visible_bull:
                    ev("mitigated", ob, i)
                continue
            if ob in visible_bull and l[i] <= ob.top and l[i - 1] > ob.top:
                ev("touched", ob, i)
            keep.append(ob)
        bulls = keep

        keep = []
        for ob in bears:
            if target_bear > ob.top:
                if ob in visible_bear:
                    ev("mitigated", ob, i)
                continue
            if ob in visible_bear and h[i] >= ob.btm and h[i - 1] < ob.btm:
                ev("touched", ob, i)
            keep.append(ob)
        bears = keep

        # --- новые OB ---
        if _is_pivot_high(v, k, length):
            hl2 = (h[k] + l[k]) / 2
            if os_ == 1:
                ob = OrderBlock("bull", hl2, l[k], t[k], t[i], i)
                if not target_bull < ob.btm:        # в Pine сразу пробитый OB удаляется тем же баром
                    bulls.insert(0, ob)
                    ev("formed", ob, i)
            elif os_ == 0:
                ob = OrderBlock("bear", h[k], hl2, t[k], t[i], i)
                if not target_bear > ob.top:
                    bears.insert(0, ob)
                    ev("formed", ob, i)

    return events, bulls, bears


def events_on_last_bar(df: pd.DataFrame, **kw) -> List[Event]:
    events, _, _ = detect(df, **kw)
    if not len(df):
        return []
    last = df.index[-1]
    return [e for e in events if e.bar_time == last]
