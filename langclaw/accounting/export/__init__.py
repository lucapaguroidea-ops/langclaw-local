"""
Exports to accounting software — one exporter per target, one registry.

An exporter turns a client's documents rows (imported / posted invoices) into
one downloadable batch (:class:`ExportBatch`). Targets are declared once in
:data:`EXPORTERS`; the ``accounting_export`` tool picks one by name.

- ``saga``: SAGA C. "Import facturi XML" files, zipped into ``intrari/`` (the
  client buys) and ``iesiri/`` (the client sells). Works offline — the
  accountant imports the zip in SAGA.
- ``nextup``: **not implemented** — NextUp's API documentation wasn't
  reachable when this was built, so the exporter raises
  :class:`ExportUnavailable` instead of guessing at an API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ExportUnavailable(RuntimeError):
    """The target is declared but can't export yet (says why)."""


@dataclass(slots=True)
class ExportBatch:
    data: bytes
    """The file to hand over (a zip for SAGA)."""
    filename: str
    content_type: str
    exported: list[str] = field(default_factory=list)
    """``bucket_key`` of every document in the batch."""
    skipped: list[dict[str, str]] = field(default_factory=list)
    """``{"bucket_key", "reason"}`` for documents left out."""


class Exporter(Protocol):
    name: str

    def build(self, rows: list[dict[str, Any]], *, own_cif: str) -> ExportBatch: ...


def _registry() -> dict[str, type]:
    from langclaw.accounting.export.nextup import NextUpExporter
    from langclaw.accounting.export.saga import SagaExporter

    return {"saga": SagaExporter, "nextup": NextUpExporter}


EXPORTERS: dict[str, type] = _registry()


def make_exporter(name: str) -> Exporter:
    """The exporter for *name*.

    Raises:
        ValueError: unknown target (the message lists the known ones).
    """
    try:
        return EXPORTERS[name.strip().lower()]()
    except KeyError:
        raise ValueError(
            f"Unknown export target {name!r}; choose one of: {', '.join(EXPORTERS)}."
        ) from None


__all__ = ["EXPORTERS", "ExportBatch", "ExportUnavailable", "Exporter", "make_exporter"]
