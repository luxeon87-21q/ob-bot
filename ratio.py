"""
Пут-ратио диагональ под рост IV перед отчётом (идея с AMD: −2 пута ближней серии, +4 пута дальней серии ниже).

Пример: AMD 12.10, цена 620, отчёт 03.11 (через 21 день): −2 пута 580 (20.11), +4 пута 510 (18.12)
за кредит ≈ 1,00. Дельта ≈ 0, гамма ≈ 0, вега положительная (4 дальних пута весят больше 2 ближних) —
позиция зарабатывает на подъёме IV в преддверии отчёта, выход — до отчёта.

Что делает скрипт (по готовой выгрузке скринера cache/optscreen.json, без новых запросов к Yahoo):
  1. кандидаты: акции с отчётом через DAYS_MIN–DAYS_MAX дней, IV30 < IV_MAX, IV Rank ≤ IVR_MAX (низкий процентиль),
     объём опционов ≥ VOL_MIN;
  2. перебирает ближнюю экспирацию E1 (после отчёта, не дальше +28 дней) и дальнюю E2 (≥ E1 + 14 дней),
     короткий страйк K1 (−2…−14% от цены) и длинный K2 (на 4–25% ниже цены, ниже K1), пропорция 2:4;
  3. оставляет комбинации с кредитом по середине ≥ CREDIT_MIN и |дельтой| ≤ DELTA_MAX на лот (2×4, ×100 акций);
  4. считает P&L в день выхода (за день до отчёта) по модели подъёма IV перед событием:
       дисперсия опциона = базовая × T + (ход на отчёте)²,  σ(T) = √(σb² + m²/T).
     σb и m находятся из текущих ATM-IV двух экспираций. Когда до экспирации остаётся меньше дней,
     доля отчёта в цене растёт — IV ближней серии поднимается сильнее. Это базовый сценарий «отчёт оценён так же, как сейчас»;
     плюс сценарий «рынок заложил ход на 25% больше» (обычный подъём IV перед отчётом) и «IV не изменилась»;
     цена — нормально с базовой волатильностью σb за дни до выхода (−2σ…+2σ); при падении акции IV растёт
     (ΔIV ≈ −0,7 · ln(S₂/S)) — так 4 дальних пута зарабатывают на обвале, как AMD в октябре;
  5. залог — как у брокера для диагонального спреда: 2 × (K1 − K2) × 100 (минус кредит);
  6. ранг — ожидаемый P&L в базовом сценарии к залогу, при условии, что худший из ±1,5σ не хуже −MAX_LOSS залога.

  python ratio.py                         — все кандидаты → cache/ratio.json
  python ratio.py --tickers AMD,NVDA      — только эти (без фильтров IV и дней до отчёта)
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
DS_FILE = BASE / "cache" / "optscreen.json"
OUT_FILE = BASE / "cache" / "ratio.json"
log = logging.getLogger("ob_bot.ratio")

DAYS_MIN, DAYS_MAX = 12, 35     # дней до отчёта (в примере 21)
IV_MAX = 0.50                   # IV30 ниже 50%
IVR_MAX = 40                    # «низкий процентиль»
VOL_MIN = 5000
SHORT_N, LONG_N = 2, 4
CREDIT_MIN = -0.25              # по середине: кредит или почти ноль
DELTA_MAX = 8.0                 # на лот 2×4 (в «акциях»), в примере ≈ 3
MAX_LOSS = 0.20                 # худший из ±1,5σ — не хуже 20% залога
OI_MIN = 50
RATE = 0.04
SPOT_VOL_BETA = 0.7             # IV растёт при падении акции: ΔIV ≈ −β · ln(S₂/S). У акций обычно 0,5–1
Z = np.array([-2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2])
W = np.exp(-Z ** 2 / 2); W /= W.sum()


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def npdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def bs_put(S, K, T, iv, r=RATE):
    if T <= 0 or iv <= 0:
        return max(K - S, 0.0), (-1.0 if K > S else 0.0), 0.0, 0.0, 0.0
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + iv * iv / 2) * T) / (iv * sq)
    d2 = d1 - iv * sq
    p = K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1)
    delta = ncdf(d1) - 1
    gamma = npdf(d1) / (S * iv * sq)
    vega = S * npdf(d1) * sq / 100                     # на 1 пункт IV
    theta = (-S * npdf(d1) * iv / (2 * sq) + r * K * math.exp(-r * T) * ncdf(-d2)) / 365
    return p, delta, gamma, vega, theta


def event_split(iv1, T1, iv2, T2):
    """Базовая IV и ход на отчёте из двух экспираций, обе после отчёта."""
    if T2 <= T1:
        return None
    sb2 = (iv2 * iv2 * T2 - iv1 * iv1 * T1) / (T2 - T1)
    if sb2 <= 0:
        return None
    m2 = max(iv1 * iv1 * T1 - sb2 * T1, 0.0)
    return math.sqrt(sb2), math.sqrt(m2)


def atm_iv(rows, S):
    best = None
    for r in rows:
        if r["iv"] and (best is None or abs(r["k"] - S) < abs(best["k"] - S)):
            best = r
    return best["iv"] if best else None


def candidates(ds: dict, only: Optional[List[str]] = None) -> List[dict]:
    today = pd.Timestamp(ds["today"])
    out = []
    for i, t in enumerate(ds["tickers"]):
        if only:
            if t["t"] not in only:
                continue
        else:
            if t.get("etf") or not t.get("earn") or not t.get("iv"):
                continue
            d = (pd.Timestamp(t["earn"]) - today).days
            if not (DAYS_MIN <= d <= DAYS_MAX) or t["iv"] >= IV_MAX or (t.get("vol") or 0) < VOL_MIN:
                continue
            if t.get("ivr") is not None and t["ivr"] > IVR_MAX:
                continue
        out.append(dict(i=i, **t))
    return out


def scan_ticker(t: dict, chain: Dict[str, List[dict]], today: pd.Timestamp) -> List[dict]:
    S = t["p"]
    earn = t.get("earn")
    if not earn:
        return []
    e_day = pd.Timestamp(earn)
    exit_days = max(1, (e_day - today).days - 1)            # выход за день до отчёта
    exps = sorted(chain)
    res = []
    for e1 in exps:
        d1 = (pd.Timestamp(e1) - today).days
        if not (pd.Timestamp(e1) > e_day and (pd.Timestamp(e1) - e_day).days <= 28):
            continue
        for e2 in exps:
            d2 = (pd.Timestamp(e2) - today).days
            if d2 < d1 + 14:
                continue
            iv1, iv2 = atm_iv(chain[e1], S), atm_iv(chain[e2], S)
            if not iv1 or not iv2:
                continue
            sp = event_split(iv1, d1 / 365, iv2, d2 / 365)
            if not sp:
                continue
            sb, m = sp
            T1x, T2x = (d1 - exit_days) / 365, (d2 - exit_days) / 365

            def atm_after(mult):
                return (math.sqrt(sb * sb + (m * mult) ** 2 / T1x), math.sqrt(sb * sb + (m * mult) ** 2 / T2x))

            scen = {"base": atm_after(1.0), "rich": atm_after(1.25)}
            shorts = [r for r in chain[e1] if -0.14 <= r["k"] / S - 1 <= -0.02 and r["bid"] > 0 and r["iv"] and r["oi"] >= OI_MIN]
            longs = [r for r in chain[e2] if -0.25 <= r["k"] / S - 1 <= -0.04 and r["ask"] > 0 and r["iv"] and r["oi"] >= OI_MIN]
            for a in shorts:
                pa = bs_put(S, a["k"], d1 / 365, a["iv"])
                mid_a = (a["bid"] + a["ask"]) / 2
                for b in longs:
                    if b["k"] >= a["k"] - 0.02 * S:
                        continue
                    mid_b = (b["bid"] + b["ask"]) / 2 if b["bid"] > 0 else b["ask"]
                    credit = SHORT_N * mid_a - LONG_N * mid_b
                    if credit < CREDIT_MIN:
                        continue
                    pb = bs_put(S, b["k"], d2 / 365, b["iv"])
                    g = [(-SHORT_N * pa[j] + LONG_N * pb[j]) * 100 for j in range(1, 5)]   # дельта, гамма, вега, тета на лот
                    if abs(g[0]) > DELTA_MAX:
                        continue
                    margin = SHORT_N * (a["k"] - b["k"]) * 100 - max(credit, 0) * 100
                    if margin <= 0:
                        continue
                    slip = (SHORT_N * (a["ask"] - a["bid"]) + LONG_N * max(b["ask"] - b["bid"], 0)) / 2 * 100   # выход по худшей стороне

                    def pnl(ivs):
                        out = []
                        for z in Z:
                            S2 = S * math.exp(z * sb * math.sqrt(exit_days / 365))
                            sv = -SPOT_VOL_BETA * math.log(S2 / S)
                            ia = max(a["iv"] + (ivs[0] - iv1) + sv, 0.05)
                            ib = max(b["iv"] + (ivs[1] - iv2) + sv, 0.05)
                            va = bs_put(S2, a["k"], T1x, ia)[0]
                            vb = bs_put(S2, b["k"], T2x, ib)[0]
                            out.append(100 * (SHORT_N * (mid_a - va) + LONG_N * (vb - mid_b)) - slip)
                        return np.array(out)

                    base, rich, flat = pnl(scen["base"]), pnl(scen["rich"]), pnl((iv1, iv2))
                    worst = min(base[1], base[-2])                           # −1,5σ и +1,5σ
                    if worst < -MAX_LOSS * margin:
                        continue
                    exp_base = float((base * W).sum())
                    res.append(dict(e1=e1, d1=d1, k1=a["k"], e2=e2, d2=d2, k2=b["k"],
                                    bid1=a["bid"], ask1=a["ask"], bid2=b["bid"], ask2=b["ask"],
                                    credit=round(credit, 2), credit_cons=round(SHORT_N * a["bid"] - LONG_N * b["ask"], 2),
                                    delta=round(g[0], 1), gamma=round(g[1], 2), vega=round(g[2], 0), theta=round(g[3], 1),
                                    margin=round(margin), iv1=round(iv1, 4), iv2=round(iv2, 4), sb=round(sb, 4), m=round(m, 4),
                                    iv1_exit=round(scen["base"][0], 4), iv2_exit=round(scen["base"][1], 4),
                                    exit_days=exit_days, slip=round(slip),
                                    exp_base=round(exp_base), exp_rich=round(float((rich * W).sum())),
                                    exp_flat=round(float((flat * W).sum())), worst=round(worst),
                                    grid=[round(x) for x in base], ret=round(exp_base / margin * 100, 1)))
    res.sort(key=lambda r: -r["ret"])
    return res


def run(ds: dict, only: Optional[List[str]] = None) -> dict:
    today = pd.Timestamp(ds["today"])
    cols = {c: i for i, c in enumerate(ds["cols"])}
    cands = candidates(ds, only)
    want = {c["i"] for c in cands}
    chains: Dict[int, Dict[str, List[dict]]] = {}
    for r in ds["puts"]:
        if r[0] not in want:
            continue
        e = ds["exps"][r[cols["ei"]]]
        chains.setdefault(r[0], {}).setdefault(e, []).append(dict(
            k=r[cols["k"]], bid=r[cols["bid"]], ask=r[cols["ask"]], iv=r[cols["iv"]], oi=r[cols["oi"]] or 0))
    rows = []
    for c in cands:
        best = scan_ticker(c, chains.get(c["i"], {}), today)
        rows.append(dict(ticker=c["t"], name=c.get("n"), price=c["p"], earn=c.get("earn"), iv30=c.get("iv"),
                         ivr=c.get("ivr"), ivs=c.get("ivs"), vol=c.get("vol"), n_combos=len(best), best=best[:5]))
    rows.sort(key=lambda r: -(r["best"][0]["ret"] if r["best"] else -1e9))
    return dict(made=ds.get("made"), data_et=ds.get("et"), today=ds["today"], candidates=len(cands),
                params=dict(days=[DAYS_MIN, DAYS_MAX], iv_max=IV_MAX, ivr_max=IVR_MAX, vol_min=VOL_MIN,
                            ratio=[SHORT_N, LONG_N], credit_min=CREDIT_MIN, delta_max=DELTA_MAX, max_loss=MAX_LOSS),
                rows=rows)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers")
    ap.add_argument("--data", default=str(DS_FILE))
    ap.add_argument("--out", default=str(OUT_FILE))
    a = ap.parse_args()
    ds = json.loads(Path(a.data).read_text(encoding="utf-8"))
    only = [x.strip().upper() for x in a.tickers.split(",")] if a.tickers else None
    res = run(ds, only)
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = [r for r in res["rows"] if r["best"]]
    log.info("кандидатов %d, с подходящими комбинациями %d", res["candidates"], len(ok))
    for r in ok[:15]:
        b = r["best"][0]
        log.info("%s %.2f отчёт %s IV %.0f%%: −2×%s %s / +4×%s %s кредит %.2f, залог %d, ожид. %+d (%.1f%%), худший %+d",
                 r["ticker"], r["price"], r["earn"], (r["iv30"] or 0) * 100, b["k1"], b["e1"], b["k2"], b["e2"],
                 b["credit"], b["margin"], b["exp_base"], b["ret"], b["worst"])


if __name__ == "__main__":
    main()
