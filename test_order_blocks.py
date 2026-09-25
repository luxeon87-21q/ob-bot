"""Проверки: python -m pytest -q  (или python test_order_blocks.py)"""
import numpy as np
import pandas as pd

from data import to_4h
from order_blocks import detect


def rand_df(n=600, seed=0):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = c + rng.normal(0, .3, n)
    h = np.maximum(o, c) + rng.random(n)
    l = np.minimum(o, c) - rng.random(n)
    v = rng.integers(1_000, 100_000, n).astype(float)
    idx = pd.date_range("2025-01-01", periods=n, freq="4h", tz="America/New_York")
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=v), index=idx)


def pine_reference(df, length=5, mitigation="Wick"):
    """Построчный перенос Pine (series-семантика через индексы) — независимая проверка."""
    h, l, c, v = (df[k].to_numpy() for k in ("high", "low", "close", "volume"))
    os_ = 0
    bull_top, bull_btm, bear_top, bear_btm = [], [], [], []
    formed = []
    for i in range(len(df)):
        if i < length:
            continue
        upper = max(h[i - length + 1:i + 1]); lower = min(l[i - length + 1:i + 1])
        tb, tr = (min(c[i - length + 1:i + 1]), max(c[i - length + 1:i + 1])) if mitigation == "Close" else (lower, upper)
        os_ = 0 if h[i - length] > upper else 1 if l[i - length] < lower else os_
        k = i - length
        phv = k - length >= 0 and all(v[k] > v[j] for j in range(k - length, k + length + 1) if j != k)
        if phv and os_ == 1:
            bull_top.insert(0, (h[k] + l[k]) / 2); bull_btm.insert(0, l[k]); formed.append(("bull", i))
        if phv and os_ == 0:
            bear_top.insert(0, h[k]); bear_btm.insert(0, (h[k] + l[k]) / 2); formed.append(("bear", i))
        keep = [j for j, b in enumerate(bull_btm) if not tb < b]
        bull_top = [bull_top[j] for j in keep]; bull_btm = [bull_btm[j] for j in keep]
        keep = [j for j, t in enumerate(bear_top) if not tr > t]
        bear_top = [bear_top[j] for j in keep]; bear_btm = [bear_btm[j] for j in keep]
    return formed, list(zip(bull_top, bull_btm)), list(zip(bear_top, bear_btm))


def test_matches_pine_reference():
    for seed in range(30):
        for mit in ("Wick", "Close"):
            df = rand_df(seed=seed)
            events, bulls, bears = detect(df, 5, mit, track_last=0)
            ref_formed, ref_bull, ref_bear = pine_reference(df, 5, mit)
            # OB, пробитые на том же баре, в Pine появляются и сразу удаляются — сигнал "formed" им не шлём
            got = [(e.ob.side, e.bar_idx) for e in events if e.type == "formed"]
            assert set(got) <= set(ref_formed)
            assert [(o.top, o.btm) for o in bulls] == ref_bull
            assert [(o.top, o.btm) for o in bears] == ref_bear


def test_scenario_formed_touched_mitigated():
    # падение -> свеча с пиком объёма на дне -> рост -> откат в зону -> пробой вниз
    closes = [110, 108, 106, 104, 102, 100, 98, 96, 94, 92,     # падение
              90,                                                # свеча OB (индекс 10), объём-пик
              93, 96, 99, 102, 105, 108, 110, 112, 111,          # рост
              104, 97, 90, 92, 95, 99,                           # откат: касание зоны (индекс 22)
              88, 85]                                            # пробой (индекс 27)
    n = len(closes)
    c = np.array(closes, float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.5
    l = np.minimum(o, c) - 0.5
    v = np.full(n, 1000.0); v[10] = 50_000
    l[10] -= 2          # длинная нижняя тень у свечи OB
    idx = pd.date_range("2025-01-01", periods=n, freq="4h", tz="America/New_York")
    df = pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=v), index=idx)

    events, bulls, _ = detect(df, 5, "Wick", 3)
    kinds = [(e.type, e.ob.side, e.bar_idx) for e in events]
    print(kinds)
    formed = [e for e in events if e.type == "formed" and e.ob.side == "bull"]
    assert formed and formed[0].bar_idx == 15 and formed[0].ob.ob_time == idx[10]
    ob = formed[0].ob
    assert ob.btm == l[10] and ob.top == (h[10] + l[10]) / 2
    touched = [e for e in events if e.type == "touched" and e.ob is ob]
    mitig = [e for e in events if e.type == "mitigated" and e.ob is ob]
    assert touched and touched[0].bar_idx == 22
    assert mitig and mitig[0].bar_idx == 27
    assert ob not in bulls


def test_to_4h_sessions():
    # один торговый день часовых свечей 09:30..15:30 ET
    idx = pd.date_range("2026-09-24 09:30", periods=7, freq="1h", tz="America/New_York")
    hr = pd.DataFrame(dict(open=range(7), high=[x + 1 for x in range(7)], low=range(7),
                           close=range(7), volume=[10] * 7), index=idx, dtype=float)
    # в 15:00 закрыт только утренний бар
    out = to_4h(hr, pd.Timestamp("2026-09-24 15:00", tz="America/New_York"))
    assert list(out.index.strftime("%H:%M")) == ["09:30"]
    assert out.iloc[0].open == 0 and out.iloc[0].close == 3 and out.iloc[0].volume == 40
    out = to_4h(hr, pd.Timestamp("2026-09-24 16:05", tz="America/New_York"))
    assert list(out.index.strftime("%H:%M")) == ["09:30", "13:30"]
    assert out.iloc[1].open == 4 and out.iloc[1].close == 6 and out.iloc[1].high == 7


def test_to_4h_incomplete_last_bar():
    et = "America/New_York"
    idx = pd.date_range("2026-09-24 09:30", periods=6, freq="1h", tz=et)      # нет часового бара 15:30
    hr = pd.DataFrame(dict(open=range(6), high=range(1, 7), low=range(6), close=range(6),
                           volume=[1] * 6), index=idx, dtype=float)
    assert list(to_4h(hr, pd.Timestamp("2026-09-24 16:10", tz=et)).index.strftime("%H:%M")) == ["09:30"]
    assert list(to_4h(hr, pd.Timestamp("2026-09-25 09:00", tz=et)).index.strftime("%H:%M")) == ["09:30", "13:30"]


def test_telegram_private_bot(tmp_path):
    import tg
    tg.SETTINGS_FILE = tmp_path / "tg.json"
    tg.LEGACY_CHAT_FILE = tmp_path / "none"
    sent, q = [], []

    def api(tok, method, **p):
        if method == "getUpdates":
            return [u for u in q if u["update_id"] >= (p.get("offset") or 0)]
        sent.append(p["chat_id"])

    tg.api = api
    msg = lambda i, c, t: {"update_id": i, "message": {"chat": {"id": c, "type": "private"}, "text": t}}
    q += [msg(1, 111, "/start"), msg(2, 999, "/all"), msg(3, 111, "/all")]
    st = tg.process_updates("x")
    assert st["chat_id"] == "111" and st["only_new"] is False and st["offset"] == 3
    q.append(msg(4, 111, "/only_new"))
    assert tg.process_updates("x")["only_new"] is True


if __name__ == "__main__":
    test_matches_pine_reference(); test_scenario_formed_touched_mitigated(); test_to_4h_sessions()
    print("OK")
