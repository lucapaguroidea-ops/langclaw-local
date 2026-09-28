"""One client's books as a self-contained zip — a copy that doesn't depend on
the database (to keep, or to hand over when a client leaves).

Every table of the client's schema that holds their books is written as JSON
lines (one row per line, in primary-key order), with ``manifest.json`` naming
the client, the time, and each table's row count and SHA-256. Tables the client
never used (no bank statements yet, say) are simply absent.

This is an export, not a restore: reading an archive back is a separate step.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langclaw.documents.store import DocumentStore

#: The client's tables, in the order they're archived.
ARCHIVED_TABLES = (
    "journal_entries",
    "journal_lines",
    "closed_periods",
    "period_log",
    "bank_transactions",
    "fixed_assets",
    "documents",
)


async def archive_books(store: DocumentStore) -> tuple[bytes, dict[str, Any]]:
    """The zip and its manifest for the client whose schema *store* uses."""
    pool = await store._db()
    present = {r["table_name"] for r in await pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = $1",
        store.schema)}  # fmt: skip
    tables = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for table in ARCHIVED_TABLES:
            if table not in present:
                continue
            rows = await pool.fetch(f'SELECT * FROM "{store.schema}"."{table}" ORDER BY 1')
            body = "".join(
                json.dumps(_plain(dict(r)), ensure_ascii=False, default=str) + "\n" for r in rows
            ).encode()
            zf.writestr(f"{table}.jsonl", body)
            tables.append({"table": table, "rows": len(rows),
                           "sha256": hashlib.sha256(body).hexdigest()})  # fmt: skip
        manifest = {
            "client": store.schema.removeprefix("tenant_"),
            "schema": store.schema,
            "created_at": datetime.now(UTC).isoformat(),
            "tables": tables,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue(), manifest


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    row.pop("embedding", None)  # search vectors are rebuilt, not archived
    if isinstance(row.get("fields"), str):
        row["fields"] = json.loads(row["fields"])
    return row
