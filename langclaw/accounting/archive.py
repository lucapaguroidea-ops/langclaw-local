"""One client's books as a self-contained zip — a copy that doesn't depend on
the database (to keep, or to hand over when a client leaves).

Every table of the client's schema that holds their books is written as JSON
lines (one row per line, in primary-key order), with ``manifest.json`` naming
the client, the time, and each table's row count and SHA-256. Tables the client
never used (no bank statements yet, say) are simply absent.

:func:`restore_books` reads one back into a client whose books are empty (after
losing the database, or to move a client), checking every file against the
manifest first.
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


class ArchiveError(ValueError):
    """An archive that can't be restored (damaged, or books already there)."""


def read_archive(data: bytes) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """The manifest and each table's rows, after checking every file's SHA-256
    and row count against the manifest.

    Raises:
        ArchiveError: not an archive, or a file doesn't match its manifest line.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(zf.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ArchiveError(f"Not a books archive: {exc}") from exc
    tables: dict[str, list[dict[str, Any]]] = {}
    for entry in manifest.get("tables") or []:
        name = entry.get("table", "")
        if name not in ARCHIVED_TABLES:
            raise ArchiveError(f"The archive names an unknown table {name!r}.")
        try:
            body = zf.read(f"{name}.jsonl")
        except KeyError as exc:
            raise ArchiveError(f"{name}.jsonl is missing from the archive.") from exc
        if hashlib.sha256(body).hexdigest() != entry.get("sha256"):
            raise ArchiveError(f"{name}.jsonl doesn't match its checksum: the archive is damaged.")
        rows = [json.loads(line) for line in body.splitlines() if line.strip()]
        if len(rows) != entry.get("rows"):
            raise ArchiveError(f"{name}.jsonl has {len(rows)} rows, the manifest says "
                               f"{entry.get('rows')}.")  # fmt: skip
        tables[name] = rows
    return manifest, tables


async def restore_books(store: DocumentStore, data: bytes) -> dict[str, int]:
    """Load the archive *data* into *store*'s client, in one transaction.

    Returns:
        Rows restored per table.

    Raises:
        ArchiveError: damaged archive, or the client already has journal
            entries or documents (a restore never merges into existing books).
    """
    from langclaw.accounting.assets import FixedAssets
    from langclaw.accounting.bank.store import BankBook
    from langclaw.accounting.journal import Journal

    _, tables = read_archive(data)
    pool = await store._db()  # creates the documents table
    for owner in (Journal(store), BankBook(store), FixedAssets(store)):
        await owner._db()  # and the accounting tables, as the code expects them
    schema = f'"{store.schema}"'
    async with pool.acquire() as conn, conn.transaction():
        for table in ("journal_entries", "documents"):
            if await conn.fetchval(f"SELECT count(*) FROM {schema}.{table}"):
                raise ArchiveError(f"This client already has {table.replace('_', ' ')}: "
                                   "restore only into empty books.")  # fmt: skip
        restored: dict[str, int] = {}
        for table in ARCHIVED_TABLES:  # parents before children (journal_lines → entries)
            rows = tables.get(table)
            if rows is None:
                continue
            await conn.execute(f"DELETE FROM {schema}.{table}")
            await conn.execute(
                f"INSERT INTO {schema}.{table} SELECT * FROM "
                f"jsonb_populate_recordset(NULL::{schema}.{table}, $1::jsonb)",
                json.dumps(rows),
            )
            if "id" in (rows[0] if rows else {}):  # new rows continue after the restored ids
                await conn.execute(
                    f"SELECT setval(pg_get_serial_sequence('{schema}.{table}', 'id'), "
                    f"(SELECT max(id) FROM {schema}.{table}))"
                )
            restored[table] = len(rows)
    return restored


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    row.pop("embedding", None)  # search vectors are rebuilt, not archived
    if isinstance(row.get("fields"), str):
        row["fields"] = json.loads(row["fields"])
    return row
