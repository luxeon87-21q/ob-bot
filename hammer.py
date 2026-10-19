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
def weekly(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["dv"] = d["close"] * d["volume"]
    d["day"] = d.index
    w = d.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                 "dv": "mean", "day": "last"}).dropna(subset=["open", "close"])
    return w


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


def main():
    import seasonal
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers")
    ap.add_argument("--out")
    a = ap.parse_args()
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


if __name__ == "__main__":
    main()
