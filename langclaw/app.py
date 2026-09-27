"""
Langclaw application class — the developer's primary interface.

Usage::

    from langclaw import Langclaw

    app = Langclaw()

    @app.tool()
    async def my_tool(query: str) -> str:
        \"\"\"My custom tool.\"\"\"
        return f"Result: {query}"

    app.role("power_user", tools=["my_tool", "web_search"])
    app.run()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from langclaw.agents.builder import create_claw_agent
from langclaw.config.schema import (
    LangclawConfig,
    PermissionsConfig,
    RoleConfig,
    load_config,
)
from langclaw.context import LangclawContext
from langclaw.gateway.commands import CommandContext
from langclaw.naming import check_command_name_allowed, check_tool_name_allowed
from langclaw.workflows import WorkflowRegistry, WorkflowRuntime, WorkflowSpec

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.runnables import Runnable
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.types import Checkpointer

    from langclaw.bus.base import BaseMessageBus, InboundMessage
    from langclaw.cron.scheduler import CronManager
    from langclaw.gateway.base import BaseChannel
    from langclaw.workflows.graph import RunIndex


class Langclaw:
    """Central application object for building multi-channel agent systems.

    Wraps :func:`~langclaw.agents.builder.create_claw_agent` and the gateway
    infrastructure, exposing a declarative API for tool/role/channel/middleware
    registration.

    Args:
        config:        Pre-built configuration. When ``None``, loaded from
                       env vars, ``.env``, and ``~/.langclaw/config.json``
                       via :func:`~langclaw.config.schema.load_config`.
        system_prompt: Additional instructions **appended** after the base
                       ``AGENTS.md`` prompt.  Use this to give your app a
                       distinct personality, domain focus, or behavioural
                       rules without replacing the built-in defaults
                       (memory protocol, tone, tool-use guidelines).

                       The base ``AGENTS.md`` is always loaded first from
                       the workspace (``~/.langclaw/workspace/AGENTS.md``).
                       Your ``system_prompt`` is concatenated after it,
                       separated by a blank line.  To fully replace the
                       base prompt, edit ``AGENTS.md`` directly instead.

                       Example::

                           app = Langclaw(
                               system_prompt=(
                                   "## Research Assistant\\n"
                                   "You are a financial research assistant.\\n"
                                   "Always check stock prices before answering."
                               ),
                           )
        context_schema: Custom context schema to use for the agent. If omitted,
                       uses the default LangclawContext.
        enable_interpreter: Opt into the sandboxed code interpreter (RLM)
                       programmatically — equivalent to setting
                       ``interpreter.enabled=true`` in config.  Off by default.
                       Requires the ``interpreter`` extra
                       (``uv add 'langclaw[interpreter]'``).
    """

    def __init__(
        self,
        config: LangclawConfig | None = None,
        *,
        system_prompt: str | None = None,
        context_schema: type[LangclawContext] | None = None,
        enable_interpreter: bool = False,
        backend: Any | None = None,
    ) -> None:
        self._config = config or load_config()
        self._system_prompt = system_prompt
        self._context_schema = context_schema
        self._enable_interpreter = enable_interpreter
        # Optional explicit deepagents backend (instance or runtime factory)
        # applied to every agent. ``None`` (default) builds the config-selected
        # backend per agent, correctly re-rooted at each agent's workspace. Pass
        # an instance only for the advanced backends config can't express
        # (StoreBackend with a custom store/namespace, CompositeBackend, a
        # sandbox); note a shared instance is *not* re-rooted per named agent —
        # pass a ``Callable[[ToolRuntime], BackendProtocol]`` if you need that.
        self._backend = backend
        self._extra_tools: list[Any] = []
        # Tools loaded from config-declared MCP servers at gateway startup.
        self._mcp_tools: list[Any] = []
        self._mcp_servers: list[dict[str, Any]] = []
        self._extra_channels: list[BaseChannel] = []
        self._extra_middleware: list[Any] = []
        self._extra_roles: dict[str, list[str]] = {}
        self._extra_role_subagents: dict[str, list[str]] = {}
        self._extra_role_workflows: dict[str, list[str]] = {}
        self._extra_commands: list[tuple[str, Callable[[CommandContext], Awaitable[str]], str]] = []
        self._subagents: list[dict[str, Any]] = []
        self._named_agents: dict[str, dict[str, Any]] = {}
        self._workflows = WorkflowRegistry()
        self._workflow_runtime: WorkflowRuntime | None = None
        # Durable run records (status, reviews); opened at startup.
        self._graph_run_index: RunIndex | None = None
        #: Validation errors of workflow files that failed to load, by name.
        self.graph_file_errors: dict[str, list[str]] = {}
        self._startup_hooks: list[Callable] = []
        self._shutdown_hooks: list[Callable] = []
        self._bus: BaseMessageBus | None = None
        # Probe mode: force a WebSocket-only, isolated channel set (set by run()).
        self._probe_ws_only: bool = False
        self._probe_port: int | None = None
        self._context_defaults: dict[str, Any] = {}
        self._context_factory: (
            Callable[[InboundMessage, dict[str, Any]], Awaitable[LangclawContext]] | None
        ) = None

    @classmethod
    def from_env(cls) -> Langclaw:
        """Create a ``Langclaw`` app from env vars / ``.env`` / ``config.json``."""
        return cls(config=load_config())

    @property
    def config(self) -> LangclawConfig:
        """The resolved configuration object."""
        return self._config

    def get_bus(self) -> BaseMessageBus | None:
        """Return the running message bus, or ``None`` if not yet started."""
        return self._bus

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def tool(self, *, roles: list[str] | None = None) -> Callable:
        """Decorator to register a function as a LangChain tool.

        If the decorated function is not already a ``BaseTool``, it is
        wrapped with ``langchain_core.tools.tool``.

        Args:
            roles: Optional list of role names that should be granted
                   access to this tool.  When provided, the corresponding
                   roles are created/updated in the RBAC config.

        Returns:
            A decorator that registers the tool and returns it.
        """

        def decorator(fn: Callable) -> Any:
            from langchain_core.tools import BaseTool as _BaseTool
            from langchain_core.tools import tool as lc_tool

            t = fn if isinstance(fn, _BaseTool) else lc_tool(fn)
            check_tool_name_allowed(t.name)
            self._extra_tools.append(t)

            if roles:
                for role_name in roles:
                    self._extra_roles.setdefault(role_name, []).append(t.name)

            return t

        return decorator

    def register_tool(self, tool: Any, roles: list[str] | None = None) -> None:
        """Register an existing ``BaseTool`` instance."""
        check_tool_name_allowed(tool.name)
        self._extra_tools.append(tool)

        if roles:
            for role_name in roles:
                self._extra_roles.setdefault(role_name, []).append(tool.name)

    def register_tools(self, tools: list[Any], roles: list[str] | None = None) -> None:
        """Register multiple ``BaseTool`` instances at once."""
        for tool in tools:
            check_tool_name_allowed(tool.name)
        self._extra_tools.extend(tools)

        if roles:
            for role_name in roles:
                for tool in tools:
                    self._extra_roles.setdefault(role_name, []).append(tool.name)

    # ------------------------------------------------------------------
    # Command registration
    # ------------------------------------------------------------------

    def command(
        self,
        name: str,
        *,
        description: str = "",
    ) -> Callable:
        """Decorator to register a custom bot command.

        Commands bypass the LLM and message bus — they are fast system
        operations handled directly by the :class:`CommandRouter`.

        The decorated function must accept a single
        :class:`~langclaw.gateway.commands.CommandContext` argument and
        return a ``str`` response.

        Args:
            name:        Command name without the leading ``/``
                         (e.g. ``"ping"``).
            description: Short help text shown by ``/help``.

        Returns:
            A decorator that registers the command and returns the
            original function.

        Example::

            @app.command("ping", description="check if bot is alive")
            async def ping(ctx: CommandContext) -> str:
                return "Pong!"
        """

        check_command_name_allowed(name)

        def decorator(
            fn: Callable[[CommandContext], Awaitable[str]],
        ) -> Callable[[CommandContext], Awaitable[str]]:
            self._extra_commands.append((name, fn, description))
            return fn

        return decorator

    # ------------------------------------------------------------------
    # Workflow registration
    # ------------------------------------------------------------------

    def _reserved_names(self) -> set[str]:
        """Names already claimed by tools, subagents, named agents, commands.

        Used to reject a workflow name that would make dispatch ambiguous.
        Collision detection runs against the names known at registration time;
        register tools/subagents/agents before the workflows that must not
        clash with them.
        """
        names: set[str] = set()
        for t in self._extra_tools:
            tname = getattr(t, "name", None)
            if tname:
                names.add(tname)
        names.update(s["name"] for s in self._subagents if s.get("name"))
        names.update(self._named_agents)
        names.update(name for name, _fn, _desc in self._extra_commands)
        return names

    def workflow(
        self,
        name: str,
        *,
        graph: Any,
        description: str = "",
        input: type | None = None,
        output: type | None = None,
        max_steps: int | None = None,
        max_concurrency: int = 8,
        timeout_s: float | None = None,
        uses_tools: list[str] | None = None,
        output_key: str = "",
    ) -> Any:
        """Register a LangGraph workflow.

        Pass an **uncompiled** ``StateGraph``; langclaw compiles it with the
        gateway's checkpointer, so every run is checkpointed (a crash resumes from
        the last finished node) and can pause for human review. Nodes reach
        langclaw's tools, models and subagents through
        :func:`langclaw.workflows.steps`, and pause for a person with
        :func:`langclaw.workflows.request_review`::

            from langclaw.workflows import request_review, steps

            async def classify(state):
                text = await steps().tool("bucket_read", key=state["key"])
                return {"sender": await steps().llm(f"Who sent this?\\n{text}")}

            builder = StateGraph(DocState)
            builder.add_node("classify", classify)
            ...
            app.workflow("doc_intake", graph=builder, description="File a document")

        The run input is the graph's input state; the output is its final state
        (or one key of it, with *output_key*). Workflows can also be files —
        ``workflows/<name>.graph.json`` in the agent workspace — which load
        automatically. Workflows are inert unless ``config.workflows.enabled`` is
        ``True``.

        Args:
            name:            Unique handle: tool ``workflow_<name>``,
                             ``/workflows run <name>``, cron, API.
            graph:           The uncompiled ``StateGraph``.
            description:     The **tool description** the LLM reads to decide
                             *when* to run this workflow — write it as guidance.
            input:           Optional Pydantic model validating the run input.
            output:          Optional Pydantic model describing the output.
            max_steps:       LangGraph ``recursion_limit`` per run (``None`` → global).
            max_concurrency: Max nodes running in parallel within one run.
            timeout_s:       Per-run wall-clock budget in seconds.
            uses_tools:      Tool names this workflow declares it needs.
            output_key:      State key returned as the run's output (e.g. ``"report"``);
                             ``""`` ⇒ the whole final state.

        Returns:
            *graph*, unchanged.

        Raises:
            ValueError: If the name collides with an existing workflow, tool,
                        subagent, named agent, or command, or *graph* is not an
                        uncompiled ``StateGraph``.
        """
        self._workflows.register(
            WorkflowSpec(
                name=name,
                graph=graph,
                description=description,
                input_model=input,
                output_model=output,
                max_steps=max_steps,
                max_concurrency=max_concurrency,
                timeout_s=timeout_s,
                uses_tools=list(uses_tools or []),
                output_key=output_key,
            ),
            reserved_names=self._reserved_names(),
        )
        return graph

    # ------------------------------------------------------------------
    # RBAC
    # ------------------------------------------------------------------

    def role(
        self,
        name: str,
        *,
        tools: list[str] | None = None,
        subagents: list[str] | None = None,
        workflows: list[str] | None = None,
    ) -> None:
        """Define or update a permission role.

        If the role already exists (from config or a prior call), each
        axis is merged independently (order-stable dedupe).  Registering
        any role automatically enables the permissions system.

        Three independent RBAC axes:

        - ``tools``     — **default-deny** for unknown roles; ``["*"]`` grants all.
        - ``subagents`` — **default-deny**; subagent types reachable via the
                          ``task`` tool. ``["*"]`` allows every registered one.
        - ``workflows`` — **default-deny**; workflows reachable as the
                          ``workflow_<name>`` tool. ``["*"]`` allows all.

        Args:
            name:      Role identifier (e.g. ``"admin"``, ``"viewer"``).
            tools:     Tool names this role may invoke. Use ``["*"]`` for all.
            subagents: Subagent types this role may delegate to.
            workflows: Workflow names this role may invoke.
        """

        def _merge(store: dict[str, list[str]], values: list[str] | None) -> None:
            existing = store.get(name, [])
            store[name] = list(dict.fromkeys(existing + (values or [])))

        # Always key the role into _extra_roles (even with no tools) so a
        # workflow-only / subagent-only role still triggers the permissions
        # merge in _effective_config.
        _merge(self._extra_roles, tools or [])
        if subagents is not None:
            _merge(self._extra_role_subagents, subagents)
        if workflows is not None:
            _merge(self._extra_role_workflows, workflows)

    # ------------------------------------------------------------------
    # Subagent registration
    # ------------------------------------------------------------------

    def subagent(
        self,
        name: str,
        *,
        description: str,
        graph: Runnable | dict[str, Any] | None = None,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        model: str | BaseChatModel | None = None,
        roles: list[str] | None = None,
        output: Literal["main_agent", "channel"] = "main_agent",
    ) -> None:
        """Register a subagent that the main agent can delegate tasks to.

        Subagents are invoked by the main agent via the ``task`` tool
        provided by deepagents.  Each subagent runs in an isolated
        context and returns a single result.

        There are three ways to define what the subagent does:

        1. **Declarative** — pass ``system_prompt`` (and optionally
           ``tools``, ``model``).  Langclaw builds the agent, resolves
           tool names, and injects its middleware.

        2. **Pre-built graph** — pass a ``Runnable`` or
           ``CompiledStateGraph`` via ``graph``.  Langclaw wraps it
           into a deepagents ``CompiledSubAgent`` and passes it through
           as-is.  The runnable's state schema **must** include a
           ``messages`` key.

        3. **deepagents dict** — pass a ``SubAgent`` or
           ``CompiledSubAgent`` TypedDict via ``graph``.  For
           ``SubAgent`` dicts, Langclaw prepends its middleware
           (channel context, RBAC).  ``CompiledSubAgent`` dicts (with
           a ``runnable`` key) are passed through unchanged.

        When ``graph`` is ``None``, ``system_prompt`` is required.

        Args:
            name:          Unique identifier used by the main agent when
                           calling the ``task`` tool.
            description:   What this subagent does.  Be specific —
                           the main agent uses this to decide when to
                           delegate.
            graph:         A pre-built ``Runnable``, ``CompiledStateGraph``,
                           or deepagents ``SubAgent``/``CompiledSubAgent``
                           dict.  Mutually exclusive with ``system_prompt``.
            system_prompt: Instructions for the subagent (declarative mode).
                           Required when ``graph`` is not provided.
            tools:         Tool **names** this subagent may use (declarative
                           mode only).  Resolved at build time against all
                           registered tools.  ``None`` inherits the main
                           agent's full tool set.
            model:         Override the main agent's model (declarative mode
                           only).  Accepts ``"provider:model"`` strings or
                           a ``BaseChatModel`` instance.
            roles:         Reserved for future RBAC scoping of which user
                           roles may trigger this subagent.
            output:        ``"main_agent"`` (default) returns the result
                           to the main agent.  ``"channel"`` publishes
                           the result directly to the originating channel
                           via the message bus (declarative mode only).

        Raises:
            ValueError: If neither ``graph`` nor ``system_prompt`` is
                        provided, or if both are provided, or if
                        ``output`` is invalid.

        Example::

            # Declarative — Langclaw builds the agent
            app.subagent(
                "researcher",
                description="Researches topics using web search",
                system_prompt="You are a thorough researcher...",
                tools=["web_search", "web_fetch"],
                model="openai:gpt-4.1",
            )

            # Pre-built LangGraph graph
            my_graph = create_agent("openai:gpt-4.1", tools=[...])
            app.subagent(
                "my-graph",
                description="Custom LangGraph pipeline",
                graph=my_graph,
            )

            # deepagents SubAgent dict
            app.subagent(
                "analyst",
                description="Financial analyst",
                graph={
                    "system_prompt": "Analyze data.",
                    "tools": [my_tool],
                    "model": "openai:gpt-4.1",
                },
            )
        """
        from langchain_core.runnables import Runnable as _Runnable

        if graph is not None and system_prompt is not None:
            raise ValueError(
                "'graph' and 'system_prompt' are mutually exclusive. "
                "Use 'graph' to bring a pre-built agent, or "
                "'system_prompt' for Langclaw to build one."
            )

        if graph is not None:
            if isinstance(graph, _Runnable):
                self._subagents.append(
                    {
                        "name": name,
                        "description": description,
                        "runnable": graph,
                    }
                )
            elif isinstance(graph, dict):
                self._subagents.append({**graph, "name": name, "description": description})
            else:
                raise TypeError(f"'graph' must be a Runnable or dict, got {type(graph).__name__}")
            return

        if system_prompt is None:
            raise ValueError("Either 'graph' or 'system_prompt' is required.")

        if output not in ("main_agent", "channel"):
            raise ValueError(
                f"Invalid output mode {output!r} for subagent {name!r}. "
                "Must be 'main_agent' or 'channel'."
            )

        self._subagents.append(
            {
                "name": name,
                "description": description,
                "system_prompt": system_prompt,
                "tools": tools,
                "model": model,
                "roles": roles,
                "output": output,
            }
        )

    # ------------------------------------------------------------------
    # Named agent registration
    # ------------------------------------------------------------------

    def agent(
        self,
        name: str,
        *,
        description: str,
        display_name: str | None = None,
        system_prompt: str | None = None,
        tools: list[Any] | None = None,
        model: str | BaseChatModel | None = None,
    ) -> None:
        """Register a named agent that users can switch to via ``/switch <name>``.

        Named agents are fully independent agent instances built with the same
        :func:`~langclaw.agents.builder.create_claw_agent` factory as the main
        agent.  Each named agent:

        - Gets its own isolated LangGraph conversation thread
          (``context_id = "agent:<name>"``), so history never bleeds across modes.
        - Shares the same checkpointer backend as the main agent.
        - Can use a different system prompt, tool set, or model.

        Users switch between agents via the built-in ``/agent <name>`` command,
        and can return to the main agent with ``/agent default``.

        Args:
            name:          Unique identifier used with ``/agent <name>``.
                           Must not be ``"default"`` (reserved sentinel).
            description:   Short description shown by ``/agent`` with no args.
            display_name:  Optional human-facing name for this agent. Injected
                           into the system prompt so the model knows its own
                           name, and shown alongside the routing key in
                           ``/agent`` listings.  When ``None``, only the
                           registered ``name`` is used.
            system_prompt: System prompt for this agent.  When ``None``, the
                           base ``AGENTS.md`` prompt is used unchanged.
            tools:         Explicit list of tool instances for this agent.
                           ``None`` inherits the config-driven built-in tools
                           without the extra tools registered on the app.
            model:         Override the default model.  Accepts
                           ``"provider:model"`` strings or a ``BaseChatModel``.

        Raises:
            ValueError: If ``name`` is ``"default"`` (reserved).

        Example::

            app.agent(
                "researcher",
                description="Deep research mode with web tools",
                system_prompt="You are a meticulous researcher. Always cite sources.",
                tools=[web_search, web_fetch],
                model="openai:gpt-4.1",
            )
        """
        if name == "default":
            raise ValueError(
                "'default' is a reserved agent name — it refers to the main agent. "
                "Choose a different name."
            )
        self._named_agents[name] = {
            "name": name,
            "description": description,
            "display_name": display_name,
            "system_prompt": system_prompt,
            "tools": tools,
            "model": model,
        }

    # ------------------------------------------------------------------
    # Channels & middleware
    # ------------------------------------------------------------------

    def add_channel(self, channel: BaseChannel) -> None:
        """Register a custom channel alongside config-driven ones."""
        self._extra_channels.append(channel)

    def add_middleware(self, middleware: Any) -> None:
        """Append middleware to the end of the built-in stack."""
        self._extra_middleware.append(middleware)

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def on_startup(self, fn: Callable) -> Callable:
        """Decorator to register an async function called on gateway startup."""
        self._startup_hooks.append(fn)
        return fn

    def on_shutdown(self, fn: Callable) -> Callable:
        """Decorator to register an async function called on gateway shutdown."""
        self._shutdown_hooks.append(fn)
        return fn

    # ------------------------------------------------------------------
    # Context hooks
    # ------------------------------------------------------------------

    def set_context_defaults(self, **kwargs: Any) -> None:
        """Set extra kwargs merged into context construction.

        Use for app-level singletons like service clients or shared runners
        that every context instance needs.

        Args:
            **kwargs: Extra keyword arguments to pass to the context schema.
        """
        self._context_defaults.update(kwargs)

    def context_factory(
        self,
        fn: Callable[[InboundMessage, dict[str, Any]], Awaitable[LangclawContext]],
    ) -> Callable[[InboundMessage, dict[str, Any]], Awaitable[LangclawContext]]:
        """Decorator to register a per-message context factory.

        The factory receives the inbound message and base kwargs dict,
        and must return a context instance. Takes precedence over
        ``set_context_defaults()`` when set.

        Args:
            fn: Async callable ``(msg, base_kwargs) -> LangclawContext``.

        Returns:
            The original function.
        """
        self._context_factory = fn
        return fn

    # ------------------------------------------------------------------
    # Agent creation (lower-level API for REPL / tests)
    # ------------------------------------------------------------------

    def create_agent(
        self,
        *,
        checkpointer: Checkpointer | None = None,
        cron_manager: CronManager | None = None,
        model: BaseChatModel | None = None,
        bus: BaseMessageBus | None = None,
        context_schema: type[LangclawContext] | None = None,
    ) -> CompiledStateGraph:
        """Build the agent with all registered tools, middleware, and roles.

        This is the lower-level API — use it when you need the compiled
        LangGraph agent without the full gateway (e.g. for a REPL or
        tests).  :meth:`run` calls this internally.

        Args:
            checkpointer: LangGraph checkpoint saver for conversation state.
            cron_manager:  Running cron manager for scheduled jobs.
            model:         Override the configured LLM.
            bus:           Running message bus — required when any registered
                           subagent uses ``output="channel"``.

        Returns:
            A compiled LangGraph runnable.
        """
        effective_config = self._build_effective_config()

        return create_claw_agent(
            effective_config,
            checkpointer=checkpointer,
            cron_manager=cron_manager,
            extra_tools=[*self._extra_tools, *self._mcp_tools] or None,
            extra_middleware=self._extra_middleware or None,
            subagents=self._subagents or None,
            system_prompt=self._system_prompt,
            bus=bus,
            model=model,
            backend=self._backend,
            context_schema=context_schema,
            display_name=effective_config.agents.display_name or None,
            # Pass the registry whenever workflows are enabled (even if empty) so
            # a workflow file added at runtime goes live on rebuild.
            workflow_registry=self._workflows if effective_config.workflows.enabled else None,
            workflow_runtime=self._get_workflow_runtime(effective_config),
        )

    def _get_workflow_runtime(self, effective_config: LangclawConfig) -> WorkflowRuntime | None:
        """Lazily build (and cache) the shared :class:`WorkflowRuntime`.

        Returns ``None`` only when workflows are disabled. Built whenever the
        feature is enabled — even with zero registered workflows — so a workflow
        file added at runtime can run. Cached so every agent shares one runtime,
        i.e. one global ``max_concurrent_runs`` ceiling.
        """
        if not effective_config.workflows.enabled:
            return None
        if self._workflow_runtime is None:
            from langclaw.workflows.files import WorkflowFiles

            runtime = WorkflowRuntime(effective_config.workflows)
            runtime.files = WorkflowFiles(
                self._config.agents.workflows_dir,
                registry=self._workflows,
                reload_cb=self._reload_workflow_files,
                catalog=runtime.catalog,
            )
            self._workflow_runtime = runtime
        return self._workflow_runtime

    def _reload_workflow_files(self) -> bool:
        """Reconcile the registry with ``workflows/<name>.graph.json`` files.

        Idempotent — run at startup (before the agent is built) and whenever the
        gateway notices the folder changed, so an added, edited, or deleted file
        goes live without a restart. Only file-loaded specs are touched; a
        workflow registered in Python always wins, so a file can never shadow or
        evict it. Invalid files are skipped and their errors kept in
        :attr:`graph_file_errors`. Returns whether the registry changed.
        """
        if not self._config.workflows.enabled:
            return False
        from langclaw.workflows.graph import build_state_graph, load_graph_files

        valid, invalid = load_graph_files(self._config.agents.workflows_dir)
        self.graph_file_errors = {name: exc.errors for name, exc in invalid.items()}
        for name, exc in invalid.items():
            logger.warning(f"Skipping workflow file {name!r}: {exc}")
        changed = False
        for name in self._workflows.names():
            spec = self._workflows.get(name)
            if spec is None or spec.graph_spec is None:
                continue
            want = valid.get(name)
            if want is None or want.model_dump() != spec.graph_spec.model_dump():
                self._workflows.unregister(name)
                changed = True
        reserved = self._reserved_names()
        for name, graph_spec in valid.items():
            if name in self._workflows:
                if self._workflows.get(name).graph_spec is None:
                    logger.warning(
                        f"Workflow file {name!r} shadows a workflow registered in code; ignoring."
                    )
                continue
            try:
                self._workflows.register(
                    WorkflowSpec(
                        name=name,
                        graph=build_state_graph(graph_spec),
                        description=graph_spec.description,
                        graph_spec=graph_spec,
                    ),
                    reserved_names=reserved,
                )
                changed = True
            except ValueError as exc:
                logger.warning(f"Skipping workflow file {name!r}: {exc}")
        return changed

    async def _open_workflow_stores(self, stack: AsyncExitStack, cp_cfg: Any, wf_cfg: Any) -> None:
        """Open the durable run index (status, reviews), bound to *stack*.

        Uses the checkpointer's backend: a sibling SQLite file (avoids write-lock
        contention with the checkpointer DB) or the same Postgres DSN. No-op when
        workflows are off.
        """
        if not wf_cfg.enabled:
            return

        from pathlib import Path

        from langclaw.workflows.graph import RunIndex, StoreRunIndexBackend
        from langclaw.workflows.store import make_workflow_store_backend

        db_path = str(Path(cp_cfg.sqlite.db_path).expanduser().with_suffix(".workflows.db"))
        backend = make_workflow_store_backend(
            cp_cfg.backend, db_path=db_path, dsn=cp_cfg.postgres.dsn
        )
        await stack.enter_async_context(backend)
        self._graph_run_index = RunIndex(StoreRunIndexBackend(backend.get_store()))

    def _attach_graph_runner(self, cfg: LangclawConfig, checkpointer: Any) -> None:
        """Run workflows on the gateway checkpointer + durable run index."""
        runtime = self._get_workflow_runtime(self._build_effective_config())
        if runtime is None:
            return
        from langclaw.workflows.graph import GraphWorkflowRunner

        runtime.set_graph_runner(
            GraphWorkflowRunner(
                checkpointer=checkpointer,
                index=self._graph_run_index,
                max_steps=cfg.workflows.max_steps_per_run,
            )
        )

    async def _resume_incomplete_workflows(self) -> None:
        """Continue runs a prior process left mid-flight (crash recovery).

        Must run *after* the agent is built — that's when the step executor is
        registered. Runs paused for review stay paused.
        """
        if self._workflow_runtime is None or self._graph_run_index is None:
            return
        resumed = await self._workflow_runtime.resume_incomplete(self._workflows.get)
        if resumed:
            logger.info(f"Resumed {len(resumed)} interrupted workflow run(s).")

    # ------------------------------------------------------------------
    # Gateway (high-level API)
    # ------------------------------------------------------------------

    def run(self, *, probe: bool = False, probe_port: int | None = None) -> None:
        """Start the multi-channel gateway (blocking).

        Wires up the message bus, checkpointer, channels, cron manager,
        and agent, then runs ``GatewayManager`` until cancelled.

        Args:
            probe: When True, run a **WebSocket-only** gateway with every other
                channel disabled regardless of config. This isolates the surface
                for the probe harness so test traffic never reaches a real
                Telegram/Discord chat. Applied at the channel-assembly seam — the
                user's config file is never mutated.
            probe_port: Override the WebSocket port in probe mode (defaults to the
                configured ``channels.websocket.port``).
        """
        self._probe_ws_only = probe
        self._probe_port = probe_port
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        """Async gateway startup — the core wiring logic."""
        from langclaw.bus import make_message_bus
        from langclaw.checkpointer import make_checkpointer_backend
        from langclaw.gateway.manager import GatewayManager

        cfg = self._config

        # Every log sink redacts known secrets (tokens, API keys, DSNs): third-party
        # libraries log request URLs and exception messages that can contain them.
        import sys

        from langclaw.log_redaction import (
            RedactingFormatter,
            SecretRedactor,
            collect_secrets,
            install_redacting_streams,
            make_daily_file_writer,
            make_redacting_sink,
        )

        redactor = SecretRedactor(collect_secrets(cfg))
        # Also covers output that bypasses logging, e.g. the CLI's crash traceback.
        install_redacting_streams(redactor)

        # Configure stdlib logging (used by channel implementations).
        # Below WARNING → stdout, WARNING+ → stderr: hosts like Railway label
        # every stderr line an error, which buried real errors under INFO noise.
        quiet = logging.StreamHandler(sys.stdout)
        quiet.addFilter(lambda record: record.levelno < logging.WARNING)
        loud = logging.StreamHandler(sys.stderr)
        loud.setLevel(logging.WARNING)
        logging.basicConfig(level=cfg.log_level.upper(), handlers=[quiet, loud])
        for handler in logging.getLogger().handlers:
            handler.setFormatter(
                RedactingFormatter(
                    redactor,
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
        # Configure loguru (used by GatewayManager, middleware, tools).
        # diagnose=False: tracebacks must not dump local variable values.
        logger.remove()
        level = cfg.log_level.upper()
        warning_no = logger.level("WARNING").no
        logger.add(
            make_redacting_sink(redactor, lambda m: sys.stdout.write(m)),
            level=level,
            filter=lambda record: record["level"].no < warning_no,
            diagnose=False,
        )
        logger.add(
            make_redacting_sink(redactor, lambda m: sys.stderr.write(m)),
            level=max(logger.level(level).no, warning_no),
            diagnose=False,
        )

        # Write INFO-and-above to date-based log files so agents can self-debug.
        logger.add(
            make_redacting_sink(
                redactor, make_daily_file_writer(cfg.agents.workspace_dir / "logs")
            ),
            level="INFO",
            diagnose=False,
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} - {message}"
            ),
        )

        bus_cfg = cfg.bus
        cp_cfg = cfg.checkpointer

        bus = self._bus = make_message_bus(
            bus_cfg.backend,
            rabbitmq_url=bus_cfg.rabbitmq.amqp_url,
            rabbitmq_queue=bus_cfg.rabbitmq.queue_name,
            kafka_servers=bus_cfg.kafka.bootstrap_servers,
            kafka_topic=bus_cfg.kafka.topic,
            kafka_group_id=bus_cfg.kafka.group_id,
        )
        checkpointer_backend = make_checkpointer_backend(
            cp_cfg.backend,
            db_path=cp_cfg.sqlite.db_path,
            dsn=cp_cfg.postgres.dsn,
        )

        channels = self._build_all_channels()
        if not channels:
            logger.error(
                "No channels enabled. Enable at least one in your config "
                "or register one with app.add_channel()."
            )
            return

        for hook in self._startup_hooks:
            await hook()

        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(bus)
                await stack.enter_async_context(checkpointer_backend)
                await self._open_workflow_stores(stack, cp_cfg, cfg.workflows)

                # Load runtime-authored (file-written) workflows from disk so they
                # boot as workflow_<name> tools alongside @app.workflow ones.
                self._reload_workflow_files()

                cron_manager = None
                if cfg.cron.enabled:
                    from langclaw.cron import make_cron_manager

                    cron_manager = make_cron_manager(bus=bus, config=cfg.cron)

                # Connect config-declared MCP servers; their tools join the
                # agent's toolset (fail-soft per server).
                from langclaw.mcp import load_mcp_tools

                mcp = await load_mcp_tools(cfg)
                self._mcp_tools, self._mcp_servers = mcp.tools, mcp.servers

                # Build the main agent and capture the spec used so that the
                # gateway can rebuild it when AGENTS.md changes.
                checkpointer = checkpointer_backend.get()
                self._attach_graph_runner(cfg, checkpointer)
                agent = self.create_agent(
                    checkpointer=checkpointer,
                    cron_manager=cron_manager,
                    bus=bus,
                    context_schema=self._context_schema,
                )

                # Crash recovery (after the agent build registers the executor).
                await self._resume_incomplete_workflows()

                manager = GatewayManager(
                    config=self._build_effective_config(),
                    bus=bus,
                    checkpointer_backend=checkpointer_backend,
                    agent=agent,
                    channels=channels,
                    cron_manager=cron_manager,
                    extra_commands=self._extra_commands or None,
                    context_schema=self._context_schema,
                    context_defaults=self._context_defaults,
                    context_factory=self._context_factory,
                    named_agent_specs=self._named_agents or None,
                    agent_backend=self._backend,
                    default_agent_spec={
                        "extra_tools": [*self._extra_tools, *self._mcp_tools] or None,
                        "extra_middleware": self._extra_middleware or None,
                        "subagents": self._subagents or None,
                        "system_prompt": self._system_prompt,
                        "bus": bus,
                        "model": None,
                    },
                    workflow_runtime=self._workflow_runtime,
                    # Pass the registry (even if empty) whenever workflows are
                    # enabled so /workflows stays registered/discoverable; None
                    # when the feature is off so the command stays hidden.
                    workflow_registry=(self._workflows if cfg.workflows.enabled else None),
                    # Reconcile workflows/<name>.graph.json on folder change → live tools.
                    workflows_reload_cb=(
                        self._reload_workflow_files if cfg.workflows.enabled else None
                    ),
                    mcp_servers=self._mcp_servers,
                    # The control plane saves/deletes workflow files here.
                    workflows_dir=cfg.agents.workflows_dir if cfg.workflows.enabled else None,
                    workflow_file_errors=lambda: self.graph_file_errors,
                )

                cron_status = "enabled" if cron_manager else "disabled"
                logger.info(
                    "Gateway starting — channels: {}, bus: {}, checkpointer: {}, cron: {}",
                    [ch.name for ch in channels if ch.is_enabled()],
                    bus_cfg.backend,
                    cp_cfg.backend,
                    cron_status,
                )
                await manager.run()
        finally:
            self._bus = None
            for hook in self._shutdown_hooks:
                await hook()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _interpreter_active(self) -> bool:
        """Whether the sandboxed code interpreter should be wired in.

        Active when either the ``enable_interpreter=True`` constructor flag was
        passed or ``interpreter.enabled`` is set in config.
        """
        return bool(self._enable_interpreter or self._config.interpreter.enabled)

    def _build_effective_config(self) -> LangclawConfig:
        """Return a config copy with programmatic overrides applied.

        Merges ``app.role()`` definitions and reflects the
        ``enable_interpreter=True`` constructor flag onto
        ``interpreter.enabled`` so downstream consumers (the agent builder) see
        a single coherent config.
        """
        flag_flips_interpreter = self._enable_interpreter and not self._config.interpreter.enabled
        has_roles = bool(
            self._extra_roles or self._extra_role_subagents or self._extra_role_workflows
        )
        if not has_roles and not flag_flips_interpreter:
            return self._config

        cfg = self._config.model_copy(deep=True)
        if has_roles:
            cfg.permissions = self._merge_permissions(cfg.permissions)
        if flag_flips_interpreter:
            cfg.interpreter.enabled = True
        return cfg

    def _merge_permissions(self, base: PermissionsConfig) -> PermissionsConfig:
        """Merge ``app.role()`` definitions into the permissions config.

        Programmatic roles are merged on top of config-file roles.
        Registering any role auto-enables the permissions system.
        """
        perms = base.model_copy(deep=True)
        perms.enabled = True

        names = (
            set(self._extra_roles)
            | set(self._extra_role_subagents)
            | set(self._extra_role_workflows)
        )

        def _merge(existing: list[str], extra: list[str]) -> list[str]:
            return list(dict.fromkeys(existing + extra))

        for name in names:
            current = perms.roles.get(name)
            perms.roles[name] = RoleConfig(
                tools=_merge(
                    current.tools if current else [],
                    self._extra_roles.get(name, []),
                ),
                subagents=_merge(
                    current.subagents if current else [],
                    self._extra_role_subagents.get(name, []),
                ),
                workflows=_merge(
                    current.workflows if current else [],
                    self._extra_role_workflows.get(name, []),
                ),
            )

        return perms

    def _build_all_channels(self) -> list[BaseChannel]:
        """Build channels from config + programmatically registered ones."""
        channels: list[BaseChannel] = []

        ch_cfg = self._config.channels

        # Probe mode: WebSocket-only, every other channel forced off. Built from a
        # config *copy* so the user's config file is untouched; extra programmatic
        # channels are excluded too, to guarantee an isolated test surface.
        if self._probe_ws_only:
            from langclaw.gateway.websocket import WebSocketChannel

            # Force the loopback host. Probe mode is an isolated test surface — the
            # full toolset + LLM behind a channel with no auth by default — so it
            # must never bind a public interface, even if the user configured
            # channels.websocket.host (e.g. 0.0.0.0) for a real deployment.
            updates: dict[str, Any] = {"enabled": True, "host": "127.0.0.1"}
            if self._probe_port is not None:
                updates["port"] = self._probe_port
            ws_cfg = ch_cfg.websocket.model_copy(update=updates)
            return [WebSocketChannel(ws_cfg)]

        if ch_cfg.telegram.enabled:
            try:
                from langclaw.gateway.telegram import TelegramChannel

                channels.append(TelegramChannel(ch_cfg.telegram))
            except ImportError:
                logger.warning(
                    "Telegram enabled but python-telegram-bot not installed. "
                    "Run: uv add 'langclaw[telegram]'"
                )

        if ch_cfg.discord.enabled:
            try:
                from langclaw.gateway.discord import DiscordChannel

                channels.append(DiscordChannel(ch_cfg.discord))
            except ImportError:
                logger.warning(
                    "Discord enabled but discord.py not installed. Run: uv add 'langclaw[discord]'"
                )

        if ch_cfg.websocket.enabled:
            try:
                from langclaw.gateway.websocket import WebSocketChannel

                channels.append(WebSocketChannel(ch_cfg.websocket))
            except ImportError:
                logger.warning(
                    "WebSocket enabled but websockets not installed. "
                    "Run: uv add 'langclaw[websocket]'"
                )

        if ch_cfg.slack.enabled:
            try:
                from langclaw.gateway.slack import SlackChannel

                channels.append(SlackChannel(ch_cfg.slack))
            except ImportError:
                logger.warning(
                    "Slack enabled but slack-bolt not installed. Run: uv add 'langclaw[slack]'"
                )

        if ch_cfg.matrix.enabled:
            try:
                from langclaw.gateway.matrix import MatrixChannel

                channels.append(MatrixChannel(ch_cfg.matrix))
            except ImportError:
                logger.warning(
                    "Matrix enabled but matrix-nio not installed. Run: uv add 'langclaw[matrix]'"
                )

        if ch_cfg.api.enabled:
            from langclaw.gateway.api import ApiChannel

            channels.append(ApiChannel(ch_cfg.api))

        _check_channel_credentials(channels, ch_cfg)
        channels.extend(self._extra_channels)
        return channels


# Config fields each built-in channel needs before it can start. Keyed by
# ``BaseChannel.name``, which is also the channel's section under ``channels``.
_CHANNEL_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram": ("token",),
    "discord": ("token",),
    "slack": ("bot_token", "app_token"),
    "matrix": ("homeserver_url", "user_id", "access_token", "device_id"),
    "api": ("token",),
}


def _check_channel_credentials(channels: list[BaseChannel], ch_cfg: Any) -> None:
    """Raise if a config-enabled channel is missing the credentials it needs.

    Without this, ``GatewayManager`` drops the channel (``is_enabled()`` is
    False) and the bot silently never connects.

    Raises:
        ValueError: Naming the channel and each empty ``LANGCLAW__`` env var.
    """
    for channel in channels:
        if channel.is_enabled():
            continue
        section = getattr(ch_cfg, channel.name, None)
        missing = [
            f"LANGCLAW__CHANNELS__{channel.name.upper()}__{field.upper()}"
            for field in _CHANNEL_REQUIRED_FIELDS.get(channel.name, ())
            if not getattr(section, field, "")
        ]
        detail = ", ".join(missing) if missing else "its required credentials"
        raise ValueError(
            f"Channel {channel.name!r} is enabled but cannot start: set {detail} "
            f"(or disable it with LANGCLAW__CHANNELS__{channel.name.upper()}__ENABLED=false)."
        )


__all__ = ["Langclaw", "CommandContext"]
