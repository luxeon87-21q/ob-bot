"""
Бот: следит за акциями США на 4H и пишет в Telegram, когда
  • сформировался новый ордер-блок (LuxAlgo Order Block Detector)
  • цена коснулась (зашла в зону) действующего ордер-блока
  • ордер-блок пробит (mitigated) — по желанию

Запуск:
  python bot.py              — работает постоянно, проверяет после закрытия каждой 4H свечи
  python bot.py --once       — один прогон сейчас
  python bot.py --once --dry — один прогон, вывод в консоль вместо Telegram
  python bot.py --status AAPL — показать действующие ордер-блоки по тикеру
  python bot.py --chat-id    — узнать свой chat_id (сначала напишите боту /start)
  python bot.py --test       — отправить тестовое сообщение
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests

import data
from order_blocks import Event, detect

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "cache" / "state.json"
log = logging.getLogger("ob_bot")


# ---------------------------------------------------------------- настройки
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #")[0].split("\t#")[0]
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg(name, default=None, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.lower() in ("1", "true", "yes", "да", "on")
    return cast(v)


def settings():
    return dict(
        token=cfg("TELEGRAM_TOKEN", ""),
        chat_id=cfg("TELEGRAM_CHAT_ID", ""),
        universe=cfg("UNIVERSE", "sp500,nasdaq100"),
        length=cfg("OB_LENGTH", 5, int),
        mitigation=cfg("OB_MITIGATION", "Wick"),
        track_last=cfg("OB_TRACK_LAST", 3, int),
        notify_formed=cfg("NOTIFY_FORMED", True, bool),
        notify_touched=cfg("NOTIFY_TOUCHED", True, bool),
        notify_mitigated=cfg("NOTIFY_MITIGATED", False, bool),
        delay_min=cfg("CHECK_DELAY_MIN", 10, int),
    )


# ---------------------------------------------------------------- telegram
def tg(method: str, token: str, **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram {method}: {j}")
    return j["result"]


def send(text: str, s: dict, dry: bool = False) -> None:
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for ch in chunks:
        if dry or not s["token"] or not s["chat_id"]:
            print(ch)
            continue
        for attempt in range(3):
            try:
                tg("sendMessage", s["token"], chat_id=s["chat_id"], text=ch,
                   parse_mode="HTML", disable_web_page_preview=True)
                break
            except Exception as e:
                log.warning("Telegram: %s", e)
                time.sleep(3 * (attempt + 1))
        time.sleep(0.5)


# ---------------------------------------------------------------- сообщения
def fmt(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def link(t: str) -> str:
    return (f'<a href="https://www.tradingview.com/chart/?symbol={html.escape(t)}&amp;interval=240">'
            f'<b>{html.escape(t)}</b></a>')


SECTIONS = [
    ("formed", "bull", "🟢 Новый бычий ордер-блок"),
    ("formed", "bear", "🔴 Новый медвежий ордер-блок"),
    ("touched", "bull", "👇 Касание бычьего ордер-блока (поддержка)"),
    ("touched", "bear", "👆 Касание медвежьего ордер-блока (сопротивление)"),
    ("mitigated", "bull", "❌ Бычий ордер-блок пробит"),
    ("mitigated", "bear", "❌ Медвежий ордер-блок пробит"),
]


def build_message(found: Dict[str, List[Event]]) -> str:
    if not found:
        return ""
    bars = sorted({e.bar_time for evs in found.values() for e in evs})
    head = ", ".join(b.strftime("%d.%m %H:%M") for b in bars)
    lines = [f"📊 <b>Ордер-блоки 4H</b> — свеча {head} ET"]
    for etype, side, title in SECTIONS:
        rows = []
        for t in sorted(found):
            for e in found[t]:
                if e.type != etype or e.ob.side != side:
                    continue
                ob = e.ob
                zone = f"{fmt(ob.btm)}–{fmt(ob.top)}"
                if etype == "formed":
                    extra = f"свеча OB {ob.ob_time.strftime('%d.%m %H:%M')}, цена {fmt(e.close)}"
                elif etype == "touched":
                    wick = e.low if side == "bull" else e.high
                    dist = (e.close - ob.avg) / ob.avg * 100
                    extra = (f"{'мин' if side == 'bull' else 'макс'} {fmt(wick)}, "
                             f"закрытие {fmt(e.close)} ({dist:+.1f}% от середины)")
                else:
                    extra = f"закрытие {fmt(e.close)}"
                rows.append(f"• {link(t)}  {zone}  <i>{extra}</i>")
        if rows:
            lines += ["", f"<b>{title}</b>"] + rows
    return "\n".join(lines)


# ---------------------------------------------------------------- проверка
def load_state() -> Dict[str, str]:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(st: Dict[str, str]) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=0))


SNAPSHOT_FILE = BASE / "cache" / "snapshot.json"
HISTORY_FILE = BASE / "cache" / "signals.jsonl"
HISTORY_SEED_BARS = 30      # при первом запуске кладём в ленту сигналы за последние ~15 дней (без отправки)


def ob_dict(ob, close: float) -> dict:
    if ob.side == "bull":        # поддержка ниже цены
        dist = 0.0 if close <= ob.top else (close - ob.top) / close * 100
    else:                        # сопротивление выше цены
        dist = 0.0 if close >= ob.btm else (ob.btm - close) / close * 100
    return dict(side=ob.side, top=round(ob.top, 4), btm=round(ob.btm, 4),
                ob_time=ob.ob_time.isoformat(), detected=ob.detected_time.isoformat(),
                dist=round(dist, 3))


def event_dict(t: str, e: Event) -> dict:
    return dict(ticker=t, type=e.type, side=e.ob.side, bar_time=e.bar_time.isoformat(),
                top=round(e.ob.top, 4), btm=round(e.ob.btm, 4), ob_time=e.ob.ob_time.isoformat(),
                close=round(e.close, 4), high=round(e.high, 4), low=round(e.low, 4))


def run_once(s: dict, dry: bool = False, progress=None) -> int:
    tickers = data.load_universe(s["universe"], BASE / "tickers.txt")
    log.info("акций в списке: %d, загружаю свечи…", len(tickers))
    if progress:
        progress("загрузка свечей", 0, len(tickers))
    bars = data.update_bars(tickers)
    log.info("свечи получены по %d акциям", len(bars))
    if len(bars) < max(1, len(tickers) // 3):
        raise RuntimeError(f"Yahoo отдал данные только по {len(bars)} из {len(tickers)} акций — проверка отменена")

    wanted = {k for k, on in (("formed", s["notify_formed"]), ("touched", s["notify_touched"]),
                              ("mitigated", s["notify_mitigated"])) if on}
    state = load_state()
    found: Dict[str, List[Event]] = {}
    history: List[dict] = []
    snap_rows = []
    for n, (t, df) in enumerate(bars.items()):
        if progress and n % 25 == 0:
            progress("поиск ордер-блоков", n, len(bars))
        if len(df) < 3 * s["length"]:
            continue
        events, bulls, bears = detect(df, s["length"], s["mitigation"], s["track_last"])
        last_seen = state.get(t)
        if last_seen:
            cut = pd.Timestamp(last_seen)
            new = [e for e in events if e.bar_time > cut]
            history += [event_dict(t, e) for e in new]
        else:
            new = [e for e in events if e.bar_time == df.index[-1]]
            seed_from = df.index[max(0, len(df) - HISTORY_SEED_BARS)]
            history += [event_dict(t, e) for e in events if e.bar_time >= seed_from]
        new = [e for e in new if e.type in wanted]
        if new:
            found[t] = new
        state[t] = df.index[-1].isoformat()

        close = float(df["close"].iloc[-1])
        prev = float(df["close"].iloc[-3]) if len(df) > 2 else close   # ~1 торговый день назад
        k = s["track_last"] or None
        obs = [ob_dict(o, close) for o in bulls[:k]] + [ob_dict(o, close) for o in bears[:k]]
        snap_rows.append(dict(ticker=t, close=round(close, 4), chg=round((close / prev - 1) * 100, 2),
                              bar_time=df.index[-1].isoformat(), obs=obs))

    msg = build_message(found)
    if msg:
        send(msg, s, dry)
    else:
        log.info("новых сигналов нет")
    save_state(state)

    STATE_FILE.parent.mkdir(exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(dict(
        updated=pd.Timestamp.now(tz=data.ET).isoformat(), settings={k: v for k, v in s.items()
                                                                  if k not in ("token", "chat_id")},
        rows=snap_rows), ensure_ascii=False))
    if history:
        history.sort(key=lambda r: r["bar_time"])
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            for r in history:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return sum(len(v) for v in found.values())


def next_run(now: pd.Timestamp, delay_min: int) -> pd.Timestamp:
    """Ближайшее закрытие 4H свечи (13:30 или 16:00 ET, пн–пт) + задержка."""
    day = now.normalize()
    for d in range(0, 8):
        base = day + pd.Timedelta(days=d)
        if base.weekday() >= 5:
            continue
        for hm in ((13, 30), (16, 0)):
            t = base + pd.Timedelta(hours=hm[0], minutes=hm[1] + delay_min)
            if t > now:
                return t
    raise RuntimeError("unreachable")


def loop(s: dict) -> None:
    send("🤖 Бот ордер-блоков запущен. Проверка после закрытия каждой 4H свечи "
         "(13:30 и 16:00 по Нью-Йорку).", s)
    while True:
        now = pd.Timestamp.now(tz=data.ET)
        nxt = next_run(now, s["delay_min"])
        log.info("следующая проверка: %s ET", nxt.strftime("%a %d.%m %H:%M"))
        while pd.Timestamp.now(tz=data.ET) < nxt:     # короткие сны — переживает сон ноутбука
            time.sleep(30)
        try:
            n = run_once(s)
            log.info("сигналов: %d", n)
        except Exception as e:
            log.exception("ошибка проверки: %s", e)
            send(f"⚠️ Ошибка проверки: {html.escape(str(e))[:500]}", s)


def status(ticker: str, s: dict) -> None:
    ticker = ticker.upper()
    df = data.update_bars([ticker])[ticker]
    events, bulls, bears = detect(df, s["length"], s["mitigation"], s["track_last"])
    last = df.iloc[-1]
    print(f"{ticker}: последняя 4H свеча {df.index[-1]:%d.%m %H:%M} ET, закрытие {last.close:.2f}")
    for name, obs in (("Бычьи (поддержка)", bulls), ("Медвежьи (сопротивление)", bears)):
        print(f"\n{name}:")
        for ob in obs[: s["track_last"] or None]:
            print(f"  {ob.btm:.2f} – {ob.top:.2f}   свеча {ob.ob_time:%d.%m.%Y %H:%M}")
    print("\nПоследние события:")
    for e in events[-8:]:
        print(f"  {e.bar_time:%d.%m %H:%M}  {e.type:9} {e.ob.side}  {e.ob.btm:.2f}–{e.ob.top:.2f}")


def main():
    load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(BASE / "bot.log", encoding="utf-8")])
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--status")
    p.add_argument("--chat-id", action="store_true")
    p.add_argument("--test", action="store_true")
    a = p.parse_args()
    s = settings()

    if a.chat_id:
        for u in tg("getUpdates", s["token"]):
            m = u.get("message") or u.get("channel_post") or {}
            ch = m.get("chat", {})
            print(ch.get("id"), ch.get("type"), ch.get("title") or ch.get("username") or ch.get("first_name"))
        return
    if a.test:
        send("✅ Тест: бот ордер-блоков на связи.", s)
        return
    if a.status:
        status(a.status, s)
        return
    if a.once:
        run_once(s, a.dry)
        return
    loop(s)


if __name__ == "__main__":
    main()
