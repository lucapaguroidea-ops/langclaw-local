"""
``WorkflowFiles`` — the one write path for ``workflows/<name>.graph.json``.

Every surface that creates or edits a workflow file — the control-plane API (and
so the UI), and the agent's ``manage_workflows`` tool — goes through this class,
so they validate the same way and share one version history:

- **validate** — parse + structural checks (errors), and names checked against
  the live tool/subagent catalog (warnings: a tool can be temporarily missing,
  e.g. an MCP server that is down).
- **save / delete** — snapshot the current file into
  ``workflows/.history/<name>/<version>.graph.json`` first, then write/remove, then
  reconcile the registry so the change is live immediately.
- **versions / read_version / restore** — browse and roll back.

A workflow registered in Python code can't be replaced or deleted from here.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from langclaw.workflows.graph.spec import (
    GRAPH_SUFFIX,
    SAFE_NAME,
    GraphSpec,
    GraphSpecError,
    parse_graph_spec,
)

if TYPE_CHECKING:
    from langclaw.workflows.registry import WorkflowRegistry

#: Snapshots kept per workflow (oldest are pruned).
MAX_VERSIONS = 50
_HISTORY = ".history"


class WorkflowFileNotFound(KeyError):
    """No such workflow file (or version)."""

    def __str__(self) -> str:  # KeyError quotes its arg; keep messages readable
        return str(self.args[0]) if self.args else "not found"


class WorkflowFiles:
    """Validated, versioned access to a workflows folder.

    Args:
        directory: The folder holding ``<name>.graph.json`` files.
        registry: Used to refuse overwriting a workflow defined in code.
        reload_cb: Reconciles the folder into the registry after a change.
        catalog: Returns ``{"tools": [...], "subagents": [...]}`` available to
            workflow steps, for validation warnings (``None`` ⇒ not checked).
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        registry: WorkflowRegistry | None = None,
        reload_cb: Callable[[], Any] | None = None,
        catalog: Callable[[], Mapping[str, list[str]] | None] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self._registry = registry
        self._reload_cb = reload_cb
        self._catalog = catalog

    # -- reading -----------------------------------------------------------------

    def names(self) -> list[str]:
        """Workflow file names on disk (valid or not), sorted."""
        if not self.directory.is_dir():
            return []
        return sorted(p.name[: -len(GRAPH_SUFFIX)] for p in self.directory.glob(f"*{GRAPH_SUFFIX}"))

    def read(self, name: str) -> dict[str, Any]:
        """The file as JSON, or ``{"_raw": text}`` when it doesn't parse.

        Raises:
            WorkflowFileNotFound: no such file.
        """
        text = self._path(name).read_text(encoding="utf-8") if self.exists(name) else None
        if text is None:
            raise WorkflowFileNotFound(f"No workflow file {name!r}.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}
        return data if isinstance(data, dict) else {"_raw": text}

    def exists(self, name: str) -> bool:
        return bool(SAFE_NAME.match(name or "")) and self._path(name).is_file()

    # -- validation --------------------------------------------------------------

    def validate(self, name: str, graph: Mapping[str, Any] | str) -> dict[str, Any]:
        """Check a workflow without saving it.

        Returns:
            ``{"valid": bool, "errors": [...], "warnings": [...]}`` — errors block
            saving; warnings (unknown tools/subagents) don't.
        """
        try:
            spec = parse_graph_spec(name, _as_graph(graph))
        except GraphSpecError as exc:
            return {"valid": False, "errors": exc.errors, "warnings": []}
        return {"valid": True, "errors": [], "warnings": self._warnings(spec)}

    def _warnings(self, spec: GraphSpec) -> list[str]:
        catalog = self._catalog() if self._catalog else None
        if not catalog:
            return []
        tools, subagents = set(catalog.get("tools", [])), set(catalog.get("subagents", []))
        out = []
        for nid, node in spec.nodes.items():
            if node.type == "tool" and node.tool not in tools:
                out.append(f"node {nid!r}: tool {node.tool!r} is not available right now")
            if node.type == "subagent" and node.subagent not in subagents:
                out.append(f"node {nid!r}: subagent {node.subagent!r} is not registered")
        return out

    # -- writing -----------------------------------------------------------------

    def save(self, name: str, graph: Mapping[str, Any] | str) -> dict[str, Any]:
        """Validate and write ``<name>.graph.json``, snapshotting the previous version.

        Returns:
            ``{"name", "created": bool, "warnings": [...]}``.

        Raises:
            GraphSpecError: invalid graph (lists every problem) or name.
            ValueError: *name* is a workflow defined in code.
        """
        self._refuse_code_workflow(name)
        spec = parse_graph_spec(name, _as_graph(graph))
        created = not self.exists(name)
        if not created:
            self._snapshot(name)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(name).write_text(spec.to_file(), encoding="utf-8")
        logger.info(f"Saved workflow file {name!r} ({'created' if created else 'updated'})")
        self._reload()
        return {"name": name, "created": created, "warnings": self._warnings(spec)}

    def delete(self, name: str) -> None:
        """Remove ``<name>.graph.json`` (its history is kept, so it can be restored).

        Raises:
            WorkflowFileNotFound: no such file.
            ValueError: *name* is a workflow defined in code.
        """
        self._refuse_code_workflow(name)
        if not self.exists(name):
            raise WorkflowFileNotFound(f"No workflow file {name!r}.")
        self._snapshot(name)
        self._path(name).unlink()
        logger.info(f"Deleted workflow file {name!r}")
        self._reload()

    # -- versions ----------------------------------------------------------------

    def versions(self, name: str) -> list[dict[str, Any]]:
        """Saved snapshots of *name*, newest first: ``[{"version", "saved_at"}]``."""
        folder = self._history(name)
        if not folder.is_dir():
            return []
        out = []
        for path in sorted(folder.glob(f"*{GRAPH_SUFFIX}"), reverse=True):
            version = path.name[: -len(GRAPH_SUFFIX)]
            out.append({"version": version, "saved_at": _version_time(version)})
        return out

    def read_version(self, name: str, version: str) -> dict[str, Any]:
        """One snapshot's JSON.

        Raises:
            WorkflowFileNotFound: unknown name or version.
        """
        path = self._version_path(name, version)
        if not path.is_file():
            raise WorkflowFileNotFound(f"No version {version!r} of workflow {name!r}.")
        return json.loads(path.read_text(encoding="utf-8"))

    def restore(self, name: str, version: str) -> dict[str, Any]:
        """Make snapshot *version* the current file (the current one is snapshotted)."""
        return self.save(name, self.read_version(name, version))

    # -- internals ---------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.directory / f"{name}{GRAPH_SUFFIX}"

    def _history(self, name: str) -> Path:
        if not SAFE_NAME.match(name or ""):
            raise WorkflowFileNotFound(f"No workflow file {name!r}.")
        return self.directory / _HISTORY / name

    def _version_path(self, name: str, version: str) -> Path:
        if not version or not all(c.isalnum() or c in "-_" for c in version):
            raise WorkflowFileNotFound(f"No version {version!r} of workflow {name!r}.")
        return self._history(name) / f"{version}{GRAPH_SUFFIX}"

    def _snapshot(self, name: str) -> None:
        folder = self._history(name)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        (folder / f"{stamp}{GRAPH_SUFFIX}").write_text(
            self._path(name).read_text(encoding="utf-8"), encoding="utf-8"
        )
        for old in sorted(folder.glob(f"*{GRAPH_SUFFIX}"))[:-MAX_VERSIONS]:
            old.unlink()

    def _refuse_code_workflow(self, name: str) -> None:
        spec = self._registry.get(name) if self._registry is not None else None
        if spec is not None and spec.graph_spec is None:
            raise ValueError(f"{name!r} is defined in code; a workflow file cannot replace it.")

    def _reload(self) -> None:
        if self._reload_cb is not None:
            self._reload_cb()


def _as_graph(graph: Mapping[str, Any] | str) -> dict[str, Any] | str:
    return dict(graph) if isinstance(graph, Mapping) else graph


def _version_time(version: str) -> str:
    try:
        stamp = datetime.strptime(version, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    except ValueError:
        return ""
    return stamp.isoformat(timespec="seconds")
