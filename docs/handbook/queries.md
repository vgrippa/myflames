# Query editor & slow queries

The Query editor combines multi-tab SQL sessions, schema browsing, completion, formatting and result exports. Each tab has a dedicated session, so transaction state belongs to that tab.

## Work with SQL and results

Choose a connection and database, enter SQL, then run the statement or selected statements. Use the transaction controls deliberately. Results can be exported as CSV, JSON, SQL or Markdown. The row inspector lays out larger values, including JSON.

Completion uses schema and column information from the selected server. Live work needs a local `mysql` or `mariadb` client. Use Stop to cancel a running query. If the outcome of a transaction operation is unknown, inspect the session before retrying a change.

## Find the queries to investigate

Query digest has three sources:

- **Statement digests:** Performance Schema aggregates. Availability of samples, quantiles and CPU data depends on the engine/version.
- **Slow log table:** Read `mysql.slow_log` on the selected connection.
- **Slow log file:** Aggregate a local MySQL, Percona Server or MariaDB slow log by statement fingerprint, with no database connection.

For Performance Schema, a time range selects statements seen in that range; the displayed aggregates remain totals since counting began. It does not turn those counters into interval-only measurements.

```bash
myflames slow-log /path/to/slow.log --order total --limit 20
myflames slow-log /path/to/slow.log \
  --from '2026-11-04 09:00' --to '2026-11-04 10:00'
```

The file's timestamps are interpreted in the log's own clock.

## Import CSV

Open Import CSV from a table's menu in Schemas or Server → Tables. To create a table, use the database's menu. The import checks rows against the destination types, reports problems by line, and reviews the write before sending it.

Imports use batched INSERT statements by default. LOAD DATA LOCAL is offered only when the server already enables `local_infile`; myflames does not change that setting. Choose the transaction behavior and how bad rows are handled before importing. Import CSV has no CLI.

See the [detailed query workspace reference](../QUERY_WORKSPACE.md) for completion, dedicated sessions, result values and table data editing.
