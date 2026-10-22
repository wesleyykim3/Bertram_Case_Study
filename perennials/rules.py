"""Qualification + pricing + dye-lot reservation. All deterministic.

Discount assumption (stated in README/DESIGN): stacking is additive,
unit = list * (1 - trade - volume). Cut fee $25/line under 5 yd.
Quote valid 30 days; reservation hold 14 days.
"""
from datetime import datetime, timedelta, timezone

FREE_DOMAINS = ("gmail.", "yahoo.", "hotmail.", "outlook.", "aol.")
TRADE_WORDS = ("interior", "upholster", "architect", "hospitality", "hotel", "marine", "yacht",
               "studio", "design", "specif", "restaurant", "lobby", "guest room", "client",
               "project", "refit", "banquette", "refresh")


def _now():
    return datetime.now(timezone.utc)


def qualify(inbound, m):
    """Return (outcome, reason_code). Outcome in QUALIFIED/BLOCKED/DISCARDED/NOT_QUALIFIED/NEEDS_REVIEW."""
    msg = (inbound["message"] or "").lower()
    email = (inbound["email"] or "").lower()

    if "seo" in msg or "backlink" in msg or "rank-boost" in email:
        return "DISCARDED", "SPAM"
    if m.get("account_status") == "Do Not Sell":
        return "BLOCKED", "DO_NOT_SELL"
    if m.get("outcome") == "auto_link" and (m.get("account_type") or "").startswith("Trade"):
        return "QUALIFIED", ("EXISTING_CUSTOMER" if m.get("account_status") == "Customer" else "EXISTING_PROSPECT")
    if m.get("outcome") == "proposal":
        return "NEEDS_REVIEW", "POSSIBLE_DUPLICATE"

    # --- new person: judge from signals ---
    if "student" in email or "student" in msg or "studio project" in msg:
        return "NOT_QUALIFIED", "STUDENT"
    is_free = any(d in email for d in FREE_DOMAINS)
    has_company = bool((inbound["company"] or "").strip())
    trade_hits = [w for w in TRADE_WORDS if w in msg or w in (inbound["company"] or "").lower()]

    if is_free and not has_company:
        # ambiguous self-described designer on free email (WEB-012) vs plain consumer (WEB-002)
        if "designer" in msg and ("firm" in msg or "own" in msg):
            return "NEEDS_REVIEW", "AMBIGUOUS_TRADE"
        return "NOT_QUALIFIED", "CONSUMER"
    if has_company and trade_hits:
        return "QUALIFIED", "NEW_TRADE"
    if has_company and not is_free:
        return "QUALIFIED", "NEW_TRADE"
    return "NEEDS_REVIEW", "AMBIGUOUS_TRADE"


def is_trade(conn, account_id):
    if account_id:
        a = conn.execute("SELECT type,status FROM crm_accounts WHERE id=?", (account_id,)).fetchone()
        if a and (a["type"] or "").startswith("Trade"):
            return True
    return False


def available_by_lot(conn, sku):
    rows = conn.execute("SELECT * FROM erp_lots WHERE sku=? ORDER BY id", (sku,)).fetchall()
    return [{"dye_lot": r["dye_lot"], "on_hand": r["yards_on_hand"], "reserved": r["yards_reserved"],
             "available": r["yards_on_hand"] - r["yards_reserved"]} for r in rows]


def total_available(conn, sku):
    return sum(l["available"] for l in available_by_lot(conn, sku))


def price_line(list_price, qty_yds, trade):
    trade_disc = 0.15 if trade else 0.0
    if qty_yds is None:
        qty_yds = 0
    vol = 0.10 if qty_yds >= 100 else (0.05 if qty_yds >= 50 else 0.0)
    unit = round(list_price * (1 - trade_disc - vol), 2)
    cut = 25.0 if 0 < qty_yds < 5 else 0.0
    return unit, cut, round(unit * qty_yds + cut, 2)


def allocate(conn, sku, qty):
    """Single-lot preferred (smallest lot that fits), else FIFO split. Returns (allocs, warning) or (None, reason)."""
    lots = [l for l in available_by_lot(conn, sku) if l["available"] > 0]
    total = sum(l["available"] for l in lots)
    if qty is None or total < qty:
        return None, "shortfall: need %s, available %s" % (qty, total)
    fits = [l for l in lots if l["available"] >= qty]
    if fits:
        pick = min(fits, key=lambda l: l["available"])
        return [(pick["dye_lot"], qty)], None
    allocs, remaining = [], qty
    for l in lots:
        take = min(l["available"], remaining)
        allocs.append((l["dye_lot"], take))
        remaining -= take
        if remaining <= 0:
            break
    return allocs, "dye-lot split across %s lots (colour variation risk)" % len(allocs)


def quote_dates():
    n = _now()
    return n.strftime("%Y-%m-%d"), (n + timedelta(days=30)).strftime("%Y-%m-%d"), (n + timedelta(days=14)).strftime("%Y-%m-%d")
