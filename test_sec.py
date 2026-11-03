"""Проверки фундаментала и инсайдеров: python -m pytest -q test_sec.py"""
from datetime import date

import fundamentals as F
import insiders as I

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0001582961</issuerCik><issuerTradingSymbol>DOCN</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Srinivasan Padmanabhan T</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>0</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-21</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>140</value></transactionPricePerShare></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>752817</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-21</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>1394</value></transactionShares>
        <transactionPricePerShare><value>140</value></transactionPricePerShare></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>751423</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-20</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>5000</value></transactionShares></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <footnotes><footnote id="F1">The sale was effected pursuant to a Rule 10b5-1 trading plan.</footnote></footnotes>
</ownershipDocument>"""


def test_form4_sale_with_plan():
    p = I.parse_form4(FORM4)
    assert p["who"] == "Srinivasan Padmanabhan T"
    assert p["role"] == "Chief Executive Officer, Director"
    assert p["plan"] is True                                # сноска про 10b5-1
    assert len(p["trades"]) == 1                            # исполнение опционов (M) не показываем
    s = p["trades"][0]
    assert s["code"] == "S" and s["sh"] == 11394 and s["px"] == 140.0 and s["val"] == 1595160
    assert s["own"] == 751423 and s["chg"] == round(100 * 11394 / (751423 + 11394), 1)


def test_form4_doc_and_clusters():
    assert I.form4_xml_doc("xslF345X05/wk-form4_1.xml") == "wk-form4_1.xml"
    assert I.form4_xml_doc("form4.xml") == "form4.xml"
    rows = [dict(t="X", code="P", who="A", filed="2026-10-20", date="2026-10-19"),
            dict(t="X", code="P", who="B", filed="2026-10-25", date="2026-10-24"),
            dict(t="X", code="S", who="C", filed="2026-10-25", date="2026-10-24"),
            dict(t="Y", code="P", who="A", filed="2026-10-25", date="2026-10-24"),
            dict(t="Z", code="P", who="A", filed="2026-08-01", date="2026-08-01"),
            dict(t="Z", code="P", who="B", filed="2026-08-02", date="2026-08-02")]
    assert I.clusters(rows, date(2026, 11, 3)) == {"X": 2}


PTR = """
Name:            Hon. Nancy Pelosi
Status:          Member
State/District:  CA11

ID     Owner Asset                                          Transaction Date          Notification Amount            Cap.
                                                            Type                      Date                           Gains >
                                                                                                                     $200?
       SP     Bloom Energy Corporation Class A Common      P        07/24/2026        07/24/2026   $1,000,001 -
              Stock (BE) [OP]                                                                      $5,000,000
              F     S     : New
              D           : Purchased 100 call options with a strike price of $100 and an expiration
              date of 6/17/27.
       SP     Intel Corporation - Common Stock (INTC)      S (partial) 07/28/2026     07/28/2026   $250,001 -
              [ST]                                                                                 $500,000
              F     S     : New
* For the complete list of asset type abbreviations
"""


def test_ptr_parse():
    rows = I.parse_ptr_text(PTR)
    assert len(rows) == 2
    be, intc = rows
    assert be["t"] == "BE" and be["kind"] == "OP" and be["type"] == "P" and be["owner"] == "супруг(а)"
    assert be["lo"] == 1000001 and be["hi"] == 5000000 and be["date"] == "2026-07-24"
    assert "call options" in be["desc"]
    assert intc["t"] == "INTC" and intc["type"] == "S" and intc["partial"] and intc["hi"] == 500000
    assert intc["asset"].startswith("Intel Corporation")


def test_message():
    new4 = [dict(t="X", code="P", who="A B", role="CEO", sh=1000, px=10.0, val=10000, plan=False),
            dict(t="Y", code="S", who="C", role="CFO", sh=10000, px=200.0, val=2_000_000, plan=False, chg=12.5),
            dict(t="Z", code="S", who="D", role="CFO", sh=10000, px=200.0, val=2_000_000, plan=True)]
    house = [dict(name="Nancy Pelosi", district="CA11", filed="2026-08-21", url="u",
                  rows=[dict(t="BE", kind="OP", type="P", owner="супруг(а)", lo=1000001, hi=5000000,
                             date="2026-07-24", days=28, desc="call options"),
                        dict(t="NOTINLIST", kind="ST", type="P", owner="сам", lo=1, hi=2, date="2026-07-24", days=28)])]
    m = I.build_message(new4, house, {"X": 3}, {"X", "Y", "Z", "BE"})
    assert "X" in m and "кластер: 3" in m and "−12.5% доли" in m
    assert ">Z<" not in m                                   # продажа по плану — не показываем
    assert "BE" in m and "(опционы)" in m and "NOTINLIST" not in m
    assert I.build_message([], [], {}, set()) == ""


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
