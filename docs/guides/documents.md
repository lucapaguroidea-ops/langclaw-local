# Documents

Give the agent — and workflow steps — a **bucket** for files and a **`documents`
table** for what was extracted from them (sender, receiver, date, type, amount,
summary, anything else). Built for pipelines like "read each uploaded invoice,
classify it, ask me when unsure, file it, let me search it later".

```bash
uv add "langclaw[documents]"
LANGCLAW__DOCUMENTS__ENABLED=true
LANGCLAW__DOCUMENTS__DATABASE_URL=postgresql://...     # or DOCUMENTS_DATABASE_URL
LANGCLAW__DOCUMENTS__INTAKE_WORKFLOW=document_intake   # optional, see below
```

The bucket is any S3-compatible store. Each unset
`LANGCLAW__DOCUMENTS__BUCKET__{ENDPOINT,NAME,ACCESS_KEY,SECRET_KEY,REGION}` falls
back to the plain `BUCKET_*` variable Railway injects for a linked bucket, so on
Railway the bucket needs no extra settings.

!!! tip "Use a separate database"
    Point `DATABASE_URL` at a Postgres of its own, not the checkpointer's: the
    tools run queries on the model's behalf, and a separate database keeps that
    away from conversation state and lets you back up / restore it on its own.

## Tools

| Tool | Does |
|---|---|
| `bucket_list(prefix, limit)` | Files in the bucket, newest first |
| `bucket_read(key)` | A file's text — PDFs page by page, text files as-is (capped at `max_text_chars`). Scanned PDFs and images return a `note` instead: there's no OCR yet |
| `bucket_link(key, expires_minutes)` | A temporary download link |
| `bucket_new_files(prefix, limit)` | Files not yet in the `documents` table (for a scheduled scan) |
| `documents_save(bucket_key, sender, receiver, document_date, doc_type, amount, currency, summary, status, fields)` | Insert **or update** the record for a file — saving the same key twice never duplicates, so a re-run workflow step is safe. Extra `fields` merge into a JSON column |
| `documents_search(text, sender, receiver, doc_type, date_from, date_to, status, limit)` | Filter filed documents (case-insensitive; `text` searches summary, file name, and extra fields) |
| `documents_get(bucket_key)` | One record |
| `documents_start_intake(prefix, limit, channel, chat_id)` | Start `INTAKE_WORKFLOW` for every new file. Each gets a `processing` record first, so a second scan never queues it twice. Reports to the given chat, else to `workflows.review_channel` / `review_chat_id` |

Failures come back as `{"error": "..."}` — a missing object, a bad date
(`YYYY-MM-DD`), an unreachable database. The table is created on first use.

## Documents sent in chat

With `INTAKE_WORKFLOW` set, a **file** sent in chat (a PDF or text document — not
a photo, voice note, or video) is uploaded to
`<intake_prefix><YYYY-MM-DD>/<id>-<filename>` (default prefix `inbox/`) and the
workflow starts right away, with no agent turn, getting:

```json
{"key": "inbox/2026-09-27/1a2b3c4d-invoice.pdf", "filename": "invoice.pdf",
 "mime_type": "application/pdf", "caption": "the message text, if any"}
```

The chat gets "📥 Saved invoice.pdf — running document_intake.", then the
workflow's progress, any review request, and its result. The file gets a
`processing` record straight away, so a bucket scan won't pick it up again.
Without `INTAKE_WORKFLOW`, attachments go to the agent as before.

## The intake workflow

The console's **New workflow** menu has two templates. They are also files in
[`ui/templates/`](https://github.com/lucapaguroidea-ops/langclaw-local/tree/main/ui/templates):
copy them to `workflows/<name>.graph.json` or save them from the UI.

**`document_intake`**:

```
fetch (bucket_read) → classify (llm: doc_type, sender, receiver, document_date,
amount, currency, summary, confidence) → check
  confidence < 0.75 → review (human, edit the extraction) → save
  otherwise         → save (documents_save, status "filed")
  review rejected   → mark_rejected (status "rejected")
```

Change the threshold in the `check` node. Add fields to `classify.output` and
pass them to `save`'s `fields` to keep more.

**`bucket_scan`** is one step, `documents_start_intake`. It covers files that
reach the bucket some other way, such as an upload or a sync. Schedule it by asking the
agent, e.g. "run bucket_scan every day at 7", which uses the cron tool with
`workflow_name=bucket_scan`. Set `workflows.review_channel` / `review_chat_id`
so scanned runs know where to report.

## Limits

- No OCR: scanned PDFs and photos have no text to read.
- Search is filter / substring based; semantic (vector) search isn't wired yet.
