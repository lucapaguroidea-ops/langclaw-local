"""
WP-01 — load the Catalog Cale (``langclaw_acct/catalog/**.yaml``) and fail closed.

- Every YAML is loaded; files with ``mode: additive`` append their rows to the
  catalog of the same name (``60_harvest``). A duplicate id is an error.
- Cross-references are checked both ways: ``ArticoleFlux.write_modules`` ↔
  ``ArticoleWriteModule.used_by_flux`` (drift is a load error), each graph's
  ``allowed_flux`` / ``allowed_hitl``, each HITL kind's ``graph_ids``.
- A WriteModule may be ``active`` only with a green copy-firm fixture
  (``fixture`` + ``approved_at``) — 00_LAW §3 / AGENTS.md.
- Lookups of an unknown ``articol_id`` or HITL kind raise; nothing is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CATALOG_DIR = Path(__file__).parent / "catalog"

#: For each catalog: the list holding its rows and the key naming a row.
ROWS: dict[str, tuple[str, str]] = {
    "ArticoleFlux": ("flux", "articol_id"),
    "ArticoleReconcile": ("flux", "articol_id"),
    "ArticolBon": ("rows", "articol_id"),
    "ArticoleWriteModule": ("modules", "module_id"),
    "ArticoleHITL": ("kinds", "kind"),
    "ArticoleGraph": ("graphs", "graph_id"),
    "ArticoleSourceDoc": ("rows", "source_doc_id"),
    "ArticoleJobs": ("jobs", "job_kind"),
    "ArticoleControls": ("controls", "control_id"),
    "ArticoleFiling": ("filings", "filing_id"),
    "ArticolePins": ("pins", "pins_id"),
    "ArticoleClose": ("kinds", "close_kind"),
    "JevAnnex": ("decisions", "decision_id"),
    "JevValidateV": ("gates", "id"),
}
#: Catalogs whose rows are articole de cale (00_LAW §5).
ARTICOL_CATALOGS = ("ArticoleFlux", "ArticoleReconcile", "ArticolBon")


class CatalogError(ValueError):
    """The catalog can't be loaded as law: a bad file, a duplicate, drift."""


class UnknownId(KeyError):
    """An id that isn't in the catalog — refused, never skipped (AGENTS.md)."""


@dataclass
class Catalog:
    """The loaded Catalog Cale: raw documents by name plus indexed rows."""

    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    rows: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def row(self, catalog: str, row_id: str) -> dict[str, Any]:
        try:
            return self.rows[catalog][row_id]
        except KeyError:
            raise UnknownId(f"{catalog} has no row {row_id!r}") from None

    def articol(self, articol_id: str) -> dict[str, Any]:
        """The articol de cale *articol_id*, from Flux, Reconcile or Bon."""
        for name in ARTICOL_CATALOGS:
            if articol_id in self.rows.get(name, {}):
                return self.rows[name][articol_id]
        raise UnknownId(f"unknown articol_id {articol_id!r}: HITL define_articol, not a guess")

    def hitl_kind(self, kind: str, graph_id: str | None = None) -> dict[str, Any]:
        """HITL kind *kind*; with *graph_id*, also allowed on that graph."""
        row = self.row("ArticoleHITL", kind)
        if graph_id is not None and kind not in self.row("ArticoleGraph", graph_id).get(
            "allowed_hitl", []
        ):
            raise UnknownId(f"HITL kind {kind!r} is not allowed on graph {graph_id!r}")
        return row

    def write_module(self, module_id: str) -> dict[str, Any]:
        return self.row("ArticoleWriteModule", module_id)


def load_catalog(directory: Path | str = CATALOG_DIR) -> Catalog:
    """Load and check every catalog YAML under *directory*.

    Raises:
        CatalogError: unreadable YAML, duplicate id, additive file without a
            base, cross-reference drift, or an ``active`` module without a
            green fixture.
    """
    cat = Catalog()
    additive: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(Path(directory).rglob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise CatalogError(f"{path.name}: not valid YAML: {exc}") from exc
        name = doc.get("catalog") if isinstance(doc, dict) else None
        if not name:
            continue  # an example / annex, not a catalog (e.g. JEV_ANNEX_EXAMPLE)
        if doc.get("mode") == "additive":
            additive.append((path, doc))
        elif name in cat.documents:
            raise CatalogError(f"{path.name}: catalog {name} defined twice")
        else:
            cat.documents[name] = doc
            cat.rows[name] = _index(path, name, doc, {})
    for path, doc in additive:
        name = doc["catalog"]
        if name not in cat.documents:
            raise CatalogError(f"{path.name}: additive rows for missing catalog {name}")
        _index(path, name, doc, cat.rows[name])
    _check(cat)
    return cat


def _index(path: Path, name: str, doc: dict, into: dict) -> dict:
    list_key, id_key = ROWS.get(name, (None, None))
    if list_key is None:
        return into
    for row in doc.get(list_key) or []:
        row_id = row.get(id_key)
        if not row_id:
            raise CatalogError(f"{path.name}: a {name} row has no {id_key}")
        if row_id in into:
            raise CatalogError(f"{path.name}: duplicate {name} id {row_id!r}")
        into[row_id] = row
    return into


def _check(cat: Catalog) -> None:
    flux = cat.rows.get("ArticoleFlux", {})
    modules = cat.rows.get("ArticoleWriteModule", {})
    graphs = cat.rows.get("ArticoleGraph", {})
    kinds = cat.rows.get("ArticoleHITL", {})
    articole = {a for n in ARTICOL_CATALOGS for a in cat.rows.get(n, {})}
    problems: list[str] = []
    for aid, row in flux.items():
        for mid in row.get("write_modules") or []:
            if mid not in modules:
                problems.append(f"flux {aid} names unknown WriteModule {mid}")
            elif aid not in (modules[mid].get("used_by_flux") or []):
                problems.append(f"drift: flux {aid} → {mid}, but {mid}.used_by_flux lacks it")
        if row.get("graph_id") not in graphs:
            problems.append(f"flux {aid} names unknown graph {row.get('graph_id')}")
    for mid, row in modules.items():
        for aid in row.get("used_by_flux") or []:
            if aid not in flux:
                problems.append(f"WriteModule {mid} used_by unknown flux {aid}")
            elif mid not in (flux[aid].get("write_modules") or []):
                problems.append(f"drift: {mid}.used_by_flux has {aid}, which doesn't list it")
        if row.get("status") == "active" and not (row.get("fixture") and row.get("approved_at")):
            problems.append(f"WriteModule {mid} is active without a green copy-firm fixture")
    for gid, row in graphs.items():
        problems += [f"graph {gid} allows unknown HITL kind {k}"
                     for k in row.get("allowed_hitl") or [] if k not in kinds]  # fmt: skip
        problems += [f"graph {gid} allows unknown articol {a}"
                     for a in row.get("allowed_flux") or [] if a not in articole]  # fmt: skip
    for kind, row in kinds.items():
        problems += [f"HITL kind {kind} names unknown graph {g}"
                     for g in row.get("graph_ids") or [] if g not in graphs]  # fmt: skip
    if problems:
        raise CatalogError("Catalog Cale doesn't hold together:\n- " + "\n- ".join(problems))
