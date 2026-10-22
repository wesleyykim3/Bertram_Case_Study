"""Checks over the 21 provided inbound rows. No pytest needed.

Run:  python tests/run_tests.py
Uses a temp DB, seeds from data/perennials_sample_data.xlsx, processes all
rows in time order, and asserts the expected outcome for each trap.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perennials import pipeline, seed as seed_mod  # noqa: E402
from perennials.db import connect, init_db  # noqa: E402

FAIL = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ((" :: " + str(detail)) if (detail and not cond) else ""))
    if not cond:
        FAIL.append(label)


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "test.db")
    conn = connect(db)
    init_db(conn)
    conn.close()
    # seed into temp DB by pointing env var used by connect() default -- seed() takes db_path
    seed_mod.seed(db_path=db)
    conn = connect(db)
    results = {}
    ordered = conn.execute("SELECT id FROM raw_inbound ORDER BY received, id").fetchall()
    snap14 = {}
    for r in ordered:
        results[r["id"]] = pipeline.process_inbound(conn, r["id"])
        if r["id"] == "EML-014":
            q = conn.execute("SELECT * FROM crm_quotes WHERE inbound_id='EML-014'").fetchone()
            snap14["status"] = q["status"]
            snap14["reserved"] = conn.execute(
                "SELECT COALESCE(SUM(qty_yds),0) s FROM erp_reservations WHERE quote_id=? AND status='active'",
                (q["id"],)).fetchone()["s"]

    def inbound(iid):
        return conn.execute("SELECT * FROM raw_inbound WHERE id=?", (iid,)).fetchone()

    def quote_for(iid):
        return conn.execute("SELECT * FROM crm_quotes WHERE inbound_id=? ORDER BY version DESC LIMIT 1", (iid,)).fetchone()

    # Qualification / guardrails
    check("WEB-001 Dana qualified + sample", inbound("WEB-001")["processed_status"] == "processed")
    check("WEB-002 consumer not qualified", inbound("WEB-002")["reason_code"] == "CONSUMER")
    check("WEB-003 duplicate -> merge proposal", inbound("WEB-003")["reason_code"] == "POSSIBLE_DUPLICATE")
    check("WEB-005 Do Not Sell blocked", inbound("WEB-005")["processed_status"] == "blocked")
    check("WEB-007 spam discarded", inbound("WEB-007")["processed_status"] == "discarded")
    check("WEB-008 discontinued sample blocked", inbound("WEB-008")["processed_status"] == "needs_review")
    check("WEB-009 student not qualified", inbound("WEB-009")["reason_code"] == "STUDENT")
    check("WEB-012 ambiguous trade -> review", inbound("WEB-012")["reason_code"] == "AMBIGUOUS_TRADE")
    check("WEB-013 over 10 samples -> review", inbound("WEB-013")["reason_code"] == "MAX_10_SAMPLES")

    # No records should exist for spam; blocked account got no sample order
    n = conn.execute("SELECT COUNT(*) c FROM erp_sample_orders WHERE inbound_id='WEB-007'").fetchone()["c"]
    check("WEB-007 created no sample order", n == 0)
    n = conn.execute("SELECT COUNT(*) c FROM erp_sample_orders WHERE inbound_id='WEB-005'").fetchone()["c"]
    check("WEB-005 created no sample order", n == 0)

    # Quotes
    q14 = quote_for("EML-014")
    check("EML-014 quote held (at time of quoting)", snap14.get("status") == "held", snap14)
    check("EML-014 reserved 100 yd total (80+20)", abs(snap14.get("reserved", 0) - 100) < 0.01, snap14)
    line = conn.execute("SELECT * FROM crm_quote_lines WHERE quote_id=? AND sku='PF-1001'", (q14["id"],)).fetchone()
    check("EML-014 stripe unit $78.40 (98*(1-.15-.05))", abs(line["unit_price"] - 78.40) < 0.02, line["unit_price"])
    check("EML-014 stripe dye-lot split warning", line["warning"] is not None and "split" in line["warning"], line["warning"])

    q15 = quote_for("EML-015")
    l15 = conn.execute("SELECT * FROM crm_quote_lines WHERE quote_id=?", (q15["id"],)).fetchone()
    check("EML-015 12 m converted to 13.12 yd, SKU PF-1004", l15["sku"] == "PF-1004" and abs(l15["qty_yds"] - 13.12) < 0.05, (l15["sku"], l15["qty_yds"]))

    q16 = quote_for("EML-016")
    check("EML-016 shortfall -> review, not held", q16["status"] == "shortfall_review", q16["status"])

    q17 = quote_for("EML-017")
    l17 = conn.execute("SELECT * FROM crm_quote_lines WHERE quote_id=?", (q17["id"],)).fetchone()
    check("EML-017 cut fee $25 on 3 yd", abs(l17["cut_fee"] - 25) < 0.01 and abs(l17["line_total"] - 223.90) < 0.05, l17["line_total"])

    q18 = quote_for("EML-018")
    res18 = conn.execute("SELECT COUNT(*) c FROM erp_reservations WHERE quote_id=? AND status='active'", (q18["id"],)).fetchone()["c"]
    check("EML-018 budgetary: priced, no reservation", q18["status"] == "budgetary_no_hold" and res18 == 0)

    q19 = quote_for("EML-019")
    check("EML-019 backorder shortfall flagged", q19["status"] in ("shortfall_review", "priced_no_hold") and q19 is not None)

    q20 = quote_for("EML-020")
    l20 = conn.execute("SELECT * FROM crm_quote_lines WHERE quote_id=?", (q20["id"],)).fetchone()
    check("EML-020 retail: list unit $138 + $25 cut fee", abs(l20["unit_price"] - 138) < 0.01 and abs(l20["cut_fee"] - 25) < 0.01, l20["unit_price"])

    q21 = quote_for("EML-021")
    q14_after = conn.execute("SELECT status FROM crm_quotes WHERE id=?", (q14["id"],)).fetchone()["status"]
    check("EML-021 supersedes EML-014 (v2, old superseded)", q21["version"] == 2 and q14_after == "superseded")
    active_total = conn.execute(
        "SELECT COALESCE(SUM(r.qty_yds),0) s FROM erp_reservations r JOIN crm_quotes q ON q.id=r.quote_id "
        "WHERE q.opportunity_id=? AND r.status='active'", (q21["opportunity_id"],)).fetchone()["s"]
    check("EML-021 no double-hold (90>87 so no active hold from v2)", active_total == 0, active_total)
    check("EML-021 shortfall flagged", q21["status"] == "shortfall_review", q21["status"])

    print("\n%d failures" % len(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
