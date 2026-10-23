# Changelog

All notable changes to myflames are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Build an unsigned Windows x64 installer of the desktop app (`myflames-<version>-windows-x64-setup.exe`, NSIS, installed per user without an administrator prompt) with `npm run package:win` in `apps/desktop`. The new `Windows desktop` workflow builds the backend with PyInstaller on a Windows runner, packages the installer, runs the packaged smoke and uploads the installer with its SHA-256 as a workflow artifact. It is a preview for testing, not a release asset: quitting force-stops the backend, after a crash a statement already running keeps running until it ends, database clients are found only through PATH, and the installer is not signed. `build-backend.py` now finds the build venv and the Python license on Windows too. The app names its publisher (Vinicius Grippa and the myflames contributors) in the exe and the uninstall entry instead of Electron's default.

### Fixed

- Commands no longer crash on Windows when their output is redirected or piped and contains characters outside the ANSI code page (query text, table names, the audit log): standard output and error are written as UTF-8, as `-o` files already were. SQL piped to `sql` (`-f -` or stdin) and passwords from `--password-stdin` are read as UTF-8 there instead of the code page, so a non-ASCII password is no longer changed on the way.
- ProxySQL Query digests evaluates rule patterns on Windows: the pattern worker's lines end with `\r\n` there, so its ready line was never recognized and every pattern was unknown.
- The error log hint for a MariaDB server on Linux keeps its `/` path when myflames runs on Windows.
- `sql` names the rows in its warning note ("Statement 1: 1 row(s); 1 warning(s)." instead of "None") and `-q` suppresses that note like the others.
- Shortcut hints read Ctrl+K, Ctrl+Alt+R and Ctrl+Alt+I on Windows and Linux instead of ⌘K, ⌥⌘R and ⌥⌘I.
- Windows no longer shows a scroll bar down the whole window: an idle status line hung one pixel below it. The 48 px icon rail hides its scroll bar on short windows (it still scrolls).

## [3.2.0] - 2026-10-23

### Changed

- Rewrite the README around the macOS app, with screenshots; detailed behaviour moved to the guides.
- The macOS app is signed with a Developer ID and notarized by Apple, and ships as `myflames-<version>-macos-arm64.dmg`, so it opens from Finder without Control-click or `xattr`. Check for updates in 3.1.1 finds and downloads the DMG. `npm --prefix apps/desktop run package:mac:release` (`apps/desktop/scripts/package-mac-release.sh`) builds it: the certificate comes from `MYFLAMES_MAC_IDENTITY` and the notary sign-in from `APPLE_KEYCHAIN_PROFILE` or an App Store Connect API key (`APPLE_API_KEY`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER`, which keeps working while the screen is locked), every binary (the bundled Python backend included) is signed under the hardened runtime with only the JIT entitlement, the app and the DMG are notarized and stapled, and the script runs the packaged smoke, checks that Gatekeeper accepts the app, the DMG and the app inside it, refuses a DMG over the 150 MiB update download limit and writes its SHA-256. Local builds (`package:mac`) stay unsigned.

### Fixed

- Processlist shows the server's State in its own column. Ages and measured durations use words ("36 s", "4 min 12 s", "18 d 12 h"); Replication includes both lag tables, inspector signals, summary, lag tile and topology, and the Query editor's ALTER review includes transaction age. Exact seconds, including decimals, are on hover in duration table cells, Processlist's Time in state/Open for/Lock wait facts, Locks' Longest wait/Oldest transaction summary facts and Transactions Idle duration, and Replication's lag/delay/signal facts, lag tile and topology labels. Replication keeps numeric lag sorting, unknown states and hover caveats. Other summary sentences, Locks badges and narrative text, and the ALTER review age use words without an exact-seconds hover guarantee. Settings, sampling windows, thresholds, budgets and progress counters keep their units. Locks' transaction age is "Open for" and its Wait column uses the same words.

## [3.1.1] - 2026-10-22

### Added

- Support Percona Server for MySQL as a MySQL flavor. Percona Server and Percona XtraDB Cluster keep engine `mysql`, so every MySQL rule applies, and are named by their product everywhere a server is named: every view's summary line, the window footer, the connection test ("Connected · Percona Server 8.4.11-11"), Tuning, the Full analysis, Replication's source version (read from the relay log by Percona's release suffix), Cluster, Privileges, the CLI text and the version note. Reports carry `flavor` (`mysql`, `percona`, `mariadb`) and `server_product` (`report-1.0` and `cluster-1.0` schemas list both). What Percona adds is read from the server when it reports it: with `userstat=ON`, Indexes reads `INDEX_STATISTICS` beside Performance Schema and keeps an index Performance Schema shows unused when user statistics saw it read ("Keep: read per user statistics"), Tables shows rows changed per table (inserts, updates and deletes together) and names them for tables without a usable key, with partitioned tables' counts unknown because user statistics miss their range reads (Indexes on Percona puts their indexes under No data when user statistics are the source), and with Performance Schema off Indexes and I/O use user statistics as on MariaDB; Health shows the thread pool's threads, idle threads and (8.4) queued requests under `thread_handling=pool-of-threads`; the slow log check says when `log_slow_rate_limit` logs only a sample, including above a 10 s threshold, following Percona's rule (statements slower than `slow_query_log_always_write_time` are always logged, those `log_queries_not_using_indexes` or `log_query_errors` add are still sampled, and session sampling keeps whole connections); Parameters marks the variables Percona adds with a Percona tag (`vendor: "percona"`, `parameters-1.0` `flavor`). The GA matrix (`scripts/ga-matrix.sh`) adds Percona Server 8.0 and 8.4 and a Percona 8.4 replica with seeded data, workload and saved connections, and `scripts/record-perf-fixtures.py` records a live view read as a replay fixture.
- Add **Check for updates** to Home's version footer and About myflames. It asks GitHub only when clicked, shows progress, then Up to date, the newer version with **Download…** (the existing update dialog), or why it could not check. The time of the last check is kept in the workspace store across restarts (`/api/app/update/status`, `app-update-status-1.0`) and shown in local time; a result older than 24 hours reads as the last result.
- Add `scripts/cluster-matrix.sh`, which starts a MariaDB Galera, a Percona XtraDB Cluster and a MySQL InnoDB Cluster (with MySQL Router) of three nodes each in Docker on 127.0.0.1, with shared accounts, a small `testdb`, saved connections in an isolated home, `fail` and `recover` for one node, and `fixtures` that records each node's cluster state as JSON; healthy, degraded and recovered captures are in `test/fixtures/cluster/` (see `test/README.md`). `scripts/record-cluster-fixtures.py` records one live `myflames cluster` read as the replay fixture `test_cluster.py` reads, with the password from the matrix's credentials file, never printed. Its `proxysql` group adds `mf-proxysql` (ProxySQL 3.0.11; Admin on 127.0.0.1:36032 with a remote admin user, MySQL interface on 36033) in front of Galera (`mysql_galera_hostgroups` 10, 11, 12, 19) and InnoDB Cluster (`mysql_group_replication_hostgroups` 30, 31, 32, 39), with a least-privilege monitor account on both clusters, `mf_admin` as the frontend user, three read/write-split query rules, a few reads so the stats tables have rows, and a saved `proxysql-admin` connection.
- Add Cluster, a place of its own right after Replication, and `myflames cluster` (`cluster-1.0`, `docs/schemas/cluster-1.0.json`; `POST /api/live/cluster`; the read-only MCP tool `cluster_status`). It shows the cluster the connection in use belongs to: MariaDB Galera Cluster, Galera Cluster for MySQL, Percona XtraDB Cluster, MySQL InnoDB Cluster (also read from one of its read replicas, judged by its `read_replica_replication` channel; the group's quorum and failures tolerated are then unknown, with the reason, never claimed), or Group Replication without InnoDB Cluster metadata. Members are shown as the connected server sees them, with address, engine and version, state and, for Group Replication, role: Galera exposes only the connected node's state, so the other nodes are listed in the component without a verdict, while Percona XtraDB Cluster (`pxc_cluster_view`, in the node's own words such as Donor/Desynced and Joiner, the raw value kept in `pxc_status`) and Group Replication name every member's state. One severity scale for every kind, the same in the CLI, JSON, MCP, the place, the sidebar and `--fail-on`: OK with quorum and every expected member healthy; Watch while quorum holds but a member is missing (a Galera node not in the component, an InnoDB Cluster member MISSING or UNREACHABLE), in transition, or one more failure would lose the majority; Problem without quorum or outside a Primary component, for a member in ERROR, when the connected member is OFFLINE or in ERROR (no group size is then claimed, and the metadata and router check-ins are marked as its possibly stale copy), or a MariaDB node whose global `wsrep_on` is OFF while it stays in the component. Findings cover a non-Primary component (a joining node is reported as joining, never as having lost quorum), a node not Synced (with `wsrep_desync` ON said so), fewer nodes than `wsrep_cluster_address` names (only when this node is identified in the list, naming the entries not in the component when they can be matched; a partial seed list or an arbitrator never reads "3 of 2"), failures tolerated (Group Replication by the majority rule over the current view; Galera assuming equal `pc.weight`), flow control paused 10% of the time or more, certification failures, maintenance mode, no quorum, a member UNREACHABLE, RECOVERING, OFFLINE or MISSING from the group, mixed versions and the primary count; `--fail-on problem|watch` exits 1. The Full analysis's Galera check uses the same rules, so it and Cluster agree (see Changed). InnoDB Cluster adds the metadata name and mode, its read replicas (drawn apart from the group) and the registered MySQL Routers with their ports and last check-in (its age and UTC time). The place reads when opened (Refresh and Server's Refresh all read again) and shows a summary line, the findings first when something is wrong (one line each, the reason on demand), tiles, Topology (a still picture: member cards on one group line, read replicas and routers below, a degraded member drawn with a dashed edge, a cut link and its shape; the shared dot plus a state word on cards, in rows and in the inspector), Members (Nodes on Galera) and Routers, with an inspector per member and router; at the 720 px minimum window the cards wrap and the first tab starts above the fold. A report from another connection, or kept after a failed read, is greyed and marks its member "Read here", never "This connection"; a failed read shows one short sentence. Once a read says the connection is not part of a cluster, its sidebar item looks disabled (also while open) but stays focusable and explains why. Percona XtraDB Cluster 8.4 is detected by `wsrep_provider` because its `wsrep_on` is a session-only variable.
- Add reviewed changes to the Cluster place, in a member's inspector only (`POST /api/live/cluster/plan` and `/api/live/cluster/action`, `cluster-action-1.0`; the workspace's `/api/live/cluster` adds each member's `actions`, the CLI and MCP report does not): **Make primary…** on an ONLINE secondary in single-primary mode with no member recovering or unreachable (`SELECT group_replication_set_as_primary('<MEMBER_ID>')`, MySQL 8.0.13 or later; in an InnoDB ClusterSet replica cluster the server refuses and MySQL Shell's setPrimaryInstance is the way), **Stop Group Replication…** on the connected member (it becomes read-only; the name is typed when it is the primary or the last ONLINE member), **Start Group Replication…** on the connected member only while it is OFFLINE (a member in ERROR is offered Stop first) and `group_replication_bootstrap_group` is OFF (recovery may replace its data with a clone, said in the review), switching a plain Group Replication group between single- and multi-primary with the name typed (not for InnoDB Cluster, where MySQL Shell also updates `auto_increment` and the metadata), and on MariaDB Galera **Desync this node…** and **Resync this node…** (`wsrep_desync`, read from the variable, so a donor serving a state transfer or holding a backup lock is offered neither) and **Bootstrap a Primary component…** (`pc.bootstrap=YES`, only on a connected, non-joining node whose component is non-Primary, with the node name typed, its `wsrep_last_committed` and the split-brain risk in the review). `SET GLOBAL wsrep_on` is not offered (it stops replicating writes only from sessions opened later), and Percona XtraDB Cluster and Galera Cluster for MySQL get no change until their sources are checked.
- Add ProxySQL, a place of its own right after Cluster (`proxysql-1.0`, `docs/schemas/proxysql-1.0.json`; `POST /api/live/proxysql`), for a connection to ProxySQL's Admin interface, 2.x or 3.x. It is recognized by `admin-version` in `global_variables` and `SELECT @@version` answering the same, so a MySQL server with a `mysql_servers` table is never taken for it. Servers lists each backend as ProxySQL runs it (`runtime_mysql_servers` and `stats_mysql_connection_pool`: hostgroup, role in its monitor's hostgroups, status, weight, connections in use and free, errors since start, queries, ping) with the configuration row it comes from (a monitor may run it in another of its hostgroups; a writer above `max_writers` reads as a standby writer); Hostgroups, Query rules (with hits) and Query digests (the 100 with the most total time) follow, each with an inspector. Tables are read by column name from the client's XML output, so SQL NULL never reads like the text 'NULL' and a table or column a version lacks is unknown, never an error; a stats-only account sees what runs and is offered no change. A table counts as missing from the version only when every copy (memory, runtime, disk) answers "no such table"; any other copy that cannot be read makes what LOAD or SAVE would change unknown, and that module offers no change. An active rule that sends to a hostgroup with no server is judged from the rules ProxySQL runs, not the configuration in memory, and is unknown when they cannot be read. It reads when opened on screen, once per connection; another connection's report is greyed with its changes off; once a read says the connection is not a ProxySQL Admin interface, the sidebar item looks disabled for that connection only. Changes in a row's inspector set a server ONLINE, OFFLINE_SOFT (not offered for a server the Galera monitor manages: on 3.0.11 it put the server back in traffic within about 20 seconds) or OFFLINE_HARD, change its weight (0 to 10,000,000) or move it to another hostgroup (only a row in no monitor's hostgroups: on 3.0.11 a row moved out of the Group Replication monitor's hostgroups stayed ONLINE in its reader hostgroup too), or enable or disable a query rule. Each edits the configuration in memory and loads it to runtime in one unit (`UPDATE ...` then `LOAD MYSQL SERVERS TO RUNTIME` or `LOAD MYSQL QUERY RULES TO RUNTIME`), refuses while other changes wait in memory, and is not saved to disk: **Save servers to disk…** and **Save query rules to disk…** (offered after the change and on the Configuration line, with **Load … to runtime…**) are separate reviewed changes that list every difference between memory and disk or runtime first (`POST /api/live/proxysql/plan` and `/api/live/proxysql/action`, `proxysql-action-1.0`). LOAD and SAVE are sent without the `/*myflames:proxysql_op*/` tag, because Admin matches those commands on their whole text.
- Every Cluster and ProxySQL change follows the write contract (`myflames/reviewed_writes.py`, `ActionReview.tsx`): the review names the statement verb, the target, the connection with its address and type, the facts and the undo, and shows the statements exactly as sent; every one is a risky write, so Production and Development both ask and Read-only refuses (audited, before any connection opens); destructive ones need the target's name typed (OFFLINE_HARD, the last ONLINE server of a hostgroup, stopping the primary or the last member, a mode switch, `pc.bootstrap`); the review token binds the whole plan, so a changed value, target or statement is refused, and the backend reads the target again and sends nothing when the statements or the reviewed identity changed (ProxySQL: every row of the module copies involved; Galera bootstrap: the component's members) or the saved policy changed since the request; the statements go as one unit on the client's standard input with `--comments --binary-mode`; a server refusal, a partial apply (the client's "at line N") and a lost reply (Outcome unknown, never sent again) are each reported and audited, and only an error the client printed with its number counts as a refusal: a client that ended without one (killed, crashed) is Outcome unknown; Cancel and Escape record a declined attempt, and a confirm the workspace stops itself (saved connections unreadable or under another policy) is recorded as refused (`/api/audit/declined` takes `reason`).
- Add orders and filters to the ProxySQL place's Query digests (`POST /api/live/proxysql/digests`, `proxysql-digests-1.0`, `docs/schemas/proxysql-digests-1.0.json`, `myflames/proxysql_digests.py`): **Slowest** (average time), **Most time**, **Most frequent**, **Highest rate** (executions per second between a digest's first and last execution) and **Most rows sent**, the top 10, 25, 50 or 100, and filters by hostgroup, schema, user, digest text, one digest, or the digests a rule applies to. Each choice is one bounded Admin statement (ORDER BY and LIMIT run in Admin), read only while the tab is on screen; typed text is sent as a hex literal, so no statement ever names `stats_mysql_query_digest_reset`, whose read would reset the digests. A narrow window offers the order as a menu.
- Cross check query rules with query digests in the ProxySQL place: each digest shows **Ran on** and **Rules send to**, where the active runtime rules (flagIN, flagOUT, apply, negate, CASELESS, query processor iterations), fast routing and the user's default hostgroup send it, evaluated by myflames with Python's `re` and labelled so (ProxySQL uses PCRE or RE2, `mysql-query_processor_regex`). A different hostgroup is flagged as a mismatch with its possible reasons (a transaction kept on one hostgroup, a `/* hostgroup=N */` comment, rules loaded later); `match_pattern`, which tests the query text a digest does not keep, is approximate, and a digest on the hostgroup of such a rule is "Maybe rule N"; the query cache (hostgroup -1) and statements ProxySQL answers itself (such as `select @@version_comment limit 1`) are named. Each rule shows its hits, with a rule that has none marked, and the digests it applies to; a rule and a digest open each other. Patterns are tested in a separate process with two seconds per pattern, so a pattern that backtracks without end leaves its rule unknown instead of holding up the app; when the rules go on further than myflames follows, or fast routing could not be read, a different hostgroup is not judged.
- Explain Safe updates in the Query editor: an ⓘ beside the box in Run options says in plain words what `sql_safe_updates` refuses (an UPDATE or DELETE whose WHERE cannot use an index, unless it has a LIMIT, with error 1175 and no row changed; on MariaDB also a DELETE without a WHERE, even with a LIMIT; SELECT is not limited), that it is on by default for Production and off for Development for the tab's session, how to turn it off for the tab, and links to the MySQL and MariaDB documentation, opened in your browser. It opens over Run options without closing it, and Escape closes the explanation first.
- Add a menu to each Query editor tab (right-click, or Shift+F10 or the context-menu key on the tab): **Rename…**, **Duplicate** (a copy right after it with the same SQL, parameters and row limit, in its own session), **Close** and **Close others** (one tab at a time; it skips a tab with a running statement and stops at one whose transaction is open, or may be, to ask how to close it). Renaming, also by double-click, F2 or **Rename query tab** in the command palette, trims the name, refuses an empty one with a message, holds at most 64 characters (cut without splitting an emoji), cancels on Escape, and survives a restart; a long name ends in an ellipsis with the whole name in the tooltip.
- Add **Format SQL** to the Query editor: the indent button in the toolbar, Shift+Option+F (Shift+Alt+F), or the command palette lays out the selection or the whole tab with sql-formatter 15.9.0 (MIT, MySQL or MariaDB dialect, two-space indent), loaded on first use, as one undo step that never runs SQL. Keywords go to upper case only where the server ignores case (words reserved in both MySQL and MariaDB, each statement's first word, statement openings such as SHOW FULL PROCESSLIST); names keep their case. The result is compared with the original token by token under both backslash modes and with MariaDB's `[name]` (sql_mode=MSSQL) read as one quoted name, and any change beyond white space and keyword case (a string, a quoted name, an executable comment or optimizer hint, a line comment that would swallow code, the space in `COUNT (*)`) refuses the format with the line named and the text unchanged. DELIMITER blocks, stored program bodies, client commands, unclosed quotes, names in square brackets and statements the formatter cannot read are left as written and named in the message; a selection made, changed or cleared while the formatter first loads changes nothing and says so.
- Show MySQL and MariaDB news on Home: up to six headlines from Planet MySQL (the MySQL team's posts) and the MariaDB Foundation, read by the backend over HTTPS only while Home is open, cached for six hours and shown from the cache with its age when offline (`/api/news`, `news-1.0`). Turn it off in Settings with **Show MySQL and MariaDB news on Home**; nothing is read while it is off.
- Link every parameter in Server > Parameters to its entry in the official manual: the MySQL Reference Manual for the server's version (8.1 to 8.3 use 8.4's, 9.0 to 9.6 use 9.7's; a variable that manual leaves out links to the nearest one that has it, and the link names that manual), MariaDB's documentation, and Percona Server's and Percona XtraDB Cluster's manuals for the variables a Percona server adds. The inspector shows **Documentation** and the row menu **Open documentation**; links open in the default browser (the desktop app now also allows `docs.percona.com`). `parameters-1.0` adds `documentation` (`{url, source}` or null) to each item and the report's `flavor`. The locations are generated into `myflames/sysvar_docs.py` by `scripts/gen_sysvar_docs.py` from saved official reference pages, with every anchor checked against the saved pages; nothing is fetched at run time. A live MySQL 8.4.11 server links 621 of its 623 parameters, MariaDB 11.4 657 of 674.
- Maintenance > Dump lists the tables of the database you choose: picking exactly one database reads its tables (`/api/live/catalog` with `stats: true`, which adds each table's engine, the server's estimated rows and its size to `catalog-1.0`, 200 at a time with Load more tables) into a searchable list, All tables by default. A table dump sends exactly the ticked names; a reload that no longer lists one blocks the preflight, and the dump preflight adds a `tables` check that fails for a name the schema does not have. The free-text Tables field and the "Tables of one database" scope are gone: choose Databases, then one database.

### Changed

- Keep replication recovery controls stationary and focused during preflight, focus Cancel when the review opens, and return focus to the initiating control when it closes.
- Extend action feedback to Plans and Maintenance: busy controls retain focus, reads show elapsed progress with Stop waiting and retry, and exports name the saved file. Slow lifecycle starts and Cluster/ProxySQL confirmations can close their sheets while the request continues; late outcomes remain visible without resending. ProxySQL Refresh now offers Stop, retry and its completion time.

- From 651 to 900 px, use a 48 px sidebar icon rail with tooltips and status dots; the toggle opens the full sidebar as an overlay, and Open plans (N) opens the plan list.
- Show status as a dot plus a word, including OK in Health and InnoDB rows and Cluster cards; summary words are OK, Watch, Problem, Not judged and Unknown, with compact navigation markers using neutral counts and accessible state text.
- At 900 px and below, put summary tiles in one compact strip and fold the facts line into Details.
- Fit Tuning categories without an empty grid cell and mute bars when fewer than half the checks are measured, with coverage such as "100% of 1 check".
- Size key/value labels to their longest text and stack values below labels at 900 px and below.
- Put Query results Export on the results tab row.
- Show the reported server version wherever a place has a window footer, and truncate flexible text before write-safety words.
- Draw visible Esc hints as a keycap.
- Open Replication channel and Parameters details in the side inspector while keeping their lists available.
- Open ProxySQL server, hostgroup, query rule and digest details in the 320 px side inspector (a bottom sheet below 1100 px) while keeping the list mounted and the row selected; give Empty hostgroups a neutral dot, count all hostgroup rows on the Hostgroups tab and tile ("5 with servers · 8 in monitors"), show "connected now" under Client connections, and style Load to runtime and Save to disk as secondary buttons with a reason tooltip when memory matches runtime or disk.
- Wrap long Replication GTID sets after commas.
- Offer Save N open plans as an investigation… when plans are open and hide the Investigations empty list.
- Keep unread Galera node state on one compact line and offer Use &lt;connection&gt; when a saved connection matches the node.
- Keep each action in one place in Tuning, Database diagram and I/O: Scorecard owns Run full analysis, the analysis and diagram use Refresh, and I/O keeps its controls in the view toolbar.
- Draw execution tree self time on a common baseline under Self of total in the Python and TypeScript renderers.
- Server actions that open a statement in the Query editor (Show index, Show create table, Processlist's Open in Query editor) open it in a new tab, as Top statements already did; Parameters' Session change still goes to the current tab, because it changes that tab's session.
- The Full analysis's version support check no longer applies MySQL's lifecycle dates to Percona Server: it is unknown there, with MySQL's date shown for reference. Its text names the product ("MySQL 8.0.46", not "mysql 8.0.46").

- Server > Parameters' inspector is one compact section: a one-sentence description (on MySQL 8.0 and 8.4, which report none, the help text the variable is declared with in the MySQL source, as MySQL 9.0+ reports it; generated into `myflames/mysql_sysvar_help.py` by `scripts/gen_mysql_sysvars.py`), the Documentation link, and Current value (Value, Default, Source, Changes in words instead of Apply type and Scope, Allowed values, Persisted, and the checks that cover it); a Recommendation follows only when a check flags the parameter, with the most severe check's reason, the suggested statement and option file line, and its caveat in one line. The per-check "Explanation and recommendations" blocks, which repeated the description, are gone, and the actions moved to the bar under the parameter's name.
- Maintenance > Dump is a two-column form with each label left of its control, and the tool's options are grouped as Performance (Threads with its unit, Chunking, Chunk size, Rate limit per thread), Contents (Include Triggers, Stored routines, Events), SQL output (mysqldump and mariadb-dump) and Compression, with each checkbox on its label's line and help under its field. The Options heading summarizes what differs from the tool's defaults, the Tool list names the SQL dump tool for the server, and the encryption note is one line with Why. `dump_options.capability_catalog` adds `group`, `row` and `groups`, and `encryption.summary`; the allowlist, validation and the reviewed plan are unchanged.
- Move Query digest out of the Query editor into a place of its own, right below Query editor in the sidebar's SQL workspace group (and in the command palette); the editor loses its Editor | Query digest switch, and Query digest itself is unchanged except that it reads only while on screen (once per connection; Refresh, a filter or the row count read again). Its Open in editor and Explain still open the Query editor. It keeps the editor's height, so its list and inspector scroll on their own, and Start fresh or another window's newer workspace starts it over.
- Move Database diagram out of Server > Schema into a place of its own in the SQL workspace group, after Query digest. Home's Database diagram card, the command palette, links and a workspace saved on the old Server view open the place; Server's Refresh all still reads it once a database is chosen, and opening the place while that read runs keeps its answer.
- Top statements and Error log read when opened on screen, once per connection, like every other view: no Load button. Error log reads MySQL's `performance_schema.error_log`, or on MariaDB the verified local log file, and otherwise says why it is not available.
- Replication reads **Not a replica** for a server with no asynchronous replication channel, a calm state with the reason, never Unknown; once read, its sidebar item is greyed with "This connection is not a replica" and stays focusable, like Cluster. `replication-1.0` leaves Group Replication's own channels (`group_replication_applier`, `group_replication_recovery`) out of `channels` and names them in the new `group_replication_channels`, so a group member with no other channel has `replica: false`. A server that is not a replica gets `cluster_hint` (`galera` or `group_replication`, from `wsrep_provider` and `group_replication_group_name`) and its message names Cluster (`myflames cluster`); the place offers **Open Cluster** for a group member or a Galera node. Unknown now means only that the status could not be read or was not allowed.
- Add a context menu to Processlist rows (right click, Shift+F10 or the context menu key) with Show plan, Open in Query editor, Open in Locks, filters, Copy query, Copy connection ID, **Kill query…** and **Kill connection…**. Each Kill opens the single Kill confirmation with that KILL chosen (exact statement, identity re-read, write policy, privilege, audit); an item that cannot run stays in the menu, focusable, with its reason. After a Kill (Processlist and Locks) the answer shows at once while the list is read again; the headline reads Done, Already ended, The server refused, Outcome unknown (a request without a reply, never "not sent") or Nothing was sent, and Kill sessions… results no longer read "Nothing was sent." after a successful bulk kill.

- Reviewed Cluster and ProxySQL changes now show what happens after Confirm in the same sheet: **Applying…** on a button that cannot be pressed twice, the step (reading the target again, sent and waiting for the server's answer with the seconds waited, reading the result back) and each statement's state (Waiting, Sent, Done, Failed, Not run, Not sent, Outcome unknown), from the backend's progress of that send (`POST /api/live/action/progress`, `reviewed_writes.Progress`; phases and states only). A statement is Done only after the server answered. The sheet then shows the outcome, **What changed** (rows of before and now, read again after the write; the result's new `states`, `effect` and `checks`), the Audit log record with **Open the Audit log** (which selects it), and for an unknown outcome **What to check**; Close leaves the result panel in the view. Plans carry `timeout_s` and `checks`.
- Every action in the workspace now reacts in the frame of the click and says what it is doing and what happened. The button that started it shows a spinner and a present-tense label (Refreshing…, Testing…, Saving…, Applying…, Exporting…), keeps its width and keyboard focus, and ignores a second click; after a second the line beside it counts the seconds; when the server is slow to answer it says so and offers Stop, Stop waiting, or, on a confirmation sheet after ten seconds, Close (the request goes on and its outcome shows in the view); the result stays on screen (Updated at …, Headlines updated., Exported … as …, the error with Try again), and one polite announcement covers the start and the end. Converted: every Server and Workload view's Refresh, Cluster, Tuning's Run full analysis, Refresh all, Users and roles, Home connections (Test, Save, Delete, Forget password) and news, Investigations, Import plan, the Query editor's first run, results export, schema browser and history, Locks' Kill, Kill sessions…, the reviewed SQL actions of Indexes, Tables and Instruments, table data Apply and the Audit log export.
- Refresh all names the views it is reading ("Refreshing 4 of 17 · Waits, I/O…") with the seconds, and announces only its start and its end.
- A Kill's answer in Locks names the connection it was sent on, and offers no Refresh, when another connection is in use (a sheet closed before the reply, or a switch since); a Kill sessions reply that arrives after its sheet was closed shows in a Kill sessions dialog opened since, without closing it, and reaches Processlist when that dialog closes (also when the dialog was closed because the connection in use changed while the KILLs waited for their reply).
- Failed update checks report a short reason and are never shown as up to date (a certificate that could not be verified reads "a secure connection to GitHub could not be verified", not offline); the update dialog uses sentence case. Check for updates and the update download use no proxy from the environment, follow redirects only to the official GitHub addresses and never read a redirect's body, and the check reads at most 1 MiB and answers within 10 seconds in all (a slow DNS lookup, connection or certificate read ends in the background, and no second check starts while it runs).
- The desktop app saves a downloaded update in the user's Downloads folder (`MYFLAMES_DOWNLOADS_DIR`), not in its isolated backend home.
- Rename the Processlist **Mass kill** button to **Kill sessions…** and its dialog to Kill sessions, with the same exact KILL review, confirmation, policy re-check and declined audit; the bulk result message reads "Kill sessions finished: N killed".
- Explain the Tuning grade where it is shown: a line beside it says why ("Capped at C: 1 check has a problem (89 is in the B range)" or "Score 84 is in the B range, 80 to 89"), an ⓘ next to it lists the scale (A 90 and above, B 80 to 89, C 70 to 79, D 50 to 69, F below 50), the cap at C and when there is no grade, and the Tuning ⓘ describes the score and the grade. Thresholds come from `GRADE_SCALE` in `tuningRules.ts`.
- Remove the Local badge from the top bar; About myflames now has one privacy line naming what the app contacts and when (your database servers, official release pages when you check for updates, and news feeds when shown and turned on).
- Move Privileges out of the top bar: the connection picker's menu has Account and privileges…, which opens a sheet with the connected account, active roles, grants, denied and unverified actions and Refresh privileges; the command palette opens it as Account and privileges.
- Replication shows each source's engine and version (Topology source node, the connector's details, the inspector, `myflames replication` text as "Source version: 9.7.2 (relay log)", and `source_version` in `replication-1.0`): read from the source's format event in the replica's relay log (needs REPLICATION SLAVE on MySQL), else through a saved connection with the host and port the replica reports (its own saved password; accepted when its server ID, and server UUID when the replica reports one, are the source's; for a localhost or loopback source address (127.0.0.0/8, ::1 in any spelling) opened only when the replica itself is on this computer, reached at a loopback address and reporting this computer's host name, so a replica reached through an SSH tunnel never logs in to a server on this computer, and accepted only when it runs on the replica's host; "only its server ID could be compared" on MariaDB), else "Version unknown" with the reason and a hint to add a saved connection. A saved connection is opened at most once while myflames runs, success or failure, until it is edited, also after its result is no longer kept (the source then says "Not checked"), so a stale password never logs in again on every read; one read of a saved connection runs at a time, and after 1,024 attempts myflames opens no more until it restarts; each source's login and read get 3 seconds and all saved connections of one read 5 seconds, saved password lookups included; nothing is sent after that, and the rest say "Not checked".
- Float the Server and Workload list of views by default; a labelled count badge on the chevron names other views needing attention, while the title reflects its own view. Dock keeps the list beside the view and moves focus to the top bar's Float control.
- Home keeps its starts on one short row and preserves connection type tags (Prod, Dev, Read-only) in the compact table at the 720 px minimum window.
- The Full analysis's Galera check (`checks.wsrep`, also in `myflames report` and the MCP `server_report`) now uses the Cluster place's rules. A Primary component of one or two nodes is Watch (it cannot survive another failure), and so are `pxc_maint_mode` other than DISABLED and fewer nodes than `wsrep_cluster_address` names (when this node is identified there). A MariaDB node whose global `wsrep_on` is OFF while it stays in the component is now a Problem; 3.1.0 left the check out. `report --fail-on watch` (or `--fail-on problem` for `wsrep_on` OFF) can therefore fail on servers that passed in 3.1.0.
- The command palette lists commands whose name contains every typed word first, then those found only by a keyword or their group, each in the usual order: typing innodb opens the InnoDB view, not the Cluster place, and health opens the Health view, not Tuning.
- Production connections are purple and Development connections teal (tags, the picker dot and the window footer), so a Production connection never reads as a problem: red now only means Problem and green only OK.

### Removed

- Remove the optional 3D view of the B+tree lesson and its 2D | 3D switch from Learn. The 2D lessons are unchanged; the 3D view continues as a separate project.

### Fixed

- Report "Sent; could not verify" when a Kill is accepted but its verification read fails, including bulk Kill query and its audit record. An interrupted bulk verification keeps the same unverified message.
- Preserve MariaDB identity in connection tests when only the server's version comment identifies MariaDB, including a plain `11.4.13` version.
- Users and roles retains late account-change results and generated passwords on their original connection until dismissed, including after closing a slow Apply sheet, switching connections or leaving the view. Later writes preserve unacknowledged passwords; follow-up reads check the active connection after each wait.
- A late Query editor reconnect no longer treats a password being typed in the connection picker as committed. Privilege reads wait for an explicit password commit on blur or Enter.

- Resolve Processlist's pending Bulk Kill note when a late policy check refuses the request, including after Close or a connection change. Policy-read failures now say nothing was sent and do not claim an unknown execution or execution audit record; lost replies after sending remain unknown and are never retried.
- Preserve Percona Server and Percona XtraDB Cluster names in the Query digest footer and text export for both statement digests and slow-query reports.
- Target the Settings dialog itself when measuring its opening, so unrelated menus and popovers cannot satisfy the performance runner's readiness check.
- Remeasure refreshed table rows when returning from full-width details, even at the same window width, so larger numbers fit their columns.
- Invalidate cached and pending ProxySQL digest reads after reviewed changes, then reread when Query digests is visible so routing reflects the current rules and hostgroups.
- Keep Cluster and ProxySQL pending changes visible after closing the review sheet while an inspector is open. Correct the action-feedback tests to honor HTTP 503 scenarios and to fail Refresh all reads without a scenario ReferenceError.
- Processlist now keeps pending Kill progress visible after Close, shows connection-aware outcomes before rereading, and lets Kill sessions own its dialog lifetime and outcome focus. Parameters change/reset and batch reviews can close while awaiting replies; replication preflight, skip/start and loop Stop show named busy states and elapsed time. These views expose Stop and Try again for reads, and parameter/replication sends distinguish explicit refusals from unknown outcomes without retrying writes.
- Show action progress in Top statements, Error log, Query editor and Database diagram. Keep busy editor controls focused, show Stop and session-operation elapsed time, distinguish unknown transaction outcomes, and allow a stalled diagram import to be closed without replacing the current diagram.

- Show Not part of a cluster with Open ProxySQL on a ProxySQL Admin connection; show the password hint only for Access denied.
- Explain a Query editor tab's connection mismatch, disable Run, Run all, Explain and Run analysis until its session closes, and label older results From &lt;connection&gt;.
- State the Error log read limitation once and keep copyable commands apart from the explanation.
- Format Parameters Value and Default consistently.
- Hide Learn's empty Phases panel.
- Separate Database diagram relationship lines at crossings so they do not appear joined.
- Successful Kill results consistently begin with "Done." before the server's message in Locks, Processlist and Kill sessions. Browser expectations follow Try again for failed reads, focus-preserving busy buttons and Users and roles' automatic first read followed by Refresh.
- Keep partitioned-table indexes out of Unused on MySQL, MariaDB and Percona Server. Check partitioning only for candidate tables in bounded batches; put partitioned or unresolved candidates under No data with a reason, and suppress DROP and visibility SQL across index lists and inspectors.

- ProxySQL digest routing shows Not judged and names an unread runtime variable when the rule walk depends on it, instead of reporting a false match or mismatch.
- Completed Cluster and ProxySQL actions close their sheet after a connection switch and offer no follow-up change on the replacement connection; the Audit log keeps the attempt.
- ProxySQL digests with separators in schema, user or client names keep separate row selections and rule links.
- ProxySQL digest hash filters accept lowercase and mixed-case hexadecimal hashes with the same results as uppercase hashes.
- Use TCP for host-and-port connections, including localhost, across reads, writes, editor sessions and lifecycle tools; keep Unix sockets explicit.
- Audit bulk Kill policy refusals, expired reviews, credential lookup failures and connection errors after validating every target.
- Filter slow-log executions by duration before applying the limit, preserve source failures in notes, and report unmeasured counters as null.
- Cap SHOW FULL PROCESSLIST statement text at 16,384 Unicode characters while preserving truncation indicators and the 10,000-session report cap.
- Share server identity and final process-list reads across bulk Kill requests, remove per-target settle delays, and retain every target identity and transaction check.
- Return an unavailable Error log report with file-viewing guidance on MySQL versions before 8.0.22.
- Require write confirmation for HANDLER OPEN because it holds a metadata lock until HANDLER CLOSE or the session ends; READ and CLOSE remain reads.
- Replace vacuous discovery tests with assertions for slow-log results, duration filters, source errors and the SQL ordering of top statements.
- Bound lifecycle control-query output to 64 MiB on stdout and 1 MiB on stderr, with client cleanup on overflow.
- Detect the editor session engine from both version and version_comment so a customized MariaDB version string retains MariaDB behavior.
- Set explain_json_format_version only on MySQL 8.3 or later and give MCP callers the actionable MySQL 8.4 requirement for JSON EXPLAIN ANALYZE.
- Bound update downloads by an overall deadline and open the validated release page after a direct-download failure.
- Refuse and audit Kill requests for Group Replication data-stream connections and expose the reason in session reports, including the truncated `Group Replicatio` command returned by MySQL process-list tables.
- Commit the session password on blur or Enter before refreshing privileges; password keystrokes send no login attempts.
- Preserve Kill sessions regex escapes and whitespace; invalid patterns match nothing and show their reason.
- Read Users and roles on open and save its table column choices.
- Show Not judged with the reason when account metadata is unreadable, with one Details column and compact account names.
- Ask Review changes, Discard or Cancel before selecting another account with staged grant edits.
- Share escaped identifier and literal builders across Indexes and Tables reviewed statements.
- Count Refresh all sections without constructing requests on every render, and remove unused badges and ineffective command memoization.
- Remove the unused ReportView and unreachable Sidebar query/session branches.
- Measure row context menus after rendering, preserve copy actions and keyboard focus, and share the menu with TableData.
- Correct Server and Workload read-on-open guidance, Refresh all scope, and Processlist links.
- Keep Parameters and Instruments reports greyed after an on-screen connection switch until Refresh.
- Forward cancellation to Waits, InnoDB, Locks and Processlist API requests.
- Apply only the newest saved-connection response to the picker and safety footer.
- Stop inspector, table-column, schema-browser and editor resizing on pointer cancellation or lost capture.
- Use theme tokens for session, table and editor colors and label lock rows with accessible icons.
- Preserve the latest Workload settings when replication loop activity changes.
- Keep Full analysis elapsed time across Tuning remounts and use the shared Export menu for both exports.

- The Query editor no longer stops with "Maximum update depth exceeded" when its text is replaced outside typing (Format SQL): the editor's height measurement no longer queues an unchanged value on every render.
- Kill sessions… no longer reports "Nothing was sent." after it ended connections; it reports Done, The server refused, Could not end it or Nothing was sent from the server's counts, and preparing its review no longer says "Terminating sessions…".
- A reviewed SQL action (Set invisible…, Optimize…, Analyze…, Instruments Apply) refused by the server with an error is shown as failed in the server's words, not as an unknown outcome with Stop.
- Table data Apply refused by the server before it ran is shown as failed in the server's words; only an Apply with no reply checks its outcome as unknown.
- A confirmation sheet whose request never answers no longer traps the window: after ten seconds Cancel becomes Close (Locks' Kill, Kill sessions…, reviewed SQL, table data Apply, Users and roles Apply, connection and investigation Delete).
- A double click on Refresh all no longer stops the run it started (Stop takes clicks after 600 ms), and a fast double click on Refresh news no longer sends two reads.
- A Kill's answer in Locks shows at once; the sessions are read again after it, with a line saying so, instead of the answer waiting for that read.
- The Query editor's first run in a tab shows Running… and its seconds in the results area instead of "No results yet".
- Preserve recent SQL edits during reload or quit, keep filtered Kill sessions matches visible after scrolling, align newly shown editor highlights and completion menus, measure columns on later table pages, and retain fractional parameter bounds.
- Keep popovers above the window footer: an ⓘ panel opened near the bottom of a 720x520 window ended under the footer, which hid its last lines; it now stops at the footer's top edge and scrolls.
- Replication Topology: the connector's details popover is no longer cut off below the map.
- HTTPS reads (Check for updates, the update download, tool update checks and Home news) verify certificates in the packaged macOS app on a Mac without Homebrew's OpenSSL: its bundled OpenSSL only looked for certificate authorities at the build machine's paths, so every read failed verification there. When OpenSSL finds none, myflames now also trusts the roots macOS trusts: certificates from the system root and System keychains that the system, admin or user trust settings list as an unrestricted trusted root and no domain denies, read with `/usr/bin/security`; intermediates and identities in those keychains are left out. Nothing is added when any trust domain cannot be read, so a Never Trust choice is never dropped, and a failed read is tried again after a minute instead of turning the fallback off until the app restarts. When `SSL_CERT_FILE` or `SSL_CERT_DIR` is set, myflames uses only what they name and adds no macOS roots (`myflames/tls.py`).

## [3.1.0] - 2026-10-20

### Added

- Add Server > Overview > Error log and `myflames error-log` (`error-log-1.0`). On MySQL it reads `performance_schema.error_log`, with level and text filters, rows from the previous run, the most frequent error codes and the ring buffer's state. On MariaDB, which has no SQL access to its error log, it reads the log file only when the server runs on the same computer and its identity is verified; otherwise it says where to read the log.
- Add Tuning, a place of its own in the sidebar (palette keyword "mysql tuning"; never a Server view), with a Scorecard tab and the Full analysis as its second tab. The Scorecard scores 19 checks from the statuses Health, Parameters, Tables and the Full analysis already decide, in five weighted categories: Buffer pool and redo (30%), Queries (25%), Connections and threads (20%), Tables and caches (15%) and Security (10%). A measured check counts 100 when OK, 50 to watch and 0 for a problem; a check that was not measured is shown with the reason and the view that measures it and never counts, and the summary says how many were measured. The score is provisional, with no grade, until at least half the checks and one in each category are measured, and a problem caps the grade at C. Only reports of the connection in use are scored; another connection's stay greyed. Each check shows the measured value and the evidence first, then the rule and what to do: statements to copy or open in the Query editor (`SET PERSIST` on MySQL, `SET GLOBAL` plus an option-file line on MariaDB, `SET SESSION` for per-session limits, version-gated), never run from the page. The Full analysis tab shows the full report with its export. The scorecard exports as HTML (every server string escaped), Markdown or JSON.
- Add `health_metrics` to `report-1.0` (`myflames report --json`, `/api/live/report`): every Health metric of the window with its status, ok, info and unknown included, so a consumer can tell a measured OK metric from one that was not measured (findings keep only watch and problem metrics). Optional in the schema; older reports omit it.
- Package an unsigned Apple Silicon macOS app with the Python engine and workspace bundled. It opens from Finder, discovers installed clients in standard macOS locations, and keeps app data separate from the CLI. Include LICENSE and NOTICE in the bundle and verify packaged startup without system Python.
- Browse complete scoped table and index inventories with pagination and CSV, HTML and PDF export. Filter known file-per-table reusable space without treating it as index fragmentation. Preserve unknown size values instead of deriving false percentages.
- Give Home an application version, release link and direct SQL, Server and diagram entry points. Group the Query editor and Plans under SQL workspace, and refresh the app mark with saturated colors on white.
- Show Mermaid ER imports on the in-app diagram canvas with a direct View Mermaid action.
- Add a Diagram | Mermaid switch to Database diagram. The Mermaid view renders the diagram beside the code (`mermaid` 11.17.2 loaded on first use in separate chunks, `securityLevel: "strict"`, light and dark appearance, zoom in, zoom out and fit, inline render errors) with Copy, Download `.mmd` and export notes.
- Refresh every Server section on request, including unopened sections, with bounded reads, preserved scopes and a separate Full analysis phase.
- Replace Server > Configuration with Parameters, modelled on an AWS RDS parameter group (`parameters-1.0` on `/api/live/config`, `myflames/parameters.py`, and an 817-variable catalog in `myflames/mysql_sysvars.py` generated by `scripts/gen_mysql_sysvars.py`). Browse every global parameter in one table with search and Show filters (All, Modified, Dynamic, Static, Recommendations, Pending), edit supported dynamic and persistable static values inline, preview staged changes, and review and apply up to 25 changes in one batch with per-row re-reads and audit records, or use the inspector for Edit value, Reset to default, single-parameter Change… (including This session only) and Reset persisted value….
- Start an Electron desktop development app around the existing Python engine and workspace, with isolated app data and restricted renderer access. Signed installers and payment integration are not included.
- Integrate the desktop design into the working workspace: compact neutral navigation, open query documents, a command palette, and full-width Server details with Back navigation. Current desktop delivery targets macOS.
- Complete SQL using keywords and bounded, connection-scoped schema, table and column metadata, with simple alias support and keyboard controls.
- Browse table data and stage inserts, updates and deletes for exact SQL review. Apply supported InnoDB batches after checking row identity, current values, table metadata, write policy and privileges. The review explicitly includes the temporary table WRITE lock and its blocking effect. Unsupported tables remain read-only.
- Rebuild Database diagrams from the connected server with a database picker, retained positions and confirmation before replacing an imported diagram.
- Link detected tools to official downloads and show verified release updates, with bounded checks, caching and explicit unknown states when verification fails.
- Choose backup folders through the host's native dialog and configure supported dump compression, chunking, rate limits and object options. Local file encryption remains explicitly unsupported.
- Open Top statement details across the available width and prepare reviewed SELECT projections from supported write statements without executing those writes.
- Add reviewed Optimize table actions and an interactive Instruments picker (`instruments[]` in `instruments-1.0`). Browse Features, Instruments, Consumers, and Actors and objects tabs; filter and select up to 2,000 instruments or consumers with dependency checks; and run Enable, Enable without timing or Disable in batches of up to 500 names per UPDATE through the reviewed SQL runner, with copy-only option file lines for Keep after restart.
- Restyle Learn and standalone `myflames teach` pages to match the workspace: a view toolbar (Search lessons, Family filter, Start learning), a one-line summary ("23 lessons, 16 in the learning path"), grouped rows by family with "Step n of 16", a compact open-lesson toolbar, `data-embedded="true"` header and footer hiding, light and dark tokens (`prefers-color-scheme`, `data-theme`, `?theme=`, `?embedded=1`), semantic `--svg-*` drawing colors resolved by `anim.cssVar` and `anim.palette`, colon lesson titles, and a standalone catalog filter.

### Changed

- `/api/investigations/list` returns one page of summaries (`investigation-list-1.0`: name, notes, tags, baseline and plan names, never the plans; `limit` 1 to 50, `cursor`, `search`) instead of every saved bundle, and the new `/api/investigations/get` returns one bundle for Open, Compare to baseline and Export, so the files they write are unchanged. Home reads 6 rows and Library pages of 25 with Load more and a server-side search; with 50 saved investigations of 2 MiB, Home's request fell from 104 MiB (0.45 s) to 1 KB. Summaries are kept in step with records written by older myflames versions on the same database.
- The workspace's `/api/analyze` returns `chart-data-2.0` (`docs/schemas/chart-data-2.0.json`), which stores each analysis message once in `highlight_messages` and refers to it from each node by `highlight_id`; a grouped full-scan warning names every table and applied to each of them, so the chart data grew with the square of the scan count (800 scans: 11.3 MiB to 1.05 MiB; 1,600: 44.3 MiB to 2.1 MiB). HTML reports and `explore` pages still embed `chart-data-1.0`, and the chart runtime draws both identically.
- `/api/analyze` sends the plan's SQL once (`query`, `analysis.query.raw`) instead of in each teach hook at the top level and in the sidecar; an 801-operator plan with 71,769 characters of SQL fell from a 122.6 MiB response to 2.7 MiB (with the chart data change). Export JSON still writes `query_sql` on every teach hook, so the exported analysis equals the CLI sidecar.
- Plans fills the window in every chart view: in Flame graph, Execution tree and Bar chart the chart and the inspector now run to the bottom of the window like Visual Explain, Visual Explain's canvas reaches the bottom edge, and a plan with findings opens the inspector on Findings ("Choose an operator in the chart or the Operators list." otherwise). Finding severities read Critical, Warning and Note. The window footer no longer says "No live session" on Plans. Flame graph, Execution tree and Bar chart are drawn again for the new width when the window or a pane is resized (the selection is kept), so a Compare opened wide and narrowed to 720 px keeps its value labels in the pane.
- Compare reads in words: Change shows a ratio from 100% on ("750× slower", "2× faster") with the exact percentage in its tooltip; each pane names its plan's total time and the notes say each chart is scaled to it; Operator changes show one title for a same-label pair, the matching method in words ("Same operator · high confidence", "No counterpart") and sentence-case badges; a pane's inspector opens only for a selection, and both inspectors share one line, so Operator changes stay in view; Compare bar charts keep every value label (the bar chart's label column narrows with the chart, down to a 480 px chart).
- One Export control everywhere it is a menu: `ExportMenu` in `kit.tsx` draws the export icon, Export and a chevron-down pull-down of formats (a plain button when there is one format). Plans and Compare (JSON, HTML report) and the Tables and Indexes inventories (CSV, HTML, PDF…) use it; the right chevron and the native Export… select are gone.
- Plan charts read the same: the bar chart prints times like the execution tree (4.66 ms, 3.57 s, 66.6 µs) and colors each bar by its share of the plan's time (each color step halves the share) instead of cycling eight colors by rank, in the TypeScript runtime and the Python SVG (`output_bargraph.bar_color`, `label_width`); tree and bar headers are sentence case at 12 px (Operation, Loops, Self time; Self, Total, Self time bar) and the tree links read Expand all and Collapse all; every chart search reads Search operators; Visual Explain's zoom controls read −, percentage, +, Fit like the Database diagram, and each edge's rows label sits on an opaque chip so the edge line no longer runs through it.
- Locks blocking tree: Waits for is capped at 40 characters (full text in the tooltip and details) so Statement keeps real width with an ellipsis at 1440 px; at 720 px the tree scrolls with its edge cues.
- Error log and Processlist keep a one-row view toolbar: their filters (Entries / By code, levels, Time, the text filter, This run only and Export; Active / All / Locked, Source, Running at least and the filter) move to the row above the list, like Indexes and Tables. Error log time choices use sentence case (All time, Last hour, Custom range).
- Top statements and Users and roles show an unverified privilege reason only in the button's description and the review dialog; the visible note under Explain top N, New user and New role appears when the action is denied or blocked.
- Indexes and Tables replace the bordered Actions button on every row with the ⋯ row menu used by Processlist, in sentence case and with the same labels as the right-click menu (an ellipsis on items that open a review); every ⋯ row menu (Processlist, Indexes, Tables, Query results) shows on the hovered, focused or selected row.
- Health counts each status once: the summary line counts tiles and diagnostics together (no separate diagnostics clause), and a metric tile without a threshold draws no marker instead of a lone dash (its sparkline stays at the trailing edge).
- The Locks "End it yourself" section and reviewed-statement "Server messages" use the shared disclosure chevron; the Database diagram Export button carries the export icon.
- Replication puts its summary line first, then the shared metric tiles (Replica state, Channels, Max applier lag, GTID mode) in sentence case; the maximum lag reads Unknown, in the tiles and the summary, whenever any judged channel's lag is unknown. Its sections are Topology, Channels (Connections on MariaDB), Lag, GTID and Recovery; the GTID table reads the real binary log positions; the inspector shows positions and GTID state in one list (GTID mode, Enforce GTID consistency), the raw status behind the shared disclosure, and skip actions only on a channel whose applier stopped or has a failure to skip.
- Tuning puts Scorecard | Full analysis in the header row as the shared segmented control (still tabs, with the same ids and keys); after a connection switch the scorecard's note offers Run full analysis and its Export is off until this server is read.
- The Query editor shows a privilege note only for denied statements, as one line at the end of its toolbar so a late answer never moves the editor; unverified privileges add no note. The footer no longer repeats the transaction state, rows and time; "Sorting applies to loaded rows" appears only on a cut result; the modes read Editor | Query digest, and Query digest and the Schemas and History pane titles use sentence case.
- Learn family labels and lesson titles use sentence case ("Scan, sort and temp family", "Unique key lookup: single-row index lookup"), Derived table materialization gets a subtitle, and lesson summaries are plain text.
- Add inline proportion bars to Waits total and window wait cells, and surface key server-wide InnoDB I/O rates as a compact row of summary tiles above the I/O table while keeping the full list in its details section.
- Reduce stacked chrome in the workspace. The top bar names the place once as plain text with one sidebar toggle: the Server and Workload breadcrumb menus that repeated the sidebar and the list of views are gone (the view header names the view, and a narrow window gets a Views menu in the place toolbar), and Replication and Tuning no longer repeat their name above their own header. Plans moves Explore | Compare and Import plan into the top bar and merges the plan name, stats, Plan summary, SQL (with Copy SQL) and Export into one plan header; Plan summary and SQL open a panel over the chart in one click. Below 820 px of plan or Compare width the chart view, metric and branch become one select and an Options menu and the Visual Explain legend a popover, so the chart controls fit one row at the 720 px window.
- Move the Investigation notebook from under the charts into a sheet opened by Save investigation (investigation name, plan name, baseline, tags, notes, Export investigation bundle); Save in the sheet saves the investigation and Close keeps the edits as unsaved changes.
- Server no longer keeps a hidden second copy of the Replication view: links to Replication open the Replication place, and a skip loop restored with the workspace is found by the Replication place, which mounts at once for it.
- Use a consistent blue accent in both workspace appearances, including embedded lessons. Filled buttons use a darker blue so white labels retain AA contrast. In status columns (Health, InnoDB and other one-line rows, metric tiles, InnoDB sections) an OK row leaves the status cell empty, still named OK for screen readers, and No threshold shows a faint dash, so only Watch and Problem draw the eye.
- Give flexible table text columns at least 260 px, and give every metric tile in a group the same width: up to six share one row, seven or eight form rows of four, and a row that does not fit becomes balanced rows of three or two instead of one tile stretched across a row (Health, I/O, and the Tuning categories at narrow widths).
- Use one marker system in the Server and Workload lists and the sidebar places: a small status dot for state (problem red, watch amber, a gray ring for unknown or off, nothing for OK) and a neutral count pill beside it, replacing the mix of colored dots and red, amber, green and gray count pills. Accessible names keep both, such as "5 blocked".
- Settings is a compact menu-style popover without a title bar or close button; Escape or a click outside closes it, and the restore hint is one line.
- Replication topology map is now a static diagram: one row per channel with its source, a connector and the replica. A solid line with "Running", a dashed line naming the stopped thread, or a red line with "Error <code>" shows the state, with measured lag as text. The animated packets, pulses, play and pause control and speed control are removed.
- Remove duplicated controls and status badges across Home, About myflames, Error log, Indexes, Tables, Database diagram, Instruments, Maintenance, Users and roles, Top statements, Replication, and Query editor results.
- Workspace icons now come from the free Hugeicons set (Stroke Rounded, MIT, vendored into `icons.generated.ts`) instead of hand-drawn paths. Tuning uses a gauge instead of a sparkle, Replication and Error log have their own glyphs, and Users and roles uses a user group. Hugeicons notice added to `NOTICE`.
- Rename Server > Configuration to Parameters in the workspace (the CLI `myflames config` is unchanged), update Full analysis and InnoDB links to open Parameters, and read Parameters and Instruments on open (one attempt per connection; Refresh retries).
- A Server, Workload or Replication view reads when it is opened on screen (once per connection; Refresh retries). Tables and Indexes keep their schema-size consent, Error log waits for Load, and nothing reads off screen: Server and Replication mount on their first visit, and views kept mounted while hidden are told they are off screen (`viewOnScreen.ts`).
- Show the write safety of the connection in use in the window footer instead of a strip under the top bar, which covered the breadcrumb and view headers. The footer of the Query editor, Server, Workload, Replication and Tuning starts with a dot and words ("Production · changes ask first", "Read-only · changes refused", "Development · risky changes ask first", "Production assumed · settings not loaded" with Retry); its accessible name is the full sentence. Home, Plans, Investigations, Learn and the Audit log show none. The PRODUCTION tag in the connection picker stays.
- Make Tuning a place of its own: it opens full width with its own header and the window footer, never inside Server's list of views, and Server never shows it. The sidebar label is "Tuning" (it also serves MariaDB). The Full analysis has one home, the Tuning place's second tab: links and the command palette's Full analysis open it there.
- Apply the macOS look to the window chrome: the system font stack led by SF Pro Text and SF Mono, 13 px navigation rows of 28 px, 11 px sentence-case section headers instead of spaced capitals, 15 px view titles, and secondary text at 4.5:1 or more in light and dark. Exactly one place looks selected (a rounded tinted fill, not the gray hover); keyboard focus draws a ring only for the keyboard, never after a click.
- Tables JSON now uses `schema-2.0`: unknown reusable-space percentages are null. Consumers must handle null instead of assuming every percentage is numeric.
- `myflames report` section lines list each flagged title under its own status ("1 to watch: Slow query log; 1 unknown: Version support"), so an unknown finding such as Version support for a series without a reviewed lifecycle (MariaDB 12.3 and 13.0, MySQL 26.7) never reads as one to watch in a WATCH section. The same summary text appears in JSON, Markdown and HTML.
- `myflames sql --help`, the README and the Query workspace guide state that live results carry no column types, so `json` keeps numbers as strings, `table` quotes them and `insert` writes them as quoted text.
- Fit Top statements, Processlist and Locks transactions beside the list of views at 1440 px without a sideways scroll, keeping 260 px for the statement. Processlist rows no longer carry Kill and Plan buttons: each row's ⋯ menu has Show plan and Kill…, which opens the same confirmation sheet as Kill in the details (Cancel first, Escape returns focus to ⋯). Some columns start off and stay in the Columns menu, with their values in the details: Flags in Processlist (the Status cell names the flags on hover), Rows sent and Schema in Top statements, and Lock structs and Isolation in Locks transactions; Reset columns turns them off again. Numeric headers are shorter (Processlist Time, Top statements Avg (ms)), with the full name in the tooltip and the Columns menu. At the 720 px minimum window these tables still scroll inside their frame.
- Open the workspace faster: the first screen loads a 365 KB script instead of 1.1 MB. The Query editor loads beside it, and Server and its views, Replication, Investigations, the Audit log, the Plans chart, Table data and Query Digest load their code on first use (the places opened most are preloaded in idle time; styles stay one stylesheet in the same order). Large Processlist and Indexes lists no longer filter, sort and measure again on every selection or keystroke elsewhere, sorting reads each value once, and number formatting reuses its formatters.
- Workspace shell: the sidebar no longer shows a fixed version (the version is on Home and in About myflames, read from the installed package), keeps one width in every place, and fits its badges (Replication shows a dot and a count, the state word in its tooltip and accessible name); the Server and Workload lists of views no longer scroll sideways.
- On Server and Workload, Refresh all is a text button right after the connection picker, so it no longer sits above a view's own Refresh; the top bar keeps its hairline and the Local badge in every place, and a view's Refresh stays at the end of its toolbar on detail pages.
- The command palette lists Plans, Investigations and Learn with the other places in sidebar order, files Compare under Plans, and gives the Audit log its own icon.
- The light appearance uses a white window background, like the dark one, so no gray bands show below charts, empty states, Home, Investigations, Learn, the Audit log or Replication; Learn is edge to edge and its lesson toolbar stays under the top bar.
- Home shows one Import plan (in the top bar) and one primary action (Add connection), the version footer at the bottom and a full-height divider beside Recent work; connections read Not saved for no saved password and a dash for no default schema.
- The autosave status reads Saving… and All changes saved, the Settings hint says passwords are never part of the workspace, About myflames names the product and the databases once with Close as its default button and fits a 720 x 520 window, the empty Audit log describes every recorded change and disables Export, Users and roles counts read 1 user and 1 role, and plain disclosures use the shared chevron.

### Deprecated

- Deprecate `myflames ui` (the browser-based workspace) in favor of the standalone desktop application (`myflames.app`). Running `myflames ui` directly from the command line now emits a deprecation warning to stderr.

### Fixed

- Maintenance no longer goes blank when a preflight result arrives while another step's report is shown (a plan without a tool was read as if it had one).
- Capture reads table statistics for up to 100 tables per client call (a `UNION ALL` of the per-table `information_schema.TABLES` lookups, under 24,000 characters) instead of one client process per table: 800 tables take 8 calls instead of 800, and about 150 tables took 0.07 to 0.11 s instead of 3.7 to 4.0 s on MySQL 8.4 and MariaDB 11.4. Results are identical per table, and a failed batch is read one table at a time so only an unreadable table is left out.
- Full analysis reads the statement-attribution metadata and coverage (server state, digest consumers, sample text limit, overflow row, Uptime and lost digests) once per analysis instead of once per digest list: 9 client calls instead of 24 on the MySQL recordings and 8 instead of 20 on MariaDB, with identical findings.
- Indexes builds the `performance_schema.setup_objects` lookup once per report instead of once per table: 5,000 tables with 1,000 rules took 0.03 s instead of 0.44 s (10,000 tables: 0.06 s instead of 0.84 s).
- Tables decides whether a General tablespace exists, and folds tablespace names for `lower_case_table_names` 1 or 2, once per report instead of scanning every tablespace for each table: 20,000 tables took 0.16 s instead of 2.4 s, and 0.17 s instead of 9.2 s with `lower_case_table_names=2`.
- Locks builds each metadata-lock group's owner list once instead of copying it for every pending request: 5,000 holders and 5,000 waiting requests took 0.02 s instead of 0.78 s.
- Plan charts redraw when their pane changes width (the window, the sidebar or Compare), at most once per animation frame, keeping the search, the selection, collapsed tree rows, the focused operator and the scroll position. The Execution tree and Bar chart fit the 720 px window beside the sidebar (narrowest layouts 489 and 480 px, Flame graph 360 px; they were 820, 760 and 720): the operator label column gives up room first, shortened labels end in "…" and show the full name as a tooltip and in the inspector, and the time, share, loops and value columns keep their width.
- The Add column and Remove column dialogs in Server > Tables are modal sheets like the other confirmations: labelled by their title, focus moves in (the column name, or Cancel for Remove column) and stays inside, Escape and Cancel close them and return focus to the button or Actions menu that opened them, and Cancel and the review button stay in view in the 720x520 window.
- Quitting the desktop app during a dump, restore or clone no longer leaves the tool running. The backend stops each tool's process group before its slower cleanup and audits the operation as cancelled with outcome unknown; when the backend itself has to be killed, the app also stops the tool groups the backend listed. Quitting waits at most 7 seconds for them to be gone.
- Theme the Query Digest view with workspace design tokens in light and dark appearances, including the in-app theme switch, flat proportion bars, and responsive layout down to 720x520.
- Fit the desktop app's 720x520 minimum window: the Query editor keeps Export and three result rows above the footer with a working splitter, and its toolbar wraps instead of scrolling when its pane is narrow; About myflames, every review and confirmation sheet, Mass kill, Import plan, Save investigation and the Add column and Remove column dialogs keep their title and actions in view and scroll only their body; a Server or Workload view stays on screen when the window narrows, and its Back button names its place; view toolbars move their controls below the title; the Server and Workload top bar keeps the connection name; Plans shows its four measurements as columns and Compare keeps both plan names readable; the Kill review's error is styled as an error; Query Digest uses design tokens instead of hardcoded colors.
- On Windows, client output no longer keeps CRLF line ends in raw reads (the client writes in text mode there).
- Workspace colors meet WCAG AA contrast in light and dark: status words use new `--ok-text`, `--warn-text`, `--danger-text` and `--info-text` tokens (green, orange and red words measured 1.8:1 to 3.6:1 in light), status dots reach 3:1, filled danger buttons and count badges use `--danger-fill`, input, select, checkbox and button borders reach 3:1 (`--control-line`), a highlighted context menu item is an accent fill with white words (it was white on pale blue, 1.1:1), and the update badge and dialog and the chosen Mass kill preset use theme tokens instead of fixed dark palette colors.
- Move focus into DataTable row menus, navigate them with arrow keys, contain Tab and restore focus on dismissal.
- Make table-data cell menus keyboard accessible, with named menus, scoped focus and Enter/F2 access to the existing editors.
- Give Tuning tabs roving focus and ArrowLeft, ArrowRight, Home and End navigation.
- Focus the floating Server and Workload views drawer on activation, contain keyboard navigation and restore the trigger on Escape.
- Show denied, blocked and unknown permission reasons to keyboard users without adding status badges to action labels.
- Label Mass Kill duration and selection controls, expose preset state and keep nested confirmation dismissal inside its focus scope.
- Keep replication connector details outside button names, available at every width, hoverable and dismissible with Escape.
- Keep the safety Retry action outside the labelled note and include connection type and read-only policy in picker names.
- Give navigation status markers visible words and count badges accessible labels so severity does not rely on color.
- The macOS app's Python backend now stops on its own when the app is force quit or crashes, instead of staying in the background with its local port open.
- The macOS app bundle now includes the Electron, Chromium and Python license notices beside LICENSE and NOTICE, no longer carries stale development package metadata, and disables Electron's run-as-Node, `NODE_OPTIONS` and inspector entry points, with asar integrity validation turned on. `npm run build:backend` uses the documented build venv when the system Python lacks PyInstaller, or says how to create it.
- An estimated capture (EXPLAIN only) no longer counts as proof that a sort ran in memory, and a materialization fed by several inputs (a UNION or a recursive CTE) no longer claims the first input's rows as its size bound.
- Visual Explain in dark mode draws the join Venn circles with dark fills and strokes, so their L and R labels are readable again (they were light text on light circles).
- The Tuning header shows its title beside the ⓘ again, like every other view.
- Keep the Full analysis (Tuning's second tab) greyed with the other-connection note after a connection switch instead of dropping its report, and disable Run full analysis and its Refresh while Refresh all runs.
- A Full analysis link that opens a row in a view of another place keeps that row's inspector open.
- A Query editor run refused by the last write-policy re-read (the connection became read-only or Production while it connected) now ends the session that run had just opened instead of leaving it open until Close; a session the tab already had is kept.
- The SQL classifier splits MariaDB `SET STATEMENT ... FOR <statement>` at the `FOR` that ends the assignments, not at the one in `NEXT VALUE FOR seq` or `PREVIOUS VALUE FOR seq`, so the value (a sequence advance is a write) and the real statement are judged.
- Bind Query Digest reports and editor actions to the complete connection identity, dropping late responses and disabling stale actions.
- Derive replication card status from judged channels and distinguish stopped, unknown and unjudged states.
- Read the reported GTID mode in replication cards and coordinates, with an unavailable state for missing values.
- Describe replication lag as a measured maximum with unknown-channel coverage and receiver/worker caveats.
- Export only supported digest execution statistics; leave uncollected minima, row/lock maxima and percentiles unknown.
- Preserve Health diagnostic state in Full analysis findings so Tuning retains applicability and coverage.
- Gate automatic Parameters and Instruments reads on whether their view is on screen.
- Gate Tables and Indexes schema-catalog reads on whether their view is on screen.
- Automatically read each newly selected connection in retained server views while keeping the previous report until success.
- Exclude sessions that only hold locks from the Mass Kill Lock waiters preset.
- Recommend only an option-file redo size and a restart on MariaDB before 10.9.1; keep online resize advice for later versions.
- Apply the TempTable per-table limit only from MySQL 8.0.28, and recommend the shared global temptable_max_ram limit when its RAM alone is exhausted.
- Explain that MariaDB 11.2+ MyISAM table copies hold a read lock, blocking UPDATE and DELETE while permitting concurrent inserts; correct the shared row-ID counter version range to MariaDB 10.6 to 10.11.
- Emit unsupported-version notes for live CLI reports from their existing server metadata, respecting quiet mode without extra queries.
- Preserve captured schema evidence, engine and server version in MCP plan analysis and digest findings to match CLI output.
- Guard unknown version support as non-actionable through Full analysis aggregation while retaining known end-of-support problems.
- Show failed dump reasons once in CLI text without repeated status words or duplicate operation verification lines.
- Emit the unavailable error-log JSON envelope while retaining exit 2 and the stderr reason and hint.
- Keep configuration numbers exact without exponent notation, report verified values already set as unchanged, and use SQL operations and CLI flags in configuration guidance.
- Identify MariaDB roles when account commands omit --role.
- Avoid a false SQL refusal audit entry while preparing CLI confirmation, including with --yes, and correct plural script confirmation wording.
- Render known numeric SQL columns without text quotes, preserving BIGINT and DECIMAL precision in JSON and quoted untyped text in INSERT.
- Accept capture JSON in MCP plan analysis and digest tools using the CLI's capture validator.
- Describe unknown version support without development-source wording or unverified maturity labels.
- Include received GTID positions in replication text output.
- Capture from a MariaDB digest no longer refuses every statement with an aggregate: digest text writes `SUM (` with a space, and the SELECT-only check now reads the rejoined `SUM(` for statement text that came from a digest. Raw SQL with a space before the parenthesis is still refused.
- `capture --mode analyze` on MySQL before 8.4 says EXPLAIN ANALYZE FORMAT=JSON needs MySQL 8.4 or later and suggests `--mode explain`, instead of printing the raw server error. Capture errors no longer show the internal script line number.
- Capture and the dump, restore and clone plans note when the server is below MySQL 8.4 or MariaDB 10.11.
- Dump with MySQL Shell shows one clean error line (no colour codes, no NOTE lines), and refuses before running when the Shell's major version is older than the server's, suggesting `--no-shell`; an older minor version adds a warning.
- `upgrade-check` on MariaDB refuses a target older than the server and a MySQL version number such as 8.4.0, since MariaDB to MySQL is not an upgrade path.
- Theme all workspace and standalone runtime chart canvases, node cards, controls and details in light and dark appearances while preserving operator and heat palettes. Chart search placeholders show the macOS shortcut on Apple platforms.
- Reserve one real trailing table-menu column with opaque sticky cells instead of padding the neighbouring data column; retain readable SQL in the Locks blocking tree.
- Add scroll clearance above the window footer and below command-palette results, space the lesson navigation toolbar, preserve diagram captions, and keep the narrow connection picker readable.
- Keep the Query editor's schema header and query tabs flush under the top bar: the footer clearance made the page 16 px taller than the window, so focusing the editor scrolled that row under the bar. Server, Workload and Tuning views with little content no longer scroll by 16 px, and on narrow windows the Query editor grows with its results so the last row clears the footer.
- Keep the connection pill and Privileges apart below 650 px: the pill gives up width first (its dot and a few characters stay) and Privileges becomes a key icon button named "Privileges" for screen readers and in its tooltip.
- Tables wider than their frame fade out at an edge with more columns beyond it, and the Columns menu column casts a shadow over the column scrolled beneath it, so a cut column reads as scrolled rather than covered; the Locks blocking tree fades the same way. Columns that start wider than their values give that room back first, and Home connections fit at 1440 beside a Recent work list of at most 360 px (connection names keep 160 px).
- The command palette's list ends on a whole command instead of cutting the last visible one in half above its footer.
- The Server and Workload list of views stays pinned under the top bar while a long view scrolls.
- Keep the selection fill on a selected Sessions row that waits on or holds a lock.
- Open the requested setting in the Instruments inspector when a Workload view (Top statements, Waits, I/O) links to it: the place switch no longer closes the inspector the link just opened.
- Server's Refresh all now feeds the Replication place: the report is shared, so opening Replication afterwards shows the refreshed connection instead of the previous one. Opening Replication after another connection was chosen reads once for that connection.
- An unexpected error in a live read now answers "Could not read this from the server. Try Refresh or the CLI." instead of the plan wording, and its traceback (without passwords) goes to stderr.
- `scripts/ga-matrix.sh profiles` saves `ssl_mode` PREFERRED so the saved connections work in the workspace (a saved password is only sent to the exact TLS settings it was saved with).
- Read MariaDB SHOW FULL PROCESSLIST rows with a Progress column without an undefined numeric-helper error.
- Calculate lock transaction ages and waits in the server SYSTEM time zone, including Kill session review.
- Recommend legacy redo configuration and a restart when MySQL lacks innodb_redo_log_capacity.
- Show rows read and written for untimed MariaDB userstat I/O findings in Full analysis.
- Omit redo resize recommendations when the target equals the current capacity, while retaining checkpoint pressure findings.
- Check Full analysis table-scope consent once for both chosen and all schemas, skipping Indexes and Tables when gated.
- Remove the failed setup_instruments read note when the compatibility fallback succeeds.
- The workspace column list for a table now rejects invalid schema or table names and quotes them safely.
- The MCP `explain_query` tool now accepts only one SELECT (or WITH ... SELECT) statement and refuses anything else before connecting.
- A SET of a session variable whose value calls a function, assigns a variable or runs a subquery is now classified as a write, not a read.
- Turning safe updates off when opening an editor session now re-reads the saved connection policy after the password lookup and refuses if it changed.
- Status, Close, Stop and Rollback in the Query editor now follow the connection endpoint, so a changed connection type or read-only flag no longer leaves a session open that Close reported as closed.
- Capture now limits the size of the MySQL client output it reads and stops the client with a clear error when the limit is exceeded.
- An update download that ends before its declared Content-Length, or whose size differs from the release asset the update check found, is removed instead of being published to Downloads.
- A SET value that calls a function named with any non-ASCII character (such as `é` or `€`), `$` or quotes (including ANSI_QUOTES names), runs a `TABLE` subquery, or advances a MariaDB sequence with `NEXT VALUE FOR` or `seq.NEXTVAL`, is now classified as a write, also inside MariaDB `SET STATEMENT ... FOR`. myflames' own `CONVERT(X'...' USING utf8mb4)` text value, alone, is now a read, so Parameters session changes with such a value work on read-only connections.
- A SELECT that calls a function whose name contains a non-ASCII symbol or space (such as `€()` or `CONCAT` followed by a no-break space) is no longer classified as a read, so capture and the MCP `explain_query` tool refuse it.
- The MCP `explain_query` tool now runs EXPLAIN ANALYZE inside a read-only transaction (`SET SESSION TRANSACTION READ ONLY`, `START TRANSACTION READ ONLY`, then `ROLLBACK`), like capture.
- Turning safe updates off when opening an editor session now re-reads the saved connection policy again after the client connects; if it changed, nothing is sent and a session that request started is closed.
- Preserve existing update downloads, choose an unused destination name, and remove partial files after failed downloads.
- Start update downloads only from HTTPS URLs under `github.com/vgrippa/myflames/releases/download/`, and only for the asset the last update check found. GitHub's release asset hosts are accepted only as a redirect target or final response URL, and every redirect is checked before it is followed.
- Match update assets to both the operating system and architecture, including universal macOS builds.
- Collect the current password when generating a new account password, from a prompt or stdin.
- Preserve backslashes in the current password when using MySQL RANDOM PASSWORD with REPLACE.
- Preserve leading and trailing spaces in string parameter values during review and execution.
- Hash successful logical dump files once and reuse the checksums in the manifest.
- Mass Kill now shows the exact KILL statements and the server in a confirmation sheet (Cancel is the default) and sends nothing until you confirm; cancelling is recorded as declined in the audit trail.
- Table data: Copy row as CSV protects formulas, Copy row as JSON writes SQL NULL as null, and copied TSV and CSV leave NULL empty like the editor's export. On existing rows the cell action is now named Revert change (it only drops the staged edit); Set to Default stays for new rows.
- Query editor: Copy value copies the raw value instead of tab-separated, quoted text with a line break.
- Charts: in-chart search also opens with Cmd+F on macOS, still only while focus is inside the chart.
- Add column and Drop column build ALTER TABLE with backtick-escaped schema, table and column names.
- Query digest: the minimum rows sent and examined come from the data instead of always showing 0, and the CSV export protects formula-like text.
- Fix Server > Schema > Indexes, Drop index: the statement is sent alone, because the Query editor already adds its own `SET SESSION lock_wait_timeout = 5` before an ALTER TABLE and refused a run that carried a second one.
- Fix the Replication recovery table: its Skip button now has the same privilege, read-only, loop-status and active-loop checks as the inspector button.
- Fix Server > Error log: it no longer reads when opened. It reads on Load error log or Refresh all, as documented.
- Fix Processlist: the mass kill dialog closes when the connection in use changes, instead of keeping the previous connection's sessions.
- Fix Full analysis: the elapsed-seconds counter is kept per connection and cleared when a run ends, so it no longer carries over to the next run.
- Server views that read when first opened (Health, InnoDB, Locks, Processlist, Waits, I/O, Indexes, Tables, Replication) now do so only while they are on screen.
- Give every Query editor tab its own close button (shown on the active tab and on hover, always on touch screens) and close a tab with a middle click. The tab on the left takes over. The last tab stays open.
- Keep schema browser column and index rows on one line. The actions button no longer wraps under each column and shows only on the row being pointed at, the focused row, or on touch screens.
- Fix Database diagram Mermaid export and import: use Mermaid entity escaping (`#quot;`, `#39;`), keep readable sanitized types (`int_unsigned`, `decimal(15-5)`), emit PK, FK and single-column UK markers, add readable column aliases with the original name in a comment, emit comments only where information would otherwise be lost, read names and nullability back on import, and verify exports with the real Mermaid library in `apps/workspace/test/mermaid-export.mjs`.
- Show permission details inside the parameter review dialog instead of Unverified badges on its buttons.
- Keep keyboard focus in instrumentation details when following a link back to the same setting.
- Remove the duplicate Import plan control from Plans; the main import action remains available.
- Treat reusable InnoDB DATA_FREE as information instead of flagging large allocations as a problem. The report does not infer capacity or filesystem exhaustion from free extents.
- Preserve original editor SQL when preparing an analysis projection and require an explicit second action to run it. Converted statements are excluded from batch analysis until reviewed individually.
- Restore the README reference for saved connection selection across live commands.
- Refresh all reads Processlist with the same parameters and source (`SHOW FULL PROCESSLIST` unless the view asked for another) as the view's own read, so a running read is joined and the report matches the view's source picker.
- The Replication place's "How to turn it on" link opens Server > Instruments at that row instead of doing nothing, and Replication no longer shows a Back to Server button.
- Home's Database diagram entry opens Server > Database diagram instead of the last Server view.
- On phones, Server's list of views is a screen of its own again (a chosen view replaces it), and the top bar names the place without a clipped breadcrumb.
- Report the measured result of a MariaDB UNION, INTERSECT or EXCEPT (`union_result` `r_rows`) instead of the sum of its branches. A result table that was never read stays unknown. A UNION ALL without a result table is not measured: MariaDB applies a LIMIT or OFFSET on the whole union when sending rows, the plan does not show it, and each table branch counts rows before its WHERE. An OFFSET on a set operation with a result table is counted in `r_rows` although those rows are not sent.
- Stop showing a MariaDB join's inner table rows per scan as the rows the join returns. MariaDB has no counter on the join itself. The rows a join returns are the next table's `r_loops` (or the `block-nl-join` `r_loops` when that table uses a join buffer, MariaDB 10.6.13 and later). The last join of a select is not measured, and neither are Weedout and FirstMatch/LooseScan wrappers.
- Keep a MariaDB `r_rows: null` (a table that was never scanned) as not measured instead of 0 rows. `r_loops: 0` is kept as 0 loops instead of the optimizer's estimated loops, and the estimate (`loops`) is no longer shown as measured loops.
- Count a one-table MariaDB query's rows sent as the rows that passed its condition (`r_rows` times `r_filtered`), not the rows it read.
- Show a MySQL operator that never ran as "not executed" instead of "not measured" in the bar chart and tree details.
- Count rows examined as rows per loop times loops, so a lookup run 10,000 times for 2 rows counts 20,000 rows.
- Show measurements an estimated plan does not carry as not measured. The sidecar `plan_summary` (now `1.4`) has null `total_time_ms`, `rows_sent` and `rows_examined_estimate` in that case. The digest, the HTML summary, the executive summary, and the bar chart and tree details say "not measured" instead of 0.
- Judge full table scans in an estimated plan by the optimizer's row estimate, so `check --fail-on full_scan` fails on an estimated million-row scan. The warning is labeled as an estimate ("about N rows, estimated").
- Stop calling a hash join on a function such as `t1.d = concat('r', t2.y)` quadratic and recommending removing the function. A hash join hashes each row once. A hash join with no equality condition, and a Block Nested-Loop join, are still reported.
- Take the hash join build-phase row count from the build input (the child labeled `Hash`) instead of the join's output. For a MariaDB hash join (BNLH) the count is the rows put in the join buffer over all refills (`block-nl-join` `r_loops`, MariaDB 10.6.13 and later). The join buffer advice for MariaDB says that the inner table is read once per refill, instead of claiming a spill to disk.
- Replace characters XML does not allow (U+0001 and other control characters) in every SVG renderer and in flame graph tooltips, so plans that contain them still render as valid SVG.
- Make flame graph tooltip details linear in the number of frames: each frame looks up its own operator instead of matching every operator by label.
- Report the engine the parser detected in `digest` and in file sidecars, so MySQL `explain_json_format_version=1` plans (MySQL 8.0 EXPLAIN) are no longer labeled MariaDB. A capture file's recorded engine and server version are used when present.
- Exit 2 with a diagnostic, without a traceback, when writing to standard output fails (for example a closed pipe).
- Keep every table of a MariaDB 10.6 join. MariaDB 10.7 and earlier write the tables of a join as repeated `"table"` keys in one object (10.8.1 and later write a `nested_loop` array), and all but the last were dropped.
- Stop saying a temporary table "will spill to on-disk InnoDB" because of a small `tmp_table_size`. The summary now says the plan does not show whether it spilled. MariaDB uses Aria or MyISAM on disk, and configuration size alone does not prove a spill.
- Claim a sort spill only from measured merge passes: the capture's `sort_merge_passes` counter, or MariaDB ANALYZE `r_sort_passes` (a rounded per-run average, so the total is shown as "about"). A sort with no merge passes reads "sorted in memory (0 sort merge passes measured)" and gets no `sort_buffer_size` warning. Without evidence, the summary says the plan does not show whether the sort spilled, instead of "will likely spill". The sort advice is now "Add an index that returns rows in ORDER BY order to avoid the sort", and "Raise sort_buffer_size only if Sort_merge_passes shows the sort merged runs from disk" is dropped when 0 passes were measured, since a bigger buffer never avoids the sort.
- Stop warning that a temporary table "may spill to disk" (no plan format shows a spill) and stop printing "up to 0 rows": the warning gives the most rows its input wrote into the table, when measured. The temporary-table limit reads "the smaller of tmp_table_size and max_heap_table_size" where both apply (MEMORY, MariaDB) and "tmp_table_size" for TempTable.
- Count a MariaDB estimated plan's nested-loop reads from rows that pass `filtered` (or the printed `loops`, MariaDB 11.0.1 and later), and read a table under a join buffer once per refill instead of once per outer row.
- Give the temporary-table advice once when server variables are known, instead of a plan-level and a server-level copy.
- Link findings to the MySQL manual of the server's version (8.0, 8.4, 9.7, 26.7; the current manual for other versions), fix the semijoin link (`semijoins-antijoins.html` from 8.4), and link the missing-index warning to "How MySQL Uses Indexes" instead of the system variables page. MariaDB plans link the MariaDB documentation page for the finding, or give no link when no MariaDB page fits, instead of a MySQL page.
- End the executive summary with one period when the main finding is already a sentence.

## [3.0.0] - 2026-10-16

### Added

- Add `sql -o/--output PATH`, `--insert-table NAME` (default `result`), and `--confirm-target NAME` for exact DROP DATABASE/SCHEMA confirmation.
- Add connection duplication and multi-selection deletion with a shared confirmation sheet, per-connection password notices and failure reporting, and keyboard controls.
- Add Database diagram under Server > Schema, with live metadata, SQL/Mermaid/JSON imports, keyboard and pointer layout controls, editable JSON, SQL, Mermaid and SVG exports. Imports never execute SQL; unsupported details and lossy exports are reported.
- Add a source-verified privilege catalog and complete grant scopes to Users and roles. Unticked privileges and the Add scope picker feed one batch review for users and roles; metadata lists support exact manual names when coverage is incomplete. Include MariaDB package/package-body grants with reviewed temporary Oracle SQL mode, MySQL 9.2+ MLE library scopes, and anonymous proxy accounts.
- Add shared CLI server-write confirmation through `dbops.cli_confirm` with default No, stderr-only plans and prompts, exact typed guards, and audited credential/confirmation cancellation. Non-terminal Development SQL also requires `--yes`.
- Follow the system dark appearance across the workspace, chart controls, Compare and Audit while preserving light chart canvases and operator colors.
- Open Configuration Session statements in the Query editor for review and execution in the session used for subsequent SQL.
- Add `myflames sql` to execute SQL scripts from `-e`, `-f`, or standard input in a Query editor session with write safety, audit logging, and export formatting.
- Add `sql --format insert` to output query results as SQL INSERT statements into a named table (`--insert-table`, default `result`) with typed binary literals.
- Add read-only MCP tools: `server_report`, `top_statements`, `sessions`, `locks_report`, `health_sample`, `innodb_status`, `my_privileges`, and `replication_status`.
- Add privilege awareness and account management via `myflames privileges`, `POST /api/live/privileges`, `myflames accounts`, and `POST /api/accounts/*`.
- Add `connector.MySQLConnection.run_stdin` to pipe statements on client standard input with empty temporary HOME and no automatic reconnect.
- Add `dbops.strip_credentials` to remove IDENTIFIED clauses from SHOW GRANTS and SHOW CREATE USER strings.
- Add `myflames io --all-instances` to inspect table and file I/O beyond 2,000 instances within collection budgets.
- Add Server place Replication view and recovery commands supporting single transaction skips and multi-error skip loops.
- Add Server place Maintenance view supporting tool detection, upgrade checking, dumps, restores, and instance cloning.
- Add variable modification in Configuration view and CLI (`myflames config set`, `reset-persist`, `POST /api/live/variable/*`) supporting global, persist, persist-only, and session scopes.
- Add lifecycle management in `myflames/lifecycle.py` and CLI (`upgrade-check`, `dump`, `restore`, `clone`, `POST /api/lifecycle/*`).
- Add Query editor workbench with statement splitting, cursor or selection execution, Stop via KILL QUERY, multi-tab results, Messages, and transaction controls.
- Add persistent Query editor server sessions (`myflames/editor_session.py`, `POST /api/editor/*`) maintaining variables, temporary tables, settings, and open transactions.
- Add write safety framework in `myflames/dbops.py` with Production, Development, and Read-only policies, statement classification, and confirmation tokens.
- Add local SQLite audit logging (`~/.myflames/workspace.sqlite3`) tracking write attempts, refusals, and declines with credential redaction.
- Add workspace Audit log viewer under Settings with status filtering, record inspection, retention settings, and CSV or JSON export.
- Add Full analysis report (`myflames report`, `POST /api/live/report`, `report-1.0`) composing Server views into ranked findings with export.
- Add workload attribution in `myflames top` and `/api/live/top` (`--order full_join`, `--order scan`, `--order no_index`, `--table SCHEMA.TABLE`).
- Add Server place Full analysis view executing native health checklists with copyable remediation lines and export.
- Add Server place Refresh all action (Option+Cmd+R) reloading loaded views in single-flight parallel execution with Stop.
- Add statement attribution links in Health and Indexes inspectors opening filtered Top statements.
- Add storage engine breakdown (`summary.engines` in `schema-1.0`) reporting table counts and bytes per engine.
- Add interactive 3D B+tree lesson view in Learn (`apps/teach-3d`, Three.js) with 2D or 3D switching and page navigation.
- Add connection inspection in `myflames sessions` and `POST /api/live/sessions` (`sessions-1.0`) with transaction metrics and long-running flags.
- Add parsed InnoDB status in `myflames innodb` and `POST /api/live/innodb` (`innodb-1.0`) summarizing semaphores, transactions, deadlocks, and buffer pools.
- Add table health and fragmentation inspection in `myflames tables` and Server Tables view (`schema-1.0`) detecting missing primary keys and free space.
- Add multi-database selection picker for Indexes and Tables views with table counts and default user database scoping.
- Add metadata lock blocker tracking and single-session kill in `myflames locks` and Server Locks view (`locks-1.1`).
- Add server health diagnostics in `myflames health` and Server Health view evaluating multi-delta status metrics and buffer or redo performance.
- Add read-only Server views: Waits (`waits-1.0`), Indexes (`indexes-1.0`), I/O (`io-1.0`), and Configuration (`config-1.0`).
- Add direct explain and analysis from Performance Schema digests in Top statements with parameter skeletons.
- Add `myflames capture --statement-digest` to capture statements behind digest hashes (retaining `--digest` as a deprecated alias).
- Add Top statements and Running queries tabs to the workspace Server place.
- Add `myflames import-shell` to convert MySQL Shell diagnostic zip archives into capture JSON.
- Add MySQL Shell plugin in `contrib/mysqlsh-plugin/` providing `explain()`, `digest()`, and `advise()` functions.
- Add advisor rule detecting large row estimate discrepancies with histogram and ANALYZE syntax suggestions.
- Add fresh InnoDB statistics collection during live captures for misestimated tables.
- Add advisor rule detecting redundant BTREE indexes sharing leftmost prefixes.
- Add advisor rule detecting cold buffer pool cache scans across repeated analyze runs.
- Add `--ignore` and `--list-rules` flags on `check` and `advise` commands with documentation links.
- Add Performance Schema instruments guide in `myflames instruments` and Server Instruments view (`instruments-1.0`) with presets and setup SQL.

### Changed

- Rebuild Home around a dense Connections table with an inspector and one Recent work list; keep New query tab in the Query editor and focus the table from Manage connections.
- Keep flame graph, execution tree and bar chart labels readable on phones with a scrollable canvas.
- Use colons in accessible chart titles and HTML report titles, and replace phrase-separating dashes in report copy.
- The workspace credits Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs in an About myflames window (Settings menu), like a desktop app's About, instead of a footer on every screen.
- Deprecate `capture --digest` in favor of `capture --statement-digest`.
- Deprecate `myflames running` in favor of `myflames sessions`, retaining `running-1.0` output and warning on stderr.
- Deprecate `diff` in favor of `compare`, printing a warning unless quiet mode is active.
- Deprecate `treemap` view in favor of the bar chart; `--type treemap` warns on stderr and renders a bar chart, while `render_explain(output_type="treemap")` raises DeprecationWarning.
- Add unified window status bar at the bottom of Plans, Query editor, and Server places reporting engine, connection, session, transaction, last run, duration, rows, and refresh status.
- Update workspace buttons to be privilege-aware, disabling definitively denied actions with explanatory tooltips while keeping unverified permissions enabled.
- Update Query editor statement privilege checks to display a single quiet status note below the toolbar without blocking mixed batches.
- Update Visual Explain fit-to-view behavior to fit width between 75% and 100% starting from top inputs, keeping wide and tall trees pannable.
- Update Visual Explain Compare mode to stack plan canvases vertically at narrow widths to preserve card legibility.
- Streamline Home place into a launcher with one Import plan button in the top bar, connection management, recent work, and drag-and-drop import.
- Implement scoped focus trapping for dialogs, sheets, popovers, and drawers, cycling Tab and Shift+Tab, navigating menus with arrow keys, and restoring focus upon Escape.
- Add context menu triggers in table grids and schema trees via right-click, row More actions buttons, and Shift+F10 or ContextMenu keys.
- Update phone layouts to wrap toolbars and segmented controls cleanly, hiding the duplicate secondary editor status bar in favor of the window status bar.
- Reorganize workspace Server place navigation into grouped categories: Overview, Workload, Contention, Schema, Setup, and Security.
- Add pointer and keyboard column resizing, column visibility menus, and layout persistence to all Server tables.
- Consolidate auto-refresh controls into a single segmented selector defaulting to Off across Server views.
- Restrict capture comment recognition for `-- ` to require trailing ASCII whitespace, matching mysql client behavior.
- Subject Kill blocker and connection actions to connection write policies, requiring explicit confirmation on Production and auditing all outcomes.
- Apply desktop design tokens across the workspace with 28 px control heights, hairline dividers, compact badges, and system typography.
- Render charts in workspace and HTML reports using inlined TypeScript runtime from `chart-data-1.0` payloads.
- Standalone `--type workbench` replaces `--type diagram`, preserving diagram as a compatible alias.

#### CLI consistency

- Add `sessions --kill ID [--query] [--yes]` as an alias of `locks --kill`, sharing confirmation, identity checks, write policy, audit, and exit codes.
- Print a Top statements command for flagged joins without an index in health text and for the first table read without an index in indexes text.
- Standardize stderr-only expected failures under `--json`: exit code 2, empty stdout, and unchanged output destination.
- Route skip-loop live events to stderr and write identical final text or JSON for started loops to stdout or `-o`.
- Suppress deprecated-alias warnings using parsed quiet flags, including `-qq` and accepted long-option abbreviations.
- Reject conflicting digest and compare output modes; listing modes and empty server results honor output destinations.
- Return exit code 2 for teach write failures, unreadable query files, and failed explicit sidecar exports; keep automatic auxiliary exports best-effort.
- Share root live-render connection and profile resolution, enable live mode from `--execute`, and preserve TLS client paths and client executable overrides.
- Add quiet flags to artifact commands and output flags to guide and replication skip-loop; live events go to stderr.
- Reject repeated single-schema filters on top, sessions, and I/O.

### Fixed

- Recheck saved connection policy after editor credential lookup and immediately before execution; a concurrent read-only or type change refuses the run.
- Require an exact typed database name for DROP DATABASE/SCHEMA in the editor and SQL CLI. Confirm each DDL separately; scripts containing several DDL statements must run them one at a time.
- Review ALTER TABLE with approximate table size, observed metadata locks and open transactions. Run the shown five-second session lock-wait guard immediately before ALTER, and offer explicit INPLACE/LOCK=NONE SQL only for supported InnoDB forms. Suggested SQL requires a new review; ALTER runs on its own.
- Resolve replication worker error 3030 to its underlying failure in the skip loop's final guards; refuse when that failure cannot be identified.
- Constrain the optional MCP SDK to `mcp>=1.2,<2` and report its installed version and original transport import failure.
- Reject an incorrect restore target before starting a job in the API, CLI and direct calls, with an audited "Nothing was restored" refusal.
- Treat native `SELECT SLEEP()`, `UUID()` and `UUID_SHORT()` as reads while preserving confirmation for side-effecting and unknown functions.

- Give each client invocation its own temporary credential file to prevent parallel Full analysis failures and password-file leaks; report exits 2 when all views fail and warns on partial failures.

- Every client connection uses utf8mb4 (`--default-character-set=utf8mb4` and `SET NAMES utf8mb4`), so non-ASCII names and values no longer depend on the locale or come back as latin1 or `?`; this also covers MariaDB 10.11 servers reached through a MySQL client.
- Support replication skip loops with disabled receiver stages, denied stage reads, or Performance Schema disabled.
- Preserve executed account results through `-o/--output`; unsent refusals and declines exit 2 with stderr only, including Ctrl+C and EOF.
- Audit account drift as a stale refusal before rebuilding account plans, and enforce single-claim server plan ownership.
- Enforce owner-only POSIX permissions on workspace database opens, including custom directories and SQLite WAL, SHM, and journal files.
- Prioritize granted lock waits over queued waiters in Locks view and share metadata lock inspection round-robin.
- Accept `--all-schemas` alongside `--schema` in `indexes`, `tables`, and `report` to lift the 2,000-table limit.
- Strip password hashes and plugin credentials in `dbops.read_grants` and `dbops.redact`.
- Preserve literal databases, patterns, and package or library routines without dropping lines in `dbops.parse_grants`.
- Correct MariaDB SUPER privilege mapping for split privileges across 10.11 releases up to 11.0.1 and READ_ONLY ADMIN before 10.11.1.
- Fix read-only connection refusal messaging to use proper plural phrasing when blocking multiple statements.
- Synchronize Query editor unsaved edit state immediately on keystrokes, preventing missed leave-page prompts during reload.
- Place inspector identifier labels on dedicated lines above values across all views to prevent awkward mid-name wrapping.
- Reserve minimum widths for primary name columns in Server tables during narrow viewports while number columns scroll horizontally.
- Track flame graph timings in microseconds, preventing sub-millisecond operators from vanishing due to rounding.
- Capture overflow executions and elapsed time from full Performance Schema digest tables in `top`.
- Align Configuration review redo log checkpoint age thresholds with server source forced flushing points.
- Recognize MariaDB `innodb_flush_log_at_trx_commit=3` as durable without generating false warnings.
- Reject `SELECT ... FOR SHARE` locking clauses in capture consistently with `FOR UPDATE`.
- Filter out internal capture helper statements and metadata queries from `top`.
- Validate plan structure strictly in `check`, `advise`, and `digest`, rejecting non-plan JSON with exit code 2.
- Account for sequential scan read-ahead pages on MySQL buffer pools in the cold-cache advisor rule.
- Handle carriage returns and Unicode line separators within result field values during connector client parsing.
- Eliminate precision floating-point formatting noise from `compare --digest` output.
- Parse and preserve MariaDB and MySQL format-1 subqueries, CTEs, and wrapper operations in unified plan trees.
- Avoid duplicate time accounting for derived table materialization and inner iterators in MariaDB plans.
- Skip recursive CTE work tables and reference actual base tables behind aliases in advisor suggestions.
- Update Temporary tables educational lesson to reflect engine-specific memory limits for TempTable, MEMORY, and MariaDB engines.
- Send KILL QUERY to terminate executing server queries before client termination upon Ctrl+C during capture.
- Guard session restore request retries so newer autosaved session revisions are not overwritten.
- Avoid unsupported POSIX chmod calls on Windows during connector configuration file setup.
- Parse valid plans containing embedded `EXPLAIN:` strings inside queries or conditions without corruption.
- Bind Server reports, selections, and queries strictly to originating connection identities across switches.
- Respect cancellation, reject mismatched connection reports, and clean up jobs promptly during Top statements Explain top N.
- Replace earlier in-flight queries rather than dropping newer requests in Sessions and Locks views.
- Keep Query editor tabs originating from Top statements bound to their source server across connection changes.
- Execute connection testing and saving operations single-flight and validate matching form settings before applying results.
- Clear stale comparison results on pending or failed comparisons in Compare view.
- Preserve focus in view inspectors during background automated refreshes.

### Security

- Replace the HTML API token with a one-use launch fragment, per-instance HttpOnly cookie, and origin-scoped session proof. Both credentials are required for refresh and API requests; server restart invalidates them.
- Isolate read clients, capture jobs and editor sessions from ambient client environment settings, personal and system option files, and login files. Validate browser connection fields before remembered-password lookup.
- Remove automatic server public-key retrieval from client options.
- Re-read lifecycle profile policy at job and tool boundaries; refuse removed or changed connections before starting the next operation.
- Check recipient TLS before sending a saved clone donor password, and bind clone execution to the reviewed donor/recipient endpoints and policies.
- Reject state-changing administrative functions and unverified function calls inside SELECT during read-only capture; the Query editor requires write review for them.
- Preserve endpoint and profile ownership for ended editor sessions so another connection cannot reuse their results. Redact the reused session's actual password from history and SQL credential literals in persisted editor drafts.
- Require one-use server review tokens for Kill, single replication skips and lifecycle starts; reject missing, expired, altered or replayed plans before credentials are read.
- Bound HTTP workers and body-read deadlines, account request size and retained-plan memory, and restore manifest traversal.
- Reject symlinks and foreign owners for workspace files, sidecars, vault directories, keys and locks; use exclusive creation and close SQLite connections after setup failures.

- Reject SQL with ambiguous backslash string escaping under differing NO_BACKSLASH_ESCAPES modes during capture.
- Key stored connection credentials strictly by host, port, user, and TLS parameters, preventing cross-profile exposure.
- Validate numeric input in plan parsers strictly, rejecting invalid types, infinities, and values exceeding 64-bit limits.
- Escape ANSI and control characters in server text output, preventing terminal injection from external SQL queries.

### License

- myflames is now licensed under the Apache License 2.0 (`LICENSE`, `NOTICE`). It began as a fork of Brendan Gregg's FlameGraph (CDDL 1.0); the flame graph renderers (`myflames/flamegraph.py` and the workspace chart) were rewritten as original, clean-room implementations from a functional specification, so no FlameGraph code remains. Releases up to and including 2.3.0 remain under CDDL 1.0. The inspiration credit to Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs is unchanged.

## [2.3.0] (2026-09-30)

### Added

- `scripts/demo-db.sh` starts a local MySQL demo container with the fixture
  schema and a read-only `demo` user for trying live capture and the UI.
- README query samples for an inner join and a left join on the same tables,
  with Visual Explain screenshots and interactive demos in
  `docs/demos/mysql-joins/`. `scripts/regenerate_docs_demos.py` rebuilds them.
- Query investigation workspace with neutral colors, system typography, a
  persistent inspector, and responsive layouts.
- Linked selection across all five charts; branch focus/collapse, metric bars,
  bottleneck shortcuts, and contextual Teach navigation.
- Explicit local investigations, notes, tags, named experiments, baselines,
  portable bundles, and connection profiles without stored passwords.
- Live query laboratory and `capture` CLI with typed parameters, estimated or
  analyzed plans, repeated measurements, cancellation, and time limits.
- Captured schema/index details, statement statistics, session status deltas,
  and searchable optimizer traces with availability notices.
- `explore` CLI for interactive selection, focus/collapse, and metric inspection.
- Structural plan matching with confidence and added/removed/changed/uncertain
  states. Missing timings remain distinct from measured zero.
- Canonical preservation of MySQL SELECT-list subquery branches.
- Workbench view in the UI and `--type workbench`: operator colors, join glyphs,
  estimated-row arrows, full parsed tree, pan/zoom/search, node inspection, and
  portable SVG/HTML exports. Independently implemented from Workbench 26.7
  visual conventions with the existing Python parser and analysis.
- Teach in the local workspace: searchable lesson catalog, family filters,
  embedded interactive lessons, curriculum navigation, and offline HTML downloads.
  Lesson metadata and content come from the existing Python registry.
- `myflames ui`: local React/TypeScript workspace for saved MySQL and MariaDB
  plans. Import files or paste JSON, inspect six chart views and operator
  details, compare plans, and download HTML reports or JSON analysis.
- Bundled frontend assets ship with Python, with no Node.js runtime requirement.
  The stdlib server binds to loopback and supports `--port` and `--no-browser`.
  Plans remain in memory for the page session.
- **Advisor names the index that removes a filesort.** When a plan has a
  single-base-table `ORDER BY` filesort on plain columns, myflames now suggests
  the ordered/covering index (`CREATE INDEX … (col1, col2 [DESC])`, preserving
  each column's sort direction) that lets MySQL read rows already sorted and
  skip the O(n log n) sort; not just "grow the sort buffer." The parser threads
  the sort columns through (MySQL `sort_fields`, MariaDB `sort_key`); the rule
  deliberately bails to the generic hint on joins, aggregates, or expression
  sort keys, where a naive single-table index cannot satisfy the order. Every
  claim was verified against MySQL/MariaDB server source.
- **`semijoin_firstmatch` teach lesson**: the flagship of the semijoin cluster,
  animating the FirstMatch early-out: a `WHERE col IN (subquery)` asks
  *does-it-exist*, not *how-many*, so the inner scan stops at the first match.
- **`--quiet/-q` on `digest`, `advise`, and `compare`** (previously only on
  `check`). Uniform contract across subcommands: suppress incidental stderr
  diagnostics (the `Written to <path>` line, digest's tokenizer `Note:`); stdout
  data and the exit code are never affected.
- **Zero-install browser playground linked from the README**: paste a plan and
  render it client-side via Pyodide, installing the published PyPI wheel with
  micropip.

### Changed

- Simplified stacked-flame app mark for the README, sidebar, welcome screen, and
  favicon. The original `myflames.jpeg` stays in the repository.
- Merged Diagram and Workbench into one Visual Explain view; existing `diagram`
  commands and Python calls remain compatible aliases.
- Enlarged join symbols with Venn regions for reported join semantics, including
  the whole left circle for left joins. Unknown join types remain unshaded.
- Smoothed mouse and trackpad zoom with proportional deltas and bounded momentum.
- **The MCP `analyze_plan` tool now returns the token-cheap decision-grade
  analysis by default** (`detail="core"`): plan summary, warnings, suggestions,
  index hints, optimizer-switch explanations, and the operator tree. This drops
  the heavy `collected` (schema/stats/variables), `query`, and `teach_hooks`
  blocks that an agent rarely needs, honoring myflames' token-efficiency thesis
  on the surface agents call by default. Pass `detail="full"` to restore the
  complete record. The `detail` gate lives on `build_sidecar` as the single
  source of truth; a written `.sidecar.json` and the `digest` path are
  unaffected (they stay `full`). `detail="full"` output is byte-identical to
  prior releases, so consumers pinned to the full shape can opt back in.
- **The "Fix first" primary action is ranked by one policy, shared with
  `advise`.** The HTML card and the ranked findings list previously used
  different heuristics and could disagree; both now dereference
  `findings.primary_suggestion_index` / `build_findings`, so a medium-severity
  suggestion correctly outranks a low one everywhere. When a plan has no
  findings at all, the report shows an explicit "No blocking issues found" card
  instead of a blank space.
- **The before/after compare report now carries the "Made with myflames"
  attribution and the Brendan Gregg / Tanel Poder inspiration credit**, matching
  the main HTML report footer.

### Fixed

- Align the saved-investigations heading, show loading and empty search states,
  and add a clear-search action. Trace search also reports no matches.
- Restore the correct contextual Teach lesson when reopening an operator link.
- Render code spans in Teach explanation cards without exposing literal tags.
- Label operator search for assistive technology and describe estimated
  comparisons without implying measured runs.
- Visual Explain reports the current zoom percentage, shows button press
  feedback, and disables zoom directions at their limits. Click the percentage
  to reset to 100%.
- Version the favicon URL so Chrome refreshes its cached tab icon after the
  app-mark update.
- Import dialog file-picker overlap, narrow-screen wrapping, and scrolling on
  short windows; empty drops and canceled file selections no longer reopen the picker.
- Logo requests from older open UI tabs continue to work after a rebuild; new
  builds use one stable logo URL for the sidebar, welcome screen, and favicon.
- Restore bar-chart and treemap search controls by emitting valid JavaScript
  modulo operators.

### Review fixes (2026-09-15)

- Preserve raw EXPLAIN JSON from the database client and escape passwords in
  option files, including literal backslashes and control characters.
- Preserve table references when SQL literals contain comment markers; handle
  MySQL `#` comments and subtraction of negative numbers correctly.
- Exclude primary-key and covering ranges from secondary-index MRR suggestions.
- Apply the correct temporary-table limit for MySQL TempTable versus MEMORY and
  MariaDB. Buffer-pool and sort suggestions now distinguish measured evidence
  from configuration-based hints.
- Classify scalar and streaming aggregates as O(n), accounting for DISTINCT
  separately. Leave unmodeled ordered/multidimensional aggregates unclassified.
- Keep embedded JSON-LD valid when SQL contains HTML comment markers, and avoid
  splitting XML entities when truncating bar-chart annotations.
- Convert bar-chart self and total times consistently to microseconds without
  mutating the parsed plan.
- Return exit code 2 and a diagnostic for render/compare input and output errors.
- Preserve repeated operators in HTML and JSON comparisons instead of dropping
  every occurrence after the first.
- Freeze lesson delays when paused, apply speed changes during playback, and
  resume the correct tween position after seeking.
- Handle malformed comparison share links and preserve an explicitly empty
  selection in the teaching explorer.
- Make the MySQL fixture image, output directory, and container name configurable;
  refuse to delete an existing container and clean up owned containers on failure.
- Rewrite the README around installation and common workflows, correct MySQL's
  JSON-format setup in the quick start, and add opt-in current-MySQL corpus tests.

Validation details and remaining limitations: [September review](docs/reviews/2026-09-15.md).

## [2.2.0] (2026-06-30)

### Fixed

- **Advisor no longer recommends a no-op MRR setting.** When `optimizer_switch`
  has `mrr=off`, MRR is globally disabled and `mrr_cost_based` has no effect, so
  the advisor now suggests `mrr=on` (which re-enables MRR; `mrr_cost_based`, on
  by default, still gates it per query) instead of the previous `mrr_cost_based=on`.
- **Visual Explain diagram no longer drops branches of a 3+-way join/UNION.**
  `build_diagram_steps` now emits every child of a multi-input node (e.g. a
  multi-branch `UNION ALL` Append), not just the first two.
- **`parse_explain` raises a clear `ValueError` for valid-but-non-object JSON**
  (a scalar or array) instead of leaking an opaque `TypeError`. This also fixes
  the MCP tools crashing on such input.
- **`check` rejects an unrecognized `--fail-on` trigger** (exit code 2) instead
  of silently matching nothing and exiting 0; a misspelled trigger no longer
  turns a CI gate green.
- **`render` exits 2 (bad input), not 1, when a plan has no flame-graph data**
  (all-zero self-time), keeping exit code 1 reserved for a tripped finding.
- Corrected MariaDB BNLH advice (the lever is `join_cache_level >= 3`, since
  `join_cache_hashed` is on by default), the `rowid_filter` version (MariaDB
  10.4+, not 10.5+), the non-sargable-join wording (a functional index on the
  exact expression *can* be used), and the `const`/`system` access-type
  complexity (O(1), read once, not O(log n) per outer row).
- Tooltip attribute escaping is now consistent across all renderers (newlines
  encoded, `'` escaped); long tooltips are truncated before escaping so a
  multi-byte entity can't be clipped mid-token.

### Documentation

- Documented the `--no-teach-bundle` and `--refresh-teach-bundle` render flags.

## [2.1.0] (2026-06-05)

### Changed

- **Renamed the `tokens` subcommand to `digest`.** The command's product is the
  digest; the compact, source-grounded projection of a plan you hand an LLM,
  so the name now advertises that. Bare `myflames digest plan.json` emits the
  digest text (previously `tokens --digest`); the token + $ savings report moved
  to `myflames digest --cost` (previously the bare `tokens` default). `--show`
  became `--show-prompts`. The old `--exact` flag was removed in favor of
  `--tokenizer claude`. The internal module `myflames/tokens.py` is now
  `myflames/digest.py`.
- **Renamed the `findings` subcommand to `advise`** (a verb, consistent with
  `compare` / `check` / `digest`, and with the advisor). Flags are unchanged.
- Both old names remain as **deprecated aliases**: `tokens` and `findings` still
  run, print a one-line deprecation notice to stderr (never stdout), and
  preserve their previous behavior (`tokens` defaults to the savings report).
  They will be removed in a future release.

### Added

- **Exact GPT token counts, keyless**: `myflames digest --cost --tokenizer gpt`
  uses tiktoken (optional extra `pip install 'myflames[gpt]'`) to count tokens
  exactly for GPT models. No API key or network needed.

## [2.0.2] (2026-06-05)

### Added / Verified

- **MySQL 9.7 support, verified against a live server** (and the checked-out
  9.6 source). A broad battery (scan / pk / range / join / group-by / filesort /
  semijoin / antijoin / index_merge / derived / window) was captured on real
  MySQL 9.7 under **both** the traditional and **hypergraph** optimizer; 24/24
  parse, validate, and detect correctly. The 9.x `query_plan` envelope
  (`{query, query_plan, query_type, json_schema_version}`) and new access types
  (`temp_table_aggregate`, `rows_fetched_before_execution`) are handled; the
  hypergraph optimizer's FirstMatch semijoin is now picked up. Real 9.7
  fixtures + regression tests added (`test/mysql-9.7-*.json`). Fully backward
  compatible; every 8.4 / 10.11 / 11.4 fixture still passes.
- **MariaDB 11.8 support, verified against a live server** (11.8.8). Same
  battery captured via `ANALYZE FORMAT=JSON`; 11/11 parse, validate, and
  detect; real fixtures + tests added (`test/mariadb-11.8-*.json`).

### Docs

- Documented how to use `--exact` with **your own** `ANTHROPIC_API_KEY`
  (export the env var; example uses a clearly-fake placeholder key), and stated
  explicitly that myflames reads the key from the environment at call time and
  **never stores it** anywhere. README + walkthrough + `--exact` help text.

### Fixed

- **`myflames tokens --exact` now degrades gracefully when no key is
  configured.** The Anthropic SDK does not validate auth at construction (it
  only fails when a request is made), so a regular user who had installed the
  `[tokens]` extra but had no `ANTHROPIC_API_KEY` got an uncaught `TypeError`
  mid-run. `make_counter` now probes `count_tokens` (a free endpoint) up front
  and falls back to the offline heuristic with a clear stderr note
  (`--exact unavailable (no ANTHROPIC_API_KEY)`) instead of crashing.

## [2.0.1] (2026-06-05)

Advisor correctness pass. Every claim was verified against real MySQL 8.4
plans (with optimizer switches toggled on and off on a live server) and the
MySQL server source, rather than against documentation or assumption.

### Fixed

- **Main finding is ranked by impact, not a fixed category order.** A trivial
  50-row scan no longer headlines over a 16,000-row sort: among scan / sort /
  temp-table findings, the one that touches the most rows (rows × loops) wins
  (cost is ~proportional to rows processed). Join-strategy findings (BNL, hash
  join) still lead when present.
- **Trivial full scans are no longer flagged.** A scan touching fewer than
  ~100 rows total (rows × loops) cannot be helped by an index; for a narrow
  InnoDB table it is one or two pages, and the optimizer itself declines an
  index there; so "full table scan, add an index" was noise. Gating on
  rows × loops keeps a small inner table of a large join flagged.
- **Advisor no longer flags scans of derived tables / CTEs / `<temporary>`
  results as base-table full scans.** A "table scan" over a materialized
  intermediate (the `GROUP BY`/`DISTINCT` temp table, a derived table, or a
  CTE) is not a missing-index problem; you cannot index a result that only
  exists at query time. The structural signal is that such a scan node has a
  sub-plan (children); a real base-table scan is a leaf. The `full_scans`
  detector now excludes both angle-bracket pseudo-tables (`<temporary>`,
  `<derived N>`) and named derived/CTE aliases (`agg`, `prod_stats`, …).
- **`use_index_extensions` is no longer reported.** It was keyed on
  `details["covering"] is True`, but a covering-index read is not evidence the
  switch was used; verified against MySQL 8.4 by toggling the switch on/off
  (the plan, including the covering read, is identical either way; the flag is
  not surfaced in EXPLAIN at all). A covering read is now surfaced as a plain
  `covering index` feature instead, never as an optimizer_switch claim.
- **`index_merge` sort-union is now detected on MySQL 8.4.** Real v2 output is
  `access_type=index_merge`, operation `"Sort-deduplicate by row ID"`
  (mysql-server `explain_access_path.cc:1382`); myflames matched neither and
  detected nothing. It now recognizes the sort-union operation text and a
  generic `index_merge` access type as a fallback. Real-capture fixtures added.

## [2.0.0] (2026-06-05)

Major release. A full `EXPLAIN ANALYZE FORMAT=JSON` plan is large and
repetitive, which makes it expensive to pass to an LLM and unusable from an
SVG. 2.0 adds a compact, source-grounded text digest of a plan and the tooling
around it: a token and cost comparison, agent and CI subcommands, and an MCP
server, while keeping the core package dependency-free (Python stdlib only).
The same analysis is produced once and rendered several ways: SVG for people,
the digest or JSON for tools, an exit code for CI.

### Added

- **`tokens` subcommand**: quantifies the saving from handing an LLM the
  myflames digest instead of the raw plan: side-by-side token counts and USD
  cost (Opus 4.8 / Sonnet 4.6 / Haiku 4.5 input pricing), `--digest` to emit
  the digest itself, `--show` to print both prompts, `--exact` for real Claude
  counts via Anthropic `count_tokens` (optional `myflames[tokens]` extra),
  `--json` for machine output. New module `myflames/tokens.py`
  (`build_digest`, `build_compare_digest`, `estimate_tokens`, `compare`).
- **`findings` subcommand**: ranked warnings + suggestions, each with a
  `confidence`, as text or `--json`. New module `myflames/findings.py`.
- **`check` subcommand**: CI gate: `--fail-on <categories|severities|any>`
  exits non-zero when the plan trips a trigger. Lives in `myflames/findings.py`.
- **`diff` alias** for `compare`, plus `--digest` (token-cheap text diff) and
  `--json` (structured `compare-1.0` delta) modes.
- **MCP server** (`myflames-mcp`, optional `myflames[mcp]` extra) exposing
  `analyze_plan`, `digest_plan`, `compare_plans`, `explain_optimizer_switch`,
  and `explain_query` so agents call myflames directly. New module
  `myflames/mcp_server.py`; tool logic is pure stdlib and unit-tested.
- **"Agent-ready" panel** in every HTML report showing the token/cost saving,
  plus a "Made with myflames" footer (carrying the Brendan Gregg / Tanel Poder
  inspiration credit).
- **`ROADMAP.md`**: contributor-facing vision, shipped/next, and non-goals.
- **Browser playground scaffold** ([docs/playground/](docs/playground/)),
  client-side Pyodide page (pending a published wheel).
- **Live, reproducible token-savings walkthrough**
  ([docs/examples/](docs/examples/)) backed by a real MySQL 8.4 Docker script.
- Optional dependency groups: `myflames[mcp]`, `myflames[tokens]`.

### Changed

- **Exit-code contract unified across all subcommands:** `0` success, `1` a
  gate/finding tripped (`check`), `2` bad input (unreadable/missing file,
  unparseable JSON). The default render path previously exited `1` on a parse
  error; it now exits `2`. (Behavior change → major bump.)
- `tokens` and `findings` gained `-o/--output`; machine output now goes to
  stdout while diagnostics (including `check`'s status line) go to stderr, so
  `| jq` and redirects stay clean.

### Internal

- `myflames[tokens]` exact counting uses Anthropic `count_tokens`, never
  `tiktoken` (which mis-counts Claude tokens). The offline heuristic is the
  zero-dependency default and is labelled as an estimate everywhere it surfaces.

## [1.5.0] (2026-04-25)

Minor release focused on **correctness, identity, accessibility, and
authoring ergonomics**. Highlights: advisor rules verified against MySQL 8.4
source (multiple incorrect claims fixed), every plan node gets a stable
`node_id` referenced across JSON / SVG / warnings, every report ships with a
JSON-LD envelope + cross-link to its sibling sidecar, the HTML report's
"Collected environment" panel is a clickable accordion with human-readable
byte values, and lesson JavaScript now lives in real `.js` files with a
headless-browser regression harness that catches the kind of silent JS bugs
`node --check` misses.

Schema bump: sidecar **`1.2` → `1.3`** (added `$schema` URL, `plan_tree`
identity graph, `node_id` references, `environment_findings` digest). New
**`compare-1.0`** schema for the diff sidecar emitted alongside the
comparison report.

### Added

#### Advisor correctness (verified against MySQL / MariaDB source)

- **M1; BNL / `hash_join` advice rewritten.** The pre-1.5 advice
  recommended `optimizer_switch='hash_join=on,block_nested_loop=off'`. That's
  wrong on MySQL 8.0.20+: `hash_join` is defined in `sql/sql_const.h:221` but
  never checked anywhere in the planner, and `block_nested_loop=off` actually
  *disables* the BNL→hash rewrite the executor performs at runtime
  (`sql/sql_executor.cc:~2891`). The new rule explains directly and
  recommends an index instead. MariaDB plans (detected by `join_cache_hashed`
  in `optimizer_switch`) get a separate, correct branch.
- **M2; MRR rule gated** on a detected secondary-index range scan; never
  forces `mrr=on` (only suggests `mrr_cost_based=on` so the cost-based gate
  stays in charge; important on SSDs where MRR is often a regression).
- **M3; BKA + BNL glossary corrected.** BKA is **off by default**
  (verified in `OPTIMIZER_SWITCH_DEFAULT` in `sql/sys_vars.cc:213`).
- **M4; MariaDB `range-checked-for-each-record` normalization** added.
- **M5; `innodb_flush_log_at_trx_commit` durability split.** `=2` (OS
  crash only) and `=0` (any crash including mysqld) now have distinct
  rule_ids, severities, and Why explanations.
- **Advisor rule tagging.** Every finding carries a `rule_id` + `severity`
  so external CI can gate on specific rules without scraping prose.

#### Identity + schema backbone

- **Stable `node_id`** on every parse-tree node. Derived from the canonical
  tuple `(operation, table_name, access_type, key, sibling_position)` walked
  from root, hashed to `n:` + 12 hex chars. Same fixture → same ids on every
  parse. Test: `test/test_slice1_contracts.py::TestNodeIdStability`.
- **`$schema` URL** in every sidecar pointing at the published JSON Schema
  (`docs/schemas/sidecar-v1.json`).
- **`plan_tree` identity graph**: compact tree of `{node_id, short_label,
  children}` so external consumers can resolve any `node_id` reference back
  to a position + label without parsing SVG.
- **`operator_complexities[].node_id`** added.

#### HTML report polish

- **Design tokens** in `:root`; `--font-{ui,mono}`, `--sp-{1..6}`
  (4/8/12/16/24/32), `--rad-{sm,,lg}`, `--dur-{sm,md,lg}`,
  `--ease-{out,in-out}`.
- **Accessibility floor.** Global `:focus-visible` ring, darkened `--muted`
  for WCAG AA, skip-link.
- **`prefers-reduced-motion: reduce`** guard collapses every animation
  duration to ~0 across HTML, the standalone complexity SVG, and the Tier-1
  lesson runtime.
- **JSON-LD envelope**: sidecars wrap as `{ "@context":
  "https://myflames.dev/ns/v1", "@type": "QueryPlanAnalysis", "@id": ... }`
  so search crawlers and LLM retrieval pipelines parse correctly.
- **`<link rel="alternate" type="application/json">`** in every HTML
  `<head>` pointing at the sibling sidecar.
- **Header metadata strip** (D5); two-row sticky header with title +
  toolbar above engine / version / operator-count / total-time / generated-at.
- **Glossary chips become anchors.** `<abbr>` upgraded to `<a
  href="#gloss-{key}">` with `:target` highlight + a JS shim that
  auto-opens the collapsed `<details>`. Sibling **"Learn → "** button on
  chips whose key has a matching teach lesson.
- **Primary-action card always carries a `why`**: `_why_fallback()`
  derives a non-empty rationale from the suggestion's `category`.
- **Concrete `CREATE INDEX` DDL** in the primary-action card when the
  primary suggestion has `category == "index"`.
- **myteach hub.** New `myflames teach --index -o PATH` emits the
  centralized algorithm catalog HTML; every report has a "myteach" section
  below the glossary linking to it and listing the lessons relevant to
  *this* plan.
- **Collected environment panel rewrite.**
  Bytes humanized (`134217728` → `128 MB`, raw bytes in tooltip).
  `optimizer_switch` collapsed into a 27-flag chip list, color-coded
  `=on` / `=off`. Tables card replaces the previous Stats + Schema cards
  with a clickable per-table accordion: click the table name → expands
  columns + indexes inline with PK / UNIQUE / INDEX badges.

#### Visualization

- **V1; categorical flame palette** keyed to the 12 canonical access_type
  families. Severity red stays reserved for advisor chrome (D4).
- **V3; squarified treemap.** Bruls/Huijsen/van Wijk replaces the old
  slice-and-dice; cells stay near 1:1 aspect ratio.
- **V4; unified search counter.** Bargraph and treemap report honest
  `Match N/M`, scroll-to-next via `n` / `N` / `Esc`.
- **V5; shared `fit_label()`** with Unicode middle-ellipsis and CJK-aware
  width estimation. Migrated bargraph, treemap, diagram.
- **viewBox** now emitted by bargraph and treemap (CLAUDE.md SVG rule was
  previously unenforced).
- **Hash chart winner annotation.** When a complexity chart sets
  `lowerIsBetter: true`, the y-label gets `(lower = better ↓)` and the lower
  curve at the cursor gets a green ✓ + a "N× cheaper" pill.
- **Full-scan scanner is stroke-only** (was a 0.95-opacity overlay that
  silently covered the row text underneath).

#### teach lessons (Tier 0 + Tier 1)

- **Tier 0; JS authoring ergonomics.** Every lesson's JavaScript was
  extracted from Python raw-string literals into a sibling `<lesson>.js`
  file loaded at render time via `_html.load_lesson_js(__file__)`. Editor
  syntax highlighting, eslint, prettier, and `node --check` all work now.
- **Tier 1; Motion One + d3 helpers via committed bundle.** New
  `assets/` workspace with esbuild + TypeScript builds
  `myflames/assets/anim-runtime.js` (68 KB minified, 25 KB gzipped) which
  installs additive helpers on `window.anim`: `flip`, `spring`, `squarify`,
  `smoothPath`. Bundle is checked in; end users never touch npm.
- **`anim.arrival(el, opts)` primitive**: universal "this just landed"
  pulse used in every lesson's `onComplete` for visual consistency.
- **`lesson_stage()` scaffold** in `_html.py` that returns the four blobs
  `render_page` expects.
- **New lessons.** `teach/scan_family/covering_index.py`,
  `teach/cache_family/buffer_pool_warmup.py`. Both verified against the
  MySQL 8.4 source tree (`storage/innobase/dict/dict0dict.cc:3149` for the
  PK-append behavior, `ha_innodb.cc:22692` for the dump_pct default).
- **`nested_loop` flagship rewrite**: replaced state-swaps with tweens,
  added arc'd probe pills via `anim.path` with 80 ms stagger, added
  per-driver match verdicts ("Acme id=1 → 2 orders ✓").
- **LRU lerpColor flashes + staged verdict fade-ins** (A4); act
  boundaries fade their conclusion in over 400 ms instead of appearing
  instantly.
- **`buffer_pool_warmup` + `covering_index` Play-button wiring** fixed,
  were binding to `#btn-play` directly instead of via `wireToolbar`.
- **`BASELINE_SPEED_SCALE`** retuned 0.52 → 0.42 (~20% slower).

### Fixed

- **Critical: `%%` in lesson JS was emitted as invalid JavaScript.** Six
  lessons had `var col = i %% colsPerRow` in their raw-string templates,
  a Python `.format()` escape mistakenly applied to lessons that never went
  through `%`-formatting. Result: `SyntaxError` at script-parse time that
  silently killed the whole lesson's animation in the browser.
  `node --check` revealed it immediately.
- **`tl.tween` was never a thing.** `skip_scan.js` called
  `tl.tween(stage.cursor, {x,y}, 300, ease)`; that method doesn't exist
  on the timeline API. Rewritten to `tl.call`-then-`tl.add({onUpdate})`.
  Caught by the new headless harness on its first run.
- **`<circle cy="NaN">` in `non_unique_lookup.js`.** Stage object missing
  its `idxY` field; `resetStage()` did `setAttribute("cy", undefined +
  220)` → NaN. Caught by the harness.

### Test infrastructure

- **`assets/verify-animations.mjs`**: headless Chromium harness that
  loads every lesson, captures every `console.error` / `pageerror`, clicks
  Play, and verifies the SVG byte-snapshot diverges after ~1.5 s of
  animation. Three pre-existing silent JS bugs found on first run.
- **`test/test_slice1_contracts.py`**: 7 contracts (advisor digest
  goldens, MariaDB normalization invariants, `node_id` stability,
  sidecar `$schema`, `plan_tree`, `operator_complexities[].node_id`).
- **`test/test_compare_sidecar.py`**: round-trip + classification +
  node-id validation for the new `compare-1.0` sidecar.
- **`test/test_labels.py`**: pure-function tests for `fit_label()`
  including CJK and surrogate-pair cases.
- **`test/test_svg_contract.py`**: `assert_svg_contract()` helper:
  asserts viewBox presence, no `calcMode="linear"` on SMIL, no unbounded
  SMIL without a class/id/begin handle.

### CI

- **`.github/workflows/fixtures-drift.yml`**: nightly cron regenerates
  fixtures from `mysql:8.4` and `mariadb:11.4` Docker images and fails on
  drift.
- **`.github/workflows/integration.yml`**: matrix integration tests
  against live MySQL 8.4 + MariaDB 11.4 service containers with
  `MYFLAMES_REQUIRE_LIVE=1` so connector tests fail rather than silently
  skip.

### Tests

`1379` (1.4.0) → **`1423`** green at 1.5.0 (44 new). Headless harness
reports **20 / 20 lessons passing**.

### Build dependencies

The Tier-1 bundle pulls these at build time only; they don't ship with
the wheel and end users don't run npm:

- **Motion One** (MIT); animation runtime.
- **d3-hierarchy** (ISC); squarified treemap layout.
- **d3-shape** (ISC); Catmull-Rom curve generators.
- **esbuild** (MIT, dev); bundler.
- **TypeScript** (Apache-2.0, dev); runtime types.
- **Puppeteer** (Apache-2.0, dev); headless browser harness.

All licenses verified by fetching upstream LICENSE files.


## [1.4.0] (2026-04-23)

Minor release adding **Big O complexity annotations to every operator in every
view**, a **JSON sidecar schema bump** (1.2), and a refreshed README with
inline screenshots. People who use myflames to learn MySQL internals can now
see `O(n · log m)`, `O(n · m)`, `O(log n + k)`, …  on each operator, backed
by a vetted decision table and a shared severity palette.

### Added

- **`myflames/complexity.py`**: single source of truth for per-operator Big O
  annotations. `compute_complexity(node, parent=None)` returns a dict with
  `big_o`, `short`, `severity` (`good` / `medium` / `bad`), `rationale`,
  `confidence` (`exact` / `typical` / `worst_case`), and an optional glossary
  `learn_more` key. Returns `None` on uncertainty so renderers can omit
  chips rather than display a misleading value.
- **`myflames/complexity_legend.py`**: shared SVG legend fragment
  (`render_complexity_legend_svg`) embedded at the bottom of the diagram,
  bargraph, and treemap views so a newcomer can decode the chips without
  leaving the page.
- **Big O surfaces in every renderer**:
  - **Flame graph**: colored severity dot at the right edge of every bar;
    compact `O(...)` appended to the bar label when there is ≥ 120 px of
    width available; tooltip gains a `Complexity: O(...)` line.
  - **Bar chart**: dedicated **COMPLEXITY** column between the label and the
    loops count (auto-hidden below 900 px canvas width), with color-coded
    chips matching the severity palette.
  - **Treemap**: `data-complexity="O(...)"` attribute on every tile, plus a
    corner chip on tiles larger than `80 × 40 px`.
  - **Diagram**: colored chip below each access box and below each join
    diamond. The chip lives on the join frame only; inner children of a
    join never duplicate the chip; so the `O(n · m)` / `O(n · log m)`
    story sits in the right place.
- **JSON sidecar schema 1.2**: new optional top-level
  `operator_complexities` array, one entry per operator node that carries
  complexity metadata. Each entry is keyed by `folded_label` and
  `short_label` (matching the existing `teach_hooks` convention) and
  contains the full complexity dict. The array is omitted entirely when no
  node has complexity metadata, keeping payloads minimal and avoiding any
  shape surprise for consumers pinned to 1.1.
- **`test/test_complexity.py`**: 41 unit tests, one per row of the decision
  table plus contract and palette tests. The decision table is the
  authoritative reference for what Big O class we claim for each operator.
- **README refresh**: hero diagram, an "output types" table with thumbnail
  previews of each view, and a "What does the output look like?" section
  highlighting the Big O chips. Screenshots live under `docs/screenshots/`.
- **docs/VISUAL_EXPLAIN_REFERENCE.md**: new section documenting the
  complexity dict shape, the per-renderer surface area, the severity
  palette, and the schema 1.2 sidecar contract.

### Changed

- **`myflames/parser.py`**: `parse_node` now attaches
  `details["complexity"]` once per node via `compute_complexity(node)`;
  downstream consumers read the same field.
- **`myflames/output_sidecar.py`**: `SCHEMA_VERSION` bumped to `"1.2"`;
  `validate_sidecar()` extended to validate `operator_complexities` entries
  (severity enum, confidence enum, required string fields, optional
  `build_complexity` / `scan_complexity` sub-dicts for Materialize).
- **`myflames/flamegraph.py`**: `folded_to_svg` accepts a new optional
  `complexity_by_folded` mapping; `enhance_tooltip_flame` (in
  `parser.py`) appends a `Complexity: ...` line to flamegraph tooltips.
- **`.github/workflows/test.yml`**: broadened the discovery pattern from
  `test_myflames.py` to `test_*.py` so the new `test_complexity.py` and
  `test_sidecar.py` suites run in CI.

### Notes on correctness

The complexity claims were vetted against the MySQL 8.4 and MariaDB 11.x
cost model via the `/mysql-expert` skill before landing:

- Nested loop with indexed inner (`ref` / `eq_ref` / `range`) is reported
  as `O(n · log m)` (exact). Block Nested Loop is `O(n · m)` (exact) and
  Batched Key Access gets its own distinct `O(n · log m)` entry so the
  pedagogical difference is preserved.
- Hash join is `O(n + m)` (exact). Disk-spill does not change the
  asymptotic class, only the constants; the rationale says so.
- Filesort defaults to `O(n log n)` at `worst_case` confidence; a future
  release will narrow to `O(n log k)` for priority-queue filesort once
  outer-LIMIT parsing lands.
- Skip scan is `O(d · log n)` at `typical` confidence with `d = distinct
  prefix values, not exposed by EXPLAIN` called out in the rationale.
- Materialize emits two-phase `build_complexity` + `scan_complexity`
  rather than a single `big_o` string; collapsing into one would mislead
  students about how `actual_loops` compounds with the build phase.

### Deferred

- Priority-queue filesort detection (needs outer LIMIT parsing, which
  `parse_explain` does not walk today).
- Per-node disk-spill rationale for hash join (needs `join_buffer_size`
  awareness).

## [1.3.1] (2026-04-13)

Feature and maintenance release adding **interactive teach lessons**,
**modernized Visual Explain diagrams**, and a thorough repo cleanup.

### Added

- **Interactive algorithm teach lessons (`myflames/teach/`)**: five
  self-contained, animated HTML lessons that visualize how MySQL internals
  work: **B-tree index lookup**, **Block Nested Loop (BNL)**, **Hash Join**,
  **Join algorithm comparison**, and **LRU buffer pool cache**.
  - YouTube-style scrubber for stepping through animation frames.
  - Interactive complexity charts showing algorithmic cost curves.
  - Pause, speed control, and real SQL examples baked into each lesson.
  - Rendered demos served via GitHub Pages at `docs/teach/`.
- **Modernized Visual Explain diagram**: hotspot layer with viridis
  heat-map palette, borders-only hotspot marks, and WCAG-compliant text
  contrast.

### Removed

- **4 legacy root-level Python scripts** (`mysql_explain.py`,
  `mysql_explain_bargraph.py`, `mysql_explain_flamegraph.py`,
  `stackcollapse_mysql_explain_json.py`); thin wrappers superseded by
  the unified `myflames` CLI entry point since v1.0.
- **91 legacy Linux perf test fixtures** (`test/perf-*.txt`,
  `test/results/`); inherited from the original FlameGraph fork,
  unused by the MySQL-focused test suite.
- **Dead code**: `render_analysis_panel()` from `parser.py` (replaced by
  `render_info_panel()`), plus 6 associated tests.
- **Empty `requirements.txt`**: `pyproject.toml` is the authoritative
  dependency spec.

### Changed

- **Extracted inline CSS/JS from `output_html_report.py`** into separate
  `output_html_report.css` (369 lines) and `output_html_report.js`
  (138 lines) files for proper syntax highlighting, linting, and cleaner
  diffs. Module reduced from 1261 to 758 lines.
- **Moved internal dev artifacts** out of `docs/`:
  - `docs/prompts/` → `.claude/prompts/`
  - `docs/teach/ALGORITHM_ROADMAP.md` → `.claude/ALGORITHM_ROADMAP.md`

## [1.3.0] (2026-04-10)

A large quality-of-life release focused on **three audiences at once**:
newcomers, senior DBAs, and AI agents. The main additions are a live-
connection mode, a machine-readable sidecar, progressive-disclosure HTML
reports, a non-sargable join detector, and a +236 test growth (997 → 1233).

### Added

- **Live-connection mode (`-h host -P port -u user -p -D db -e "SELECT ..."`)**,
  myflames can now connect directly to a MySQL 8.4 or MariaDB 10.11/11.4 server
  using the same flags as the `mysql` CLI, run `EXPLAIN ANALYZE FORMAT=JSON` (or
  MariaDB's `ANALYZE FORMAT=JSON`) for you, collect server state, and render the
  result in one invocation. Replaces the need to pipe `mysql -e '...' | myflames`
  for simple cases.
  - Uses the real `mysql` / `mariadb` client binary under the hood; **no PyMySQL
    or driver install**, stays stdlib-only, authenticates via whatever plugin the
    server uses (`mysql_native_password`, `caching_sha2_password`, Kerberos, etc.).
  - Passwords go into a mode-0600 `--defaults-extra-file` temp file; never
    visible in `ps`, process env, or logs. Temp file is deleted on exit.
  - Supports every SSL flag the `mysql` client supports:
    `--ssl-mode`, `--ssl-ca`, `--ssl-cert`, `--ssl-key`. Verified against RDS
    with `--ssl-mode=VERIFY_IDENTITY --ssl-ca=global-bundle.pem`.
  - `-p` with no value prompts; `-p'password'` inline works; `-p` alone works.
  - `--mysql-binary /path/to/mysql` overrides the binary autodetection when
    you want a specific client version.
- **Live collectors (on by default; individually toggleable)**: when live mode
  is active, myflames also captures:
  - `SHOW CREATE TABLE` for every table the query touches (`--no-collect-schema`
    to disable)
  - Row counts / data length / index length from `information_schema.tables`
    (`--no-collect-stats` to disable)
  - A curated subset of `SHOW SESSION VARIABLES` the advisor inspects; buffer
    pool, sort/join/tmp buffers, `optimizer_switch`, durability settings
    (`--no-collect-variables` to disable)
- **Environment advisor (`myflames/advisor.py`)**: eight rules that combine the
  parsed plan with the collected environment data and emit concrete, copy-paste-able
  tuning suggestions with a `Why:` clause grounded in the MySQL cost model:
  - `innodb_buffer_pool_size` vs the working set of tables touched
  - `sort_buffer_size` vs filesorts (in-memory vs disk-merge trade-off)
  - `join_buffer_size` vs hash joins / BNL (separate explanations for each)
  - `tmp_table_size` + `max_heap_table_size` vs materialized temp tables
    (both must be raised together; MySQL always picks the smaller)
  - `optimizer_switch` overrides (`hash_join=off`, `mrr=off`,
    `derived_condition_pushdown=off`)
  - Missing indexes; cross-checks plan heuristics against the collected schema
    so we only flag indexes that **actually don't exist**
  - MyISAM / non-InnoDB engine detection
  - `innodb_flush_log_at_trx_commit` on UPDATE/DELETE/INSERT queries
- **Non-sargable join predicate detection**: a new advisor rule walks every
  join condition looking for function calls on column references
  (`CONCAT(col)`, `CAST(col AS ...)`, `LOWER(col)`, `DATE(col)`,
  `CONVERT(col USING ...)`, 30+ functions total) and flags them as the
  **primary action** because no index or buffer tweak can help until the
  predicate is rewritten. Emits severity `error`, category `nonsargable_join`.
  Cross-engine: works on MySQL hash joins (via `hash_condition`) and MariaDB
  BNL joins (via `block-nl-join.attached_condition`, now preserved through
  normalization).
- **JSON sidecar (v1 schema)**: every `--output` invocation now writes a
  `<base>.json` next to the SVG/HTML with a stable, versioned, schema-validated
  structured summary of the analysis. Consumable by AI agents, CI dashboards,
  or `jq` without parsing SVG or HTML. Key fields:
  `schema_version`, `generated_at`, `myflames_version`, `source.{type,engine,
  engine_version}`, `plan_summary.{total_time_ms,rows_sent,rows_examined_estimate,
  operator_count,max_depth}`, `optimizer_switches[]`, `warnings[]` (severity +
  category + source + node_labels), `suggestions[]` (severity + category +
  action + why + target_variable), `executive_summary`, `primary_action`,
  `collected.{variables,stats,schema}`. Fail-fast validation enforces enum
  discipline and required keys at write time.
- **Progressive-UX HTML report template**: the `--output foo.html` pipeline
  emits a redesigned self-contained report with:
  - **Executive summary strip** + **"Fix first" primary action card** above
    the fold (newcomer-friendly)
  - **Glossary chips** (`<abbr>` tooltips with zero JS) on every jargon term:
    filesort, hash join, BNL, materialize, ICP, MRR, sargable, etc.
  - **Copy-paste affordance**: every SET / CREATE INDEX / ALTER TABLE
    suggestion lives in a selectable `<pre><code>` block; never locked
    inside the SVG
  - **Collapsible `<details>` sections** for warnings, Why clauses, collected
    environment, and raw sidecar JSON (dense but navigable for DBAs)
  - **JSON-LD `<script>`** in `<head>` with the full sidecar payload; AI
    agents can answer "what's wrong with this query?" from the HTML alone
    without parsing SVG text nodes
  - **Semantic HTML landmarks** (`role="banner"`, `role="main"`, `<nav>`,
    `<aside>`) + ARIA labels so screen readers can navigate sections
  - **Responsive layout**: scales from 720px mobile to 1400px desktop
- **Responsive SVG output**: the CLI now post-processes every SVG it writes
  to inject `style="max-width: 100%; height: auto;"` and backfill a `viewBox`
  when one is missing. Standalone `.svg` files opened directly in a browser
  now scale to the viewport instead of overflowing at the fixed 1800px
  flamegraph width.
- **Glossary module (`myflames/glossary.py`)**: 30+ canonical EXPLAIN /
  optimizer terms with three tiers of explanation each:
  - `short`: ≤90-char tooltip text
  - `technical`: senior-DBA grade with cost-model citations
  - `newcomer`: plain English, no jargon, no analogies that distort
  Also exposes `find_terms_in_text()` for HTML glossary-chip wrapping and
  `generate_executive_summary()` for deterministic one-line plan summaries.
- **Complex-query demos**: 3 new example topics (15 files each):
  - **CTE + window functions** (`ROW_NUMBER() OVER (PARTITION BY ...)` top-N per group)
  - **Correlated subquery** (N+1 anti-pattern)
  - **Recursive CTE** (org-chart traversal)
- **105 new unit + integration tests** (parser, sidecar, glossary, advisor,
  collectors, connector, non-sargable, responsive SVG). Integration tests
  spin up live MySQL 8.4 + MariaDB 11.4 containers, create both
  `mysql_native_password` and `caching_sha2_password` users, and drive the
  whole pipeline end-to-end; they skip automatically when Docker is absent.
- **Three new specialized skills** (`.claude/skills/`):
  `mysql-expert` (domain correctness), `progressive-ux` (approachability),
  `structured-output` (machine consumption). Used to divide work cleanly
  when adding new rules, new output fields, or new glossary entries.

### Changed

- **`-h` is now `--host`, not `--help`**: to match the `mysql` CLI. Use
  `--help` if you want help. (`--version` still works.)
- **HTML report template rebuilt**: the old `output_html_report.py` layout
  has been replaced with the progressive-ux template described above. Every
  existing test assertion (`<!DOCTYPE html>`, `<svg`, `Export SVG`,
  `Export Analysis JSON`, `Print`, `Warnings`, `Suggestions`) still passes.
- **Advisor suggestions always include `Why:` explanations**: the contract
  is now enforced by a dedicated test
  (`test_every_suggestion_explains_why`) so future rules cannot ship
  without a cost-model justification. Rewrote every existing rule to
  explain the trade-off (in-memory sort vs tmpdir merge, hash-join probe
  re-reads on spill, MEMORY → InnoDB temp-table conversion cost, etc.).
- **`myflames/parser.py`**: `analyze_plan()` now also returns
  `nonsargable_joins`, `optimizer_switches`, and `node_highlights` entries
  for non-sargable predicates. `_detect_optimizer_switches()` promoted to
  a standalone function with a stable dict shape.
- **Docs**: `docs/index.html` reorganized with new sections for Complex
  queries, Optimizer switches, and Live mode; the 20 new demos are all
  linked. Every legacy demo was regenerated through the new template so
  the whole set is consistent.

### Removed

- Stale demo files with no matching fixture or broken naming
  (`mysql-query-analysis-derived-sort.*`, bare `mysql-query-analysis-*.svg`
  without a renderer suffix). The top-level orphan `demos/` directory was
  also removed; all demos live in `docs/demos/` as they should.

### Fixed

- **MariaDB BNL join condition lost during normalization**: the
  `block-nl-join.attached_condition` field was being dropped when the
  inner table was normalized, hiding non-sargable predicates on BNL
  plans. Now preserved on the normalized node so the advisor sees it.
- **Standalone SVG viewing overflow**: `width="1800"` SVGs no longer
  require horizontal scrolling when opened directly in a browser.

## [1.2.0] (2026-04-09)

### Added

- **MariaDB 10.11 and 11.4 support**: myflames now auto-detects and visualizes
  MariaDB `ANALYZE FORMAT=JSON` output alongside MySQL `EXPLAIN ANALYZE FORMAT=JSON`.
  All five view types (flame graph, bar chart, treemap, diagram, tree) work with both
  databases. Supported MariaDB input methods:
  - `ANALYZE FORMAT=JSON SELECT ...`
  - `SHOW ANALYZE FORMAT=JSON FOR <connection_id>` (live query analysis)
  - `SHOW EXPLAIN FORMAT=JSON FOR <connection_id>`
- **MariaDB normalization layer**: transparent conversion of MariaDB's
  `query_block/nested_loop/table` JSON structure into the existing MySQL tree format.
  Handles nested loops, filesort, unions, window functions, materialized subqueries,
  derived tables, covering indexes, and correlated subqueries.
- **MariaDB fixture generation**: `scripts/generate-mariadb-fixtures.sh` generates 60
  test fixtures (30 per version) from Docker containers running MariaDB 10.11 and 11.4.
- **28 new MariaDB-specific unit tests** covering format detection, normalization,
  timing accuracy, access type mapping, and analysis plan detection.

## [1.1.0] (2026-04-01)

### Added

- **HTML report output**: `myflames --output report.html explain.json` generates a
  self-contained HTML file with embedded interactive SVG, analysis sidebar, stat cards,
  and export buttons (SVG, JSON, Print/PDF). Works with all five view types.
- **Before vs after comparison**: `myflames compare before.json after.json` produces
  an HTML diff report with total time delta, per-operator changes (self-time, rows,
  loops), new/removed full scans, resolved/new warnings, and a color-coded summary.
- **Guided onboarding**: `myflames guide` prints which view to pick based on what you
  want to learn (time distribution, slowest operator, join order, etc.).
- **`--version` flag**: `myflames --version` prints the installed version.
- **`--output` / `-o` flag**: write output to a file instead of stdout. Automatically
  produces an HTML report when the path ends in `.html`.
- **`sample.json`**: included in the repo root so new users can try the tool in 30
  seconds: `myflames sample.json > query.svg`.
- **Diagram zoom buttons**: +/− and reset (↺) buttons in the bottom-right corner of
  the diagram view replace the previous Ctrl+scroll-wheel zoom. Drag-to-pan and
  double-click-to-reset are unchanged.
- **Installable via pip**: `pip install myflames` installs the `myflames` console
  command. No external dependencies.

### Fixed

- **MySQL CLI output parsing**: the parser now handles all common `mysql` command-line
  output formats automatically:
  - Escaped newlines/tabs (`mysql -N -e` without `-r`)
  - `EXPLAIN` column header (`mysql -e` without `-N`)
  - Table-formatted output (`+---+` borders and `| ... |` rows)
  - UTF-8 BOM (files saved from Windows editors)
  - Junk text before JSON (e.g. MySQL warnings on stderr leaking to stdout)

  Users no longer need `-s -r` flags; any `mysql -e` invocation works. When input
  cannot be parsed, the error message now suggests the correct flags.

  Reported by [Anil Joshi](https://github.com/aniljoshi); thanks for catching this!

## [1.0.0] (2025-12-15)

### Added

- **Five visualization types**: flame graph (default), bar chart, treemap, Visual
  Explain–style diagram, and collapsible execution tree.
- **Query Analysis panel**: every view includes warnings (full table scans, hash joins,
  BNL join buffers, temp tables, filesorts), optimizer feature detection, tuning
  suggestions, and heuristic index suggestions with DDL.
- **Interactive features**: click-to-zoom, hover details, search (Ctrl+F), pin/unpin,
  drag-to-pan (diagram), collapse/expand (tree).
- **MySQL 9.7 support**: `query_plan` envelope and hypergraph optimizer plans.
- **SQL embedding**: `--query` and `--query-file` flags, plus auto-extraction from
  the EXPLAIN JSON.
- **Time unit auto-detection**: switches between ms and µs based on total query time.
- **Zero dependencies**: pure Python 3.7+ stdlib.
