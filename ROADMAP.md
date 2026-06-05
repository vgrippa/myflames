# myflames roadmap

> Where myflames is going and why. For *what changed in each release*, see [CHANGELOG.md](CHANGELOG.md). For the queue of teaching lessons, see [.claude/ALGORITHM_ROADMAP.md](.claude/ALGORITHM_ROADMAP.md).

## Vision

**myflames is the token-efficient, source-grounded interface between a query plan and an AI agent — and a clear set of pictures for a human.** Raw `EXPLAIN ANALYZE FORMAT=JSON` is verbose and noisy; an LLM burns tokens parsing it before it can reason, and SVGs are invisible to agents. myflames parses and analyzes a plan once, then projects it many ways: flame graph / bar chart / treemap / diagram (for humans), a compact source-verified digest and JSON sidecar (for agents), and an exit code (for CI). One analysis, many projections.

It stays a single project: pure-Python stdlib core, with heavy integrations behind optional extras (`myflames[mcp]`, `myflames[tokens]`). The only separate codebase is the `releem_flames` React UI.

## Shipped

- **Five views** — flame graph, bar chart, treemap, Visual-Explain diagram, tree — each with vetted Big O complexity chips.
- **Source-verified advisor** — rules and `optimizer_switch` explanations checked line-by-line against MySQL/MariaDB source.
- **JSON sidecar + JSON-LD** — every report has a machine-readable sibling; agents never OCR an SVG.
- **Token-cheap digest + savings report** — `myflames tokens` (with `--digest`, `--exact`, `--show`); an "Agent-ready" panel in every HTML report.
- **Agent/CI subcommands** — `diff` (plan comparison), `check` (CI gate with a `0/1/2` exit contract), `findings` (ranked warnings + suggestions with confidence).
- **MCP server** — `myflames-mcp` (`pip install myflames[mcp]`) exposing `analyze_plan`, `digest_plan`, `compare_plans`, `explain_optimizer_switch`, `explain_query`.

## Next

- **Publish to PyPI** so `pip install myflames` and the browser playground's wheel resolve. (Currently install is from source / pipx-from-checkout.)
- **Browser playground** — finish [docs/playground/](docs/playground/) once the wheel is published (client-side Pyodide, zero install).
- **CLI consistency** — uniform flag vocabulary, exit codes, and stdout/stderr discipline across all subcommands (owned by the `cli-ux` skill).
- **More teaching lessons** — see [.claude/ALGORITHM_ROADMAP.md](.claude/ALGORITHM_ROADMAP.md) for the queued operator families.
- **Wider distribution** — MCP registries, awesome-lists, and a "made with myflames" footer on shared reports, so the tool is discoverable by humans and agents alike.

## Deferred / non-goals

- **No LLM bundled into the package.** myflames is the deterministic, source-grounded tool an agent *calls* — it does not become the agent. (Exact token counting uses Anthropic's `count_tokens` only as an optional extra.)
- **No sixth visualization view.** The renderers are feature-complete; effort compounds in the corpus (advisor correctness) and the agent surface, not in more charts.
- **No splitting into multiple packages/repos.** Optional extras are the dependency boundary.
