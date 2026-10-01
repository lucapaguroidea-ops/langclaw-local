# Railway snapshot before the PoartaContabila switch (2026-10-01)

This repository ran on Railway until 2026-10-01. The project was then switched in place to
PoartaContabila (a standalone LangGraph app), and the services below were replaced or removed.
This file records what ran so the old setup can be rebuilt from commit `9441429` on `main`.
Only structure is recorded: no variable values, secrets or client data. Database and bucket
backups, if taken, are kept privately and never in this public repository.

## Code

- Commit `9441429` on `main` (optionally tag it `railway-final-2026-10-01`). The `langclaw`
  service deployed from `main`. Its latest deploy (2026-09-28) **failed**; the UI's latest deploy succeeded.

## Project

- One project, `production` environment, region `europe-west4`; the bucket is in `ams`.

| Service | Source | Build | Start / health | Domain → port | Volume |
|---|---|---|---|---|---|
| `langclaw` | this repo, `main`, root `.` | `Dockerfile` | `langclaw gateway` | `langclaw-production.up.railway.app` → 8080 | `langclaw-data` 5 GB at `/root/.langclaw` |
| `langclaw-ui` | this repo, `main`, root `ui` | `Dockerfile`, watch `ui/**` | health `/_stcore/health`, timeout 180 s | `langclaw-ui-production.up.railway.app` → 8501 | — |
| `Postgres` | `postgres-ssl:18` | — | — | private | 500 MB |
| `Postgres-H5ez` | `postgres-ssl:18` | — | — | private | 5 GB |
| bucket `durable-taco` | Railway bucket (S3 API) | — | — | — | — |

The gateway used two databases: `DATABASE_URL` (checkpointer, cron, workflow store) and
`DOCUMENTS_DATABASE_URL` (documents and accounting tables). One change was staged but never
applied: remove `MONGO_URL` from `langclaw`.

## Variable names (values not recorded)

`langclaw`:
`APPSMITH_URL`, `BUCKET_ACCESS_KEY`, `BUCKET_ENDPOINT`, `BUCKET_NAME`, `BUCKET_REGION`,
`BUCKET_SECRET_KEY`, `DATABASE_URL`, `DOCUMENTS_DATABASE_URL`,
`LANGCLAW__AGENTS__BACKEND__BACKEND`, `LANGCLAW__AGENTS__MODEL`,
`LANGCLAW__CHANNELS__API__ENABLED`, `LANGCLAW__CHANNELS__API__HOST`,
`LANGCLAW__CHANNELS__API__PORT`, `LANGCLAW__CHANNELS__API__TOKEN`,
`LANGCLAW__CHANNELS__TELEGRAM__ALLOW_FROM`, `LANGCLAW__CHANNELS__TELEGRAM__ENABLED`,
`LANGCLAW__CHANNELS__TELEGRAM__TOKEN`, `LANGCLAW__CHECKPOINTER__BACKEND`,
`LANGCLAW__CHECKPOINTER__POSTGRES__DSN`, `LANGCLAW__CRON__DATA_STORE__BACKEND`,
`LANGCLAW__CRON__DATA_STORE__POSTGRES__DSN`, `LANGCLAW__CRON__ENABLED`,
`LANGCLAW__DOCUMENTS__EMBEDDING_MODEL`, `LANGCLAW__DOCUMENTS__ENABLED`,
`LANGCLAW__DOCUMENTS__INTAKE_WORKFLOW`, `LANGCLAW__DOCUMENTS__OCR_MODEL`,
`LANGCLAW__INTERPRETER__ENABLED`, `LANGCLAW__LOG_LEVEL`, `LANGCLAW__TOOLS__SEARCH_BACKEND`,
`LANGCLAW__WORKFLOWS__ENABLED`, `MONGO_URL`, `OPENROUTER_API_KEY`.

`langclaw-ui`: `LANGCLAW_API_TOKEN`, `LANGCLAW_URL`, `PORT`, `UI_PASSWORD`.

## To rebuild

Create the services above from commit `9441429`, set the variables (new secrets, new Telegram token),
point `DATABASE_URL` and `DOCUMENTS_DATABASE_URL` at fresh Postgres instances, and restore a
private dump if one was kept.
