"""
Вход на дне по второму пересечению скользящих (дневки).

  красная — EMA 20 по закрытию
  синяя   — SMA 35, сдвинутая на 5 дней вперёд (значение синей сегодня = SMA35 пять дней назад)

Правило:
  1) перед этим долгий спуск: красная была ниже синей минимум DOWN_BARS дней подряд;
  2) первое пересечение красной снизу вверх — пропускаем (по автору ~70% ложных);
  3) красная снова уходит ниже синей;
  4) второе пересечение снизу вверх — ВХОД (по открытию следующего дня).
  Если второе не случилось за WINDOW дней после первого — сбрасываем.
  Если после первого красная снова пролежала ниже синей DOWN_BARS дней — это новый спуск,
  следующее пересечение снова считается первым.

Сейчас модуль только проверяет стратегию на истории (backtest):
  python macross.py --backtest            — все ликвидные акции США, ~20 лет → cache/macross_backtest.json
  python macross.py --backtest --tickers AAPL,MSFT
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "macross_backtest.json"
log = logging.getLogger("ob_bot.macross")


@dataclass
class MParams:
    ema: int = 20
    sma: int = 35
    shift: int = 5
    down_bars: int = 20       # долгий спуск: красная ниже синей не меньше N дней подряд
    window: int = 60          # второе пересечение должно случиться за N дней после первого
    false_bars: int = 20      # «ложное» пересечение: красная снова ниже синей в течение N дней
    years: int = 20
    min_price: float = 5.0
    min_dvol: float = 5e6


def lines(close: pd.Series, P: MParams):
    red = close.ewm(span=P.ema, adjust=False).mean()
    blue = close.rolling(P.sma).mean().shift(P.shift)
    return red, blue


def find_events(df: pd.DataFrame, P: MParams) -> List[dict]:
    """События: first — первое пересечение после спуска, second — вход, any — любое пересечение вверх."""
    red, blue = lines(df["close"], P)
    r, b = red.to_numpy(), blue.to_numpy()
    n = len(r)
    valid = ~np.isnan(b)
    above = np.where(valid, r > b, False)
    ev = []
    below_run = 0
    state, t1 = 0, -1           # 0 — ждём спуск, 1 — было первое, 2 — после первого снова ниже
    start = P.sma + P.shift
    for i in range(start, n):
        if not valid[i] or not valid[i - 1]:
            continue
        up = above[i] and not above[i - 1]
        down = (not above[i]) and above[i - 1]
        if state in (1, 2) and i - t1 > P.window:
            state = 0
        if up:
            ev.append({"i": i, "kind": "any", "below": below_run})
            if state == 2 and below_run < P.down_bars and i - t1 <= P.window:
                ev.append({"i": i, "kind": "second", "below": below_run, "gap": i - t1})
                state = 0
            elif below_run >= P.down_bars:
                ev.append({"i": i, "kind": "first", "below": below_run})
                state, t1 = 1, i
            else:
                state = 0
            below_run = 0
        elif down:
            if state == 1:
                state = 2
            below_run = 1
        elif not above[i]:
            below_run += 1
    return ev


class Arrays:
    """Цены одной акции и SPY, выровненные по датам акции — чтобы не пересчитывать на каждом событии."""

    def __init__(self, df: pd.DataFrame, P: MParams, spy: Optional[pd.Series]):
        self.idx = df.index
        self.o, self.h, self.l, self.c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        red, blue = lines(df["close"], P)
        self.red, self.blue = red.to_numpy(), blue.to_numpy()
        self.spy = spy.reindex(df.index).ffill().to_numpy(float) if spy is not None else None
        self.P = P


def outcomes(A: Arrays, i: int, full: bool = True) -> Optional[dict]:
    """Результат входа по открытию дня i+1 (сигнал — по закрытию дня i)."""
    P = A.P
    e = i + 1
    if e >= len(A.o) or A.o[e] <= 0:
        return None
    entry = A.o[e]
    res = {"date": str(A.idx[i].date())}
    for hz in (10, 20, 40, 60):
        j = e + hz - 1
        if j < len(A.c):
            res[f"r{hz}"] = float(A.c[j] / entry - 1) * 100
            if A.spy is not None and A.spy[e] > 0 and not np.isnan(A.spy[j]):
                res[f"x{hz}"] = res[f"r{hz}"] - float(A.spy[j] / A.spy[e] - 1) * 100
    if not full:
        return res
    end = min(len(A.o), e + 60)
    hit = None
    if e + 60 <= len(A.o):
        hit = False
        for j in range(e, end):
            if A.l[j] <= entry * 0.9:          # стоп в тот же день, что и цель, — считаем неудачей
                break
            if A.h[j] >= entry * 1.1:
                hit = True
                break
    res["hit10"] = hit
    seg = A.l[e:end]
    res["mae"] = float(seg.min() / entry - 1) * 100 if len(seg) else None
    rr, bb = A.red[i + 1:i + 1 + P.false_bars], A.blue[i + 1:i + 1 + P.false_bars]
    res["false"] = bool(np.any(rr < bb)) if len(rr) == P.false_bars else None
    return res


def summarize(rows: List[dict]) -> dict:
    out = {"n": len(rows)}
    if not rows:
        return out
    df = pd.DataFrame(rows)
    for hz in (10, 20, 40, 60):
        v = pd.to_numeric(df.get(f"r{hz}"), errors="coerce").dropna()
        x = pd.to_numeric(df.get(f"x{hz}"), errors="coerce").dropna()
        if len(v):
            out[f"r{hz}_mean"] = round(float(v.mean()), 2)
            out[f"r{hz}_median"] = round(float(v.median()), 2)
            out[f"r{hz}_win"] = round(float((v > 0).mean()) * 100, 1)
        if len(x):
            out[f"x{hz}_mean"] = round(float(x.mean()), 2)
            out[f"x{hz}_win"] = round(float((x > 0).mean()) * 100, 1)
    hit = df["hit10"].dropna() if "hit10" in df else pd.Series(dtype=bool)
    if len(hit):
        out["hit10"] = round(float(hit.astype(bool).mean()) * 100, 1)
    f = df["false"].dropna() if "false" in df else pd.Series(dtype=bool)
    if len(f):
        out["false"] = round(float(f.astype(bool).mean()) * 100, 1)
    m = pd.to_numeric(df["mae"], errors="coerce").dropna() if "mae" in df else pd.Series(dtype=float)
    if len(m):
        out["mae_median"] = round(float(m.median()), 2)
    return out


def baseline(A: Arrays, step: int = 10) -> List[dict]:
    """Вход в каждый 10-й день подряд — с чем сравнивать (обычная доходность этой акции)."""
    out = []
    for i in range(A.P.sma + A.P.shift, len(A.o) - 61, step):
        r = outcomes(A, i, full=False)
        if r:
            out.append(r)
    return out


def backtest(hist: Dict[str, pd.DataFrame], P: MParams) -> dict:
    spy = hist["SPY"]["close"] if "SPY" in hist else None
    groups = {"first": [], "second": [], "any": [], "first_kept": [], "base": []}
    events = []
    per_year: Dict[str, Dict[str, int]] = {}
    recent = []
    tickers = 0
    for t, df in hist.items():
        if t == "SPY":
            continue
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df[(df["open"] > 0) & (df["low"] > 0)]
        if len(df) < 300:
            continue
        tickers += 1
        try:
            A = Arrays(df, P, spy)
            evs = find_events(df, P)
            for e in evs:
                r = outcomes(A, e["i"])
                if not r:
                    continue
                r["ticker"] = t
                groups[e["kind"]].append(r)
                if e["kind"] in ("first", "second"):
                    events.append(dict(r, kind=e["kind"], close=float(df["close"].iloc[e["i"]])))
                if e["kind"] == "first" and r.get("false") is False:
                    groups["first_kept"].append(r)
                if e["kind"] in ("first", "second"):
                    y = r["date"][:4]
                    per_year.setdefault(y, {"first": 0, "second": 0})[e["kind"]] += 1
                if e["kind"] == "second" and r["date"] >= str((df.index[-1] - pd.Timedelta(days=30)).date()):
                    recent.append({"ticker": t, "date": r["date"], "price": float(df["close"].iloc[-1])})
            groups["base"].extend(baseline(A))
        except Exception as ex:
            log.warning("%s: %s", t, ex)
    res = {k: summarize(v) for k, v in groups.items()}
    # стабильность: вторые пересечения по периодам
    sec = pd.DataFrame(groups["second"])
    periods = {}
    if len(sec):
        for name, lo, hi in (("2006-2010", "2006", "2011"), ("2011-2015", "2011", "2016"),
                             ("2016-2020", "2016", "2021"), ("2021-2026", "2021", "2027")):
            part = sec[(sec["date"] >= lo) & (sec["date"] < hi)]
            periods[name] = summarize(part.to_dict("records"))
    return dict(updated=pd.Timestamp.now(tz="UTC").isoformat(), params=asdict(P), tickers=tickers,
                groups=res, periods=periods, per_year=per_year,
                recent=sorted(recent, key=lambda x: x["date"], reverse=True)[:50],
                events=events if tickers <= 20 else [])


def report(res: dict) -> str:
    g = res["groups"]
    names = {"second": "2-е пересечение (вход)", "first": "1-е пересечение после спуска",
             "first_kept": "1-е, которое не оказалось ложным", "any": "любое пересечение вверх",
             "base": "вход в случайный день"}
    lines_ = [f"Акций: {res['tickers']}"]
    for k in ("second", "first", "first_kept", "any", "base"):
        s = g.get(k, {})
        if not s.get("n"):
            continue
        lines_.append(f"\n{names[k]}: сигналов {s['n']}")
        lines_.append("  " + " | ".join(
            f"{hz}д: ср {s.get(f'r{hz}_mean')}% мед {s.get(f'r{hz}_median')}% в плюсе {s.get(f'r{hz}_win')}% "
            f"vs SPY {s.get(f'x{hz}_mean')}%" for hz in (20, 40, 60)))
        if "hit10" in s:
            lines_.append(f"  +10% раньше −10% за 60д: {s['hit10']}% · ложных (снова ниже за 20д): "
                          f"{s.get('false')}% · медианная просадка {s.get('mae_median')}%")
    return "\n".join(lines_)


def main():
    import seasonal
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--tickers")
    ap.add_argument("--out", help="куда сохранить (по умолчанию cache/macross_backtest.json)")
    a = ap.parse_args()
    P = MParams()
    if not a.backtest:
        ap.error("пока доступен только --backtest")
    if a.tickers:
        tickers = [x.strip().upper() for x in a.tickers.split(",")]
    else:
        SP = seasonal.Params()
        tickers = seasonal.prefilter(seasonal.all_us_stocks(), SP)
    hist = seasonal.download(sorted(set(tickers) | {"SPY"}), P.years + 1)
    SP = seasonal.Params(min_price=P.min_price, min_dvol=P.min_dvol)
    hist = {t: df for t, df in hist.items() if t == "SPY" or seasonal.liquid(df, SP)}
    log.info("ликвидных акций с историей: %d", len(hist) - 1)
    res = backtest(hist, P)
    out = BASE / a.out if a.out else OUT_FILE
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(report(res))


if __name__ == "__main__":
    main()
