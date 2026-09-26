"""MCP servers from config become namespaced agent tools (langclaw/mcp.py)."""

from __future__ import annotations

import sys
import textwrap

import pytest

from langclaw.config.schema import LangclawConfig, McpServerConfig

pytest.importorskip("langchain_mcp_adapters")

from langclaw.mcp import load_mcp_tools  # noqa: E402

_SERVER = textwrap.dedent(
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("demo")

    @server.tool()
    def add(a: int, b: int) -> int:
        \"\"\"Add two numbers.\"\"\"
        return a + b

    server.run("stdio")
    """
)


def _stdio(script_path) -> McpServerConfig:
    return McpServerConfig(transport="stdio", command=sys.executable, args=[str(script_path)])


def test_servers_parse_from_env_json(monkeypatch) -> None:
    monkeypatch.setenv(
        "LANGCLAW__MCP__SERVERS",
        '{"docs": {"transport": "streamable_http", "url": "https://x/mcp",'
        ' "headers": {"Authorization": "Bearer abc12345"}}}',
    )
    server = LangclawConfig().mcp.servers["docs"]
    assert server.url == "https://x/mcp"
    assert server.headers == {"Authorization": "Bearer abc12345"}


def test_invalid_server_name_rejected(monkeypatch) -> None:
    monkeypatch.setenv("LANGCLAW__MCP__SERVERS", '{"my-docs": {"url": "https://x/mcp"}}')
    with pytest.raises(ValueError, match="my-docs"):
        LangclawConfig()


async def test_no_servers_loads_nothing() -> None:
    result = await load_mcp_tools(LangclawConfig())
    assert result.tools == [] and result.servers == []


async def test_stdio_server_tools_are_namespaced_and_callable(tmp_path) -> None:
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    cfg = LangclawConfig()
    cfg.mcp.servers = {"demo": _stdio(script)}

    result = await load_mcp_tools(cfg)

    assert [t.name for t in result.tools] == ["mcp_demo_add"]
    assert result.servers == [
        {"name": "demo", "transport": "stdio", "tools": ["mcp_demo_add"], "error": None}
    ]
    out = await result.tools[0].ainvoke({"a": 2, "b": 3})
    assert "5" in str(out)


async def test_broken_server_is_skipped_not_fatal(tmp_path) -> None:
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    cfg = LangclawConfig()
    cfg.mcp.servers = {
        "broken": McpServerConfig(transport="stdio", command="/nonexistent/binary"),
        "demo": _stdio(script),
        "off": McpServerConfig(url="https://x/mcp", enabled=False),
    }

    result = await load_mcp_tools(cfg, timeout=20)

    assert [t.name for t in result.tools] == ["mcp_demo_add"]
    by_name = {s["name"]: s for s in result.servers}
    assert by_name["broken"]["error"] and by_name["broken"]["tools"] == []
    assert "off" not in by_name


def test_mcp_prefix_is_reserved_for_developer_tools() -> None:
    from langclaw.naming import check_tool_name_allowed

    with pytest.raises(ValueError, match="mcp"):
        check_tool_name_allowed("mcp_github_search")


def test_mcp_secrets_are_redacted() -> None:
    from langclaw.log_redaction import collect_secrets

    cfg = LangclawConfig()
    cfg.mcp.servers = {
        "docs": McpServerConfig(
            url="https://x/mcp", headers={"Authorization": "Bearer header-secret-1"}
        ),
        "local": McpServerConfig(transport="stdio", command="x", env={"API_KEY": "env-secret-2"}),
    }
    secrets = collect_secrets(cfg, environ={})
    assert "Bearer header-secret-1" in secrets
    assert "env-secret-2" in secrets


async def test_unreachable_server_reports_root_cause() -> None:
    cfg = LangclawConfig()
    cfg.mcp.servers = {"down": McpServerConfig(url="http://127.0.0.1:9/mcp")}

    result = await load_mcp_tools(cfg, timeout=20)

    error = result.servers[0]["error"]
    assert "TaskGroup" not in error
    assert "Connect" in error or "connect" in error
