"""Тесты вкладки «Трейдер» (trader.py): план сделки, прогоны, история OB, сканер, новости."""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import trader


def bars(closes, start="2026-01-05", spread=0.002):
    """Свечи 4H (9:30 и 13:30 ET) по списку закрытий."""
    days = pd.bdate_range(start, periods=(len(closes) + 1) // 2, tz="America/New_York")
    idx = [d + pd.Timedelta(hours=h, minutes=30) for d in days for h in (9, 13)][:len(closes)]
    c = np.array(closes, float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) * (1 + spread), low=np.minimum(o, c) * (1 - spread),
                             close=c, volume=1e6), index=pd.DatetimeIndex(idx))


def test_plan_formed_bull_is_limit_at_top():
    p = trader.plan_for(dict(type="formed", side="bull", top=100, btm=98, close=104))
    assert p["side"] == "long" and p["how"] == "limit"
    assert p["entry"] == 100 and p["stop"] == 98 and p["target"] == 104      # 2R


def test_plan_touched_bear_is_market_short():
    p = trader.plan_for(dict(type="touched", side="bear", top=52, btm=50, close=50.5))
    assert p["side"] == "short" and p["how"] == "market"
    assert p["entry"] == 50.5 and p["stop"] == 52 and p["target"] == 47.5


def test_plan_refuses_tiny_risk_and_mitigated():
    assert trader.plan_for(dict(type="touched", side="bull", top=100.05, btm=100.0, close=100.02)) is None
    assert trader.plan_for(dict(type="mitigated", side="bull", top=100, btm=98, close=97)) is None


def test_simulate_driftless_market_is_near_breakeven():
    rng = np.random.default_rng(3)
    r = rng.normal(0, 0.01, 600)
    r -= r[-500:].mean()                                     # пул без тренда
    df = bars(100 * np.exp(np.cumsum(r)))
    c = float(df["close"].iloc[-1])
    plan = dict(side="long", entry=c, stop=c * 0.98, target=c * 1.04, r=2.0, how="market")
    mc = trader.simulate(df, len(df) - 1, plan, horizon=300)
    assert mc["fill"] == 100.0
    assert 25 <= mc["first"] <= 40                           # теория 33% (2R), на свечах чуть ниже
    assert mc["be"] == 33.3


def test_simulate_is_repeatable_with_seed():
    df = bars(100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 400)) * 0.3)
    c = float(df["close"].iloc[-1])
    plan = dict(side="long", entry=c * 0.99, stop=c * 0.97, target=c * 1.03, r=2.0, how="limit")
    assert trader.simulate(df, len(df) - 1, plan) == trader.simulate(df, len(df) - 1, plan)


def test_replay_history_counts_real_outcomes():
    class OB:
        def __init__(s, side, top, btm): s.side, s.top, s.btm = side, top, btm

    class Ev:
        def __init__(s, i, close): s.type, s.ob, s.bar_idx, s.close = "touched", OB("bull", 100, 98), i, close
    up = [100] * 5 + [101, 102, 103, 104, 105] + [105] * 30           # после касания — рост до цели
    df = bars(up, spread=0.0)
    h = trader.replay_history(df, [Ev(4, 100.0)], dict(type="touched", side="bull"), len(df))
    assert h["n"] == 1 and h["target"] == 1 and h["stop"] == 0
    down = [100] * 5 + [99, 98, 97] + [97] * 30                        # падение под стоп
    df = bars(down, spread=0.0)
    h = trader.replay_history(df, [Ev(4, 100.0)], dict(type="touched", side="bull"), len(df))
    assert h["stop"] == 1 and h["avg_r"] == -1.0


def test_verdict_uses_breakeven_for_r():
    assert trader.verdict(30, 70, 2.0)[0] == "Около безубытка"       # 30% против 33%
    assert trader.verdict(45, 55, 2.0)[1] == 1                         # +12 п. — склоняется к цели
    assert trader.verdict(10, 90, 2.0)[1] == -2
    assert trader.verdict(1, 2, 2.0)[0] == "Мало данных"


def test_setups_breakout_and_oversold():
    c = np.r_[np.linspace(100, 100, 25), 110.0]
    assert "breakout_20d" in trader.setups(c)
    falling = np.linspace(100, 60, 30)
    assert "oversold" in trader.setups(falling)
    bounce = np.r_[falling, 61.0]
    assert "oversold_bounce" in trader.setups(bounce)


def test_daily_closes_drops_unfinished_today():
    df = bars([10, 11, 12, 13, 14], start="2026-03-02")             # пн 2 свечи, вт 2, ср только утро
    now = pd.Timestamp("2026-03-04 12:00", tz="America/New_York")
    d = trader.daily_closes(df, now)
    assert len(d) == 2 and d["close"].iloc[-1] == 13


def test_parse_rss_and_tags():
    xml = """<rss><channel>
      <item><title>Apple beats Q3 earnings estimates</title><link>https://finance.yahoo.com/a</link>
        <pubDate>Mon, 02 Nov 2026 13:00:00 +0000</pubDate></item>
      <item><title>Old story</title><link>https://finance.yahoo.com/b</link>
        <pubDate>Mon, 02 Feb 2026 13:00:00 +0000</pubDate></item>
      <item><title>Bad link</title><link>javascript:alert(1)</link>
        <pubDate>Mon, 02 Nov 2026 13:00:00 +0000</pubDate></item>
    </channel></rss>"""
    items = trader.parse_rss(xml, datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [i["title"] for i in items] == ["Apple beats Q3 earnings estimates"]
    assert "отчёт" in items[0]["tags"]


def test_tg_line():
    tm = dict(plan=dict(how="limit"), mc=dict(fill=67.2, first=43.6, be=33.3, verdict="Склоняется к цели"),
              hist=dict(target=3, stop=4, n=26))
    assert trader.tg_line(tm) == "🎲 вход 67% · цель раньше стопа 44% (безубыток 33%) · история 3 из 7 · склоняется к цели"
    assert trader.tg_line(None) == ""
