FROM python:3.13-slim

WORKDIR /app
COPY . .

# Extras:
#   telegram — the Telegram channel
#   postgres — Postgres checkpointer (conversation memory)
#   search   — web_search / web_fetch (crawl4ai)
# Extra packages:
#   langchain-openrouter — the openrouter:<model> provider
#   sqlalchemy + asyncpg — Postgres cron data store (scheduled jobs)
RUN pip install --no-cache-dir -e ".[telegram,postgres,search]" \
        langchain-openrouter sqlalchemy asyncpg \
    # crawl4ai drives a headless Chromium; install it and its system libraries.
    && crawl4ai-setup

CMD ["langclaw", "gateway"]
