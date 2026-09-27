"""
Run index for graph workflows — one record per run, alongside the checkpointer.

The checkpointer holds each run's *state* (thread ``workflow:<run_id>``); this
index holds what the checkpointer can't answer cheaply: which runs exist for a
workflow, their status and timestamps, who is waiting on a review, and who
answered it. A record::

    {
      "run_id": "doc_intake:1a2b3c", "workflow": "doc_intake",
      "status": "running" | "waiting" | "completed" | "failed" | "rejected",
      "input": {...}, "output": ..., "error": "",
      "trigger": "telegram" | "api" | "cron" | "agent" | ...,
      "reply_to": {"channel", "user_id", "context_id", "chat_id"},
      "reviews": [{"interrupt_id", "node", "message", "data", "editable",
                   "created_at", "decision": null | {...}}],
      "started_at": "...", "updated_at": "..."
    }

A review is *pending* while it is listed with ``decision is None``; the first
answer (Telegram, UI, or ``/workflows approve``) fills ``decision`` under a lock,
so a second answer is refused with who answered first.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

_NAMESPACE = ("workflow_graph_runs",)
_PAGE = 100

#: Run statuses that will not change again.
FINAL_STATUSES = frozenset({"completed", "failed", "rejected"})


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ReviewAlreadyResolved(RuntimeError):
    """Raised when a review was already answered (first answer wins)."""

    def __init__(self, run_id: str, decision: dict[str, Any] | None) -> None:
        self.decision = decision or {}
        if not decision:
            super().__init__(f"Run {run_id} has no pending review.")
            return
        who = self.decision.get("by") or "someone"
        via = self.decision.get("via")
        verb = _PAST.get(self.decision.get("action", ""), "answered")
        where = f" via {via}" if via else ""
        super().__init__(f"Run {run_id} was already {verb} by {who}{where}.")


_PAST = {"approve": "approved", "edit": "edited", "reject": "rejected"}


def review_key(run_id: str, interrupt_id: str) -> str:
    """A short, stable id for one review — fits in a Telegram button's 64-byte payload."""
    return hashlib.sha1(f"{run_id}|{interrupt_id}".encode()).hexdigest()[:12]


class RunIndexBackend(Protocol):
    """Key-value persistence for run records (``BaseStore`` or in-memory)."""

    async def get(self, run_id: str) -> dict[str, Any] | None: ...

    async def put(self, run_id: str, record: dict[str, Any]) -> None: ...

    async def all(self) -> list[dict[str, Any]]: ...


class InMemoryRunIndexBackend:
    """Process-local backend (tests, or when no store is configured)."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    async def get(self, run_id: str) -> dict[str, Any] | None:
        record = self._records.get(run_id)
        return copy.deepcopy(record) if record is not None else None

    async def put(self, run_id: str, record: dict[str, Any]) -> None:
        self._records[run_id] = copy.deepcopy(record)

    async def all(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(r) for r in self._records.values()]


class StoreRunIndexBackend:
    """Durable backend over a LangGraph ``BaseStore`` (SQLite or Postgres)."""

    def __init__(self, store: BaseStore) -> None:
        self._store = store

    async def get(self, run_id: str) -> dict[str, Any] | None:
        item = await self._store.aget(_NAMESPACE, run_id)
        return dict(item.value) if item is not None else None

    async def put(self, run_id: str, record: dict[str, Any]) -> None:
        await self._store.aput(_NAMESPACE, run_id, record)

    async def all(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._store.asearch(_NAMESPACE, limit=_PAGE, offset=offset)
            out.extend(dict(item.value) for item in page)
            if len(page) < _PAGE:
                return out
            offset += _PAGE


class RunIndex:
    """Run records for graph workflows, with first-answer-wins review claims.

    The claim lock is per process: one gateway replica is the supported setup.
    """

    def __init__(self, backend: RunIndexBackend | None = None) -> None:
        self._backend = backend or InMemoryRunIndexBackend()
        self._lock = asyncio.Lock()

    async def get(self, run_id: str) -> dict[str, Any] | None:
        return await self._backend.get(run_id)

    async def create(
        self,
        run_id: str,
        workflow: str,
        run_input: Any,
        *,
        trigger: str = "",
        reply_to: dict[str, str] | None = None,
        role: str = "",
        tenant: str = "",
    ) -> dict[str, Any]:
        stamp = now_iso()
        record = {
            "run_id": run_id,
            "workflow": workflow,
            "status": "running",
            "input": run_input,
            "output": None,
            "error": "",
            "trigger": trigger,
            "reply_to": reply_to or {},
            "role": role,
            "tenant": tenant,
            "reviews": [],
            "started_at": stamp,
            "updated_at": stamp,
        }
        await self._backend.put(run_id, record)
        return record

    async def update(self, run_id: str, **fields: Any) -> dict[str, Any]:
        async with self._lock:
            record = await self._backend.get(run_id) or {"run_id": run_id, "reviews": []}
            record.update(fields)
            record["updated_at"] = now_iso()
            await self._backend.put(run_id, record)
            return record

    async def add_reviews(
        self, run_id: str, pending: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Record newly pending reviews (idempotent per ``interrupt_id``) and mark waiting.

        Returns:
            ``(record, added)`` — *added* are the reviews not seen before (the ones
            to notify about).
        """
        async with self._lock:
            record = await self._backend.get(run_id) or {"run_id": run_id, "reviews": []}
            known = {r["interrupt_id"] for r in record.get("reviews", [])}
            added = []
            for review in pending:
                if review["interrupt_id"] not in known:
                    entry = {
                        **review,
                        "key": review_key(run_id, review["interrupt_id"]),
                        "created_at": now_iso(),
                        "decision": None,
                        "notices": [],
                    }
                    record.setdefault("reviews", []).append(entry)
                    added.append(copy.deepcopy(entry))
            record["status"] = "waiting"
            record["updated_at"] = now_iso()
            await self._backend.put(run_id, record)
            return record, added

    async def add_notice(self, run_id: str, interrupt_id: str, notice: dict[str, Any]) -> None:
        """Remember where a review request was sent (so it can be updated when answered)."""
        async with self._lock:
            record = await self._backend.get(run_id)
            if record is None:
                return
            for review in record.get("reviews", []):
                if review["interrupt_id"] == interrupt_id:
                    review.setdefault("notices", []).append(notice)
            await self._backend.put(run_id, record)

    async def find_review(self, key: str) -> tuple[str, str] | None:
        """``(run_id, interrupt_id)`` of the review with short *key*, among recent runs."""
        for record in await self.list(limit=500):
            for review in record.get("reviews", []):
                if review.get("key") == key:
                    return record["run_id"], review["interrupt_id"]
        return None

    async def claim_review(
        self, run_id: str, decision: dict[str, Any], *, interrupt_id: str = ""
    ) -> dict[str, Any]:
        """Atomically answer a pending review; the first answer wins.

        Args:
            run_id: The waiting run.
            decision: Normalized decision (``action``, ``data``, ``by``, ``via``...).
            interrupt_id: The review to answer; empty ⇒ the oldest pending one.

        Returns:
            The claimed review entry (with ``decision`` filled in).

        Raises:
            KeyError: unknown run.
            ReviewAlreadyResolved: nothing pending, or that review was answered.
        """
        async with self._lock:
            record = await self._backend.get(run_id)
            if record is None:
                raise KeyError(run_id)
            reviews = record.get("reviews", [])
            target = None
            for review in reviews:
                if interrupt_id and review["interrupt_id"] != interrupt_id:
                    continue
                if interrupt_id or review.get("decision") is None:
                    target = review
                    break
            if target is None:
                last = next((r["decision"] for r in reversed(reviews) if r.get("decision")), None)
                raise ReviewAlreadyResolved(run_id, last)
            if target.get("decision") is not None:
                raise ReviewAlreadyResolved(run_id, target["decision"])
            target["decision"] = {**decision, "at": now_iso()}
            if all(r.get("decision") is not None for r in reviews):
                record["status"] = "running"
            record["updated_at"] = now_iso()
            await self._backend.put(run_id, record)
            return copy.deepcopy(target)

    async def list(
        self, *, workflow: str = "", status: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        """Records newest first, optionally filtered by workflow and status."""
        records = await self._backend.all()
        if workflow:
            records = [r for r in records if r.get("workflow") == workflow]
        if status:
            records = [r for r in records if r.get("status") == status]
        records.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return records[:limit]

    async def pending_reviews(self, *, workflow: str = "") -> list[dict[str, Any]]:
        """Every unanswered review, oldest first, with its run's identity."""
        out: list[dict[str, Any]] = []
        for record in await self.list(workflow=workflow, status="waiting", limit=10_000):
            for review in record.get("reviews", []):
                if review.get("decision") is None:
                    out.append(
                        {
                            "run_id": record["run_id"],
                            "workflow": record.get("workflow", ""),
                            **review,
                        }
                    )
        out.sort(key=lambda r: r.get("created_at", ""))
        return out
