"""
Shared harness for the workflow-pattern cookbook.

Each pattern file (``classify_and_act.py``, ``adversarial_verify.py``, …) is a real
LangGraph workflow registered with ``app.workflow(name, graph=builder)``. You can
run it on its own or drive it through the probe. They share:

1. ``make_app()`` — a WebSocket-only Langclaw app with workflows on and the
   outward-facing channels forced off (so an example never hijacks a real bot).

2. A couple of tolerant parsers (``pick_label`` / ``norm``) for the few places that
   read a *subagent's* free-text reply — real models don't always honour "reply with
   only X", so the control flow degrades gracefully instead of crashing.

Reaching langclaw from a node
-----------------------------
A node is plain LangGraph code. It reaches langclaw's capabilities through
``steps()``, and each pattern uses them *by fit*:

- **``steps().llm(prompt, schema=Model)``** — one model call, no tools, no agent loop,
  for a one-shot judgment (classify / score / compare / extract). With a Pydantic
  ``schema`` you get a validated object back from a single call — no parsing.
- **``steps().subagent(type, prompt)``** — when the leaf does multi-step work with its
  own tools in an isolated context (research a contender, gather evidence).
- **``steps().tool(name, **kwargs)``** — call a registered tool directly.

LangGraph supplies the control flow: conditional edges for routing, ``Send`` for
fan-out (each branch is its own checkpointed task), and loops back to a node for
"until done". Every node boundary is a checkpoint, so a crash resumes mid-run.

Run one pattern:
    uv run python -m examples.workflow_patterns.tournament
    uv run langclaw probe '/workflows run prioritize {"items": ["A","B"], "criterion": "impact"}'
"""

from __future__ import annotations

import re

from langclaw import Langclaw
from langclaw.config.schema import LangclawConfig


def make_app(system_prompt: str = "") -> Langclaw:
    """A safe, WebSocket-only app with workflows enabled."""
    config = LangclawConfig()
    config.channels.websocket.enabled = True
    config.channels.telegram.enabled = False
    config.channels.discord.enabled = False
    config.channels.matrix.enabled = False
    config.workflows.enabled = True
    # Keyless-of-LLM web search: Brave if a key is configured, else DuckDuckGo.
    config.tools.search_backend = "brave" if config.tools.brave_api_key else "duckduckgo"
    return Langclaw(config=config, system_prompt=system_prompt)


# --- tolerant parsers (only needed where a subagent returns free text) ------


def pick_label(text: str, choices: list[str], default: str = "") -> str:
    """Return the first *choice* that appears in *text* as a whole word (case-insensitive).

    Whole-word so a label isn't matched inside another token — e.g. ``"supported"``
    must not match inside ``"unsupported"``.
    """
    low = text.lower()
    for c in choices:
        if re.search(rf"\b{re.escape(c.lower())}\b", low):
            return c
    return default or (choices[0] if choices else "")


def norm(s: str) -> str:
    """Normalise an item for dedup (lowercase, strip punctuation/whitespace)."""
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def links(hits: object) -> str:
    """Render up to three search hits as markdown links."""
    if not isinstance(hits, list) or not hits:
        return "_no results_"
    return "\n".join(
        f"- [{h.get('title') or 'link'}]({h.get('url', '')})"
        for h in hits[:3]
        if isinstance(h, dict)
    )
