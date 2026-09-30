# Which catalog to open

| You are doing | Open |
|---|---|
| “What is this file?” | `20_document/ARTICOLE_SOURCE_DOC_v1.yaml` + ADD |
| “May we emit?” | SourceDoc + `fixtures/architecture.jsonlogic.json` `emit` |
| “Which walk?” | `30_cale/ARTICOLE_FLUX_v1.yaml` |
| “Which Job kind?” | `20_document/ARTICOLE_JOBS_v1.yaml` |
| “Which SAGA mouth?” | `30_cale/ARTICOLE_WRITE_MODULE_v1.yaml` |
| “Which graph node / HITL kind?” | `30_cale/ARTICOLE_GRAPH_v1.yaml` + `50_control/ARTICOLE_HITL_v1.yaml` + ADD |
| “TVA × exig legal?” | `10_lege_firma/ARTICOLE_CODIT_T_v1.yaml` |
| “Formă × impozit?” | `ARTICOLE_CODIT_PAIRS_v1.yaml` |
| “Rates this year?” | `ARTICOLE_PINS_v1.yaml` |
| “New CO.DiT axis?” | `60_harvest/ARTICOLE_CODIT_AXES_v1.yaml` |
| “Already in RJ / SPV?” | `40_sink/ARTICOLE_RECONCILE_v1.yaml` |
| “May we file?” | `40_sink/ARTICOLE_CLOSE_v1.yaml` + `60_harvest/ARTICOLE_CONTROLS_v1.yaml` + `ARTICOLE_FILING_v1.yaml` |
| “Bon matrix?” | `30_cale/ARTICOL_BON_v1.yaml` — parked |
| “Statement PDF grain?” | `60_harvest/ARTICOLE_EXTRAS_GRAIN_v1.yaml` |
| “Jev field allowed?” | `50_control/JEV_ANNEX_v1.yaml` + `JEV_VALIDATE_V_v1.yaml` |

Unknown `articol_id` or HITL kind is an error, not a skip.

Flux.write_modules is the walk. WriteModule.used_by_flux is the index. WP-01 rejects drift.
