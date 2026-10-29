"""
Панель приборов рынка (вкладка «Рынок»): широта S&P 500, веса и вклад компонентов, их скользящие,
ротация секторов, режим риска и сводный «термометр».

1. Широта — доля акций S&P 500 выше своей SMA 20 / 50 / 100 / 200 (аналоги S5TW / S5FI / S5OH / S5TH),
   считается сами из дневок всех 500 акций. Плюс Zweig Breadth Thrust (по доле растущих акций),
   новые 52-недельные максимумы/минимумы и дивергенции «индекс у максимума — широта падает».
2. Веса — капитализация компаний (Yahoo) по секторам GICS (Википедия); вклад = вес × изменение.
   Мегакапы — топ-10 компаний по капитализации, SMH — доля полупроводников в индексе.
3. Скользящие — SMA и EMA 20 / 50 / 100 / 200 на днях и неделях у SPX, секторных ETF, SMH/QQQ/RSP/IWM
   и мегакапов: расстояние в % и ATR, состояние (поддержка, сопротивление, отбой, пробой).
4. Ротация (RRG) — относительная сила сектора к SPX и её импульс, недели.
5. Риск — VIX и его срочная структура, HYG, доходность 10Y, доллар, подразумеваемая корреляция.
6. Термометр 0–100 и статистика: что исторически было с SPX через 5 / 20 / 60 дней после каждого сигнала.

Запуск (по будням после закрытия, .github/workflows/market.yml):
  python market.py            — расчёт, cache/market.json, Telegram (новые события)
  python market.py --no-tg    — без Telegram
"""
from __future__ import annotations

import argparse
import html
import io
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "market.json"
SENT_FILE = BASE / "cache" / "market_sent.json"
CAPS_FILE = BASE / "cache" / "market_caps.json"
GICS_FILE = BASE / "cache" / "universe_sp500_gics.json"
ET = "America/New_York"
log = logging.getLogger("ob_bot.market")

INDEX = "^GSPC"
SECTORS = {
    "XLK": ("Технологии", "Information Technology"),
    "XLF": ("Финансы", "Financials"),
    "XLC": ("Коммуникации", "Communication Services"),
    "XLY": ("Потреб. циклич.", "Consumer Discretionary"),
    "XLV": ("Здравоохранение", "Health Care"),
    "XLI": ("Промышленность", "Industrials"),
    "XLP": ("Потреб. базовые", "Consumer Staples"),
    "XLE": ("Энергетика", "Energy"),
    "XLU": ("Коммунальные", "Utilities"),
    "XLRE": ("Недвижимость", "Real Estate"),
    "XLB": ("Материалы", "Materials"),
}
THEMES = {"SMH": "Полупроводники", "QQQ": "Nasdaq-100", "RSP": "S&P 500 равновзв.", "IWM": "Russell 2000"}
SEMI_SUB = ("Semiconductors", "Semiconductor Materials & Equipment")
RISK = ["^VIX", "^VIX3M", "^VIX9D", "HYG", "^TNX", "DX-Y.NYB", "^COR1M", "SPY"]
MEGA_FALLBACK = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "BRK-B", "JPM"]
LENS = (20, 50, 100, 200)
KINDS = ("SMA", "EMA")
HORIZONS = (5, 20, 60)
OHLCV = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _env(name, default, cast=float):
    v = os.environ.get(name)
    return default if v in (None, "") else cast(v)


@dataclass
class Params:
    low: float = 15.0          # зона перепроданности широты, %
    high: float = 85.0         # зона перекупленности широты, %
    warm: float = 70.0         # «тёплая» зона — линия на графике
    years: int = 20            # история для статистики сигналов
    chart_days: int = 760      # сколько дней истории отдавать на сайт (≈3 года)
    near_atr: float = 0.75     # «у средней» — ближе 0,75 ATR своего таймфрейма
    touch_atr: float = 0.25    # касание — минимум/максимум свечи ближе 0,25 ATR
    debounce: int = 10         # одно и то же событие не чаще раза в 10 сессий
    caps_days: int = 7         # капитализации обновляются раз в неделю

    @classmethod
    def from_env(cls) -> "Params":
        p = cls()
        for k, v in asdict(p).items():
            setattr(p, k, _env("MKT_" + k.upper(), v, type(v)))
        return p


# ======================================================================== состав индекса
def sp500_gics() -> pd.DataFrame:
    """Состав S&P 500 с секторами GICS (Википедия, кэш на неделю)."""
    import requests
    fresh = GICS_FILE.exists() and time.time() - GICS_FILE.stat().st_mtime < 7 * 86400
    if not fresh:
        try:
            h = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                             headers={"User-Agent": "Mozilla/5.0 (ob-bot; personal use)"}, timeout=30).text
            for t in pd.read_html(io.StringIO(h)):
                cols = {str(c).strip(): c for c in t.columns}
                if "Symbol" in cols and "GICS Sector" in cols:
                    df = pd.DataFrame({
                        "ticker": t[cols["Symbol"]].astype(str).str.strip().str.replace(".", "-", regex=False),
                        "name": t[cols.get("Security", cols["Symbol"])].astype(str),
                        "sector": t[cols["GICS Sector"]].astype(str),
                        "sub": t[cols["GICS Sub-Industry"]].astype(str) if "GICS Sub-Industry" in cols else "",
                    })
                    if len(df) >= 400:
                        GICS_FILE.parent.mkdir(exist_ok=True)
                        GICS_FILE.write_text(df.to_json(orient="records", force_ascii=False), encoding="utf-8")
                        return df
        except Exception as e:
            log.warning("состав S&P 500 с секторами: %s", e)
    if GICS_FILE.exists():
        return pd.DataFrame(json.loads(GICS_FILE.read_text(encoding="utf-8")))
    import data
    tick = data.load_universe("sp500", BASE / "tickers.txt")
    return pd.DataFrame({"ticker": tick, "name": tick, "sector": "", "sub": ""})


def company_of(name: str) -> str:
    """«Alphabet Inc. (Class A)» и «(Class C)» — одна компания."""
    return re.sub(r"\s*\((?:Class|Series)\s+\w+\)\s*$", "", str(name)).strip()


def market_caps(tickers: List[str], days: int) -> Dict[str, float]:
    """Капитализация по Yahoo (fast_info), кэш на неделю."""
    try:
        cached = json.loads(CAPS_FILE.read_text(encoding="utf-8"))
    except Exception:
        cached = {}
    age = time.time() - CAPS_FILE.stat().st_mtime if CAPS_FILE.exists() else 1e12
    have = cached.get("caps", {})
    if age < days * 86400 and len(have) >= 0.9 * len(tickers):
        return have
    import yfinance as yf

    def one(t):
        for attempt in range(2):
            try:
                v = yf.Ticker(t).fast_info["market_cap"]
                if v and v > 0:
                    return t, float(v)
            except Exception:
                time.sleep(1 + attempt)
        return t, None

    out: Dict[str, float] = {}
    with ThreadPoolExecutor(12) as ex:
        for t, v in ex.map(one, tickers):
            if v:
                out[t] = v
            elif t in have:
                out[t] = have[t]
    log.info("капитализации: %d из %d", len(out), len(tickers))
    if len(out) >= 0.5 * len(tickers):
        CAPS_FILE.parent.mkdir(exist_ok=True)
        CAPS_FILE.write_text(json.dumps({"updated": pd.Timestamp.now(tz="UTC").isoformat(), "caps": out}),
                             encoding="utf-8")
        return out
    return have or out


# ======================================================================== индикаторы
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    e = s.ewm(span=n, adjust=False, min_periods=n).mean()
    return e


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()      # RMA, как в TradingView


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Недели по пятницам; текущая незакрытая неделя участвует, как на графике TradingView."""
    return df.resample("W-FRI").agg(OHLCV).dropna(subset=["open", "close"])


def ma_state(f: pd.DataFrame, ma: pd.Series, at: pd.Series, P: Params, k: int) -> Optional[dict]:
    """Где цена относительно средней сейчас и что было за последние k свечей."""
    if len(f) < k + 3 or pd.isna(ma.iloc[-1]) or pd.isna(at.iloc[-1]) or at.iloc[-1] <= 0:
        return None
    c, h, l = f["close"].to_numpy(), f["high"].to_numpy(), f["low"].to_numpy()
    m, a = ma.to_numpy(), at.to_numpy()
    i = len(c) - 1
    d = (c[i] - m[i]) / a[i]
    above = c[i] > m[i]
    lo = i - k + 1
    if np.isnan(m[lo - 1]):
        return None
    was_above = c[lo - 1] > m[lo - 1]
    win = range(lo, i + 1)
    touch_dn = any(l[j] <= m[j] + P.touch_atr * a[j] and c[j] >= m[j] for j in win)
    touch_up = any(h[j] >= m[j] - P.touch_atr * a[j] and c[j] <= m[j] for j in win)
    if was_above and not above and d < -P.touch_atr:
        st = "break_dn"
    elif not was_above and above and d > P.touch_atr:
        st = "break_up"
    elif abs(d) <= P.near_atr:
        st = "support" if above else "resist"
    elif above and was_above and touch_dn:
        st = "bounce"
    elif not above and not was_above and touch_up:
        st = "reject"
    else:
        st = "above" if above else "below"
    slope = m[i] - m[max(0, i - 5)]
    return dict(v=round(float(m[i]), 4), pct=round(float((c[i] / m[i] - 1) * 100), 2), atr=round(float(d), 2),
                st=st, up=bool(slope > 0))


def instrument(df: pd.DataFrame, P: Params) -> dict:
    """Цена, изменения и все средние на днях и неделях."""
    out = dict(price=round(float(df["close"].iloc[-1]), 4))
    c = df["close"]
    for n in (1, 5, 20, 60):
        out[f"r{n}"] = round(float((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100), 2) if len(c) > n else None
    out["hi52"] = round(float((c.iloc[-1] / c.tail(252).max() - 1) * 100), 2)
    mas = {}
    for tf, f, k in (("D", df, 3), ("W", weekly(df), 2)):
        at = atr(f)
        out[f"atr_{tf}"] = round(float(at.iloc[-1] / f["close"].iloc[-1] * 100), 2) if not pd.isna(at.iloc[-1]) else None
        for kind in KINDS:
            for n in LENS:
                ma = sma(f["close"], n) if kind == "SMA" else ema(f["close"], n)
                s = ma_state(f, ma, at, P, k)
                if s:
                    mas[f"{tf}{kind}{n}"] = s
    out["ma"] = mas
    return out


# ======================================================================== широта
def breadth(closes: pd.DataFrame, highs: pd.DataFrame, lows: pd.DataFrame) -> pd.DataFrame:
    res = {}
    n_all = closes.notna().sum(axis=1)
    for n in LENS:
        m = closes.rolling(n, min_periods=n).mean()
        valid = m.notna() & closes.notna()
        res[f"p{n}"] = (closes > m).where(valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan) * 100
    ch = closes.diff()
    adv, dec = (ch > 0).sum(axis=1), (ch < 0).sum(axis=1)
    res["adv"] = adv / (adv + dec).replace(0, np.nan)
    res["ad_line"] = (adv - dec).cumsum()
    hi = closes >= highs.rolling(252, min_periods=200).max()
    lo = closes <= lows.rolling(252, min_periods=200).min()
    res["nh"], res["nl"] = hi.sum(axis=1), lo.sum(axis=1)
    res["n"] = n_all
    out = pd.DataFrame(res)
    out["zweig"] = out["adv"].ewm(span=10, adjust=False).mean()
    return out[out["n"] >= 0.5 * n_all.max()]


# ======================================================================== события и статистика
def debounce(mask: pd.Series, gap: int) -> pd.Series:
    """Только первое срабатывание: предыдущее было не ближе gap сессий."""
    idx = np.flatnonzero(mask.to_numpy())
    keep, last = [], -10 ** 9
    for i in idx:
        if i - last > gap:
            keep.append(i)
        last = i
    out = pd.Series(False, index=mask.index)
    out.iloc[keep] = True
    return out


def cross_below(s: pd.Series, lvl: float) -> pd.Series:
    return (s < lvl) & (s.shift(1) >= lvl)


def cross_above(s: pd.Series, lvl: float) -> pd.Series:
    return (s > lvl) & (s.shift(1) <= lvl)


def fwd_table(close: pd.Series) -> pd.DataFrame:
    t = {f"f{h}": close.shift(-h) / close - 1 for h in HORIZONS}
    fut = pd.concat([close.shift(-j) for j in range(1, 21)], axis=1).min(axis=1)
    t["dd20"] = (fut / close - 1).where(close.shift(-20).notna())
    return pd.DataFrame(t)


def stats_of(dates: pd.DatetimeIndex, fwd: pd.DataFrame) -> dict:
    f = fwd.reindex(dates)
    out = dict(n=int(len(dates)))
    for h in HORIZONS:
        x = f[f"f{h}"].dropna()
        out[f"m{h}"] = round(float(x.median() * 100), 2) if len(x) else None
        out[f"a{h}"] = round(float(x.mean() * 100), 2) if len(x) else None
        out[f"w{h}"] = round(float((x > 0).mean() * 100), 0) if len(x) else None
        out[f"k{h}"] = int(len(x))
    x = f["dd20"].dropna()
    out["dd20"] = round(float(x.median() * 100), 2) if len(x) else None
    out["last"] = [d.strftime("%Y-%m-%d") for d in dates[-6:]]
    return out


LINE_NAME = {20: "S5TW", 50: "S5FI", 100: "S5OH", 200: "S5TH"}


def breadth_events(b: pd.DataFrame, idx: pd.Series, P: Params) -> Dict[str, pd.Series]:
    """Все события широты как булевы ряды по датам (для статистики и алертов)."""
    ev: Dict[str, pd.Series] = {}
    for n in LENS:
        s = b[f"p{n}"]
        for lvl in sorted({10.0, 15.0, 20.0, P.low}):
            ev[f"p{n}|lo|in|{lvl:g}"] = cross_below(s, lvl)
            ev[f"p{n}|lo|out|{lvl:g}"] = cross_above(s, lvl)
        for lvl in sorted({70.0, 80.0, 85.0, 90.0, P.high}):
            ev[f"p{n}|hi|in|{lvl:g}"] = cross_above(s, lvl)
            ev[f"p{n}|hi|out|{lvl:g}"] = cross_below(s, lvl)
    z = b["zweig"]
    ev["zweig"] = cross_above(z, 0.615) & (z.rolling(10).min() < 0.40)
    # дивергенции: индекс у максимума/минимума 60 дней, а широта (50д) далеко от своего
    ix = idx.reindex(b.index)
    p50 = b["p50"]
    ev["div_bear"] = (ix >= ix.rolling(60).max() * 0.99) & (p50 <= p50.rolling(60).max() - 20)
    ev["div_bull"] = (ix <= ix.rolling(60).min() * 1.01) & (p50 >= p50.rolling(60).min() + 20)
    return {k: debounce(v.fillna(False).astype(bool), 20 if k.startswith(("div", "zweig")) else P.debounce)
            for k, v in ev.items()}


def event_title(key: str) -> str:
    if key == "zweig":
        return "Zweig Breadth Thrust: доля растущих (EMA 10) рывком с < 40% до > 61,5% за 10 дней"
    if key == "div_bear":
        return "Медвежья дивергенция: SPX у максимума 60 дней, а широта 50д на 20+ п. ниже своего пика"
    if key == "div_bull":
        return "Бычья дивергенция: SPX у минимума 60 дней, а широта 50д на 20+ п. выше своего дна"
    if key.startswith("score"):
        _, side, lvl = key.split("|")
        return f"Термометр {'ниже' if side == 'lo' else 'выше'} {lvl}"
    line, side, kind, lvl = key.split("|")
    n = int(line[1:])
    nm = f"% выше SMA {n} ({LINE_NAME[n]})"
    if side == "lo":
        return f"{nm} упал ниже {lvl}%" if kind == "in" else f"{nm} вернулся выше {lvl}% (выход из перепроданности)"
    return f"{nm} поднялся выше {lvl}%" if kind == "in" else f"{nm} ушёл ниже {lvl}% (выход из перекупленности)"


# ======================================================================== ротация и термометр
def rrg(etf: pd.Series, ix: pd.Series, tail: int = 8) -> Optional[dict]:
    w = pd.concat([etf, ix], axis=1).dropna().resample("W-FRI").last().dropna()
    if len(w) < 40:
        return None
    rs = w.iloc[:, 0] / w.iloc[:, 1]
    ratio = 100 * rs.ewm(span=10, adjust=False).mean() / rs.ewm(span=30, adjust=False).mean()
    mom = 100 * ratio / ratio.shift(4)
    pts = [[round(float(a), 3), round(float(b), 3)] for a, b in zip(ratio.tail(tail), mom.tail(tail))
           if not (np.isnan(a) or np.isnan(b))]
    if not pts:
        return None
    x, y = pts[-1]
    q = ("lead" if y >= 100 else "weak") if x >= 100 else ("improve" if y >= 100 else "lag")
    return dict(pts=pts, q=q)


def clip01(x):
    return np.clip(x, 0, 1)


def thermometer(b: pd.DataFrame, H: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Сводная оценка 0–100 по пяти блокам (все ряды дневные; недельные MA — их дневные аналоги)."""
    ix = H[INDEX]["close"].reindex(b.index).ffill()
    comp = pd.DataFrame(index=b.index)
    comp["breadth"] = (b["p20"] + b["p50"] + b["p200"]) / 3
    # SMA 20/50/100/200 дней + аналоги 20/50/100/200 недель (100/250/500/1000 дней)
    tr = [(ix > sma(ix, n)).where(sma(ix, n).notna()) for n in (20, 50, 100, 200, 250, 500, 1000)]
    comp["trend"] = pd.concat(tr, axis=1).mean(axis=1) * 100
    if "RSP" in H and "SPY" in H:
        r = (H["RSP"]["close"] / H["SPY"]["close"]).reindex(b.index).ffill()
        comp["particip"] = clip01((r / sma(r, 50) - 1 + 0.02) / 0.04) * 100
    sec = []
    for t in SECTORS:
        if t in H:
            c = H[t]["close"].reindex(b.index).ffill()
            sec.append((c > sma(c, 50)).where(sma(c, 50).notna()))
    if sec:
        comp["sectors"] = pd.concat(sec, axis=1).mean(axis=1) * 100
    risk = []
    if "^VIX" in H and "^VIX3M" in H:
        v = (H["^VIX"]["close"] / H["^VIX3M"]["close"]).reindex(b.index).ffill()
        risk.append(clip01((1.05 - v) / 0.25) * 100)
    if "HYG" in H:
        hy = H["HYG"]["close"].reindex(b.index).ffill()
        risk.append(clip01((hy / sma(hy, 50) - 1 + 0.015) / 0.03) * 100)
    if risk:
        comp["risk"] = pd.concat(risk, axis=1).mean(axis=1)
    comp["score"] = comp.mean(axis=1)
    return comp


def score_label(x: float) -> str:
    if x is None or np.isnan(x):
        return "—"
    return ("Слабость" if x < 30 else "Осторожно" if x < 45 else "Нейтрально" if x < 60
            else "Здоровый рост" if x < 75 else "Сильный рынок")


# ======================================================================== прогон
def run(P: Params) -> dict:
    import seasonal
    gics = sp500_gics()
    cons = sorted(set(gics["ticker"]))
    extra = [INDEX] + list(SECTORS) + list(THEMES) + RISK
    log.info("S&P 500: %d акций, загрузка истории за %d лет", len(cons), P.years)
    H = seasonal.download(sorted(set(cons + extra)), P.years)
    if INDEX not in H:
        raise RuntimeError("нет истории ^GSPC")
    got = [t for t in cons if t in H]
    if len(got) < 0.8 * len(cons):
        raise RuntimeError(f"история загрузилась только по {len(got)} из {len(cons)} акций")
    log.info("история: акций %d, прочих %d", len(got), len([t for t in extra if t in H]))

    ix_df = H[INDEX]
    days = ix_df.index
    C = pd.DataFrame({t: H[t]["close"] for t in got}).reindex(days)
    Hh = pd.DataFrame({t: H[t]["high"] for t in got}).reindex(days)
    Ll = pd.DataFrame({t: H[t]["low"] for t in got}).reindex(days)
    b = breadth(C, Hh, Ll)
    ix = ix_df["close"]
    fwd = fwd_table(ix)

    # ---------------- события и статистика
    ev = breadth_events(b, ix, P)
    th = thermometer(b, H)
    for lvl in (30, 45):
        ev[f"score|lo|{lvl}"] = debounce(cross_below(th["score"], lvl).fillna(False), P.debounce)
    for lvl in (60, 75):
        ev[f"score|hi|{lvl}"] = debounce(cross_above(th["score"], lvl).fillna(False), P.debounce)
    stats = {}
    for k, m in ev.items():
        st = stats_of(m.index[m.to_numpy()], fwd)
        st["title"] = event_title(k)
        stats[k] = st
    base = stats_of(fwd.index[fwd["f60"].notna()], fwd)
    base["title"] = "Любой день (для сравнения)"
    stats["base"] = base
    recent = []
    for k, m in ev.items():
        hits = m.index[m.to_numpy()]
        for d in hits[hits >= b.index[-min(len(b), 10)]]:
            recent.append(dict(key=k, date=d.strftime("%Y-%m-%d"), title=event_title(k)))
    recent.sort(key=lambda r: r["date"], reverse=True)

    # ---------------- веса
    caps = market_caps(got, P.caps_days)
    g = gics[gics["ticker"].isin(got)].copy()
    g["cap"] = g["ticker"].map(caps)
    g["company"] = g["name"].map(company_of)
    comp = (g.dropna(subset=["cap"]).sort_values("cap", ascending=False)
            .groupby("company", sort=False).agg(ticker=("ticker", "first"), cap=("cap", "max"),
                                                sector=("sector", "first"), sub=("sub", "first")))
    total = comp["cap"].sum()
    wsec = (comp.groupby("sector")["cap"].sum() / total * 100) if total else pd.Series(dtype=float)
    wsemi = comp[comp["sub"].isin(SEMI_SUB)]["cap"].sum() / total * 100 if total else None
    mega = comp.head(10)
    mega_list = list(mega["ticker"]) if len(mega) == 10 else MEGA_FALLBACK
    top10 = round(float(mega["cap"].sum() / total * 100), 1) if total and len(mega) == 10 else None

    # широта внутри секторов
    last = C.iloc[-1]
    sec_breadth = {}
    for etf, (_, gs) in SECTORS.items():
        ts = [t for t in gics[gics["sector"] == gs]["ticker"] if t in C.columns]
        if not ts:
            continue
        row = {}
        for n in (20, 50, 200):
            m = C[ts].rolling(n, min_periods=n).mean().iloc[-1]
            ok = m.notna() & last[ts].notna()
            row[f"p{n}"] = round(float((last[ts][ok] > m[ok]).mean() * 100), 0) if ok.any() else None
        row["n"] = len(ts)
        sec_breadth[etf] = row

    # ---------------- компоненты
    rows = []

    def add(t, name, group, weight=None, sector=None):
        if t not in H:
            return
        try:
            r = instrument(H[t], P)
        except Exception as e:
            log.warning("%s: %s", t, e)
            return
        r.update(ticker=t, name=name, group=group, w=None if weight is None else round(float(weight), 2))
        if t in SECTORS or t in THEMES:
            r["rrg"] = rrg(H[t]["close"], ix)
        if t in sec_breadth:
            r["breadth"] = sec_breadth[t]
        if sector:
            r["sector"] = sector
        rows.append(r)

    add(INDEX, "S&P 500", "index", 100.0)
    for t, (nm, gs) in SECTORS.items():
        add(t, nm, "sector", wsec.get(gs) if len(wsec) else None)
    for t, nm in THEMES.items():
        add(t, nm, "theme", wsemi if t == "SMH" else None)
    gmap = dict(zip(gics["ticker"], gics["name"]))
    secname = {gs: nm for _, (nm, gs) in SECTORS.items()}
    for t in mega_list:
        w = comp.loc[comp["ticker"] == t, "cap"].sum() / total * 100 if total else None
        sec = gics.loc[gics["ticker"] == t, "sector"]
        add(t, company_of(gmap.get(t, t)), "mega", w, secname.get(sec.iloc[0]) if len(sec) else None)
    for r in rows:                                       # вклад в SPX, п.п.
        for n in (1, 5, 20):
            r[f"c{n}"] = round(r["w"] * r[f"r{n}"] / 100, 3) if r["w"] is not None and r.get(f"r{n}") is not None else None

    # ---------------- риск
    def lastv(t):
        return float(H[t]["close"].iloc[-1]) if t in H else None

    def chg(t, n):
        if t not in H or len(H[t]) <= n:
            return None
        c = H[t]["close"]
        return round(float(c.iloc[-1] - c.iloc[-1 - n]), 3)

    risk = {}
    if "^VIX" in H:
        v = H["^VIX"]["close"]
        risk["vix"] = dict(v=round(lastv("^VIX"), 2), d5=chg("^VIX", 5),
                           pct1y=round(float((v.tail(252) < v.iloc[-1]).mean() * 100), 0))
    if "^VIX" in H and "^VIX3M" in H:
        risk["term"] = dict(v=round(lastv("^VIX") / lastv("^VIX3M"), 3))
    if "^VIX9D" in H and "^VIX" in H:
        risk["short"] = dict(v=round(lastv("^VIX9D") / lastv("^VIX"), 3))
    if "HYG" in H:
        c = H["HYG"]["close"]
        risk["hyg"] = dict(v=round(lastv("HYG"), 2), dev50=round(float((c.iloc[-1] / c.tail(50).mean() - 1) * 100), 2),
                           r20=round(float((c.iloc[-1] / c.iloc[-21] - 1) * 100), 2))
    if "^TNX" in H:
        risk["tnx"] = dict(v=round(lastv("^TNX"), 3), d20=chg("^TNX", 20))
    if "DX-Y.NYB" in H:
        c = H["DX-Y.NYB"]["close"]
        risk["dxy"] = dict(v=round(lastv("DX-Y.NYB"), 2), dev50=round(float((c.iloc[-1] / c.tail(50).mean() - 1) * 100), 2))
    if "^COR1M" in H:
        c = H["^COR1M"]["close"]
        risk["cor"] = dict(v=round(lastv("^COR1M"), 2), pct1y=round(float((c.tail(252) < c.iloc[-1]).mean() * 100), 0))

    # ---------------- история для графиков
    hist = b.join(th[["score"]]).tail(P.chart_days)
    ser = dict(d=[d.strftime("%Y-%m-%d") for d in hist.index],
               spx=[round(float(x), 2) for x in ix.reindex(hist.index)],
               score=[None if pd.isna(x) else round(float(x), 1) for x in hist["score"]])
    for n in LENS:
        ser[f"p{n}"] = [None if pd.isna(x) else round(float(x), 1) for x in hist[f"p{n}"]]
    ser["zweig"] = [None if pd.isna(x) else round(float(x), 3) for x in hist["zweig"]]
    ser["nh"] = [int(x) for x in hist["nh"]]
    ser["nl"] = [int(x) for x in hist["nl"]]

    lb, lt = b.iloc[-1], th.iloc[-1]
    now = dict(date=b.index[-1].strftime("%Y-%m-%d"), n=int(lb["n"]),
               zweig=round(float(lb["zweig"]), 3), nh=int(lb["nh"]), nl=int(lb["nl"]),
               score=round(float(lt["score"]), 1), label=score_label(float(lt["score"])),
               parts={k: (None if pd.isna(v) else round(float(v), 1)) for k, v in lt.items() if k != "score"},
               top10=top10)
    for n in LENS:
        s = b[f"p{n}"]
        now[f"p{n}"] = round(float(s.iloc[-1]), 1)
        now[f"p{n}_d5"] = round(float(s.iloc[-1] - s.iloc[-6]), 1)
    return dict(updated=pd.Timestamp.now(tz="UTC").isoformat(), params=asdict(P), now=now, series=ser,
                rows=rows, risk=risk, stats=stats, recent=recent, history_from=b.index[0].strftime("%Y-%m-%d"))


# ======================================================================== Telegram
ST_NAME = {"support": "у поддержки", "resist": "у сопротивления", "bounce": "отбилась вверх",
           "reject": "отбита вниз", "break_up": "пробой вверх", "break_dn": "пробой вниз"}
ST_ICON = {"support": "🟢", "resist": "🟠", "bounce": "↗️", "reject": "↘️", "break_up": "🚀", "break_dn": "🔻"}
NOTE_STATES = set(ST_NAME)


def tv(t: str, iv: str = "D") -> str:
    sym = {"^GSPC": "SPX"}.get(t, t.replace("-", "."))
    return (f'<a href="https://www.tradingview.com/chart/?symbol={html.escape(sym)}&amp;interval={iv}">'
            f'<b>{html.escape("SPX" if t == INDEX else t)}</b></a>')


def collect_alerts(res: dict, sent: dict) -> tuple:
    """Новые события для Telegram и обновлённый список отправленного."""
    S = res["stats"]
    out_b, out_m = [], []
    today = res["now"]["date"]
    P = res["params"]
    want = {f"p{n}|lo|in|{P['low']:g}" for n in LENS} | {f"p{n}|lo|out|{P['low']:g}" for n in LENS} | \
           {f"p{n}|hi|in|{P['high']:g}" for n in LENS} | {f"p{n}|hi|out|{P['high']:g}" for n in LENS} | \
           {"zweig", "div_bear", "div_bull", "score|lo|30", "score|hi|75"}
    for e in res["recent"]:
        key = f"ev|{e['key']}|{e['date']}"
        if e["key"] in want and key not in sent:
            sent[key] = today
            out_b.append(e)
    # скользящие: дни — 50/100/200, недели — все; один раз на состояние, повтор — после смены
    seen = set()
    for r in res["rows"]:
        for k, s in r["ma"].items():
            tf, n = k[0], int(k[4:])
            if tf == "D" and n == 20:
                continue
            key = f"ma|{r['ticker']}|{k}"
            seen.add(key)
            if s["st"] in NOTE_STATES:
                if sent.get(key) != s["st"]:
                    sent[key] = s["st"]
                    out_m.append((r, k, s))
            else:
                sent.pop(key, None)
    for key in [k for k in sent if k.startswith("ma|") and k not in seen]:
        sent.pop(key)
    return out_b, out_m


def fmt_stat(st: dict) -> str:
    if not st or not st.get("n"):
        return "истории нет"
    m20, w20, m60 = st.get("m20"), st.get("w20"), st.get("m60")
    f = lambda x: "—" if x is None else f"{x:+.1f}%".replace(".", ",")
    k = st["n"]
    raz = "раза" if k % 10 in (2, 3, 4) and k % 100 not in (12, 13, 14) else "раз"
    return (f"было {k} {raz}: SPX через 20 дн. {f(m20)} (медиана), рост в {w20:.0f}% случаев; "
            f"через 60 дн. {f(m60)}") if w20 is not None else f"было {k} {raz}"


def build_message(res: dict, evs: List[dict], mas: List[tuple]) -> str:
    n = res["now"]
    lines = [f"🧭 <b>Панель рынка — {n['date'][8:10]}.{n['date'][5:7]}</b>",
             f"Термометр <b>{n['score']:.0f}</b>/100 — {html.escape(n['label'])} · широта: "
             f"20д {n['p20']:.0f}% · 50д {n['p50']:.0f}% · 100д {n['p100']:.0f}% · 200д {n['p200']:.0f}%"]
    if evs:
        lines += ["", "<b>Широта и режим рынка</b>"]
        for e in evs:
            lines.append(f"• {html.escape(e['title'])}\n   <i>{html.escape(fmt_stat(res['stats'].get(e['key'])))}</i>")
    if mas:
        lines += ["", "<b>Компоненты SPX у своих скользящих</b>"]
        by = {}
        for r, k, s in mas:
            by.setdefault(r["ticker"], (r, []))[1].append((k, s))
        order = {"index": 0, "sector": 1, "theme": 2, "mega": 3}
        for t, (r, items) in sorted(by.items(), key=lambda x: (order.get(x[1][0]["group"], 9), -(x[1][0]["w"] or 0))):
            w = f" · вес {r['w']:.1f}%".replace(".", ",") if r.get("w") else ""
            parts = []
            for k, s in sorted(items, key=lambda x: (x[0][0] != "W", int(x[0][4:]))):
                tfn = "W" if k[0] == "W" else "D"
                parts.append(f"{ST_ICON[s['st']]} {k[1:4]} {k[4:]}{tfn} {ST_NAME[s['st']]} ({s['atr']:+.1f} ATR)".replace(".", ","))
            lines.append(f"• {tv(t, 'W')} {html.escape(r['name'])}{w}\n   " + "\n   ".join(parts))
    lines += ["", "<i>Вся панель — вкладка «Рынок» на сайте. Не инвестиционная рекомендация.</i>"]
    return "\n".join(lines)


def load_sent() -> dict:
    try:
        return json.loads(SENT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    import bot
    import tg
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tg", action="store_true")
    ap.add_argument("--if-new", action="store_true", help="пропустить, если сегодня после закрытия уже считали")
    a = ap.parse_args()
    now = pd.Timestamp.now(tz=ET)
    if a.if_new:
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        if prev.get("today") == now.date().isoformat() and prev.get("after_close"):
            log.info("за сегодня уже посчитано — пропускаю")
            out = os.environ.get("GITHUB_OUTPUT")
            if out:
                with open(out, "a") as f:
                    f.write("skip=true\n")
            return
    P = Params.from_env()
    res = run(P)
    res["today"] = now.date().isoformat()
    res["after_close"] = (now.hour, now.minute) >= (16, 15)
    sent = load_sent()
    first = not sent
    evs, mas = collect_alerts(res, sent)
    if first:
        mas = []            # первый запуск: текущие состояния запоминаем молча, иначе придёт простыня
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    SENT_FILE.write_text(json.dumps(sent, separators=(",", ":")), encoding="utf-8")
    log.info("термометр %.0f, событий широты %d, скользящих %d%s", res["now"]["score"], len(evs), len(mas),
             " (первый запуск)" if first else "")
    msg = build_message(res, evs, mas) if (evs or mas) else ""
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if msg and not a.no_tg and token:
        k = tg.broadcast_one(token, tg.load(), msg, key="market")
        log.info("разослано подписчикам: %d", k)
    else:
        print(msg or "новых событий нет")


if __name__ == "__main__":
    main()
