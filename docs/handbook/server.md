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

The Full analysis report composes several diagnostic modules. Error log and Locks are outside that report's coverage; inspect those views separately.

## Replication, clusters and maintenance

Dedicated places inspect replication channels, GTIDs, workers and lag; MariaDB Galera, Percona XtraDB Cluster, MySQL InnoDB Cluster and Group Replication; ProxySQL backend pools, query rules and digests; and Readyset shallow and deep caches, proxied queries, base-table snapshotting, dynamic parameters and performance telemetry across ProxySQL + Readyset + MySQL or MariaDB topologies.

Maintenance finds the MySQL and MariaDB tools on this computer as soon as it opens and runs upgrade checks, dumps, restores and MySQL CLONE. **Start dump…**, **Start restore…** and **Start clone…** check first (reading only), then show the exact commands for you to confirm. A running job shows a progress bar, the step it is on and the elapsed time. MySQL Shell reports its own percent. `mysqldump` and `mariadb-dump` print nothing, so for them the bar is an estimate ("≈46%") from the bytes written to `dump.sql` against the server's table sizes, while that estimate exists and the output stays below it; otherwise the bar keeps moving without a percent. The job card also shows the bytes written and, once measurable, the rate. A compact summary beside the Start button shows the job's state, the percent when known and the bytes written, with **Show progress** to jump to the job card. A dump you started opens its folder in Finder when it completes, and **Open in Finder** opens it again; on Windows, use **Open in File Explorer**. Account management and protected server changes require review. Read the [write-safety guide](safety.md) before taking an action.

An unused-index finding is a candidate to investigate, not proof that dropping the index is safe. Collection windows, restarts, instrumentation and less frequent workloads all affect the evidence.

## Move files over SSH

**File transfer**, in the sidebar after Tuning, moves logs, dumps and configuration files between this computer and your servers over SSH/SFTP, in two panes side by side. It does not use the database connection in use: save **SSH connections** of their own (host, port, user, and sign-in with your SSH agent, a key file or a password; on Windows, the SSH agent or a key without a passphrase), or start one from a saved database connection or a `~/.ssh/config` alias. The first connection to a server shows its host key fingerprint to compare before you trust it. Right-click a saved SSH connection to open, edit, duplicate, test, copy or delete it, and a file of this computer to show it in Finder or File Explorer. Drag files between the panes or from Finder. Transfers show their progress, with the rate and time left when known; a running transfer has **Cancel**, and a failed or cancelled one has **Retry**. On Windows, uploads and downloads are not available yet.
