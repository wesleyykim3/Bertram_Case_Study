# Design note — Perennials Sample-to-Quote PoC

## 1. Architecture

One FastAPI app + SQLite, seeded from the provided workbook. Tables are
namespaced by system of record so the PoC's seams are the production seams:

| PoC | Real system | Owns (source of truth for) |
|---|---|---|
| `raw_inbound` | WooCommerce form / email ingest | The message exactly as received (immutable; re-extract without re-ingesting) |
| `crm_accounts`, `crm_contacts`, `crm_opportunities`, `crm_quotes` | Salesforce / HubSpot | Who the customer is, relationship/pipeline state, quote status |
| `erp_items`, `erp_lots`, `erp_sample_orders`, `erp_reservations` | NetSuite | SKUs, list price, dye-lot stock, orders, inventory holds |
| `pricing_rules` | NetSuite config, read by the quote engine | Discounts, tiers, cut fee, validity/hold periods |
| `extractions`, `match_decisions`, `review_queue`, `audit_log` | PoC (new) | Derived extraction (regenerable), identity decisions, human judgements, audit trail |

Pipeline per inbound (processed in received-time order; inbound ID is the
idempotency key, so replays never duplicate):

**Extract → Match → Qualify → Sample/Opportunity → Quote → Reserve-or-flag.**

## 2. Who decides what

- **Extraction (deterministic parser; no LLM in the PoC):** free text → {fabric, colour, qty, unit, intent} with
  confidence, via an alias/regex parser behind the `extractor.extract` interface.
  An LLM could replace that parser behind the same interface in production, but
  none is used here. Its output is
  validated against the ERP catalog — an invented SKU cannot pass — and it is
  never allowed to price, qualify, or promise stock.
- **Deterministic rules (money & stock):** spam discard; **Do-Not-Sell hard
  block** (checked before any trade signal, so a competitor can never receive
  samples or the trade price list); trade qualification with reason codes;
  max-10-samples and discontinued guards; pricing (additive stacking:
  trade 15%, volume +5%/+10% per line, $25 cut fee < 5 yd, 30-day validity);
  availability = on hand − reserved **per dye lot**; allocation prefers a
  single lot (smallest that fits), splits FIFO with a customer-visible
  dye-variation warning; 14-day holds.
- **Human (judgement):** a review queue for ambiguous trade status (student /
  brand-new firm / consumer), duplicate-merge proposals, over-limit sample
  requests, and shortfall/backorder offers (partial hold, split shipment).
  The system recommends with a reason code; the human clicks; both are logged.

## 3. Identity & versioning

Account and Contact are matched separately: a new person at a known company
becomes a new Contact under that Account, and a generic inbox (`purchasing@`)
can resolve an Account (triggering Do Not Sell) without claiming a person.
Exact email auto-links; fuzzy evidence (same domain + surname + initial, as in
WEB-003) produces a merge proposal. On confirmation the secondary email is
stored as an alias, so the matcher gets smarter. Quote *updates* supersede:
EML-021 releases EML-014's hold in the same step as re-checking, so an
Opportunity never carries two live holds, and availability is judged against
stock including the requester's own returned hold (90 yd vs. 87 available =
short 3, correctly flagged).

## 4. Failure behaviour

Nothing fails silently. Shortfalls (EML-016: 450 yd wanted, 330 available),
zero-stock and backordered SKUs (EML-015 Moss, EML-019 Rope Texture, ETA
2026-11-15), discontinued samples (WEB-008), and budgetary-only intent
(EML-018: priced, deliberately not reserved) each produce a priced quote or a
refusal **plus** a flagged reason and, where a commercial choice exists, a
review-queue item. Every automated decision writes an audit row.

## 5. Mapping to production

- **WooCommerce → Salesforce:** form webhook posts to an ingest endpoint that
  stores the raw payload, then upserts Lead/Contact via the matching ladder
  (native dedupe rules + a custom proposal queue replacing `review_queue`).
- **Salesforce:** qualification ladder as validation rules/Flow with the same
  reason codes; Opportunity created on qualification; Quote (CPQ) attached.
- **NetSuite:** item/lot availability via saved search or RESTlet; sample
  orders and pending-order holds as native transactions; a scheduled job
  expires 14-day holds; CRM↔ERP sync via an outbox pattern keyed on the
  inbound/quote IDs used here.
- **Optional future LLM:** if extraction breadth becomes a problem, a hosted model
  could sit behind `extractor.extract`, JSON-schema constrained,
  with the provided 21 rows (plus adversarial additions) as a regression eval.
  The PoC as submitted uses no LLM.

