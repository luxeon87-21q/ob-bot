"""
Инсайдеры (Form 4 в SEC) и сделки конгрессменов (PTR Палаты представителей).

Form 4 — по акциям сайта, сделки на открытом рынке:
  • P — покупка, S — продажа (гранты, опционы сотрудникам, налоги и подарки не показываются);
  • план 10b5-1 — продажа по заранее утверждённому графику (отметка в форме или сноска): о будущем
    она почти ничего не говорит; покупки и продажи БЕЗ плана — интереснее;
  • кластер — за 30 дней покупали ≥ 2 разных инсайдера одной компании.
Инсайдер обязан подать Form 4 за 2 рабочих дня, поэтому это свежие данные.

Конгресс — PTR Палаты: индекс деклараций за год (ZIP с XML) и PDF каждой декларации,
текст из PDF (pdftotext -layout). Сделки раскрываются до 45 дней спустя, суммы — диапазоны.
Бумажные декларации (сканы) не разбираются — показываются ссылкой. Сенат — только вручную на
efdsearch.senate.gov (сайт требует принять соглашение).

Запуск (по будням после закрытия, .github/workflows/sec.yml):
  python insiders.py            — обновить, Telegram (новые покупки инсайдеров и сделки конгрессменов)
  python insiders.py --no-tg
"""
from __future__ import annotations

import argparse
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

import sec

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "insiders.json"
SEEN_FILE = BASE / "cache" / "insiders_seen.json"
KEEP_DAYS = 120          # сколько дней истории держать
FIRST_DAYS = 45          # первый запуск: сколько дней назад смотреть
CLUSTER_DAYS = 30
HOUSE_INDEX = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{y}FD.zip"
HOUSE_PTR = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{y}/{doc}.pdf"
HOUSE_UA = {"User-Agent": "ob-bot (github.com/luxeon87-21q/ob-bot)"}
SITE_URL = "https://luxeon87-21q.github.io/ob-bot/#insiders"
log = logging.getLogger("ob_bot.insiders")


# ======================================================================== Form 4
def _v(el, path: str) -> Optional[str]:
    x = el.find(path)
    if x is None:
        return None
    v = x.find("value")
    t = (v.text if v is not None else x.text) or ""
    return t.strip() or None


def _num(s) -> Optional[float]:
    try:
        return float(str(s).replace(",", ""))
    except Exception:
        return None


def _true(s) -> bool:
    return str(s or "").strip().lower() in ("1", "true", "y", "yes")


def parse_form4(xml: str) -> Optional[dict]:
    """Один Form 4 → кто, роль, покупки и продажи на открытом рынке (по сумме за документ)."""
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        return None
    ow = root.find("reportingOwner")
    who = _v(ow, "reportingOwnerId/rptOwnerName") if ow is not None else None
    rel = ow.find("reportingOwnerRelationship") if ow is not None else None
    roles = []
    if rel is not None:
        if _true(_v(rel, "isOfficer")):
            roles.append((_v(rel, "officerTitle") or "Officer"))
        if _true(_v(rel, "isDirector")):
            roles.append("Director")
        if _true(_v(rel, "isTenPercentOwner")):
            roles.append("10% owner")
        if _true(_v(rel, "isOther")) and not roles:
            roles.append(_v(rel, "otherText") or "Other")
    notes = " ".join((f.text or "") for f in root.iter("footnote"))
    plan = _true(_v(root, "aff10b5One")) or bool(re.search(r"10b5-?1", notes, re.I))
    agg: Dict[str, dict] = {}
    for tx in root.iter("nonDerivativeTransaction"):
        code = (_v(tx, "transactionCoding/transactionCode") or "").upper()
        if code not in ("P", "S"):
            continue
        sh = _num(_v(tx, "transactionAmounts/transactionShares")) or 0
        px = _num(_v(tx, "transactionAmounts/transactionPricePerShare"))
        a = agg.setdefault(code, dict(sh=0.0, val=0.0, date=None, own=None))
        a["sh"] += sh
        a["val"] += sh * (px or 0)
        d = _v(tx, "transactionDate")
        a["date"] = max(filter(None, [a["date"], (d or "")[:10]]), default=None)
        own = _num(_v(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction"))
        if own is not None:
            a["own"] = own
    if not agg:
        return dict(who=who, role=", ".join(roles), plan=plan, trades=[])
    trades = []
    for code, a in agg.items():
        px = a["val"] / a["sh"] if a["sh"] else None
        before = (a["own"] + a["sh"]) if code == "S" and a["own"] is not None else \
                 (a["own"] - a["sh"]) if code == "P" and a["own"] is not None else None
        chg = round(100 * a["sh"] / before, 1) if before else None
        trades.append(dict(code=code, sh=round(a["sh"]), px=None if px is None else round(px, 2),
                           val=round(a["val"]), date=a["date"], own=None if a["own"] is None else round(a["own"]),
                           chg=chg))
    return dict(who=who, role=", ".join(roles), plan=plan, trades=trades)


def form4_xml_doc(primary: str) -> str:
    """primaryDocument бывает «xslF345X05/form4.xml» (HTML-вид) — сам XML лежит без префикса."""
    return primary.split("/", 1)[1] if primary.lower().startswith("xsl") and "/" in primary else primary


def form4_rows(api: sec.SEC, t: str, cik: int, seen: set, since: str) -> List[dict]:
    sub = api.submissions(cik)
    out = []
    for f in sec.recent(sub or {}):
        if f.get("form") not in ("4", "4/A"):
            continue
        if (f.get("filingDate") or "") < since:
            break                                   # список отсортирован: дальше старее
        acc = f["accessionNumber"]
        if acc in seen:
            continue
        doc = form4_xml_doc(f.get("primaryDocument", ""))
        r = api.get(sec.archive_url(cik, acc, doc))
        seen.add(acc)
        if r.status_code != 200:
            continue
        p = parse_form4(r.text)
        if not p:
            continue
        for tr in p["trades"]:
            out.append(dict(t=t, filed=f.get("filingDate"), who=p["who"], role=p["role"], plan=p["plan"],
                            url=sec.archive_url(cik, acc, f.get("primaryDocument", "")), acc=acc, **tr))
    return out


def clusters(rows: List[dict], today: date, days: int = CLUSTER_DAYS) -> Dict[str, int]:
    """Сколько разных инсайдеров покупало каждую акцию за последние days дней (только ≥ 2)."""
    since = (today - timedelta(days=days)).isoformat()
    buyers: Dict[str, set] = {}
    for r in rows:
        if r["code"] == "P" and (r.get("date") or r["filed"]) >= since:
            buyers.setdefault(r["t"], set()).add(r["who"])
    return {t: len(w) for t, w in buyers.items() if len(w) >= 2}


# ======================================================================== Конгресс (Палата)
def _iso(mdy: str) -> Optional[str]:
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", mdy or "")
    if not m:
        return None
    y = int(m.group(3))
    y += 2000 if y < 100 else 0
    try:
        return date(y, int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def parse_house_index(data: bytes) -> List[dict]:
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
            data = z.read(name)
    root = ET.fromstring(data)
    out = []
    for m in root.iter("Member"):
        f = {c.tag: (c.text or "").strip() for c in m}
        if f.get("FilingType") != "P":
            continue
        name = " ".join(x for x in (f.get("First"), f.get("Last")) if x)
        out.append(dict(doc=f.get("DocID", ""), name=name, district=f.get("StateDst", ""),
                        year=int(f.get("Year") or 0), filed=_iso(f.get("FilingDate", ""))))
    return out


TXN = re.compile(r"^\s*(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+"
                 r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+(?P<amount>\S.*?)\s*$")
DESC = re.compile(r"^\s*D(?:ESCRIPTION)?\s*:\s*(.*)$", re.I)
STOP = re.compile(r"^\s*(F(?:ILING)?\s+S(?:TATUS)?\s*:|S(?:UBHOLDING)?\s+O(?:F)?\s*:|L(?:OCATION)?\s*:|\* For the complete|"
                  r"I CERTIFY|Digitally Signed|Filing ID #|ID\s+Owner\s+Asset|Type\s+Date\b)", re.I)
OWNER = {"SP": "супруг(а)", "JT": "совместно", "DC": "ребёнок", "": "сам"}
TYPE = {"P": "P", "S": "S", "S (partial)": "S", "E": "E"}


def parse_ptr_text(text: str) -> List[dict]:
    """Строки сделок из текста PTR (pdftotext -layout). Тикер — в скобках в названии актива."""
    rows, cur, mode, amount_open = [], None, None, False
    for raw in text.splitlines():
        line, s = raw.rstrip(), raw.strip()
        t = TXN.match(line)
        if t and _iso(t.group("date")):
            if cur:
                rows.append(cur)
            cur = dict(owner=t.group("owner") or "", asset=t.group("asset"), type=TYPE[t.group("type")],
                       partial=t.group("type") == "S (partial)", date=_iso(t.group("date")),
                       amount=t.group("amount"), desc="")
            amount_open = cur["amount"].endswith("-")
            mode = "asset"
            continue
        if cur is None or not s:
            if cur is not None and mode == "desc":
                mode = None
            continue
        d = DESC.match(line)
        if d:
            cur["desc"], mode = d.group(1).strip(), "desc"
            continue
        if STOP.match(s):
            mode = None
            continue
        if mode == "asset":
            parts = re.split(r"\s{2,}", s)
            if amount_open and re.fullmatch(r"\$[\d,]+", parts[-1]):
                cur["amount"] += " " + parts.pop()
                amount_open = False
            if parts and parts[0]:
                cur["asset"] += " " + " ".join(parts)
        elif mode == "desc":
            cur["desc"] += " " + s
    if cur:
        rows.append(cur)
    out = []
    for r in rows:
        asset = " ".join(r["asset"].split())
        tick = re.findall(r"\(([A-Z][A-Z0-9.\-]{0,9})\)", asset)
        kind = re.findall(r"\[([A-Z]{2})\]", asset)
        name = re.sub(r"\s*\[[A-Z]{2}\]", "", asset)
        if tick:
            name = re.sub(r"\s*\(%s\)" % re.escape(tick[-1]), "", name)
        nums = [int(x.replace(",", "")) for x in re.findall(r"\$([\d,]+)", r["amount"])]
        out.append(dict(owner=OWNER.get(r["owner"], r["owner"]), t=tick[-1].replace(".", "-") if tick else None,
                        kind=kind[-1] if kind else None, asset=name.strip(" -"), type=r["type"],
                        partial=r["partial"], date=r["date"], lo=nums[0] if nums else None,
                        hi=nums[1] if len(nums) > 1 else None, desc=" ".join(r["desc"].split()) or None))
    return out


def pdf_text(data: bytes) -> Optional[str]:
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        try:
            p = subprocess.run([exe, "-layout", f.name, "-"], capture_output=True, timeout=60)
        except Exception:
            return None
    return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else None


def house(seen: set, today: date, since: str) -> List[dict]:
    """Новые PTR Палаты: каждая — dict(doc, name, district, filed, url, rows, scan)."""
    years = [today.year] + ([today.year - 1] if today.month == 1 else [])
    filings = []
    for y in years:
        try:
            r = requests.get(HOUSE_INDEX.format(y=y), headers=HOUSE_UA, timeout=60)
            r.raise_for_status()
            filings += parse_house_index(r.content)
        except Exception as e:
            log.warning("индекс Палаты %d: %s", y, e)
    new = [f for f in filings if f["filed"] and f["filed"] >= since and f["doc"] not in seen]
    log.info("Конгресс: деклараций в индексе %d, новых %d", len(filings), len(new))
    out = []
    for f in sorted(new, key=lambda x: x["filed"]):
        url = HOUSE_PTR.format(y=f["year"] or today.year, doc=f["doc"])
        try:
            r = requests.get(url, headers=HOUSE_UA, timeout=60)
            r.raise_for_status()
        except Exception as e:
            log.debug("PTR %s: %s", f["doc"], e)
            continue
        seen.add(f["doc"])
        txt = pdf_text(r.content)
        rows = parse_ptr_text(txt) if txt else []
        for x in rows:
            x["days"] = (date.fromisoformat(f["filed"]) - date.fromisoformat(x["date"])).days if x["date"] else None
        out.append(dict(f, url=url, rows=rows, scan=not (txt or "").strip() or not rows))
        time.sleep(0.3)
    return out


# ======================================================================== прогон и сообщение
def money(v) -> str:
    if v is None:
        return "—"
    v = abs(v)
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K" if v >= 1e3 else f"${v:.0f}"


def rng(lo, hi) -> str:
    if lo is None:
        return "—"
    return f"{money(lo)}–{money(hi)}" if hi else f"от {money(lo)}"


def build_message(new_f4: List[dict], new_house: List[dict], cl: Dict[str, int], universe: set, top: int = 25) -> str:
    buys = sorted([r for r in new_f4 if r["code"] == "P"], key=lambda r: -(r.get("val") or 0))
    big_sells = sorted([r for r in new_f4 if r["code"] == "S" and not r["plan"] and (r.get("val") or 0) >= 1e6],
                       key=lambda r: -r["val"])
    cong = [dict(x, who=f["name"], district=f["district"], filed=f["filed"], url=f["url"])
            for f in new_house for x in f["rows"] if x.get("t") and x["t"] in universe]
    if not (buys or big_sells or cong):
        return ""
    tk = lambda t: (f'<a href="https://luxeon87-21q.github.io/ob-bot/#fund/{html.escape(t)}"><b>{html.escape(t)}</b></a>')
    out = ["🕵️ <b>Инсайдеры и Конгресс — новое</b>"]
    if buys:
        out.append("\n🟢 <b>Покупки инсайдеров на рынке</b> (Form 4)")
        for r in buys[:top]:
            c = f" · 👥 кластер: {cl[r['t']]} покупателя за 30 дн." if r["t"] in cl else ""
            out.append(f"{tk(r['t'])}: {html.escape(r['who'] or '?')} ({html.escape(r['role'] or '—')}) — "
                       f"{r['sh']:,} акц. по ${r['px'] or 0:,.2f} = <b>{money(r['val'])}</b>{c}".replace(",", " "))
    if big_sells:
        out.append("\n🔴 <b>Крупные продажи без плана 10b5-1</b> (от $1M)")
        for r in big_sells[:10]:
            ch = f", −{r['chg']}% доли" if r.get("chg") else ""
            out.append(f"{tk(r['t'])}: {html.escape(r['who'] or '?')} ({html.escape(r['role'] or '—')}) — "
                       f"<b>{money(r['val'])}</b>{ch}")
    if cong:
        out.append("\n🏛 <b>Конгресс</b> (раскрыто до 45 дней спустя)")
        for x in cong[:top]:
            act = {"P": "купил(а)", "S": "продал(а)", "E": "обмен"}.get(x["type"], x["type"])
            opt = " (опционы)" if x.get("kind") == "OP" or "option" in (x.get("desc") or "").lower() else ""
            out.append(f"{tk(x['t'])}: {html.escape(x['who'])} ({x['district']}, {x['owner']}) {act}{opt} "
                       f"{rng(x['lo'], x['hi'])}, сделка {x['date'][8:10]}.{x['date'][5:7]}, раскрыто через {x['days']} дн.")
    out.append(f"\nВсе сделки: {SITE_URL}")
    out.append("⚠️ Публичные раскрытия, не прогноз. Продажи по плану 10b5-1 и гранты не показаны.")
    return "\n".join(out)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def run(tickers: List[str], no_house: bool = False, budget_s: float = 2400, workers: int = 4) -> dict:
    today = date.today()
    prev = load_json(OUT_FILE, {})
    seen_d = load_json(SEEN_FILE, {})
    seen4, seenh = set(seen_d.get("f4", [])), set(seen_d.get("house", []))
    first = not seen_d
    since = (today - timedelta(days=FIRST_DAYS if first else 10)).isoformat()
    api = sec.SEC()
    ciks = api.cik_map()
    todo = [(t, ciks[t]) for t in tickers if t in ciks]
    log.info("Form 4: %d тикеров, с %s%s", len(todo), since, " (первый запуск)" if first else "")
    new4, t0 = [], time.time()
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(form4_rows, api, t, c, seen4, since): t for t, c in todo}
        for f in as_completed(futs):
            try:
                new4 += f.result()
            except Exception as e:
                log.debug("%s: %s", futs[f], e)
            if time.time() - t0 > budget_s:
                log.warning("лимит времени — остальные в следующий раз")
                for g in futs:
                    g.cancel()
                break
    log.info("Form 4: новых сделок P/S %d за %.0f с", len(new4), time.time() - t0)
    newh = [] if no_house else house(seenh, today, (today - timedelta(days=FIRST_DAYS if first else 30)).isoformat())

    keep = (today - timedelta(days=KEEP_DAYS)).isoformat()
    f4 = [r for r in (prev.get("f4") or []) if r["filed"] >= keep] + new4
    f4 = list({(r["acc"], r["code"]): r for r in f4}.values())
    f4.sort(key=lambda r: (r["filed"], r.get("val") or 0), reverse=True)
    hs = [h for h in (prev.get("house") or []) if (h.get("filed") or "") >= keep] + newh
    hs = list({h["doc"]: h for h in hs}.values())
    hs.sort(key=lambda h: h["filed"], reverse=True)
    cl = clusters(f4, today)
    SEEN_FILE.write_text(json.dumps(dict(f4=sorted(seen4)[-60000:], house=sorted(seenh)[-5000:]),
                                    separators=(",", ":")), encoding="utf-8")
    res = dict(updated=datetime.now(timezone.utc).isoformat(timespec="seconds"), today=today.isoformat(),
               f4=f4, house=hs, clusters=cl)
    return res, ([] if first else new4), ([] if first else newh), cl


def main():
    import bot
    import tg
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tg", action="store_true")
    ap.add_argument("--no-house", action="store_true")
    ap.add_argument("--tickers", default="")
    a = ap.parse_args()
    universe = ([x.strip().upper().replace(".", "-") for x in a.tickers.split(",") if x.strip()]
                if a.tickers else sec.universe_tickers())
    res, new4, newh, cl = run(universe, no_house=a.no_house)
    OUT_FILE.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    log.info("записано: Form 4 %d, деклараций Конгресса %d, кластеров %d", len(res["f4"]), len(res["house"]), len(cl))
    msg = build_message(new4, newh, cl, set(universe))
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if msg and not a.no_tg and token:
        k = tg.broadcast_one(token, tg.load(), msg, key="insiders")
        log.info("разослано подписчикам: %d", k)
    else:
        print(msg or "нового нет (первый запуск копит историю молча)")


if __name__ == "__main__":
    main()
