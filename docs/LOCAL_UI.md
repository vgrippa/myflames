# Performance workspace

The same workspace can run inside the optional Electron development app in
`apps/desktop`, currently targeting macOS. See [Desktop app](DESKTOP.md) for
setup. Compact navigation, open query documents and the command palette (⌘K)
are shared with the browser workspace. Server details use the full work area;
Back returns to the list with its selection, filters and scroll retained.

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

The sidebar separates where you go from what you have open:

| Place | What it holds |
| --- | --- |
| **Home** | Connections in a table beside one Recent work list of open plans, saved investigations and query tabs. **Add connection** is the primary action in the Connections toolbar. **Import plan** appears once, in the top bar, whether or not anything is open; the empty Recent work list offers **Explore sample plan**. Passwords show **Saved** or **Not saved**, and a connection without a default schema shows a dash. **News**, under Recent work (below it on a narrow window), lists up to six MySQL and MariaDB headlines with their source and age; each opens in your browser, **Refresh news** reads again, and one line says when news is loading, unavailable offline (with when it was last updated) or empty. The version line sits at the bottom of Home with **Check for updates**, which asks GitHub only when clicked and shows the outcome with the time of the last check (Not checked yet, Up to date, a newer version with **Download…**, or why it could not check; a result older than a day reads as the last result). About myflames has the same status and button. Dropping a plan file anywhere on Home imports it. **New query tab** belongs to the Query editor |
| **Plans** | The open plan with its charts, findings, and inspector. **Explore** and **Compare**, in the top bar beside the place name, switch between one plan and two plans side by side; **Import plan** is at the end of the top bar. One plan header holds the plan's name, the stats (time, rows examined, rows returned, findings), **Plan summary** (or **Investigate first** when a finding leads), **SQL** with **Copy SQL**, and **Export**; Plan summary and SQL each open a panel over the chart with one click (a click elsewhere or Escape closes it). The chart with its own toolbar and the inspector fill the rest of the window; findings and execution context follow below. **Save investigation** opens a sheet with the notebook (investigation name, plan name, baseline, tags, notes and conclusions, Export investigation bundle) and saves with **Save**; **Close** keeps edits as unsaved changes |
| **Query editor** | SQL tabs that run any SQL in a session of their own (Run, Run all, Stop, results per result set, Messages, export, transactions), Explain and Run analysis, the schema browser, the run history, and the connection in use |
| **Query digest** | Right below the Query editor: the statement digests of the connection in use profiled in the style of pt-query-digest (ranked by total time, full scans, joins without an index, temporary tables on disk, lock time, or slow queries), with Export. A row's **Open in editor** and **Explain** open the Query editor, which runs them. See [Query digest](#query-digest) |
| **Database diagram** | After Query digest: the tables, columns and relationships of a database, read from the connection in use or imported without executing SQL, with a Diagram \| Mermaid view. See [Database diagram](#database-diagram) |
| **Server** | Views of the connection in use that only read (the exceptions are **Kill** in Locks or Sessions, which ends one session after you confirm and needs the privilege, parameter changes in **Parameters**, runtime instrument or consumer updates in **Instruments**, a dump, restore or clone in **Maintenance**, and account changes in **Users and roles**, each after its own confirmation), in a grouped sub-navigation: **Overview** (Health, which opens first, InnoDB, Error log), **Contention** (Locks), **Schema** (Indexes, Tables), **Setup** (Parameters, Instruments, Maintenance), and **Security** (Users and roles). See [The Server place](#the-server-place) |
| **Workload** | The same kind of views for the statements and resources: **Queries** (Top statements, Processlist) and **Resources** (Waits, I/O) |
| **Replication** | Replication channels or connections of the connection in use, with a skip after its own confirmation. A server with no asynchronous channel reads **Not a replica** (never Unknown), and the item is greyed in the sidebar once a read says so |
| **Cluster** | The MariaDB Galera Cluster, Galera Cluster for MySQL, Percona XtraDB Cluster or MySQL InnoDB Cluster (or Group Replication) the connection in use belongs to: members, roles, state, quorum, failures tolerated and routers, with a member's changes (Make primary, Stop or Start Group Replication, Desync, Resync, Bootstrap a Primary component) in its inspector, each after its own confirmation. Greyed in the sidebar once the connection is known not to be part of a cluster |
| **ProxySQL** | The ProxySQL Admin interface the connection in use reaches: backends as ProxySQL runs them with their connection pool, hostgroups, query rules and query digests, with a server's or rule's changes in its inspector and Load and Save to disk on the Configuration line, each after its own confirmation. Greyed in the sidebar once the connection is known not to be a ProxySQL Admin interface |
| **Tuning** | A place of its own (for MySQL and MariaDB; the command palette also finds "mysql tuning"): the tuning scorecard full width, without Server's list of views, and the **Full analysis** as its second tab. Links to the Full analysis open it there |
| **Investigations** | Saved investigations: search, open, delete |
| **Learn** | The lesson catalog and player (`myflames teach` on the command line) |

The app follows the system light or dark appearance. Check chart labels,
Compare highlights, tables, dialogs, and Audit status colors in both appearances;
status text and icons remain available alongside color.

### The Server place

Every Server view has the same anatomy, after the Apple apps DBAs already know
(Activity Monitor, System Settings, the Finder inspector):

- **Sub-navigation.** The Server views are grouped by job: Overview,
  Contention, Schema, Setup, Security (Workload groups Queries and Resources in its own place). Each item shows a marker from data
  you already loaded (Health and Parameters show their worst check status, Locks
  the blocked sessions, Sessions the active ones, Indexes what to review). One
  system everywhere, the sidebar places included: a small dot for state (red
  for a problem, amber to watch, a gray ring when unknown or off, nothing when
  OK) and a neutral count beside it, so color always means state.
  Nothing is read just for a badge. A view reads when it is opened on screen,
  once per connection (Refresh retries), Top statements and Error log
  included; Tables and Indexes keep their schema-size consent, and nothing
  reads off screen (Server, Replication, Cluster, ProxySQL and Query digest
  mount on their first visit, and a hidden view never reads on its own). The arrow keys move between items.
- **The list of views floats** by default in a wide window, so the view gets
  the full width: the view's title is a pop-up button ("Health ▾") that opens
  the list under it, with the same groups, icons and markers. Enter, Space or
  Down opens it on the current view; the arrow keys, Home and End move; Enter
  or Space chooses (the list closes and the chosen view's title has focus);
  Escape or a click outside closes it and returns focus to the title. While
  the list is closed, a dot beside the title shows the worst state among the
  other views (red for a problem, amber to watch, nothing when all are fine),
  and screen readers hear it in words ("Health, 1 other view has a problem");
  counts stay in the list. **Dock the list of views** (in the list's header or
  the top bar) keeps it beside the view instead, and the choice is remembered;
  docked from the list's header, focus moves to the top bar's toggle, which
  now reads **Float the list of views** and floats it again. Workload works
  the same way. In a narrow window the list is a screen of its own, as below.
- **Toolbar.** The view's title with an ⓘ that explains it, its controls (a
  sampling window, auto-refresh, a database picker, a filter) and **Refresh**.
  List controls wait on the list while details are open. Refresh stays at the
  end of the toolbar, also on a detail page. The place's own controls follow the
  connection picker in the top bar: **Refresh all** (a text button, so it never
  reads as a second copy of a view's Refresh), an ⓘ with what Server does, and
  one control for the list of views (dock or float it; in a narrow window a
  **Views** menu instead).
- **One summary line**, with the status and a **N notes** button for engine
  differences, privileges, and caveats.
- **A table or one-line rows.** Tables sort by any column, keep the header in
  view, and scroll sideways inside their frame: an edge with more columns
  beyond it fades out, and the Columns menu column at the right casts a shadow
  over the column continuing beneath it, so a cut column reads as scrolled.
  Columns that start from a fixed width wider than their values give that room
  back before anything truncates. Rows take the arrow keys (and
  Page Up, Page Down, Home, End); Enter or a click opens the inspector, Escape
  closes it and returns to the row. Large counts read compact (159.0B, 14.2M)
  with the exact value on hover and in the inspector; a long name truncates
  (full text on hover) and its flags stay beside it. Tables with more than 300
  rows draw only the rows in view. In a status column only what needs a look
  is drawn: Problem and Watch show a dot and the word, No threshold a faint
  dash, and an OK row leaves the cell empty (screen readers still hear OK).
  A metric tile without a threshold draws no marker at all.
  Metric tiles in a group all have the same width: up to six share one row,
  and a row that does not fit becomes balanced rows of three or two.
- **Columns.** Drag a header's right edge to resize any column, or focus the
  edge and use the Left and Right arrow keys (Shift for bigger steps, Home and
  End for the narrowest and widest); double-click the edge to fit. The
  **Columns** button at the right end of the header lists every column with a
  checkbox to hide or show it (the last visible one stays) and **Reset
  columns**. Widths and hidden columns are kept per table with the workspace.
  So that the widest lists fit beside the list of views, a few columns start
  off and stay in the Columns menu, with their values in the details: Flags in
  Processlist (the Status cell names them on hover), Rows sent and Schema in
  Top statements, and Lock structs and Isolation in Locks transactions.
  Processlist rows have no buttons: each row's ⋯ menu holds **Show plan** and
  **Kill…**, which opens the same confirmation sheet as Kill in the details.
  Right click on a row (or Shift+F10, or the context menu key) opens its
  menu: Show plan, Open in Query editor, Open in Locks, filters, **Copy
  query**, **Copy connection ID**, then **Kill query…** and **Kill
  connection…** for that row. Each opens the same sheet with that KILL
  chosen. An item that cannot run stays in the menu with its reason under it
  (a read-only connection, settings still loading, a missing privilege, a
  server thread, a session with no statement to stop). While the KILL is sent
  the sheet's button says so; the answer then shows above the list at once
  (Done, Already ended, The server refused, Outcome unknown, or Nothing was
  sent) while the list is read again.
  **Kill sessions…** in the Processlist toolbar opens the Kill sessions
  dialog: presets (Runaway queries, Idle sleeping, Lock waiters, All clients)
  and filters choose client sessions (system threads and your own connection
  are never listed), and Kill N queries or Kill N connections shows the exact
  KILL statements and the server in a confirmation sheet that starts on
  Cancel. Nothing is sent until you confirm, the connection's type and
  read-only setting are read again first, and Cancel is recorded as declined
  in the Audit log. On a read-only connection the dialog says so and its Kill
  button stays off. Group Replication data-stream connections are marked as
  replication system threads and protected from Kill, including when MySQL
  reports their command as `Group Replicatio`; the reason points to Cluster.
  Indexes and Tables use the same ⋯ row menu (Set invisible…, Drop index…,
  Table health; Add column…, Remove column…, Optimize, Analyze and Check
  table…, View indexes), with the labels of the row's right-click menu; an
  item that opens a review ends in an ellipsis. A row's ⋯ shows on the row being pointed at, the
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

**Refresh all** (top bar, after the connection picker; ⌥⌘R or Alt+Ctrl+R) reads every Server section,
including sections not yet opened, using each view's window, databases and scope.
At most three section reads run at once, with Health, Waits and I/O first.
Full analysis runs in a separate phase because it manages its own bounded reads.
Maintenance detects tools; it never starts upgrade checks or jobs. Existing
large-schema consent applies only to the same connection and scope.
While it runs the button becomes **Stop**
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
was written when this server started), from at most its last 1 MB. For a remote server it says where the log is and
how to read it on the server host. It is not part of Full analysis yet.

**Full analysis** (the second tab in the Tuning place) runs every Server view once on request, the
MySQLTuner-style checklist that `myflames report` prints, implemented with
myflames' own modules. **Run full analysis** takes about 10 to 20 seconds and
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
caches, and Security. A measured check counts 100 when OK, 50 to watch and 0
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
above. **Export** saves the scorecard as HTML, Markdown or JSON.

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
and parameters whose apply type is unknown are not editable in the table.
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
run, so check the value before trying again. Cancel or Escape sends nothing and
records a declined attempt for every planned row in the [Audit log](#audit-log).

The inspector has one compact section per parameter: a one-sentence
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
name, are **Edit
value**, **Reset to default** (stages `DEFAULT` for MySQL numeric and boolean
dynamic parameters), **Change…** (the single-parameter sheet with every mode,
including **This session only**, which opens the planned `SET SESSION`
statement in the Query editor without running it or recording a declined audit
on cancel), and **Reset persisted value…** (MySQL, when persisted). The Change
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
details and the inspector's **Source version** row. The inspector shows the threads, the last error once (code, time,
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
  and version, its state as a word beside its shape (a check for OK, a dot for
  Watch, a triangle for Problem, a ring for not judged), its queues (or this
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
own node's state, so the other nodes read **In the component**: their state
and queues are read on that node, so connect to it to inspect them. Percona
XtraDB Cluster names every node's state, and Group Replication every member's
state and role. Connected to a member that is offline or in error, the place
says so, claims no group size, lists the other instances as **Not visible
here**, and marks the routers' check-in as this member's copy, which may be out
of date: connect to an ONLINE member to judge the cluster. A server that is
not part of a cluster shows a calm **Not part of a cluster** with the reason;
from then on the sidebar item looks disabled (faint text and icon, tooltip
"This connection is not part of a cluster"), also while it is open, but stays
focusable and opens the place. Before the first read, and for another
connection, it looks like any other place. Choosing another connection while
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
  hostgroups, status, weight, connections in use and free, connection errors
  since ProxySQL started, queries and ping. A writer the Galera monitor keeps
  above `max_writers` reads **SHUNNED (standby writer)**: it is ONLINE in the
  backup writer hostgroup. The inspector shows the runtime row, the connection
  pool, the configuration row it comes from (a monitor may run it in another of
  its hostgroups) and the monitor.
- **Hostgroups**: each hostgroup in runtime with its role and how many servers
  are in each status.
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

ProxySQL keeps three copies of its configuration: memory (what Admin edits),
runtime (what it uses) and disk (what it loads at its next start). The
**Configuration** line above Servers and Query rules says whether memory
matches runtime and disk, with **Load … to runtime…** and **Save … to disk…**.
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
follows the send: the button reads **Applying…** and cannot be pressed twice,
Cancel and Escape wait, a line names the step (reading the target again; sent
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

**Maintenance** (Setup, last) has five tasks in a segmented control. **Tools**
(**Detect tools**) lists the MySQL and MariaDB tools on this computer
(mysqlsh, mysqldump, mariadb-dump, and the rest) with their versions and what
they offer for the connection in use. Each tool links to its official download page. Detect tools and Refresh also check vendor release metadata; newer versions appear beside the installed version. Client tools are compared within their installed series, while MySQL Shell uses its latest GA release. Checks can be cached for an hour; failed checks are cached for a minute and remain unknown. No installer runs automatically. **Upgrade check** runs MySQL Shell's
upgrade checker for a target version you choose, or a **Partial check
(native subset)** without it, labeled as such, which never says an upgrade is
safe; findings are rows (Error, Warning, Notice, Passed, Not checked, Check
failed) with the objects and documentation in the inspector, and **Export
JSON**. MariaDB gets the mariadb-upgrade steps to run on the server host.
**Dump**, **Restore** and **Clone** each have a short form and **Run
preflight…**, which only reads: failed checks stay on screen with their
reasons and nothing can start; a clean preflight opens the confirmation with
the target, the exact commands, the warnings, and the checks. A dump goes into
a folder on the computer running myflames. **Choose folder** opens its system folder dialog on macOS or Windows; Linux uses Zenity when available. You can also type the path. Cancelling leaves the existing path unchanged, and choosing a folder starts no backup. The folder must not exist yet or be empty (a folder with files in it is refused at the preflight); restore and clone also ask you to
type the target. The confirmation goes stale after a minute (run the preflight
again), and myflames repeats the preflight right before starting. A running
job shows its progress, the last output line, and **Cancel**; finished jobs
show the verification (passed, failed, or not checked, with the reason) and
the dump manifest. Jobs belong to the myflames process and to the connection
they ran on: after a switch they stay greyed until you switch back, a reloaded
page finds them again, and a Start whose reply was lost is found in the job
list instead of being sent twice. A running job is also named above any other
Server view with **Open Maintenance**. The Dump form puts each label left of
its control (rows stack below 650 px): **Scope** (Whole instance or Databases),
**Databases**, and once exactly one database is chosen, **Tables**: its tables
are read then (name, engine, the server's estimated rows and size; 200 at a
time with **Load more tables**) into a searchable list whose default is **All
tables**. Ticked tables make a table dump of exactly those names; a table that
a **Reload list** no longer shows blocks the preflight until you choose again,
and the preflight itself fails for a name the database does not have. A dump
uses MySQL Shell when it is offered, or the engine's SQL dump tool when you
choose it (the Tool list names it). **Options for** the tool are one form in
sections, with a line under the heading summarizing what differs from the
tool's defaults: **Performance** (MySQL Shell: Threads, Chunking, Chunk size,
Rate limit per thread), **Contents** (Include Triggers, Stored routines and
Events; a MySQL Shell table dump has no routines or events), **SQL output** (SQL
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
**Restore workspace on startup** (passwords are never part of the workspace),
**Show MySQL and MariaDB news on Home** (on by default; off hides News and stops
every news read; its hint says the headlines come from Planet MySQL and the
MariaDB Foundation, read while Home is open), the **Audit log** (see
[Audit log](#audit-log)), and **About myflames**, a window
with the version, the license, one privacy line (myflames contacts only your
database servers, official release pages when you check for updates,
and news feeds when shown and turned on), and the credits (Brendan Gregg's
FlameGraph and Tanel Poder's SQL Plan FlameGraphs). Close is its default button, and in a
short window its text scrolls while Close stays in view. The
top bar names the place once (Server and Workload name the view in the view
header, Plans names the plan in the plan header, and Replication, Cluster,
Tuning, Query digest and Database diagram, one view each, name the place in their view header only) and has one sidebar
toggle. With plans open it also shows **Save investigation** with **Unsaved
changes** or **Investigation saved**. The top bar holds no status badge and no
privileges button: on the places that send to a connection it shows the sidebar
toggle, the place, the connection picker, then on Server and Workload **Refresh
all**, an ⓘ about the place and the control that docks or floats the list of
views (a **Views** menu in a narrow window), and **Search…**.

The connection picker's menu ends with **Account and privileges…** and **Manage
connections…**. Account and privileges opens a sheet for the connection in use:
the account new sessions connect as, the note that an editor tab can have
different active roles, the active roles, the grants, the denied and
unverified actions with their reasons, and **Refresh privileges**. Close is its
default button, Escape closes it, and focus returns to the picker. The command
palette opens the same sheet as **Account and privileges** (keywords include
privileges, grants and roles) from any place. Greyed actions keep their reasons
where they appear, in their tooltips, descriptions and review dialogs. The sidebar keeps one width in every place.
The light appearance uses a white window, like the dark one uses its content
color: gray is for the sidebar and side panes only.

The command palette (⌘K or **Search…** in the top bar) lists the places in the
sidebar's order, then what belongs to one of them (Full analysis under Tuning,
**Compare** under Plans), the Server and Workload views, and actions such as
Import plan and the Audit log. When you type, commands whose name contains
every word come first, then those that match only a keyword or their group,
each in that same order: **innodb** opens the InnoDB view, not the Cluster
place, and **health** opens the Health view, not Tuning.

Use the Up and Down arrow keys to move between places, and Home or End to jump
to the first or last. Below 650 pixels the sidebar becomes a compact bar with
the status, Settings, and a **Menu** button that opens the places and open plans
in a drawer; Escape closes it.

### Window status bar

Across Plans, the Query editor, Server, Workload, Replication, Cluster and Tuning, one
window status bar at the bottom of the viewport shows the state of what is on
screen. On the places that send to the connection in use it starts with that
connection's write safety, a dot plus words: **Production** · changes ask
first, **Read-only** · changes refused, **Development** · risky changes ask
first, or **Production assumed** · reading settings / settings not loaded (with
**Retry**). Its tooltip and accessible name are the full sentence ("Production:
every change on NAME asks first"). Then come the database engine and version,
the connection, session state, the last run and refresh status. The Query
editor's transaction state stays in its toolbar, and the rows and time of a run
in its result tabs, so the footer does not repeat them. On phones it stays one line at the bottom: the
safety state comes first and stays visible, and later facts scroll inside the
bar. Home, Plans, Investigations, Learn and the Audit log show no safety state.

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
  that opened it.
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

On phone and narrow screens (below 720 px):
- Flame graphs, execution trees and bar charts are redrawn for the width of
  their pane. Execution trees and bar charts shorten operator names (the full
  name shows on hover and in the inspector) and keep their numbers; below
  about 480 px (360 px for a flame graph) they scroll horizontally at their
  full text size. Swipe sideways or focus the chart and use arrow keys.
- Toolbars and segmented controls wrap onto multiple lines without clipping
  buttons or dropping controls offscreen.
- The sidebar collapses into a compact bar with a **Menu** drawer.
- Below 650 px the connection pill gives up width first (its dot and a few
  characters of the name stay, the rest ends in an ellipsis). In the Server and
  Workload top bar, which also holds the place's own actions, this starts below
  800 px (the 720 px window), and the pill leaves the connection type to its
  dot, its accessible name and the window footer, keeping the connection name
  readable.
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

## Connections

Home's **Connections** table shows the name, type and Read-only in one tag,
user@host:port, default schema, saved-password state, and Last used. The accent
bar marks the connection in use, also named for screen readers. Last used covers
choices made during this window; earlier use reads **Not recorded**. A search
field appears from six connections. Columns can be resized or hidden through
the shared table controls. Column choices last while Home is open.

Click to select a row. Double click, press Enter, or choose **Use** from its
row menu to use it. Checkboxes, Cmd/Ctrl click and Shift click select several
rows; arrow keys move between rows, and Shift with an arrow extends selection.
The row menu contains **Use**, **Edit**, **Duplicate**, **Test**, **Forget
password**, and **Delete…**. Duplicate opens a copy of the settings with no
password. **Add connection** and **Edit** open an inspector beside the table,
or a sheet on a narrow screen, with Test and Save. TLS settings stay collapsed.
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
password field appears there; **Use a different password** replaces a saved one
for this page. A typed password is never saved in the workspace session.

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

The window footer of the Query editor, Query digest, Database diagram, Server,
Workload, Replication, Cluster, ProxySQL and Tuning starts with the write safety
of the connection in use: a purple dot and
"Production · changes ask first" (the full sentence, "Production: every change
on NAME asks first", is its tooltip and accessible name), an outlined dot and
"Read-only · changes refused", or a teal dot and "Development · risky changes
ask first". Nothing is drawn under the top bar, so the top bar and view
headers stay clear. The picker has a dot of the same color (purple Production,
teal Development, an outline when read-only) and the PRODUCTION tag. The
server enforces the type on every write; the footer and the dot only show it. Saved connections are read
again whenever the window comes back and right before a Kill is sent, so a
type or read-only change made in another tab or with `myflames profiles set`
shows up; a Kill confirmed under the old setting sends nothing. While they
are loading or cannot be read, the footer says **Production assumed** (with
**Retry** after a failure) and Kill stays off. A Development choice in the form holds only for
the host, port, user and TLS settings it was made with.

## Audit log

**Settings > Audit log** opens the record of every change myflames sent to a
server, kept on this computer (`~/.myflames/workspace.sqlite3`, never a
password): when, which connection and its type, the operation, the statements
as stored with secrets redacted, the status, the error and the duration. It is
a screen of its own, not a place, so no place is highlighted while it is open.

| Status | Meaning |
| --- | --- |
| **Done** | Sent, and the server answered without an error. |
| **Failed** | The server or the connection returned an error (the code and message are listed). |
| **Outcome unknown** | Interrupted, timed out or disconnected after the statement may have reached the server; check the server. |
| **Not finished** | Recorded before sending, but myflames stopped before it recorded the result; the statement may have run. |
| **Refused** | myflames did not send it: a read-only connection, or the target changed or ended. |
| **Declined** | Cancelled in the confirmation (Cancel or Escape); nothing was sent. |

Filter by connection, status, and period (**Since**); the server applies the
filters and lists the newest 500 matches, and **Load 500 older** brings the
next ones. Select a record for the full detail in the inspector: the
statements, the connection, the result, the identity you confirmed, what was
checked before sending, and the write policy. **Export** saves every record
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
4. Search the operator list and select an operator to inspect its measurements
   and raw fields. A plan with findings opens the inspector on **Findings**
   until you choose an operator. Findings with a matching operator include an
   inspection link.
5. Choose **Export** (export icon and a pull-down) and then **HTML report** for
   a portable report or **JSON** for the existing versioned analysis sidecar.
   The same Export pull-down saves Tables and Indexes inventories as CSV, HTML
   or PDF.

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
**Fit**, like the Database diagram). Search within the chart and select a node to update
both its chart details and the workspace operator inspector. HTML export keeps
the interactive view. The percentage shows the current zoom; click it to reset
to 100%. See [Visual Explain](WORKBENCH_VIEW.md) for measurement
semantics and differences from Workbench.

## Compare plans

Import two plans, choose **Compare** in the Plans bar, and choose the before and after versions.
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
"Step n of 16".

An open lesson has one compact toolbar (**All lessons**, **Previous lesson**,
**Step n of 16**, **Next lesson**, **2D | 3D** when available, **Download
lesson**) that stays under the top bar while the lesson scrolls, and fills
the window edge to edge like every other place; the top bar shows the lesson title
(lesson titles use a colon, such as "B+tree lookup: how InnoDB finds a row").
The workspace adds `data-embedded="true"` to the lesson page, which hides its
own header and footer. Topics outside the learning path can be opened directly
from the catalog, and **Download lesson** saves the self-contained HTML for
offline use.

Lesson pages and the standalone catalog (`myflames teach --index`, which
includes a lesson filter) use the workspace's light and dark tokens
(`prefers-color-scheme`, `data-theme` overrides, and `?theme=` / `?embedded=1`
URL parameters), restyled controls, and semantic `--svg-*` drawing colors
resolved by `anim.cssVar` and `anim.palette`, so drawings redraw when the
appearance changes and keep their meaning (descent path, arrival pulse, PK hop,
build vs probe, hit, spill).

Lessons with a 3D view (today the B+tree lesson) show a **2D | 3D** switch in
the lesson toolbar. 2D is the default and is unchanged. The 3D view draws the
index at its real size, one tile per page, in four chapters: the tree's shape,
a primary key lookup, a secondary index lookup, and a range scan. Drag to turn
the view; **Follow the query** brings the camera back to the animation. It
needs WebGL; without it the narration and numbers still work. **Download
lesson** saves whichever view is open (`btree-3d.html` for the 3D view).

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
myflames contacts only official release pages when you check for updates
(**Check for updates**, or **Detect tools** in Maintenance) and the two news
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
clipboard), the schema browser and the run history are described in
[Run SQL in the Query editor](QUERY_WORKSPACE.md#run-sql-in-the-query-editor).
Results stay in page memory: session restore keeps the tab's text, caret,
row limit and pane sizes, never results.
Connections store settings (the API and CLI call them profiles). When you save
one with a typed password, myflames asks whether to save the password too; it is saved only if you choose
**Save with password**, and then in the operating system's credential store or
an encrypted local file, never in the workspace database or the saved session.
Each saved connection is Production (asks before every write, the default) or
Development (asks only before risky writes), and can be read-only (refuses
every write); every write attempt goes into a local audit trail
(**Settings > Audit log**, or `myflames audit`). See
[Production, Development, Read-only](#production-development-read-only) and
[Write safety](../README.md#4-write-safety-model-and-audit-trail).
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

Top statements opens a selected statement in a detail view using the available width. Back returns to the retained list with its sorting, filters and scroll position. Supported write statements offer a read-only SELECT projection for review before Explain or Run analysis. The original editor text remains in its own tab; the converted SELECT needs a second explicit action. This is a different plan and does not measure writes, UPDATE assignment expressions, triggers, constraints or locking. Batch analysis leaves these rows for individual review.

Tables shows reusable InnoDB free space as information. **Optimize table** is an optional action with exact SQL and rebuild caveats to review. Instruments reads on open (one attempt per connection) and organizes its view into **Features**, **Instruments**, **Consumers**, and **Actors and objects** tabs: filter and select up to 2,000 instruments (with **Enable**, **Enable without timing**, **Disable**, and **Clear selection**) or consumers (enabling required parent consumers automatically), and run batched `UPDATE` statements (up to 500 names per statement) through the reviewed SQL runner, with copy-only option file lines under **Keep after restart**. Both require confirmation on Development and Production connections, refuse Read-only connections and remain disabled for stale data from another connection. Copying statements still works independently.

## Query digest

**Query digest**, right below the Query editor in the sidebar's SQL workspace
group (and in the command palette), profiles the statement digests of the
connection in use in the style of pt-query-digest. It reads when it is opened
on screen, once per connection; **Refresh** reads again, and choosing another
filter or row count reads that list. The filters rank by total time, full
scans, joins without an index, temporary tables on disk or lock time, or list
slow queries (over 1 s). **Export** saves a pt-query-digest style text report
or CSV. A row's **Open in editor** and **Explain** open it in a new Query
editor tab, which runs it (the editor's own checks and confirmations apply).
It is not a tab of the Query editor. It fills the window between the top bar
and the footer, so the list of statements and the inspector scroll on their
own however many rows are read. Start fresh, or another window's newer
workspace, starts it over: its search and selection are cleared and it reads
again.

## Database diagram

Open **Database diagram** in the sidebar's SQL workspace group (after Query
digest; Home's Database diagram card and the command palette open it too) to inspect a database's tables,
columns, keys, and foreign-key relationships. Choose a database from the connected server and select **Rebuild from database** to read fresh metadata. Rebuilding retains positions for tables that remain and incorporates added or removed tables and relationships. Replacing an imported diagram asks for confirmation first; cancel leaves it intact. You can also import SQL (`.sql`), Mermaid (`.mmd` or `.mermaid`), or editable JSON
without a database connection. Imports are parsed as data and never executed.
Files and models are limited to 2 MiB; a model holds at most 200 tables,
10,000 columns, 1,000 relationships, and 20,000 index key parts. Unsupported input and incomplete metadata produce
warnings; inspect them before treating a diagram as a complete schema. SQL
import supports CREATE TABLE and ALTER TABLE ADD keys and foreign keys. Mermaid
uses an erDiagram subset and reads column names and nullability back on import. Other clauses or statements are reported rather than
executed. Live metadata reads share a 30-second budget. **View Mermaid** opens an
ER diagram from a pasted definition or file directly on the interactive canvas.

A **Diagram | Mermaid** switch toggles between the interactive ER canvas and the
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
key. Mermaid cardinalities are retained as modeling information.

Export SQL or Mermaid after reviewing any loss warnings, editable JSON to keep
the model and table positions, or standalone SVG to share the drawing. Export
does not apply schema changes. The diagram and layout stay in memory and are
not part of session restore; save JSON to continue later. The place has no
Refresh all of its own: Server's Refresh all reloads live diagrams and can
build the selected database before the first diagram load, and opening the
place while it reads keeps that answer. Imported diagrams remain unchanged.


## API

The API is internal to the local workspace. A one-use launch fragment is
exchanged at `/api/auth/session` for a per-instance HttpOnly cookie and an
origin-scoped session proof. API POSTs require both the cookie and the proof in
`X-Myflames-Token`, with exact Host and Origin checks. The HTML page contains no
token. There is no CORS access. See the [authentication and limits contract](DEVELOPER_REFERENCE.md#local-authentication-and-request-limits).

| Endpoint | Request | Response |
| --- | --- | --- |
| `/api/teach/catalog` | Empty object | `lessons`, `families`, `curriculum` |
| `/api/teach/lesson` | `lesson`: registered lesson key; optional `view`: `2d` (default) or `3d` when the catalog lists it | `html`: original self-contained lesson, or the prebuilt 3D page |
| `/api/analyze` | `plan`: raw plan text | `analysis`, `operators`, `query`, `teach_hooks`, `chart_data` (`chart-data-2.0`, drawn in the page by the chart runtime). The SQL is sent once, in `query` and `analysis.query.raw`; teach hooks leave out `query_sql`, which Export JSON adds back |
| `/api/investigations/list` | optional `limit` (1 to 50, default 25), `cursor`, `search` | `investigation-list-1.0`: `items` (summaries, newest first, no plans) and `next_cursor` (null on the last page) |
| `/api/investigations/get` | `id` | the saved `investigation-1.0` bundle |
| `/api/render` | `plan`, `view` | `html`: standalone chart document (kept for compatibility; the workspace draws from `chart_data`) |
| `/api/report` | `plan`, `view` | `html`: portable report |
| `/api/compare` | `before`, `after`: raw plan text | `analysis`, `html` |
| `/api/app/update/check` | `force`: true from the Check for updates button | `app-update-1.0` with `checked_at`, `error` and `reason` on failure, and the stored `last_check` |
| `/api/app/update/status` | Empty object | `app-update-status-1.0`: `current_version` and the stored `last_check` (or null); never reads the network |

Workspace routing in `myflames/ui.py` (`api_response`) dispatches across dedicated subsystems:

- Browser authentication (`/api/auth/session`).
- Database diagrams (`/api/schema-diagram/read`, `/api/schema-diagram/import`, `/api/schema-diagram/export`).
- Session management (`/api/session/load`, `/api/session/save`, `/api/session/clear`, `/api/session/settings`).
- Local audit logging (`/api/audit/list`, `/api/audit/export`, `/api/audit/settings`, `/api/audit/declined`).
- Query editor sessions (`/api/editor/open`, `/api/editor/run`, `/api/editor/status`, `/api/editor/stop`, `/api/editor/commit`, `/api/editor/rollback`, `/api/editor/close`, `/api/editor/history`, `/api/editor/export`).
- Server lifecycle tasks (`/api/lifecycle/tools`, `/api/lifecycle/upgrade-check`, `/api/lifecycle/prepare`, `/api/lifecycle/start`, `/api/lifecycle/status`, `/api/lifecycle/cancel`, `/api/lifecycle/jobs`).
- Users and roles (`/api/accounts/list`, `/api/accounts/read`, `/api/accounts/scope-objects`, `/api/accounts/plan`, `/api/accounts/apply`). The grant matrix supports all catalog privileges at their legal levels, dynamic grant options, role admin/default settings, anonymous proxy accounts, MariaDB package/package-body scopes and MySQL 9.2+ MLE library scopes. Packages and libraries accept exact names.
- Full analysis export (`/api/report/export`).
- Home news (`/api/news`, `news-1.0`, and `/api/news/settings` for the Settings switch); see [Session and scope](#session-and-scope).
- Saved profiles and investigations (`/api/profiles/*`, `/api/investigations/*`, `/api/connection/test`).
- Live diagnostics, Server views, and actions (`/api/live/*`, including `health`, `innodb`, `error-log`, `top`, `sessions`, `running`, `locks`, `kill/prepare`, `kill`, `waits`, `indexes`, `tables`, `io`, `config`, `variable`, `instruments`, `replication`, `cluster`, `privileges`, `catalog`, and `schemas`).

Detailed schemas and request/response payloads are documented in [QUERY_WORKSPACE.md](QUERY_WORKSPACE.md#api-additions).

The lesson catalog (`teach/catalog`) contains each lesson’s `key`, `title`, `summary`, `family`, and `views` (`["2d"]`, or `["2d", "3d"]` for lessons with a 3D view).
`curriculum` lists lesson keys in the existing learning order. Lesson rendering
accepts registered keys only.

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

Open **Server → Security → Users and roles**. Load reads accounts and roles
without displaying password hashes; selecting a row reads its effective grants
and account attributes. The navigation badge counts only the loaded list.
Without access to the account tables, the list shows the connected account and
the backend's coverage warning. Create user and Create role are in the toolbar;
Lock, Unlock, Expire password, Set password, Rename and Drop live in the
inspector action area.

Edits stay under Pending changes. Review changes shows the before/requested-after
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
Columns are entered manually. For example, select a user, add database `shop`,
tick **CREATE ROUTINE**, choose **Review changes**, inspect the GRANT, and
**Apply**. Roles use the same workflow. Inherited privileges remain visible
without direct-edit controls. Catalog availability and legal levels are
separate from the connected account's authority to grant them.

Typed passwords are cleared after planning and re-entered for Apply: the
30-minute single-use plan handle stores no secrets. Generated passwords work on
MySQL and MariaDB and appear once with Copy. Account reports, drafts, plans,
passwords and results are never saved; restore keeps the selected view only.
The shared privilege provider reads on connection changes and stale window focus.
Accounts controls use its canonical action answers; other screens use the [privilege gating map](UI_TESTING.md#stage-b-gating-map).

### Tables and indexes: browse and export

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
