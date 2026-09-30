# Poarta Primară — standalone pack

This directory is the source of truth. Do not open the Word drafts, the harvest zip, or `annex/` unless you are tracing history.

```
INDEX.md                 this file
AGENTS.md                how a coding agent must work
00_LAW.md                unit, lexicon, invariants, locked decisions
ARCHITECTURE.md          system to build (self-contained)
BUILD.md                 work packages an agent may pick
EXTRACT.md               extract adaptor contract
IDEMPOTENCY.md           keys
CATALOG_LOOKUP.md        which YAML to open
CITATIONS.md             public report id ↔ harvest lens id
catalog/                 Catalog Cale — executable law
  10_lege_firma          Pins, T*, F*
  20_document            SourceDoc, Jobs
  30_cale                Graph, Flux (= căi), WriteModule, Bon (parked)
  40_sink                Reconcile, Close
  50_control             HITL, Jev
  60_harvest             Controls, Filings, CO.DiT axes, extra HITL — added from practice
fixtures/                json-logic + jev tests
annex/                   superseded briefs and old contract — not SoT
```

**Unit you think in:** articol de cale.

**Lege:** Documentul primar nu ia calea fără poartă.

**Product face:** Poarta Primară.  
**Method:** Catalog Cale.  
**Machine:** Graful Primar (four compiled LangGraph graphs).  
**Code name until rename:** `langclaw_acct`.

A Flux row in `ARTICOLE_FLUX_v1.yaml` *is* an articol de cale. The filename stays Flux so nothing is lost. Say **cale** / **articol de cale** in prose. Say `articol_id` in code.
