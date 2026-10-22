"""Perennials Sample-to-Quote PoC -- single-file FastAPI UI.

Run:  python -m perennials.seed && uvicorn app:app --reload --port 8000
"""
import html
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402

from perennials import pipeline, seed as seed_mod  # noqa: E402
from perennials.db import connect  # noqa: E402
from perennials.rules import available_by_lot, total_available  # noqa: E402

app = FastAPI(title="Perennials Sample-to-Quote PoC")

CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f7f5f0;color:#222}
header{background:#23404d;color:#fff;padding:14px 22px} header a{color:#cfe3ea;text-decoration:none;margin-right:16px}
header b{color:#fff;margin-right:22px} main{max-width:1150px;margin:0 auto;padding:20px}
table{border-collapse:collapse;width:100%;background:#fff;margin:10px 0 22px}
th,td{border:1px solid #ddd;padding:7px 9px;font-size:13px;vertical-align:top;text-align:left}
th{background:#eef2f3} .card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 16px;margin:12px 0}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:#e8e8e8}
.ok{background:#d9ead3}.bad{background:#f4cccc}.warn{background:#fce5cd}.info{background:#cfe2f3}
button{background:#23404d;color:#fff;border:0;padding:7px 13px;border-radius:5px;cursor:pointer;margin:2px}
button.alt{background:#6b7f88} a{color:#1a5a78} .muted{color:#777;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px}.stat b{font-size:22px;display:block}
pre{white-space:pre-wrap;background:#f4f4f4;padding:10px;border-radius:6px;font-size:12px}
"""


def page(title, body):
    return HTMLResponse("<html><head><title>%s</title><style>%s</style></head><body>"
                        "<header><b>Perennials — Sample to Quote</b>"
                        "<a href='/'>Dashboard</a><a href='/inbound'>Inbound</a><a href='/review'>Review queue</a>"
                        "<a href='/catalog'>Catalog (ERP)</a><a href='/crm'>CRM</a><a href='/quotes'>Quotes</a>"
                        "<a href='/audit'>Audit log</a></header><main><h2>%s</h2>%s</main></body></html>"
                        % (title, CSS, title, body))


def pill(status):
    cls = "ok" if status in ("processed", "QUALIFIED", "Customer", "Active", "held", "Placed") else \
        "bad" if status in ("blocked", "discarded", "not_qualified", "Do Not Sell") else \
        "warn" if status in ("needs_review", "shortfall_review", "Prospect") else "info"
    return "<span class='pill %s'>%s</span>" % (cls, html.escape(str(status or "-")))


def get_conn():
    conn = connect()
    # auto-seed if empty
    try:
        n = conn.execute("SELECT COUNT(*) c FROM raw_inbound").fetchone()["c"]
    except Exception:
        n = 0
    if n == 0:
        conn.close()
        seed_mod.seed()
        conn = connect()
    return conn


@app.get("/", response_class=HTMLResponse)
def dashboard():
    conn = get_conn()
    counts = {}
    for label, sql in [
        ("Inbound requests", "SELECT COUNT(*) c FROM raw_inbound"),
        ("Processed", "SELECT COUNT(*) c FROM raw_inbound WHERE processed_status='processed'"),
        ("Needs review", "SELECT COUNT(*) c FROM review_queue WHERE status='pending'"),
        ("Sample orders (ERP)", "SELECT COUNT(*) c FROM erp_sample_orders"),
        ("Opportunities (CRM)", "SELECT COUNT(*) c FROM crm_opportunities"),
        ("Quotes (CRM)", "SELECT COUNT(*) c FROM crm_quotes"),
        ("Active reservations (ERP)", "SELECT COUNT(*) c FROM erp_reservations WHERE status='active'"),
        ("Blocked / discarded", "SELECT COUNT(*) c FROM raw_inbound WHERE processed_status IN ('blocked','discarded')"),
    ]:
        counts[label] = conn.execute(sql).fetchone()["c"]
    stats = "".join("<div class='stat'><b>%s</b>%s</div>" % (v, html.escape(k)) for k, v in counts.items())
    body = ("<div class='grid'>%s</div>"
            "<div class='card'><b>Flow:</b> Website/email &rarr; extract (LLM seam) &rarr; match (CRM) &rarr; "
            "qualify (rules) &rarr; sample order (ERP) + Opportunity (CRM) &rarr; quote (rules pricing) &rarr; "
            "reservation (ERP dye lots). Anything ambiguous lands in the review queue.</div>"
            "<form method='post' action='/process-all'><button>Process all inbound (time order)</button></form> "
            "<form method='post' action='/reset' style='display:inline'><button class='alt'>Reset demo (reseed)</button></form>"
            % stats)
    conn.close()
    return page("Dashboard", body)


@app.get("/inbound", response_class=HTMLResponse)
def inbound_list():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM raw_inbound ORDER BY received, id").fetchall()
    trs = ""
    for r in rows:
        trs += ("<tr><td><a href='/inbound/%s'>%s</a></td><td>%s</td><td>%s</td><td>%s<br><span class='muted'>%s</span></td>"
                "<td>%s</td><td>%s</td><td>%s</td><td><form method='post' action='/process/%s'><button>Process</button></form></td></tr>"
                % (r["id"], r["id"], r["type"], r["received"], html.escape(r["name"] or "-"),
                   html.escape(r["email"] or ""), html.escape((r["message"] or "")[:110]) + "...",
                   pill(r["processed_status"]), pill(r["reason_code"] or r["outcome"] or ""), r["id"]))
    conn.close()
    return page("Inbound requests", "<table><tr><th>ID</th><th>Type</th><th>Received</th><th>From</th><th>Message</th><th>Status</th><th>Reason</th><th></th></tr>%s</table>" % trs)


@app.get("/inbound/{inbound_id}", response_class=HTMLResponse)
def inbound_detail(inbound_id: str):
    conn = get_conn()
    r = conn.execute("SELECT * FROM raw_inbound WHERE id=?", (inbound_id,)).fetchone()
    if not r:
        return page("Not found", "<p>Unknown inbound id.</p>")
    ext = conn.execute("SELECT * FROM extractions WHERE inbound_id=?", (inbound_id,)).fetchone()
    md = conn.execute("SELECT * FROM match_decisions WHERE inbound_id=? ORDER BY id DESC LIMIT 1", (inbound_id,)).fetchone()
    rev = conn.execute("SELECT * FROM review_queue WHERE inbound_id=?", (inbound_id,)).fetchall()
    body = "<div class='card'><b>%s</b> · %s · %s<br><b>%s</b> (%s) %s<br><pre>%s</pre>Status: %s %s</div>" % (
        r["id"], r["type"], r["channel"], html.escape(r["name"] or "-"), html.escape(r["company"] or "-"),
        html.escape(r["email"] or ""), html.escape(r["message"] or ""), pill(r["processed_status"]), pill(r["reason_code"] or ""))
    if ext:
        body += "<div class='card'><b>Extraction</b> (confidence %s)<pre>%s</pre></div>" % (ext["confidence"], html.escape(ext["payload_json"]))
    if md:
        body += "<div class='card'><b>Match decision</b>: tier %s, outcome %s, contact %s, account %s<br><span class='muted'>%s</span></div>" % (
            md["tier"], md["outcome"], md["contact_id"] or "-", md["account_id"] or "-", html.escape(md["signals"] or ""))
    for v in rev:
        body += "<div class='card'><b>Review #%s</b> %s — %s<br>%s<br><i>%s</i> [%s]</div>" % (
            v["id"], v["kind"], v["reason_code"], html.escape(v["detail"] or ""), html.escape(v["recommendation"] or ""), v["status"])
    body += "<form method='post' action='/process/%s'><button>Process this request</button></form>" % inbound_id
    conn.close()
    return page("Inbound %s" % inbound_id, body)


@app.post("/process/{inbound_id}")
def process_one(inbound_id: str):
    conn = get_conn()
    pipeline.process_inbound(conn, inbound_id, actor="human-ui")
    conn.close()
    return RedirectResponse("/inbound/%s" % inbound_id, status_code=303)


@app.post("/process-all")
def process_all():
    conn = get_conn()
    pipeline.process_all(conn, actor="human-ui")
    conn.close()
    return RedirectResponse("/inbound", status_code=303)


@app.post("/reset")
def reset():
    seed_mod.seed()
    return RedirectResponse("/", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review_list():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM review_queue ORDER BY status, id").fetchall()
    trs = ""
    for v in rows:
        actions = ""
        if v["status"] == "pending":
            labels = {"MERGE_PROPOSAL": ("merge", "Keep separate"), "QUALIFICATION": ("approve", "Reject"),
                      "OVER_SAMPLE_LIMIT": ("approve (first 10)", "Reject"), "RESERVATION_SHORTFALL": ("approve partial", "Leave unreserved")}
            ok, no = labels.get(v["kind"], ("Acknowledge", "Dismiss"))
            actions = ("<form method='post' action='/review/%s/approve' style='display:inline'><button>%s</button></form>"
                       "<form method='post' action='/review/%s/reject' style='display:inline'><button class='alt'>%s</button></form>"
                       % (v["id"], ok, v["id"], no))
        trs += "<tr><td>%s</td><td><a href='/inbound/%s'>%s</a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s %s</td></tr>" % (
            v["id"], v["inbound_id"], v["inbound_id"], v["kind"], pill(v["reason_code"]), html.escape(v["detail"] or ""),
            pill(v["status"]), html.escape(v["resolution"] or ""), actions)
    conn.close()
    return page("Human review queue", "<p class='muted'>The system recommends; a person decides. Every resolution is audit-logged.</p>"
                "<table><tr><th>#</th><th>Inbound</th><th>Kind</th><th>Reason</th><th>Detail</th><th>Status</th><th>Action</th></tr>%s</table>" % trs)


@app.post("/review/{review_id}/{action}")
def review_resolve(review_id: int, action: str):
    conn = get_conn()
    pipeline.resolve_review(conn, review_id, action, actor="human-ui")
    conn.close()
    return RedirectResponse("/review", status_code=303)


@app.get("/catalog", response_class=HTMLResponse)
def catalog():
    conn = get_conn()
    items = conn.execute("SELECT * FROM erp_items ORDER BY sku").fetchall()
    trs = ""
    for it in items:
        lots = available_by_lot(conn, it["sku"])
        lot_txt = "<br>".join("%s: %s on hand − %s res = <b>%s avail</b>" % (l["dye_lot"], l["on_hand"], l["reserved"], l["available"]) for l in lots)
        trs += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>$%s</td><td>%s</td><td><b>%s</b></td></tr>" % (
            it["sku"], html.escape(it["fabric"]), html.escape(it["color"]), pill(it["status"]), it["list_price"], lot_txt, total_available(conn, it["sku"]))
    rules_rows = conn.execute("SELECT * FROM pricing_rules").fetchall()
    rtxt = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (r["rule"], r["value"], html.escape(r["notes"] or "")) for r in rules_rows)
    conn.close()
    return page("Catalog & inventory (ERP / NetSuite)",
                "<table><tr><th>SKU</th><th>Fabric</th><th>Colour</th><th>Status</th><th>List $/yd</th><th>Dye lots</th><th>Total avail</th></tr>%s</table>"
                "<h3>Pricing rules (ERP config)</h3><table><tr><th>Rule</th><th>Value</th><th>Notes</th></tr>%s</table>" % (trs, rtxt))


@app.get("/crm", response_class=HTMLResponse)
def crm():
    conn = get_conn()
    acc = conn.execute("SELECT * FROM crm_accounts ORDER BY id").fetchall()
    a_trs = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        a["id"], html.escape(a["name"] or ""), html.escape(a["type"] or ""), pill(a["status"]), a["source"]) for a in acc)
    con = conn.execute("SELECT c.*, a.name acc FROM crm_contacts c LEFT JOIN crm_accounts a ON a.id=c.account_id ORDER BY c.id").fetchall()
    c_trs = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        c["id"], html.escape(c["name"] or ""), html.escape(c["email"] or ""), html.escape(c["acc"] or ""), c["source"]) for c in con)
    opp = conn.execute("SELECT * FROM crm_opportunities ORDER BY id").fetchall()
    o_trs = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        o["id"], html.escape(o["title"] or ""), o["contact_id"], pill(o["stage"]), o["source_inbound"]) for o in opp)
    conn.close()
    return page("CRM (Salesforce / HubSpot)",
                "<h3>Accounts</h3><table><tr><th>ID</th><th>Name</th><th>Type</th><th>Status</th><th>Source</th></tr>%s</table>"
                "<h3>Contacts</h3><table><tr><th>ID</th><th>Name</th><th>Email</th><th>Account</th><th>Source</th></tr>%s</table>"
                "<h3>Opportunities</h3><table><tr><th>ID</th><th>Title</th><th>Contact</th><th>Stage</th><th>From</th></tr>%s</table>"
                % (a_trs, c_trs, o_trs))


@app.get("/quotes", response_class=HTMLResponse)
def quotes():
    conn = get_conn()
    rows = conn.execute("SELECT q.*, c.name contact_name FROM crm_quotes q LEFT JOIN crm_contacts c ON c.id=q.contact_id ORDER BY q.id").fetchall()
    trs = ""
    for q in rows:
        trs += "<tr><td><a href='/quotes/%s'>%s</a> v%s</td><td>%s</td><td>%s</td><td>%s</td><td>$%.2f</td><td>%s</td><td>%s</td></tr>" % (
            q["id"], q["id"], q["version"], q["inbound_id"], html.escape(q["contact_name"] or ""), q["intent"], q["total"] or 0, pill(q["status"]), q["valid_until"])
    conn.close()
    return page("Quotes (CRM, priced from ERP)", "<table><tr><th>Quote</th><th>Inbound</th><th>Contact</th><th>Intent</th><th>Total</th><th>Status</th><th>Valid until</th></tr>%s</table>" % trs)


@app.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail(quote_id: str):
    conn = get_conn()
    q = conn.execute("SELECT * FROM crm_quotes WHERE id=?", (quote_id,)).fetchone()
    if not q:
        return page("Not found", "<p>Unknown quote.</p>")
    lines = conn.execute("SELECT l.*, i.fabric, i.color, i.list_price FROM crm_quote_lines l JOIN erp_items i ON i.sku=l.sku WHERE l.quote_id=?", (quote_id,)).fetchall()
    trs = "".join("<tr><td>%s %s %s</td><td>%s %s</td><td>%s</td><td>$%s</td><td>$%s</td><td>$%.2f</td><td>%s</td></tr>" % (
        l["sku"], html.escape(l["fabric"]), html.escape(l["color"]),
        l["raw_qty"] if l["raw_qty"] is not None else "?", html.escape(l["raw_unit"] or ""),
        l["qty_yds"] if l["qty_yds"] is not None else "?", l["unit_price"], l["cut_fee"], l["line_total"] or 0,
        html.escape(l["warning"] or "")) for l in lines)
    res = conn.execute("SELECT * FROM erp_reservations WHERE quote_id=?", (quote_id,)).fetchall()
    r_trs = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s yd</td><td>%s</td><td>%s</td></tr>" % (
        r["id"], r["sku"], r["dye_lot"], r["qty_yds"], pill(r["status"]), r["expires_at"]) for r in res)
    body = ("<div class='card'><b>%s</b> v%s %s · Opportunity %s · Inbound %s · Intent %s · Valid until %s<br>"
            "<b>Total: $%.2f</b> %s</div>"
            "<table><tr><th>Item</th><th>Asked</th><th>Yards</th><th>Unit $</th><th>Cut fee</th><th>Line total</th><th>Warnings</th></tr>%s</table>"
            "<h3>Reservations (ERP)</h3><table><tr><th>ID</th><th>SKU</th><th>Dye lot</th><th>Qty</th><th>Status</th><th>Expires</th></tr>%s</table>"
            % (q["id"], q["version"], pill(q["status"]), q["opportunity_id"], q["inbound_id"], q["intent"], q["valid_until"], q["total"] or 0,
               ("(supersedes %s)" % q["supersedes_id"]) if q["supersedes_id"] else "", trs, r_trs or "<tr><td colspan='6'>No reservations</td></tr>"))
    conn.close()
    return page("Quote %s" % quote_id, body)


@app.get("/audit", response_class=HTMLResponse)
def audit():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    trs = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        r["id"], r["ts"], r["actor"], r["action"], html.escape(r["entity"] or ""), html.escape(r["detail"] or "")) for r in rows)
    conn.close()
    return page("Audit log", "<table><tr><th>#</th><th>Time (UTC)</th><th>Actor</th><th>Action</th><th>Entity</th><th>Detail</th></tr>%s</table>" % trs)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
