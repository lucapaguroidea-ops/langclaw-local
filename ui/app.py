"""Langclaw web console (Streamlit).

Talks to the langclaw control-plane API; the API token stays server-side.

Environment:
    LANGCLAW_URL        e.g. http://langclaw.railway.internal:18790
    LANGCLAW_API_TOKEN  the gateway's LANGCLAW__CHANNELS__API__TOKEN
    UI_PASSWORD         password for this console (required)
"""

from __future__ import annotations

import hmac
import json
import os
import uuid

import streamlit as st
from client import LangclawClient, LangclawError

st.set_page_config(page_title="Langclaw", page_icon="🦀", layout="wide")


# -- auth ----------------------------------------------------------------------


def _check_password() -> bool:
    expected = os.environ.get("UI_PASSWORD", "")
    if not expected:
        st.error("UI_PASSWORD is not set on this service; refusing to start without a password.")
        return False
    if st.session_state.get("authed"):
        return True
    with st.form("login"):
        st.title("🦀 Langclaw")
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


def _show_turn(turn: dict) -> None:
    """Render one turn's outputs: agent text, tool steps, command replies."""
    for m in turn.get("messages", []):
        kind, content = m.get("type"), m.get("content", "")
        if kind in ("ai", "command"):
            st.markdown(content)
        elif kind == "tool_progress":
            st.caption(f"⚙️ {content}")
        elif kind == "tool_result":
            with st.expander("tool result"):
                st.code(content)
    if turn.get("status") != "done":
        st.warning("Still running — refresh to see the rest.")


# -- pages ---------------------------------------------------------------------

_NEW_GRAPH = {
    "description": "What this workflow does (the agent reads this).",
    "nodes": {"answer": {"type": "llm", "prompt": "Summarise: {{input.text}}"}},
    "edges": [{"from": "START", "to": "answer"}],
    "output": "answer",
}


def page_chat(lc: LangclawClient) -> None:
    st.header("Chat")
    context = st.sidebar.text_input(
        "Conversation", value="web", help="Memory is kept per conversation."
    )
    if st.sidebar.button("New conversation"):
        context = f"web-{uuid.uuid4().hex[:6]}"
    try:
        history = lc.history(context)
    except LangclawError as exc:
        st.error(str(exc))
        return
    for message in history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    if prompt := st.chat_input("Message langclaw (or /help)"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"), st.spinner("Thinking…"):
            try:
                _show_turn(lc.chat_and_wait(prompt, context_id=context))
            except LangclawError as exc:
                st.error(str(exc))


def page_workflows(lc: LangclawClient) -> None:
    st.header("Workflows")
    try:
        workflows = lc.workflows()
    except LangclawError as exc:
        st.error(str(exc))
        return
    st.dataframe(workflows, width="stretch", hide_index=True)

    names = ["➕ New workflow"] + [w["name"] for w in workflows]
    choice = st.selectbox("Open", names)
    current = {} if choice == names[0] else lc.workflow(choice)
    editable = choice == names[0] or current.get("editable", False)
    if current.get("mermaid"):
        with st.expander("Graph (Mermaid)"):
            st.code(current["mermaid"], language="mermaid")

    with st.form("wf"):
        name = st.text_input(
            "Name (snake_case)", value=current.get("name", ""), disabled=choice != names[0]
        )
        graph_text = st.text_area(
            "Workflow file (.graph.json — see docs/guides/workflows.md)",
            value=json.dumps(current.get("graph", _NEW_GRAPH), indent=2),
            height=360,
            disabled=not editable,
            help=None if editable else "Workflows defined in Python code are read-only here.",
        )
        saved = st.form_submit_button("💾 Save", disabled=not editable)
    if saved:
        try:
            lc.save_workflow(name, json.loads(graph_text))
            st.success(f"Saved {name}.")
            st.rerun()
        except json.JSONDecodeError as exc:
            st.error(f"Not valid JSON: {exc}")
        except LangclawError as exc:
            st.error(str(exc))

    if choice != names[0]:
        raw = st.text_area("Run input (JSON)", value="{}", height=80)
        col_run, col_del = st.columns(2)
        if col_run.button("▶️ Run"):
            try:
                with st.spinner("Running…"):
                    _show_turn(lc.run_workflow_and_wait(choice, json.loads(raw or "{}")))
            except json.JSONDecodeError as exc:
                st.error(f"Input is not valid JSON: {exc}")
            except LangclawError as exc:
                st.error(str(exc))
        if editable and col_del.button("🗑️ Delete"):
            try:
                lc.delete_workflow(choice)
                st.rerun()
            except LangclawError as exc:
                st.error(str(exc))


def page_schedules(lc: LangclawClient) -> None:
    st.header("Schedules")
    try:
        jobs, status, workflows = lc.schedules(), lc.status(), lc.workflows()
    except LangclawError as exc:
        st.error(str(exc))
        return
    st.dataframe(jobs, width="stretch", hide_index=True)

    with st.form("add"):
        st.subheader("Add schedule")
        name = st.text_input("Name")
        channel = st.selectbox("Deliver to channel", status["channels"])
        user_id = st.text_input(
            "User / chat id on that channel", help="e.g. your Telegram numeric id"
        )
        kind = st.radio("Run", ["Prompt", "Workflow"], horizontal=True)
        message = st.text_area("Prompt", height=80)
        workflow = st.selectbox("Workflow", [w["name"] for w in workflows] or ["—"])
        cron = st.text_input("Cron (min hour day month weekday)", value="0 9 * * *")
        if st.form_submit_button("Add"):
            try:
                lc.add_schedule(
                    name=name,
                    channel=channel,
                    user_id=user_id,
                    message=message if kind == "Prompt" else "",
                    workflow_name=workflow if kind == "Workflow" else "",
                    cron_expr=cron,
                )
                st.rerun()
            except LangclawError as exc:
                st.error(str(exc))

    if jobs:
        target = st.selectbox("Delete schedule", [f"{j['name']} ({j['id']})" for j in jobs])
        if st.button("🗑️ Delete"):
            try:
                lc.delete_schedule(target.rsplit("(", 1)[1].rstrip(")"))
                st.rerun()
            except LangclawError as exc:
                st.error(str(exc))


def page_status(lc: LangclawClient) -> None:
    st.header("Status")
    try:
        status = lc.status()
    except LangclawError as exc:
        st.error(str(exc))
        return
    cols = st.columns(3)
    cols[0].metric("Version", status["version"])
    cols[1].metric("Channels", ", ".join(status["channels"]))
    cols[2].metric("Agents", len(status["agents"]))
    st.write(f"**Model:** `{status['model']}`")
    st.write("**Features:**")
    for feature, on in status["features"].items():
        st.write(f"{'✅' if on else '⬜'} {feature}")

    st.subheader("MCP servers")
    servers = status.get("mcp_servers", [])
    if not servers:
        st.caption("None configured. Add them with LANGCLAW__MCP__SERVERS (see the MCP guide).")
    for server in servers:
        if server["error"]:
            st.error(f"**{server['name']}** ({server['transport']}): {server['error']}")
        else:
            st.success(
                f"**{server['name']}** ({server['transport']}): {len(server['tools'])} tools"
            )
            st.caption(", ".join(server["tools"]))


# -- main ----------------------------------------------------------------------

if _check_password() and (lc := _client()) is not None:
    pages = {
        "💬 Chat": page_chat,
        "🧩 Workflows": page_workflows,
        "⏰ Schedules": page_schedules,
        "📊 Status": page_status,
    }
    page = st.sidebar.radio("Langclaw", list(pages))
    pages[page](lc)
