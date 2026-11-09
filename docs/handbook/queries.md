# Query editor & slow queries

The Query editor combines multi-tab SQL sessions, schema browsing, completion, formatting and result exports. Each tab has a dedicated session, so transaction state belongs to that tab.

## Work with SQL and results

Choose a connection and the tab's default **Database**, enter SQL, then run the statement or selection. Its database list is read only while the Query editor is on screen. Choosing a database with an open session sends `USE` there; **None** keeps that session's database until it closes, then starts the next session without one, including after restore. A tab with a session on another connection refuses a database change.

Under **Run options**, **On error: Stop / Continue** controls **Run all**. A finished Continue run counts succeeded and failed statements, plus not run, stopped and outcome unknown when present. After a deadlock or a lock wait timeout, a note says what InnoDB does in that case (a deadlock rolls back the victim's whole transaction; a lock wait timeout does only with `innodb_rollback_on_timeout` ON); check the transaction state before you go on. The ⓘ explains that an InnoDB error normally rolls back only the failed statement, while these cases roll back the whole transaction. Changing the mode requires a fresh write review.

Open and save `.sql` or `.txt` files from the tab menu: **Open SQL file…** (**⌘O** / **Ctrl+O**), **Save…** (**⌘S** / **Ctrl+S**) and **Save as…** (**⇧⌘S** / **Ctrl+Shift+S**). Open allows up to 5 MiB, refuses special files and links swapped in after selection, and opens a new tab. Save refuses a symbolic-link target or a folder changed since selection; use Save as to choose it again. Adding `.sql` never replaces an existing file: choose the full name to replace it. An edited file tab shows a dot and asks before closing. If a backend restart loses its binding, Save opens Save as in the same operation; cancellation or failure keeps unsaved edits.

Use **Find and replace** (**⌘F** / **Ctrl+F**, **⌥⌘F** / **Ctrl+H**) with case, whole-word and regular-expression toggles. Replacement text is literal, including `$1` and `$&`; a regex that exceeds the 250 ms search budget reports that it is too slow. In **Schemas**, expand a database to browse tables, views, columns and indexes and its **Routines**, **Triggers** and **Events** folders (double-click to insert the qualified name, or use the row menu for **Show CREATE…**). Names containing XML-forbidden control characters keep their exact identity. Use transaction controls deliberately. Results export as CSV, JSON, SQL or Markdown; the row inspector lays out larger values, including JSON.

After a run, the line beside the results reports the server's examined and returned rows, scans, joins without an index and temporary tables on disk for each statement. Open its information button for the counters and the verdict rule, or choose **Explain analyze** to review an analysis of that statement. The transaction pill shows how long the transaction has been open, how many rows it changed and whether other waiting InnoDB transactions are blocked by it; open it for Commit, Roll back or **Show who waits**. If the account cannot read InnoDB details, the age is measured from the tab's first observation; if statement collection is off, the information button points to Server > Instruments.

A result can be searched without running the query again: the filter field matches the loaded rows, and a cell's menu offers **Show only this value** and **Hide this value** as removable chips. Selection totals, copy and export then work on the filtered rows. **Pin result** keeps a result in the tab (up to 5,000 rows), and **Compare with pinned** lines a later result with the same columns up against it by a key column, marking added, removed and changed rows.

Completion uses schema and column information from the selected server. Live work needs a local `mysql` or `mariadb` client. Use Stop to cancel a running query. If the outcome of a transaction operation is unknown, inspect the session before retrying a change.

## Explain a statement

**Explain** works like MySQL Workbench: it runs the server's own `EXPLAIN` for the statement under the cursor, in the tab's session, and shows the rows in an Explain result tab you can export. Choose the format beside the button: Table (`EXPLAIN FORMAT=TRADITIONAL`, plain `EXPLAIN` on MariaDB), Tree (MySQL 8.0.16 and later) or JSON. For a supported SELECT, **Explore** captures a JSON plan in the tab's session and opens it in Plans as Visual Explain, flame graph, tree or bar chart; on MySQL, first commit or roll back any open transaction and turn autocommit on. `:name` placeholders are filled from Parameters as literal values.

Explain does not run the statement, but the server evaluates constant expressions and derived tables while it plans. A statement whose planning could change something, such as taking a named lock or assigning a variable, is reviewed under the connection's policy like any other change. **Run analysis** executes a SELECT for a timed plan (`EXPLAIN ANALYZE FORMAT=JSON` on MySQL 8.3 and later, `ANALYZE FORMAT=JSON` on MariaDB). For a simple single-table UPDATE or DELETE, or an INSERT or REPLACE with a SELECT source, it prepares a SELECT that reads the same rows, in a new tab, for you to review first; that measures finding the rows, not the cost of the write, triggers, constraints or locking. Other statements are refused.

## Find the queries to investigate

Query digest has three sources:

- **Statement digests:** Performance Schema aggregates. Availability of samples, quantiles and CPU data depends on the engine/version.
- **Slow log table:** Read `mysql.slow_log` on the selected connection.
- **Slow log file:** Aggregate a local MySQL, Percona Server or MariaDB slow log by statement fingerprint, with no database connection.

For Performance Schema, a time range selects statements seen in that range; the displayed aggregates remain totals since counting began. It does not turn those counters into interval-only measurements.

With Statement digests, **Save baseline** in the Refresh menu keeps the statement totals on this computer for that server (`server_uuid` on MySQL, or `hostname:port` on MariaDB, read with the digest list; literal values hidden, no sample statements). **Compare with baseline** lists what is new since the baseline and what got more than 50% slower or more frequent, before and after; the information button next to the list states the rule. Growth needs at least ten calls since the baseline. After a restart or when the digest table's oldest `first_seen` timestamp moved forward (a digest table reset), only average latency is compared.

The file's timestamps are interpreted in the log's own clock.

With the **Slow log table** source, opening a statement also reads its **Tables** section (metadata only, no table rows).

Top statements and Query digest share one statement inspector and statement thread. When a statement in **Top statements** or **Query digest** was cut off ("ON DUPLICATE ..."), its details name the server setting that cut it, its current value, a suggested value and about how much memory raising it costs. Both settings need a restart: copy the option-file lines, or on MySQL 8.0 and later the `SET PERSIST_ONLY` statements, which you can also apply from Server > Parameters as a reviewed change.

In **Top statements** and Query digest's **Statement digests** source, a statement flagged No index used or Full scan offers **Suggested index** under Flags. Choose **Read suggestion** to check an estimated plan (`EXPLAIN FORMAT=JSON`, SELECT only) and the table's existing indexes. Planning can evaluate expressions, so opening the inspector starts no such read. A fresh result is reused when you reopen it; **Read again** reads anew. A suggestion is a `CREATE INDEX` to copy or open in the Query editor; nothing is created for you. An index already leading with those columns is named instead. For the narrow `SELECT * FROM t` case, the recorded text is enough to explain that no index reduces the rows read if `t` is a base table. After a suggested `CREATE INDEX` succeeds in its Query editor tab, **Measure the statement again** there opens the source statement and asks before one Run analysis on the same connection. Its **Analysis** tab shows the digest's before average latency and rows examined per execution beside this run's statement-history time and rows examined (which may be unavailable). **Compare plans** appears there when an earlier non-session capture of the same SQL, connection and database is still in memory, and opens the two captures in **Plans > Compare**.

## Import CSV

Open Import CSV from a table's menu in Schemas or Server → Tables. To create a table, use the database's menu. The import checks rows against the destination types, reports problems by line, and reviews the write before sending it.

Imports use batched INSERT statements by default. LOAD DATA LOCAL is offered only when the server already enables `local_infile`; myflames does not change that setting. Choose the transaction behavior and how bad rows are handled before importing.

See the [detailed query workspace reference](../QUERY_WORKSPACE.md) for completion, dedicated sessions, result values and table data editing.
