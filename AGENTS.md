# MyFlames Project Rules

This is the canonical project rules file for every coding agent. `CLAUDE.md` and `GEMINI.md` are loading adapters. Read [docs/DEVELOPER_REFERENCE.md](docs/DEVELOPER_REFERENCE.md) before changing a feature; it holds the detailed feature contracts.

## Project Overview

**myflames** is a MySQL and MariaDB performance workspace. Its core visualizes `EXPLAIN ANALYZE FORMAT=JSON` output as interactive flame graphs, bar charts, execution trees, and Visual Explain–style diagrams; around that it finds expensive statements (top and running queries), shows locks and deadlocks, and compares plans. New performance views go in the workspace's Server place (see ROADMAP.md "Performance workspace"). It requires MySQL 8.4+ or MariaDB 10.11+/11.4+ and Python 3.7+ with no external dependencies.

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/). **Never remove this inspiration / credit line** from the README (or anywhere else it appears). Brendan Gregg and Tanel Poder are the origin of this work and must always be acknowledged.

## Skills

Before working in an area, read the matching SKILL.md; if your tool loads skills automatically it will, otherwise open the file. Skills are kept local to maintainer machines (gitignored): `.claude/skills/` is the real folder (Claude Code and Kimi read it) and `.agents/skills` is a symlink to it (Codex and Antigravity read that). If a skill file is missing in your checkout, follow this file and docs/DEVELOPER_REFERENCE.md.

| skill | path | read it before… |
| --- | --- | --- |
| animation-expert | `.claude/skills/animation-expert/SKILL.md` | Authoring or changing animations, especially animated teaching lessons. |
| brutal-review (`/brutal-review` in Claude Code) | `.claude/skills/brutal-review/SKILL.md` | Reviewing the most recent change (git `HEAD`) from multiple perspectives. Gather context, run independent reviews of logic/Python 3.7 compatibility, reliability/testing, clean-campground/invariants, and MySQL correctness/output safety, then synthesize prioritized findings. Enforce stdlib-only, single parser, SVG height/viewBox lockstep, SVG/HTML escaping, inspiration credit, and mandatory docs. Route MySQL claims to `mysql-correctness-reviewer`. |
| cli-ux (`/cli-ux` in Claude Code) | `.claude/skills/cli-ux/SKILL.md` | Adding or changing a subcommand or flag in `cli.py`: subcommand versus flag design, consistent flag names, help text, the `0/1/2` exit-code contract, and stdout versus stderr. This owns the CLI surface; progressive-ux owns rendered output UX. |
| mysql-expert (`/mysql-expert` in Claude Code) | `.claude/skills/mysql-expert/SKILL.md` | Adding or modifying `advisor.py` rules, `OPTIMIZER_SWITCH_EXPLANATIONS`, or any plain-English MySQL/MariaDB plan claim. This skill owns database correctness and optimizer_switch explanations. |
| progressive-ux (`/progressive-ux` in Claude Code) | `.claude/skills/progressive-ux/SKILL.md` | Changing HTML wrappers, onboarding copy, or glossary entries. Serve newcomers and senior DBAs through glossary chips, primary-action strips, and collapsible details. |
| removing-ai-isms | `.claude/skills/removing-ai-isms/SKILL.md` | Editing prose to remove formulaic AI phrasing while preserving technical facts. |
| skill-creator | `.claude/skills/skill-creator/SKILL.md` | Creating or revising reusable skills. |
| steve-jobs-persona | `.claude/skills/steve-jobs-persona/SKILL.md` | Reviewing product focus, simplicity, or presentation. |
| structured-output (`/structured-output` in Claude Code) | `.claude/skills/structured-output/SKILL.md` | Adding output data or changing serialization for external tools or AI agents. This owns the JSON sidecar schema, token-cheap digest, and what gets serialized where. |
| teaching | `.claude/skills/teaching/SKILL.md` | Authoring or revising `teach/*` lessons and their pedagogical structure. |
| test-pro (`/test-pro` in Claude Code) | `.claude/skills/test-pro/SKILL.md` | Writing, running, or fixing Python tests and verifying MySQL explain parsing and coverage. |
| ui-architecture (`/ui-architecture` in Claude Code) | `.claude/skills/ui-architecture/SKILL.md` | Adding screens, navigation items, or actions, or renaming anything user-visible. This owns the object model, canonical names (`references/vocabulary.md`), navigation, and action placement. |
| ui-review (`/ui-review` in Claude Code) | `.claude/skills/ui-review/SKILL.md` | Workspace UI changes, both before and after. Perform screenshot-based visual QA with `scripts/screenshot.mjs` and an isolated server, never the user's `~/.myflames`. |
| viz-specialist (`/viz-specialist` in Claude Code) | `.claude/skills/viz-specialist/SKILL.md` | Changing what a chart width, color, arrow, label, tooltip or time text means, chart data fields, zoom/search/selection behavior, Python vs TS parity, or the clean-room flame graph files. Page layout belongs to web-design. |
| web-design (`/web-design` in Claude Code) | `.claude/skills/web-design/SKILL.md` | Any CSS or component styling. Follow the workspace design system for tokens, components, icons, density, and accessibility. |
| web-dev (`/web-dev` in Claude Code) | `.claude/skills/web-dev/SKILL.md` | Building or changing the workspace (`apps/workspace`, React/TypeScript/Vite, plain CSS), its chart runtime, or the api.ts / ui.py contract. Also covers the separate releem_flames app. |
| db-operations | `.claude/skills/db-operations/SKILL.md` | Adding or changing any code path that writes to a server: KILL, DDL or DML from the editor, `SET GLOBAL/PERSIST/PERSIST_ONLY/SESSION`, `RESET PERSIST`, replication skips, CLONE, restore. Owns confirmation, privilege preflight, target re-read, engine gates, the audit trail and read-only mode. |
| replication | `.claude/skills/replication/SKILL.md` | Building replication views, lag metrics, replica health checks, or any replication action (skip one transaction, the opt-in skip-until-stopped loop). MySQL channels and GTID modes vs MariaDB connection names and domain ids. |
| server-lifecycle | `.claude/skills/server-lifecycle/SKILL.md` | Adding upgrade checks, dump/restore, CLONE, or any code that runs an external MySQL/MariaDB tool (mysqlsh, mysqldump, mariadb-dump, mariadb-backup, mariadb-upgrade). Tool detection, secrets kept off command lines, progress, cancel, verification. |
| server-analysis | `.claude/skills/server-analysis/SKILL.md` | Building Refresh all for the Server place, the Full analysis report (MySQLTuner-style checks implemented natively; MySQLTuner is GPL, never copy its code or text), its export, or links from a server metric to the statements behind it. |
| query-editor | `.claude/skills/query-editor/SKILL.md` | Adding SQL execution, result grids, multiple result sets, transactions, Stop, history, or export to the Query editor, grounded in what `connector.py` can do. |
| full-review (`/full-review` in Claude Code) | `.claude/skills/full-review/SKILL.md` | Running a full project review or release-readiness pass: the GA MySQL/MariaDB Docker matrix (`scripts/ga-matrix.sh`), partitioned reviews by Codex, Gemini and Kimi, live testing per engine, APFS lanes for parallel fixes, the integration gate, and commit/push. |
| accounts | `.claude/skills/accounts/SKILL.md` | Greying out an action the connected account cannot run, adding a privilege check, or building users, roles, GRANT/REVOKE or password features. Owns the SHOW GRANTS model, the has-privilege rule (unknown leaves actions enabled), the action-to-privilege map, and the Users and roles space; writes follow db-operations. |

See `.claude/skills/README.md` for the full ownership map (one owner per decision; who defers to whom).

## Worker roles

Any coding tool can play these roles. The main session is the coordinator and delegates well-scoped work with least-privilege access; each worker follows its matching skill. See `.claude/agents/README.md`.

Claude Code: these are subagents in .claude/agents (the `.claude/agents/` directory).

| Role | Worker | Responsibility |
| --- | --- | --- |
| Reviewer of MySQL claims | `mysql-correctness-reviewer` | Read-only escalation reviewer. Verify every MySQL/MariaDB claim against `mysql-server/` and `mariadb-server/` source before shipping advisor, explanation, or complexity changes. Follows mysql-expert. |
| Test author | `test-author` | Write and run the mandatory paired tests. Follows test-pro. |
| Renderer builder | `renderer-builder` | Implement `output_*.py` and demo changes. Follows viz-specialist and web-design. |
| Lesson author | `teach-lesson-author` | Author `teach/*` lessons. Follows teaching and animation-expert. |

For a logic change, implement, run the MySQL correctness review and test author work in parallel, apply corrections, confirm tests pass, then ship.

## Development Workflow & Testing

- **Mandatory Tests**: Every new feature or logic change in the Python package MUST include a corresponding test case.
- **Verification**: Use the test-pro skill (`/test-pro` in Claude Code) to ensure coverage and verify that MySQL explain data is parsed correctly before completing a task.
- **Full suite**: `./run-tests.sh` (it runs with a throwaway `HOME` and `MYFLAMES_SECRET_BACKEND=vault`; never run tests against the real `~/.myflames`).
- **Direct**: `python3 -m unittest discover -s test -p "test_myflames.py" -v`.

## Visualization & UI Standards

- **Consistency**: All HTML demos and SVGs must share a unified design language (modern system fonts like Inter/Roboto, consistent color palettes for 'Join' vs 'Scan').
- **Modernization**: For any file in `docs/demos/`, ensure high-contrast colors and responsive layouts.
- **Review**: Proactively use the viz-specialist skill (`/viz-specialist` in Claude Code) when modifying renderers (`output_*.py`) to audit visual hierarchy and accessibility.

## Architecture (Python package: `myflames/`)

1. **`parser.py`**: Single entry point. Builds a unified tree. Auto-detects MySQL vs MariaDB format. MariaDB normalization (`_normalize_mariadb*` functions) converts MariaDB's `query_block/nested_loop/table` structure into MySQL's `operation/inputs` tree before `parse_node` processes it. `analyze_plan(root)` scans for full_scans, hash_joins, etc.
2. **`cli.py`**: Argument parsing and SVG height patching for the analysis panel.
3. **Renderers**: `flamegraph.py`, `output_bargraph.py`, `output_tree.py`, `output_diagram.py`, `output_workbench.py` write standalone `.svg` output. (The treemap view was removed; `--type treemap` is a deprecated alias that warns and renders the bar chart.)
4. **3D lesson views** (`apps/teach-3d`): an extra 3D option beside a 2D teach lesson (today the B+tree), built with three.js into one offline page per lesson (`myflames/assets/teach3d-btree.html`, committed, with a sources hash checked by `test/test_teach.py`). Its numbers mirror `myflames/teach/_cost_model.py` and are parity-tested against a fixture that Python writes. Never replace or change the 2D lesson when working on the 3D view; the 2D lessons stay SVG only.
5. **Chart runtime**: HTML reports, `explore` pages, and the workspace draw every chart with the TypeScript runtime in `apps/workspace/src/charts/` (built by `npm run build` into the committed `myflames/assets/myflames-charts.js`, inlined by `chart_assets.py`). Python stays the source of every semantic decision: `chart_data.py` emits `chart-data-1.0` for reports and `explore` pages and `chart-data-2.0` for the workspace (`docs/schemas/chart-data-*.json`; 2.0 stores each analysis message once, and the bundle reads both) using the same helpers as the Python renderers, and the TS only lays out and handles interaction. Keep both renderers in step: `apps/workspace/test/parity.mjs` compares them on every fixture, `test_chart_data.py` fails if the committed bundle is older than its sources or over 80 KB. Charts are instance-scoped (no page-wide IDs), set plan text with `textContent` only, and capture Ctrl+F only while focus is inside the chart.

## Key Constraints & Coding Standards

- **No External Dependencies**: Stdlib only. Python 3.7+ compatible (no walrus operator, no `match` statements).
- **Single Parser**: Never parse JSON in output modules; always use `parser.parse_explain`.
- **SVG Rules**: Always update both `height` AND `viewBox` together. Use `_wrap()` for info panel text.
- **Preserve Order**: Do not sort `inputs[]` in the JSON; order is semantically significant (outer vs inner table).
- **Preserve Inspiration Credit**: Never remove the "Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs" line from the README or anywhere else. This attribution is load-bearing for the project and must always remain. In the workspace app it lives in the **About myflames** window (Settings menu), like a desktop app's About, rather than a footer on every screen; README (near the top), NOTICE, this file, CLAUDE.md and HTML report footers keep it.
- **License: Apache-2.0** (`LICENSE`, `NOTICE`). myflames began as a fork of FlameGraph (CDDL 1.0); the flame graph renderers (`myflames/flamegraph.py`, `apps/workspace/src/charts/flamegraph.ts`) are clean-room rewrites from a functional spec. Never copy or consult code from `flamegraph.pl`, the FlameGraph repo, or this repository's pre-2.4 history of those files when changing them. Releases up to 2.3.0 stay CDDL 1.0. The inspiration credit is separate from the license and must stay.
- **Commit & release-note conventions**: Do NOT add `Co-Authored-By` or any AI-attribution trailer to commit messages. Keep commit messages, CHANGELOG entries, and GitHub release notes professional: no emojis, no "generated with" footers, and no meta-commentary that an AI/LLM authored or verified the work. Legitimate product references (the tool targets LLMs/agents; `--tokenizer claude` uses Anthropic's `count_tokens`) are fine.
- **Documentation is mandatory, not optional**: Any change to a user-facing parameter, subcommand, flag, or core feature (adding, renaming, changing behavior, or removing) MUST update the docs in the same change: `README.md` (the relevant section + the Agent/CI subcommands block + any CLI reference), `AGENTS.md` and `docs/DEVELOPER_REFERENCE.md`, the `CHANGELOG.md` `[Unreleased]` section, and any affected file under `docs/` (walkthroughs, examples) and `ROADMAP.md`. A PR/commit that ships a CLI surface change without the matching doc edits is incomplete. When renaming, keep a deprecated alias and document it.

## Workspace operation contracts

- The current macOS package is unsigned Apple Silicon, with a PyInstaller backend and bundled LICENSE/NOTICE. Explicitly disable Developer ID signing and notarization for local builds. Verify the delivered app with minimal PATH and an unavailable system Python; normal Finder launches must not require a terminal. Keep standard client-discovery paths fixed and preserve explicit PATH precedence.

- Desktop development currently targets macOS in `apps/desktop`: Electron hosts the existing workspace and owns its loopback Python child. Keep the renderer sandboxed and isolated, with no Node integration or broad IPC bridge. Quitting stops the backend's dump, restore and clone tools (their own process groups): the backend stops them first and audits them as cancelled, outcome unknown; the groups it lists in its private `tool-groups-1.0` file are killed by Electron when the backend must be killed, and quit resolves only when all are gone, within a fixed bound. Development and tests use separate app data, never the real CLI workspace. `docs/design/desktop-workspace.html` is synthetic design material, not a live feature. See `docs/DESKTOP.md` for scope and distribution work still required.

- The shared workspace uses compact navigation, a command palette and full-width Server details. Back preserves the mounted list and its filters, selection and scroll. SQL completion reads bounded catalog metadata, scopes caches to connection identity, cancels stale reads and preserves native input, undo and execution shortcuts.
- Table data edits are separate from arbitrary SQL results. Keep snapshots and baselines server-owned and bounded; accept typed staged values, never client SQL or predicates. Exact review is required for Development too. Apply only supported InnoDB tables with a verified reliable key and trigger absence; refuse foreign keys, CHECK constraints, expression defaults and unsupported automatic columns. Never evaluate views or generated columns in the viewer. Hold an explicit table READ lock for capture; review the blocking table WRITE lock for apply, compare baselines and metadata before writes, re-read policy, audit, and never automatically retry an unknown outcome.
- CLI server writes share `dbops.cli_confirm`: exact statements and target on stderr, default No, `--yes` for every non-terminal write, exact typed guards, and audited EOF/Ctrl+C declines. Before any statement is sent, expected refusals and declines exit 2 with empty stdout, including `--json`, and preserve output files. Executed results retain their existing envelopes.
- Browser access starts from the fresh one-use launch URL. The HTML shell contains no API token. The per-instance HttpOnly cookie and origin-scoped `myflames.session-proof` must both validate; restart requires a new launch. Follow the [authentication contract](docs/DEVELOPER_REFERENCE.md#local-authentication-and-request-limits).
- Read clients and editor sessions use the same isolated client environment as writes, with exclusive defaults and no personal login files. Saved-password lookup accepts validated connection fields only. Ended editor sessions retain endpoint/profile ownership. Persisted drafts and history redact recognized SQL credentials.
- Privilege availability comes from SHOW PRIVILEGES; legal levels come from myflames' source-verified tables. The Users and roles matrix includes available unticked privileges and Add scope, with edits flowing through one batch review. Unknown actor authority stays explicit. Routine kinds follow the engine and version: MariaDB packages/package bodies, MySQL 9.2+ MLE libraries. Package grant reviews include a temporary Oracle SQL mode and exact session-mode restoration; proxy scopes permit anonymous users.
- Tool update checks contact fixed official HTTPS sources only when explicitly requested by the workspace; CLI discovery and preflight remain offline. Bound network reads, workers and total wait, and report unknown when metadata cannot be verified. Native folder selection accepts no command or path input and does not start a backup. Dump options use the tool-specific allowlist and remain bound to the reviewed plan.
- Analysis projections (`analysis_query.prepare`) are conservative, reviewed SELECTs from supported DML shapes. Preserve original editor SQL in its tab, show the projection and limitations, and require a second explicit capture action. Never describe a projection as the original write plan; UPDATE assignment expressions are omitted. Batch Top analysis skips converted rows for individual review.
- Server > Error log (`error_log.py`, `error-log-1.0`) is read-only and reads nothing until Load or Refresh all. MySQL reads `performance_schema.error_log` (a 5 MB ring buffer); when `@@log_error` names a file the server reloads its tail at startup, so earlier rows are marked previous run by epoch seconds (never naive local time). Settings come from SHOW GLOBAL VARIABLES, status counters from an explicit name list. MariaDB has no SQL access to its error log: never use LOAD_FILE. Read the `@@log_error` file only for a loopback connection whose `@@hostname` equals this computer's host name, from at most the last 1 MiB, and only when the latest startup banner in those bytes names `@@version` (or `'built' as '@@version'`) and `@@port` and was logged within an hour after the server's start; return parsed entries only (at most 100 continuation lines each), otherwise not available with a `how_to_view` hint. `log_error_verbosity` can hide warnings and notes, so zero counts there mean not recorded.
- InnoDB DATA_FREE is reusable allocation, not a capacity alarm. Known file-per-table free space is informational at every size; warnings require measured capacity evidence. Optional Optimize and Instruments actions require exact SQL review and confirmation even on Development connections. Instruments selection builds `UPDATE performance_schema.setup_instruments` (at most 500 names per statement; `ENABLED` and `TIMED` for timed instruments, `ENABLED` only for untimed `memory` and `error`; built-in `memory/performance_schema` skipped by Disable; names with a backslash refused) and `UPDATE performance_schema.setup_consumers` (including required parent consumers) only from names in the loaded report.
- Server > Parameters (`Config.tsx`, `/api/live/config` with `parameters-1.0` from `parameters.py` and the generated `mysql_sysvars.py` catalog) and Instruments read on open (one attempt per connection; Refresh retries). Parameters inline Edit offers only changeable rows (MySQL dynamic and persistable static via `SET PERSIST_ONLY`; MariaDB dynamic only). Review and apply plans up to 25 changes (`SET PERSIST` or `SET GLOBAL` on MySQL dynamic, `SET PERSIST_ONLY` on MySQL static, `SET GLOBAL` plus copy-only option file line on MariaDB), confirms and applies them one at a time after re-reading each variable, stops at the first unconfirmed change (a request without a reply is `unknown`, never unchanged), and records a declined audit entry for every planned row on Cancel or Escape. Values the server sets itself (`modifiable: none`) are never editable. Single-parameter Session changes open planned SQL in the Query editor, which owns execution. Database diagram belongs under Server > Schema with a Diagram | Mermaid view (`mermaid` 11.17.2 in separate chunks, `securityLevel: "strict"`); imports never execute SQL and editable JSON preserves layout. See [Database diagram](docs/LOCAL_UI.md#database-diagram).
- A view reads when it is opened on screen (once per connection; Refresh retries). Tables and Indexes keep their schema-size consent, Error log waits for Load, and nothing reads off screen: Server and Replication mount on their first visit, and every view under a hidden place or kept mounted while hidden gets `useViewOnScreen()` false (`viewOnScreen.ts`); automatic first reads wait for true.
- The write safety of the connection in use is the first item of the window footer (`SafetyStatus` in `Connections.tsx`, `WindowStatusBar lead` in `kit.tsx`) on the places that send to it (Query editor, Server, Workload, Replication, Tuning): a dot plus words, the full sentence as its accessible name, Retry when the saved connections could not be read. Nothing is drawn under the top bar; Home, Plans, Investigations, Learn and the Audit log show no safety state.
- Tuning (`Tuning.tsx`, `tuningRules.ts`) is a place of its own (sidebar label "Tuning", palette keyword "mysql tuning"), rendered by `Server` with `place="tuning"`: full width, no Server list of views or toolbar, the window footer. It is never a Server view (Server never shows it; a route to it, or to the Full analysis, its second tab, navigates to the place). Its Scorecard tab gives a 0–100% score and letter grade (A–F). It scores the statuses Health, Parameters, Tables and the Full analysis already decide, read by their real keys, never thresholds of its own, in 5 weighted categories (Buffer pool and redo 30%, Queries 25%, Connections and threads 20%, Tables and caches 15%, Security 10%): OK 100, watch 50, problem 0. Unknown (not loaded, not collected, or Python's info) never counts and is shown with its reason; a check that does not apply is not counted. The score is provisional with no grade until half the checks and one per category are measured; a problem caps the grade at C. The grade always shows what it means: a reason line beside it (`gradeReason`) and an ⓘ legend rendered from `GRADE_SCALE` and `PROBLEM_GRADE_CAP`, the single source of the thresholds in `tuningRules.ts`. Only reports bound to the connection in use are scored. Remediation is copy or open in the Query editor only: `SET PERSIST` on MySQL, `SET GLOBAL` plus an option-file line on MariaDB, `SET SESSION` for per-session limits, version-gated (MariaDB innodb_log_file_size is read-only before 10.9; MySQL innodb_redo_log_capacity from 8.0.30). DATA_FREE is not scored.
- Learn embeds lesson pages with `data-embedded="true"` (hiding the standalone header and footer) and shares workspace light and dark tokens (`prefers-color-scheme`, `data-theme`, `?theme=`, `?embedded=1`) plus semantic `--svg-*` drawing colors resolved by `anim.cssVar` and `anim.palette`.
- Editor runs re-read saved policy after credential lookup and immediately before start_run; drift refuses. DDL runs require a separate confirmation per DDL (multi-DDL scripts refuse), DROP DATABASE/SCHEMA also requires the exact typed name on both connection types. ALTER runs alone: review table size and Locks evidence, then show and send SET SESSION lock_wait_timeout = 5 before ALTER in the same session. Optional ALGORITHM/LOCK SQL is never substituted silently.
- Tests use temporary HOME and USERPROFILE with the vault backend. The workspace ships only as the macOS desktop app (`apps/desktop`); there is no browser version. UI verification includes light and dark appearances at 1440 pixels and at the app's minimum window width of 720 pixels, especially charts, Compare and Audit; a release gate builds the packaged app and smoke-tests it. Never infer a passed live gate from a fixture test. Phone charts preserve readable text within a scrollable region; test chart scale and page overflow.


## Review corrections (2026-09-15)

- Treat parsed operator times as milliseconds. `render_bargraph` converts display
  units internally; callers must not scale the shared tree.
- Comparison pairs repeated labels by traversal occurrence. Preserve all
  occurrences; this is not structural matching across reordered plans.
- Render/compare input and output failures return 2 with a stderr diagnostic.
- MySQL TempTable is limited per table by `tmp_table_size`; MEMORY also uses
  `max_heap_table_size`. Collect `internal_tmp_mem_storage_engine` before making
  engine-specific recommendations. Configuration size alone does not prove a spill.
- Scalar/streaming aggregation is linear in input rows; sorting is a separate
  operator, and DISTINCT or ordered aggregates require separate accounting.
- Test current MySQL versions with the configurable fixture generator and
  `MYFLAMES_MYSQL_FIXTURES`; see `test/README.md`. Keep fresh version corpora
  separate from the committed fixture directory unless intentionally updating it.

## Commands

Invoke subcommands with `python3 -m myflames <command>`. For flags, schemas, implementation paths, safety contracts, and feature-specific tests, read [docs/DEVELOPER_REFERENCE.md](docs/DEVELOPER_REFERENCE.md). The default command renders a plan with `--type`.

| Subcommand | Purpose |
| --- | --- |
| `ui` | Deprecated: Open the local Performance workspace in a browser (use the desktop app binary). |
| `sql` | Run SQL from `-e`, `-f` or stdin in one Query editor session; shared write policy, confirmation, audit, row limits and exports (table, CSV, JSON, TSV, Markdown, INSERT via `--format insert --insert-table NAME`, default `result`; `-o/--output PATH`; DROP DATABASE/SCHEMA requires an exact typed name, `--confirm-target NAME` with `--yes`; one DDL per confirmation). |
| `capture` | Capture a live estimated or analyzed plan and server evidence as JSON; `--statement-digest` captures from a digest (`--digest` deprecated). |
| `explore` | Export interactive HTML with branch focus and operator metrics. |
| `top` | List expensive statements from Performance Schema digests. |
| `sessions` | Inspect connections, idle transactions, and lock waits; optionally explain a connection; `--kill ID [--query] [--yes]` is an alias of `locks --kill`, with the same confirmation, identity checks, write policy and audit. |
| `running` | Deprecated alias of sessions, retaining the running-1.0 contract. |
| `locks` | Inspect blockers, waiting DDL, transactions, row/metadata locks, and deadlocks; explicitly confirm a single kill (follows the connection's write policy and is audited). |
| `innodb` | Parse SHOW ENGINE INNODB STATUS into sections and health findings. |
| `error-log` | Read the server error log: `performance_schema.error_log` on MySQL, or on MariaDB the log file when the server is on this computer and its identity is verified (`--level`, `--limit`, `--fail-on`). |
| `health` | Sample global status twice for diagnostics, rates, and health statuses. |
| `waits` | Inspect wait events over a sampling window and since server start. |
| `indexes` | Find unused/redundant indexes and tables read mostly by full scans. |
| `tables` | Find missing usable keys and review InnoDB free space. |
| `io` | Inspect table and file I/O over a window and since server start. |
| `config` | Review settings, engine defaults, sources, and health findings; `config set` and `config reset-persist` change one variable (SET GLOBAL, SET PERSIST, SET PERSIST_ONLY, SET SESSION, RESET PERSIST) after showing the statement and confirming (follows the connection's write policy and is audited). |
| `instruments` | Review Performance Schema coverage and generate setup SQL without executing writes. |
| `replication` | Show each replication channel or connection with thread states, errors, positions and lag caveats; `skip` skips the one failed transaction after a confirmation, `skip-loop` skips chosen error codes until stopped (both follow the connection's write policy and are audited). |
| `report` | Run every Server view once: ranked findings, per-area sections, replication and account basics, and the statements behind the numbers (text, JSON, Markdown, HTML, digest; `--fail-on`). |
| `upgrade-check` | Check a MySQL server before an upgrade with MySQL Shell, else a partial native subset (`--no-shell`, `--fail-on`); MariaDB gets the `mariadb-upgrade` steps. |
| `dump` | Dump databases into an absent or empty directory (MySQL Shell, `mysqldump`, `mariadb-dump`) with a checksummed manifest, after a plan and a confirmation. |
| `restore` | Restore a myflames dump after checking the manifest and typing the target; verifies tables and row counts. |
| `clone` | `CLONE INSTANCE` from a MySQL donor into the connected recipient after a preflight of both servers (in place or `--data-directory`). |
| `profiles` | Manage saved connections and their remembered passwords with list, set (type production or development, read-only), forget, or delete. |
| `privileges` | Show the connected account's effective privileges (a new session, active roles included) and, for every myflames action key, allowed, denied or not verifiable from SHOW GRANTS, with the reason. |
| `accounts` | Users and roles: `list` and `show` (no password hashes); `create`, `drop`, `rename`, `lock`, `unlock`, `expire`, `password`, `grant`, `revoke`, `role grant/revoke/default` show the statements, confirm, re-read the account, send on stdin and report the before and after grants (follows the connection's write policy and is audited; passwords from a prompt or `--password-stdin`, never argv). |
| `audit` | List, export (JSON or CSV), or purge the local audit trail of server writes sent, refused, or declined. |
| `compare` | Compare before/after EXPLAIN plans as HTML, text digest, or structured JSON. |
| `diff` | Alias of compare. |
| `digest` | Emit a compact plan digest and optionally estimate token and cost savings. |
| `tokens` | Deprecated alias of digest, defaulting to the cost comparison. |
| `check` | Gate a plan on finding categories or severities using exit codes. |
| `advise` | Emit ranked warnings and suggestions with confidence. |
| `findings` | Deprecated alias of advise. |
| `import-shell` | Convert a MySQL Shell diagnostics ZIP into capture JSON. |
| `guide` | Show which visualization to choose for a use case. |
| `teach` | Generate interactive algorithm lessons. |

The optional `myflames-mcp` entry point exposes agent tools, including read-only `server_report`, `top_statements`, `sessions`, `locks_report`, `health_sample`, `innodb_status`, `my_privileges` and `replication_status` (saved `profile` required; compact text by default, `detail="full"` for module JSON; no password or write arguments). The developer reference also covers the browser playground and MySQL/MariaDB fixture generators.

### Workspace refinements (2026-10-16)

Refresh all reads every Server section, including unopened views, with at most three concurrent section reads. Full analysis owns a separate phase. Preserve connection/scope consent, single-flight reads, cancellation and stale-response guards. Maintenance only detects tools; refresh never starts jobs or upgrade checks. Imported diagrams are preserved.

Home shows the installed version and links to releases. Update status stays unverified until the user clicks **Check for updates** (Home's version footer or About myflames); nothing asks GitHub automatically. The outcome and its time are kept in the workspace store and shown in local time, and a result older than 24 hours is shown as the last result, never as current. Query editor and Plans share the SQL workspace navigation group. Parameters stages inline edits and batch reviews (up to 25 changes) from server variable metadata and the generated `mysql_sysvars.py` catalog; legal scope selection, exact SQL review and Session editor ownership remain mandatory. Explanations stay in the inspector.

Tables/Indexes workspace reports include complete scoped inventories with a 100,000-row category ceiling and existing schema-size consent. Paginate after sorting; exports include all filtered rows. CSV protects formula-like values, HTML escapes names and values, and PDF uses the system print dialog. DATA_FREE never represents per-index fragmentation; reusable-space percentages require known sizes and file-per-table ownership. Preserve unknowns. CLI row limits remain unchanged.

Parameters uses **Review and apply** for batch edits and **Change…** in the inspector for single-parameter modes. Permission details appear inside the review dialog; the buttons do not show an Unverified badge. Existing confirmation and privilege checks still apply.
