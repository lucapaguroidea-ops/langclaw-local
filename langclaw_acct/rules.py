"""
WP-09 — explained rules: the only way a sink document the expected set won't
have (bank fees, SAGA-native entries) stops counting as unexplained.

Versioned: saving a rule id again makes a new version; the latest applies.
Every version says who made it and why — a rule without a reason is refused,
because a rule is exactly how an unexplained line would otherwise be hidden
(``cannot: hide_unexplained_without_rule``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from langclaw_acct.controls import ExplainedRule

_SLUG = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class RuleError(ValueError):
    """A rule that can't be saved: bad id, no reason, matches nothing."""


@dataclass(frozen=True, slots=True)
class RuleVersion:
    rule: ExplainedRule
    version: int
    reason: str
    by: str


def _validate(rule: ExplainedRule, reason: str) -> None:
    if not _SLUG.match(rule.rule_id):
        raise RuleError(f"rule id {rule.rule_id!r} must be a lowercase slug")
    if not (rule.doc_class or rule.number_prefix):
        raise RuleError("a rule must name a document class or a number prefix")
    if not reason.strip():
        raise RuleError("say why these documents are explained")


class RuleStore(Protocol):
    async def save(self, rule: ExplainedRule, *, reason: str, by: str) -> RuleVersion: ...
    async def active(self) -> list[ExplainedRule]: ...
    async def get(self, rule_id: str) -> RuleVersion | None: ...


@dataclass
class MemoryRuleStore:
    versions: dict[str, list[RuleVersion]] = field(default_factory=dict)

    async def save(self, rule: ExplainedRule, *, reason: str, by: str) -> RuleVersion:
        _validate(rule, reason)
        history = self.versions.setdefault(rule.rule_id, [])
        v = RuleVersion(rule, len(history) + 1, reason, by)
        history.append(v)
        return v

    async def active(self) -> list[ExplainedRule]:
        return [h[-1].rule for h in self.versions.values()]

    async def get(self, rule_id: str) -> RuleVersion | None:
        h = self.versions.get(rule_id)
        return h[-1] if h else None


class PgRuleStore:
    """Rules in ``<schema>.acct_rules`` (one row per version)."""

    def __init__(self, pool: Any, schema: str) -> None:
        if not _IDENT.match(schema):
            raise ValueError(f"bad schema name {schema!r}")
        self._pool, self._s, self._ready = pool, schema, False

    async def ensure(self) -> None:
        if self._ready:
            return
        async with self._pool.acquire() as con:
            await con.execute(f"""
                CREATE SCHEMA IF NOT EXISTS {self._s};
                CREATE TABLE IF NOT EXISTS {self._s}.acct_rules (
                    rule_id text NOT NULL, version int NOT NULL,
                    doc_class text, number_prefix text,
                    reason text NOT NULL, by_actor text NOT NULL DEFAULT '',
                    at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (rule_id, version));
            """)
        self._ready = True

    async def save(self, rule: ExplainedRule, *, reason: str, by: str) -> RuleVersion:
        _validate(rule, reason)
        await self.ensure()
        async with self._pool.acquire() as con, con.transaction():
            await con.execute("SELECT pg_advisory_xact_lock(hashtext($1))", rule.rule_id)
            n = await con.fetchval(
                f"SELECT coalesce(max(version), 0) + 1 FROM {self._s}.acct_rules "
                "WHERE rule_id = $1",
                rule.rule_id,
            )
            await con.execute(
                f"INSERT INTO {self._s}.acct_rules "
                "(rule_id, version, doc_class, number_prefix, reason, by_actor) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                rule.rule_id, n, rule.doc_class, rule.number_prefix, reason, by,
            )  # fmt: skip
        return RuleVersion(rule, n, reason, by)

    async def _latest(self, where: str = "", *args: Any) -> list[Any]:
        await self.ensure()
        async with self._pool.acquire() as con:
            return await con.fetch(
                f"SELECT DISTINCT ON (rule_id) * FROM {self._s}.acct_rules {where} "
                "ORDER BY rule_id, version DESC",
                *args,
            )

    async def active(self) -> list[ExplainedRule]:
        return [ExplainedRule(r["rule_id"], r["doc_class"], r["number_prefix"])
                for r in await self._latest()]  # fmt: skip

    async def get(self, rule_id: str) -> RuleVersion | None:
        rows = await self._latest("WHERE rule_id = $1", rule_id)
        if not rows:
            return None
        r = rows[0]
        rule = ExplainedRule(r["rule_id"], r["doc_class"], r["number_prefix"])
        return RuleVersion(rule, r["version"], r["reason"], r["by_actor"])
