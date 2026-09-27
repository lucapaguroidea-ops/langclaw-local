# Context

Two things here are easy to confuse:

- **`LangclawContext`** — per-request channel metadata (who/where a message came from), available to tools and middleware.
- **`steps()` / `request_review()`** — what a workflow node uses to call tools, the model and subagents, and to pause for human review.

## LangclawContext

::: langclaw.LangclawContext

## Workflow steps

Inside a workflow node, `steps()` reaches the gateway's tools, model and
subagents, and `request_review()` pauses the run for a person. See the
[workflows guide](../guides/workflows.md#reaching-langclaw-from-a-node).

::: langclaw.workflows.graph.steps.WorkflowSteps

::: langclaw.workflows.graph.steps.request_review
