# Langclaw web console

A small Streamlit app over the langclaw control-plane API
(`docs/guides/control-plane-api.md`): **Chat**, **Workflows** (edit/save/run
saved JS workflows), **Schedules**, and **Status**. The API token stays
server-side; the browser only sees this app, behind a password.

## Deploy on Railway

Create a service from this repo with **Root Directory** `ui` (it uses
`ui/railway.json` + `ui/Dockerfile`), generate a public domain, and set:

| Variable | Value |
|---|---|
| `LANGCLAW_URL` | `http://langclaw.railway.internal:18790` |
| `LANGCLAW_API_TOKEN` | `${{langclaw.LANGCLAW__CHANNELS__API__TOKEN}}` (reference, no copy) |
| `UI_PASSWORD` | a password you choose (the app refuses to start without one) |

## Run locally

```bash
pip install -r ui/requirements.txt
LANGCLAW_URL=http://127.0.0.1:18790 LANGCLAW_API_TOKEN=... UI_PASSWORD=... \
  streamlit run ui/app.py
```

Chat history shown here is the API's in-memory turn list (resets when
langclaw restarts); the agent's conversation memory is unaffected.
