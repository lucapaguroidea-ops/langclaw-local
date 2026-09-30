# Architecture — what to build

Self-contained. Catalogs in `catalog/` are the row-level law. This file is the machine.

## 1. Shape

```
sources (SPV UBL, S3 dump, Telegram, email, photo, SPV register, SAGA report pack)
    → Railway  four compiled LangGraph graphs
         Jev in nodes · Grok HITL text · RunPod if needs_ocr
         Mongo domain · Postgres checkpointer · Bucket blobs
    → XML/DBF packages
    → Windows VPS  SAGA C as AGENT (Import; Validare human in v1)
    ← SagaEye v1 from SAGA report pack / RJ-CM export
```

SAGA is the mouth. The report pack is the v1 eye. This system hops documente primare through articole de cale and PreFiles. It does not keep books.

## 2. Four graphs

| graph_id | thread | In | Out |
|---|---|---|---|
| folder_triage | `batch:{id}` | dump | Packs + emit |
| ingest_source_doc | `job:{id}` | Job | packaged / acked / already_in_sink |
| reconcile_sink | `recon:{cui}:{period}` | Jobs + sink lines | pre/post verdict |
| monthly_close | `close:{cui}:{period}` | lock | file / hold + V4 |

Do not nest compiled graphs. Glue = Mongo ids.

### folder_triage

sniff → aisle (SPV > foreign > bon CUI > extras > rest) → pair UBL+PDF → fork bon → emit if class ∧ identity ∧ primary ∧ posting_eligible.

PDF RO without UBL is not primary.

### ingest_source_doc

```
extract → v3_classify → match → v3_judge → checks
  → interrupt v3_approve?
  → PRE reconcile_sink
  → package WriteModule (blocked if ArticoleControls hard_failures)
  → interrupt wait_validare
  → intent_check SagaEye
  → acked | reopened | failed
```

`package` does not talk to Firebird. No SAGA call before `interrupt()` in the approve node.

Job unique on `(tenant_cui, source_hash)`.

### reconcile_sink

PRE: already in RJ or SPV register → do not package.
POST: how vs expected accounts.
det first, llm_review cannot flip to posted. contest → HITL.
Matcher tolerance 0.05 on keys. Missing sink → `need_rj_export`.

### monthly_close

```
lock_expected_set → recon POST → pull report pack → PeriodDiff
  → run ArticoleControls
  → v2_gate (Jev may not clear material)
  → file | hold | patch_maps | reopen
  → v4_codit after file
```

Buckets: expected | explained_sink_only | unexplained.
material if outbound hole, unexplained inbound, watched |Δ| ≥ 0.01, lock mismatch, blocking control FAIL.

## 3. Types (minimum)

Money and dates in state are strings.

```
TenantRef        cui, punct="default", saga_firm_folder
SourceRef        kind, bucket_key, content_type, source_hash
PartnerRef       cui?, name, role, saga_analytic?
CanonicalDocument  job_id, tenant, period, doc_class, number, date, partner,
                   totals, lines, is_storno, storno_of, source, maps, jev
JobRecord        status machine below; export_key; module_id; saga{}
ExpectedItem     job_id, doc_class, number, date, partner_cui, gross, net, vat, analytic
SinkDoc          saga_key, same business keys, validated
BucketRow        kind, expected?, sink?, rule_id?, delta_gross
PeriodDiff       outbound_holes, inbound[], synthetic_delta, analytic_delta,
                 material, blockers[], snapshot_id, hard_failures
WriteModule      see catalog
ControlRun       control_id, status PASS|FAIL|INFO, target, actual, diff
FilingItem       filing_id, period, state open|filed, receipt_key?
CO.DiT           {cui, period} axes + pins + certainty + derive()
```

Job statuses (forward only except `reopened` after SAGA compensation):

```
ingested → extracted → bound → reconcile_pre
  → approved | already_in_sink | needs_human
approved → packaged → wait_validare → acked
acked | wait_validare → reopened → packaged
* → rejected | failed | needs_human
```

Close is a CloseRun, not a Job.

## 4. Storage

| Store | Holds | Does not |
|---|---|---|
| Mongo jobs, canonical, maps, expected_sets, explained_rules, write_modules, close_snapshots, codit, filings, control_runs | domain | FDB rows |
| Postgres checkpointer | cursor, interrupts | domain |
| Bucket | source, XML/DBF, report packs, backup labels, receipts | secrets |

After a SAGA side effect: write Mongo, then return from the node.

Object key: `tenants/{cui}/{punct}/{period}/{kind}/{jobId}/...`

## 5. WriteModules (v1 catalog, all draft)

`iesire_factura_xml` `intrare_factura_xml` `storno_iesire_xml` `storno_intrare_xml` `incasare_xml` `plata_xml` `parteneri_xml` `articole_xml` `nota_nc_dbf`

Parked: `bon_via_nota`.

Facturi routing: FurnizorCIF == societate CUI → Ieșiri; ClientCIF == societate CUI → Intrări. Analytics from Lane A maps.

Validare: human until the module fixture is green on a copy firm.

`hard_failures > 0` on prefile controls ⇒ do not package.

## 6. SagaEye v1

```
documents(cui, period) -> list[SinkDoc]
solduri(cui, period) -> dict
analytic(cui, period, root) -> dict
```

v1 reads SAGA report pack / RJ-CM export (practice takeover pack). FDB SQL later on a pinned SAGA C build. Core graphs depend only on the protocol.

Watched v1: 401, 4111, 4426, 4427, 4428, 5121, 5311.

## 7. CO.DiT

Write order: exig defaults → T* → F* → F7/A* → derive().
Hard pair → ValidationError, document not saved.
Axes in `catalog/10_lege_firma` plus additive `catalog/60_harvest/ARTICOLE_CODIT_AXES_v1.yaml`.
Empty profile must not default to `tva_platitor`.
Period document, not a sticky tenant flag.

## 8. HITL

Kind ∈ ArticoleHITL ∪ HITL_ADD ∩ graph.allowed_hitl. Unknown kind = bug. Resume `extra=forbid`.

Core kinds:

- `v3_approve` resume `{decision: approve|reject|edit, edit?}` — XOR, not three bools
- `v2_close` resume `{action: file|hold|patch_maps|reopen, explained_rule?}`
- `wait_validare`, `request_devalidare`, `define_articol`, `define_module`, `explained_rule`, `need_rj_export`
- additive: `decision_menu` (client sends `option` only; server stamps confirmer/at), `codit_premise`, `filing_receipt`, `control_disposition`

## 9. Compensation

| SAGA fact | SAGA action | Job |
|---|---|---|
| Imported, not validated | Anulează importul | rejected |
| Validated, not in SPV / not filed | Devalidare by human | reopened |
| In SPV or receipt exists | Stornare + Reglare | new job `is_storno=True` |
| Panic | Human restore of labeled snapshot | runbook |

Agent never devalidates. Agent never auto-restores. Archive metadata must include `tenant_cui`. Restore of another tenant is refused.

## 10. HTTP

```
POST /ingest
GET  /jobs/{id}
POST /jobs/{id}/resume
GET  /close/{cui}/{period}
POST /close/{cui}/{period}/resume
GET  /agent/pull
POST /agent/imported
POST /agent/snapshot
POST /agent/ack-backup
POST /maps
POST /rules
POST /filings/{id}/receipt
```

Agent token ≠ Grok token. No `SAGA_SYSDBA` in Railway env.

## 11. Package

```
langclaw_acct/
  types/
  graphs/document.py period.py reconcile.py triage.py
  jev/packs/v3_classify.json v3_judge.json v2_declaration_gate.json
  sinks/saga_xml.py saga_eye.py
  controls.py          # ArticoleControls
  period_diff.py       # Layer 1 only
  maps.py idempotency.py agent_api.py
```

No ReAct supervisor. No `Journal.post`.

## 12. Jev packs

`v3_classify` → `{doc_class, needs_ocr, needs_human, confidence}`
`v3_judge` → `{accounts_ok, risk, needs_human}`
`v2_declaration_gate` → `{books_support_declaration, gap_materiality, action}`

JSON only. Cache `{pack, input_hash}`. Layer 2 cannot clear `material`.

## 13. Windows agent

```
GET /agent/pull → backup label → Import as AGENT → POST /agent/imported
snapshot_request → parse report pack or replica → POST /agent/snapshot
```

AGENT: import only. No Devalidare, no închidere lună, no admin.

## 14. Env

```
LANGCLAW__CHECKPOINTER__BACKEND=postgres
LANGCLAW__CHECKPOINTER__POSTGRES__DSN=
MONGODB_URI=
S3_ENDPOINT= S3_ACCESS_KEY= S3_SECRET_KEY= S3_BUCKET=
JEV_BASE_URL= JEV_API_KEY=
GROK_API_KEY=
RUNPOD_API_KEY=
AGENT_SHARED_TOKEN=
```

Absent: `SAGA_SYSDBA`, Firebird write password, `CIEL_SA`, `NEXTUP_*`.
