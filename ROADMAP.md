# myflames roadmap

> Where myflames is going and why. For *what changed in each release*, see [CHANGELOG.md](CHANGELOG.md). For the queue of teaching lessons, see [.claude/ALGORITHM_ROADMAP.md](.claude/ALGORITHM_ROADMAP.md).

## Vision

**myflames is the token-efficient, source-grounded interface between a query plan and an AI agent — and a clear set of pictures for a human.** Raw `EXPLAIN ANALYZE FORMAT=JSON` is verbose and noisy; an LLM burns tokens parsing it before it can reason, and SVGs are invisible to agents. myflames parses and analyzes a plan once, then projects it many ways: flame graph / bar chart / treemap / diagram (for humans), a compact source-verified digest and JSON sidecar (for agents), and an exit code (for CI). One analysis, many projections.

It stays a single project: pure-Python stdlib core, with heavy integrations behind optional extras (`myflames[mcp]`, `myflames[tokens]`). The optional local workspace in `apps/workspace/` uses React and TypeScript; its built assets ship in the Python package. The Python engine remains shared by the CLI, UI, and agent tools.

## Shipped

- **Query investigation workflow** — linked operator selection, focus/collapse,
  contextual Teach, local investigation bundles, structural comparisons, and a
  query laboratory with repeated live captures, cancellation, schema/index
  inspection, statement statistics, and optimizer traces. Shared `capture` and
  `explore` CLI commands. The UI uses a simplified mark derived from the project logo, stable logo URLs across rebuilds, and a neutral/blue design.

- **Local browser workspace** — `myflames ui` imports saved plans, displays five chart views and operator details, compares plans, and exports HTML/JSON. The Teach section opens the existing interactive lessons with search, family filters, curriculum navigation, and downloads. React/TypeScript assets ship with the standard-library Python server.

- **Five views** — flame graph, bar chart, treemap, execution tree, and unified Visual Explain. Diagram and Workbench share one renderer with larger Venn join symbols, operation colors, row-volume arrows, and smooth mouse/trackpad navigation. Existing `diagram` commands remain compatible.
- **Source-verified advisor** — rules and `optimizer_switch` explanations checked line-by-line against MySQL/MariaDB source.
- **JSON sidecar + JSON-LD** — every report has a machine-readable sibling; agents never OCR an SVG.
- **Token-cheap digest + savings report** — `myflames digest` (with `--cost`, `--tokenizer {heuristic,claude,gpt}`, `--show-prompts`); an "Agent-ready" panel in every HTML report.
- **Agent/CI subcommands** — `diff` (plan comparison), `check` (CI gate with a `0/1/2` exit contract), `advise` (ranked warnings + suggestions with confidence).
- **MCP server** — `myflames-mcp` (`pip install myflames[mcp]`) exposing `analyze_plan`, `digest_plan`, `compare_plans`, `explain_optimizer_switch`, `explain_query`.
- **Published to PyPI** — `pip install myflames` (and `myflames[mcp]` / `myflames[tokens]` / `myflames[gpt]`) resolves; a GitHub Release auto-publishes via a Trusted Publisher workflow.
- **Honest agent default (the token thesis, kept)** — the MCP `analyze_plan` tool returns the token-cheap decision-grade sidecar by default (`detail="core"`: summary, warnings, suggestions, switches, plan tree), dropping the heavy `collected`/`query`/`teach_hooks` blocks; `detail="full"` restores the complete record. The `detail` gate lives on `build_sidecar` (single source of truth); the CLI `--sidecar` path and the `digest` path stay `full`. Removes the one place the agent surface contradicted the vision, prerequisite to broadcasting it.
- **One ranker, every projection** — the "fix first" decision is computed once, by `findings.build_findings` / `findings.primary_suggestion_index`. The HTML "Fix first" card and the sidecar `primary_action` both dereference it, so the human card and the ranked `advise` list can no longer disagree (medium correctly outranks low everywhere). A genuinely clean plan shows an explicit "No blocking issues found" card instead of a blank.
- **Advisor names the index that removes a filesort** — for a single-base-table `ORDER BY` on plain columns, myflames suggests `CREATE INDEX … (col1, col2 [DESC])` (direction-preserving) so MySQL reads rows already sorted, instead of only "grow the sort buffer." The parser threads the sort columns through (MySQL `sort_fields`, MariaDB `sort_key`); the rule bails to the generic hint on joins/aggregates/expression sort keys. Verified against MySQL/MariaDB source.
- **Uniform `--quiet/-q`** — the flag now behaves identically on `check`, `digest`, `advise`, and `compare` (was `check`-only): suppress stderr diagnostics, leave stdout data and the exit code untouched.
- **Attribution + credit on the compare report** — the before/after report now carries the "Made with myflames" link and the Brendan Gregg / Tanel Poder inspiration credit, matching the main HTML report.
- **`semijoin_firstmatch` teach lesson** — the flagship of the semijoin cluster (the `IN (subquery)` early-out).

## Next

- **Workspace follow-ups** — broader SQL editing conveniences, richer optimizer-trace summaries, and optional desktop packaging. Query capture, saved investigations, structural comparison, and contextual Teach are implemented.
  Import-dialog layout coverage includes open/error states and short windows;
  continue expanding interaction coverage as the workspace grows.
  Visual Explain zoom includes a live percentage, press feedback, and visible limits.
  The workspace audit covers every primary screen, all Teach lesson players,
  live Sakila capture, export/import, search states, and responsive breakpoints;
  see `docs/UI_TESTING.md` for remaining platform checks.

Distribution multiplies whatever is inside, and the inside is now honest (decision-grade agent default) and consistent (one ranker). So the remaining work is reach and depth.

1. **Wider distribution** — the "made with myflames" footer now ships on the reports (main + compare); what remains is external reach: MCP registries and awesome-lists, so agents and humans discover the tool.
2. **Browser playground** — linked from the README and installs the published wheel via micropip; still needs an end-to-end pass in a real browser and confirmation the GitHub Pages deploy serves [docs/playground/](docs/playground/).
3. **CLI consistency (remainder)** — `--quiet/-q` is now uniform; the open work is auditing the rest of the flag vocabulary and exit-code / stdout-vs-stderr discipline across every subcommand. (owned by `cli-ux`)
4. **Finish the semijoin cluster** — `semijoin_firstmatch` shipped as the flagship; `loosescan`, `materialization`, and a real `duplicate_weedout` (today a stub) remain, each taught as a contrast against the FirstMatch early-out. See [.claude/ALGORITHM_ROADMAP.md](.claude/ALGORITHM_ROADMAP.md). (owned by `teaching`)

## Deferred / non-goals

- **No LLM bundled into the package.** myflames is the deterministic, source-grounded tool an agent *calls* — it does not become the agent. (Exact token counting uses Anthropic's `count_tokens`, or tiktoken for GPT, only as optional extras.)
- **No splitting into multiple packages/repos.** Optional extras are the dependency boundary.

## September 2026 review

The [project review](docs/reviews/2026-09-15.md) adds current-MySQL corpus checks
and fixes connection escaping, advisor evidence, renderer output, comparison
matching, and teaching controls. Follow-up work remains for broader SQL metadata recognition and
complexity models for ordered/multidimensional aggregates.
