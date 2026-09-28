"""How much review work is waiting, and who answers it how fast."""

from __future__ import annotations

from datetime import UTC, datetime

from langclaw.workflows.graph.runs import review_load

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _run(run_id, tenant, workflow, reviews):
    return {"run_id": run_id, "tenant": tenant, "workflow": workflow, "reviews": reviews}


def _review(created, decision=None):
    return {"created_at": created, "decision": decision}


RECORDS = [
    _run("r1", "acme", "accounting_proposal", [_review("2026-09-28T09:00:00+00:00")]),
    _run("r2", "acme", "accounting_month", [_review("2026-09-26T12:00:00+00:00")]),
    _run("r3", "beta", "accounting_proposal", [
        _review("2026-09-27T10:00:00+00:00",
                {"action": "approve", "by": "@ana", "actor": "telegram:1",
                 "at": "2026-09-27T12:00:00+00:00"}),
        _review("2026-09-27T12:00:00+00:00",
                {"action": "reject", "by": "@ana", "actor": "telegram:1",
                 "at": "2026-09-27T18:00:00+00:00"}),
    ]),
    _run("r4", "", "doc_intake", [
        _review("2026-08-01T10:00:00+00:00",  # answered before the window
                {"action": "approve", "by": "ion", "at": "2026-08-01T11:00:00+00:00"}),
    ]),
]  # fmt: skip


def test_pending_reviews_by_client_and_workflow_with_the_oldest() -> None:
    load = review_load(RECORDS, now=NOW)
    pending = load["pending"]
    assert pending["total"] == 2
    assert pending["oldest"] == {"run_id": "r2", "tenant": "acme",
                                 "workflow": "accounting_month", "hours": 48.0}  # fmt: skip
    assert pending["by_tenant"] == {"acme": {"count": 2, "oldest_hours": 48.0}}
    assert pending["by_workflow"]["accounting_proposal"] == {"count": 1, "oldest_hours": 3.0}


def test_answers_per_person_in_the_window_with_the_median_wait() -> None:
    answered = review_load(RECORDS, now=NOW, days=30)["answered"]
    assert answered == {"total": 2, "days": 30, "by_person": {
        "telegram:1": {"name": "@ana", "count": 2, "approved": 1, "rejected": 1, "edited": 0,
                       "median_hours": 4.0}}}  # fmt: skip
    assert review_load([], now=NOW)["pending"]["oldest"] is None
