"""
Сборка статического сайта (для GitHub Pages) + проверка + Telegram.

  python build_site.py          — проверить акции, отправить сигналы, собрать сайт в папку site/
  python build_site.py --no-tg  — без Telegram

Запускается по расписанию из .github/workflows/scan.yml
"""
from __future__ import annotations

import argparse
import json
import os
import logging
import shutil
import sys
from pathlib import Path

import pandas as pd

import bot
import data
import tg
import web

BASE = Path(__file__).resolve().parent
SITE = BASE / "site"
log = logging.getLogger("ob_bot.site")


def expected_last_bar(now: pd.Timestamp) -> pd.Timestamp:
    """Время открытия последней 4H свечи, которая уже закрылась (с запасом 10 минут на задержку Yahoo)."""
    day = now.normalize()
    for back in range(0, 10):
        d = day - pd.Timedelta(days=back)
        if d.weekday() >= 5:
            continue
        for h, m, dur in ((13, 30, 150), (9, 30, 240)):
            start = d + pd.Timedelta(hours=h, minutes=m)
            if start + pd.Timedelta(minutes=dur + 10) <= now:
                return start
    return day


def nothing_new() -> bool:
    """True, если последняя закрытая свеча уже обработана — тогда проверку можно пропустить."""
    try:
        state = json.loads(bot.STATE_FILE.read_text())
        done = max(pd.Timestamp(v) for v in state.values())
    except Exception:
        return False
    want = expected_last_bar(pd.Timestamp.now(tz=data.ET))
    log.info("последняя закрытая свеча: %s, обработано до: %s", want, done)
    return done >= want


def build(s: dict, telegram: bool) -> None:
    # подписчиков и их команды ведёт отдельная задача (telegram.yml); здесь только читаем
    tgs = tg.load()
    n_subs = len(tgs["subs"])
    s["only_new"] = False                      # собираем все сигналы, режим выбирает каждый подписчик
    live = bool(telegram and s["token"] and n_subs)

    def deliver(found):
        msg_all = bot.build_message(found)
        msg_new = bot.build_message(bot.only_new(found))
        if live:
            k = tg.broadcast(s["token"], tgs, msg_all, msg_new)
            log.info("разослано подписчикам: %d из %d", k, n_subs)
        else:
            print(msg_all)

    n = bot.run_once(dict(s), dry=True, deliver=deliver)
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
        last_signals=n, telegram=live, subscribers=n_subs)), encoding="utf-8")
    log.info("сайт собран: %d графиков", ok)


def main():
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    p = argparse.ArgumentParser()
    p.add_argument("--no-tg", action="store_true")
    p.add_argument("--if-new", action="store_true", help="пропустить, если новой свечи ещё нет")
    a = p.parse_args()
    if a.if_new and nothing_new():
        log.info("новой закрытой свечи нет — пропускаю")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write("skip=true\n")
        return
    build(bot.settings(), telegram=not a.no_tg)


if __name__ == "__main__":
    main()
