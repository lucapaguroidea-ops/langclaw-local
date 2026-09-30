# Idempotency keys

Write Mongo after the SAGA side effect. Resume re-enters the node from line 1.

| Object | Key | Collision |
|---|---|---|
| Job | `(tenant_cui, source_hash)` | second emit is a no-op |
| thread document graph | `job:{job_id}` | do not mix prefixes |
| thread triage | `batch:{batch_id}` | |
| thread recon | `recon:{cui}:{period}` | |
| thread close | `close:{cui}:{period}` | |
| extract | `source_hash` + backend | reuse `normalized/` |
| package XML/DBF | `export_key` = `{module_id}:{job_id}:{schema_version}` | write once |
| PRE recon | `(job_id, stage=pre, sink_snapshot_id)` | |
| POST recon | `(job_id, stage=post, sink_snapshot_id)` | |
| HITL resume | `(thread_id, interrupt_id)` | same body twice = same state |
| CloseRun lock | `(cui, period)` | one expected-set hash |
| ControlRun | `(cui, period, control_id, snapshot_id)` | |
| FilingItem | `(cui, period, filing_id)` | receipt closes, date does not |
| Backup label | `{cui}:{folder}:{utc}` | restore refused if tenant mismatch |

Money and fiscal dates in graph state are strings. Decimal lives inside the compute node.
