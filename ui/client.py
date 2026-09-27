"""Typed client for the langclaw control-plane API (see docs/guides/control-plane-api.md)."""

from __future__ import annotations

from typing import Any

import httpx

# Long-poll window per request; the API caps ``wait`` at 120 s.
_WAIT_SECONDS = 25
# Give up following a turn after this many long-polls (~10 minutes).
_MAX_POLLS = 24


class LangclawError(Exception):
    """An API call failed. ``status`` is the HTTP status (0 when unreachable)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class LangclawClient:
    """Small synchronous client; one instance per UI session."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=_WAIT_SECONDS + 10,
            transport=transport,
        )

    # -- plumbing --------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise LangclawError(f"Cannot reach langclaw: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {"error": resp.text}
        if resp.status_code >= 400:
            message = body.get("error") if isinstance(body, dict) else None
            raise LangclawError(message or f"HTTP {resp.status_code}", resp.status_code)
        return body

    def _follow(self, turn: dict[str, Any]) -> dict[str, Any]:
        for _ in range(_MAX_POLLS):
            if turn.get("status") == "done":
                break
            turn = self.turn(turn["turn_id"], wait=_WAIT_SECONDS)
        return turn

    # -- status ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

    def catalog(self) -> dict[str, list[str]]:
        return self._request("GET", "/v1/catalog")

    def turn(self, turn_id: str, wait: int = 0) -> dict[str, Any]:
        return self._request("GET", f"/v1/turns/{turn_id}", params={"wait": wait} if wait else None)

    # -- documents -------------------------------------------------------------

    def documents(
        self,
        q: str = "",
        *,
        semantic: bool = False,
        fields: dict[str, str] | None = None,
        tenant: str = "",
        **filters: Any,
    ) -> dict:
        """``{"documents", "count", "mode", "semantic"}`` — see ``GET /v1/documents``.

        *fields* filters on extracted extras (sent as ``field.<name>=<value>``).
        """
        params = {k: v for k, v in filters.items() if v}
        params.update({f"field.{k}": v for k, v in (fields or {}).items()})
        if tenant:
            params["tenant"] = tenant
        if q:
            params["q"] = q
        if semantic:
            params["semantic"] = "true"
        return self._request("GET", "/v1/documents", params=params)

    def document(self, key: str, *, tenant: str = "") -> dict[str, Any]:
        """``{"document": {...}, "link": "https://..."}``."""
        return self._request(
            "GET", f"/v1/documents/{key}", params={"tenant": tenant} if tenant else None
        )

    # -- clients (tenants) -------------------------------------------------------

    def tenants(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/tenants")["tenants"]

    def save_tenant(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/v1/tenants/{tenant_id}", json=payload)

    def delete_tenant(self, tenant_id: str) -> bool:
        self._request("DELETE", f"/v1/tenants/{tenant_id}")
        return True

    # -- workflows -------------------------------------------------------------

    def workflows(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/workflows")["workflows"]

    def workflow(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workflows/{name}")

    def save_workflow(self, name: str, graph: dict[str, Any]) -> dict[str, Any]:
        """Create/replace ``workflows/<name>.graph.json`` (400 lists every problem)."""
        return self._request("PUT", f"/v1/workflows/{name}", json=graph)

    def validate_workflow(self, name: str, graph: dict[str, Any]) -> dict[str, Any]:
        """``{"valid", "errors", "warnings"}`` without saving."""
        return self._request("POST", f"/v1/workflows/{name}/validate", json=graph)

    def delete_workflow(self, name: str) -> bool:
        self._request("DELETE", f"/v1/workflows/{name}")
        return True

    def versions(self, name: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/workflows/{name}/versions")["versions"]

    def version(self, name: str, version: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workflows/{name}/versions/{version}")

    def restore(self, name: str, version: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workflows/{name}/versions/{version}/restore")

    # -- runs & reviews ------------------------------------------------------------

    def runs(self, workflow: str = "", *, status: str = "", limit: int = 50) -> list[dict]:
        params = {"limit": limit, **({"status": status} if status else {})}
        if workflow:
            return self._request("GET", f"/v1/workflows/{workflow}/runs", params=params)["runs"]
        return self._request("GET", "/v1/runs", params=params)["runs"]

    def run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}")

    def reviews(self, workflow: str = "") -> list[dict[str, Any]]:
        params = {"workflow": workflow} if workflow else None
        return self._request("GET", "/v1/reviews", params=params)["reviews"]

    def answer_review(
        self,
        run_id: str,
        action: str,
        *,
        interrupt_id: str = "",
        data: dict[str, Any] | None = None,
        by: str = "ui",
    ) -> dict[str, Any]:
        """Approve / edit / reject. A 409 ``LangclawError`` means someone answered first."""
        body = {"action": action, "interrupt_id": interrupt_id, "by": by, "via": "ui"}
        if data:
            body["data"] = data
        return self._request("POST", f"/v1/runs/{run_id}/review", json=body)

    def start_run(self, name: str, workflow_input: Any, *, tenant: str = "") -> dict[str, Any]:
        """Start a run (for client *tenant*, if given); returns ``{"run_id", "turn_id"}``."""
        body: dict[str, Any] = {"input": workflow_input}
        if tenant:
            body["tenant"] = tenant
        return self._request("POST", f"/v1/workflows/{name}/runs", json=body)

    def follow_turn(self, turn_id: str) -> dict[str, Any]:
        """Long-poll a turn (a run's progress lines and output) until it finishes."""
        return self._follow({"turn_id": turn_id, "status": "running"})

    # -- schedules -------------------------------------------------------------

    def schedules(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/schedules")["schedules"]

    def add_schedule(self, **fields: Any) -> str:
        body = {k: v for k, v in fields.items() if v not in (None, "")}
        return self._request("POST", "/v1/schedules", json=body)["id"]

    def delete_schedule(self, job_id: str) -> bool:
        self._request("DELETE", f"/v1/schedules/{job_id}")
        return True
