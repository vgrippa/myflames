# Performance workspace

The same workspace can run inside the optional Electron development app in
`apps/desktop`, currently targeting macOS. See [Desktop app](DESKTOP.md) for
setup. Compact navigation, open query documents and the command palette (⌘K)
are shared with the browser workspace. Server details use the full work area;
Back returns to the list with its selection, filters and scroll retained.
If Refresh changes the rows while details are open, Back fits the columns to
the new values even at the same window width.

The myflames Performance workspace is a local browser app for MySQL and MariaDB
performance work: execution plans, live captures, and what the server is doing
right now (server health, InnoDB status, the error log, top statements, sessions, locks,
wait events, index health, table health, I/O hot spots, and server parameters). The
[query investigation guide](QUERY_WORKSPACE.md) documents linked exploration,
local saves, live capture, execution context, and the Server views. See the
[UI testing checklist](UI_TESTING.md) for screen and interaction coverage.

Launch the workspace from an installed package:

```bash
myflames ui
```

From a source checkout:

```bash
python3 -m myflames ui
```

The command starts a server on `127.0.0.1`, chooses an available port, and opens
your browser. Keep the terminal running while using the workspace. Press Ctrl+C
to stop it. Python is the only runtime requirement; the frontend is bundled with
the package.

```bash
myflames ui --no-browser           # print the URL without opening a browser
myflames ui --port 8765            # choose a fixed local port
myflames ui --no-restore           # start clean once; the saved session is kept
```

The workspace autosaves and reopens where you left off: open plans, query tabs,
the connection in use without its password, the screen, chart view, and
selection, and the Server view with its inspector open or closed. See [Session restore](QUERY_WORKSPACE.md#session-restore).

Open the fresh launch URL printed by this process. The URL contains a one-use
secret that is removed from the address bar as soon as the app starts. Reloads
use an HttpOnly cookie and this browser origin's stored session proof. The
page itself contains neither credential. A server restart needs its new launch
URL. If browser storage is blocked, the current tab works but cannot recover
after a reload without a fresh launch. See the [authentication contract](DEVELOPER_REFERENCE.md#local-authentication-and-request-limits).


## Find your way around

Actions show a busy label and keep keyboard focus while waiting. Top statements,
Error log and Database diagram offer Stop for slow reads and Try again for read
errors. In the Query editor, Stop and session operations show elapsed time;
Commit and Roll back report an unknown outcome when their reply is lost. The
workspace does not retry those writes. Close on a stalled diagram import stops
waiting and keeps the current diagram.

The sidebar separates where you go from what you have open:

| Place | What it holds |
| --- | --- |
| **Home** | Connections in a table beside one Recent work list of open plans, saved investigations and query tabs. **Add connection** is the primary action in the Connections toolbar. **Import plan** appears once, in the top bar, whether or not anything is open; the empty Recent work list offers **Explore sample plan**. Passwords show **Saved** or **Not saved**, and a connection without a default schema shows a dash. **News**, under Recent work (below it on a narrow window), lists up to six MySQL and MariaDB headlines with their source and age; each opens in your browser, **Refresh news** reads again, and one line says when news is loading, unavailable offline (with when it was last updated) or empty. The version line sits at the bottom of Home with **Check for updates**, which checks on demand (the app also checks at start and once a day while **Settings > Check for updates automatically** is on) and shows the outcome with the time of the last check (Not checked yet, Up to date, a newer version with **Download…**, or why it could not check; a result older than a day reads as the last result). When a current check finds a newer version, a round badge beside the flame logo at the top of the sidebar shows the update available, the download's progress, preparing or ready, and while an update is pending the Settings popover lists **Update available…**, **Update progress…** or **Restart to update…**; either opens the Software update dialog, whose **Update** stages the verified update file and **Restart to update** installs it in place. About myflames has the same status and button. Dropping a plan file anywhere on Home imports it. **New query tab** belongs to the Query editor |
| **Plans** | The open plan with its charts, findings, and inspector. **Explore** and **Compare**, in the top bar beside the place name, switch between one plan and two plans side by side; **Import plan** is at the end of the top bar. One plan header holds the plan's name, the stats (time, rows examined, rows returned, findings), **Plan summary** (or **Investigate first** when a finding leads), **SQL** with **Copy SQL**, and **Export**; Plan summary and SQL each open a panel over the chart with one click (a click elsewhere or Escape closes it). The chart with its own toolbar and the inspector fill the rest of the window; findings and execution context follow below. **Save investigation** opens a sheet with the notebook (investigation name, plan name, baseline, tags, notes and conclusions, Export investigation bundle) and saves with **Save**; **Close** keeps edits as unsaved changes; while Save is running it continues and reports its outcome in the window |
| **Query editor** | SQL tabs that run any SQL in a session of their own (Run, Run all, Stop, results per result set, Messages, export, transactions), Explain and Run analysis, the schema browser, the run history, and the connection in use |
| **Query digest** | Right below the Query editor: statement rankings with a named source, totals, a sortable table and a shared inspector. **Open in Query editor** and **Explain** are in the inspector and row menu; **Export** offers Text report and CSV. See [Query digest](#query-digest) |
| **Database diagram** | After Query digest: the tables, columns and relationships of a database, read from the connection in use or imported without executing SQL, with a Diagram \| Mermaid view. See [Database diagram](#database-diagram) |
| **Schema versions** | After Database diagram: versions of a database's schema saved on this computer, and comparisons of any two schemas (saved versions, or databases read now through any connection): what differs between a source and a target, and the sync script that would change the target to match the source. See [Schema versions](#schema-versions) |
| **Server** | Views of the connection in use that only read (the exceptions are **Kill** in Locks or Sessions, which ends one session after you confirm and needs the privilege, parameter changes in **Parameters**, runtime instrument or consumer updates in **Instruments**, a dump, restore or clone in **Maintenance**, and account changes in **Users and roles**, each after its own confirmation), in a grouped sub-navigation: **Overview** (Health, which opens first, InnoDB, Error log), **Contention** (Locks), **Schema** (Indexes, Tables), **Setup** (Parameters, Instruments, Maintenance), and **Security** (Users and roles). See [The Server place](#the-server-place) |
| **Workload** | The same kind of views for the statements and resources: **Queries** (Top statements, Processlist) and **Resources** (Waits, I/O, Memory) |
| **Replication** | Replication channels or connections of the connection in use, with a skip after its own confirmation. A server with no asynchronous channel reads **Not a replica** (never Unknown), and the item is greyed in the sidebar once a read says so |
| **Cluster** | The MariaDB Galera Cluster, Galera Cluster for MySQL, Percona XtraDB Cluster or MySQL InnoDB Cluster (or Group Replication) the connection in use belongs to: members, roles, state, quorum, failures tolerated and routers, with a member's changes (Make primary, Stop or Start Group Replication, Desync, Resync, Bootstrap a Primary component) in its inspector, each after its own confirmation. Greyed in the sidebar once the connection is known not to be part of a cluster |
| **ProxySQL** | The ProxySQL Admin interface the connection in use reaches: backends as ProxySQL runs them with their connection pool, hostgroups, query rules and query digests, with a server's or rule's changes in its inspector and Load and Save to disk on the Configuration line, each after its own confirmation. Greyed in the sidebar once the connection is known not to be a ProxySQL Admin interface |
| **Readyset** | The Readyset SQL adapter or Admin SQL listener the connection in use reaches: multi-tier topology across ProxySQL, Readyset and upstream MySQL or MariaDB servers, shallow and deep caches, proxied queries with live `EXPLAIN CREATE CACHE` prechecks, replicated base tables, dynamic parameters and allowlists, and cached-versus-upstream telemetry, with reviewed operational changes. Greyed in the sidebar once the connection is known not to be a Readyset instance |
| **Tuning** | A place of its own (for MySQL and MariaDB; the command palette also finds "mysql tuning"): the tuning scorecard full width, without Server's list of views, and the **Full analysis** as its second tab. Links to the Full analysis open it there |
| **File transfer** | A place of its own, after Tuning: saved **SSH connections** on the left and, for the one chosen, two panes side by side (**This computer** and the server) with the **Transfers** panel below, to move files over SSH/SFTP like FileZilla. It does not use the database connection in use. See [The File transfer place](#the-file-transfer-place) |
| **Investigations** | Saved investigations: search, open, delete; an investigation saved by Health's **Capture when…** lists its **Evidence** (processlist, InnoDB status, locks) with Copy and Download. With none saved, the empty list is hidden. If plans are open, **Save N open plans as an investigation…** opens the notebook sheet; with no plans open, the empty state offers **Import plan** |
| **Learn** | The lesson catalog and player (`myflames teach` on the command line) |

The app follows the system light or dark appearance. Check chart labels,
Compare highlights, tables, dialogs, and Audit status colors in both appearances;
status text and icons remain available alongside color.

### Query editor results and connection changes

**Export** sits at the end of the results tab row, beside **Result N** and
**Messages**, and applies to the selected result.

Changing the connection picker leaves each tab's session on its original
server. When these differ, the editor says "This tab is still connected to
A. Close its session to run on B." and offers **Close session**. Run, Run all,
Explain and Run analysis stay disabled until the session is closed or you
choose its connection again. Stop, Commit and Roll back still use the tab's
session. An old result says **From &lt;connection&gt;**; closing the session keeps
that label, and Load more is unavailable for that result.

The ALTER TABLE review sheet starts its facts with **Predicted**: INSTANT,
INPLACE with or without a table rebuild, or COPY, and whether writes continue
(for example "INPLACE, rebuilds the table"), or "not predicted" with the
reason. **Why** under it opens each clause's reason, the server source it
follows and any assumed defaults. When the prediction is INSTANT the sheet
offers no `ALGORITHM=INPLACE, LOCK=NONE` rewrite. The prediction never changes
the statement; the server decides when it runs. See [Run SQL in the Query
editor](QUERY_WORKSPACE.md#run-sql-in-the-query-editor).

### The Server place

Every Server view has the same anatomy, after the Apple apps DBAs already know
(Activity Monitor, System Settings, the Finder inspector):

- **Sub-navigation.** The Server views are grouped by job: Overview,
  Contention, Schema, Setup, Security (Workload groups Queries and Resources in its own place). Each item shows a marker from data
  you already loaded (Health and Parameters show their worst check status, Locks
  the blocked sessions, Sessions the active ones, Indexes what to review). One
  system everywhere, the sidebar places included: a small dot for state (red
  for a problem, amber to watch, a neutral dot for Not judged, and no marker for OK in navigation)
  and a neutral count beside it, so color always means state.
  Nothing is read just for a badge. A view reads when it is opened on screen,
  once per connection (Refresh retries), Top statements and Error log
  included; Tables and Indexes keep their schema-size consent, and nothing
  reads off screen (Server, Replication, Cluster, ProxySQL, Readyset and Query digest
  mount on their first visit, and a hidden view never reads on its own). The arrow keys move between items.
- **The list of views floats** by default in a wide window, so the view gets
  the full width: the view's title is a pop-up button ("Health ▾") that opens
  the list under it, with the same groups, icons and markers. Enter, Space or
  Down opens it on the current view; the arrow keys, Home and End move; Enter
  or Space chooses (the list closes and the chosen view's title has focus);
  Escape or a click outside closes it and returns focus to the title. While
  the list is closed, a labelled badge on its chevron gives the number of
  other views that need attention, with the reason on hover and in its
  accessible name. The title has no bare status dot: its summary describes
  the view on screen, and the badge describes the other views. **Dock the list of views** (in the list's header or
  the top bar) keeps it beside the view instead, and the choice is remembered;
  docked from the list's header, focus moves to the top bar's toggle, which
  now reads **Float the list of views** and floats it again. Workload works
  the same way. In a narrow window the list is a screen of its own, as below.
- **Toolbar.** The view's title with an ⓘ that explains it, its controls (a
  sampling window, auto-refresh, a database picker, a filter) and **Refresh**.
  List controls wait on the list while details are open. Refresh stays at the
  end of the toolbar, also on a detail page; the arrow beside it opens
  **Refresh every view on <connection>** (Refresh all), so a view shows one
  Refresh. The place's own controls follow the connection picker in the top
  bar: an ⓘ with what Server does, one control for the list of views (dock or
  float it; in a narrow window a **Views** menu instead), and, while Refresh
  all runs, its progress and **Stop**.
- **One summary line**, with the status and a **N notes** button for engine
  differences, privileges, and caveats. Summary status words are **OK**,
  **Watch**, **Problem**, **Not judged** and **Unknown**.
- **A table or one-line rows.** Tables sort by any column, keep the header in
  view, and scroll sideways inside their frame: an edge with more columns
  beyond it fades out, so a cut column reads as scrolled. The **Columns**
  button has its own narrow gutter at the frame's right edge, outside the part
  that scrolls, so no scroll position puts a header or a value behind it; a
  row's ⋯ actions scroll with their row. Columns that start from a fixed width wider than their values give that room
  back before anything truncates. Rows take the arrow keys (and
  Page Up, Page Down, Home, End); Enter or a click opens the inspector, Escape
  closes it and returns to the row. Large counts read compact (159.0B, 14.2M)
  with the exact value on hover and in the inspector; a long name truncates
  (full text on hover) and its flags stay beside it. Tables with more than 300
  rows draw only the rows in view. Status cells show a dot and a word,
  including **OK** in Health and InnoDB rows; OK uses quieter text.
  A metric tile without a threshold draws no marker at all.
  Metric tiles share equal widths in a wide window. At 900 px and below,
  they form one compact strip that scrolls sideways within the view, and
  the facts line folds into **Details** so the table starts nearer the top.
  Each Health tile group heading then names its share of the summary's count
  ("1 problem, 1 to watch"), so a tile scrolled out of its strip still adds
  up. InnoDB section summaries keep the same gap after an OK status as after
  any other.
- **Errors.** A read or change that fails to connect leads with plain words
  and what to check, then the client's own error line: could not connect
  (check that the server is running and the host and port, or the socket
  path, are right), the host name was not found, the connection was lost
  (with what was being done, such as "Could not end connection 41: …"), or
  the server refused the user name or password, said only when the server
  answers Access denied. "Could not complete the request:" is added only to
  a message that does not already say what failed.
- **Columns.** Drag a header's right edge to resize any column, or focus the
  edge and use the Left and Right arrow keys (Shift for bigger steps, Home and
  End for the narrowest and widest); double-click the edge to fit. The
  **Columns** button, in its gutter at the right of the header, lists every column with a
  checkbox to hide or show it (the last visible one stays) and **Reset
  columns**. Widths and hidden columns are kept per table with the workspace.
  So that the widest lists fit beside the list of views, a few columns start
  off and stay in the Columns menu, with their values in the details: Flags,
  Database and Command in Processlist (the Status cell names the flags on
  hover, and State shows the command when the server reports no state, such
  as Sleep), Rows sent and Schema in Top statements, and Lock structs and
  Isolation in Locks transactions; Parameters does the same with Allowed
  values, Data type and Description while its inspector is a sheet. In every
  Tables list the Table column keeps at least 220 px for `schema.table` names.
  Durations read the same everywhere, as the CLI writes them: "36 s",
  "4 min 12 s", "4 h 5 min", "18 d 12 h". Exact seconds, including decimals,
  are on hover in table duration cells and these facts: Processlist's Time in
  state, Open for and Lock wait; Locks' Longest wait and Oldest transaction
  summary facts; and Replication's Lag, Configured delay and lag signals.
  Locks' Idle duration in the Transactions table also has exact seconds on
  hover, as do the Kill sessions preview's duration cells, Replication's lag
  tile and topology labels. Replication's Channels and Lag tables sort on raw
  seconds and retain lag caveats on hover. Summary sentences, other Locks
  badges and narrative text, and the Query editor's ALTER review transaction
  age use words without promising exact seconds on hover. Sampling windows,
  thresholds, budgets, input controls and progress counters keep their units.
  Processlist shows State (what the session is doing now) beside Time (how
  long it has been in that state).
  Statement text in Processlist is capped at 4,096 characters with a truncation
  indicator when longer.
  Processlist rows have no buttons: each row's ⋯ menu holds **Show plan** and
  **Kill…**, which opens the same confirmation sheet as Kill in the details.
  Right click on a row (or Shift+F10, or the context menu key) opens its
  menu: Show plan, Open in Query editor (a new tab, like Show index and Show
  create table elsewhere), Open in Locks, filters, **Copy
  query**, **Copy connection ID**, then **Kill query…** and **Kill
  connection…** for that row. Each opens the same sheet with that KILL
  chosen. An item that cannot run stays in the menu with its reason under it
  (a read-only connection, settings still loading, a missing privilege, a
  server thread, a session with no statement to stop). While the KILL is sent
  the sheet's button says so; the answer then shows above the list at once
  (Done, Already ended, The server refused, Outcome unknown, or Nothing was
  sent) while the list is read again. A successful result begins with **Done.**
  followed by the server's explanation; Kill sessions uses the same headline.
  When the server accepted KILL but its verification read failed, the explanation
  and Audit log say **Sent; could not verify**. Refresh the list to check completion.
  If a KILL has no reply after ten seconds, **Close** leaves it running and
  shows its progress above the list. Its eventual result names the original
  server when another connection is in use, and offers no Refresh for that
  other server. A late bulk reply never closes a newer Kill sessions dialog.
  A policy refusal or failed policy read also replaces the waiting note after
  Close. It says **Nothing was sent**, names the original target as **Reviewed
  on** when needed, and does not claim an execution audit record.
  **Kill sessions…** in the Processlist toolbar opens the Kill sessions
  dialog: presets (Runaway queries, Idle sleeping, Lock waiters, All clients)
  and filters choose client sessions (system threads and your own connection
  are never listed); **Ignore users** ticks users seen in the loaded sessions
  and lists the chosen names under the field (a name outside the list can still
  be typed), and **Match host** suggests the client hosts of the loaded sessions
  and still takes free text, including patterns. Kill N queries or Kill N connections shows the exact
  KILL statements and the server in a confirmation sheet that starts on
  Cancel. Nothing is sent until you confirm, the connection's type and
  read-only setting are read again first, and Cancel is recorded as declined
  in the Audit log. On a read-only connection the dialog says so and its Kill
  button stays off. Group Replication data-stream connections are marked as
  replication system threads and protected from Kill, including when MySQL
  reports their command as `Group Replicatio`; the reason points to Cluster.
  Bulk Kill validates every target before checking policy and keeps an audit
  row per target, including refusals, declined or expired reviews, and credential
  or connection failures. It reads server metadata once for the batch, then
  rechecks each target, transaction, server identity and saved policy before
  sending. One final process-list snapshot supplies the results and audit
  after-state, without a settle wait per target. An unreadable or interrupted
  snapshot leaves the after-state unknown and says **Sent; could not verify**.
  Acceptance and verified completion are separate; a failed verification never
  resends KILL.
  Indexes and Tables use the same ⋯ row menu (Set invisible…, Drop index…,
  Table health; Add column…, Remove column…, Optimize, Analyze and Check
  table…, View indexes), with the labels of the row's right-click menu; an
  item that opens a review ends in an ellipsis. The reviews that send an
  ALTER TABLE (Set invisible…, Drop index…, Add column…, Remove column…) show
  one **Predicted:** line with the algorithm the server is expected to use;
  Add column… and Remove column… send the ALTER alone, and the Query editor
  adds its own lock wait guard. In the Indexes inspector, Set invisible (or
  visible; Set ignored on MariaDB) and Drop index appear once, in its action
  bar; the advice under them explains and offers the SQL to copy. Tables and
  Indexes keep their filter (**Filter tables** in Tables), count, Rows, Export
  and paging together above the list, while the database scope and Fresh
  numbers stay in the view toolbar. A row's ⋯ shows on the row being pointed at, the
  focused or selected row and the row whose menu is open, so Tab reaches it
  from a focused row; touch screens always show it.
  Short numeric headers (Time, Avg (ms)) give the full name on hover and in
  the Columns menu.
  Modes of one view are a segmented control (Active / All, Unused / Redundant,
  Events / Classes); rarely used detail waits behind a disclosure (Advanced,
  Breakdown, Not instrumented). A list's filters sit in the row above it, as
  in Indexes and Tables: Error log (Entries / By code, levels, Time, the text
  filter, This run only, Export) and Processlist (Active / All / Locked,
  Source, Running at least, the filter), so the view toolbar keeps one row
  with Refresh, Auto-refresh and **Kill sessions…**.
- **Details.** The selected row's explanation, rule, numbers, statements and
  actions (Explain, Show plan, Kill, links to other views) occupy the full work
  area on both wide and narrow windows. Back or Escape returns to the same
  list and selected row. Kill opens a confirmation sheet that starts on Cancel; Cancel
  or Escape sends nothing and is recorded in the Audit log as declined.
  Labels that are identifiers (`innodb_buffer_pool_size`) take their own line
  above the value.
- **Prerequisites.** A view that cannot run (Performance Schema off, an
  instrument off, no PROCESS privilege) says why in one sentence, with one
  action such as the Instruments row that turns it on.
- **Privilege-aware buttons.** Action buttons across Server views (such as Kill,
  Change variable, Skip transaction, Dump, Restore, Clone, and user management)
  reflect effective database privileges. Definitive denials disable the button
  with `aria-disabled="true"`, keeping it keyboard focusable while blocking click
  and keyboard triggers, with the required privilege stated in its tooltip and
  accessible description. The description is also visible beside the action or
  on keyboard focus, including connection-policy restrictions. Unknown privileges
  remain enabled with a separate explanation so incomplete catalog visibility does not block valid
  actions. Connection write policies remain authoritative: read-only connections
  refuse writes, and Production connections ask for explicit confirmation.

**Refresh all** (**Refresh every view on <connection>** in the menu beside a view's
Refresh; ⌥⌘R or Alt+Ctrl+R) reads Server
and Workload sections, Replication and Cluster, including sections not yet opened,
using each view's window, databases and scope. Database diagram is included once
a database is chosen; imported diagrams stay unchanged.
At most three section reads run at once, with Health, Waits and I/O first.
Full analysis runs in a separate phase because it manages its own bounded reads.
Maintenance detects tools; it never starts upgrade checks or jobs. Existing
large-schema consent applies only to the same connection and scope.
While it runs the top bar shows its progress and **Stop**
(Escape also stops it) and each view waiting or reading shows a spinner in the
list of views; a view whose own Refresh is already reading the same thing is
joined, not read twice. Afterwards one line says "4 of 5 views refreshed; Locks
failed"; it opens the list of views with each outcome and when it was refreshed.
A view that failed or was not reached keeps its previous report, gets a mark in
the list of views, and shows the reason in one line above it, until a newer
report replaces it (its own Refresh); the list keeps what the run did. A view
counts as refreshed only when its answer was shown: if a newer read of that
view was asked for meanwhile, the run lists it as Replaced. Settings a view
keeps to itself (Locks' Waits only, Tables' Fresh numbers) are the ones of the
read that view last asked for. Switching the connection stops a run and drops
its late answers; it also drops any view's read still on its way, so switching
back never shows an answer read before the switch. It can start before any
section has been loaded.

**DDL progress.** Processlist shows how far a running ALTER TABLE (or other
DDL that reports stage counters) has come: a small bar and percentage in its
State cell, with the raw stage and the estimate on hover, and a **DDL
progress** section in its inspector with the stage, the percentage, the work
done and estimated (rows for ALGORITHM=COPY, work units for InnoDB stages), the
elapsed time and the time left, exact seconds on hover. The time left is an
estimate, and it reads unknown below 1%, before the first second, and while the
server's own estimate only grows with the work done (out-of-date table
statistics), rather than showing a false 99%. **Follow progress** sets
Auto-refresh to every 10 seconds. The progress comes from Performance Schema
stage events; on MariaDB with Performance Schema off it comes from the
server's own progress columns, for the operations that report them (the copy
algorithm). When the stage consumers or instruments are off, the view says
which and **Turn on stage tracking…** opens that row in Instruments, where the
change is reviewed; reading progress never changes a setting. The Query
editor shows the same meter under its own running ALTER TABLE, OPTIMIZE TABLE
or CREATE INDEX, read every 2 seconds while the editor is on screen, and stops
when the statement ends, you press Stop, switch tabs or leave.

**Health** shows **Live charts** above Diagnostics from its first reading
(queries per second, threads running, buffer pool reads from disk per second
and connections in use), from up to 60 readings per connection kept in
memory. With one reading each card shows its value only, with "Collecting…" beside Live charts; from the second reading each chart draws with its current value
and the range shown, and readings that never change say "Steady over the last
N readings" instead of a minimum equal to its maximum (the inspector's trend
and the tile sparklines say the same). Hover a chart for each reading's local
time and value. **Auto-refresh** defaults to **While visible** (using the sampling window while Health is on screen), with **Off** and every 10, 30, or 60 seconds: each
automatic read is scheduled one full interval after the previous read finishes,
never replaces another connection's greyed report (after a connection switch the
report stays greyed until **Refresh**, and the schedule resumes after it), and
reads immediately on return unless a failed read is still in backoff. **Save baseline** in the
Refresh menu keeps the reading on this computer for that server (`with_identity:
true`, up to 20 per server, requiring a verified identity that matches the server reached when saving), and **Compare with baseline** shows the
baseline value under each tile and marks changed statuses.
**Capture when…** in its toolbar watches the connection in use
for threads running at or above a number, a row lock wait older than a number
of seconds, or a statement running longer than that, checking every 5, 10, 30
or 60 seconds only while Health is on screen ("Watching: checked 4 times, last
at 14:02:10." in your local time, with **Stop**). A row lock wait counts from
InnoDB's own wait start; a statement waiting for a metadata or table lock is
caught by the statement condition instead. A condition it could not read is
named in the status line (**Not judged: …** or **Partly judged: …**), never
counted as zero. When a condition is met it reads the processlist, InnoDB
status and locks and saves them as a new investigation, with **Open
investigation**; it captures once each time a condition is met, by default
stops after the first capture, and finishes a capture already under way even
when you leave Health or switch connections. Opening the investigation shows
that **Evidence** as text with **Copy** and **Download**, with passwords in
the captured SQL replaced before it was saved.
See [Server health](QUERY_WORKSPACE.md#server-health).

**Error log** (Overview, after InnoDB) reads when it is opened on screen, once
per connection (Refresh and Refresh all read again). On MySQL it reads `performance_schema.error_log`: the newest
entries with their level (System, Error, Warning, Note), code and subsystem,
the most frequent codes under **By code**, and entries the server reloaded from
its previous run (hide them with **This run only**). **Time** limits the list
to All time, the last hour, 6 or 24 hours, or a custom range. The summary is
Watch when this run logged an Error-level event; it is never Problem, because
an Error-level line alone does not tell how serious it is (an InnoDB assertion
failure ends the server, so after the restart it is marked previous run). The summary says how much
of the server's 5 MB error log buffer is in use and how many older events
expired; when `log_error_verbosity` hides warnings or notes, a note says so.
MariaDB has no SQL access to its error log, so the view reads the log file
itself only when the server runs on this computer and the file is verifiably
that server's log (its latest startup line names the same version and port and
was written when this server started), from at most its last 1 MB. MySQL before
8.0.22 provides file-viewing guidance; myflames does not read its log file.
For a remote server it says where the log is and
how to read it on the server host. When the log cannot be read, the summary
says why once. File settings and guidance follow separately; commands have
their own copyable blocks, apart from the explanation. It is not part of
Full analysis yet.

**Full analysis** (the second tab in the Tuning place) composes a report from
Health, Waits, I/O, Parameters, InnoDB, Processlist, Instruments, server checks,
Tables and Indexes on request. Error log and Locks are outside its coverage.
It is the MySQLTuner-style checklist that `myflames report` prints, implemented
with myflames' own modules. **Run full analysis** takes about 10 to 20 seconds and
only reads. The view then shows one summary line, the server facts (version,
host, uptime, when it was taken and how long it took, the databases, how many
views were read), **Look at first**, the findings to look at as one-line rows
(status, title, the key number) in the report's own ranking, **Not judged**
for checks that could not run with the reason (a missing privilege, Performance
Schema off, a view that failed), the sections (memory, redo, connections,
Parameters, and so on) as a grouped list, and the findings with no rule behind a disclosure.
The inspector holds a finding's verdict, numbers, settings, rule, why it
matters, what to do (a statement and a my.cnf line to copy; myflames never runs
or applies them), the caveat, links to the view it came from (check links open Parameters at that check's first parameter), and the
statements behind the number with Explain and Run analysis. The coverage of
those statements and the checks left out are behind the notes button.
**Export** saves the report as HTML, Markdown, JSON, or text, generated from
the report JSON on the server, with a "Connection:" line naming the connection
it was read from (never a password). The report belongs to its connection: after a
switch it stays greyed with its server actions off (export still works). It is
never saved; the workspace only remembers that it ran.

**Tuning** gives a score from the checks the other views already judge
(Health, Parameters, Tables and the Full analysis), in five weighted
categories: Buffer pool and redo, Queries, Connections and threads, Tables and
caches, and Security. Category cards fit the available width without a blank
grid cell. A category measured on fewer than half its checks has a muted,
hatched bar and names its coverage, such as "100% of 1 check"; it also says
how many of its checks were measured. A measured check counts 100 when OK, 50 to watch and 0
for a problem. A check that was not measured shows **Not measured** with what
would measure it (for example "Needs Health or Full analysis"), and it never
counts: the summary says how many checks were measured. Until at least half
the checks, and one in each category, are measured, the score is provisional
and has no grade. Grades are A 90 and above, B 80 to 89, C 70 to 79, D 50 to
69 and F below 50, and a problem caps the grade at C. A line under the label
says why the grade is what it is ("Capped at C: 1 check has a problem (89 is in
the B range)" or "Score 84 is in the B range, 80 to 89"), and the ⓘ next to the
grade shows the scale, the cap and when there is no grade; the Tuning ⓘ
explains the score and the grade too. Select a check to see what was
measured and the numbers first, then what to do, the rule and why it matters.
Statements are for you to copy or open in the Query editor (SET PERSIST on
MySQL; SET GLOBAL and an option-file line on MariaDB); the page never runs
them. Encrypted connections is read from Parameters, which lists every global
variable. Only reports of the connection in use are scored; after a switch the
scorecard stays greyed, its note offers **Run full analysis** to load the
server in use, and its **Export** is off until then. **Scorecard | Full
analysis** is the segmented control in the header row. The **Full analysis** tab shows the report described
above and uses its **Refresh** to run it again; the Scorecard toolbar owns
**Run full analysis**. **Export** saves the scorecard as HTML, Markdown or JSON.

**Parameters** (Setup, formerly Configuration) is modelled on an AWS RDS
parameter group. It reads once on open for each connection (Refresh retries) and
shows one table of every global parameter the server reports: Name, Value,
Allowed values, Apply type (Dynamic, Static, Unknown), Source, Data type, and
Description. The view toolbar stays pinned under the top bar with **Filter
parameters** (search over name, value and description) and a **Show** filter:
All, Modified (source is not the compiled default), Dynamic, Static,
Recommendations (parameters covered by a Watch or Problem check), and Pending
while editing. The sidebar badge still shows the worst status from the checks,
and InnoDB's link reads **Open Parameters**.

**Edit** turns the Value column into inline controls (switch, list or text box
from the parameter's type) for parameters that can change: MySQL dynamic
(applies now) and MySQL static that can be persisted (applies after the next
restart through `SET PERSIST_ONLY`), or MariaDB dynamic only. MariaDB static
parameters are option-file only, values the server sets itself (for example
`version`, `hostname` or `have_*`) say **Set by the server** and cannot change,
and parameters whose apply type is unknown are not editable in the table. A
set-typed parameter such as `sql_mode` edits as a list of the allowed members
to tick; the value is the ticked members joined with commas in the catalog's
order, and **Edit as text** keeps the raw field.
**Preview changes** lists Name, Current value, New value, Apply type and When it
applies; a parameter that also has a session value applies now to new sessions,
while sessions already connected keep their own value. **Review and apply** opens one review
for up to 25 changes: the server plans each change (exact statement, problems,
privileges, effect, undo), MySQL dynamic rows use **Keep after restart**
(checked: `SET PERSIST`, unchecked: `SET GLOBAL`), static rows use `SET
PERSIST_ONLY`, and MariaDB rows use `SET GLOBAL` and show the option file line
to copy. Confirm applies the changes one at a time (each re-read and checked
first), stops at the first change that is not confirmed, and shows a result per
row. A change whose reply does not arrive shows **Outcome unknown**: it may have
run, so check the value before trying again. Before sending, Cancel or Escape
records a declined attempt for every planned row in the [Audit log](#audit-log).
While applying, the confirm button shows **Applying…** and the sheet counts
the seconds. After ten seconds without a reply, **Close** keeps the request
running and shows its progress in Parameters until the outcome arrives. This
also applies to single-parameter Change and Reset persisted value. Further
changes wait for that reply; closing the sheet never resends a change.

Selecting a parameter opens the side inspector and keeps the table on screen
in a wide window. Below about 1,040 pixels, where the inspector is a sheet, the
table shows Name, Value, Apply type and Source; Allowed values (still in the
inspector), Data type and Description can be turned back on from **Columns**.
Value and Default use the same formatting, so equal values
look equal. The inspector has one compact section per parameter: a one-sentence
description (the server's own text on MySQL 9.0 and later and on MariaDB; on
MySQL 8.0 and 8.4 the same help text, taken from the MySQL source because
those servers do not report it; otherwise the opening sentence of a check
about that parameter), a
**Documentation** link to its entry in the official manual, and **Current
value** (Value, Default, Source with its path and who set it when, Changes:
Now, Now for new sessions, After a restart, Option file then a restart, or Set
by the server; Allowed values; Persisted on MySQL; and Checked, the checks that
cover it when none flags it). Only when a check flags the parameter (Watch or
Problem) does a **Recommendation** follow: the most severe check, its reason,
the suggested statement and option file line to copy, and its caveat in one
line; any other check that flags it is named once. The link opens in your
browser: the MySQL Reference Manual for the server's version (8.1 to 8.3 use
the 8.4 manual, 9.0 to 9.6 the 9.7 manual; a variable that manual leaves out
links to the nearest one that has it, which the link's label names), MariaDB's
documentation, and Percona's manuals (Percona Server and Percona XtraDB
Cluster) for the variables a Percona server adds. The locations come from the
generated `myflames/sysvar_docs.py`; nothing is fetched to build them. A row's
context menu also has **Open documentation**. Its actions, in the bar under the
name, are **Stage value for review** (turns on editing in the table, filtered
to that parameter, with its value control focused; Review and apply sends it
later), **Reset to default** (stages `DEFAULT` for MySQL numeric and boolean
dynamic parameters), **Change this parameter…** (the single-parameter sheet with every mode,
including **This session only**, which opens the planned `SET SESSION`
statement in the Query editor without running it or recording a declined audit
on cancel), and **Reset persisted value…** (MySQL, when persisted). **Version
history** opens Versions > Parameter history for the parameter and the
connection's engine. The Change
sheet reads the variable and offers only what it allows: **Now only** (SET
GLOBAL, until a restart), **Now and after restart** (SET PERSIST, MySQL),
**After restart only** (SET PERSIST_ONLY, MySQL), **Session**, and **Remove the
persisted value** (RESET PERSIST); MariaDB shows the option file line to copy
instead of SET PERSIST. It shows the exact statement, the effect, the privileges,
the undo statement and the caveats; the connection's type decides whether it
asks (Production asks for every change, Development for SET GLOBAL and SET
PERSIST, Read-only refuses); right before sending it reads the variable again
and sends nothing if it changed; the answer shows the value before and after
(the server may round it). Permission
details appear inside the review dialog without an Unverified badge on the
buttons. The CLI `myflames config` is unchanged, and `config set --session`
still uses a one-off connection to test a value and explains this on stderr
(unless quiet). Parameter edits are off while the report is from another
connection.

**Compare** is the second tab of Parameters (**Parameters | Compare |
Versions** in the toolbar). It compares the global parameters of a **Source** and a **Target**
without changing either server: each is **This connection**, a saved
connection, a saved parameter snapshot, or **Latest snapshot of** a server
address. Choose them and press **Compare**; nothing is read until then, nor
after a connection switch (only the old result greys; Source, Target and
Compare stay ready). Compare is unavailable, with the reason, while both
sides are the same, and Target starts with the latest snapshot of the server
in use when there is one. **Swap** exchanges
the sides and **Refresh choices** reloads the saved connections and snapshots
from this computer. Once a comparison has run, Source and Target fold into one
bar (Source → Target, **Swap**, **Change…**, **Compare**) that keeps naming the
sides the result came from, with when each was read in its tooltip, until you
press Compare again. Swap or a new choice under Change… only changes the chosen
sides and reads nothing; a note says "The chosen sides changed (next: Source X
→ Target Y). Press Compare to update the differences." Change… opens the setup
above the results (with Manage snapshots and Refresh choices), and Escape or a
second click closes it while keeping them. A failed Compare keeps the previous
result and the setup as you left it. Differences lists each parameter as
**Different**, **Only in source**, **Only in target** or **Not compared**
(with the reason, such as a value the server truncated or a secret that is
never shown); these badges describe the comparison, not the server's health,
so none uses the OK, Watch or Problem colors: Different is tinted, Only in
source and Only in target are outlined, Not compared is muted and Identical is
faint. The list has **Filter
differences**, **Show identical**, and **Show server-specific settings** for
`server_id`, `hostname`, `port` and the like; the counts always cover every
row. Values compare by meaning where the type is known (ON equals 1, 128M
equals 134217728) and exactly otherwise, and the raw values and defaults are
shown as the server reported them; Different rows come first. Select a row for
its detail over the whole view, one Setting, Source and Target table as in
Schema versions, with **Back to differences**. Parameters stays loaded after
you first open it, so a comparison, a snapshot save under way and staged
edits survive a switch to another Server view. **Save snapshot…** in the Parameters
toolbar reads the connection in use and keeps its global parameters on this
computer, numbered per server address with an optional label ("Saved snapshot
#3 · db1:3306"); **Manage snapshots** lists them and deletes one after a
confirmation. The same comparison runs as `myflames config compare` and
`myflames config snapshot`.

**Versions** is the third tab of Parameters and answers from the release
catalogs shipped with myflames; nothing in it reads a server, so it has no
Refresh and Refresh all does not involve it. The catalogs cover every MySQL
GA, LTS and Innovation release from 8.0.11 and every stable MariaDB release
from 10.6 that could be started in a lab and read from the running server; a
release that could not be started is listed as skipped with the reason. It
has two modes. **Compare versions** picks an **Engine** (MySQL or MariaDB,
following the connection in use, with Percona counting as MySQL), a **From**
and a **To** release (grouped by series; From starts at the server's release
when cataloged, else the nearest earlier one, and To at the newest cataloged
release) with **Swap**, and lists each parameter added, removed, deprecated
or changed between them: the summary, a kind filter with counts (All, Added,
Removed, Deprecated, Default changed, Other), a name filter, and **Only
parameters set on** the connection when its Parameters report is loaded and
its engine matches (parameters whose value differs from the default). The
table shows Parameter, Change (comparison badges, never health colors), From,
To and **Since** (the release the To value holds from). A row's inspector
shows both releases' facts with the sentences for each change, and **Show
full history** switches to Parameter history for that name. **Parameter
history** picks an Engine and a Parameter from the catalog's names and shows
the facts at the newest release, then every release where the parameter
changed or was deprecated, grouped by series, oldest first, with Version,
Change, Before and After; an unknown name offers **Close names** as buttons.
The Parameters list's inspector opens this mode with **Version history**. A
default the server computes at startup reads **Computed at startup** (the lab
value on hover; Parameter history adds how the server sets it and the names
that are aliases of the parameter), a path that follows the build **Depends on the build**, an
unread default Unknown, and a parameter absent from a release Not present. An
unknown or host-dependent default, a value that names the release (version,
build_id) and a build path are never counted as a default change, and a
parameter that only some builds have (innodb_numa_interleave) is never shown
as added or removed; its history says why in one line. One-line notes say when a Percona connection uses the MySQL
catalog (variables Percona Server adds are not cataloged), when the connected
server is newer than the catalog, and when the From release was skipped or is
missing (naming the fallback used). The same answers run as `myflames config
versions list`, `config versions compare FROM TO` and `config versions
history NAME`.

**Replication** (a place of its own in the sidebar) lists each replication channel
(MySQL) or connection (MariaDB) of the connection in use: status, source,
I/O and SQL threads, lag, and the last error code. It reads when it is opened
on screen, once per connection (when it is opened after another connection was
chosen elsewhere it reads once for that one); **Refresh** reads again, and the Server
place's **Refresh all** feeds this place too, so opening Replication afterwards shows
that answer without another read. Choosing another connection while Replication stays
on screen greys the old report until **Refresh**. A server with no
asynchronous replication channel reads **Not a replica**, a calm state with
the reason, never Unknown: a Group Replication member (whose
group_replication_applier and group_replication_recovery channels belong to
the group) and a Galera node get **Open Cluster**, and the sidebar item is
greyed with "This connection is not a replica" while staying focusable.
Unknown means only that the status could not be read or was not allowed. The header switches between
**Topology**, **Channels** (**Connections** on MariaDB), **Lag**, **GTID** and
**Recovery**. Under the summary line four tiles show the replica state, the
channel count, the maximum applier lag (**Unknown** whenever any judged
channel's lag is unknown, so a measured 0 s beside a failed channel never reads
as caught up) and the GTID mode. In Topology each source shows its engine and
version beside its port, like the replica does. myflames reads it, when the view
reads, from the source's format event at the start of the replica's relay log
("as recorded in the relay log": the version that wrote the binary log the
replica was reading, so right after a source upgrade it can show the earlier
one; needs REPLICATION SLAVE on MySQL, SLAVE MONITOR on MariaDB). Only when the
relay log gives no version does it open a saved connection whose host and port
are the ones the replica reports (ignoring case; 127.0.0.1, localhost and ::1
count as one host only for a replica on this computer and only when one saved
connection matches), with that connection's own saved password, and it uses the
answer only when its server ID (and server UUID when the replica reports one)
is the source's and, for a source address on the replica's own computer
(localhost or any loopback address, such as 127.0.0.1, 127.0.0.2 or ::1), when
that server runs on the replica's own host. For such an address it opens a
saved connection only when the replica itself is on this computer (reached at a
loopback address and reporting this computer's host name), so a replica reached
through an SSH tunnel never logs in to a server on this computer. On
MariaDB only the server ID can be compared, and the version says "only its
server ID could be compared". A saved connection is opened at most once while
myflames runs, whether it answered or failed, until you edit it, also after its
result is no longer kept (the source then says "Not checked"), so a stale saved
password never sends a failed login on every read. One read of a saved
connection runs at a time, and after 1,024 attempts myflames opens no more until
it restarts. Each source's login
and read get 3 seconds, and all saved connections of one read 5 seconds,
counting the time spent looking up their saved passwords; nothing is sent once
that time is used, and the remaining sources say "Not checked" and get their
turn on the next Refresh. Otherwise it says **Version
unknown** with the reason and "Add a saved connection for host:port to show its
version." Where the version came from is in the tooltip, the connector's
details and the inspector's **Source version** row. Selecting a channel opens
the side inspector while keeping the list on screen in a wide window. Long
GTID sets wrap after commas and remain selectable. The inspector shows the threads, the last error once (code, time,
table, failing GTID, the server's text), the lag signals with their caveats
(Seconds_Behind_Source alone never says a replica is caught up), the
positions and GTID state in one list (GTID mode and Enforce GTID consistency
named in full), what recovery needs, and the raw channel status behind a
disclosure. Group Replication's own channels
are not listed (a note names them when the server also has an asynchronous
channel) and cannot be skipped here. A healthy channel shows no
recovery actions. When the applier stopped
on an error, the inspector offers two changes, each with its own confirmation
sheet that reads the failure again before it opens and shows the exact
statements, the privileges they need, and the drift warning (a skipped
transaction is missing on the replica):

- **Skip this transaction…** skips the one failed transaction (an empty GTID
  transaction, or the skip counter where that is the safe method) and starts
  the applier again. The answer appears above the table with what was checked
  after (whether the applier moved past it) and what to do next.
- **Skip until stopped…** skips later failures of the error codes you tick
  (none is ticked for you: 1062 duplicate key, 1032 row not found, 1452
  foreign key child row, and the error there now) until a number of skips or
  minutes you set, the first other error, or **Stop**. Each skip is planned
  again, checked, and recorded on its own. A running loop shows one line with
  what it skips, how many, and the last one, both in this view and above any
  other Server view, with **Stop**, which also works on a read-only
  connection (it ends a change you already confirmed). Loops live in the
  running myflames only: after myflames restarts the view says "Stopped when
  myflames restarted"; a reloaded page finds a loop still running in the same
  myflames; nothing ever starts a loop again by itself. Changing the
  connection's type or Read-only keeps the loop's status and Stop.

Opening a skip review shows **Checking…** while the failure is read again;
another click sends no second check, and the initiating button stays in place
and focused. Its review focuses **Cancel** first and returns focus to that
button when dismissed. After confirmation, the sheet shows
**Skipping…** or **Starting…**, with elapsed time. After ten seconds without
a reply, **Close** leaves the request running and shows its progress in
Replication. A lost write reply shows **Outcome unknown** and is never
resent automatically. Loop Stop shows **Stopping…** and, after ten seconds,
warns that the loop may still be running.

If the failure changed between the sheet and the click, or the sheet stayed
open for more than a minute, myflames reads it again, shows the new
statements, and sends nothing until you confirm again; the server checks it
once more right before sending. Cancel and Escape are recorded as declined in
the [Audit log](#audit-log).

**Cluster** (a place of its own, right after Replication) shows the cluster
the connection in use belongs to: a MariaDB Galera Cluster, a Galera Cluster
for MySQL, a Percona XtraDB Cluster, a MySQL InnoDB Cluster (also when you
connect to one of its read replicas), or Group Replication without InnoDB
Cluster metadata. Opening it only reads. It reads when it is opened on screen,
once per connection; **Refresh** reads again, and the Server place's
**Refresh all** reads it too. A member's inspector offers the changes that do
what they say on that server (see [Cluster changes](#cluster-changes)).

One scale for every kind: **OK** with quorum and every expected member present
and healthy; **Watch** while quorum holds but a member is missing (a Galera
node not in the component, an InnoDB Cluster member missing from the group or
unreachable), a member is in transition (Donor/Desynced, Joiner, Joined,
RECOVERING), or one more failure would stop the cluster; **Problem** without
quorum or outside a Primary component, for a member in error, or when the
member you are connected to is offline or in error. The same word reaches the
sidebar, `myflames cluster` and its `--fail-on`.

The summary line names the cluster and its state. When something is wrong the
findings come next, one line each (the state's shape and word, the title with
the member's name as a link, **Details** for the reason), then four tiles: the
component (Galera) or quorum, each with the failures it tolerates (a Galera
figure assumes every node has the same `pc.weight`); the nodes in the
component and how many are inspected here, or the members ONLINE out of the
group; this node or member; and flow control (Galera) or the mode. The header
switches between **Topology**, **Members** (**Nodes** on Galera) and, for
InnoDB Cluster, **Routers**:

- **Topology** is a still picture: one card per member with its role
  (Primary or Secondary for Group Replication), its name and address, engine
  and version, its state as a word beside the shared status dot, its queues (or this
  node's receive and send queues and flow control on Galera) as text, and
  **This connection** on the member you are connected to, each joined to one
  line for the group, labelled with the component or the group mode, quorum and
  the failures it tolerates. A node that `wsrep_cluster_address` names but
  that is not in the component is a dashed placeholder when it can be named.
  Read replicas hang below the group on dashed links (they replicate
  asynchronously and are not group members), and routers below them. A member
  that is unreachable, missing from the group, offline or in error has a
  dashed edge, a cut link and its shape; a member to watch has a heavier left
  edge. In a narrow window the cards wrap inside a frame that stands for the
  group. The arrow keys move between cards and Enter opens the inspector.
- **Members** (**Nodes**) is a table of every member with its state, role,
  address, version and counters; **Routers** lists the MySQL Routers registered
  in the InnoDB Cluster metadata with their ports and last check-in (its age
  and UTC time; shown, not judged: MySQL Router writes it periodically, by
  default every few seconds and never by default in a ClusterSet, or as its
  `stats_updates_frequency` option sets). Members (Nodes on Galera and Percona
  XtraDB Cluster) and Routers each keep their own column widths and hidden
  columns, saved with the workspace.

Members are shown as the connected server sees them. Galera exposes only its
own node's state, so the other nodes read **In the component · not read here**
on a compact line. Their state and queues are read on that node; the notes
explain this once. **Use &lt;connection&gt;** appears when a saved connection
matches the node and switches to it so you can inspect that node. Percona
XtraDB Cluster names every node's state, and Group Replication every member's
state and role. Connected to a member that is offline or in error, the place
says so, claims no group size, lists the other instances as **Not visible
here**, and marks the routers' check-in as this member's copy, which may be out
of date: connect to an ONLINE member to judge the cluster. A server that is
not part of a cluster shows a calm **Not part of a cluster** with the reason;
from then on the sidebar item looks disabled (faint text and icon, tooltip
"This connection is not part of a cluster"), also while it is open, but stays
focusable and opens the place. Before the first read, and for another
connection, it looks like any other place. On a ProxySQL Admin connection,
Cluster shows **Not part of a cluster**, the sentence "This connection is a
ProxySQL Admin interface." and **Open ProxySQL**. A password hint appears only
for an Access denied response. Choosing another connection while
Cluster stays on screen greys the old report until **Refresh**, and a failed
read keeps the last report greyed with the time it was read and one sentence
saying why; either way its member is marked **Read here**, never **This
connection**. Terms such as Primary component, flow control, failures
tolerated, queues and the router ports explain themselves on hover. The command
line twin is `myflames cluster` (it only reads).

#### Cluster changes

A member's inspector shows the changes it offers as buttons, and only these:

- **Make primary…** on an ONLINE secondary of a single-primary group, while no
  member is recovering or unreachable: `group_replication_set_as_primary` with
  the member's UUID. The current primary becomes a read-only secondary once its
  running transactions finish. In an InnoDB ClusterSet replica cluster the
  server refuses; use MySQL Shell's `setPrimaryInstance` there.
- **Stop Group Replication…** on the member you are connected to: it leaves the
  group and becomes read-only. You type its name when it is the primary (the
  group elects another) or the last ONLINE member (the group stops and needs a
  bootstrap).
- **Start Group Replication…** on the member you are connected to, only while it
  is OFFLINE and `group_replication_bootstrap_group` is OFF. Recovery may
  replace its data with a clone; the review says so. A member in ERROR is
  offered Stop first: fix the cause in the error log, then start it.
- **Switch the group to multi-primary…** or **single-primary…** for Group
  Replication without InnoDB Cluster metadata, with the member's name typed.
  For InnoDB Cluster use MySQL Shell, which also sets `auto_increment` and the
  metadata.
- On MariaDB Galera, on the node you are connected to: **Desync this node…**
  (`wsrep_desync = ON`: out of flow control, for example for a backup; it keeps
  applying writes and its reads can be stale) and **Resync this node…**
  (`OFF`, which waits until it caught up), and **Bootstrap a Primary
  component…** only when its component is non-Primary and it is connected and
  not joining. Bootstrap makes this node and the nodes it reaches a Primary
  component: run it on one node only, the one with the highest
  `wsrep_last_committed`, or two components take writes. You type the node's
  name.

Percona XtraDB Cluster and Galera Cluster for MySQL offer no change yet, and
`SET GLOBAL wsrep_on` is never offered. Every change works like ProxySQL's
(below): the review, the policy, the typed name, the check right before
sending, the result panel and the Audit log.

**ProxySQL** (a place of its own, right after Cluster) reads the ProxySQL
Admin interface the connection in use reaches (port 6032 by default; save a
connection to it like any other, with an admin account: ProxySQL's default
`admin` user can only log in from the ProxySQL host itself). It reads when it is
opened on screen, once per connection; **Refresh** reads again. The summary
line names the version, the uptime, the backends and hostgroups and how many
backends are ONLINE; problems come first (a hostgroup in use with no ONLINE
server, an active rule ProxySQL runs that sends to a hostgroup with no server;
when what runs cannot be read the status is unknown, with the reason), then
tiles and four tabs:

- **Servers**: each backend as ProxySQL runs it (runtime), with its hostgroup,
  its role in a Galera, Group Replication, replication or Aurora monitor's
  hostgroups, status (with the shared status dot), weight, connections in use and free, connection errors
  since ProxySQL started, queries and ping. A writer the Galera monitor keeps
  above `max_writers` reads **SHUNNED (standby writer)**: it is ONLINE in the
  backup writer hostgroup. Selecting a server, hostgroup, query rule or digest
  opens the side inspector while keeping the table on screen in a wide window
  (a bottom sheet below 1,100 px), and Escape returns focus to the row. The
  server inspector shows the runtime row, the connection pool, the configuration
  row it comes from (a monitor may run it in another of its hostgroups) and the
  monitor.
- **Hostgroups**: each hostgroup in runtime or in a monitor with its role and
  how many servers are in each status. The tab and the Hostgroups tile count
  every listed hostgroup, and the tile sub-line says how many have servers and
  how many are in monitors (for example "5 with servers · 8 in monitors"); an
  **Empty** hostgroup has a neutral status dot rather than OK. The Client
  connections tile sub-line reads "connected now".
- **Query rules**: each rule with what it matches, its destination hostgroup,
  Apply, its hits (**No hits** when no query matched it since the rules were
  last loaded to runtime) and **Digests**: how many of the 100 digests with the
  most total time it applies to, plus those it may apply to. The inspector lists
  those digests (each opens in Query digests), **Show its digests in Query
  digests**, and every column set.
- **Query digests**: reads on its own while the tab is on screen, one bounded
  statement in Admin per choice. Choose the order (**Slowest**: average time;
  **Most time**: total time; **Most frequent**: executions; **Highest rate**:
  executions per second between a digest's first and last execution, at least
  one second; **Most rows sent**, ProxySQL 2.0.7 and later), **Top** 10, 25, 50
  or 100, and filter by hostgroup, schema, user or digest text (typing waits for
  a pause before it reads). A narrow window offers the order as a menu. A
  digest or rule filter shows as a chip with **Remove**. The line above the
  table says what is listed, out of how many digests and executions, since
  the first execution recorded. Each digest shows **Ran on** (the hostgroup it
  ran on) and **Rules send to** (where the rules send it): the hostgroup and the
  rule, fast routing or the user's default hostgroup when they agree;
  **Mismatch** when it ran elsewhere; **Maybe rule N** when it ran on the
  hostgroup of a rule that tests what a digest does not keep; **Query cache**
  (hostgroup -1); **Answered by ProxySQL** for statements ProxySQL replies to
  itself, such as `select @@version_comment limit 1`. The inspector's
  **Routing** explains it, names the rules on the way (each opens in Query
  rules) and, for a mismatch, the reasons ProxySQL can choose another hostgroup.

Routing is evaluated by myflames, not by ProxySQL: the active rules of
`runtime_mysql_query_rules` in rule_id order, following flagIN, flagOUT and
apply (and `mysql-query_processor_iterations`), then
`mysql_query_rules_fast_routing`, then the frontend user's default hostgroup.
Patterns are tested with Python's `re`, while ProxySQL uses PCRE or RE2
(`mysql-query_processor_regex`), so a pattern can match differently;
`match_pattern` tests the query text, which a digest does not keep (comments
are removed and values are `?`), so it is tested on the digest text and marked
approximate; a rule on proxy_addr, proxy_port or a client address the digest
did not record is unknown. Patterns are tested in a separate process, two
seconds at most each, so a pattern that never finishes leaves that rule unknown
instead of holding up the app. When the rules can go on further than myflames
follows (many restarts or flagOUT choices), or fast routing could not be read,
a different hostgroup is not judged. The ⓘ beside Routing says the same.

When a runtime variable needed by the rule walk cannot be read, affected digests show **Not judged** and name the variable. Digest hash filters accept lowercase and mixed-case hexadecimal digits. Schema, user and client names containing separators keep distinct row selections and rule links. If you change connections while a reviewed Cluster or ProxySQL action runs, its sheet closes when it finishes; the Audit log keeps the attempt, and no follow-up change is offered on the replacement connection.

After a reviewed change, Query digests drops its old routing and reads again
when visible. A pending read from before the change cannot restore the old
rules. An unknown write outcome stays unknown and is never sent again.

ProxySQL keeps three copies of its configuration: memory (what Admin edits),
runtime (what it uses) and disk (what it loads at its next start). The
**Configuration** line above Servers and Query rules says whether memory
matches runtime and disk, with **Load … to runtime…** and **Save … to disk…**
(when memory already matches runtime or disk, the matching button is a secondary
ghost control with "Nothing to load: memory matches runtime" or "Nothing to
save: disk matches memory" in its tooltip, while staying usable).
When a copy of a table could not be read (anything but a table this ProxySQL
version does not have), what loading or saving would change is unknown, so
that module offers no change; the notes name the table.
A server's inspector offers **Set ONLINE…**, **Set OFFLINE_SOFT…** (no new
traffic; running queries and transactions finish), **Set OFFLINE_HARD…**
(ProxySQL stops using it at once), **Change weight…** and, for a server in no
monitor's hostgroups, **Move to hostgroup…** (a number first, then the
review: a server moved out of a monitor's hostgroups stays in them in
runtime); a rule's inspector offers
**Enable rule…** or **Disable rule…**. Each one changes the configuration in
memory and loads it to runtime in one step, and is refused while other changes
wait in memory (load or undo them first). It is not saved to disk: the result
offers **Save servers to disk…** (or query rules) as a separate change, which
lists every difference between memory and disk before you confirm. OFFLINE_SOFT
is not offered for a server the Galera monitor manages, because the monitor puts
it back in traffic within seconds; use OFFLINE_HARD or weight 0.

Every change shows its review first: the statement verb, a question naming the
target, the connection with its address and type, what changes, the undo (or
that it cannot be undone), warnings, and the statements exactly as they will be
sent. Production and Development connections both ask (these are risky
changes); a Read-only connection refuses and opens no review. A destructive
change also needs the target's name typed (OFFLINE_HARD, the last ONLINE server
of a hostgroup, stopping the primary or the last member, a mode switch,
bootstrapping a Primary component). Right before sending, myflames reads the
target again and sends nothing if it changed. After you confirm, the same sheet
follows the send: the button reads **Sending…** and cannot be pressed twice,
Cancel and Escape wait for ten seconds; then Close or Escape hides the sheet
while the request continues under the toolbar, including while the member or
server inspector is open. A line names the step (reading the target again; sent
and waiting for the server's answer, with the seconds waited and the limit;
reading the result back), and each statement shows **Waiting**, **Sent**,
**Done**, **Failed**, **Not run**, **Not sent** or **Outcome unknown**; a
statement is Done only once the server answered. Then the sheet shows the
outcome, **What changed** (before and now, read again after the write), that
the attempt is recorded with **Open the Audit log** (which opens it at the
record), the undo when it partly ran or its outcome is unknown, **What to check**
for an unknown outcome, and the next change it offers (Save … to disk…) beside
**Close**. Close leaves the result as a panel under the toolbar: **Done** with
what runs now, **Failed**, **Partly applied** (which statements ran), or
**Outcome unknown** when no reply arrived or the client ended without the
server's answer, which is never sent again. Cancel and Escape are recorded as declined, and every attempt is in
the [Audit log](#audit-log). Another connection's report is greyed and offers no
change. Once a read says the connection is not a ProxySQL Admin interface, the
sidebar item looks disabled for that connection only.

**Readyset** (a place of its own, right after ProxySQL) reads the Readyset SQL
adapter (port 3307 by default) or Admin SQL listener (port 6036) the connection
in use reaches. It reads when opened on screen, once per connection; **Refresh**
reads again. Tabs cover **Topology** (linking ProxySQL frontends, Readyset and
upstream MySQL or MariaDB standalone servers, replicas and clusters when saved
connections match), **Caches** (shallow and deep caches with **Create cache…**,
**Export cache DDL**, **Flush cache…** and **Drop cache…**), **Proxied queries**
(observed queries with one-click **Create cache…** prefill and **Drop all
proxied queries…**), **Tables** (base-table snapshot status, **Re-snapshot
table…**, **Add tables…** and replication controls), **Dynamic parameters**
(log level, eviction memory limit, eviction period, shallow cache allowlists,
maintenance mode, **Flush privileges…** and Admin-port **Shutdown…**), and
**Performance** (cached versus upstream share, latency percentiles and shallow
hit rate). **Create cache…** runs a read-only `EXPLAIN CREATE CACHE` precheck
and warns when a query ID or cache name already exists. Every change follows the
same reviewed write sheet, connection-type policy and [Audit log](#audit-log)
rules as Cluster and ProxySQL; once a read says the connection is not a
Readyset instance, the sidebar item looks disabled for that connection only.

**Maintenance** (Setup) has five tasks in a segmented control. **Tools**
lists the MySQL and MariaDB tools on this computer (mysqlsh, mysqldump,
mariadb-dump, and the rest) with their versions and what they offer for the
connection in use; Maintenance finds them by itself when it opens (local
probes only, reused for 60 seconds), and **Detect again** looks again. Each
tool links to its official download page. Tool discovery stays offline until
you choose **Check for updates**, which asks vendor release metadata; newer
versions appear beside the installed version. Client tools are compared within their installed series, while MySQL Shell uses its latest GA release. Checks can be cached for an hour; failed checks are cached for a minute and remain unknown. No installer runs automatically. **Upgrade check** runs MySQL Shell's
upgrade checker for a target version you choose, or a **Partial check
(native subset)** without it, labeled as such, which never says an upgrade is
safe; findings are rows (Error, Warning, Notice, Passed, Not checked, Check
failed) with the objects and documentation in the inspector, and **Export
JSON**. MariaDB gets the mariadb-upgrade steps to run on the server host.
**Dump**, **Restore** and **Clone** each have a short form and one button
named for the task, **Start dump…**, **Start restore…** or **Start clone…**,
which first runs the preflight (it only reads; the button reads
**Checking…** meanwhile). Restore's **Default database** offers **As in
the dump**, the server's databases, or **New database…** for a name to type;
Clone's **Fill from saved connection** copies a saved connection's host, port,
user and TLS settings into the donor fields (never its password). The
preflight only reads: failed checks are listed under the button with their
reasons and a **Next step** (**Choose another folder…**, **Review fields**,
or **Remove <table> from the selection**), the passed checks folded below
them, and nothing can start. After you change a field, an earlier failure is
marked as coming from the last preflight and its commands are hidden. A clean
preflight opens the confirmation with the target, the exact commands, the
warnings, and the checks; a dump's confirmation names its contents and puts
its warnings first, and a restore that replaces the mysql system schema
(accounts, passwords and grants) asks for a separate acknowledgement checkbox
as well as the typed target. A dump goes into
a new folder on the computer running myflames. **Output folder** is filled
in for you: `<database or connection>-YYYY-MM-DD` in your home folder, or
beside the last dump, with `-2`, `-3` added when the name is taken; a path you
type is never replaced. **Browse…** chooses the folder it goes in, through
the system folder dialog on macOS or Windows (Linux uses Zenity when
available). Cancelling leaves the path unchanged, and choosing a folder
starts no backup. The folder must not exist yet or be empty (a folder with files in it is refused at the preflight); restore and clone also ask you to
type the target. The confirmation goes stale after a minute (run the preflight
again), and myflames repeats the preflight right before starting. Starting
brings the job's card into view. A running job shows a progress bar, the step
it is on, the elapsed time, the last output line and **Cancel**. MySQL Shell
reports its own percent; `mysqldump` and `mariadb-dump` print nothing, so for
them the bar is an estimate ("≈46%") from the bytes written to `dump.sql`
beside the server's table sizes, with the size written and the rate. The same
summary sits next to the disabled Start button, with **Show progress** to jump
to the card. Finished jobs show the verification (passed, failed, or not
checked, with the reason) and the dump manifest; a completed dump has **Open
in Finder** (**Open in File Explorer** on Windows), and on macOS a dump you
started opens its folder by itself when it completes. Jobs belong to the myflames process and to the connection
they ran on: after a switch they stay greyed until you switch back, a reloaded
page finds them again, and a Start whose reply was lost is found in the job
list instead of being sent twice. A running job is also named above any other
Server view with **Open Maintenance**. The Dump form puts each label left of
its control (rows stack below 650 px): **Scope** (Whole instance or Databases),
**Databases**, and once exactly one database is chosen, **Tables**: its tables
are read then (name, engine, the server's estimated rows and size; 200 at a
time with **Load more tables**) into a searchable list whose default is **All
tables**. Ticked tables make a table dump of exactly those names; a table that
a **Reload tables** no longer shows blocks the preflight until you remove it
(**Remove <table> from the selection**) or choose again, and the preflight
itself fails for a name the database does not have. A dump
uses MySQL Shell when it is offered, or the engine's SQL dump tool when you
choose it (the Tool list names it). **Options for** the tool are one form in
sections, with a line under the heading summarizing what differs from the
tool's defaults: **Performance** (MySQL Shell: Threads, Chunking, Chunk size,
Rate limit per thread), **Contents** (Include Triggers, Stored routines and
Events; a MySQL Shell table dump has no routines or events; **Structure only
(no rows)** dumps the definitions without rows: MySQL Shell `ddlOnly`,
`mysqldump` and `mariadb-dump` `--no-data`; the plan and the review sheet say
"Structure only", and a restore of such a dump compares the objects but skips
the row counts, saying so), **SQL output** (SQL
dump tools: binary values as hexadecimal, DROP TABLE before each CREATE TABLE)
and **Compression** (MySQL Shell: Method and Level, the level needing Shell 8.4
or newer; SQL dump tools: compress the traffic from the server, which leaves
the backup file uncompressed). One line says backup files are not encrypted,
with **Why**: connection compression and TLS do not encrypt local backup files;
use an encrypted destination or encrypt the completed backup separately. The
exact options appear in the preflight and manifest. The donor password for a clone stays in the page and is
never saved, and a clone always requires TLS to the donor (the donor address
can differ when the recipient reaches it by another name). Clone is MySQL
only; MariaDB is pointed to mariadb-backup on the server host.

**Show statements** links a server number to the statements behind it.
Health's "Joins without an index", "Full scans of the first table", and "Rows
read by full scans", and a table in Indexes > Full scans, have it in their
inspector: the five heaviest Performance Schema digests with that counter
above zero (for a table, those whose text names it, labeled "text match, not
proof"), each with Explain and Run analysis (which asks first, as in Top
statements), and **Open in Top statements** for the whole list. Top
statements and I/O choose their **Schema** from the databases of the
connection in use (All schemas first, system databases grouped apart); when
the list cannot be read the field becomes a text box and its ⓘ says why. Top
statements ranks by joins without an index, full scans of the first table, or
reads without an index, and a table link adds a removable "Table:
schema.table" chip. Digest counters run since the server started or the
digest table was truncated, not over Health's window; the notes say so, with
the uptime, the oldest first-seen time, the overflow row, and lost digests.

Below about 1,040 pixels of width the inspector becomes a sheet that slides up
from the bottom; below about 720 pixels the list of views becomes a screen of
its own and choosing a view pushes it on screen, with **Server** (or
**Workload**) to go back. A view already on screen stays there when the window
narrows.

Under the places, **Open plans** lists every open plan with its time (or
**Estimated plan**), and **Baseline** on the plan others are compared against
when two or more are open. Hover or focus a row to remove it. Only the place on
screen is highlighted; an open plan is highlighted while Plans shows it.

The footer holds one status: **All changes saved**, **Saving…**, or **Not
saved:** with the reason. **Settings**, next to it, opens a compact menu (Escape
or a click outside closes it) with the appearance (System, Light, Dark),
**Restore workspace on startup** (its hint: off clears the saved workspace, and
passwords are never saved in it),
**Show MySQL and MariaDB news on Home** (on by default; off hides News and stops
every news read; its hint says the headlines come from Planet MySQL and the
MariaDB Foundation, read over HTTPS while Home is open), the **Audit log** (see
[Audit log](#audit-log)), and **About myflames**, a window
with the version, the license, one privacy line (myflames contacts only your
database servers, official release pages when it checks for updates (at start
and once a day while that setting is on, or when you ask),
and news feeds when shown and turned on), and the credits (Brendan Gregg's
FlameGraph and Tanel Poder's SQL Plan FlameGraphs). Close is its default button, and in a
short window its text scrolls while Close stays in view. The
top bar names the place once (Server and Workload name the view in the view
header, Plans names the plan in the plan header, and Replication, Cluster,
Tuning, Query digest and Database diagram, one view each, name the place in their view header only) and has one sidebar
toggle. With plans open it also shows **Save investigation** with **Unsaved
changes** or **Investigation saved**. The top bar holds no status badge and no
privileges button: on the places that send to a connection it shows the sidebar
toggle, **Back** and **Forward** (**⌘[** and **⌘]** on macOS, **Alt+Left** and
**Alt+Right** elsewhere; up to 100 entries across places, Server and Workload
views, query tabs, and open plans, never switching the connection in use), a
**Back to &lt;place&gt;** chip after you follow a cross-link from another view,
the place, the connection picker, then on Server and Workload **Refresh
all**, an ⓘ about the place and the control that docks or floats the list of
views (a **Views** menu in a narrow window), and **Search…**.

The connection picker's menu ends with **New connection…**, **Manage
connections…** and **Account and privileges…**. New connection… opens Home with
the Add connection form ready (the command palette's **New connection** does the
same). Account and privileges opens a sheet for the connection in use:
the account new sessions connect as, the note that an editor tab can have
different active roles, the active roles, the grants, the denied and
unverified actions with their reasons, and **Refresh privileges**. Close is its
default button, Escape closes it, and focus returns to the picker. The command
palette opens the same sheet as **Account and privileges** (keywords include
privileges, grants and roles) from any place. Greyed actions keep their reasons
where they appear, in their tooltips, descriptions and review dialogs. The sidebar keeps the same width across places at a given window size.
The light appearance uses a white window, like the dark one uses its content
color: gray is for the sidebar and side panes only.

The command palette (⌘K or **Search…** in the top bar) starts with a **Recent**
group of up to five recent destinations when the search field is empty, then
lists the places in the
sidebar's order, what belongs to one of them (Full analysis under Tuning,
**Compare** under Plans), the Server and Workload views, and actions such as
Import plan, the Audit log, and the Query editor's **Rename query tab** and
**Format SQL** (they open the Query editor and act on its current tab).
When you type, commands whose name contains
every word come first, then those that match only a keyword or their group,
each in that same order: **innodb** opens the InnoDB view, not the Cluster
place, and **health** opens the Health view, not Tuning. With a connection in
use and 2 to 128 characters typed, the palette also searches database objects on
that server after a short pause (`POST /api/live/object-search`, up to 50
tables, views, procedures, functions, triggers and events across visible schemas),
with matching database names from the cached schema list. Choosing a database,
table or view reveals it in the Query editor's Schemas panel; choosing a routine,
trigger or event opens its `SHOW CREATE` in a new query tab, selected and not run.
Object search and schema object folders preserve names containing control
characters without losing their exact identity.

Use the Up and Down arrow keys to move between places, and Home or End to jump
to the first or last. From 651 to 900 px the sidebar becomes a **48 px icon
rail**. Place names remain in tooltips and accessible names; status markers
remain visible. The sidebar toggle opens the full list over the content, and
Escape closes it and returns focus to the toggle. An **Open plans (N)** rail
button opens a list of the open plans. At 650 px and below the sidebar becomes a compact bar with
the status, Settings, and a **Menu** button that opens the places and open plans
in a drawer; Escape closes it.

### Memory

**Workload > Resources > Memory** reads tracked allocations by category, session
and account over a sampling window. Values use binary units; Numbers and Server context in the inspector show the exact bytes beside each value. General rules of thumb live
once in the notes (ⓘ); a finding about a row has a status pill on the row and a
reason in its inspector. **Sum of highs** and **Share (%)** have their full names
in tooltips; the inspector keeps **Sum of event highs**, or **Session high** for
MariaDB's PROCESSLIST counters. These highs are upper bounds, not a recorded
server peak. Missing total highs read "sum of highs not collected". When no
categories were collected but sessions were, the view opens on **By session**.
Tracked allocations are not process RSS or machine RAM; collection gaps remain
visible instead of reading as zero. Refresh reads again; the view does not poll.

### The File transfer place

**File transfer** moves files between this computer and your servers over SSH/SFTP,
in the spirit of FileZilla. It is a place of its own in the sidebar (after
Tuning) and does not use the database connection in use: it works with
**SSH connections**, saved objects of their own.

**SSH connections** are listed on the left; **New** opens **New SSH
connection** (the empty list offers it too). **Start from** fills Host, Port
and User from a saved database connection (its SSH tunnel, or else its own
host) or a `~/.ssh/config` alias; pasting `user@host:port` or
`sftp://user@host:port/path` into Host fills User, Port and the default
remote folder as well, and Name follows `user@host` until you type one.
**Authentication** is **SSH agent or keys** (your agent and `~/.ssh/config`),
**Key file** (a pick list of the keys in `~/.ssh`, `.pem` files included,
or a typed path, with an optional passphrase) or **Password**. A password or
passphrase you save shows **Saved securely** with **Replace** and **Forget**;
it is kept in the platform credential store, bound to the host, user, port,
sign-in method and key file, and changing any of those asks for it again.
**Default remote folder** and **Default local folder** set where the panes
open. **Test connection** signs in without saving and says how long it took
and the server's home folder. On Windows, password and passphrase sign-in are
not available yet.

The first connection to a server shows **Trust <host>?** with the server's
host key fingerprints (type, size, SHA-256, with **Copy**) to compare with
the ones your administrator gives you; **Trust and connect** keeps the key in
myflames' own `ssh_known_hosts` file (never `~/.ssh/known_hosts`) and signs in
again. A server whose key differs from the one already trusted, in myflames'
file or in your own known_hosts, is refused with the file and line of the
trusted key; nothing offers to trust it.

The browser shows **This computer** on the left and the server on the right.
Each pane has Back and Forward, Up, Home, Refresh and New folder, a path bar of
breadcrumbs (click a folder; **Edit path** or ⌘L / Ctrl+L to type one;
**Browse…** on This computer opens the system folder dialog), a **Filter**,
and sortable columns (Name, Size, Modified, and Permissions on the server).
Folders come first; **Show hidden files** reveals names that start with a
dot; symbolic links carry a link badge and open when they point to a folder.
Select like the Finder: click, ⌘ or Ctrl to add, Shift for a range, ⌘A or
Ctrl+A for all; arrows move, Enter or a double-click opens a folder or
transfers a file to the other pane, Backspace goes up, F2 renames and Delete
removes on the server. Each row's **⋯** button and a right-click open the
same menu: Upload or Download, Open, Rename…, New folder…, Delete…, Copy
path and Refresh. The footer says how many items there are and what is
selected, with **Upload →** or **← Download**. Drag rows to the other pane,
or files and folders from the Finder or Explorer onto the server pane, to
transfer them. A slow listing counts its seconds and offers **Stop**; an
error stays in its pane with **Try again**.

Transfers handle folders as well as files. When names already exist at the
destination, the sheet lists them with both sizes and dates and offers
**Skip existing** or **Overwrite** (existing folders are merged; the choice
applies to the files inside); overwriting a server file removes the old copy
first and cannot bring it back if the upload stops. Symbolic links and special
files inside folders are skipped, never followed, and a transfer stops before
writing anything if it has more than 10000 files or 64 folder levels. The
**Transfers** panel lists each job for **This connection** or **All
connections** with progress, files, rate and time left, **Cancel** while it
runs and **Retry** for failed or cancelled files; a job that could not sign
in offers **Edit connection** or **Trust host…**. **Clear finished** keeps
failed and cancelled jobs. Downloads are written as `.myflames-part` files
and renamed only when complete. File transfer is not a SQL feature: the connection
type and read-only switch do not apply and transfers are not in the
[Audit log](#audit-log). On Windows, uploads and downloads are not available
yet.

### Window status bar

Across Plans and the places that use a connection, one
window status bar at the bottom of the viewport shows the state of what is on
screen. On the places that send to the connection in use it starts with that
connection's write safety, a dot plus words: **Production** · changes ask
first, **Read-only** · changes refused, **Development** · risky changes ask
first, or **Production assumed** · reading settings / settings not loaded (with
**Retry**). Its tooltip and accessible name are the full sentence ("Production:
every change on NAME asks first"). Every place with a window status bar shows
the database product and version when that place has read them. Unknown values
stay hidden. Then come
the connection, session state, the last run and refresh status. The Query
editor's transaction state stays in its toolbar, and the rows and time of a run
in its result tabs, so the footer does not repeat them. The footer stays one
line: connection names and other flexible text truncate before the safety
words, leaving the safety state readable. Home, Plans, Investigations, Learn and the Audit log show no safety state.

### Dialogs, menus, and focus management

Dialogs, confirmation sheets, popovers, and drawers use scoped focus trapping:
- When a modal surface opens, focus moves automatically to the first interactive
  element or designated control.
- Keyboard navigation (Tab and Shift+Tab) cycles strictly within the active
  surface, preventing focus from escaping to background elements.
- Menus focus their first item; ArrowUp/ArrowDown and Home/End move between
  items. Tuning and result tabs use ArrowLeft/ArrowRight and Home/End, with only
  the selected tab in the Tab order.
- Pressing Escape dismisses the active surface and restores focus to the control
  that opened it. Visible Esc hints use a keycap.
- Context menus on table rows (in Query editor results grids and schema trees) can
  be opened by right-clicking, by clicking the dedicated "More actions" button on
  the row (`aria-haspopup="menu"`), or by pressing Shift+F10 (or the ContextMenu
  key) on the selected row.
- In table data, Tab reaches cells and their native editors. Enter or F2 on a
  cell focuses its value editor (the type selector for NULL or Default).
  Shift+F10 or the ContextMenu key opens Cell actions from a cell or its editor;
  Revert change and Set to Default use the same menu keyboard controls.
- With the Server or Workload list of views floating, the view's title opens
  it (Enter, Space or Down) on the current view. Arrow keys, Home and End move
  between views; Enter or Space opens one. Escape returns to the title.
- Escape in a nested confirmation, including the Kill sessions review, dismisses that
  confirmation without closing its parent dialog.
- Replication connector details appear on hover or focus. The pointer can move
  onto the details, and Escape dismisses them. Their accessible descriptions
  remain available at narrow widths too.

### Responsive and phone layout

At 900 px and below, summary tiles form a compact strip, facts fold into
**Details**, and key/value lists stack each value below its label. In wider
windows the label column fits the longest label, up to 40% of the list width.
At the 720 px minimum window, Home keeps connection types readable as tags
(**Prod**, **Dev**, **Read-only**) beside the connection names.

On phone and narrow screens (below 720 px):
- Flame graphs, execution trees and bar charts are redrawn for the width of
  their pane. Execution trees and bar charts shorten operator names (the full
  name shows on hover and in the inspector) and keep their numbers; below
  about 480 px (360 px for a flame graph) they scroll horizontally at their
  full text size. Swipe sideways or focus the chart and use arrow keys.
- Toolbars and segmented controls wrap onto multiple lines without clipping
  buttons or dropping controls offscreen.
- The sidebar is a 48 px icon rail from 651 to 900 px, with the full list
  available as an overlay; at 650 px and below it uses the **Menu** drawer.
- At 1,100 px and below the connection pill drops the address and the type
  pills and keeps its dot, the name and a short type word (**Prod** or
  **Dev** in the type's color, outlined for a read-only connection; the full
  type stays in its accessible name and the window footer). Below 650 px the
  pill gives up width first (its dot, the type word and a few characters of
  the name stay, the rest ends in an ellipsis). In the Server and Workload top
  bar, which also holds the place's own actions, this starts below 800 px (the
  720 px window), keeping the connection name readable.
- The Server place presents the view list as a separate screen, pushing selected
  views into view with a button named after the place (**Server** or
  **Workload**) to return; a view shown before the window narrowed stays on
  screen. A view's controls move to rows under its title. The top bar names the place
  only (the view header names the view). In a narrow window (the 720 px minimum
  with the sidebar shown) the Server and Workload toolbar has a **Views** menu
  that switches views without going back to the list; in a wide window the same
  spot docks or floats the list of views.
- In a narrow window a plan's chart view, metric and branch become one view
  select and an **Options** menu, the Visual Explain legend a **Legend**
  popover, and Compare's four views one select, so the chart controls do not
  take extra rows.
- Inspectors become bottom sheets that slide up and take keyboard focus.
- In the Query editor, toolbars and parameter inputs wrap, while the secondary
  editor status bar is hidden to avoid duplicate information above the responsive
  window status bar. The editor and its results stack and the page scrolls, so
  the last result row clears the window footer.

On wide windows the Query editor fills the window between the top bar and the
footer exactly, and the Server, Workload and Tuning views are at least that
tall, so the page never scrolls by a few pixels and the first row (the query
tabs, a view's toolbar) stays flush under the top bar. When it is docked, the
Server and Workload list of views stays pinned under the top bar while a long
view scrolls; while it floats, it opens from the view's title. The
command palette's list ends on a whole command when it is taller than the
palette.

### Privilege-aware actions

Workspace actions are governed by effective database privileges and connection
write safety:
- **Denied actions**: when an action is definitively denied by database grants,
  its button is disabled using `aria-disabled="true"`. The button remains focusable
  so screen readers and keyboard users can discover the restriction, while pointer
  and keyboard clicks are blocked. The tooltip and accessible description
  (`aria-describedby`) state the required privilege and why it is unavailable.
  The reason is visible beside the action or appears when the action receives
  keyboard focus; blocked write policies use the same treatment.
- **Unverified actions**: when grants cannot definitively prove whether an action
  is allowed (such as MariaDB role trees, routine definitions, or partial revokes),
  the button remains enabled with a separate visible explanation, including on
  keyboard focus, rather than a badge inside its label. This ensures users are not
  blocked by incomplete catalog data, leaving the server as the final arbiter.
  Toolbar actions that sit beside a summary (Explain top N in Top statements,
  New user and New role in Users and roles) keep an unverified reason in the
  button's description and the review dialog only; their visible note appears
  when the action is denied or blocked.
- **Query editor privilege note**: statement privilege analysis checks statements
  against the connection model and write policy. Rather than disabling the editor,
  it shows one line at the end of the editor toolbar only when something is
  denied (or a mixed batch has denied statements), so a late answer never moves
  the editor; the full text is its tooltip and each action's description. A
  statement whose privileges cannot be verified shows no note (the server
  decides; the reason stays in each action's accessible description). Mixed
  batches stay runnable unless every statement is definitively denied.
- **Write safety precedence**: explicit connection safety rules (Read-only
  refusals and Production confirmations) always override privilege states.

### MySQL 5.7

MySQL 5.7 is end of life (Oracle Sustaining Support since 2023-10-25). myflames
reads it (tested on 5.7.44) and says in one plain sentence where 5.7 lacks
something, never as an error; a MySQL 5.6 or older server gets a note that results may be
missing or wrong. Explain, plan files and every chart work on its `EXPLAIN
FORMAT=JSON`, but **Run analysis** is disabled with "Run analysis needs EXPLAIN
ANALYZE FORMAT=JSON, which MySQL has from 8.3 (8.4 LTS and later); use Explain
for the estimated plan." (MySQL 8.0 and 8.2 get the same.) The Query editor, Table data, Kill and Import CSV work (its advice is
SET GLOBAL with the option file, and SUPER); the transaction indicator says it
cannot tell while 5.7's transaction instrument is off, and the ALTER review says
5.7 is not predicted. Query digest's Statement digests have no sample text,
quantiles or CPU time, and the Slow log table and Slow log file sources work.
In Server, Locks reads InnoDB's lock tables, Indexes and Tables give no
invisible-index SQL, Error log gives file guidance, Parameters show no source
and no **Keep after restart**, with SET GLOBAL plus an option-file line instead
of SET PERSIST, and take each parameter's apply type from the version catalog's
row for that 5.7 release (Unknown, and not editable inline, for a release it
does not have), and Replication reads `SHOW SLAVE STATUS`. Cluster reads Group
Replication from 5.7.17 and offers no reviewed changes below 8.0.13. Users and
roles needs 5.7.6 or later, lists users only and says "This MySQL version has no
roles; MySQL added them in 8.0." Schema versions read, compare and export, and
Checks leave reserved words Not judged before 8.0.11. Maintenance has the
upgrade check, dump and restore (structure only included) and no CLONE.

## Connections

Home's **Connections** table shows the name, the type and Read-only as two
pills that never truncate (a read-only Development connection keeps its teal
pill beside the Read-only outline), user@host:port (which takes the spare
width), **Database** (tooltip "Default database"), **Server**, saved-password state, and Last used.
The default columns are sized for 1440 px. In Server, product and version take priority over the "seen … stale" hint, which yields first; hover for the full text. The import sheet is wider to fit its preview columns and shows no empty error alert. The Server column keeps the last facts collected for that server (product and
version, replication or cluster role, read-only and Performance Schema
state), so they stay visible while the server is offline, marked as stale.
**Test connection**, an explicit choice in the connection picker, and **Check
now** in a connection's menu refresh them; opening the window never connects
to refresh them. The Connections header menu has **Import from MySQL Workbench or DBeaver…**,
which opens the **Import connections** sheet with a **From** selector (**MySQL
Workbench** or **DBeaver**): a preview lists what was found with duplicates
unchecked, and imported connections are Production, carry no password and
never connect automatically. The accent
bar marks the connection in use, also named for screen readers. Last used covers
choices made during this window; a connection not used in it reads "—", with
"Not used in this window. Earlier use is not recorded." on hover. A search
field appears from six connections. Columns can be resized or hidden through
the shared table controls. Column choices last while Home is open.

Click to select a row. Double click, press Enter, or choose **Use** from its
row menu to use it. Checkboxes, Cmd/Ctrl click and Shift click select several
rows; arrow keys move between rows, and Shift with an arrow extends selection.
The row menu contains **Use**, **Edit**, **Duplicate**, **Test**, **Forget
password**, and **Delete…**. Duplicate opens a copy of the settings with no
password. **Add connection** and **Edit** open an inspector beside the table,
or a sheet on a narrow screen, with Test, **Save connection** and **Cancel**;
the form keeps a readable width, and **Delete connection** (when editing) is
styled as a destructive action. Host-and-port connections
use TCP, including `localhost`; Unix sockets require an explicit socket
setting. TLS settings stay collapsed. Spaces pasted around a host name or IP
address (and around the CA file path, or after the schema) are removed when
you leave the field and again by the server, so `10.0.0.5 ` and
` db.example.com` connect; a host with a space inside says so under the field.
Pasting `host:3306`, `[::1]:3306` or a `mysql://user@host:3306/db` URL into
Host moves the port, user, password and schema to their fields. Port must be
1 to 65535: an empty or invalid port is shown under the field and **Use**
refuses it instead of connecting to another port. A new connection is named
`user@host` (with `:port` when the port is not 3306) as you type, until you
type a name of your own. A name another saved connection already has (ignoring
letter case and spaces around it) is refused under the field and **Save
connection** stays off; editing a connection may keep its own name. After
**Test connection**, Default database becomes a pick list of the server's
databases (up to 1,000), with the system databases behind **Show system
databases** and **Type a name…** for a name that is not listed; when the list
cannot be read, the field stays a text box and says why.

**Connect through SSH** (a section under the TLS settings) reaches the server
through an SSH tunnel: **SSH host** (a host name, an IP address, or a Host
alias picked from your `~/.ssh/config`), **SSH port** and **SSH user** (empty
means your SSH configuration's value). Host and Port above are then the
database as the SSH host reaches it, often 127.0.0.1. myflames runs the
system `ssh` with your own configuration, keys and agent; password sign-in to
SSH is not supported, and an unknown host key is never accepted for you (run
`ssh HOST` once in Terminal to check and accept it). **Test connection** says
"Through SSH host X", and every read, editor session, change, dump, restore
and upgrade check of the connection goes through the tunnel. Tunnels are
shared, close after five minutes without use and always when myflames quits.
The SSH target is part of the connection: behind another SSH host the same
database address is another server for saved passwords and the connection
type. CLONE is not offered through SSH, and the MariaDB error log file is
read only for a server on this computer, so not through SSH. From the command
line: `myflames profiles set NAME --ssh HOST [--ssh-user USER] [--ssh-port
PORT]` or `--no-ssh`.
Recent work stays beside the table at desktop widths and follows it on phones;
the phone table starts with selection and names, with more columns available
from Columns. Wide tables scroll inside their frame.

**Delete (n)** in the toolbar, the row menu, and Delete or Backspace open the
same confirmation sheet. It names every selected connection and whether its
saved password will also be removed. Cancel is focused first and Escape cancels.
Deletion runs once per connection; a failed row remains selected with its error.
Deleting the connection in use also clears its typed password and sets the
picker to **No connection**. Existing editor sessions stay open and retain their
own connection. New server requests require a connection to be chosen.

The unsaved **Local MySQL** row appears only while those settings are in use.
Its menu offers **Save** and **Remove**; Remove clears the settings from the
workspace after confirmation and deletes nothing on disk. The first connection
you save becomes the one in use when none is saved in the picker.

The Query editor and Server show the connection in use in their header. Open
the picker to switch connections or enter a session password. **Manage
connections…** opens Home and focuses the table. When no password is saved, a
**Session password** field appears there; **Use a different password** replaces a saved one
for this page. **Set**, Enter or leaving the field applies it for this
window, as the hint under the field says. A typed password is never saved in
the workspace session.

### Production, Development, Read-only

Each connection has a **Type** in its form (the ⓘ says what each confirms):

- **Production**, the default for new and existing connections, asks before
  every change myflames sends.
- **Development** asks only before risky changes: DDL, KILL, SET GLOBAL, and
  UPDATE or DELETE without a key.
- **Read-only** (a switch, with either type) refuses every change before a
  connection opens.

Rows show the type and Read-only. A connection used without saving (**Use
without saving**) is always Production; it may only choose Read-only, which the
workspace keeps with the connection in use. A saved Development connection
counts only for the host, port, user and TLS settings it was saved with: change
one of them in the form and it warns that the connection is Production there,
shows Production, and saves as Production unless you choose Development again.
**Use** with such edits, before saving, is Production too.

Every view names the server by its product: MySQL, MariaDB, Percona Server
("Percona Server 8.4.11-11", the release number kept) or Percona XtraDB
Cluster, in its summary line, the window footer, the connection test, Tuning,
the Full analysis, Replication's source version and Cluster. Percona Server
follows MySQL's rules; what it adds is shown only when the server reports it:
with `userstat=ON`, Indexes keeps an index Performance Schema shows unused when
user statistics saw it read ("Keep: read per user statistics", with the rows
read in its details) and Tables adds a Rows changed column (inserts, updates
and deletes together; a dash for a partitioned table, which user statistics do
not count reliably); Health shows a
Thread pool row (threads and idle threads) in place of the Thread cache under
`thread_handling=pool-of-threads`;
Parameters tags the variables Percona adds with **Percona**.

The window footer of the Query editor, Query digest, Database diagram, Schema
versions, Server, Workload, Replication, Cluster, ProxySQL, Readyset and Tuning starts with the write safety
of the connection in use: a purple dot and
"Production · changes ask first" (the full sentence, "Production: every change
on NAME asks first", is its tooltip and accessible name), an outlined dot and
"Read-only · changes refused", or a teal dot and "Development · risky changes
ask first". Nothing is drawn under the top bar, so the top bar and view
headers stay clear; a thin strip in the type's color runs along the top edge of
the window on the same places, as decoration only (the words stay in the
footer). Every review or confirmation sheet for a change starts with a band
naming the type and the connection it was reviewed on ("Production · prod-eu
(10.0.0.5:3306)"), which stays that connection even if you choose another one
in the picker meanwhile. The picker has a dot of the same color (purple Production,
teal Development, an outline when read-only) and the type pill, with a
Read-only outline pill beside it when set; in a narrow window (1,100 px and
below) the pill keeps a short **Prod** or **Dev** in the type's color
instead, outlined for a read-only connection, never in a status color. The
server enforces the type on every write; the footer and the dot only show it. Saved connections are read
again whenever the window comes back and right before a Kill is sent, so a
type or read-only change made in another tab or with `myflames profiles set`
shows up; a Kill confirmed under the old setting sends nothing. While they
are loading or cannot be read, the footer says **Production assumed** (with
**Retry** after a failure) and Kill stays off. A Development choice in the form holds only for
the host, port, user and TLS settings it was made with.

## Audit log

Parameters and Users and roles show **Changed through myflames** for the selected
object: the newest matching successful or outcome-unknown local record, with
readable operation names (raw key on hover), "Kept on this computer.", the stored
SQL boxed with Copy beside it, and **Open in Audit log** for the exact record.


**Settings > Audit log** opens the record of every change myflames sent to a
server, kept on this computer (`~/.myflames/workspace.sqlite3`, never a
password): when, which connection and its type (and its SSH target, as
**Through SSH**, for a change sent through a tunnel), the operation, the
statements as stored with secrets redacted, the status, the error and the
duration. It is
a screen of its own, not a place, so no place is highlighted while it is open.

| Status | Meaning |
| --- | --- |
| **Done** | Sent, and the server answered without an error. |
| **Failed** | The server or the connection returned an error (the code and message are listed). |
| **Outcome unknown** | Interrupted, timed out or disconnected after the statement may have reached the server; check the server. |
| **Not finished** | Recorded before sending, but myflames stopped before it recorded the result; the statement may have run. |
| **Stopped** | You stopped a write that was under way (Import CSV's Stop); nothing more was sent, and the message says what was kept. |
| **Refused** | myflames did not send it: a read-only connection, or the target changed or ended. |
| **Declined** | Cancelled in the confirmation (Cancel or Escape); nothing was sent. |

Filter by connection, status, and period (**Since**); the server applies the
filters and lists the newest 500 matches, and **Load 500 older** brings the
next ones. The list starts with Time, Status and Operation, then the
connection, its Type (the type pill and, when set, the Read-only outline),
the statements, the error and Duration; in a window narrower than 960 px
Type and Duration step aside (Columns brings them back). Times are local,
with the stored UTC value on hover. Select a record for the full detail in
the inspector: the statements (each with **Copy**), the connection, the
result, the identity you confirmed, what was checked before sending, and the
write policy. Stored keys read as words (Timed out, Requires confirmation, Risky,
Production), with the stored key on hover. The policy's connection type is
labelled **Judged as**: a connection used with other settings than the saved
ones is judged as Production, so when it differs from the connection's own
type the inspector says so. The ⓘ beside the summary line lists every status
with its meaning. **Export** saves every record
that matches the filters as CSV or JSON. **Kept 90 days** in the toolbar
changes how long records stay (1 to 3,650 days; a shorter period deletes older
records at once). With nothing recorded yet the screen says so ("No changes
recorded yet. Every change myflames sends, refuses or you decline is listed
here.") and **Export** waits for a record. From the command line: `myflames audit`.

## Inspect a plan

1. In Plans, choose **Import plan**, drop a JSON file, or paste a saved plan.
   Each plan can be up to 5 MiB. **Explore sample plan**, on Home or in the
   empty Plans screen, loads a bundled example.
2. Review execution time, estimated rows examined, findings, and suggestions.
3. Switch between Visual Explain, flame graph, execution tree, and bar chart.
   In every view the chart and the inspector fill the window; findings and the
   execution context follow below it. Every chart's search field reads
   **Search operators**. The bar chart prints times like the execution tree
   (4.66 ms, 3.57 s) and colors each bar by its share of the plan's time, so
   only the bars that dominate are red. Resizing the window draws the flame
   graph, tree and bar chart again for the new width and keeps the selection.
   The execution tree column **Self of total** draws each operator's own
   time as a share of the whole plan's total time on a common baseline.
   The Python SVG and workspace chart use the same scale. **Total** shows
   every row's time including its children, leaves too (a leaf's total is its
   self time), and an operator without measurements reads "—" in Self and
   Total.
4. Search the operator list and select an operator to inspect its measurements
   and raw fields. A plan with findings opens the inspector on **Findings**
   until you choose an operator. Findings with a matching operator include an
   inspection link. In a window 900 px wide or narrower, the inspector with
   nothing selected keeps its tabs and shows one line, "Select an operator to
   see its details", so the chart keeps the space; Stats, a page of tables,
   keeps scrolling with the page.
5. Choose **Export** (export icon and a pull-down) and then **HTML report** for
   a portable report or **JSON** for the existing versioned analysis sidecar.
   **Hide literal values** above the formats (reach it with the arrow keys and
   toggle it with Space; the menu stays open), checked by default, replaces
   the strings, numbers and dates in the SQL, conditions, collected DDL and
   generated advice with `?` before you share the file; names and measurements
   stay, the page says **Literal values hidden**, and the plan on screen does
   not change. Clear it to export the plan as captured.
   The same Export pull-down saves Tables and Indexes inventories as CSV, HTML
   or PDF.

**Stats**, beside the chart views, totals the plan by table (**Time per
table**) and by operator type (**Time per operator type**): how many
operators, their self time over all loops, their share of the plan's time,
rows produced, estimated rows per loop and loops, sortable like any table.
Self time never adds an operator's children twice, and separate branches can
make the shares add up to more than 100%. Without ANALYZE only counts and
estimates are known. Select a row and go back to a chart: its operators are
highlighted. **All occurrences** in an operator's inspector lists every place
the same operator (same type, schema, table and index, whatever the
condition) appears, with their combined measurements: one two-line row per
place, slowest self time first, with its last two parent operators (the whole
path on hover), its self time, share of the plan's time, rows and loops. The
arrow keys, Home and End move between rows; Enter, Space or a click jumps to
that operator in the chart. It works in every chart view and in both panes of
Compare. HTML reports include the Stats tables, collapsed.

When a measured plan has eligible base-table accesses, or filters directly above them,
whose rows per loop differ by at least 10×
(`max(actual, estimated) / max(1, min(actual, estimated)) >= 10`) and whose
larger estimate or actual count times loops reaches 1,000 rows, the plan header shows **Misestimates N**: click it to step through them in
descending ratio order, marked in violet across Visual Explain, the flame
graph, the execution tree, and the bar chart (when the bar chart omits an
operator because its self time is zero, a status message above the chart
explains why and the inspector still opens it). The inspector's **Row
estimate** section shows how far off the estimate was and, when the operator's
table maps unambiguously to a single base table in the captured schema
evidence, the table's `mysql.innodb_table_stats.last_update`, statistics row count and
histograms with an `ANALYZE TABLE` statement to copy. Overestimates are skipped
below early-stop ancestors such as Limit and semijoin inner sides; MariaDB
compares only eligible index lookups and only underestimates.

The UI accepts the same MySQL and MariaDB plan formats as the Python engine,
including MySQL's `query_plan` wrapper. It checks plan size and nesting before
running the parser. Embedded SQL appears above the visualization when available.

The import dialog scrolls within short windows, keeps validation errors beside
the form, and supports keyboard file selection. Press Escape to dismiss it when
an import is not running. Browser regression checks include the open dialog at
1440, 390, and 320-pixel widths, including its error state and native file input.

The HTML export uses the existing report renderer. Its core charts and analysis
are self-contained; links to additional teaching pages may require the adjacent
`teach/` bundle generated by the CLI.

## Visual Explain

Choose **Visual Explain** above the chart for operator colors, larger Venn-style
join symbols (drawn with the appearance's colors, so their L and R labels stay
readable in dark mode), and arrows sized by estimated row counts. Drag to pan, scroll gently to
zoom, or choose **Fit** (the controls read **−**, the percentage, **+**,
**Fit**, like the Database diagram). A plan opens fitted only as far as keeps
its smallest card and edge text at 11 px; a larger plan opens at its first
inputs and you pan to the rest. **Fit** returns to that opening view, and
zooming out still shows more of the plan at once. Search within the chart and select a node to update
both its chart details and the workspace operator inspector. HTML export keeps
the interactive view. The percentage shows the current zoom; click it to reset
to 100%. See [Visual Explain](WORKBENCH_VIEW.md) for measurement
semantics and differences from Workbench.

## Compare plans

Import two plans, choose **Compare** in the Plans bar, and choose the before and after versions.
The first plan you import is Before; After stays empty until you import
another one (each later import becomes After). With only one plan open, Compare
leaves After empty and offers **Import plan…**.
The workspace shows timing changes and the existing operator comparison report.
**Change** reads as a ratio from 100% on ("750× slower", "2× faster"), otherwise
as a percentage; the exact percentage is in its tooltip. Each pane names its
plan's total time, and each chart is scaled to that total, so compare the
printed times rather than bar lengths across plans. A pane's inspector opens
when you select an operator in it, and both inspectors start on the same line.
**Operator changes** names each pair once when both sides match, says how it
was matched in words ("Same operator · high confidence", "No counterpart") and
marks it Added, Removed, Changed, Unchanged or Uncertain. Choose **Export**,
then **HTML report** or **JSON**, to save the comparison.

Operators are matched by structure and semantic identity. Ambiguous repeated
operators are marked uncertain and excluded from regression counts. Compare measurements collected
under similar conditions, including cache state and concurrent work.

## Learn

Choose **Learn** in the sidebar to browse all registered lessons. The Learn
place uses a view toolbar (**Search lessons**, **Family** filter, **Start
learning**), a one-line summary ("23 lessons, 16 in the learning path"), and
grouped rows by family, ordered by the learning path, each with its summary and
"Lesson n of 16" (**Additional topic** for a lesson outside the path). A
lesson without named phases hides the empty **Phases**
panel; lessons with phases keep their phase navigation.

An open lesson has one thin toolbar, one control high (**All lessons**,
**Previous lesson**, **Lesson n of 16**, **Next lesson**, **Download lesson**,
**Full screen**), that stays under the top bar while the lesson scrolls, and fills
the window edge to edge like every other place; the top bar shows the lesson title
(lesson titles use a colon, such as "B+tree lookup: how InnoDB finds a row").
Below 860 px **Download lesson** shows its icon only (the name stays its
tooltip and accessible name), so the toolbar keeps one row.
The workspace adds `data-embedded="true"` to the lesson page, which hides its
own header and footer. Topics outside the learning path can be opened directly
from the catalog, and **Download lesson** saves the self-contained HTML
(for example, `btree.html`) for offline use.

A lesson opens with its stage first: the parameters in one compact row (each
hint is the (?) beside its label, shown on hover or while the control has
keyboard focus), then the toolbar, scrubber, step bar and the drawing, all on
screen at 1440x900 without scrolling. **About this example** (the example
query and what the animation shows) follows the drawing; the link beside the
parameters' title scrolls to it. The drawing never grows taller than the
window allows, and keeps its proportions.

**Full screen** shows only the animation: the lesson's Play, Reset, Loop and
Speed controls with the scrubber at the top, the stage scaled to fit the width
and height of the screen, and a strip at the bottom with the caption (the phase
narration) and the live numbers (as one-line chips on short screens). The
lesson's **Phases** list stays beside the stage in a window wider than 900 px.
**Details** opens the parameters, example query, explainer, explanation, chart
and Learn more in a panel beside the stage, which stays visible and keeps
running. The workspace asks for full screen (the desktop app grants it, and the
lesson then fills the screen); when that is refused or unavailable, the lesson
covers the whole app window instead, above the sidebar and toolbars. Both have a bar with the lesson
title and **Exit full screen**. Escape leaves full screen whether focus is in
the lesson or on the bar; with **Details** open and focus in the lesson, the
first Escape closes Details (in the browser's own full screen the browser may
leave at once). Tab stays inside while it is open, and focus returns to
**Full screen** afterwards. The lesson's own keys apply only while focus is
inside it. Leaving Learn ends full screen.
The workspace and the lesson switch the layout with one fixed message type
(`myflames-lesson-focus`), each accepting it only from the other window, like
the appearance message. A downloaded lesson has its own **Full screen** button
in its stage toolbar (the browser's full screen of the page, or the same
layout filling the window when refused) and the same **Details** and Escape
behaviour.

Every lesson is guided: it steps through the animation one idea at a time: **Next step** plays one step
and holds, **Back** returns one, and Play runs the rest. A **Simplified** code
panel beside the drawing (under it in a narrow window, and in full screen)
highlights the lines the current step runs; a grey name after a line is the
MySQL source function that does it, given only where it was checked in the
source. At the end, **Check yourself** asks a few multiple-choice
questions and answers at once with a one-sentence reason for every choice;
nothing is saved or sent. **Explore** replaces the step buttons with a slider
over every step and, for a lesson with parameters, an **Inputs** button under
the step bar: changing an input shows the same step again (**Replay step**
plays it), and leaving Explore continues the guided steps from there. The
Filesort lesson's **Server** input switches between MySQL's comparison sort
and MariaDB's radix sort for short sort keys, with each server's default
sort_buffer_size, and Buffer pool warmup's dump rule follows MySQL (a share of
the cached pages) or MariaDB (a share of the pool).

Lesson pages and the standalone catalog (`myflames teach --index`, which
includes a lesson filter) use the workspace's light and dark tokens
(`prefers-color-scheme`, `data-theme` overrides, and `?theme=` / `?embedded=1`
URL parameters), restyled controls, and semantic `--svg-*` drawing colors
resolved by `anim.cssVar` and `anim.palette`, so drawings redraw when the
appearance changes and keep their meaning (descent path, arrival pulse, PK hop,
build vs probe, hit, spill).

Switching between Learn and Plans keeps your imported plans in
memory. Leaving a lesson stops its animation; reopening it resets its controls.
The catalog comes directly from Python's lesson registry, so newly registered
lessons appear without maintaining a separate frontend list.

## Session and scope

The workspace saves its session automatically to `~/.myflames/workspace.sqlite3`
and restores it the next time you run `myflames ui`: open plans, query tabs, the
current view, selections, and the Server view (sessions saved with the old
Running queries tab open on Sessions). Choose **Start fresh** to clear it, turn off
**Restore workspace on startup** in **Settings** to stop saving, or launch with
`myflames ui --no-restore` to start clean once without deleting it. Save
investigations to keep named copies in the library. Downloads are saved through your browser. Your session and
saved investigations stay on this computer. Apart from your database servers,
myflames contacts only official release pages when it checks for updates (at
start and once a day while **Settings > Check for updates automatically** is
on, or when you ask with **Check for updates** on Home, About or Maintenance's
Tools) and the two news
feeds described below, while Home is shown and news is turned on.

Home news only reads: while Home is on screen and **Settings > Show MySQL and
MariaDB news on Home** is on (the default), the backend reads two fixed public
feeds over HTTPS, Planet MySQL (keeping the MySQL team's posts on
blogs.oracle.com) and the MariaDB Foundation, at most every six hours (ten
minutes after a failed read, or on **Refresh news**), and keeps the headlines
in `news-cache.json` next to the workspace database. The requests carry no
cookies and nothing about you or your connections; the page itself never
contacts the feeds. With the switch off, nothing is read and the section is
hidden.

The Query editor runs SQL and captures live plans using the existing local
database client (`mysql` or `mariadb`). Each query tab keeps one client
process, so its server session (SET SESSION, temporary tables, an open
transaction) lasts until the tab closes, and myflames never reconnects without
asking. Results, Messages, export (CSV, JSON, SQL INSERT, Markdown, the
clipboard; SQL INSERT starts with the table of a result read from a single
table, and its schema when the statement names one or a USE earlier in the
same run chose it; both stay editable), the schema browser and the run history are
described in
[Run SQL in the Query editor](QUERY_WORKSPACE.md#run-sql-in-the-query-editor).
Results stay in page memory: session restore keeps the tab's text, caret,
default database, On error choice, row limit and pane sizes, never results or
file paths. The toolbar has a **Database** picker for the tab's default
database (its list of databases is read only while the Query editor is on
screen; choosing a database while a session is open sends `USE` in that session
via `POST /api/editor/use`, and choosing **None** keeps the open session's
current database while setting the default for a new session to none) and
**Find and replace** (**⌘F** / **Ctrl+F**, **⌥⌘F** / **Ctrl+H**). None survives restore explicitly, even when the connection has a default database; a tab with a session on another connection refuses a database change. Run options (the arrow beside Run)
holds Rows, Time limit, **On error: Stop / Continue** (for Run all), and **Safe updates**. A finished Continue run counts succeeded, failed, not run, stopped and outcome unknown statements in Messages. Errors 1213 and 1205 add a note on InnoDB behavior (a deadlock rolls back the victim's whole transaction; a lock wait timeout does only with `innodb_rollback_on_timeout` ON), never a claim about this run. The On error ⓘ explains statement-only rollback by default and whole-transaction rollback for these cases. The error mode is part of the write review, so changing it asks again; the ⓘ beside Safe updates says in
plain words what `sql_safe_updates` refuses, its default per connection type,
how to turn it off for the tab, and links to the MySQL and MariaDB
documentation (opened in your browser). A tab's menu (right-click, or Shift+F10
or the context-menu key on the tab) has **Open SQL file…** (**⌘O** / **Ctrl+O**,
for `.sql` and `.txt` files up to 5 MiB), **Save…** (**⌘S** / **Ctrl+S**),
**Save as…** (**⇧⌘S** / **Ctrl+Shift+S**), **Save query…**, **Rename…**, **Duplicate**, **Close**
and **Close others**; double-click or F2 renames too, and a file-backed tab with
unsaved edits shows an edited dot (`●`) and asks **Unsaved changes** before
closing. The Schemas panel also lists **Routines**, **Triggers**, and **Events**
folders under each expanded database. **Format SQL** (the
indent button in the toolbar, Shift+Option+F) lays out the selection or the
whole tab as one undo step, changes only white space and keyword case, never
runs anything, and refuses when the result would read differently; see
[Format SQL](QUERY_WORKSPACE.md#format-sql).
Save refuses symbolic-link targets and a folder changed since it was chosen;
use **Save as…** to choose it again. Adding `.sql` never replaces a file that
already exists: choose its full name to approve replacement. Open refuses special
files and a file or folder swapped for a link after selection, and enforces the
5 MiB limit even if the file grows while being read. If a backend restart loses
the file binding, Save opens Save as within the same operation and reports the
outcome. Cancelling or failing a save or close keeps unsaved edits.

Connections store settings (the API and CLI call them profiles). When you save
one with a typed password, myflames asks whether to save the password too; it is saved only if you choose
**Save with password**, and then in the operating system's credential store or
an encrypted local file, never in the workspace database or the saved session.
Each saved connection is Production (asks before every write, the default) or
Development (asks only before risky writes), and can be read-only (refuses
every write); every write attempt goes into a local audit trail
(**Settings > Audit log**, or `myflames audit`). See
[Production, Development, Read-only](#production-development-read-only) and
[Audit log](#audit-log).
See the [query investigation guide](QUERY_WORKSPACE.md) for capture modes,
saved credentials, and limits.

The server binds only to IPv4 loopback. Use the exact URL it prints. API requests
require the instance cookie and origin-scoped proof with exact Host and Origin; report previews run in a
sandboxed iframe. It is a local desktop utility, not a public hosting server.

## Build the frontend

The source lives in `apps/workspace/`, using React, TypeScript, and Vite.

```bash
cd apps/workspace
npm ci
npm run build
```

The build writes `myflames/ui_assets/` and the chart runtime
`myflames/assets/myflames-charts.js` (sources in `apps/workspace/src/charts/`),
which HTML reports and explore pages inline. Commit these generated assets with
source changes so Python users can launch the UI without Node.js; a Python test
fails when the committed chart bundle is older than its sources. `npm run dev`
watches and rebuilds the app assets; run `myflames ui --no-browser` in another
terminal and reload the page after a rebuild.

With **Slow log table**, opening a statement reads its **Tables** section immediately (metadata only, no table rows).

Top statements and Query digest share one statement inspector (`StatementInspector.tsx`) and statement thread (`statementThread.ts`). Top statements opens a selected statement in a detail view using the available width. Back returns to the retained list with its sorting, filters and scroll position. In Top statements and Query digest's Statement digests source, a statement flagged No index used or Full scan offers **Suggested index** under Flags. **Read suggestion** starts the estimated `EXPLAIN FORMAT=JSON` and existing-index read (`POST /api/live/index-suggestion`); planning can evaluate expressions, so the inspector never starts it on its own. A fresh result is reused on reopening, and **Read again** reads anew. The result proposes a `CREATE INDEX` to copy or open in the Query editor, names an index already leading with the columns, or explains why it cannot suggest one. A proven unfiltered `SELECT * FROM t` needs no server read to explain that no index reduces rows read if `t` is a base table, and a truncated statement shows the server setting that cut it with the memory cost and option-file or `SET PERSIST_ONLY` lines. Supported write statements offer a read-only SELECT projection for review before Explain or Run analysis. The original editor text remains in its own tab; the converted SELECT needs a second explicit action. This is a different plan and does not measure writes, UPDATE assignment expressions, triggers, constraints or locking. Batch analysis leaves these rows for individual review. After a suggested `CREATE INDEX` succeeds in its Query editor tab, **Measure the statement again** there opens the source statement and asks before one Run analysis on the same connection. Its **Analysis** tab shows the digest's before average latency and rows examined per execution beside this run's statement-history time and rows examined (which may be unavailable). **Compare plans** appears there when an earlier non-session capture of the same SQL, connection and database is still in memory, and opens the two captures in **Plans > Compare**.

**Table structure** is the first action in a Tables row's menu, its context menu and the inspector's action bar: a sheet with the table's columns (type, nullability, default, character set, collation, comment), indexes, foreign keys and the exact `SHOW CREATE TABLE`, read once when it opens through the connection the list was read with, with **Copy CREATE TABLE** and **Download .sql** (`<schema>.<table>.sql`). It is read-only; a refused `SHOW CREATE TABLE` keeps the other tabs with its reason, and columns a server lacks (MySQL 5.7 index visibility) read Unknown. It is disabled with the reason on a list read from another connection. Tables shows reusable InnoDB free space as information. **Optimize table** is an optional action with exact SQL and rebuild caveats to review. Instruments reads on open (one attempt per connection) and organizes its view into **Features**, **Instruments**, **Consumers**, and **Actors and objects** tabs: filter and select up to 2,000 instruments (with **Enable**, **Enable without timing**, **Disable**, and **Clear selection**) or consumers (enabling required parent consumers automatically), and run batched `UPDATE` statements (up to 500 names per statement) through the reviewed SQL runner, with copy-only option file lines under **Keep after restart**. Both require confirmation on Development and Production connections, refuse Read-only connections and remain disabled for stale data from another connection. Copying statements still works independently.

## Query digest

**Query digest**, below the Query editor in the SQL workspace group, reads
when opened on screen, once per connection. **Refresh**, a new **Rank by**
choice, Top 20/50/100 or a changed default database reads again. **Schema**
lists the schemas of the loaded statements (and **No default schema** when
some have none) and filters the rows on screen without reading again; **Filter
statements** searches the loaded SQL, schema and digest without another read.
In the 720 px minimum window the controls take two rows: Rank by, Schema and
Top on the first, the filter field (which grows) and Export on the second;
Refresh stays beside the title.

The summary line below the toolbar leads with the numbers: the loaded
statement count, their total time and executions, and for Performance Schema
rankings how long the counters cover ("12 statements · 567.66 s total ·
1,465,072 executions · since 2 d 7 h ago"; the "since" part explains on hover
that counting runs from the server's start or the last reset of the digest
table). The slow log sources show the numbers and their range, and "· N shown" follows while
a filter hides rows. For the slow log sources it reads "Slow log file: <name> · N statements · … total · … executions · <range>" (or "mysql.slow_log · …"). The ⓘ beside the title names the table the report on
screen read and its counting window:

- **Performance Schema statement digests** (Source: **Statement digests**) reads
  `performance_schema.events_statements_summary_by_digest`. Counters accumulate
  since server start or the last digest-table reset; server uptime is shown
  when reported, without claiming the reset time is known.
- **Slow log table** and **Slow log file** (below) read the slow query log.
  They replaced the "Slow queries over 1 s" ranking, retired in 3.3.1.

**Source** chooses where the statements come from: **Statement digests**
(Performance Schema, as above), **Slow log table** (`mysql.slow_log` of the
connection in use) or **Slow log file**, which opens a MySQL 5.7 to 9.x, Percona
Server or MariaDB slow query log on this computer and needs no server. A file
source reads nothing until a file is chosen: the empty state **Open a slow query
log** offers **Choose file…** (the system file chooser) and **Type a path
instead** (a bad path is explained under the field); **Open another file…**
sits at the end of the summary line. A read shows "Reading <name>…", a progress
bar and **Stop**, which keeps the last report. Both slow log sources sum
statements by fingerprint (myflames' own: literals as `?`, lists as `(...)`)
and show count, total, average, P95 and maximum time, lock time and rows, with
**Rank by** Total time, Calls, Average time, Slowest call, P95, Lock time, Rows
examined or Rows sent, and a **User** filter that is part of the read. Changing
Source starts the range and the user over (Rank by stays). The inspector shows
Latency (Min, P95, P99, P99.9 and the histogram from the report, no read),
**Who ran it** (users and hosts), **Fingerprint** (16 hex characters, with
Copy) and **Sample (the slowest call)** with its time, user and host; it has no
Tables section for a file, and Explain and Open in Query editor for a file row
run on the connection in use (their tooltip says so). Counters a log does not
record read Unknown, never zero. The text export adds Source, Logged by, Clock,
Time range and User, and the CSV adds Last Seen, Min Time (ms), Users and
Fingerprint ID. Nothing of this is saved in the session.

**Time range** opens a calendar with a date and time for From and To and quick
ranges (**Last hour**, **Last 24 hours**, **Today**; **Any time** clears it).
Times are never moved to another zone: a log's entries are compared as the log
wrote them, in its own clock (UTC, an offset or the server's local time, which
the report names), and both ends are inclusive to the precision written (a To
without seconds keeps that whole minute). The slow log sources count only the
entries logged in the range, and leave out entries with no time, saying how
many. Performance Schema statements keep their totals since counting began, so
the range there keeps the statements seen in it (first seen before To, last
seen after From), says "Numbers are totals since counting began, not only this
range: the range keeps the statements seen in it." and reads nothing. Quick
ranges end at the file's last entry, the server's current time or the newest
last seen.

**Share of total time** is each statement's share of the server's total
statement time (every digest the server holds, not only the loaded ones; its
header says so on hover), and the table ends with one line for the statements
it did not load, such as "212 other statements · 18.4% of total time · 3.1M
calls". The slow log, which has no server total, keeps shares of the loaded
total before text filtering. Conditional notes retain lost-digest counts,
overflow executions and the report's source, access or truncation caveats, say
when counting began after the server started ("Counting since …: the digest
table was reset or statements began after the server started.") and when
sample text was cut at `performance_schema_max_sql_text_length`.

A statement first seen in the last 24 hours carries a **New** badge before its
text and in the inspector (the time it was first seen on hover); it shows only
while the counters cover more than a day, so a restart or a reset does not mark
everything new. On MySQL, **CPU time** is a column you can show from the
Columns menu and a fact in the inspector; it reads **Not collected** while the
`events_statements_cpu` consumer is off (it is by default), with one muted line
under it ("events_statements_cpu is off.") and a link to that row in
Instruments. The inspector's **Latency** section (MySQL) shows P95,
P99 and P99.9, each the upper bound of the server's histogram bucket, and a
latency histogram of the selected statement read from
`events_statements_histogram_by_digest` when the section comes into view: bars
on a time scale in tenths of a decade, each with its range and calls on hover,
a sentence such as "Most calls took 1 to 2 ms; the slowest took 1.23 s.",
and the buckets as a table under a disclosure. MariaDB has no percentiles,
histogram or CPU time. Another connection's report reads no histogram.

Below Latency (or before Normalized SQL when there is no Latency section), the
inspector's **Tables** section lists the tables the statement's text names,
"Found in the statement text": each table's type and engine, **Rows
(estimate)**, Data and Indexes sizes, and its indexes with their columns
(unique and invisible marked). It reads table metadata only when the section
comes into view, on MySQL and MariaDB alike, and never reads rows; its ⓘ says
it is a text match, not proof of access, since views and routines can refer
to other tables. A table without metadata reads **Metadata unavailable** (it
may also mean no privilege). **Open in Indexes** opens a base table in Server >
Indexes. A result stays for that statement until Refresh; another
connection's report reads nothing.

The shared sortable table shows normalized SQL, schema, share of total time
(of the server's total, or of the loaded total for the slow log or when the
server total is unknown),
total time, calls, average, maximum and rows examined per row sent. Times read
as Top statements writes them ("59.04 ms", "1.20 s"), with the exact
milliseconds on hover. Select a row for **Open in Query editor**,
**Explain**, facts, the digest, normalized SQL and sample SQL. The inspector's
fact lists line up in one label column and one value column. The digest fits
one line, shortened in the middle, with the full value on hover and in its
**Copy**. **First seen** and **Last seen** are the server's own clock (the
server records them without a time zone), written as a short date and time
without converting to this computer's zone; hover for "Server time:" and the
exact value.
Right-click or Shift+F10 offers the same view actions, then **Copy statement**
and **Copy digest**. Explain shows its refusal reason when capture is unavailable.
Another connection's report stays greyed with **From <connection>** and its
statement actions disabled until refreshed; dispatch checks identity again.
With **Statement digests**, **Save baseline** in the Refresh menu keeps the
current digest totals on this computer for that server (`with_identity: true`,
literals hidden, no samples, up to 20 per server), and **Compare with baseline**
lists new digests and digests whose average time or call rate rose by more than
50% (comparing average latency only when the server restarted or the digest
table's `oldest_first_seen` moved forward since the baseline).

**Export** offers **Text report** (preview, Copy and download) and **CSV** for
all loaded rows. Both add New, P95, P99, P99.9 and CPU time per statement; only
the text report also gives the server totals and the line for the statements
not loaded (the CSV holds the loaded rows). The text starts with `# myflames query digest` and names the
server product, filter, source and coverage. The footer uses the report's
product name, including Percona Server and Percona XtraDB Cluster, only when
it belongs to the connection in use. The table and inspector scroll separately;
the inspector becomes a sheet in narrow windows. Start fresh, or another
window's newer workspace, clears this place's search and selection.

## Database diagram

Open **Database diagram** in the sidebar's SQL workspace group (after Query
digest; Home's Database diagram card and the command palette open it too) to inspect a database's tables,
columns, keys, and foreign-key relationships. Choose a database from the connected server, then select **Load <database>** (the empty state) or the toolbar's **Load**; choosing a database never reads it. The toolbar button reads **Refresh** once that database's diagram is shown, and choosing another database while a diagram is shown offers to load it (loading asks before replacing the diagram). A diagram from another connection says so and offers **Load <database>**. **Reload database list** is an icon button beside the Database list. Rebuilding retains positions for tables that remain and incorporates added or removed tables and relationships. Replacing an imported diagram asks for confirmation first; cancel leaves it intact. You can also import SQL (`.sql`), Mermaid (`.mmd` or `.mermaid`), or editable JSON
without a database connection. Imports are parsed as data and never executed.
Files and models are limited to 2 MiB; a model holds at most 200 tables,
10,000 columns, 1,000 relationships, and 20,000 index key parts. Unsupported input and incomplete metadata produce
warnings; inspect them before treating a diagram as a complete schema. SQL
import supports CREATE TABLE and ALTER TABLE ADD keys and foreign keys. Mermaid
uses an erDiagram subset and reads column names and nullability back on import. Other clauses or statements are reported rather than
executed. Live metadata reads share a 30-second budget. Mermaid ER
files import through **Import a schema** (format Mermaid) and open on the
interactive canvas.

Once a diagram is shown, a **Diagram | Mermaid** switch toggles between the interactive ER canvas and the
**Mermaid** view. The Mermaid view renders the diagram (`mermaid` 11.17.2, loaded
on first use as separate chunks with `securityLevel: "strict"`, following light
and dark appearances, with zoom in, zoom out, fit, and inline render errors)
beside the Mermaid code, **Copy**, **Download .mmd** and the export notes.
Mermaid export uses Mermaid entity escaping (`#quot;`, `#39;`), readable
sanitized types (`int_unsigned`, `decimal(15-5)`), PK, FK and single-column UK
markers, readable column aliases with the original name in a comment, and
comments only where information would otherwise be lost (enum or set values,
renamed columns, unknown nullability).

Drag tables, pan and zoom the canvas, use Fit or auto layout, and select a table
or relationship for details. Focus a table and use the arrow keys to move it
16 pixels, or Shift+arrow for 64; Enter opens its details. Position fields and
Reset offer alternatives to dragging. Conceptual relationships use dashed
lines; a relationship without column mappings does not claim a database foreign
key. Relationship lines have a small gap at crossings so they do not look like
junctions. You can follow each relationship to its table. Mermaid
cardinalities are retained as modeling information.

Export SQL or Mermaid after reviewing any loss warnings, editable JSON to keep
the model and table positions, or standalone SVG to share the drawing. Export
does not apply schema changes. The diagram and layout stay in memory and are
not part of session restore; save JSON to continue later. The place has no
Refresh all of its own: Server's Refresh all reloads live diagrams and can
build the selected database before the first diagram load, and opening the
place while it reads keeps that answer. Imported diagrams remain unchanged.

## Schema versions

Open **Schema versions** in the sidebar's SQL workspace group (after Database
diagram, or "schema versions" in the command palette). It has two tabs.

**Saved versions** lists the newest 1,000 versions saved on this computer: database,
version number, name, server, product, when it was saved, how many objects it
holds and whether it changed since the version before it of the same
database. The **Database** choice in the toolbar lists the databases of the
connection in use, read from the server (system databases left out), and the
databases that have saved versions, each with how many it has; choosing one
shows only its versions, and a database with none offers **Save version of
NAME…**. **Save version…** reads a database of the connection in use (the one
chosen in Database, else the connection's default database) and saves it as
its next version (numbered per server address and database); the line under
the toolbar says "No changes since version N" when nothing changed. Reading
only looks at `information_schema` and `SHOW CREATE` output; nothing on the
server changes. **Import…** saves a version file exported earlier (from
**Export** or `myflames schema export`). Select a version to see its objects
and definitions in the inspector, with **Compare with the database now**,
**Compare with…**, **Export**, **Rename…** and **Delete…**. A version read by
an account that may not see every object (no TRIGGER, EVENT or routine
privilege on the database) says **May be incomplete** beside its name; the
inspector says which kinds. The inspector's **Checks** section, read only when
you open it, looks for definitions worth a second look: money columns stored
as FLOAT, DOUBLE or REAL (judged by their names), utf8mb3, MyISAM tables, zero
dates as defaults, reserved words used as names (and words a later MySQL
release reserves), foreign keys without a matching index or parent key, and
definers whose account the server does not have. Each finding is **Watch**; a
check that could not be made (for example reserved words on MariaDB, or
definers without a connection to the same server) is listed under Details as
**Not judged** with the reason, never as clean. It uses the connection in use
for the live parts (the server's current reserved words, and definers when it
is the version's own server) and never changes anything.
`myflames schema lint` runs the same checks.

**Compare** takes a **Source**, the schema to match, and a **Target**, the
schema that would change. Each is a **Saved version** or a **Database now**,
read through the connection in use or any saved connection whose password is
saved, so you can compare the same database on two servers directly. Both
sides start filled in: the target is a database of the connection in use and
the source the newest saved version of that database (else the newest
version); a database read now offers the server's databases, so nothing is
typed. Before the first comparison Source and Target sit side by side as
compact cards (also in a 720 px window), each role's hint in its label's
tooltip. The swap button exchanges them. After a comparison they fold into the
same bar as in Parameters > Compare (Swap, **Change…**, Compare), which keeps
naming the sides the result came from until you compare again; a changed
choice says so and asks you to press Compare. **Ignore definers** leaves out views,
routines, triggers and events that differ only by their DEFINER. **Compare**
(the tab has no Refresh; Compare reads again) reads what it needs and lists
each object as **Different**, **Only in source**, **Only in target** or **Not
compared** (one side could not read its definition), with what the sync script
does to the target (created, dropped or changed); **Show identical** adds the
rest. These badges, and change kinds such as **Moved**, describe the
comparison, never the server's health: none uses the OK, Watch or Problem
colors, while Drops data, May fail and Rebuilds table keep their risk colors. **Include in sync script** has a checkbox on each object, and its header
chooses every object, the identical ones too. Objects only in the target start
unchosen, so the script drops none of them until you tick them (changes within
a chosen object can still drop columns or replace the object, and they carry a
`-- WARNING:` comment and **Drops data**); changing a choice
rebuilds the script from the comparison already read, without reading a server
(the previous script stays greyed until the new one arrives). **Risk** shows
**Drops data** (a dropped object or column, or a routine, trigger or event
created again) and **May fail** (a change that depends on the rows already
there, such as a new UNIQUE key, NOT NULL, a narrower type or another
character set) and **Rebuilds table** (the server is predicted to copy or
rebuild the table for this change), with the full sentence on hover and in the
object's detail under **Review before running**. The detail also shows, for
each ALTER TABLE the script would send, a **Predicted** line (INSTANT, INPLACE
with or without a rebuild, COPY, or not predicted with the reason) and **Why**
with each clause's reason and source; the prediction comes from the target's
captured definition and server version, never from a new read. When a change cannot be made because of an
object the choice keeps in the target (a kept table's foreign key, a
constraint name in use, a view that uses it), its Sync script column says
**Not generated** and Risk says **Needs** followed by the objects to tick;
ticking them rebuilds the script. Select an object to open it over the whole view (**Back to differences**
keeps the list, its sort and scroll): a sentence saying where it is and what
the sync script does, **What differs** as a table with the source's and the
target's text side by side (columns, keys, foreign keys, checks, options,
partitioning, definition, definer, `sql_mode`, trigger order), and both
definitions side by side with the lines that differ marked, or the one
definition for an object on one side only. **Compare with the database now**
on a saved version uses the version as the source and the database as the
target: what changed since the version, and a script that would bring the
database back to it.

**Sync script** holds the statements that would change the target to match the
source, with what to review first (statements that drop data, may change data,
check existing rows, drop routine privileges or rebuild a table, and what was
not generated).
Each risk is also a `-- WARNING:` or `-- CHECK:` comment right above its
statement, each ALTER TABLE has an `-- ALGORITHM:` comment with its predicted
algorithm (no ALGORITHM or LOCK clause is added), and the header says how many differences were left out and how many
objects only in the target were not dropped.
**Copy script**, **Download .sql** and **Open in Query editor** are the only
ways it leaves the place: myflames never runs it. **Open in Query editor** is
offered only when the target is a database read now through the server of the
connection in use (a saved version is a past state, not a database to run it
on). Through SSH the SSH host is part of that server: 127.0.0.1:3306 read
through one SSH host is another server than through another SSH host or
directly, and the reason says so. The Query editor runs each statement after
its own confirmation.
**Export** saves the comparison as Markdown, JSON or the sync script.

**Export as SQL…** (a saved version's inspector, and the Saved versions toolbar for a database of the connection in use, read now) opens a sheet with a script that recreates the schema on another server. **Name in the script** defaults to the source's name, **Create the database** adds `CREATE DATABASE`, and **Keep definers** keeps the DEFINER of views, routines, triggers and events (left out by default, so the account that runs the script becomes the definer). Tables come in foreign key order (keys among tables that refer to each other are added afterwards with `ALTER TABLE`), views in dependency order, and routines, triggers and events in `DELIMITER` blocks under the sql_mode, character set and collation they were created with. **Review before running** lists what the script cannot recreate as captured (`-- WARNING:` lines). The script only creates objects. **Copy script**, **Download .sql** and **Open in Query editor** (a new tab; nothing runs) are the only ways it leaves; a changed option rebuilds it from the read already made. CLI: `myflames schema export VERSION --sql`.

## Import CSV

Import CSV loads a .csv, .tsv or .txt file into a table of the connection in
use. Open it from the Query editor's Schemas panel (a table's menu:
**Import CSV…**; a database's menu: **Import CSV into a new table…**, user
databases only) or from Server > Tables (a table row's menu or its
inspector's **Import CSV…**). The target is already filled in. The item is
off with its reason on a Read-only connection, while the write policy is not
known, when SHOW GRANTS proves INSERT (or CREATE, for a new table) missing,
or when the list was read from another connection; a privilege that cannot
be verified leaves it on.

One sheet walks through five steps:

- **File and format.** **Choose file…** opens the native file dialog;
  **Type a path instead** takes a typed path. The **Separator** (Comma,
  Semicolon, Tab, Pipe or Other with one character), **First row has column
  names**, **Encoding**, **Quote**, **Empty fields are** (NULL or Empty
  string), **Treat \N as NULL**, **Treat the word NULL as NULL** and
  **Skip first lines** start at the values detected from the file, named as
  "Detected: semicolon"; every change reads the preview again (a check it
  replaces is stopped on the server too).
- **Target and columns.** **Into** chooses **Existing table** or **New
  table**. For an existing table each CSV column maps to a table column or
  Ignore, matched by header name, then position; **Match by name again**
  redoes the matching. For a new table the name comes from the file name and
  each column gets a name, a type (the inferred one first) and a NULL
  choice; **Add an id primary key** adds an `id BIGINT UNSIGNED
  AUTO_INCREMENT` primary key.
- **Check.** Every row is checked against the column types, and problems are
  listed with their line numbers. **Method** chooses **INSERT batches** or
  **LOAD DATA LOCAL**; LOAD DATA LOCAL stays off with its reason while the
  server has `local_infile` OFF, and myflames does not change that setting.
  **If something fails** chooses **Keep nothing** (one transaction, rolled
  back) or **Commit every N rows** (committed rows stay). **Bad rows**
  chooses **Stop** or **Skip and report**.
- **Review.** The review sheet shows the connection and its type, the exact
  CREATE TABLE of a new table (sent first, on its own), the data summary and
  the first statement, the column mapping, the privileges and the write
  policy. **Import** confirms; Cancel, Escape or closing the sheet records a
  declined attempt in the Audit log.
- **Import.** Progress shows rows and batches with the elapsed seconds, and
  **Stop** ends the import between statements (a LOAD DATA in flight is
  killed). The result reads Done, Failed, Stopped, Outcome unknown or
  Nothing sent, with the rows committed, sent and skipped, the failing line
  and the server error. After ten seconds **Close** hides the sheet while
  the import continues; the view keeps one line with **Show import** and
  Stop, then the outcome with **Show result** and Dismiss. A new table the
  rows could not fill stays, empty, and the result shows its DROP TABLE to
  copy and run by hand; myflames never runs it.

Every import is a reviewed and audited write: the review binds the file
(size, modified time and a checksum) and the target's metadata, both are
checked again before anything is sent, and the Audit log records the
statements and the row counts, never row data. An import whose reply is lost
reads **Outcome unknown** and is never sent again.

## API

The API is internal to the local workspace. A one-use launch fragment is
exchanged at `/api/auth/session` for a per-instance HttpOnly cookie and an
origin-scoped session proof. API POSTs require both the cookie and the proof in
`X-Myflames-Token`, with exact Host and Origin checks. The HTML page contains no
token. There is no CORS access. See the [authentication and limits contract](DEVELOPER_REFERENCE.md#local-authentication-and-request-limits).

| Endpoint | Request | Response |
| --- | --- | --- |
| `/api/teach/catalog` | Empty object | `lessons`, `families`, `curriculum` |
| `/api/teach/lesson` | `lesson`: registered lesson key; omit `view` or use `2d`; other values return HTTP 400 | `html`: original self-contained lesson |
| `/api/analyze` | `plan`: raw plan text | `analysis`, `operators`, `query`, `teach_hooks`, `chart_data` (`chart-data-2.0`, drawn in the page by the chart runtime). The SQL is sent once, in `query` and `analysis.query.raw`; teach hooks leave out `query_sql`, which Export JSON adds back |
| `/api/investigations/list` | optional `limit` (1 to 50, default 25), `cursor`, `search` | `investigation-list-1.0`: `items` (summaries, newest first, no plans) and `next_cursor` (null on the last page) |
| `/api/investigations/get` | `id` | the saved `investigation-1.0` bundle |
| `/api/render` | `plan`, `view` | `html`: standalone chart document (kept for compatibility; the workspace draws from `chart_data`) |
| `/api/report` | `plan`, `view`, optional `hide_literals` (default false), `query`, `format: "json"` and `analysis` | `html`: portable report, or `analysis` (the sidecar) with `format: "json"`; with `hide_literals` a redacted copy |
| `/api/compare` | `before`, `after`: raw plan text, optional `hide_literals` | `analysis`, `html` (matching always from the original plans) |
| `/api/app/update/check` | `force`: true from the Check for updates button | `app-update-1.0` with `checked_at`, `error` and `reason` on failure, and the stored `last_check` |
| `/api/app/update/status` | Empty object | `app-update-status-1.0`: `current_version` and the stored `last_check` (or null); never reads the network |

Workspace routing in `myflames/ui.py` (`api_response`) dispatches across dedicated subsystems:

- Browser authentication (`/api/auth/session`).
- Database diagrams (`/api/schema-diagram/read`, `/api/schema-diagram/import`, `/api/schema-diagram/export`).
- Session management (`/api/session/load`, `/api/session/save`, `/api/session/clear`, `/api/session/settings`).
- Local audit logging (`/api/audit/list`, `/api/audit/export`, `/api/audit/settings`, `/api/audit/declined`, and `/api/audit/object` for the change history shown in an object's inspector).
- Query editor sessions (`/api/editor/open`, `/api/editor/run`, `/api/editor/preview`, `/api/editor/status`, `/api/editor/stop`, `/api/editor/commit`, `/api/editor/rollback`, `/api/editor/close`, `/api/editor/use`, `/api/editor/file/open`, `/api/editor/file/save`, `/api/editor/history`, `/api/editor/export`).
- Server lifecycle tasks (`/api/lifecycle/tools`, `/api/lifecycle/upgrade-check`, `/api/lifecycle/prepare`, `/api/lifecycle/start`, `/api/lifecycle/status`, `/api/lifecycle/cancel`, `/api/lifecycle/jobs`).
- Users and roles (`/api/accounts/list`, `/api/accounts/read`, `/api/accounts/scope-objects`, `/api/accounts/plan`, `/api/accounts/apply`). The grant matrix supports all catalog privileges at their legal levels, dynamic grant options, role admin/default settings, anonymous proxy accounts, MariaDB package/package-body scopes and MySQL 9.2+ MLE library scopes. Packages and libraries accept exact names.
- Full analysis export (`/api/report/export`).
- Home news (`/api/news`, `news-1.0`, and `/api/news/settings` for the Settings switch); see [Session and scope](#session-and-scope).
- Saved profiles and investigations (`/api/profiles/*`, `/api/investigations/*`, `/api/connection/test`, which also returns `databases`, `/api/ssh/hosts` for the SSH host pick list, `/api/live/readiness` for the cached server facts on Home, and `/api/connections/import` for MySQL Workbench and DBeaver imports).
- Saved queries (`/api/editor/saved/list`, `save`, `rename`, `move`, `delete`; local only).
- Query editor settings (`/api/settings/editor`: the Settings popover's Query editor group, kept on this computer; local only) and the editor's system-variable names (`/api/config/versions/names`, answered offline from the shipped parameter version catalogs).
- Schema versions (`/api/schema-versions/*`, including `sync` for a new choice of objects and `lint` for Checks).
- CSV import (`/api/import/csv/choose`, `open-path`, `inspect`, `plan`, `apply`, `progress` and `stop`; apply is a reviewed write).
- Parameter snapshots and server comparison (`/api/config/snapshots/list`, `save`, `get`, `delete`, `/api/config/compare`, `/api/accounts/compare`, `/api/accounts/export`; each side reads with its own connection and nothing is executed).
- Baselines (`/api/baselines/list`, `save`, `get`, `delete`; Health and Query digest readings kept on this computer, tied to the server's identity; `list` probes the server's identity, and `save` verifies the server's identity in one bounded probe).
- Live diagnostics, Server views, and actions (`/api/live/*`, including `health`, `watch` and `watch/capture`, `innodb`, `error-log`, `top`, `index-suggestion`, `digest-histogram`, `digest-tables`, `table-structure`, `slow-log`, `sessions`, `sessions/history`, `ddl-progress`, `running`, `locks`, `kill/prepare`, `kill`, `waits`, `indexes`, `tables`, `io`, `memory`, `config`, `variable`, `instruments`, `replication`, `cluster`, `proxysql`, `readyset`, `privileges`, `catalog`, `schemas`, and `object-search`).

Detailed schemas and request/response payloads are documented in [QUERY_WORKSPACE.md](QUERY_WORKSPACE.md#api-additions).

The lesson catalog (`teach/catalog`) contains each lesson’s `key`, `title`, `summary`, and `family`.
`curriculum` lists lesson keys in the existing learning order. Lesson rendering
accepts registered keys only. Omit `view` (or send `"2d"` for compatibility);
other values return HTTP 400 with "Only the 2D lesson view is available."
Session restore uses the lesson key and ignores obsolete view preferences.

`analysis` uses the existing sidecar schemas without changing their fields.
`operators` is a UI projection of canonical parser nodes: `node_id`, `label`,
`depth`, `self_time_ms`, `total_time_ms`, `rows`, `loops`, and `details`. Depth
starts at zero. Measurements retain the parser's units and semantics.

Errors are JSON objects with an `error` string. Bad plans return 400; disallowed
origins, hosts, or tokens return 403; missing routes return 404; oversized requests
return 413; unsupported request content types return 415. Requests are limited
to 12 MiB normally; session save has a 65 MiB request limit for its bounded
64 MiB stored data. Account data has an additional 256 KiB budget. The server
admits at most 32 request workers, with socket and body-read time bounds.

Run the Python suite with `./run-tests.sh` and type-check/build the frontend with
`npm run build` in `apps/workspace/`. The browser smoke test is documented in the
workspace README.

The sidebar, Home, and browser tab share a simplified stacked-flame
[app mark](brand/README.md), derived from the original project logo.


### Users and roles

Open **Server → Security → Users and roles**. Opening the view reads accounts and roles
once per connection; Refresh retries a failed read. The list never displays password hashes; selecting a row reads its effective grants
and account attributes. The navigation badge counts only the loaded list.
Without access to the account tables, the list shows the connected account and
the backend's coverage warning. When details cannot be read, the summary says **Not judged**
with the reason and the list shows **Details: Not readable**, instead of repeated
Unknown values. Column choices are saved. Create user and Create role are in the toolbar;
Lock, Unlock, Expire password, Set password, Rename and Drop live in the
inspector action area.

Edits stay under Pending changes. Choosing another account asks for **Review changes**,
**Discard**, or **Cancel**. Review keeps the current account selected. Review changes shows the before/requested-after
direct-grant comparison, exact statements, warnings and any required typed name.
Compatible edits share a statement group. Every group appears in one review,
including mixed grant/revoke operations and scopes. Planning errors appear beside
the group and block Apply until corrected or removed. Protected accounts need
one typed name each. Apply checks and sends groups in the displayed order,
re-planning each with a fresh fingerprint. It stops at the first refusal,
changed safety check or failure, and shows applied, failed and unsent groups.
Earlier changes remain applied. The account is refreshed and the old draft
cleared after every outcome, including a lost reply. Cancel records one declined
audit per planned group; each applied group keeps its backend audit records.
Unticked boxes show privileges available at the selected scope. **Add scope**
adds a database, table, column, procedure, or function; exact names can be typed
when metadata is incomplete. Object lists are capped at 500 combined names.
At the Columns level the column is picked from the chosen table's columns, read
from the server once a database and table are chosen, with the same Enter an
exact name fallback and a message when they cannot be read. At the Proxy level,
Proxied user and Proxied host suggest the names and hosts of the loaded
accounts; free text stays for an anonymous user and wildcard hosts. For example, select a user, add database `shop`,
tick **CREATE ROUTINE**, choose **Review changes**, inspect the GRANT, and
**Apply**. Roles use the same workflow. Inherited privileges remain visible
without direct-edit controls. Catalog availability and legal levels are
separate from the connected account's authority to grant them.

Typed passwords are cleared after planning and re-entered for Apply: the
30-minute single-use plan handle stores no secrets. Generated passwords work on
MySQL and MariaDB and appear with Copy, labelled with their account and connection.
Results and passwords stay on their original connection until individually
dismissed, even if Apply finishes after closing its sheet or switching connections.
Return to that connection's Users and roles view to see them. Leaving the view
or making another change preserves them; reload and Start fresh clear them.
Account reports, drafts, plans,
passwords and results are never saved; restore keeps the selected view only.
The shared privilege provider reads on connection changes and stale window focus.
Accounts controls use its canonical action answers; other screens use the [privilege gating map](UI_TESTING.md#stage-b-gating-map).

**Compare** (the second tab, **Users and roles | Compare**) compares the
accounts and roles of a **Source** and a **Target**, each this connection or a
saved connection, without changing either server; it reads only when you press
**Compare**. The setup bar, Change… and the comparison badges work as in
Parameters > Compare. Accounts pair by exact user and host. Select one for what differs:
grants **Only in source**, **Only in target** and **In both**, roles, default
roles, the authentication plugin, locked and expired, TLS requirements,
resource limits and password policy; password hashes are never read into it.
Built-in system accounts are listed too, with a **System** pill. **Export grants as SQL**
exports the accounts you tick on the chosen side (**From**), or **All accounts
only in source** (which leaves System accounts out and says how many), with the roles they need, as `CREATE USER IF NOT EXISTS`
without a password, GRANT and SET DEFAULT ROLE statements. It reads that side
again, keeps the text in memory with **Copy** and **Download .sql**, says that
passwords are not included, and runs nothing; built-in system accounts are left
out unless ticked. The same work runs as `myflames accounts compare` and
`myflames accounts export`.

### Tables and indexes: browse and export

Indexes puts partitioned tables with unused-index candidates under **No data**, with the reason “partitioned, and Performance Schema does not count every read through a partitioned table's indexes”. DROP and visibility SQL are unavailable for those tables in All indexes, Redundant, row menus and inspectors. If partitioning cannot be read or a candidate table is missing from the metadata, its indexes also stay under No data with the reason and a note. The check reads only candidate tables, in batches of at most 100 within one database.

Tables includes an All tables inventory, and Indexes includes All indexes,
including used and primary indexes. Both paginate in groups of 25, 50, 100 or
200 rows. Sorting applies to the entire result before pagination. Search and
scope changes return to the first page.

Each list exports all filtered, sorted rows as CSV or standalone HTML. PDF
opens the system print dialog; choose Save as PDF. It repeats column headings
and wraps long names. Exports describe the report scope and include every
filtered row, not only the current page. CSV guards spreadsheet formulas;
HTML escapes server-provided text and contains no scripts.

The workspace requests complete scoped metadata rather than truncating at 200
rows. Existing schema-size consent still applies. Reports above 100,000 rows
in a category are refused with a request to narrow the database selection;
client-output limits also remain in force. CLI row limits are unchanged.

Reusable space in Tables is an InnoDB tablespace metric. Its minimum-percent
filter applies only where file-per-table ownership and all denominator values
are known. Shared or unknown tablespaces and missing sizes have no percentage.
This is not per-index fragmentation; Indexes does not invent such a metric.


### Connection and table interactions

Typing the session password sends no privilege read. **Set**, leaving the
password field or pressing Enter commits the password and reads privileges once; a successful
connect also refreshes them. A reconnect response arriving while you type cannot
commit the unfinished password. Privilege reads, including focus refreshes,
wait for you to commit it.

Parameters, Instruments and Users and roles read when opened. Changing the
connection while a report stays on screen keeps that report greyed until Refresh.
Opening the view again reads the chosen connection. Saved connection settings
always use the newest list response, including the write-safety footer.

Row menus keep a view's actions followed by Copy cell, and fit their actual height
inside the window. Shift+F10 or the context menu key opens the menu; Escape returns
focus to the row. Cancelling a resize ends the drag. Processlist lock rows carry
a lock or key icon with an accessible explanation as well as their theme colors.

Kill sessions preserves the query pattern exactly as typed and matches without
case sensitivity. An invalid expression says **Not a valid pattern** and selects
no sessions. Reviewed Indexes and Tables statements quote names containing
backticks correctly. Tuning and Full analysis share the Export menu, and an
analysis's elapsed time continues when Tuning is reopened during its run.

Plans sample opening and HTML export show progress immediately; reads can stop waiting, and export failures can retry the same input. HTML and JSON exports report **Saved** with the filename. Investigation Save shows **Saving…** and keeps its error in the sheet.

Maintenance's Detect again, Check for updates, Check upgrade and Start dump…, Start restore… and Start clone… (while their preflight runs) show elapsed progress with **Stop waiting**. A read failure offers **Try again**. Start and Cluster/ProxySQL confirmations offer **Close** after ten seconds without a reply: the request continues, its progress and outcome appear in the view, and nothing is sent again. Cancelling a job shows **Cancelling…** until the reply arrives; a missing reply does not mean it stopped.
