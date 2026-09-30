# AGENTS.md — implement against this pack

You are building Poarta Primară. This directory is the only source of truth.

## Before any code

1. Read `INDEX.md`, `00_LAW.md`, this file, then the catalog file that owns the articol you will touch (`CATALOG_LOOKUP.md`).
2. Read `BUILD.md`. Take the first work package whose status is `todo` and whose `depends` are done.
3. If the WP is marked `decision`, stop and ask the human. Do not invent the answer.
4. Do not open `annex/` except to recover a type name or XML tag already copied into `ARCHITECTURE.md`.
5. Do not open the harvest zip or the Word drafts. Their verdicts that survived are already in `00_LAW.md` §6, `catalog/60_harvest/`, and `BUILD.md`.

## Operating loop

```
restate the change as: articol_id + poartă open/closed
write or extend a failing test named in the WP
smallest change that passes
run targeted tests, then the pack tests listed in the WP
update catalog status only when the WP says the fixture is green
mark the WP done in BUILD.md in the same change
```

## Hard bans

- Do not add `Journal.post`, `journal_entries`, `journal_lines`, or a 5-column trial balance as books.
- Do not INSERT/UPDATE/DELETE `CONT_BAZA.FDB`.
- Do not put an LLM on a graph edge.
- Do not let Grok or a chat agent choose mouth, TVA treatment, or file vs hold.
- Do not emit XML/DBF except through a `WriteModule` row.
- Do not set `status: active` on a WriteModule without a copy-firm fixture path that is green.
- Do not Validare as agent in v1.
- Do not invent SAGA XML tags. Extend only from a successful copy-firm import.
- Do not invent catalog ids from Jev output. Off-list → HITL `define_articol`.
- Do not mix `thread_id` prefixes.
- Do not put client PII, live CUIs, IBANs, or real amounts in this repo.
- Do not implement OpenClaw, ReAct supervisors, or `chat:` as a poster.
- Do not promote `bon_via_nota` or ArticolBon in v1 WPs.
- Do not generate D406 from this system. SAGA files D406.
- Do not treat V2 `file` as “submitted to ANAF”.

## Catalog is executable

- Lane B = YAML in `catalog/`. Lane A = per-client maps and `explained_rules` (not in this pack).
- `matches()` first. `|cands| = 1` → skip Jev. `|cands| = 0` or `> 1` → HITL.
- Flux.write_modules ↔ WriteModule.used_by_flux must agree both ways. Drift is a load error.
- Job freezes `articol_id`, `schema_version`, `client_type_hash`, `jev_annex_hash`.
- Money and dates in graph state are strings. Decimals live inside compute nodes.
- Resume payloads validate with `extra=forbid` against `ArticoleHITL.resume_schema`.

## Where to put new work

| Kind of change | File |
|---|---|
| New walk on a document | `catalog/30_cale/ARTICOLE_FLUX_v1.yaml` (articol de cale) |
| New file type / emit rule | `catalog/20_document/ARTICOLE_SOURCE_DOC_v1.yaml` |
| New Job kind | `catalog/20_document/ARTICOLE_JOBS_v1.yaml` |
| New SAGA mouth | `catalog/30_cale/ARTICOLE_WRITE_MODULE_v1.yaml` + fixture |
| New HITL kind | `catalog/50_control/ARTICOLE_HITL_v1.yaml` |
| New close control | `catalog/60_harvest/ARTICOLE_CONTROLS_v1.yaml` and wire into Layer 1 |
| New filing obligation | `catalog/60_harvest/ARTICOLE_FILING_v1.yaml` |
| New CO.DiT axis | `catalog/60_harvest/ARTICOLE_CODIT_AXES_v1.yaml` then T*/F* if hard |
| New T*/F* pair | existing files in `catalog/10_lege_firma/` |
| Graph topology | `catalog/30_cale/ARTICOLE_GRAPH_v1.yaml` + amendment in `00_LAW.md` |

Do not create `ArticoleJev`, `ArticolePack`, or `ArticoleEmit`.

## Tests that must exist before a WriteModule is live

1. Duplicate `source_hash` → second job does not package.
2. `interrupt` resume → XML written once.
3. `wait_validare` resume without snapshot → not `acked`.
4. PeriodDiff unexplained → `file` impossible even if Jev says yes.
5. Control `hard_failures > 0` → WriteModule must not package.
6. Storno job carries `is_storno` and `storno_of`.
7. Copy-firm import of the fixture succeeded once (human-recorded in the module row).

## Identity of the machine

Four compiled graphs only: `folder_triage`, `ingest_source_doc`, `reconcile_sink`, `monthly_close`. Glue = Mongo ids. Do not `add_node(compiled_other_graph)`.
