"""
Данные для скринера опционов на сайте (страница options.html) — продажа путов (Naked PUT).

Два режима:
  python optscreen.py --universe     — раз в неделю: какие акции и ETF вообще грузить
                                       (объём опционов по 3 ближайшим экспирациям ≥ UNI_VOL_MIN) → cache/opt_universe.json
  python optscreen.py [--slot]       — дважды в день: все путы этих акций с экспирацией до MAX_DTE дней
                                       и удалением страйка −40…+15% → cache/optscreen.json (для сайта)
                                       --slot — запуск по расписанию, только в окна утра/вечера (как samurai.py)

По каждой акции: цена, изменение за день, капитализация, IV30 и IV Rank (как в samurai.py), объём опционов за день,
дата отчёта, оценка аналитиков, EMA 30/50/500, MACD, RSI и своя оценка акции (Stock Score) 1–10:
  F — фундамент: маржа, рентабельность капитала, ликвидность, долг, свободный денежный поток к капитализации;
  G — рост: рост выручки и прибыли;
  T — техника: цена к EMA 50 и EMA 200, ход за 3 месяца.
Оценки — место акции среди всех акций выгрузки (10 — лучшие 10%). Это наша оценка, не Option Samurai.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import samurai as S

BASE = Path(__file__).resolve().parent
UNI_FILE = BASE / "cache" / "opt_universe.json"
OUT_FILE = BASE / "cache" / "optscreen.json"
INFO_FILE = BASE / "cache" / "optscreen_info.json"
ET = "America/New_York"
log = logging.getLogger("ob_bot.optscreen")

UNI_VOL_MIN = 2500      # объём по 3 ближайшим экспирациям — с запасом к фильтру сайта «≥ 5000 за день»
MAX_DTE = 70
MNY = (-0.40, 0.15)     # удаление страйка от цены
INFO_DAYS = 3           # как часто обновлять фундаментал/аналитиков (отчёт — чаще, см. ниже)
INFO_PER_RUN = 400
RATE = 0.04


def _etfs() -> List[str]:
    import hammer
    return list(hammer.ETFS)


# ======================================================================== список акций
def build_universe() -> dict:
    import seasonal
    import yfinance as yf
    u = seasonal.all_us_stocks()
    tickers = sorted(set(u["ticker"]) | set(_etfs()))
    hist = seasonal.download(tickers, 1)
    LP = seasonal.Params(min_price=1.0, min_dvol=2e6)
    liq = sorted(t for t, df in hist.items() if seasonal.liquid(df, LP))
    log.info("список: всего %d, с историей %d, ликвидных %d", len(tickers), len(hist), len(liq))
    time.sleep(60)
    today = pd.Timestamp.now(tz=ET).tz_localize(None).normalize()
    vols: Dict[str, int] = {}
    failed: List[str] = []

    def one(t) -> bool:
        try:
            tk = yf.Ticker(t)
            exps = [e for e in (tk.options or ()) if (pd.Timestamp(e) - today).days >= 0][:3]
            if not exps:
                return False
            v = 0
            for e in exps:
                ch = tk.option_chain(e)
                for side in (ch.calls, ch.puts):
                    if side is not None and "volume" in side:
                        v += int(pd.to_numeric(side["volume"], errors="coerce").fillna(0).sum())
            vols[t] = v
            return True
        except Exception as ex:
            log.debug("%s: %s", t, ex)
            return False

    todo = liq
    for rnd in range(3):
        failed = []
        streak = 0
        for t in todo:
            if one(t):
                streak = 0
            else:
                failed.append(t)
                streak += 1
                if streak >= 20:
                    log.info("Yahoo режет запросы — пауза")
                    time.sleep(90)
                    streak = 0
            time.sleep(0.25)
        log.info("список: круг %d, получено %d, без опционов/ошибок %d", rnd + 1, len(vols), len(failed))
        if len(failed) < 30:
            break
        todo = failed
        time.sleep(90)
    keep = sorted(t for t, v in vols.items() if v >= UNI_VOL_MIN)
    return dict(updated=pd.Timestamp.now(tz="UTC").isoformat(), checked=len(liq), with_options=len(vols),
                tickers=keep, vol={t: vols[t] for t in keep})


# ======================================================================== фундамент, аналитики, отчёт
def load_info(tickers: List[str], tstr: str) -> Dict[str, dict]:
    import yfinance as yf
    import seasonal
    cache = S._load(INFO_FILE)
    today = pd.Timestamp(tstr)

    def stale(t):
        r = cache.get(t)
        if not r or not r.get("date"):
            return True
        if (today - pd.Timestamp(r["date"])).days >= INFO_DAYS:
            return True
        e = r.get("earnings")
        return bool(e and e < tstr)                 # отчёт уже прошёл — нужна новая дата

    todo = sorted((t for t in tickers if stale(t)), key=lambda t: cache.get(t, {}).get("date", ""))[:INFO_PER_RUN]
    log.info("фундамент/аналитики: обновляю %d из %d", len(todo), len(tickers))
    streak = 0
    for rnd in range(3):
        miss = [t for t in todo if cache.get(t, {}).get("date") != tstr]
        if not miss:
            break
        if rnd:
            time.sleep(90)
        for t in miss:
            try:
                info = yf.Ticker(t).info or {}
            except Exception:
                info = {}
            if not info.get("marketCap") and not info.get("shortName") and not info.get("quoteType"):
                streak += 1
                if streak >= 15:
                    time.sleep(60)
                    streak = 0
                continue
            streak = 0
            g = lambda k: S._num(info.get(k))
            rec = dict(date=tstr, name=info.get("shortName"), qtype=info.get("quoteType"), mcap=g("marketCap"),
                       sector=info.get("sector"), rec=g("recommendationMean"), analysts=info.get("numberOfAnalystOpinions"),
                       pm=g("profitMargins"), roe=g("returnOnEquity"), cr=g("currentRatio"), de=g("debtToEquity"),
                       fcf=g("freeCashflow"), rg=g("revenueGrowth"), eg=g("earningsGrowth"))
            if rec["qtype"] != "ETF":
                ed = seasonal.next_earnings(t)
                rec["earnings"] = ed.isoformat() if ed else None
            cache[t] = rec
            time.sleep(0.5)
    S._save(INFO_FILE, cache)
    return cache


def tech_row(df: pd.DataFrame) -> Optional[dict]:
    c = df["close"].dropna()
    if len(c) < 60:
        return None
    e = lambda n: float(S.ema(c, n).iloc[-1])
    macd = S.ema(c, 12) - S.ema(c, 26)
    sig = S.ema(macd, 9)
    d = c.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = float(100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1])) if dn.iloc[-1] > 0 else 100.0
    lr = np.log(c / c.shift(1))
    hv = (lr.rolling(20).std() * math.sqrt(252)).dropna().tail(252)
    close = float(c.iloc[-1])
    return dict(close=close, chg=float(c.iloc[-1] / c.iloc[-2] - 1) * 100 if len(c) > 1 else 0.0,
                e30=e(30), e50=e(50), e200=e(200), e500=e(500) if len(c) >= 500 else None,
                macd_up=bool(macd.iloc[-1] > sig.iloc[-1]), rsi=rsi,
                m3=float(c.iloc[-1] / c.iloc[-64] - 1) if len(c) > 64 else None,
                hv_min=float(hv.min()) if len(hv) else None, hv_max=float(hv.max()) if len(hv) else None)


def pct_scores(vals: Dict[str, Optional[float]], higher_better: bool = True) -> Dict[str, float]:
    """Место среди всех (0..1)."""
    s = pd.Series({k: v for k, v in vals.items() if v is not None and not (isinstance(v, float) and math.isnan(v))})
    if s.empty:
        return {}
    r = s.rank(pct=True)
    return (r if higher_better else 1 - r + 1 / len(s)).to_dict()


def scores(tickers: List[str], info: Dict[str, dict], tech: Dict[str, dict]) -> Dict[str, dict]:
    stock = [t for t in tickers if info.get(t, {}).get("qtype") != "ETF"]
    I = lambda k: {t: info.get(t, {}).get(k) for t in stock}
    fcfy = {t: (info[t]["fcf"] / info[t]["mcap"]) if info.get(t, {}).get("fcf") is not None and info[t].get("mcap") else None
            for t in stock}
    F = [pct_scores(I("pm")), pct_scores(I("roe")), pct_scores(I("cr")), pct_scores(I("de"), False), pct_scores(fcfy)]
    G = [pct_scores(I("rg")), pct_scores(I("eg"))]
    T = [pct_scores({t: (tech[t]["close"] / tech[t]["e50"] - 1) if t in tech else None for t in stock}),
         pct_scores({t: (tech[t]["close"] / tech[t]["e200"] - 1) if t in tech else None for t in stock}),
         pct_scores({t: tech[t]["m3"] if t in tech else None for t in stock})]

    def comb(parts, t, need):
        v = [p[t] for p in parts if t in p]
        return max(1, min(10, math.ceil(np.mean(v) * 10))) if len(v) >= need else None

    return {t: dict(f=comb(F, t, 3), g=comb(G, t, 1), tt=comb(T, t, 2)) for t in stock}


# ======================================================================== опционы
def chains(t: str, S0: float, today: pd.Timestamp) -> dict:
    import yfinance as yf
    tk = yf.Ticker(t)
    exps = [e for e in (tk.options or ()) if 0 <= (pd.Timestamp(e) - today).days <= MAX_DTE]
    if not exps:
        raise RuntimeError("пустой список экспираций")
    vol, iv30, gap, rows = 0, None, 1e9, []
    for e in exps:
        dte = (pd.Timestamp(e) - today).days
        ch = tk.option_chain(e)
        for side in (ch.calls, ch.puts):
            if side is not None and "volume" in side:
                vol += int(pd.to_numeric(side["volume"], errors="coerce").fillna(0).sum())
        if abs(dte - 30) < gap and dte >= 7:
            ivs = []
            for side in (ch.calls, ch.puts):
                if side is not None and len(side):
                    r = side.iloc[(side["strike"] - S0).abs().argsort()[:1]]
                    v, b, a = (S._num(r[k].iloc[0]) for k in ("impliedVolatility", "bid", "ask"))
                    if v and 0.05 < v < 5 and (b or 0) > 0 and (a or 0) > 0:
                        ivs.append(v)
            if ivs:
                iv30, gap = float(np.mean(ivs)), abs(dte - 30)
        T = max(dte, 0.5) / 365
        for _, r in ch.puts.iterrows():
            K = S._num(r.get("strike"))
            if not K or not (MNY[0] <= K / S0 - 1 <= MNY[1]):
                continue
            bid, ask, last = (S._num(r.get(k)) or 0 for k in ("bid", "ask", "lastPrice"))
            if bid <= 0 and ask <= 0:
                continue
            iv = S._num(r.get("impliedVolatility"))
            iv = iv if iv and iv > 0.05 else None
            prob = S.prob_otm_put(S0, K, T, iv or 0, RATE)
            delta = None
            if iv:
                d1 = (math.log(S0 / K) + (RATE + iv * iv / 2) * T) / (iv * math.sqrt(T))
                delta = S.ncdf(d1) - 1
            rows.append([e, round(K, 4), round(bid, 2), round(ask, 2), round(last, 2),
                         int(S._num(r.get("volume")) or 0), int(S._num(r.get("openInterest")) or 0),
                         round(iv, 4) if iv else None, round(delta, 3) if delta is not None else None,
                         round(prob, 4) if prob is not None else None])
    return dict(vol=vol, iv30=iv30, rows=rows)


def build_dataset(tickers: Optional[List[str]] = None, slot: Optional[str] = None) -> dict:
    import seasonal
    now = pd.Timestamp.now(tz=ET)
    today = now.tz_localize(None).normalize()
    tstr = str(today.date())
    uni = S._load(UNI_FILE)
    explicit = tickers is not None
    tickers = tickers or uni.get("tickers") or []
    if not tickers:
        raise RuntimeError("список акций пуст — сначала python optscreen.py --universe")
    etfs = set(_etfs())
    hist = seasonal.download(sorted(set(tickers)), 5)
    tech = {}
    for t, df in hist.items():
        try:
            x = tech_row(df.dropna(subset=["close"]))
            if x:
                tech[t] = x
        except Exception as e:
            log.warning("%s: %s", t, e)
    log.info("техника: %d из %d", len(tech), len(tickers))
    time.sleep(60 if len(hist) > 50 else 0)
    info = load_info(sorted(tech), tstr)
    vols, ivh = S._load(S.VOL_FILE), S._load(S.IV_FILE)
    got: Dict[str, dict] = {}

    def one(t) -> bool:
        try:
            got[t] = chains(t, tech[t]["close"], today)
            return True
        except Exception as e:
            log.debug("%s: %s", t, e)
            return False

    todo = sorted(tech)
    for rnd in range(3):
        streak, fail = 0, []
        for t in todo:
            if one(t):
                streak = 0
            else:
                fail.append(t)
                streak += 1
                if streak >= 15:
                    log.info("Yahoo режет опционы — пауза")
                    time.sleep(90)
                    streak = 0
            time.sleep(0.3)
        log.info("опционы: круг %d, есть %d, нет %d", rnd + 1, len(got), len(fail))
        if not fail:
            break
        todo = fail
        time.sleep(60)

    exps = sorted({r[0] for g in got.values() for r in g["rows"]})
    ei = {e: i for i, e in enumerate(exps)}
    sc = scores(sorted(got), info, tech)
    trows, prow = [], []
    for t in sorted(got):
        g, x, i = got[t], tech[t], info.get(t, {})
        prev = vols.get(t, {})
        prev_vol = prev.get("vol") if prev.get("date") and prev["date"] < tstr else None
        vol_use = max(g["vol"], prev_vol or 0, (uni.get("vol") or {}).get(t, 0) if not g["vol"] else 0)
        if g["vol"] and (prev.get("date") != tstr or g["vol"] > prev.get("vol", 0)):
            vols[t] = {"date": tstr, "vol": g["vol"]}
        if g["iv30"]:
            h = ivh.setdefault(t, {})
            h[tstr] = round(g["iv30"], 4)
            for k in sorted(h)[:-400]:
                h.pop(k)
        own = {k: v for k, v in ivh.get(t, {}).items() if k != tstr}
        ivr, src = None, None
        if g["iv30"]:
            r = S.iv_rank_own(own, g["iv30"], 60)
            ivr, src = (r, "own") if r is not None else (S.iv_rank_proxy(g["iv30"], x["hv_min"], x["hv_max"]), "hv")
        s = sc.get(t, {})
        ti = len(trows)
        trows.append(dict(t=t, n=i.get("name"), etf=t in etfs or i.get("qtype") == "ETF", p=round(x["close"], 4),
                          chg=round(x["chg"], 2), mcap=i.get("mcap"), iv=round(g["iv30"], 4) if g["iv30"] else None,
                          ivr=round(ivr, 1) if ivr is not None else None, ivs=src, vol=vol_use,
                          earn=i.get("earnings"), rec=i.get("rec"), sec=i.get("sector"),
                          sf=s.get("f"), sg=s.get("g"), st=s.get("tt"), rsi=round(x["rsi"], 1),
                          e3050=x["e30"] > x["e50"], e30500=(x["e500"] is not None and x["e30"] > x["e500"]),
                          macd=x["macd_up"]))
        for r in g["rows"]:
            prow.append([ti, ei[r[0]]] + r[1:])
    if not explicit:
        S._save(S.VOL_FILE, vols)
        S._save(S.IV_FILE, ivh)
    return dict(made=pd.Timestamp.now(tz="UTC").isoformat(), et=now.strftime("%Y-%m-%d %H:%M"), today=tstr, slot=slot,
                iv_days=max((len(v) for v in ivh.values()), default=0), universe=len(tickers), loaded=len(got),
                failed=sorted(set(tech) - set(got)), exps=exps, tickers=trows,
                cols=["ti", "ei", "k", "bid", "ask", "last", "vol", "oi", "iv", "delta", "prob"], puts=prow)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--slot", action="store_true")
    ap.add_argument("--tickers")
    ap.add_argument("--out")
    a = ap.parse_args()
    gh_out = os.environ.get("GITHUB_OUTPUT")

    def skip(msg):
        log.info(msg)
        if gh_out:
            with open(gh_out, "a") as fh:
                fh.write("skip=true\n")

    if a.universe:
        res = build_universe()
        S._save(UNI_FILE, res)
        log.info("в списке %d акций и ETF (из %d с опционами)", len(res["tickers"]), res["with_options"])
        return
    now = pd.Timestamp.now(tz=ET)
    s = S.slot_now(now)
    slot_id = f"{now.date()}-{s}" if s else None
    if a.slot and (not s or S._load(OUT_FILE).get("slot") == slot_id):
        return skip(f"не время или уже посчитано (сейчас {now.strftime('%H:%M')} NY)")
    tickers = [x.strip().upper() for x in a.tickers.split(",")] if a.tickers else None
    res = build_dataset(tickers, None if tickers else slot_id)
    out = BASE / a.out if a.out else OUT_FILE
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    log.info("выгрузка: %d акций, %d путов, %.1f МБ", len(res["tickers"]), len(res["puts"]), out.stat().st_size / 1e6)


if __name__ == "__main__":
    main()
