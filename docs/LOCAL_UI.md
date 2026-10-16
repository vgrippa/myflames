# Performance workspace

The myflames Performance workspace is a local browser app for MySQL and MariaDB
performance work: execution plans, live captures, and what the server is doing
right now (server health, InnoDB status, top statements, sessions, locks,
wait events, index health, table health, I/O hot spots, and a configuration
review). The
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
| **Home** | Connections in a table beside one Recent work list of open plans, saved investigations and query tabs. **Add connection** is the primary action in the Connections toolbar. **Import plan** stays in the top bar and is primary when no plan is open; the empty Recent work list also offers import and the sample. Dropping a plan file anywhere on Home imports it. **New query tab** belongs to the Query editor |
| **Plans** | The open plan with its charts, findings, and inspector. **Explore** and **Compare** switch between one plan and two plans side by side. **Import plan** is in this screen's bar. The plan's name and a stats strip (time, rows examined, rows returned, findings) sit on one line, the summary and the SQL are one-line strips that expand (SQL with **Copy SQL**), and the chart with the inspector fills the rest of the window; operators, findings, context, and notes follow below |
| **Query editor** | SQL tabs that run any SQL in a session of their own (Run, Run all, Stop, results per result set, Messages, export, transactions), Explain and Run analysis, the schema browser, the run history, and the connection in use |
| **Server** | Views of the connection in use that only read (the exceptions are **Kill** in Locks or Sessions, which ends one session after you confirm and needs the privilege, **Change** in Configuration, which changes one setting after you confirm, a skip in Replication, a dump, restore or clone in Maintenance, and account changes in Users and roles, each after its own confirmation), in a grouped sub-navigation: **Overview** (Health, which opens first, InnoDB, and Full analysis), **Workload** (Top statements, Sessions, Replication, Waits, I/O), **Contention** (Locks), **Schema** (Indexes, Tables, Database diagram), **Setup** (Configuration, Instruments, Maintenance), and **Security** (Users and roles). See [The Server place](#the-server-place) |
| **Investigations** | Saved investigations: search, open, delete |
| **Learn** | The lesson catalog and player (`myflames teach` on the command line) |

The app follows the system light or dark appearance. Check chart labels,
Compare highlights, tables, dialogs, and Audit status colors in both appearances;
status text and icons remain available alongside color.

### The Server place

Every Server view has the same anatomy, after the Apple apps DBAs already know
(Activity Monitor, System Settings, the Finder inspector):

- **Sub-navigation.** The views are grouped by job: Overview, Workload,
  Contention, Schema, Setup, Security. Each item shows a status dot or a count from data
  you already loaded (Health and Configuration show their worst status, Locks
  the blocked sessions, Sessions the active ones, Indexes what to review).
  Nothing is read just for a badge. The arrow keys move between items.
- **Toolbar.** The view's title with an ⓘ that explains it, its controls (a
  sampling window, auto-refresh, a database picker, a filter), **Refresh**, and
  the inspector toggle (⌥⌘I, or Alt+Ctrl+I). The place's own toolbar holds the
  connection picker, **Refresh all**, and an ⓘ with what Server does.
- **One summary line**, with the status and a **N notes** button for engine
  differences, privileges, and caveats.
- **A table or one-line rows.** Tables sort by any column, keep the header in
  view, and scroll sideways inside their frame. Rows take the arrow keys (and
  Page Up, Page Down, Home, End); Enter or a click opens the inspector, Escape
  closes it and returns to the row. Large counts read compact (159.0B, 14.2M)
  with the exact value on hover and in the inspector; a long name truncates
  (full text on hover) and its flags stay beside it. Tables with more than 300
  rows draw only the rows in view.
- **Columns.** Drag a header's right edge to resize any column, or focus the
  edge and use the Left and Right arrow keys (Shift for bigger steps, Home and
  End for the narrowest and widest); double-click the edge to fit. The
  **Columns** button at the right end of the header lists every column with a
  checkbox to hide or show it (the last visible one stays) and **Reset
  columns**. Widths and hidden columns are kept per table with the workspace.
  Modes of one view are a segmented control (Active / All, Unused / Redundant,
  Events / Classes); rarely used detail waits behind a disclosure (Advanced,
  Breakdown, Not instrumented).
- **Inspector.** The selected row's explanation, rule, numbers, the statements
  to copy, and its actions (Explain, Show plan, Kill, links to other views). It
  can be resized by dragging or with the arrow keys on its edge (280 to 560
  pixels); this browser remembers the width, and the workspace restores whether
  it is open. Kill opens a confirmation sheet that starts on Cancel; Cancel
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
  accessible description. Unknown or unverified privileges remain enabled with an
  "Unverified" indicator so incomplete catalog visibility does not block valid
  actions. Connection write policies remain authoritative: read-only connections
  refuse writes, and Production connections ask for explicit confirmation.

**Refresh all** (place toolbar, ⌥⌘R or Alt+Ctrl+R) reloads every view that
already holds a report for the connection in use, with the parameters that view
would use (its window, databases, scope). Views you have not opened stay
unloaded, and the Full analysis never runs again by itself. At most three reads
run at once, the sampling views (Health, Waits, I/O) first, so the whole refresh
takes about the longest window. While it runs the button becomes **Stop**
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
back never shows an answer read before the switch. Refresh all is off when no
view holds a report for the connection in use.

**Full analysis** (Overview) runs every Server view once on request, the
MySQLTuner-style checklist that `myflames report` prints, implemented with
myflames' own modules. **Run full analysis** takes about 10 to 20 seconds and
only reads. The view then shows one summary line, the server facts (version,
host, uptime, when it was taken and how long it took, the databases, how many
views were read), **Look at first**, the findings to look at as one-line rows
(status, title, the key number) in the report's own ranking, **Not judged**
for checks that could not run with the reason (a missing privilege, Performance
Schema off, a view that failed), the sections (memory, redo, connections, and
so on) as a grouped list, and the findings with no rule behind a disclosure.
The inspector holds a finding's verdict, numbers, settings, rule, why it
matters, what to do (a statement and a my.cnf line to copy; myflames never runs
or applies them), the caveat, links to the view it came from, and the
statements behind the number with Explain and Run analysis. The coverage of
those statements and the checks left out are behind the notes button.
**Export** saves the report as HTML, Markdown, JSON, or text, generated from
the report JSON on the server, with a "Connection:" line naming the connection
it was read from (never a password). The report belongs to its connection: after a
switch it stays greyed with its server actions off (export still works). It is
never saved; the workspace only remembers that it ran.

**Change a setting** (Configuration). Each setting under **Current values** in
the inspector has **Change…**; a read-only one has **Change at next restart…**
on MySQL (SET PERSIST_ONLY) or **Option file line…** on MariaDB, and a value
kept with SET PERSIST also has **Reset persisted value…**. The sheet reads the
variable from the server and offers only what it allows: **Now only** (SET
GLOBAL, until a restart), **Now and after restart** (SET PERSIST, MySQL),
**After restart only** (SET PERSIST_ONLY, MySQL), **Session** (SET SESSION
opened in the current Query editor tab without running it), and **Remove the persisted
value** (RESET PERSIST). MariaDB has no SET PERSIST, so the sheet shows the
option file line to copy instead. Type the value (sizes take K, M, G and T;
ON/OFF and lists are menus): the server checks it as you type and the sheet
shows the exact statement, the effect, the privileges it needs, the statement
that undoes it, and the caveats. The confirm button names the action (**Set
globally**, **Set and persist**, **Persist for next restart**, **Open in Query
editor**, **Remove persisted value**) and is on only when the statement on
screen is the checked one; the connection's type decides whether it is asked
(Production asks for every change, Development for SET GLOBAL and SET PERSIST,
Read-only refuses). Right before sending, myflames reads the variable again and
sends nothing if it changed. The answer appears above the settings with the
value before and after (the server may round it, for example a buffer pool
size), and the configuration is read again. Cancel and Escape send nothing and
are recorded as declined in the [Audit log](#audit-log) for server changes.
Session only prepares SQL: opening or cancelling its draft sends no write and
creates no declined audit. Run it in the Query editor to affect that tab’s
session. If the editor is unavailable, copy the exact statement into your tab.
The CLI `config set --session` still uses a one-off connection to test a value
and explains this on stderr (unless quiet).
Change is off while the report is from another connection.

**Replication** (Workload, after Sessions) lists each replication channel
(MySQL) or connection (MariaDB) of the connection in use: status, source,
I/O and SQL threads, lag, and the last error code. Nothing is read until
**Load**. The inspector shows the threads, the last error once (code, time,
table, failing GTID, the server's text), the lag signals with their caveats
(Seconds_Behind_Source alone never says a replica is caught up), the
positions and GTID sets, and what recovery needs. Group Replication channels
are listed as Not judged and cannot be skipped here. When the applier stopped
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

**Maintenance** (Setup, last) has five tasks in a segmented control. **Tools**
(**Detect tools**) lists the MySQL and MariaDB tools on this computer
(mysqlsh, mysqldump, mariadb-dump, and the rest) with their versions and what
they offer for the connection in use. **Upgrade check** runs MySQL Shell's
upgrade checker for a target version you choose, or a **Partial check
(native subset)** without it, labeled as such, which never says an upgrade is
safe; findings are rows (Error, Warning, Notice, Passed, Not checked, Check
failed) with the objects and documentation in the inspector, and **Export
JSON**. MariaDB gets the mariadb-upgrade steps to run on the server host.
**Dump**, **Restore** and **Clone** each have a short form and **Run
preflight…**, which only reads: failed checks stay on screen with their
reasons and nothing can start; a clean preflight opens the confirmation with
the target, the exact commands, the warnings, and the checks. A dump goes into
a folder on this computer that must not exist yet or be empty (a folder with
files in it is refused at the preflight); restore and clone also ask you to
type the target. The confirmation goes stale after a minute (run the preflight
again), and myflames repeats the preflight right before starting. A running
job shows its progress, the last output line, and **Cancel**; finished jobs
show the verification (passed, failed, or not checked, with the reason) and
the dump manifest. Jobs belong to the myflames process and to the connection
they ran on: after a switch they stay greyed until you switch back, a reloaded
page finds them again, and a Start whose reply was lost is found in the job
list instead of being sent twice. A running job is also named above any other
Server view with **Open Maintenance**. A dump uses MySQL Shell when it is
offered, or the engine's SQL dump tool when you choose it; a table dump needs
exactly one database. The donor password for a clone stays in the page and is
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
its own and choosing a view pushes it on screen, with **Server** to go back.

Under the places, **Open plans** lists every open plan with its time (or
**Estimated plan**), and **Baseline** on the plan others are compared against
when two or more are open. Hover or focus a row to remove it. Only the place on
screen is highlighted; an open plan is highlighted while Plans shows it.

The footer holds one status: **All changes saved**, **Saving…**, or **Not
saved:** with the reason. **Settings**, next to it, holds **Restore workspace on
startup**, the **Audit log** (see [Audit log](#audit-log)), and **About
myflames**, a window with the version, the license, and the
credits (Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs). The
top bar shows where you are, **Save investigation** with
**Unsaved changes** or **Investigation saved** when plans are open, and one
**Local** badge.

Use the Up and Down arrow keys to move between places, and Home or End to jump
to the first or last. Below 650 pixels the sidebar becomes a compact bar with
the status, Settings, and a **Menu** button that opens the places and open plans
in a drawer; Escape closes it.

### Window status bar

Across Plans, the Query editor, and the Server place, a unified window status bar
at the bottom of the viewport displays the active environment and execution state:
database engine and version, active connection name, session state, transaction
status, timestamp of the last run, execution duration, row counts, and data
refresh status. In narrow and phone layouts, this status bar wraps its indicators
onto multiple lines while remaining readable. In the Query editor, the embedded
secondary status bar is hidden on phone layouts (`display: none`) to eliminate
duplicate readouts, while editor operation feedback remains visible.

### Dialogs, menus, and focus management

Dialogs, confirmation sheets, popovers, and drawers use scoped focus trapping:
- When a modal surface opens, focus moves automatically to the first interactive
  element or designated control.
- Keyboard navigation (Tab and Shift+Tab) cycles strictly within the active
  surface, preventing focus from escaping to background elements.
- Within menu structures and grouped rows, arrow keys move focus between items.
- Pressing Escape dismisses the active surface and restores focus to the control
  that opened it.
- Context menus on table rows (in Query editor results grids and schema trees) can
  be opened by right-clicking, by clicking the dedicated "More actions" button on
  the row (`aria-haspopup="menu"`), or by pressing Shift+F10 (or the ContextMenu
  key) on the selected row.

### Responsive and phone layout

On phone and narrow screens (below 720 px):
- Below 650 px, flame graphs, execution trees and bar charts scroll horizontally
  at their full text size. Swipe sideways or focus the chart and use arrow keys.
- Toolbars and segmented controls wrap onto multiple lines without clipping
  buttons or dropping controls offscreen.
- The sidebar collapses into a compact bar with a **Menu** drawer.
- The Server place presents the view list as a separate screen, pushing selected
  views into view with a **Server** button to return.
- Inspectors become bottom sheets that slide up and take keyboard focus.
- In the Query editor, toolbars and parameter inputs wrap, while the secondary
  editor status bar is hidden to avoid duplicate information above the responsive
  window status bar.

### Privilege-aware actions

Workspace actions are governed by effective database privileges and connection
write safety:
- **Denied actions**: when an action is definitively denied by database grants,
  its button is disabled using `aria-disabled="true"`. The button remains focusable
  so screen readers and keyboard users can discover the restriction, while pointer
  and keyboard clicks are blocked. The tooltip and accessible description
  (`aria-describedby`) state the required privilege and why it is unavailable.
- **Unverified actions**: when grants cannot definitively prove whether an action
  is allowed (such as MariaDB role trees, routine definitions, or partial revokes),
  the button remains enabled with an "Unverified" badge. This ensures users are not
  blocked by incomplete catalog data, leaving the server as the final arbiter.
- **Query editor quiet notice**: statement privilege analysis checks statements
  against the connection model and write policy. Rather than disabling the editor,
  it displays a single quiet note below the toolbar ("Privileges not verified for
  this statement" or specific warnings), keeping mixed batches runnable unless
  every statement is definitively denied.
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

While the connection in use is Production, a thin red strip under the top bar
of the Query editor and Server says "Production: every change on NAME asks
first"; a read-only connection gets a neutral "Read-only" strip; Development
shows none. The picker has a dot of the same color (red Production, green
Development, an outline when read-only). The server enforces the type on
every write; the strip and the dot only show it. Saved connections are read
again whenever the window comes back and right before a Kill is sent, so a
type or read-only change made in another tab or with `myflames profiles set`
shows up; a Kill confirmed under the old setting sends nothing. While they
are loading or cannot be read, the strip says **Production assumed** (with
**Retry**) and Kill stays off. A Development choice in the form holds only for
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
records at once). From the command line: `myflames audit`.

## Inspect a plan

1. In Plans, choose **Import plan**, drop a JSON file, or paste a saved plan.
   Each plan can be up to 5 MiB. **Explore sample plan**, on Home or in the
   empty Plans screen, loads a bundled example.
2. Review execution time, estimated rows examined, findings, and suggestions.
3. Switch between Visual Explain, flame graph, execution tree, and bar chart.
4. Search the operator list and select an operator to inspect its measurements
   and raw fields. Findings with a matching operator include an inspection link.
5. Use **Export HTML** for a portable report or **Export JSON** for the existing
   versioned analysis sidecar.

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
join symbols, and arrows sized by estimated row counts. Drag to pan, scroll gently to
zoom, or choose **Fit plan**. Search within the chart and select a node to update
both its chart details and the workspace operator inspector. HTML export keeps
the interactive view. The percentage shows the current zoom; click it to reset
to 100%. See [Visual Explain](WORKBENCH_VIEW.md) for measurement
semantics and differences from Workbench.

## Compare plans

Import two plans, choose **Compare** in the Plans bar, and choose the before and after versions.
The workspace shows timing changes and the existing operator comparison report.
Export HTML or JSON from that view to save the comparison.

Operators are matched by structure and semantic identity. Ambiguous repeated
operators are marked uncertain and excluded from regression counts. Compare measurements collected
under similar conditions, including cache state and concurrent work.

## Learn

Choose **Learn** in the sidebar to browse all registered lessons. Search by
name or topic, filter by family, or choose **Start learning** to follow the
existing curriculum from table scans through indexes, joins, and caching.

Lessons open inside the workspace with their original sliders, animation
controls, and explanations. **Previous lesson** and **Next lesson** follow the
curriculum; **All lessons** returns to the catalog. Topics outside the learning
path can be opened directly from the catalog. **Download lesson** saves the
self-contained HTML for offline use.

Lessons with a 3D view (today the B+tree lesson) show a **2D | 3D** switch next
to **All lessons**. 2D is the default and is unchanged. The 3D view draws the
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
investigations to keep named copies in the library. Downloads are saved through your browser. Nothing is sent to an
external service.

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
[Write safety](../README.md#write-safety).
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

## Database diagram

Open **Server > Schema > Database diagram** to inspect a database's tables,
columns, keys, and foreign-key relationships. Select a database to read its
metadata, or import SQL (`.sql`), Mermaid (`.mmd` or `.mermaid`), or editable JSON
without a database connection. Imports are parsed as data and never executed.
Files and models are limited to 2 MiB; a model holds at most 200 tables,
10,000 columns, 1,000 relationships, and 20,000 index key parts. Unsupported input and incomplete metadata produce
warnings; inspect them before treating a diagram as a complete schema. SQL
import supports CREATE TABLE and ALTER TABLE ADD keys and foreign keys. Mermaid
uses an erDiagram subset. Other clauses or statements are reported rather than
executed. Live metadata reads share a 30-second budget.

Drag tables, pan and zoom the canvas, use Fit or auto layout, and select a table
or relationship for details. Focus a table and use the arrow keys to move it
16 pixels, or Shift+arrow for 64; Enter opens its details. Position fields and
Reset offer alternatives to dragging. Conceptual relationships use dashed
lines; a relationship without column mappings does not claim a database foreign
key. Mermaid cardinalities are retained as modeling information.

Export SQL or Mermaid after reviewing any loss warnings, editable JSON to keep
the model and table positions, or standalone SVG to share the drawing. Export
does not apply schema changes. The diagram and layout stay in memory and are
not part of session restore; save JSON to continue later. Refresh all reloads
loaded live diagrams, while imported diagrams remain unchanged.


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
| `/api/analyze` | `plan`: raw plan text | `analysis`, `operators`, `query`, `chart_data` (`chart-data-1.0`, drawn in the page by the chart runtime) |
| `/api/render` | `plan`, `view` | `html`: standalone chart document (kept for compatibility; the workspace draws from `chart_data`) |
| `/api/report` | `plan`, `view` | `html`: portable report |
| `/api/compare` | `before`, `after`: raw plan text | `analysis`, `html` |

Workspace routing in `myflames/ui.py` (`api_response`) dispatches across dedicated subsystems:

- Browser authentication (`/api/auth/session`).
- Database diagrams (`/api/schema-diagram/read`, `/api/schema-diagram/import`, `/api/schema-diagram/export`).
- Session management (`/api/session/load`, `/api/session/save`, `/api/session/clear`, `/api/session/settings`).
- Local audit logging (`/api/audit/list`, `/api/audit/export`, `/api/audit/settings`, `/api/audit/declined`).
- Query editor sessions (`/api/editor/open`, `/api/editor/run`, `/api/editor/status`, `/api/editor/stop`, `/api/editor/commit`, `/api/editor/rollback`, `/api/editor/close`, `/api/editor/history`, `/api/editor/export`).
- Server lifecycle tasks (`/api/lifecycle/tools`, `/api/lifecycle/upgrade-check`, `/api/lifecycle/prepare`, `/api/lifecycle/start`, `/api/lifecycle/status`, `/api/lifecycle/cancel`, `/api/lifecycle/jobs`).
- Users and roles (`/api/accounts/list`, `/api/accounts/read`, `/api/accounts/scope-objects`, `/api/accounts/plan`, `/api/accounts/apply`). The grant matrix supports all catalog privileges at their legal levels, dynamic grant options, role admin/default settings, anonymous proxy accounts, MariaDB package/package-body scopes and MySQL 9.2+ MLE library scopes. Packages and libraries accept exact names.
- Full analysis export (`/api/report/export`).
- Saved profiles and investigations (`/api/profiles/*`, `/api/investigations/*`, `/api/connection/test`).
- Live diagnostics, Server views, and actions (`/api/live/*`, including `health`, `innodb`, `top`, `sessions`, `running`, `locks`, `kill/prepare`, `kill`, `waits`, `indexes`, `tables`, `io`, `config`, `variable`, `instruments`, `replication`, `privileges`, `catalog`, and `schemas`).

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
