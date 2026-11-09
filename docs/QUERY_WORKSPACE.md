# Query investigations

This guide covers query investigations in the
[myflames Performance workspace](LOCAL_UI.md) (`myflames ui`): exploring and
comparing plans, capturing queries from a database, and the Server views that
find the statement to investigate. The workspace uses the same Python parser,
analysis, renderers, and comparison engine as the CLI. Its places are **Home**
(getting started, connections, recent work), **Server**, **Workload**,
**Replication**, **Tuning**, **Query editor**, **Plans**, **Investigations**,
and **Learn**; see [Find your way around](LOCAL_UI.md#find-your-way-around). Its interface uses the
[stacked-flame app mark](brand/README.md), neutral surfaces, system typography,
and a blue accent for selected items and primary actions. Layout and hierarchy
follow [Apple's layout guidance](https://developer.apple.com/design/human-interface-guidelines/layout).

## Explore a plan

Select an operator in any of the four charts, the operator table, or the metric
list inside the chart. Selection stays linked to the inspector when you switch
views. The inspector shows measured timings, actual and estimated rows,
related findings, raw fields, and a lesson when the existing Teach registry has
a matching topic. **Learn this operator** opens that lesson in Learn; **Back to
plan** returns to the same plan and selection.

Use **Focus branch** to isolate a subtree. The breadcrumb returns to an ancestor
or the entire plan. **Collapse branch** hides descendants without changing the
recorded measurements; **Expand all** restores them. The chart's metric selector
controls a separate set of operator bars below the native chart:

- Self time: milliseconds across all loops.
- Total time: includes input work; overlapping totals must not be summed.
- Actual rows: rows per loop.
- Estimation mismatch: larger row count divided by smaller, with the direction
  labeled. Missing values and a zero/nonzero pair are shown explicitly.

Native charts keep their original visual meanings. Selecting rows does not turn
a time-width flame graph into a row-width chart. **Largest self time** and
**Largest estimate mismatch** select the relevant operator. When a measured plan has eligible base-table accesses,
or filters directly above them,
whose rows per loop differ by at least 10×
(`max(actual, estimated) / max(1, min(actual, estimated)) >= 10`) and whose
larger estimate or actual count times loops reaches 1,000 rows, the plan header shows
**Misestimates N**: click it to step through them in descending ratio order,
with violet markers in Visual Explain, the flame graph, the execution tree, and
the bar chart (if the bar chart omits an operator because its self time is
zero, a status message above the chart explains why and the inspector still
opens it). The inspector's **Row estimate** section shows the direction and
ratio and, when the operator's table maps unambiguously to a single base table
in the captured schema evidence, the table's `mysql.innodb_table_stats.last_update`, statistics row count and
histograms with an `ANALYZE TABLE` statement to copy. Overestimates are skipped
below early-stop ancestors such as Limit and semijoin inner sides; MariaDB
compares only eligible index lookups and only underestimates. Operators omitted
by a native view remain accessible in the full operator table and metric list.
MySQL SELECT-list subquery inputs are retained as separate branches; their
timing is not subtracted from the enclosing iterator's self time.

## Save and reopen work

The investigation notebook, below each plan, holds a name, notes, tags, named
plans, and a baseline. **Save investigation**, in the top bar, explicitly stores
these on this computer in `~/.myflames/workspace.sqlite3`; next to it,
**Unsaved changes** or **Investigation saved** says whether the open plans differ
from the saved copy. The **Investigations** place supports search, reopening,
and deletion, and Home lists the most recent ones. Opening another bundle while unsaved plans exist downloads a backup
bundle before replacing them. Separately, [session restore](#session-restore)
keeps the open workspace across reloads and restarts.

## Session restore

The workspace saves itself and reopens where you left off, the way a text
editor reopens its files. About one second after each change, and again when
the page is hidden or closed, the UI saves a snapshot to the local server. The
next time you open `myflames ui`, or reload the page, it restores:

- open plans (raw text, names, plan notes, and imported or captured evidence)
  and the active plan
- the place you were on, the chart view, metric, selected operator, focused
  branch, and collapsed branches
- compare selections, the baseline, and the opened investigation, including
  notebook name, notes, and tags you had not saved yet
- the open lesson in Learn
- Query editor tabs (names, SQL, parameter JSON, the caret or selection, the
  row limit, the default database, the On error choice, and which results tab
  was open) and the active tab, the time
  limit, analysis runs, trace option, and conditions, and the editor's panes
  (editor height, schema browser open and its width, history open, the run
  time limit). Results, the tab's server session, file paths, and credentials
  are never saved
- the connection in use: its saved connection ID and its non-secret fields
  (name, host, port, user, schema, TLS mode, and CA path)
- the Server or Workload view (Health, InnoDB, Error log, Locks, Indexes,
  Tables, Database diagram, Parameters, Instruments, Maintenance, Users and
  roles, Top statements, Processlist, Waits, or I/O), whether
  its inspector is open, whether a Full analysis ran (`slow.report_run`; the
  report itself is never saved), Maintenance's task (`slow.maintenance_section`:
  tools, upgrade, dump, restore or clone), whether a skip loop was running
  (`slow.replication_loop_active`, only so Replication can say it stopped when
  myflames restarted; a restore never starts a loop or a job), and its controls: Rank by in Top statements, the Health, Waits and I/O sampling windows, the
  schema shared by I/O and Top statements, Active or All and the minimum time
  in Sessions, the databases chosen in the Indexes and Tables pickers, each
  view its own, and the column widths and hidden columns of every Server table
  and of the Cluster place's Members (Nodes on Galera) and Routers tables
  (`slow.columns_v1`, by table id; unknown tables and columns are dropped and
  widths clamped on restore), never the samples, the reports, or the selected
  rows. A
  session saved with the old Running queries tab opens on Sessions, and a
  session saved with Full analysis or Tuning as a Server view opens on Health. The
  inspector's width is a preference of this browser, not of the workspace

A password is never saved in the session, whether you typed it or it is
remembered for a connection. The client copies connection fields one by one, and
the server removes every `password` key at any depth before writing. Results of
live jobs and slow-query lists are not saved; captures that you opened as plans
are. The snapshot lives in a single `session` row of
`~/.myflames/workspace.sqlite3`, created with owner-only permissions (file
0600, directory 0700).

After a restore, a notice says when the workspace was saved, with **Start
fresh**. Start fresh clears the stored session and resets the workspace. When
open plans are not in a saved investigation, it first offers **Download backup
and start fresh**, which saves the same `unsaved-investigation.json` bundle as
opening another investigation. Anything that no longer fits is left out with a
note instead of failing: plans that no longer parse, a screen or chart view
this version lacks, operators that are gone, a deleted connection (its
settings stay as an unsaved connection), or a lesson that was removed. Sessions
saved by earlier versions open on the matching place: the plan explorer and
Compare plans open Plans (or Home when no plan is left), the query laboratory
opens the Query editor, and Teach opens Learn.

The sidebar footer shows the save status (**All changes saved**, **Saving…**, or
**Not saved:** and the reason). **Settings**, next to it, holds the **Restore
workspace on startup** option, which is on by default and stored in the same
database. Turning it off stops autosave and
clears the stored session. The browser's leave-page prompt appears only when
autosave is off and there is unsaved investigation work, or when the last save
failed.

Each save carries the revision it was based on. When another browser tab saved
a newer revision first, this tab shows "Your workspace changed in another
browser tab" and loads that newer workspace. Changes made in this tab since its
last successful save are replaced.

Limits: the session can be up to 64 MiB. A larger workspace is not saved, the
previous session is kept, and the status shows the reason until you remove
plans. The final save when a page closes is sent with `keepalive`, which
browsers only allow for small requests (about 64 KiB), so with large plans the
changes from the last second before closing the tab may be lost. Every save
sends the whole snapshot, which is fast on the loopback connection but grows
with the size of the open plans.

**Export investigation bundle** creates a portable `investigation-1.0` JSON
file containing raw plans, captured evidence, notes, and baseline references.
Import it through the same file picker used for plans. An investigation holds up
to 30 plans and 10 MiB; individual plans remain limited to 5 MiB. Passwords
are excluded from saved connections, saved investigations, bundles, and capture
output.

## Compare plans

Choose **Compare** in the Plans bar, then the before and after plans. Selecting either chart or an operator comparison row
highlights corresponding operators. Matches use semantic subtrees, operator
identity, and surrounding structure. Each match reports its method and
confidence. Added, removed, changed, and ambiguous operators are explicit.
Ambiguous repeated operators remain tentative; they do not count as regressions.

Estimated plans can be compared structurally. A timing comparison requires
measurements on both sides; missing measurements are not zero. Actual measured
zero is retained. Repeated captures expose median, range, standard deviation,
and conditions in **Measurements**. The comparison chart uses the last captured
plan and labels the run summary separately. Cache state is not reset between
runs. Differences across servers, datasets, cache state, and instrumentation can
affect measurements.

## Capture a live query

On Home, choose **Add connection** under **Connections** and enter a name, host,
port, user, password, and default schema. TLS mode and CA path are under **TLS
settings**. **Test connection** also lists visible schemas. **Save connection**
keeps the settings; **Use without saving** puts them in use for this workspace
only. The connection marked **In use** is the one the Query editor and Server
use, and **Use** on any row switches to it. A local `mysql` or `mariadb` client
executable is required, as with the existing CLI connection workflow. The UI
says "connection"; the API and CLI call a saved connection a profile.

Each saved connection also has a type and a read-only setting that decide how
myflames guards statements that change the server (today, ending a session
from Locks; see [Write safety](LOCAL_UI.md#production-development-read-only)). **Production**, the
default for new and existing connections, asks before every write.
**Development** asks only before risky writes, such as DDL, UPDATE or DELETE
without a key in WHERE, KILL, SET GLOBAL, replication commands, and CLONE.
**Read-only** refuses every write, whatever the type, before a connection is
opened. Choose them in the connection form (**Type** and **Read-only**, in
Home > Connections), or with `myflames profiles set NAME --type development`
or `--read-only` / `--read-write`. A connection used without saving is
Production and may only choose Read-only. A saved connection used with another
host, port, user or TLS setting counts as Production; the form warns when you
change them on a Development connection, and saves it as Production unless you
choose Development again. The window footer of the Query editor, Server,
Workload, Replication, Cluster and Tuning starts with the connection's write safety: a
purple dot and "Production · changes ask first" (accessible name "Production:
every change on NAME asks first"), "Read-only · changes refused" with an
outlined dot, or "Development · risky changes ask first" with a teal dot; the
connection picker shows a dot of the same color with the type pill and, when
set, a Read-only outline pill (a short Prod or Dev in narrow windows,
outlined when read-only). Every write
attempt, including declined and refused ones, is recorded in the local audit
trail: **Settings > Audit log** in the workspace, or `myflames audit`.

Then open the **Query editor**. Its header shows the connection in use, with a
switch to another saved connection, **Manage connections** (back to Home), and
a **Session password** field when no password is saved (**Set**, Enter or
leaving the field applies it for this window; it is never saved). The editor runs any SQL in a
session of its own ([Run SQL in the Query editor](#run-sql-in-the-query-editor));
**Explain** shows the server's EXPLAIN output in the editor and **Run
analysis** measures the statement, as described here.

### Remember a password

**Save connection** with a typed password asks **Also save the password?**
Choose **Save with password** to reconnect later without typing it, or
**Settings only**. Saving a connection that already has a saved password,
without typing a new one, keeps it. A new connection with an existing name asks
whether to replace that connection. When a connection has no saved password, a
successful **Test connection** with a typed password offers to save it. The
prompt names the store:

| Platform | Store | Named in the prompt as |
| --- | --- | --- |
| macOS | Login Keychain, item service `myflames`, account = profile ID | Saved in macOS Keychain |
| Linux | Secret Service through `secret-tool` (attributes `service myflames`, `account <profile ID>`) | Saved in Secret Service |
| Windows | Credential Manager, generic credential `myflames:<profile ID>` | Saved in Windows Credential Manager |
| Any, when none of the above works | `~/.myflames/credentials.vault` | Encrypted on this computer |

The password never goes into `workspace.sqlite3`; the saved connection records only
that one is saved and where. myflames passes it to the OS tools on stdin, never
on the command line. Each connection row on Home says where its password is
("Password saved in macOS Keychain") or "No saved password". With a saved
password in use, Test connection, Explain, Run analysis, and the Server views
work without typing; editing the connection shows **Saved password** in its
password field. Typing a password uses it for this session instead, in the
form or with **Use a different password** in the connection picker. **Forget
saved password** (or **Forget password** on the row) deletes it; **Delete
connection** (or **Delete** on the row, which asks first) deletes the
connection and its password.

A saved password is tied to the host, port, user, TLS mode, and CA path it was
saved with. If you change any of them, myflames asks for the password again
instead of sending the saved one to another server or over a weaker TLS
setting.

The encrypted file is the fallback. Its 32-byte random key is
`~/.myflames/vault.key` (mode 0600, directory 0700), or, when
`MYFLAMES_VAULT_PASSPHRASE` is set, a key derived from that passphrase with
PBKDF2-SHA256 (600,000 iterations). Each password is encrypted with an
HMAC-SHA256 keystream and authenticated with HMAC-SHA256 before decryption, so
a modified file is reported instead of returning a wrong password. It protects
against copies of the vault or the workspace database made without the key file,
such as backups and synced folders. It does not protect against malware or
anyone who can run programs as your user account, because they can read the key
file too. With a passphrase, the key file is not needed, but the passphrase must
be in the environment of `myflames ui` and the CLI.

`MYFLAMES_SECRET_BACKEND` overrides the automatic choice: `keychain` (the OS
store only), `vault` (the encrypted file), or `none` (turn remembering off; the
checkbox is disabled).

For a practice server, run `./scripts/demo-db.sh` (requires Docker). It starts
MySQL 8.4 on `127.0.0.1:3406` and prints credentials for a read-only `demo`
user. It loads `testdb` (users, orders, order_items, products, categories,
reviews; about 9M rows with skewed values, from `scripts/demo-db-seed.sql`),
MySQL's `sakila` sample, and the `employees` sample. Set `DEMO_DATASETS` to
load fewer, for example `DEMO_DATASETS=testdb`.

Use query tabs to keep drafts and name each query. Every tab has its own close button (shown on the active tab, on hover, and always on touch screens); a middle-click closes a tab, the tab on the left takes over, and the last tab stays.
Right-click a tab (or focus it and press Shift+F10 or the context-menu key)
for its menu: **Open SQL file…** (⌘O on macOS, Ctrl+O elsewhere), **Save…**
(⌘S / Ctrl+S), **Save as…** (⇧⌘S / Ctrl+Shift+S), **Save query…**, **Rename…**
(also a double-click or F2 on the tab, and **Rename query tab** in the command
palette), **Duplicate** (a copy right after it with the same SQL, parameters,
default database, On error choice, and row limit; the copy opens its own
session when it runs), **Close**, and **Close others**. A tab opened from or
saved to a local file shows an edited dot (`●`) before its title while its SQL
differs from the file, and closing it asks **Unsaved changes** (**Discard and
close** or **Cancel**). A name is trimmed, may not be empty
(Enter on an empty name keeps the field open and says why; leaving the field
keeps the old name) and holds at most 64 characters (a longer name is cut
without splitting an emoji); Escape cancels. Names are
saved with the workspace session, so they come back after a restart. Close
others closes the other tabs one at a time and stops at a tab whose
transaction is open, or may be, to ask how to close it; a tab with a running
statement is kept and named in the message. Parameters are typed
values in a JSON object, referenced as `:name` outside SQL literals/comments:

```sql
SELECT * FROM orders WHERE customer_id = :customer_id
```

A value may be a number, a string, `true`/`false` or `null`; a JSON list of
those (`{"ids": [1, 2, 3]}`) is written as a comma-separated list, for
`IN (:ids)` (1 to 1,000 values).

```json
{"customer_id": 42}
```

- **Explain** works like MySQL Workbench: it runs `EXPLAIN` on the statement
  as written, in the tab's own session, and shows the rows in an **Explain**
  results tab in the editor, with Export. The **Explain format** menu beside
  the button picks **Table** (`EXPLAIN FORMAT=TRADITIONAL`, the classic
  `id`, `select_type`, `table`, `type`, `possible_keys`, `key`, `rows`,
  `filtered` and `Extra` columns; plain `EXPLAIN` on MariaDB), **Tree**
  (`EXPLAIN FORMAT=TREE`, MySQL 8.0.16 and later) or **JSON**
  (`EXPLAIN FORMAT=JSON`); the choice is remembered on this computer. EXPLAIN
  does not run the statement, so an UPDATE or DELETE can be explained on any
  connection. The server does evaluate constant parts while it plans (a
  subquery with no table, such as `(SELECT GET_LOCK('x', 0))`), so a
  statement whose expressions would need a confirmation under Run (named
  locks, `:=`, stored or unknown functions) needs the same one under Explain. A statement that starts with `ANALYZE` is refused, because
  `EXPLAIN ANALYZE` would run it. `:name` placeholders (a Top statements
  digest) take their values from Parameters, written as the same literals Run
  analysis uses; a statement without placeholders is sent exactly as written.
  **Explore** on the result captures the visual plan
  (SELECT only) and opens it in Plans; nothing leaves the editor until you
  choose it.
- **Run analysis** executes the SELECT, after a confirmation that says so, and
  captures actual iterator timings. The **Analysis** results tab shows how
  many times it ran, the median time and the SQL analyzed; **Explore** opens
  the plan in Plans.
- Choose 1–10 analysis runs and a 1–300 second limit per run under **Run
  analysis options**.

The editor can hold several statements, as in DBeaver or MySQL Workbench.
Explain and Run analysis send one of them:

- **Selected text.** With text selected, only the selection runs. It is
  trimmed, and a trailing `;` (and any comment after it) is dropped. The
  buttons read **Explain selection** and **Run analysis on selection**, and a chip shows
  how many lines are selected. A selection that holds two or more statements,
  or only comments, is refused with a message before anything is sent.
- **Statement under the cursor.** With no selection and several statements
  separated by `;`, the statement that contains the cursor runs. A `;` inside
  quotes, backticks, or `--`, `#`, and `/* */` comments does not split a
  statement. A comment above a statement belongs to it, and a comment after
  the `;` on the same line belongs to the statement before. The chip names the
  statement and its lines, for example **Statement 2 of 3 · lines 4–6**, and
  the statement is highlighted briefly when it runs.
- **One statement.** With a single statement in the editor, it runs as a
  whole.

**Ctrl+E** (**⌘E** on macOS) explains, and **Ctrl+Shift+E** (**⇧⌘E**) runs
analysis, on the same text the buttons would send (earlier versions used
Ctrl+Enter and Ctrl+Shift+Enter, which now run SQL in the tab's session). The
button tooltips list the shortcuts. Parameters are bound against the text
that runs, so a selection only needs the parameters it references; a reference
that the JSON does not define is reported as `Missing parameter: name`. Run
never binds them. The **Analysis** results tab shows **SQL analyzed**, the
text of the last analysis, and the plan's SQL card in Plans shows the
statement that was executed, with parameter values bound. Messages shows the
exact `EXPLAIN …` statement an Explain sent. The time limit per run, analysis
runs, optimizer trace and **Parameters and execution conditions** sit under
**Run analysis options** (the arrow beside Run analysis); Parameters apply to
Explain too, the rest to Run analysis only.
- **Cancel capture** requests cancellation of this job's database query and
  terminates its client process. A time limit follows the same cancellation path.

Capture accepts one read-only SELECT (including a WITH…SELECT), without
assignments, file output, locking clauses, or client commands. SQL validation is
conservative; use the existing CLI workflow for unsupported statement forms.
Write a quote inside a string by doubling it (`'it''s'`): a backslash before a
quote (`'it\'s'`) is refused, because with sql_mode NO_BACKSLASH_ESCAPES the
same text would end the string early and could run as several statements.
Unclosed strings and comments are refused too.
Draft tabs are kept by [session restore](#session-restore). Captured SQL is
included when you save an investigation. At most two captures run concurrently. Finished captures are
kept in server memory for up to one hour, with a maximum of 50 jobs.

Run analysis can prepare a reviewed SELECT projection from supported DML. Conversion creates a new editor tab and keeps the original SQL unchanged. It does not start a capture: read the warning and choose Run analysis again. Explain never converts: it explains the statement as written. Ordinary Run still executes the original statement under the existing write policy and confirmation rules.

## Format SQL

**Format SQL** (the toolbar button with the indent icon, Shift+Option+F on
macOS or Shift+Alt+F elsewhere, or **Format SQL** in the command palette) lays
out the selection, or the whole tab when nothing is selected: one clause per
line, two-space indent, keywords in upper case, one blank line between
statements. It uses sql-formatter (MIT) with the MySQL dialect, or the MariaDB
dialect when the tab's session is on MariaDB, loaded the first time you use
it. Formatting never runs SQL and is one undo step (⌘Z or Ctrl+Z); a selection
stays selected on the formatted text, and without one the caret keeps its
place.

It changes only white space and the case of keywords. Names keep their case
(a table or alias can be case sensitive on the server): only words reserved
in both MySQL and MariaDB, each statement's first word, and the keywords of a
statement opening such as SHOW FULL PROCESSLIST or START TRANSACTION are put
in upper case. Before the text is replaced, the result is compared with the
original token by token, under both backslash modes and with MariaDB's
`[name]` (sql_mode=MSSQL) read as one quoted name; strings, quoted names,
numbers, variables, executable comments (`/*!80000 ... */`, `/*M!...*/`) and
optimizer hints must stay byte for byte, a line comment must still end its
line, and the space between a built-in function such as `COUNT` and its
parenthesis must stay (without IGNORE_SPACE, `COUNT (*)` and `COUNT(*)` are
read differently). Any other difference refuses the format, nothing changes,
and the message names the line, for example "Format SQL changed nothing:
formatting would have changed “count (*)” to “count(*)” on line 2, which the
server can read differently."

Left exactly as written, and named in the message: statements inside a
DELIMITER block, stored program bodies (CREATE PROCEDURE, FUNCTION, TRIGGER
or EVENT, `BEGIN NOT ATOMIC` and other compound statements), a statement with
a client command (`\G`), an unclosed quote or comment, a name in square
brackets (MariaDB under sql_mode=MSSQL), an executable comment or optimizer
hint the formatter would change, and a statement the formatter cannot read.
Comments between statements stay where they are. A selection inside a
DELIMITER block is left alone; a selection that starts inside a
statement keeps its first word's case. A selection must start and end between
words, outside strings, quoted names and comments (otherwise nothing changes
and the message says where), and the whole tab, not only the selection, is
compared after the change. A word next to an executable comment keeps its case
when the comment holds the dot of a qualified name, and a statement that
starts with one has no opening keywords to change. The first Format SQL
loads the formatter; if you select other text or clear the selection while it
loads, nothing changes and the message says so (a caret moved without
selecting is kept where you put it).

## SQL completion and table data

The editor suggests SQL keywords and catalog names while you type. Press
Ctrl+Space (or Ctrl+Shift+Space when macOS reserves that shortcut), use the arrow
keys, and accept with Tab or Return. Escape dismisses suggestions. Execution
keeps its existing shortcut. Suggestions do not run statements and stay off
inside strings, comments and input-method composition.

Catalog suggestions use the selected connection's configured database. They
include schemas, tables, columns and simple aliases, with quoted identifiers.
They do not represent temporary tables or a database selected by running `USE`
in an editor session. Complex nested scopes and CTEs are deliberately limited.
Metadata reads are bounded and cached briefly in memory. Switching connection,
database or credentials discards that cache and cancels stale requests.

Open a table's data from the schema browser to work in a dedicated full-width
grid. Stage cell edits, inserted rows or deletions, then choose Review. The
review shows the exact generated SQL and target. Apply always requires explicit
confirmation, including on Development connections. Closing or replacing a grid
with pending changes asks before discarding them. Arbitrary query result sets
remain read-only.

The first editor supports InnoDB base tables with a primary key or a full-column
UNIQUE key whose columns are NOT NULL. Tables with triggers, foreign keys,
CHECK constraints, expression defaults, automatic columns other than
auto-increment, unsupported types or unsupported text character sets stay
read-only. Views and tables with generated columns cannot be opened in this
viewer; use the Query editor explicitly for them. Opening this viewer requires
verified SELECT and LOCK TABLES authority.
Editing additionally requires verified global SELECT and target TRIGGER
authority so this initial implementation can prove that related metadata is
visible. These are application limits, not general database requirements.
Text editing supports UTF-8 and ASCII; integers and decimals remain exact
strings, binary values use hexadecimal, and NULL is distinct from an empty
value. Temporal values use UTC.

A page contains at most 100 rows, with 20 changes per review. Snapshots expire
after five minutes; cells are limited to 64 KiB and snapshots to 2 MiB.
Loading briefly takes a table READ lock so its definition cannot change
during capture. Applying takes a table WRITE lock and compares original values
and the table definition before sending the reviewed changes. The review shows
this lock explicitly: it temporarily blocks other sessions from reading or
writing that table. Do not apply while that interruption is unacceptable.
All row changes then commit together. A conflict or a rejected
statement rolls back the batch. A lost commit reply is reported as an unknown
outcome; inspect current data before retrying. Reload after success to see the
current rows. Table snapshots and pending values are not saved with the workspace.

## Run SQL in the Query editor

The Query editor also runs any SQL, the way MySQL Workbench or DBeaver do.
Each query tab owns one server session: one `mysql` or `mariadb` client
process started on the tab's first run and kept until the tab closes, its
connection changes, it is idle for 30 minutes, or the workspace stops. `SET
SESSION`, user variables, temporary tables and transactions last across runs
in that tab. myflames never reconnects on its own: when a session ends, the tab
says **Session ended: <reason>. Session settings were lost and any open
transaction was rolled back.** and **Reconnect** starts a new one.

The screen has three panes: the **schema browser** on the left, the editor
with its results in the middle, and on the right one pane with two tabs,
**History** (past runs) and **Saved** (saved queries). Both side panes can be
hidden; in a window 720 px wide or narrower they open as bottom sheets, one at
a time: opening Schemas or History closes a row's panel, opening a row's panel
closes them, and closing Schemas or History returns focus to the button that
opened it. In a wider window the panes and the row panel stay docked side by
side.

- **Run** (**⌘Return**, **Ctrl+Enter** elsewhere) runs the selection, or the
  statement at the cursor. The statement Run would send is tinted in the
  editor, and its line numbers are marked. A selection may hold several
  statements; they run in order. The server decides what runs: it splits the
  text with the session's own rules (backslashes as `sql_mode` reads them,
  `\g`, `/*! */` executable comments, DELIMITER), so the tint is a guide.
- While a tab's session is on another connection than the one in use, Run,
  Run all, Explain, Run analysis and Load more wait until you close the
  session; Stop, Commit and Roll back always go to the session's own
  connection.
- **Run all** (**⇧⌘Return**, **Ctrl+Shift+Enter**) runs every statement of the
  tab in order. By default (**On error: Stop** in Run options) a run stops at
  the first error; with **On error: Continue**, a full-tab run continues after a
  statement error (stopping only on an explicit Stop, a time limit, a lost
  session, or the output size limit). After a Continue run finishes, Messages
  counts succeeded and failed statements, plus not run, stopped and outcome
  unknown when present. Errors 1213 and 1205 add a
  note on InnoDB behavior (a deadlock rolls back the victim's whole
  transaction; a lock wait timeout does only with `innodb_rollback_on_timeout`
  ON), never a claim about this run. The On error ⓘ explains that an InnoDB
  error normally rolls back only the failed statement, while those two cases
  roll back the whole transaction. Changing Stop or Continue requires a fresh
  write review.
- **Stop** (or **Escape** in the editor) sends `KILL QUERY` for the tab's
  session from a second connection. KILL QUERY ends the statement and undoes
  what that statement changed; an open transaction stays open, and later
  statements of the run do not run. A statement that had already finished took
  effect. A statement stopped inside `SLEEP()` can finish without an error:
  SLEEP returns 1 when it is interrupted, and the server reports an error only
  if the statement checks for the KILL again (for example before reading
  another row).
- `DELIMITER` lines work as in the `mysql` client, so `CREATE PROCEDURE`,
  triggers and events run as one statement. Run on a statement after a
  DELIMITER line sends it with its DELIMITER line.
- The editor shows line numbers, SQL syntax colors, the bracket or quote that
  pairs with the one next to the caret, and a wavy underline where the last
  error points. Undo, input methods and spell check stay the browser's own
  (the editor is a native text area under a colored layer). Above 60,000
  characters the colors are off.
- **Find and replace** (**⌘F** / **Ctrl+F** for Find, **⌥⌘F** / **Ctrl+H**
  for Replace) searches SQL in the active tab with **Match case** (`Aa`),
  **Whole word** (`|ab|`) and **Regular expression** (`.*`). It counts up to
  100,000 nonempty matches and highlights up to 1,000 while syntax highlighting
  is enabled (up to 60,000 characters). Enter and Shift+Enter, or **⌘G** /
  **⇧⌘G** (**Ctrl+G** / **Ctrl+Shift+G**), step between matches. **Replace**
  and **Replace all** insert the replacement literally, including `$1` and
  `$&`, through the native text area. Regex search runs in a background worker
  with a 250 ms budget; a slow pattern reports "Pattern too slow to search".
  Escape closes the bar and returns focus to the editor.

Results come back in tabs: one per result set (**Result 1 · 1,000 rows ·
12 ms**), then **Messages**, which lists every statement of the run with its
outcome: rows, affected rows and the server's info line (`Rows matched: 3
Changed: 3`), warnings, duration, and errors with their code, SQLSTATE and
message. **Go to line N, column M** puts the cursor where the error points.
Explain's rows come back as an **Explain** tab (with **Explore** beside
Export), and after Run analysis an **Analysis** tab shows the measurement and
the SQL analyzed.

- The grid is the shared table: resize and hide columns, sort the loaded rows
  (a result cut at the row limit says "Sorting applies to loaded rows"), row numbers, and long results
  drawn as you scroll. NULL is a separate **NULL** token; an empty string is an
  empty cell; the text 'NULL' is plain text. Binary values show as the client's
  `0x…` hex with their size. Big integers and DECIMAL are shown as the server
  sent them, never rounded. An empty result set has no column names in the
  client's output, so it reads **No rows** with its statement.
- **Select cells** to total them: click a cell; Shift+click or Shift with the
  arrow keys extends the range, and Left and Right move the cell; a row number
  selects its row; **Option+click** (Alt+click elsewhere) on a column header
  selects the whole column (a plain click still sorts); ⌘A (Ctrl+A) selects
  every cell. The line under the grid reads **Count 3 · Sum 6 · Avg 2 · Min 1 ·
  Max 3** for the numbers in the selection, or **4 cells** when none is a
  number; NULL is skipped and counted apart (**1 NULL**). Sums are exact:
  integers past 2^63 and DECIMAL values are added as exact decimals, and Avg
  keeps six digits after the longest fraction. A value written with an
  exponent (FLOAT, DOUBLE) switches the line to floating point, and it says
  **approximate (floating point)**.
- Right-click a cell (or press Shift+F10 on a row) for **Show row** and
  **Select column**, then **Copy selection**, **Copy value**, **Copy row**,
  **Copy column** and **Copy all rows with header**; ⌘C (Ctrl+C) copies the
  selection (a single cell: its value as is). Copies of several cells are
  tab-separated, NULL as an empty field.
- **Show row** (double-click a row, Return on it, or the cell menu) opens the
  row panel beside the grid, a bottom sheet on the 720 px window: every column
  with its type when known and its value. NULL reads **NULL** in muted type,
  never empty; an empty string reads **empty string**; binary stays hex with
  its size; JSON (a JSON column, or text that is a JSON object or array) is
  laid out in a block with **Copy JSON**, its numbers kept exactly as the
  server sent them. Up and Down move to the previous or next row, in the grid
  or the panel; Escape closes it and returns focus to the row the panel was
  showing, which may not be the row you opened it from.
- **Rows** in the toolbar is the row limit per result set (default 1,000).
  The statement is never rewritten: myflames stops reading at the limit, and
  stops a plain read with KILL QUERY so it does not keep holding locks. The
  result then reads **stopped after 1,000 rows**, or **first 1,000 rows; the
  statement ran to its end** for a CALL or a write, and **Load more** runs that
  one read again with a larger limit (rows may differ from the first run).
- **Time limit** stops a run after that many seconds with KILL QUERY (0, the
  default, means no limit).
- **Database** in the toolbar sets the tab's default database (**None** or a
  database from the server's list, read through `POST /api/live/schemas` only
  while the Query editor is on screen). When the tab has no open session, the
  choice is kept on the tab and used when its next session starts; when a
  session is already open, choosing a database sends `USE <database>` in that
  session (`POST /api/editor/use`), while choosing **None** keeps the open
  session's current database and records that a new session starts with none.
  None is kept explicitly through restore, even if the connection has a default
  database. A tab whose session belongs to another connection refuses a database
  change until that session is closed.
- **Open SQL file…** in a tab's menu (or **⌘O** / **Ctrl+O**) opens a `.sql` or
  `.txt` file from this computer (`POST /api/editor/file/open`, UTF-8, up to 5
  MiB) into a new tab. **Save…** (**⌘S** /
  **Ctrl+S**) writes back to the tab's file path, and **Save as…** (**⇧⌘S** /
  **Ctrl+Shift+S**) chooses a new path (`POST /api/editor/file/save`, up to
  8,000,000 characters, adding `.sql` when the chosen name has no extension).
Save refuses symbolic-link targets and a folder changed since it was chosen;
use **Save as…** to choose it again. Adding `.sql` never replaces a file that
already exists: choose its full name to approve replacement. Open refuses special
files and a file or folder swapped for a link after selection, and enforces the
5 MiB limit even if the file grows while being read. If a backend restart loses
the file binding, Save opens Save as within the same operation and reports the
outcome. Cancelling or failing a save or close keeps unsaved edits.

- **Save query…** in a tab's menu (right-click the tab) or the command
  palette keeps the tab's SQL on this computer under a name (the tab's, to
  start with), in a folder you pick or create, with an optional note. The SQL,
  name and note are redacted with the history's rules (a password after
  IDENTIFIED BY, SOURCE_PASSWORD and the like reads `[redacted]`) before they
  are stored. The **Saved** tab lists them by folder, each folder collapsible,
  with a search over names, folders, notes and SQL. Clicking one opens its SQL
  in a new tab and runs nothing; its menu has **Open in a new tab**, **Insert
  at the cursor**, **Rename…**, **Move to folder…** and **Delete…** (which asks
  once). At most 500 saved queries and 4 MB are kept; a full list refuses a
  new one rather than drop an old one.
- **Export** on a result: CSV (RFC 4180; NULL is an empty field, an empty
  string `""`; an option prefixes values that start with `=`, `+`, `-` or `@`
  against spreadsheet formulas), JSON (`columns` and `rows`, NULL as null, every
  value as text), SQL INSERT (you name the table; columns you mark **Treat as
  binary** are written as `X'..'`, because hex is only a guess), Markdown, and
  **Copy as TSV**. Exports use the values as loaded and never include
  connection settings or passwords.
- After a run, a statement insights pill beside the result tabs summarizes the
  server's examined and returned rows, index or full-scan access, joins without
  an index, and temporary tables on disk for the selected statement (from
  `performance_schema.events_statements_history` when available), with an ⓘ
  popover for the counters and verdict rule and **Explain analyze** to run
  analysis on that statement. Results can also be filtered in place (**Filter
  rows**, plus **Show only this value** and **Hide this value** in the cell
  menu as removable chips) and pinned (**Pin result**, up to 5,000 rows, with
  **Compare with pinned** to compare a later result with the same columns by a
  key column).

The toolbar shows the transaction state of the tab's session: **Auto-commit**,
**Manual commit** (autocommit off, no transaction), **Transaction open**, or
**Transaction unknown** (MySQL without the transaction instrument, or a server
that cannot tell). While a transaction is open, the pill shows how long it has
been open, how many rows it modified, and how many waiting InnoDB transactions
it blocks (`transaction_meter`), with a popover for the details, **Commit**,
**Roll back**, and **Show who waits** (which opens **Server > Locks**). With a
transaction open or unknown, **Commit** and **Roll back** appear, the tab shows
a dot, and closing the tab asks **Commit and
close**, **Roll back and close**, or Cancel. When the connection in use is not
the one the tab's session runs on, runs wait until you **Close session**.

**Safe updates** (in Run options, the arrow beside Run) sets `sql_safe_updates`
for the tab's session: on by default for Production connections, off for
Development. The server then refuses, with error 1175 and before any row
changes, an UPDATE or DELETE whose WHERE cannot use an index unless it has a
LIMIT; a multiple-table UPDATE or DELETE is refused when it would scan a whole
table it changes (on MariaDB, any table of the join). MariaDB also refuses a
DELETE without a WHERE (or with a constant one), even with a LIMIT, and a
multiple-table DELETE without a WHERE even under EXPLAIN. SELECT is not
limited: the `mysql` client's `--safe-updates` option also sets
`sql_select_limit` and `max_join_size`, which myflames does not. Turning it
off on a Production connection asks first and is audited; a read-only
connection refuses. The ⓘ beside the box explains this in short and links to
the
[MySQL](https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html#sysvar_sql_safe_updates)
and [MariaDB](https://mariadb.com/docs/server/server-management/variables-and-modes/server-system-variables#sql_safe_updates)
documentation, which the desktop app opens in your browser. It opens over Run
options without closing it; Escape closes the explanation first.

Statements that change the server follow the connection's write policy: a Production connection asks before
every write, a Development connection only before risky ones, and a read-only
connection refuses them. The confirmation shows the exact statements, the
reason, the connection and its transaction state, and warns about statements
that commit an open (or maybe open) transaction first; those always ask, also
on Development. A confirmation is valid for five minutes and for those
statements only; after that the run asks again. Cancel, or Escape, sends
nothing and is recorded in the Audit log as declined; a decline never runs
anything, also when the connection's type changed while the sheet was open. Reads (SELECT, SHOW,
DESCRIBE, EXPLAIN) never ask. `HANDLER ... OPEN` is treated as risky and asks
for confirmation because it holds a metadata lock until `HANDLER ... CLOSE` or
the session ends; `HANDLER ... READ` and `HANDLER ... CLOSE` stay reads.

Every DDL, including temporary-table DDL, needs a confirmation. A script with several DDL statements is refused before password lookup: select and run them one at a time. Multiple DML statements still share a review listing every statement. DROP DATABASE and DROP SCHEMA require the exact database name, including case and spaces; a token alone is insufficient. The server rechecks the saved connection policy after password lookup and immediately before execution. A policy change refuses the reviewed run.

Run ALTER TABLE on its own. Its review opens the tab's session for metadata reads, shows the persistent table's approximate data-plus-index size (in binary units such as 120.6 KiB, with the exact bytes on hover), metadata locks, and transactions associated through granted metadata locks. Unavailable instrumentation or privileges, truncation, and transactions whose table ownership cannot be established remain explicit. The snapshot can change after review. The confirmation lists `SET SESSION lock_wait_timeout = 5` before the original ALTER, both executed and audited in that session. A failed guard stops before ALTER; the setting remains five seconds afterwards. This bounds each metadata-lock wait, not total execution time. A longer MariaDB WAIT or a MySQL SET_VAR timeout override is refused.

The review predicts how the server will run the ALTER: **Predicted** shows INSTANT (only the table's metadata changes), INPLACE with or without a table rebuild, or COPY, and whether other sessions can keep writing; **Why** gives each clause's reason and the server source it follows. The statement's outcome is all its clauses together, as the server decides it, so a mix can be slower than every clause alone, and the notes say so. The prediction reads the table's `SHOW CREATE TABLE` and the settings that matter (`foreign_key_checks`, `sql_mode`, `innodb_page_size`, MySQL's `old_alter_table`, and on MariaDB `innodb_strict_mode`, `innodb_instant_alter_column_allowed`, `old_mode`, `innodb_file_per_table` and, before 11.5, `alter_algorithm`), plus, only when the outcome depends on them, facts the definition cannot show: MySQL's row versions left, a hidden FTS_DOC_ID column left by a dropped FULLTEXT index, other tables' foreign keys to this table and MariaDB's default collations. A fact it could not read leaves the prediction unknown. It covers MySQL 8.4 and 9.x and MariaDB 10.11 to 13.0 (rules checked against the MySQL 9.7 and MariaDB 13.0 source, live-checked on MySQL 8.4.11 and MariaDB 11.4.13); partitioned, temporary, system-versioned and non-InnoDB tables, partition and tablespace clauses and some table options are not predicted, with the reason. It never runs, rewrites or blocks the statement, and the server decides when it runs.

For recognized ADD/DROP COLUMN or INDEX forms on InnoDB (MySQL 8.4+ or MariaDB 10.11+), the review can offer an optional `ALGORITHM=INPLACE, LOCK=NONE` rewrite, but not when the statement is predicted to run instantly: on MySQL that clause turns an instant ADD or DROP COLUMN into a full rebuild. The server decides whether the operation supports these constraints; INPLACE can still rebuild a table. Existing ALGORITHM/LOCK clauses, partition operations, unverified engines or versions, and complex/commented forms receive no rewrite. Copy the suggested SQL into the editor and review it again to use it. A COPY or rebuild can require additional disk space and delay replicas. Open **Server > Locks** to inspect contention while ALTER runs.

### Privilege preview and Run behavior

Before queries are executed, `/api/editor/preview` checks each statement in the
selection or current statement against the connection's effective privilege model
and write policy:
- When every statement in the batch is definitively denied by database grants,
  the **Run** or **Run selection** button is disabled with `aria-disabled="true"`.
  The button remains keyboard focusable to convey the restriction, while pointer
  and keyboard execution are blocked. The tooltip and accessible description
  state the missing privilege.
- When only some statements in a script are denied, or when privileges cannot be
  fully proven absent from grants (such as MariaDB role trees or routine bodies),
  the batch remains runnable with an aggregate state of unknown.
- The editor shows one line at the end of its toolbar only for denied statements
  (enumerating them), never for privileges it cannot verify, and never as a new
  row that pushes the editor down, rather than blocking the entire script.
- Connection write policy preflights remain the final boundary: read-only
  connections reject writes before execution, and Production connections prompt
  for explicit confirmation.

The **schema browser** reads the schema list of the connection in use by
itself the first time it is shown for that connection (once per connection;
with no connection chosen it says to choose one), and its header has **Reload
schemas**. It lists the schemas, a schema's tables and views, and a table's
columns (type, key, NULL) and indexes, plus collapsible **Routines**,
**Triggers**, and **Events** folders under each schema; tables, columns,
indexes, and object folders are read when you expand them (`POST
/api/live/catalog`, up to 200 routines, triggers or events per folder with a
truncation note). Column and index
rows stay on one line, and the actions button shows only on the row being
pointed at, the focused row, or on touch screens. Double-click a
name (or press Enter or Space on a routine, trigger or event) to insert it at
the cursor (`schema.object`, quoted with backticks only
when needed). A table's menu (right-click or Shift+F10) has **Insert name**,
**SELECT * … LIMIT 100** and **Show CREATE TABLE** (both added at the end of
the tab and run), and **Open in Indexes** or **Open in Tables** (the Server
views, on that table); a routine, trigger or event's menu has **Insert name**
and **Show CREATE PROCEDURE / FUNCTION / TRIGGER / EVENT** (added at the end of
the tab and run).

The **history** lists the runs of every tab, newest first, with their
connection, time, duration, rows and outcome; search it, click an entry to
insert its SQL at the cursor, or **Clear** it. It is kept on this computer
(`workspace.sqlite3`, at most 1,000 entries and 4 MiB) with passwords in
`IDENTIFIED BY` and similar clauses redacted, and never a connection password.

The status bar names the connection, the server version, the session (its
connection ID, type, and read-only state), the transaction state and the last
run (statements, time and rows).

| Shortcut | macOS | Elsewhere |
| --- | --- | --- |
| Run the selection or the statement at the cursor | ⌘Return | Ctrl+Enter |
| Run all | ⇧⌘Return | Ctrl+Shift+Enter |
| Explain | ⌘E | Ctrl+E |
| Run analysis | ⇧⌘E | Ctrl+Shift+E |
| Stop | Escape | Escape |
| Open SQL file… | ⌘O | Ctrl+O |
| Save SQL file | ⌘S | Ctrl+S |
| Save SQL file as… | ⇧⌘S | Ctrl+Shift+S |
| Find | ⌘F | Ctrl+F |
| Find and replace | ⌥⌘F | Ctrl+H |

## Server: health, InnoDB, top statements, sessions, locks, waits, indexes, I/O, parameters, and security

When you don't know which statement to capture yet, open **Server** or **Workload**. Their views
are of the connection in use and only read (the exceptions are a **Kill** you
confirm in Locks or Sessions, parameter changes you review and apply in
Parameters, runtime instrument or consumer changes in Instruments, a skip
in Replication, a dump, restore or clone in Maintenance, and account changes in
Users and roles, each confirmed on its own). The place's toolbar holds the same
connection picker as the Query editor. **Server** uses a grouped sub-navigation:
**Overview** (Health, InnoDB, Error log), **Contention** (Locks), **Schema** (Indexes, Tables, Database diagram),
**Setup** (Parameters, Instruments, Maintenance), and **Security** (Users and roles).
**Workload** is a place of its own with **Queries** (Top statements, Processlist) and
**Resources** (Waits, I/O); **Replication** is a place of its own for replication channels;
**Cluster** is a place of its own for the Galera, Percona XtraDB Cluster or InnoDB Cluster the server belongs to;
and **Tuning** is a place of its own with the **Scorecard** tab and the **Full analysis** tab.
Each view in the sub-navigation shows a status dot or a count from data you already loaded. Every view has a toolbar with **Refresh**, one summary
line with an **N notes** button, a table or one-line rows, and an inspector for
the selected row; see [The Server place](LOCAL_UI.md#the-server-place). A view reads when it is opened on screen, once per connection (**Refresh** retries); **Tables** and **Indexes** keep their schema-size consent, **Error log** waits for **Load**, and nothing reads off screen. **Refresh
all** in the place's toolbar (⌥⌘R) reads all sections for the connection
in use, including unopened sections, at most three at a time. Full analysis
runs in its own phase; see [The Server place](LOCAL_UI.md#the-server-place). See [Wait
events, index health, I/O, and
parameters](#wait-events-index-health-io-and-parameters) for Waits to
Parameters, and [Performance Schema
instruments](#performance-schema-instruments) for Instruments.

- **Health** (the first Server view, open by default) shows how the server is doing
  right now. See [Server health](#server-health).
- **Replication** (a standalone sidebar place; `POST /api/live/replication`, the same report as
  `myflames replication`) lists each channel or connection with its threads,
  lag and last error. **Skip this transaction…** and **Skip until stopped…**
  in the inspector are the changes, each confirmed with the exact statements
  and recorded in the Audit log; see [The Server place](LOCAL_UI.md#the-server-place).
  Its badge is the number of failed channels, or the worst status.
- **Cluster** (a standalone sidebar place after Replication; `POST /api/live/cluster`, the same
  `cluster-1.0` report as `myflames cluster`) shows the cluster's members with their state and
  role as this server sees them, quorum, flow control and queues, and InnoDB Cluster routers, in
  Topology, Members and Routers. Read-only. Once a read says the connection is not part of a
  cluster, the sidebar item looks disabled but still opens the place; see
  [The Server place](LOCAL_UI.md#the-server-place).
- **Maintenance** detects tools, checks an upgrade (MySQL Shell, or a labeled
  partial check), and dumps, restores or clones after a read-only preflight
  and a confirmation (`/api/lifecycle/*`, the same work as `myflames
  upgrade-check`, `dump`, `restore` and `clone`). Its badge counts running
  or failed jobs of the connection in use; see [The Server
  place](LOCAL_UI.md#the-server-place).
- **Full analysis** (the second tab in the **Tuning** place) runs every Server view once when you choose **Run full
  analysis** (about 10 to 20 seconds, read-only; `POST /api/live/report`, the
  same report as `myflames report`). It shows the summary and server facts,
  **Look at first**, the problem and watch findings as one-line rows in the
  report's ranking, **Not judged** (checks that could not run, with the
  reason, and views that failed), and the sections (including **Parameters**). A finding's inspector has
  the verdict, numbers, settings, rule, why, what to do with the statement and
  my.cnf line to copy (never applied), the caveat, links to its view (links that name a check open Parameters at that check's first parameter), and the
  statements behind its number with Explain and Run analysis. **Export** saves
  HTML, Markdown, JSON, or text made from the report JSON
  (`POST /api/report/export`). After a connection switch the report stays
  greyed with its server actions off; it is never saved.
- **InnoDB** shows `SHOW ENGINE INNODB STATUS` parsed into sections
  (semaphores, transactions and purge, the latest deadlock and foreign key
  error, file I/O, the change buffer, the adaptive hash index, the redo log,
  the buffer pool, row operations), each as a group of a few one-line rows with
  its status and a one-line summary. The inspector has every value, the rules,
  the parsed numbers, the transaction holding purge back, and links: the
  deadlock to Locks, old transactions to Sessions, the buffer pool and redo
  log to Health, and settings to **Open Parameters**. **Show raw output** reads the raw text only when you ask, for
  support. Without PROCESS, one sentence gives the GRANT to run.
- **Top statements** ranks normalized statements from
  `performance_schema.events_statements_summary_by_digest` by total time, with
  calls, average time, rows examined and sent, and flags for no index used,
  on-disk temporary tables, sort merge passes, and low selectivity (rows sent
  under half of rows examined, SELECT only). Each row has **Explain**, **Run
  analysis**, and **Use this query**; see [Explain a top
  statement](#explain-a-top-statement). MySQL 8.0.3+ keeps a sample of the
  real text (up to `performance_schema_max_sql_text_length`, 1024 bytes by
  default); MariaDB keeps only the normalized text. Performance Schema is off
  by default on MariaDB, so the view says in one sentence how to turn it on
  instead of a list. The toolbar has **Rank by** (total time, average time,
  rows examined, calls, or the statements behind a server number: joins
  without an index, full scans of the first table, reads without an index,
  each listing only digests whose counter is above zero, with that counter as
  a column), the **Schema** filter, a removable **Table: schema.table** chip
  when a link filters to one table (digests that read some table without an
  index and whose text names it: a text match, not proof), and a filter for
  the rows on screen; the notes say what the list covers (uptime, the oldest
  first-seen time, the overflow row, lost digests); one row per statement, sortable, with up to
  three flags. Select a row for the full statement (with **Copy**), latency,
  rows, access and sort counters, and the actions.

  The slow query log is read through Query digest's **Source** (Slow log table,
  Slow log file), which replaced the one-off "Slow queries over 1 s" ranking in
  3.3.1. Counters a slow log does not record are unknown (`null`), rather than
  zero; the rows-examined ratio does not establish an access method.

- **Sessions** (it replaces Running queries) lists the connections with their
  open InnoDB transactions, from the process list: a counter strip (connected,
  limit, max seen, running, history list length), one summary line (active,
  idle in transaction, waiting for a lock, running long, the oldest
  transaction), **Active** or **All**, **Running at least** a number of
  seconds, and a breakdown by state, user, host, database, and command behind a
  disclosure. Each row has a status dot and flags (long running, idle in
  transaction, lock wait, server thread). The inspector shows why it is
  flagged, the statement or the last statement, the transaction, what a lock
  wait waits for (with **Open Locks**), the rules, **Show plan**, and **Kill**.
  **Show plan** opens the plan of that connection's statement: estimated on
  MySQL 8.3+ (`EXPLAIN FORMAT=JSON FOR CONNECTION`), or partial runtime
  statistics on MariaDB 10.9+ (`SHOW ANALYZE`). On MySQL, explaining another
  user's connection needs broad global privileges, not just PROCESS. Prepared
  statements cannot be explained this way. **Kill** uses the same confirmation
  sheet and checks as Locks, and is never offered for server threads. A
  running ALTER TABLE (or other DDL that reports stage counters) shows its
  stage, percentage and estimated time left in the State cell and the
  inspector's **DDL progress**, with **Follow progress** to refresh every 10
  seconds; see [The Server place](LOCAL_UI.md#the-server-place).
- **Locks** shows who is blocking whom, including who blocks a waiting DDL
  statement, and can end one blocker after you confirm. See [Locks](#locks).
- **Users and roles** (`/api/accounts/*`, `POST /api/live/privileges`, the same work as
  `myflames privileges` and `accounts`) lists accounts and roles without password hashes,
  inspects grants and account details, and plans guarded user, password, grant, revoke,
  and role changes with before and after grant diffs. Its **Compare** tab
  compares accounts between two servers and exports the chosen accounts'
  grants as SQL without passwords (`/api/accounts/compare`,
  `/api/accounts/export`, the same as `myflames accounts compare` and
  `accounts export`); nothing is executed.

### Explain a top statement

With **Slow log table**, opening a statement reads its **Tables** section immediately (metadata only, no table rows).

Top statements and Query digest share one statement inspector (`StatementInspector.tsx`) and statement thread (`statementThread.ts`). A selected Top statement opens in a detail view using the available width (in Query digest it opens in the side inspector or sheet); Back restores the list's sorting, filters and scroll. In Top statements and Query digest's **Statement digests** source, a statement flagged **No index used** or **Full scan** offers **Suggested index** under Flags. Choose **Read suggestion** to run `EXPLAIN FORMAT=JSON` for a runnable SELECT and read existing indexes from `information_schema.STATISTICS` (`POST /api/live/index-suggestion`): planning can evaluate expressions, so opening the inspector starts no such read. A fresh result is reused when you reopen the statement; **Read again** reads anew. The result proposes a `CREATE INDEX` to copy or open in the Query editor, names an index already leading with those columns, or gives a reason it cannot suggest one. The narrow `SELECT * FROM t` case can be explained from the recorded text without a read: if `t` is a base table and there is no filter, no index reduces the rows read. When `DIGEST_TEXT` or `QUERY_SAMPLE_TEXT` was cut off, the inspector names the server setting that cut it (`max_digest_length`, `performance_schema_max_digest_length`, or `performance_schema_max_sql_text_length`), its current value, a suggested value, the approximate memory cost, and the option-file or `SET PERSIST_ONLY` lines. Its actions use the statement Performance Schema or the slow log recorded:

- **Explain** and **Run analysis** switch to the Query editor and open the
  statement in a new tab named after its rank and first table (for example
  **Top #3 · orders**). Explain runs there as the editor's Explain does (rows
  in an **Explain** tab, **Explore** for the visual plan); Run analysis
  measures it with the Query editor's time limit, analysis runs, and
  optimizer trace settings, and **Explore** on its **Analysis** tab opens the
  plan in Plans. When the connection has no schema, the statement runs in the
  schema it was recorded in; the tab says which schema it uses. Explain is
  offered for any recorded statement, UPDATE and DELETE included, because
  EXPLAIN does not run it (the server says when a statement cannot be
  explained); Run analysis stays off, with the reason, for statements
  capture refuses.
- **Run analysis** first states what it does: it executes the statement on
  the server, in a read-only transaction that is rolled back, for the chosen
  number of runs, each stopped at the time limit. **Execute and analyze**
  starts it; **Cancel** or Escape closes the note.
- **Use this query** opens the tab in the Query editor without running anything.
- Supported single-table UPDATE and DELETE statements can be projected to `SELECT *` with the original target, WHERE, ORDER and LIMIT. INSERT and REPLACE with a SELECT source can use that source. Review the generated SELECT and warning before starting analysis. This measures target-row selection or source reading only. UPDATE assignments and their subqueries are omitted; write cost, triggers, constraints, locks and changed rows are not reproduced. Batch analysis skips converted rows so each receives its own review.
- Other forms, ambiguous quoting, RETURNING, multi-table targets and unsafe functions are refused with a reason. Write an explicit SELECT when the conservative conversion cannot represent the read you want. Every resulting SELECT still passes capture's independent read guard.

When only normalized text exists (MariaDB always, and MySQL when the sample
reached `performance_schema_max_sql_text_length`), myflames turns each `?`
into a named parameter, `:p1`, `:p2`, and so on, skipping `?` inside quotes,
backticks, and comments. A collapsed list, `IN (...)` or `?, ...`, becomes a
single parameter that takes a JSON list (`[1, 2]` binds `1, 2`), and the
notice says so. Built-in function calls that the digest prints with a space,
such as `COUNT ( * )`, get the space removed so the SQL parses. The tab opens
with a `{"p1": null, ...}` parameters skeleton, a notice to fill in real
values, and a form, **Fill in the values of <tab>**, that lists each
parameter with a few words of SQL around its marker (`` … `customer_id` IN
(▢) AND … ``), a type (Number, Text or NULL; Number where the SQL is numeric:
LIMIT, arithmetic, or a column named like an id or a count) and a value. A
collapsed list takes several values separated by commas. Enter moves to the
next value, then to **Use values**, which writes the values into Parameters
(still the source of truth) and runs nothing; **Later** closes the form and
opens Parameters to edit by hand; Escape closes it and returns to the editor.
A number with more than 15 digits is refused there, since it would lose
precision; write it into the SQL. **Fill in values…** on the notice opens the
form again. Nothing runs until the values are filled in; Explain and Run
analysis refuse to send a digest parameter that is still null (a NULL chosen
in the form counts as filled in).

**Explain top N** (next to the summary line; 5 by default, up to 20) estimates the plans of the first N
statements in the list that run as recorded. It runs EXPLAIN only, never
analyze, one statement at a time, and waits when two captures are already
running. A progress line shows which statement is being explained, and
**Cancel** stops the current capture on the server. Each plan is added to the
open plans without leaving Server. The summary lists each statement
with the optimizer's estimated rows (the sum over its table accesses), key
findings such as full scans and filesorts, and **Open plan**. Statements that
need parameter values, and statements that are not a SELECT, are skipped and
listed by rank. The optimizer trace is not collected for these plans.

A capture started from a digest records where it came from in the
`capture-1.0` object:

```json
"source": {"kind": "digest", "digest": "028d37443170c0c8…", "schema": "shop"}
```

The query tab keeps the same source, so a capture you run later from that tab
records it too. Session restore and saved investigations keep both. Captures
imported from MySQL Shell carry a text label instead (`"mysqlsh-diagnostics"`).

Opening a suggested index in the Query editor keeps its source statement, before
average latency and rows examined per execution, and target connection in page
memory only. After that exact single `CREATE INDEX` succeeds in its tab,
**Measure the statement again** opens the statement in another tab (with a fill
form first if it has normalized placeholders), then asks before one Run analysis
on the same connection. The Analysis tab compares the before averages with the last
captured run's statement-history time and rows examined, without substituting
plan iterator timings for missing counters. **Compare plans** appears there
when an earlier non-session capture of the same SQL, connection and database is
still in memory, and opens both in **Plans > Compare**. The before digest
averages and this single run can have different parameters, workload and cache
conditions; the comparison does not prove that the index caused a change.

### Server health

The Health view takes two samples of `SHOW GLOBAL STATUS` a few seconds apart
and reads a few `SHOW GLOBAL VARIABLES`. It needs no Performance Schema and no
special privilege, so it works on a default MariaDB install too. It reads once
when opened on screen; choose the **Window** (1, 2, 5, 10, 15, 30, or 60
seconds; 5 by default) in the toolbar and press **Refresh** to sample again. The summary line shows the
worst status, the counts, the window, and the time of the last sample; the
facts under it show the server, host, uptime, buffer pool, and
`max_connections`.

**Diagnostics** come first: one line per explained answer, each built from
several deltas, problems first. A row shows its status and the key number (for
example "12.4 MiB/s read from disk" or "Warming: 63% full, about 9 min to full
at this rate"). Select it for the verdict in plain language, the rule it uses,
the explanation, the numbers behind it with units and rates (estimates are
tagged), the settings involved with their current value and engine default, a
trend line, and the status variables it reads. At watch or problem the
inspector also shows **What to do** with its caveat; myflames never changes a
setting.

- **Buffer pool: full or warming.** InnoDB's page cleaner keeps
  `innodb_lru_scan_depth` free pages per buffer pool instance (MySQL 1024 by
  default, MariaDB 1536 with one instance), so full means free pages at or
  below that, not zero. Full with almost no disk reads is OK; full with 10 or
  more pages read from disk per second is watched (a problem when the hit
  ratio is also below 95%), with the bytes per second (pages read times
  `innodb_page_size`) next to `Innodb_data_read` and the share of read-ahead
  pages evicted unused. Not full right after a start, or while it grows, is
  **Warming**, with the growth of data pages and an estimated time to full;
  not full and not growing after a long uptime is **Not full** (room to
  spare).
- **Redo log size for the write rate.** The redo generated per second and per
  hour, how long the redo capacity lasts at that rate (a common rule of thumb
  aims for about an hour at peak; the window may not be the peak), the
  checkpoint age, and the time until InnoDB flushes harder. On MySQL the rate
  is the log sequence number's growth: `Innodb_os_log_written` is shown too
  but rewrites the last partly filled block on each write, so with many small
  commits it runs about twice as high. The capacity in use is
  `Innodb_redo_log_capacity_resized` when the server reports it. The checkpoint
  age is a watch at about 73% of the capacity on MySQL (7/8 of
  `Innodb_checkpoint_max_age` on MariaDB) and a problem at about 79% (all of
  `Innodb_checkpoint_max_age`), where InnoDB forces flushing or makes writes
  wait.
- **Table open cache** (overflows per second are the signal; misses alone are
  normal) and **Table definition cache** (definitions opened while the cache
  is full).
- **Thread cache** (connections that needed a new thread; not applicable with
  a thread pool), **Binary log cache** (transactions that spilled to a
  temporary file; not applicable with the binary log off, MariaDB's default),
  **Open files** (descriptors myflames can account for against
  `open_files_limit`), **Rows read by full scans** (`Handler_read_rnd_next`,
  which also counts internal temporary tables, so no threshold), **Temporary
  tables on disk** (with the in-memory limit for TempTable, MEMORY, or
  MariaDB's Aria, and on MySQL 9.1 and later which limit was hit), and **Sorts
  that spill to disk** (merge passes against `sort_buffer_size`).

The metrics follow in groups, as compact tiles: **Throughput** (statements from
clients, all statements, SELECT, INSERT, UPDATE, DELETE per second),
**Connections** (open, running now with the larger of both samples, failed
attempts, dropped connections, the share of `max_connections` in use),
**InnoDB buffer pool** (hit ratio over the window, pages read from disk, dirty
and free pages, waits for a free page), **Row locks** (waits, time waiting per
second, average wait, sessions waiting now), **Temporary tables and sorts**
(the share of temporary tables on disk, sort merge passes, joins without an
index, full scans), **Table cache** (tables opened, cache in use), and **Redo
and I/O** (redo generated, data file reads and writes, waits for the log
buffer). Each tile shows the value with its unit and a status dot with text:
**OK**, **Watch**, **Problem**, **No threshold** (no universal limit, for
example throughput), or **Unknown** (the server does not report it, or the
counter went down because the server restarted or a status reset ran).
Selecting a tile opens what the metric measures, the rule of thumb behind its
status, the count in the window, and the status variables it comes from. The
rules are deliberately conservative and are rules of thumb, not verdicts.

The inspector of a flagged tile or diagnostic links to the view that shows the
cause: row lock tiles to **Locks**, statements running now to **Sessions**,
temporary tables,
sorts, and joins without an index to **Top statements**, and the buffer pool,
temporary table, filesort, join, and full scan tiles to their lesson in Learn.
Small trend lines follow statements from clients, statements running, row lock
waits, pages read from disk, bytes read from disk, and redo bytes per second
across up to 60 completed readings, small on the tiles and larger in the inspector; the
samples stay in memory and are never saved. **Auto-refresh** defaults to **While
visible** (using the sampling window while Health is on screen), with **Off** and every
10, 30, or 60 seconds; each automatic read is scheduled one full interval after
the previous read finishes (so While visible with a 5 s sampling window reads
for 5 s, then waits 5 s before the next read), never replaces another
connection's greyed report (after a connection switch the report stays greyed
until **Refresh**, and the schedule resumes after it), reads immediately on return unless a failed read is still in backoff, skips a tick while a sample is still running, and
backs off after errors (up to 60 s). **Save baseline** in the Refresh menu keeps the current
Health reading on this computer for that server (`server_uuid` on MySQL, or
`hostname:port` on MariaDB, read in the same call via `with_identity: true`; up
to 20 baselines per server), requiring a verified identity that matches the server reached when saving, and **Compare with baseline** shows the
baseline value under each tile and marks changed statuses (skipping counter
rates when the server restarted since the baseline).

**Live charts** sit above Diagnostics from the first reading: queries per
second, threads running, buffer pool reads from disk per second and
connections in use. With one reading each card shows its title and value
only, under "Charts start with the next reading. Turn on Auto-refresh." (or
"… (every N s)." with Auto-refresh on); from the second, each chart has its
title, current value and the minimum and maximum of the range shown, and a
text summary for screen readers. Readings that never change say "Steady over
the last N readings" instead of a minimum equal to its maximum, as do the
inspector's trend and the tile sparklines. They keep up to 60 readings per
connection in memory (cleared when you switch connections, never saved) and
plot the same numbers the report computes. In a window 900 px wide or
narrower, each tile group's heading names its share of the summary's count
("1 problem, 1 to watch"), so a tile scrolled out of its strip still counts.

**Capture when…** in the toolbar watches for trouble and saves the evidence
for you, like pt-stalk, while you keep Health open. Tick one or more
conditions (**Threads running at or above** a number, **A row lock wait older
than** a number of seconds, **A statement running longer than** a number of
seconds; whole numbers), choose a **Check interval** (5, 10, 30 or 60 s) and
**Stop after the first capture** (on by default), then **Start**. Each check
is one small read-only query set (`POST /api/live/watch`: `Threads_running`,
the process list and `information_schema.INNODB_TRX`). A row lock wait is
timed from InnoDB's own wait start (`trx_wait_started`), never from how long
the statement has run, so a statement that ran for minutes and just began
waiting does not trip it; a wait InnoDB has not timed, or a server read
without the PROCESS privilege, is not judged. A statement waiting for a
metadata or table lock (an ALTER waiting for its lock, for example) is
reported with how long it has run and caught by the statement condition,
never as a lock wait. It checks only while Health is on screen, the window is
visible and the Query editor is not capturing, and only on the connection it
started on; switching connections stops the checks, and while Health is
hidden it pauses and says so. The status line reads "Watching: checked 12
times, last at 14:02:10." in this computer's local time, with **Stop**, and
names any condition a read could not judge (**Not judged: …** or **Partly
judged: …**); an unread value never counts as zero. When a condition is met,
Health reads the processlist, InnoDB status and locks of that connection
(progress beside the status, 1 of 3 to 3 of 3), saves them in a new
investigation named "Capture: <condition> on <connection>, <date and time>"
(local time) and says "Saved capture." with **Open investigation**. It
captures once each time a condition is met: with Stop after the first capture
off, the next capture waits until a check finds no condition met. A capture
already under way finishes even if you leave Health or switch connections,
and its result names its own connection. The investigation lists that
**Evidence** with each item's time and title; select one to read its text,
with **Copy** and **Download**. Credentials in the captured text and data,
such as `IDENTIFIED BY` literals and `PASSWORD('…')`, are redacted before
they are stored. Nothing is changed on the server.

Engine differences the report handles: on MySQL, pages read from disk add
`Innodb_buffer_pool_read_ahead` (linear read-ahead) to
`Innodb_buffer_pool_reads`, while MariaDB already counts read-ahead in
`Innodb_buffer_pool_reads`. MariaDB counts a temporary table that moves from
memory to disk twice in `Created_tmp_tables`, so its on-disk share reads low.
MySQL refuses clients at `max_connections` without counting them in
`Aborted_connects` (they are in `Connection_errors_max_connections`, which
marks the connection limit tile as a problem). MySQL's redo rate comes from
`Innodb_redo_log_current_lsn`. On a MariaDB replica, parallel replication
workers always count as running, so the running limits rise by
`slave_parallel_threads`. Each window includes this tool's own second sample
(about three statements and one connection), which never trips a rule by
itself.

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

### Wait events, index health, I/O, and parameters

Waits, I/O, Indexes, Tables, and Parameters each read when opened on screen
(once per connection; **Refresh** retries), while Indexes and Tables keep their
schema-size consent above 2,000 user tables. Each view shows a loading line under the toolbar, an
error in an alert that keeps the last report, and the notes about the read
behind the **N notes** button. None of these reads truncates a Performance
Schema table or changes an instrument, consumer, or parameter.

- **Waits** reads `performance_schema.events_waits_summary_global_by_event_name`
  twice, a **Sampling window** apart (2, 5, 10, 15, 30, or 60 seconds; 10 by
  default), and lists the wait events with the most wait time over the window:
  waits, total wait, milliseconds of waiting per second of wall time, and the
  average, each with its class; the inspector has its plain-language meaning.
  Wait time is summed across threads, so it can exceed the window, and table
  I/O includes the file I/O and lock waits inside each call, so **Classes**
  lists each class on its own and never adds them up or shows a share.
  **Since server start** switches the table to the counts since the server
  started (or Performance Schema was reset), with the longest single wait; an
  empty window offers it. **Not instrumented** (collapsed) lists the classes
  that are off (mutexes, rwlocks, conditions and sockets by default on both
  engines; MariaDB also turns off the metadata lock instrument); select one for
  the `UPDATE performance_schema.setup_instruments` statement and the
  `performance-schema-instrument` my.cnf line to copy. The inspector of a lock
  wait links to Locks, and of an I/O wait to I/O. The wait summary is cheap to
  read, so **Auto-refresh** (30 or 60 seconds, Off by default) is offered here.
  With Performance Schema off (MariaDB's default), the view says so in one
  sentence with a link to the Instruments row.
- **Indexes** reads the databases chosen in its **Databases** picker. The
  picker lists every database with its table count, has a search box, hides
  the system databases (mysql, sys, performance_schema, information_schema)
  unless you ask, and accepts a typed name. **All user databases** is the
  default only while the server has at most 2,000 tables in user databases
  (MySQL's default table definition cache; the Performance Schema read costs
  time per table, and MariaDB opens every table for its index list); above
  that, Refresh stays off and one sentence says why until you choose. A
  segmented control switches between **Unused** (no reads or
  writes through the index since the server started or since the table's
  indexes changed; PRIMARY and UNIQUE indexes are never listed), marking an
  index that backs a foreign key **Keep: backs a foreign key**; **Redundant
  indexes** (another index starts with the same columns, or is identical; the
  advisor's rules, from `information_schema`, so they work without Performance
  Schema); **Tables read without an index** (rows and read time without an
  index against those through indexes, with **Mostly full scans** at half the
  read time or more; the inspector's **Show statements** lists the statements
  that name the table, labeled "text match, not proof", with Explain, Run
  analysis, and **Open in Top statements** filtered to that table);
  and **No data** (tables with no activity since the counts started, never
  judged). Selecting an index shows the evidence, the statement that makes the
  index invisible (MySQL) or ignored (MariaDB) to test without it, the DROP
  INDEX statement, and the caveat, as text to copy; the actions that send them
  (Set invisible or visible, Set ignored on MariaDB, and Drop index…) appear
  once, in the inspector's action bar. On MariaDB with Performance Schema off and
  `userstat=ON`, usage comes from `INDEX_STATISTICS` and `TABLE_STATISTICS`;
  with neither, Unused and Full scans say so in one sentence. The inspector
  links to **Table health** for that table.
- **Tables** has the same **Databases** picker, with **All user databases** as
  the default at any size, and on MySQL a **Fresh numbers** box. **Filter
  tables** sits with each list's count, Rows, Export and paging, as in Indexes,
  and the Table column keeps at least 220 px for its names. It shows:
  - **Missing primary keys**: base tables without a PRIMARY KEY, sortable by
    table, engine, rows, size, clustered index, and status, filtered by status,
    engine, and name. **Problem** means no unique key on NOT NULL columns
    either: InnoDB clusters the rows on a hidden 6-byte row ID. **Watch** means
    a unique key on NOT NULL columns (whole columns, not a prefix, a
    functional part, or a virtual generated column) stands in for it; the
    clustered index column names it. Tables with MySQL's generated invisible
    primary key (`my_row_id`) are listed as fine. **Why a primary key matters**
    opens in the inspector and explains, for this engine and version, how
    replicas find changed rows, clustering and the hidden row ID, Group
    Replication or Galera, and online schema change tools. Selecting a row
    shows the checks to run first
    (rows with NULL, or a duplicate check to adapt) and the statements to copy:
    make a unique key the primary key with `ALGORITHM=INPLACE, LOCK=NONE` (the
    server refuses instead of blocking writes when it cannot do it online), or
    add an invisible `AUTO_INCREMENT` column, which needs `LOCK=SHARED`, and
    the caveat.
  - **Free space**: `DATA_FREE` reports reusable extents inside an InnoDB tablespace. Known file-per-table free space is informational, including large amounts after deletes. The percentage is an approximation based on reported data, index and free bytes. It does not measure remaining file capacity or filesystem headroom, and it does not establish a need to rebuild. Shared tablespaces are listed separately because the value belongs to the whole space. Missing tablespace metadata remains unknown.
    **Optimize table** is optional maintenance. Review the exact SQL and the warning about metadata locks, disk space for a copy and replica work, then confirm before execution. Reusing allocated space can avoid the cost of rebuilding when the table grows again.
  MySQL keeps these numbers, free space included, in a cache for up to
  `information_schema_stats_expiry` seconds (a day by default; the Statistics
  fact says which you got). **Fresh numbers**
  sets it to 0 for myflames' own connection only, not the server, and takes
  longer on servers with many tables. MariaDB has no cache, so the box is
  hidden there. A table's inspector links to **Indexes** for that table; a link
  opens the other view with the picker set to the table's database and a
  **Showing only** chip until you clear it.
- **I/O** takes two snapshots a window apart (10 seconds by default) and shows
  server-wide InnoDB rates as compact tiles (data read and written,
  operations, fsyncs, redo, pages read from disk; no Performance Schema
  needed), then one table at a time: **Tables** (rows read and written and
  their time), **Files** (bytes and time per file, with its kind: table data,
  redo log, undo, doublewrite, binary log), and **File kinds** (which keep the
  I/O of temporary files closed during the window), over the window or since
  server start. The optional **Schema** limits the table read, which costs
  time on servers with many tables, so there is no automatic refresh. A
  table's inspector links to **Index health** and **Top statements** for its
  schema.
- **Parameters** (formerly Configuration) is modelled on an AWS RDS parameter
  group, with a view toolbar pinned under the top bar. It lists every global
  parameter the server reports in one table: **Name**, **Value**, **Allowed
  values**, **Apply type** (**Dynamic**, **Static**, **Unknown**), **Source**,
  **Data type**, and **Description**. **Filter parameters** searches name,
  value and description, and **Show** filters by **All**, **Modified** (source
  is not the compiled default), **Dynamic**, **Static**, **Recommendations**
  (parameters covered by a check that is Watch or Problem), and **Pending**
  while editing. **Edit** turns the Value column into inline controls (switch,
  list or text box from the parameter's type) for parameters that can change:
  MySQL dynamic (applies now) and MySQL static that can be persisted (applies
  after the next restart through `SET PERSIST_ONLY`), and MariaDB dynamic only
  (MariaDB static parameters are option-file only; parameters whose apply type
  is unknown are not editable in the table). **Preview changes** lists Name,
  Current value, New value, Apply type and When it applies. **Review and
  apply** opens one review for up to 25 changes: the server plans each change
  (exact statement, problems, privileges, effect, undo), MySQL dynamic rows use
  **Keep after restart** (checked: `SET PERSIST`, unchecked: `SET GLOBAL`),
  static rows use `SET PERSIST_ONLY`, and MariaDB rows use `SET GLOBAL` and
  show the option file line to copy. Confirm applies the changes one at a time
  (each re-read and checked first), stops at the first change that is not
  confirmed, and shows a result per row; Cancel or Escape records a declined
  attempt for every planned row in the audit trail. Selecting a parameter shows
  value, default, source with path and who set it when, persisted (MySQL),
  allowed values, scope, apply type, description, and every covering check
  (status, explanation, what to do), with the actions **Stage value for
  review** (stages a new value in the table for Review and apply), **Reset
  to default** (stages `DEFAULT` for MySQL numeric and boolean dynamic
  parameters), **Change this parameter…** (the single-parameter sheet with every mode,
  including **This session only**, which opens the statement in the Query
  editor; see [The Server place](LOCAL_UI.md#the-server-place)), and
  **Reset persisted value…** (MySQL, when persisted). The CLI `myflames config`
  is unchanged (no parameter list, no new flags), and `myflames config set`
  supports `--session` alongside `--persist` and `--persist-only`.

The schema you enter is shared by I/O and Top statements, and a link from
another view fills it in. Indexes and Tables each keep their own database
choice. Session restore keeps the selected view, the schema, both database
choices, and the Waits and I/O windows, never a report. At 390 px the list of
views is its own screen, the inspector is a bottom sheet, and wide tables
scroll inside their own region.

### Performance Schema instruments

The **Instruments** view (after Parameters under Setup) reads on open (one
attempt per connection; **Refresh** retries) and lets you pick what to turn on.
The **Performance Schema** row, preset picker and change banner stay at the top,
above four tabs: **Features** (the "What myflames can show" checklist),
**Instruments**, **Consumers**, and **Actors and objects**. The read is four
small setup tables, so it is cheap. Every change shows the exact SQL and
requires confirmation, including on Development connections; Read-only
connections refuse changes. Running the statements needs UPDATE on
`performance_schema.setup_instruments` and `setup_consumers` (INSERT and DROP,
for TRUNCATE, on `setup_actors` and `setup_objects` to restore them).

- **Performance Schema** (the first row): on or off, the engine default, and its
  own memory, with how to turn it on or off in the inspector. It is read-only
  at runtime, so turning it on or off is a my.cnf line and a restart; on MySQL,
  `SET PERSIST_ONLY performance_schema = ON` and a restart also works. When it
  is off, the rest of the view shows what the engine turns on by default once
  it is on.
- **Presets and change banner**: the presets (**Minimal for myflames**, **Lock
  troubleshooting**, **Wait analysis**, **Back to defaults**, and **Custom**
  when the server matches none) show summary, cost level, and whether the
  server already matches; **Show statements** opens the runtime SQL, my.cnf
  lines, undo and restore statements, and notes in the inspector.
  *Minimal for myflames* turns on exactly what the views read (on MySQL it is
  already the default; on MariaDB it adds the metadata lock instrument and the
  statement current and history consumers). *Lock troubleshooting* adds table
  lock and transaction instruments and the statement and transaction history.
  *Wait analysis* turns on every wait instrument, internal locks included, for a
  short investigation and comes with an undo statement. *Back to defaults*
  returns every instrument and consumer to the engine's compiled-in defaults
  and, when `setup_actors` or `setup_objects` were changed, adds the statements
  that restore them.
- **Features** tab: the "What myflames can show" checklist, one line per view
  that reads Performance Schema (Top statements, capture statistics in the
  Query editor, metadata locks and an idle blocker's last statement in Locks,
  InnoDB row locks on MySQL, Waits, Indexes, per-table and per-file I/O) with
  Ready, Partly, Off, Unavailable, or Not needed and the first thing it needs.
  The inspector lists everything missing, a **Show** link to each row that
  fixes it, and a link to the view.
- **Instruments** tab: search, group filter, state filter (**All**,
  **Enabled**, **Disabled**, **Different from default**), a checkbox per row
  and a header checkbox for every row shown, **Select all shown** (up to
  2,000), and the actions **Enable**, **Enable without timing**, **Disable**,
  and **Clear selection**.
- **Consumers** tab: checkboxes, **Enable**, and **Disable**; enabling a
  consumer also enables the consumers it requires (`global_instrumentation`,
  `thread_instrumentation`, or the matching current consumer) and says so.
- **Actors and objects** tab: whether `setup_actors` and `setup_objects` still
  have their defaults, with the restore statements when they do not.
- **Statements and persistence**: statements are built only from names in the
  report: `UPDATE performance_schema.setup_instruments SET ENABLED = ..., TIMED = ... WHERE NAME IN (...)`
  for timed instruments and `ENABLED` only for untimed ones (memory, error), at
  most 500 names per statement; built-in `memory/performance_schema`
  instruments are skipped by Disable; names containing a backslash are refused;
  consumers use `UPDATE performance_schema.setup_consumers`. They run through
  the existing reviewed SQL runner (exact SQL, confirmation, write policy,
  audit). Runtime changes take effect at once and are lost at restart; **Keep
  after restart** shows copy-only option file lines
  (`performance-schema-instrument='name=ON|OFF|COUNTED'`,
  `performance-schema-consumer-name=ON|OFF`).
- **How changes apply** (in the inspector): mutex, rwlock, cond and socket
  instruments re-apply to existing instances at once, table instruments from the
  next statement on each open table, and a file only once it is reopened;
  metadata locks and memory count only from the moment the instrument is on. A
  history consumer records only what its current consumer records, under
  `global_instrumentation` (statement, transaction and wait events also need
  `thread_instrumentation`), so "On, but blocked by" names the one that is off.
  `setup_actors` changes reach new sessions only.

When another view finds an instrument or consumer off (Waits' Not instrumented
list, Indexes or I/O with Performance Schema off, Locks without metadata locks,
or Top statements with the digest consumer off), its one-sentence note links to
the matching row here, which is selected, shown in the inspector, and takes
focus. MySQL 8.4 and 9.x share the same defaults in source, and MySQL 26.7
matched them live. MariaDB ships Performance Schema off; when it is on, the
metadata lock, transaction and memory instruments are off, and of the consumers
only `global_instrumentation`, `thread_instrumentation` and `statements_digest`
are on.

### Locks

Processlist puts the **Root blocker** pill first in Lock / Trx. In the shared
Kill sheet, **What to end** sits directly below **Statement to send**, before
the remaining details, to keep the choice and exact statement together in the
minimum window.

Parameters and Users and roles show **Changed through myflames** for the newest
matching successful or outcome-unknown local record: readable operation name
(raw key on hover), "Kept on this computer.", boxed SQL with Copy, and **Open in
Audit log** for that exact record. This records changes made through myflames
on this computer, not all changes on the server.

The Locks view reads the server's lock tables once per **Refresh**. One summary
line says what is waiting and on whom, the facts under it count blocked
sessions, root blockers, the longest wait, and open transactions, and the view
shows:

- **Waiting DDL** (first, when there is one): an outline with each waiting
  ALTER TABLE, CREATE INDEX, DROP, RENAME, TRUNCATE, or OPTIMIZE, its blockers
  under it, and the sessions queued behind it. Select a row for the statement,
  its table, how long it has waited, and who holds it up:
  - **Blocked by**: the sessions holding a conflicting metadata lock, from
    `performance_schema.metadata_locks` (MySQL by default; MariaDB with the
    instrument on). The usual culprit, a transaction left idle after it read
    or changed the table, is marked **Usual culprit** with its transaction
    age, rows changed, last statement (when Performance Schema records it),
    user@host, and connection id. A blocker that itself waits names what it
    waits for, and that root blocker is listed too.
  - **Holding a lock on the table**: on MariaDB with the optional
    `METADATA_LOCK_INFO` plugin, which lists granted locks but not waits.
  - **Likely blockers**: on MariaDB without the metadata lock instrument (the
    default) myflames cannot see who holds the table, so it lists the open
    transactions, idle ones and ones that started before the DDL first, and
    the notes say how to get exact blockers. It never changes the server.
  - **Sessions queued behind it**: once a DDL waits for its exclusive lock, new
    reads and writes of the table queue behind it, even though the blocker's
    lock would let them through. That is why one idle transaction and one
    blocked ALTER TABLE can stop an application. Cancelling the DDL
    (`KILL QUERY` on its connection, offered as text to copy) releases them too.
- **Kill**, in the inspector of a DDL blocker, a likely blocker, or a root
  blocker in the tree, appears only when your account may end that session: its own user's
  sessions, or any session with `CONNECTION_ADMIN` or `SUPER` on MySQL
  (`SYSTEM_USER` too for a session of a `SYSTEM_USER` account) or
  `CONNECTION ADMIN` on MariaDB (`SUPER` before 11.0). myflames reads this
  from `SHOW GRANTS`, which includes the roles active at login, and treats it
  as a hint: the server decides, and its refusal is shown as such. Without the
  privilege the inspector says why and offers the `KILL n` text to hand on.
  **Kill…** opens a confirmation sheet with the connection id, user@host, state,
  transaction age, rows changed (a large count means a long rollback that
  keeps the locks while it runs, and the dialog warns), the last or running
  statement, and what it blocks. The choices are **Kill connection** (rolls
  back its open transaction; the default for an idle session, where `KILL
  QUERY` would do nothing), **Kill query only** (only for a running
  statement; its transaction and locks stay unless it runs in autocommit
  mode), and Cancel, which has the focus. Cancel or Escape sends nothing and
  is recorded in the audit trail as declined, with the `KILL` it would have
  sent. The confirm button names the action
  and the id, such as "Kill connection 1234". Before sending `KILL`, the
  server side reads the session again and refuses if it ended, is now a
  different session (another user, host, thread, or transaction start, as when
  an id is reused or the connection switched user), is the session myflames
  uses, runs a myflames statement, is a server thread, or is already rolling
  back its transaction. A "Killed" command alone does not stop it, because
  MariaDB also shows that after `KILL QUERY` on an idle session. On MySQL, a
  session that ran `XA PREPARE` gets no Kill button: its locks belong to the
  prepared XA transaction, which survives the end of the session (on 8.0.29
  and later the session stays listed as their owner), so the inspector offers
  `XA COMMIT` and `XA ROLLBACK` with its xid to copy instead. An ALTER TABLE
  that adds a foreign key or renames the table also locks the parent tables
  on MySQL; the row then names the table it waits on. Then the report refreshes
  and a note says whether the DDL proceeds or what it still waits for. One
  session per confirmation; automatic refresh pauses while the dialog is open
  and never ends anything.
- **Blocking tree**, an outline: each root blocker (a session that blocks
  others and waits for nobody) is marked, with the sessions waiting on it
  nested below, how long each has waited, the lock it wants, and its
  statement. The disclosure triangles, or the Left and Right arrow keys, fold
  and unfold each chain. A blocker that is
  idle with an open transaction is marked **Idle in transaction**; its last
  statement is shown when Performance Schema records it. Row lock waits and
  metadata lock waits share one tree, so a SELECT queued behind an ALTER TABLE
  that waits for a long transaction appears as one chain. In a narrow window
  the lock a session waits for wraps and the statement moves to the
  inspector, so the tree never scrolls sideways. Select a connection
  for its details (the transaction once, with its ID and one age, **Open
  for**) and the plan of its running statement (**Show plan**, the
  same flow as Sessions). For a root blocker the inspector shows **Kill** when
  your account may end it, and `KILL QUERY n` and `KILL n` with a **Copy** button
  to run yourself. `KILL QUERY` stops only the statement, and the transaction
  keeps its row and metadata locks until it ends; `KILL` closes the connection
  and rolls its transaction back. Kill always asks first, a read-only
  connection refuses it, and every attempt is recorded in the audit trail.
- A segmented control then shows one of four tables. **Transactions** from
  `information_schema.INNODB_TRX`: age, state, rows
  locked, lock structs, isolation level, and the current or last statement.
- **Row locks**, filterable by table, with each mode in plain words: next-key
  (the record and the gap before it), gap, record only, insert intention, and
  the table-level intention, S, X, and AUTO-INC locks. Hover or focus a mode
  for the full explanation. Lock data shows the locked key values; it is empty
  when the page is not in the buffer pool.
- **Metadata locks** from `performance_schema.metadata_locks`. myflames pairs
  each waiting request with the granted locks that conflict with it, or the
  waiting request it queues behind, using the server's compatibility rules.
  (`sys.schema_table_lock_waits` pairs every waiting lock with every granted
  one, which overstates the blockers.)
- **Latest deadlock**, parsed from `SHOW ENGINE INNODB STATUS`: each
  transaction, its statement, the locks it held and waited for, and the one
  InnoDB rolled back. **Show raw output** in a transaction's inspector shows
  the raw text.

What each engine can show:

| | MySQL 8.0, 8.4, 9.x | MariaDB 10.6, 10.11, 11.4, 11.8+ |
| --- | --- | --- |
| Row locks | Every lock, held or waiting (`performance_schema.data_locks`) | Only locks involved in a wait (`information_schema.INNODB_LOCKS`); rows locked counts the rest |
| Lock modes | Next-key, gap, record only, and insert intention are distinct | Record-only and next-key both read `X`; gap and insert intention both read `X,GAP` |
| Row lock waits | `data_lock_waits`; needs Performance Schema | `INNODB_LOCK_WAITS`; works with Performance Schema off |
| Metadata locks | On by default | Needs `performance_schema=ON` and `performance-schema-instrument='wait/lock/metadata/sql/mdl=ON'` (both off by default) |
| Last statement of an idle transaction | On by default | Needs the `events_statements_current` consumer (off by default) |
| Without PROCESS | Row locks, waits, and metadata locks still work; transactions and the deadlock are missing | Transactions, row locks, waits, and the deadlock are missing; metadata locks still work |
| Blockers of a waiting DDL | Exact, from `metadata_locks` | Exact with the instrument on; with the `METADATA_LOCK_INFO` plugin, the sessions holding the table; otherwise likely blockers (open transactions) |
| Kill | `CONNECTION_ADMIN` or `SUPER` (and `SYSTEM_USER` for a `SYSTEM_USER` account's session), or your own user's sessions | `CONNECTION ADMIN` (`SUPER` before 11.0), or your own user's sessions |

Implicit locks, the ones protecting rows a transaction inserted or changed,
are listed only after another session conflicts with them, on both engines.

On MySQL before 8.0.40, on 8.1 to 8.4.2, and on 9.0, each read of
`performance_schema.data_locks` or `data_lock_waits` holds InnoDB's global
lock-system latch, which briefly stalls locking for the whole server. The view
says so for those versions. **Waits only** skips the full lock list and the
deadlock, so it reads less, but the wait list still takes the latch.
**Auto-refresh** is Off by default; set to every 10, 30, or 60 seconds, it
pauses while the browser tab is hidden or a Kill sheet is open, and stops
after an error.

## Understand execution context

**Tables & indexes** shows definitions, parsed columns/indexes, and table
statistics collected for referenced tables. **Settings** shows captured server
variables. Missing privileges produce collection notices; they do not fabricate
evidence.

**Measurements** includes each run's plan time, session status changes, and
Performance Schema statement statistics when available. Capture keeps these in
the same session as its EXPLAIN statement. Status deltas include capture and
instrumentation overhead. Performance Schema records the EXPLAIN/ANALYZE
statement, including its result row; these counters are not interchangeable
with the underlying SELECT's returned rows or iterator time. Statement history
requires suitable instruments, consumers, and privileges. See [MySQL statement
event tables](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-statement-tables.html).

Enable **Capture optimizer trace** before a run, then browse the expandable,
searchable trace tree. myflames disables tracing immediately after the target
statement so later collection queries do not replace its trace. Truncation and
privilege restrictions are displayed. A considered alternative is not
necessarily the selected plan. See [MySQL optimizer tracing](https://dev.mysql.com/doc/refman/8.4/en/optimizer-tracing.html).

## CLI equivalents

The command line is deprecated (it keeps working and gets no new features; see the README's Python CLI section). Capture uses the same execution engine as the UI. Bare `-p` prompts for a
password. The default mode produces an estimated plan; `--mode analyze` runs the
SELECT. Output is a versioned `capture-1.0` document that the UI can import.
The root command also accepts `-e` or `--execute` to connect to a server and render a live plan directly.

```bash
myflames capture -h 127.0.0.1 -P 3306 -u analyst -p -D app \
  -e 'SELECT * FROM orders WHERE customer_id = :id' \
  --parameters '{"id": 42}' --mode analyze --repeat 3 --timeout 30 \
  --trace --conditions 'Warm cache, test dataset' -o capture.json

myflames explore explain.json --type workbench \
  --metric estimate_error -o exploration.html
```

Find a statement first, or import evidence that MySQL Shell collected:

```bash
myflames top -h 127.0.0.1 -u analyst -p --order examined --limit 10 --json
myflames capture -h 127.0.0.1 -u analyst -p --statement-digest 028d37443170 --mode analyze -o capture.json
myflames sessions -h 127.0.0.1 -u analyst -p                # summary, flagged sessions, the list
myflames sessions --profile prod --all --min-seconds 10 --json -o sessions.json
myflames sessions --profile prod --explain 42 > running-plan.json   # was: myflames running --explain 42
myflames innodb -h 127.0.0.1 -u analyst -p                  # SHOW ENGINE INNODB STATUS, parsed
myflames innodb --profile prod --fail-on problem -q         # exit 1 on a problem section
myflames innodb --profile prod --raw > innodb-status.txt    # the raw text, for support only
myflames locks -h 127.0.0.1 -u analyst -p                  # tree, transactions, row locks
myflames locks --profile prod --waits-only --json -o locks.json
myflames locks --profile prod --deadlock                   # latest deadlock, parsed and raw
myflames locks --profile prod --kill 42                    # Kill blocker: shows it, asks y/N
myflames health -h 127.0.0.1 -u analyst -p --interval 10   # server health over 10 s
myflames health --profile prod --json -o health.json
myflames health --profile prod --fail-on watch -q          # exit 1 at watch or worse
myflames waits -h 127.0.0.1 -u analyst -p --interval 10    # top wait events over 10 s
myflames indexes --profile prod --schema shop --json -o indexes.json
myflames indexes --profile prod --schema shop --schema billing  # several schemas
myflames tables --profile prod                             # no primary key, free space
myflames tables --profile prod --fresh --fail-on problem -q  # fresh statistics; exit 1 on a problem
myflames io --profile prod --schema shop --interval 15     # table and file I/O
myflames config --profile prod --fail-on problem -q        # exit 1 on a problem
myflames config snapshot save --profile prod --label before-upgrade  # keep global parameters locally
myflames config compare latest profile:prod --fail-on different     # what changed since the snapshot
myflames accounts compare profile:prod profile:staging --json -o accounts.json
myflames accounts export profile:prod --account app@% -o grants.sql  # SQL text without passwords; never run
myflames schema lint live --profile prod -D shop --fail-on watch    # Checks; Not judged never trips
myflames instruments --profile prod                        # P_S instruments: state, gain, cost
myflames instruments --profile prod --preset waits         # one preset's SQL and my.cnf lines
myflames report --profile prod --html -o report.html       # full analysis of every Server view
myflames top --profile prod --order full_join              # statements behind joins without an index
myflames top --profile prod --table shop.orders            # statements that name a scanned table
myflames sql --profile prod -e 'SELECT * FROM users' --format insert --insert-table users -o users.sql # emit INSERT statements
myflames import-shell diagnostics.zip -o capture.json   # then Import in the UI
```

`myflames sessions` adds a DDL PROGRESS section for running DDL that reports
stage counters (for example "ALTER on 42: copying rows (stage/sql/copy to tmp
table) 42%, about 2 min 54 s left (estimate)") and one line naming what to
turn on when stage tracking is off; `--json` carries the same data as `ddl`.
`myflames explore`, `myflames compare` and the root command take
`--hide-literals` to replace literal values with `?` in a plan you share.

`myflames sql` runs scripts from `-e`, `-f` or standard input in one Query
editor session, sharing write safety policies, confirmations, audit logging,
row limits, and output formats (`table`, `csv`, `json`, `tsv`, `markdown`,
`insert`). With `--format insert`, it emits rows as SQL `INSERT` statements
into `--insert-table NAME` (default `result`), quoted as one identifier; a dot is
part of that name, not a schema separator. Typed binary values use hex literals
(`X'...'`). `-o/--output PATH` writes any format to a file; messages stay on
stderr and unsent refusals preserve the file. DROP DATABASE/SCHEMA requires the
exact name from a terminal prompt or `--yes --confirm-target NAME`. DDL and ALTER
review follow the editor rules, including the same-session lock-wait guard;
an ALTER TABLE confirmation adds an `ALTER predicted:` line on stderr with the
same prediction, and its `ALTER preflight:` JSON carries `prediction`.
Live results carry no column types (the mysql and mariadb clients print none
with `--xml`, and myflames sends no second metadata query), so `json` keeps
numbers as strings, `table` quotes them, and `insert` writes numbers and
hex-shown binary values as quoted text. NULL stays NULL in every format.

`top` shows the first 12 characters of each digest in its DIGEST column (the
JSON has the full value). `capture --statement-digest HASH` (with `--digest` as a
deprecated alias) takes the full digest or a
prefix of at least 8 hexadecimal characters, and cannot be combined with `-e`.
It reads the statement from `performance_schema.events_statements_summary_by_digest`;
when the same digest was recorded under several schemas, the row for `-D` wins,
otherwise the one with the most total time. It runs the sample text, uses the
recorded schema as the default database unless `-D` or the profile sets one,
and records `source` in the capture. When there is no runnable sample, it exits
2 and prints the statement with `:p1`, `:p2` parameters for `-e` and
`--parameters`:

```text
myflames: Digest d05d74f87385 (schema shop) has no runnable sample statement: ...
  myflames capture -D shop -e 'SELECT * FROM `orders` WHERE `customer_id` IN (:p1) AND `created_at` > :p2' --parameters '{"p1": null, "p2": null}'
note: p1 replaces a collapsed list of values; give it a JSON list, such as [1, 2], or write the list into the SQL.
```

An unknown or ambiguous digest, a statement that is not a SELECT, and
Performance Schema being off also exit 2.

`locks` prints the blocking tree first, then transactions, row locks, metadata
lock waits, and the latest deadlock; notes about the server go to stderr
(`-q` hides them). `--limit N` (1 to 5000, default 500) bounds each section.
`--waits-only` and `--deadlock` cannot be combined. It exits 0 on success and 2
on bad input or a connection failure. `--json` emits `locks-1.0`: `engine`,
`server_version`, `captured_at`, `scope` (`all`, `waits`, or `deadlock`),
`capabilities`, `notes`, `summary`, `blocking_tree`, `transactions`,
`row_locks`, `waits`, `metadata_locks` (`locks`, `waits`, `truncated`), and
`deadlock`.

`health` prints a DIAGNOSTICS block first (a status marker, the title, the key
number, the verdict, and for watch or problem the rule, the suggestion, and its
caveat), then a compact table grouped like the Health tab, with a status
marker on each line (`ok`, `WATCH`, `PROBLEM`, `-` for no threshold, `?` for
unknown) and a WHY section for flagged metrics; notes go to stderr (`-q` hides
them). `--interval N` (1 to 60, default 5) sets the window. `--fail-on problem`
exits 1 when any diagnostic or metric is a problem, `--fail-on watch` when any
is watch or worse, in the same contract as `check`: 0 otherwise, 2 on bad input
or a connection failure; the FAIL lines list diagnostics first. `--json` emits
`health-1.0`: `engine`, `server_version`, `interval`, `elapsed_s` (measured),
`started_at`, `taken_at`, `uptime_s`, `restarted`, `server` (version, uptime,
`max_connections`, `table_open_cache`, buffer pool size, page size, redo
capacity), `summary` (the worst status of metrics and diagnostics, the metric
counts, and `diagnostics` with the diagnostic counts), `groups`,
`diagnostics`, `metrics`, and `notes`. Each diagnostic has `key`, `title`,
`status`, `state` (`full`, `warming`, or `room` for the buffer pool,
`not_applicable` when the binary log is off or a thread pool runs, otherwise
null), `headline`, `value` and `unit` (the key number), `verdict`, `why`,
`rule`, `numbers` (each with `key`, `label`, `value`, `unit`, `text`, and
`estimate` for projections), `settings` (`name`, `value`, `display`,
`default`), `recommendation` (`text` and `caveat`, only at watch or problem),
`sources`, `learn`, `link`, and `trend` (bytes read from disk and redo bytes
per second). Each metric has `key`, `group`, `label`, `explain`, `unit`,
`value`, `delta`, `rate`, `max`, `status`, `why`, `rule`, `detail`, `link` (a
Server view), `learn` (a lesson key), `sources`, `inputs`, and `sparkline`.

`waits`, `indexes`, `tables`, `io`, and `config` print a compact text report,
notes on stderr (`-q` hides them), and exit 0 on success and 2 on bad input or
a connection failure; `config --fail-on problem|watch` and `tables --fail-on
problem|watch` also exit 1 like `health`. With Performance Schema off, `waits` still exits 0 and its report
says so (`available: false`, `unavailable: "performance_schema_off"`).
`top`, `sessions`, and `io` reject repeated `--schema` flags (accepting at most one).
`indexes` and `tables` take `--schema NAME` more than once, or `--all-schemas`
(every schema except mysql, sys, performance_schema and information_schema).
`indexes` without either reads every user schema and warns on stderr; for
`tables` that is the default. `tables --fresh` sets
`information_schema_stats_expiry = 0` for myflames' own connection (MySQL
only), so row counts and sizes come from InnoDB now instead of a cache up to a
day old. `tables` never runs `ALTER TABLE` or `OPTIMIZE TABLE`: the text output
lists the statements to review, and `--json` adds the checks and caveats.
JSON shapes:

- `waits-1.0`: `engine`, `server_version`, `taken_at`, `uptime_s`,
  `performance_schema`, `interval`, `elapsed_s`, `started_at`, `restarted`,
  `available`, `unavailable`, `coverage` (per class: `enabled`, `timed`,
  `total`, `state`, and `enable` with `runtime` and `config` statements),
  `enable_caveat`, `window` (`status`: `ok`, `reset`, or `restarted`;
  `events` with `count`, `wait_ms`, `avg_ms`, `wait_ms_per_s`, `meaning`;
  `classes`; `untimed`), `since_start` (events with `max_ms`), and `notes`.
- `indexes-1.0`: `scope` (`schema` when one was chosen, `schemas`,
  `server_wide`), `source` (`performance_schema`, `userstat`, or
  null), `available`, `unused` (`verdict` `candidate` or `keep`,
  `foreign_keys`, `why`, `test_sql`, `drop_sql`, `caveat`), `redundant`
  (`dominant`, `reason`, `why`, `test_sql`, `drop_sql`, `caveat`),
  `full_scans` (`rows_without_index`, `rows_with_index`, `share_rows`,
  `fetch_ms_without_index`, `share_time`, `mostly`), `no_data` (`reason`:
  `not_opened`, `no_activity`, `not_instrumented`, or `not_read`), `summary`,
  `more`, and `notes`.
- `schema-2.0` (`tables`): `scope` (`schemas`, `all_user_schemas`,
  `excluded`), `statistics` (`fresh`, `cached`, `cache_expiry_s`),
  `tablespaces_known`, `summary`, `thresholds`, `rules`, `why` (titles and
  texts for this engine and version), `missing_primary_keys` (`kind`
  `no_usable_key` or `not_null_unique`, `status`, `engine`, `rows`,
  `size_bytes`, `partitioned`, `clustered_on`, `unique_keys` with `usable` and
  `why_not`, `why`, `checks` and `fixes` with `label`, `sql` and `template`,
  `caveat`), `generated_primary_keys`, `fragmentation` (`data_bytes`,
  `index_bytes`, `free_bytes`, nullable `free_pct`, `tablespace` `file_per_table`,
  `system`, `general` or `shared`, `tablespace_name`, `status` `ok`, `info`,
  `shared` or `unknown`, `why`, and optional
  `fix_sql`, `online_sql`, `caveat`), `shared_tablespaces`, `more`, and
  `notes`.
- `schemas-1.0` (the workspace picker's list): `schemas` (`name`, `tables`,
  `system`), `user_tables`, `all_limit`, `all_note`, `system`.
- `io-1.0`: `interval`, `elapsed_s`, `scope`, `source` (`performance_schema`,
  `userstat`, or `status`), `global` (rates with `explain` and `sources`),
  `window` (`tables`, `files` with `kind`, `schema`, `table`, `partition`,
  bytes and times, `file_classes`), `since_start`, and `notes`.
- `config-1.0`: `summary`, `groups`, `checks` (`key`, `group`, `label`,
  `explain`, `value`, `status`, `why`, `rule`, `caveat`, `action` with `sql`,
  `config` or `text`, `settings` with `display`, `default`, `set_by`, and
  `alias`, and `sources`), and `notes` (the CLI `myflames config` is unchanged; `/api/live/config` adds `parameters`).

Reuse a connection saved in the workspace (a profile, on the command line), with
its remembered password:

```bash
myflames capture --profile prod -e 'SELECT * FROM orders WHERE id = 1' -o capture.json
myflames top --profile prod --order avg      # -D, --ssl-mode, ... override the profile
myflames profiles list                       # add --json for profiles-1.0 JSON
myflames profiles set prod --type production --read-only   # guard writes on this connection
myflames profiles forget prod                # delete the saved password, keep the profile
myflames profiles delete prod                # delete the profile and its password
myflames audit --since 7                     # writes sent, refused or declined (export: --csv)
```

`--profile` takes a profile name or ID. Explicit flags override the profile's
fields. A missing profile exits 2. Pointing `-h`, `-P`, or `-u` at a different
server, or changing `--ssl-mode` or `--ssl-ca`, also exits 2 unless you pass
`-p`, because the saved
password is only sent to its own host, port, user, and TLS settings.

Start the workspace without restoring the last session:

```bash
myflames ui --no-restore    # clean for this launch; the saved session is kept
```

With `--no-restore`, the UI neither restores nor autosaves during that launch,
so the stored session is unchanged for the next normal launch. The **Restore
workspace on startup** option still turns restoring off for good.

`explore` accepts `--focus NODE_ID`, repeatable `--collapse NODE_ID`, and
`--selected NODE_ID`; IDs come from the canonical sidecar. `--type` accepts the
four chart types plus `diagram`, a compatibility alias for `workbench`. Both commands accept `--help`, send payloads to stdout
when `-o` is omitted, and return exit code 2 for invalid input or failed capture.

CLI server writes share one confirmation helper. The exact statements and
target precede required prompts on stderr, with No as the default. Every
non-terminal write needs `--yes`, including ordinary Development SQL. Typed
guards match exactly, including spaces and case. EOF or Ctrl+C before sending
is an audited decline. Unsent refusals and declines exit 2 with empty stdout,
even with `--json`, and leave `-o` files untouched. Sent and partial results
retain their operation schemas. See the [CLI contract](DEVELOPER_REFERENCE.md#shared-cli-confirmation).

Session restore removes password fields and recognized SQL credential literals
from editor drafts before persistence; ordinary SELECT text is retained.
Editor history uses the actual live session password for redaction even when a
later run omits credentials. An ended editor session remains owned by its
original endpoint and saved profile, and cannot answer another connection's
status or export request.

The [Database diagram guide](LOCAL_UI.md#database-diagram) covers the Schema
view, the **Diagram | Mermaid** switch, SQL/Mermaid/JSON imports, editable layout, SVG/Mermaid export, and model limits.


## API additions

All endpoints are POSTs using the [cookie and origin-proof authentication](DEVELOPER_REFERENCE.md#local-authentication-and-request-limits). The HTML shell contains no API credential.
A `connection` sent by the workspace also carries `type` and `read_only`, what
the connection in use is judged by (part of its key, so reports and
confirmations rebind when they change). The server decides on its own
(`dbops.connection_safety`): it ignores a claimed type on an unsaved connection
and never lowers a saved connection's protection.

| Endpoint | Request | Result |
| --- | --- | --- |
| `schema-diagram/read` | `connection`, `database` | `schema-diagram-1.0` with tables, keys, relationships, coverage warnings and incomplete status; metadata reads only |
| `schema-diagram/import` | `format` (`sql`, `mermaid`, `json`), `text` | Validated diagram; no database connection or SQL execution |
| `schema-diagram/export` | `format` (`sql`, `mermaid`), `diagram` | `text`, `warnings`; the UI requires review before a lossy download |
| `schema-versions/list` | none, or `source_key` | `schema-version-list-1.0`: saved versions newest first, without definitions |
| `schema-versions/get` | `id` | The version's summary and its `snapshot` (`schema-snapshot-1.0`) |
| `schema-versions/save` | `connection`, `database`, optional `name`, `note` | Reads the database (read-only) and saves it as the next version; the summary with `same_as` when nothing changed since the latest version |
| `schema-versions/update` | `id`, `name`, `note` | The renamed version |
| `schema-versions/delete` | `id` | `deleted` |
| `schema-versions/export` | `id` | `filename`, `text` (a `schema-snapshot-1.0` file with its name and note) |
| `schema-versions/import` | `text` (at most 36 MiB of UTF-8; snapshot validation and storage add their own bounds) | Saves the file as the next version of its database; the summary with `same_as` |
| `schema-versions/compare` | `source` (the schema to match), `target` (the schema that would change), each `{version: id}` or `{live: {connection, database}}` (each live side gets its own saved password), `ignore_definers`, `markdown`, optional `chosen_keys` | `schema-compare-1.0` with the sync script that changes the target to match the source (never run; by default without objects only in the target), `comparison_id` and, with `markdown: true`, its Markdown export |
| `schema-versions/sync` | `comparison_id`, `chosen_keys` (comparison keys, `namespace:name`) | The same comparison with the sync script rebuilt for that choice (`sync.chosen_keys`, `omitted`, `skipped`, `objects[].blocked_by`), from snapshots kept in memory for ten minutes; it reads no server and takes no DDL from the request. An expired `comparison_id` asks for Compare again |
| `schema-versions/lint` | `id` (a saved version, `latest` or `live`), optional `connection`, `database` (`live` needs both and saves nothing) | `schema-lint-1.0`: `findings` (Watch) and `not_judged` (with reasons). Reads only; nothing is sent to change a server |
| `schema-versions/export-sql` | `id`, or `live` (`connection`, `database`), or `export_id`; `database`, `create_database`, `keep_definers` | The Export as SQL script (`schema_export.py`) with its warnings, notes and summary; a live read returns `export_id` for option changes. |
| `investigations/list` | optional `limit` (1 to 50, default 25), `cursor` (the previous page's `next_cursor`), `search` (name, tags or notes, ignoring case) | `investigation-list-1.0`: `items` (summaries newest first: `id`, `updated_at`, `name`, `notes`, `tags`, `baseline_id`, plan `id`, `name` and capture mode; never the plans) and `next_cursor` (null on the last page) |
| `investigations/get` | `id` | The saved bundle with `id`, `updated_at` (what Open, Compare to baseline and Export read) |
| `investigations/save` | `value`: bundle, optional `id` | Saved bundle with `id`, `updated_at`. A bundle holds at least one plan or one `evidence` item (at most 30 each: `sessions`, `innodb`, `locks` or `watch` reports with their text; data over 2 MiB, or a bundle over 10 MiB, is trimmed before the text, with a note; every string of an evidence item is stored with credentials such as `IDENTIFIED BY` literals redacted) |
| `investigations/delete` | `id` | `deleted` |
| `profiles/list` | `{}` | `items` (settings plus `type` (`production` or `development`), `read_only`, `has_saved_password`, `secret_backend`, `secret_backend_label`) and `secret_store` (`backend`, `label`, `available`) |
| `profiles/save` | `value`: settings (optionally `type` and `read_only`; omitted, the stored values stay, and a new profile is `production`, read-write), optional `id`, `password`, `remember_password` | Saved profile without the password. `remember_password: false` deletes a saved one |
| `profiles/forget-password` | `id` | Profile with `has_saved_password: false` |
| `profiles/delete` | `id` | `deleted`; also deletes the saved password |
| `news` | optional `refresh` (boolean; reads again when the last read is at least a minute old) | `news-1.0`: `enabled`, up to six `items` (`title`, `link`, `source`, `source_id`, `engine`, `published` UTC or null, `summary`), `fetched_at`, `stale`, optional `error` and `offline`, and `sources` with each feed's last read. With the Settings switch off: `enabled: false` and nothing read |
| `news/settings` | optional `enabled` (boolean) | `enabled`, the Settings switch **Show MySQL and MariaDB news on Home** (default on, stored in the workspace store) |
| `connection/test` | `connection` | `version`, `engine`, `flavor`, `product`, visible `schemas`, `databases` (at most 1,000 from SHOW DATABASES, with `databases_truncated`) or `databases_error` (the test still succeeds), and `through_ssh` ("Through SSH host X") for a connection through SSH. Identity uses both version and version comment, including MariaDB with a numeric-only version |
| `live/readiness` | `connection` (a saved `profile_id` fills its password) | The last-known server facts: `engine`, `flavor`, `product`, `version`, `read_only`, `super_read_only`, `performance_schema`, `replica`, `group_member`, `galera_member` (booleans or null when unavailable), `privileges`, `actions` (the sessions-all, kill and replication-status decisions), `notes` and `checked_at`. One bounded read-only invocation (3 s budget by default) plus up to two follow-up invocations for MariaDB replica status (`SHOW ALL SLAVES STATUS` or `SHOW SLAVE STATUS`) and ProxySQL Admin fallback, each capped at 64 MiB of output and 1 MiB of error text; no writes |
| `connections/import` | `tool` (`workbench` or `dbeaver`), optional `choose` (true opens a fixed-argv native file dialog) | `items` (name, host, port, user, database, `type: "production"`, optional SSH fields), `found`, `cancelled`. Reads only the tool's own connections file (fixed default locations or the chosen file, at most 2 MiB and 1,000 connections); no passwords or keys are read or returned; nothing connects |
| `ssh/hosts` | none | `ssh-hosts-1.0`: `hosts` (the Host aliases of `~/.ssh/config` and its Includes, at most 500; no patterns or negations), `ssh_found`, `message`. Runs no ssh and reads no network |
| `live/start` | `connection`, `query`, `parameters`, `mode`, `repeat`, `timeout`, `trace`, `conditions`, optional `source` (`{"kind": "digest", "digest", "schema"}`, copied into the capture) | Job `id`, `state` |
| `live/status` | `id` | `state`, `progress`, `repeat`, `error`, optional `result` |
| `live/cancel` | `id` | Current job snapshot |
| `live/top` | `connection`, optional `limit`, `schema` (the shared schema filter), `order` (`total`, `avg`, `examined`, `count`, or the attribution orders `full_join`, `scan`, `no_index`), `table` (`"schema.table"` or `{schema, table}`), `with_identity` (boolean; attaches `reading_server` from `baselines.read_with_identity` in the same call, plus `coverage.oldest_first_seen`) | `top-1.0` statements and notes. Each statement has `capture_problem`: null when Run analysis (and Explore) can capture it, otherwise the reason (Explain in the editor does not need a capture), and the attribution counters `select_full_join`, `select_range_check`, `select_scan`; with `table`, each has `table_match` and the report has `table` and `match` (label "text match, not proof", caveats). `coverage`: uptime, the oldest `first_seen`, the overflow row, `digest_lost`, `counting_since` (the oldest FIRST_SEEN of the whole digest table when it began more than an hour after the server started, else null). Ranked against the whole server in one more aggregate read: `totals` (`digests`, `total_ms`, `executions` of every digest row), `others` (the digests the list does not hold, or null), `cpu` (`{collected}` on MySQL with SUM_CPU_TIME: false while the `events_statements_cpu` consumer is off, null when unread; null on MariaDB), and per statement `cpu_ms`, `p95_ms`, `p99_ms`, `p999_ms` (null where the server has no such column) and `new` (first seen in the last 24 hours, decided on the server, and only while the counters cover more than a day) |
| `live/index-suggestion` | `connection`, `digest`, optional `schema` | Re-reads a server-owned statement by digest, never accepts client SQL, and returns `status` (`no_predicate`, `suggestion`, `covered`, `not_explainable`, `unknown`) with `reason` and optional `ddl` and `caveats`. Reads `EXPLAIN FORMAT=JSON` for a runnable SELECT in a read-only transaction and existing indexes from `information_schema.STATISTICS`; planning can evaluate expressions, so the UI requires **Read suggestion** or **Read again**. A proven unfiltered SELECT-star can answer from stored text. No schema-version wrapper and no index creation |
| `live/digest-histogram` | `connection`, `digest` (32 to 64 hexadecimal characters), `schema` (the statement's default schema; empty or null for none) | `digest-histogram-1.0`: the buckets of `performance_schema.events_statements_histogram_by_digest` with at least one call (`bucket`, `low_ms`, `high_ms` null for the open-ended last bucket, `count`). MariaDB and servers without the table answer `available: false` with a `reason`. Query digest reads when the Latency section is on screen; Top statements reads only after its **Latency histogram** click |
| `live/digest-tables` | `connection`, `schema` (the statement's default schema, or null), `text` (normalized digest text or sample SQL, at most 1,000,000 characters) | `digest-tables-1.0`: `method` ("statement text"), `tables` (at most 20: `schema`, `name`, `found`, `type`, `engine`, `rows_estimate`, `data_bytes`, `index_bytes`, `indexes` with `name`, `unique`, `columns`, `visible`) and `notes`. Names come from the statement text; at most two bounded `information_schema` reads, never row data. A name without a schema stays unread. Read when the statement's Tables section is on screen |
| `live/table-structure` | `connection`, `schema`, `table` | `table-structure-1.0` (`schema_browser.table_structure`); read-only; the saved password is filled like the views. |
| `live/slow-log` | `connection`, `order`, `limit`, `from`, `to`, `user` | `slow-log-1.0` read from `mysql.slow_log` (Query digest, Slow log table); validated before connecting, 30 s. |
| `slow-log/choose` | none | The system file chooser for a slow query log; `file_id` and the file name, or `cancelled`. |
| `slow-log/open-path` | `path` | The same as `slow-log/choose` for a typed absolute path (a regular readable file of at most 4 GiB). |
| `slow-log/read` | `file_id`, optional `progress_id`, `order`, `limit`, `from`, `to`, `user` | `slow-log-1.0` summed by statement fingerprint (Query digest, Slow log file); at most two reads at once. |
| `slow-log/progress` | `progress_id` | `slow-log-progress-1.0`: bytes only, no SQL and no path. |
| `slow-log/stop` | `progress_id` | Stops that read. |
| `live/report` | `connection`, optional `interval` (2 to 60, default 10), `schemas` (or `schema`), `all_schemas` | `report-1.0` Full analysis: header (server, views, failures), summary, `look_first`, ranked `findings`, `sections`, `attribution`, `checklist`, notes. One run per connection at a time; an identical request joins it |
| `report/export` | `report` (a `report-1.0` report), `format` (`html`, `markdown`, `json`, `text`, `digest`), optional `source` (`label`, `host`, `port`, `user`, `type` of the connection it was read from; other fields are dropped) | `{format, content, mime, filename}`, generated from the JSON; every format names the source connection |
| `live/running` | `connection`, optional `min_seconds` | `running-1.0` statements. Kept for compatibility; `live/sessions` replaces it |
| `live/sessions` | `connection`, optional `active_only` (boolean, default true: sessions running a statement, idle in a transaction, or waiting for a lock), `user`, `host` (client host, with or without the port), `db`, `min_seconds` (0 to 31536000), `limit` (1 to 10000, default 1000) | `sessions-1.0` report: `summary` over every visible session (connections against `max_connections`, counts by command, state, user, host and database, flag counts, the oldest open transaction, the history list length, a one-line `text`), `attention` (long-running, idle in transaction, lock waits), and `sessions`, each with its client host and port, statement, open InnoDB transaction (`trx`), `lock_wait`, MariaDB progress, `flags`, `status`, and `why`; plus `capabilities` (PROCESS), `rules`, and notes. The filters narrow only `sessions`. Blocking: `blocking_edges` (`{waiter, blocker}` connection-ID pairs, collected before the filters and limit, at most 5,000), per session `blocked_by`, `blocks`, `blocks_ids`, `blocks_total`, `root_blocker`, `longest_wait_s`, and `summary.root_blockers` |
| `live/sessions/history` | `connection`, `id` (a connection ID, 1 to 4294967295, checked before connecting) | The session's up to ten most recent statements from Performance Schema history: `{id, available, reason, limit, statements}` with `event_id` (a decimal string), `sql` (cut at 4,096 characters), `duration_ms`, `rows_examined`, `rows_sent`, `errors`, `errno`, `message`. At most four reads, 5 s each; disabled instrumentation or missing privileges return `available: false` with the reason; never enables anything |
| `live/ddl-progress` | `connection`, `connection_ids` (1 to 200 positive integers) | `ddl-progress-1.0`: `available`, `reason`, `missing` (consumers or instruments to turn on), `source` and `items` (`id`, `stage`, `stage_label`, `percent`, `completed`, `estimated`, `elapsed_s`, `left_s`, `statement_kind`, `source`, `units`, optional `note`). One metadata read and one bounded progress read (on MariaDB a second native read when the first is refused); query limit 5 s; never changes instrumentation. `live/sessions` carries the same object as `ddl` |
| `live/innodb` | `connection`, optional `raw` (boolean, default false) | `innodb-1.0` report: `SHOW ENGINE INNODB STATUS` parsed into `sections` (semaphores, transactions and purge, latest deadlock, latest foreign key error, file I/O, change buffer, adaptive hash index, redo log, buffer pool, row operations, background), each with `summary`, `items` (label, display text, status, rule), parsed `data`, and `links`; plus `header` (print time, averaging window), `summary`, `parse` (what was found), `rules`, and notes. `raw.text` only with `raw: true`. Without PROCESS: `available: false` and `error.code: "no_process"` |
| `live/explain-connection` | `connection`, `connection_id` | Plan `raw`, `mode` (`explain` or `analyze-partial`), and a `capture-1.0` object for the viewer |
| `live/locks` | `connection`, optional `limit` (1 to 5000, default 500), `waits_only` | `locks-1.1` report: DDL statements waiting for a metadata lock with their blockers (`exact`, `granted_locks`, or `likely`) and queued sessions, blocking tree, transactions, row locks, waits, metadata locks, latest deadlock, `kill_privilege`, per-session `kill_allowed`, capabilities, and notes |
| `live/kill/prepare` | `connection`, `connection_id`, `mode`, `expect` | Exact reviewed kill details with a server-issued `review_token`; reads only. Apply must return the token with the same details |

| `live/kill` | `connection`, `review_token`, `connection_id`, `mode` (`connection`, the default, or `query`), `expect` (`user`, `host`, and when known `trx_started` and `ps_thread_id`, as the report showed them) | `kill-1.0` result: `status` (`killed`, `refused`, `denied` by the server, or `gone`), `message`, the `statement` sent, what was `verified`, the re-read `target`, the session `after` the KILL, and `audit_id`. `killed` acknowledges an accepted KILL; unavailable verification leaves `after: null` and says "Sent; could not verify". On a read-only connection (the saved profile's setting, or `connection.read_only: true`) nothing is opened and the result is `refused` with the reason. myflames re-reads the session first and sends nothing when it changed. Refusals are results; bad input returns 400. One session per request, sent only from the confirmation dialog after `live/kill/prepare` issues a one-use `review_token` |
| `audit/list` | optional `since_days`, `server` (saved connection name, host, or host:port), `status` (one audit status), `id` (a 32-character lowercase hexadecimal audit id, to filter to one record), `limit` (default 500), `before` (the id of the last record listed: the next page) | `audit-1.0`: `retention_days`, `has_more` (older records match), and `records`, newest first (every filter applies before `limit`), each with `status` (`pending`, `ok`, `error`, `declined`, `refused`, `unknown`), `outcome_unknown`, `outcome`, `surface`, `os_user`, `operation`, `connection`, the redacted `statements`, `target`, `verified`, `policy`, `error_code`, `message`, `after`, `duration_ms`. No server connection |
| `audit/export` | `format` (`json` or `csv`), optional `since_days`, `server`, `status`, `id` | `format`, `content`, `mime`, `filename` for every matching record |
| `audit/object` | `server` (the `host:port` endpoint as recorded, never a saved-connection label), `kind` (`parameter`, `account` or `index`), `key` (the parameter name, `{name, host, role}` or `{schema, table, index}`) | `audit-1.0` with `records`: up to five newest matching `ok`/`unknown` records. Local store read only, no server connection; an index matches only records that name it, never a whole-table change |
| `audit/settings` | optional `retention_days` (1 to 3650) | `retention_days` (default 90); a lower value purges older records now |
| `editor/open` | `tab_id` (1 to 100 of `A-Z a-z 0-9 _ . : -`), `connection`, optional `safe_updates` (true, false or null for the connection type's default: on for Production, off for Development), `idle_minutes` (1 to 1440, default 30), and `confirmed` with `confirm_token` or `decline` when turning safe updates off | `status`, `session` (`editor-session-1.0`: `state` starting, idle, running or ended; `connection` name, host, port, user, `type`, `read_only`; `engine`, `server_version`, `connection_id`, `autocommit`, `in_transaction` (true, false, or null when unknown) with `transaction_source` and `transaction_note`, `sql_mode`, `backslash_escapes`, `safe_updates`, `read_only_transactions`, `idle_seconds`, `idle_timeout_seconds`, `ended`, `ended_reason`, `message`, `notes`, `run` id and state). Starts the tab's client session, or returns the open one; a different connection for the same tab is refused (409 `identity_mismatch`), including for ended sessions and racing requests. Ended sessions compare endpoint and saved-profile identity; explicit reconnect for the same owner evaluates fresh policy. Errors do not reveal another connection's name. `safe_updates: false` is a write that lowers protection (`SET SESSION sql_safe_updates = 0`): it follows the write policy (Production answers `needs_confirmation` before opening anything) and is audited as `editor_safe_updates`; `true` is applied without asking |
| `editor/run` | `tab_id`, `connection`, `sql`, optional `selection` (`{start, end}` in UTF-16 offsets of `sql`; only that part runs, split with the delimiter in effect where it starts) or `cursor` (a UTF-16 offset: the server runs the one statement the caret chooses, found with its own splitter and the session's reading of backslashes; scope `statement`), `on_error` (`"stop"`, the default, or `"continue"` for a full-tab run without `selection` or `cursor`), `limit` (rows kept per result set, 1 to 100,000, default 1,000), `timeout` (seconds, 0 for none), `wait` (seconds to wait for the result, 0 to 60, default 10), `confirmed` with `confirm_token`, `typed_target` for DROP DATABASE/SCHEMA, `decline`, and `safe_updates: true` / `idle_minutes` when this run starts the session (`safe_updates: false` is refused here; use `editor/open`) | `status`: `complete`, `error`, `stopped`, `running` (poll `editor/status`), `session_ended`, `needs_confirmation` (with `confirmation`: a one-time `token` valid `expires_in` seconds (300) for exactly the statements shown, `title`, `text`, `tone`, `kind`, `category`, `typed_target` (exact database name or null), `alter_preflight` (table, size, locks, transactions, coverage, warnings, optional rewrite), `reasons`, `warnings` (statements that commit a transaction that is or may be open; such statements always ask, also on Development), `in_transaction`, the exact `statements` and `count`, `connection_type`, `connection`), `refused` or `declined` (both with `audit_id`), or `invalid` (`code`, `message`: a statement myflames cannot send, nothing sent). A run has `run` (`editor-run-1.0`: `id`, `state`, `scope`, `limit`, `duration_ms`, `on_error` (`"continue"` for a full-tab Continue run, otherwise `"stop"`), `statements` with `index`, `text`, `start`, `end`, `verb`, `kind`, `status` ok, error, stopped (interrupted by Stop and undone), not_run, session_ended (the session ended while it was out: whether it ran is unknown), `duration_ms`; `results` in order, each `rows` (`statement`, `columns` with `name`, `hex` (a guess: every value looks like the client's 0x form), `type`: null, `columns_known`, `rows` of null or text (empty while the run is still running), `row_count`, `truncated`, `stopped` (`row_limit`: one plain read stopped with KILL QUERY; `row_limit_read`: anything else read to its end with the extra rows dropped; `size_limit`, `size_limit_read`), `label`, `server_rows`, `warnings`, `duration_ms`), `ok` (`affected_rows`, `info`, `warnings`) or `error` (`code`, `sqlstate`, `message`, `interrupted`, `position` with `offset`, `line`, `column`); the first `error`, `stopped` (`by`, `interrupted`, `message`), `counts`, `audit_id`, `history_id`) and `session`. A second run while one runs is 409 `busy`; an audit record that cannot be written is 400 `audit_failed` and nothing is sent |
| `editor/preview` | `tab_id`, `connection`, `sql`, optional `selection` (`{start, end}` in UTF-16 offsets) or `cursor`, as for `editor/run` | Read-only planning with the same `_plan_run` and classification as Run. Returns `status: ready`, `scope`, `statements[] {index, kind, action_keys, state: allowed\|denied\|unknown, missing, reason}`, `aggregate {state, missing, reason, warning}`, and `policy {action, text}`; invalid SQL returns `status: invalid`, `code`, `message`, empty statements and an unknown aggregate. Uses only the workspace's cached `privileges-1.0` report for the same connection identity; no connection, saved-password lookup or SQL is sent, and no SQL or credentials are echoed. Missing reports, unmapped statements and sessions that changed roles are unknown. Only an all-denied batch disables Run; mixed batches warn, unknown remains enabled. Read-only policy refusal is independent |
| `editor/status` | `tab_id`, optional `run_id`, `wait` (seconds, 0 to 60), optional `refresh_insights` (true refreshes the insights once per 10 s per tab, skipping an active run) | `status`, `session`, and `run` (the current or last run, with results once it finished); `status: "none"` when the tab has no session. With `refresh_insights` the session can carry `transaction_meter` (`age_seconds`, `age_source`, `rows_modified`, `rows_locked`, `lock_structs`, `blocking`, `blocking_truncated`, `note`; null when no transaction is open), and a run's statements can carry `server_stats` (rows examined, returned and changed, index and scan counters, temporary tables, `errors`, `warnings`) |
| `editor/stop` | `tab_id`, `connection`, optional `force` | `stopped` (true only when KILL QUERY was sent), `stop_requested`, `message`, `session`. Sends `KILL QUERY` for the session's own connection id from a second connection; later statements of the run do not run. A statement it interrupts ends with an error and is undone (status `stopped`); one that had already finished took effect (status `ok`). A statement still running 15 seconds after the KILL ends the session (`ended_reason: "unresponsive"`). `force: true` ends the session |
| `editor/commit` | `tab_id`, `connection`, optional `confirmed` with `confirm_token`, `decline` | Like `editor/run` for a tagged `COMMIT`: a write, confirmed on Production and audited |
| `editor/rollback` | `tab_id`, `connection` | Like `editor/run` for a tagged `ROLLBACK`; no confirmation |
| `editor/close` | `tab_id`, optional `discard` | `closed`, `session`. Unless `discard: true`, a session whose transaction is open or unknown is not closed: `closed: false`, `reason` `transaction_open` or `transaction_unknown`. Ending the client rolls the transaction back |
| `editor/use` | `tab_id`, `connection`, `database` (nonblank, 1 to 64 characters, no control characters) | With no session, `{status: "none", database, session: null}`; with an ended session, `status: "session_ended"`; otherwise the session state, `database` and `session`. `_use_database` acquires the run lock, sends tagged `USE` with backquotes doubled, and verifies `SELECT DATABASE()` using the session helper timeout (30 s). Busy or another connection's session is refused |
| `editor/file/open` | `{}` | Native `.sql` / `.txt` chooser (`file_picker.choose_sql_file`, 120 s), followed by a bounded regular-file read (5 MiB, `SQL_MAX_BYTES`, checked even if the file grows). A file or folder swapped for a link after selection is refused. Returns `{cancelled: false, name, sql, token, warning}` or `{cancelled: true}`; no path is returned. UTF-8 BOM stripped; invalid bytes become U+FFFD with a warning. Local only |
| `editor/file/save` | `sql` (up to 8,000,000 characters), optional `token`, `save_as: true`, `name` | The token binds a backend path and parent identity; Save as or no token uses `file_picker.choose_save_file`. Returns `{cancelled: false, token, name, path}` or `{cancelled: true}`. Atomic UTF-8 without BOM; symbolic-link targets and changed parent folders refused. Adding `.sql` never overwrites a file; choose its full name to replace it. An expired binding returns `unknown_token`; the UI opens Save as in the same Save operation. Local only; renderer-supplied paths are never used |
| `editor/history` | optional `action` (`list`, the default, or `clear`), `limit` (default 200), `profile_id` | `items` newest first (`at`, `tab_id`, `connection` profile_id, name, host, port, `engine`, `kind`, `scope`, redacted `sql`, `sql_truncated`, `statements`, `statements_run`, `duration_ms`, `rows`, `affected_rows`, `status`, `error_code`), `max_entries`, `max_bytes`; or `cleared` |
| `editor/saved/list` | optional `limit` (1 to 500, default 500) | `items`: `saved-query-1.0` records (`docs/schemas/saved-query-1.0.json`: `id`, `name`, `folder`, redacted `sql`, `note`, `database`, `created`, `updated`), the most recently saved, renamed or moved first; `max_entries` (500), `max_bytes` (4 MiB), `max_sql` (20,000 characters, the history's cap). Local only: no connection |
| `editor/saved/save` | `name` (1 to 200 characters, one line), `sql`, optional `folder` (up to 200, empty for none), `note` (up to 2,000), `database` | `saved` (the record with its `id`) and `redacted` (true when a credential in the SQL was replaced). The SQL, name and note go through `dbops.redact`, as the history, before they are stored; SQL longer than the cap after redaction is refused, and so is a new query when 500 are kept or the store would pass 4 MiB (nothing is dropped) |
| `editor/saved/rename` | `id`, `name` | `saved`: the record with its new (redacted) name and `updated` |
| `editor/saved/move` | `id`, `folder` (empty or null for no folder) | `saved`: the record in its new folder |
| `editor/saved/delete` | `id` | `deleted: 1`; an unknown `id` is an error ("no longer kept") |
| `editor/export` | `format` (`csv`, `tsv`, `json`, `sql`, `markdown`), `result` (`columns`: names or `{name, hex, binary, type}`, `rows`), `table` and optional `schema` for `sql`, `engine`, `options` (`header`, `bom`, `formula_guard`, `objects`, `rows_per_insert`) | `format`, `content`, `mime`, `filename`. SQL writes `X'..'` only for columns marked `binary: true` (confirm the `hex` guess first); other values stay quoted text. No server connection; nothing from the connection is included |
| `audit/declined` | `operation`, `statements` (as the confirmation showed them), optional `target`, `connection` | `audit_id`, `status: "declined"`: records a cancelled confirmation; nothing is sent to the server. The workspace calls it when a Kill sheet is cancelled (Cancel or Escape) with the tagged `KILL` statement, the identity it would have confirmed (`connection_id`, `mode`, `user`, `host`, `trx_started`, `ps_thread_id`), and the same `connection` the Kill would have used |
| `live/health` | `connection`, optional `interval` (1 to 60 seconds, default 5), `with_identity` (boolean; attaches `reading_server`; a verified server or version change between samples is refused, while an unverifiable identity leaves `verified: false`) | `health-1.0` report: diagnostics (headline, verdict, why, rule, numbers, settings, recommendation, trend), metrics with value, delta, rate, unit, status, why, rule, link, and learn, plus summary, server facts, and notes. The request takes the sampling window to answer |
| `live/watch` | `connection`, optional `limit` | `watch-1.0`: `threads_running`, `longest_statement`, `longest_lock_wait` (how long an InnoDB transaction has waited for its row lock, from `INNODB_TRX.trx_wait_started` only), `longest_waiting_statement` (a statement waiting for a metadata or table lock, with how long it has run; never judged as a lock wait), `read_at`, `errors` (per field), `notes` and `reason`; one bounded read for Health's **Capture when…** (Threads_running, the process list and `INNODB_TRX`, never `data_lock_waits`); a failed source is named in `errors` and leaves its field null |
| `live/watch/capture` | `connection`, optional `watch` (the snapshot that tripped) | The `evidence` items for an investigation: the processlist, InnoDB status and locks reports with their text, and the watch snapshot |
| `live/waits` | `connection`, optional `interval` (2 to 60 seconds, default 10), `limit` (1 to 200, default 20) | `waits-1.0` report: wait events over the window and since start, classes, coverage with enable statements, and notes |
| `live/indexes` | `connection`, optional `schemas` (a list; the older `schema` still works), `all_schemas: true`, `limit` (1 to 5000; omitted collects the full scoped inventory, capped at 100,000 rows per category) | `indexes-1.0` report: full index inventory, unused, redundant, full scans, no data, summary, and notes. Without `schemas` it reads every user schema when they hold at most 2,000 tables, and otherwise returns 400 with the reason after one count; `all_schemas: true` skips the count |
| `live/tables` | `connection`, optional `schemas` (a list; none means every user schema), `fresh` (boolean, MySQL: `information_schema_stats_expiry = 0` for this connection only), `limit` (1 to 5000; omitted collects the full scoped inventory, capped at 100,000 rows per category) | `schema-2.0` report: full table inventory, tables without a primary key with checks, fix statements and caveats, generated invisible keys, free space per InnoDB table, shared tablespaces, summary, and notes. Read-only: the statements are text |
| `live/catalog` | `connection`, `schema`, optional `table`, optional `group` | `catalog-1.0` for the Query editor's schema browser (`schema_browser.py`): without `table` or `group`, `level: "tables"` with the schema's `tables` (`name`, `type`: table, view, system view or sequence) in name order, at most `limit` (5,000) with `truncated`; with `table`, `level: "table"` with `columns` (`name`, `type`, `nullable`, `key`, `default`, `extra`, `generated`) and `indexes` (`name`, `unique`, `primary`, `type`, `columns` with `name`, `expression`, `sub_part`); with `group` (`routines`, `triggers` or `events`), `level: "objects"` with the schema's `objects` in name order (routines carry `name` and `kind` (procedure or function), triggers `name`, `table`, `event` and `timing`, events `name` and `status`), at most `limit` (200) with `truncated`. Reads `information_schema` only, tagged `/*myflames:editor*/`, one schema at a time; names are sent as hex literals. A missing table or one without privileges is a 400 error. The saved password is filled like the Server views |
| `live/schemas` | `connection` | `schemas-1.0`: every visible schema with its table count and whether it is a system schema, `user_tables`, and the Indexes limit (`all_limit`, `all_note`). Opens no table |
| `live/object-search` | `connection`, `text` (2–128 characters) | `object-search-1.0` for the command palette (`schema_browser.object_search`): `objects` (`schema`, `name`, `kind`: table, view, system view, sequence, procedure, function, trigger or event) across every visible schema, name-ordered, at most `limit` (50) with `truncated`. One UNION read of information_schema TABLES, ROUTINES, TRIGGERS and EVENTS with the text as a hex-literal LIKE pattern (wildcards escaped with `!`, `ESCAPE '!'`), tagged `/*myflames:editor*/`, with a 2-second query timeout. The saved password is filled like the Server views; errors never carry it |
| `live/io` | `connection`, optional `interval` (2 to 60 seconds, default 10), `schema`, `limit` (1 to 200, default 20) | `io-1.0` report: server-wide rates, tables, files and file kinds over the window and since start, and notes |
| `live/memory` | `connection`, optional `interval` (2 to 60 seconds, default 5) | `memory-1.0` report: `server_product`, `server_version`, tracked allocations over a two-sample window, nullable `source` (`"performance_schema"`, `"mariadb_status"`, or `null` when unavailable), totals and growth, categories, top events, sessions and accounts (bounded lists), instrumentation `coverage` and `collection`, `findings` (rules of thumb), `status` and notes. Read-only; never changes instrumentation |
| `live/config` | `connection` | `config-1.0` report (internal view id `config`): checks with settings (each with `dynamic`: whether it can change at runtime, or null), defaults, sources, statuses, actions and caveats, summary, notes, and `parameters` (`parameters-1.0` from `myflames/parameters.py` through `config_review.config_report(conn, parameters=True)`: `complete`, `notes`, and `items[]` with `name`, `value`, `truncated`, `scope`, `apply_type`, `modifiable`, `type`, `values`, `min`, `max`, `block_size`, `default`, `source`, `source_label`, `source_path`, `set_time`, `set_user`, `set_host`, `persisted`, `modified`, `description`, `checks`) |
| `live/variable` | `connection`, `name` | `variable-1.0`: one variable's global and session values, scope, `dynamic`, type, range or values, default, source, the value in `mysqld-auto.cnf` (MySQL), and which modes it allows. Reads only |
| `live/variable/plan` | `connection`, `name`, `value`, `mode` (`global`, `persist`, `persist_only`, `session`, `reset_persist`) | `variable-plan-1.0`: `status` (`ready` or `refused` with `problems`), the exact `statement`, before values, effect, privileges hint, undo, caveats, the option file line, `alternative` (MariaDB's option file line instead of PERSIST), `expect`, and the connection's `policy`. Reads only |
| `live/variable/apply` | `connection`, `name`, `value`, `mode`, `expect` (the plan's), `confirmed: true` | `variable-change-1.0`: `status` (`changed`, `unchanged`, `refused`, `needs_confirmation`, `denied`, `refused_by_server`, `interrupted`, `failed`), `message`, the `statement` sent, what was `verified`, `before` and `after` (re-read), `adjusted`, and `audit_id`. The write policy runs before any connection opens; myflames re-reads the variable and sends nothing when it, or the statement, changed since the plan |
| `live/instruments` | `connection` | `instruments-1.0` report: Performance Schema status (with how to turn it on or off), what each myflames view needs and is missing, `instruments[]` (`name`, `group`, `enabled`, `timed`, `timed_applies`, `default_enabled`, `default_timed`, `builtin`, `properties`), instrument groups and consumers with state, default, gain, loss, cost and on/off statements, `setup_actors` and `setup_objects` with restore statements, presets, how changes apply, privileges, and notes. Read-only: the statements are text |
| `live/replication` | `connection` | `replication-1.0` report (`replication.read_status`): engine, version, `available`, `replica`, `message`, `gtid`, `settings`, and `channels` (threads, `lag_s` with `lag_notes` and `lag_signals`, positions, worker, `failing_gtid`, `relay`, `status`, `status_why`, `source_version` (`version`, `engine`, `provenance` `saved_connection`, `relay_log` or null, `label`, `caveat`, `reason`, `hint`), and `skip_plan` with the exact `statements`, `identity`, privileges, drift warning and next steps when the applier stopped on an error), `capabilities`, `summary`, notes. Reads only. To show a source's version it reads the start of the relay log first; only when that has no version may it open a saved connection whose host and port match the source the replica reports, with that connection's saved password (at most 4 per report and once while myflames runs until the connection is edited, for a loopback source only when the replica is on this computer, accepted only when every server ID and UUID the replica reports matches). See [Replication](DEVELOPER_REFERENCE.md#replication-status-skip-skip-loop) for the full contract |
| `live/replication/skip` | `connection`, `review_token`, `channel`, `expect` (the plan's `identity`), `statements` (as the confirmation showed them), `confirmed: true` | `replication-skip-1.0`: `status` (`skipped`, `refused`, `needs_confirmation`, `denied`, `refused_by_server`, `failed`), `message`, the `statements` sent, `verified`, `target`, `plan`, `after` (re-read; `passed`), `audit_id`. The write policy runs before any connection opens; myflames reads the channel again and sends nothing when the failure or the statements changed. The workspace calls `audit/declined` with the tagged statements when the sheet is cancelled |
| `live/replication/skip-loop/start` | `connection`, `channel`, `codes` (chosen error numbers), `max_skips` (1 to 1000), `max_minutes` (1 to 1440), `poll_s`, `confirmed: true` | `replication-skip-loop-1.0` (`job_id`, `state` starting, running, stopping, stopped, refused or needs_confirmation, `message`, codes, limits, `skips_done`, `skips`, `last_error`, `stop_reason`, `needs_attention`, `drift_warning`). One loop per channel and connection; each skip is planned, checked and audited on its own |
| `live/replication/skip-loop/status` | `connection`, optional `job_id` | `loops` of this connection (or one loop). Opens no database connection; loops are matched by the connection's endpoint (saved connection, host, port, user, TLS), so changing its type or Read-only keeps them. A myflames restart starts with none |
| `live/replication/skip-loop/stop` | `connection`, `job_id` | The loop's status; it stops before its next skip. Not subject to the write policy (it ends a confirmed change) |
| `live/cluster` | `connection` | `cluster-1.0` report (`cluster.read_status`, the same as `myflames cluster`): kind, members with their state, role, address and version as this server sees them, quorum and failures tolerated (null when this server cannot see them, such as a read replica), flow control and queues, InnoDB Cluster read replicas and routers, findings, status, notes. Reads only; query timeout 30 s |
| `lifecycle/tools` | `connection` (optional) | `lifecycle-tools-1.0`: every tool found on this computer (`available`, `deprecated`, `path`, `version`, `version_tuple`, `engine`), and with a connection the `server` (engine, version) and what is `offered` (dump tools, upgrade method, clone, physical backup) |
| `lifecycle/upgrade-check` | `connection`, `target_version` (`X.Y.Z`), optional `prefer_shell` (default true), `config_path` | `upgrade-check-1.0`: `method` (`mysqlsh`, `native_subset` with `partial: true`, or `mariadb_upgrade_guidance`), `checks` (`id`, `title`, `status`, `severity`, `affected_objects`, `doc_link`, details), `counts`, `manual_checks`, `limitations`. Reads only |
| `lifecycle/prepare` | `connection`, `operation` (`dump`: `directory`, `schemas`, `tables`, `threads`, `prefer_shell`; `restore`: `directory`, `schema`, `existing` refuse, ignore or drop, `allow_source`, `resume`, `threads`; `clone`: `donor` (a connection, `profile_id` allowed), `donor_address`, `data_directory`, `require_ssl`) | `lifecycle-plan-1.0`: `review_token`, `target`, the exact `commands`, `checks` (passed, failed, not_checked, with the reason), `warnings`, `ready`, `binding`, `server`, `tool`. Reads only (MySQL Shell's dryRun runs here); a folder that exists and is not empty is a 400 error |
| `lifecycle/start` | `connection`, `operation`, `plan` (the preflight's, including its `review_token`), `typed_target` (restore and clone), `donor` (clone), `confirmed: true` | `lifecycle-job-1.0` snapshot (`id`, `operation`, `state` queued, running, done, failed, refused or cancelled, `progress`, `detail`, `events`, `result` with `status`, `verification`, `manifest`, `partial_state`, `audit_id`, `error`). The write policy runs first; the job repeats the preflight and refuses (nothing started) unless it still matches |
| `lifecycle/status` | `id` | The job's snapshot |
| `lifecycle/cancel` | `id` | The job's snapshot after cancelling (process group ended, server session sent KILL QUERY) |
| `lifecycle/jobs` | `connection` | `lifecycle-jobs-1.0`: `jobs`, the snapshots of the dump, restore and clone jobs this myflames holds for the connection's endpoint (running and finished ones, until they expire). Opens no database connection and starts nothing: the workspace reads it when Maintenance opens and after a Start whose reply was lost, so a job is followed again instead of started twice |
| `live/privileges` | `connection` | `privileges-1.0`: effective privileges and `actions` dictionary showing allowed, denied, or unknown status for each myflames action key |
| `accounts/list` | `connection` | `accounts-1.0`: users and roles without password hashes |
| `accounts/read` | `connection`, `user`, `host`, optional `role` | `account-1.0`: account details, grants, and its `fingerprint` |
| `accounts/plan` | `connection`, `operation`, `user`, `host`, and options | `account-plan-1.0`: `plan_id`, redacted statements, warnings, guards, `expect`, and `policy`. Reads only |
| `accounts/apply` | `connection`, `plan_id`, `expect`, `confirmed: true`, optional `typed_name`, `password`, `current_password` | `account-change-1.0`: before and after grants diff, verified status, and `audit_id`. Enforces write policy, rebuilds plan from fresh read, and verifies against expect and statements |
| `config/snapshots/list` | optional `source_key`, `limit` (1 to 5,000, default 1,000) | `parameter-snapshot-list-1.0`: saved parameter snapshots, newest first, without values |
| `config/snapshots/save` | `connection`, optional `label` (up to 200 characters) | Reads the global parameters (read-only) and saves them as the server's next snapshot: `id`, `number`, `label`, `server`, `captured_at`, `saved_at`, `source_key`, `complete`, `count` |
| `config/snapshots/get` | `id` (or `latest`) | The summary plus `snapshot` (`parameter-snapshot-1.0`) |
| `config/snapshots/delete` | `id` | `deleted: true`, `id`; local only |
| `baselines/list` | `connection`, `kind` (`health` or `digest`) | `baseline-list-1.0`: this server's baselines, newest first, with `server`, `server_key`, `items` and `max` (20). Always probes the server's identity (3 s query timeout), filling the saved password for that read only; it does not reuse a client-supplied identity |
| `baselines/save` | `connection`, `kind`, `name`, `report` (a `health-1.0` reading, or `top-1.0` with `read_at` and at most 500 statements) | Validates the report's verified `reading_server`, then reads the current identity (3 s query timeout) and saves the reading (digest literals hidden, no samples), returning the item plus `max`. Refuses an unverified or mismatching reading, another engine/version, a reading taken before a restart, or a 21st baseline of that kind on that server. Health refuses a verified server/version change while collecting its two samples; a reading taken before a restart detected by the save-time uptime check cannot be saved |
| `baselines/get` | `id` | The item's summary plus its payload; local read |
| `baselines/delete` | `id` | `deleted: true`, `id`; local only, a missing id is refused |
| `config/compare` | `source`, `target`, each `{live: {connection}}`, `{profile: NAME_OR_ID}`, `{snapshot: ID_OR_LATEST}` or `{file: JSON_TEXT}` | `parameter-compare-1.0`. Each side reads with its own connection and saved password, at most 120 s; a failed side becomes its `error` and its rows Not compared. Never changes a server |
| `accounts/compare` | `source`, `target`, each `{live: {connection}}` or `{profile: NAME_OR_ID}` | `account-compare-1.0`: accounts paired by user and host with grant, role, default role and attribute differences, never password hashes |
| `accounts/export` | `side` (`live` or `profile`), optional `accounts` (`user@host` strings) | `filename` (`account-grants.sql`), `text` (CREATE USER IF NOT EXISTS without a password, GRANT and SET DEFAULT ROLE statements; built-in system accounts left out unless named), `side`. Text only, never run |
| `session/load` | `{}` | `revision`, `saved_at`, `state` (object or null), `restore`, and `skip_restore` (true when started with `--no-restore`). `state` is null when restore is off or skipped |
| `session/save` | `state` (JSON object), `revision`, optional `client_id`, `seq` | New `revision` and `saved_at`. A stale `revision` from another client returns 409 with `code: "session_conflict"` and the current `revision`. Over 64 MiB returns 413 with `code: "session_too_large"`; the stored session is unchanged. Refused while restore is off or with `--no-restore` |
| `session/clear` | optional `client_id` | New `revision`, `restore`; the state is removed and settings stay |
| `session/settings` | `restore` (boolean), optional `client_id` | New `revision`, `restore`. `false` also clears the stored state |

Session requests may be up to 65 MiB; other requests keep the 12 MiB limit. The
`state` must be a JSON object nested at most 200 levels, with finite numbers;
`password` keys are removed at any depth. `client_id` identifies one page and
`seq` orders its saves, so a page's own overlapping saves never conflict and a
late older save is ignored (the response then has `stale: true`).

Every endpoint that takes `connection` also accepts `connection.profile_id`.
When `connection.password` is empty and that profile remembers a password, the
server reads it from the store for that request only, provided host, port, and
user match the profile. Paths have the `/api/` prefix. Existing `render` also accepts `selected_id`,
`focus_id`, `collapsed_ids`, and `metric`. Existing `analyze` returns parent/child
IDs and teaching hooks, and accepts captured schema/settings for analysis.
Comparison deltas add matching confidence/status and measurement-availability
flags; existing canonical node IDs and legacy summary fields remain available.
Consumers should honor `timing_available` before interpreting legacy timing
summary numbers. Unmeasured and uncertain classifications are explicit.
