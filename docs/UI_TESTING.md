# Performance workspace UI testing

For the optional Electron host, also run the tests and isolated launch smoke in
`apps/desktop`. Check authentication, renderer isolation, navigation restrictions
and owned-process shutdown on macOS. Review the actual workspace in light and
dark appearances at 1440 pixels and the macOS app's 720x520 minimum window (narrower phone layouts still exist in the CSS but are not a delivery target). Verify Back restores filters,
selection, focus and scroll; command search only navigates or opens existing
actions. Windows packaging and validation are deferred.

Run `sql-completion.mjs`, `completion-browser.mjs`, `desktop-shell.mjs` and the
table-data suites alongside the existing browser checks. Completion must retain
undo and input methods, refuse stale metadata after a connection switch, and
never execute SQL. Table changes need live insert/update/delete and conflict
proof on isolated MySQL and MariaDB, including rollback, read-only policy,
unsupported tables, one-use review tokens and exact large numeric values.

Use a separate `WorkspaceServer(store_path=...)` database for audits. Run audit
servers with a temporary `HOME` and `MYFLAMES_SECRET_BACKEND=vault` so
remembered passwords never reach your real keychain. Use a
disposable database account and sample dataset for live captures. Keep credentials,
screenshots containing query data, downloaded bundles, and audit databases under
the ignored `local/` directory.

## Interaction matrix

| Screen | Checks |
| --- | --- |
| Navigation | Nine places in order (Home, Server, Workload, Replication, Tuning, Query editor, Plans, Investigations, Learn), each with its own icon; exactly one active place with `aria-current=page`, and only while its screen is shown (the empty welcome is Home, not Plans); top bar names the place once as plain text (no place or view menu) with one sidebar toggle; Up/Down/Home/End move between places and Enter opens one; Open plans list with count, title and time, Baseline only with two or more plans, highlighted only while Plans explores it, remove button on hover or focus; one status indicator (All changes saved, Saving…, Not saved); no Local badge and no Privileges button in the top bar of any place, no "Local workspace" line; the connection picker lists Account and privileges… then Manage connections…, and Account and privileges (picker item or palette action) opens a sheet titled Account and privileges with the connection's name, Refresh privileges and Close (focused first; Escape returns focus to the picker); About myflames shows one privacy line (database servers you choose, official release pages when you check for updates, news feeds when shown and turned on); one sidebar width in every place; the command palette lists the places in sidebar order, Compare under Plans (never "Compare plans" as a place), Account and privileges under Actions, and the Audit log with its own icon; on Server and Workload, Refresh all is a text button after the connection picker, never above a view's Refresh, and the top bar keeps its hairline; window status bar at bottom of Plans, Query editor, Server, Workload, Replication and Tuning (engine version, connection, session, last run, refresh status; the Query editor's transaction state stays in its toolbar and rows and time in its result tabs); Settings popover, a compact menu with no title bar or close button (focus moves in, Escape returns focus to Settings, click outside closes, Appearance, Restore workspace on startup and its hint (passwords are never part of the workspace), Audit log, About myflames); at 390 px a compact bar with status, Settings, and Menu, a drawer that takes focus and closes with Escape or after choosing a place, Settings inside the viewport, and no horizontal scroll on any place |
| Home | Launcher layout: exactly one visible Import plan (the top bar, secondary, with or without recent work) and one primary action (Add connection); the empty Recent work offers Explore sample plan; passwords read Saved or Not saved and an empty default schema a dash; the version footer at the bottom; New query tab in the top bar (icons only below 650 px, names kept for screen readers), one title (the top bar's); Connections on the left; recent work on the right (open plans, recent investigations, All investigations), with Get started (Explore sample plan, Open Query editor) while no plan is open; a file dropped anywhere on Home is imported; keyboard focus; one column below 1,000 px |
| Connections | Rows with name, host:port, user, default schema, TLS mode, and saved-password status ("Password saved in macOS Keychain" / "No saved password"); the unsaved connection in use as a Not saved row; Use, Test (row result), Edit, Forget password, Delete with Keep / Delete permanently; Add connection with focus on the first field; Save connection asks Also save the password? (Save with password / Settings only), duplicate name asks Replace / Save as new, Escape cancels the prompt, saving settings keeps an existing saved password, a successful Test connection with a typed password offers to save it, Use without saving; the first saved connection becomes the one in use; disabled store never asks |
| Write safety | Connection form: Type (Production pressed by default, Development) with an ⓘ that says what each confirms, a Read-only switch; rows show the type and Read-only; Save sends `type` and `read_only`; a new connection set to Development says it applies once saved; Use without saving stays Production in the row, the footer safety state, the picker dot and requests (`type: "production"`), and may only choose Read-only (`read_only: true` in requests, restored with the workspace); editing a saved Development connection's host, port, user or TLS settings shows the warning, shows and saves Production until Development is chosen again, and its Use without saving is Production; the window footer's first item (`[data-status="safety"]`) on the Query editor, Server, Workload, Replication and Tuning only: a red dot with "Production · changes ask first" (accessible name "Production: every change on NAME asks first"), an outlined dot with "Read-only · changes refused", a green dot with "Development · risky changes ask first", nothing under the top bar, and no safety state on Home, Plans, Investigations, Learn and the Audit log; the picker's type dot (red, green, outline for read-only) with its name for screen readers; Cancel and Escape on a Kill sheet (Locks and Sessions) send one `/api/audit/declined` each with the tagged `KILL CONNECTION n` or `KILL QUERY n`, the confirmed identity and the same connection, and a confirmed Kill sends none; a Development choice is bound to the host, port, user and TLS settings it was made with (another port shows Production, the same one Development again); a failed `profiles/list` (on window focus) shows "Production assumed" with Retry and turns Kill off; a type changed elsewhere shows after Retry or focus, greys reports read under the old policy, and a Kill sheet confirmed before the change sends nothing ("type or read-only setting changed") while an unchanged one goes through (`npm run test:profiles`, `test:navigation`, `test:locks`, `test:sessions`) |
| Audit log | Settings > Audit log opens a screen of its own: no place active, the top bar says Audit log, focus on its title; empty: "No changes recorded yet" saying every change myflames sends, refuses or you decline is listed here, with Export disabled; records written by the real audit code (`test/audit-seed.py`: Done, Failed, Outcome unknown, Not finished, Refused, Declined, one 40 days old) newest first with time, connection, type, operation, statements as stored (a password arrives as `[redacted]` and never reaches the page), status, error, duration; ⓘ lists every status with its meaning; connection, status and Since filters are sent to `/api/audit/list` (`server`, `status`, `since_days`) and applied before the page of 500; with 520 newer records an older Outcome unknown is still found by its status and opens in the inspector, Load 500 older sends `before` and brings the rest; Status is the second column and "Outcome unknown" stays whole and in view with the inspector open at 1440 px; Kept N days is in the toolbar, also when the log is empty; the inspector has the meaning, the statements, connection, result, confirmed target, what was checked and the policy, identifier labels on their own line; Columns menu and resize handles; Export CSV and JSON through `/api/audit/export` with the same filters (`status` included); Kept N days changes retention through `/api/audit/settings` and drops older records; 390 px without page scroll, the inspector as a sheet (`npm run test:audit`) |
| Import | Empty form, malformed JSON, wrong plan shape, file upload, same-file retry, size rejection, Escape, narrow/short windows, successful recovery |
| Plans | Explore / Compare switch and Import plan once, in the top bar (`.topbar .plans-bar`; the top bar title is "Plans" only) (Compare is flush too: one header row with Before, After and the exports, the stats strip, two chart panes divided by a hairline, operator changes; canvases stack vertically at narrow widths; below 820 px of Compare width the views are one select) (compact empty state: one line, Explore sample plan, drop a file), one plan header with the name, the stats, Plan summary and SQL disclosures (collapsed; each opens a panel over the chart, SQL with Copy SQL) and Export, no strip below it, the chart starts above the fold at 1440 x 900 and fills the window with the inspector beside it, all four views (each fills the window with the inspector beside it; one "Search operators" wording; the bar chart prints times like the tree and colors by share of time; sentence-case chart headers; Visual Explain zoom −, percentage, +, Fit and rows labels on a chip), a plan with findings opens the inspector on Findings, Export is the shared pull-down (`ExportMenu`: icon, Export, chevron-down; items JSON and HTML report), no "No live session" in the footer, operator search/no results, linked selection, all metrics, focus/collapse/expand, breadcrumbs, bottleneck shortcuts, raw fields; below 820 px of plan width the view, metric and branch are one select and an Options menu and the legend a popover |
| Visual Explain | Zoom percentage, plus/minus, reset, fit (fits width first between 75% and 100%, top inputs first, larger trees pannable), limits, press feedback, keyboard operation, wheel and pan; in the dark appearance the join Venn circles take dark fills and strokes with readable L and R labels (at least 4.5:1) |
| Query editor | Connection picker (name, host and schema, password state, switch, inline password when none is saved, Use a different password, Manage connections returns to Home), connection failure/recovery, saved connections with and without a remembered password (connect/capture/Server views without typing, typed password overrides, changed host asks again), query tabs (every tab has its own close button shown on the active tab, on hover, and always on touch screens; middle-click closes a tab; the tab on the left takes over; the last tab stays), parameter errors, estimated/measured capture, repeated runs, trace, SQL errors, timeout, cancellation, run scope (selection sends only the selected text and the buttons read Explain selection / Analyze selection; several statements run the one under the cursor with a brief highlight and a Statement N of M chip; quotes, backticks, and comments do not split; a selection with several statements or only comments is refused; Ctrl/Cmd+E explains and Ctrl/Cmd+Shift+E runs analysis; Enter alone still types a line break; a missing parameter in the selection; SQL sent (Plan results tab) and the plan's SQL card show what ran) |
| Query editor: running SQL | Run (Ctrl/Cmd+Enter) sends the selection or the statement at the cursor as `selection` offsets of the whole text, Run all (Ctrl/Cmd+Shift+Enter) sends the whole text; statement privilege preview (/api/editor/preview) disables Run when all statements are denied with visible reason, keeps unverified/mixed statements runnable, with one line at the end of the toolbar only for denied statements (none for unverified ones, never a late row under the toolbar); mode switch reads Editor | Query digest; the Schemas and History pane titles are sentence case; "Sorting applies to loaded rows" only on a cut result; the statement Run would send is tinted and its line numbers marked; a statement after a DELIMITER line goes alone with its DELIMITER line; one results tab per result set ("Result 1 · 2 rows · 3 ms") then Messages (every statement: Done, Error, Stopped, Not run, Outcome unknown; rows, affected rows, the info line, warnings, duration) and Plan after a capture; NULL as a token, an empty string as an empty cell, the text 'NULL' plain, binary hex with its size; an error opens Messages, underlines the position and marks its line number, Go to line puts the caret there; Stop and Escape send `editor/stop` (KILL QUERY) and the run reports what was undone; the row limit (Rows) is sent as `limit`, a cut result reads "stopped after N rows" and Load more runs that one read again with a larger limit and a "rows may differ" note; Auto-commit / Manual commit / Transaction open / Transaction unknown with Commit and Roll back while open or unknown, a dot on the tab, and closing the tab asks Commit and close / Roll back and close / Cancel (`editor/close` with `discard` only after the choice); a write that needs a confirmation shows the exact statements, the reason, the implicit-commit warnings and the connection, Cancel focused, a confirmed token is sent once, an expired one asks again, Cancel or Escape sends `decline`; Safe updates off on Production asks first; export sends the loaded values (CSV, JSON, SQL INSERT with the table name and the columns marked binary, Markdown, Copy as TSV) and nothing from the connection; right-click on a cell, row More actions button, or Shift+F10 / ContextMenu opens context menu to copy value, row, or column; history panel (search, click inserts, Clear asks); schema browser reads nothing until Load schemas, lists tables, columns with type and key, and indexes (column and index rows stay on one line and the actions button shows only on the row being pointed at, the focused row, or on touch screens), double-click inserts a quoted-when-needed name, the table menu runs SELECT * … LIMIT 100 and Show CREATE TABLE and opens Indexes or Tables, hiding it keeps what it read; a session that ended says why and that settings and any open transaction were lost, runs wait for Reconnect; a session on another connection waits for Close session; restore keeps the text, caret or selection, row limit, results tab and editor height, never results; at 390 px toolbars wrap, side panes open as sheets, and second editor status bar is hidden in favor of window status bar (`npm run test:editor`, mocked editor API) |
| Server | Place toolbar with the title, its ⓘ (the intro), and the connection picker shared with the Query editor; grouped sub-navigation on Server (Overview: Health, InnoDB, Error log; Contention: Locks; Schema: Indexes, Tables, Database diagram; Setup: Parameters, Instruments, Maintenance; Security: Users and roles) and Workload (Queries: Top statements, Processlist; Resources: Waits, I/O), one glyph per view, one current item, one tab stop, Up/Down/Home/End move and select; a view reads when opened on screen, once per connection (Refresh retries), while Tables and Indexes keep their schema-size consent, Error log waits for Load, and nothing reads off screen; badges from loaded data only, one marker system (a status dot for state: problem red, watch amber, unknown or off a gray ring, OK nothing visible; a neutral count pill beside it for counts, never a colored pill; the hidden text names both, such as "5 blocked") for Health, InnoDB, Tables, Parameters, Instruments and counts for Locks blocked, Sessions active, Indexes to review, flagged Top statements, accounts; the sidebar Replication place uses the same marker with the meaning for screen readers; privilege-aware buttons (Kill, Change parameter, Skip transaction, Dump, Restore, Clone, account actions): denied disabled with visible reason, unknown enabled with Unverified; dialogs, sheets and drawers use scoped focus trapping (Tab/Shift+Tab cycling, Escape closes and restores focus); every view: toolbar (title, ⓘ, controls, Refresh, inspector toggle), one summary line with "N notes", a table or one-line rows, no explanatory paragraph on screen; inspector toggle and ⌥⌘I, a click or Enter on a row opens it, arrow keys move the selection and the inspector follows, Escape closes it and returns focus to the row (not when a popover or the Kill sheet handles Escape), resize by drag or arrow keys on its edge within 280 to 560 px, width remembered in localStorage; at 390 px toolbars wrap, list of views is its own screen, inspector is a bottom sheet; no horizontal scroll at any width; session restore keeps view and settings |
| Table columns | Every DataTable (Top statements, Sessions, Waits, I/O, Locks, Indexes, Tables, Parameters, Instruments): a resize handle on every header (`role=separator`, `aria-valuenow/min/max`), pointer drag and keyboard (Left/Right 16 px, Shift 64 px, Home minimum, End maximum, double-click fits), any column including the left one; numbers and flags never truncate, and only text columns wider than 140 px give up room (to 140 px) before the table scrolls sideways (a number column's widest value and a cell's flags are part of its minimum); one column (the flexible one, else the last text column) takes the room left; Columns menu at the header's right end lists every column with a checkbox, hidden ones included, refuses to hide the last visible one, and has Reset columns; widths and hidden columns saved per table in the workspace session (`slow.columns_v1`), restored after a reload, a corrupt stored layout (unknown tables or columns, huge or negative widths, every column hidden) ignored; sorting by a column that is then hidden keeps the order; large counts compact (159.0B, 1.78B, 14.2M) with the exact value as tooltip and in the inspector; a name with flags stays on one line (name truncates, full text on hover); when the frame narrows (inspector open) the name column (the first text column, or `primary`) keeps 220 px of text beside its flags and gives up room after the other text columns, while number columns scroll inside the frame (Indexes Full scans); above 300 rows only the rows in view are drawn (`aria-rowcount`, `aria-rowindex`) and Up/Down, Page Up/Page Down, Home/End and Enter still reach and open every row; 390 px without page scroll (`npm run test:datatable`) |
| Connection switching | Every Server report, selection, and read is bound to the connection it came from (saved connection id or host, port, user, and TLS settings; never the password): switching the picker leaves a loaded report on screen, greyed, with "From <connection>, refresh to load this server" and Refresh; Kill, Show plan, Explain, Run analysis, Use this query, and Explain top N are off on it and its badge is gone; a read still running for the previous connection does not block Refresh on the new one, and its late answer (or error) is dropped; switching back makes the report current again; Kill goes to the connection the sheet was confirmed for (the sheet names the server) and sends nothing when the connection in use changed, and a Kill answered after a switch neither re-reads nor shows an outcome from the other server; links from a report (Index health, Top statements, Table health, Indexes) are off on another connection's report and a stale click reads nothing; the database list is read again for another CA file; a newer Sessions scope or filter replaces a read still running (latest wins); Explain top N keeps the connection and settings it started with when the picker changes mid-batch, refuses a report from another connection, never imports a plan that arrives after Cancel, and cancels a job whose start answers after Cancel; a Run analysis confirmation is bound to the statement (schema and digest, not rank), its SQL, the connection, the schema it runs in, and the runs and time limit, and closes with "nothing ran" when any of them changes; Locks automatic refresh stops while Server or the view is hidden; the inspector sheet keeps focus on an action across automatic refreshes |
| Editor and connection races | A tab opened from Top statements (Run analysis on a statement that needs parameter values) stays bound to its server: after switching connections, filling the values and Ctrl/Cmd+Shift+E (Run analysis), Ctrl+Shift+Enter (Run all) or Run analysis send nothing and say where it came from, a reload keeps the binding, and Use this connection moves it explicitly; Explain top N cancelled while a finished plan is analyzed adds nothing; Cancel capture while the start is still on its way cancels the job it started and never polls or imports it; a result that completes after Cancel is dropped; Save connection is single flight (one request for a burst of clicks, Saving… meanwhile), and a late answer keeps a connection chosen meanwhile with Use without saving; a saved row's Test answer is not shown once the row's host changed; a Test connection answer applies only to the settings it tested (dropped after an edit or another form), and an edit clears the Connected line; query tabs are tabs (one tab stop, Left/Right/Home/End move and open, `aria-controls` to the editor panel, the New query tab button outside the tablist); a failed or pending comparison never shows or exports the previous pair; with autosave off, edited query tabs alone get the leave-page prompt, and so do unconfirmed changes larger than a keepalive request; restore sends each plan's text once and plans that still cannot be reopened stay in the saved workspace (Download them / Remove them) |
| Top statement actions | Selecting a row opens the inspector with the full statement (Copy), latency, rows, access and sort counters, and flags; Use this query, Explain, and Run analysis there switch to the Query editor; Explain from a row (new tab named Top #N · table, digest schema as default database only when the connection has none, the Query editor's time limit / runs / trace sent, plan opens with From Top statements and the short digest); Run analysis note (server, runs, read-only transaction, time limit; focus on Execute and analyze; Escape returns focus; nothing starts before confirming); normalized text (`?` to `:p1`, `IN (...)` noted as one parameter, skeleton prefilled, Parameters open and focused, notice visible, Explain refused while a digest parameter is null, notice gone once filled); non-SELECT row with both actions disabled in its inspector and the reason linked by `aria-describedby`; Explain top N next to the summary line (default 5, max 20, EXPLAIN only, trace off, waits on the two-capture limit, progress line, failure row, skipped ranks for parameters and non-SELECT, plans added without leaving Server, editor runs disabled meanwhile, Open plan, Cancel stops the server job and starts nothing more); session restore keeps the plan's `source` and the tab's source and parameters; 390 px without horizontal scroll. Live check: each engine's real top list (MySQL sample path, MariaDB parameter path) |
| Refresh all | Text button in the top bar after the connection picker (⌥⌘R) reads all sections including unopened sections with each view's parameters (window, order, databases, scope); Full analysis runs in a separate phase; at most three reads at once, Health, Waits and I/O first; the button becomes Stop while it runs and ⌥⌘R starts nothing more; a spinner on each view waiting or reading in the list of views; a view's own Refresh reading the same thing is joined (one request); a failed view keeps its previous report, gets a mark in the list of views (with the reason for screen readers) and one line above it, and the status line reads "4 of 5 views refreshed; Locks failed" and opens each view's outcome and time; Escape stops it (queued views are never read); a connection switch stops it and drops late answers |
| Full analysis | The Scorecard grade has an ⓘ (accessible name "How the grade works"; Enter opens it, Escape returns focus) with the scale A 90 and above, B 80 to 89, C 70 to 79, D 50 to 69, F below 50 (the current grade marked "this server" and `aria-current`), the cap at C and the provisional rule; a reason line beside the label ("Capped at C: 1 check has a problem (N is in the A range)." with a problem, "Score N is in the X range, …" without, none when provisional); the Tuning ⓘ lists the grades too. The Tuning header shows the title Tuning beside its ⓘ like every other view (no lone icon), with the Scorecard | Full analysis segmented control (tab semantics: `#tuning-tab-*` tabs controlling `#tuning-panel-*`, one tab stop, arrows, Home and End), Export and Run full analysis; at the 720 px window the controls take one row under the title; after a connection switch the Scorecard note offers Run full analysis and its Export is off; the Scorecard categories keep equal widths at 390 px (an odd last row ends in an empty ruled cell). The Tuning place's second tab (`#tuning-tab-analysis`, harness `h.fullAnalysis()`); not a Server view and no list badge (its status shows on the Scorecard); nothing read until Run full analysis (about 10 to 20 s, a progress line counting seconds); summary line, facts (server, host, uptime, taken and duration, databases, views read); Look at first; the problem and watch findings as one-line rows in the report's order; Not judged with each reason (unknown checks and failed views); the sections in order (including the Parameters section); findings with no rule collapsed; no paragraph on screen, the report notes, attribution coverage, caveats and the checks not done behind "N notes"; the inspector (click, Enter, arrows) with verdict, numbers, settings, rule, why, what to do with the SET and my.cnf line to copy (never applied) and the caveat, links to the view and row (links that name a check open Parameters at that check's first parameter), the lesson, and the statements behind the number with Explain and Run analysis (confirmation first); a section lists its findings; Export (HTML, Markdown, JSON, Text) posts the report as read to `/api/report/export` and downloads the file; a Top statements link reads with its order; after a connection switch the report is greyed with the other-connection note (the Scorecard names that server too), Explain and the Top statements link off, Export still on; restore keeps that it ran (never the report or the place as a saved Server view); Run full analysis and Refresh are off while Refresh all runs; a link that reveals a row in another place keeps its inspector open; 390 px with the inspector as a sheet |
| Statements behind a number | Health's Joins without an index, Full scans of the first table and Rows read by full scans, and an Indexes full-scan table: the inspector reads nothing until Show statements; then `/api/live/top` with the order (`full_join`, `scan`, `no_index`), `limit` 5, and for a table `{schema, table}`; the list (one line of SQL, calls, total, the counter, schema) with Explain and Run analysis (confirmation, Cancel sends nothing) and no Use this query; "text match, not proof" for a table; coverage and caveats in the notes; Performance Schema off is a gate with the Instruments link; the list read before shows again on return; Open in Top statements opens Rank by on that order with a removable Table chip that clears the schema filter, and removing the chip reads every statement again |
| Health | First Server view, open by default; reads once when opened on screen (Refresh samples again); Window choices (1 to 60 s, 5 by default) sent as `interval`; loading line names the window; summary line (worst status, one problem/watch/ok count across tiles and diagnostics, window and time; the Diagnostics heading has its own counts) and a fact strip (server, host, uptime, buffer pool, max_connections); diagnostics as one-line rows sorted by severity with the headline number; tiles grouped as before, at most 120 px tall, value and unit, every tile in a grid the same width (one row up to six tiles, rows of four for seven or eight, balanced rows of three or two when a row does not fit, never one tile stretched across a row; check at 1440 and 390 px), status dot plus text for Watch, Problem and Unknown, while OK is an empty status cell and a tile without a threshold draws no marker (a diagnostic row without one keeps a faint dash; both still named for screen readers); selecting a row or tile (click or Enter) opens the inspector with the verdict, What to do and caveat (watch or problem only), trend, rule, why, numbers (estimates tagged), settings with defaults, and sources; links in the inspector to Locks (focus moves to the item, the report stays), Sessions, Top statements, and the lesson in Learn; a second sample draws sparklines on the key tiles and a larger trend in the inspector, with a text summary and a per-sample hover; history capped at 20; Auto-refresh Off by default, runs on the chosen period, stops when set to Off or after an error (error in an alert, last report kept); MariaDB report with engine-specific explanations; 390 px without horizontal page scroll; session restore keeps the view, the window, and the open inspector, never the report or the samples. Fixtures are `health-1.0` reports computed from live two-sample captures of MySQL 8.4 and MariaDB 13.0 |
| Locks | Under Contention; reads once when opened on screen (Refresh reads again); summary line (DDL waiting, blocked sessions, root blockers, idle in transaction) and facts; badge counts blocked sessions, a green dot when nothing waits; blocking tree as a treegrid (root blockers first, chain nesting by `aria-level`, Left/Right fold and unfold a chain, insert intention, metadata wait queued behind ALTER), root rows marked, Idle in transaction pill, wait ages (`~` when only the statement time is known); inspector with waits, transaction, last statement, Show plan for a running connection or the idle-connection message, KILL text copied but never sent; segmented Transactions / Row locks / Metadata locks / Latest deadlock (waiting first, mode label plus tooltip, table filter, metadata lock waits, deadlock rows with the raw text only behind Show raw output); Waits only request and its deadlock message; latch caution on MySQL 8.0.39 with Auto-refresh Off by default (`aria-describedby`), 10 s Auto-refresh that stops on an error; MariaDB with Performance Schema off (metadata locks gate with one action, row-lock note); MySQL without PROCESS; nothing waiting; notes behind "N notes"; 390 px without horizontal page scroll (tables scroll inside, the view is pushed); Locks view restored after reload without the report. Fixtures are real `locks-1.0` reports captured from MySQL 8.0.39, MySQL 8.4 (with and without PROCESS), and MariaDB 13.0 with Performance Schema off |
| Locks: waiting DDL | A `locks-1.0` report shows no Waiting DDL group and no Kill action; Waiting DDL comes before the blocking tree as an outline: the DDL row (connection, what it waits for), its blockers (Usual culprit, idle in transaction, the reason) and a row for the sessions queued behind it; the DDL explanation is behind the group's ⓘ; the DDL inspector has the waiting statement, its table, requested lock, Blocked by with each blocker, the queued sessions with the plain explanation and the copyable `KILL QUERY` for the DDL; a blocker's inspector has the lock held until the transaction ends, rows changed, last statement, and Kill; MariaDB without the instrument shows Likely blockers, open transactions that started before the DDL, the queue marked likely, and the note with `INSTALL SONAME 'metadata_lock_info'`. Fixtures are real `locks-1.1` reports captured during a blocked ALTER TABLE on MySQL 26.7 (exact) and MariaDB 11.4 with Performance Schema off (likely) |
| Locks: Kill | Kill in the inspector for an eligible target (DDL blocker and tree root); definitive denial keeps a focusable disabled control with its reason and Copy `KILL n`; the confirmation sheet opens with focus on Cancel and shows connection, user@host, state, transaction age, rows changed, last or running statement, and what it blocks (queued sessions counted with their DDL, not listed again); an idle session offers only Kill connection with why KILL QUERY would not help; a running one defaults to Kill query only with the autocommit caveat and switches the confirm label between "Kill query on connection n" and "Kill connection n"; 250,000 rows changed shows the long-rollback warning; Escape and Cancel send nothing, leave the inspector open, and return focus to the button; prepare obtains a one-use review token; confirm sends one `/api/live/kill` with `review_token`, `connection_id`, `mode`, and `expect` (user, host, `trx_started`, `ps_thread_id`), refreshes the report, and announces the outcome ("no longer waits" or "still waits, now for connection n") with focus on it; refused and denied results are alerts; a MySQL blocker that ran XA PREPARE shows the prepared XA guidance with Copy `XA ROLLBACK` and `XA COMMIT` and no Kill button or `KILL n` text, for the DDL blocker and the tree root; a lock held by no client connection explains the background thread or detached XA transaction on MySQL and only the background thread on MariaDB; an ALTER TABLE that waits on a parent table names both tables; the sheet fits 390 px without horizontal scroll |
| Sessions | Processlist under Workload > Queries (replaces Running queries); reads once when opened on screen (Refresh reads again); Active by default (`active_only: true`), All reads again with `active_only: false`; Running at least sent as `min_seconds`; counter strip (connected, limit, max seen, running, history list) and one summary line; Breakdown (state, user, host, database, command) collapsed; rows in the report's order (problems first) with a status dot and flag pills (Long running, Idle in transaction, Lock wait, Server thread); badge counts active sessions (red while one is a problem, nothing for zero); inspector with why, statement or last statement, transaction, the rule of each flag, and for a lock wait what it waits for (InnoDB, metadata, or table-level) with Open Locks; Show plan for explainable statements, its absence explained otherwise; Kill disabled for server threads and myflames' own statements, the shared confirmation sheet (Cancel first, Escape sends nothing), one `/api/live/kill` with `expect` (user, host with port, `trx_started`), then a refresh and the outcome; MariaDB; without PROCESS a one-sentence gate; error keeps the list; filter on screen; Auto-refresh Off by default; 390 px with the sheet; restore keeps the view, Active / All, and the minimum time. Kill sessions… (toolbar, `data-action=kill-sessions`, `aria-haspopup=dialog`) opens the dialog titled Kill sessions (Kill mode segmented control, no "Mass kill" text) and Escape closes it with nothing sent. Fixtures are `sessions-1.0` reports computed by `sessions.py` from live recordings (`server-fixtures.py`) |
| InnoDB | Under Overview; reads once when opened on screen (`raw: false`; Refresh reads again); summary line names the sections that need attention and the averaging window; facts (server, uptime, print time); every section in order as a group with its status (watch or problem only), up to four rows (graded first) and "N more", and its one-line summary; a section the server does not print shows its summary as its one row; badge shows the worst status (unknown without PROCESS); the inspector has the summary, every value with its status, the rules, the transaction holding purge back, the transactions or the deadlock's statements, Every parsed number (collapsed), and links (Sessions, Locks, Health, Open Parameters, I/O); arrow keys walk the rows across sections; Show raw output requests `raw: true` and shows the text in the inspector; without PROCESS a gate with the GRANT; error keeps the last report; 390 px. Fixtures are `innodb-1.0` reports computed by `innodb_status.py` from live recordings (`server-fixtures.py`) |
| Waits | Under Workload > Resources; reads once when opened on screen (Refresh reads again); Window choices 2 to 60 s (10 by default) sent as `interval`; loading line names the window; summary line (events in the window, classes not instrumented) and facts; segmented Events / Classes and Over the window / Since server start (with Longest), sortable columns; an empty window offers the events since server start; no total or share anywhere (the reason behind ⓘ); inspector with the meaning and numbers, Open Locks / Open I/O (a class shows the range on screen first, with Over the window and Since server start in their own labelled sections); Not instrumented collapsed, its rows (counts) open the enable UPDATE and my.cnf line with Copy and the caveat, opened with the keyboard; Auto-refresh Off by default, stops when set to Off; error keeps the report; Performance Schema off gate (MariaDB) with the Instruments link; MariaDB notes behind "N notes"; 390 px; restore keeps the view and window, never the report |
| Indexes | Databases picker in the toolbar (search, typed name with Enter, system databases hidden until asked, Reload list, Escape returns focus) with All user databases by default; Refresh and Load disabled with a one-sentence gate when the list reports more than 2,000 tables in user databases, enabled once databases are chosen; the request sends `schemas` (none for all); summary line and facts (databases, usage source, tables, indexes); segmented Unused / Redundant / Full scans / No data with counts; badge counts what to review; Unused (Candidate, Keep: backs a foreign key without drop SQL, already invisible/ignored pill), the inspector with why, INVISIBLE (MySQL) or IGNORED (MariaDB) and DROP INDEX to copy; arrow keys move the selection; Redundant with the covering index; Full scans with Mostly full scans and the statements that name the table (Show statements, Open in Top statements with the table chip); No data; Performance Schema off gate on Unused (redundant still listed, userstat how-to); userstat source; Table health link (opens Tables for that table) and a focus chip when Tables links here; "N more not shown. Choose fewer databases to see them." under every list the server cut; returning to the view does not read again; no auto-refresh; 390 px; restore keeps the view and its own databases (Top statements keeps its own schema) |
| Tables | After Indexes under Schema; Databases picker with All user databases by default at any size; Fresh numbers (MySQL only, hidden for MariaDB) sent as `fresh`, its explanation behind ⓘ; heads-up gate above 2,000 tables when the read touches every table; loading line, facts (Statistics: fresh, cached and how old, or read now), summary line at most 120 characters; segmented Missing primary keys / Free space; Missing primary keys sorted worst first, sortable headers with `aria-sort`, Status, Engine and name filters, Why a primary key matters in the inspector, row inspector with the NULL check and fix statements to copy and the caveat, not InnoDB pill, generated invisible keys listed as fine; Free space shows informational reusable extents without byte/percentage warning thresholds, Show filter for information, shared and not judged; optional Optimize opens exact SQL review with its caveat; shared tablespaces remain a group without a per-table fix; cached statistics retain their provenance; tables scroll inside their region with the header in view; error keeps the report; Indexes link with focus chip; 390 px; restore keeps the view and its databases |
| I/O | Under Workload > Resources; reads once when opened on screen (Refresh reads again); optional schema and window sent; server-wide rates as tiles (at most 120 px) with what each measures in the inspector; segmented Tables / Files / File kinds and Over the window / Since server start; kind pills; table inspector links to Index health (loads that schema) and Top statements; no auto-refresh; Performance Schema off shows only server-wide rates and a gate with one action; error keeps the report; 390 px; restore keeps view, schema, window |
| Parameters | Named Parameters in the sub-navigation and the title; reads on open (one attempt per connection; Refresh retries); view toolbar stays pinned under the top bar; summary line and facts; one table of every global parameter the server reports (Name, Value, Allowed values, Apply type: Dynamic, Static, Unknown, Source, Data type, Description); Filter parameters search over name, value and description, and a Show filter (All, Modified, Dynamic, Static, Recommendations, and Pending while editing); badge shows the worst status from the checks; Edit turns the Value column into inline controls (switch, list or text box from the parameter's type) for MySQL dynamic and persistable static parameters and MariaDB dynamic parameters only (MariaDB static parameters are option-file only; unknown apply types are not editable in the table); Preview changes lists Name, Current value, New value, Apply type and When it applies; Review and apply opens one review for up to 25 changes (exact statement, problems, privileges, effect, undo; MySQL dynamic rows use Keep after restart for SET PERSIST vs SET GLOBAL, static rows use SET PERSIST_ONLY, MariaDB rows use SET GLOBAL and show the option file line to copy; Confirm applies one at a time, re-reading and checking each first, stopping at the first unconfirmed change, and showing a result per row; Cancel or Escape records a declined attempt for every planned row in the audit trail); the inspector (Enter, click, arrows) shows value, default, source with path and who set it when, persisted (MySQL), allowed values, scope, apply type, description, every covering check (status, explanation, what to do), and actions (Edit value, Reset to default staging DEFAULT for MySQL numeric and boolean dynamic parameters, Change… with every mode including This session only which opens the statement in the Query editor, Reset persisted value… on MySQL when persisted); Full analysis check links open Parameters at that check's first parameter and InnoDB's link reads Open Parameters; MariaDB aliases (log_slow_query, tmp_memory_table_size) and defaults; error keeps the report; 390 px |
| Instruments | After Parameters under Setup; reads on open (one attempt per connection; Refresh retries); summary line (Performance Schema on or off, views ready); Performance Schema row, preset picker and change banner stay, with Show statements opening runtime SQL, undo, restore and my.cnf in the inspector, Copy, and reviewed Apply with mandatory confirmation on every connection type that permits writes; tabs: Features (the "What myflames can show" checklist with Ready, Partly, Off, Unavailable, Not needed, the first thing missing, the inspector with everything missing, Show links and the view link), Instruments (search, group filter, state filter: All, Enabled, Disabled, Different from default, a checkbox per row and a header checkbox for every row shown, Select all shown up to 2,000, and Enable, Enable without timing, Disable, Clear selection), Consumers (checkboxes, Enable, Disable; enabling also enables required consumers and says so), and Actors and objects; statements are built only from names in the report (`UPDATE performance_schema.setup_instruments SET ENABLED = ..., TIMED = ... WHERE NAME IN (...)` for timed instruments and `ENABLED` only for untimed memory and error instruments, at most 500 names per statement; built-in `memory/performance_schema` instruments skipped by Disable; names with a backslash refused; `UPDATE performance_schema.setup_consumers` for consumers) and run through the reviewed SQL runner (exact SQL, confirmation, write policy, audit); runtime changes are lost at restart and Keep after restart shows copy-only option file lines (`performance-schema-instrument='name=ON|OFF|COUNTED'`, `performance-schema-consumer-name=ON|OFF`); MariaDB (no Errors row, MDL off); Performance Schema off; error keeps the report; links from Top statements (consumer off gate), Waits (Performance Schema off gate) and Locks (metadata locks gate) select and focus the matching row; 390 px with rows and SQL wrapping inside |
| Replication | Standalone Replication place; reads once when opened on screen (Refresh reads again); no privilege and not a replica are one-sentence gates with no table and no loop status read; sections Topology, Channels (Connections on MariaDB), Lag, GTID, Recovery in the header; summary line first (engine and version, channels or connections, failed, max lag, or lag unknown when any judged channel's lag is unknown), then the shared metric tiles (Replica state, its pill counting the channels behind it such as "1 of 2 channels", Channels, Max applier lag reading Unknown in that case, GTID mode); table (channel, status, source, I/O, SQL, lag, error), MariaDB says Connection; Topology source nodes show the engine and version (or Version unknown) with where it came from in the tooltip, the connector's details and the inspector's Source version row; badge counts failed channels from loaded data only; inspector: threads, the last error once (code, time, table, failing GTID, text), lag signals in words with their caveats, positions and GTID state in one list (GTID mode, Enforce GTID consistency), recovery and privileges, raw status behind the kit disclosure; no skip actions on a healthy channel; Skip this transaction and Skip until stopped read the failure again before the sheet opens; the sheet shows the exact tagged statements, the drift warning and privileges, starts on Cancel; Cancel and Escape record `audit/declined` with the statements; a plan that changed or is older than a minute is shown again with "Nothing was sent" and needs another click; the skip result (Skipped, checked after) and the re-read; the loop sheet ticks no error code, needs a cap, sends codes, max skips, max minutes; a running loop shows one line with Stop in the view and above other Server views, status read while it runs and not after; the session keeps only `replication_loop_active`; after a reload with no loop the view says "Stopped when myflames restarted" without reading the server or starting anything; a type change on the same saved connection keeps the loop and its Stop; another connection greys the report with skip actions off and no badge; 390 px |
| Maintenance | Last view under Setup; the job list (no database read) is asked on open, nothing else until Detect tools; Tools table (Available, Not found, Deprecated) and what is offered; sections as a segmented control, restored with the workspace (`maintenance_section`); Upgrade check: target from the Shell version or an exact one, Partial check without MySQL Shell sends `prefer_shell: false`, summary "Partial check (native subset)" with counts and the subset limitation in notes, Result column (Error, Warning, Notice, Passed, Not checked, Check failed), inspector with objects and documentation, MySQL Shell check summary; Dump: databases picker, output folder, Run preflight sends the request, a folder with files is refused with the server's reason and no sheet; the sheet (target, exact commands, warnings, checks) starts on Cancel, Cancel and Escape record `audit/declined`, Start sends the plan and `confirmed`; a running job shows progress, the last line and Cancel, is polled while it runs and not after, Cancel shows the partial state; a Start whose reply was lost finds the job in `lifecycle/jobs` and never sends Start twice; a completed job shows verification and the manifest; Restore: Start stays off until the target is typed exactly; Clone: failed preflight checks first with their reason in the inspector and no sheet; the donor password and fields never reach the session; a connection switch greys the jobs (Cancel off) and the plan, polling waits; after a reload the running job is found again on its connection; 390 px |
| Execution context | Missing context, schema/index expansion, run statistics, trace search/no results/clear (one pass over the trace per search; closed branches render nothing until opened), server settings |
| Investigations | Empty/loading states, names/notes/tags search, no results/clear, Save investigation in the top bar opens the notebook sheet (`dialog.notebook-sheet`: name, plan name, baseline, tags, notes, Export investigation bundle; no notebook form under the chart), Save in the sheet (Unsaved changes, then Investigation saved), Close or Escape keeps the edits unsaved, reload/reopen, recent investigations on Home, delete confirmation/Keep |
| Compare | Compare mode of Plans (remembered when returning to Plans), empty state, same-plan guard, measured versus estimated, two measured plans, Change as a ratio from 100% ("750× slower") with the exact percentage on hover, each pane's total time, bar value labels in both panes at 1440 px and after narrowing the window to 720 px and back (charts other than Visual Explain are drawn again for the new width), no inspector until a selection and both inspectors on one line, Operator changes in view at 1440 x 900 with one title per same-label pair, matching words (no enums) and sentence-case badges (`node test/compare-text.mjs`), row selection, HTML/JSON exports |
| Learn | View toolbar (Search lessons, Family filter, Start learning), one-line summary ("23 lessons, 16 in the learning path"), and grouped rows by family ordered by the learning path, each with its summary and "Step n of 16"; family labels and lesson titles in sentence case ("Scan, sort and temp family", "Derived table materialization: a subquery becomes a temp table"; `test_teach.TestLessonTitlesSentenceCase`); an open lesson has one compact toolbar (All lessons, Previous lesson, Step n of 16, Next lesson, 2D/3D when available, Download lesson), fills the remaining height, shows the lesson title in the top bar (with a colon, such as "Isolation levels: Repeatable Read vs Read Committed"), and receives `data-embedded="true"` so the lesson hides its own header and footer; Play/Pause/Reset, speed/loop/sliders, first/last boundaries, contextual return/reopen; lesson pages and the standalone catalog (`myflames teach --index`, which also has a lesson filter) use the workspace's light and dark tokens (`prefers-color-scheme`, `data-theme="light|dark"`, `?theme=light|dark`, `?embedded=1`), and the workspace sets the iframe's `data-theme` to match its resolved appearance while `--svg-*` drawing colors resolve through `anim.cssVar(name, fallback)` and `anim.palette()` and redraw on appearance change |
| Session restore | Autosave status (All changes saved, Saving…, Not saved), reload and server restart restore plans, place, view, metric, selection, notebook drafts, query tabs, parameters, the connection in use (picker and form password fields empty), the Server view and whether its inspector is open (a saved `running` opens Sessions; `constructor`, `__proto__` or `toString` open Health); sessions from earlier versions open on the matching place (plan explorer and compare open Plans, or Home with no plans; query laboratory opens the Query editor; Teach opens Learn); restored notice and Start fresh with and without a backup; conflict notice between two tabs; Restore workspace on startup in Settings off (no saves, leave-page prompt, also right after typing in a query tab with React's scheduler tasks held back 400 ms: the editor hands its tabs to the unload check in the same task as the keystroke, while the full state follows in a deferred effect; `npm run test:session` ten times in a row; `test:session-rules` checks that comparing editor states never serializes the query text) and on; `--no-restore` and its Settings hint; stale plan, view, or connection left out with a note; no password bytes in the database file |
| Portability | Analysis JSON, HTML report, comparison files, investigation bundle, imported bundle, capture JSON from `myflames import-shell`, unsaved-work backup, password exclusion |

For each primary screen check desktop, tablet, narrow, and short windows. The
September 2026 audit used widths 1440, 768, 390, and 320 pixels. Check both page
overflow and actual screenshots: an element can be clipped without increasing
the document width. Use viewport screenshots for sticky layouts; stitched
full-page captures can repeat sticky or embedded content.

## Security, grants, and appearance gates

Use a temporary HOME and USERPROFILE with `MYFLAMES_SECRET_BACKEND=vault` for
manual test servers as well as Python tests. Never use the real workspace store
or existing user containers. Browser harnesses must authenticate from the
server's launch URL; the shell no longer supplies a token.

Run `npm run test:auth` for launch-secret removal, one-use bootstrap, reload,
restart, proof-plus-cookie checks, cross-port isolation, and storage failure.
Run `npm run test:review-auth` for stale, altered, expired and replayed action
reviews. Keep request-budget, credential isolation, ended-session ownership and
storage descriptor checks in Python tests; browser fixtures do not establish
server security or live database behavior.

For grants, run `node test/accounts.mjs --unit` and `node test/accounts.mjs`.
Run `node test/accounts-all-grants.mjs` for every recorded server catalog,
user/role, legal scope, grant/revoke, static/dynamic/proxy grant option, role
admin/default setting, supported routine kind and anonymous proxy scope. Its
review plans come from the real Python planner; Apply is forbidden in the
synthetic browser fixture. Live validation remains a separate gate.
Check a user and a role with no direct database grants: add a database scope,
tick CREATE ROUTINE, Review changes, inspect the exact GRANT, Apply, and confirm
the direct checkbox after refresh. Check dynamic grant options, inherited cells,
partial metadata, manual names, invalid levels, scope deduplication, switching
tabs, and late object-list replies. Repeat the CREATE ROUTINE path against a
throwaway live server and verify SHOW GRANTS separately. Batch errors must
preserve applied/failed/not-sent outcomes and stop further sends.

Re-run the browser suites after theme changes. Capture light and dark versions
of every main view at 1440 and 390 pixels, with particular attention to chart
text and selection, Compare changes, Audit statuses, disabled controls, focus
rings, and exact SQL in review sheets. Inspect screenshots as well as overflow
measurements. Run chart parity after rebuilding.

For chart appearance, exercise the system preference, an explicit
`data-theme="light|dark"`, and standalone report/explore URLs with
`?theme=light|dark`. Test two plans and Compare in every chart view. Canvases,
node cards, controls and details follow the appearance; operator palettes and
the self-time heat scale retain their meanings. Text on light operator bands
stays dark. The workspace uses system blue for selection/focus and a darker
blue fill for white button labels: white on #007AFF is only 4.02:1 and white on
#0A84FF is 3.65:1, while the #0068D9 fill gives 5.27:1. Embedded lessons receive
the parent appearance and accent tokens without restarting the animation.

At 1440×900 and 390×844, also check these layout boundaries:

- Scroll Home connections, Locks transactions and Processlist to the far
  right. The final data header and value must fit before the opaque 36 px
  action column, which also holds Processlist's ⋯ row menu. Check hover,
  selection, column hiding and resizing. Plain Query results and the Locks
  blocking tree must not inherit menu clearance.
- Top statements, Processlist and Locks transactions fit at 1440 with the
  list of views and their default columns, without a sideways scroll.
  Processlist has Show plan and Kill in each row's ⋯ menu and in the details
  (no buttons in the row; Kill still opens the confirmation sheet on Cancel,
  and Escape returns focus to ⋯), and starts with Flags off (the Status cell
  names the flags on hover). Top statements starts with Rows sent and Schema
  off and keeps Calls, Total, Avg and Rows examined. Locks transactions starts
  with Lock structs and Isolation off. Numeric headers are short (Time, Avg
  (ms)) with the full name in the tooltip and the Columns menu. A column that
  starts off is in the Columns menu (showing it keeps it with the workspace;
  Reset columns turns it off again) and its value is in the details.
- A table wider than its frame (any of these at 720, after showing more
  columns, or Top statements ranked by a counter) fades out before the
  Columns menu column, which casts a shadow (visible in light; the fade
  carries dark); scrolled away from the start its left edge fades too, and at
  the far right neither right cue shows. The page itself never scrolls
  sideways. The Locks blocking tree fades at both edges the same way. Home
  connections fit at 1440 without scrolling (Name keeps 160 px; Recent work
  is at most 360 px wide).
- Top statements and other flexible SQL columns retain at least 260 px,
  monospace text, an ellipsis and a full-text title (the whole statement is in
  the details); narrow tables scroll within their frame.
- Reach the final command with the keyboard, including after filtering. When
  the list is taller than the palette it ends on a whole command (none cut in
  half above the footer). Scroll to InnoDB's final row, the diagram caption and
  Plans' final help text; the footer must not cover them (at the top of a long
  view the footer is simply the fold). Lesson navigation has space above and
  below it.
- At 1440 the Query editor fills the window exactly (the page does not scroll)
  and its schema header and query tabs sit flush under the top bar, before and
  after focusing the editor or running; Server, Workload and Tuning views with
  little content do not scroll either. The Query editor fills the window at
  every width; in a short window the SQL editor gives way so the results keep
  three rows above the footer. Scrolling a long Server view keeps the
  list of views pinned under the top bar.
- Health metric grids wrap flexible tiles so the final row fills its width
  (auto-fill/auto-fit alone still leaves gaps in a partially filled row).
  OK and No threshold keep
  their status text without a colored dot; Watch and Problem retain emphasis.
- Error log and Processlist keep a one-row toolbar at 1440 (Refresh,
  Auto-refresh, Kill sessions…); their filters are in the row above the list, like
  Indexes and Tables, and wrap only there. The Locks blocking tree fits its
  frame at 1440: Waits for is capped at 40 characters (full text on hover and
  in the details) and Statement keeps at least 200 px with an ellipsis; at 720
  it scrolls with the edge cues.
- Indexes and Tables rows have no Actions button: the ⋯ row menu sits in the
  trailing column. Every ⋯ (Processlist, Indexes, Tables, Query results) is
  hidden at rest and shows on hover, on the focused or selected row (Tab from
  the row reaches it) and while its menu is open.
- The phone connection pill retains its dot and at least a few name characters,
  and never overlaps the controls beside it.

At the desktop app's minimum window, 720×520 (its content area is 720×492
under the macOS title bar, so drive Puppeteer at 720×492 as well as the
720×520 screenshots), with the sidebar shown, check light and dark:

- Query editor: the editor and its results fill the window with no page
  scroll. After a SELECT runs, the result tabs, Export and at least three
  rows sit above the footer, and the SQL editor keeps at least three lines.
  The editor never takes the results' three rows: the splitter moves from the
  height on screen, and a drag past that limit stores the limit. The toolbar
  wraps to two rows and the run scope shares the second one.
- About myflames fits without scrolling: the credit line, Report a bug and
  Close are all on screen.
- Every review and confirmation sheet fits the window (Kill, Kill sessions and its
  review, the editor's run confirmation, Parameters and account reviews, Add
  column and Remove column, Import plan, Save investigation, the
  pt-query-digest export). The title and the actions stay in view and only
  the body scrolls, with a hairline on an edge that has more content.
- Server and Workload: a view shown at 1440 stays on screen when the window
  narrows to 720, including one a reload restored; Back is labelled with its
  place (Server or Workload) and shows the list of views. View toolbars keep
  Back, the title and Refresh on the first row, and the view's controls wrap
  on rows below it from the left edge, so no label touches the title (Top
  statements' Schema, Processlist).
- The Server and Workload top bar keeps the connection name in the pill (the
  type stays on its dot, in its accessible name and first in the window
  footer).
- Plans: the four measurements are columns, label over value, on one row.
  Flame graph, Execution tree and Bar chart controls take one row; Visual
  Explain takes two (the chart and Options, then the joined Fit and zoom
  group, the search and Legend). Compare puts the two plan pickers on a row
  of their own so both names are readable.
- Plans charts fit the pane: the Execution tree, Bar chart and Flame graph
  have no sideways scroll beside the sidebar, their times and shares stay on
  screen, and shortened labels end in "…" with the full name on hover.
  Shrinking the window from 1440 to 720 and back redraws the chart and keeps
  its search, selection and collapsed rows (`browser.mjs`).
- Add column and Remove column (Server > Tables) are modal: focus starts in
  the column name or on Cancel, Tab stays inside, Escape and Cancel return
  focus to the button or Actions menu that opened them (`tables.mjs`).
- No page scrolls sideways: the screenshot tool's overflow report lists 0 for
  every 720 file.

The compact Server view-menu change requires `Server.tsx`: replace the pushed
list (`showList = compact && !pushed`) with a compact, current-view-labelled
Popover in the place toolbar containing the existing grouped SourceList. Keep
`choose(id)` as the selection handler, close the popover on selection or Escape,
and return focus to its trigger. Keep the selected view visible while the menu
is closed; remove the compact `show-list`/Back presentation and its
`ViewOnScreenContext` suppression. Retain the existing keyboard navigation,
badges and item ids, mounting only one SourceList at a time. This navigation
change is separate from the CSS fixes above and still needs implementation.

Database diagram coverage includes offline SQL/Mermaid/JSON import, malformed
and oversized input, token/nesting/statement and model limits, the shared live
read budget, missing relationships,
conceptual versus foreign-key lines, keyboard and pointer movement, pan/zoom,
Fit, narrow inspectors, JSON layout round trips, escaped SVG text, loss warnings,
the Diagram | Mermaid switch (rendered diagram with `mermaid` 11.17.2 loaded in
separate chunks, `securityLevel: "strict"`, light and dark, zoom in/out/fit,
inline render errors, Copy, Download `.mmd` and export notes), Mermaid export
escaping (`#quot;`, `#39;`), readable sanitized types (`int_unsigned`,
`decimal(15-5)`), `PK`, `FK` and single-column `UK` markers, readable column
aliases with original names in comments, minimal comments, Mermaid import
reading names and nullability back, and connection switches. Live diagrams and a
selected database before the first build join Refresh all; imported
models stay unchanged. A reload must not pretend an unsaved diagram was restored.


## Regression commands

```bash
./run-tests.sh
cd apps/workspace
npm run build
npm run test:browser
npm run test:update-check  # Check for updates states, persisted time, stale result, About
npm run test:auth         # one-use launch, reload, cookie plus origin proof, restart
npm run test:review-auth  # reviewed-action tamper, replay and expiry
npm run test:diagram      # database diagram imports, layout, Diagram | Mermaid switch, exports and connection binding
node test/mermaid-export.mjs # Mermaid export cases parsed and rendered with the real Mermaid library
node test/accounts.mjs   # grant matrix, Add scope and batch review
npm run test:investigation
npm run test:exploration
npm run test:zoom
npm run test:discover     # Top statements table and inspector, Sessions Show plan, gates (mocked API)
npm run test:datatable    # Table columns: resize, Columns menu, Reset, restore, windowing, compact numbers
npm run test:server-nav   # Server shell: grouped sub-navigation, badges, inspector (toggle, ⌥⌘I, Escape, resize, width), popovers, gates, phone list and sheet, restore and old view names
npm run test:locks        # Locks: waiting DDL outline, Kill sheet (Cancel and Escape recorded as declined), blocking tree, tables, deadlock, auto-refresh (mocked API)
npm run test:sessions     # Sessions: Active/All, flags, inspector, Show plan, Kill (declined on Cancel and Escape), no PROCESS, restore (sessions-1.0 fixtures)
npm run test:innodb       # InnoDB: sections, inspector, links, raw output on request, no PROCESS (innodb-1.0 fixtures)
npm run test:errorlog     # Error log: Load, level and text filters, This run only, By code, inspector, MariaDB file and not available states, 390 px (error-log-1.0 fixtures)
npm run test:health       # Health: diagnostics rows, tiles, inspector, links, statements behind a number, sparklines, auto-refresh, restore (mocked API)
npm run test:report       # Full analysis (run, ranking, inspector, Not judged, export download, greyed after a switch, restore) and Refresh all (all sections, three at once, single flight, joined reads, failures per view, Escape, switch) (mocked API)
npm run test:waits        # Waits: events, classes, since start, not instrumented, links, auto-refresh, P_S off, restore (mocked API)
npm run test:indexes      # Indexes: picker and 2,000-table gate, unused/keep, redundant, full scans and the statements that name a table, Top statements table chip, userstat, links, restore (mocked API)
npm run test:tables       # Tables: picker, fresh numbers, missing keys, free space, shared tablespaces, sort/filter, links, restore (mocked API)
npm run test:io           # I/O: rate tiles, tables, files, kinds, links to Indexes and Top, P_S off, restore (mocked API)
npm run test:instruments  # Instruments: read on open, Performance Schema row, presets, Features/Instruments/Consumers/Actors and objects tabs, selection and UPDATE batches, links from other views (mocked API)
npm run test:config       # Parameters: read on open, parameter group table, filters, inline Edit, Preview changes, Review and apply batch, inspector actions, MariaDB aliases (mocked API)
npm run test:replication  # Replication: gates, table, inspector, skip sheet with the exact statements (declined on Cancel and Escape), changed and expired plans, result, skip loop start, status, Stop from any view, restart note, type change, greyed after a switch, MariaDB (mocked API, real audit)
npm run test:replication-source-version  # Replication > Topology: each source's engine and version beside its port (MySQL 9.7.2 from the relay log, from a saved connection, Version unknown with the reason and hint; MariaDB 11.8.9 with the full string in the tooltip), the provenance in the tooltip, the connector's details and the inspector's Source version row; dark; 390 px without page overflow
npm run test:maintenance  # Maintenance: tools, upgrade check (partial and Shell), dump preflight refusal and sheet (declined), start, progress, Cancel, lost Start reply, restore typed target, clone failed checks, greyed jobs, restore of the section and the running job (mocked API, real audit)
npm run test:session     # reload, restart, Start fresh, conflict, toggle, --no-restore
npm run test:navigation  # places, active state, open plans, Settings (Audit log, About), footer safety status and picker dot, Home connections, picker, keyboard, 390 px drawer (mocked profile API)
npm run test:profiles    # Home connections: remember / forget saved passwords; Type, Read-only, unsaved stays Production, moved Development warning (mocked API)
npm run test:audit       # Audit log: empty state, seeded records per status, filters, inspector, redaction, CSV/JSON export, retention, 390 px (real server, temporary database)
npm run test:charts       # chart runtime: search, keyboard, pin, collapse, two charts per page
npm run test:parity       # TS charts vs Python SVG on every fixture
npm run test:sql-selection    # statement splitting and run scope (Node, no browser)
npm run test:query-selection  # run selection / statement at cursor, mocked capture API
npm run test:editor           # running SQL in the tab's session: Run, Run all, results, Messages, Stop, Load more, transactions, confirmations, export, history, schema browser, session ended, restore (mocked editor API)
npm run test:placeholders     # digest ? to :p1 conversion, shared cases with Python (Node, no browser)
npm run test:digest-actions   # Top statements Explain / Run analysis / Explain top N from the inspector, mocked live API
npm run test:connection-binding  # reports, selections, reads and actions bound to their connection; late answers dropped; Kill, Explain top N and Run analysis targets; Locks polling while hidden; sheet focus; compare, capture cancel, and connection save/test races (mocked API)
npm run test:session-rules    # legacy view names, query tab ids, unload risk, connection identity (Node, no browser)
```

The Python suite checks server, parser, renderer, persistence, and capture
behavior. Browser suites exercise the packaged frontend; the zoom suite uses
real SVG geometry. The September 2026 audit also used direct browser interaction
against MySQL 8.4 with Sakila, checked all 23 lesson players, and rendered all
38 Sakila estimated/measured plans across all five chart types available then
(the treemap has since been removed).

## Platform checks

Repeat OS file drag/drop and native file-picker cancellation on the target
platform. Verify physical Magic Mouse/trackpad gestures and the native Chrome
favicon cache on macOS. These are distinct from in-app browser checks and
synthetic wheel tests. Inspect every permanent-delete confirmation, but use
backend tests for disposable deletion when interactive approval is unavailable.

This matrix covers named screens and representative states. It does not imply
every possible SQL input, animation parameter combination, or browser version
has been tested.

The Sessions and InnoDB fixtures in `apps/workspace/test/fixtures/` are computed
by the backend from the recorded live replays in `test/fixtures/perf/`; run
`python3 apps/workspace/test/server-fixtures.py` from the repository root after
the backend changes its output. The Replication and Maintenance fixtures
(`replication-*.json`, `lifecycle-*.json`, `upgrade-*.json`) come from
`python3 apps/workspace/test/ops-fixtures.py`: the live replication recordings,
`skip_one` and `SkipLoop` replayed over them, and the lifecycle plans, jobs and
upgrade checks computed with `test/test_lifecycle.py`'s fake tools and the live
recordings in `test/fixtures/lifecycle/`.

A live check of Replication and Maintenance needs throwaway containers only (for
example a MySQL 8.4 source and a GTID replica on their own Docker network):
break the replica with a duplicate key (insert a row on the replica, then the
same key on the source), skip it from the inspector and confirm the applier
runs and the source's next row arrived; check an upgrade with MySQL Shell and
with the partial check; dump one database into an absent folder under a
temporary directory and try a folder with a file in it. Remove the containers,
the network and the folders afterwards. The `--demo` screenshot run below takes
`DEMO_HOST`, `DEMO_PORT`, `DEMO_USER`, `DEMO_PASSWORD` and `DEMO_DATABASE`, so
it can point at such a replica.

For before and after evidence of a Server change, run
`node .claude/skills/ui-review/scripts/screenshot.mjs --demo --out DIR`: it
starts an isolated server (temporary `HOME`, `MYFLAMES_SECRET_BACKEND=vault`),
connects to the demo database (`./scripts/demo-db.sh`), loads every Server
view, adds fixture replays for states the quiet demo server cannot show (lock
chains, a waiting DDL, sessions, InnoDB), and writes each view at 1440, 768,
and 390 pixels, with and without the inspector, plus the density numbers
(rows above the fold, visible paragraphs, card heights) in `index.json`.


## Users and roles

Run from `apps/workspace`: `npx tsc --noEmit -p .`, `npm run build`,
`node test/accounts.mjs --unit`, then `node test/accounts.mjs` and
`node test/server-nav.mjs` on a host that allows the isolated browser server.
`node test/accounts.mjs --compile-only` checks the fixture build without opening
that server. Unit checks render actual account-list outputs from five recorded
MySQL/MariaDB versions; they do not replace browser interaction or visual QA.

Check Security after Setup, a loaded-list count badge, view-only restore,
canonical action keys, focusable denied controls with linked reasons, unknown
controls enabled, scope-level static Grant option and individual dynamic grant options, compacted
role changes, and one batch review for mixed edits. Before
Apply, compare the requested direct-grant preview and the exact SQL. Check typed
protected-account confirmation, Cancel/Escape audit, stale/lost plans, password
re-entry on Apply with fields cleared after each request, and once-only generated
passwords on both engines. MariaDB forms show the binary-log-only note and reviews
show validation-plugin warnings from the plan. Capture 1440, 768 and 390 px.

Before merging with other UI work also run `navigation.mjs`, `session.mjs`,
`connection-binding.mjs` and `report.mjs`: mounting the privilege provider adds
one read on connection changes, and Refresh all can now include Accounts.
The shared `perf-harness.mjs` inventory also needs `Security: ["Users and roles"]`
for `discover.mjs`; `instruments.mjs` must select Setup explicitly instead of
assuming it is the last group. Those files are outside the Stage A edit list.
Run both suites after their owners update them. Other mock inventories may need
`/api/live/privileges`; never remove stale
response or no-unrequested-view-read assertions to accommodate it.

## Stage B: gating map

Use canonical `privileges.ACTION_KEYS` when indexing `report.actions`; the
backend's aliases are accepted by `can()` but are absent from that report.
Use `PrivilegeGate`, or `usePrivilege` with `privilegeButtonProps` and a reason
element whose id is passed as the fourth argument (`aria-describedby`). Keep
busy, stale-connection, target and write-policy guards. A definitive denial stays
visible and focusable; unknown stays enabled with Unverified. Target-specific
backend answers take precedence over generic new-session answers.

| Existing control | Action key | Integration rule |
| --- | --- | --- |
| Locks `KillControl`, all blocker/transaction/metadata rows; KillDialog confirm | `kill` (both connection and query) | Remove the current hidden-on-denial branch. Preserve own-user and target SYSTEM_USER hints; generic `kill` must not disable a permitted own-user kill. Keep target guards and policy re-read. |
| Sessions inspector Kill connection / Kill query and shared KillDialog | `kill` (both connection and query) | Same rules as Locks; do not hide denied Kill. |
| Sessions Show plan | `explain_connection` | Use the target-specific hint including own-user; PROCESS is not a universal MySQL Explain requirement. |
| QueryLab Explain, Analyze selection / Run analysis, keyboard shortcuts | `explain`, `run_analysis` | Object absence stays unknown; existing SQL eligibility, placeholders and confirmation still apply. Recheck in the capture handler so shortcuts cannot bypass a gate. |
| SlowQueries row/inspector Explain / Run analysis and Explain top N | `explain`, `run_analysis` | Apply to individual row actions and batch entry. Preserve the bound digest identity; show unverified in analysis review. |
| Parameters inspector Change…, inline Review and apply, every selected mode and confirm | `set_global`, `set_persist`, `set_persist_only`, `set_session_restricted`, `reset_persist` | Use the mode and parameter-specific plan hint. Ordinary SET SESSION does not need the restricted privilege. Do not disable Change… solely because one mode is denied; let the user choose a permitted mode. |
| Replication Skip this transaction and confirm | `skip_gtid` or `skip_counter` | Choose from the server's selected method. Tagged GTIDs and MariaDB need the complete target-specific hint. Bridge the resulting reason to the existing `missingPrivileges.replication_skip` prop. |
| Replication Skip until stopped and Start skipping | Same method-specific key | Bridge to `missingPrivileges.replication_skip_loop`. Re-evaluate each iteration on the backend. Keep Stop enabled even after a privilege loss. |
| Maintenance Dump prepare and confirm | `dump_shell`, `dump_mysqldump`, `dump_mariadb` | Select by the tool chosen by preflight, including dump options. No generic `dump` key appears in the report; unknown stays enabled until the tool is known. Existing `missingPrivileges.dump` is a presentation adapter. |
| Maintenance Restore prepare and confirm | `restore` | Actual dump contents determine privileges. Absent object grants remain unverified; server preflight decides. |
| Maintenance Clone prepare and confirm | `clone_recipient`, donor `clone_donor` | Evaluate donor using its own credentials, recipient with in-place/data-directory context. Do not use the recipient provider to decide donor privileges. Hide unsupported engines. Bridge to `missingPrivileges.clone`. |
| Maintenance Upgrade check Run | No action key yet | `upgrade_check` is absent from `ACTION_KEYS`. Until implemented, unknown stays enabled; show checker findings. Bridge to `missingPrivileges.upgrade_check` only when authoritative. |
| QueryLab Run, Run selection, Run all and keyboard shortcuts for writes | Backend classification maps to `insert`, `update`, `delete`, `create_table`, `alter_table`, `drop_table`, `create_index`, `set_session_restricted`, `set_global`, account keys, etc. | No single global Run privilege. Aggregate statement-specific answers from the persistent editor session, including SET ROLE; unknown remains enabled. Keep read-only refusal, review, audit and transaction behavior. Stop, rollback and closing a session must remain available. |
| Health and Parameters Load/Refresh | `health`, `config`, `config_sources` | Reads stay enabled; no global privilege required. |
| Sessions Load/Refresh | `sessions_own`, `sessions_all` | PROCESS absence narrows the data; never disable reading own sessions. |
| Locks Load/Refresh | `locks_transactions`, `locks_metadata` | Keep Refresh enabled; show prerequisites per unavailable section. |
| InnoDB Load/Refresh | `innodb_status` | Keep Refresh enabled; show the PROCESS prerequisite if no data can be read. |
| Top statements, Waits, I/O, Instruments, Indexes usage Load/Refresh | `top_statements`, `waits`, `io`, `instruments`, `indexes_usage` | SELECT on P_S cannot be proven absent; keep enabled. Surface the backend prerequisite when P_S is unavailable. |
| Tables and Indexes Load/Refresh | `table_lists`, `tablespaces` | Keep enabled with partial coverage; PROCESS absence makes tablespace data unknown. |
| Replication Load/Refresh | `replication_status` | Keep enabled; render the server's prerequisite message. |
| Server Refresh all and Full analysis | Per-view read keys above | No synthetic aggregate privilege or write action. Keep partial results and unavailable sections. |
| Users and roles Load/Refresh, read selected account | `accounts_list`, `grants_other` | Keep enabled; own-account fallback and warnings come from the server. |
| Users and roles New user / New role | `create_user`, `create_role` | Wired in Stage A. Exact global denial disables; object alternatives can keep the answer unknown. |
| Lock / Unlock / Expire password | `lock_account`, `unlock_account`, `expire_password` | Wired in Stage A; system accounts are read-only. |
| Set password / Rename / Drop | `set_password` / `set_own_password`, `rename_user`, `drop_user` / `drop_role` | Wired in Stage A. Protected-target and self-lockout typed names come from plan guards. |
| Matrix grant/revoke, Grant option, Review changes / Apply | `grant`, `revoke` | Generic hints are provisional; the plan checks each selected scope and privilege. Separate groups share one batch review and each receives a fresh fingerprint before applying. A definitive denial disables only affected edits. |
| Role membership/admin option/defaults | `grant_role`, `revoke_role`, `default_roles` | Wired in Stage A. ADMIN OPTION alternatives remain backend decisions. MariaDB permits one default role. |

Read actions deliberately stay enabled even when state is denied and
`grey_out:false`; the prerequisite belongs inside the view. A read-only
connection blocks writes independently of its privilege answer. Do not
equate a missing global CREATE USER with a denied action when Python reports
an unknown alternative through `mysql` object privileges.


Stage B also wires the shared provider to the Account and privileges sheet (a
connection picker item and a command palette action) and
successful reconnect notifications (`connectRevision`), and calls
`accountChanged()` after contradictory denials. Password changes on the active
connection, connection identity/type changes and stale window-focus refreshes
are handled by the Stage A provider. Never infer persistent editor-session
privileges from its new-session report after SET ROLE.

## Workstation interaction regression checks

- Database diagram: connected database picker, typed fallback, explicit Rebuild, added/removed tables and foreign keys, retained positions, import replacement cancel/confirm, stale connection responses, Diagram | Mermaid switch with rendered Mermaid preview, and Mermaid export/import round trips (`test/mermaid-export.mjs`).
- Maintenance tools: official links for available and missing tools, MariaDB aliases, newer GA badges, same-series client comparisons, offline/malformed/redirected sources remaining unknown, cache expiry, bounded concurrent checks and no network from default CLI discovery.
- Backup folders and options: native selection, cancel preserving the path, busy and timeout fallback, manual entry, no backup started by selection, unsupported options refused, Shell version/scope gates, exact reviewed options retained in execution and manifest, and explicit separation of file encryption from TLS or network compression. Windows dialog tests use mocks until exercised on Windows.
- Top statements and Query editor: full-width details at 1440 and 390 pixels, return restoring list state, safe projection warning and SQL review, UPDATE assignments omitted, unsupported DML blocked, original editor text retained, second capture click required, and batch conversion excluded.
- Tables and Instruments: 238 GiB reusable in about 2 TiB remains informational and does not trigger `--fail-on watch`; optional Optimize and Apply display exact SQL, decline sends no write, Development still confirms, read-only and stale connection refusal, progress/Stop, and refreshed evidence after success. Capacity alerts require separate measured evidence.

Paired checks include `test_tool_updates.py`, `test_schema_health.py`, `test_folder_picker.py`, `test_dump_options.py` and `test_analysis_query.py`, plus the Maintenance, Database diagram, Tables, Instruments and analysis-projection browser suites. Recomputed browser reports retain their original metadata provenance; they are not fresh live-server evidence.

## Workspace refinement checks

`test/update-check.mjs` exercises Check for updates in Home's footer and About myflames: no check without a click, progress with focus kept on the button, up to date, a newer release with Download opening the update dialog, a failure with its reason, the last check time kept across a reload, and a result older than 24 hours shown as the last result, in light and dark at 1440 and 720 pixels. `test/home-workspace.mjs` exercises Home version/release information, grouped SQL navigation and real Mermaid parsing into the canvas. `test/table-index-pagination.mjs` exercises more than 200 rows, global sorting, last-page access, complete exports and narrow layouts. `test/report-table-export.mjs` checks CSV formula protection, HTML escaping, Unicode and report credits. PDF integration also requires a native Electron print check. Parameters draft controls must never execute on toggle or input change.
