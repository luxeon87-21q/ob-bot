"""
Telegram-бот: подписчики, их команды и рассылка сигналов.

Бот открыт для всех. Команды (в личку боту):
  /start     — подписаться на сигналы
  /stop      — отписаться
  /only_new  — присылать только новые ордер-блоки (по умолчанию)
  /all       — присылать все сигналы (новые OB + касания)
  /settings  — показать текущий режим

Подписчики хранятся в cache/tg_settings.json:
  {"offset": 123, "owner": "<chat_id владельца>",
   "subs": {"<chat_id>": {"only_new": true, "name": "Denis", "since": "2026-09-25"}}}

Файл пишет только задача telegram.yml (раз в 10 минут); проверка акций его лишь читает.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date
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
    {"command": "stop", "description": "Отписаться от сигналов"},
]


# ---------------------------------------------------------------- хранение
def load() -> dict:
    try:
        st = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("offset", 0)
    st.setdefault("subs", {})
    # миграция со старого формата (один чат: chat_id + only_new) — только один раз
    old = st.pop("chat_id", None)
    if not old and "owner" not in st and LEGACY_CHAT_FILE.exists():
        old = LEGACY_CHAT_FILE.read_text().strip()
    if old and "owner" not in st:
        old = str(old)
        st["owner"] = old
        st["subs"].setdefault(old, {"only_new": bool(st.get("only_new", True)), "name": "",
                                    "since": str(date.today())})
    st.pop("only_new", None)
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


# ---------------------------------------------------------------- тексты
def mode_text(sub: dict) -> str:
    return ("🆕 Режим: <b>только новые ордер-блоки</b>" if sub["only_new"]
            else "📊 Режим: <b>все сигналы</b> — новые ордер-блоки и касания")


HELP = ("\n\nКоманды:\n/only_new — только новые ордер-блоки\n/all — все сигналы (новые OB + касания)\n"
        "/settings — текущий режим\n/stop — отписаться\n\n🌐 Сайт: " + SITE_URL)

WELCOME = ("✅ <b>Вы подписаны на сигналы ордер-блоков 4H</b>\n"
           "Акции S&amp;P 500 и Nasdaq-100 (индикатор LuxAlgo Order Block Detector). "
           "Сигналы приходят после закрытия 4H свечей: ≈20:40 и 23:10 по Кишинёву, пн–пт.\n\n"
           "⚠️ Это не инвестиционная рекомендация.\n\n")


# ---------------------------------------------------------------- команды
def process_updates(token: str) -> dict:
    """Читает новые сообщения боту и выполняет команды подписчиков. Возвращает настройки."""
    st = load()
    if not token:
        return st
    try:
        ups = api(token, "getUpdates", offset=st["offset"] + 1 if st["offset"] else None, timeout=0,
                  allowed_updates=["message", "my_chat_member"])
    except Exception as e:
        log.warning("getUpdates: %s", e)
        return st
    subs = st["subs"]
    for u in ups:
        st["offset"] = max(st["offset"], u["update_id"])

        # пользователь заблокировал бота — убираем из рассылки
        mcm = u.get("my_chat_member")
        if mcm:
            cid = str(mcm.get("chat", {}).get("id"))
            if mcm.get("new_chat_member", {}).get("status") in ("kicked", "left"):
                subs.pop(cid, None)
            continue

        m = u.get("message") or {}
        ch = m.get("chat") or {}
        if ch.get("type") != "private":
            continue
        cid = str(ch["id"])
        words = (m.get("text") or "").strip().split()
        cmd = words[0].split("@")[0].lower() if words else ""
        sub = subs.get(cid)

        if cmd == "/start":
            if not sub:
                sub = subs[cid] = {"only_new": True, "name": ch.get("first_name", ""),
                                   "since": str(date.today())}
                st.setdefault("owner", cid)
            reply = WELCOME + mode_text(sub) + HELP
        elif cmd == "/stop":
            if sub:
                subs.pop(cid, None)
                reply = "🔕 Вы отписаны. Чтобы снова получать сигналы — /start"
            else:
                reply = "Вы и так не подписаны. Подписаться — /start"
        elif cmd in ("/only_new", "/onlynew", "/new", "/all", "/все", "/settings", "/status"):
            if not sub:
                reply = "Сначала подпишитесь — /start"
            elif cmd in ("/settings", "/status"):
                reply = mode_text(sub) + HELP
            else:
                sub["only_new"] = cmd not in ("/all", "/все")
                reply = "✅ Готово. " + mode_text(sub)
        elif not sub:
            reply = "Чтобы получать сигналы ордер-блоков — /start"
        else:
            reply = None                                     # обычный текст подписчика — молчим
        if reply:
            try:
                api(token, "sendMessage", chat_id=cid, text=reply, parse_mode="HTML",
                    disable_web_page_preview=True)
            except Exception as e:
                log.warning("sendMessage %s: %s", cid, e)
    if ups or not SETTINGS_FILE.exists():
        save(st)
    return st


# ---------------------------------------------------------------- рассылка
def chunks(text: str, limit: int = 3900):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def broadcast(token: str, st: dict, msg_all: str, msg_new: str) -> int:
    """Рассылает сигналы: подписчикам в режиме only_new — msg_new, остальным — msg_all."""
    sent = 0
    for cid, sub in list(st["subs"].items()):
        text = msg_new if sub.get("only_new", True) else msg_all
        if not text:
            continue
        for part in chunks(text):
            for attempt in range(3):
                try:
                    api(token, "sendMessage", chat_id=cid, text=part, parse_mode="HTML",
                        disable_web_page_preview=True)
                    break
                except Exception as e:
                    msg = str(e)
                    if "blocked" in msg or "deactivated" in msg or "chat not found" in msg:
                        log.info("подписчик %s недоступен: %s", cid, msg)
                        break
                    if "Too Many Requests" in msg:
                        time.sleep(5 * (attempt + 1))
                    else:
                        log.warning("sendMessage %s: %s", cid, msg)
                        time.sleep(1)
            time.sleep(0.05)          # лимит Telegram ~30 сообщений/сек
        sent += 1
    return sent


def set_commands(token: str) -> None:
    try:
        api(token, "setMyCommands", commands=COMMANDS)
        api(token, "setMyDescription", description=(
            "Сигналы ордер-блоков 4H по акциям США (S&P 500 и Nasdaq-100) по индикатору "
            "LuxAlgo Order Block Detector. Нажмите «Запустить», чтобы подписаться."))
        api(token, "setMyShortDescription",
            short_description="Сигналы ордер-блоков 4H по акциям США")
    except Exception as e:
        log.warning("setMyCommands: %s", e)


if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    tok = os.environ.get("TELEGRAM_TOKEN", "")
    set_commands(tok)
    s = process_updates(tok)
    log.info("подписчиков: %d, offset=%s", len(s["subs"]), s["offset"])
