"""Poarta Primară: explained rules (WP-09) and monthly_close (WP-10)."""

from __future__ import annotations

import os
import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from langclaw_acct.catalog import load_catalog
from langclaw_acct.close import close_graph
from langclaw_acct.controls import ExplainedRule
from langclaw_acct.rules import MemoryRuleStore, PgRuleStore, RuleError
from langclaw_acct.sinks import RegistruJurnalEye, read_registru_jurnal
from langclaw_acct.types import ExpectedItem

DSN = os.environ.get("LANGCLAW_TEST_POSTGRES_DSN", "")
RJ = (
    "data,nr_document,fel_document,explicatie,cont_debit,cont_credit,suma\n"
    "2026-09-10,A9,factura_intrare,Servicii,628,401.00001,100\n"
    "2026-09-10,A9,factura_intrare,TVA,4426,401.00001,21\n"
    "2026-09-30,C1,comision,Comision banca,627,5121,5\n"
)
A9 = ExpectedItem(job_id="j-A9", doc_class="factura_intrare", number="A9", date="2026-09-10",
                  partner_cui="2", gross="121.00", net="100", vat="21")  # fmt: skip


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def _graph(catalog, rules, items=(A9,)):
    eye = RegistruJurnalEye()
    eye.load("1", "2026-09", read_registru_jurnal("generic_csv", RJ.encode()))

    async def load(_cui: str, _period: str) -> list[ExpectedItem]:
        return list(items)

    return close_graph(catalog, eye, rules, load).compile(checkpointer=MemorySaver())


CFG = {"configurable": {"thread_id": "close:1:2026-09"}}


async def test_rules_need_an_id_a_match_and_a_reason() -> None:
    store = MemoryRuleStore()
    with pytest.raises(RuleError, match="why"):
        await store.save(ExplainedRule("bank_fees", doc_class="comision"), reason=" ", by="ana")
    with pytest.raises(RuleError, match="name a document class"):
        await store.save(ExplainedRule("bank_fees"), reason="fees", by="ana")
    v1 = await store.save(ExplainedRule("bank_fees", doc_class="comision"), reason="f", by="a")
    v2 = await store.save(ExplainedRule("bank_fees", number_prefix="C"), reason="g", by="a")
    assert (v1.version, v2.version) == (1, 2)
    assert await store.active() == [ExplainedRule("bank_fees", number_prefix="C")]


async def test_file_is_refused_on_a_material_month_until_a_rule_explains_it(catalog) -> None:
    rules = MemoryRuleStore()
    g = _graph(catalog, rules)
    ask = (await g.ainvoke({"cui": "1", "period": "2026-09"}, CFG))["__interrupt__"][0].value
    assert ask["material"] and not ask["may_file"]
    assert "unexplained in books: rj:comision:C1:2026-09-30" in ask["blockers"]

    again = await g.ainvoke(Command(resume={"action": "file"}), CFG)
    assert "file refused" in again["__interrupt__"][0].value["refused"][-1]

    missing = await g.ainvoke(Command(resume={"action": "hold", "explained_rule": "fees"}), CFG)
    assert "no saved rule 'fees'" in missing["__interrupt__"][0].value["refused"][-1]

    await rules.save(ExplainedRule("fees", doc_class="comision"), reason="bank fees", by="ana")
    clean = await g.ainvoke(Command(resume={"action": "hold", "explained_rule": "fees"}), CFG)
    ask = clean["__interrupt__"][0].value
    assert not ask["material"] and ask["may_file"]
    done = await g.ainvoke(Command(resume={"action": "file", "by": "ana"}), CFG)
    assert done["outcome"] == "file" and done["decided_by"] == "ana"


async def test_hold_ends_the_close_and_a_missing_job_blocks(catalog) -> None:
    extra = A9.model_copy(update={"job_id": "j-F404", "number": "F404"})
    g = _graph(catalog, MemoryRuleStore(), items=(A9, extra))
    ask = (await g.ainvoke({"cui": "1", "period": "2026-09"}, CFG))["__interrupt__"][0].value
    assert "outbound hole: j-F404" in ask["blockers"]
    out = await g.ainvoke(Command(resume={"action": "hold"}), CFG)
    assert out["outcome"] == "hold"


async def test_close_resume_payload_is_strict(catalog) -> None:
    g = _graph(catalog, MemoryRuleStore())
    await g.ainvoke({"cui": "1", "period": "2026-09"}, CFG)
    with pytest.raises(Exception, match="extra"):
        await g.ainvoke(Command(resume={"action": "file", "force": True}), CFG)


@pytest.mark.skipif(not DSN, reason="LANGCLAW_TEST_POSTGRES_DSN not set")
async def test_postgres_rules_are_versioned() -> None:
    import asyncpg

    schema = f"acct_test_{uuid.uuid4().hex[:8]}"
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        store = PgRuleStore(pool, schema)
        await store.save(ExplainedRule("fees", doc_class="comision"), reason="a", by="ana")
        v = await store.save(ExplainedRule("fees", number_prefix="C"), reason="b", by="ion")
        assert v.version == 2 and (await store.get("fees")).by == "ion"
        assert await store.active() == [ExplainedRule("fees", None, "C")]
        assert await store.get("nope") is None
    finally:
        async with pool.acquire() as con:
            await con.execute(f"DROP SCHEMA {schema} CASCADE")
        await pool.close()
