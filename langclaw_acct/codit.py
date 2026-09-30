"""
WP-11 — CO.DiT: one client's premises for one period (forma, impozit, tva,
exig, …), written through the catalog's rules.

Write order (ARTICOLE_CODIT_T / _PAIRS): defaults → hard pairs (a violation
raises, nothing is saved) → soft pairs (saved, flagged, HITL kind) → pins
copied for the year. Every axis carries ``value``, ``as_of``, ``source`` and
``certainty``. An empty profile stays empty: nothing defaults to
``tva_platitor`` (ARTICOLE_CODIT_AXES ``never``).

A CO.DiT is a period document, not a sticky tenant flag: a value that
changes against the previous period's is flagged ``A_FLIP``; a ``contested``
axis ``A_CONTESTED`` — both ``codit_premise`` for a person.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from langclaw_acct.catalog import Catalog
from langclaw_acct.types import FiscalDate, Period


class CoDitError(ValueError):
    """A hard pair failed: the CO.DiT is not saved."""

    def __init__(self, rule_id: str, code: str, fix: str) -> None:
        super().__init__(f"{rule_id} {code}: {fix}")
        self.rule_id, self.code, self.fix = rule_id, code, fix


class AxisValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: str | None
    as_of: FiscalDate
    source: str
    certainty: Literal["confirmed", "de_confirmat", "contested"]


class SoftFlag(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rule_id: str
    code: str
    hitl: str
    blocks_file: bool = False


class CoDit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cui: str
    period: Period
    axes: dict[str, AxisValue]
    pins: dict[str, Any] = Field(default_factory=dict)
    flags: list[SoftFlag] = Field(default_factory=list)

    def value(self, axis: str) -> str | None:
        a = self.axes.get(axis)
        return a.value if a else None

    @property
    def blocks_file(self) -> bool:
        return any(f.blocks_file for f in self.flags)


def _holds(cond: dict[str, Any], v: dict[str, str | None]) -> bool | None:
    """Evaluate one catalog condition; None when it names something we don't track."""
    for key, want in cond.items():
        for suffix, op in (("_not_in", "not_in"), ("_in", "in"), ("_ne", "ne")):
            if key.endswith(suffix):
                axis, kind = key[: -len(suffix)], op
                break
        else:
            axis, kind = key, "eq"
        if axis not in _TRACKED:
            return None
        have = v.get(axis)
        if kind == "eq":
            ok = have == want
        elif kind == "ne":
            ok = have != want
        elif kind == "in":
            ok = have in (want or [])
        else:
            ok = have not in (want or [])
        if not ok:
            return False
    return True


_TRACKED = {"forma", "impozit", "tva", "exig"}


def write_codit(
    catalog: Catalog, cui: str, period: str, axes: dict[str, Any], previous: CoDit | None = None
) -> CoDit:
    """Validate and build a period CO.DiT.

    Raises:
        CoDitError: a hard pair (T1–T3, F1–F6) fails.
    """
    parsed = {k: a if isinstance(a, AxisValue) else AxisValue(**a) for k, a in axes.items()}
    values = {k: a.value for k, a in parsed.items()}
    if values.get("tva") == "tva_platitor" and "exig" not in values:
        values["exig"] = "tva_exig_livrare"
        parsed["exig"] = AxisValue(
            value="tva_exig_livrare",
            as_of=parsed["tva"].as_of,
            source="default (T*)",
            certainty=parsed["tva"].certainty,
        )
    pairs = catalog.documents["ArticoleCoDitPairs"]
    for rule in pairs.get("hard", []):
        if rule.get("layer") == "identity_join":
            continue
        cond = {**rule.get("if", {}), **rule.get("and", {})}
        cond = {k: (None if v == "null" else v) for k, v in cond.items()}
        if _holds(cond, values):
            raise CoDitError(
                rule["id"], rule["code"], rule.get("fix") or rule.get("hitl_repair", "")
            )
    flags = [
        SoftFlag(
            rule_id=r["id"], code=r["code"], hitl=r["hitl"], blocks_file=bool(r.get("blocks_file"))
        )
        for r in pairs.get("soft", [])
        if _holds(r.get("if", {}), values)
    ]
    for axis, a in parsed.items():
        if a.certainty == "contested":
            flags.append(SoftFlag(rule_id="A_CONTESTED", code=axis, hitl="codit_premise"))
        if previous is not None and axis in previous.axes and previous.value(axis) != a.value:
            flags.append(SoftFlag(rule_id="A_FLIP", code=axis, hitl="codit_premise"))
    year = int(period[:4])
    pins = next((p for p in catalog.rows["ArticolePins"].values() if p.get("year") == year), {})
    return CoDit(cui=cui, period=period, axes=parsed, pins=dict(pins), flags=flags)
