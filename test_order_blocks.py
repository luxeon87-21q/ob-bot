"""Проверки: python -m pytest -q  (или python test_order_blocks.py)"""
import numpy as np
from pathlib import Path

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


def rand_df_smc(n=600, seed=0):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = c + rng.normal(0, .3, n)
    h = np.maximum(o, c) + rng.random(n)
    l = np.minimum(o, c) - rng.random(n)
    if seed % 3 == 0:                     # редкие «волатильные» свечи (high-low ≥ 2·ATR)
        k = rng.choice(n, n // 40, replace=False)
        h[k] += rng.random(len(k)) * 8
        l[k] -= rng.random(len(k)) * 8
    v = rng.integers(1_000, 100_000, n).astype(float)
    idx = pd.date_range("2025-01-01", periods=n, freq="4h", tz="America/New_York")
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=v), index=idx)


def smc_reference(df, size_in=5, size_sw=50, mitigation="High/Low"):
    """Независимый перенос Pine: серии через pandas (rolling/ewm/diff), цикл только для состояния."""
    H, L, C = df["high"], df["low"], df["close"]
    tr = pd.concat([H - L, (H - C.shift()).abs(), (L - C.shift()).abs()], axis=1).max(axis=1)
    tr.iloc[0] = H.iloc[0] - L.iloc[0]
    seed = tr.copy() * np.nan
    if len(tr) >= 200:
        seed.iloc[199] = tr.iloc[:200].mean()
        seed.iloc[200:] = tr.iloc[200:]
    atr = seed.ewm(alpha=1 / 200, adjust=False, ignore_na=False).mean()
    atr[seed.isna() & (np.arange(len(seed)) < 199)] = np.nan
    hv = ((H - L) >= 2 * atr).fillna(False).to_numpy()
    ph = np.where(hv, L, H); pl = np.where(hv, H, L)

    def legs(size):
        nh = H.shift(size) > H.rolling(size).max()
        nl = L.shift(size) < L.rolling(size).min()
        leg = pd.Series(np.where(nh, 0.0, np.where(nl, 1.0, np.nan)), index=H.index).ffill().fillna(0)
        return leg.diff().fillna(0).to_numpy()

    d_sw, d_in = legs(size_sw), legs(size_in)
    h, l, c = H.to_numpy(), L.to_numpy(), C.to_numpy()
    piv = {k: dict(cur=np.nan, crossed=False, bi=-1) for k in ("sh", "sl", "ih", "il")}
    prev = dict(ih=np.nan, il=np.nan, sh=np.nan, sl=np.nan)
    arrs = {"internal": [], "swing": []}
    formed = []
    for i in range(len(df)):
        for d, size, hk, lk in ((d_sw, size_sw, "sh", "sl"), (d_in, size_in, "ih", "il")):
            if d[i] == 1:
                piv[lk].update(cur=l[i - size], crossed=False, bi=i - size)
            elif d[i] == -1:
                piv[hk].update(cur=h[i - size], crossed=False, bi=i - size)
        new = []
        for kind, hk, lk in (("internal", "ih", "il"), ("swing", "sh", "sl")):
            for side, k in (("bull", hk), ("bear", lk)):
                p = piv[k]
                lv, pv = p["cur"], prev[k]
                prev[k] = lv
                if i == 0 or np.isnan(lv) or np.isnan(pv):
                    continue
                if side == "bull":
                    cross = c[i] > lv and c[i - 1] <= pv
                    extra = kind == "swing" or piv["ih"]["cur"] != piv["sh"]["cur"]
                else:
                    cross = c[i] < lv and c[i - 1] >= pv
                    extra = kind == "swing" or piv["il"]["cur"] != piv["sl"]["cur"]
                if cross and not p["crossed"] and extra:
                    p["crossed"] = True
                    seg = range(p["bi"], i)
                    j = min(seg, key=lambda x: (pl[x], x)) if side == "bull" else min(seg, key=lambda x: (-ph[x], x))
                    ob = dict(side=side, bh=ph[j], bl=pl[j], kind=kind, i=i)
                    if len(arrs[kind]) >= 100:
                        arrs[kind].pop()
                    arrs[kind].insert(0, ob); new.append(ob)
        sb = c[i] if mitigation == "Close" else h[i]
        sl = c[i] if mitigation == "Close" else l[i]
        gone = []
        for kind in ("internal", "swing"):
            a = arrs[kind]; j = 0
            while j < len(a):
                o = a[j]
                if (o["side"] == "bear" and sb > o["bh"]) or (o["side"] == "bull" and sl < o["bl"]):
                    gone.append(o); del a[j]
                j += 1
        formed += [(o["kind"], o["side"], i) for o in new if not any(o is g for g in gone)]
    return formed, arrs


def test_smc_matches_reference():
    total = 0
    for seed in range(60):
        df = rand_df_smc(n=700, seed=seed)
        for mit, rmit in (("Wick", "High/Low"), ("Close", "Close")):
            events, bulls, bears = detect(df, 5, mit, track_last=0)
            ref_formed, ref = smc_reference(df, mitigation=rmit)
            got = [(e.ob.kind, e.ob.side, e.bar_idx) for e in events if e.type == "formed"]
            assert sorted(got) == sorted(ref_formed), (seed, mit)
            total += len(got)
            for kind in ("internal", "swing"):
                mine = sorted([(o.side, o.bar_high, o.bar_low, o.detected_idx) for o in bulls + bears if o.kind == kind])
                theirs = sorted([(o["side"], o["bh"], o["bl"], o["i"]) for o in ref[kind]])
                assert mine == theirs, (seed, mit, kind)
    assert total > 1000
    print("сверено OB:", total)


def test_smc_events_consistency():
    df = rand_df_smc(n=900, seed=7)
    events, bulls, bears = detect(df, 5, "Wick", 5)
    assert len([o for o in bulls + bears if o.kind == "internal"]) <= 5
    assert len([o for o in bulls + bears if o.kind == "swing"]) <= 5
    kinds = {e.ob.kind for e in events}
    assert kinds == {"internal", "swing"}
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    for e in events:
        ob = e.ob
        assert ob.top >= ob.btm
        if e.type == "mitigated":
            assert (ob.side == "bull" and l[e.bar_idx] < ob.bar_low) or (ob.side == "bear" and h[e.bar_idx] > ob.bar_high)
        if e.type == "touched":
            i = e.bar_idx
            assert (ob.side == "bull" and l[i] <= ob.top < l[i - 1]) or (ob.side == "bear" and h[i] >= ob.btm > h[i - 1])
        if e.type == "formed":
            assert ob.ob_time < e.bar_time and ob.detected_idx == e.bar_idx


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


def test_telegram_subscribers(tmp_path):
    import tg
    tg.SETTINGS_FILE = tmp_path / "tg.json"
    tg.LEGACY_CHAT_FILE = tmp_path / "chat_id"
    tg.LEGACY_CHAT_FILE.write_text("111")                      # старый формат: один владелец
    sent, q = [], []

    def api(tok, method, **p):
        if method == "getUpdates":
            return [u for u in q if u["update_id"] >= (p.get("offset") or 0)]
        sent.append((p["chat_id"], p["text"]))
        return {"message_id": len(sent)}

    tg.api = api
    msg = lambda i, c, t: {"update_id": i, "message": {"chat": {"id": c, "type": "private"}, "text": t}}
    q += [msg(1, 222, "/start"), msg(2, 222, "/all"), msg(3, 333, "/only_new"), msg(4, 444, "/start"),
          msg(5, 444, "/stop"), {"update_id": 6, "my_chat_member": {"chat": {"id": 111},
                                                                    "new_chat_member": {"status": "kicked"}}}]
    st = tg.process_updates("x")
    assert set(st["subs"]) == {"222"}                           # 111 заблокировал, 333 не подписан, 444 отписался
    assert st["subs"]["222"]["only_new"] is False and st["owner"] == "111"
    q.append(msg(7, 555, "/start"))
    st = tg.process_updates("x")
    assert set(st["subs"]) == {"222", "555"} and st["subs"]["555"]["only_new"] is True

    sent.clear()
    tg.broadcast("x", st, "ALL", "NEW")
    assert sorted(sent) == [("222", "ALL\n"), ("555", "NEW\n")]
    sent.clear()
    tg.broadcast("x", st, "ALL", "")                            # новых OB нет — only_new молчит
    assert sent == [("222", "ALL\n")]

    # волны Вульфа — отдельное сообщение и отдельный выключатель
    q += [msg(8, 555, "/wolfe"), msg(9, 222, "/ob")]
    st = tg.process_updates("x")
    assert st["subs"]["555"]["wolfe"] is False and st["subs"]["222"]["ob"] is False
    sent.clear()
    assert tg.broadcast("x", st, "ALL", "NEW", "WOLFE") == 2
    assert sorted(sent) == [("222", "WOLFE\n"), ("555", "NEW\n")]


def _wolfe_df(pts, n):
    import numpy as np
    c = np.interp(np.arange(n), [p[0] for p in pts], [p[1] for p in pts])
    idx = pd.date_range("2026-01-05 09:30", periods=n, freq="4h", tz="America/New_York")
    return pd.DataFrame(dict(open=c, high=c + .3, low=c - .3, close=c, volume=1000.0), index=idx)


def test_wolfe():
    import wolfe
    df = _wolfe_df([(0, 105), (10, 100), (20, 112), (30, 96), (40, 104), (52, 90), (75, 113)], 80)
    ev = wolfe.detect(df, 3)
    assert len(ev) == 1
    e = ev[0]
    assert e.w.side == "bull" and e.w.idx == [10, 20, 30, 40]
    assert e.low <= e.w.line13 + 1e-9 and e.w.target > e.close           # цель выше цены
    assert 40 + 3 <= e.bar_idx <= 52                                    # не раньше подтверждения точки 4
    # зеркально — медвежья
    m = df.copy(); m["open"] = 200 - df["open"]; m["close"] = 200 - df["close"]
    m["high"] = 200 - df["low"]; m["low"] = 200 - df["high"]
    eb = wolfe.detect(m, 3)
    assert len(eb) == 1 and eb[0].w.side == "bear" and eb[0].w.target < eb[0].close
    # расходящиеся линии (не клин) — не Вульф
    bad = _wolfe_df([(0, 105), (10, 100), (20, 110), (30, 95), (40, 106), (52, 88), (75, 113)], 80)
    assert wolfe.detect(bad, 3) == []
    # цена ушла выше точки 4 до касания линии 1–3 — фигура сломана
    brk = _wolfe_df([(0, 105), (10, 100), (20, 112), (30, 96), (40, 104), (46, 101), (50, 108), (60, 85)], 70)
    assert wolfe.detect(brk, 3) == []

def test_options_oi(tmp_path):
    import options
    options.OPTIONS_FILE = tmp_path / "options.json"
    calls = []

    def fetch(t, today):
        calls.append(t)
        if t == "BAD":
            raise RuntimeError("yahoo")
        return {"oi": {"AAPL": 900000, "SMALL": 1200}.get(t, 0), "exp": 4}

    d = options.refresh(["AAPL", "SMALL", "BAD", "BRK.B"], fetch=fetch, workers=2)
    assert d["AAPL"]["oi"] == 900000 and d["SMALL"]["oi"] == 1200 and "BAD" not in d
    assert "BRK-B" in calls                                        # тикер в формате Yahoo
    assert options.passes(d, "AAPL") and not options.passes(d, "SMALL") and options.passes(d, "BAD")
    calls.clear()
    options.refresh(["AAPL", "SMALL", "BAD"], fetch=fetch)          # за сегодня уже есть — повторно не качаем
    assert set(calls) == {"BAD"}
    assert options.fmt_oi(912345) == "OI 912.3K" and options.fmt_oi(None) == "OI н/д"


if __name__ == "__main__":
    import tempfile
    test_smc_matches_reference(); test_smc_events_consistency(); test_to_4h_sessions()
    test_to_4h_incomplete_last_bar(); test_wolfe()
    test_telegram_subscribers(Path(tempfile.mkdtemp())); test_options_oi(Path(tempfile.mkdtemp()))
    print("OK")
