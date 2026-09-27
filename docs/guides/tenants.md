# Clients (tenants)

One langclaw deployment can serve several clients whose data must never mix —
for example an accounting firm's client companies. Turn it on with:

```bash
LANGCLAW__TENANTS__ENABLED=true
```

Then add clients on the console's **Clients** page (or `PUT /v1/tenants/{id}`):

| Field | What it's for |
|---|---|
| `id` | Short lowercase slug (`acme`). Names the client's storage folder and database schema, so it can't change later. |
| `name`, `tax_id` | Display name and fiscal code (CUI). |
| `chats` | The chats that belong to this client, as `channel:chat_id` — e.g. `telegram:-1001234567890` for the client's Telegram group. A chat belongs to **one** client at most. |
| `review_chat` | Where this client's review requests also go. |
| `profile` | Company context (VAT payer, VAT on collection, tax regime, CAEN, `expected_documents` for the month-close checklist, anything else) for workflows to ground decisions in. |

## How the separation works

The model never decides whose data it touches. Everything below is decided by
code, from where a message came from:

1. **The gateway resolves the client from the chat.** A message from
   `telegram:-100111` belongs to the client that lists that chat. Nothing a
   user types or puts in message metadata can pick a different client. The only
   messages that name their client explicitly are langclaw's own workflow
   messages (chat intake, bucket scans, runs started from the console).
2. **The client is current for the whole turn or run.** Tools read it from
   context (`langclaw.tenants.current_tenant()`), not from their arguments.
3. **Runs remember their client.** The client is stored on the run record, so a
   run answered hours later — from any chat, the console, or after a restart —
   continues for the same client. A run whose client was deleted fails instead of
   running against the wrong data.
4. **Document tools only see the current client's data.** Files live under
   `tenants/<id>/` in the bucket and records in the `tenant_<id>` database schema.
   Keys the model passes are relative to that folder and can't climb out of it
   (`../other/…` is refused).
5. **No client, no documents.** In a chat linked to no client, the document tools
   and chat intake refuse with a message saying so. The agent still answers.

Review requests go to the chat the run started in, the client's review chat, and
the global review chat (`workflows.review_chat_id`), and they name the client.

## For developers

```python
from langclaw.tenants import current_tenant

@app.tool()
async def client_note() -> dict:
    tenant = current_tenant()   # None when tenancy is off or the chat has no client
    if tenant is None:
        return {"error": "This chat isn't linked to a client."}
    return {"client": tenant.name, "vat_payer": tenant.profile.get("vat_payer")}
```

## Limits

- **One database, one schema per client.** Isolation is enforced by langclaw's
  code paths, not by separate database credentials per client. The bucket is
  also shared, with one prefix per client.
- **Chat-based only.** There's no `/client` switch yet for staff who work across
  several clients from one chat; give each client its own chat, or use the
  console, which has a client picker.
- **Deleting a client keeps its data.** Its files and records stay in its
  folder and schema, and re-adding the same id finds them again.
- **Existing data isn't moved.** Documents filed before tenancy was turned on
  stay in the old, shared location and aren't visible under any client.
