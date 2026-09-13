---
name: kanban-worker
description: Pitfalls, examples, and edge cases for Hermes Kanban workers. The lifecycle itself is auto-injected into every worker's system prompt as KANBAN_GUIDANCE (from agent/prompt_builder.py); this skill is what you load when you want deeper detail on specific scenarios.
version: 2.0.0
platforms: [linux, macos, windows]
environments: [kanban]
metadata:
  hermes:
    tags: [kanban, multi-agent, collaboration, workflow, pitfalls]
    related_skills: [kanban-orchestrator]
---

# Kanban Worker — Pitfalls and Examples

## Block reasons that get answered fast

```

The block message is what appears in the dashboard / gateway notifier. The comment is the deeper context a human reads when they open the task.

### Special prefix: `review-required:`

When your work is complete but needs a second pair of eyes before it can be
considered done, block with `reason="review-required: <what to review>"`. This
is an **agent-review handoff**, NOT a request for the operator — the manager
(or a cold-review subagent, Gate 2.5) disposes it on the SAME board. Never
park work on a human.

```python
kanban_block(reason="review-required: PR #121 rate-limiter key choice — cold review the diff")
```

For genuinely operator-only actions (physical hardware, credentials, legal,
spending decisions) that no agent can perform, block with
`reason="operator-action: <exact instruction>"` — these are surfaced to the
operator via the daily digest, not a shadow board.

## Heartbeats worth sending

Good heartbeats name progress: `"epoch 12/50, loss 0.31"`, `"scanned 1.2M/2.4M rows"`, `"uploaded 47/120 videos"`.

