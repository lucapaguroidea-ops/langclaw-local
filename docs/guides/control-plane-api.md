# Control-Plane API & UIs

The **API channel** exposes a running gateway over HTTP so a UI (Appsmith,
Retool, your own web app) or a script can chat with the agent and manage
workflows, runs, and schedules. It is an ordinary langclaw channel: chat goes
through the same bus → gateway → agent pipeline as Telegram, and management
calls go through the gateway's `ControlPlane`, the same code the `/workflows`
chat command uses.

## Enable it

```bash
uv add "langclaw[api]"

LANGCLAW__CHANNELS__API__ENABLED=true
LANGCLAW__CHANNELS__API__TOKEN=<long random secret>   # required
LANGCLAW__CHANNELS__API__HOST=::                      # default 127.0.0.1
LANGCLAW__CHANNELS__API__PORT=18790
LANGCLAW__CHANNELS__API__USER_ID=api                  # identity chat turns run as
```

Every endpoint except `GET /healthz` requires `Authorization: Bearer <token>`.
If the channel is enabled with an empty token, the gateway refuses to start and
names the missing variable. The token is redacted from all logs.

!!! warning "Keep it private"
    The token grants full agent access (chat, workflows, schedules). Bind to a
    private network where you can: on Railway, leave the langclaw service
    without a public domain and call it from other services at
    `http://<service>.railway.internal:18790`.

## Chat is turn-based

An agent turn with tool calls can take longer than an HTTP client's timeout,
so chat returns a **turn** you poll:

```bash
curl -X POST "$URL/v1/chat?wait=25" -H "Authorization: Bearer $TOKEN" \
     -d '{"content": "Summarise today'\''s news", "context_id": "ui"}'
# 200 {"turn_id": "…", "status": "done", "messages": [...]}   finished within 25 s
# 202 {"turn_id": "…", "status": "running", ...}               still working

curl "$URL/v1/turns/<turn_id>?wait=25" -H "Authorization: Bearer $TOKEN"
```

- `messages` holds every output of the turn in order: `ai` text,
  `tool_progress` (tool name and args in `metadata`), `tool_result`,
  `command`, or `review` — a workflow run paused for review (`metadata` holds
  `run_id`, `interrupt_id`, `data`, `editable`; answer it with
  `POST /v1/runs/{run_id}/review`).
- `wait` (0–120 s) long-polls; omit it to return immediately.
- `context_id` selects the conversation thread (memory is kept per
  `user_id` + `context_id`). `agent_name` routes to a named agent.
- Content starting with `/` runs a chat command (`/help`, `/workflows runs`…)
  and returns a completed turn at once.
- `GET /v1/turns?context_id=ui` lists recent turns. Turns are kept in memory
  (`max_turns`, default 500), so this list resets on restart. The agent's
  conversation memory lives in the checkpointer and is unaffected.

## Management endpoints

| Method & path | Does |
|---|---|
| `GET /v1/status` | Version, model, channels, agents, enabled features |
| `GET /v1/catalog` | Tool and subagent names workflow steps can use |
| `GET /v1/workflows` | Workflows (`source`: `file` or `code`, `editable`, `valid`); files that failed to load are listed with their `errors` |
| `GET /v1/workflows/{name}` | One workflow with a `mermaid` drawing; file workflows include `graph` (the file) |
| `PUT /v1/workflows/{name}` | Create/replace `workflows/<name>.graph.json`; the body is the file (see the [workflows guide](workflows.md#as-a-file-workflowsnamegraphjson)). An invalid graph is a 400 listing every problem; unknown tools come back as `warnings`. The previous version is kept. |
| `POST /v1/workflows/{name}/validate` | Check a graph without saving: `{"valid", "errors", "warnings"}` |
| `DELETE /v1/workflows/{name}` | Delete a workflow file (its history is kept) |
| `GET /v1/workflows/{name}/versions` | Saved versions, newest first |
| `GET /v1/workflows/{name}/versions/{version}` | One saved version |
| `POST /v1/workflows/{name}/versions/{version}/restore` | Make that version current |
| `POST /v1/workflows/{name}/runs` | Start a run: `{"input"?, "tenant"?}` → `202 {"run_id", "turn_id"}` (`tenant`: run it for that client) |
| `GET /v1/workflows/{name}/runs` | That workflow's runs (`?status=&limit=`) |
| `GET /v1/runs` | Recent runs (`?workflow=&status=&limit=`) |
| `GET /v1/runs/{run_id}` | One run: status, trigger, reviews (with answers), final state, and each step's result |
| `POST /v1/runs/{run_id}/cancel` | Cancel a run executing in this gateway |
| `GET /v1/reviews` | Reviews waiting for an answer (`?workflow=`) |
| `POST /v1/runs/{run_id}/review` | Answer a review: `{"action": "approve" \| "edit" \| "reject", "data"?, "comment"?, "interrupt_id"?, "by"?, "via"?}`. The run continues on the channel that started it. A second answer is a **409** whose `decision` says who answered first, and where. |
| `GET /v1/documents` | Filed documents (read-only). `?q=` searches text — or ranks by meaning with `&semantic=true` when `documents.embedding_model` is set — plus `sender`, `receiver`, `doc_type`, `date_from`, `date_to`, `status`, `limit`, and `field.<name>=<text>` for type-specific details (e.g. `field.jurisdiction=Delaware`). With clients on, `tenant=<id>` is required. Returns `{"documents", "count", "mode": "filter"\|"text"\|"semantic", "semantic"}` (`semantic`: whether meaning search is available); ranked rows carry `similarity` 0–1 |
| `GET /v1/documents/{bucket_key}` | One record + a 1-hour download `link` for its file |
| `GET /v1/tenants` · `GET /v1/tenants/{id}` | Clients (with `LANGCLAW__TENANTS__ENABLED`) |
| `PUT /v1/tenants/{id}` | Create/replace a client: `{"name", "tax_id"?, "chats"?: ["telegram:-100…"], "review_chat"?, "profile"?}`. A chat already linked to another client is a 400. |
| `DELETE /v1/tenants/{id}` | Remove a client (its files and records are kept) |
| `GET /v1/schedules` | Scheduled jobs |
| `POST /v1/schedules` | `{"name", "channel", "user_id", "message" \| "workflow_name", "cron_expr" \| "every_seconds", "chat_id"?, "workflow_input"?}` |
| `DELETE /v1/schedules/{id}` | Remove a scheduled job |

A workflow run started over the API is tracked as a turn: poll
`/v1/turns/{turn_id}` for its progress lines and final output.

Errors are `{"error": "..."}`: **400** invalid input, **401** bad token,
**404** not found, **409** feature disabled (the message names the setting to
turn on) or review already answered, **503** gateway not ready.

Workflows need `LANGCLAW__WORKFLOWS__ENABLED=true`; schedules need
`LANGCLAW__CRON__ENABLED=true`; documents need `LANGCLAW__DOCUMENTS__ENABLED=true`;
clients need `LANGCLAW__TENANTS__ENABLED=true`.

## Building a UI with Appsmith

Appsmith runs API queries **server-side**, so it can reach the private
`railway.internal` address and the token never reaches the browser.

**1. Datasource.** *Data → New datasource → Authenticated API*:

- URL: `http://langclaw.railway.internal:18790` (your service name and port)
- Headers: `Authorization` = `Bearer <your token>`
- Save. Every query below uses this datasource. In each query's *Settings*,
  raise **Query timeout** to `30000` ms so long-polls fit.

**2. Queries.** Create these on the datasource:

| Query | Method | Path | Body |
|---|---|---|---|
| `getStatus` | GET | `/v1/status` | |
| `sendChat` | POST | `/v1/chat?wait=20` | `{"content": {{msgInput.text}}, "context_id": "appsmith"}` |
| `getTurn` | GET | `/v1/turns/{{this.params.turn_id}}?wait=20` | |
| `listTurns` | GET | `/v1/turns?context_id=appsmith` | |
| `listWorkflows` | GET | `/v1/workflows` | |
| `getWorkflow` | GET | `/v1/workflows/{{wfTable.selectedRow.name}}` | |
| `saveWorkflow` | PUT | `/v1/workflows/{{wfName.text}}` | `{{JSON.parse(wfGraph.text)}}` |
| `deleteWorkflow` | DELETE | `/v1/workflows/{{wfTable.selectedRow.name}}` | |
| `runWorkflow` | POST | `/v1/workflows/{{wfTable.selectedRow.name}}/runs` | `{"input": {{JSON.parse(wfInput.text \|\| "{}")}}}` |
| `listSchedules` | GET | `/v1/schedules` | |
| `addSchedule` | POST | `/v1/schedules` | see step 4 |
| `deleteSchedule` | DELETE | `/v1/schedules/{{schedTable.selectedRow.id}}` | |

**3. A JS object to follow turns.** Create `Turns`:

```js
export default {
  // Poll a turn until it finishes (each getTurn long-polls for up to 20 s).
  async follow(turn) {
    let t = turn;
    for (let i = 0; i < 30 && t.status !== "done"; i++) {
      t = await getTurn.run({ turn_id: t.turn_id });
    }
    return t;
  },
  async send() {
    const turn = await this.follow(await sendChat.run());
    resetWidget("msgInput");
    await listTurns.run();
    return turn;
  },
  async runSelected() {
    const started = await runWorkflow.run();
    const turn = await this.follow({ turn_id: started.turn_id, status: "running" });
    showAlert(turn.messages.filter(m => m.type === "ai").map(m => m.content).join("\n") || "done");
    return turn;
  },
};
```

**4. Pages.**

- **Chat:** an Input `msgInput`, a Button (*onClick* `{{Turns.send()}}`), and a
  List bound to `{{listTurns.data.turns}}` showing `currentItem.content` (you)
  and `currentItem.messages.filter(m => m.type === "ai").map(m => m.content).join("\n\n")`
  (agent). Set `listTurns` to run on page load.
- **Workflows:** a Table `wfTable` bound to `{{listWorkflows.data.workflows}}`;
  an Input `wfName`, a multi-line Input `wfGraph` (default
  `{{JSON.stringify(getWorkflow.data.graph, null, 2)}}`, with `getWorkflow` run on
  row selection), and
  a JSON Input `wfInput`; Buttons for Save (`saveWorkflow`, then `listWorkflows`),
  Run (`{{Turns.runSelected()}}`), and Delete.
- **Schedules:** a Table `schedTable` bound to `{{listSchedules.data.schedules}}`
  and a Form whose submit runs `addSchedule` with the body
  `{{ { name: f_name.text, channel: f_channel.selectedOptionValue, user_id: f_user.text, message: f_message.text, workflow_name: f_workflow.selectedOptionValue, cron_expr: f_cron.text } }}`.
  Fill the channel Select from `{{getStatus.data.channels.map(c => ({label: c, value: c}))}}`
  and the workflow Select from `{{listWorkflows.data.workflows.map(w => ({label: w.name, value: w.name}))}}`.
- **Status:** Text/JSON widgets bound to `{{getStatus.data}}`.
