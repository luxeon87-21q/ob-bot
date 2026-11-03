"""
SEC EDGAR: общий доступ для фундаментала (fundamentals.py) и инсайдеров (insiders.py).

SEC требует User-Agent с контактом и не больше 10 запросов в секунду. Контакт задаётся
переменной SEC_USER_AGENT (секрет GitHub), по умолчанию — адрес бота на GitHub.

  https://www.sec.gov/files/company_tickers.json             тикер → CIK
  https://data.sec.gov/submissions/CIK##########.json        список документов компании
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   все числа из отчётов (XBRL)
  https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc> сам документ
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

BASE = Path(__file__).resolve().parent
CIK_FILE = BASE / "cache" / "sec_cik.json"
DEFAULT_UA = "ob-bot (github.com/luxeon87-21q/ob-bot) ob-bot@users.noreply.github.com"
log = logging.getLogger("ob_bot.sec")


class SEC:
    """Запросы к SEC с паузой между ними (общей для всех потоков)."""

    def __init__(self, ua: str = "", rate: float = 8.0):
        self.ua = (ua or os.environ.get("SEC_USER_AGENT") or DEFAULT_UA).strip()
        self.gap = 1.0 / rate
        self._lock = threading.Lock()
        self._last = 0.0
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"})

    def get(self, url: str, timeout: int = 60, tries: int = 3) -> requests.Response:
        for k in range(tries):
            with self._lock:
                wait = self._last + self.gap - time.time()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
            try:
                r = self.s.get(url, timeout=timeout)
            except requests.RequestException as e:
                if k == tries - 1:
                    raise
                time.sleep(2 * (k + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(5 * (k + 1))
                continue
            return r
        return r

    def json(self, url: str) -> Optional[dict]:
        r = self.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ---------------------------------------------------------------- тикеры
    def cik_map(self, max_age_days: float = 7) -> Dict[str, int]:
        try:
            if time.time() - CIK_FILE.stat().st_mtime < max_age_days * 86400:
                return json.loads(CIK_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        raw = self.json("https://www.sec.gov/files/company_tickers.json") or {}
        out = {}
        for v in raw.values():
            t = str(v["ticker"]).upper().replace(".", "-")
            out.setdefault(t, int(v["cik_str"]))
        CIK_FILE.parent.mkdir(exist_ok=True)
        CIK_FILE.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
        return out

    def submissions(self, cik: int) -> Optional[dict]:
        return self.json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")

    def companyfacts(self, cik: int) -> Optional[dict]:
        return self.json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")


def archive_url(cik: int, accession: str, doc: str = "") -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{doc}"


def recent(sub: dict) -> List[dict]:
    """Список последних документов компании из submissions, новые первыми."""
    r = (sub or {}).get("filings", {}).get("recent", {})
    keys = list(r)
    n = len(r.get("accessionNumber", []))
    return [{k: r[k][i] for k in keys} for i in range(n)]


def universe_tickers() -> List[str]:
    """Акции, которые показываются на сайте: ордер-блоки, отчёты, скользящие, опционный скринер."""
    out: List[str] = []
    c = BASE / "cache"
    for name, path in (("snapshot.json", ("rows", "ticker")), ("earnings.json", ("rows", "ticker")),
                       ("ma200.json", ("rows", "ticker")), ("samurai.json", ("rows", "ticker"))):
        try:
            d = json.loads((c / name).read_text(encoding="utf-8"))
            out += [r[path[1]] for r in d.get(path[0], []) if r.get(path[1])]
        except Exception:
            pass
    try:
        out += json.loads((c / "opt_universe.json").read_text(encoding="utf-8")).get("tickers", [])
    except Exception:
        pass
    return sorted(dict.fromkeys(t.upper().replace(".", "-") for t in out))
