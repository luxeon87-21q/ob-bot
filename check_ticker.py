import json, math, sys, logging
from datetime import date, timedelta
from typing import List
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from seasonal import *
import seasonal as S
logging.basicConfig(level=logging.INFO)
def explain(tk: Ticker, starts: List[date], P: Params) -> dict:
    """Почему акция попала или не попала: по каждой дате старта и направлению — какой фильтр не прошёл."""
    out = {"years": len(tk.years), "first": str(tk.dates[0]) if len(tk.dates) else None, "rows": []}
    for side in ("bull", "bear"):
        for s in starts:
            md = md_of(s)
            yh = tk.year_hits(md, side)
            n = len(yh)
            row = {"side": side, "start": s.isoformat(), "n": n}
            if n < P.min_years:
                row["fail"] = f"мало лет истории: {n} < {P.min_years}"
                out["rows"].append(row)
                continue
            hits = np.array([v for _, v, _ in yh])
            row["wins90"] = int(np.sum(hits <= P.max_days))
            k_need = max(1, math.ceil(P.win_min * n - 1e-9))
            srt = np.sort(hits)
            row["need"] = k_need
            row["years"] = [[y, None if not np.isfinite(v) else int(v)] for y, v, _ in yh]
            if not np.isfinite(srt[k_need - 1]):
                row["fail"] = (f"цель достигалась в {row['wins90']} из {n} лет за {P.max_days} дн., "
                               f"нужно {k_need} ({P.win_min:.0%})")
                out["rows"].append(row)
                continue
            L = int(max(P.min_days, srt[k_need - 1]))
            wins = int(np.sum(hits <= L))
            base = tk.base_rate(side, L)
            pval = binom_tail(wins, n, base)
            rwins = int(sum(v <= L for _, v, _ in yh[-5:]))
            row.update(L=L, wins=wins, base=round(base, 3), p=pval, recent=rwins)
            if pval > P.p_max:
                row["fail"] = f"случайность {pval:.1%} > {P.p_max:.0%} (обычно {base:.0%})"
            elif rwins < min(P.recent_min, len(yh[-5:])):
                row["fail"] = f"последние 5 лет: {rwins} < {P.recent_min}"
            else:
                nb = [tk.win_at(md_of(s + timedelta(days=k)), side, L) for k in range(-P.neighbor, P.neighbor + 1) if k]
                nb = [x for x in nb if x is not None]
                row["neighbors"] = round(float(np.mean(nb)), 3) if nb else 0
                if row["neighbors"] < P.neighbor_min:
                    row["fail"] = f"соседние даты в среднем {row['neighbors']:.0%} < {P.neighbor_min:.0%}"
                else:
                    row["fail"] = None
            out["rows"].append(row)
    return out


    ap.add_argument("--explain", help="почему тикеры (через запятую) не попали — в cache/seasonal_explain.json")
    if a.explain:
        ts = [x.strip().upper() for x in a.explain.split(",") if x.strip()]
        today = pd.Timestamp.now(tz="America/New_York").date()
        starts = [today + timedelta(days=k) for k in range(P.ahead_from, P.ahead_to + 1)]
        starts = [d for d in starts if d.weekday() < 5]
        hist = download(ts, P.max_years + 1)
        rep = {"today": today.isoformat(), "params": asdict(P), "tickers": {}}
        try:
            u = all_us_stocks()
            pre = set(prefilter(u, P))
            rep["universe"] = {t: {"in_list": t in set(u["ticker"]), "prefilter": t in pre} for t in ts}
        except Exception as e:
            rep["universe"] = str(e)
        for t in ts:
            if t not in hist:
                rep["tickers"][t] = {"error": "нет данных Yahoo"}
                continue
            df = hist[t]
            e = explain(Ticker(df, P), starts, P)
            e["liquid"] = liquid(df, P)
            e["price"] = float(df["close"].iloc[-1])
            e["dvol"] = float((df["close"] * df["volume"]).tail(20).median())
            rep["tickers"][t] = e
        (BASE / "cache" / "seasonal_explain.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(rep, ensure_ascii=False)[:5000])
        return

P = Params.from_env()
T = sys.argv[1].upper()
today = pd.Timestamp.now(tz="America/New_York").date()
starts = [today + timedelta(days=k) for k in range(P.ahead_from, P.ahead_to + 1)]
starts = [d for d in starts if d.weekday() < 5]
rep = {"today": today.isoformat()}
try:
    u = all_us_stocks()
    row = u[u.ticker == T]
    rep["universe_rows"] = len(u)
    rep["in_list"] = len(row) > 0
    rep["screener_row"] = row.astype(str).to_dict("records")
    rep["prefilter"] = T in set(prefilter(u, P))
except Exception as e:
    rep["universe_error"] = str(e)
hist = download([T, "SPY"], P.max_years + 1)
if T in hist:
    df = hist[T]
    rep["bars"] = len(df); rep["first"] = str(df.index[0].date()); rep["price"] = float(df.close.iloc[-1])
    rep["dvol"] = float((df.close * df.volume).tail(20).median()); rep["liquid"] = liquid(df, P)
    tk = Ticker(df, P)
    rep["explain"] = explain(tk, starts, P)
    rep["scan"] = [to_json(r) for r in scan_ticker(tk, starts, P)]
    # лучшие окна по всему году (без фильтров) — для понимания, когда у CBOE сезонность
    best = []
    d = date(today.year, 1, 1)
    while d.year == today.year:
        for side in ("bull", "bear"):
            yh = tk.year_hits(md_of(d), side)
            if len(yh) >= P.min_years:
                h = np.array([v for _, v, _ in yh])
                best.append((float(np.mean(h <= 90)), side, d.isoformat(), len(yh)))
        d += timedelta(days=1)
    best.sort(reverse=True)
    rep["best_year_windows_90d"] = best[:15]
else:
    rep["error"] = "нет данных Yahoo"
open("cache/check.json", "w").write(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
