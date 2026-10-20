# MyFlames Claude Code

@AGENTS.md

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/). **Never remove this inspiration / credit line** from the README (or anywhere else it appears). Brendan Gregg and Tanel Poder are the origin of this work and must always be acknowledged.

## Claude Code specifics

Invoke skills as /<name>. Subagents live in .claude/agents: mysql-correctness-reviewer, test-author, renderer-builder, teach-lesson-author. Follow the orchestrator-worker pattern defined in AGENTS.md: implement, run mysql-correctness-reviewer and test-author in parallel, then apply corrections.

External workers (Codex, Gemini, Kimi) get AGENTS.md automatically.

Accounts CLI: every change accepts `--json`, `-o/--output PATH` and `-q`
(with `--yes` to suppress the plan description). Refusals and declines return
`account-change-1.0` with `--json`; cancellation at either confirmation prompt
is audited as declined (exit 1). Use `accounts role --help` for role actions.
