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

    # -- status & chat ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

    def turn(self, turn_id: str, wait: int = 0) -> dict[str, Any]:
        return self._request("GET", f"/v1/turns/{turn_id}", params={"wait": wait} if wait else None)

    def turns(self, context_id: str) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/turns", params={"context_id": context_id})["turns"]

    def chat_and_wait(self, content: str, *, context_id: str) -> dict[str, Any]:
        turn = self._request(
            "POST",
            "/v1/chat",
            params={"wait": _WAIT_SECONDS},
            json={"content": content, "context_id": context_id},
        )
        return self._follow(turn)

    # -- workflows -------------------------------------------------------------

    def workflows(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/workflows")["workflows"]

    def workflow(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workflows/{name}")

    def save_workflow(self, name: str, script: str, description: str = "") -> dict[str, Any]:
        return self._request(
            "PUT", f"/v1/workflows/{name}", json={"script": script, "description": description}
        )

    def delete_workflow(self, name: str) -> None:
        self._request("DELETE", f"/v1/workflows/{name}")

    def run_workflow_and_wait(self, name: str, workflow_input: Any) -> dict[str, Any]:
        started = self._request(
            "POST", f"/v1/workflows/{name}/runs", json={"input": workflow_input}
        )
        return self._follow({"turn_id": started["turn_id"], "status": "running"})

    # -- schedules -------------------------------------------------------------

    def schedules(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/schedules")["schedules"]

    def add_schedule(self, **fields: Any) -> str:
        body = {k: v for k, v in fields.items() if v not in (None, "")}
        return self._request("POST", "/v1/schedules", json=body)["id"]

    def delete_schedule(self, job_id: str) -> None:
        self._request("DELETE", f"/v1/schedules/{job_id}")
