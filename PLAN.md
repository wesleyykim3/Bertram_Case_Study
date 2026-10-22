# Perennials Sample-to-Quote — PoC Plan

Timebox: 3–4 hours. Deliverable: runnable PoC + README (<5 min to run) + 1–2 pp design note + AI-usage notes. Optional 3–5 min recording.

## The bet
One thin end-to-end slice across all 6 stages (sample request → qualification → sample order + Opportunity → quote request → price quote → inventory reservation), with a human-review queue in the middle. Deterministic rules own money/inventory/qualification. An LLM (mockable, interface-stubbed) only converts free text to structured fields with confidence. Fallback keyword parser keeps the demo running without any API key.

**Stack (chosen for zero-friction run):** Python + FastAPI + SQLite + server-rendered HTML. Excel seeded via openpyxl. Tables namespaced `crm_*`, `erp_*`, `raw_inbound` to mirror Salesforce / NetSuite / WooCommerce literally.

## Source of truth
| Data | System |
|---|---|
| Accounts, Contacts, Leads, Opportunities, quote status | CRM (Salesforce/HubSpot) |
| SKUs, list price, dye lots, on-hand/reserved, orders, holds | ERP (NetSuite) |
| Raw inbound message | Website/email capture (immutable) |
| Pricing rules | ERP-owned config, read by quote engine |
| Extraction output | Derived/disposable — regenerable |

## Decision split
- **LLM:** fabric/color/qty/unit/intent extraction only, with confidence + evidence span; validated against ERP catalog.
- **Rules (pure functions, tested):** qualification, Do-Not-Sell, spam, sample cap (10), discontinued guard, pricing, availability (On Hand − Reserved per dye lot), lot allocation.
- **Human (review queue):** ambiguous trade status, merge proposals, shortfall/backorder offers, over-limit samples, hold extensions. Every override logged.

## Key assumptions (state in README)
1. Discounts stack additively: `unit = list × (1−trade−volume)`; trade 15% / non-trade 0%; volume +5% (50–99 yd), +10% (100+ yd) per line; $25 cut fee <5 yd; quote 30 days; hold 14 days.
2. Emails are matched as-is (all sample data uses `.example` domains); fuzzy identity match = human merge proposal, never auto-merge.
3. Inbound ID (`WEB-`/`EML-`) is the idempotency key; quote updates supersede (v2 replaces v1, no double reservation).
4. Dye-lot split on one line = warning (not hard block) pending Ops confirmation.

## Build order (3.5 h)
1. **0:00–0:35** Skeleton + Excel seed + catalog/CRM browse.
2. **0:35–1:20** Inbound inbox (21 rows) + extractor (fallback parser first).
3. **1:20–2:05** Matcher + qualification rules w/ reason codes + review queue.
4. **2:05–3:00** Sample orders, quote engine, lot reservation, quote versioning.
5. **3:00–3:30** Tests on all 21 rows, README, design note, AI-usage notes.

Cut line if slipping: LLM call → quote preview polish. Never cut: reason codes, Do-Not-Sell block, shortfall flags, quote versioning.

## Demo script (3–5 min)
WEB-001 happy path → WEB-005 blocked / WEB-007 spam / WEB-003 merge → EML-014 quote+hold w/ lot-split warning → EML-021 supersedes, flags 90 > 87 avail → EML-016 partial (330/450) to review → EML-018 budgetary = priced, not reserved → audit log close.

## Traps checklist (21 rows)
See canvas plan for per-row expected behaviour: WEB-002 consumer, WEB-005 Do Not Sell, WEB-007 spam, WEB-008 discontinued sample, WEB-009/WEB-012 ambiguous trade, WEB-013 >10 samples, EML-015 metres→yards + 0-stock Moss, EML-016 450>330 shortfall, EML-017 cut fee, EML-019 backorder ASAP, EML-020 retail pricing, EML-021 supersede/short-by-3.
