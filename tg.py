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
  /ma200     — включить/выключить сканер «своей скользящей» (ma200.py, по будням)
  /market    — включить/выключить панель рынка: широта, термометр, компоненты SPX у средних (market.py, по будням)
  /earnings  — отчёты, которые играются сегодняшними опционами: заложенный ход против истории (earnings.py)
  /fund TICKER — карточка компании: выручка, маржа, EPS по кварталам (fundamentals.py)
  /insiders [TICKER] — сделки инсайдеров и конгрессменов (insiders.py); без тикера — последние покупки
  /insiders_daily — включить/выключить рассылку о новых покупках инсайдеров и сделках Конгресса
  /earnings_daily — включить/выключить рассылку по отчётам (по будням после открытия)

Подписчики хранятся в cache/tg_settings.enc — зашифрованно (Fernet, ключ в переменной
TG_STATE_KEY = секрет GitHub). Внутри тот же JSON:
  {"offset": 123, "owner": "<chat_id владельца>",
   "subs": {"<chat_id>": {"only_new": true, "ob": true, "wolfe": true, "name": "Denis", "since": "2026-09-25"}}}
Репозиторий публичный, поэтому открытый cache/tg_settings.json в git больше не попадает
(.gitignore). Без ключа (локальный запуск) используется открытый файл; в GitHub Actions
без ключа сохранение запрещено, чтобы не потерять подписчиков.

Файл пишет только задача telegram.yml (раз в 10 минут); проверка акций его лишь читает.
"""
from __future__ import annotations

import json
import re
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
SETTINGS_FILE = BASE / "cache" / "tg_settings.json"      # открытый: только локально / для миграции
ENC_FILE = BASE / "cache" / "tg_settings.enc"           # зашифрованный: хранится в git
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
    {"command": "ma200", "description": "Своя скользящая — подход к средней (по будням): вкл / выкл"},
    {"command": "market", "description": "Панель рынка — широта и компоненты SPX (по будням): вкл / выкл"},
    {"command": "earnings", "description": "Отчёты сегодня: заложенный ход против истории"},
    {"command": "earnings_daily", "description": "Отчёты — рассылка после открытия (по будням): вкл / выкл"},
    {"command": "fund", "description": "Компания: выручка, маржа, EPS — /fund AMD"},
    {"command": "insiders", "description": "Инсайдеры и Конгресс — /insiders или /insiders AMD"},
    {"command": "insiders_daily", "description": "Инсайдеры и Конгресс — рассылка (по будням): вкл / выкл"},
    {"command": "settings", "description": "Текущие настройки"},
    {"command": "stop", "description": "Отписаться от сигналов"},
]


# ---------------------------------------------------------------- хранение
def _fernet():
    key = os.environ.get("TG_STATE_KEY", "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def _read_raw() -> str | None:
    """Текст настроек: из зашифрованного файла (если есть ключ), иначе из открытого."""
    if ENC_FILE.exists():
        f = _fernet()
        if f is None:
            if SETTINGS_FILE.exists():
                return SETTINGS_FILE.read_text(encoding="utf-8")
            # без ключа зашифрованное не прочитать; пустой список подписчиков молча не возвращаем
            raise RuntimeError("cache/tg_settings.enc есть, но TG_STATE_KEY не задан")
        return f.decrypt(ENC_FILE.read_bytes().strip()).decode("utf-8")
    if SETTINGS_FILE.exists():
        return SETTINGS_FILE.read_text(encoding="utf-8")
    return None


def needs_migration() -> bool:
    """Открытый файл ещё лежит, а ключ уже есть — пора зашифровать и убрать открытый."""
    return _fernet() is not None and (SETTINGS_FILE.exists() or LEGACY_CHAT_FILE.exists())


def load() -> dict:
    raw = _read_raw()
    try:
        st = json.loads(raw) if raw else {}
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
    data = json.dumps(st, ensure_ascii=False, indent=1)
    f = _fernet()
    if f is None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise RuntimeError("TG_STATE_KEY не задан в секретах GitHub — настройки не сохранены")
        SETTINGS_FILE.write_text(data, encoding="utf-8")   # локальный запуск без ключа
        return
    tmp = ENC_FILE.with_suffix(".tmp")
    tmp.write_bytes(f.encrypt(data.encode("utf-8")) + b"\n")
    tmp.replace(ENC_FILE)
    for p in (SETTINGS_FILE, LEGACY_CHAT_FILE):     # открытые копии больше не нужны
        p.unlink(missing_ok=True)


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
    ma = ("📈 Своя скользящая — подход цены к средней (по будням): <b>включена</b>" if sub.get("ma200", True)
          else "📈 Своя скользящая: <b>выключена</b> (включить — /ma200)")
    mk = ("🧭 Панель рынка — широта, термометр, компоненты SPX у средних (по будням): <b>включена</b>" if sub.get("market", True)
          else "🧭 Панель рынка: <b>выключена</b> (включить — /market)")
    er = ("📊 Отчёты — рассылка после открытия (по будням): <b>включена</b>" if sub.get("earnings", True)
          else "📊 Отчёты: <b>выключены</b> (включить — /earnings_daily)")
    ins = ("🕵️ Инсайдеры и Конгресс — новые покупки и сделки (по будням): <b>включены</b>" if sub.get("insiders", True)
           else "🕵️ Инсайдеры и Конгресс: <b>выключены</b> (включить — /insiders_daily)")
    return ob + "\n" + wv + "\n" + se + "\n" + hm + "\n" + sm + "\n" + ma + "\n" + mk + "\n" + er + "\n" + ins


HELP = ("\n\nКоманды:\n/only_new — ордер-блоки: только новые\n/all — ордер-блоки: новые + касания\n"
        "/ob — ордер-блоки вкл/выкл\n/wolfe — волны Вульфа вкл/выкл\n"
        "/season — сезонность вкл/выкл\n/hammer — хаммер + бабочка вкл/выкл\n"
        "/samurai — самурай (продажа путов) вкл/выкл\n/ma200 — своя скользящая вкл/выкл\n/market — панель рынка вкл/выкл\n"
        "/earnings — отчёты сегодня: заложенный ход против истории\n/earnings_daily — рассылка по отчётам вкл/выкл\n"
        "/fund AMD — компания: выручка, маржа, EPS\n/insiders — инсайдеры и Конгресс (/insiders AMD — по акции)\n"
        "/insiders_daily — рассылка по инсайдерам вкл/выкл\n"
        "/settings — текущие настройки\n/stop — отписаться\n\n🌐 Сайт: " + SITE_URL)

WELCOME = ("✅ <b>Вы подписаны на сигналы ордер-блоков 4H</b>\n"
           "Акции S&amp;P 500 и Nasdaq-100: ордер-блоки (LuxAlgo Smart Money Concepts) "
           "и волны Вульфа — приходят отдельными сообщениями. "
           "Сигналы приходят после закрытия 4H свечей: ≈20:40 и 23:10 по Кишинёву, пн–пт.\n"
           "По понедельникам и пятницам — отчёт по сезонности: акции, которые в ближайшие недели "
           "из года в год делают ход ≥10%.\n\n"
           "⚠️ Это не инвестиционная рекомендация.\n\n")


def earnings_text() -> str:
    """Готовый текст из последнего прогона earnings.py (без pandas — эта задача ставит только requests)."""
    try:
        res = json.loads((BASE / "cache" / "earnings.json").read_text(encoding="utf-8"))
    except Exception:
        return "📊 Данных по отчётам ещё нет — сканер считает по будням после открытия рынка.\n🌐 " + SITE_URL + "#earnings"
    msg = res.get("msg") or ("📊 Сегодня после закрытия и завтра до открытия крупных отчётов нет.\n"
                             "Вся неделя: " + SITE_URL + "#earnings")
    upd = str(res.get("updated", ""))[:16].replace("T", " ")
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        if res.get("today") != datetime.now(ZoneInfo("America/New_York")).date().isoformat():
            msg = "⚠️ <b>Сегодня сканер ещё не считал — это список прошлого прогона.</b>\n\n" + msg
    except Exception:
        pass
    return msg + (f"\n\n🕒 Данные на {upd} ET" if upd else "")


def _big(v) -> str:
    if v is None:
        return "—"
    a, s = abs(v), "−" if v < 0 else ""
    return s + (f"${a / 1e9:.2f} млрд" if a >= 1e9 else f"${a / 1e6:.1f} млн" if a >= 1e6 else f"${a / 1e3:.0f} тыс." if a >= 1e3 else f"${a:.0f}")


def _pc(x) -> str:
    return "—" if x is None else f"{x:+.1f}%".replace(".", ",")


def _cache(name: str):
    try:
        return json.loads((BASE / "cache" / name).read_text(encoding="utf-8"))
    except Exception:
        return None


def fund_text(t: str) -> str:
    """Карточка компании из cache/fund.json (без pandas)."""
    d = _cache("fund.json")
    f = (d or {}).get("tickers", {}).get(t)
    link = f"{SITE_URL}#fund/{t}"
    if not f or not f.get("q"):
        return (f"По {t} нет данных: тикера нет в списках сайта или компания не отчитывается в SEC по US GAAP."
                if d else "Данных ещё нет — сканер считает по будням после закрытия.")
    q, ttm = f["q"], f.get("ttm") or {}
    c = q[0]
    lines = [f"🏢 <b>{t}</b> — {f.get('name') or ''}",
             f"Квартал до {c['e'][8:10]}.{c['e'][5:7]}.{c['e'][:4]} ({c.get('fp') or ''}{', вычислен' if c.get('d') else ''}):",
             f"• Выручка {_big(c.get('rev'))} ({_pc(c.get('rev_yoy'))} к году назад)",
             f"• Чистая прибыль {_big(c.get('ni'))} ({_pc(c.get('ni_yoy'))}), EPS {c.get('eps') if c.get('eps') is not None else '—'} ({_pc(c.get('eps_yoy'))})",
             "• Маржа: валовая " + (f"{c['gm']:.1f}%" if c.get("gm") is not None else "—")
             + ", операционная " + (f"{c['om']:.1f}%" if c.get("om") is not None else "—")
             + ", чистая " + (f"{c['nm']:.1f}%" if c.get("nm") is not None else "—"),
             f"• За 4 квартала: выручка {_big(ttm.get('rev'))}, EPS {ttm.get('eps', '—')}"]
    hist = [f"{r['e'][5:7]}.{r['e'][2:4]}: {_big(r.get('rev'))} {_pc(r.get('rev_yoy'))}" for r in q[1:5]]
    if hist:
        lines.append("Выручка раньше: " + " · ".join(hist))
    fl = f.get("flags") or []
    if "one_off" in fl:
        lines.append("⚠️ Чистая прибыль больше операционной — внутри разовые доходы.")
    if "lag" in fl:
        lines.append("⏳ Свежий 10-Q уже подан, а чисел в базе SEC ещё нет — откройте отчёт.")
    docs = f.get("docs") or {}
    if docs.get("report"):
        lines.append(f'📄 <a href="{docs["report"]["url"]}">{docs["report"]["form"]}</a>'
                     + (f' · <a href="{docs["press"]["url"]}">пресс-релиз {docs["press"]["filed"][8:10]}.{docs["press"]["filed"][5:7]}</a>' if docs.get("press") else ""))
    ins = [r for r in (_cache("insiders.json") or {}).get("f4", []) if r.get("t") == t]
    if ins:
        b = [r for r in ins if r["code"] == "P"]
        s = [r for r in ins if r["code"] == "S"]
        lines.append(f"🕵️ Инсайдеры за 120 дн.: покупок {len(b)} на {_big(sum(r.get('val') or 0 for r in b))}, "
                     f"продаж {len(s)} на {_big(sum(r.get('val') or 0 for r in s))} (без плана 10b5-1: {sum(1 for r in s if not r.get('plan'))}) — /insiders {t}")
    lines.append(f"🌐 {link}")
    return "\n".join(lines)


def insiders_text(t: str = "") -> str:
    d = _cache("insiders.json")
    if not d:
        return "Данных ещё нет — сканер считает по будням после закрытия."
    cl = d.get("clusters") or {}
    f4 = [r for r in d.get("f4", []) if not t or r.get("t") == t]
    cg = [dict(x, who=h["name"], district=h.get("district", ""), filed=h["filed"]) for h in d.get("house", [])
          for x in h.get("rows", []) if x.get("t") and (not t or x["t"] == t)]
    head = f"🕵️ <b>Инсайдеры{' — ' + t if t else ': последние покупки'}</b>"
    if not t:
        f4 = [r for r in f4 if r["code"] == "P"]
    lines = [head]
    for r in f4[:15]:
        act = "🟢 купил" if r["code"] == "P" else "🔴 продал"
        plan = "" if r["code"] == "P" else (" (план 10b5-1)" if r.get("plan") else " (<b>без плана</b>)")
        c = f" · 👥 кластер {cl[r['t']]}" if r["code"] == "P" and r.get("t") in cl else ""
        lines.append(f"{r['filed'][8:10]}.{r['filed'][5:7]} <b>{r['t']}</b>: {r.get('who') or '?'} ({r.get('role') or '—'}) "
                     f"{act} {_big(r.get('val'))}{plan}{c}")
    if len(lines) == 1:
        lines.append("Сделок на открытом рынке за 120 дней нет.")
    if cg:
        lines.append("\n🏛 <b>Конгресс</b>")
        for x in cg[:10]:
            act = {"P": "купил(а)", "S": "продал(а)", "E": "обмен"}.get(x["type"], x["type"])
            amt = _big(x.get("lo")) + ("–" + _big(x["hi"]) if x.get("hi") else "+")
            lines.append(f"{x['filed'][8:10]}.{x['filed'][5:7]} <b>{x['t']}</b>: {x['who']} ({x['district']}, {x.get('owner', '')}) "
                         f"{act} {amt}, раскрыто через {x.get('days', '?')} дн.")
    lines.append(f"\n🌐 {SITE_URL}#{'fund/' + t if t else 'insiders'}")
    return "\n".join(lines)


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

        arg = re.sub(r"[^A-Z0-9-]", "", words[1].upper().replace(".", "-"))[:10] if len(words) > 1 else ""
        if cmd in ("/fund", "/компания"):
            reply = fund_text(arg) if arg else "Напишите тикер: <code>/fund AMD</code>"
        elif cmd in ("/insiders", "/инсайдеры"):
            reply = insiders_text(arg)
        elif cmd == "/start":
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
                     "/wolfe", "/вульф", "/ob", "/season", "/сезон", "/hammer", "/хаммер", "/samurai", "/самурай", "/ma200",
                     "/market", "/рынок", "/earnings", "/отчёты", "/отчеты", "/earnings_daily", "/insiders_daily"):
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
            elif cmd in ("/earnings", "/отчёты", "/отчеты"):
                reply = earnings_text()
            elif cmd == "/insiders_daily":
                sub["insiders"] = not sub.get("insiders", True)
                reply = "✅ Готово.\n" + mode_text(sub)
            elif cmd == "/earnings_daily":
                sub["earnings"] = not sub.get("earnings", True)
                reply = "✅ Готово.\n" + mode_text(sub)
            elif cmd in ("/market", "/рынок"):
                sub["market"] = not sub.get("market", True)
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
            for part in chunks(reply):                       # длинный ответ (/earnings) — несколькими сообщениями
                try:
                    api(token, "sendMessage", chat_id=cid, text=part, parse_mode="HTML",
                        disable_web_page_preview=True)
                except Exception as e:
                    log.warning("sendMessage %s: %s", cid, e)
    if ENC_FILE.exists() and _fernet() is not None:
        old_format = False
    else:
        old_format = not SETTINGS_FILE.exists() or '"subs"' not in SETTINGS_FILE.read_text(encoding="utf-8")
    if ups or old_format or needs_migration():
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
