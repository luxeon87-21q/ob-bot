"""
Сборка статического сайта (для GitHub Pages) + проверка + Telegram.

  python build_site.py          — проверить акции, отправить сигналы, собрать сайт в папку site/
  python build_site.py --no-tg  — без Telegram

Запускается по расписанию из .github/workflows/scan.yml
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import pandas as pd

import bot
import data
import web

BASE = Path(__file__).resolve().parent
SITE = BASE / "site"
CHAT_FILE = BASE / "cache" / "chat_id"
log = logging.getLogger("ob_bot.site")


def discover_chat_id(token: str) -> str:
    """Если TELEGRAM_CHAT_ID не задан — берём чат, из которого боту писали /start."""
    if CHAT_FILE.exists():
        return CHAT_FILE.read_text().strip()
    try:
        ups = bot.tg("getUpdates", token)
    except Exception as e:
        log.warning("getUpdates: %s", e)
        return ""
    for u in reversed(ups):
        ch = (u.get("message") or {}).get("chat") or {}
        if ch.get("type") == "private" and ch.get("id"):
            cid = str(ch["id"])
            CHAT_FILE.parent.mkdir(exist_ok=True)
            CHAT_FILE.write_text(cid)
            bot.send("✅ Бот ордер-блоков подключён. Сигналы будут приходить сюда после закрытия "
                     "4H свечей (20:40 и 23:10 по Кишинёву).", dict(token=token, chat_id=cid))
            return cid
    return ""


def build(s: dict, telegram: bool) -> None:
    if telegram and s["token"] and not s["chat_id"]:
        s["chat_id"] = discover_chat_id(s["token"])
    run = dict(s)
    if not telegram:
        run["token"] = ""
    n = bot.run_once(run, dry=not (telegram and s["token"] and s["chat_id"]))
    log.info("сигналов: %d", n)

    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "data" / "chart").mkdir(parents=True)
    html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("<head>", "<head>\n<script>window.OB_STATIC = true;</script>", 1)
    (SITE / "index.html").write_text(html, encoding="utf-8")
    (SITE / ".nojekyll").write_text("")

    shutil.copy(bot.SNAPSHOT_FILE, SITE / "data" / "snapshot.json")
    (SITE / "data" / "signals.json").write_text(
        json.dumps(web.read_signals(2000), ensure_ascii=False), encoding="utf-8")

    snap = json.loads(bot.SNAPSHOT_FILE.read_text(encoding="utf-8"))
    ok = 0
    for row in snap["rows"]:
        t = row["ticker"]
        try:
            payload = web.chart_payload(t, s, bars_limit=400)
        except Exception as e:
            log.warning("график %s: %s", t, e)
            continue
        (SITE / "data" / "chart" / f"{t}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        ok += 1

    now = pd.Timestamp.now(tz=data.ET)
    nxt = bot.next_run(now, 10) + pd.Timedelta(minutes=10)   # GitHub запускает с небольшой задержкой
    (SITE / "data" / "status.json").write_text(json.dumps(dict(
        running=False, last_run=now.isoformat(), next_run=nxt.isoformat(), last_error=None,
        last_signals=n, telegram=bool(telegram and s["token"] and s["chat_id"]))), encoding="utf-8")
    log.info("сайт собран: %d графиков", ok)


def main():
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    p = argparse.ArgumentParser()
    p.add_argument("--no-tg", action="store_true")
    a = p.parse_args()
    build(bot.settings(), telegram=not a.no_tg)


if __name__ == "__main__":
    main()
