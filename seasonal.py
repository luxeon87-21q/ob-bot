"""
Сезонность: акции, которые в одни и те же даты года из года в год делают ход ≥ N%.

Что считается «удачным годом» для окна «старт D, длина L дней»:
  вход по цене открытия первого торгового дня ≥ D;
  за L календарных дней цена КАСАЕТСЯ цели (+10% для роста / −10% для падения),
  и ДО этого не уходит против нас дальше стопа (−5% / +5%).
  Путь внутри окна не важен: может стоять во флэте, может сразу уйти к цели.
  Если цель и стоп задеты в один день — год считается неудачным (консервативно).

Дату окончания задавать не нужно — оптимизатор её находит сам:
  для каждой даты старта берём по каждому году «через сколько дней была достигнута цель»
  и выбираем самое короткое окно L (от MIN_DAYS до MAX_DAYS), в котором цель достигалась
  не реже WIN_MIN (например, в 80% лет).

Защита от случайных совпадений (перебирается много окон и много акций):
  • минимум MIN_YEARS лет истории (берём до 20 последних);
  • «обычно» — как часто такой же ход бывает при входе в ЛЮБОЙ день года (база);
    шанс случайности — биномиальная вероятность получить столько удачных лет при базовой частоте;
  • устойчивость: соседние даты старта (±3 дня) в среднем тоже должны давать высокий процент;
  • последние 5 лет показываются отдельно (свежие годы не должны «сломать» закономерность);
  • для сравнения — средний ход SPY в эти же даты.

Отбор акций: все акции США (список Nasdaq), предварительно цена ≥ $5 и оборот ≥ MIN_DVOL,
затем для найденных — открытый интерес опционов ≥ 5000 (как у ордер-блоков, options.py).

Запуск (по пятницам, .github/workflows/seasonal.yml):
  python seasonal.py                 — полный прогон, Telegram, cache/seasonal.json
  python seasonal.py --no-tg         — без Telegram
  python seasonal.py --tickers AAPL,MSFT --no-tg   — быстрая проверка на своих тикерах
  python seasonal.py --ticker NVDA   — календарь окон по одной акции на весь год
"""
from __future__ import annotations

import argparse
import html
import io
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "seasonal.json"
log = logging.getLogger("ob_bot.seasonal")
UA = {"User-Agent": "Mozilla/5.0 (ob-bot; personal use)", "Accept": "application/json, text/plain, */*"}


def _env(name, default, cast=float):
    v = os.environ.get(name)
    return default if v in (None, "") else cast(v)


@dataclass
class Params:
    move: float = 10.0        # цель, % (минимальный ход)
    adverse: float = 5.0      # стоп против направления, %
    min_days: int = 20        # кратчайшее окно, календарных дней
    max_days: int = 90        # длиннейшее окно
    win_min: float = 0.80     # доля удачных лет, чтобы окно считалось закономерностью
    min_years: int = 10       # минимум лет истории
    max_years: int = 20       # сколько последних лет учитывать
    neighbor: int = 3         # устойчивость: ± дней вокруг даты старта
    neighbor_min: float = 0.65  # средняя доля удачных лет у соседних дат (±neighbor дней)
    p_max: float = 0.01       # максимальный «шанс случайности»
    recent_min: int = 3       # из последних 5 лет удачных не меньше
    ahead_from: int = 1       # искать окна, стартующие через from..to дней после даты прогона
    ahead_to: int = 17        # пятница + 1..17: будни с ближайшего понедельника до понедельника через 2 недели
    min_price: float = 5.0
    min_dvol: float = 5e6     # средний дневной оборот, $ (предфильтр до проверки опционов)
    oi_min: int = 5000        # открытый интерес опционов, контрактов (0 — без фильтра)
    top: int = 15             # сколько лучших в каждом направлении в сообщении

    @classmethod
    def from_env(cls) -> "Params":
        p = cls()
        for k, v in asdict(p).items():
            setattr(p, k, _env("SEAS_" + k.upper(), v, type(v)))
        return p


# ======================================================================== расчёт
def first_hits(o: np.ndarray, h: np.ndarray, l: np.ndarray, dates: np.ndarray,
               move: float, adverse: float, max_days: int, side: str) -> np.ndarray:
    """Для каждого дня входа i: через сколько КАЛЕНДАРНЫХ дней цена коснулась цели раньше стопа.
    np.inf — цель не достигнута за max_days или сначала сработал стоп; np.nan — окно ещё не закончилось."""
    n = len(o)
    d = dates.astype("datetime64[D]").astype(np.int64)
    # сколько торговых дней максимум укладывается в max_days календарных
    H = int(np.max(np.searchsorted(d, d + max_days, side="right") - np.arange(n))) if n else 1
    H = max(H, 1)
    pad = lambda x, v: np.concatenate([x, np.full(H, v)])
    hw = np.lib.stride_tricks.sliding_window_view(pad(h, np.nan), H)[:n]
    lw = np.lib.stride_tricks.sliding_window_view(pad(l, np.nan), H)[:n]
    dw = np.lib.stride_tricks.sliding_window_view(pad(d, np.iinfo(np.int64).max // 2), H)[:n]
    if side == "bull":
        tgt = hw >= (o * (1 + move / 100))[:, None]
        stp = lw <= (o * (1 - adverse / 100))[:, None]
    else:
        tgt = lw <= (o * (1 - move / 100))[:, None]
        stp = hw >= (o * (1 + adverse / 100))[:, None]
    inwin = (dw - d[:, None]) <= max_days
    tgt &= inwin
    stp &= inwin
    ti = np.where(tgt.any(1), tgt.argmax(1), H + 1)
    si = np.where(stp.any(1), stp.argmax(1), H + 1)
    ok = (ti <= H) & (ti < si)
    cal = np.full(n, np.inf)
    idx = np.nonzero(ok)[0]
    cal[idx] = dw[idx, ti[idx]] - d[idx]
    # окно не закрыто (данных ещё нет), а цель пока не достигнута — результат неизвестен
    unknown = (~ok) & (d + max_days > d[-1]) & (si > H)
    cal[unknown] = np.nan
    return cal


def binom_tail(k: int, n: int, p: float) -> float:
    """P(X ≥ k), X ~ Bin(n, p)."""
    if k <= 0:
        return 1.0
    p = min(max(p, 1e-9), 1 - 1e-9)
    return float(sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1)))


class Ticker:
    """История одной акции + предрасчёт первых касаний цели для обоих направлений."""

    def __init__(self, df: pd.DataFrame, P: Params):
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df[(df["open"] > 0) & (df["low"] > 0)]
        self.df = df
        self.P = P
        self.dates = df.index.values.astype("datetime64[D]")
        self.d = self.dates.astype(np.int64)
        o, h, l = (df[c].to_numpy(float) for c in ("open", "high", "low"))
        self.close = df["close"].to_numpy(float)
        self.open = o
        self.hits = {s: first_hits(o, h, l, self.dates, P.move, P.adverse, P.max_days, s)
                     for s in ("bull", "bear")}
        last = pd.Timestamp(self.dates[-1]) if len(self.dates) else pd.Timestamp.now()
        first = pd.Timestamp(self.dates[0]) if len(self.dates) else last
        self.years = [y for y in range(last.year - P.max_years, last.year + 1) if y >= first.year]
        # для базовой частоты: отсортированные «дни до цели» по всем дням входа за учитываемые годы
        start = np.datetime64(date(self.years[0], 1, 1), "D").astype(np.int64) if self.years else 0
        self._base = {}
        for s, v in self.hits.items():
            v = v[self.d >= start]
            self._base[s] = np.sort(v[~np.isnan(v)])

    def entry_index(self, y: int, md: tuple) -> Optional[int]:
        try:
            dd = np.datetime64(date(y, *md), "D").astype(np.int64)
        except ValueError:                       # 29 февраля
            dd = np.datetime64(date(y, md[0], 28), "D").astype(np.int64) + 1
        if dd < self.d[0]:
            return None
        i = int(np.searchsorted(self.d, dd))
        if i >= len(self.d) or self.d[i] - dd > 5:   # дыра в данных
            return None
        return i

    def year_hits(self, md: tuple, side: str):
        """[(год, дней до цели или inf, индекс входа)] по полным годам."""
        out = []
        for y in self.years:
            i = self.entry_index(y, md)
            if i is None:
                continue
            v = self.hits[side][i]
            if np.isnan(v):
                continue
            out.append((y, v, i))
        return out[-self.P.max_years:]

    def base_rate(self, side: str, L: int) -> float:
        """Как часто цель достигается за L дней при входе в любой день (последние max_years лет)."""
        v = self._base[side]
        return float(np.searchsorted(v, L, side="right") / len(v)) if len(v) else 0.0

    def window_return(self, i: int, L: int) -> float:
        j = int(np.searchsorted(self.d, self.d[i] + L, side="right")) - 1
        return float(self.close[j] / self.open[i] - 1) * 100

    def evaluate(self, md: tuple, side: str) -> Optional[dict]:
        P = self.P
        yh = self.year_hits(md, side)
        n = len(yh)
        if n < P.min_years:
            return None
        hits = np.array([v for _, v, _ in yh])
        k_need = max(1, math.ceil(P.win_min * n - 1e-9))   # 0.8*15 = 12.000000000000002
        srt = np.sort(hits)
        if not np.isfinite(srt[k_need - 1]) or srt[k_need - 1] > P.max_days:
            return None
        L = int(max(P.min_days, srt[k_need - 1]))
        wins = int(np.sum(hits <= L))
        base = self.base_rate(side, L)
        pval = binom_tail(wins, n, base)
        recent = yh[-5:]
        rwins = int(sum(v <= L for _, v, _ in recent))
        if pval > P.p_max or rwins < min(P.recent_min, len(recent)):
            return None                       # дальше считаем только для прошедших отбор
        got = hits[hits <= L]
        rets = [self.window_return(i, L) for _, _, i in yh]
        # худший ход против нас до достижения цели (или за всё окно, если цели не было)
        mae = []
        for _, v, i in yh:
            end = self.d[i] + (v if np.isfinite(v) and v <= L else L)
            j = int(np.searchsorted(self.d, end, side="right"))
            seg = self.df.iloc[i:j]
            if side == "bull":
                mae.append((seg["low"].min() / self.open[i] - 1) * 100)
            else:
                mae.append((seg["high"].max() / self.open[i] - 1) * 100)
        return dict(
            side=side, md=md, L=L, n=n, wins=wins, win=wins / n, base=base, p=pval,
            recent_wins=rwins, recent_n=len(recent),
            days_avg=float(np.mean(got)) if len(got) else None,
            ret_avg=float(np.mean(rets)), mae_avg=float(np.mean(mae)),
            mae_worst=float(min(mae) if side == "bull" else max(mae)),
            years=[[y, (None if not np.isfinite(v) or v > L else int(v))] for y, v, _ in yh],
        )

    def win_at(self, md: tuple, side: str, L: int) -> Optional[float]:
        yh = self.year_hits(md, side)
        if len(yh) < self.P.min_years:
            return None
        return float(np.mean([v <= L for _, v, _ in yh]))


def md_of(d: date) -> tuple:
    return (d.month, d.day)


def scan_ticker(tk: Ticker, starts: List[date], P: Params) -> List[dict]:
    """Лучшее окно в каждом направлении среди дат старта starts (с проверкой соседей)."""
    out = []
    for side in ("bull", "bear"):
        best = None
        for s in starts:
            r = tk.evaluate(md_of(s), side)
            if r is None:
                continue
            nb = [tk.win_at(md_of(s + timedelta(days=k)), side, r["L"])
                  for k in range(-P.neighbor, P.neighbor + 1) if k]
            nb = [x for x in nb if x is not None]
            r["neighbor_min"] = float(np.mean(nb)) if nb else 0.0
            if r["neighbor_min"] < P.neighbor_min:
                continue
            r["start"] = s
            key = (r["p"], -r["win"], r["L"])
            if best is None or key < (best["p"], -best["win"], best["L"]):
                best = r
        if best:
            out.append(best)
    return out


def calendar(tk: Ticker, P: Params, year: int) -> List[dict]:
    """Все окна года для одной акции (команда --ticker), без фильтра по соседям."""
    rows = []
    d = date(year, 1, 1)
    while d.year == year:
        for side in ("bull", "bear"):
            r = tk.evaluate(md_of(d), side)
            if r:
                r["start"] = d
                rows.append(r)
        d += timedelta(days=1)
    return rows


# ======================================================================== данные
# не акции: варранты, права, юниты SPAC, префы, облигации (только целые слова — иначе отсекаются
# UnitedHealth, Unity, Brightview и т.п.)
NOT_STOCK = r"\b(?:Warrants?|Rights?|Units?|Preferred|Notes due|Notes|Debentures?)\b"
def all_us_stocks() -> pd.DataFrame:
    """Все акции США: тикер, цена, оборот, капитализация (скринер Nasdaq; запасной — nasdaqtrader)."""
    try:
        r = requests.get("https://api.nasdaq.com/api/screener/stocks",
                         params={"tableonly": "true", "limit": 10000, "download": "true"},
                         headers=UA, timeout=60).json()
        rows = r["data"]["rows"]
        df = pd.DataFrame(rows)
        num = lambda s: pd.to_numeric(s.astype(str).str.replace(r"[$,%]", "", regex=True), errors="coerce")
        out = pd.DataFrame({
            "ticker": df["symbol"].astype(str).str.strip().str.replace("/", "-").str.replace(".", "-"),
            "name": df["name"].astype(str),
            "price": num(df["lastsale"]),
            "volume": num(df["volume"]),
            "mcap": num(df["marketCap"]),
        })
        bad = out["name"].str.contains(NOT_STOCK, case=False, regex=True)
        out = out[~bad & out["ticker"].str.fullmatch(r"[A-Z]{1,5}(-[A-Z])?")]
        if len(out) > 2000:
            return out.reset_index(drop=True)
        raise RuntimeError(f"мало строк: {len(out)}")
    except Exception as e:
        log.warning("скринер Nasdaq недоступен (%s), беру nasdaqtrader", e)
    frames = []
    for url, sym, etf, test in (
            ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol", "ETF", "Test Issue"),
            ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol", "ETF", "Test Issue")):
        txt = requests.get(url, headers=UA, timeout=60).text
        df = pd.read_csv(io.StringIO(txt), sep="|")
        df = df[(df[etf] == "N") & (df[test] == "N")]
        frames.append(pd.DataFrame({"ticker": df[sym].astype(str).str.replace(".", "-"),
                                    "name": df["Security Name"].astype(str)}))
    out = pd.concat(frames)
    bad = out["name"].str.contains(NOT_STOCK, case=False, regex=True)
    out = out[~bad & out["ticker"].str.fullmatch(r"[A-Z]{1,5}(-[A-Z])?")]
    out["price"] = np.nan
    out["volume"] = np.nan
    out["mcap"] = np.nan
    return out.drop_duplicates("ticker").reset_index(drop=True)


def prefilter(u: pd.DataFrame, P: Params) -> List[str]:
    """Грубый отсев по сегодняшним цене и обороту (точный оборот проверяется по истории)."""
    if u["price"].notna().sum() == 0:
        return u["ticker"].tolist()
    m = (u["price"] >= P.min_price) & (u["price"] * u["volume"] >= P.min_dvol * 0.3)
    return u.loc[m, "ticker"].tolist()


def download(tickers: List[str], years: int, batch: int = 80) -> Dict[str, pd.DataFrame]:
    """Дневные свечи (скорректированы на сплиты и дивиденды) за years лет."""
    start = (pd.Timestamp.now() - pd.DateOffset(years=years, days=120)).strftime("%Y-%m-%d")
    out: Dict[str, pd.DataFrame] = {}
    _fetch(tickers, start, batch, out)
    missing = [t for t in tickers if t not in out]
    if missing and len(missing) < len(tickers):
        log.info("история: повторная загрузка %d тикеров", len(missing))
        time.sleep(30)
        _fetch(missing, start, 40, out)
    return out


def _fetch(tickers: List[str], start: str, batch: int, out: Dict[str, pd.DataFrame]) -> None:
    import yfinance as yf
    for b in range(0, len(tickers), batch):
        part = tickers[b:b + batch]
        for attempt in range(3):
            try:
                raw = yf.download(part, start=start, interval="1d", auto_adjust=True, group_by="ticker",
                                  threads=True, progress=False)
                break
            except Exception as e:
                log.warning("yfinance %d–%d: %s", b, b + len(part), e)
                time.sleep(10 * (attempt + 1))
        else:
            continue
        for t in part:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
                df = df.dropna(subset=["open", "high", "low", "close"])
                if len(df) > 250:
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    out[t] = df
            except Exception:
                pass
        log.info("история: %d/%d загружено, есть данные по %d", min(b + batch, len(tickers)), len(tickers), len(out))
        time.sleep(1)


def liquid(df: pd.DataFrame, P: Params) -> bool:
    tail = df.tail(20)
    if tail.empty:
        return False
    return (tail["close"].iloc[-1] >= P.min_price
            and float((tail["close"] * tail["volume"]).median()) >= P.min_dvol)


def next_earnings(ticker: str) -> Optional[date]:
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed:
            ed = ed if isinstance(ed, (list, tuple)) else [ed]
            today = date.today()
            ds = sorted(pd.Timestamp(x).date() for x in ed)
            ds = [x for x in ds if x >= today - timedelta(days=1)]
            return ds[0] if ds else None
    except Exception as e:
        log.debug("отчёт %s: %s", ticker, e)
    return None


# ======================================================================== прогон
def run(P: Params, tickers: Optional[List[str]] = None, today: Optional[date] = None) -> dict:
    today = today or pd.Timestamp.now(tz="America/New_York").date()
    # войти в этом году можно только в будний день — выходные даты старта не берём
    starts = [today + timedelta(days=k) for k in range(P.ahead_from, P.ahead_to + 1)]
    starts = [s for s in starts if s.weekday() < 5]
    explicit = tickers is not None
    if tickers is None:
        u = all_us_stocks()
        tickers = prefilter(u, P)
        log.info("акций США: %d, после отсева по цене/обороту: %d", len(u), len(tickers))
    hist = download(sorted(set(tickers) | {"SPY"}), P.max_years + 1)
    if not explicit and len(hist) < 0.5 * len(tickers):
        # Yahoo не отдал данные — не рассылаем «ничего не найдено», а падаем, чтобы было видно
        raise RuntimeError(f"история загрузилась только по {len(hist)} из {len(tickers)} акций")
    spy = Ticker(hist["SPY"], P) if "SPY" in hist else None

    found = []
    checked = 0
    for t, df in hist.items():
        if t == "SPY" or (not explicit and not liquid(df, P)):
            continue
        checked += 1
        try:
            tk = Ticker(df, P)
            for r in scan_ticker(tk, starts, P):
                r["ticker"] = t
                r["price"] = float(df["close"].iloc[-1])
                if spy is not None:
                    yh = spy.year_hits(md_of(r["start"]), r["side"])
                    if yh:
                        r["spy_ret"] = float(np.mean([spy.window_return(i, r["L"]) for _, _, i in yh]))
                found.append(r)
        except Exception as e:
            log.warning("%s: %s", t, e)
    log.info("проверено ликвидных акций: %d, окон-кандидатов: %d", checked, len(found))

    # фильтр по опционам — только для найденных (дорогой запрос)
    oi_data, today_s = {}, today.isoformat()
    if P.oi_min and found:
        import options
        want = sorted({r["ticker"] for r in found})
        time.sleep(20)                          # пауза после загрузки истории — Yahoo режет частые запросы
        oi_data = options.refresh(want, workers=3, budget_s=1500)
        miss = [t for t in want if oi_data.get(t, {}).get("date") != options_today()]
        if miss:
            log.info("опционы: повтор по %d тикерам", len(miss))
            time.sleep(60)
            oi_data = options.refresh(miss, workers=2, budget_s=600)
    res, dropped, unknown = [], [], 0
    for r in found:
        rec = oi_data.get(r["ticker"]) or {}
        fresh = rec.get("date") == options_today()
        oi = rec.get("oi") if fresh else None
        r["oi"] = oi
        if P.oi_min and oi is not None and oi < P.oi_min:
            dropped.append(f"{r['ticker']}({oi})")
            continue
        if P.oi_min and oi is None:
            unknown += 1                         # OI не получен — показываем с пометкой «OI н/д», не теряем
        res.append(r)
    log.info("кандидатов: %d, отсеяно по OI < %d: %d %s, OI не получен: %d",
             len(found), P.oi_min, len(dropped), " ".join(dropped), unknown)

    res.sort(key=lambda r: (r["p"], -r["win"]))
    for r in res:
        e = next_earnings(r["ticker"])
        r["earnings"] = e.isoformat() if e else None
        end = r["start"] + timedelta(days=r["L"])
        r["earnings_in"] = bool(e and r["start"] - timedelta(days=3) <= e <= end)
    return dict(
        updated=pd.Timestamp.now(tz="UTC").isoformat(),
        today=today.isoformat(),
        starts=[starts[0].isoformat(), starts[-1].isoformat()],
        params=asdict(P), checked=checked, candidates=len(found), oi_dropped=dropped,
        rows=[to_json(r) for r in res],
    )


def options_today() -> str:
    return pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")


def to_json(r: dict) -> dict:
    s: date = r["start"]
    out = {k: v for k, v in r.items() if k not in ("md", "start")}
    out["start"] = s.isoformat()
    out["end"] = (s + timedelta(days=r["L"])).isoformat()
    for k in ("win", "base", "p", "days_avg", "ret_avg", "mae_avg", "mae_worst", "neighbor_min", "spy_ret"):
        if out.get(k) is not None:
            out[k] = round(float(out[k]), 4)
    return out


# ======================================================================== сообщение
def tv_link(t: str) -> str:
    tv = t.replace("-", ".")
    return (f'<a href="https://www.tradingview.com/chart/?symbol={html.escape(tv)}&amp;interval=D">'
            f'<b>{html.escape(t)}</b></a>')


def ddmm(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}"


def fmt_oi(oi) -> str:
    if oi is None:
        return "OI н/д"
    return f"OI {oi / 1000:.0f}K" if oi >= 1000 else f"OI {oi}"


def fmt_p(p: float) -> str:
    p *= 100
    return "<0.01%" if p < 0.01 else f"{p:.2g}%"


def build_message(res: dict, top: int = 15) -> str:
    P = res["params"]
    a, b = res["starts"]
    lines = [f"📅 <b>Сезонность</b> — окна со стартом {ddmm(a)}–{ddmm(b)}",
             f"<i>Цель: ход ≥{P['move']:g}% раньше, чем {P['adverse']:g}% против. "
             f"История {P['min_years']}–{P['max_years']} лет"
             + (f", OI опционов ≥ {P['oi_min']}" if P['oi_min'] else "") + ".</i>"]
    rows = res["rows"]
    if not rows:
        extra = ""
        if res.get("oi_dropped"):
            extra = f"\nЕщё {len(res['oi_dropped'])} отсеяно по OI опционов: " + ", ".join(res["oi_dropped"][:20])
        lines += ["", f"На эти даты устойчивых закономерностей не нашлось (проверено акций: {res['checked']})." + extra]
        return "\n".join(lines)
    for side, title in (("bull", "🟢 <b>РОСТ</b>"), ("bear", "🔴 <b>ПАДЕНИЕ</b>")):
        rs = [r for r in rows if r["side"] == side][:top]
        if not rs:
            continue
        lines += ["", title]
        for r in rs:
            sign = "+" if side == "bull" else "−"
            spy = f" · SPY {r['spy_ret']:+.1f}%" if r.get("spy_ret") is not None else ""
            earn = ""
            if r.get("earnings_in"):
                earn = f"\n   ⚠️ <b>отчёт {ddmm(r['earnings'])}</b> внутри окна"
            elif r.get("earnings"):
                earn = f" · отчёт {ddmm(r['earnings'])}"
            lines.append(
                f"• {tv_link(r['ticker'])} {r['price']:.2f}  <b>{ddmm(r['start'])} → {ddmm(r['end'])}</b> ({r['L']} дн.)\n"
                f"   ✅ {r['wins']} из {r['n']} лет ({r['win'] * 100:.0f}%) · посл. 5 лет {r['recent_wins']}/{r['recent_n']}"
                f" · обычно {r['base'] * 100:.0f}%\n"
                f"   ⏱ цель {sign}{P['move']:g}% в ср. за {r['days_avg'] or 0:.0f} дн. · против нас в ср. {r['mae_avg']:+.1f}%"
                f" · ход за окно {r['ret_avg']:+.1f}%{spy}\n"
                f"   {fmt_oi(r.get('oi'))} · случайность {fmt_p(r['p'])}{earn}")
    lines += ["", "<i>«Обычно» — как часто такой ход бывает при входе в любой день года. "
                  "Чем больше разница с процентом удачных лет и чем меньше «случайность», тем надёжнее. "
                  "Не инвестиционная рекомендация.</i>"]
    return "\n".join(lines)


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
    ap.add_argument("--ticker", help="календарь окон на весь год по одной акции")
    ap.add_argument("--if-new", action="store_true", help="пропустить, если сегодня уже считали")
    a = ap.parse_args()
    if a.if_new:
        try:
            done = json.loads(OUT_FILE.read_text(encoding="utf-8")).get("today")
        except Exception:
            done = None
        if done == pd.Timestamp.now(tz="America/New_York").date().isoformat():
            log.info("сезонность за сегодня уже посчитана — пропускаю")
            return
    P = Params.from_env()

    if a.ticker:
        t = a.ticker.upper()
        hist = download([t], P.max_years + 1)
        if t not in hist:
            print("нет данных по", t)
            return
        tk = Ticker(hist[t], P)
        rows = calendar(tk, P, date.today().year)
        rows.sort(key=lambda r: r["start"])
        for r in rows:
            print(f"{r['side']:4} {r['start']:%d.%m} → {r['start'] + timedelta(days=r['L']):%d.%m} "
                  f"({r['L']:2} дн.)  {r['wins']:2}/{r['n']}  обычно {r['base'] * 100:3.0f}%  "
                  f"посл.5: {r['recent_wins']}/5  случайность {fmt_p(r['p'])}")
        return

    tickers = [x.strip().upper() for x in a.tickers.split(",")] if a.tickers else None
    res = run(P, tickers)
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    msg = build_message(res, P.top)
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if a.no_tg or not token:
        print(msg)
        return
    st = tg.load()
    k = tg.broadcast_one(token, st, msg, key="season")
    log.info("сезонность разослана подписчикам: %d", k)


if __name__ == "__main__":
    main()
