"""Load tools from MCP servers declared in ``config.mcp.servers``.

Requires: langclaw[mcp]  →  uv add "langclaw[mcp]"

Each server's tools are exposed to the agent as ``mcp_<server>_<tool>`` (the
``mcp_`` prefix is reserved in :mod:`langclaw.naming`), so they never collide
with built-in or developer tools and RBAC can grant them by name.

Servers load independently and fail soft: a server that is down, times out or
is misconfigured is skipped with a warning and reported in the gateway status,
so one broken integration never keeps the bot from starting. Tools are loaded
once at startup; adding or changing a server needs a restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from langclaw.naming import mcp_tool_name

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from langclaw.config.schema import LangclawConfig, McpServerConfig

_DEFAULT_TIMEOUT_S = 30.0


@dataclass
class McpLoadResult:
    """Tools loaded from MCP servers plus a per-server report for status/UIs."""

    tools: list[BaseTool] = field(default_factory=list)
    servers: list[dict[str, Any]] = field(default_factory=list)


def _connection(server: McpServerConfig) -> dict[str, Any]:
    """Translate langclaw config into a langchain-mcp-adapters connection dict."""
    if server.transport == "stdio":
        if not server.command:
            raise ValueError("stdio transport needs 'command'.")
        conn: dict[str, Any] = {
            "transport": "stdio",
            "command": server.command,
            "args": list(server.args),
        }
        if server.env:
            conn["env"] = dict(server.env)
        return conn
    if not server.url:
        raise ValueError(f"{server.transport} transport needs 'url'.")
    conn = {"transport": server.transport, "url": server.url}
    if server.headers and server.transport != "websocket":
        conn["headers"] = dict(server.headers)
    return conn


def _describe_error(exc: BaseException) -> str:
    """Human-readable root cause (the MCP client wraps failures in ExceptionGroups)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, TimeoutError) and not str(exc):
        return "timed out connecting or listing tools"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def _load_server(name: str, server: McpServerConfig, timeout: float) -> list[BaseTool]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({name: _connection(server)})
    tools = await asyncio.wait_for(client.get_tools(server_name=name), timeout=timeout)
    for tool in tools:
        tool.name = mcp_tool_name(name, tool.name)
    return tools


async def load_mcp_tools(
    config: LangclawConfig, *, timeout: float = _DEFAULT_TIMEOUT_S
) -> McpLoadResult:
    """Connect to every enabled MCP server and collect its tools.

    Args:
        config: Resolved langclaw config.
        timeout: Per-server seconds to connect and list tools.

    Returns:
        The namespaced tools and a report per enabled server
        (``name``, ``transport``, ``tools``, ``error``).

    Raises:
        ImportError: Servers are configured but ``langclaw[mcp]`` is not installed.
    """
    servers = {n: s for n, s in config.mcp.servers.items() if s.enabled}
    result = McpLoadResult()
    if not servers:
        return result
    try:
        import langchain_mcp_adapters  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "MCP servers are configured but 'langchain-mcp-adapters' is not installed. "
            "Install the extra with: uv add 'langclaw[mcp]'"
        ) from exc

    for name, server in servers.items():
        report: dict[str, Any] = {
            "name": name,
            "transport": server.transport,
            "tools": [],
            "error": None,
        }
        try:
            tools = await _load_server(name, server, timeout)
        except Exception as exc:  # fail soft: one bad server must not stop the gateway
            message = _describe_error(exc)
            logger.warning(f"MCP server '{name}' skipped — {message}")
            report["error"] = message
        else:
            result.tools.extend(tools)
            report["tools"] = [t.name for t in tools]
            logger.info(f"MCP server '{name}': loaded {len(tools)} tool(s)")
        result.servers.append(report)
    return result
