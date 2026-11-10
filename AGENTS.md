# MyFlames Project Rules

The canonical rules file for every coding agent: `GEMINI.md` is a symlink to it, and `CLAUDE.md` imports it and adds only Claude Code specifics, so every model reads the same rules. Feature contracts (flags, schemas, endpoints, safety rules, tests) live in [docs/DEVELOPER_REFERENCE.md](docs/DEVELOPER_REFERENCE.md): read the matching section (index at the end) before changing a feature.

## Project Overview

**myflames** is a MySQL and MariaDB performance workspace. Its core visualizes `EXPLAIN ANALYZE FORMAT=JSON` output as interactive flame graphs, bar charts, execution trees, and Visual Explain–style diagrams; around that it finds expensive statements (top and running queries), shows locks and deadlocks, and compares plans. New performance views go in the workspace's Server place (see ROADMAP.md "Performance workspace"). It requires MySQL 5.7+ (Percona Server for MySQL included; 5.7 is end of life and is read with what it lacks stated in plain sentences) or MariaDB 10.11+/11.4+ and Python 3.7+ with no external dependencies. The workspace ships as the desktop app (`apps/desktop`): macOS, plus Windows x64 as an unsigned preview; there is no browser version. myflames focuses on that app: the Python command line (`myflames <command>`, `python3 -m myflames`) is deprecated since 2026-11-06, so new features, subcommands and flags go in the workspace only, and the CLI gets fixes only until a major release removes it (DEVELOPER_REFERENCE "Command line deprecation"). `myflames-mcp` (the agent tools) is deprecated the same way: no new tools. The Python package stays as the app's backend.

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/). **Never remove this inspiration / credit line** from the README (or anywhere else it appears). Brendan Gregg and Tanel Poder are the origin of this work and must always be acknowledged.

## Skills

Read the matching `SKILL.md` before working in an area. Skills are local to maintainer machines (gitignored): `.claude/skills/` is the real folder (Claude Code, Kimi) and `.agents/skills` a symlink to it (Codex, Antigravity). If a skill is missing, follow this file and DEVELOPER_REFERENCE. `.claude/skills/README.md` has the ownership map.

Skills: accounts, animation-expert, brutal-review, cli-ux, db-operations, debugging, delegate, full-review, mysql-expert, progressive-ux, query-editor, release, replication, security, server-analysis, server-lifecycle, steve-jobs-persona, structured-output, sysvar-catalog, teaching, test-pro, ui-architecture, ui-review, ui-stress, viz-specialist, web-design, web-dev.

## Worker roles

Any tool can play the roles in `.claude/agents/README.md` (mysql-correctness-reviewer, test-author, renderer-builder, teach-lesson-author); the security-reviewer is always a Claude reviewer. For a logic change, implement, run the MySQL correctness review and test author work in parallel, apply corrections, confirm tests pass, then ship. A change that crosses a trust boundary (local server, Electron shell, secrets, client subprocesses and the SQL they read, SQL the app builds, rendered server text, file paths, MCP tools, dependencies, third-party skills) also gets the security-reviewer.

## Development Workflow & Testing

- **Mandatory Tests**: Every new feature or logic change in the Python package MUST include a corresponding test case.
- **Verification**: Use the test-pro skill to check coverage and that MySQL explain data parses correctly before completing a task.
- **Done check**: Before calling a task done, map each requested item to evidence observed after the last edit (the command and its result, a test, a screenshot) or mark it unverified; a green suite does not prove an item nothing exercised. When the change reaches the area, also pass: write safety (no unreviewed write or review-token bypass), engine correctness (statements and names match the engine source or manual), a live smoke test (endpoints return 200 with parsed data from running servers), the layout check (scrolling, sticky headers and navigation at 1440 and 720 px), and security (for a change that crosses a trust boundary: the security-reviewer verdict and an abuse-case test per fix). Re-run a check when anything it covered changed after it ran.
- **Full suite**: `./run-tests.sh` (a throwaway `HOME` and `MYFLAMES_SECRET_BACKEND=vault`). Tests use a temporary HOME and USERPROFILE with the vault backend; never run tests against the real `~/.myflames`.
- **Direct**: `python3 -m unittest discover -s test -p "test_myflames.py" -v`.
- **Live validation**: A feature that touches MySQL, MariaDB, ProxySQL, replication, clusters or network endpoints is validated against real servers (`scripts/ga-matrix.sh`, `scripts/cluster-matrix.sh`), never by mocks alone; never infer a passed live gate from a fixture test, and never mock out `dbops.py` classification, review tokens or policy checks.
- **Engine ground truth**: Never guess variable names, function arguments, column names or command syntax; check `../mysql-server`, `../mariadb-server` or the official manual first. The same holds for React, Vite, Puppeteer and Electron behavior: check the version the project resolves and its official documentation or installed source.
- **UI verification**: light and dark at 1440 px and at the 720 px minimum window width, especially charts, Compare and Audit (`.claude/skills/ui-review/scripts/screenshot.mjs`). A release gate builds and smoke-tests the packaged app.
- **CHANGELOG with the change**: every bug fix or feature updates `CHANGELOG.md` `[Unreleased]` in the same PR, written for users the way the release notes are (DEVELOPER_REFERENCE "Release notes"); `.github/workflows/docs-sync.yml` fails a PR that changes user-facing code (`apps/workspace/src`, `apps/desktop` outside `test/`, `myflames` outside `ui_assets`) without it, unless the PR is labelled `no-changelog`.

### Release sync

"Release a new binary" (or "ship X.Y.Z") means every step below, in one go, from the release commit; nothing waits for another ask. Contracts: DEVELOPER_REFERENCE "Release sync".

1. **Release PR, one commit**: the version in `apps/desktop/package.json`, `myflames/__init__.py` and `pyproject.toml`; `CHANGELOG.md` `[Unreleased]` moved into a new `docs/release-notes/X.Y.Z.md` page, Release highlights first (the page is the GitHub release notes, word for word), and listed at the top of `docs/release-notes/index.md`; README, handbook (`docs/handbook`) and `docs/` current for everything in the page; README screenshots from the release build (`npm run build`, then `npm run docs:screenshots` in `apps/workspace`). `python3 scripts/check_release_sync.py X.Y.Z` passes; `docs-sync.yml` runs it on the PR because the version changed.
2. **Binaries from the merged commit**: the signed DMG and the update zip (`package:mac:release`), and the Windows installer from that commit's `Windows desktop` run, each with its `.sha256`.
3. **GitHub release**: `python3 scripts/release_notes.py X.Y.Z > local/notes.md`; `gh release create vX.Y.Z --draft --target <commit> --notes-file local/notes.md` with the DMG, the Windows installer and their `.sha256`; then `gh release upload vX.Y.Z` the zip and its `.sha256` (the DMG must come first); `gh release view vX.Y.Z --json assets > local/release.json && python3 scripts/check_release_assets.py X.Y.Z local/release.json`; then `gh release edit vX.Y.Z --draft=false`. On publish, `docs-sync.yml` sets the notes from the release notes page and fails when a file is missing or the zip came first (`scripts/check_release_assets.py`).
4. **Website**: run `sync.yml` in `vgrippa/myflames-website` (Actions > Run workflow; it also runs every 6 hours). It mirrors the DMG and the Windows installer (never the update zip), rebuilds the documentation and screenshots from the latest release tag (never master, so the site describes the app people can download), runs its QA and commits to `main`. The one manual step: click **Publish** in Lovable, which is when myflames.io changes.

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
- **Commit & release-note conventions**: Do NOT add `Co-Authored-By` or any AI-attribution trailer to commit messages. Commit messages, CHANGELOG entries, release notes pages and GitHub release notes stay professional: no emojis, no "generated with" footers, no meta-commentary that an AI/LLM authored or verified the work. Product references (the tool targets LLMs/agents; `--tokenizer claude` uses Anthropic's `count_tokens`) are fine.
- **Documentation is mandatory**: A change to a user-facing parameter, subcommand, flag (CLI fixes only: the CLI is deprecated) or core feature (add, rename, change, remove) updates in the same change `README.md` (the feature's section), the feature's section of `docs/DEVELOPER_REFERENCE.md` (feature contracts go only there), `CHANGELOG.md` `[Unreleased]`, the user handbook chapter in `docs/handbook/` (the website's Documentation renders it; regenerate with `myflames-website/build_docs.py`), and affected `docs/` files and `ROADMAP.md`. Update this file only when a core rule, the layout or the command list changes. The deprecated CLI and `myflames-mcp` are documented only in `docs/DEVELOPER_REFERENCE.md` (contracts for fixes): never add CLI or MCP material to the README, the handbook or the website. A CLI fix without its DEVELOPER_REFERENCE edit is incomplete; a rename keeps a documented deprecated alias.

## Commands

`python3 -m myflames <command>` (deprecated: one notice line on stderr, no new commands or flags; not documented for users any more); `myflames <command> --help` and DEVELOPER_REFERENCE have each command's flags. The default command renders a plan with `--type`.

ui (deprecated), sql, capture, explore, top, slow-log, sessions, running (deprecated), locks, innodb, error-log, health, waits, indexes, tables, schema, io, config, instruments, replication, cluster, readyset, report, upgrade-check, dump, restore, clone, profiles, privileges, accounts, audit, compare, diff (alias), digest, tokens (deprecated), check, advise, findings (deprecated), import-shell, guide, teach.

The optional `myflames-mcp` entry point (deprecated with the CLI: one stderr notice at start, no new tools) exposes agent tools, including read-only `server_report`, `top_statements`, `sessions`, `locks_report`, `health_sample`, `innodb_status`, `my_privileges`, `replication_status`, `cluster_status` and `readyset_status` (saved `profile` required; compact text by default, `detail="full"` for module JSON; no password or write arguments).

## Where feature contracts live (DEVELOPER_REFERENCE.md headings; subsections not listed)

- Workspace shell: Local UI, Shared view presentation, Chart runtime, Bundle and lazy loading, Local authentication and request limits, Client and local-storage boundaries, Workspace navigation and configuration controls, Action feedback, Workspace interaction correctness.
- Connections: Profiles, Saved connections and Database diagram Load.
- Server writes: Write safety and audit, Shared CLI confirmation, Reviewed actions and session ownership, Reviewed SELECT projections and server actions.
- SQL workspace: SQL CLI, Query editor sessions, Completion and table-data contracts, Import CSV, Database diagram, Schema versions, Export as SQL and Table structure.
- Plans: Run, Plan Stats and shared-plan literal hiding, Digest, Plan diff, CI gate, Advise, Rule controls, Advisor evidence rules, Visual Explain (merged Diagram / Workbench), Investigation engine.
- Workload: Find slow queries, Query digest sources and Time range, Locks, Sessions.
- Server views: InnoDB, Error log, Server identity: Percona Server, Server health, Waits, Indexes, Tables, I/O, Config, Tables and indexes: browse and export, Server variables: config set, reset-persist, Instruments, Full analysis report, Database Tuning Advisor, Parameter version catalogs, Compare servers: parameters and accounts, MySQL 5.7 support.
- Operations: Server lifecycle: upgrade-check, dump, restore, clone, Structure-only dumps, MySQL Shell, Replication: status, skip, skip-loop, Cluster: Galera, Percona XtraDB Cluster, InnoDB Cluster, ProxySQL, Readyset, Users and roles: privileges, accounts, Privilege catalogs and adding scopes, File transfer: SSH connections and SFTP.
- Learn and output: Learn guided lessons, Command line deprecation, JSON and expected failures, `--quiet` / `-q`, MCP server, MySQL Fixtures, MariaDB Fixtures.
- Releases: Release sync (versions, release notes, README screenshots, release files, `docs-sync.yml`, the website sync), Release notes (format and writing style).
