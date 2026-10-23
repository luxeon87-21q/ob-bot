"""Проверки сканера MA200: python -m pytest -q test_ma200.py"""
import numpy as np
import pandas as pd

import ma200
from ma200 import Params, analyze, signal_at, weekly, monthly, daily_closed


def _df(close, lows=None, highs=None, opens=None, vols=None, start="2020-01-01", freq="B"):
    c = np.asarray(close, float)
    n = len(c)
    o = np.asarray(opens if opens is not None else np.r_[c[0], c[:-1]], float)
    h = np.asarray(highs if highs is not None else np.maximum(o, c) + 0.5, float)
    l = np.asarray(lows if lows is not None else np.minimum(o, c) - 0.5, float)
    v = np.asarray(vols if vols is not None else np.full(n, 1e6), float)
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=v), index=idx)


def setup(n=400, kind="SMA", depth=0.5, close_off=0.1, bounce=True, slope=0.05):
    """Плавный тренд, на свече n-2 минимум протыкает MA200 на depth ATR, на n-1 — отскок."""
    c = 100 + slope * np.arange(n)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.5
    l = np.minimum(o, c) - 0.5
    P = Params()
    k = n - 2
    for _ in range(30):                       # ATR зависит от самих свечей — подгоняем итерациями
        a = analyze(_df(c, l, h, o), P, kind)
        ma, at = a["ma"], a["atr"]
        c[k - 1] = ma[k - 1] + 0.6
        o[k - 1] = c[k - 2]
        h[k - 1] = max(o[k - 1], c[k - 1]) + 0.2
        l[k - 1] = min(o[k - 1], c[k - 1]) - 0.1
        o[k], c[k] = ma[k] + 0.5, ma[k] + close_off * at[k]
        l[k] = ma[k] - depth * at[k]
        h[k] = max(o[k], c[k]) + 0.3
        o[n - 1] = c[k]
        c[n - 1] = h[k] + (0.5 if bounce else -0.25)
        h[n - 1] = max(o[n - 1], c[n - 1]) + 0.2
        l[n - 1] = min(o[n - 1], c[n - 1]) - 0.2
    return _df(c, l, h, o)


def test_signal_main_trigger():
    df = setup()
    s = signal_at(df, Params(), "SMA")
    assert s and "main" in s["trig"] and s["bars_ago"] == 1, s
    assert 0.4 < s["depth"] < 0.6
    assert s["pierce"] == str(df.index[-2].date())


def test_too_deep_or_closed_below_no_signal():
    assert signal_at(setup(depth=1.3), Params(), "SMA") is None          # глубже 1 ATR
    assert signal_at(setup(close_off=-0.5), Params(), "SMA") is None     # закрылась под MA − 0,3 ATR


def test_falling_ma_no_signal():
    df = setup(slope=-0.05)
    a = analyze(df, Params(), "SMA")
    assert not a["sig"][-1]


def test_alt_trigger_needs_volume():
    df = setup(bounce=False)
    P = Params()
    s = signal_at(df, P, "SMA")
    # закрытие выше MA, но объём обычный — сигнала нет
    assert s is None
    df.iloc[-1, df.columns.get_loc("volume")] = 3e6
    s = signal_at(df, P, "SMA")
    assert s and s["trig"] == ["alt"] and s["vol_ratio"] > 100


def test_ema_needs_200_bars():
    df = setup(n=230, kind="EMA")
    a = analyze(df.iloc[:199], Params(), "EMA")
    assert np.isnan(a["ma"]).all()


def test_closed_bars():
    et = "America/New_York"
    d = _df(np.linspace(10, 20, 30), start="2026-10-01", freq="B")   # чт 01.10 … ср 11.11
    # среда 11.11, 12:00 — дневная свеча не закрыта, неделя тоже
    now = pd.Timestamp("2026-11-11 12:00", tz=et)
    assert daily_closed(d, now).index[-1] == pd.Timestamp("2026-11-10")
    assert weekly(d, now).index[-1] == pd.Timestamp("2026-11-06")
    # пятница 13.11 после закрытия — неделя закрыта
    d2 = _df(np.linspace(10, 20, 32), start="2026-10-01", freq="B")
    now = pd.Timestamp("2026-11-13 17:00", tz=et)
    assert weekly(d2, now).index[-1] == pd.Timestamp("2026-11-13")
    assert daily_closed(d2, now).index[-1] == pd.Timestamp("2026-11-13")
    # месяц: октябрь 2026 кончается в субботу 31-го → закрыт вечером пт 30.10
    m = _df(np.linspace(10, 20, 5), start="2026-06-01", freq="MS")
    assert monthly(m, pd.Timestamp("2026-10-30 12:00", tz=et)).index[-1] == pd.Timestamp("2026-09-01")
    assert monthly(m, pd.Timestamp("2026-10-30 16:30", tz=et)).index[-1] == pd.Timestamp("2026-10-01")


def test_message():
    df = setup()
    s = signal_at(df, Params(), "SMA")
    rows = [dict(s, ticker="AAPL", tf="D", oi=12345), dict(s, ma="EMA", ticker="AAPL", tf="D", oi=12345)]
    res = dict(params=vars(Params()))
    msg = ma200.build_message(res, rows)
    assert msg.count("<b>AAPL</b>") == 1 and "EMA+SMA" in msg


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
    print("ok")
