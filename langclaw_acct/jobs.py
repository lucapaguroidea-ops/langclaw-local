"""
Jobs — the posting unit after emit (ARCHITECTURE.md §3), stored per client.

A Job is unique on ``(tenant_cui, source_hash)``: emitting the same file twice
is a no-op (IDEMPOTENCY.md). Every status change goes through
:meth:`JobStore.move`, which refuses a move the status machine doesn't allow
and records it in the job's history with who / why.

:class:`PgJobStore` keeps them in Postgres (00_LAW §8: not Mongo), in the
schema it's given — one per client (``langclaw.naming.tenant_schema``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from langclaw_acct.types import JobRecord, JobStatus, can_move

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class JobError(ValueError):
    """Unknown job, or a status move the machine doesn't allow."""


class JobStore(Protocol):
    async def add(self, job: JobRecord) -> bool:
        """Store *job*; False when ``(tenant_cui, source_hash)`` is already there."""
        ...

    async def get(self, job_id: str) -> JobRecord: ...

    async def move(
        self, job_id: str, status: JobStatus, *, note: str = "", by: str = "", **fields: str
    ) -> JobRecord: ...  # fmt: skip

    async def history(self, job_id: str) -> list[dict[str, Any]]: ...


def _moved(job: JobRecord, status: str, fields: dict[str, str]) -> JobRecord:
    if not can_move(job.status, status):
        raise JobError(f"job {job.job_id}: {job.status} → {status} is not allowed")
    return job.model_copy(update={"status": status, **fields})


@dataclass
class MemoryJobStore:
    jobs: dict[str, JobRecord] = field(default_factory=dict)
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    async def add(self, job: JobRecord) -> bool:
        if any((j.tenant_cui, j.source_hash) == (job.tenant_cui, job.source_hash)
               for j in self.jobs.values()):  # fmt: skip
            return False
        self.jobs[job.job_id] = job
        self.events[job.job_id] = [{"status": job.status, "note": "emitted", "by": ""}]
        return True

    async def get(self, job_id: str) -> JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise JobError(f"no job {job_id}") from None

    async def move(
        self, job_id: str, status: JobStatus, *, note: str = "", by: str = "", **fields: str
    ) -> JobRecord:
        job = _moved(await self.get(job_id), status, fields)
        self.jobs[job_id] = job
        self.events[job_id].append({"status": status, "note": note, "by": by})
        return job

    async def history(self, job_id: str) -> list[dict[str, Any]]:
        return list(self.events.get(job_id, []))


class PgJobStore:
    """Jobs in ``<schema>.acct_jobs`` + ``<schema>.acct_job_events`` (asyncpg pool)."""

    def __init__(self, pool: Any, schema: str) -> None:
        if not _IDENT.match(schema):
            raise ValueError(f"bad schema name {schema!r}")
        self._pool, self._s = pool, schema
        self._ready = False

    async def ensure(self) -> None:
        if self._ready:
            return
        s = self._s
        async with self._pool.acquire() as con:
            await con.execute(f"""
                CREATE SCHEMA IF NOT EXISTS {s};
                CREATE TABLE IF NOT EXISTS {s}.acct_jobs (
                    job_id text PRIMARY KEY,
                    tenant_cui text NOT NULL,
                    source_hash text NOT NULL,
                    record jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (tenant_cui, source_hash));
                CREATE TABLE IF NOT EXISTS {s}.acct_job_events (
                    id bigserial PRIMARY KEY,
                    job_id text NOT NULL REFERENCES {s}.acct_jobs(job_id),
                    status text NOT NULL, note text NOT NULL DEFAULT '',
                    by_actor text NOT NULL DEFAULT '',
                    at timestamptz NOT NULL DEFAULT now());
            """)  # fmt: skip
        self._ready = True

    async def add(self, job: JobRecord) -> bool:
        await self.ensure()
        async with self._pool.acquire() as con, con.transaction():
            row = await con.fetchrow(
                f"INSERT INTO {self._s}.acct_jobs (job_id, tenant_cui, source_hash, record) "
                "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT DO NOTHING RETURNING job_id",
                job.job_id, job.tenant_cui, job.source_hash, job.model_dump_json(),
            )  # fmt: skip
            if row is None:
                return False
            await con.execute(
                f"INSERT INTO {self._s}.acct_job_events (job_id, status, note) "
                "VALUES ($1, $2, 'emitted')", job.job_id, job.status)  # fmt: skip
        return True

    async def get(self, job_id: str) -> JobRecord:
        await self.ensure()
        async with self._pool.acquire() as con:
            raw = await con.fetchval(
                f"SELECT record FROM {self._s}.acct_jobs WHERE job_id = $1", job_id
            )
        if raw is None:
            raise JobError(f"no job {job_id}")
        return JobRecord.model_validate(json.loads(raw) if isinstance(raw, str) else raw)

    async def move(
        self, job_id: str, status: JobStatus, *, note: str = "", by: str = "", **fields: str
    ) -> JobRecord:
        await self.ensure()
        async with self._pool.acquire() as con, con.transaction():
            raw = await con.fetchval(
                f"SELECT record FROM {self._s}.acct_jobs WHERE job_id = $1 FOR UPDATE", job_id
            )
            if raw is None:
                raise JobError(f"no job {job_id}")
            job = JobRecord.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
            job = _moved(job, status, fields)
            await con.execute(
                f"UPDATE {self._s}.acct_jobs SET record = $2::jsonb WHERE job_id = $1",
                job_id, job.model_dump_json())  # fmt: skip
            await con.execute(
                f"INSERT INTO {self._s}.acct_job_events (job_id, status, note, by_actor) "
                "VALUES ($1, $2, $3, $4)", job_id, status, note, by)  # fmt: skip
        return job

    async def history(self, job_id: str) -> list[dict[str, Any]]:
        await self.ensure()
        async with self._pool.acquire() as con:
            rows = await con.fetch(
                f"SELECT status, note, by_actor FROM {self._s}.acct_job_events "
                "WHERE job_id = $1 ORDER BY id", job_id)  # fmt: skip
        return [{"status": r["status"], "note": r["note"], "by": r["by_actor"]} for r in rows]
