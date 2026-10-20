"""
Недельный хаммер + колл-бабочка — проверка на истории.

Сигнал (по закрытию недели, обычно пятница):
  1) диапазон свечи H−L не меньше MIN_RANGE (5%) от закрытия;
  2) хаммер: открытие и закрытие в верхних 2/5 свечи — min(O, C) ≥ L + 0.6·(H−L), цвет не важен;
  3) минимум недели ниже минимумов каждой из 9 предыдущих недель;
  4) закрытие выше 52-недельной SMA (минимум может быть ниже);
  5) цена ≥ $5, средний дневной оборот ≥ $5 млн.

Цель: H + (H−L). Бабочка колл, симметричная:
  покупка кола со страйком около закрытия (Kl), продажа 2 колов у цели (Km), покупка крыла Kh = 2·Km − Kl.
  Экспирации: через 4 и через 5 недель (28 и 35 дней от пятницы).

Опционных цен за прошлые годы нет, поэтому бабочка оценивается по Блэку–Шоулзу:
  волатильность = историческая за 20 дней × IV_MULT, одна для всех страйков (без улыбки).
  Фильтр «кредит 2 проданных ≥ 50% дебета 2 купленных» проверяется по этой же модели.
  Результат на экспирации считается точно (выплата бабочки по цене закрытия акции).

Для сравнения те же расчёты делаются для:
  no_low — хаммер выше SMA, но без нового 9-недельного минимума;
  base   — любая неделя с диапазоном ≥ 5% выше SMA (без хаммера и минимума).

  python hammer.py                      — все ликвидные акции США + ETF → cache/hammer_backtest.json
  python hammer.py --tickers QCOM,AAPL
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "hammer_backtest.json"
log = logging.getLogger("ob_bot.hammer")

MIN_RANGE = 0.05      # H−L от закрытия
TOP_PART = 0.40       # тело в верхних 2/5
LOW_WEEKS = 9         # минимум ниже минимумов N предыдущих недель
SMA_WEEKS = 52
EXPIRIES = (4, 5)     # недель до экспирации
IV_MULT = 1.10        # подразумеваемая волатильность ≈ историческая × 1.1
IV_MULTS_ALT = (1.0, 1.3)
RATE = 0.03
FRICTION = 4 * (0.0065 + 0.01)   # на одну бабочку, $ на акцию, за одну сторону сделки
MIN_PRICE = 5.0
MIN_DVOL = 5e6
YEARS = 20

ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
        "XLRE", "XLC", "SMH", "XBI", "GLD", "SLV", "TLT", "HYG", "EEM", "EFA", "FXI", "KRE", "XOP",
        "GDX", "ARKK", "USO", "TQQQ", "SOXL", "SQQQ", "TNA", "LABU"]


# ------------------------------------------------------------------ модель опционов
def _ncdf(x):
    return 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))


def bs_call(S, K, t, sigma, r=RATE):
    S, K, t, sigma = map(np.asarray, (S, K, t, sigma))
    vt = sigma * np.sqrt(t)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / vt
    d2 = d1 - vt
    return S * _ncdf(d1) - K * np.exp(-r * t) * _ncdf(d2)


def strike_step(p: float) -> float:
    if p < 25:
        return 0.5
    if p < 100:
        return 1.0
    if p < 200:
        return 2.5
    if p < 500:
        return 5.0
    return 10.0


def fly(S: float, T: float, t_years: float, sigma: float) -> Optional[dict]:
    st = strike_step(S)
    kl = round(S / st) * st
    km = round(T / st) * st
    if km <= kl:
        km = kl + st
    kh = 2 * km - kl
    c = bs_call(S, np.array([kl, km, kh]), t_years, sigma)
    debit, credit = float(c[0] + c[2]), float(2 * c[1])
    cost = debit - credit
    if cost <= 0.01 * S * 0.01:
        return None
    return dict(kl=kl, km=km, kh=kh, debit=debit, credit=credit, cost=cost, ratio=credit / debit)


def payoff(S, kl, km, kh):
    return max(S - kl, 0) - 2 * max(S - km, 0) + max(S - kh, 0)


# ------------------------------------------------------------------ недели и сигналы
def weekly(df: pd.DataFrame, complete_only: bool = True) -> pd.DataFrame:
    d = df.copy()
    d["dv"] = d["close"] * d["volume"]
    d["day"] = d.index
    w = d.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                 "dv": "mean", "day": "last"}).dropna(subset=["open", "close"])
    if complete_only and len(w) and not week_closed(w.index[-1], df.index[-1]):
        w = w.iloc[:-1]                      # текущая неделя ещё не закрылась
    return w


def week_closed(fri: pd.Timestamp, last_day: pd.Timestamp) -> bool:
    """Неделя с пятницей fri закрыта, если в данных есть её пятница (или четверг перед праздничной пятницей)."""
    last = pd.Timestamp(last_day).tz_localize(None).normalize() if pd.Timestamp(last_day).tzinfo else pd.Timestamp(last_day).normalize()
    fri = pd.Timestamp(fri).normalize()
    return last >= fri or (fri - last == pd.Timedelta(days=1) and is_holiday(fri))


def is_holiday(d: pd.Timestamp) -> bool:
    try:
        from pandas.tseries.holiday import USFederalHolidayCalendar
        good_friday = {pd.Timestamp(x) for x in ("2026-04-03", "2027-03-26", "2028-04-14")}
        return d in good_friday or d in set(USFederalHolidayCalendar().holidays(d - pd.Timedelta(days=1), d + pd.Timedelta(days=1)))
    except Exception:
        return False


def flags(w: pd.DataFrame) -> Dict[str, np.ndarray]:
    o, h, l, c = (w[k].to_numpy() for k in ("open", "high", "low", "close"))
    rng = h - l
    size = rng >= MIN_RANGE * c
    ham = np.minimum(o, c) >= l + (1 - TOP_PART) * rng
    prev_low = w["low"].rolling(LOW_WEEKS).min().shift(1).to_numpy()
    newlow = l < prev_low
    sma = w["close"].rolling(SMA_WEEKS).mean().to_numpy()
    above = c > sma
    liq = (c >= MIN_PRICE) & (w["dv"].rolling(4).median().to_numpy() >= MIN_DVOL)
    ok = size & above & liq & ~np.isnan(sma) & ~np.isnan(prev_low)
    return {"signal": ok & ham & newlow, "no_low": ok & ham & ~newlow, "base": ok}


def outcome(df: pd.DataFrame, w: pd.DataFrame, j: int, iv_mult: float = IV_MULT) -> Optional[dict]:
    """Результат сигнала на неделе j (по закрытию этой недели)."""
    H, L, C = float(w["high"].iloc[j]), float(w["low"].iloc[j]), float(w["close"].iloc[j])
    T = H + (H - L)
    d0 = w["day"].iloc[j]
    p0 = df.index.get_loc(d0)
    closes = df["close"].to_numpy()
    lr = np.diff(np.log(closes[max(0, p0 - 20):p0 + 1]))
    if len(lr) < 15:
        return None
    hv = float(np.std(lr, ddof=1) * math.sqrt(252))
    sigma = max(hv * iv_mult, 0.15)
    r = dict(date=str(d0.date()), close=round(C, 2), high=round(H, 2), low=round(L, 2), target=round(T, 2),
             range_pct=round((H - L) / C * 100, 1), hv=round(hv * 100, 1))
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    for n in EXPIRIES:
        if j + n >= len(w):
            return None
        de = w["day"].iloc[j + n]
        pe = df.index.get_loc(de)
        if pe <= p0:
            return None
        days = (de - d0).days
        f = fly(C, T, days / 365.0, sigma)
        if f is None:
            return None
        hseg, lseg = highs[p0 + 1:pe + 1], lows[p0 + 1:pe + 1]
        hit_idx = np.nonzero(hseg >= T)[0]
        brk_idx = np.nonzero(lseg < L)[0]
        Se = float(closes[pe])
        pay = payoff(Se, f["kl"], f["km"], f["kh"])
        r[f"w{n}"] = dict(
            kl=f["kl"], km=f["km"], kh=f["kh"], cost=round(f["cost"], 4), ratio=round(f["ratio"], 3),
            maxR=round((f["km"] - f["kl"] - f["cost"]) / f["cost"], 2),
            hit=bool(len(hit_idx)), hit_day=int(hit_idx[0] + 1) if len(hit_idx) else None,
            broke=bool(len(brk_idx)),
            broke_first=bool(len(brk_idx) and (not len(hit_idx) or brk_idx[0] < hit_idx[0])),
            exp_ret=round((Se / C - 1) * 100, 2),
            zone=("below" if Se <= f["kl"] else "lower" if Se < f["km"] else "upper" if Se < f["kh"] else "above"),
            pnl=round((pay - f["cost"]) / f["cost"], 3),        # прибыль в долях дебета бабочки
            net=round((pay - (FRICTION if pay > 0 else 0) - f["cost"] - FRICTION) / (f["cost"] + FRICTION), 3),
            pay=round(pay, 4),
        )
    return r


# ------------------------------------------------------------------ сводка
def summarize(rows: List[dict], n: int) -> dict:
    xs = [r[f"w{n}"] for r in rows if f"w{n}" in r]
    if not xs:
        return {"n": 0}
    pnl = np.array([x["pnl"] for x in xs])
    ok = [x for x in xs if x["ratio"] >= 0.5]
    pnl_ok = np.array([x["pnl"] for x in ok]) if ok else np.array([])
    net_ok = np.array([x["net"] for x in ok]) if ok else np.array([])

    def trim(a):          # среднее без 1% лучших сделок — не держится ли результат на единичных выбросах
        if not len(a):
            return None
        a = np.sort(a)[: max(1, int(len(a) * 0.99))]
        return round(float(a.mean()) * 100, 1)
    zones = pd.Series([x["zone"] for x in xs]).value_counts(normalize=True)
    hd = [x["hit_day"] for x in xs if x["hit"]]
    return dict(
        n=len(xs),
        hit=round(np.mean([x["hit"] for x in xs]) * 100, 1),
        hit_day_med=float(np.median(hd)) if hd else None,
        broke_first=round(np.mean([x["broke_first"] for x in xs]) * 100, 1),
        exp_ret_mean=round(float(np.mean([x["exp_ret"] for x in xs])), 2),
        exp_ret_med=round(float(np.median([x["exp_ret"] for x in xs])), 2),
        exp_up=round(np.mean([x["exp_ret"] > 0 for x in xs]) * 100, 1),
        zones={k: round(float(v) * 100, 1) for k, v in zones.items()},
        fly_win=round(float((pnl > 0).mean()) * 100, 1),
        fly_mean=round(float(pnl.mean()) * 100, 1),
        fly_med=round(float(np.median(pnl)) * 100, 1),
        fly_big=round(float((pnl >= 1.0).mean()) * 100, 1),
        maxR_med=round(float(np.median([x["maxR"] for x in xs])) * 100, 0),
        ratio_ok=round(len(ok) / len(xs) * 100, 1),
        ok_n=len(ok),
        ok_win=round(float((pnl_ok > 0).mean()) * 100, 1) if len(pnl_ok) else None,
        ok_mean=round(float(pnl_ok.mean()) * 100, 1) if len(pnl_ok) else None,
        ok_med=round(float(np.median(pnl_ok)) * 100, 1) if len(pnl_ok) else None,
        ok_trim=trim(pnl_ok),
        ok_net=round(float(net_ok.mean()) * 100, 1) if len(net_ok) else None,
        ok_net_trim=trim(net_ok),
        ok_big=round(float((pnl_ok >= 1.0).mean()) * 100, 1) if len(pnl_ok) else None,
        ok_cost_pct=round(float(np.median([x["cost"] / x["kl"] for x in ok])) * 100, 2) if ok else None,
    )


def backtest(hist: Dict[str, pd.DataFrame], liquid_opt: set, base_every: int = 3) -> dict:
    groups: Dict[str, List[dict]] = {"signal": [], "no_low": [], "base": []}
    alt: Dict[float, List[dict]] = {m: [] for m in IV_MULTS_ALT}
    recent = []
    cutoff = None
    for t, df in hist.items():
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df[(df["open"] > 0) & (df["low"] > 0)]
        if len(df) < 300:
            continue
        try:
            w = weekly(df)
            F = flags(w)
            last_day = df.index[-1]
            cutoff = last_day if cutoff is None or last_day > cutoff else cutoff
            for g, arr in F.items():
                idx = np.nonzero(arr)[0]
                if g == "base":
                    idx = idx[::base_every]
                for j in idx:
                    if g == "signal" and w["day"].iloc[j] >= last_day - pd.Timedelta(days=35):
                        recent.append(dict(ticker=t, date=str(w["day"].iloc[j].date()),
                                           close=round(float(w["close"].iloc[j]), 2),
                                           high=round(float(w["high"].iloc[j]), 2),
                                           low=round(float(w["low"].iloc[j]), 2),
                                           opt=t in liquid_opt))
                    r = outcome(df, w, int(j))
                    if not r:
                        continue
                    r["ticker"] = t
                    r["opt"] = t in liquid_opt
                    groups[g].append(r)
                    if g == "signal":
                        for m in IV_MULTS_ALT:
                            ra = outcome(df, w, int(j), m)
                            if ra:
                                ra["opt"] = r["opt"]
                                alt[m].append(ra)
        except Exception as ex:
            log.warning("%s: %s", t, ex)

    def block(rows):
        out = {}
        for n in EXPIRIES:
            out[f"w{n}"] = summarize(rows, n)
        return out

    res = dict(tickers=len(hist), opt_tickers=len(liquid_opt & set(hist)), groups={}, opt={}, periods={},
               iv_alt={}, recent=sorted(recent, key=lambda x: (x["date"], x["ticker"]), reverse=True))
    for g, rows in groups.items():
        res["groups"][g] = block(rows)
        res["opt"][g] = block([r for r in rows if r["opt"]])
    sig = groups["signal"]
    for pn, lo, hi in (("2006-2013", "2006", "2014"), ("2014-2019", "2014", "2020"), ("2020-2026", "2020", "2027")):
        res["periods"][pn] = block([r for r in sig if lo <= r["date"] < hi])
    for m, rows in alt.items():
        res["iv_alt"][str(m)] = block(rows)
        res["iv_alt"][str(m) + "_opt"] = block([r for r in rows if r["opt"]])
    if len(hist) <= 30:
        res["events"] = sig
    return res


def report(res: dict) -> str:
    lines = [f"акций/ETF: {res['tickers']}, из них с ликвидными опционами: {res['opt_tickers']}"]
    hdr = "{:<14}{:>7}{:>7}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}"
    lines.append(hdr.format("группа", "n", "цель%", "лом%", "акц.ср", "fly+%", "flyср", "flyмед", "≥+100%", "50%ок", "ок.ср"))

    def row(name, s):
        if not s.get("n"):
            return f"{name:<14}     0"
        return hdr.format(name, s["n"], s["hit"], s["broke_first"], s["exp_ret_mean"], s["fly_win"], s["fly_mean"],
                          s["fly_med"], s["fly_big"], s["ratio_ok"], s["ok_mean"] if s["ok_mean"] is not None else "-")
    for n in EXPIRIES:
        lines.append(f"--- экспирация {n} нед.")
        for g in ("signal", "no_low", "base"):
            lines.append(row(g, res["groups"][g][f"w{n}"]))
        for g in ("signal", "no_low", "base"):
            lines.append(row(g + "/опц", res["opt"][g][f"w{n}"]))
        for pn, b in res["periods"].items():
            lines.append(row(pn, b[f"w{n}"]))
        for m, b in res["iv_alt"].items():
            lines.append(row("iv×" + m, b[f"w{n}"]))
    return "\n".join(lines)


# ================================================================== субботний сканер
SCAN_FILE = BASE / "cache" / "hammer.json"
OI_MIN = 50000          # открытый интерес коллов + путов по экспирациям в ближайшие OI_DAYS дней
OI_DAYS = 35            # 35, а не 30: иначе в неделю после месячной экспирации месячная следующего месяца
                        # выпадает из окна и OI «проседает» вдвое (так было 19.10: 145 → 77 тикеров)
DTE_MIN, DTE_MAX = 24, 35
SITE_URL = "https://luxeon87-21q.github.io/ob-bot/#hammer"


def candidates(hist: Dict[str, pd.DataFrame]) -> tuple:
    """Тикеры, у которых последняя закрытая неделя — сигнал. Возвращает (неделя, список)."""
    weeks = {}
    for t, df in hist.items():
        df = df.dropna(subset=["open", "high", "low", "close"])
        if len(df) < 300:
            continue
        w = weekly(df)
        if len(w) < SMA_WEEKS + 2:
            continue
        weeks[t] = w
    if not weeks:
        return None, []
    fri = max(w.index[-1] for w in weeks.values())
    out = []
    for t, w in weeks.items():
        if w.index[-1] != fri:
            continue
        F = flags(w)
        j = len(w) - 1
        if not F["signal"][j]:
            continue
        H_, L_, C_ = (float(w[k].iloc[j]) for k in ("high", "low", "close"))
        out.append(dict(ticker=t, day=str(w["day"].iloc[j].date()), open=round(float(w["open"].iloc[j]), 2),
                        high=round(H_, 2), low=round(L_, 2), close=round(C_, 2), target=round(2 * H_ - L_, 2),
                        range_pct=round((H_ - L_) / C_ * 100, 1),
                        sma52=round(float(w["close"].rolling(SMA_WEEKS).mean().iloc[j]), 2),
                        prev_low=round(float(w["low"].iloc[j - LOW_WEEKS:j].min()), 2)))
    return str(fri.date()), out


def _num(x):
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except Exception:
        return None


def build_fly(calls: pd.DataFrame, close: float, target: float, day: Optional[str] = None) -> Optional[dict]:
    """Симметричная бабочка: продаём 2 кола у цели, покупаем кол у цены закрытия и крыло на той же ширине выше."""
    c = calls.copy()
    c["strike"] = pd.to_numeric(c["strike"], errors="coerce")
    c = c.dropna(subset=["strike"]).set_index("strike").sort_index()
    if c.empty:
        return None
    ks = c.index.to_numpy()
    km = float(ks[np.argmin(np.abs(ks - target))])
    best = None
    for kl in sorted((k for k in ks if k < km and k <= close * 1.03), key=lambda k: abs(k - close))[:4]:
        kh = 2 * km - kl
        hit = ks[np.isclose(ks, kh, atol=1e-6)]
        if len(hit):
            best = (float(kl), float(hit[0]))
            break
    if not best:
        return None
    kl, kh = best

    def leg(k):
        r = c.loc[k]
        last, bid, ask = _num(r.get("lastPrice")), _num(r.get("bid")), _num(r.get("ask"))
        mid = (bid + ask) / 2 if bid and ask else None
        lt = r.get("lastTradeDate")
        return dict(k=k, last=last, bid=bid, ask=ask, mid=round(mid, 3) if mid else None,
                    iv=round(_num(r.get("impliedVolatility")) * 100, 1) if _num(r.get("impliedVolatility")) else None,
                    oi=int(_num(r.get("openInterest")) or 0), vol=int(_num(r.get("volume")) or 0),
                    traded=str(pd.Timestamp(lt).date()) if lt is not None and str(lt) != "NaT" else None)
    L1, M, H1 = leg(kl), leg(km), leg(kh)
    for x in (L1, M, H1):
        # по страйку в день сигнала не торговали: последняя цена старая — берём середину бид/аск, если она есть
        x["stale"] = bool(day and x["traded"] and x["traded"] < day)
        x["px"], x["src"] = x["last"], "last"
        if (x["stale"] or not x["last"]) and x["mid"]:
            x["px"], x["src"] = x["mid"], "mid"
    if not all(x["px"] for x in (L1, M, H1)):
        return None
    debit = L1["px"] + H1["px"]
    credit = 2 * M["px"]
    cost = debit - credit
    width = km - kl
    r = dict(kl=kl, km=km, kh=kh, legs=[L1, M, H1], debit=round(debit, 3), credit=round(credit, 3),
             ratio=round(credit / debit, 3), cost=round(cost, 3), width=width)
    if cost > 0:
        r.update(max_profit=round(width - cost, 3), rr=round((width - cost) / cost, 2),
                 be_low=round(kl + cost, 2), be_high=round(kh - cost, 2))
    if all(x["mid"] for x in (L1, M, H1)):
        r["cost_mid"] = round(L1["mid"] + H1["mid"] - 2 * M["mid"], 3)
    if M["iv"] and L1["iv"]:
        r["skew"] = round(M["iv"] - L1["iv"], 1)        # >0 — проданные дороже по IV (нужный наклон)
    return r


def options_for(c: dict) -> dict:
    """OI, экспирации 24–35 дней и бабочка по каждой. Поля error/reason — почему не прошло."""
    import yfinance as yf
    import options
    t = c["ticker"]
    tk = yf.Ticker(t)
    d0 = pd.Timestamp(c["day"])
    oi = options._yahoo_oi(t, d0, OI_DAYS)
    c["oi30"] = oi["oi"] if oi else None
    if not oi or oi["oi"] < OI_MIN:
        c["reason"] = f"OI {c['oi30'] or 0:,} < {OI_MIN:,}".replace(",", " ")
        return c
    exps = [e for e in (tk.options or ()) if DTE_MIN <= (pd.Timestamp(e) - d0).days <= DTE_MAX]
    if not exps:
        c["reason"] = "нет экспирации 24–35 дней"
        return c
    flies = []
    for e in exps:
        try:
            f = build_fly(tk.option_chain(e).calls, c["close"], c["target"], c["day"])
        except Exception as ex:
            log.warning("%s %s: %s", t, e, ex)
            continue
        if f:
            f["exp"] = e
            f["dte"] = (pd.Timestamp(e) - d0).days
            f["ok"] = f["ratio"] >= 0.5 and f["cost"] > 0
            f["stale"] = [x["k"] for x in f["legs"] if x["stale"] and x["src"] == "last"]   # старая цена, замены нет
            f["by_mid"] = [x["k"] for x in f["legs"] if x["src"] == "mid"]
            f["suspect"] = f.get("rr", 0) > 15             # бабочка подозрительно дешёвая — цены рассинхронизированы
            flies.append(f)
    c["flies"] = flies
    good = [f for f in flies if f["ok"]]
    if good:
        c["best"] = max(good, key=lambda f: (not f["suspect"], f.get("rr", 0)))["exp"]
    else:
        c["reason"] = "кредит < 50% дебета" if flies else "нет симметричных страйков"
    try:
        import seasonal
        ed = seasonal.next_earnings(t)
        c["earnings"] = ed.isoformat() if ed else None
    except Exception:
        c["earnings"] = None
    return c


def scan(tickers: List[str]) -> dict:
    import seasonal
    hist = seasonal.download(sorted(set(tickers)), 2)
    SPp = seasonal.Params(min_price=MIN_PRICE, min_dvol=MIN_DVOL)
    hist = {t: df for t, df in hist.items() if seasonal.liquid(df, SPp)}
    week, cand = candidates(hist)
    log.info("неделя %s: по свече прошли %d из %d", week, len(cand), len(hist))
    import time
    # сразу после массовой загрузки свечей Yahoo временно отдаёт пустые цепочки опционов — даём ему остыть
    time.sleep(60 if len(cand) and len(hist) > 100 else 0)

    def one(c):
        for attempt in range(3):
            try:
                options_for(c)
                c.pop("error", None)
                return True
            except Exception as ex:
                c["reason"] = "Yahoo не отдал опционы"
                c["error"] = f"{type(ex).__name__}: {ex}"[:200]
                log.warning("%s (попытка %d): %s", c["ticker"], attempt + 1, c["error"])
                time.sleep(5 * (attempt + 1))
        return False

    for c in cand:
        one(c)
        time.sleep(0.5)
    for rnd in range(2):                     # ещё два круга по тем, где Yahoo отказал
        failed = [c for c in cand if c.get("error")]
        if not failed:
            break
        time.sleep(60)
        for c in failed:
            one(c)
    rows = [c for c in cand if c.get("best")]
    rows.sort(key=lambda c: -max(min(f.get("rr", 0), 15) for f in c["flies"] if f["ok"]))
    rest = [c for c in cand if not c.get("best")]
    return dict(week=week, made=pd.Timestamp.now(tz="UTC").isoformat(), checked=len(hist),
                rows=rows, rest=rest)


def _f(x, d=2):
    return "—" if x is None else f"{x:,.{d}f}".replace(",", " ")


def build_message(res: dict) -> str:
    wk = res["week"]
    head = (f"🔨 <b>Хаммер + бабочка</b> — неделя до {wk[8:10]}.{wk[5:7]}\n"
            f"Проверено {res['checked']} акций и ETF: по свече прошли {len(res['rows']) + len(res['rest'])}, "
            f"с бабочкой по условиям — {len(res['rows'])}.\n")
    if not res["rows"]:
        lines = [head, "Подходящих бабочек нет."]
    else:
        lines = [head]
        for c in res["rows"]:
            f = next(x for x in c["flies"] if x["exp"] == c["best"])
            alt = [x for x in c["flies"] if x["exp"] != c["best"] and x["ok"]]
            lines.append(
                f"\n<b>{c['ticker']}</b>  закр. {_f(c['close'])}  свеча {_f(c['low'])}–{_f(c['high'])} ({c['range_pct']}%)  🎯 {_f(c['target'])}\n"
                f"  {f['exp'][8:10]}.{f['exp'][5:7]} ({f['dte']} дн.): +{_f(f['kl'], 1 if f['kl'] % 1 else 0)} −2×{_f(f['km'], 1 if f['km'] % 1 else 0)} +{_f(f['kh'], 1 if f['kh'] % 1 else 0)} C\n"
                f"  дебет {_f(f['debit'])}, кредит {_f(f['credit'])} ({f['ratio'] * 100:.0f}%) → бабочка {_f(f['cost'])}, "
                f"макс. {_f(f.get('max_profit'))} (×{f.get('rr', 0):.1f})"
                + (f"\n  ещё {alt[0]['exp'][8:10]}.{alt[0]['exp'][5:7]}: {_f(alt[0]['cost'])}, ×{alt[0].get('rr', 0):.1f}" if alt else "")
                + (f"\n  ⚠️ отчёт {c['earnings'][8:10]}.{c['earnings'][5:7]}" if c.get("earnings") and c["earnings"] <= f["exp"] else "")
                + (f"\n  ⚠️ старые цены: {', '.join(_f(k, 1) for k in f['stale'])}" if f["stale"] else "")
                + (f"\n  ⚠️ подозрительно дёшево — проверьте цены в терминале" if f.get("suspect") else ""))
    lines.append(f"\n🌐 {SITE_URL}\n⚠️ Цены — последние сделки пятницы. Не инвестиционная рекомендация.")
    return "\n".join(lines)


def main():
    import seasonal
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers")
    ap.add_argument("--out")
    ap.add_argument("--scan", action="store_true", help="сканер последней закрытой недели (по субботам)")
    ap.add_argument("--no-tg", action="store_true")
    ap.add_argument("--if-new", action="store_true", help="пропустить, если эта неделя уже посчитана")
    a = ap.parse_args()
    if a.scan:
        return main_scan(a)
    if a.tickers:
        tickers = [x.strip().upper() for x in a.tickers.split(",")]
    else:
        SP = seasonal.Params()
        tickers = seasonal.prefilter(seasonal.all_us_stocks(), SP) + ETFS
    try:
        oi = json.loads((BASE / "cache" / "options.json").read_text())
    except Exception:
        oi = {}
    liquid_opt = {t for t, v in oi.items() if (v or {}).get("oi", 0) >= 50000} | set(ETFS)
    hist = seasonal.download(sorted(set(tickers)), YEARS + 1)
    SPp = seasonal.Params(min_price=MIN_PRICE, min_dvol=MIN_DVOL)
    hist = {t: df for t, df in hist.items() if seasonal.liquid(df, SPp)}
    log.info("ликвидных с историей: %d", len(hist))
    res = backtest(hist, liquid_opt)
    out = BASE / a.out if a.out else OUT_FILE
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(report(res))


def main_scan(a):
    import os
    import seasonal
    import bot
    import tg
    bot.load_env(BASE / ".env")
    if a.if_new:
        try:
            done = json.loads(SCAN_FILE.read_text(encoding="utf-8")).get("week")
        except Exception:
            done = None
        now = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
        fri = now - pd.Timedelta(days=(now.weekday() - 4) % 7)
        if done and done >= str(fri.date()):
            log.info("неделя %s уже посчитана — пропускаю", done)
            out = os.environ.get("GITHUB_OUTPUT")
            if out:
                with open(out, "a") as fh:
                    fh.write("skip=true\n")
            return
    if a.tickers:
        tickers = [x.strip().upper() for x in a.tickers.split(",")]
    else:
        tickers = seasonal.prefilter(seasonal.all_us_stocks(), seasonal.Params()) + ETFS
    res = scan(tickers)
    out = BASE / a.out if a.out else SCAN_FILE
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    msg = build_message(res)
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if a.no_tg or not token or a.tickers:
        print(msg)
        return
    k = tg.broadcast_one(token, tg.load(), msg, key="hammer")
    log.info("хаммер разослан подписчикам: %d", k)


if __name__ == "__main__":
    main()
