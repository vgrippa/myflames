# myflames agent team (orchestrator-worker)

This directory defines the **worker roster** for myflames. The main Claude Code session acts as the **coordinator**: it plans the work, delegates well-scoped subtasks to the specialized agents below, and synthesizes their results. This mirrors the [multi-agent orchestration pattern](https://platform.claude.com/docs/en/managed-agents/multi-agent) (specialization, parallelization, escalation), adapted to Claude Code subagents.

Each worker runs in its own isolated context with least-privilege tools, and is bound to the matching **skill** in `../skills/` (the skill is the playbook; the agent is the routing target + tool scope).

## Roster

| Agent | Role | Tools | Bound skill(s) |
| --- | --- | --- | --- |
| `mysql-correctness-reviewer` | Escalation reviewer. Verifies MySQL/MariaDB claims against `mysql-server/` + `mariadb-server/` source. Read-only. | Read, Grep, Glob, Bash | `mysql-expert` |
| `test-author` | Writes + runs the mandatory paired tests for any logic change. | Read, Edit, Write, Bash, Grep, Glob | `test-pro` |
| `renderer-builder` | Implements SVG/HTML renderer changes (`output_*.py`, `docs/demos/`). | Read, Edit, Write, Bash, Grep, Glob | `viz-specialist`, `web-design` |
| `teach-lesson-author` | Authors/revises the animated `teach/*` lessons. | Read, Edit, Write, Bash, Grep, Glob | `teaching`, `animation-expert` |

## When to delegate (coordinator guidance)

- **Specialization** — route a subtask to the agent that owns its domain rather than doing it inline. A renderer change goes to `renderer-builder`; a new advisor rule's correctness goes to `mysql-correctness-reviewer`.
- **Parallelization** — independent subtasks fan out at once. Example: after adding an advisor rule, the *correctness review* (reviewer) and the *test* (test-author) are independent — dispatch both in one batch, then synthesize.
- **Escalation** — when about to assert a specific MySQL/MariaDB default, threshold, or symbol, escalate to `mysql-correctness-reviewer` instead of writing it from priors. This is the structural fix for the recurring "fabricated internals" problem.

## The standard pipeline for a logic change

```
1. Coordinator scopes the change (which file, which signal).
2. renderer-builder OR coordinator implements it.
3. In parallel:
   - mysql-correctness-reviewer verifies every internals claim against source.
   - test-author writes + runs the paired test (CLAUDE.md mandate).
4. Coordinator applies the reviewer's corrections, confirms tests are green, ships.
```

## Adding a worker

Keep tools least-privilege (a reviewer is read-only; a builder gets Edit/Write). Give the `description` a third-person `what + when` with trigger keywords and a routing cue ("Use PROACTIVELY before…"). Bind it to a skill rather than duplicating that skill's knowledge in the agent body.
