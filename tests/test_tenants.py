"""Tenants: the registry (who is a client, which chats are theirs) and the
current-tenant context that scopes document access."""

from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from langclaw.tenants import (
    Tenant,
    TenantRegistry,
    chat_ref,
    current_tenant,
    tenant_scope,
)


def test_tenant_ids_are_safe_slugs() -> None:
    assert Tenant(id="acme", name="ACME SRL").id == "acme"
    assert Tenant(id="client_7", name="x").id == "client_7"
    for bad in ["", "Acme", "a b", "../x", "x" * 41, "1abc", "a-b"]:
        with pytest.raises(ValueError, match="client id"):
            Tenant(id=bad, name="x")


def test_chats_are_channel_colon_chat_and_normalized() -> None:
    t = Tenant(id="acme", name="ACME", chats=[" telegram:-100123 ", "telegram:42"])
    assert t.chats == ["telegram:-100123", "telegram:42"]
    with pytest.raises(ValueError, match="channel:chat_id"):
        Tenant(id="acme", name="ACME", chats=["-100123"])
    with pytest.raises(ValueError, match="channel:chat_id"):
        Tenant(id="acme", name="ACME", review_chat="telegram")
    assert chat_ref("telegram", "-100123") == "telegram:-100123"


async def test_registry_saves_finds_by_chat_and_deletes() -> None:
    registry = TenantRegistry(InMemoryStore())
    await registry.save(
        Tenant(id="acme", name="ACME SRL", tax_id="RO12345678", chats=["telegram:-100"])
    )
    await registry.save(Tenant(id="beta", name="Beta SRL", chats=["telegram:-200"]))

    assert [t.id for t in await registry.list()] == ["acme", "beta"]
    assert (await registry.get("acme")).tax_id == "RO12345678"
    assert (await registry.for_chat("telegram", "-100")).id == "acme"
    assert await registry.for_chat("telegram", "-999") is None

    assert await registry.delete("beta") is True
    assert await registry.for_chat("telegram", "-200") is None
    assert await registry.delete("beta") is False


async def test_a_chat_belongs_to_at_most_one_client() -> None:
    registry = TenantRegistry(InMemoryStore())
    await registry.save(Tenant(id="acme", name="ACME", chats=["telegram:-100"]))
    with pytest.raises(ValueError, match="already linked to client 'acme'"):
        await registry.save(Tenant(id="beta", name="Beta", chats=["telegram:-100"]))
    # re-saving the owner with the same chat is fine
    await registry.save(Tenant(id="acme", name="ACME renamed", chats=["telegram:-100"]))
    assert (await registry.for_chat("telegram", "-100")).name == "ACME renamed"


async def test_registry_is_durable_across_instances() -> None:
    store = InMemoryStore()
    await TenantRegistry(store).save(Tenant(id="acme", name="ACME", chats=["telegram:1"]))
    fresh = TenantRegistry(store)
    assert (await fresh.for_chat("telegram", "1")).id == "acme"
    saved = await fresh.get("acme")
    assert saved.created_at and saved.updated_at


def test_current_tenant_is_scoped() -> None:
    acme = Tenant(id="acme", name="ACME")
    beta = Tenant(id="beta", name="Beta")
    assert current_tenant() is None
    with tenant_scope(acme):
        assert current_tenant() is acme
        with tenant_scope(beta):
            assert current_tenant() is beta
        assert current_tenant() is acme
    assert current_tenant() is None


# -- the gateway resolves the client; runs remember it --------------------------------------

import base64  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from langclaw.bus.base import Attachment, AttachmentType, InboundMessage  # noqa: E402
from langclaw.config.schema import LangclawConfig  # noqa: E402
from langclaw.workflows.executor import StepRequest  # noqa: E402
from tests.test_graph_workflows import (  # noqa: E402
    DOC_FLOW,
    FakeExecutor,
    graph_spec_of,
    runner_with,
)


async def _registry() -> TenantRegistry:
    registry = TenantRegistry(InMemoryStore())
    await registry.save(
        Tenant(
            id="acme",
            name="ACME SRL",
            chats=["telegram:-100acme"],
            review_chat="telegram:-100acme-review",
        )
    )
    await registry.save(Tenant(id="beta", name="Beta SRL", chats=["telegram:-100beta"]))
    return registry


def _manager(registry: TenantRegistry, **config_changes: Any):
    from langclaw.gateway.manager import GatewayManager
    from tests.test_graph_workflow_gateway import _Bus, _FakeChannel

    config = LangclawConfig()
    config.tenants.enabled = True
    for dotted, value in config_changes.items():
        section, key = dotted.split("__")
        setattr(getattr(config, section), key, value)
    cp = MagicMock()
    cp.get.return_value = MagicMock()
    bus = _Bus()
    mgr = GatewayManager(
        config=config,
        bus=bus,
        checkpointer_backend=cp,
        agent=MagicMock(),
        channels=[_FakeChannel()],
        tenant_registry=registry,
    )
    return mgr, bus


def _msg(chat: str, *, origin: str = "user", **meta: Any) -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        user_id="7",
        context_id=chat,
        chat_id=chat,
        content="hi",
        origin=origin,
        metadata=meta,
    )


async def test_the_gateway_takes_the_client_from_the_chat_never_from_metadata() -> None:
    mgr, _ = _manager(await _registry())
    seen: list[str | None] = []

    async def record(msg: InboundMessage) -> None:
        tenant = current_tenant()
        seen.append(tenant.id if tenant else None)

    mgr._handle_message = record
    await mgr._handle(_msg("-100acme"))
    await mgr._handle(_msg("-100beta"))
    await mgr._handle(_msg("-100nobody"))
    await mgr._handle(_msg("-100nobody", tenant="acme"))  # a user can't pick a client
    await mgr._handle(_msg("-100beta", tenant="acme"))
    # langclaw's own workflow messages (intake, scans, UI runs) carry it explicitly
    await mgr._handle(_msg("-100nobody", origin="workflow", tenant="acme"))
    await mgr._handle(_msg("-100nobody", origin="workflow", tenant="gone"))
    assert seen == ["acme", "beta", None, None, "beta", "acme", None]
    assert current_tenant() is None


async def test_runs_remember_their_client_across_a_review() -> None:
    registry = await _registry()
    seen: list[str | None] = []

    class Recording(FakeExecutor):
        async def __call__(self, request: StepRequest) -> Any:
            if request.kind == "tool":
                tenant = current_tenant()
                seen.append(tenant.id if tenant else None)
            return await super().__call__(request)

    runner = runner_with(Recording(confidence=0.1))
    runner.tenants = registry
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    with tenant_scope(await registry.get("acme")):
        paused = await runner.start(spec, {"key": "a.pdf"}, run_id="doc_flow:t1", tenant="acme")
    assert paused.status == "waiting"
    assert (await runner.index.get("doc_flow:t1"))["tenant"] == "acme"

    # answered later, from anywhere (no client in scope): the run still works for acme
    done = await runner.resume(spec, "doc_flow:t1", {"action": "approve", "by": "luca"})
    assert done.status == "completed"
    assert seen == ["acme", "acme"]  # bucket_read before the review, documents_insert after


async def test_a_run_whose_client_was_deleted_fails() -> None:
    from langclaw.workflows.executor import WorkflowStepError

    registry = await _registry()
    runner = runner_with(FakeExecutor(confidence=0.1))
    runner.tenants = registry
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    await runner.start(spec, {"key": "a.pdf"}, run_id="doc_flow:t2", tenant="beta")
    await registry.delete("beta")
    with pytest.raises(WorkflowStepError, match="client 'beta' no longer exists"):
        await runner.resume(spec, "doc_flow:t2", {"action": "approve", "by": "luca"})


# -- intake ----------------------------------------------------------------------------


def _pdf(name: str) -> Attachment:
    return Attachment(
        type=AttachmentType.FILE,
        mime_type="application/pdf",
        filename=name,
        data=base64.b64encode(b"%PDF-1.4 demo").decode(),
    )


async def test_intake_files_under_the_client_and_refuses_unlinked_chats(monkeypatch) -> None:
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from langclaw.config.schema import BucketConfig, DocumentsConfig
    from langclaw.documents import Bucket, DocumentServices
    from langclaw.documents import tools as doc_tools
    from tests.test_documents import FakeStore

    class ScopedFakeStore(FakeStore):
        def for_schema(self, schema: str) -> FakeStore:
            return self.__dict__.setdefault(schema, FakeStore())

    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="docs")
        store = ScopedFakeStore()
        services = DocumentServices(
            DocumentsConfig(intake_workflow="document_intake"),
            bucket=Bucket(BucketConfig(name="docs"), client=s3),
            store=store,
            require_tenant=True,
        )
        monkeypatch.setattr(doc_tools, "shared_services", lambda *_a, **_k: services)
        mgr, bus = _manager(
            await _registry(),
            documents__enabled=True,
            documents__intake_workflow="document_intake",
        )

        refused = _msg("-100nobody")
        refused.attachments = [_pdf("x.pdf")]
        await mgr._handle(refused)
        assert bus.published == []
        assert "isn't linked to a client" in mgr._channel_map["telegram"].sent[-1].content

        linked = _msg("-100acme")
        linked.attachments = [_pdf("invoice.pdf")]
        await mgr._handle(linked)
        (run,) = bus.published
        assert run.metadata["tenant"] == "acme" and run.origin == "workflow"
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket="docs")["Contents"]]
        assert len(keys) == 1 and keys[0].startswith("tenants/acme/inbox/")
        assert list(store.__dict__["tenant_acme"].rows) == [keys[0].removeprefix("tenants/acme/")]


# -- review requests -------------------------------------------------------------------


async def test_review_requests_reach_the_clients_review_chat_and_name_the_client() -> None:
    from tests.test_review_requests import _origin, _setup

    plane, runtime, channels = _setup()
    registry = await _registry()
    plane._tenants = registry
    runtime.graph_runner.tenants = registry
    spec = graph_spec_of("doc_flow", DOC_FLOW)
    await runtime.run_graph(
        spec,
        {"key": "a"},
        run_id="doc_flow:r1",
        reply_to=_origin("telegram", "-100acme"),
        tenant="acme",
    )
    chats = [target["chat_id"] for target, _ in channels["telegram"].requests]
    assert chats == ["-100acme", "-100acme-review"]
    assert all(req["client"] == "ACME SRL" for _, req in channels["telegram"].requests)
    from langclaw.gateway.reviews import review_request_text

    assert "ACME SRL" in review_request_text(channels["telegram"].requests[0][1])


async def test_the_current_client_reaches_tools_run_by_langgraph() -> None:
    """The agent's tools execute inside LangGraph's own tasks — the client set by
    the gateway around the turn must still be visible there."""
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    seen: list[str | None] = []

    @tool
    async def whoami() -> str:
        """Report the current client."""
        tenant = current_tenant()
        seen.append(tenant.id if tenant else None)
        return "ok"

    def call(state: MessagesState) -> dict:
        return {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "whoami", "args": {}, "id": "1"}])
            ]
        }

    builder = StateGraph(MessagesState)
    builder.add_node("call", call)
    builder.add_node("tools", ToolNode([whoami]))
    builder.add_edge(START, "call")
    builder.add_edge("call", "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()

    with tenant_scope(Tenant(id="acme", name="ACME")):
        async for _ in graph.astream({"messages": []}, stream_mode="updates"):
            pass
    async for _ in graph.astream({"messages": []}, stream_mode="updates"):
        pass
    assert seen == ["acme", None]


# -- control plane (console / HTTP API) --------------------------------------------------


def _plane(registry: TenantRegistry | None, *, enabled: bool = True, documents: Any = None):
    from langclaw.gateway.control import ControlPlane
    from langclaw.workflows import WorkflowRegistry
    from tests.test_graph_workflow_gateway import _Bus

    config = LangclawConfig()
    config.tenants.enabled = enabled
    config.workflows.enabled = True
    workflows = WorkflowRegistry()
    workflows.register(graph_spec_of("doc_flow", DOC_FLOW))
    bus = _Bus()
    plane = ControlPlane(
        config=config,
        bus=bus,
        channels=[],
        agent_names=["default"],
        workflow_registry=workflows,
        tenants=registry,
    )
    if documents is not None:
        plane.documents = documents
    return plane, bus


async def test_clients_crud_through_the_control_plane() -> None:
    from langclaw.gateway.control import FeatureDisabledError, NotFoundError

    plane, _ = _plane(TenantRegistry(InMemoryStore()))
    saved = await plane.save_tenant(
        "acme",
        {
            "name": "ACME SRL",
            "tax_id": "RO12345678",
            "chats": ["telegram:-100acme"],
            "profile": {"vat_payer": True, "tax_regime": "micro"},
        },
    )
    assert saved["id"] == "acme" and saved["created_at"]
    assert [t["id"] for t in (await plane.list_tenants())["tenants"]] == ["acme"]
    assert (await plane.get_tenant("acme"))["profile"]["tax_regime"] == "micro"
    with pytest.raises(ValueError, match="already linked to client 'acme'"):
        await plane.save_tenant("beta", {"name": "Beta", "chats": ["telegram:-100acme"]})
    with pytest.raises(ValueError, match="client id"):
        await plane.save_tenant("Bad Id", {"name": "x"})
    with pytest.raises(ValueError, match="name"):
        await plane.save_tenant("gamma", {})
    assert await plane.delete_tenant("acme") is True
    with pytest.raises(NotFoundError):
        await plane.get_tenant("acme")
    off, _ = _plane(None, enabled=False)
    with pytest.raises(FeatureDisabledError, match="LANGCLAW__TENANTS__ENABLED"):
        await off.list_tenants()


async def test_runs_started_from_the_console_can_name_their_client() -> None:
    from langclaw.gateway.control import NotFoundError

    plane, bus = _plane(await _registry())
    await plane.start_workflow(
        "doc_flow", "{}", channel="api", user_id="admin", context_id="c", tenant="acme"
    )
    assert bus.published[-1].metadata["tenant"] == "acme"
    with pytest.raises(NotFoundError, match="client 'nope'"):
        await plane.start_workflow(
            "doc_flow", "{}", channel="api", user_id="admin", context_id="c", tenant="nope"
        )


async def test_document_queries_need_a_client_when_clients_are_on() -> None:
    from langclaw.config.schema import DocumentsConfig
    from langclaw.documents import DocumentServices
    from langclaw.gateway.control import NotFoundError
    from tests.test_documents import FakeStore

    class SearchableStore(FakeStore):
        def for_schema(self, schema: str) -> FakeStore:
            return self.__dict__.setdefault(schema, SearchableStore())

        async def search(self, **_: Any) -> list[dict]:
            return list(self.rows.values())

        async def totals(self, **_: Any) -> dict:
            return {"total": len(self.rows), "amounts": {}}

    store = SearchableStore()
    await store.for_schema("tenant_acme").save("inbox/a.pdf", {"summary": "acme"})
    services = DocumentServices(DocumentsConfig(), store=store, require_tenant=True)
    plane, _ = _plane(await _registry(), documents=services)
    plane._config.documents.enabled = True

    with pytest.raises(ValueError, match="Pick a client"):
        await plane.list_documents()
    with pytest.raises(NotFoundError, match="client 'nope'"):
        await plane.list_documents(tenant="nope")
    acme = await plane.list_documents(tenant="acme")
    assert [d["bucket_key"] for d in acme["documents"]] == ["inbox/a.pdf"]
    assert (await plane.list_documents(tenant="beta"))["documents"] == []


def test_clients_on_without_a_registry_fails_loudly() -> None:
    from langclaw.gateway.manager import GatewayManager
    from tests.test_graph_workflow_gateway import _Bus, _FakeChannel

    config = LangclawConfig()
    config.tenants.enabled = True
    cp = MagicMock()
    cp.get.return_value = MagicMock()
    with pytest.raises(ValueError, match="needs a TenantRegistry"):
        GatewayManager(
            config=config,
            bus=_Bus(),
            checkpointer_backend=cp,
            agent=MagicMock(),
            channels=[_FakeChannel()],
        )
