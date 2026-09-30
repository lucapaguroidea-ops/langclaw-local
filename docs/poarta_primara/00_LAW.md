# 00 — Law

Status: LOCKED for implementers.
Package working name: `langclaw_acct`.
Product face: Poarta Primară.

## 0. Unit

An **articol de cale** is a bookkeeping state that already contains its next path and the gate that must open before the path moves.

Think: which articol is this document on, and is its poartă open?

If a change cannot be stated that way, it is not ready.

## 1. Expansions

Documentul primar nu ia calea fără poartă.

Articolele de cale stau într-un catalog de cale.

Graful Primar walks that catalog: more expandable than a frozen database application, more controlled than an LLM interaction. Models classify inside nodes. Edges, mouths, and month-close are deterministic.

## 2. One-sentence statutory split

SAGA C is the only statutory mouth.
Firebird replica / SAGA report pack is the only statutory eye.
Nothing in this system posts a *notă contabilă*.
Mongo may hold an expected set and witness snapshots. Not a ledger.

## 3. Invariants (fail the build)

1. No chart of accounts, journal, or 5-column trial balance stored as books in Postgres or Mongo.
2. No process on Railway holds SYSDBA or writes INSERT/UPDATE/DELETE on `CONT_BAZA.FDB`.
3. Posting = official SAGA path only: XML/DBF → Import date → Validare. Direct FDB write = `FORBIDDEN_FDB`.
4. Graph edges read only stored fields or Jev answers already on state. No LLM on an edge.
5. Jev is System One. Grok is System Two (explain, HITL). Grok never posts. RunPod only if `needs_ocr`.
6. `interrupt()` resume re-enters the node from line 1. Side effects sit after the interrupt, behind Mongo idempotency.
7. One firm-period has one statutory sink: SAGA C.
8. Compensation is SAGA-shaped: Anulează importul | Devalidare | Stornare. Backup/restore is firm-wide.
9. Month closed in SAGA ⇒ agent writes `0`.
10. `thread_id` prefixes: `batch:` | `job:` | `recon:` | `close:` | `chat:`. Never mix.
11. Fail closed. Extra keys on Job/Pack/resume = forbid. Money and fiscal dates in graph state are strings.
12. PDF RO without UBL is not primary. No CUI on a bon is not deductibility.
13. Layer 1 `material == true` ⇒ `file` is impossible. Layer 2 cannot clear `material`.
14. An articol that does not name a cale is a comment. A cale with no poartă is a silent post. A catalog that does not compose căi is a nomenclator.
15. Law values carry `source`, `as_of`, `certainty`. Unconfirmed stays `[de confirmat]`.
16. No client identifiers, IBANs, or live amounts in this repo, tests, or fixtures.

## 4. Two faces

| Face | Says | Does not lead with |
|---|---|---|
| Cabinet — Poarta Primară | document primar, articol de cale, catalog de cale, poartă, buckets | Graph, LangGraph, OpenClaw |
| Engineering — Graful Primar | four compiled graphs, Path/cale, WriteModule, PreFile, Latch/Hold/Gate | Chat supervisor, ReAct, ledger |

OpenClaw / chat / multi-tool is a later `chat:` face. It is not a mouth, matcher, or closer. It never resumes `job:`, `recon:`, or `close:`.

## 5. Lexicon (complete for this pack)

### Face A

| Term | Means | Does not mean |
|---|---|---|
| Articol de cale | Unit: state + path + gate. In YAML this is a Flux/Close/Reconcile/Bon/Control row | SAGA stoc; a comment |
| Cale | Pre-defined walk: document primar × contabilitate RO × fiscalitate | LLM trajectory |
| Catalog de cale | Versioned book of those walks | A drawer of things |
| Poartă | Guard on a hop | Chat “ok” |
| Poarta Primară | Product face | The ledger; ANAF filing |
| Document primar | Factură, UBL, bon, extras | Notă contabilă |
| Hopper | Intake mechanism. Not branded | The product |
| PreFile | Pack XML/DBF for SAGA Import | File at ANAF |
| Bucket | expected \| explained_sink_only \| unexplained | A local account |

### Face B

| Term | Means | Does not mean |
|---|---|---|
| Graful Primar | Compiled walker | Product name |
| LangGraph | Substrate | Product; ReAct licence |
| Path | Runtime name for cale. Code: `articol_id` in ArticoleFlux | LLM route |
| Pack | Source-doc dossier before emit | Thinking unit |
| Job | Posting unit after emit | CloseRun |
| CloseRun | One firm-month | A Job |
| WriteModule | Approved SAGA mouth | FDB INSERT |
| Expected set | Document-derived totals | General ledger |
| SagaEye | Read protocol | Write path |
| Jev / Grok / RunPod | Classify in-node / explain / gated OCR | Poster / edge |

### Statutory

Mouth = SAGA C Import + Validare. Eye = report pack / RJ-CM / later FDB replica. *Notă contabilă* is what SAGA posts. Compensation = Anulează / Devalidare / Stornare. PeriodDiff and CO.DiT are not books.

### Collisions

LangClaw = working code name, not the cover. Flux = keep filename; say cale. Naked “articol” / “catalog” / “graph” / “flow” / “file at ANAF” are banned in new prose.

Romanian domain words stay Romanian. Code identifiers stay English.

## 6. Decisions locked (harvest v1.3)

1. Witness v1 = SAGA report pack and/or RJ-CM export → SagaEye DTOs. FDB SQL later.
2. Validare on posting modules is human until that `module_id` has a green copy-firm fixture. No agent Validare in v1.
3. Explained rules = `POST /rules` + HITL `explained_rule`. Engagement backlog deferred.
4. Write adapter seam kept. No Web reads. Book of record = SAGA C.
5. V2 materiality = 0.01 RON on watched accounts. PRE/POST matcher may use 0.05 on match keys only.
6. `bon_via_nota` and ArticolBon are parked. Not in BUILD v1 sequence.

## 7. Amendment

v1 watched synthetic accounts (Layer 1 blocking): 401, 4111, 4426, 4427, 4428, 5121, 5311.

5124, 403–409, 411/413/418/419, 4423, 4424 are not on this list. Advisory controls may name them. Making them blocking is an amendment.

Changing sink product, FDB write policy, graph topology, interrupt kinds, or watched accounts requires a dated section in this file and `schema_version` bump on affected catalogs.

Adding an articol de cale inside existing enums is not an amendment. It still needs `status: draft` until the fixture/accountant rule in the row is met.

A Telegram message is not an amendment.

## 8. Amendment — 2026-09-30 (owner decisions)

1. **Book of record.** SAGA C desktop becomes the book of record once the four graphs are built and Mouth-0 is green. **Mouth-0** = the first time this system talks to SAGA as a writer.
2. **Before Mouth-0** Graful Primar walks the Catalog Cale against a **Registru Jurnal exported from the client's accounting system** (SAGA, NextUp, SAP…). Each system's layout is defined from a real export of that system (`langclaw_acct/sinks/registru_jurnal.py`, `RJ_READERS`); none is guessed. The RJ is read through the same `SagaEye` protocol, so the post-Mouth-0 eye replaces it without touching the graphs. SAGA C stays the only mouth; it is closed until Mouth-0.
3. **Storage.** Postgres for domain data (per-client schemas) and the checkpointer. Where this pack says Mongo, read Postgres. Idempotency keys (IDEMPOTENCY.md) become unique constraints.
4. **Models.** Document AI parses documents where deterministic parsing can't (UBL / e-Factura XML stays deterministic). Jev routes (typed choices). OpenRouter, with a mix of models, for decisions and review text. Grok and RunPod are not in v1.
5. **Chat** (`chat:`, OpenClaw face) is set aside, as WP-17 already says.
6. **The existing `langclaw.accounting` package** (its own journal, trial balance, closing) stays in the tree until SAGA is the book of record; the "no `Journal.post` in the tree" rule applies to `langclaw_acct`, which never imports its books (journal, period, tools). It does use `langclaw.accounting.workdays` (holiday reference data) for due dates.
7. **Catalog files as shipped** were not all valid YAML (a stray trailing `)` in 15 files; `{a?, b?}` shorthand in `row_schema`; a pseudo-code `"*" -> x` list; two unquoted items). They were quoted / trimmed with no change in meaning so WP-01 can load them.
