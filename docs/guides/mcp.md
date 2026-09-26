# MCP Servers (Tools Without Code)

Connect any [Model Context Protocol](https://modelcontextprotocol.io) server and
its tools become agent tools, with no Python needed. Useful for services that
already publish an MCP server (GitHub, Notion, Linear, databases, internal APIs).

## Configure

```bash
uv add "langclaw[mcp]"

# JSON map of server name → connection
LANGCLAW__MCP__SERVERS='{
  "docs":   {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer <token>"}},
  "search": {"transport": "sse", "url": "https://search.example.com/sse"},
  "local":  {"transport": "stdio", "command": "npx", "args": ["-y", "some-mcp-server"],
             "env": {"API_KEY": "<key>"}}
}'
```

| Field | Default | Meaning |
|---|---|---|
| `transport` | `streamable_http` | `streamable_http`, `sse`, `websocket`, or `stdio` |
| `url` | | Server URL (network transports) |
| `headers` | `{}` | HTTP headers, e.g. auth (not sent over `websocket`) |
| `command` / `args` / `env` | | Process to launch for `stdio` |
| `enabled` | `true` | Keep a server configured but off |

Server names must be `[a-z0-9_]+`: they become part of tool names.

## How tools appear

A server `docs` with a tool `search` is exposed as **`mcp_docs_search`**. The
`mcp_` prefix is reserved, so MCP tools never collide with built-in or
developer tools. With RBAC enabled, grant them like any tool
(`"tools": ["mcp_docs_search"]`).

## Behaviour and limits

- **Loaded at startup.** Adding, removing or changing a server needs a restart
  (on Railway, saving the variable redeploys).
- **Fail-soft per server.** A server that is down, times out (30 s) or is
  misconfigured is skipped with a warning; the gateway still starts. Each
  server's result (tool names or the error) is in `GET /v1/status` under
  `mcp_servers`, and on the web console's Status page.
- **Secrets are redacted.** Header and `env` values are removed from all logs.
- **`stdio` servers run inside the gateway's container**, so the command (e.g.
  `npx`, `uvx`) must exist in that image. Remote servers (`url`) need nothing
  extra.
