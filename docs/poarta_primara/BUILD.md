# BUILD — work packages

One WP per change. Status: `todo` | `in-progress` | `done` | `n-a` | `parked`.
Depends must be done. `decision` WPs need a human before code.

Law values in tests are synthetic. Invented CUIs must pass the checksum if you validate checksums.

## Sequence

| id | status | depends | title |
|---|---|---|---|
| WP-00 | done | — | Scaffold `langclaw_acct` types + Mongo indexes + fake SagaEye |
| WP-01 | done | WP-00 | Load Lane B YAML; fail closed on unknown articol / HITL kind |
| WP-02 | done | WP-01 | folder_triage + SourceDoc emit gates + Job unique `(cui, source_hash)` |
| WP-03 | todo | WP-02 | `iesire_factura_xml` + `intrare_factura_xml` fixtures; human import on copy firm |
| WP-04 | done | WP-03 | ingest graph through `packaged` + `v3_approve` interrupt (no SAGA before interrupt) |
| WP-05 | todo | WP-04 | PRE recon: RJ or SPV register already has the doc → `already_in_sink`, no package |
| WP-06 | todo | WP-03 | Windows agent pull / backup label / Import / `wait_validare` human |
| WP-07 | todo | WP-06 | intent_check against SagaEye v1 (report pack / RJ-CM) |
| WP-08 | done | WP-07 | ArticoleControls Layer 1 + PeriodDiff; `hard_failures` blocks package and file |
| WP-09 | done | WP-08 | `POST /rules` + HITL `explained_rule` + `control_disposition` |
| WP-10 | done | WP-09 | monthly_close + V2 pack; material cannot be cleared by Jev |
| WP-11 | done | WP-01 | CO.DiT seed from Pins + T* F* + additive axes; no silent `tva_platitor` |
| WP-12 | done | WP-10 | Filing items + `filing_receipt`; V2 `file` ≠ ANAF submit |
| WP-13 | todo | WP-05 | `extras_statement_pdf` extract path (document_ai); no MT940-first |
| WP-14 | parked | — | ArticolBon / `bon_via_nota` |
| WP-15 | parked | — | FDB SQL SagaEye |
| WP-16 | parked | — | Agent Validare |
| WP-17 | parked | — | Engagement backlog / OpenClaw `chat:` |
| WP-18 | parked | — | Take-on / year-end / D406 producer / FX engine |
| WP-01b | done | WP-00 | SagaEye protocol + Registru Jurnal witness (per-system readers, 00_LAW §8) |
| WP-02b | done | WP-02 | Email intake: two addresses per client, routed by recipient; client mail held for a person |
| WP-D3 | decision | WP-11 | Non-payer RC books: 4423 vs 446x on copy-firm note |

## WP details

### WP-00 Scaffold
- Done in `langclaw_acct/types.py` (Postgres per 00_LAW §8, not Mongo; indexes come with the first stored type). Tests: `tests/test_acct_pack.py`.
- Build: `langclaw_acct/types/` from ARCHITECTURE.md §3. Checkpointer MemorySaver in tests.
- Fake `SagaEye` returns empty lists.
- Tests: models reject extra keys; money fields are str.

### WP-01 Catalog loader
- Done in `langclaw_acct/catalog.py`. Tests: `tests/test_acct_pack.py`.
- Load every YAML under `catalog/` including `60_harvest` additive files (merge by catalog name).
- Unknown `articol_id` / HITL kind → error, not skip.
- Flux.write_modules ↔ WriteModule.used_by_flux both directions; drift fails load.
- Tests: load fixture pack; `define_articol` with invented id fails; Flux listing `nota_nc_dbf` without used_by_flux entry fails.

### WP-02 Triage + emit
- Done in `langclaw_acct/triage.py` (+ `jsonlogic.py` runs the pack's `emit` rule as shipped). Job uniqueness is a `JobStore` protocol with an in-memory store; the Postgres store is next. Non-XML files go to an injectable classifier (Document AI + Jev later), default `unknown` → `define_class`. Tests: `tests/test_acct_triage.py`.
- Implement class / identity / primary gates from SourceDoc + json-logic in `fixtures/architecture.jsonlogic.json`.
- Unique Job index `(tenant_cui, source_hash)`.
- Tests: PDF-only RO e-Factura does not emit; duplicate hash does not create a second job.

### WP-03 Invoice XML mouths
- Exporter in `sinks/saga_xml.py`. Tags only from a successful copy-firm import.
- Record `fixture:` path and `approved_at` on the module row after human green. Still `status: draft` until that happens; then `active`.
- Tests: XML well-formed; FurnizorCIF/ClientCIF routing documented in a unit test with synthetic CUIs.

### WP-04 Ingest to packaged
- Done up to `approved` in `langclaw_acct/ingest.py`; `package` is closed until Mouth-0 (00_LAW §8), so an approved Job ends `approved` / `mouth: closed`. PRE recon against the SagaEye runs before the approval (the status machine's order), so a document already in the books asks nobody. Jobs in Postgres: `langclaw_acct/jobs.py` `PgJobStore` (unique `(tenant_cui, source_hash)`, status history with who/why). Tests: `tests/test_acct_ingest.py`.
- Nodes as ARCHITECTURE.md §2. Jev mocked in tests.
- `v3_approve` at top of node; package after resume; idempotent `export_key`.
- Tests: resume writes XML once; reject does not package.

### WP-05 PRE recon + SPV
- Profiles from `ARTICOLE_RECONCILE_v1.yaml`.
- `spv_register` SourceDoc additive feeds expected keys.
- Tests: matching number+date in sink_lines → `already_in_sink`.

### WP-06 Agent
- HTTP as ARCHITECTURE.md §10. AGENT user cannot devalidate.
- Tests: fake agent; `wait_validare` without snapshot ≠ acked.

### WP-07 SagaEye v1
- Parse SAGA report pack / RJ-CM **headers only** first (harvest C-F11). Column map lives in `sinks/saga_eye.py`, not in graph code.
- Tests: fixture export (synthetic) → SinkDoc list.

### WP-08 Controls
- Done in `langclaw_acct/controls.py`: buckets, every `ArticoleControls` row (a control whose inputs don't exist yet reports INFO with why, never PASS), `period_diff` + `may_file`. C0 needs the expected movement per account, which comes with the expected set of approved Jobs; until then it's INFO. `M1_8_4428_open` and `M1_9_4424_watched` have no check yet (INFO). Tests: `tests/test_acct_controls.py`.
- Implement `catalog/60_harvest/ARTICOLE_CONTROLS_v1.yaml`.
- `hard_failures > 0` ⇒ package refused and V2 `file` refused.
- Tests: unexplained inbound → file impossible; already_posted → no package.

### WP-09 Explained rules
- Rules store done in `langclaw_acct/rules.py` (versioned, reason + who required; Postgres or memory); the `POST /rules` HTTP route comes with the agent API. Tests: `tests/test_acct_close.py`.
- `POST /rules` versioned. HITL `explained_rule` and `control_disposition`.
- Tests: rule tags sink lines to `explained_sink_only`; cannot hide unexplained without `rule_id`.

### WP-10 Close + V2
- Done in `langclaw_acct/close.py` (lock → diff → `v2_close`; `file` on a material month is refused and asked again; naming a saved rule re-runs the diff). The Jev Layer-2 pack isn't called yet; it could never clear `material` anyway. `reconcile_sink` POST and `v4_codit` come later. Tests: `tests/test_acct_close.py`.
- Pack inconsistency: `control_disposition` lists `monthly_close` in `graph_ids`, but the graph's `allowed_hitl` doesn't include it; left as shipped.
- Thread `close:{cui}:{period}`. Layer 2 JSON only.
- Tests: Jev action `file` with material=true is ignored.

### WP-11 CO.DiT
- Done in `langclaw_acct/codit.py`: defaults → hard pairs (T1–T3, F1–F6 raise, nothing saved) → soft pairs + `A_FLIP` / `A_CONTESTED` flags → the year's pins. Soft rows naming things not tracked yet (fleet, SAF-T, parent CUI) don't fire. In-memory; Postgres storage comes with the agent API. Tests: `tests/test_acct_codit_filings.py`.
- Seed copies Pins. T1–T3 hard. New axes default null. Certainty required on write.
- Tests: neplătitor + exig încasare → ValidationError; empty profile is not platitor.

### WP-12 Filings
- Done in `langclaw_acct/filings.py`: items from `ArticoleFiling` × CO.DiT, working-day due dates (D394 on the 30th), no date when the frequency isn't known (D300/D394 without `tva_period`, D406), closed only by a receipt. In-memory register. Tests: `tests/test_acct_codit_filings.py`.
- Rows from `ARTICOLE_FILING_v1.yaml`. Receipt closes item.
- Tests: calendar date passing does not close; receipt does.

### WP-13 Extras PDF
- Only after WP-05. Follow `EXTRACT.md` and `catalog/60_harvest/ARTICOLE_EXTRAS_GRAIN_v1.yaml`.
- Statement Pack + line Jobs. Do not match date+gross on one statement Job.
- Tests: extras without tenant identity do not emit; a two-line fixture mints two movement Jobs.

### WP-D3 Non-payer reverse charge books (`decision`)
- Ask: expected sink accounts for `foreign_rc_neplatitor` — harvest Y1 used 446x; some SAGA books use 4423.
- Until answered: articol exists, `expect_accounts: []`, `nota_nc_dbf` always-HITL, or SAGA-native + `explained_rule`.
- Do not silently fill 4423 or 446.

## Definition of done for v1

WP-00–WP-12 green. WP-13 optional. Parked WPs untouched. No `Journal.post` in the tree. No SYSDBA in env samples. Catalogs that shipped without fixtures remain `draft`.
