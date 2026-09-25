"""
Команды Telegram-бота и его настройки.

Команды (пишутся боту в личку):
  /only_new  — присылать только новые ордер-блоки
  /all       — присылать все сигналы (новые OB + касания)
  /settings  — показать текущий режим
  /start     — подключиться и получить справку

Настройки хранятся в cache/tg_settings.json:
  {"chat_id": "...", "only_new": true, "offset": 123}
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
SETTINGS_FILE = BASE / "cache" / "tg_settings.json"
LEGACY_CHAT_FILE = BASE / "cache" / "chat_id"
SITE_URL = "https://luxeon87-21q.github.io/ob-bot/"
log = logging.getLogger("ob_bot.tg")

COMMANDS = [
    {"command": "only_new", "description": "Только новые ордер-блоки"},
    {"command": "all", "description": "Все сигналы: новые OB и касания"},
    {"command": "settings", "description": "Текущий режим"},
]


def load() -> dict:
    try:
        st = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("only_new", True)
    st.setdefault("offset", 0)
    if not st.get("chat_id") and LEGACY_CHAT_FILE.exists():
        st["chat_id"] = LEGACY_CHAT_FILE.read_text().strip()
    return st


def save(st: dict) -> None:
    SETTINGS_FILE.parent.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def api(token: str, method: str, **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram {method}: {j.get('description')}")
    return j["result"]


def mode_text(st: dict) -> str:
    return ("🆕 Режим: <b>только новые ордер-блоки</b>" if st["only_new"]
            else "📊 Режим: <b>все сигналы</b> — новые ордер-блоки и касания")


HELP = ("\n\nКоманды:\n/only_new — только новые ордер-блоки\n/all — все сигналы (новые OB + касания)\n"
        "/settings — текущий режим\n\n🌐 Сайт: " + SITE_URL)


def process_updates(token: str) -> dict:
    """Читает новые сообщения боту: запоминает чат, выполняет команды. Возвращает настройки."""
    st = load()
    if not token:
        return st
    try:
        ups = api(token, "getUpdates", offset=st["offset"] + 1 if st["offset"] else None, timeout=0)
    except Exception as e:
        log.warning("getUpdates: %s", e)
        return st
    changed = False
    for u in ups:
        st["offset"] = max(st["offset"], u["update_id"])
        changed = True
        m = u.get("message") or {}
        ch = m.get("chat") or {}
        if ch.get("type") != "private":
            continue
        cid = str(ch["id"])
        text = (m.get("text") or "").strip().split("@")[0].lower()
        first = not st.get("chat_id")
        st["chat_id"] = cid
        if text in ("/only_new", "/onlynew", "/new"):
            st["only_new"] = True
            reply = "✅ Готово. " + mode_text(st)
        elif text in ("/all", "/все"):
            st["only_new"] = False
            reply = "✅ Готово. " + mode_text(st)
        elif text in ("/settings", "/status"):
            reply = mode_text(st) + HELP
        elif text == "/start" or first:
            reply = ("✅ <b>Бот ордер-блоков 4H подключён.</b>\nСигналы по акциям S&amp;P 500 и Nasdaq-100 "
                     "приходят после закрытия 4H свечей (≈20:40 и 23:10 по Кишинёву, пн–пт).\n\n"
                     + mode_text(st) + HELP)
        else:
            reply = None                     # обычный текст — не отвечаем
        if not reply:
            continue
        try:
            api(token, "sendMessage", chat_id=cid, text=reply, parse_mode="HTML",
                disable_web_page_preview=True)
        except Exception as e:
            log.warning("sendMessage: %s", e)
    if changed or not SETTINGS_FILE.exists():
        save(st)
    return st


def set_commands(token: str) -> None:
    try:
        api(token, "setMyCommands", commands=COMMANDS)
    except Exception as e:
        log.warning("setMyCommands: %s", e)


if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    tok = os.environ.get("TELEGRAM_TOKEN", "")
    set_commands(tok)
    s = process_updates(tok)
    log.info("chat=%s only_new=%s offset=%s", bool(s.get("chat_id")), s["only_new"], s["offset"])
