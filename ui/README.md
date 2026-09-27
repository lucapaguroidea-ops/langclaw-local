# Langclaw workflow console

A small Streamlit app over the langclaw control-plane API
(`docs/guides/control-plane-api.md`) for **setting up and managing workflows**.
Chat and conversation history stay in Telegram. The API token stays
server-side; the browser only sees this app, behind a password.

Pick a workflow in the sidebar (or **➕ New workflow**) and use its tabs:

| Tab | What it does |
|---|---|
| 🗺️ Graph | The workflow drawn as a flowchart, plus a table of its steps |
| ✏️ Edit | Settings, run-input fields, a form per step, connections, and the raw file; **Validate** and **Save** (the previous version is kept) |
| ▶️ Test run | Run it with a form built from its input fields; see progress and output |
| 📜 Runs | Every run with status and who started it; open one for its input, output, review answers, and each step's result |
| 🙋 Reviews | Paused runs of this workflow — Approve, Edit & approve, or Reject |
| ⏰ Schedules | This workflow's cron schedules; add or remove one |
| 🕘 Versions | Earlier versions with a diff against the current file; restore one |

The sidebar also has the **Review queue** (every paused run) and **Status**
(channels, features, tools and subagents available to workflow steps). Answers
given here update the Telegram review messages too, and vice versa. Workflows
defined in Python code are shown read-only.

## Deploy on Railway

Create a service from this repo with **Root Directory** `ui` (it uses
`ui/railway.json` + `ui/Dockerfile`), generate a public domain, and set:

| Variable | Value |
|---|---|
| `LANGCLAW_URL` | `http://langclaw.railway.internal:18790` |
| `LANGCLAW_API_TOKEN` | `${{langclaw.LANGCLAW__CHANNELS__API__TOKEN}}` (reference, no copy) |
| `UI_PASSWORD` | a password you choose (the app refuses to start without one) |
| `UI_REVIEWER` | optional: your name, recorded on reviews answered here (default `web`) |

The flowchart loads Mermaid from `cdn.jsdelivr.net` in your browser.

## Run locally

```bash
pip install -r ui/requirements.txt
LANGCLAW_URL=http://127.0.0.1:18790 LANGCLAW_API_TOKEN=... UI_PASSWORD=... \
  streamlit run ui/app.py
```

`ui/editor.py` holds the draft-editing logic (no Streamlit); it is unit-tested
in `tests/test_ui_editor.py`.
