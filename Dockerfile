FROM python:3.13-slim

WORKDIR /app
COPY . .

# Extras:
#   telegram — the Telegram channel
#   postgres — Postgres checkpointer (conversation memory)
#   search   — web_search / web_fetch (crawl4ai)
#   api      — HTTP control-plane API (UIs such as Appsmith)
#   interpreter — sandboxed JS `eval` + saved (file-authored) workflows
#   mcp      — tools from MCP servers (LANGCLAW__MCP__SERVERS)
#   documents — bucket + documents-table tools (LANGCLAW__DOCUMENTS__*)
# Extra packages:
#   langchain-openrouter — the openrouter:<model> provider
#   sqlalchemy + asyncpg — Postgres cron data store (scheduled jobs)
RUN pip install --no-cache-dir -e ".[telegram,postgres,search,api,interpreter,mcp,documents]" \
        langchain-openrouter sqlalchemy asyncpg \
    # crawl4ai drives a headless Chromium; install it and its system libraries.
    # (Not `crawl4ai-setup`: it swallows install failures and exits 0.)
    && python -m playwright install --with-deps chromium

CMD ["langclaw", "gateway"]
