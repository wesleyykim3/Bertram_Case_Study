# Perennials Fabrics — Sample-to-Quote PoC

A runnable proof of concept for the sample → quote workflow: a free-text sample
request becomes a qualified Lead, a sample order + Opportunity, then a priced
quote with a dye-lot inventory reservation — with a human review queue wherever
judgement is required.

Systems are **stubbed, not integrated**: SQLite tables are namespaced to mirror
the real systems (`raw_inbound` = website/email, `crm_*` = Salesforce/HubSpot,
`erp_*` = NetSuite) so the seams where real integrations would sit are explicit.

## Run it (under 5 minutes)

```bash
pip install -r requirements.txt
python -m perennials.seed          # loads data/perennials_sample_data.xlsx into SQLite
uvicorn app:app --port 8000        # or: python app.py
```

Open http://127.0.0.1:8000 → **Process all inbound (time order)**, then explore:

- **Inbound** — all 21 provided requests with status + reason code; click one for raw text, extraction, and match decision
- **Review queue** — everything the system refused to guess at (duplicate merge, ambiguous trade, over-limit samples, shortfalls), with one-click human resolutions
- **Catalog (ERP)** — SKUs, dye lots, available = on hand − reserved, pricing rules
- **CRM** — accounts, contacts, opportunities (seeded + created by the PoC)
- **Quotes** — line pricing, warnings (dye-lot splits, shortfalls), reservations and their expiry
- **Audit log** — every automated decision and every human override, with reason codes

Checks over all 21 rows (no pytest needed):

```bash
python tests/run_tests.py   # 24 assertions, exits non-zero on failure
```

Reset any time from the dashboard (reseeds the database).

## How it works

```
raw message ─► extractor ─► matcher ─► qualify (rules) ─► sample order (ERP) + Opportunity (CRM)
 (immutable)   LLM seam      CRM        │                     │
                                        ▼                     ▼ quote request
                                  review queue          quote (deterministic pricing)
                                  (human decides)              │
                                                          reservation (ERP dye lots,
                                                          14-day hold) or flag
```

- **Extraction** (`perennials/extractor.py`) is the only place an LLM would sit.
  The PoC ships a deterministic alias/proximity parser behind the same interface
  so the demo runs with no API key. Output is validated against the ERP catalog;
  it never prices or qualifies.
- **Matching** (`perennials/matcher.py`) resolves Account and Contact
  separately. Exact email auto-links; anything fuzzier (e.g. WEB-003, Dana's
  second email address) becomes a human merge *proposal* — a false merge is far
  costlier than a duplicate.
- **Qualification & pricing & allocation** (`perennials/rules.py`) are pure,
  deterministic functions: spam discard, Do-Not-Sell hard block, trade
  qualification, max-10 samples, discontinued guard, trade/volume discounts,
  cut fees, per-dye-lot availability and single-lot-preferred allocation.
- **Humans** decide ambiguous trade status, merges, over-limit samples, and
  shortfall offers (partial hold + backorder). Every decision carries a reason
  code and lands in the audit log.

## Assumptions (stated, per the brief)

1. Discounts stack **additively**: `unit = list × (1 − trade − volume)`;
   trade 15% / non-trade 0%; volume +5% at 50–99 yd, +10% at 100+ yd, per line;
   $25 cut fee under 5 yd; quote valid 30 days; reservation hold 14 days.
2. All emails in the sample data use `.example` domains and are matched as-is; fuzzy identity = proposal, never auto-merge.
3. Inbound ID (`WEB-`/`EML-`) is the idempotency key; a quote *update*
   (EML-021) supersedes the prior quote and releases its hold before
   re-checking stock — never two live holds on one Opportunity.
4. Dye-lot split on one line = warning, not hard block (pending Ops confirmation).
5. Samples and yardage share one stock pool in the PoC; real NetSuite may split them.

See `DESIGN.md` for architecture and the real-system mapping, and
`AI_USAGE.md` for how AI tools were used to build this.
