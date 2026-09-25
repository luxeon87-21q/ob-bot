"""
Веб-версия бота ордер-блоков.

  python3 web.py            — сайт на http://localhost:8000 + проверки по расписанию + Telegram
  python3 web.py --no-tg    — то же, но без отправки в Telegram

Переменные в .env:
  WEB_HOST=127.0.0.1   (0.0.0.0 — открыть доступ с других устройств / на VPS)
  WEB_PORT=8000
  WEB_PASSWORD=        (если задан — вход по паролю, логин любой)
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

import bot
import data
from order_blocks import detect

BASE = Path(__file__).resolve().parent
INDEX = BASE / "static" / "index.html"
log = logging.getLogger("ob_bot.web")


class Scanner:
    def __init__(self, s: dict, telegram: bool):
        self.s = s
        self.telegram = telegram
        self.lock = threading.Lock()
        self.running = False
        self.stage = ""
        self.done = 0
        self.total = 0
        self.last_run = None
        self.last_error = None
        self.last_signals = None
        self.next_run = None

    def status(self):
        return dict(running=self.running, stage=self.stage, done=self.done, total=self.total,
                    last_run=self.last_run, last_error=self.last_error,
                    last_signals=self.last_signals,
                    next_run=self.next_run.isoformat() if self.next_run is not None else None,
                    telegram=bool(self.telegram and self.s["token"] and self.s["chat_id"]))

    def _progress(self, stage, done, total):
        self.stage, self.done, self.total = stage, done, total

    def scan(self):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            self.running, self.last_error = True, None
            s = dict(self.s)
            if not self.telegram:
                s["token"] = ""
            n = bot.run_once(s, dry=not self.telegram, progress=self._progress)
            self.last_signals = n
            self.last_run = pd.Timestamp.now(tz=data.ET).isoformat()
        except Exception as e:
            log.exception("ошибка проверки")
            self.last_error = str(e)
        finally:
            self.running, self.stage = False, ""
            self.lock.release()
        return True

    def scan_async(self):
        if self.running:
            return False
        threading.Thread(target=self.scan, daemon=True).start()
        return True

    def schedule_loop(self):
        while True:
            self.next_run = bot.next_run(pd.Timestamp.now(tz=data.ET), self.s["delay_min"])
            while pd.Timestamp.now(tz=data.ET) < self.next_run:
                time.sleep(20)
            self.scan()


def wall_epoch(ts: pd.Timestamp) -> int:
    """Время ET как 'наивное' epoch — чтобы график показывал нью-йоркское время."""
    return int(pd.Timestamp(ts.tz_localize(None)).timestamp()) if ts.tzinfo else int(ts.timestamp())


def chart_payload(ticker: str, s: dict, bars_limit: int = 500) -> dict:
    f = data.BARS_DIR / f"{ticker}.csv"
    if not f.exists():
        got = data.update_bars([ticker])
        if ticker not in got:
            raise KeyError(ticker)
    df = pd.read_csv(f, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(data.ET)
    events, bulls, bears = detect(df, s["length"], s["mitigation"], s["track_last"])
    view = df.iloc[-bars_limit:]
    start = view.index[0]
    close = float(df["close"].iloc[-1])
    k = s["track_last"] or None
    obs = []
    for o in bulls[:k] + bears[:k]:
        d = bot.ob_dict(o, close)
        d["t"] = wall_epoch(max(o.ob_time, start))
        obs.append(d)
    evs = []
    for e in events:
        if e.bar_time >= start:
            d = bot.event_dict(ticker, e)
            d["t"] = wall_epoch(e.bar_time)
            evs.append(d)
    candles = [dict(time=wall_epoch(t), open=round(r.open, 4), high=round(r.high, 4), low=round(r.low, 4),
                    close=round(r.close, 4), volume=int(r.volume)) for t, r in view.iterrows()]
    return dict(ticker=ticker, candles=candles, obs=obs, events=evs, close=close,
                last_bar=df.index[-1].isoformat())


def read_signals(limit: int) -> list:
    f = bot.HISTORY_FILE
    if not f.exists():
        return []
    lines = f.read_text(encoding="utf-8").splitlines()
    rows, seen = [], set()
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except Exception:
            continue
        key = (r["ticker"], r["type"], r["side"], r["bar_time"], r["top"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: r["bar_time"], reverse=True)
    return rows


def make_handler(scanner: Scanner, password: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self):
            if not password:
                return True
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    if base64.b64decode(hdr[6:]).decode().split(":", 1)[1] == password:
                        return True
                except Exception:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="OB bot"')
            self.end_headers()
            return False

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    body = INDEX.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif u.path == "/api/status":
                    self._json(scanner.status())
                elif u.path == "/api/snapshot":
                    f = bot.SNAPSHOT_FILE
                    self._json(json.loads(f.read_text(encoding="utf-8")) if f.exists() else None)
                elif u.path == "/api/signals":
                    self._json(read_signals(int(q.get("limit", ["500"])[0])))
                elif u.path.startswith("/api/chart/"):
                    t = u.path.rsplit("/", 1)[1].upper().strip()
                    try:
                        self._json(chart_payload(t, scanner.s))
                    except KeyError:
                        self._json({"error": f"нет данных по {t}"}, 404)
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            if not self._auth():
                return
            if urlparse(self.path).path == "/api/scan":
                self._json({"started": scanner.scan_async()})
            else:
                self._json({"error": "not found"}, 404)

    return H


def main():
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(BASE / "bot.log", encoding="utf-8")])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    p = argparse.ArgumentParser()
    p.add_argument("--no-tg", action="store_true", help="не отправлять в Telegram")
    p.add_argument("--no-schedule", action="store_true", help="без проверок по расписанию")
    a = p.parse_args()

    s = bot.settings()
    host = bot.cfg("WEB_HOST", "127.0.0.1")
    port = bot.cfg("WEB_PORT", 8000, int)
    scanner = Scanner(s, telegram=not a.no_tg)
    if not a.no_schedule:
        threading.Thread(target=scanner.schedule_loop, daemon=True).start()
    if not bot.SNAPSHOT_FILE.exists():
        log.info("данных ещё нет — запускаю первую проверку")
        scanner.scan_async()
    srv = ThreadingHTTPServer((host, port), make_handler(scanner, bot.cfg("WEB_PASSWORD", "")))
    log.info("сайт: http://%s:%d", "localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
