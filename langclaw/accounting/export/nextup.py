"""
NextUp — placeholder until its API is known.

NextUp (nextup.ro) exposes an API for invoices, clients and products, but its
documentation couldn't be read when this was written, so nothing is sent: the
exporter raises :class:`ExportUnavailable`. To finish it, map a documents row
to NextUp's invoice payload here and post it with the client's API key.
"""

from __future__ import annotations

from typing import Any

from langclaw.accounting.export import ExportBatch, ExportUnavailable


class NextUpExporter:
    name = "nextup"

    def build(self, rows: list[dict[str, Any]], *, own_cif: str) -> ExportBatch:
        raise ExportUnavailable(
            "NextUp export isn't implemented yet: its API documentation wasn't available. "
            "Use target 'saga', or share NextUp's API docs to wire it."
        )
