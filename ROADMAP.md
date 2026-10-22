# myflames roadmap

> Where myflames is going and why. For *what changed in each release*, see [CHANGELOG.md](CHANGELOG.md). For the queue of teaching lessons, see [.claude/ALGORITHM_ROADMAP.md](.claude/ALGORITHM_ROADMAP.md).

## Vision

**myflames is the token-efficient, source-grounded interface between a query plan and an AI agent; and a clear set of pictures for a human.** Raw `EXPLAIN ANALYZE FORMAT=JSON` is verbose and noisy; an LLM burns tokens parsing it before it can reason, and SVGs are invisible to agents. myflames parses and analyzes a plan once, then projects it many ways: flame graph / bar chart / tree / Visual Explain (for humans), a compact source-verified digest and JSON sidecar (for agents), and an exit code (for CI). One analysis, many projections.

**Scope: a MySQL and MariaDB performance workspace.** Query plans are where myflames started and remain its center, but a slow query is usually explained by its surroundings: what else runs on the server, what it waits on, which locks block it, and whether statistics and indexes match the data. myflames covers that path in one place: find the expensive work (top statements, sessions), explain it (plans, captures, advisor), see what blocks it (locks, deadlocks), and compare the fix. Every new view follows the same rules as the plan tools: read-only by default, source-verified claims, one analysis shared by the UI, CLI, and agent tools, and honest notes where MySQL and MariaDB differ.

It stays a single project: pure-Python stdlib core, with heavy integrations behind optional extras (`myflames[mcp]`, `myflames[tokens]`). The optional local workspace in `apps/workspace/` uses React and TypeScript; its built assets ship in the Python package. The Python engine remains shared by the CLI, UI, and agent tools.

## Current workstation capabilities

The workspace ships only as the macOS desktop app (3.1.0): an unsigned Apple
Silicon `.app` that bundles Python and opens directly from Finder, attached to
the GitHub release as `myflames-3.1.0-macos-arm64.zip`. The browser launch
(`myflames ui`) is deprecated and the Python CLI remains for command-line use.
Signing, notarization, automatic updates and other platforms remain release work.
The app is Electron around the existing React/TypeScript workspace and Python
engine. The
working UI includes the new compact shell, command palette, full-width Server
details, catalog completion and reviewed table-data edits. Setup and current
limits are described in [Desktop app](docs/DESKTOP.md). The remaining proposed
extensions are CSV import, SSH tunnels, server history, schema comparison,
object editing and incident collection.

The workspace includes the Query editor's persistent sessions, light and dark
appearance, Parameters (AWS RDS-style parameter group table, inline edits, batch review up to 25 changes, and Session handoff to the editor), Users and roles with
new grant scopes and batch review, and Database diagram with a Diagram | Mermaid view (`mermaid` 11.17.2), live metadata,
SQL/Mermaid imports and exports, editable JSON and SVG. Diagram imports do not
execute SQL; save JSON to retain a layout. The live database picker and Rebuild action refresh tables, keys and relationships while retaining positions for tables that remain. Maintenance has native folder selection, tool-specific dump options, official download links and bounded release checks.

Top statement details now use the available width. Supported DML can prepare a SELECT projection for individual review before capture, with the original SQL retained. Optimize table and Instruments selection (up to 2,000 rows, 500-name batched `UPDATE` statements, consumer dependencies) provide optional operations through exact SQL confirmation. A view reads when opened on screen (once per connection, while Tables and Indexes keep their schema-size consent and Error log reads on open). Learn and standalone `myflames teach` lessons share the workspace's light and dark tokens, semantic `--svg-*` drawing palette and embedded lesson toolbar. Ordinary InnoDB free space remains informational; true storage capacity monitoring is still a separate gap.

Action feedback now covers Top statements, Error log, Query editor and Database
diagram, including focus retention, pending session operations, and cancellation
of a stalled diagram import. Browser regression scenarios cover delayed replies,
errors and missing replies; see the action-feedback suite in the UI checklist.
Cluster and ProxySQL keep pending reviewed changes visible under the toolbar
after Close, including while the selected member or server inspector is open.

Security boundaries now include launch bootstrap with cookie and origin proof,
isolated database clients, recipient TLS checks for saved clone credentials,
owner checks for ended editor sessions, one-use action reviews, bounded requests
and plans, and protected local storage. The CLI shares exact confirmation and
an unsent exit-2 contract. See [the developer contracts](docs/DEVELOPER_REFERENCE.md)
for limits and [the verification checklist](docs/UI_TESTING.md) for release gates.


## Shipped

- Backend review fixes: explicit TCP targeting, audited and faster bulk Kill with Group Replication protection (including truncated process-list command names), accurate slow-query notes and unknown counters, bounded session and lifecycle reads, version-aware error logs and analysis, editor flavor detection, and bounded update downloads with release-page fallback.

- **3.1.0 (first 3.x release; 3.0.0 was never published)**: the macOS desktop
  app as the only way to run the workspace (browser launch deprecated); the
  **Tuning** place, a 0 to 100 scorecard of 19 checks in five weighted
  categories with Full analysis as its second tab; Server > Error log;
  Parameters with batch review; reviewed table data editing, SQL completion,
  Database diagram with a Mermaid view, Users and roles with every grant
  scope, Maintenance (upgrade check, dump, restore, clone) and Replication;
  and a broad correctness pass over plan parsing, MariaDB plans, write
  classification and the update downloader. See [CHANGELOG.md](CHANGELOG.md).

- **Ideas from MySQL Shell and DBeaver**: find the query first (`top` from
  Performance Schema digests, `running` with the plan of a running statement),
  then explain or analyze a top statement in one step (`capture --digest`,
  the workspace's row actions and Explain top N),
  import MySQL Shell diagnostics bundles, a MySQL Shell plugin, upgrade-checker
  style rule controls (`--ignore`, `--list-rules`, docs links), and new
  evidence rules for row misestimates and statistics freshness, redundant
  indexes, and cold buffer pools. Saved connections can remember their
  password in the OS credential store or an encrypted local file
  (`--profile`, `myflames profiles`).
- **Write safety foundation** (`myflames/dbops.py`): every saved connection
  is Production (the default, asks before every write) or Development (asks
  only before risky writes), optionally read-only (refuses every write); one
  statement classifier (read, write, risky) for every write path and the
  SQL editor; a privilege hint helper over SHOW GRANTS for both
  engines; and a local audit trail of every write sent, refused or declined
  (`myflames audit`, `/api/audit/*`). Kill uses all of it. The workspace exposes connection types, read-only policy, and the Audit log in Settings.
- **Sessions and InnoDB status**: `myflames sessions` (`sessions-1.0`) lists
  every connection with its open InnoDB transaction, flags long-running
  statements, sessions idle in a transaction, and lock waits, and summarizes
  connections, states, users, hosts, databases, the oldest transaction and the
  history list length; it replaces `running`, now a deprecated alias.
  `myflames innodb` (`innodb-1.0`) parses SHOW ENGINE INNODB STATUS into
  sections with summaries, rule-of-thumb statuses and links, never raw unless
  asked. Both are workspace views (Processlist in the Workload place,
  replacing Running queries; InnoDB under Server > Overview).
- **Error log**: `myflames error-log` (`error-log-1.0`) and Server > Overview >
  Error log. MySQL reads `performance_schema.error_log` (8.0.22+), including
  rows the server reloaded from its previous run. MariaDB has no SQL access to
  its error log, so the file is read only when the server runs on this computer
  and its identity is verified; otherwise the view explains where to read it.
  Next: error log findings in Full analysis and an MCP tool.
- **Locks**: `myflames locks` and the workspace Locks tab, a server-wide
  blocking tree over row and metadata lock waits with root blockers and
  idle-in-transaction blockers, who blocks a waiting DDL statement (exact, or
  likely blockers on MariaDB without the metadata lock instrument) and what
  queues behind it, row locks with plain-language modes, and the parsed latest
  deadlock, on MySQL and MariaDB with each engine's limits stated. It only
  reads, except **Kill** / `--kill ID`: one session, shown and
  confirmed first, re-checked before the KILL, and offered only when the
  account's grants allow it.

- **Query investigation workflow**: linked operator selection, focus/collapse,
  contextual Teach, local investigation bundles, structural comparisons, and a
  query laboratory with repeated live captures, cancellation, schema/index
  inspection, statement statistics, and optimizer traces. Shared `capture` and
  `explore` CLI commands. The UI uses a simplified mark derived from the project logo, stable logo URLs across rebuilds, and a neutral/blue design.

- The 3D view of the B+tree lesson moved to a separate project.
- **Local browser workspace**: `myflames ui` imports saved plans, displays four chart views and operator details, compares plans, and exports HTML/JSON. Session restore autosaves the open workspace (never passwords) and reopens it on the next launch; `--no-restore` starts one launch clean. The Learn section opens the interactive lessons with a view toolbar, family filter, grouped learning-path rows ("Step n of 16"), embedded lesson bar, light/dark tokens and downloads. React/TypeScript assets ship with the standard-library Python server.

- **Four views**: flame graph, bar chart, execution tree, and unified Visual Explain. (The treemap was removed: area made relative cost hard to compare, and the bar chart answers the same question more precisely.) Diagram and Workbench share one renderer with larger Venn join symbols, operation colors, row-volume arrows, and smooth mouse/trackpad navigation. Existing `diagram` commands remain compatible.
- **Source-verified advisor**: rules and `optimizer_switch` explanations checked line-by-line against MySQL/MariaDB source.
- **JSON sidecar + JSON-LD**: every report has a machine-readable sibling; agents never OCR an SVG.
- **Token-cheap digest + savings report**: `myflames digest` (with `--cost`, `--tokenizer {heuristic,claude,gpt}`, `--show-prompts`); an "Agent-ready" panel in every HTML report.
- **Agent/CI subcommands**: `diff` (plan comparison), `check` (CI gate with a `0/1/2` exit contract), `advise` (ranked warnings + suggestions with confidence).
- **MCP server**: `myflames-mcp` (`pip install myflames[mcp]`) exposing `analyze_plan`, `digest_plan`, `compare_plans`, `explain_optimizer_switch`, `explain_query`.
- **Published to PyPI**: `pip install myflames` (and `myflames[mcp]` / `myflames[tokens]` / `myflames[gpt]`) resolves; a GitHub Release auto-publishes via a Trusted Publisher workflow.
- **Honest agent default (the token thesis, kept)**: the MCP `analyze_plan` tool returns the token-cheap decision-grade sidecar by default (`detail="core"`: summary, warnings, suggestions, switches, plan tree), dropping the heavy `collected`/`query`/`teach_hooks` blocks; `detail="full"` restores the complete record. The `detail` gate lives on `build_sidecar` (single source of truth); the CLI `--sidecar` path and the `digest` path stay `full`. Removes the one place the agent surface contradicted the vision, prerequisite to broadcasting it.
- **One ranker, every projection**: the "fix first" decision is computed once, by `findings.build_findings` / `findings.primary_suggestion_index`. The HTML "Fix first" card and the sidecar `primary_action` both dereference it, so the human card and the ranked `advise` list can no longer disagree (medium correctly outranks low everywhere). A genuinely clean plan shows an explicit "No blocking issues found" card instead of a blank.
- **Advisor names the index that removes a filesort**: for a single-base-table `ORDER BY` on plain columns, myflames suggests `CREATE INDEX … (col1, col2 [DESC])` (direction-preserving) so MySQL reads rows already sorted, instead of only "grow the sort buffer." The parser threads the sort columns through (MySQL `sort_fields`, MariaDB `sort_key`); the rule bails to the generic hint on joins/aggregates/expression sort keys. Verified against MySQL/MariaDB source.
- **Uniform `--quiet/-q`**: the flag now behaves identically on `check`, `digest`, `advise`, and `compare` (was `check`-only): suppress stderr diagnostics, leave stdout data and the exit code untouched.
- **Attribution + credit on the compare report**: the before/after report now carries the "Made with myflames" link and the Brendan Gregg / Tanel Poder inspiration credit, matching the main HTML report.
- **`semijoin_firstmatch` teach lesson**: the flagship of the semijoin cluster (the `IN (subquery)` early-out).

## Next

- **Workspace follow-ups**: broader SQL editing conveniences, richer optimizer-trace summaries, and signed and notarized desktop builds with a verified update channel. Query capture, saved investigations, structural comparison, and contextual Teach are implemented.
  Import-dialog layout coverage includes open/error states and short windows;
  continue expanding interaction coverage as the workspace grows.
  Visual Explain zoom includes a live percentage, press feedback, and visible limits.
  The workspace audit covers every primary screen, all Teach lesson players,
  live Sakila capture, export/import, search states, and responsive breakpoints;
  see `docs/UI_TESTING.md` for remaining platform checks.

Distribution multiplies whatever is inside, and the inside is now honest (decision-grade agent default) and consistent (one ranker). So the remaining work is reach and depth.

1. **Wider distribution**: the "made with myflames" footer now ships on the reports (main + compare); what remains is external reach: MCP registries and awesome-lists, so agents and humans discover the tool.
2. **Browser playground**: linked from the README and installs the published wheel via micropip; still needs an end-to-end pass in a real browser and confirmation the GitHub Pages deploy serves [docs/playground/](docs/playground/).
3. **CLI consistency**: shared server-write confirmation and the unsent exit-2 contract are implemented. Preserve them as commands grow. (owned by `cli-ux`)
4. **Finish the semijoin cluster**: `semijoin_firstmatch` shipped as the flagship; `loosescan`, `materialization`, and a real `duplicate_weedout` (today a stub) remain, each taught as a contrast against the FirstMatch early-out. See [.claude/ALGORITHM_ROADMAP.md](.claude/ALGORITHM_ROADMAP.md). (owned by `teaching`)

## Performance workspace: proposed next views

Candidates for the workspace's **Server** place, in suggested order. Each is read-only, works on MySQL 8.4+/9.x and MariaDB 10.11+ (with engine notes), has a CLI equivalent with `--json`, and links back to plans where it can.

**Shipped: Percona Server for MySQL.** Percona Server 8.0 and 8.4 and Percona XtraDB Cluster are named by their product everywhere while MySQL rules apply, and the views read what Percona adds when the server reports it: user statistics in Indexes and Tables, the thread pool in Health, slow log sampling, and Percona variables marked in Parameters. Checked live on the GA matrix's Percona Server 8.0.46-37 and 8.4.11-11 (with a replica) and on PXC 8.4.11-11.1. Next: Percona's own lifecycle dates for version support, its audit log (`audit_log` plugin on 8.0, `audit_log_filter` on 8.4) in Security basics once installed in the matrix, and Group Replication peers' product.

**Shipped: Server health.** The **Health** tab (first on Server) and `myflames health` report deltas and rates over a sampling window from two `SHOW GLOBAL STATUS` samples, with rule-of-thumb statuses, `health-1.0` JSON, and `--fail-on` for CI and cron. No Performance Schema is required. Diagnostics come first: whether the buffer pool is full or warming (and the bytes read from disk per second), whether the redo log fits the write rate, the table, definition, thread, and binary log caches, open files, full scans, temporary tables, and sorts, each with its rule, verdict, why, and settings. Its claims passed a `mysql-correctness-reviewer` review and were checked live on MySQL 8.4, 9.7, and 26.7 and MariaDB 11.4 and 13.0.

**Shipped: Wait events, Index health, I/O hot spots, and Parameters (`config`).** The **Waits** and **I/O** views in the Workload place and **Indexes** and **Parameters** on Server, along with `myflames waits`, `indexes`, `io`, and `config` (`waits-1.0`, `indexes-1.0`, `io-1.0`, `config-1.0` with `parameters-1.0` in `/api/live/config`), read raw Performance Schema and information_schema tables, never the sys views, and never truncate or change instruments. Waits ranks wait events over a window without adding classes up and lists the classes that are off with the statements to turn them on. Indexes lists unused indexes (PRIMARY and UNIQUE excluded on both engines, foreign-key indexes kept; partitioned or unknown candidate tables under No data with DROP and visibility SQL withheld), redundant indexes with the advisor's rules, and tables read mostly by full scans, with a MariaDB `userstat` fallback. I/O shows tables, files, and file kinds over a window and since start, linked to Index health and Top statements. CLI `config` checks a short list of settings with engine defaults, sources, statuses, and caveats, and `--fail-on` for CI; the workspace **Parameters** view adds a full parameter group table (`myflames/parameters.py`, 817-variable `myflames/mysql_sysvars.py` catalog) with inline edits and batch review. The cost of each read is documented: per-table and per-index reads run only on request, scoped to a schema, and `INNODB_BUFFER_PAGE` is never read. Their claims passed a `mysql-correctness-reviewer` review and were checked live on MySQL 8.4, 9.7, and 26.7 and MariaDB 11.4 and 13.0, with Performance Schema on and off.

**Shipped: Table health (missing primary keys and fragmentation).** The **Tables** tab (after Indexes on Server) and `myflames tables` (`schema-1.0`) list tables without a primary key, telling apart a unique key on NOT NULL columns that InnoDB uses as the clustered index from the hidden 6-byte row ID, and counting MySQL's generated invisible primary key as a key. They show `DATA_FREE` as reusable space, with the system and general tablespaces listed apart. Its size or percentage alone does not produce a warning or recommend a rebuild. Capacity and filesystem headroom remain unmeasured. Key changes remain statements to review; the optional Optimize action opens exact SQL for confirmation before execution. `--fresh` reads MySQL statistics past the `information_schema_stats_expiry` cache for myflames' own connection only. A shared database picker (all user databases by default) replaces the single schema field in Indexes, which reads every user database by default only up to 2,000 tables. Claims were checked against the server source by the `mysql-correctness-reviewer` and live on MySQL 8.4 and 26.7 and MariaDB 11.4 and 13.0, including fresh versus cached statistics.

Possible follow-ups: per-partition free space from `information_schema.PARTITIONS`, MyISAM and Aria free space (`DATA_FREE` there is deleted-row space in the data file), and a link from a plan's table to its Tables row.

**Shipped: Performance Schema instruments guide.** The **Instruments** tab (under Server > Setup) and `myflames instruments` (`instruments-1.0`, including `instruments[]`) show which instruments and consumers are on, which myflames views they light up, what each costs (rule of thumb), and the runtime SQL and my.cnf lines to change them, with presets (Minimal for myflames, Lock troubleshooting, Wait analysis, Back to defaults) and interactive tabs (Features, Instruments, Consumers, Actors and objects). Collection only reads the setup tables (and reads on open in the workspace). The workspace can select up to 2,000 instruments or consumers and apply batched runtime `UPDATE` statements after exact review and confirmation; the CLI continues to generate statements without executing them. The other Server tabs link their "instrument is off" notes to its rows. Presets and restores were run live on MySQL 8.4, 9.7, and 26.7 and MariaDB 11.4 and 13.0, and its claims passed a `mysql-correctness-reviewer` review.

**Shipped: Server place redesign.** The flat Server tabs became grouped sub-navigation across Server (Overview, Contention, Schema, Setup, Security) and Workload (Queries, Resources), alongside the standalone Replication and Tuning places, with status dots and counts from data already loaded, and every view now has the same anatomy: a toolbar with Refresh, one summary line with an "N notes" button, a sortable table or one-line rows, and a resizable inspector for the selected row (a bottom sheet on phones). Explanations moved behind ⓘ popovers and into the inspector, readiness card grids became one-sentence prerequisite gates, and Instruments starts with Easy setup and a one-line checklist. Processlist (Sessions) replaced Running queries in Workload and InnoDB joined Server > Overview. The patterns follow Apple's Activity Monitor, System Settings, and the Finder inspector, and the Workbench and DBeaver behaviors verified in their source. At 1440 by 900 the views show 0 to 2 explanatory paragraphs instead of 2 to 13, and no cards instead of up to 40.

Possible follow-ups: per-table buffer pool usage as an explicit opt-in (MySQL `INNODB_CACHED_INDEXES` is cheap; `INNODB_BUFFER_PAGE` scans the whole pool), and links from a plan's table to its I/O and index rows.

**Shipped (backend): Full analysis report and workload attribution.** `myflames report` and `POST /api/live/report` (`report-1.0`) run every Server view once on one connection (at most three at a time, windowed views first) and compose ranked findings with their numbers, rule, why, caveat and a suggestion that is never applied, one "look at first" pick, and sections from memory to Performance Schema coverage, exported as text, JSON, Markdown, static HTML or a token-cheap digest, with `--fail-on` for CI. New read-only checks cover replication status on a replica, accounts without a password, anonymous accounts, `root` from any host, the MariaDB query cache and binary log expiry. `myflames top --order full_join|scan|no_index` and `--table SCHEMA.TABLE` link Health's joins without an index and Indexes' full scans to the statement digests behind them, every table match labeled "text match, not proof". MySQLTuner served as a checklist only. Recorded live on MySQL 8.4 (a replica), MySQL 8.0 (as root and as a limited account) and MariaDB 11.4 (Performance Schema on and off). The workspace Tuning place (Scorecard and Full analysis tabs), Refresh all, and statement links are implemented. Remaining families listed as not checked (Galera, the MyISAM key cache and Aria page cache, a total memory budget shown as an upper bound).

**Shipped (CLI and API): Upgrade check, dump, restore and clone.** `myflames upgrade-check`, `dump`, `restore` and `clone` and `POST /api/lifecycle/*` run MySQL Shell, `mysqldump`, `mariadb-dump` and the `mysql` client from the machine running myflames (passwords in a removed 0600 option file, CLONE's on stdin), with a plan and exact commands shown before a confirmation (typed target for restore and clone), the connection's write policy, progress, cancel of the process group and the server session, verification (manifest checksums, exact row counts, `clone_status` tied to this clone, the GTID set) and an audit record per attempt. Without MySQL Shell the upgrade check is a labeled partial subset whose reserved-word list matches `information_schema.KEYWORDS` read live on MySQL 8.0, 8.4 and 9.7. Validated live on MySQL 8.0.46, 8.4.11 and 9.7.2 (dump and restore both ways, a DATA DIRECTORY clone, in-place clones through the restart, cancel mid-copy) and MariaDB 11.4 (guidance paths). The workspace screen shipped too: Server > Setup > Maintenance (Tools, Upgrade check, Dump, Restore, Clone) with read-only preflights, confirmation sheets with the exact commands, job progress and Cancel, and `POST /api/lifecycle/jobs` so a reload or a lost Start reply finds a job instead of starting it twice. Next: wiring MariaDB `mariadb-backup` (backend exists; it needs the server host's files), and a live MariaDB dump and restore run with MariaDB client tools installed.

**Shipped (CLI and API): Replication.** `myflames replication` and `POST /api/live/replication` (`replication-1.0`) list every channel (MySQL) or connection (MariaDB) with thread states, errors, positions or GTIDs and lag with its caveats (NULL and 0 semantics, SQL_Delay, worker commit timestamps, heartbeat and last queued transaction), read by column name; Group Replication's own channels are left out and named in `group_replication_channels`, so a group member with no asynchronous channel is not a replica (the workspace says **Not a replica** and points to Cluster, never Unknown). `myflames replication skip` and `POST /api/live/replication/skip` skip the one failed transaction (an empty GTID transaction on MySQL when it has a GTID, the skip counter for anonymous transactions, AGAT channels and MariaDB) after a confirmation, with a re-read right before sending, a check of the relay log that the counter passes the failed transaction, and a read after that shows whether it was passed. `myflames replication skip-loop` and `/api/live/replication/skip-loop/start|status|stop` skip chosen error codes until stopped, bounded by a cap and an optional duration, and stop on any other condition; loops are keyed by connection identity and never resume. Every attempt is audited. Validated live on MySQL 8.4 (GTID on, file and position, two channels), MySQL 8.0 and MariaDB 11.4 (named and default connections, parallel replication over two domains). The workspace place shipped too: Replication (a standalone sidebar place) with the channel table and inspector, the skip and skip-loop confirmation sheets, and the loop's line with Stop above every Server view; loops are matched by the connection's endpoint, so a change of type or read-only keeps their Stop. Each source's engine and version comes from the relay log, else a saved connection to the source opened at most once per run until it is edited (one read at a time, at most 1,024 attempts per run, and for a loopback source only when the replica is on this computer). Next: replay fixtures from MySQL 9.x and MariaDB 10.11 replicas.

**Shipped: Cluster.** A place of its own after Replication, `myflames cluster`, `POST /api/live/cluster` and the MCP tool `cluster_status` (`cluster-1.0`) show the MariaDB Galera Cluster, Percona XtraDB Cluster, MySQL InnoDB Cluster or Group Replication cluster the connection belongs to: members with state and role as the connected server sees them (Galera exposes only its own node's state, so other nodes are not judged), quorum and tolerated failures, flow control and queues, and, from InnoDB Cluster metadata, members missing from the group, read replicas and routers, with `--fail-on` for CI. The Full analysis's Galera check reuses `cluster.py`'s rules. Claims were checked against mariadb-server, mysql-server and mysql-shell source and live on MariaDB 11.8 Galera, Percona XtraDB Cluster 8.4 and a MySQL 8.4 InnoDB Cluster (healthy, a donor, a node down, an isolated node, a member stopped, a member unreachable). Reviewed changes shipped in the place too, under db-operations: Make primary, Stop and Start Group Replication on the connected member, plain Group Replication mode switches, and MariaDB Galera desync, resync and pc.bootstrap (checked against the same sources; desync and resync and Stop and Start Group Replication run live on the lab clusters). Next: Percona XtraDB Cluster and Galera Cluster for MySQL changes once checked against their sources, InnoDB Cluster mode switches and rejoin through MySQL Shell, ClusterSet, and read replica states read from each instance when viewed from a group member.

ProxySQL correctness fixes: unread routing variables leave affected digests Not judged, hexadecimal digest filters accept either letter case, and digest selection and rule links preserve names containing separators. Reviewed Cluster and ProxySQL actions discard completed sheets after a connection switch while retaining the Audit log record.

**Shipped: ProxySQL.** A place of its own after Cluster and `POST /api/live/proxysql` (`proxysql-1.0`) read ProxySQL's Admin interface (2.x and 3.x): backends as ProxySQL runs them with their connection pool and configuration row, hostgroups and the Galera, Group Replication, replication and Aurora monitors that manage them, query rules with hits and the top digests, plus the differences between memory, runtime and disk. Reviewed changes set a server's status, weight or hostgroup, or a rule's active flag, edit memory and load it to runtime in one unit; saving to disk is its own reviewed step. Checked against the ProxySQL source and live on ProxySQL 3.0.11 in front of the MariaDB Galera and MySQL InnoDB Cluster lab. Query digests (`POST /api/live/proxysql/digests`, `proxysql-digests-1.0`) orders the digests (slowest, most time, most frequent, highest rate, most rows sent), lists the top 10 to 100 and filters them, bounded in Admin, and the rules are cross checked with the digests: where the active rules send each digest, evaluated here, next to the hostgroup it ran on, with mismatches flagged, and the digests each rule applies to. Reviewed changes show their progress and result in the review sheet (`/api/live/action/progress`). Next: `mysql_users` (read-only first), `stats_mysql_processlist`, digest resets, a ProxySQL Cluster view (`proxysql_servers`, checksums), live checks on 2.x (the digest columns differ before 2.0.7), a CLI command and an MCP tool.

**Shipped (backend, CLI and API): Privilege awareness and Users and roles.** `myflames privileges` and `POST /api/live/privileges` (`privileges-1.0`) answer, for every myflames action key, whether the connected account may run it: allowed, denied (only from a missing global or dynamic privilege) or not verified (object privileges, MariaDB SUPER after 10.11, role graphs), with the missing privileges named, so the UI can grey an action with its reason and leave unverifiable ones enabled. `myflames accounts` and `POST /api/accounts/list|read|plan|apply` list users and roles without password hashes and create, drop, rename, lock, unlock, expire, set passwords, grant and revoke at every level and manage roles and default roles, with the exact statements, a confirmation, a re-read that refuses when the account changed, statements on stdin, a before and after diff, the typed-name guards (protected accounts, self-lockout), and an audit record per statement. Verified live on MySQL 8.0, 8.4, 9.7 and MariaDB 10.11, 11.4 (including the MariaDB binary log on the three-step create and with simple_password_check). The Users and roles view, scope-aware grant matrix, batch review, and shared privilege gates are implemented. The matrix includes all source-defined scopes, anonymous proxy accounts, MariaDB packages/package bodies, and MySQL 9.2+ MLE libraries, with per-privilege dynamic grant options and role admin/default controls. Follow-ups include copying an account, resource-limit and password-policy editors, and folding `replication.py`'s and `variables.py`'s own privilege texts into `privileges.requirements`.

## Deferred / non-goals

- **No LLM bundled into the package.** myflames is the deterministic, source-grounded tool an agent *calls*; it does not become the agent. (Exact token counting uses Anthropic's `count_tokens`, or tiktoken for GPT, only as optional extras.)
- **No splitting into multiple packages/repos.** Optional extras are the dependency boundary.

## September 2026 review

The [project review](docs/reviews/2026-09-15.md) adds current-MySQL corpus checks
and fixes connection escaping, advisor evidence, renderer output, comparison
matching, and teaching controls. Follow-up work remains for broader SQL metadata recognition and
complexity models for ordered/multidimensional aggregates.

### Workspace refinement, October 2026

Successful Kill feedback in Locks, Processlist and Kill sessions uses **Done.** followed by the server's message.

Implemented shared action feedback for Processlist Kill, Parameters change/reset and batch apply, and Replication preflight, skip, loop start and Stop. Slow write sheets can close while their progress and outcomes remain in the view; reads expose Stop, Try again and their completion time. Delayed-reply browser scenarios are in the action-feedback harness, including preflight button stability, Cancel focus on review opening and return to the initiating control on dismissal.

Shipped: Query digest and Database diagram are places of their own in the SQL workspace group (Query editor, Query digest, Database diagram, Plans); the Query editor has no digest tab and Server's Schema group lists Indexes and Tables. Top statements and Error log read when opened on screen, like every other view. Processlist rows have a context menu with Copy query, Kill query… and Kill connection…, which open the reviewed single Kill sheet and show its progress and answer. Replication reads Not a replica for a server with no asynchronous channel, and its sidebar item is greyed like Cluster's.

Implemented Home version information and workflow shortcuts, grouped SQL navigation, a white app mark with saturated flames, the Database diagram Diagram | Mermaid view (`mermaid` 11.17.2) and Mermaid export/import fixes, all-section Refresh all, the Parameters view (`parameters-1.0` and the 817-variable `mysql_sysvars.py` catalog) replacing Configuration, interactive Instruments selection, and the Learn workspace restyle with light and dark tokens and `--svg-*` drawing colors. **Check for updates** in Home's version footer and About myflames asks GitHub only when clicked and shows Up to date, the newer version with **Download…** (which saves the release file to Downloads and never installs it), or why it could not check. The outcome and its time are kept in the workspace store (`app-update-status-1.0`), and a result older than 24 hours reads as the last result; until a check, the version reads Not checked yet. Automatic update checks, installers and a signed, verified update channel remain future work.

Implemented complete scoped Tables and Indexes inventories, pagination and CSV/HTML/PDF exports. A reusable-space filter is available for known file-per-table allocations. Per-index fragmentation measurement remains unavailable from these metadata sources.

Parameters uses **Review and apply** for batch edits (up to 25 changes) and **Change…** in the inspector for single-parameter modes. Permission details appear inside the review dialog; the buttons do not show an Unverified badge. Existing confirmation and privilege checks still apply.

**Shipped: Parameters documentation links and a calmer Dump form.** The Parameters inspector is one compact section with a Documentation link per variable (MySQL Reference Manual by server version, MariaDB documentation, Percona manuals for Percona-only variables), generated from saved official pages into `sysvar_docs.py`, and a single Recommendation only when a check flags it. Maintenance > Dump reads the tables of the one chosen database into a searchable list and groups the tool's options as a two-column form. MySQL 8.0 and 8.4 variables are described by the help text they are declared with in the server source (MySQL 9.0+ and MariaDB describe their own). Next: the variables declared outside the generator's source folders (the `admin_ssl_*` family), and refreshing the saved pages when a new MySQL manual ships.

Home shows MySQL and MariaDB news under Recent work: up to six headlines from Planet MySQL (the MySQL team's posts) and the MariaDB Foundation, read by the backend while Home is open and cached for six hours, with a Settings switch that stops every read. More official sources can join `news.SOURCES` once their feeds answer a neutral client; the Oracle MySQL blog feed and mariadb.com's feed refused one in October 2026.


Workspace correctness follow-up (implemented): committed-password privilege
reads, consistent on-open reads, retained account drafts, escaped table/index
statements, measured context menus, cancelled resize cleanup, and analysis
progress across view remounts. Browser verification at 1440x900 and 720x520 in
light and dark remains part of the release gate.

Completed workspace feedback: Plans sample opening, HTML/JSON export and investigation Save, Maintenance reads and jobs, and Cluster/ProxySQL confirmed sends share progress and outcome feedback. A slow sent request can close its sheet while remaining single flight. Browser cases live in `test/action-feedback-batch-c.mjs`; the socket-free reviewed-action unit cases also cover late replies after Close and connection changes.
