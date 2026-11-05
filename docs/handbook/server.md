# Server diagnostics & operations

Use the Server place to connect query behavior with server evidence. The data available depends on the engine, version, effective privileges and instrumentation that is already enabled.

## Begin with Health

Health reads counters and shows live charts after two samples. Turn on auto-refresh to follow throughput, threads, connections and buffer pool disk reads. **Capture when…** can save processlist, InnoDB status and lock evidence into an investigation when a configured condition is met.

The watcher checks only while Health is on screen and for the connection it started on. A missing measurement is reported as Not judged or Partly judged, rather than interpreted as zero.

## Follow a specific question

| Question | Where to look |
| --- | --- |
| What is running or holding a transaction open? | Processlist |
| Who blocks this statement or DDL? | Locks: blocking trees, metadata locks, row locks and latest deadlock |
| Where does the server wait? | Waits and InnoDB |
| Which files or tables are busy? | I/O |
| Which indexes need investigation? | Indexes and Tables |
| How is the server configured? | Parameters, Versions and parameter comparisons |
| What deserves a broader review? | Tuning and Full analysis report |

## Use the CLI when useful

The examples assume a saved `prod` profile:

```bash
myflames top -p --profile prod --order total --limit 25
myflames report -p --profile prod --html -o server-report.html
myflames sessions -p --profile prod
myflames locks -p --profile prod
```

The Full analysis report composes several diagnostic modules. Error log and Locks are outside that report's coverage; inspect those views separately.

## Replication, clusters and maintenance

Dedicated places inspect replication channels, GTIDs, workers and lag; MariaDB Galera, Percona XtraDB Cluster, MySQL InnoDB Cluster and Group Replication; ProxySQL backend pools, query rules and digests; and Readyset shallow and deep caches, proxied queries, base-table snapshotting, dynamic parameters and performance telemetry across ProxySQL + Readyset + MySQL or MariaDB topologies.

Maintenance detects local tools and provides preflights for upgrade checks, dumps, restores and MySQL CLONE. Account management and protected server changes require review. Read the [write-safety guide](safety.md) before taking an action.

An unused-index finding is a candidate to investigate, not proof that dropping the index is safe. Collection windows, restarts, instrumentation and less frequent workloads all affect the evidence.
