"""
WP-00 types (docs/poarta_primara/ARCHITECTURE.md §3).

Every model refuses unknown keys (00_LAW §3.11: fail closed). Money and fiscal
dates are strings in graph state; :func:`money` / :func:`fiscal_date` validate
the shape, and Decimal lives only inside the node that computes.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

_MONEY = re.compile(r"^-?\d+(\.\d{1,2})?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _money(v: str) -> str:
    if not _MONEY.match(v):
        raise ValueError(f"money must be a decimal string like '1234.50', got {v!r}")
    return v


def _date(v: str) -> str:
    if not _DATE.match(v):
        raise ValueError(f"fiscal date must be 'YYYY-MM-DD', got {v!r}")
    return v


def _period(v: str) -> str:
    if not _PERIOD.match(v):
        raise ValueError(f"period must be 'YYYY-MM', got {v!r}")
    return v


Money = Annotated[str, AfterValidator(_money)]
FiscalDate = Annotated[str, AfterValidator(_date)]
Period = Annotated[str, AfterValidator(_period)]


class Strict(BaseModel):
    """Base for every Poarta Primară type: unknown keys are an error."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TenantRef(Strict):
    cui: str
    punct: str = "default"
    saga_firm_folder: str | None = None


class SourceRef(Strict):
    kind: str
    bucket_key: str
    content_type: str
    source_hash: str


class PartnerRef(Strict):
    cui: str | None = None
    name: str
    role: Literal["furnizor", "client"]
    saga_analytic: str | None = None


class Totals(Strict):
    net: Money
    vat: Money
    gross: Money


class DocLine(Strict):
    description: str
    quantity: str
    net: Money
    vat_rate: str
    vat: Money


class CanonicalDocument(Strict):
    job_id: str
    tenant: TenantRef
    period: Period
    doc_class: str
    number: str
    date: FiscalDate
    partner: PartnerRef
    totals: Totals
    lines: list[DocLine] = Field(default_factory=list)
    is_storno: bool = False
    storno_of: str | None = None
    source: SourceRef
    maps: dict[str, str] = Field(default_factory=dict)
    jev: dict[str, str] = Field(default_factory=dict)


JobStatus = Literal[
    "ingested", "extracted", "bound", "reconcile_pre", "approved", "already_in_sink",
    "needs_human", "packaged", "wait_validare", "acked", "reopened", "rejected", "failed",
]  # fmt: skip

#: Forward-only status machine (ARCHITECTURE.md §3). Any status may go to
#: rejected / failed / needs_human.
JOB_EDGES: dict[str, frozenset[str]] = {
    "ingested": frozenset({"extracted"}),
    "extracted": frozenset({"bound"}),
    "bound": frozenset({"reconcile_pre"}),
    "reconcile_pre": frozenset({"approved", "already_in_sink", "needs_human"}),
    "approved": frozenset({"packaged"}),
    "packaged": frozenset({"wait_validare"}),
    "wait_validare": frozenset({"acked", "reopened"}),
    "acked": frozenset({"reopened"}),
    "reopened": frozenset({"packaged"}),
}
_ANY_TO = frozenset({"rejected", "failed", "needs_human"})


def can_move(src: str, dst: str) -> bool:
    """Whether a Job may go from *src* to *dst*."""
    return dst in _ANY_TO or dst in JOB_EDGES.get(src, frozenset())


class JobRecord(Strict):
    job_id: str
    tenant_cui: str
    source_hash: str
    articol_id: str
    schema_version: str
    status: JobStatus = "ingested"
    module_id: str | None = None
    export_key: str | None = None
    saga: dict[str, str] = Field(default_factory=dict)

    def moved(self, status: JobStatus) -> JobRecord:
        """A copy at *status*; refuses a move the status machine doesn't allow."""
        if not can_move(self.status, status):
            raise ValueError(f"job {self.job_id}: {self.status} → {status} is not allowed")
        return self.model_copy(update={"status": status})


class ExpectedItem(Strict):
    job_id: str
    doc_class: str
    number: str
    date: FiscalDate
    partner_cui: str | None
    gross: Money
    net: Money
    vat: Money
    analytic: str | None = None


class SinkDoc(Strict):
    """One document as the statutory books show it (SagaEye output)."""

    saga_key: str
    doc_class: str
    number: str
    date: FiscalDate
    partner_cui: str | None = None
    gross: Money
    net: Money | None = None
    vat: Money | None = None
    analytic: str | None = None
    validated: bool = True


class BucketRow(Strict):
    kind: Literal["expected", "explained_sink_only", "unexplained"]
    expected: ExpectedItem | None = None
    sink: SinkDoc | None = None
    rule_id: str | None = None
    delta_gross: Money


class PeriodDiff(Strict):
    outbound_holes: list[str] = Field(default_factory=list)
    inbound: list[BucketRow] = Field(default_factory=list)
    synthetic_delta: dict[str, Money] = Field(default_factory=dict)
    analytic_delta: dict[str, Money] = Field(default_factory=dict)
    material: bool
    blockers: list[str] = Field(default_factory=list)
    snapshot_id: str
    hard_failures: int = 0


class ControlRun(Strict):
    control_id: str
    status: Literal["PASS", "FAIL", "INFO"]
    target: str
    actual: str
    diff: str


class FilingItem(Strict):
    filing_id: str
    period: Period
    state: Literal["open", "filed"] = "open"
    receipt_key: str | None = None
