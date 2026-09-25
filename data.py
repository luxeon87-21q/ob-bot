"""
Данные: список акций и 4-часовые свечи США.

4H свечи собираются из часовых свечей Yahoo Finance так же, как их строит TradingView
для американских акций в обычную сессию (без пре/постмаркета):
    бар 09:30–13:30 ET  и  бар 13:30–16:00 ET
"""
from __future__ import annotations

import io
import re
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd
import requests

log = logging.getLogger("ob_bot.data")

ET = "America/New_York"
BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache"
BARS_DIR = CACHE / "bars"

WIKI = {
    "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"),
    "nasdaq100": ("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker"),
}
UA = {"User-Agent": "Mozilla/5.0 (ob-bot; personal use)"}


# ---------------------------------------------------------------- список акций
EXTRA_URLS = {"nasdaq100": ["https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"]}


def _nasdaq_api() -> List[str]:
    r = requests.get("https://api.nasdaq.com/api/quote/list-type/nasdaq100",
                     headers={**UA, "Accept": "application/json"}, timeout=30).json()
    rows = r["data"]["data"]["rows"]
    return [str(x["symbol"]).strip().replace(".", "-") for x in rows if x.get("symbol")]


def _wiki_tickers(name: str) -> List[str]:
    errors = []
    for url in [WIKI[name][0]] + EXTRA_URLS.get(name, []):
        try:
            return _wiki_table(url)
        except Exception as e:
            errors.append(f"{url}: {e}")
    if name == "nasdaq100":
        try:
            tick = _nasdaq_api()
            if len(tick) >= 50:
                return tick
        except Exception as e:
            errors.append(f"api.nasdaq.com: {e}")
    raise RuntimeError("; ".join(errors))


def _wiki_table(url: str) -> List[str]:
    html = requests.get(url, headers=UA, timeout=30).text
    best: List[str] = []
    for table in pd.read_html(io.StringIO(html)):
        for col in table.columns:
            name = " ".join(map(str, col)) if isinstance(col, tuple) else str(col)
            name = name.strip().lower()
            if "ticker" in name or "symbol" in name:
                vals = [str(v).strip().replace(".", "-") for v in table[col].dropna()]
                vals = [v for v in vals if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", v)]
                if len(vals) > len(best):
                    best = vals
    if len(best) < 50:
        raise RuntimeError(f"не нашёл список тикеров на {url}")
    return best


def load_universe(spec: str, tickers_file: Path) -> List[str]:
    """spec: через запятую — sp500, nasdaq100, file (tickers.txt)."""
    CACHE.mkdir(exist_ok=True)
    out: List[str] = []
    for part in [p.strip().lower() for p in spec.split(",") if p.strip()]:
        if part == "file":
            if tickers_file.exists():
                for line in tickers_file.read_text(encoding="utf-8").splitlines():
                    line = line.split("#")[0].strip().upper()
                    if line:
                        out.append(line)
            continue
        if part not in WIKI:
            raise ValueError(f"неизвестный список: {part}")
        cache = CACHE / f"universe_{part}.json"
        fresh = cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400
        if fresh:
            out += json.loads(cache.read_text())
            continue
        try:
            tick = _wiki_tickers(part)
            cache.write_text(json.dumps(tick))
            out += tick
        except Exception as e:
            if cache.exists():
                log.warning("не обновил %s (%s), беру старый кэш", part, e)
                out += json.loads(cache.read_text())
            else:
                log.warning("список %s не загрузился: %s", part, e)
    if not out:
        raise RuntimeError("не удалось загрузить ни один список акций")
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


# ---------------------------------------------------------------- свечи
def to_4h(hourly: pd.DataFrame, now_et: pd.Timestamp | None = None) -> pd.DataFrame:
    """Часовые свечи (RTH) -> 4H как в TradingView. Незакрытый бар отбрасывается."""
    if hourly.empty:
        return hourly
    df = hourly.copy()
    idx = df.index
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    df.index = idx.tz_convert(ET)
    mins = df.index.hour * 60 + df.index.minute
    df = df[(mins >= 570) & (mins < 960)]          # 09:30–16:00
    mins = df.index.hour * 60 + df.index.minute
    day = df.index.normalize()
    key = day + pd.to_timedelta(((mins >= 810) * 240 + 570), unit="min")   # 09:30 или 13:30
    g = df.groupby(key)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    }).dropna(subset=["open", "high", "low", "close"])
    out.index.name = "time"

    now_et = now_et or pd.Timestamp.now(tz=ET)
    end = out.index + pd.to_timedelta(
        [240 if t.hour == 9 else 150 for t in out.index], unit="min")
    return out[end <= now_et]


def _download(tickers: List[str], period: str) -> Dict[str, pd.DataFrame]:
    import yfinance as yf

    raw = yf.download(tickers, period=period, interval="1h", group_by="ticker",
                      auto_adjust=False, prepost=False, threads=True, progress=False)
    res: Dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return res
    for t in tickers:
        try:
            sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        sub = sub.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        sub = sub.dropna(subset=["close"])
        if not sub.empty:
            res[t] = sub
    return res


def update_bars(tickers: Iterable[str], history: str = "720d", chunk: int = 80,
                pause: float = 1.0) -> Dict[str, pd.DataFrame]:
    """
    Возвращает 4H свечи по всем тикерам. Первая загрузка — history (до ~2 лет, лимит Yahoo для 1h),
    дальше докачиваются только последние дни и склеиваются с кэшем.
    """
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    tickers = list(tickers)
    cached: Dict[str, pd.DataFrame] = {}
    need_full, need_tail = [], []
    for t in tickers:
        f = BARS_DIR / f"{t}.csv"
        if f.exists():
            df = pd.read_csv(f, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
            cached[t] = df
            last = df.index[-1] if len(df) else None
            if last is None or pd.Timestamp.now(tz=ET) - last > timedelta(days=5):
                need_full.append(t)
            else:
                need_tail.append(t)
        else:
            need_full.append(t)

    now_et = pd.Timestamp.now(tz=ET)
    result: Dict[str, pd.DataFrame] = {}
    for group, period in ((need_full, history), (need_tail, "7d")):
        for i in range(0, len(group), chunk):
            part = group[i:i + chunk]
            try:
                fresh = _download(part, period)
            except Exception as e:
                log.warning("ошибка загрузки %s…: %s", part[:3], e)
                fresh = {}
            for t in part:
                old = cached.get(t)
                if t in fresh:
                    new4 = to_4h(fresh[t], now_et)
                    if old is not None and period != history:
                        df = pd.concat([old[old.index < new4.index.min()], new4]) if len(new4) else old
                    else:
                        df = new4
                    df = df[~df.index.duplicated(keep="last")].sort_index()
                    df.to_csv(BARS_DIR / f"{t}.csv")
                    result[t] = df
                elif old is not None:
                    result[t] = old
            if pause:
                time.sleep(pause)
    return result
