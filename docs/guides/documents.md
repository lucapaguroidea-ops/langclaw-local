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
LANGCLAW__DOCUMENTS__OCR_MODEL=openrouter:google/gemini-2.5-flash  # optional: read scans
LANGCLAW__DOCUMENTS__EMBEDDING_MODEL=openai/text-embedding-3-small # optional: search by meaning
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
| `bucket_read(key)` | A file's text — PDFs page by page, text files as-is (capped at `max_text_chars`). Scanned PDFs and photos go through OCR when `OCR_MODEL` is set (`"ocr": true` in the result); without it they return a `note` |
| `bucket_link(key, expires_minutes)` | A temporary download link |
| `bucket_new_files(prefix, limit)` | Files not yet in the `documents` table (for a scheduled scan) |
| `documents_save(bucket_key, sender, receiver, document_date, doc_type, amount, currency, summary, status, fields)` | Insert **or update** the record for a file — saving the same key twice never duplicates, so a re-run workflow step is safe. Extra `fields` merge into a JSON column |
| `documents_search(text, sender, receiver, doc_type, date_from, date_to, status, fields, limit)` | Filter filed documents (case-insensitive; `text` searches summary, file name, and extra fields). `fields` filters on type-specific details: `{"jurisdiction": "Delaware"}`, or `{"notice.days": "90"}` for a nested value |
| `documents_get(bucket_key)` | One record |
| `documents_semantic_search(query, ...filters, limit)` | *With `EMBEDDING_MODEL`:* records ranked by meaning ("power bills" finds electricity invoices), each with a `similarity` 0–1; the same filters as `documents_search` narrow them |
| `documents_reindex(limit)` | *With `EMBEDDING_MODEL`:* embed records saved before it was on (or whose embedding failed); repeat while `remaining` > 0 |
| `documents_start_intake(prefix, limit, channel, chat_id)` | Start `INTAKE_WORKFLOW` for every new file. Each gets a `processing` record first, so a second scan never queues it twice. Reports to the given chat, else to `workflows.review_channel` / `review_chat_id` |

Failures come back as `{"error": "..."}` — a missing object, a bad date
(`YYYY-MM-DD`), an unreachable database. The table is created on first use.

## Several clients

With [clients (tenants)](tenants.md) enabled, every tool works inside the current
client's own folder (`tenants/<id>/…`) and table (`tenant_<id>.documents`);
keys like `inbox/…` are relative to it. A chat linked to no client gets no
document access, and chat intake tells the sender so.

## Scans and photos (OCR)

Files with a text layer are read directly. When there's none — a scanned PDF, a
phone photo of a receipt — and `OCR_MODEL` names a vision-capable model,
`bucket_read` renders each page (first `OCR_MAX_PAGES`) to an image and asks the
model to transcribe it. The intake workflow needs no change: its `fetch` step
just gets text back. OCR failures come back as a `note`, never an exception.

## Search by meaning

With `EMBEDDING_MODEL` set, `documents_save` also embeds each record (type,
sender, receiver, date, summary, extra fields) through OpenRouter — or any
OpenAI-compatible endpoint via `EMBEDDING_BASE_URL` — and stores the vector in the
table's `embedding` column. If embedding fails the record is still saved and the
result carries a `note`; `documents_reindex` catches it up later, as it does for
records from before search was on. Vectors never go back to the model.

## Documents sent in chat

With `INTAKE_WORKFLOW` set, a **file** sent in chat (a PDF or text document — and a
**photo** too when `OCR_MODEL` is set; never a voice note or video) is uploaded to
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

Besides the fixed fields, `classify` returns `details`: whatever else matters for
*that kind* of document (an invoice's `tax_id` / `iban` / `due_date`, a contract's
`jurisdiction` / `notice_days`), which `save` stores in `fields`. Every record can
have different details; search them with `documents_search(fields=...)`, or the
**Fields** box on the console's Documents page (`jurisdiction=Delaware`).
Change the threshold in the `check` node.

**`bucket_scan`** is one step, `documents_start_intake`. It covers files that
reach the bucket some other way, such as an upload or a sync. Schedule it by asking the
agent, e.g. "run bucket_scan every day at 7", which uses the cron tool with
`workflow_name=bucket_scan`. Set `workflows.review_channel` / `review_chat_id`
so scanned runs know where to report.

## Limits

- OCR is a vision-model call per page (first `OCR_MAX_PAGES` pages), used only
  when a file has no text layer. Pick a model that accepts images; a text-only
  model makes the call fail and `bucket_read` returns the error as a `note`.
- Field filters are substring matches over the JSON `fields` column, so they
  scan rows (like semantic search) — fine at tens of thousands of documents.
  Details keep whatever names the model picks; describing the names you want in
  the `details` output (as the template does) keeps them consistent.
- Semantic search compares vectors in plain SQL — no pgvector extension needed —
  by scanning every embedded row. That's fast up to tens of thousands of documents;
  beyond that, a pgvector index is the next step. Changing `EMBEDDING_MODEL` means
  old vectors don't match new ones: clear the column and run `documents_reindex`.
