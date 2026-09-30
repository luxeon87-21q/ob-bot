"""
Открытый интерес (OI) по опционам: сумма коллов и путов по всем экспирациям
в ближайшие 30 дней. Используется как фильтр сигналов ордер-блоков (OI ≥ 5000).

Данные Yahoo обновляются раз в сутки (биржа публикует OI после закрытия),
поэтому каждый тикер пересчитывается не чаще раза в день.
Кэш: cache/options.json  {"AAPL": {"oi": 812345, "date": "2026-09-30", "exp": 5}, ...}
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd

BASE = Path(__file__).resolve().parent
OPTIONS_FILE = BASE / "cache" / "options.json"
ET = "America/New_York"
OI_MIN = 5000          # порог фильтра, контрактов
DAYS = 30              # экспирации в ближайшие N дней
log = logging.getLogger("ob_bot.options")


def load() -> Dict[str, dict]:
    try:
        return json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(d: Dict[str, dict]) -> None:
    OPTIONS_FILE.parent.mkdir(exist_ok=True)
    OPTIONS_FILE.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")


def _yahoo_oi(ticker: str, today: pd.Timestamp, days: int = DAYS) -> Optional[dict]:
    """Сумма OI коллов и путов по экспирациям в [сегодня, сегодня + days]. None — не удалось получить."""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    exps = tk.options or ()
    end = today + pd.Timedelta(days=days)
    use = [e for e in exps if today <= pd.Timestamp(e) <= end]
    total = 0
    for e in use:
        ch = tk.option_chain(e)
        for side in (ch.calls, ch.puts):
            if side is not None and "openInterest" in side:
                total += int(pd.to_numeric(side["openInterest"], errors="coerce").fillna(0).sum())
    return {"oi": total, "exp": len(use)}


def refresh(tickers: Iterable[str], fetch=_yahoo_oi, workers: int = 6, budget_s: float = 900) -> Dict[str, dict]:
    """Обновляет OI тикеров, у которых данные не сегодняшние. Ошибки не валят проверку:
    при неудаче остаётся прошлое значение. Ограничение по времени — остальные дообновятся в следующий раз."""
    data = load()
    today = pd.Timestamp.now(tz=ET).normalize().tz_localize(None)
    tstr = today.strftime("%Y-%m-%d")
    todo = [t for t in tickers if data.get(t, {}).get("date") != tstr]
    if not todo:
        return data
    log.info("опционы: обновляю OI по %d акциям", len(todo))
    t0, ok, fail = time.time(), 0, 0
    yahoo = [t.replace(".", "-") for t in todo]
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(_safe, fetch, y, today): t for t, y in zip(todo, yahoo)}
        for f in as_completed(futs):
            t, r = futs[f], f.result()
            if r is None:
                fail += 1
            else:
                data[t] = dict(r, date=tstr)
                ok += 1
            if time.time() - t0 > budget_s:
                log.warning("опционы: лимит времени, обновлено %d, остальные — в следующий раз", ok)
                for g in futs:
                    g.cancel()
                break
    log.info("опционы: обновлено %d, ошибок %d", ok, fail)
    save(data)
    return data


def _safe(fetch, ticker, today):
    for attempt in range(2):
        try:
            return fetch(ticker, today)
        except Exception as e:
            if attempt:
                log.debug("OI %s: %s", ticker, e)
            time.sleep(1.5)
    return None


def oi_of(data: Dict[str, dict], ticker: str) -> Optional[int]:
    r = data.get(ticker)
    return None if r is None else int(r["oi"])


def passes(data: Dict[str, dict], ticker: str, min_oi: int = OI_MIN) -> bool:
    """Проходит ли тикер фильтр. Если OI неизвестен (Yahoo не отдал) — не отсекаем, чтобы не терять сигналы."""
    oi = oi_of(data, ticker)
    return oi is None or oi >= min_oi


def fmt_oi(oi: Optional[int]) -> str:
    if oi is None:
        return "OI н/д"
    if oi >= 1_000_000:
        return f"OI {oi / 1e6:.1f}M"
    if oi >= 1000:
        return f"OI {oi / 1000:.1f}K"
    return f"OI {oi}"
