"""The UI's draft-editing helpers (ui/editor.py) — pure, no Streamlit needed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ui"))

import editor  # noqa: E402

from langclaw.workflows.graph import parse_graph_spec  # noqa: E402
from tests.test_graph_workflows import DOC_FLOW  # noqa: E402


@pytest.mark.parametrize("template", list(editor.TEMPLATES))
def test_templates_are_valid_workflows(template: str) -> None:
    parse_graph_spec("new_flow", editor.new_draft(template))


def test_new_draft_is_a_copy() -> None:
    first = editor.new_draft("Blank (one LLM step)")
    first["nodes"].clear()
    assert editor.new_draft("Blank (one LLM step)")["nodes"]


@pytest.mark.parametrize("node_type", editor.NODE_TYPES)
def test_node_templates_have_their_type(node_type: str) -> None:
    assert editor.node_template(node_type)["type"] == node_type


def test_add_node_connects_the_first_one_from_start() -> None:
    draft = editor.add_node({"nodes": {}, "edges": []}, "first", "llm")
    assert draft["edges"] == [{"from": "START", "to": "first"}]
    draft = editor.add_node(draft, "second", "tool")
    assert draft["edges"] == [{"from": "START", "to": "first"}]  # no second START edge
    assert set(draft["nodes"]) == {"first", "second"}


@pytest.mark.parametrize("bad", ["", "1x", "has-dash", "START", "input"])
def test_add_node_rejects_bad_ids(bad: str) -> None:
    with pytest.raises(ValueError):
        editor.add_node({"nodes": {}}, bad, "llm")


def test_add_node_rejects_duplicates_and_unknown_types() -> None:
    draft = editor.add_node({}, "a", "llm")
    with pytest.raises(ValueError, match="already"):
        editor.add_node(draft, "a", "llm")
    with pytest.raises(ValueError, match="Unknown node type"):
        editor.add_node(draft, "b", "script")


def test_remove_node_cleans_up_every_reference() -> None:
    draft = editor.remove_node(DOC_FLOW, "review")
    assert "review" not in draft["nodes"]
    assert all(e["to"] != "review" and e["from"] != "review" for e in draft["edges"])
    check = draft["nodes"]["check"]
    assert check["rules"] == [] and check["else"] == "save"
    parse_graph_spec("doc_flow", draft)  # still a valid workflow
    assert "review" in DOC_FLOW["nodes"]  # the original is untouched


def test_remove_node_clears_output_and_routes() -> None:
    draft = editor.remove_node(DOC_FLOW, "save")
    assert draft["output"] == ""
    assert draft["nodes"]["check"]["else"] == "END"
    joined = {"nodes": {"a": {}, "b": {}, "c": {}}, "edges": [{"from": ["a", "b"], "to": "c"}]}
    assert editor.remove_node(joined, "a")["edges"] == [{"from": "b", "to": "c"}]


def test_edges_round_trip_through_table_rows() -> None:
    edges = [{"from": "START", "to": "a"}, {"from": ["a", "b"], "to": "c"}]
    rows = editor.edges_to_rows({"edges": edges})
    assert rows[1] == {"from": "a, b", "to": "c"}
    assert editor.rows_to_edges([*rows, {"from": "", "to": "x"}, {"from": "a"}]) == edges


def test_input_fields_round_trip() -> None:
    fields = {
        "key": {"type": "string", "description": "Bucket key"},
        "limit": {"type": "integer", "required": False},
    }
    assert editor.rows_to_input(editor.input_to_rows({"input": fields})) == fields
    assert editor.rows_to_input([{"name": " ", "type": "string"}]) == {}


def test_coerce_input_types_form_values() -> None:
    fields = {
        "text": {"type": "string"},
        "n": {"type": "integer"},
        "x": {"type": "number"},
        "flag": {"type": "boolean"},
        "tags": {"type": "array"},
        "opt": {"type": "string", "required": False},
    }
    values = {"text": "hi", "n": "3", "x": "0.5", "flag": "yes", "tags": '["a"]', "opt": ""}
    assert editor.coerce_input(fields, values) == {
        "text": "hi",
        "n": 3,
        "x": 0.5,
        "flag": True,
        "tags": ["a"],
    }
    with pytest.raises(ValueError, match="tags is not valid JSON"):
        editor.coerce_input({"tags": {"type": "array"}}, {"tags": "[oops"})


def test_mermaid_html_escapes_the_diagram() -> None:
    html = editor.mermaid_html("graph TD; a-->b<script>")
    assert "a--&gt;b&lt;script&gt;" in html
    assert "mermaid" in html
    assert 'securityLevel: "strict"' in html


def test_graph_diff_shows_changes_only() -> None:
    changed = {**DOC_FLOW, "description": "new words"}
    diff = editor.graph_diff(DOC_FLOW, changed)
    assert '+  "description": "new words",' in diff
    assert editor.graph_diff(DOC_FLOW, DOC_FLOW) == ""


def test_parse_field_filters() -> None:
    assert editor.parse_field_filters("") == ({}, [])
    assert editor.parse_field_filters("jurisdiction=State of Delaware, tax_id = IT0123") == (
        {"jurisdiction": "State of Delaware", "tax_id": "IT0123"},
        [],
    )
    assert editor.parse_field_filters("notice.days=90, oops") == (
        {"notice.days": "90"},
        ["'oops' — write name=value"],
    )


def test_documents_page_caption_counts_every_match() -> None:
    page = {"count": 50, "offset": 50, "total": 312, "amounts": {"RON": 12345.5, "EUR": 10}}
    assert editor.documents_caption(page) == (
        "Showing **51–100** of **312** · total 12,345.50 RON, 10.00 EUR"
    )
    assert editor.documents_caption({"count": 3, "offset": 0, "total": 3, "amounts": {}}) == (
        "Showing **1–3** of **3**"
    )
    ranked = {"count": 50, "offset": 0, "total": None, "mode": "semantic"}
    assert editor.documents_caption(ranked) == "Showing **1–50**, ranked by meaning"
    assert editor.documents_caption({"count": 0, "offset": 0, "total": 0}) == (
        "No documents match."
    )
    assert editor.documents_caption({"count": 0, "offset": 50, "total": None}) == (
        "No more documents."
    )


def test_tenant_payload_from_the_clients_form() -> None:
    payload = editor.tenant_payload(
        name=" ACME SRL ",
        tax_id="RO12345678",
        chats_text="telegram:-100acme\n\n  telegram:42 \n",
        review_chat="telegram:-100acme-review",
        profile={"vat_payer": True, "tax_regime": "micro", "caen": "6201"},
        extra_json='{"fiscal_year_start": "01-01"}',
    )
    assert payload == {
        "name": "ACME SRL",
        "tax_id": "RO12345678",
        "chats": ["telegram:-100acme", "telegram:42"],
        "review_chat": "telegram:-100acme-review",
        "profile": {
            "vat_payer": True,
            "tax_regime": "micro",
            "caen": "6201",
            "fiscal_year_start": "01-01",
        },
    }
    with pytest.raises(ValueError, match="Other profile fields"):
        editor.tenant_payload(name="x", extra_json="[1, 2")
    with pytest.raises(ValueError, match="name"):
        editor.tenant_payload(name="  ")


def test_recent_months_and_overview_alerts() -> None:
    from datetime import date

    assert editor.recent_months(date(2026, 2, 10), 3) == ["2026-02", "2026-01", "2025-12"]
    view = {
        "report": {"blockers": [{"bucket_key": "a"}], "closed": None,
                   "documents": {"missing": [{"label": "Extras BT"}], "needs_review": []},
                   "trial_balance": {"balanced": True}},
        "outlook": {"deadlines": [{"form": "D300", "due": "2026-10-25"}],
                    "thresholds": [{"name": "vat_registration", "used_pct": "85.0", "warn": True}],
                    "cash": {"receivables": {"overdue": "700.00"}}},
        "bank": {"movements": [{"key": "k"}]},
    }  # fmt: skip
    alerts = editor.overview_alerts(view)
    assert "1 invoice(s) without an entry" in alerts
    assert "Missing documents: Extras BT" in alerts
    assert "vat_registration at 85.0% of the limit" in alerts
    assert "Overdue receivables: 700.00" in alerts
    assert "1 bank movement(s) not matched" in alerts
    carry = {"report": {"result_to_carry": {"year": 2025, "kind": "profit", "amount": "5000.00"}},
             "outlook": {}, "bank": {}}  # fmt: skip
    assert editor.overview_alerts(carry) == [
        "2025 profit of 5000.00 still on 121: carry it with accounting_result_carry"
    ]
    checks = {"chain": [{"problem": "gap", "statement": "bank/10.sta"}],
              "accounts": [{"iban": "RO1", "bank": "150.00", "ledger": "130.00",
                            "account": "5121", "agrees": False}]}  # fmt: skip
    got = editor.overview_alerts({"report": {"bank": checks}, "outlook": {}, "bank": {}})
    assert got == ["Bank statement gap: bank/10.sta", "Bank RO1 says 150.00, books (5121) 130.00"]
    paged = {"report": {}, "outlook": {}, "bank": {"movements": [{"key": "k"}], "total": 75}}
    assert "75 bank movement(s) not matched" in editor.overview_alerts(paged)
    errored = editor.overview_alerts({"report": {"error": "boom"}, "outlook": {}, "bank": {}})
    assert errored == ["Close report: boom"]


def test_overview_alerts_cover_results_and_partners_errors() -> None:
    view = {"report": {}, "outlook": {}, "bank": {}, "results": {"error": "db"},
            "partners": {"error": "db"}}  # fmt: skip
    assert editor.overview_alerts(view) == ["Results: db", "Partners: db"]


def test_overview_alerts_cover_cash_problems_and_open_advances() -> None:
    report = {"cash": {"problems": [{"day": "2026-09-03", "problem": "Cash negative: -5.00"},
                                    {"day": "2026-09-04", "problem": "Cash above the limit"}],
                       "open_advances": [{"employee": "Ana", "open": "40.00"},
                                         {"employee": "Ion", "open": "10.00"}]}}  # fmt: skip
    alerts = editor.overview_alerts({"report": report, "cash": {"error": "db"}})
    assert "Cash: db" in alerts
    assert "Cash negative on 1 day(s): the month can't close" in alerts
    assert "Cash above the limit on 1 day(s)" in alerts
    assert "Open employee advances: 50.00 (2)" in alerts


def test_overview_alerts_count_balance_anomalies() -> None:
    report = {"anomalies": [{"account": "401", "balance": "20.00", "problem": "p"},
                            {"account": "5311", "balance": "-1.00", "problem": "q"}]}  # fmt: skip
    assert "Balances on the wrong side: 401, 5311" in editor.overview_alerts({"report": report})


def test_overview_alerts_count_open_partner_advances() -> None:
    report = {"partner_advances": [{"cui": "RO1", "partner": "A", "received": "10.00",
                                    "paid": "0.00"}]}  # fmt: skip
    assert "Partner advances not yet applied: 1" in editor.overview_alerts({"report": report})


def test_overview_alerts_count_possible_offsets() -> None:
    report = {"offsets_possible": [{"cui": "RO1", "partner": "A", "amount": "5.00"}]}
    assert "Partners to offset (compensare): 1" in editor.overview_alerts({"report": report})
