# Query editor & slow queries

The Query editor combines multi-tab SQL sessions, schema browsing, completion, formatting and result exports. Each tab has a dedicated session, so transaction state belongs to that tab.

## Work with SQL and results

Choose a connection and database, enter SQL, then run the statement or selected statements. Use the transaction controls deliberately. Results can be exported as CSV, JSON, SQL or Markdown. The row inspector lays out larger values, including JSON.

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

The file's timestamps are interpreted in the log's own clock.

## Import CSV

Open Import CSV from a table's menu in Schemas or Server → Tables. To create a table, use the database's menu. The import checks rows against the destination types, reports problems by line, and reviews the write before sending it.

Imports use batched INSERT statements by default. LOAD DATA LOCAL is offered only when the server already enables `local_infile`; myflames does not change that setting. Choose the transaction behavior and how bad rows are handled before importing.

See the [detailed query workspace reference](../QUERY_WORKSPACE.md) for completion, dedicated sessions, result values and table data editing.
