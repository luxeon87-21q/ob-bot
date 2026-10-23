"""
MA200: ложный пробой 200-й скользящей в пределах 1 ATR и отскок вверх (дневки, недели, месяцы).

Для каждого таймфрейма (D, W, M) и каждого типа средней (EMA и SMA, период 200) проверяется
последняя ЗАКРЫТАЯ свеча — она сигнальная, бар [0]:
  1) тренд: MA[0] ≥ MA[5] — средняя растёт или горизонтальна (не ловим «падающий нож»);
  2) прокол на одной из свечей i = 1..N (N = 3):
       Low[i] < MA[i]                       — минимум ушёл под среднюю,
       Low[i] ≥ MA[i] − ATR14[i]            — но не глубже 1 ATR,
       Close[i] > MA[i] − 0,3 × ATR14[i]    — и закрытие не закрепилось под ней;
  3) отскок на свече [0], хотя бы один из триггеров:
       основной — свеча бычья (Close > Open) и Close[0] > High[i] свечи прокола;
       ранний   — Close[0] > MA[0] и Volume[0] > SMA20(Volume)[0].
ATR — как ta.atr в TradingView (RMA от True Range), EMA — как ta.ema.

Недели собираются из дневок (пт — конец недели), месяцы грузятся отдельно за всю историю,
чтобы EMA200 на месяцах совпадала с графиком (200 месяцев ≈ 17 лет: молодых компаний на M нет).
Незакрытая свеча (день до 16:15 по Нью-Йорку, неделя до вечера пятницы, месяц до последнего
рабочего дня) не используется — сигналы не «перерисовываются».

Отбор акций: все акции США (список Nasdaq) с ценой ≥ $5 и оборотом ≥ $5 млн в день,
у найденных — открытый интерес опционов ≥ 5000 (коллы + путы, экспирации ≤ 30 дней), как у сезонности.

Запуск (по будням после закрытия, .github/workflows/ma200.yml):
  python ma200.py                    — полный прогон, Telegram (только новые сигналы), cache/ma200.json
  python ma200.py --no-tg            — без Telegram
  python ma200.py --tickers AAPL,MSFT --no-tg --out cache/ma200_test.json
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "ma200.json"
SENT_FILE = BASE / "cache" / "ma200_sent.json"
ET = "America/New_York"
TFS = ("D", "W", "M")
MAS = ("EMA", "SMA")
log = logging.getLogger("ob_bot.ma200")


def _env(name, default, cast=float):
    v = os.environ.get(name)
    return default if v in (None, "") else cast(v)


@dataclass
class Params:
    ma_len: int = 200
    atr_len: int = 14
    n: int = 3                # прокол — на одной из N свечей перед сигнальной
    slope: int = 5            # MA[0] ≥ MA[slope]
    depth_atr: float = 1.0    # минимум не глубже MA − 1 ATR
    close_atr: float = 0.3    # закрытие свечи прокола выше MA − 0,3 ATR
    vol_len: int = 20         # средний объём для раннего триггера
    years: int = 21           # история дневок (недели строятся из них)
    min_price: float = 5.0
    min_dvol: float = 5e6     # средний дневной оборот, $
    oi_min: int = 5000        # OI опционов (0 — без фильтра)
    top: int = 40             # сколько строк на таймфрейм в сообщении

    @classmethod
    def from_env(cls) -> "Params":
        p = cls()
        for k, v in asdict(p).items():
            setattr(p, k, _env("MA200_" + k.upper(), v, type(v)))
        return p


# ======================================================================== индикаторы
def rma(x: np.ndarray, n: int) -> np.ndarray:
    """ta.rma: первое значение — SMA первых n, дальше (prev·(n−1) + x)/n."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    tr = np.empty(len(h))
    if len(h):
        tr[0] = h[0] - l[0]
        pc = c[:-1]
        tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    return rma(tr, n)


def moving(c: pd.Series, kind: str, n: int) -> np.ndarray:
    if kind == "EMA":
        return c.ewm(span=n, adjust=False).mean().to_numpy().copy()   # = ta.ema (старт с первого значения)
    return c.rolling(n).mean().to_numpy().copy()


def analyze(df: pd.DataFrame, P: Params, kind: str) -> Dict[str, np.ndarray]:
    """Условия по всем свечам. sig[j] — свеча j сигнальная; pier[j] — индекс свечи прокола (или −1)."""
    o, h, l, c, v = (df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    n = len(df)
    ma = moving(df["close"], kind, P.ma_len)
    if kind == "EMA":
        ma[:P.ma_len - 1] = np.nan                 # пока нет 200 свечей — EMA не считаем
    at = atr(h, l, c, P.atr_len)
    vma = pd.Series(v).rolling(P.vol_len).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        pierce = (l < ma) & (l >= ma - P.depth_atr * at) & (c > ma - P.close_atr * at)
        trend = np.zeros(n, bool)
        trend[P.slope:] = ma[P.slope:] >= ma[:-P.slope]
        alt = (c > ma) & (v > vma)
    main = np.zeros(n, bool)
    pier = np.full(n, -1)
    for j in range(1, n):
        if not trend[j]:
            continue
        for i in range(1, P.n + 1):            # ближайшая к сигналу свеча прокола
            k = j - i
            if k < 0 or not pierce[k]:
                continue
            if pier[j] < 0:
                pier[j] = k
            if c[j] > o[j] and c[j] > h[k]:
                main[j] = True
                pier[j] = k                    # основной триггер — от свечи, чей максимум пробит
                break
    sig = trend & (pier >= 0) & (main | alt)
    return dict(sig=sig, main=main, alt=alt & (pier >= 0), pier=pier, ma=ma, atr=at, vma=vma)


def signal_at(df: pd.DataFrame, P: Params, kind: str, j: int = -1) -> Optional[dict]:
    """Сигнал на свече j (по умолчанию последней) или None."""
    if len(df) < P.ma_len + P.slope + 1:
        return None
    a = analyze(df, P, kind)
    j = j % len(df)
    if not a["sig"][j]:
        return None
    k = int(a["pier"][j])
    c, v, low = float(df["close"].iloc[j]), float(df["volume"].iloc[j]), float(df["low"].iloc[k])
    ma, at, vma = float(a["ma"][j]), float(a["atr"][k]), float(a["vma"][j])
    trig = [t for t, ok in (("main", a["main"][j]), ("alt", a["alt"][j])) if ok]
    return dict(ma=kind, trig=trig, bar=str(df.index[j].date()), price=round(c, 4), ma_val=round(ma, 4),
                dist=round((c / ma - 1) * 100, 2), vol_ratio=round(v / vma * 100) if vma > 0 else None,
                pierce=str(df.index[k].date()), bars_ago=j - k,
                depth=round((float(a["ma"][k]) - low) / at, 2) if at > 0 else None,
                pierce_high=round(float(df["high"].iloc[k]), 4))


# ======================================================================== свечи
OHLCV = dict(open="first", high="max", low="min", close="last", volume="sum")


def _left(bucket_end: date, now: pd.Timestamp) -> bool:
    """Остались ли ещё торговые часы в периоде, который заканчивается bucket_end (выходные не считаются)."""
    today = now.date()
    if bucket_end < today:
        return False
    if today.weekday() < 5 and (now.hour, now.minute) < (16, 15):
        return True                                   # сегодняшняя сессия ещё не закрыта
    d = today + timedelta(days=1)
    while d <= bucket_end:
        if d.weekday() < 5:
            return True
        d += timedelta(days=1)
    return False


def daily_closed(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    if len(df) and _left(df.index[-1].date(), now):
        return df.iloc[:-1]
    return df


def weekly(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    w = df.resample("W-FRI").agg(OHLCV).dropna(subset=["open", "close"])
    if len(w) and _left(w.index[-1].date(), now):
        w = w.iloc[:-1]
    return w


def monthly(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    m = df.resample("MS").agg(OHLCV).dropna(subset=["open", "close"])
    if len(m):
        end = (m.index[-1] + pd.offsets.MonthEnd(0)).date()
        if _left(end, now):
            m = m.iloc[:-1]
    return m


def fetch_monthly(tickers: List[str], batch: int = 80) -> Dict[str, pd.DataFrame]:
    """Месячные свечи за всю историю (для EMA200 на месяцах нужно больше 20 лет)."""
    import yfinance as yf
    out: Dict[str, pd.DataFrame] = {}
    for b in range(0, len(tickers), batch):
        part = tickers[b:b + batch]
        raw = None
        for attempt in range(3):
            try:
                raw = yf.download(part, period="max", interval="1mo", auto_adjust=True, group_by="ticker",
                                  threads=True, progress=False)
                break
            except Exception as e:
                log.warning("yfinance 1mo: %s", e)
                time.sleep(10 * (attempt + 1))
        if raw is None:
            continue
        for t in part:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
                df = df.dropna(subset=["open", "high", "low", "close"])
                if len(df):
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    out[t] = df
            except Exception:
                pass
    return out


# ======================================================================== прогон
def check_ticker(t: str, frames: Dict[str, pd.DataFrame], P: Params) -> List[dict]:
    rows = []
    for tf, df in frames.items():
        for kind in MAS:
            try:
                s = signal_at(df, P, kind)
            except Exception as e:
                log.debug("%s %s %s: %s", t, tf, kind, e)
                s = None
            if s:
                rows.append(dict(s, ticker=t, tf=tf))
    return rows


def run(P: Params, tickers: Optional[List[str]] = None, now: Optional[pd.Timestamp] = None,
        batch: int = 80) -> dict:
    import options
    import seasonal
    now = now or pd.Timestamp.now(tz=ET)
    explicit = tickers is not None
    if tickers is None:
        SP = seasonal.Params(min_price=P.min_price, min_dvol=P.min_dvol)
        u = seasonal.all_us_stocks()
        tickers = seasonal.prefilter(u, SP)
        log.info("акций США: %d, после отсева по цене/обороту: %d", len(u), len(tickers))
    SP = seasonal.Params(min_price=P.min_price, min_dvol=P.min_dvol)
    rows: List[dict] = []
    checked = {tf: 0 for tf in TFS}
    last_bar: Dict[str, str] = {}
    loaded = 0
    for b in range(0, len(tickers), batch):
        part = tickers[b:b + batch]
        daily = seasonal.download(part, P.years)
        loaded += len(daily)
        daily = {t: df for t, df in daily.items() if explicit or seasonal.liquid(df, SP)}
        mon = fetch_monthly(list(daily)) if daily else {}
        for t, d in daily.items():
            d = d[(d["open"] > 0) & (d["low"] > 0)]
            frames = {"D": daily_closed(d, now), "W": weekly(d, now)}
            if t in mon:
                m = mon[t]
                frames["M"] = monthly(m[(m["open"] > 0) & (m["low"] > 0)], now)
            for tf, f in frames.items():
                if len(f) >= P.ma_len + P.slope + 1:
                    checked[tf] += 1
                    lb = str(f.index[-1].date())
                    if lb > last_bar.get(tf, ""):
                        last_bar[tf] = lb
            rows += check_ticker(t, frames, P)
        log.info("MA200: %d/%d, сигналов пока %d", min(b + batch, len(tickers)), len(tickers), len(rows))
    if not explicit and loaded < 0.5 * len(tickers):
        raise RuntimeError(f"история загрузилась только по {loaded} из {len(tickers)} акций")
    # сигналы только по последней закрытой свече таймфрейма (у тикеров с пропусками — отбрасываем старые)
    rows = [r for r in rows if r["bar"] == last_bar.get(r["tf"])]

    oi_dropped = []
    if rows and P.oi_min:
        found = sorted({r["ticker"] for r in rows})
        try:
            oi = options.refresh(found, budget_s=600)
        except Exception as e:
            log.warning("OI: %s", e)
            oi = options.load()
        keep = []
        for r in rows:
            r["oi"] = options.oi_of(oi, r["ticker"])
            if options.passes(oi, r["ticker"], P.oi_min):
                keep.append(r)
            elif r["ticker"] not in oi_dropped:
                oi_dropped.append(r["ticker"])
        rows = keep
    rows.sort(key=lambda r: (TFS.index(r["tf"]), -(r["vol_ratio"] or 0)))
    return dict(updated=pd.Timestamp.now(tz="UTC").isoformat(), today=now.date().isoformat(),
                params=asdict(P), checked=checked, bars=last_bar, rows=rows, oi_dropped=oi_dropped)


# ======================================================================== Telegram
TF_NAME = {"D": "Дневки", "W": "Недели", "M": "Месяцы"}
TF_TV = {"D": "D", "W": "W", "M": "M"}
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь",
          "октябрь", "ноябрь", "декабрь"]


def bar_label(tf: str, iso: str) -> str:
    if tf == "M":
        return f"{MONTHS[int(iso[5:7]) - 1]} {iso[:4]}"
    if tf == "W":
        return f"неделя до {iso[8:10]}.{iso[5:7]}"
    return f"{iso[8:10]}.{iso[5:7]}"


def key_of(r: dict) -> str:
    return f"{r['ticker']}|{r['tf']}|{r['pierce']}"


def merge(rows: List[dict]) -> List[dict]:
    """Одна строка на тикер и таймфрейм: EMA и SMA вместе."""
    out: Dict[tuple, dict] = {}
    for r in rows:
        k = (r["ticker"], r["tf"])
        if k not in out:
            out[k] = dict(r, mas=[r["ma"]], trigs=set(r["trig"]))
        else:
            out[k]["mas"].append(r["ma"])
            out[k]["trigs"] |= set(r["trig"])
    return list(out.values())


def build_message(res: dict, rows: List[dict], top: int = 40) -> str:
    P = res["params"]
    lines = ["📈 <b>MA200: ложный пробой и отскок</b> — новые сигналы",
             f"<i>Прокол 200-й средней (EMA/SMA) не глубже {P['depth_atr']:g} ATR на одной из {P['n']} свечей "
             f"и разворот вверх" + (f", OI опционов ≥ {P['oi_min']}" if P['oi_min'] else "") + ".</i>"]
    m = merge(rows)
    for tf in TFS:
        rs = [r for r in m if r["tf"] == tf]
        if not rs:
            continue
        rs.sort(key=lambda r: -(r["vol_ratio"] or 0))
        lines += ["", f"<b>{TF_NAME[tf]}</b> · {bar_label(tf, rs[0]['bar'])}"]
        for r in rs[:top]:
            tv = r["ticker"].replace("-", ".")
            link = (f'<a href="https://www.tradingview.com/chart/?symbol={html.escape(tv)}&amp;interval={TF_TV[tf]}">'
                    f'<b>{html.escape(r["ticker"])}</b></a>')
            trig = " + ".join({"main": "закрытие выше хая прокола", "alt": "выше MA на объёме"}[t]
                              for t in ("main", "alt") if t in r["trigs"])
            oi = ""
            if r.get("oi") is not None:
                oi = f" · OI {r['oi'] / 1000:.0f}K" if r["oi"] >= 1000 else f" · OI {r['oi']}"
            lines.append(f"• {link} {r['price']:.2f} · {'+'.join(sorted(r['mas']))} {r['dist']:+.1f}% от MA · "
                         f"объём {r['vol_ratio'] or 0}%\n   прокол {r['pierce'][8:10]}.{r['pierce'][5:7]}"
                         f" ({r['depth']:.2f} ATR) · {trig}{oi}")
        if len(rs) > top:
            lines.append(f"   … и ещё {len(rs) - top} — на сайте")
    lines += ["", "<i>Полный список и фильтры — вкладка «MA200» на сайте. Не инвестиционная рекомендация.</i>"]
    return "\n".join(lines)


def load_sent() -> Dict[str, str]:
    try:
        return json.loads(SENT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_sent(sent: Dict[str, str], today: str) -> None:
    cut = (date.fromisoformat(today) - timedelta(days=500)).isoformat()
    sent = {k: v for k, v in sent.items() if v >= cut}
    SENT_FILE.parent.mkdir(exist_ok=True)
    SENT_FILE.write_text(json.dumps(sent, separators=(",", ":")), encoding="utf-8")


# ======================================================================== запуск
def main():
    import bot
    import tg
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tg", action="store_true")
    ap.add_argument("--tickers", help="свой список через запятую вместо всех акций")
    ap.add_argument("--out", help="куда сохранить (по умолчанию cache/ma200.json)")
    ap.add_argument("--if-new", action="store_true", help="пропустить, если сегодня после закрытия уже считали")
    a = ap.parse_args()
    now = pd.Timestamp.now(tz=ET)
    if a.if_new:
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        if prev.get("bars", {}).get("D") == now.date().isoformat():
            log.info("MA200 за сегодня уже посчитан — пропускаю")
            out = os.environ.get("GITHUB_OUTPUT")
            if out:
                with open(out, "a") as f:
                    f.write("skip=true\n")
            return
    P = Params.from_env()
    tickers = [x.strip().upper() for x in a.tickers.split(",")] if a.tickers else None
    res = run(P, tickers, now)
    out = BASE / a.out if a.out else OUT_FILE
    sent = load_sent()
    for r in res["rows"]:
        r["new"] = key_of(r) not in sent
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("MA200: сигналов %d (новых %d)", len(res["rows"]), sum(r["new"] for r in res["rows"]))
    if a.tickers:
        print(build_message(res, res["rows"], P.top))
        return
    fresh = [r for r in res["rows"] if r["new"]]
    msg = build_message(res, fresh, P.top) if fresh else ""
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if a.no_tg or not token:
        print(msg or "новых сигналов нет")
    elif msg:
        k = tg.broadcast_one(token, tg.load(), msg, key="ma200")
        log.info("MA200 разослан подписчикам: %d", k)
    for r in res["rows"]:
        sent.setdefault(key_of(r), res["today"])
    save_sent(sent, res["today"])


if __name__ == "__main__":
    main()
