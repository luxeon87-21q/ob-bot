"""
Фундаментал: что компания сама подала в SEC (XBRL из 10-Q / 10-K), квартал за кварталом.

На каждый тикер: выручка, валовая / операционная / чистая маржа, чистая прибыль, разводнённый EPS,
рост к тому же кварталу год назад, сумма за 4 квартала (TTM) и флаги:
  • one_off  — чистая прибыль больше операционной: внутри разовые доходы (переоценка вложений, налоги);
  • lag      — 10-Q / 10-K уже подан, а в XBRL-данных SEC его квартала ещё нет;
  • derived  — 4-й квартал: 10-Q за него нет, считается как год (10-K) минус три квартала.
Плюс ссылки на последний 10-Q / 10-K и пресс-релиз с результатами (8-K, пункт 2.02).

Обновление экономное: каждый день читается только список документов компании, а сами числа
(companyfacts, несколько МБ) перекачиваются, когда появился новый 10-Q / 10-K.

Запуск (по будням после закрытия, .github/workflows/sec.yml):
  python fundamentals.py                    — все акции сайта, cache/fund.json
  python fundamentals.py --tickers AMD,DOCN — только эти (дописываются в тот же файл)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sec

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "cache" / "fund.json"
QUARTERS = 8
REPORT_FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A")
log = logging.getLogger("ob_bot.fund")

CONCEPTS = {
    "rev": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "RevenuesNetOfInterestExpense",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet", "InterestAndDividendIncomeOperating"],
    "cogs": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold", "CostOfServices"],
    "gp": ["GrossProfit"],
    "oi": ["OperatingIncomeLoss"],
    "ni": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
}


# ======================================================================== разбор XBRL
def _d(s: str) -> date:
    return date.fromisoformat(s)


def _facts(cf: dict, concept: str, unit: str) -> List[dict]:
    node = (cf.get("facts") or {}).get("us-gaap", {}).get(concept)
    if not node:
        return []
    units = node.get("units") or {}
    return units.get(unit) or (next(iter(units.values())) if len(units) == 1 else [])


def quarters_of(facts: List[dict]) -> Dict[str, dict]:
    """{конец периода: {"v", "start", "derived", "fp", "fy", "filed"}} — значения за ~3 месяца.
    Период, поданный несколько раз, берётся из последнего документа (пересчёты учитываются),
    метка квартала — из первого. 4-й квартал = год минус три квартала внутри него."""
    latest, first = {}, {}
    for f in facts:
        if "start" not in f or str(f.get("form", "")).split("/")[0] not in ("10-Q", "10-K", "20-F", "40-F"):
            continue
        key = (f["start"], f["end"])
        st = (f.get("filed", ""), f.get("accn", ""))
        if key not in latest or st > (latest[key].get("filed", ""), latest[key].get("accn", "")):
            latest[key] = f
        if key not in first or st < (first[key].get("filed", ""), first[key].get("accn", "")):
            first[key] = f
    q, years = {}, []
    for (s, e), f in latest.items():
        days = (_d(e) - _d(s)).days
        meta = dict(v=f["val"], start=s, derived=False, fp=first[(s, e)].get("fp"),
                    fy=first[(s, e)].get("fy"), filed=f.get("filed"))
        if 80 <= days <= 100:
            q[e] = meta
        elif 350 <= days <= 380:
            years.append((s, e, f))
    for s, e, f in years:
        if e in q:
            continue
        inside = sorted((x for k, x in q.items() if x["start"] >= s and k < e), key=lambda x: x["start"])
        if len(inside) != 3:
            continue
        q[e] = dict(v=f["val"] - sum(x["v"] for x in inside), start=inside[-1]["start"], derived=True,
                    fp="Q4", fy=first[(s, e)].get("fy"), filed=f.get("filed"))
    return q


def series(cf: dict, key: str) -> Dict[str, dict]:
    unit = "USD/shares" if key == "eps" else "USD"
    out: Dict[str, dict] = {}
    for c in CONCEPTS[key]:
        for e, v in quarters_of(_facts(cf, c, unit)).items():
            out.setdefault(e, v)
    return out


def _yoy(ser: Dict[str, dict], end: str) -> Optional[float]:
    target = _d(end) - timedelta(days=365)
    best = min(((abs((_d(e) - target).days), e) for e in ser), default=None)
    return ser[best[1]]["v"] if best and best[0] <= 12 else None


def _pct(a, b) -> Optional[float]:
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def _m(part, whole) -> Optional[float]:
    return None if part is None or not whole else round(100.0 * part / whole, 1)


def analyse(cf: dict, n: int = QUARTERS) -> Optional[dict]:
    s = {k: series(cf, k) for k in CONCEPTS}
    ends = sorted(set(s["rev"]) | set(s["ni"]), reverse=True)[:n]
    if not ends:
        return None
    rows = []
    for e in ends:
        g = lambda k: s[k].get(e, {}).get("v")
        rev, gp, oi, ni, eps = g("rev"), g("gp"), g("oi"), g("ni"), g("eps")
        if gp is None and rev is not None and g("cogs") is not None:
            gp = rev - g("cogs")
        base = s["rev"].get(e) or s["ni"].get(e) or {}
        rows.append(dict(
            e=e, fp=f"{base.get('fp') or ''} {base.get('fy') or ''}".strip(),
            d=bool(s["rev"].get(e, {}).get("derived") or s["ni"].get(e, {}).get("derived")),
            rev=rev, gp=gp, oi=oi, ni=ni, eps=None if eps is None else round(eps, 2),
            gm=_m(gp, rev), om=_m(oi, rev), nm=_m(ni, rev),
            rev_yoy=_pct(rev, _yoy(s["rev"], e)), ni_yoy=_pct(ni, _yoy(s["ni"], e)),
            eps_yoy=_pct(eps, _yoy(s["eps"], e))))
    ttm = {}
    for k in ("rev", "ni", "eps"):
        last4 = sorted(s[k])[-4:]
        if len(last4) == 4 and (_d(last4[-1]) - _d(last4[0])).days < 300:
            ttm[k] = sum(s[k][x]["v"] for x in last4)
    if "eps" in ttm:
        ttm["eps"] = round(ttm["eps"], 2)
    cur = rows[0]
    flags = []
    if cur["ni"] is not None and cur["oi"] is not None and cur["ni"] > max(cur["oi"], 0):
        flags.append("one_off")
    if cur["d"]:
        flags.append("derived")
    return dict(name=cf.get("entityName"), q=rows, ttm=ttm, flags=flags)


def latest_docs(subs: List[dict], cik: int) -> dict:
    """Последний 10-Q / 10-K и последний пресс-релиз с результатами (8-K, пункт 2.02)."""
    out = {}
    for f in subs:
        form = f.get("form", "")
        if "report" not in out and form in REPORT_FORMS:
            out["report"] = dict(form=form, filed=f.get("filingDate"), period=f.get("reportDate"),
                                 accn=f.get("accessionNumber"),
                                 url=sec.archive_url(cik, f["accessionNumber"], f.get("primaryDocument", "")))
        if "press" not in out and form == "8-K" and "2.02" in str(f.get("items", "")):
            out["press"] = dict(filed=f.get("filingDate"),
                                url=sec.archive_url(cik, f["accessionNumber"]))
        if len(out) == 2:
            break
    return out


# ======================================================================== прогон
def one(api: sec.SEC, t: str, cik: int, prev: Optional[dict]) -> Tuple[str, Optional[dict], str]:
    sub = api.submissions(cik)
    if not sub:
        return t, prev, "нет submissions"
    docs = latest_docs(sec.recent(sub), cik)
    rep = docs.get("report") or {}
    today = date.today().isoformat()
    if prev and prev.get("accn") == rep.get("accn") and prev.get("q"):
        prev.update(docs=docs, checked=today)
        return t, prev, "без изменений"
    cf = api.companyfacts(cik)
    res = analyse(cf) if cf else None
    if not res:
        return t, dict(cik=cik, name=sub.get("name"), accn=rep.get("accn"), docs=docs, checked=today, q=[]), "нет XBRL"
    if rep.get("period") and res["q"] and rep["period"] > res["q"][0]["e"] \
            and (_d(rep["period"]) - _d(res["q"][0]["e"])).days > 20:
        res["flags"].append("lag")
    res.update(cik=cik, accn=rep.get("accn"), docs=docs, checked=today)
    return t, res, "обновлено"


def run(tickers: List[str], budget_s: float = 2400, workers: int = 4) -> dict:
    api = sec.SEC()
    try:
        data = json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    store: Dict[str, dict] = data.get("tickers") or {}
    ciks = api.cik_map()
    todo = [(t, ciks[t]) for t in tickers if t in ciks]
    log.info("фундаментал: %d тикеров, у SEC найдено %d", len(tickers), len(todo))
    t0, stat = time.time(), {}
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(one, api, t, c, store.get(t)): t for t, c in todo}
        for f in as_completed(futs):
            t = futs[f]
            try:
                _, rec, how = f.result()
            except Exception as e:
                how, rec = f"ошибка: {type(e).__name__}", None
                log.debug("%s: %s", t, e)
            if rec is not None:
                store[t] = rec
            stat[how.split(":")[0]] = stat.get(how.split(":")[0], 0) + 1
            if time.time() - t0 > budget_s:
                log.warning("лимит времени — остальные в следующий раз")
                for g in futs:
                    g.cancel()
                break
    log.info("фундаментал: %s за %.0f с", stat, time.time() - t0)
    return dict(updated=datetime.now(timezone.utc).isoformat(timespec="seconds"), tickers=store)


def main():
    import bot
    bot.load_env(BASE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="")
    a = ap.parse_args()
    tickers = ([x.strip().upper().replace(".", "-") for x in a.tickers.split(",") if x.strip()]
               if a.tickers else sec.universe_tickers())
    res = run(tickers)
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    log.info("записано %d компаний", len(res["tickers"]))


if __name__ == "__main__":
    main()
