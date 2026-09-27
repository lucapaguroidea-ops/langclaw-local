"""
Review requests on channels — how a paused workflow run asks a person to decide.

When a run pauses for review, :meth:`ControlPlane.notify_review_requests` sends a
request to where the run started (and to ``workflows.review_channel`` /
``review_chat_id`` when set). Each channel renders it via
:meth:`BaseChannel.send_review_request`: Telegram with Approve / Edit / Reject
buttons, others as text with the commands to answer. When the review is answered
— from any surface — :meth:`BaseChannel.mark_review_resolved` updates each sent
request, so every place shows the same outcome.

Button payloads are ``wfr:<a|e|r>:<key>`` where ``key`` is the review's short id
(:func:`langclaw.workflows.graph.runs.review_key`), well under Telegram's 64 bytes.
"""

from __future__ import annotations

import json
from typing import Any

#: Button action code → review action.
ACTIONS = {"a": "approve", "e": "edit", "r": "reject"}
_PREFIX = "wfr"
_PAST = {"approve": "✅ Approved", "edit": "✏️ Edited and approved", "reject": "❌ Rejected"}


def review_callback(action: str, key: str) -> str:
    """Encode a button payload, e.g. ``review_callback("approve", key)``."""
    code = next(c for c, a in ACTIONS.items() if a == action)
    return f"{_PREFIX}:{code}:{key}"


def parse_review_callback(data: str) -> tuple[str, str] | None:
    """``(action, key)`` from a button payload, or ``None`` if it isn't one."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != _PREFIX or parts[1] not in ACTIONS:
        return None
    return ACTIONS[parts[1]], parts[2]


def _fmt(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def review_request_text(request: dict[str, Any], *, commands: bool = True) -> str:
    """Plain-text body of a review request.

    Args:
        request: ``{"run_id", "workflow", "client", "message", "data", "editable", ...}``
            (``client``: the run's client name, when clients are enabled).
        commands: Append the ``/workflows`` commands to answer (for channels
            without buttons).
    """
    title = f"⏸ Review needed — {request.get('workflow', 'workflow')}"
    if request.get("client"):
        title += f" · {request['client']}"
    lines = [title, "", request["message"]]
    data = request.get("data") or {}
    for key, value in data.items():
        text = _fmt(value)
        if len(text) > 1500:
            text = text[:1500] + "…"
        lines += ["", f"{key}:", text]
    lines += ["", f"Run: {request['run_id']}"]
    if commands:
        run = request["run_id"]
        lines += [
            "",
            f"Answer: /workflows approve {run}  ·  /workflows reject {run}",
        ]
        if request.get("editable"):
            lines.append(f'Or correct it: /workflows edit {run} {{"{request["editable"]}": ...}}')
    return "\n".join(lines)


def edit_instructions(request: dict[str, Any]) -> str:
    """What to send to correct a review's editable value."""
    key = request.get("editable") or ""
    run = request["run_id"]
    if not key:
        return f"This review has nothing to edit — approve or reject it (run {run})."
    current = (request.get("data") or {}).get(key)
    payload = json.dumps({key: current}, ensure_ascii=False, default=str)
    return (
        f"To correct {key!r}, send this command with your changes:\n\n"
        f"/workflows edit {run} {payload}"
    )


def resolution_line(decision: dict[str, Any]) -> str:
    """One line saying who answered, how, and where."""
    verb = _PAST.get(decision.get("action", ""), "Answered")
    who = decision.get("by") or "someone"
    via = decision.get("via")
    return f"{verb} by {who}" + (f" via {via}" if via else "")
