# AI usage notes

## Tools used
- **Cursor** — planning, code generation, and debugging
  for the whole PoC; document/data extraction from the `.docx` brief and
  `.xlsx` sample data (parsed with `python-docx`/`openpyxl` rather than by eye).


## Where AI helped
- **Planning and designing:** helped analyse the brief and sample data, map the six-stage workflow, and settle the system boundaries.
- **Scaffolding:** helped generate the FastAPI UI, the SQLite schema namespaced by system of record, the Excel seed loader, and the test harness.
- **Testing:** helped turn the 21 provided rows into a test suite of 24 assertions covering qualification, guardrails, pricing, stock reservations, and quote supersession, plus smoke tests of the running UI.
- **Debugging:** helped investigate failing tests and pinpoint issues in the extraction and processing logic, such as quantities being missed in free-text messages.


## Where it had to be corrected
- **Scope and complexity:** the AI tended to propose more than the brief
  needed — extra features and an LLM in the runtime path. I guided it back to
  a simpler deterministic design that runs reliably within the timebox, with
  scope frozen once the core workflow was complete.
- **Pricing ownership:** early drafts blurred the line between extracting a
  request and deciding what it costs. I corrected this so pricing, discounts,
  and availability are deterministic rules, kept separate from any free-text
  interpretation.
- **Ambiguous rules presented as settled:** where the source data allowed
  more than one reading (e.g. how discounts stack), the AI initially picked
  one without flagging the ambiguity. I made the final call and required the
  assumption to be stated explicitly in the documentation.
- **Inefficient generated code:** the first passes included unused imports,
  unused parameters, and unreachable branches. I audited the codebase and
  removed the dead code rather than shipping it.
