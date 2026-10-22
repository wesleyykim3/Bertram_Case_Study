"""SQLite schema + helpers. One DB file, namespaced tables per system of record."""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("PERENNIALS_DB", os.path.join(os.path.dirname(__file__), "..", "perennials.db"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS erp_items (
    sku TEXT PRIMARY KEY, fabric TEXT, color TEXT, status TEXT, list_price REAL
);
CREATE TABLE IF NOT EXISTS erp_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT, dye_lot TEXT,
    yards_on_hand REAL, yards_reserved REAL
);
CREATE TABLE IF NOT EXISTS pricing_rules (
    rule TEXT PRIMARY KEY, value TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS crm_accounts (
    id TEXT PRIMARY KEY, name TEXT, type TEXT, status TEXT, source TEXT DEFAULT 'seed'
);
CREATE TABLE IF NOT EXISTS crm_contacts (
    id TEXT PRIMARY KEY, account_id TEXT, name TEXT, email TEXT, source TEXT DEFAULT 'seed'
);
CREATE TABLE IF NOT EXISTS contact_emails (
    contact_id TEXT, email TEXT, is_primary INTEGER DEFAULT 0, PRIMARY KEY (contact_id, email)
);
CREATE TABLE IF NOT EXISTS raw_inbound (
    id TEXT PRIMARY KEY, type TEXT, channel TEXT, received TEXT,
    name TEXT, company TEXT, email TEXT, message TEXT,
    processed_status TEXT DEFAULT 'unprocessed', outcome TEXT, reason_code TEXT
);
CREATE TABLE IF NOT EXISTS extractions (
    inbound_id TEXT PRIMARY KEY, payload_json TEXT, confidence REAL
);
CREATE TABLE IF NOT EXISTS match_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, inbound_id TEXT, tier TEXT,
    contact_id TEXT, account_id TEXT, outcome TEXT, signals TEXT
);
CREATE TABLE IF NOT EXISTS crm_opportunities (
    id TEXT PRIMARY KEY, account_id TEXT, contact_id TEXT, source_inbound TEXT,
    title TEXT, stage TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS erp_sample_orders (
    id TEXT PRIMARY KEY, opportunity_id TEXT, contact_id TEXT, inbound_id TEXT,
    status TEXT, notes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS erp_sample_order_lines (
    order_id TEXT, sku TEXT, fabric TEXT, color TEXT
);
CREATE TABLE IF NOT EXISTS crm_quotes (
    id TEXT PRIMARY KEY, opportunity_id TEXT, contact_id TEXT, inbound_id TEXT,
    version INTEGER, supersedes_id TEXT, status TEXT, intent TEXT,
    total REAL, valid_until TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS crm_quote_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, quote_id TEXT, sku TEXT, qty_yds REAL,
    raw_qty REAL, raw_unit TEXT, unit_price REAL, cut_fee REAL, line_total REAL,
    available_yds REAL, warning TEXT
);
CREATE TABLE IF NOT EXISTS erp_reservations (
    id TEXT PRIMARY KEY, quote_id TEXT, quote_line_id INTEGER, sku TEXT, dye_lot TEXT,
    qty_yds REAL, status TEXT, expires_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, inbound_id TEXT, kind TEXT, reason_code TEXT,
    detail TEXT, recommendation TEXT, status TEXT DEFAULT 'pending',
    resolution TEXT, created_at TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT,
    entity TEXT, inbound_id TEXT, detail TEXT
);
"""


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def audit(conn, action, entity, inbound_id, detail, actor="system"):
    conn.execute(
        "INSERT INTO audit_log(ts,actor,action,entity,inbound_id,detail) VALUES (?,?,?,?,?,?)",
        (now_iso(), actor, action, entity, inbound_id, detail),
    )


def add_review(conn, inbound_id, kind, reason, detail, recommendation):
    # don't duplicate a pending review of same kind for same inbound
    existing = conn.execute(
        "SELECT id FROM review_queue WHERE inbound_id=? AND kind=? AND status='pending'",
        (inbound_id, kind),
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO review_queue(inbound_id,kind,reason_code,detail,recommendation,created_at) VALUES (?,?,?,?,?,?)",
        (inbound_id, kind, reason, detail, recommendation, now_iso()),
    )
    return cur.lastrowid


def dumps(obj):
    return json.dumps(obj, indent=2, default=str)
