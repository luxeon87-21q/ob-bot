"""
Вкладка «Трейдер»: инструменты из пака day-trader-skill, переделанные под 4H ордер-блоки.

  • Time Machine — каждый сигнал OB (новый / касание) превращается в план сделки
    (вход, стоп, цель 2R) и проверяется двумя способами:
      1) 10 000 прогонов следующих 20 свечей 4H (≈10 торговых дней), собранных из настоящих
         свечей этой же акции за последний год (утренняя свеча — из утренних, дневная — из дневных);
      2) история: то же правило на всех прошлых ордер-блоках этой акции того же типа и стороны.
    Итог — доля «вход сработал», «цель раньше стопа», «стоп раньше цели», средний R и вердикт.
    Это симуляция по прошлым свечам, а не прогноз.
  • Сканер сетапов (по дневным закрытиям, собранным из свечей 4H): пробой 20-дневного
    максимума закрытий, бычий флаг, возврат над MA20, перепроданность RSI14 < 30, отскок.
  • Новости: заголовки Yahoo Finance и события 8-K в SEC по акциям с сигналами за 3 дня.

  python trader.py            — посчитать всё и записать cache/trader.json
  python trader.py --no-news  — без сетевых запросов за новостями

Запускается из build_site.py (после проверки свечей), строка Time Machine идёт в сигнал Telegram.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET_XML
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "trader.json"
SIGNALS_FILE = BASE / "cache" / "signals.jsonl"
log = logging.getLogger("ob_bot.trader")

R_TARGET = 2.0          # цель = 2 риска
HORIZON = 20            # свечей 4H после сигнала (≈10 торговых дней)
REPLAYS = 10_000
SEED = 19
POOL_BARS = 500         # сколько последних свечей берём в «пул» для прогонов (≈1 год)
MIN_POOL = 120
RECENT_DAYS = 7         # сигналы для таблицы
NEWS_DAYS = 3           # по каким сигналам собирать новости
NEWS_MAX_TICKERS = 40


# ---------------------------------------------------------------- план сделки
def plan_for(ev: dict, r_mult: float = R_TARGET) -> Optional[dict]:
    """ev: словарь события (как в signals.jsonl). Возвращает план или None, если план не имеет смысла.

    Бычий OB: покупка, стоп под нижней границей. Медвежий: продажа, стоп над верхней.
    Новый OB — лимитный вход на границе блока (ждём возврата цены). Касание — вход по закрытию.
    """
    side = ev["side"]
    top, btm, close = float(ev["top"]), float(ev["btm"]), float(ev["close"])
    if not (top > btm > 0 and close > 0):
        return None
    long = side == "bull"
    if ev["type"] == "formed":
        edge = top if long else btm
        inside = close <= edge if long else close >= edge
        entry, how = (close, "market") if inside else (edge, "limit")
    elif ev["type"] == "touched":
        entry, how = close, "market"
    else:
        return None
    stop = btm if long else top
    risk = (entry - stop) if long else (stop - entry)
    if risk <= entry * 0.001:              # стоп внутри спреда — такой план не считаем
        return None
    target = entry + r_mult * risk if long else entry - r_mult * risk
    return dict(side="long" if long else "short", entry=round(entry, 4), stop=round(stop, 4),
                target=round(target, 4), r=r_mult, how=how, risk_pct=round(risk / entry * 100, 2))


# ---------------------------------------------------------------- прогоны
def _pool(df: pd.DataFrame, upto: int) -> Optional[dict]:
    """Свечи до upto (включительно) → отношения к предыдущему закрытию, отдельно по слотам дня."""
    lo = max(1, upto + 1 - POOL_BARS)
    seg = df.iloc[lo - 1: upto + 1]
    pc = seg["close"].to_numpy(float)[:-1]
    o, h, l, c = (seg[k].to_numpy(float)[1:] for k in ("open", "high", "low", "close"))
    ok = np.isfinite(pc) & np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & (pc > 0)
    if ok.sum() < MIN_POOL:
        return None
    ratios = np.stack([o / pc, h / pc, l / pc, c / pc], axis=1)[ok]
    slot = (seg.index[1:].hour >= 12).astype(int)[ok]       # 0 — утренняя свеча 9:30, 1 — дневная 13:30
    by = {s: ratios[slot == s] for s in (0, 1)}
    if min(len(v) for v in by.values()) < MIN_POOL // 3:
        by = {0: ratios, 1: ratios}
    return by


def simulate(df: pd.DataFrame, idx: int, plan: dict, replays: int = REPLAYS, horizon: int = HORIZON,
             seed: int = SEED) -> Optional[dict]:
    """Монте-Карло: следующие horizon свечей собираются из прошлых свечей этой акции."""
    by = _pool(df, idx)
    if by is None:
        return None
    rng = np.random.default_rng(seed)
    long = plan["side"] == "long"
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    risk = abs(entry - stop)
    p = np.full(replays, float(df["close"].iloc[idx]))
    filled = np.full(replays, plan["how"] == "market")
    done = np.zeros(replays, bool)
    res = np.zeros(replays)                       # R по итогу
    out = np.zeros(replays, int)                  # 0 — не вошли, 1 — цель, 2 — стоп, 3 — открыта к концу
    slot = 0 if df.index[idx].hour >= 12 else 1   # следующая свеча после дневной — утренняя
    for _ in range(horizon):
        pool = by[slot]
        r = pool[rng.integers(0, len(pool), replays)]
        o, h, l, c = (p * r[:, k] for k in range(4))
        act = ~done
        if long:
            new_fill = act & ~filled & (l <= entry)
            # бар, на котором вошли: стоп считаем, цель — только если стоп не задет
            hit_stop = act & (filled | new_fill) & (l <= stop)
            hit_tgt = act & (filled | new_fill) & (h >= target) & ~hit_stop
        else:
            new_fill = act & ~filled & (h >= entry)
            hit_stop = act & (filled | new_fill) & (h >= stop)
            hit_tgt = act & (filled | new_fill) & (l <= target) & ~hit_stop
        filled |= new_fill
        out[hit_stop] = 2
        res[hit_stop] = -1.0
        out[hit_tgt] = 1
        res[hit_tgt] = plan["r"]
        done |= hit_stop | hit_tgt
        p = c
        slot ^= 1
    still = filled & ~done
    out[still] = 3
    res[still] = ((p[still] - entry) if long else (entry - p[still])) / risk
    nf = int(filled.sum())
    return _stats(out, res, plan["r"], nf, replays)


def _stats(out: np.ndarray, res: np.ndarray, r: float, nf: int, n: int) -> dict:
    tg, st, op = (int((out == k).sum()) for k in (1, 2, 3))
    d = dict(n=n, fill=round(nf / n * 100, 1),
             target=round(tg / nf * 100, 1) if nf else None,
             stop=round(st / nf * 100, 1) if nf else None,
             open=round(op / nf * 100, 1) if nf else None,
             avg_r=round(float(res[out > 0].mean()), 3) if nf else None,
             first=round(tg / (tg + st) * 100, 1) if tg + st else None,   # цель раньше стопа среди дошедших
             be=round(100 / (1 + r), 1))                                     # безубыток для этого R
    d["verdict"], d["lean"] = verdict(tg, st, r)
    return d


def verdict(tg: int, st: int, r: float):
    """Как в Time Machine: среди дошедших до цели или стопа — как часто первой была цель,
    против безубытка 1 / (1 + R)."""
    hit = tg + st
    if hit < 10:
        return "Мало данных", 0
    share = tg / hit * 100
    be = 100 / (1 + r)
    d = share - be
    if abs(d) < 5:
        return "Около безубытка", 0
    if abs(d) < 15:
        return ("Склоняется к цели", 1) if d > 0 else ("Склоняется к стопу", -1)
    return ("Сильно к цели", 2) if d > 0 else ("Сильно к стопу", -2)


def replay_history(df: pd.DataFrame, events: list, ev: dict, idx: int, r_mult: float = R_TARGET,
                   horizon: int = HORIZON) -> dict:
    """То же правило на прошлых OB этой акции того же типа и стороны (по настоящим свечам)."""
    h_, l_, c_ = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    out, res = [], []
    for e in events:
        if e.type != ev["type"] or e.ob.side != ev["side"]:
            continue
        i = e.bar_idx
        if i + horizon >= idx:                    # нужен полный горизонт до текущего сигнала
            continue
        pl = plan_for(dict(type=e.type, side=e.ob.side, top=e.ob.top, btm=e.ob.btm, close=e.close), r_mult)
        if not pl:
            continue
        long = pl["side"] == "long"
        filled, o, rr = pl["how"] == "market", 3, 0.0
        for j in range(i + 1, i + 1 + horizon):
            nf = not filled and ((l_[j] <= pl["entry"]) if long else (h_[j] >= pl["entry"]))
            if filled or nf:
                filled = True
                if (l_[j] <= pl["stop"]) if long else (h_[j] >= pl["stop"]):
                    o, rr = 2, -1.0
                    break
                if (h_[j] >= pl["target"]) if long else (l_[j] <= pl["target"]):
                    o, rr = 1, pl["r"]
                    break
        if not filled:
            o = 0
        elif o == 3:
            last = c_[i + horizon]
            rr = ((last - pl["entry"]) if long else (pl["entry"] - last)) / abs(pl["entry"] - pl["stop"])
        out.append(o)
        res.append(rr)
    n = len(out)
    a, b = np.array(out, int), np.array(res, float)
    tg, st, op, nf = int((a == 1).sum()), int((a == 2).sum()), int((a == 3).sum()), int((a > 0).sum())
    return dict(n=n, filled=nf, target=tg, stop=st, open=op,
                avg_r=round(float(b[a > 0].mean()), 3) if nf else None)


def evaluate(df: pd.DataFrame, events: list, ev: dict, idx: int) -> Optional[dict]:
    plan = plan_for(ev)
    if not plan:
        return None
    mc = simulate(df, idx, plan)
    if mc is None:
        return None
    return dict(plan=plan, mc=mc, hist=replay_history(df, events, ev, idx))


def tg_line(tm: Optional[dict]) -> str:
    """Короткая строка для сигнала в Telegram:
    🎲 вход 67% · цель раньше стопа 44% (безубыток 33%) · история 3 из 7 · склоняется к цели"""
    if not tm:
        return ""
    mc, h, pl = tm["mc"], tm["hist"], tm["plan"]
    parts = []
    if pl["how"] == "limit":
        parts.append(f"вход {mc['fill']:.0f}%")
    if mc.get("first") is not None:
        parts.append(f"цель раньше стопа {mc['first']:.0f}% (безубыток {mc['be']:.0f}%)")
    if h["target"] + h["stop"]:
        parts.append(f"история {h['target']} из {h['target'] + h['stop']}")
    parts.append(mc["verdict"].lower())
    return "🎲 " + " · ".join(parts)


# ---------------------------------------------------------------- сканер сетапов
def daily_closes(df: pd.DataFrame, now: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Свечи 4H → дневные закрытие и объём. Незакрытый сегодняшний день отбрасываем."""
    d = df.groupby(df.index.date).agg(close=("close", "last"), volume=("volume", "sum"),
                                       last=("close", lambda s: s.index[-1]))
    if len(d) and now is not None:
        last_bar = d["last"].iloc[-1]
        if d.index[-1] == now.date() and (last_bar.hour < 12 or now.hour < 16):
            d = d.iloc[:-1]
    return d[["close", "volume"]]


def rsi14(c: np.ndarray) -> np.ndarray:
    """RSI 14 по Уайлдеру."""
    out = np.full(len(c), np.nan)
    if len(c) < 16:
        return out
    d = np.diff(c)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:14].mean(), dn[:14].mean()
    for i in range(14, len(d) + 1):
        if i > 14:
            au = (au * 13 + up[i - 1]) / 14
            ad = (ad * 13 + dn[i - 1]) / 14
        out[i] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def setups(closes: np.ndarray) -> List[str]:
    c = closes
    tags = []
    if len(c) < 22:
        return tags
    if c[-1] > c[-21:-1].max():
        tags.append("breakout_20d")
    if len(c) >= 16:
        run = c[-6] / c[-16] - 1
        last5 = c[-5:]
        if run > 0.10 and (last5.max() / last5.min() - 1) < 0.05:
            tags.append("bull_flag")
    ma = pd.Series(c).rolling(20).mean().to_numpy()
    if np.isfinite(ma[-2]) and c[-2] < ma[-2] and c[-1] > ma[-1]:
        tags.append("reclaim_ma20")
    r = rsi14(c)
    if np.isfinite(r[-1]) and r[-1] < 30:
        tags.append("oversold")
    if np.isfinite(r[-2]) and r[-2] < 30 and c[-1] > c[-2]:
        tags.append("oversold_bounce")
    return tags


def scan(bars: Dict[str, pd.DataFrame], now: pd.Timestamp, recent_ob: Dict[str, str]) -> dict:
    rows = []
    as_of = None
    for t, df in bars.items():
        d = daily_closes(df, now)
        if len(d) < 22:
            continue
        c = d["close"].to_numpy(float)
        tags = setups(c)
        if not tags:
            continue
        r = rsi14(c)
        vol = float(d["volume"].iloc[-20:].mean())
        rows.append(dict(ticker=t, close=round(float(c[-1]), 2), chg=round((c[-1] / c[-2] - 1) * 100, 2),
                         rsi=round(float(r[-1]), 1) if np.isfinite(r[-1]) else None,
                         dvol=round(vol * float(c[-1])), tags=tags, ob=recent_ob.get(t, "")))
        as_of = max(as_of or d.index[-1], d.index[-1])
    rows.sort(key=lambda x: -x["dvol"])
    return dict(as_of=str(as_of) if as_of else None, rows=rows)


# ---------------------------------------------------------------- новости
YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={t}&region=US&lang=en-US"
TAGS = [
    ("отчёт", r"\b(earnings|results|eps|revenue|quarter|q[1-4]\b|guidance|outlook)"),
    ("аналитики", r"\b(upgrade|downgrade|price target|analyst|rating|initiat)"),
    ("сделка", r"\b(acqui|merger|buyout|deal|takeover|stake)"),
    ("регулятор", r"\b(sec|fda|doj|ftc|antitrust|probe|investigation|lawsuit|sued|recall)"),
    ("руководство", r"\b(ceo|cfo|resign|steps down|appoint)"),
    ("капитал", r"\b(buyback|dividend|offering|split|debt)"),
]
ITEMS_8K = {"1.01": "соглашение", "1.03": "банкротство", "2.01": "сделка", "2.02": "отчёт", "2.05": "сокращения",
            "2.06": "списание", "3.01": "делистинг", "4.02": "пересмотр отчётности", "5.02": "руководство",
            "5.07": "голосование", "7.01": "раскрытие", "8.01": "прочее"}


def tag_title(title: str) -> List[str]:
    low = title.lower()
    return [name for name, rx in TAGS if re.search(rx, low)]


def parse_rss(xml_text: str, since: datetime) -> List[dict]:
    out = []
    try:
        root = ET_XML.fromstring(xml_text)
    except ET_XML.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if not title or when < since or not link.startswith("http"):
            continue
        out.append(dict(src="yahoo", time=when.astimezone(timezone.utc).isoformat(timespec="minutes"),
                        title=title, url=link, tags=tag_title(title)))
    return out


def news(tickers: List[str], now: datetime) -> dict:
    import requests
    import sec as sec_mod
    since = now - timedelta(days=7)
    res: Dict[str, List[dict]] = {}
    errors = []
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (ob-bot; personal use)"
    for t in tickers:
        try:
            r = s.get(YAHOO_RSS.format(t=t), timeout=20)
            if r.ok:
                res.setdefault(t, []).extend(parse_rss(r.text, since)[:6])
        except Exception as e:
            errors.append(f"yahoo {t}: {e}")
    try:
        if not sec_mod.CIK_FILE.exists():           # список CIK ведёт задача SEC; без него 8-K не ищем (SEC мог ответить 403)
            raise RuntimeError("список CIK ещё не загружен задачей SEC")
        sc = sec_mod.SEC()
        ciks = sc.cik_map()
        cut = (now - timedelta(days=14)).date().isoformat()
        for t in tickers:
            cik = ciks.get(t)
            if not cik:
                continue
            sub = sc.submissions(cik)
            for f in sec_mod.recent(sub):
                if f.get("form") != "8-K" or str(f.get("filingDate", "")) < cut:
                    continue
                items = [x.strip() for x in str(f.get("items", "")).split(",") if x.strip()]
                what = ", ".join(ITEMS_8K.get(x, x) for x in items) or "8-K"
                res.setdefault(t, []).append(dict(
                    src="8-K", time=str(f.get("acceptanceDateTime") or f.get("filingDate"))[:16],
                    title=f"8-K: {what}", tags=[ITEMS_8K[x] for x in items if x in ITEMS_8K][:2],
                    url=sec_mod.archive_url(cik, f["accessionNumber"], f.get("primaryDocument", ""))))
    except Exception as e:
        errors.append(f"SEC: {e}")
    for t in res:
        res[t].sort(key=lambda x: x["time"], reverse=True)
    return dict(items={t: v for t, v in res.items() if v}, errors=errors[:10])


# ---------------------------------------------------------------- сборка
def read_recent_signals(days: int, now: pd.Timestamp) -> List[dict]:
    cut = now - pd.Timedelta(days=days)
    out = []
    try:
        lines = SIGNALS_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return out
    for ln in lines[-6000:]:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("type") not in ("formed", "touched"):
            continue
        if pd.Timestamp(r["bar_time"]) >= cut:
            out.append(r)
    return out


def load_bars(tickers) -> Dict[str, pd.DataFrame]:
    import data
    out = {}
    for t in tickers:
        f = data.BARS_DIR / f"{t}.csv"
        if not f.exists():
            continue
        df = pd.read_csv(f, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(data.ET)
        out[t] = df
    return out


_MEMO: Dict[tuple, Optional[dict]] = {}


def evaluate_signal(df: pd.DataFrame, events: list, sig: dict) -> Optional[dict]:
    """sig — словарь события; бар ищется по времени. Результат кэшируется на время запуска."""
    key = (sig["ticker"], sig["type"], sig["side"], sig["bar_time"], sig.get("ob_time"))
    if key in _MEMO:
        return _MEMO[key]
    ts = pd.Timestamp(sig["bar_time"])
    try:
        idx = int(df.index.get_indexer([ts])[0])
    except Exception:
        idx = -1
    tm = evaluate(df, events, sig, idx) if idx >= 0 else None
    _MEMO[key] = tm
    return tm


def build(s: dict, with_news: bool = True, bars: Optional[Dict[str, pd.DataFrame]] = None) -> dict:
    import data
    from order_blocks import detect
    now = pd.Timestamp.now(tz=data.ET)
    sigs = read_recent_signals(RECENT_DAYS, now)
    if bars is None:
        bars = load_bars(data.load_universe(s["universe"], BASE / "tickers.txt"))
    tm_rows = []
    det_cache: Dict[str, list] = {}
    for sg in sigs:
        df = bars.get(sg["ticker"])
        if df is None or len(df) < 3 * s["length"]:
            continue
        if sg["ticker"] not in det_cache:
            det_cache[sg["ticker"]] = detect(df, s["length"], s["mitigation"], s["track_last"])[0]
        tm = evaluate_signal(df, det_cache[sg["ticker"]], sg)
        if tm:
            tm_rows.append(dict(ticker=sg["ticker"], type=sg["type"], side=sg["side"], kind=sg.get("kind", ""),
                                bar_time=sg["bar_time"], top=sg["top"], btm=sg["btm"], close=sg["close"], **tm))
    tm_rows.sort(key=lambda r: r["bar_time"], reverse=True)

    # статистика правила по всем акциям: настоящие свечи, все прошлые OB (главная проверка «работают ли OB»)
    agg = {f"{ty}_{sd}": dict(n=0, filled=0, target=0, stop=0, open=0, sum_r=0.0)
           for ty in ("formed", "touched") for sd in ("bull", "bear")}
    n_tick = 0
    for t, df in bars.items():
        if len(df) < 3 * s["length"] + HORIZON:
            continue
        if t not in det_cache:
            try:
                det_cache[t] = detect(df, s["length"], s["mitigation"], s["track_last"])[0]
            except Exception as e:
                log.warning("detect %s: %s", t, e)
                continue
        n_tick += 1
        for key, a in agg.items():
            ty, sd = key.split("_")
            h = replay_history(df, det_cache[t], dict(type=ty, side=sd), len(df))
            for k in ("n", "filled", "target", "stop", "open"):
                a[k] += h[k]
            if h["avg_r"] is not None:
                a["sum_r"] += h["avg_r"] * h["filled"]
    rules = {}
    for key, a in agg.items():
        hit = a["target"] + a["stop"]
        rules[key] = dict(n=a["n"], filled=a["filled"],
                          fill=round(a["filled"] / a["n"] * 100, 1) if a["n"] else None,
                          target=a["target"], stop=a["stop"], open=a["open"],
                          first=round(a["target"] / hit * 100, 1) if hit else None,
                          avg_r=round(a["sum_r"] / a["filled"], 3) if a["filled"] else None,
                          verdict=verdict(a["target"], a["stop"], R_TARGET)[0])
    rules_info = dict(tickers=n_tick, be=round(100 / (1 + R_TARGET), 1))

    recent_ob = {}
    for sg in sorted(sigs, key=lambda r: r["bar_time"]):
        recent_ob[sg["ticker"]] = f"{'🟢' if sg['side'] == 'bull' else '🔴'} {'новый' if sg['type'] == 'formed' else 'касание'}"
    sc = scan(bars, now, recent_ob)

    nw = {"items": {}, "errors": [], "tickers": []}
    if with_news:
        fresh = [r for r in sigs if pd.Timestamp(r["bar_time"]) >= now - pd.Timedelta(days=NEWS_DAYS)]
        tick = list(dict.fromkeys(r["ticker"] for r in sorted(fresh, key=lambda r: r["bar_time"], reverse=True)))
        tick = tick[:NEWS_MAX_TICKERS]
        try:
            nw = news(tick, now.to_pydatetime().astimezone(timezone.utc))
        except Exception as e:
            nw = {"items": {}, "errors": [str(e)]}
        nw["tickers"] = tick
    elif OUT_FILE.exists():                         # без сети — оставляем прошлые новости
        try:
            nw = json.loads(OUT_FILE.read_text(encoding="utf-8")).get("news", nw)
        except Exception:
            pass

    res = dict(updated=now.isoformat(timespec="minutes"),
               params=dict(r=R_TARGET, horizon=HORIZON, replays=REPLAYS, seed=SEED, pool=POOL_BARS),
               tm=tm_rows, rules=rules, rules_info=rules_info, scan=sc, news=nw)
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("трейдер: Time Machine %d сигналов, сетапов %d, новостей по %d акциям",
             len(tm_rows), len(sc["rows"]), len(nw.get("items", {})))
    return res


def main():
    import bot
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    p = argparse.ArgumentParser()
    p.add_argument("--no-news", action="store_true")
    a = p.parse_args()
    build(bot.settings(), with_news=not a.no_news)


if __name__ == "__main__":
    main()
