"""Pipeline: raw_inbound -> extraction -> match -> qualify -> sample/quote -> reservation.

Every branch writes an audit row and, where judgement is required, a
review_queue item instead of guessing.
"""
import json

from . import extractor as extractor_mod
from . import matcher as matcher_mod
from . import rules
from .db import add_review, audit, dumps, now_iso


# ---------------------------------------------------------------- ids
def _next_id(conn, table, prefix):
    n = conn.execute("SELECT COUNT(*) c FROM %s WHERE id LIKE ?" % table, (prefix + "%",)).fetchone()["c"]
    return "%s%04d" % (prefix, n + 1)


# ---------------------------------------------------------------- records
def ensure_contact(conn, inbound, m, trade_guess=True, status="Prospect"):
    """Return (contact_id, account_id), creating POC records if genuinely new."""
    if m.get("contact") is not None:
        return m["contact"]["id"], m["account_id"]
    if m.get("outcome") == "account_only" and m.get("account_id"):
        account_id = m["account_id"]
    else:
        account_id = _next_id(conn, "crm_accounts", "ACC-POC-")
        acc_type = "Trade - Other" if trade_guess else "Consumer / Other"
        conn.execute("INSERT INTO crm_accounts(id,name,type,status,source) VALUES (?,?,?,?, 'poc')",
                     (account_id, inbound["company"] or inbound["name"] or "Unknown", acc_type, status))
    contact_id = _next_id(conn, "crm_contacts", "CON-POC-")
    conn.execute("INSERT INTO crm_contacts(id,account_id,name,email,source) VALUES (?,?,?,?, 'poc')",
                 (contact_id, account_id, inbound["name"] or "Unknown", (inbound["email"] or "").lower()))
    if inbound["email"]:
        conn.execute("INSERT OR IGNORE INTO contact_emails VALUES (?,?,1)", (contact_id, inbound["email"].lower()))
    audit(conn, "create_contact", contact_id, inbound["id"], "created from inbound (account %s)" % account_id)
    return contact_id, account_id


def find_or_create_opportunity(conn, contact_id, account_id, inbound, title=None):
    row = conn.execute(
        "SELECT * FROM crm_opportunities WHERE contact_id=? AND stage NOT IN ('Closed Won','Closed Lost') "
        "ORDER BY created_at DESC LIMIT 1", (contact_id,)).fetchone()
    if row:
        return row["id"]
    opp_id = _next_id(conn, "crm_opportunities", "OPP-")
    conn.execute("INSERT INTO crm_opportunities VALUES (?,?,?,?,?,?,?)",
                 (opp_id, account_id, contact_id, inbound["id"],
                  title or ("Sample / project - %s" % (inbound["company"] or inbound["name"] or contact_id)),
                  "Qualification", now_iso()))
    audit(conn, "create_opportunity", opp_id, inbound["id"], "linked to contact %s" % contact_id)
    return opp_id


def get_extraction(conn, inbound):
    row = conn.execute("SELECT * FROM extractions WHERE inbound_id=?", (inbound["id"],)).fetchone()
    if row:
        return json.loads(row["payload_json"])
    ext = extractor_mod.extract(conn, inbound)
    conn.execute("INSERT OR REPLACE INTO extractions VALUES (?,?,?)",
                 (inbound["id"], dumps(ext), ext["confidence"]))
    return ext


# ---------------------------------------------------------------- samples
def create_sample_order(conn, inbound, ext, contact_id, account_id, cap=None):
    lines = ext["lines"][:cap] if cap else ext["lines"]
    if not lines:
        add_review(conn, inbound["id"], "EXTRACTION", "NO_FABRIC_FOUND",
                   "No fabric could be extracted from the message.", "Edit extraction manually, then re-process.")
        return None
    blocked = []
    for l in lines:
        item = conn.execute("SELECT status FROM erp_items WHERE sku=?", (l["sku"],)).fetchone()
        if item and item["status"] == "Discontinued":
            blocked.append(l)
    if blocked:
        add_review(conn, inbound["id"], "SAMPLE_BLOCKED", "DISCONTINUED_NO_SAMPLES",
                   "Discontinued SKU(s) cannot be sampled: %s" % ", ".join(b["sku"] for b in blocked),
                   "Contact customer: offer remaining-stock quote instead of a swatch.")
        audit(conn, "sample_blocked", inbound["id"], inbound["id"],
              "discontinued sample refused: %s" % ", ".join(b["sku"] for b in blocked))
        return None
    opp_id = find_or_create_opportunity(conn, contact_id, account_id, inbound)
    order_id = _next_id(conn, "erp_sample_orders", "SO-")
    oos = [l["sku"] for l in lines if rules.total_available(conn, l["sku"]) <= 0]
    notes = ("Out-of-stock swatch flagged: %s" % ", ".join(oos)) if oos else ""
    conn.execute("INSERT INTO erp_sample_orders VALUES (?,?,?,?,?,?,?)",
                 (order_id, opp_id, contact_id, inbound["id"], "Placed", notes, now_iso()))
    for l in lines:
        conn.execute("INSERT INTO erp_sample_order_lines VALUES (?,?,?,?)",
                     (order_id, l["sku"], l["fabric"], l["color"]))
    conn.execute("UPDATE crm_opportunities SET stage='Sample Sent' WHERE id=?", (opp_id,))
    audit(conn, "create_sample_order", order_id, inbound["id"],
          "%d swatch(es): %s" % (len(lines), ", ".join(l["sku"] for l in lines)))
    return order_id


# ---------------------------------------------------------------- quotes
def _release_quote_reservations(conn, quote_id, new_status):
    rows = conn.execute("SELECT * FROM erp_reservations WHERE quote_id=? AND status='active'", (quote_id,)).fetchall()
    for r in rows:
        conn.execute("UPDATE erp_lots SET yards_reserved = yards_reserved - ? WHERE sku=? AND dye_lot=?",
                     (r["qty_yds"], r["sku"], r["dye_lot"]))
        conn.execute("UPDATE erp_reservations SET status=? WHERE id=?", (new_status, r["id"]))
    return len(rows)


def create_quote(conn, inbound, ext, contact_id, account_id):
    opp_id = find_or_create_opportunity(conn, contact_id, account_id, inbound, title="Quote - %s" % (inbound["company"] or inbound["name"]))
    trade = rules.is_trade(conn, account_id)
    _, valid_until, hold_until = rules.quote_dates()

    # Supersede prior live quote on this opportunity (EML-021 updates EML-014)
    prior = conn.execute(
        "SELECT * FROM crm_quotes WHERE opportunity_id=? AND status NOT IN ('superseded') ORDER BY version DESC LIMIT 1",
        (opp_id,)).fetchone()
    version, supersedes = 1, None
    if prior and ext.get("update_hint"):
        released = _release_quote_reservations(conn, prior["id"], "superseded")
        conn.execute("UPDATE crm_quotes SET status='superseded' WHERE id=?", (prior["id"],))
        version, supersedes = prior["version"] + 1, prior["id"]
        audit(conn, "supersede_quote", prior["id"], inbound["id"],
              "v%s superseded by new quote; released %d reservation(s) first (no double-hold)" % (prior["version"], released))

    quote_id = _next_id(conn, "crm_quotes", "Q-")
    conn.execute("INSERT INTO crm_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (quote_id, opp_id, contact_id, inbound["id"], version, supersedes,
                  "draft", ext["intent"], 0, valid_until, now_iso()))

    total, line_rows, any_short = 0.0, [], False
    cur = conn.cursor()
    for l in ext["lines"]:
        item = conn.execute("SELECT * FROM erp_items WHERE sku=?", (l["sku"],)).fetchone()
        qty = l["qty_yds"]
        avail = rules.total_available(conn, l["sku"])
        unit, cut, line_total = rules.price_line(item["list_price"], qty or 0, trade)
        warnings = []
        if qty is None:
            warnings.append("quantity not extracted - needs confirmation")
        elif avail < qty:
            warnings.append("shortfall: need %s yd, available %s yd" % (qty, avail))
            any_short = True
        if item["status"] != "Active":
            warnings.append("SKU status: %s" % item["status"])
            if avail <= 0:
                any_short = True
        elif avail <= 0:
            warnings.append("zero stock")
            any_short = True
        cur.execute("INSERT INTO crm_quote_lines(quote_id,sku,qty_yds,raw_qty,raw_unit,unit_price,cut_fee,line_total,available_yds,warning)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (quote_id, l["sku"], qty, l["raw_qty"], l["raw_unit"], unit, cut, line_total, avail,
                     "; ".join(warnings) or None))
        line_rows.append({"id": cur.lastrowid, "sku": l["sku"], "qty": qty, "avail": avail, "item_status": item["status"]})
        total += line_total
    conn.execute("UPDATE crm_quotes SET total=? WHERE id=?", (round(total, 2), quote_id))
    audit(conn, "create_quote", quote_id, inbound["id"],
          "v%d total $%.2f trade=%s intent=%s" % (version, total, trade, ext["intent"]))

    # Reservation decision
    intent = ext["intent"]
    if intent in ("budgetary",):
        status = "budgetary_no_hold"
        audit(conn, "quote_no_reservation", quote_id, inbound["id"], "budgetary intent: priced, deliberately not reserved")
    elif intent in ("inquiry",):
        status = "priced_no_hold"
        audit(conn, "quote_no_reservation", quote_id, inbound["id"], "price inquiry: no hold requested")
        if any_short:
            add_review(conn, inbound["id"], "QUOTE_EXCEPTION", "STOCK_EXCEPTION",
                       "Quote %s has unavailable/short stock." % quote_id, "Confirm lead time / alternatives with customer.")
    elif any_short:
        status = "shortfall_review"
        add_review(conn, inbound["id"], "RESERVATION_SHORTFALL", "INSUFFICIENT_AVAILABLE",
                   "Quote %s cannot be fully reserved from available dye lots." % quote_id,
                   "Offer partial reservation + backorder/split shipment, or hold pending customer decision.")
        audit(conn, "reservation_blocked", quote_id, inbound["id"], "shortfall: no auto-reservation")
    else:
        status = "held"
        for lr in line_rows:
            allocs, warn = rules.allocate(conn, lr["sku"], lr["qty"])
            if allocs is None:
                status = "shortfall_review"
                break
            for lot, q in allocs:
                rid = _next_id(conn, "erp_reservations", "RES-")
                conn.execute("INSERT INTO erp_reservations VALUES (?,?,?,?,?,?,?,?,?)",
                             (rid, quote_id, lr["id"], lr["sku"], lot, q, "active", hold_until, now_iso()))
                conn.execute("UPDATE erp_lots SET yards_reserved = yards_reserved + ? WHERE sku=? AND dye_lot=?",
                             (q, lr["sku"], lot))
            if warn:
                conn.execute("UPDATE crm_quote_lines SET warning=? WHERE id=?", (warn, lr["id"]))
                audit(conn, "reservation_split_warning", quote_id, inbound["id"], "%s: %s" % (lr["sku"], warn))
        audit(conn, "create_reservation", quote_id, inbound["id"], "stock held until %s" % hold_until)
    conn.execute("UPDATE crm_quotes SET status=? WHERE id=?", (status, quote_id))
    conn.execute("UPDATE crm_opportunities SET stage='Quote Sent' WHERE id=?", (opp_id,))
    return quote_id


# ---------------------------------------------------------------- main entry
def set_outcome(conn, inbound, status, outcome, reason):
    conn.execute("UPDATE raw_inbound SET processed_status=?, outcome=?, reason_code=? WHERE id=?",
                 (status, outcome, reason, inbound["id"]))


def process_inbound(conn, inbound_id, actor="system"):
    inbound = conn.execute("SELECT * FROM raw_inbound WHERE id=?", (inbound_id,)).fetchone()
    if not inbound:
        return {"error": "not found"}
    if inbound["processed_status"] not in ("unprocessed", None):
        return {"status": "already_processed", "outcome": inbound["outcome"]}
    ext = get_extraction(conn, inbound)
    m = matcher_mod.match(conn, inbound)
    conn.execute("INSERT INTO match_decisions(inbound_id,tier,contact_id,account_id,outcome,signals) VALUES (?,?,?,?,?,?)",
                 (inbound_id, m["tier"], m["contact"]["id"] if m.get("contact") else None,
                  m.get("account_id"), m["outcome"], "; ".join(m.get("signals") or [])))
    outcome, reason = rules.qualify(inbound, m)
    is_quote = ext["is_quote"]
    audit(conn, "process", inbound_id, inbound_id,
          "match=%s qualify=%s/%s lines=%s" % (m["outcome"], outcome, reason, [l["sku"] for l in ext["lines"]]), actor=actor)

    if outcome == "DISCARDED":
        set_outcome(conn, inbound, "discarded", outcome, reason)
        conn.commit()
        return {"status": "discarded", "reason": reason}
    if outcome == "BLOCKED":
        set_outcome(conn, inbound, "blocked", outcome, reason)
        audit(conn, "blocked_do_not_sell", inbound_id, inbound_id, "no sample, no pricing released")
        conn.commit()
        return {"status": "blocked", "reason": reason}

    if not is_quote:
        # ---------------- sample path ----------------
        if outcome == "NEEDS_REVIEW" and reason == "POSSIBLE_DUPLICATE":
            c = m["contact"]
            add_review(conn, inbound_id, "MERGE_PROPOSAL", reason,
                       "Possible duplicate of %s (%s, %s). Signals: %s" % (c["name"], c["id"], c["email"], "; ".join(m["signals"])),
                       "Confirm merge (sample joins existing opportunity) or create a separate record.")
            set_outcome(conn, inbound, "needs_review", outcome, reason)
            conn.commit()
            return {"status": "needs_review", "reason": reason}
        trade_guess = outcome == "QUALIFIED"
        contact_id, account_id = ensure_contact(conn, inbound, m, trade_guess=trade_guess)
        if outcome == "NEEDS_REVIEW":
            add_review(conn, inbound_id, "QUALIFICATION", reason,
                       "Ambiguous trade status for %s (%s)." % (inbound["name"], inbound["email"]),
                       "Qualify manually (resale cert / portfolio) or decline free samples.")
            set_outcome(conn, inbound, "needs_review", outcome, reason)
            conn.commit()
            return {"status": "needs_review", "reason": reason}
        if outcome == "NOT_QUALIFIED":
            set_outcome(conn, inbound, "not_qualified", outcome, reason)
            audit(conn, "lead_not_qualified", contact_id, inbound_id, reason)
            conn.commit()
            return {"status": "not_qualified", "reason": reason}
        # qualified sample guards
        if len(ext["lines"]) > 10:
            add_review(conn, inbound_id, "OVER_SAMPLE_LIMIT", "MAX_10_SAMPLES",
                       "Request expands to %d samples (max 10 per request)." % len(ext["lines"]),
                       "Approve to send first 10, or trim with the customer.")
            set_outcome(conn, inbound, "needs_review", outcome, "MAX_10_SAMPLES")
            conn.commit()
            return {"status": "needs_review", "reason": "MAX_10_SAMPLES"}
        order_id = create_sample_order(conn, inbound, ext, contact_id, account_id)
        if order_id is None:
            set_outcome(conn, inbound, "needs_review", outcome, "SAMPLE_BLOCKED")
        else:
            set_outcome(conn, inbound, "processed", outcome, reason)
        conn.commit()
        return {"status": "processed", "reason": reason, "sample_order": order_id}
    else:
        # ---------------- quote path ----------------
        # trade status from matched account if there is one; else from prior qualification
        contact_id, account_id = (None, None)
        if m.get("contact") is not None:
            contact_id, account_id = m["contact"]["id"], m["account_id"]
        else:
            contact_id, account_id = ensure_contact(conn, inbound, m, trade_guess=False)
        quote_id = create_quote(conn, inbound, ext, contact_id, account_id)
        q = conn.execute("SELECT status FROM crm_quotes WHERE id=?", (quote_id,)).fetchone()
        status = "needs_review" if q["status"] in ("shortfall_review",) else "processed"
        set_outcome(conn, inbound, status, "QUOTED", q["status"])
        conn.commit()
        return {"status": status, "reason": q["status"], "quote": quote_id}


def process_all(conn, actor="system"):
    rows = conn.execute("SELECT id FROM raw_inbound ORDER BY received, id").fetchall()
    return {r["id"]: process_inbound(conn, r["id"], actor=actor) for r in rows}


# ---------------------------------------------------------------- review resolution
def resolve_review(conn, review_id, action, actor="human"):
    rev = conn.execute("SELECT * FROM review_queue WHERE id=?", (review_id,)).fetchone()
    if not rev or rev["status"] != "pending":
        return {"error": "review not pending"}
    inbound = conn.execute("SELECT * FROM raw_inbound WHERE id=?", (rev["inbound_id"],)).fetchone()
    ext = get_extraction(conn, inbound)

    def close(resolution, inbound_status=None, outcome=None, reason=None):
        conn.execute("UPDATE review_queue SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
                     (resolution, now_iso(), review_id))
        if inbound_status:
            set_outcome(conn, inbound, inbound_status, outcome or inbound["outcome"], reason or inbound["reason_code"])
        audit(conn, "review_resolved", "review-%s" % review_id, inbound["id"], "%s: %s" % (action, resolution), actor=actor)
        conn.commit()
        return {"status": "resolved", "resolution": resolution}

    if rev["kind"] == "MERGE_PROPOSAL":
        if action in ("approve", "merge"):
            m = matcher_mod.match(conn, inbound)
            c = m["contact"]
            if inbound["email"]:
                conn.execute("INSERT OR IGNORE INTO contact_emails VALUES (?,?,0)", (c["id"], inbound["email"].lower()))
            order_id = create_sample_order(conn, inbound, ext, c["id"], c["account_id"])
            audit(conn, "merge_contacts", c["id"], inbound["id"],
                  "alias %s added; sample joined existing opportunity" % inbound["email"], actor=actor)
            return close("merged into %s; sample order %s" % (c["id"], order_id), "processed", "QUALIFIED", "MERGED_DUPLICATE")
        return close("kept separate (no sample created by reviewer)", "processed", "QUALIFIED", "KEPT_SEPARATE")
    if rev["kind"] == "QUALIFICATION":
        if action == "approve":
            m = matcher_mod.match(conn, inbound)
            contact_id, account_id = ensure_contact(conn, inbound, m, trade_guess=True)
            order_id = create_sample_order(conn, inbound, ext, contact_id, account_id)
            return close("qualified by reviewer; sample order %s" % order_id, "processed", "QUALIFIED", "HUMAN_QUALIFIED")
        return close("declined by reviewer", "not_qualified", "NOT_QUALIFIED", "HUMAN_DECLINED")
    if rev["kind"] == "OVER_SAMPLE_LIMIT":
        if action == "approve":
            m = matcher_mod.match(conn, inbound)
            contact_id, account_id = ensure_contact(conn, inbound, m, trade_guess=True)
            order_id = create_sample_order(conn, inbound, ext, contact_id, account_id, cap=10)
            return close("approved: first 10 samples sent (%s)" % order_id, "processed", "QUALIFIED", "HUMAN_CAPPED_10")
        return close("declined by reviewer", "not_qualified", "NOT_QUALIFIED", "HUMAN_DECLINED")
    if rev["kind"] == "RESERVATION_SHORTFALL":
        if action == "approve":
            q = conn.execute("SELECT * FROM crm_quotes WHERE inbound_id=? ORDER BY version DESC LIMIT 1", (inbound["id"],)).fetchone()
            _, _, hold_until = rules.quote_dates()
            reserved = []
            lines = conn.execute("SELECT * FROM crm_quote_lines WHERE quote_id=?", (q["id"],)).fetchall()
            for lr in lines:
                avail = rules.total_available(conn, lr["sku"])
                take_qty = min(lr["qty_yds"] or 0, avail)
                if take_qty <= 0:
                    continue
                allocs, warn = rules.allocate(conn, lr["sku"], take_qty)
                if allocs:
                    for lot, qy in allocs:
                        rid = _next_id(conn, "erp_reservations", "RES-")
                        conn.execute("INSERT INTO erp_reservations VALUES (?,?,?,?,?,?,?,?,?)",
                                     (rid, q["id"], lr["id"], lr["sku"], lot, qy, "active", hold_until, now_iso()))
                        conn.execute("UPDATE erp_lots SET yards_reserved=yards_reserved+? WHERE sku=? AND dye_lot=?",
                                     (qy, lr["sku"], lot))
                    reserved.append("%s:%s" % (lr["sku"], take_qty))
            conn.execute("UPDATE crm_quotes SET status='partially_held' WHERE id=?", (q["id"],))
            return close("partial reservation placed: %s" % ", ".join(reserved), "processed", "QUOTED", "PARTIALLY_HELD")
        return close("left unreserved by reviewer", "processed", "QUOTED", "LEFT_UNRESERVED")
    # SAMPLE_BLOCKED / QUOTE_EXCEPTION / EXTRACTION etc: acknowledgement
    return close("acknowledged by reviewer (%s)" % action, inbound["processed_status"], inbound["outcome"], inbound["reason_code"])
