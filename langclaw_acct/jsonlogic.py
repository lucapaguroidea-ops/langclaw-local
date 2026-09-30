"""
The json-logic rules the pack ships (``fixtures/architecture.jsonlogic.json``),
evaluated as written so the catalog — not code — says when a pack may emit.

Only the operators the pack uses: ``var`` (dotted path), ``==``, ``!==``,
``and``, ``or``, ``!``, ``in``. Anything else is an error, not a guess.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

RULES_FILE = Path(__file__).parent / "fixtures" / "architecture.jsonlogic.json"


@cache
def rules() -> dict[str, Any]:
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))["rules"]


def _var(ctx: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(ctx, dict) or part not in ctx:
            return None
        ctx = ctx[part]
    return ctx


def evaluate(rule: Any, ctx: dict[str, Any]) -> Any:
    if not isinstance(rule, dict):
        return rule
    if len(rule) != 1:
        raise ValueError(f"json-logic node must have one operator: {rule}")
    op, args = next(iter(rule.items()))
    if op == "var":
        return _var(ctx, args)
    vals = [evaluate(a, ctx) for a in (args if isinstance(args, list) else [args])]
    if op == "==":
        return vals[0] == vals[1]
    if op == "!==":
        return vals[0] != vals[1]
    if op == "and":
        return all(vals)
    if op == "or":
        return any(vals)
    if op == "!":
        return not vals[0]
    if op == "in":
        return vals[0] in (vals[1] or [])
    raise ValueError(f"json-logic operator {op!r} is not supported")


def check(rule_name: str, ctx: dict[str, Any]) -> bool:
    """Evaluate the pack's rule *rule_name* against *ctx*."""
    return bool(evaluate(rules()[rule_name], ctx))
