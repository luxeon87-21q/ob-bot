"""Проверки сканера отчётов: python -m pytest -q test_earnings.py"""
from datetime import date

import pandas as pd

import earnings as E


def test_session():
    assert E.session_of(pd.Timestamp("2026-11-03 07:00", tz=E.ET)) == "BMO"
    assert E.session_of(pd.Timestamp("2026-11-03 16:05", tz=E.ET)) == "AMC"
    assert E.session_of(pd.Timestamp("2026-11-03 12:00", tz=E.ET)) == "DMH"
    assert E.session_of(pd.Timestamp("2026-11-03 00:00", tz=E.ET)) is None
    # время в UTC пересчитывается в Нью-Йорк: 21:05 UTC зимой = 16:05 ET
    assert E.session_of(pd.Timestamp("2026-11-03 21:05", tz="UTC")) == "AMC"


def test_reaction_day():
    assert E.reaction_day(date(2026, 11, 3), "BMO") == date(2026, 11, 3)
    assert E.reaction_day(date(2026, 11, 3), "AMC") == date(2026, 11, 4)
    assert E.reaction_day(date(2026, 11, 6), "AMC") == date(2026, 11, 9)      # пятница → понедельник


def test_reaction_moves():
    idx = pd.to_datetime(["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"]).tz_localize(E.ET)
    c = pd.Series([100.0, 110.0, 99.0, 99.0], index=idx)
    mv = E.reaction_moves(c, [(date(2026, 8, 4), "BMO"),       # 100 → 110: +10%
                              (date(2026, 8, 4), "AMC"),       # 110 → 99: −10%
                              (date(2026, 8, 5), None),        # время неизвестно — пропуск
                              (date(2026, 8, 6), "AMC")])      # следующего дня нет — пропуск
    assert mv == [("2026-08-04", 10.0), ("2026-08-04", -10.0)]


def test_straddle_mid_and_last():
    calls = pd.DataFrame(dict(strike=[95, 100, 105], bid=[6, 3, 1], ask=[6.4, 3.2, 1.2], lastPrice=[6, 3, 1]))
    puts = pd.DataFrame(dict(strike=[95, 100, 105], bid=[0, 2.8, 0], ask=[0, 3.0, 0], lastPrice=[1, 2.9, 6]))
    s = E.straddle(calls, puts, 101)
    assert s["strike"] == 100 and s["src"] == "mid" and s["price"] == 6.0 and s["pct"] == round(6 / 101 * 100, 2)
    # вне торгов бид/аск нули — берём последнюю сделку
    puts2 = puts.assign(bid=0, ask=0)
    s2 = E.straddle(calls, puts2, 101)
    assert s2["src"] == "last" and s2["price"] == round(3.1 + 2.9, 2)


def test_stats_and_verdict():
    moves = [(f"2020-0{i}-01", m) for i, m in enumerate([4, -6, 8, -10, 12], 1)]
    st = E.stats(moves, 6.0)
    assert st["hist_avg"] == 8.0 and st["hist_n"] == 5 and st["last_move"] == 12
    assert st["ratio"] == 0.75 and E.verdict(st["ratio"]) == "cheap"
    assert st["over"] == 0.6                    # 8, 10, 12 больше 6
    assert st["inside12"] == 0.4                # ≤ 7,2: 4 и 6
    assert E.verdict(1.3) == "rich" and E.verdict(1.0) == "fair" and E.verdict(None) is None
    few = E.stats(moves[:2], 6.0)
    assert few["ratio"] is None                 # мало истории — перекос не считаем


def test_message_picks_tradable():
    res = dict(today="2026-11-03", rows=[
        dict(ticker="AMD", date="2026-11-03", session="AMC", implied=8.8, hist_avg=10.6, hist_n=40,
             verdict="cheap", over=0.55, inside12=0.6, eps_est=1.95, beats=7, beat_n=8, vol=900000,
             ob=dict(side="bear", type="formed", bar_time="x")),
        dict(ticker="SHOP", date="2026-11-03", session="BMO", implied=13.6, hist_avg=13.6, hist_n=30),
        dict(ticker="UBER", date="2026-11-04", session="BMO", implied=7.0, hist_avg=6.0, hist_n=20,
             verdict="rich", over=0.3, inside12=0.9),
        dict(ticker="APP", date="2026-11-04", session="AMC", implied=17.0, hist_avg=22.3, hist_n=12),
    ])
    msg = E.build_message(res)
    assert "AMD" in msg and "UBER" in msg
    assert "SHOP" not in msg and "APP" not in msg      # уже прошёл / завтра после закрытия
    assert "дешевле" in msg and "дороже" in msg and "📦🔴" in msg
    assert E.build_message(dict(today="2026-11-03", rows=[])) == ""


def test_details_with_fake_yahoo(monkeypatch):
    """Вся цепочка по одной акции на поддельном Yahoo: время, история, стрэддл, EPS."""
    import sys
    import types
    import numpy as np

    days = pd.bdate_range("2025-01-01", "2026-11-02", tz=E.ET)
    close = pd.Series(100.0, index=days)
    past = [pd.Timestamp(x, tz=E.ET) for x in ("2026-02-03 16:05", "2026-05-05 16:05", "2026-08-04 16:05",
                                                "2025-11-04 16:05")]
    for k, t in enumerate(past):                       # после каждого отчёта следующий день ±10%
        nxt = days[days.get_loc(t.normalize()) + 1]
        close.loc[nxt:] *= 1.1 if k % 2 else 0.9
    hist = pd.DataFrame({"Close": close})
    ed = pd.DataFrame({"EPS Estimate": [1.0, 1.0, 1.0, 1.0, 1.95], "Reported EPS": [1.1, 0.9, 1.2, 1.3, np.nan]},
                      index=pd.DatetimeIndex(past + [pd.Timestamp("2026-11-03 16:15", tz=E.ET)], name="Earnings Date"))
    spot = float(close.iloc[-1])
    k = round(spot)
    calls = pd.DataFrame(dict(strike=[k], bid=[4.0], ask=[4.2], lastPrice=[4.1]))
    puts = pd.DataFrame(dict(strike=[k], bid=[3.8], ask=[4.0], lastPrice=[3.9]))

    class T:
        def __init__(self, t):
            self.options = ("2026-11-06", "2026-11-13")
        def get_earnings_dates(self, limit=12):
            return ed
        def history(self, **kw):
            return hist
        def option_chain(self, e):
            return types.SimpleNamespace(calls=calls, puts=puts)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=T))
    r = E.details("AMD", date(2026, 11, 3), date(2026, 11, 3))
    assert r["session"] == "AMC" and r["react"] == "2026-11-04" and r["eps_est"] == 1.95
    assert r["straddle"]["exp"] == "2026-11-06" and r["straddle"]["src"] == "mid"
    assert r["hist_n"] == 4 and abs(r["hist_avg"] - 10.0) < 0.01
    assert r["beats"] == 3 and r["beat_n"] == 4
    assert r["verdict"] == "cheap"                       # стрэддл ≈ 8% < 0,85 × 10%


def test_quality():
    assert E.quality(8.0, dict(exp_days=2, src="last"), 10.0) is None
    assert E.quality(8.0, dict(exp_days=17, src="mid"), 10.0) == "far"
    assert E.quality(0.3, dict(exp_days=1, src="last"), 3.7) == "stale"
    assert E.quality(0.3, dict(exp_days=1, src="mid"), 3.7) is None       # живой бид/аск — верим
    assert E.quality(None, None, 5.0) is None
