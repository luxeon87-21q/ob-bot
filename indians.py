"""
Паттерн «Три индейца» (Three Indians / Three Drives) — разворот после трёх толчков.

Бычий (разворот вверх после падения):
  H0 — максимум, с которого началось падение (начало паттерна);
  L1 > L2 > L3 — три минимума подряд, каждый ниже предыдущего («три индейца»);
  H1 > H2 — откаты между ними, каждый ниже предыдущего;
  время L1→L2 и L2→L3 примерно одинаковое (отношение 0.5–2).
  Вход — закрытие выше H2 (пробой последнего отката) после подтверждения L3.
  Стоп — под L3. Цель — H0 (возврат к началу паттерна).
Медвежий — зеркально.

Точки — развороты зигзага (как у волн Вульфа), PIVOT свечей с каждой стороны.

  python indians.py --tickers ABBV            — текущие паттерны по тикерам (дневки и 4H) + их история
  python indians.py --backtest                — статистика по всем ликвидным акциям США (дневки, ~20 лет)
Результат — cache/indians.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from wolfe import zigzag

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "indians.json"
log = logging.getLogger("ob_bot.indians")

PIVOT = 5          # свечей с каждой стороны для разворота (дневки)
MAX_WAIT = 30      # сколько свечей после подтверждения L3 ждём пробоя H2
SPACING = (0.5, 2.0)
HORIZON = 120      # сколько свечей ждём цель/стоп


def _detect_side(high: np.ndarray, low: np.ndarray, close: np.ndarray, L: int, side: str) -> List[dict]:
    """Бычьи паттерны по high/low. Для медвежьих вызывается с перевёрнутыми ценами."""
    piv = zigzag(high, low, L)
    out = []
    n = len(close)
    for k in range(len(piv) - 5):
        seq = piv[k:k + 6]
        if [p[1] for p in seq] != ["H", "L", "H", "L", "H", "L"]:
            continue
        (h0i, _, h0), (l1i, _, l1), (h1i, _, h1), (l2i, _, l2), (h2i, _, h2), (l3i, _, l3) = seq
        if not (l1 > l2 > l3 and h0 > h1 > h2):
            continue
        d1, d2 = l2i - l1i, l3i - l2i
        if d1 <= 0 or d2 <= 0 or not (SPACING[0] <= d2 / d1 <= SPACING[1]):
            continue
        confirm = l3i + L                       # L3 видна как разворот только через L свечей
        if confirm >= n:
            continue
        entry_i = None
        for j in range(confirm, min(n, confirm + MAX_WAIT)):
            if low[j] < l3:                     # новый минимум ниже L3 — паттерн сломан
                break
            if close[j] > h2:
                entry_i = j
                break
        status = "entry" if entry_i is not None else (
            "waiting" if confirm + MAX_WAIT > n and low[confirm:].min() >= l3 else "failed")
        p = dict(side=side, idx=[h0i, l1i, h1i, l2i, h2i, l3i], price=[h0, l1, h1, l2, h2, l3],
                 confirm=confirm, entry_i=entry_i, status=status,
                 fading=bool((h2 - l3) < (h1 - l2)))      # третий толчок слабее второго
        if entry_i is not None:
            entry = close[entry_i]
            target, stop = h0, l3
            res = None
            for j in range(entry_i + 1, min(n, entry_i + 1 + HORIZON)):
                if low[j] <= stop:
                    res = "stop"
                    break
                if high[j] >= target:
                    res = "target"
                    break
            p.update(entry=entry, target=target, stop=stop, result=res,
                     rr=(target - entry) / (entry - stop) if entry > stop else None)
            for hz in (20, 40, 60):
                j = entry_i + hz
                p[f"r{hz}"] = (close[j] / entry - 1) * 100 if j < n else None
        out.append(p)
    return out


def detect(df: pd.DataFrame, L: int = PIVOT) -> List[dict]:
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    bull = _detect_side(h, l, c, L, "bull")
    bear = _detect_side(-l, -h, -c, L, "bear")
    for p in bear:                                   # вернуть цены в нормальный вид
        p["price"] = [-x for x in p["price"]]
        for k in ("entry", "target", "stop"):
            if p.get(k) is not None:
                p[k] = -p[k]
        for hz in (20, 40, 60):          # в перевёрнутых ценах r = изменение цены; для шорта прибыль — со знаком минус
            if p.get(f"r{hz}") is not None:
                p[f"r{hz}"] = -p[f"r{hz}"]
    pats = sorted(bull + bear, key=lambda p: p["idx"][-1])
    for p in pats:
        p["dates"] = [str(df.index[i])[:16] for i in p["idx"]]
        if p.get("entry_i") is not None:
            p["entry_date"] = str(df.index[p["entry_i"]])[:16]
        p["confirm_date"] = str(df.index[min(p["confirm"], len(df) - 1)])[:16]
    return pats


def summarize(pats: List[dict]) -> dict:
    e = [p for p in pats if p.get("entry_i") is not None]
    out = {"patterns": len(pats), "entries": len(e)}
    if not e:
        return out
    done = [p for p in e if p.get("result")]
    out["target_before_stop"] = round(100 * sum(p["result"] == "target" for p in done) / len(done), 1) if done else None
    out["no_result_120"] = sum(p.get("result") is None for p in e)
    rr = [p["rr"] for p in e if p.get("rr")]
    out["rr_median"] = round(float(np.median(rr)), 2) if rr else None
    for hz in (20, 40, 60):
        v = [p[f"r{hz}"] for p in e if p.get(f"r{hz}") is not None]
        if v:
            out[f"r{hz}_mean"] = round(float(np.mean(v)), 2)
            out[f"r{hz}_win"] = round(100 * float(np.mean(np.array(v) > 0)), 1)
    # ожидание в R: цель = +rr, стоп = −1
    ex = [(p["rr"] if p["result"] == "target" else -1.0) for p in done if p.get("rr")]
    out["expectancy_R"] = round(float(np.mean(ex)), 2) if ex else None
    return out


def strip(p: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in p.items()}


def main():
    import seasonal
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers")
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    res = {"updated": pd.Timestamp.now(tz="UTC").isoformat(), "pivot": PIVOT}

    if a.tickers:
        ts = [x.strip().upper() for x in a.tickers.split(",") if x.strip()]
        daily = seasonal.download(ts, 21)
        res["tickers"] = {}
        try:
            import data
            h1 = data._download(ts, "720d")
        except Exception as e:
            log.warning("4H: %s", e)
            h1 = {}
        for t in ts:
            r = {}
            if t in daily:
                df = daily[t]
                pats = detect(df)
                piv = {L: [(str(df.index[i].date()), k, round(v, 2)) for i, k, v in
                           zigzag(df["high"].to_numpy(float), df["low"].to_numpy(float), L)[-10:]] for L in (3, 5)}
                r["daily_pivots"] = piv
                r["daily"] = dict(last_close=float(df["close"].iloc[-1]), last_date=str(df.index[-1].date()),
                                  stats=summarize(pats), recent=[strip(p) for p in pats[-6:]],
                                  history=[strip(p) for p in pats if p.get("entry_i") is not None])
            if t in h1:
                df4 = data.to_4h(h1[t])
                pats4 = detect(df4, L=4)
                r["h4_pivots"] = [(str(df4.index[i])[:16], k, round(v, 2)) for i, k, v in
                                  zigzag(df4["high"].to_numpy(float), df4["low"].to_numpy(float), 4)[-12:]]
                r["h4"] = dict(last_close=float(df4["close"].iloc[-1]), last_date=str(df4.index[-1])[:16],
                               stats=summarize(pats4), recent=[strip(p) for p in pats4[-6:]])
            res["tickers"][t] = r
            print(t, json.dumps({k: v.get("stats") for k, v in r.items()}, ensure_ascii=False))

    if a.backtest:
        tickers = seasonal.prefilter(seasonal.all_us_stocks(), seasonal.Params())
        hist = seasonal.download(tickers, 21)
        SP = seasonal.Params()
        allp = {"bull": [], "bear": []}
        n = 0
        for t, df in hist.items():
            df = df.dropna(subset=["open", "high", "low", "close"])
            if len(df) < 300 or not seasonal.liquid(df, SP):
                continue
            n += 1
            try:
                for p in detect(df):
                    allp[p["side"]].append(p)
            except Exception as e:
                log.warning("%s: %s", t, e)
        res["backtest"] = {"tickers": n, **{s: summarize(v) for s, v in allp.items()},
                           "bull_fading": summarize([p for p in allp["bull"] if p["fading"]]),
                           "bull_not_fading": summarize([p for p in allp["bull"] if not p["fading"]])}
        print(json.dumps(res["backtest"], ensure_ascii=False, indent=1))

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
