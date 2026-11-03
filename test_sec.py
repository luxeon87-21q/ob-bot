"""Проверки фундаментала: python -m pytest -q test_sec.py"""
import fundamentals as F


def _fact(start, end, val, form="10-Q", filed="2026-01-01", fp="Q1", fy=2026, accn="a"):
    return dict(start=start, end=end, val=val, form=form, filed=filed, fp=fp, fy=fy, accn=accn)


def test_quarters_and_derived_q4():
    facts = [_fact("2025-01-01", "2025-03-31", 100, fp="Q1", fy=2025),
             _fact("2025-04-01", "2025-06-30", 110, fp="Q2", fy=2025),
             _fact("2025-07-01", "2025-09-30", 120, fp="Q3", fy=2025),
             _fact("2025-01-01", "2025-12-31", 460, form="10-K", fp="FY", fy=2025),
             _fact("2025-01-01", "2025-06-30", 210, fp="Q2", fy=2025),          # полугодие — пропуск
             _fact("2025-07-01", "2025-09-30", 121, filed="2026-11-01", fp="Q3", fy=2026, accn="b")]  # пересчёт
    q = F.quarters_of(facts)
    assert set(q) == {"2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"}
    assert q["2025-09-30"]["v"] == 121 and q["2025-09-30"]["fy"] == 2025        # значение новое, метка — первая
    assert q["2025-12-31"]["derived"] and q["2025-12-31"]["v"] == 460 - 100 - 110 - 121


def test_analyse():
    def node(vals, unit="USD"):
        return {"units": {unit: [_fact(s, e, v) for s, e, v in vals]}}
    qs = [("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-03-31"),
          ("2026-04-01", "2026-06-30"), ("2026-07-01", "2026-09-30")]
    cf = {"entityName": "Test Co", "facts": {"us-gaap": {
        "Revenues": node([(s, e, v) for (s, e), v in zip(qs, [100, 110, 120, 130, 150])]),
        "GrossProfit": node([(s, e, v) for (s, e), v in zip(qs, [50, 55, 60, 65, 75])]),
        "OperatingIncomeLoss": node([(s, e, v) for (s, e), v in zip(qs, [10, 11, 12, 13, 10])]),
        "NetIncomeLoss": node([(s, e, v) for (s, e), v in zip(qs, [8, 9, 10, 11, 20])]),
        "EarningsPerShareDiluted": node([(s, e, v) for (s, e), v in zip(qs, [0.1, 0.11, 0.12, 0.13, 0.25])], "USD/shares"),
    }}}
    r = F.analyse(cf)
    cur = r["q"][0]
    assert r["name"] == "Test Co" and cur["e"] == "2026-09-30"
    assert cur["rev_yoy"] == 50.0 and cur["gm"] == 50.0 and cur["om"] == 6.7 and cur["eps"] == 0.25
    assert r["ttm"]["rev"] == 110 + 120 + 130 + 150 and r["ttm"]["eps"] == 0.61
    assert "one_off" in r["flags"]                           # чистая 20 > операционной 10


def test_latest_docs():
    subs = [dict(form="8-K", items="2.02,9.01", filingDate="2026-11-03", accessionNumber="0001-26-1", primaryDocument="a.htm"),
            dict(form="4", filingDate="2026-10-02", accessionNumber="0001-26-0", primaryDocument="b.xml"),
            dict(form="10-Q", filingDate="2026-08-06", reportDate="2026-06-30", accessionNumber="0001-26-2",
                 primaryDocument="q.htm")]
    d = F.latest_docs(subs, 1582961)
    assert d["report"]["form"] == "10-Q" and d["report"]["period"] == "2026-06-30"
    assert d["report"]["url"].endswith("/1582961/000126 2/q.htm".replace(" ", ""))
    assert d["press"]["filed"] == "2026-11-03"
