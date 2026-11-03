# MyFlames Project Rules

The canonical rules file for every coding agent; `CLAUDE.md` and `GEMINI.md` are loading adapters. Feature contracts (flags, schemas, endpoints, safety rules, tests) live in [docs/DEVELOPER_REFERENCE.md](docs/DEVELOPER_REFERENCE.md): read the matching section (index at the end) before changing a feature.

## Project Overview

**myflames** is a MySQL and MariaDB performance workspace. Its core visualizes `EXPLAIN ANALYZE FORMAT=JSON` output as interactive flame graphs, bar charts, execution trees, and Visual Explain–style diagrams; around that it finds expensive statements (top and running queries), shows locks and deadlocks, and compares plans. New performance views go in the workspace's Server place (see ROADMAP.md "Performance workspace"). It requires MySQL 5.7+ (Percona Server for MySQL included; 5.7 is end of life and is read with what it lacks stated in plain sentences) or MariaDB 10.11+/11.4+ and Python 3.7+ with no external dependencies. The workspace ships as the desktop app (`apps/desktop`): macOS, plus Windows x64 as an unsigned preview; there is no browser version.

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/). **Never remove this inspiration / credit line** from the README (or anywhere else it appears). Brendan Gregg and Tanel Poder are the origin of this work and must always be acknowledged.

## Skills

Read the matching `SKILL.md` before working in an area. Skills are local to maintainer machines (gitignored): `.claude/skills/` is the real folder (Claude Code, Kimi) and `.agents/skills` a symlink to it (Codex, Antigravity). If a skill is missing, follow this file and DEVELOPER_REFERENCE. `.claude/skills/README.md` has the ownership map.

Skills: accounts, animation-expert, brutal-review, cli-ux, db-operations, delegate, full-review, mysql-expert, progressive-ux, query-editor, release, replication, server-analysis, server-lifecycle, steve-jobs-persona, structured-output, sysvar-catalog, teaching, test-pro, ui-architecture, ui-review, viz-specialist, web-design, web-dev.

## Worker roles

Any tool can play the roles in `.claude/agents/README.md` (mysql-correctness-reviewer, test-author, renderer-builder, teach-lesson-author). For a logic change, implement, run the MySQL correctness review and test author work in parallel, apply corrections, confirm tests pass, then ship.

## Development Workflow & Testing

- **Mandatory Tests**: Every new feature or logic change in the Python package MUST include a corresponding test case.
- **Verification**: Use the test-pro skill to check coverage and that MySQL explain data parses correctly before completing a task.
- **Full suite**: `./run-tests.sh` (a throwaway `HOME` and `MYFLAMES_SECRET_BACKEND=vault`). Tests use a temporary HOME and USERPROFILE with the vault backend; never run tests against the real `~/.myflames`.
- **Direct**: `python3 -m unittest discover -s test -p "test_myflames.py" -v`.
- **Live validation**: A feature that touches MySQL, MariaDB, ProxySQL, replication, clusters or network endpoints is validated against real servers (`scripts/ga-matrix.sh`, `scripts/cluster-matrix.sh`), never by mocks alone; never infer a passed live gate from a fixture test, and never mock out `dbops.py` classification, review tokens or policy checks.
- **Engine ground truth**: Never guess variable names, function arguments, column names or command syntax; check `../mysql-server`, `../mariadb-server` or the official manual first.
- **UI verification**: light and dark at 1440 px and at the 720 px minimum window width, especially charts, Compare and Audit (`.claude/skills/ui-review/scripts/screenshot.mjs`). A release gate builds and smoke-tests the packaged app.

## Visualization & UI Standards

- HTML demos and SVGs share one design language (system fonts, consistent Join vs Scan palettes); `docs/demos/` keeps high contrast and responsive layouts. Use viz-specialist when changing renderers (`output_*.py`) or charts.
- Never remove, unwrap or alter top-level layout wrappers (`.workspace`, `.topbar-stack` in `main.tsx`) when changing a child component or toolbar; check sticky headers, scroll containers, breakpoints and the sidebar before calling UI work done.
- Phone charts keep readable text in a scrollable region; test chart scale and page overflow.

## Architecture (Python package: `myflames/`)

1. **`parser.py`**: Single entry point. Builds a unified tree. Auto-detects MySQL vs MariaDB format. MariaDB normalization (`_normalize_mariadb*` functions) converts MariaDB's `query_block/nested_loop/table` structure into MySQL's `operation/inputs` tree before `parse_node` processes it. `analyze_plan(root)` scans for full_scans, hash_joins, etc.
2. **`cli.py`**: Argument parsing and SVG height patching for the analysis panel.
3. **Renderers**: `flamegraph.py`, `output_bargraph.py`, `output_tree.py`, `output_diagram.py`, `output_workbench.py` write standalone `.svg` output. (The treemap view was removed; `--type treemap` is a deprecated alias that warns and renders the bar chart.) HTML reports, `explore` pages and the workspace use the TypeScript chart runtime (DEVELOPER_REFERENCE "Chart runtime").

## Key Constraints & Coding Standards

- **No External Dependencies**: Stdlib only. Python 3.7+ compatible (no walrus operator, no `match` statements).
- **Single Parser**: Never parse JSON in output modules; always use `parser.parse_explain`.
- **SVG Rules**: Always update both `height` AND `viewBox` together. Use `_wrap()` for info panel text.
- **Preserve Order**: Do not sort `inputs[]` in the JSON; order is semantically significant (outer vs inner table).
- **Preserve Inspiration Credit**: Never remove the "Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs" line from the README or anywhere else; it is load-bearing. The workspace shows it in the **About myflames** window (Settings menu); README (near the top), NOTICE, this file and HTML report footers keep it.
- **License: Apache-2.0** (`LICENSE`, `NOTICE`). myflames began as a fork of FlameGraph (CDDL 1.0); the flame graph renderers (`myflames/flamegraph.py`, `apps/workspace/src/charts/flamegraph.ts`) are clean-room rewrites from a functional spec. Never copy or consult code from `flamegraph.pl`, the FlameGraph repo, or this repository's pre-2.4 history of those files when changing them. Releases up to 2.3.0 stay CDDL 1.0. The inspiration credit is separate from the license and must stay.
- **Server writes**: Reviewed SQL is executed SQL: the review token (`reviewed_writes`, `dbops`) binds it, and action endpoints require the token and never re-plan or change statements at execution. Classify only with `dbops.KINDS` (`read`, `write`, `risky`); re-check the target right before executing (db-operations skill).
- **Commit & release-note conventions**: Do NOT add `Co-Authored-By` or any AI-attribution trailer to commit messages. Commit messages, CHANGELOG entries and GitHub release notes stay professional: no emojis, no "generated with" footers, no meta-commentary that an AI/LLM authored or verified the work. Product references (the tool targets LLMs/agents; `--tokenizer claude` uses Anthropic's `count_tokens`) are fine.
- **Documentation is mandatory**: A change to a user-facing parameter, subcommand, flag or core feature (add, rename, change, remove) updates in the same change `README.md` (the section, the Agent/CI subcommands block, the CLI reference), the feature's section of `docs/DEVELOPER_REFERENCE.md` (feature contracts go only there), `CHANGELOG.md` `[Unreleased]`, and affected `docs/` files and `ROADMAP.md`. Update this file only when a core rule, the layout or the command list changes. A CLI surface change without its doc edits is incomplete; a rename keeps a documented deprecated alias.

## Commands

`python3 -m myflames <command>`; README "CLI reference" has each command's purpose and flags. The default command renders a plan with `--type`.

ui (deprecated), sql, capture, explore, top, slow-log, sessions, running (deprecated), locks, innodb, error-log, health, waits, indexes, tables, schema, io, config, instruments, replication, cluster, report, upgrade-check, dump, restore, clone, profiles, privileges, accounts, audit, compare, diff (alias), digest, tokens (deprecated), check, advise, findings (deprecated), import-shell, guide, teach.

The optional `myflames-mcp` entry point exposes agent tools, including read-only `server_report`, `top_statements`, `sessions`, `locks_report`, `health_sample`, `innodb_status`, `my_privileges`, `replication_status` and `cluster_status` (saved `profile` required; compact text by default, `detail="full"` for module JSON; no password or write arguments).

## Where feature contracts live (DEVELOPER_REFERENCE.md headings; subsections not listed)

- Workspace shell: Local UI, Shared view presentation, Chart runtime, Bundle and lazy loading, Local authentication and request limits, Client and local-storage boundaries, Workspace navigation and configuration controls, Action feedback, Workspace interaction correctness.
- Connections: Profiles, Saved connections and Database diagram Load.
- Server writes: Write safety and audit, Shared CLI confirmation, Reviewed actions and session ownership, Reviewed SELECT projections and server actions.
- SQL workspace: SQL CLI, Query editor sessions, Completion and table-data contracts, Import CSV, Database diagram, Schema versions, Export as SQL and Table structure.
- Plans: Run, Plan Stats and shared-plan literal hiding, Digest, Plan diff, CI gate, Advise, Rule controls, Advisor evidence rules, Visual Explain (merged Diagram / Workbench), Investigation engine.
- Workload: Find slow queries, Query digest sources and Time range, Locks, Sessions.
- Server views: InnoDB, Error log, Server identity: Percona Server, Server health, Waits, Indexes, Tables, I/O, Config, Tables and indexes: browse and export, Server variables: config set, reset-persist, Instruments, Full analysis report, Database Tuning Advisor, Parameter version catalogs, Compare servers: parameters and accounts, MySQL 5.7 support.
- Operations: Server lifecycle: upgrade-check, dump, restore, clone, Structure-only dumps, MySQL Shell, Replication: status, skip, skip-loop, Cluster: Galera, Percona XtraDB Cluster, InnoDB Cluster, ProxySQL, Users and roles: privileges, accounts, Privilege catalogs and adding scopes.
- Learn and output: Learn guided lessons, JSON and expected failures, `--quiet` / `-q`, MCP server, Playground, MySQL Fixtures, MariaDB Fixtures.
