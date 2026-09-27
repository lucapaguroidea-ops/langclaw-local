# Workflows

A **workflow** is a named, multi-step procedure — a LangGraph `StateGraph` —
that the agent, a user, a cron job, or the API can run. Every run is
checkpointed on the gateway's checkpointer, so a crash resumes from the last
finished step, and a run can **pause for human review** until someone
approves, edits, or rejects.

```bash
LANGCLAW__WORKFLOWS__ENABLED=true   # workflows are off by default
```

Write a workflow in **Python** (full LangGraph: any node code, `Send` fan-out,
loops) or as a **file** (`workflows/<name>.graph.json`, editable from the UI or
by the agent). Both run the same way.

## In Python

```python
from typing_extensions import TypedDict
from langgraph.graph import END, START, StateGraph
from langclaw.workflows import request_review, steps

class Doc(TypedDict, total=False):
    key: str
    sender: str
    approved: bool

async def classify(state: Doc) -> dict:
    text = await steps().tool("bucket_read", key=state["key"])   # langclaw tools
    return {"sender": await steps().llm(f"Who sent this?\n{text}")}  # one model call

def review(state: Doc) -> dict:
    decision = request_review("Is the sender right?", data={"sender": state["sender"]})
    return {"approved": decision["action"] != "reject"}

builder = StateGraph(Doc)
builder.add_node("classify", classify)
builder.add_node("review", review)
builder.add_edge(START, "classify")
builder.add_edge("classify", "review")
builder.add_edge("review", END)

app.workflow("doc_intake", graph=builder, description="Classify and file a document")
```

Pass the **uncompiled** builder — langclaw compiles it with its checkpointer.
The run input is the graph's input state; the output is its final state (or
one key of it, with `output_key=`).

## As a file: `workflows/<name>.graph.json`

Files in the agent's `workflows/` folder load automatically (and reload when
they change), so the UI or the agent can create and edit them:

```json
{
  "description": "Classify a document and file it.",
  "input": {"key": {"type": "string"}},
  "nodes": {
    "fetch":    {"type": "tool", "tool": "bucket_read", "args": {"key": "{{input.key}}"}},
    "classify": {"type": "llm", "prompt": "Who sent this?\n{{fetch}}",
                 "output": {"sender": {"type": "string"}, "confidence": {"type": "number"}}},
    "check":    {"type": "branch",
                 "rules": [{"if": {"path": "classify.confidence", "op": "lt", "value": 0.8},
                            "then": "review"}],
                 "else": "save"},
    "review":   {"type": "human_review", "message": "Sender {{classify.sender}}?",
                 "show": ["classify"], "editable": "classify"},
    "save":     {"type": "tool", "tool": "documents_insert", "args": {"meta": "{{classify}}"}}
  },
  "edges": [
    {"from": "START", "to": "fetch"},
    {"from": "fetch", "to": "classify"},
    {"from": "classify", "to": "check"},
    {"from": "review", "to": "save"}
  ],
  "output": "save"
}
```

| Node type | Does |
|---|---|
| `llm` | One model call. `output` fields ⇒ structured result; `model` overrides the default model. |
| `tool` | Calls a registered tool with templated `args`. |
| `subagent` | Delegates a `prompt` to a registered subagent. |
| `branch` | Goes to the first rule whose `if` holds, else to `else`. Ops: `eq ne lt le gt ge in not_in contains exists not_exists truthy falsy`. |
| `human_review` | Pauses the run. `show` picks what the reviewer sees; `editable` names the result they may correct; `on_reject` (default `END`) is where a rejection goes. |

Each node's result is stored under its id (or `save_as`), and templates such as
`{{input.key}}` or `{{classify.sender}}` read from those results. A node with no
outgoing edge ends the run; `{"from": ["a", "b"], "to": "c"}` waits for both
`a` and `b`. The validator reports every problem at once (unknown nodes, bad
template keys, unreachable nodes, …); an invalid file is skipped with a warning.

## Reviews

When a run pauses, the channel that started it gets a message with the run id.
Answer from any surface — the **first answer wins**, and later answers are told
who answered and where:

```
/workflows reviews                     # everything waiting
/workflows approve <run_id>
/workflows reject <run_id>
/workflows edit <run_id> {"classify": {"sender": "Globex"}}
```

Paused runs and their reviews live in the checkpointer and a run index in the
same database, so they survive restarts. Runs interrupted by a crash continue
from their last checkpoint on startup.

!!! note "One gateway replica"
    The first-answer-wins lock is per process; run a single gateway replica.

## Run a workflow

**Via the agent** — each workflow is a `workflow_<name>` tool; the agent calls it
when a request matches its `description`. If the run pauses for review, the tool
tells the agent (and so the user) how to answer.

**Via chat commands:**
```
/workflows                       # list registered workflows
/workflows run research {"topic": "solid-state batteries"}
/workflows runs                  # recent runs
/workflows status <run_id>       # one run's status
/workflows cancel <run_id>       # cancel a run executing now
/workflows reviews               # runs waiting for review
```

**Via cron** — schedule it and it fires on the schedule with no agent turn in
between. See [Scheduled Jobs](cron.md).

**Via the API** — `POST /v1/workflows/{name}/runs`. See the
[control-plane guide](control-plane-api.md).

## Reaching langclaw from a node

A node is ordinary LangGraph code. `steps()` gives it the gateway's live
capabilities:

| Call | Does |
|---|---|
| `await steps().llm(prompt, schema=Model, system=..., model=...)` | One model call, no tools. With `schema`, a validated Pydantic object; otherwise text. `model` overrides the default model. |
| `await steps().tool(name, **kwargs)` | Call a registered tool. |
| `await steps().subagent(type, prompt)` | Delegate to a registered subagent (its own tools and context window). |
| `request_review(message, data=..., editable=...)` | Pause the run for a person; returns their decision. |

Use LangGraph itself for control flow: conditional edges to route, `Send` to fan
out (each branch is its own checkpointed task), and an edge back to a node to
loop. `app.workflow(..., output_key="report")` returns one state key as the
run's output instead of the whole state.

## Orchestration patterns

All six patterns from Anthropic's [dynamic workflows guide](https://claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code)
are runnable LangGraph examples:

| Pattern | What it does | Example workflow | LangGraph shape |
|---|---|---|---|
| **Classify-and-act** | Route by type, run a specialized branch | `triage` (also as `triage.graph.json`) | conditional edges |
| **Fan-out-and-synthesize** | Parallel subagents, one merged output | `landscape` | `Send` + reducer |
| **Adversarial verification** | Independent skeptics try to refute each claim | `fact_check` | `Send` per (claim, vote) |
| **Generate-and-filter** | Candidates in parallel, scored, top kept | `tagline_studio` | two `Send` fan-outs |
| **Tournament** | Rank by pairwise duels | `prioritize` | loop, one round per checkpoint |
| **Loop-until-done** | Keep going until a real stop condition | `edge_hunt` | self-loop with stop reason |

```bash
# run all six patterns locally
LANGCLAW__WORKFLOWS__ENABLED=true uv run python -m examples.workflow_patterns
uv run langclaw probe '/workflows'
```

See [`examples/workflow_patterns/`](https://github.com/tisu19021997/langclaw/tree/main/examples/workflow_patterns)
and [`examples/workflow_research.py`](https://github.com/tisu19021997/langclaw/tree/main/examples/workflow_research.py)
(fan-out, then pause for approval).

## Progress streaming

As each step finishes, the channel that started the run gets a progress line
(`⚙️ research: search`). File workflows use each node's `label`.

## Durability

Always on — no settings. Runs live on the checkpointer (SQLite in development,
Postgres in production) with a run index in the same database:

- a run interrupted by a crash **continues from its last finished step** on
  startup (finished steps don't re-run);
- a paused review survives restarts and can be answered later;
- if the process dies after a review was answered but before the run continued,
  the answer is applied on startup.

A step that was mid-flight when the process died runs again, so make side
effects idempotent where it matters (e.g. upsert rather than insert).
