# MyFlames Claude Code

@AGENTS.md

## Claude Code specifics

Invoke skills as /<name>. Subagents live in .claude/agents: mysql-correctness-reviewer, security-reviewer, test-author, renderer-builder, teach-lesson-author, plus the relay workers codex-worker, gemini-worker and kimi-worker (the delegate skill). Follow the orchestrator-worker pattern defined in AGENTS.md: implement, run mysql-correctness-reviewer and test-author in parallel, then apply corrections.

External workers (Codex, Gemini, Kimi) get AGENTS.md automatically.
