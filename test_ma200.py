"""Проверки сканера «своей» скользящей: python -m pytest -q test_ma200.py"""
import time

import numpy as np
import pandas as pd

import ma200
from ma200 import Params, touches, evaluate, weekly, daily, atr


def _df(c, h=None, l=None, start="2010-01-01", freq="W-FRI"):
    c = np.asarray(c, float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.5 if h is None else np.asarray(h, float)
    l = np.minimum(o, c) - 0.5 if l is None else np.asarray(l, float)
    idx = pd.date_range(start, periods=len(c), freq=freq)
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=np.full(len(c), 1e6)), index=idx)


def path(n=300, dips=((100, True), (160, True), (220, False))):
    """Цена идёт над растущей линией 100 + 0.2·i, в точках dips опускается к ней:
    True — потом отскок вверх, False — пробой вниз."""
    line = 100 + 0.2 * np.arange(n)
    c = line + 8.0
    for k, good in dips:
        for d in range(6):                          # плавный спуск к линии
            c[k - 6 + d] = line[k - 6 + d] + 8 - 1.3 * (d + 1)
        c[k] = line[k] + 0.3
        if good:
            for d in range(1, 8):
                c[k + d] = line[k + d] + 0.3 + 1.2 * d
        else:
            for d in range(1, 8):
                c[k + d] = line[k + d] - 2.0 * d
            for d in range(8, 30):
                if k + d < n:
                    c[k + d] = line[k + d] - 14 + 0.8 * (d - 8)
    return line, c


def test_touches_bounce_and_break():
    line, c = path()
    df = _df(c)
    at = atr(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), 14)
    ts = touches(df, line, at, Params(), "W", len(df))
    near = lambda k: [t["ok"] for t in ts if k - 2 <= t["i"] <= k]
    assert near(100) == [True] and near(160) == [True] and near(220) == [False], ts


def test_one_touch_per_approach():
    line, c = path(dips=((100, True),))
    c[101:104] = line[101:104] + 0.4                 # несколько свечей подряд у линии — одно касание
    df = _df(c)
    at = atr(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), 14)
    ts = touches(df, line, at, Params(), "W", len(df))
    assert len([t for t in ts if 95 <= t["i"] <= 110]) == 1


def test_evaluate_random_and_speed():
    rng = np.random.default_rng(1)
    n = 5300
    c = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n)))
    df = _df(c, h=c * (1 + rng.random(n) * 0.01), l=c * (1 - rng.random(n) * 0.01), freq="B")
    t0 = time.time()
    P = Params(min_win=0)
    r = evaluate(df, P, "D")
    w, closed = weekly(df, pd.Timestamp("2040-01-01", tz="America/New_York"))
    rw = evaluate(w, P, "W", closed)
    dt = time.time() - t0
    assert dt < 2, dt
    for x in r + rw:
        assert x["ma"] in [f"{k} {n}" for k, n in ma200.MAS] and 0 <= x["win"] <= 1


def test_closed_bars():
    et = "America/New_York"
    d = _df(np.linspace(10, 20, 30), start="2026-10-01", freq="B")   # чт 01.10 … ср 11.11
    now = pd.Timestamp("2026-11-11 12:00", tz=et)
    assert daily(d, now)[1] == len(d) - 1
    w, closed = weekly(d, now)
    assert w.index[closed - 1] == pd.Timestamp("2026-11-06")
    now = pd.Timestamp("2026-11-13 17:00", tz=et)
    d2 = _df(np.linspace(10, 20, 32), start="2026-10-01", freq="B")
    w, closed = weekly(d2, now)
    assert closed == len(w)


def test_message():
    r = dict(ticker="CAT", tf="W", price=702.96, ma="EMA 200", ma_val=492.67, dist_atr=1.2, dist_pct=4.1,
             wins=5, n=6, win=0.83, rise_avg=35.0, atr_pct=9.5, last=[["2022-10-14", True, 40.0]])
    msg = ma200.build_message(dict(params=vars(Params())), [r])
    assert "<b>CAT</b>" in msg and "EMA 200" in msg and "5/6" in msg


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            print("ok", k)
