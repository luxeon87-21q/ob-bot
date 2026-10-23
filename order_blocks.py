"""
Ордер-блоки по индикатору "Smart Money Concepts [LuxAlgo]" (Pine Script v5), перенос на Python.

Оригинал: © LuxAlgo, лицензия CC BY-NC-SA 4.0 (только некоммерческое использование).

Берётся только то, что нужно для ордер-блоков (настройки индикатора по умолчанию):
  • структура: swing (длина 50) и internal (длина 5) — функции leg / getCurrentStructure;
  • OB создаётся, когда закрытие пробивает последний swing/internal pivot (BOS или CHoCH):
      бычий  — close пересекает вверх pivot high  → OB = свеча с минимальным parsedLow
               между pivot и текущей свечой (текущая не входит);
      медвежий — close пересекает вниз pivot low  → OB = свеча с максимальным parsedHigh;
    parsedHigh/parsedLow: у «волатильной» свечи (high − low ≥ 2 × ATR(200)) high и low меняются местами;
  • internal-пробой учитывается, только если internal pivot не совпадает со swing pivot
    (как extraCondition в Pine);
  • OB удаляется (mitigation), «High/Low»: бычий — low < низа OB, медвежий — high > верха OB;
    «Close» — то же по закрытию;
  • на графике видно по 5 последних OB каждого вида (internal и swing, обе стороны вместе).

Дополнительно (в оригинале нет, нужно для бота):
  «касание» — свеча зашла в зону видимого OB, а предыдущая была вне зоны:
      бычий:    low[i]  <= top  и low[i-1]  > top
      медвежий: high[i] >= btm  и high[i-1] < btm
  «пробит» сообщается для видимых OB (как алерт Pine, но только для блоков на графике).

Интерфейс прежний: detect(df, length, mitigation, track_last) -> (events, bulls, bears),
у каждого OB есть поле kind: 'internal' | 'swing'.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import numpy as np
import pandas as pd

SWING_LENGTH = 50      # swingsLengthInput
INTERNAL_LENGTH = 5    # getCurrentStructure(5, false, true)
ATR_LENGTH = 200       # ta.atr(200)
MAX_STORED = 100       # if orderBlocks.size() >= 100 → pop


@dataclass(eq=False)
class OrderBlock:
    side: str                 # 'bull' | 'bear'
    top: float
    btm: float
    ob_time: pd.Timestamp     # время свечи ордер-блока (левый край box в Pine)
    detected_time: pd.Timestamp  # свеча, на которой OB появился (пробой структуры)
    detected_idx: int
    kind: str = "internal"    # 'internal' | 'swing'
    bar_high: float = field(default=float("nan"), repr=False)   # parsedHigh свечи OB (как в Pine)
    bar_low: float = field(default=float("nan"), repr=False)    # parsedLow свечи OB

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


class _Pivot:
    __slots__ = ("current", "last", "crossed", "bar_index")

    def __init__(self):
        self.current = float("nan")
        self.last = float("nan")
        self.crossed = False
        self.bar_index = -1


def _atr(h, l, c, n=ATR_LENGTH):
    """ta.atr(n) = ta.rma(ta.tr(true), n): первое значение — SMA первых n TR, далее RMA."""
    m = len(h)
    tr = np.empty(m)
    if m:
        tr[0] = h[0] - l[0]
    if m > 1:
        pc = c[:-1]
        tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    atr = np.full(m, np.nan)
    if m >= n:
        atr[n - 1] = tr[:n].mean()
        for i in range(n, m):
            atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def _legs(h, l, size):
    """leg(size) из Pine: 0 — медвежья нога (новый high size назад), 1 — бычья (новый low)."""
    m = len(h)
    leg = np.zeros(m, dtype=np.int8)
    cur = 0
    for i in range(m):
        if i >= size:
            hs = h[i - size]
            ls = l[i - size]
            if hs > h[i - size + 1:i + 1].max():
                cur = 0
            elif ls < l[i - size + 1:i + 1].min():
                cur = 1
        leg[i] = cur
    return leg


def detect(df: pd.DataFrame, length: int = INTERNAL_LENGTH, mitigation: str = "Wick",
           track_last: int = 5, swing_length: int = SWING_LENGTH):
    """
    Прогоняет индикатор по всей истории.

    df: колонки open, high, low, close, volume; индекс — время открытия бара, по возрастанию.
    length: длина internal-структуры (в Pine зашита 5); swing_length — swing (Pine: 50).
    mitigation: 'Wick' / 'High/Low' — по теням, 'Close' — по закрытию.
    track_last: сколько последних OB каждого вида «видно» (Pine: 5). Касания и пробития
                сообщаются только для них; 0 — для всех непробитых.

    Возвращает (events, bulls, bears): bulls/bears — видимые непробитые OB обоих видов, новые первыми.
    """
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    t = df.index
    n = len(df)
    use_close = mitigation.lower() == "close"

    atr = _atr(h, l, c)
    with np.errstate(invalid="ignore"):
        hv = (h - l) >= 2 * atr                       # NaN → False, как в Pine
    p_high = np.where(hv, l, h)
    p_low = np.where(hv, h, l)

    leg_sw = _legs(h, l, swing_length)
    leg_in = _legs(h, l, length)

    sw_hi, sw_lo, in_hi, in_lo = _Pivot(), _Pivot(), _Pivot(), _Pivot()
    trend = {"internal": 0, "swing": 0}
    # уровни, с которыми сравнивал ta.crossover/crossunder на прошлой свече
    prev_lvl = {k: float("nan") for k in ("in_hi", "in_lo", "sw_hi", "sw_lo")}
    blocks = {"internal": [], "swing": []}            # как массивы Pine: новые в начале
    events: List[Event] = []

    def ev(kind, ob, i):
        events.append(Event(kind, ob, t[i], i, c[i], h[i], l[i]))

    def update_structure(leg, i, size, hi: _Pivot, lo: _Pivot):
        if i == 0:
            return
        ch = int(leg[i]) - int(leg[i - 1])
        if ch == 0:
            return
        k = i - size
        if ch == 1:          # начало бычьей ноги → pivot low
            lo.last, lo.current, lo.crossed, lo.bar_index = lo.current, l[k], False, k
        else:                # начало медвежьей ноги → pivot high
            hi.last, hi.current, hi.crossed, hi.bar_index = hi.current, h[k], False, k

    def store(kind, piv: _Pivot, side, i, new_obs):
        a, b = piv.bar_index, i
        if side == "bull":
            j = a + int(np.argmin(p_low[a:b]))
        else:
            j = a + int(np.argmax(p_high[a:b]))
        bh, bl = float(p_high[j]), float(p_low[j])
        ob = OrderBlock(side, max(bh, bl), min(bh, bl), t[j], t[i], i, kind, bh, bl)
        arr = blocks[kind]
        if len(arr) >= MAX_STORED:
            arr.pop()
        arr.insert(0, ob)
        new_obs.append(ob)

    def display(kind, i, new_obs):
        internal = kind == "internal"
        hi, lo = (in_hi, in_lo) if internal else (sw_hi, sw_lo)
        kh, kl = ("in_hi", "in_lo") if internal else ("sw_hi", "sw_lo")
        # бычий пробой: close пересекает вверх pivot high
        lvl, plvl = hi.current, prev_lvl[kh]
        prev_lvl[kh] = lvl
        cross = i > 0 and c[i] > lvl and c[i - 1] <= plvl          # NaN → False
        extra = (in_hi.current != sw_hi.current) if internal else True
        if cross and not hi.crossed and extra:
            hi.crossed = True
            trend[kind] = 1
            store(kind, hi, "bull", i, new_obs)
        # медвежий пробой: close пересекает вниз pivot low
        lvl, plvl = lo.current, prev_lvl[kl]
        prev_lvl[kl] = lvl
        cross = i > 0 and c[i] < lvl and c[i - 1] >= plvl
        extra = (in_lo.current != sw_lo.current) if internal else True
        if cross and not lo.crossed and extra:
            lo.crossed = True
            trend[kind] = -1
            store(kind, lo, "bear", i, new_obs)

    for i in range(n):
        # что было видно на графике до этой свечи — для касаний и пробитий
        visible: List[OrderBlock] = []
        for kind in ("internal", "swing"):
            arr = blocks[kind]
            visible += arr if track_last <= 0 else arr[:track_last]

        update_structure(leg_sw, i, swing_length, sw_hi, sw_lo)
        update_structure(leg_in, i, length, in_hi, in_lo)

        new_obs: List[OrderBlock] = []
        display("internal", i, new_obs)
        display("swing", i, new_obs)

        src_bear = c[i] if use_close else h[i]
        src_bull = c[i] if use_close else l[i]
        removed = set()
        for kind in ("internal", "swing"):
            arr = blocks[kind]
            # Pine: for [index, ob] in orderBlocks + remove(index) — после удаления следующий
            # элемент сдвигается на место удалённого и в этой свече не проверяется
            j = 0
            while j < len(arr):
                ob = arr[j]
                if (ob.side == "bear" and src_bear > ob.bar_high) or \
                        (ob.side == "bull" and src_bull < ob.bar_low):
                    removed.add(id(ob))
                    del arr[j]
                j += 1

        for ob in new_obs:
            if id(ob) not in removed:                 # сразу пробитый OB в Pine тут же исчезает
                ev("formed", ob, i)

        for ob in visible:                            # OB, созданные на этой свече, сюда не входят
            if id(ob) in removed:
                ev("mitigated", ob, i)
            elif i > 0:
                if ob.side == "bull" and l[i] <= ob.top and l[i - 1] > ob.top:
                    ev("touched", ob, i)
                elif ob.side == "bear" and h[i] >= ob.btm and h[i - 1] < ob.btm:
                    ev("touched", ob, i)

    bulls: List[OrderBlock] = []
    bears: List[OrderBlock] = []
    for kind in ("internal", "swing"):
        arr = blocks[kind]
        for ob in (arr if track_last <= 0 else arr[:track_last]):
            (bulls if ob.side == "bull" else bears).append(ob)
    bulls.sort(key=lambda o: o.detected_idx, reverse=True)
    bears.sort(key=lambda o: o.detected_idx, reverse=True)
    return events, bulls, bears


def events_on_last_bar(df: pd.DataFrame, **kw) -> List[Event]:
    events, _, _ = detect(df, **kw)
    if not len(df):
        return []
    last = df.index[-1]
    return [e for e in events if e.bar_time == last]
