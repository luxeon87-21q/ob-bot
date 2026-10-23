"""
Telegram-бот: подписчики, их команды и рассылка сигналов.

Бот открыт для всех. Команды (в личку боту):
  /start     — подписаться на сигналы
  /stop      — отписаться
  /only_new  — присылать только новые ордер-блоки (по умолчанию)
  /all       — присылать все сигналы (новые OB + касания)
  /settings  — показать текущий режим
  /wolfe     — включить/выключить волны Вульфа (отдельная функция)
  /ob        — включить/выключить ордер-блоки
  /season    — включить/выключить пятничный отчёт по сезонности (seasonal.py)
  /hammer    — включить/выключить субботний отчёт «хаммер + бабочка» (hammer.py)
  /samurai   — включить/выключить сканер продажи путов «Самурай» (samurai.py, 2 раза в день)
  /ma200     — включить/выключить сканер «MA200: ложный пробой и отскок» (ma200.py, по будням)

Подписчики хранятся в cache/tg_settings.json:
  {"offset": 123, "owner": "<chat_id владельца>",
   "subs": {"<chat_id>": {"only_new": true, "ob": true, "wolfe": true, "name": "Denis", "since": "2026-09-25"}}}

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
    {"command": "only_new", "description": "Ордер-блоки: только новые"},
    {"command": "all", "description": "Ордер-блоки: новые OB и касания"},
    {"command": "wolfe", "description": "Волны Вульфа: вкл / выкл"},
    {"command": "ob", "description": "Ордер-блоки: вкл / выкл"},
    {"command": "season", "description": "Сезонность (пн и пт): вкл / выкл"},
    {"command": "hammer", "description": "Хаммер + бабочка (сб): вкл / выкл"},
    {"command": "samurai", "description": "Самурай — продажа путов (2 раза в день): вкл / выкл"},
    {"command": "ma200", "description": "MA200 — ложный пробой и отскок (по будням): вкл / выкл"},
    {"command": "settings", "description": "Текущие настройки"},
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
    if not sub.get("ob", True):
        ob = "📦 Ордер-блоки: <b>выключены</b> (включить — /ob)"
    elif sub["only_new"]:
        ob = "📦 Ордер-блоки: <b>только новые</b>"
    else:
        ob = "📦 Ордер-блоки: <b>все сигналы</b> — новые и касания"
    wv = ("🌊 Волны Вульфа: <b>включены</b>" if sub.get("wolfe", True)
          else "🌊 Волны Вульфа: <b>выключены</b> (включить — /wolfe)")
    se = ("📅 Сезонность (пн и пт): <b>включена</b>" if sub.get("season", True)
          else "📅 Сезонность: <b>выключена</b> (включить — /season)")
    hm = ("🔨 Хаммер + бабочка (сб): <b>включён</b>" if sub.get("hammer", True)
          else "🔨 Хаммер + бабочка: <b>выключен</b> (включить — /hammer)")
    sm = ("🗡 Самурай — продажа путов (2 раза в день): <b>включён</b>" if sub.get("samurai", True)
          else "🗡 Самурай: <b>выключен</b> (включить — /samurai)")
    ma = ("📈 MA200 — ложный пробой и отскок (по будням): <b>включён</b>" if sub.get("ma200", True)
          else "📈 MA200: <b>выключен</b> (включить — /ma200)")
    return ob + "\n" + wv + "\n" + se + "\n" + hm + "\n" + sm + "\n" + ma


HELP = ("\n\nКоманды:\n/only_new — ордер-блоки: только новые\n/all — ордер-блоки: новые + касания\n"
        "/ob — ордер-блоки вкл/выкл\n/wolfe — волны Вульфа вкл/выкл\n"
        "/season — сезонность вкл/выкл\n/hammer — хаммер + бабочка вкл/выкл\n"
        "/samurai — самурай (продажа путов) вкл/выкл\n/ma200 — MA200 (ложный пробой и отскок) вкл/выкл\n"
        "/settings — текущие настройки\n/stop — отписаться\n\n🌐 Сайт: " + SITE_URL)

WELCOME = ("✅ <b>Вы подписаны на сигналы ордер-блоков 4H</b>\n"
           "Акции S&amp;P 500 и Nasdaq-100: ордер-блоки (LuxAlgo Smart Money Concepts) "
           "и волны Вульфа — приходят отдельными сообщениями. "
           "Сигналы приходят после закрытия 4H свечей: ≈20:40 и 23:10 по Кишинёву, пн–пт.\n"
           "По понедельникам и пятницам — отчёт по сезонности: акции, которые в ближайшие недели "
           "из года в год делают ход ≥10%.\n\n"
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
        elif cmd in ("/only_new", "/onlynew", "/new", "/all", "/все", "/settings", "/status",
                     "/wolfe", "/вульф", "/ob", "/season", "/сезон", "/hammer", "/хаммер", "/samurai", "/самурай", "/ma200"):
            if not sub:
                reply = "Сначала подпишитесь — /start"
            elif cmd in ("/settings", "/status"):
                reply = mode_text(sub) + HELP
            elif cmd in ("/wolfe", "/вульф"):
                sub["wolfe"] = not sub.get("wolfe", True)
                reply = "✅ Готово.\n" + mode_text(sub)
            elif cmd in ("/season", "/сезон"):
                sub["season"] = not sub.get("season", True)
                reply = "✅ Готово.\n" + mode_text(sub)
            elif cmd in ("/samurai", "/самурай"):
                sub["samurai"] = not sub.get("samurai", True)
                reply = "✅ Готово.\n" + mode_text(sub)
            elif cmd == "/ma200":
                sub["ma200"] = not sub.get("ma200", True)
                reply = "✅ Готово.\n" + mode_text(sub)
            elif cmd in ("/hammer", "/хаммер"):
                sub["hammer"] = not sub.get("hammer", True)
                reply = "✅ Готово.\n" + mode_text(sub)
            elif cmd == "/ob":
                sub["ob"] = not sub.get("ob", True)
                reply = "✅ Готово.\n" + mode_text(sub)
            else:
                sub["only_new"] = cmd not in ("/all", "/все")
                sub["ob"] = True
                reply = "✅ Готово.\n" + mode_text(sub)
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
    old_format = not SETTINGS_FILE.exists() or '"subs"' not in SETTINGS_FILE.read_text(encoding="utf-8")
    if ups or old_format:
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


def broadcast(token: str, st: dict, msg_all: str, msg_new: str, msg_wolfe: str = "") -> int:
    """Рассылает сигналы: ордер-блоки (only_new — msg_new, иначе msg_all) и отдельным
    сообщением волны Вульфа (кто их не выключил). Возвращает число подписчиков, кому всё дошло."""
    sent = 0
    for cid, sub in list(st["subs"].items()):
        texts = []
        if sub.get("ob", True):
            texts.append(msg_new if sub.get("only_new", True) else msg_all)
        if sub.get("wolfe", True):
            texts.append(msg_wolfe)
        parts = [p for t in texts if t for p in chunks(t)]
        if parts and send_parts(token, cid, sub, parts):
            sent += 1
    return sent


def broadcast_one(token: str, st: dict, text: str, key: str) -> int:
    """Одно сообщение всем подписчикам, у кого включена функция key (по умолчанию включена)."""
    sent = 0
    parts = chunks(text) if text else []
    for cid, sub in list(st["subs"].items()):
        if parts and sub.get(key, True) and send_parts(token, cid, sub, parts):
            sent += 1
    return sent


def send_parts(token: str, cid: str, sub: dict, parts) -> bool:
    ok = True
    for part in parts:
        delivered = False
        for attempt in range(3):
            try:
                r = api(token, "sendMessage", chat_id=cid, text=part, parse_mode="HTML",
                        disable_web_page_preview=True)
                delivered = True
                log.info("→ %s (%s): сообщение %s", cid, sub.get("name", ""), r.get("message_id"))
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
        ok = ok and delivered
        time.sleep(0.05)          # лимит Telegram ~30 сообщений/сек
    return ok


def set_commands(token: str) -> None:
    try:
        api(token, "setMyCommands", commands=COMMANDS)
        api(token, "setMyDescription", description=(
            "Сигналы ордер-блоков 4H по акциям США (S&P 500 и Nasdaq-100) по индикатору "
            "LuxAlgo Smart Money Concepts. Нажмите «Запустить», чтобы подписаться."))
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
