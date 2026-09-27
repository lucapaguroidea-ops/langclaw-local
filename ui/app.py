"""Langclaw workflow console (Streamlit).

Set up, inspect, and manage langclaw workflows: each workflow's step graph, a
step-by-step editor, test runs, run history, the review queue, schedules, and
saved versions. Chat and conversation history stay in Telegram.

Talks to the langclaw control-plane API; the API token stays server-side.

Environment:
    LANGCLAW_URL        e.g. http://langclaw.railway.internal:18790
    LANGCLAW_API_TOKEN  the gateway's LANGCLAW__CHANNELS__API__TOKEN
    UI_PASSWORD         password for this console (required)
    UI_REVIEWER         name recorded on reviews answered here (default "web")
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any

import editor
import streamlit as st
from client import LangclawClient, LangclawError

st.set_page_config(page_title="Langclaw workflows", page_icon="🦀", layout="wide")

NEW = "➕ New workflow"
STATUS_ICONS = {
    "running": "⏳",
    "waiting": "⏸",
    "completed": "✅",
    "failed": "💥",
    "rejected": "❌",
}


# -- auth & client --------------------------------------------------------------


def _check_password() -> bool:
    expected = os.environ.get("UI_PASSWORD", "")
    if not expected:
        st.error("UI_PASSWORD is not set on this service; refusing to start without a password.")
        return False
    if st.session_state.get("authed"):
        return True
    with st.form("login"):
        st.title("🦀 Langclaw workflows")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            if hmac.compare_digest(password.encode(), expected.encode()):
                st.session_state.authed = True
                st.rerun()
            st.error("Wrong password.")
    return False


def _client() -> LangclawClient | None:
    url, token = os.environ.get("LANGCLAW_URL", ""), os.environ.get("LANGCLAW_API_TOKEN", "")
    if not url or not token:
        st.error("Set LANGCLAW_URL and LANGCLAW_API_TOKEN on this service.")
        return None
    if "client" not in st.session_state:
        st.session_state.client = LangclawClient(url, token)
    return st.session_state.client


def _reviewer() -> str:
    return os.environ.get("UI_REVIEWER", "web")


def _call(fn, *args: Any, **kwargs: Any) -> Any:
    """Run an API call; show its error and return ``None`` on failure."""
    try:
        return fn(*args, **kwargs)
    except LangclawError as exc:
        st.error(str(exc))
        return None


# -- shared widgets ---------------------------------------------------------------


def _mermaid(code: str, height: int = 480) -> None:
    # The diagram text is HTML-escaped and Mermaid runs in strict mode, so labels
    # (which the agent can write) can't inject markup into this iframe.
    st.iframe(editor.mermaid_html(code, height=height).strip(), height=height)


def _show_turn(turn: dict) -> None:
    """A run's progress lines and final output (as delivered to the API channel)."""
    for m in turn.get("messages", []):
        kind, content = m.get("type"), m.get("content", "")
        if kind == "tool_progress":
            st.caption(content)
        elif kind == "review":
            st.info(f"⏸ Waiting for review: {content}")
        elif content:
            st.markdown(content)
    if turn.get("status") != "done":
        st.warning("Still running — check the Runs tab.")


def _review_card(lc: LangclawClient, review: dict, *, key: str) -> None:
    """One pending review with Approve / Edit / Reject."""
    with st.container(border=True):
        st.markdown(f"**{review['message']}**")
        st.caption(
            f"{review.get('workflow', '')} · run `{review['run_id']}` · "
            f"waiting since {review.get('created_at', '')}"
        )
        for name, value in (review.get("data") or {}).items():
            st.markdown(f"`{name}`")
            st.json(value, expanded=True)
        editable = review.get("editable") or ""
        edited_text = ""
        if editable:
            current = (review.get("data") or {}).get(editable)
            edited_text = st.text_area(
                f"Correct `{editable}` (JSON), then press Edit",
                value=json.dumps(current, indent=2, ensure_ascii=False),
                key=f"{key}:edit",
                height=140,
            )
        cols = st.columns(3)
        action, data = None, None
        if cols[0].button("✅ Approve", key=f"{key}:a", type="primary"):
            action = "approve"
        if editable and cols[1].button("✏️ Edit & approve", key=f"{key}:e"):
            try:
                data = {editable: editor.parse_json(edited_text, editable)}
                action = "edit"
            except ValueError as exc:
                st.error(str(exc))
        if cols[2].button("❌ Reject", key=f"{key}:r"):
            action = "reject"
        if action:
            try:
                lc.answer_review(
                    review["run_id"],
                    action,
                    interrupt_id=review["interrupt_id"],
                    data=data,
                    by=_reviewer(),
                )
                st.success("Answered — the run continues.")
                st.rerun()
            except LangclawError as exc:
                if exc.status == 409:
                    st.warning(f"{exc} The queue has been refreshed.")
                else:
                    st.error(str(exc))


# -- workflow tabs --------------------------------------------------------------------


def tab_graph(workflow: dict) -> None:
    if not workflow.get("valid", True):
        st.error("This workflow file doesn't load. Fix it in the Edit tab:")
        for err in workflow.get("errors", []):
            st.markdown(f"- {err}")
        return
    if workflow.get("mermaid"):
        _mermaid(workflow["mermaid"])
    graph = workflow.get("graph")
    if graph:
        rows = [
            {
                "step": nid,
                "type": editor.TYPE_LABELS.get(node.get("type"), node.get("type")),
                "label": node.get("label", ""),
                "what it does": _summary(node),
            }
            for nid, node in graph.get("nodes", {}).items()
        ]
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.info("Defined in Python code — read-only here. The drawing shows its steps.")


def _summary(node: dict) -> str:
    kind = node.get("type")
    if kind == "llm":
        return (node.get("prompt") or "")[:80]
    if kind == "tool":
        return f"{node.get('tool')}({', '.join(node.get('args', {}))})"
    if kind == "subagent":
        return f"{node.get('subagent')}: {(node.get('prompt') or '')[:60]}"
    if kind == "branch":
        rules = [
            f"{r['if'].get('path')} {r['if'].get('op')} {json.dumps(r['if'].get('value'))}"
            f" → {r['then']}"
            for r in node["rules"]
        ]
        return "; ".join([*rules, f"else → {node.get('else', 'END')}"])
    if kind == "human_review":
        return node.get("message", "")
    return ""


def tab_edit(lc: LangclawClient, name: str, workflow: dict, catalog: dict) -> None:
    if workflow.get("source") == "code":
        st.info("This workflow is defined in Python code, so it can't be edited here.")
        return
    draft_key = f"draft::{name}"
    original = workflow.get("graph") or {}
    if draft_key not in st.session_state:
        st.session_state[draft_key] = json.loads(json.dumps(original))
    draft: dict = st.session_state[draft_key]
    dirty = draft != original

    top = st.columns([1, 1, 1, 3])
    if top[0].button("🔎 Validate", key=f"{name}:validate"):
        _show_check(_call(lc.validate_workflow, name, draft))
    if top[1].button("💾 Save", key=f"{name}:save", type="primary", disabled=not dirty):
        saved = _call(lc.save_workflow, name, draft)
        if saved is not None:
            st.session_state.pop(draft_key, None)
            st.session_state["flash"] = ("success", f"Saved {name}.")
            for warning in saved.get("warnings", []):
                st.session_state.setdefault("flash_warnings", []).append(warning)
            st.rerun()
    if top[2].button("↩️ Discard", key=f"{name}:discard", disabled=not dirty):
        st.session_state.pop(draft_key, None)
        st.rerun()
    top[3].caption("Unsaved changes" if dirty else "No changes")

    st.subheader("Settings")
    description = st.text_area(
        "Description (the agent reads this to decide when to run the workflow)",
        value=draft.get("description", ""),
        key=f"{name}:desc",
    )
    output = st.text_input(
        "Output — which step's result the run returns (e.g. `answer` or `draft.text`)",
        value=draft.get("output", ""),
        key=f"{name}:output",
    )
    st.markdown("**Run input fields**")
    input_rows = st.data_editor(
        editor.input_to_rows(draft),
        num_rows="dynamic",
        key=f"{name}:input",
        column_config={
            "type": st.column_config.SelectboxColumn(options=editor.FIELD_TYPES, required=True),
            "required": st.column_config.CheckboxColumn(default=True),
        },
        width="stretch",
    )
    if st.button("Apply settings", key=f"{name}:apply_settings"):
        draft["description"] = description
        draft["output"] = output
        draft["input"] = editor.rows_to_input(input_rows)
        st.rerun()

    st.subheader("Steps")
    nodes = draft.setdefault("nodes", {})
    add = st.columns([2, 2, 1])
    new_id = add[0].text_input("New step id", key=f"{name}:new_id", placeholder="e.g. classify")
    new_type = add[1].selectbox(
        "Type", editor.NODE_TYPES, format_func=editor.TYPE_LABELS.get, key=f"{name}:new_type"
    )
    if add[2].button("➕ Add step", key=f"{name}:add"):
        try:
            st.session_state[draft_key] = editor.add_node(draft, new_id.strip(), new_type)
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    if nodes:
        chosen = st.selectbox(
            "Edit step",
            list(nodes),
            format_func=lambda n: f"{editor.TYPE_LABELS.get(nodes[n].get('type'), '')}  {n}",
            key=f"{name}:chosen",
        )
        _node_form(name, draft, chosen, catalog)
        if st.button(f"🗑️ Remove step `{chosen}`", key=f"{name}:{chosen}:remove"):
            st.session_state[draft_key] = editor.remove_node(draft, chosen)
            st.rerun()

    st.subheader("Connections")
    st.caption(
        "Each row runs `to` after `from` finishes. `from` can be START, or several steps "
        "separated by commas (wait for all). Branch steps route by their rules instead. "
        "A step with no outgoing connection ends the run."
    )
    edge_rows = st.data_editor(
        editor.edges_to_rows(draft),
        num_rows="dynamic",
        key=f"{name}:edges",
        column_config={
            "to": st.column_config.SelectboxColumn(options=editor.targets(draft), required=True),
        },
        width="stretch",
    )
    if st.button("Apply connections", key=f"{name}:apply_edges"):
        draft["edges"] = editor.rows_to_edges(edge_rows)
        st.rerun()

    with st.expander("Raw file (advanced)"):
        raw = st.text_area(
            "The whole .graph.json",
            value=json.dumps(draft, indent=2, ensure_ascii=False),
            height=400,
            key=f"{name}:raw:{hash(json.dumps(draft, sort_keys=True))}",
        )
        if st.button("Apply raw file", key=f"{name}:apply_raw"):
            try:
                st.session_state[draft_key] = editor.parse_json(raw, "The file")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


def _show_check(check: dict | None) -> None:
    if check is None:
        return
    if check["valid"]:
        st.success("Valid.")
    else:
        st.error("Not valid yet:")
        for err in check["errors"]:
            st.markdown(f"- {err}")
    for warning in check.get("warnings", []):
        st.warning(warning)


def _node_form(name: str, draft: dict, nid: str, catalog: dict) -> None:
    """A form for one step; Apply writes it back into the draft."""
    node = draft["nodes"][nid]
    kind = node.get("type")
    k = f"{name}:{nid}"
    with st.form(f"{k}:form"):
        label = st.text_input("Label (shown in progress messages)", node.get("label", ""))
        save_as = st.text_input("Store result as (default: the step id)", node.get("save_as", ""))
        update: dict[str, Any] = {}
        if kind == "llm":
            update["prompt"] = st.text_area(
                "Prompt — use {{input.field}} or {{step_id.field}}", node.get("prompt", "")
            )
            update["system"] = st.text_area("System instructions", node.get("system", ""))
            update["model"] = st.text_input(
                "Model override (blank = default)", node.get("model", "")
            )
            st.markdown("Structured output fields (leave empty for plain text)")
            out_rows = st.data_editor(
                editor.output_to_rows(node.get("output") or {}),
                num_rows="dynamic",
                key=f"{k}:out",
                column_config={
                    "type": st.column_config.SelectboxColumn(
                        options=editor.FIELD_TYPES, required=True
                    ),
                },
            )
        elif kind == "tool":
            tools = catalog.get("tools", [])
            current = node.get("tool", "")
            options = sorted({*tools, current} - {""}) or [""]
            update["tool"] = st.selectbox(
                "Tool", options, index=options.index(current) if current in options else 0
            )
            args_text = st.text_area(
                "Arguments (JSON) — values can use {{...}} templates",
                json.dumps(node.get("args", {}), indent=2),
            )
        elif kind == "subagent":
            subs = catalog.get("subagents", [])
            current = node.get("subagent", "")
            options = sorted({*subs, current} - {""}) or [""]
            update["subagent"] = st.selectbox(
                "Subagent", options, index=options.index(current) if current in options else 0
            )
            update["prompt"] = st.text_area("Prompt", node.get("prompt", ""))
        elif kind == "branch":
            st.caption("Rules are checked in order; the first match wins.")
            rule_rows = st.data_editor(
                [
                    {
                        "path": r["if"].get("path", ""),
                        "op": r["if"].get("op", "truthy"),
                        "value": json.dumps(r["if"].get("value")),
                        "then": r.get("then", ""),
                    }
                    for r in node.get("rules", [])
                ],
                num_rows="dynamic",
                key=f"{k}:rules",
                column_config={
                    "op": st.column_config.SelectboxColumn(options=editor.BRANCH_OPS),
                    "then": st.column_config.SelectboxColumn(options=editor.targets(draft)),
                },
            )
            targets = editor.targets(draft)
            current = node.get("else", "END")
            update["else"] = st.selectbox(
                "Otherwise go to",
                targets,
                index=targets.index(current) if current in targets else 0,
            )
        elif kind == "human_review":
            update["message"] = st.text_area("Question for the reviewer", node.get("message", ""))
            keys = [n for n in draft["nodes"] if draft["nodes"][n].get("type") != "branch"]
            update["show"] = st.multiselect(
                "Show these results (empty = everything so far)",
                keys,
                default=[s for s in node.get("show", []) if s in keys],
            )
            editable_opts = ["", *keys]
            current = node.get("editable", "")
            update["editable"] = st.selectbox(
                "Result the reviewer may correct",
                editable_opts,
                index=editable_opts.index(current) if current in editable_opts else 0,
            )
            targets = editor.targets(draft)
            current = node.get("on_reject", "END")
            update["on_reject"] = st.selectbox(
                "If rejected, go to",
                targets,
                index=targets.index(current) if current in targets else 0,
            )
        applied = st.form_submit_button("Apply step changes")
    if not applied:
        return
    try:
        if kind == "llm":
            update["output"] = editor.rows_to_input(out_rows)
        if kind == "tool":
            update["args"] = editor.parse_json(args_text, "Arguments")
        if kind == "branch":
            update["rules"] = [
                {
                    "if": {
                        "path": row["path"],
                        "op": row.get("op") or "truthy",
                        "value": editor.parse_json(row.get("value") or "null", "Rule value"),
                    },
                    "then": row["then"],
                }
                for row in rule_rows
                if row.get("path") and row.get("then")
            ]
    except ValueError as exc:
        st.error(str(exc))
        return
    fresh = {"type": kind, **({"label": label} if label else {})}
    if save_as:
        fresh["save_as"] = save_as
    for key, value in update.items():
        if value not in ("", [], {}, None):
            fresh[key] = value
    draft["nodes"][nid] = fresh
    st.rerun()


def tab_test_run(lc: LangclawClient, name: str, workflow: dict) -> None:
    if not workflow.get("valid", True):
        st.info("Fix the workflow before running it.")
        return
    fields = (workflow.get("graph") or {}).get("input") or {}
    st.caption(
        "Runs started here report to this console (Runs tab). If a step pauses for "
        "review, answer it in the Reviews tab — or from Telegram when a review chat "
        "is configured."
    )
    with st.form(f"{name}:run"):
        if fields:
            values = {
                field: st.text_input(
                    f"{field} ({spec.get('type', 'string')})",
                    help=spec.get("description") or None,
                )
                for field, spec in fields.items()
            }
        else:
            raw = st.text_area("Input (JSON)", value="{}", height=120)
        go = st.form_submit_button("▶️ Run", type="primary")
    if not go:
        return
    try:
        run_input = (
            editor.coerce_input(fields, values) if fields else editor.parse_json(raw, "Input")
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    started = _call(lc.start_run, name, run_input)
    if started is None:
        return
    st.caption(f"Run `{started['run_id']}`")
    with st.spinner("Running…"):
        turn = _call(lc.follow_turn, started["turn_id"])
    if turn:
        _show_turn(turn)
    run = _call(lc.run, started["run_id"])
    if run and run.get("status") == "waiting":
        st.caption("Answer it in the Reviews tab (or from Telegram).")


def tab_runs(lc: LangclawClient, name: str) -> None:
    runs = _call(lc.runs, name) or []
    if not runs:
        st.info("No runs yet.")
        return
    st.dataframe(
        [
            {
                "": STATUS_ICONS.get(r["status"], "•"),
                "run": r["run_id"],
                "status": r["status"],
                "started by": r.get("trigger", ""),
                "started": r.get("started_at", ""),
                "updated": r.get("updated_at", ""),
                "reviews waiting": r.get("pending_reviews", 0),
            }
            for r in runs
        ],
        hide_index=True,
        width="stretch",
    )
    chosen = st.selectbox("Open run", [r["run_id"] for r in runs], key=f"{name}:run_pick")
    run = _call(lc.run, chosen)
    if not run:
        return
    cols = st.columns(3)
    cols[0].metric("Status", f"{STATUS_ICONS.get(run['status'], '')} {run['status']}")
    cols[1].metric("Started by", run.get("trigger") or "—")
    cols[2].metric("Steps", len(run.get("steps", [])))
    if run.get("error"):
        st.error(run["error"])
    with st.expander("Input"):
        st.json(run.get("input"))
    if run.get("output") is not None:
        st.markdown("**Output**")
        output = run["output"]
        if isinstance(output, str):
            st.markdown(output)
        else:
            st.json(output)
    if run.get("reviews"):
        st.markdown("**Reviews**")
        for review in run["reviews"]:
            decision = review.get("decision")
            status = (
                f"{decision['action']} by {decision.get('by') or '?'} "
                f"via {decision.get('via') or '?'} at {decision.get('at', '')}"
                if decision
                else "⏸ waiting"
            )
            st.markdown(f"- {review.get('message', '')} — _{status}_")
    st.markdown("**Steps**")
    for step in run.get("steps", []):
        icon = "💥" if step.get("error") else ("⏸" if step.get("paused") else "✔️")
        with st.expander(f"{icon} {step['node']}  ·  {step.get('at', '')}"):
            if step.get("error"):
                st.error(step["error"])
            st.json(step.get("result"))


def tab_reviews(lc: LangclawClient, name: str) -> None:
    reviews = _call(lc.reviews, name) or []
    if not reviews:
        st.info("Nothing waiting for review.")
    for review in reviews:
        _review_card(lc, review, key=f"{name}:{review['run_id']}:{review['interrupt_id']}")


def tab_schedules(lc: LangclawClient, name: str) -> None:
    status = _call(lc.status) or {}
    if not status.get("features", {}).get("schedules"):
        st.info("Schedules are off. Set LANGCLAW__CRON__ENABLED=true on the langclaw service.")
        return
    jobs = [j for j in (_call(lc.schedules) or []) if j.get("workflow_name") == name]
    if jobs:
        st.dataframe(
            [
                {
                    "name": j["name"],
                    "when": j["schedule"],
                    "reports to": j["channel"],
                    "id": j["id"],
                }
                for j in jobs
            ],
            hide_index=True,
            width="stretch",
        )
        target = st.selectbox("Remove schedule", [j["id"] for j in jobs], key=f"{name}:unsched")
        if st.button("🗑️ Remove", key=f"{name}:unsched_btn"):
            if _call(lc.delete_schedule, target):
                st.rerun()
    else:
        st.caption("Not scheduled.")
    with st.form(f"{name}:schedule"):
        st.markdown("**Add a schedule**")
        label = st.text_input("Name", value=f"{name} schedule")
        cron = st.text_input("Cron (min hour day month weekday)", value="0 9 * * *")
        channel = st.selectbox("Deliver results to", status.get("channels", []))
        user_id = st.text_input("Chat / user id on that channel", help="e.g. your Telegram id")
        raw = st.text_area("Input (JSON)", value="{}")
        if st.form_submit_button("Add"):
            try:
                wf_input = json.dumps(editor.parse_json(raw, "Input"))
            except ValueError as exc:
                st.error(str(exc))
                return
            if _call(
                lc.add_schedule,
                name=label,
                channel=channel,
                user_id=user_id,
                workflow_name=name,
                workflow_input=wf_input,
                cron_expr=cron,
            ):
                st.rerun()


def tab_versions(lc: LangclawClient, name: str, workflow: dict) -> None:
    if workflow.get("source") == "code":
        st.info("Workflows defined in code are versioned in git, not here.")
        return
    versions = _call(lc.versions, name) or []
    if not versions:
        st.info("No earlier versions yet — one is kept every time the workflow is saved.")
        return
    chosen = st.selectbox(
        "Earlier version",
        [v["version"] for v in versions],
        format_func=lambda v: next((x["saved_at"] for x in versions if x["version"] == v), v),
        key=f"{name}:ver",
    )
    old = _call(lc.version, name, chosen)
    if old:
        diff = editor.graph_diff(old["graph"], workflow.get("graph") or {})
        if diff:
            st.caption("Changes since this version (− this version, + current):")
            st.code(diff, language="diff")
        else:
            st.caption("Identical to the current file.")
    if st.button("⏪ Restore this version", key=f"{name}:restore"):
        if _call(lc.restore, name, chosen) is not None:
            st.session_state.pop(f"draft::{name}", None)
            st.session_state["flash"] = ("success", "Restored — the current file was kept too.")
            st.rerun()


# -- pages -------------------------------------------------------------------------------


def page_workflow(lc: LangclawClient, name: str) -> None:
    workflow = _call(lc.workflow, name)
    if workflow is None:
        return
    catalog = _call(lc.catalog) or {"tools": [], "subagents": []}
    badge = "📄 file" if workflow.get("source") == "file" else "🐍 code"
    if not workflow.get("valid", True):
        badge += " · ⚠️ doesn't load"
    st.header(f"🧩 {name}")
    st.caption(f"{badge} · tool `workflow_{name}`")
    if workflow.get("description"):
        st.markdown(workflow["description"])
    reviews = len(_call(lc.reviews, name) or [])
    tabs = st.tabs(
        [
            "🗺️ Graph",
            "✏️ Edit",
            "▶️ Test run",
            "📜 Runs",
            f"🙋 Reviews ({reviews})" if reviews else "🙋 Reviews",
            "⏰ Schedules",
            "🕘 Versions",
        ]
    )
    with tabs[0]:
        tab_graph(workflow)
    with tabs[1]:
        tab_edit(lc, name, workflow, catalog)
    with tabs[2]:
        tab_test_run(lc, name, workflow)
    with tabs[3]:
        tab_runs(lc, name)
    with tabs[4]:
        tab_reviews(lc, name)
    with tabs[5]:
        tab_schedules(lc, name)
    with tabs[6]:
        tab_versions(lc, name, workflow)
    if workflow.get("source") == "file":
        with st.expander("Danger zone"):
            st.caption("Deleting keeps the file's history, so it can be restored later.")
            if st.button(f"🗑️ Delete workflow {name}", key=f"{name}:delete"):
                if _call(lc.delete_workflow, name):
                    st.query_params.clear()
                    st.rerun()


def page_new(lc: LangclawClient) -> None:
    st.header("➕ New workflow")
    with st.form("new"):
        name = st.text_input("Name (snake_case)", placeholder="e.g. document_intake")
        template = st.selectbox("Start from", list(editor.TEMPLATES))
        create = st.form_submit_button("Create", type="primary")
    if create:
        if not editor.valid_id(name):
            st.error("Use snake_case: a letter, then letters, digits or underscores.")
            return
        if _call(lc.save_workflow, name, editor.new_draft(template)) is not None:
            st.query_params["wf"] = name
            st.rerun()


def page_reviews(lc: LangclawClient) -> None:
    st.header("🙋 Review queue")
    st.caption("Every paused run, across workflows. Answers here also update Telegram.")
    reviews = _call(lc.reviews) or []
    if not reviews:
        st.success("Nothing waiting.")
    for review in reviews:
        _review_card(lc, review, key=f"all:{review['run_id']}:{review['interrupt_id']}")


STATUSES = ["", "filed", "processing", "needs_review", "rejected"]


def page_documents(lc: LangclawClient) -> None:
    st.header("📄 Documents")
    st.caption("What the intake workflow filed. Read-only — edits happen through workflows.")
    with st.form("doc_search"):
        q = st.text_input("Search", placeholder="e.g. electricity bills from last spring")
        cols = st.columns(4)
        sender = cols[0].text_input("Sender")
        doc_type = cols[1].text_input("Type", placeholder="invoice")
        status = cols[2].selectbox("Status", STATUSES, format_func=lambda s: s or "any")
        by_meaning = cols[3].toggle("By meaning", value=True, help="Semantic search")
        dates = st.columns(2)
        date_from = dates[0].date_input("From", value=None)
        date_to = dates[1].date_input("To", value=None)
        st.form_submit_button("Search")
    result = _call(
        lc.documents,
        q,
        semantic=by_meaning,
        sender=sender,
        doc_type=doc_type,
        status=status,
        date_from=date_from.isoformat() if date_from else "",
        date_to=date_to.isoformat() if date_to else "",
    )
    if not result:
        return
    if q and by_meaning and not result["semantic"]:
        st.info(
            "Search by meaning is off (set LANGCLAW__DOCUMENTS__EMBEDDING_MODEL) — "
            "showing text matches."
        )
    docs = result["documents"]
    st.write(
        f"**{result['count']}** document(s)"
        + (" · ranked by meaning" if result["mode"] == "semantic" else "")
    )
    if not docs:
        return
    rows = [
        {
            "date": d.get("document_date") or "",
            "type": d.get("doc_type", ""),
            "sender": d.get("sender", ""),
            "receiver": d.get("receiver", ""),
            "amount": f"{d['amount']:,.2f} {d.get('currency', '')}".strip()
            if d.get("amount") is not None
            else "",
            "status": d.get("status", ""),
            **({"match": d["similarity"]} if "similarity" in d else {}),
            "summary": d.get("summary", ""),
            "file": d["bucket_key"],
        }
        for d in docs
    ]
    picked = st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        column_config={"match": st.column_config.ProgressColumn("match", min_value=0, max_value=1)},
    )
    selected = picked.selection.rows if picked else []
    if not selected:
        st.caption("Select a row to see the full record.")
        return
    _document_detail(lc, docs[selected[0]]["bucket_key"])


def _document_detail(lc: LangclawClient, key: str) -> None:
    detail = _call(lc.document, key)
    if not detail:
        return
    doc = detail["document"]
    st.subheader(doc.get("filename") or key)
    if detail.get("link"):
        st.link_button("⬇️ Open file", detail["link"])
    left, right = st.columns(2)
    for label, field in [
        ("Type", "doc_type"),
        ("Sender", "sender"),
        ("Receiver", "receiver"),
        ("Date", "document_date"),
        ("Status", "status"),
    ]:
        left.write(f"**{label}:** {doc.get(field) or '—'}")
    if doc.get("amount") is not None:
        right.write(f"**Amount:** {doc['amount']:,.2f} {doc.get('currency', '')}")
    right.write(f"**Filed:** {doc.get('created_at', '')[:16]}")
    right.write(f"**Updated:** {doc.get('updated_at', '')[:16]}")
    right.write(f"**Bucket key:** `{key}`")
    if doc.get("summary"):
        st.write(doc["summary"])
    if doc.get("fields"):
        with st.expander("Other extracted fields"):
            st.json(doc["fields"])


def page_status(lc: LangclawClient) -> None:
    st.header("📊 Status")
    status = _call(lc.status)
    if not status:
        return
    cols = st.columns(3)
    cols[0].metric("Version", status["version"])
    cols[1].metric("Channels", ", ".join(status["channels"]))
    cols[2].metric("Agents", len(status["agents"]))
    st.write(f"**Model:** `{status['model']}`")
    for feature, on in status["features"].items():
        st.write(f"{'✅' if on else '⬜'} {feature}")
    catalog = _call(lc.catalog) or {}
    st.subheader("Available to workflow steps")
    st.write("**Tools:** " + (", ".join(f"`{t}`" for t in catalog.get("tools", [])) or "—"))
    st.write("**Subagents:** " + (", ".join(f"`{s}`" for s in catalog.get("subagents", [])) or "—"))
    st.subheader("MCP servers")
    servers = status.get("mcp_servers", [])
    if not servers:
        st.caption("None configured.")
    for server in servers:
        if server["error"]:
            st.error(f"**{server['name']}** ({server['transport']}): {server['error']}")
        else:
            st.success(
                f"**{server['name']}** ({server['transport']}): {len(server['tools'])} tools"
            )


def _flash() -> None:
    flash = st.session_state.pop("flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    for warning in st.session_state.pop("flash_warnings", []):
        st.warning(warning)


def main(lc: LangclawClient) -> None:
    workflows = _call(lc.workflows)
    if workflows is None:
        return
    reviews = _call(lc.reviews) or []
    st.sidebar.title("🦀 Langclaw")
    names = [w["name"] for w in workflows]
    marks = {
        w["name"]: ("⚠️ " if not w.get("valid", True) else "")
        + ("🐍 " if w.get("source") == "code" else "")
        for w in workflows
    }
    options = [*names, NEW]
    wanted = st.query_params.get("wf")
    index = options.index(wanted) if wanted in options else (0 if names else len(options) - 1)
    choice = st.sidebar.selectbox(
        "Workflow", options, index=index, format_func=lambda n: f"{marks.get(n, '')}{n}"
    )
    if choice != NEW:
        st.query_params["wf"] = choice
    page = st.sidebar.radio(
        "View",
        ["Workflow", f"Review queue ({len(reviews)})", "Documents", "Status"],
        label_visibility="collapsed",
    )
    st.sidebar.caption("Chat with langclaw in Telegram.")
    _flash()
    if page.startswith("Review queue"):
        page_reviews(lc)
    elif page == "Documents":
        page_documents(lc)
    elif page == "Status":
        page_status(lc)
    elif choice == NEW:
        page_new(lc)
    else:
        page_workflow(lc, choice)


if _check_password() and (lc := _client()) is not None:
    main(lc)
