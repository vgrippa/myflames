"""Server health: a read-only snapshot of how a MySQL or MariaDB server is doing.

:func:`health_report` reads a few ``SHOW GLOBAL VARIABLES``, takes two samples
of ``SHOW GLOBAL STATUS`` *interval* seconds apart (1 to 60, default 5), and
returns the ``health-1.0`` envelope shared by ``myflames health`` and the
workspace's Server > Health tab: throughput, connections, the InnoDB buffer
pool, row locks, temporary tables and sorts, the table cache, and redo/IO,
each as a delta over the window and a per-second rate with a status.

Sampling (:func:`read_variables`, :func:`take_sample`) is separate from the
arithmetic (:func:`compute_health`), which is pure and needs no server.

Every statement is a SHOW: nothing is written, and Performance Schema is not
required. Each sample opens one short connection of its own, so the window
includes this tool's own few statements, and ``Threads_connected`` and
``Threads_running`` include its session.

Statuses are rules of thumb, not verdicts (see ``RULES``): ``ok``, ``watch``,
``problem``; ``info`` for figures with no universal limit (throughput depends
on the workload and the hardware), and ``unknown`` when the server does not
report a variable or a counter was reset during the window.

Verified against server source (mysql-server 9.7 trunk "M/", mariadb-server
13.0 trunk "Ma/"). Every status variable used here is present in
``M/sql/mysqld.cc`` ``status_vars[]`` and ``M/storage/innobase/handler/
ha_innodb.cc`` ``innodb_status_variables[]`` at tags mysql-8.4.0, 8.4.9, 9.0.1
and 9.7.1, and in the MariaDB equivalents at mariadb-10.11.16, 11.4.9, 11.8.6
and 12.3.1. MySQL 26.x has no tag in the checkout; it was checked live.

* All are global status. MySQL's SHOW GLOBAL STATUS / VARIABLES read
  ``performance_schema.global_status`` / ``global_variables``
  (M/sql/sql_show_status.cc:280-310), which are "perpetual" (available with
  ``performance_schema=OFF``) and readable by every user
  (M/storage/perfschema/table_global_status.cc:73-86,
  table_global_variables.cc:74-84, ha_perfschema.cc:111-112,
  pfs_engine_table.cc:1071-1137). MariaDB fills SHOW STATUS from its own
  arrays, never from Performance Schema, and needs no privilege
  (Ma/sql/sql_show.cc:11438-11456).
* Server counters: M/sql/mysqld.cc:11687-12051 (Aborted_clients 11688,
  Aborted_connects 11689, Com 11718, Connection_errors_max_connections 11734,
  Created_tmp_disk_tables 11749, Created_tmp_tables 11754,
  Max_used_connections 11854, Open_tables 11867, Opened_tables 11871, Queries
  11885, Questions 11886, Select_full_join 11891, Select_scan 11902,
  Sort_merge_passes 11916, Threads_connected 12045, Threads_running 12049,
  Uptime 12051); Ma/sql/mysqld.cc:7830-8067.
* ``Questions`` counts client commands and each statement of a multi-statement
  request, not statements inside stored programs (M/sql/sql_parse.cc:1837-1840,
  2230-2231; Ma/sql/sql_parse.cc:1679-1680, 1973). ``Queries`` is the global
  statement counter (the reading session's query id: M/sql/mysqld.cc:
  11254-11258, Ma/sql/mysqld.cc:7268-7274), so it counts nearly everything:
  stored program instructions (M/sql/sp_instr.cc:419), replicated events
  (M/sql/log_event.cc:4426) and, on MySQL, COM_PING (M/sql/sql_parse.cc:1833).
* ``Threads_running``: MySQL counts threads inside dispatch_command
  (M/sql/sql_parse.cc:1835, 2512), including binlog dump threads (2356-2362),
  plus the event scheduler (M/sql/event_scheduler.cc:175, 274); MariaDB counts
  threads whose command is not Sleep when the status is summed
  (Ma/sql/sql_show.cc:4119-4130), so replication I/O, SQL and every parallel
  worker count all the time (Ma/sql/rpl_parallel.cc:1291, slave.cc:4692,
  5475). Both include the session reading the status. ``Uptime`` has
  one-second resolution (M/sql/mysqld.cc:11283-11289, Ma/sql/mysqld.cc:
  7284-7291).
* Connections. MySQL accepts max_connections + 1 (the last for
  CONNECTION_ADMIN/SUPER) plus admin-port connections and counts refusals only
  in ``Connection_errors_max_connections``, not ``Aborted_connects``
  (M/sql/conn_handler/connection_handler_manager.cc:108-122, 258-262).
  MariaDB counts refusals in ``Aborted_connects`` too (Ma/sql/sql_connect.cc:
  1574-1577) and adds extra_port connections to the peak (Ma/sql/mysqld.cc:
  6506-6518). So ``Max_used_connections`` at the limit does not prove a
  refusal; ``Connection_errors_max_connections`` does (Ma/sql/mysqld.cc:7853).
* ``Select_scan`` counts a full table or index scan of the first table of a
  query block, ``Select_full_join`` one of a later table (no index used for
  the join; on MySQL a hash join's inner table counts, read once rather than
  once per outer row): mysql-8.4.9 sql/sql_select.cc:3400-3408, and from 9.0
  M/sql/join_optimizer/access_path.cc:1906-1925; Ma/sql/sql_select.cc:
  16495-16565 (where a full index scan of any table counts as Select_scan,
  16500-16504).
* Temporary tables. MySQL counts ``Created_tmp_tables`` once per internal
  temporary table (M/sql/sql_tmp_table.cc:2403) and ``Created_tmp_disk_tables``
  when one is created in InnoDB, directly or when TempTable overflows
  (create_ondisk_from_heap, 2607-2723, counted at 2333-2336). TempTable's
  overflow into memory-mapped files is not counted (``temptable_max_mmap``,
  default 0 from 8.4: M/sql/sys_vars.cc:5217-5223). MariaDB counts a table
  that starts in memory and moves to disk twice in ``Created_tmp_tables``
  (open_tmp_table, Ma/sql/sql_select.cc:23313-23331, then
  create_internal_tmp_table from create_internal_tmp_table_from_heap,
  23606-23607, 23769-23770, 23834) and once in ``Created_tmp_disk_tables``;
  the same holds at mariadb-10.11.16, 11.4.9, 11.8.6 and 12.3.1.
* ``Opened_tables`` counts tables opened from their share (a table cache
  miss, or a user temporary table; internal temporary tables are not
  counted): M/sql/table.cc:3340-3344, Ma/sql/table.cc:4780-4782.
* Resets. MySQL FLUSH STATUS zeroes SHOW_LONG globals such as
  ``Aborted_clients`` (M/sql/mysqld.cc:13914-13939, M/sql/sql_show.cc:
  3944-3952). MariaDB 10.11 and 11.4 FLUSH STATUS zeroes them too
  (mariadb-10.11.16 sql/mysqld.cc:9204); from 11.5 FLUSH STATUS resets only
  the session (Ma/sql/sql_yacc.yy:15414-15419) and FLUSH GLOBAL STATUS
  (11.5.1, MDEV-33145) zeroes the global counters, ``Questions`` included
  (Ma/sql/mysqld.cc:9635-9675). Counters are ``ulong``, 32 bits on Windows,
  so they can wrap there. A counter that goes down between samples therefore
  means a restart (``Uptime`` went down), a reset, or a wrap.
* InnoDB: M/storage/innobase/handler/ha_innodb.cc:1142-1290 with values from
  srv_export_innodb_status (M/storage/innobase/srv/srv0srv.cc:1555-1680);
  Ma/storage/innobase/handler/ha_innodb.cc:942-1014.
  ``Innodb_buffer_pool_read_requests`` counts page requests
  (M/.../srv0srv.cc:1591). On MySQL ``Innodb_buffer_pool_reads`` counts
  synchronous, background and random read-ahead page reads
  (M/storage/innobase/buf/buf0rea.cc:283-284, 295, 317) but not linear
  read-ahead, which only ``Innodb_buffer_pool_read_ahead`` counts (584), so
  disk reads are the sum, with no double counting. On MariaDB
  ``Innodb_buffer_pool_reads`` is every page read, read-ahead included
  (Ma/storage/innobase/buf/buf0rea.cc:191, 398, 569), so nothing is added (as
  in advisor._rule_cold_cache). Read-ahead can exceed requests, so the hit
  ratio is clamped at 0.
* ``Innodb_buffer_pool_wait_free``: MySQL counts each retry of a session that
  found no free page and flushed one itself (M/storage/innobase/buf/
  buf0lru.cc:1422-1429, ut0counter.h:160-165); MariaDB counts each one-second
  wait for the page cleaner (Ma/storage/innobase/buf/buf0lru.cc:425).
* Dirty pages. MySQL flushes proportionally from
  ``innodb_max_dirty_pages_pct_lwm`` (default 10, M/storage/innobase/handler/
  ha_innodb.cc:22397-22399) up to io_capacity at ``innodb_max_dirty_pages_pct``
  (default 90 on both engines; M/storage/innobase/buf/buf0flu.cc:2184-2197).
  MariaDB's lwm defaults to 0, so its page cleaner can stay idle below the
  maximum (Ma/storage/innobase/handler/ha_innodb.cc:19284-19290, buf/
  buf0flu.cc:2679-2681) and a healthy server can sit near it. A high share is
  therefore only watched, never a problem.
* Row locks: ``Innodb_row_lock_waits`` grows when a wait starts and
  ``Innodb_row_lock_time`` adds the wait's milliseconds when it ends
  (M/storage/innobase/lock/lock0wait.cc:167-169, 321-327, srv0srv.cc:1665;
  Ma/storage/innobase/lock/lock0lock.cc:2095, 2103-2116, which truncates each
  wait to whole milliseconds), so an average over a short window is
  approximate.
* Redo: MySQL ``Innodb_os_log_written`` counts bytes written to the redo files
  and re-counts rewrites of partly filled blocks (M/storage/innobase/log/
  log0write.cc:1759-1762, 1807), so the redo rate uses the delta of
  ``Innodb_redo_log_current_lsn`` (M/.../ha_innodb.cc:1233, log0log.cc:1227)
  when present. MariaDB's ``Innodb_os_log_written`` is the redo generated
  since startup (LSN difference, Ma/storage/innobase/srv/srv0srv.cc:959-960);
  MariaDB starts flushing at about 0.71 of ``innodb_log_file_size`` and forces
  it at about 0.81 (Ma/storage/innobase/log/log0log.cc:76-86).
  ``Innodb_log_waits`` counts waits for space in the log buffer
  (M/storage/innobase/log/log0buf.cc:866-872; Ma/storage/innobase/mtr/
  mtr0mtr.cc:881-922, once per buffer-full event in 10.11.16 and 11.4.9).
* ``Innodb_data_reads`` / ``writes`` count operations, not pages
  (M/.../srv0srv.cc:1585-1587). On MySQL redo writes go through os_file_write
  and are counted (M/storage/innobase/log/log0files_io.cc:378,
  os/os0file.cc:4971); MariaDB writes redo with pwrite, which is not counted
  (Ma/storage/innobase/log/log0log.cc:169-170).

Diagnostics (``diagnostics`` in the report; :func:`_diagnostics`) combine
several deltas into one explained answer each. Every name below exists on
MySQL 8.4.0/8.4.9/9.0.1/9.7 and MariaDB 10.11.16/11.4.9/11.8.6/12.3.1/13.0
unless marked, and was read live from 8.4.11, 9.7.2, 26.7.0, 11.4.13 and
13.0.2.

* Buffer pool, full or warming. ``Innodb_buffer_pool_pages_total`` /
  ``_free`` / ``_data`` / ``_misc``: data = LRU length, misc = total - LRU -
  free (M/storage/innobase/srv/srv0srv.cc:1609-1624; Ma/storage/innobase/srv/
  srv0srv.cc:879-883, ha_innodb.cc:941-962). The page cleaner evicts from the
  LRU tail until each instance has ``innodb_lru_scan_depth`` free pages
  (MySQL default 1024, M/storage/innobase/buf/buf0flu.cc:1517-1521, 1963-1971,
  ha_innodb.cc:22693; instances: 1 below 1 GiB, else autosized,
  ha_innodb.cc:4551-4570). MariaDB has one instance, evicts when free < depth/2
  and refills to depth (default 1536; Ma/storage/innobase/buf/
  buf0flu.cc:1242-1270, 2469-2476; ha_innodb.cc:19468). So full means free <=
  depth x instances, not zero. MySQL refills the free list only while the
  server is busy (the idle branch flushes the flush list only, M/storage/
  innobase/buf/buf0flu.cc:3051-3085, 3137-3139), so an idle full pool can sit
  at 0 free pages (measured live).
  Bytes read = (``Innodb_buffer_pool_reads`` [+ ``_read_ahead`` on MySQL]) x
  ``innodb_page_size``. ``Innodb_data_read`` adds each fil read's length, the
  physical page size (M/storage/innobase/fil/fil0fil.cc:7548; Ma/storage/
  innobase/fil/fil0fil.cc:2824), so it is lower for ROW_FORMAT=COMPRESSED
  tables, and MySQL also adds redo reads during startup only
  (M/storage/innobase/log/log0log.cc:1689). ``Innodb_buffer_pool_read_ahead_
  evicted`` counts read-ahead pages evicted unused (M/storage/innobase/buf/
  buf0lru.cc:1129-1135). ``Innodb_buffer_pool_load_status`` reports "Loaded
  N/M pages" while a dump load runs.
* Redo. MySQL's LSN counts 512-byte blocks with their headers and trailers
  (log_translate_sn_to_lsn, M/storage/innobase/include/log0log.h:85-88), so
  the LSN delta and ``Innodb_os_log_written`` both include block overhead;
  the latter adds rewrites of the last partial block (write_size minus
  write-ahead padding, M/storage/innobase/log/log0write.cc:1807) and measured
  2.1x the LSN delta under small commits on 8.4.11. Capacity in use:
  ``Innodb_redo_log_capacity_resized`` (8.0.30+, M/storage/innobase/log/
  log0files_capacity.cc:398; the variable shows the default when legacy
  innodb_log_file_size x innodb_log_files_in_group overrides it, mysql-8.4.0
  ha_innodb.cc:4607-4663), then the variables (config_review.redo_capacity).
  Checkpoint age = ``Innodb_redo_log_current_lsn`` -
  ``Innodb_redo_log_checkpoint_lsn`` (M/storage/innobase/log/log0log.cc:
  1222-1227). Thresholds as fractions of the capacity, from
  log0files_capacity.cc:124-139, 292-330, 431-437, 475-499 and log0chkp.cc:
  1012-1084, 1145-1162: redo-driven adaptive flushing starts near 0.08
  (innodb_adaptive_flushing_lwm, buf0flu.cc:2204-2240) and its age factor
  reaches its limit near 0.73, sync flushing near 0.79 (innodb_flush_sync ON,
  log0chkp.cc:664), user writes wait near 0.84 (0.72 / 0.78 / 0.83 at 100
  MiB). They compare the oldest dirty page's age, at most the checkpoint age. MariaDB: ``Innodb_os_log_written`` is the LSN
  growth since startup, ``Innodb_checkpoint_age`` and
  ``Innodb_checkpoint_max_age`` are exported (Ma/storage/innobase/srv/
  srv0srv.cc:956-964); flush-ahead starts at 7/8 of max_age, writes wait at
  max_age (Ma/storage/innobase/log/log0log.cc:69-87, mtr/mtr0mtr.cc:991-1027).
  The "about an hour of peak writes" target is a common rule of thumb (Baron
  Schwartz, "How to calculate a good InnoDB log file size", Percona, 2008),
  shared with config_review.REDO_HOURS.
* Table open cache. Hits and misses: M/sql/sql_base.cc:3250, 3501;
  Ma/sql/table_cache.cc:874-896. An overflow closes an unused instance when an
  instance holds more than table_open_cache / table_open_cache_instances (M/
  sql/table_cache.h:381-392, mysqld.cc:10811; default 4000 / 16). MariaDB
  compares each active instance with table_open_cache itself (Ma/sql/
  table_cache.cc:361-378; default 2000, up to 8 instances, active ones in
  ``Table_open_cache_active_instances``), so Open_tables can exceed it.
  Definitions: ``Opened_table_definitions`` counts new shares (M/sql/
  dd_table_share.cc:2392, Ma/sql/table.cc:3580); table_definition_cache
  defaults to min(400 + table_open_cache / 2, 2000) on MySQL (M/sql/
  mysqld.cc:10815-10825) and 400 on MariaDB (measured on 11.4 and 13.0).
* Thread cache. ``Threads_created`` grows only when no cached thread is idle
  (M/sql/conn_handler/connection_handler_per_thread.cc:404-437; Ma/sql/
  mysqld.cc:6454-6461); with MariaDB's pool-of-threads it counts pool workers
  (Ma/sql/threadpool_generic.cc:887). ``Connections`` is the next thread id
  (M/sql/mysqld.cc:11320-11326; Ma/sql/mysqld.cc:7850): connections past the
  max_connections check, failed logins included, and internal threads
  (refusals get no thread id: M/sql/conn_handler/
  connection_handler_manager.cc:258-263, Ma/sql/mysqld.cc:6507-6527). thread_cache_size: MySQL min(8 +
  max_connections / 100, 100) (M/sql/mysqld.cc:6935-6951), MariaDB 256 capped
  at max_connections (Ma/sql/sys_vars.cc:4441-4444, mysqld.cc:9300-9301).
* Binary log cache. ``Binlog_cache_use`` grows per non-empty cache at reset
  and ``Binlog_cache_disk_use`` when it wrote to its temporary file (M/sql/
  binlog.cc:1007-1011; Ma/sql/log_cache.h:120-132, 373-385); 32 KiB defaults
  on both (M/sql/sys_vars.cc:1294-1311, Ma/sql/sys_vars.cc:561-587). log_bin
  defaults ON on MySQL, OFF on MariaDB. MariaDB 12.3+ with binlog_storage_engine
  spills into the InnoDB binary log itself and still counts it (Ma/sql/
  log.cc:536-557, mysys/mf_iocache.c:1823-1833).
* Open files. ``Open_files`` counts files opened through mysys (M/mysys/
  my_static.cc:87-130; Ma/mysys/my_open.c:109-137); InnoDB opens its files
  with open(2) directly (M/storage/innobase/os/os0file.cc:3174, Ma/storage/
  innobase/os/os0file.cc:984) and counts them in ``Innodb_num_open_files``.
  Sockets are not counted, so myflames adds Threads_connected; the total is
  a lower bound. open_files_limit is read-only on both engines.
* Full scans. ``Handler_read_rnd_next`` grows per row read by a sequential
  scan, in every engine, internal temporary tables and Performance Schema
  included (M/storage/temptable/src/handler.cc:309, M/storage/perfschema/
  ha_perfschema.cc:1765; Ma/sql/handler.cc:4063); full index scans count in
  Handler_read_first / _next instead.
* Temporary tables. Per-engine limits come from config_review.tmp_table_limit.
  With TempTable, BLOB and TEXT columns stay in memory; only the MEMORY
  engine sends them to disk (M/sql/sql_tmp_table.cc:2087-2094), while
  MariaDB uses Aria for them (Ma/sql/sql_select.cc:22448). MySQL 9.1+ adds
  ``Count_hit_tmp_table_size`` (M/sql/mysqld.cc:11757, sql_class.cc:2517; it
  also counts user MEMORY tables that fill up, M/storage/heap/ha_heap.cc:
  221-227) and ``TempTable_count_hit_max_ram`` (M/storage/temptable/src/
  plugin.cc:56-111), which grows when TempTable's total RAM is used up,
  before any memory-mapped overflow (M/storage/temptable/include/temptable/
  allocator.h:224-228): which limit was hit. temptable_max_ram defaults to 3%
  of memory clamped to 1 to 4 GiB on 8.4.0 to 9.5, and to 1 GiB from 9.6
  unless server_memory is set (M/sql/sys_vars.cc:5188-5215, mysqld.cc:
  6807-6817); temptable_max_mmap defaults to 0.
* Sorts. ``Sort_merge_passes``, ``Sort_range``, ``Sort_scan`` and ``Sort_rows``
  on both engines (M/sql/mysqld.cc:11916-11924, Ma/sql/mysqld.cc:8001-8005);
  sort_buffer_size defaults to 256 KiB on MySQL and 2 MiB on MariaDB.
"""
import datetime
import re
import time

from .connector import ConnectorError, MySQLConnection
# Compatibility exports for one release.
from .perf_common import _bytes_text, _count_text, _duration_text, _number, _round, _version_tuple
from .server_info import detect_flavor

SCHEMA_VERSION = "health-1.0"
#: Health statements carry the myflames prefix so ``top`` excludes them.
HEALTH_TAG = "/*myflames:health*/ "
DEFAULT_INTERVAL = 5
MIN_INTERVAL, MAX_INTERVAL = 1, 60
STATUSES = ("ok", "watch", "problem", "info", "unknown")
#: Severity order for summaries and ``--fail-on``.
SEVERITY = {"ok": 0, "watch": 1, "problem": 2}
GROUPS = (
    ("throughput", "Throughput"),
    ("connections", "Connections"),
    ("buffer_pool", "InnoDB buffer pool"),
    ("row_locks", "Row locks"),
    ("temp_sorts", "Temporary tables and sorts"),
    ("table_cache", "Table cache"),
    ("redo_io", "Redo and I/O"),
)

#: Fewer page requests than this in the window give no meaningful hit ratio
#: (one cold ad-hoc query on a quiet server must not read as a problem).
MIN_PAGE_REQUESTS = 10000
#: Below this many disk reads per second a low hit ratio is at most watch.
MIN_DISK_READS_PER_S = 10.0
#: Fewer temporary tables than this cap the on-disk ratio's status at watch.
MIN_TMP_TABLES = 20
#: A server younger than this is still filling its buffer pool.
WARMUP_SECONDS = 3600

# ---- rules of thumb ---------------------------------------------------------
# (watch, problem) limits, "higher is worse" unless noted. They are deliberately
# conservative: a status should point at something worth a look, not page
# someone. The UI and the CLI show RULES text with every status.
#: Counters that should stay at zero: watch on any, problem at 1 per second.
SHOULD_BE_ZERO = (0, 1.0)
#: Joins without an index: watch on any, problem at 10 per second (a MySQL
#: hash join's inner table counts too, scanned once rather than per row).
JOINS_WITHOUT_INDEX = (0, 10.0)
#: Counters that are normal in moderation: watch at 1/s, problem at 10/s, and
#: only with at least MIN_EVENTS in the window. Each window includes this
#: tool's own second sample (measured live: 3 statements, 1 internal temporary
#: table, 1 or 2 scans, and on MySQL 1 opened table), which must never trip a
#: rule by itself at --interval 1.
IN_MODERATION = (1.0, 10.0)
MIN_EVENTS = 5
CONNECTIONS_USED_PCT = (75.0, 90.0)
#: No core count is visible through SQL, so these are high on purpose. On a
#: MariaDB replica both rise by slave_parallel_threads (always running).
THREADS_RUNNING = (32, 100)
#: Lower is worse.
HIT_RATIO_PCT = (99.0, 95.0)
#: Fraction of innodb_max_dirty_pages_pct (90 by default on both engines) to
#: watch at; never a problem (see the module docstring).
DIRTY_OF_MAX = (1.0, None)
ROW_LOCK_MS_PER_S = (100.0, 1000.0)
ROW_LOCK_AVG_MS = (50.0, 1000.0)
#: Judged on the smaller of the two samples: waiting in both, not a blip.
ROW_LOCK_CURRENT = (1, 10)
TMP_DISK_PCT = (10.0, 25.0)

RULES = {
    "should_be_zero": "watch when any occur in the window, problem at 1 per second or more",
    "in_moderation": "watch at 1 per second, problem at 10 per second, with at least %d in the window" % MIN_EVENTS,
    "joins_without_index": "watch when any occur in the window, problem at 10 per second",
    "connections_used": ("watch at 75% of max_connections, problem at 90% or when clients were refused "
                         "in the window (Connection_errors_max_connections)"),
    "threads_running": ("watch at 32, problem at 100 (the larger sample). Above the CPU core count, "
                        "statements queue; myflames cannot see the core count, so the limits are high"),
    "hit_ratio": ("watch below 99%%, problem below 95%% with at least %d disk reads per second; judged only "
                  "with %s or more page requests" % (MIN_DISK_READS_PER_S, "{:,}".format(MIN_PAGE_REQUESTS))),
    "dirty": ("watch at innodb_max_dirty_pages_pct; a high share alone is not a stall, waits for a free page "
              "and a nearly full redo log are"),
    "row_lock_time": "watch at 100 ms of waiting per second, problem at 1,000 ms per second (one session always waiting)",
    "row_lock_avg": "watch at 50 ms per wait, problem at 1 s per wait",
    "row_lock_current": "watch at 1 session waiting in both samples, problem at 10",
    "tmp_disk": ("watch at 10%% on disk, problem at 25%%; with fewer than %d temporary tables in the window, "
                 "watch at most" % MIN_TMP_TABLES),
    "none": "no threshold: compare with this server's usual values",
}

#: Variables read once per report (missing ones are simply absent).
VARIABLES = ("version", "version_comment", "max_connections", "table_open_cache",
             "innodb_buffer_pool_size", "innodb_page_size", "innodb_max_dirty_pages_pct",
             "innodb_max_dirty_pages_pct_lwm", "slave_parallel_threads",
             "innodb_redo_log_capacity", "innodb_log_file_size", "innodb_log_files_in_group",
             "internal_tmp_mem_storage_engine", "temptable_max_mmap", "temptable_use_mmap",
             # diagnostics
             "innodb_lru_scan_depth", "innodb_buffer_pool_instances", "table_open_cache_instances",
             "table_definition_cache", "thread_cache_size", "thread_handling", "log_bin",
             "binlog_cache_size", "binlog_stmt_cache_size", "open_files_limit", "sort_buffer_size",
             "tmp_table_size", "max_heap_table_size", "temptable_max_ram", "tmp_memory_table_size",
             "tmp_disk_table_size")

# ---- diagnostics: rules of thumb -------------------------------------------------
#: Free-list target per buffer pool instance when innodb_lru_scan_depth is not
#: reported (the engine defaults; see the module docstring).
LRU_SCAN_DEPTH_DEFAULT = {"mysql": 1024, "mariadb": 1536}
#: MySQL 8.0.30+ checkpoint-age points as fractions of the redo capacity at
#: default settings: full adaptive flushing, forced (sync) flushing, user
#: writes wait (see the module docstring for the derivation).
REDO_POINTS_MYSQL = (0.73, 0.79, 0.84)
#: MariaDB points as fractions of Innodb_checkpoint_max_age: flush-ahead
#: (max_modified_age_async = 7/8 of it) and writes wait (the max age).
REDO_POINTS_MARIADB = (0.875, 1.0)
#: A thread cache miss share above this, with threads created at 1 per
#: second or more, is watched (never a problem: creating a thread is cheap).
THREAD_MISS_PCT = 10.0
#: Binary log cache uses that went to a temporary file: watch at this share.
BINLOG_DISK_PCT = 10.0
#: Read-ahead pages evicted unused, as a share of pages read ahead, worth a
#: sentence in a full buffer pool's verdict (rule of thumb; no status).
READ_AHEAD_WASTE_PCT = 20.0
#: Less buffer pool growth than this in the window gives no time-to-full
#: estimate (a few pages are noise).
MIN_FILL_BYTES = 1 << 20
#: Accounted file descriptors as a share of open_files_limit.
OPEN_FILES_PCT = (75.0, 90.0)

DIAGNOSTIC_RULES = {
    "buffer_pool": ("full when free pages are at or below innodb_lru_scan_depth x buffer pool instances (the "
                    "free list InnoDB's page cleaner keeps). Full: ok below %g pages read from disk per second, "
                    "watch at %g or more, problem when the hit ratio is also below 95%%. Not full: no threshold"
                    % (MIN_DISK_READS_PER_S, MIN_DISK_READS_PER_S)),
    "table_cache": ("watch at 1 overflow per second, problem at 10, with at least %d in the window. Misses "
                    "without overflows are not judged" % MIN_EVENTS),
    "table_definitions": ("watch when table definitions are opened at 1 per second or more (at least %d in the "
                          "window) while the definition cache is at least 95%% full" % MIN_EVENTS),
    "thread_cache": ("watch when more than %g%% of new connections needed a new thread, with threads created "
                     "at 1 per second or more (at least %d in the window); never a problem"
                     % (THREAD_MISS_PCT, MIN_EVENTS)),
    "binlog_cache": ("watch when %g%% or more of the transactions or statements that used a binary log cache "
                     "spilled to a temporary file, with at least %d spills in the window; never a problem"
                     % (BINLOG_DISK_PCT, MIN_EVENTS)),
    "open_files": ("watch when the file descriptors myflames can account for reach %g%% of open_files_limit, "
                   "problem at %g%%" % OPEN_FILES_PCT),
    "full_scans": "no threshold: compare with this server's usual values",
}

#: Engine defaults for variables the diagnostics show, beyond
#: config_review.DEFAULTS (source-verified; see the module docstring).
DIAGNOSTIC_DEFAULTS = {
    "mysql": {"innodb_page_size": 16384, "innodb_lru_scan_depth": "1024",
              "innodb_buffer_pool_instances": "1 below 1 GiB of buffer pool, otherwise sized from it and the CPUs",
              "table_open_cache_instances": "16",
              "table_definition_cache": "400 + table_open_cache / 2, at most 2000",
              "thread_cache_size": "8 + max_connections / 100, at most 100",
              "binlog_cache_size": 32768, "binlog_stmt_cache_size": 32768, "log_bin": "ON",
              "open_files_limit": "sized at startup", "innodb_log_files_in_group": "2",
              "temptable_max_ram": "3% of memory, 1 to 4 GiB (8.4 to 9.5); 1 GiB from 9.6 unless server_memory is set",
              "temptable_max_mmap": 0},
    "mariadb": {"innodb_page_size": 16384, "innodb_lru_scan_depth": "1536",
                "table_open_cache_instances": "8",
                "table_definition_cache": "400",
                "thread_cache_size": "256, at most max_connections",
                "binlog_cache_size": 32768, "binlog_stmt_cache_size": 32768, "log_bin": "OFF",
                "open_files_limit": "sized at startup", "tmp_disk_table_size": "unlimited"},
}
_BYTE_SETTINGS = ("innodb_buffer_pool_size", "innodb_page_size", "innodb_redo_log_capacity", "innodb_log_file_size",
                  "binlog_cache_size", "binlog_stmt_cache_size", "sort_buffer_size", "tmp_table_size",
                  "max_heap_table_size", "temptable_max_ram", "temptable_max_mmap", "tmp_memory_table_size",
                  "tmp_disk_table_size")

# ---- sampling (talks to the server) ------------------------------------------


def _rows(conn, sql):
    """Run tagged SQL; return rows of unescaped strings."""
    text = conn.run(HEALTH_TAG + sql, extra_flags=["--comments"])
    rows = []
    # Only \n separates rows (see connector.query_rows).
    for line in text.split("\n"):
        if line:
            rows.append([MySQLConnection._unescape_mysql(c) for c in line.split("\t")])
    return rows


def _pairs(rows):
    return {row[0]: row[1] if len(row) > 1 else "" for row in rows if row}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read_variables(conn):
    """The few ``SHOW GLOBAL VARIABLES`` the report needs, as strings."""
    sql = "SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)" % ", ".join("'%s'" % v for v in VARIABLES)
    return _pairs(_rows(conn, sql))


def take_sample(conn, clock=None):
    """One ``SHOW GLOBAL STATUS`` read: {status, t, taken_at}.

    ``t`` is the monotonic clock when the result arrived; the server read the
    status just before, after the connection was set up, so both samples
    carry the same small bias and the window between them stays accurate.
    """
    status = _pairs(_rows(conn, "SHOW GLOBAL STATUS"))
    return {"status": status, "t": (clock or time.monotonic)(), "taken_at": _now()}


def _check_interval(interval):
    if isinstance(interval, bool) or not isinstance(interval, int) \
            or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ValueError("Choose a sampling interval from %d to %d seconds." % (MIN_INTERVAL, MAX_INTERVAL))
    return interval


def health_report(conn, interval=DEFAULT_INTERVAL, sleep=None, clock=None):
    """Return the ``health-1.0`` report. Read-only; see the module docstring.

    Reads the variables, samples the status, waits until *interval* seconds
    have passed since the first sample, samples again, and computes.
    *sleep* and *clock* default to time.sleep and time.monotonic.
    """
    interval = _check_interval(interval)
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    try:
        variables = read_variables(conn)
        first = take_sample(conn, clock)
        remaining = interval - (clock() - first["t"])
        if remaining > 0:
            sleep(remaining)
        second = take_sample(conn, clock)
    except ConnectorError as exc:
        text = " ".join(str(exc).split())
        raise ValueError("Could not read the server status: " + re.sub(r"^mysql client exited \d+:\s*", "", text))
    return compute_health(first, second, variables, interval)

# ---- arithmetic (pure) ----------------------------------------------------------


def _grade(value, limits, lower_is_worse=False):
    """ok / watch / problem for *value* against (watch, problem) limits.

    A watch limit of 0 means "any occurrence": strictly greater than zero.
    """
    watch, problem = limits
    if value is None:
        return "unknown"
    if lower_is_worse:
        if value < problem:
            return "problem"
        if value < watch:
            return "watch"
        return "ok"
    if problem is not None and value >= problem:
        return "problem"
    if (value > 0 if watch == 0 else value >= watch):
        return "watch"
    return "ok"


class _Window(object):
    """Two status samples, the variables, and what went missing or reset."""

    def __init__(self, first, second, variables, interval):
        self.a = first.get("status") or {}
        self.b = second.get("status") or {}
        self.vars = variables or {}
        self.interval = interval
        elapsed = (second.get("t") or 0) - (first.get("t") or 0)
        self.elapsed = elapsed if elapsed > 0 else None
        self.version = self.vars.get("version") or ""
        self.engine = detect_flavor(self.version, self.vars.get("version_comment"))
        up_a, up_b = _number(self.a.get("Uptime")), _number(self.b.get("Uptime"))
        self.uptime = up_b
        self.restarted = up_a is not None and up_b is not None and up_b < up_a
        self.missing = []
        self.reset = []

    def _miss(self, name):
        if name not in self.missing:
            self.missing.append(name)

    def gauge(self, name, sample="b"):
        """Current value from the second (or first) sample, or None."""
        value = _number((self.b if sample == "b" else self.a).get(name))
        if value is None:
            self._miss(name)
        return value

    def var(self, name):
        return _number(self.vars.get(name))

    def delta(self, *names):
        """Sum of the counters' increases over the window, or None.

        None when a counter is missing, the server restarted, or a counter
        went down (reset or wrap); those are recorded for the notes.
        """
        total = 0
        for name in names:
            before, after = _number(self.a.get(name)), _number(self.b.get(name))
            if before is None or after is None:
                self._miss(name)
                return None
            if self.restarted:
                return None
            if after < before:
                if name not in self.reset:
                    self.reset.append(name)
                return None
            total += after - before
        return total

    def rate(self, delta):
        if delta is None or not self.elapsed:
            return None
        return delta / self.elapsed


def _metric(key, group, label, explain, unit, sources, value=None, delta=None, rate=None,
            status="info", observation="", rule="none", detail="", link=None, learn=None,
            inputs=None, maximum=None, history=False):
    """One metric of the report. ``why`` joins the observation and the rule of thumb."""
    rule_text = RULES[rule]
    if status == "unknown":
        why = observation or "Not enough data in this window."
    elif status == "info":
        why = (observation + ". " if observation else "") + "No threshold: compare with this server's usual values."
    else:
        why = (observation + ". " if observation else "") + "Rule of thumb: " + rule_text + "."
    return {
        "key": key, "group": group, "label": label, "explain": explain, "unit": unit,
        "value": _round(value, 2), "delta": _round(delta, 2), "rate": _round(rate, 2),
        "max": _round(maximum, 2), "status": status, "why": why,
        "rule": None if rule == "none" else rule_text, "detail": detail,
        "link": link, "learn": learn, "sources": list(sources), "inputs": inputs or {},
        "sparkline": bool(history),
    }


_LOCKS = {"view": "locks", "label": "Open Locks"}
_TOP = {"view": "top", "label": "Open Top statements"}


def _counter_metric(w, key, group, label, explain, names, rule, limits, unit="/s", link=None,
                    learn=None, noun="", history=False):
    """A per-second counter graded by its rate (or by any occurrence)."""
    delta = w.delta(*names)
    rate = w.rate(delta)
    if delta is None:
        return _metric(key, group, label, explain, unit, names, status="unknown",
                       observation=_missing_text(w, names), link=link, learn=learn, history=history)
    if limits is None:
        status = "info"
    else:
        status = _grade(rate, limits)
        if limits[0] == 0 and delta > 0 and status == "ok":
            status = "watch"
        if rule == "in_moderation" and delta < MIN_EVENTS:
            status = "ok"
    observation = "%s %s in %s s (%s per second)" % (_count_text(delta), noun or "events",
                                                    _count_text(_round(w.elapsed, 1)), _count_text(_round(rate, 2)))
    return _metric(key, group, label, explain, unit, names, value=rate, delta=delta, rate=rate,
                   status=status, observation=observation, rule=rule,
                   detail="%s in the window" % _count_text(delta), link=link, learn=learn,
                   inputs={n: w.delta(n) for n in names} if len(names) > 1 else {}, history=history)


def _missing_text(w, names):
    if w.restarted:
        return "The server restarted between the two samples, so this counter started over."
    missing = [n for n in names if n in w.missing]
    if missing:
        return "This server does not report %s." % ", ".join(missing)
    reset = [n for n in names if n in w.reset]
    if reset:
        return "%s went down between the samples (a reset or a restart), so there is no rate." % ", ".join(reset)
    if not w.elapsed:
        return "The samples have no time between them."
    return "Not enough data in this window."


def _throughput(w):
    group = "throughput"
    out = [
        _counter_metric(w, "questions", group, "Statements from clients",
                        "Statements clients sent per second (Questions). Statements inside stored "
                        "programs are not counted here; they are in all statements.",
                        ("Questions",), "none", None, link=_TOP, noun="statements", history=True),
        _counter_metric(w, "queries", group, "All statements",
                        "Nearly every statement the server ran per second (Queries): client statements "
                        "plus those inside stored procedures, functions, triggers and events, replicated "
                        "events, and on MySQL pings.",
                        ("Queries",), "none", None, link=_TOP, noun="statements"),
    ]
    for key, label, names in (
            ("com_select", "SELECT", ("Com_select",)),
            ("com_insert", "INSERT", ("Com_insert", "Com_insert_select")),
            ("com_update", "UPDATE", ("Com_update", "Com_update_multi")),
            ("com_delete", "DELETE", ("Com_delete", "Com_delete_multi"))):
        explain = "%s statements per second (%s)." % (label, " + ".join(names))
        out.append(_counter_metric(w, key, group, "%s statements" % label, explain, names, "none", None,
                                   link=_TOP, noun="statements"))
    return out


def _connections(w):
    group = "connections"
    out = []
    connected = w.gauge("Threads_connected")
    connected_a = w.gauge("Threads_connected", "a")
    max_conn = w.var("max_connections")
    out.append(_metric(
        "threads_connected", group, "Open connections",
        "Client connections open now (Threads_connected), including this tool's own.",
        "connections", ("Threads_connected",), value=connected,
        maximum=max(x for x in (connected, connected_a) if x is not None) if connected is not None else None,
        status="info" if connected is not None else "unknown",
        observation=("%s open; %s at the first sample" % (_count_text(connected), _count_text(connected_a)))
        if connected is not None else _missing_text(w, ("Threads_connected",)),
        detail="of max_connections %s" % _count_text(max_conn) if max_conn else ""))

    running = w.gauge("Threads_running")
    running_a = w.gauge("Threads_running", "a")
    peak = max([x for x in (running, running_a) if x is not None] or [None]) if running is not None else None
    workers = (w.var("slave_parallel_threads") or 0) if w.engine == "mariadb" else 0
    limits = (THREADS_RUNNING[0] + workers, THREADS_RUNNING[1] + workers)
    observation = ("%s now, %s at most across both samples" % (_count_text(running), _count_text(peak))) \
        if peak is not None else _missing_text(w, ("Threads_running",))
    if workers and peak is not None:
        observation += "; the limits rise by slave_parallel_threads (%s), whose workers always count" % _count_text(workers)
    out.append(_metric(
        "threads_running", group, "Statements running now",
        "Sessions executing a statement at the moment of each sample (Threads_running), "
        "including this tool's own. Waiting on a lock or I/O counts as running. It also counts "
        + ("replication threads and every parallel replication worker."
           if w.engine == "mariadb" else "binlog dump threads and the event scheduler."),
        "threads", ("Threads_running",) + (("slave_parallel_threads",) if workers else ()),
        value=running, maximum=peak,
        status=_grade(peak, limits) if peak is not None else "unknown",
        observation=observation,
        rule="threads_running", detail="max of both samples %s" % _count_text(peak) if peak is not None else "",
        link={"view": "running", "label": "Open Running queries"}, history=True))

    out.append(_counter_metric(
        w, "aborted_connects", group, "Failed connection attempts",
        "Connection attempts that failed (Aborted_connects): a wrong password, a missing "
        "privilege, a handshake timeout, or a load balancer's TCP check that connects and "
        "closes. " + ("On MariaDB, refusals at max_connections count too."
                      if w.engine == "mariadb" else
                      "MySQL counts refusals at max_connections separately (see Connection limit used)."),
        ("Aborted_connects",), "in_moderation", IN_MODERATION, noun="failed attempts"))
    out.append(_counter_metric(
        w, "aborted_clients", group, "Connections dropped",
        "Connections that ended without closing properly (Aborted_clients): the client exited, "
        "crashed or was killed, a network error, wait_timeout expired, or a packet exceeded "
        "max_allowed_packet.",
        ("Aborted_clients",), "in_moderation", IN_MODERATION, noun="dropped connections"))

    used = 100.0 * connected / max_conn if connected is not None and max_conn else None
    peak_used = w.gauge("Max_used_connections")
    # Optional: a refusal counter makes "full" certain (missing is not noted).
    refused = None
    if "Connection_errors_max_connections" in w.a and "Connection_errors_max_connections" in w.b:
        refused = w.delta("Connection_errors_max_connections")
    detail = "%s of max_connections %s" % (_count_text(connected), _count_text(max_conn)) if used is not None else ""
    observation = "%.1f%% of max_connections in use" % used if used is not None else (
        "This server does not report max_connections." if not max_conn else _missing_text(w, ("Threads_connected",)))
    status = _grade(used, CONNECTIONS_USED_PCT) if used is not None else "unknown"
    if used is not None and refused:
        status = "problem"
        observation += "; %s clients were refused at max_connections in the window " \
                       "(Connection_errors_max_connections)" % _count_text(refused)
    if used is not None and peak_used is not None and max_conn and peak_used >= max_conn:
        observation += "; the peak since startup or the last status reset (Max_used_connections %s) " \
                       "reached max_connections; refusals are counted in " \
                       "Connection_errors_max_connections" % _count_text(peak_used)
    if used is not None and peak_used is not None:
        detail += "; peak %s" % _count_text(peak_used)
    out.append(_metric(
        "connections_used", group, "Connection limit used",
        "Open connections as a share of max_connections. When it is full, new clients get "
        "\"Too many connections\" (one more is kept for administrators).",
        "%", ("Threads_connected", "Max_used_connections", "max_connections")
        + (("Connection_errors_max_connections",) if refused is not None else ()), value=used,
        status=status, observation=observation, rule="connections_used", detail=detail,
        inputs={"Threads_connected": connected, "Max_used_connections": peak_used, "max_connections": max_conn,
                "Connection_errors_max_connections": refused}))
    return out


def _buffer_pool(w):
    group = "buffer_pool"
    out = []
    read_names = ("Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_ahead") if w.engine == "mysql" \
        else ("Innodb_buffer_pool_reads",)
    disk = w.delta(*read_names)
    requests = w.delta("Innodb_buffer_pool_read_requests")
    sources = ("Innodb_buffer_pool_read_requests",) + read_names
    inputs = {"Innodb_buffer_pool_read_requests": requests, "disk_reads": disk}
    if w.engine == "mysql":
        inputs["Innodb_buffer_pool_reads"] = w.delta("Innodb_buffer_pool_reads")
        inputs["Innodb_buffer_pool_read_ahead"] = w.delta("Innodb_buffer_pool_read_ahead")
    read_ahead = ("MySQL counts linear read-ahead only in Innodb_buffer_pool_read_ahead, so it is added; "
                  if w.engine == "mysql" else
                  "MariaDB's Innodb_buffer_pool_reads already includes read-ahead; ")
    if disk is None or requests is None:
        out.append(_metric("bp_hit_ratio", group, "Buffer pool hit ratio",
                           "Share of page requests served from memory over the window.", "%", sources,
                           status="unknown", observation=_missing_text(w, sources), learn="lru"))
    else:
        ratio = max(0.0, 100.0 * (1 - float(disk) / requests)) if requests else None
        if requests < MIN_PAGE_REQUESTS:
            status, observation = "info", "Only %s page requests in the window, too few to judge" % _count_text(requests)
        else:
            status = _grade(ratio, HIT_RATIO_PCT, lower_is_worse=True)
            observation = "%s disk reads for %s page requests" % (_count_text(disk), _count_text(requests))
            if status == "problem" and (w.rate(disk) or 0) < MIN_DISK_READS_PER_S:
                status = "watch"
                observation += ", but only %s disk reads per second" % _count_text(_round(w.rate(disk), 2))
        out.append(_metric(
            "bp_hit_ratio", group, "Buffer pool hit ratio",
            "Share of page requests InnoDB served from memory over the window, without reading "
            "the page from disk. %s" % (read_ahead + "disk reads are the sum."
                                        if w.engine == "mysql" else read_ahead + "nothing is added."),
            "%", sources, value=ratio, status=status, observation=observation,
            rule="hit_ratio" if status != "info" else "none",
            detail="%s disk reads per %s requests" % (_count_text(disk), _count_text(requests)),
            learn="lru", inputs=inputs))
    out.append(_metric(
        "bp_disk_reads", group, "Pages read from disk",
        "InnoDB pages read from disk per second (%s)." % " + ".join(read_names),
        "pages/s", read_names, value=w.rate(disk), delta=disk, rate=w.rate(disk),
        status="info" if disk is not None else "unknown",
        observation=("%s pages in the window" % _count_text(disk)) if disk is not None else _missing_text(w, read_names),
        detail="%s in the window" % _count_text(disk) if disk is not None else "", learn="buffer_pool_warmup",
        history=True))

    total = w.gauge("Innodb_buffer_pool_pages_total")
    dirty = w.gauge("Innodb_buffer_pool_pages_dirty")
    free = w.gauge("Innodb_buffer_pool_pages_free")
    max_dirty = w.var("innodb_max_dirty_pages_pct")
    limit = max_dirty if max_dirty else 90.0
    if dirty is None or not total:
        out.append(_metric("bp_dirty", group, "Dirty pages", "Changed pages not yet written to disk.", "%",
                           ("Innodb_buffer_pool_pages_dirty", "Innodb_buffer_pool_pages_total"), status="unknown",
                           observation=_missing_text(w, ("Innodb_buffer_pool_pages_dirty", "Innodb_buffer_pool_pages_total"))))
    else:
        pct = 100.0 * dirty / total
        out.append(_metric(
            "bp_dirty", group, "Dirty pages",
            "Pages changed in memory and not yet flushed to disk, as a share of the buffer pool. "
            "Near innodb_max_dirty_pages_pct (%s%%) InnoDB flushes harder. A high share alone is "
            "not a stall; waits for a free page and a nearly full redo log are. %s"
            % (_count_text(limit), "MariaDB's page cleaner can stay idle below the maximum by default "
               "(innodb_max_dirty_pages_pct_lwm 0), so a healthy server can sit near it."
               if w.engine == "mariadb" else
               "MySQL starts flushing gradually at innodb_max_dirty_pages_pct_lwm (10 by default)."),
            "%", ("Innodb_buffer_pool_pages_dirty", "Innodb_buffer_pool_pages_total", "innodb_max_dirty_pages_pct"),
            value=pct, status=_grade(pct, (limit * DIRTY_OF_MAX[0], DIRTY_OF_MAX[1])),
            observation="%s of %s pages are dirty" % (_count_text(dirty), _count_text(total)), rule="dirty",
            detail="%s of %s pages" % (_count_text(dirty), _count_text(total)),
            inputs={"Innodb_buffer_pool_pages_dirty": dirty, "Innodb_buffer_pool_pages_total": total,
                    "innodb_max_dirty_pages_pct": max_dirty}))
    out.append(_metric(
        "bp_free", group, "Free pages",
        "Empty pages ready for new reads. A busy server's buffer pool is usually full, which is "
        "normal: InnoDB evicts the least recently used pages. Waits for a free page are the signal.",
        "pages", ("Innodb_buffer_pool_pages_free", "Innodb_buffer_pool_pages_total"), value=free,
        status="info" if free is not None else "unknown",
        observation=("%s of %s pages are free" % (_count_text(free), _count_text(total)))
        if free is not None else _missing_text(w, ("Innodb_buffer_pool_pages_free",)),
        detail="of %s pages" % _count_text(total) if total else "", learn="lru",
        inputs={"Innodb_buffer_pool_pages_free": free, "Innodb_buffer_pool_pages_total": total}))
    out.append(_counter_metric(
        w, "bp_wait_free", group, "Waits for a free page",
        "Times a session found no free page, even after scanning the LRU list, so it had to flush "
        "a page or wait (Innodb_buffer_pool_wait_free; " + (
            "MariaDB counts each one-second wait for the page cleaner"
            if w.engine == "mariadb" else "MySQL counts each retry") + "). The page cleaner is not "
        "keeping up, or the buffer pool is small for the workload.",
        ("Innodb_buffer_pool_wait_free",), "should_be_zero", SHOULD_BE_ZERO, noun="waits", learn="lru"))
    return out


def _row_locks(w):
    group = "row_locks"
    out = []
    waits = w.delta("Innodb_row_lock_waits")
    ms = w.delta("Innodb_row_lock_time")
    out.append(_counter_metric(
        w, "row_lock_waits", group, "Row lock waits",
        "Times a statement had to wait for a row lock another transaction held "
        "(Innodb_row_lock_waits, counted when the wait starts).",
        ("Innodb_row_lock_waits",), "in_moderation", IN_MODERATION, link=_LOCKS, noun="waits", history=True))
    rate = w.rate(ms)
    if ms is None:
        out.append(_metric("row_lock_time", group, "Time waiting for row locks",
                           "Milliseconds sessions spent waiting for row locks.", "ms/s",
                           ("Innodb_row_lock_time",), status="unknown",
                           observation=_missing_text(w, ("Innodb_row_lock_time",)), link=_LOCKS))
    else:
        out.append(_metric(
            "row_lock_time", group, "Time waiting for row locks",
            "Milliseconds of row lock waiting per second of wall time (Innodb_row_lock_time, added "
            "when each wait ends). 1,000 ms per second means one session waited the whole time. A "
            "long wait that ends in the window adds all of its time here.",
            "ms/s", ("Innodb_row_lock_time",), value=rate, delta=ms, rate=rate,
            status=_grade(rate, ROW_LOCK_MS_PER_S),
            observation="%s ms of waiting ended in the window" % _count_text(ms), rule="row_lock_time",
            detail="%s ms in the window" % _count_text(ms), link=_LOCKS))
    if waits is None or ms is None:
        out.append(_metric("row_lock_avg", group, "Average row lock wait",
                           "Average length of a row lock wait in the window.", "ms",
                           ("Innodb_row_lock_time", "Innodb_row_lock_waits"), status="unknown",
                           observation=_missing_text(w, ("Innodb_row_lock_time", "Innodb_row_lock_waits")),
                           link=_LOCKS))
    elif not waits:
        out.append(_metric(
            "row_lock_avg", group, "Average row lock wait",
            "Average length of a row lock wait in the window.", "ms",
            ("Innodb_row_lock_time", "Innodb_row_lock_waits"), status="ok" if not ms else "info",
            observation="No row lock wait started in the window" + (
                "; %s ms of earlier waits ended in it" % _count_text(ms) if ms else ""),
            rule="row_lock_avg", link=_LOCKS))
    else:
        avg = float(ms) / waits
        out.append(_metric(
            "row_lock_avg", group, "Average row lock wait",
            "Wait time that ended in the window divided by the waits that started in it. Waits "
            "that span the window's edges make it approximate." + (
                " MariaDB rounds each wait down to whole milliseconds." if w.engine == "mariadb" else ""),
            "ms", ("Innodb_row_lock_time", "Innodb_row_lock_waits"), value=avg,
            status=_grade(avg, ROW_LOCK_AVG_MS),
            observation="%s ms over %s waits" % (_count_text(ms), _count_text(waits)), rule="row_lock_avg",
            link=_LOCKS))
    current = w.gauge("Innodb_row_lock_current_waits")
    current_a = w.gauge("Innodb_row_lock_current_waits", "a")
    both = min(current, current_a) if current is not None and current_a is not None else current
    out.append(_metric(
        "row_lock_current", group, "Waiting for a row lock now",
        "Sessions waiting for a row lock at the second sample (Innodb_row_lock_current_waits). "
        "Brief waits are normal, so the status needs waits in both samples; if it stays above "
        "zero, Locks shows who blocks whom.",
        "sessions", ("Innodb_row_lock_current_waits",), value=current,
        status=_grade(both, ROW_LOCK_CURRENT) if current is not None else "unknown",
        observation=("%s waiting now, %s at the first sample" % (_count_text(current), _count_text(current_a)))
        if current is not None else _missing_text(w, ("Innodb_row_lock_current_waits",)),
        rule="row_lock_current", link=_LOCKS))
    return out


def _temp_sorts(w):
    group = "temp_sorts"
    out = []
    tmp = w.delta("Created_tmp_tables")
    disk = w.delta("Created_tmp_disk_tables")
    names = ("Created_tmp_disk_tables", "Created_tmp_tables")
    counting = ("On MariaDB a table that starts in memory and moves to disk counts twice in "
                "Created_tmp_tables, so the share understates moves to disk."
                if w.engine == "mariadb" else
                "TempTable overflow into memory-mapped files (temptable_max_mmap) is not counted as disk.")
    explain = ("Internal temporary tables (for GROUP BY, DISTINCT, UNION, derived tables) that were "
               "created on disk or moved there, as a share of all of them. " + counting)
    if tmp is None or disk is None:
        out.append(_metric("tmp_disk_ratio", group, "Temporary tables on disk", explain, "%", names,
                           status="unknown", observation=_missing_text(w, names), link=_TOP, learn="tmp"))
    elif not tmp:
        out.append(_metric("tmp_disk_ratio", group, "Temporary tables on disk", explain, "%", names,
                           status="ok" if not disk else "info",
                           observation="No internal temporary tables in the window", rule="tmp_disk",
                           detail="0 created", link=_TOP, learn="tmp",
                           inputs={"Created_tmp_tables": tmp, "Created_tmp_disk_tables": disk}))
    else:
        pct = min(100.0, 100.0 * disk / tmp)
        status = _grade(pct, TMP_DISK_PCT)
        if tmp < MIN_TMP_TABLES and status == "problem":
            status = "watch"
        out.append(_metric(
            "tmp_disk_ratio", group, "Temporary tables on disk", explain, "%", names, value=pct,
            delta=disk, rate=w.rate(disk), status=status,
            observation="%s of %s temporary tables were on disk" % (_count_text(disk), _count_text(tmp)),
            rule="tmp_disk", detail="%s of %s (%s/s)" % (_count_text(disk), _count_text(tmp),
                                                       _count_text(_round(w.rate(tmp), 2))),
            link=_TOP, learn="tmp", inputs={"Created_tmp_tables": tmp, "Created_tmp_disk_tables": disk}))
    out.append(_counter_metric(
        w, "sort_merge_passes", group, "Sort merge passes",
        "Merge passes sorts needed because the rows did not fit in sort_buffer_size, so sorted "
        "runs were written to temporary files and merged (Sort_merge_passes).",
        ("Sort_merge_passes",), "in_moderation", IN_MODERATION, link=_TOP, learn="filesort", noun="passes"))
    out.append(_counter_metric(
        w, "select_full_join", group, "Joins without an index",
        "Joins that read a later table with a full scan because no index fit the join "
        "(Select_full_join). Usually a missing index on the join column." + (
            " On MySQL a hash join's inner table counts too; it is read once, not once per row."
            if w.engine == "mysql" else ""),
        ("Select_full_join",), "joins_without_index", JOINS_WITHOUT_INDEX, link=_TOP, learn="join",
        noun="joins"))
    out.append(_counter_metric(
        w, "select_scan", group, "Full scans of the first table",
        "Query blocks whose first table was read with a full table or index scan (Select_scan). "
        "Common and often harmless for small tables." + (
            " MariaDB also counts a full index scan of a later table here." if w.engine == "mariadb" else ""),
        ("Select_scan",), "none", None, link=_TOP, learn="full_scan", noun="scans"))
    return out


def _table_cache(w):
    group = "table_cache"
    out = [_counter_metric(
        w, "opened_tables", group, "Tables opened",
        "Tables opened because no free instance was in the table cache (Opened_tables; user "
        "temporary tables count too). A steady rate suggests table_open_cache is too small.",
        ("Opened_tables",), "in_moderation", IN_MODERATION, noun="tables")]
    open_now = w.gauge("Open_tables")
    cache = w.var("table_open_cache")
    used = 100.0 * open_now / cache if open_now is not None and cache else None
    out.append(_metric(
        "table_cache_used", group, "Table cache in use",
        "Open table instances as a share of table_open_cache. A full cache is normal on a busy "
        "server; tables opened per second shows whether it is too small.",
        "%", ("Open_tables", "table_open_cache"), value=used,
        status="info" if used is not None else "unknown",
        observation=("%s of table_open_cache %s" % (_count_text(open_now), _count_text(cache)))
        if used is not None else ("This server does not report table_open_cache." if not cache
                                  else _missing_text(w, ("Open_tables",))),
        detail="%s of %s" % (_count_text(open_now), _count_text(cache)) if used is not None else "",
        inputs={"Open_tables": open_now, "table_open_cache": cache}))
    return out


def _redo_capacity(w):
    """(bytes, source name): config_review.redo_capacity, shared with Config."""
    from .config_review import redo_capacity
    return redo_capacity(w.engine, w.vars, w.b)


def _redo_io(w):
    group = "redo_io"
    out = [
        _counter_metric(w, "data_reads", group, "Data file reads",
                        "Read operations on InnoDB data files per second (Innodb_data_reads). One "
                        "operation can read several pages.",
                        ("Innodb_data_reads",), "none", None, noun="reads"),
        _counter_metric(w, "data_writes", group, "Data file writes",
                        "Write operations InnoDB issued per second (Innodb_data_writes). " + (
                            "On MySQL this includes redo log writes."
                            if w.engine == "mysql" else "MariaDB does not count redo log writes here."),
                        ("Innodb_data_writes",), "none", None, noun="writes"),
    ]
    # MySQL's Innodb_os_log_written re-counts rewrites of partly filled
    # blocks, so the LSN delta measures the redo generated when it exists.
    source = "Innodb_os_log_written"
    if "Innodb_redo_log_current_lsn" in w.a and "Innodb_redo_log_current_lsn" in w.b:
        source = "Innodb_redo_log_current_lsn"
    written = w.delta(source)
    rate = w.rate(written)
    capacity, capacity_name = _redo_capacity(w)
    explain = "Redo log generated per second (%s%s). " % (source, {
        "Innodb_redo_log_current_lsn": ", the log sequence number's growth",
        "Innodb_os_log_written": ", the redo generated since startup on MariaDB" if w.engine == "mariadb" else "",
    }[source])
    detail = ""
    points = _redo_points(w, capacity)
    age = points["ages"][1]
    if capacity and rate and age is not None and points["pressure"]:
        if age < points["pressure"]:
            detail = "%s in about %s at this rate (checkpoint age %s of %s)" % (
                "adaptive flushing reaches its limit" if w.engine == "mysql" else "flush-ahead starts",
                _duration_text((points["pressure"] - age) / rate), _bytes_text(age), _bytes_text(capacity))
        else:
            detail = "checkpoint age %s of %s: %s" % (
                _bytes_text(age), _bytes_text(capacity),
                "adaptive flushing is at its limit" if w.engine == "mysql" else "InnoDB is flushing ahead")
    elif capacity and rate:
        detail = "%s of redo capacity covers about %s at this rate" % (_bytes_text(capacity),
                                                                       _duration_text(capacity / rate))
    elif capacity:
        detail = "redo capacity %s" % _bytes_text(capacity)
    if written is None:
        out.append(_metric("redo_generated", group, "Redo generated", explain, "B/s", (source,),
                           status="unknown", observation=_missing_text(w, (source,))))
    else:
        out.append(_metric(
            "redo_generated", group, "Redo generated",
            explain + "MySQL's adaptive flushing reaches its age limit when the checkpoint age is about 73% "
            "of the redo capacity, and MariaDB flushes ahead from 7/8 of Innodb_checkpoint_max_age (about 71% "
            "of innodb_log_file_size), so the time shown runs from the current checkpoint age to that point. "
            "Writes stall if the log fills before a checkpoint frees it.",
            "B/s", (source,) + ((capacity_name,) if capacity_name else ())
            + (tuple(n for n in points["names"] if n != source) if age is not None else ()),
            value=rate, delta=written, rate=rate, status="info",
            observation="%s of redo in the window" % _bytes_text(written), detail=detail,
            inputs={source: written, "redo_capacity_bytes": capacity, "checkpoint_age_bytes": age}))
    out.append(_counter_metric(
        w, "log_waits", group, "Waits for the log buffer",
        "Times a change had to wait for space in the redo log buffer (Innodb_log_waits). "
        "innodb_log_buffer_size is small for the write load.",
        ("Innodb_log_waits",), "should_be_zero", SHOULD_BE_ZERO, noun="waits"))
    return out


# ---- diagnostics (pure) -----------------------------------------------------------
# Each diagnostic combines a few deltas into one explained answer: a title,
# the numbers with units and rates, the rule used, a plain-language verdict,
# the why, the settings involved (current value and engine default), and a
# recommendation only with a caveat. myflames never changes a setting.


def _fmt(value, unit):
    """A number with its unit, as the diagnostics print it."""
    if value is None:
        return "?"
    if unit == "B/s":
        return _bytes_text(value) + "/s"
    if unit == "B":
        return _bytes_text(value)
    if unit == "%":
        return "%.1f%%" % value
    if unit == "s":
        return _duration_text(value)
    if unit.startswith("/"):
        return "%s%s" % (_count_text(_round(value, 2)), unit)
    return ("%s %s" % (_count_text(_round(value, 2)), unit)).strip()


def _about(seconds):
    """'about 4 min 10 s', or 'less than a second'."""
    return "less than a second" if seconds is not None and seconds < 1 else "about " + _duration_text(seconds)


def _number_item(key, label, value, unit, text=None, estimate=False):
    item = {"key": key, "label": label, "value": _round(value, 2), "unit": unit,
            "text": text if text is not None else _fmt(value, unit)}
    if estimate:
        item["estimate"] = True
    return item


def _setting(w, name):
    """{name, value, display, default} for one variable, or None if absent."""
    if name not in w.vars:
        return None
    from .config_review import DEFAULTS
    default = DIAGNOSTIC_DEFAULTS[w.engine].get(name, DEFAULTS[w.engine].get(name))
    return {"name": name, "value": w.vars.get(name), "display": _setting_text(name, w.vars.get(name)),
            "default": _setting_text(name, default) if default is not None else None}


def _setting_text(name, value):
    number = _number(value)
    if name in _BYTE_SETTINGS and isinstance(number, int):
        return "unlimited" if number >= (1 << 62) else _bytes_text(number)
    if isinstance(number, float):
        return _count_text(_round(number, 2))
    return str(value)


def _diagnostic(key, title, status, headline, verdict, why, rule, value=None, unit="", numbers=(), settings=(),
                recommendation=None, sources=(), learn=None, link=None, trend=None, state=None):
    return {"key": key, "title": title, "status": status, "state": state, "headline": headline,
            "value": _round(value, 2), "unit": unit, "verdict": verdict, "why": why,
            "rule": "No threshold: compare with this server's usual values" if rule is None else rule,
            "numbers": [n for n in numbers if n], "settings": [s for s in settings if s],
            "recommendation": recommendation, "sources": list(sources), "learn": learn, "link": link,
            "trend": trend}


def _unknown(w, key, title, why, names, sources=None, settings=(), learn=None, link=None, trend_key=None,
             trend_label="", trend_unit="", text=None):
    text = text or _missing_text(w, names)
    return _diagnostic(key, title, "unknown", "Unknown", text, why, "Not judged: " + text[0].lower() + text[1:],
                       sources=sources or names, settings=settings, learn=learn, link=link,
                       trend={"key": trend_key, "label": trend_label, "unit": trend_unit, "value": None}
                       if trend_key else None)


def _in_moderation(delta, rate):
    """The IN_MODERATION rule: watch at 1/s, problem at 10/s, MIN_EVENTS needed."""
    if delta is None or rate is None:
        return "unknown"
    if delta < MIN_EVENTS:
        return "ok"
    return _grade(rate, IN_MODERATION)


def _diag_buffer_pool(w, metrics):
    key, title = "buffer_pool", "Buffer pool: full or warming"
    total = w.gauge("Innodb_buffer_pool_pages_total")
    free = w.gauge("Innodb_buffer_pool_pages_free")
    data = w.gauge("Innodb_buffer_pool_pages_data")
    misc = w.gauge("Innodb_buffer_pool_pages_misc")
    data_a = _number(w.a.get("Innodb_buffer_pool_pages_data"))
    page = w.var("innodb_page_size") or _number(w.b.get("Innodb_page_size")) or 16384
    depth = w.var("innodb_lru_scan_depth")
    assumed_depth = depth is None
    depth = depth or LRU_SCAN_DEPTH_DEFAULT[w.engine]
    instances = (w.var("innodb_buffer_pool_instances") or 1) if w.engine == "mysql" else 1
    target = depth * instances
    read_names = ("Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_ahead") if w.engine == "mysql" \
        else ("Innodb_buffer_pool_reads",)
    pages = ("Innodb_buffer_pool_pages_total", "Innodb_buffer_pool_pages_free", "Innodb_buffer_pool_pages_data",
             "Innodb_buffer_pool_pages_misc")
    sources = pages + read_names + ("Innodb_data_read", "innodb_page_size", "innodb_lru_scan_depth") + (
        ("innodb_buffer_pool_instances",) if w.engine == "mysql" else ())
    settings = [_setting(w, n) for n in ("innodb_buffer_pool_size", "innodb_page_size", "innodb_lru_scan_depth",
                                         "innodb_buffer_pool_instances")]
    trend_label = "Read from disk"
    read_ahead = ("MySQL adds Innodb_buffer_pool_read_ahead, because Innodb_buffer_pool_reads leaves out linear "
                  "read-ahead" if w.engine == "mysql" else
                  "MariaDB's Innodb_buffer_pool_reads already includes read-ahead, so nothing is added")
    why = ("InnoDB keeps a small free list: %s (%s x %s = %s pages here%s). So \"full\" means free pages at or "
           "below that target, not zero. Once it is full, every page read from disk "
           "evicts another one. Bytes read from disk are the pages read from disk times innodb_page_size (%s); "
           "%s. Innodb_data_read counts the bytes InnoDB read from its data files: it is lower for "
           "ROW_FORMAT=COMPRESSED tables, which read the smaller compressed page%s."
           % ("while the server is busy, its page cleaner evicts the least recently used pages until each buffer "
              "pool instance has innodb_lru_scan_depth free pages, and an idle full pool can show 0 free pages"
              if w.engine == "mysql" else
              "once free pages drop below half of innodb_lru_scan_depth, the page cleaner evicts the least "
              "recently used pages until there are innodb_lru_scan_depth free pages again (MariaDB has one "
              "buffer pool instance)",
              _count_text(depth), _count_text(instances), _count_text(target),
              ", the engine default because the server did not report the variable" if assumed_depth else "",
              _bytes_text(page), read_ahead,
              ", and on MySQL it also counts redo log reads, but only during startup" if w.engine == "mysql" else ""))
    if total and target * 2 >= total:
        why += (" This buffer pool is small for that target (%s pages in all), so InnoDB keeps a large share of "
                "it free and the full or warming call is approximate." % _count_text(total))
    if not total or free is None:
        return _unknown(w, key, title, why, pages, sources, settings, learn="buffer_pool_warmup",
                        trend_key="bp_read_bytes", trend_label=trend_label, trend_unit="B/s")
    used_pct = 100.0 * (total - free) / total
    disk = w.delta(*read_names)
    disk_rate = w.rate(disk)
    read_bytes = disk * page if disk is not None else None
    read_rate = w.rate(read_bytes)
    data_read = w.delta("Innodb_data_read")
    data_read_rate = w.rate(data_read)
    growth = data - data_a if data is not None and data_a is not None and not w.restarted else None
    fill_rate = w.rate(growth)
    full = free <= target
    eta = (free - target) / fill_rate if not full and fill_rate and growth * page >= MIN_FILL_BYTES else None
    load = w.b.get("Innodb_buffer_pool_load_status") or ""
    loading = load.lower().startswith("loaded ")
    numbers = [
        _number_item("used_pct", "Buffer pool in use", used_pct, "%",
                     "%.1f%% (%s of %s pages hold data or other structures)" % (
                         used_pct, _count_text(total - free), _count_text(total))),
        _number_item("free_pages", "Free pages", free, "pages",
                     "%s (the page cleaner's target is %s)" % (_count_text(free), _count_text(target))),
        _number_item("data_pages", "Data pages", data, "pages",
                     "%s%s" % (_count_text(data), "" if growth is None else ", %s%s in the window" % (
                         "+" if growth > 0 else "", _count_text(growth)))) if data is not None else None,
        _number_item("misc_pages", "Other pages (adaptive hash index, locks, compressed-page buffers)", misc, "pages") if misc is not None
        else None,
        _number_item("disk_reads", "Pages read from disk", disk_rate, "/s",
                     "%s/s (%s in the window)" % (_count_text(_round(disk_rate, 2)), _count_text(disk)))
        if disk is not None else None,
        _number_item("read_bytes", "Bytes read from disk (pages x page size)", read_rate, "B/s",
                     "%s/s (%s in the window)" % (_bytes_text(read_rate), _bytes_text(read_bytes)))
        if read_bytes is not None and read_rate is not None else None,
        _number_item("data_read", "Innodb_data_read", data_read_rate, "B/s",
                     "%s/s (%s in the window)" % (_bytes_text(data_read_rate), _bytes_text(data_read)))
        if data_read is not None and data_read_rate is not None else None,
    ]
    if read_rate and data_read_rate:
        why += " In this window Innodb_data_read is %.0f%% of pages x page size." % (100.0 * data_read_rate / read_rate)
    # Read-ahead waste: pages read ahead and evicted before any access.
    waste = None
    if "Innodb_buffer_pool_read_ahead_evicted" in w.a and "Innodb_buffer_pool_read_ahead_evicted" in w.b:
        ahead = w.delta("Innodb_buffer_pool_read_ahead")
        evicted = w.delta("Innodb_buffer_pool_read_ahead_evicted")
        if ahead and evicted is not None:
            waste = 100.0 * evicted / ahead
            numbers.append(_number_item("read_ahead_waste", "Read-ahead pages evicted unused", waste, "%",
                                        "%s of %s (%s)" % (_count_text(evicted), _count_text(ahead),
                                                           _fmt(waste, "%"))))
            sources += ("Innodb_buffer_pool_read_ahead_evicted",)
    trend = {"key": "bp_read_bytes", "label": trend_label, "unit": "B/s", "value": _round(read_rate, 2)}
    recommendation = None
    if full:
        state = "full"
        headline = "%s/s read from disk" % _bytes_text(read_rate) if read_rate is not None else "Full"
        if disk is None or disk_rate is None:
            status = "unknown"
            verdict = "The buffer pool is full (%s free pages), but the disk reads are unknown: %s" % (
                _count_text(free), _missing_text(w, read_names))
        elif disk_rate < MIN_DISK_READS_PER_S:
            status = "ok"
            verdict = ("The buffer pool is full (%s free pages, at or below the page cleaner's target of %s) and "
                       "InnoDB read almost nothing from disk: %s pages per second (%s/s). The data this workload "
                       "uses fits in memory." % (_count_text(free), _count_text(target),
                                                 _count_text(_round(disk_rate, 2)), _bytes_text(read_rate)))
        else:
            hit = next((m for m in metrics if m["key"] == "bp_hit_ratio"), None)
            status = "problem" if hit and hit["status"] == "problem" else "watch"
            pool = w.var("innodb_buffer_pool_size")
            verdict = ("The buffer pool is full and InnoDB read %s pages per second from disk (%s/s). Pages this "
                       "workload needs are evicted and read again: the data it uses is larger than the buffer pool "
                       "(innodb_buffer_pool_size %s), or some queries read much more data than they return."
                       % (_count_text(_round(disk_rate, 2)), _bytes_text(read_rate),
                          _bytes_text(pool) if pool else "?"))
            if status == "problem":
                verdict += " %s of page requests missed the buffer pool." % (
                    "%.1f%%" % (100 - hit["value"]) if hit.get("value") is not None else "Many")
            if waste is not None and waste >= READ_AHEAD_WASTE_PCT:
                verdict += (" %.0f%% of the pages read ahead were evicted before any use, so read-ahead "
                            "is adding I/O here." % waste)
            recommendation = {
                "text": "Look for queries that scan large tables first (Top statements). If the reads are data the "
                        "workload really needs, a larger innodb_buffer_pool_size keeps more of it in memory; the "
                        "Parameters view (myflames config) shows how this server can resize it.",
                "caveat": "Leave memory for connections, the operating system and other processes: a buffer pool "
                          "that makes the server swap is far worse than disk reads. One window can catch a one-off "
                          "scan, so watch the trend over several refreshes before changing anything."}
    else:
        young = w.uptime is not None and w.uptime < WARMUP_SECONDS
        growing = growth is not None and growth > 0
        state = "warming" if young or growing or loading else "room"
        status = "info"
        if state == "warming":
            headline = "Warming: %.0f%% full" % used_pct
            if eta is not None:
                headline += ", %s to full at this rate" % _about(eta)
            verdict = "The buffer pool is still filling: %.1f%% of it is in use (%s of %s pages are free)." % (
                used_pct, _count_text(free), _count_text(total))
            if growing:
                verdict += " Data pages grew by %s in the window (%s pages per second, %s/s)." % (
                    _count_text(growth), _count_text(_round(fill_rate, 2)), _bytes_text(fill_rate * page))
                if eta is not None:
                    verdict += (" At this rate it is full in %s. That is an estimate: the fill rate "
                                "follows the workload." % _about(eta))
            elif growth is not None:
                verdict += " It did not grow in this window."
            if loading:
                verdict += " A buffer pool load from the dump file is in progress (%s)." % load
            verdict += " Reads from disk are expected while it warms."
        else:
            headline = "Not full: %.0f%% in use" % used_pct
            verdict = ("The buffer pool is %.1f%% in use%s and did not grow in this window. The data read since "
                       "startup fits with room to spare (or the pool was resized or emptied recently)."
                       % (used_pct, " after %s up" % _duration_text(w.uptime) if w.uptime is not None else ""))
        if eta is not None:
            numbers.append(_number_item("fill_rate", "Fill rate", fill_rate, "pages/s",
                                        "%s pages/s (%s/s)" % (_count_text(_round(fill_rate, 2)),
                                                               _bytes_text(fill_rate * page))))
            numbers.append(_number_item("time_to_full", "Time to full at this rate", eta, "s", _about(eta),
                                        estimate=True))
    return _diagnostic(key, title, status, headline, verdict, why, DIAGNOSTIC_RULES["buffer_pool"],
                       value=read_rate if full else used_pct, unit="B/s" if full else "%", numbers=numbers,
                       settings=settings, recommendation=recommendation, sources=sources,
                       learn="lru" if full else "buffer_pool_warmup", trend=trend, state=state)


def _redo_points(w, capacity):
    """Checkpoint ages (first and second sample) and the source thresholds,
    in bytes: {"ages", "pressure", "forced", "label", "names"}.

    MySQL 8.0.30+: age = Innodb_redo_log_current_lsn - _checkpoint_lsn; the
    points are fractions of the capacity (REDO_POINTS_MYSQL). MariaDB: age =
    Innodb_checkpoint_age, against Innodb_checkpoint_max_age (flush-ahead at
    7/8 of it, writes wait at it), or 0.71 / 0.81 of innodb_log_file_size when
    max_age is missing. See the module docstring for the sources.
    """
    if w.engine == "mysql":
        ages = []
        for sample in (w.a, w.b):
            current = _number(sample.get("Innodb_redo_log_current_lsn"))
            checkpoint = _number(sample.get("Innodb_redo_log_checkpoint_lsn"))
            ages.append(current - checkpoint if current is not None and checkpoint is not None else None)
        points = {"pressure": REDO_POINTS_MYSQL[0] * capacity if capacity else None,
                  "forced": REDO_POINTS_MYSQL[1] * capacity if capacity else None,
                  "wait": REDO_POINTS_MYSQL[2] * capacity if capacity else None,
                  "names": ("Innodb_redo_log_current_lsn", "Innodb_redo_log_checkpoint_lsn")}
    else:
        ages = [_number(w.a.get("Innodb_checkpoint_age")), _number(w.b.get("Innodb_checkpoint_age"))]
        max_age = _number(w.b.get("Innodb_checkpoint_max_age"))
        if max_age:
            points = {"pressure": REDO_POINTS_MARIADB[0] * max_age, "forced": REDO_POINTS_MARIADB[1] * max_age,
                      "wait": max_age, "names": ("Innodb_checkpoint_age", "Innodb_checkpoint_max_age")}
        else:
            points = {"pressure": 0.71 * capacity if capacity else None,
                      "forced": 0.81 * capacity if capacity else None,
                      "wait": 0.81 * capacity if capacity else None, "names": ("Innodb_checkpoint_age",)}
    points["ages"] = ages
    return points


def _diag_redo(w):
    key, title = "redo", "Redo log size for the write rate"
    lsn = "Innodb_redo_log_current_lsn" if w.engine == "mysql" and "Innodb_redo_log_current_lsn" in w.a \
        and "Innodb_redo_log_current_lsn" in w.b else None
    written = w.delta("Innodb_os_log_written")
    generated = w.delta(lsn) if lsn else written
    rate = w.rate(generated)
    written_rate = w.rate(written)
    capacity, capacity_name = _redo_capacity(w)
    waits = w.delta("Innodb_log_waits")
    points = _redo_points(w, capacity)
    ages = points["ages"]
    age = ages[1]
    peak = max([a for a in ages if a is not None] or [None]) if age is not None else None
    sources = ((lsn,) if lsn else ()) + ("Innodb_os_log_written", "Innodb_log_waits") + points["names"] + (
        (capacity_name,) if capacity_name else ())
    legacy = capacity_name == "innodb_log_file_size x innodb_log_files_in_group"
    settings = [_setting(w, n) for n in (("innodb_redo_log_capacity",) if w.engine == "mysql" else ())
                + (("innodb_log_file_size", "innodb_log_files_in_group") if legacy
                   else ("innodb_log_file_size",) if w.engine == "mariadb" else ())]
    if w.engine == "mysql":
        counters = ("Two counters measure redo. The log sequence number (Innodb_redo_log_current_lsn) grows by the "
                    "redo generated, counted in 512-byte log blocks including their headers and trailers; it is "
                    "what fills the capacity, so the times are computed from it. Innodb_os_log_written counts the "
                    "bytes written to the redo files: the log writer rewrites the last, partly filled block on "
                    "each write, so with many small commits it runs well above the redo generated" if lsn else
                    "This server does not report Innodb_redo_log_current_lsn (MySQL before 8.0.30), so the rate "
                    "uses Innodb_os_log_written, the bytes written to the redo files, which re-counts the last "
                    "partly filled block on each write and so runs high")
        thresholds = ("The checkpoint age (Innodb_redo_log_current_lsn minus Innodb_redo_log_checkpoint_lsn) is "
                      "how much of the capacity is in use. MySQL's redo-driven adaptive flushing starts early "
                      "(about 8% of the capacity, innodb_adaptive_flushing_lwm) and grows with the age, reaches "
                      "its age limit at about 73%, forces flushing from about 79% (innodb_flush_sync, on by "
                      "default), and makes user writes wait from about 84%. These points compare the age of the "
                      "oldest dirty page, which is at most the checkpoint age.")
    else:
        counters = ("MariaDB's Innodb_os_log_written is the growth of the log sequence number since startup (the "
                    "redo generated), not the bytes written to the file, so one counter serves both")
        thresholds = ("The checkpoint age (Innodb_checkpoint_age) is how much of the redo log is in use. MariaDB "
                      "starts flushing ahead at 7/8 of Innodb_checkpoint_max_age (about 71% of "
                      "innodb_log_file_size) and makes writes wait at Innodb_checkpoint_max_age (about 81%).")
    why = ("InnoDB writes every change to the redo log first. A checkpoint frees redo space once the matching dirty "
           "pages are flushed, so the capacity limits how far flushing can fall behind. %s A larger redo log "
           "smooths that out but makes crash recovery longer. %s. Innodb_log_waits counts waits for space in the "
           "in-memory log buffer (innodb_log_buffer_size), a different limit." % (thresholds, counters))
    rule = (("watch when the checkpoint age reaches about 73% of the capacity (where adaptive flushing reaches "
             "its age limit) in either sample, problem at about 79% (forced flushing)" if w.engine == "mysql" else
             "watch when the checkpoint age reaches 7/8 of Innodb_checkpoint_max_age (flushing ahead) in either "
             "sample, problem at Innodb_checkpoint_max_age (writes wait)")
            + "; also watch when the capacity holds less than about %g hour of redo at this window's rate (a "
              "common rule of thumb; this window may not be the peak) or when Innodb_log_waits grew"
            % _redo_hours())
    trend = {"key": "redo_bytes", "label": "Redo generated", "unit": "B/s", "value": _round(rate, 2)}
    if generated is None:
        names = ((lsn,) if lsn else ()) + ("Innodb_os_log_written",)
        return _unknown(w, key, title, why, names, sources, settings, trend_key="redo_bytes",
                        trend_label="Redo generated", trend_unit="B/s")
    per_hour = rate * 3600 if rate is not None else None
    coverage = capacity / rate if capacity and rate else None
    pressure_at = points["pressure"]
    until = (pressure_at - age) / rate if pressure_at and age is not None and rate and age < pressure_at else None
    numbers = [
        _number_item("redo_rate", "Redo generated" + (" (LSN growth)" if lsn else ""), rate, "B/s",
                     "%s/s (%s in the window)" % (_bytes_text(rate), _bytes_text(generated))),
        _number_item("redo_per_hour", "At this rate, per hour", per_hour, "B", _bytes_text(per_hour)),
        _number_item("coverage", "Capacity covers", coverage, "s",
                     "%s of writes at this rate" % _about(coverage)) if coverage else None,
        _number_item("os_log_written", "Written to the redo files (Innodb_os_log_written)", written_rate, "B/s",
                     "%s/s (%s in the window)%s" % (
                         _bytes_text(written_rate), _bytes_text(written),
                         "; %.0f%% more than the redo generated" % (100.0 * (written - generated) / generated)
                         if generated and written and written > generated else ""))
        if lsn and written is not None else None,
        _number_item("capacity", "Redo capacity", capacity, "B", "%s (%s)" % (_bytes_text(capacity), capacity_name))
        if capacity else None,
        _number_item("checkpoint_age", "Checkpoint age now", age, "B", "%s%s" % (
            _bytes_text(age), " (%.0f%% of the capacity)" % (100.0 * age / capacity) if capacity else ""))
        if age is not None else None,
        _number_item("until_pressure", "Time until the adaptive-flushing limit, at this rate" if w.engine == "mysql"
                     else "Time until InnoDB flushes ahead, at this rate", until, "s",
                     "%s (from the current checkpoint age to %s)" % (_about(until), _bytes_text(pressure_at)),
                     estimate=True)
        if until is not None else None,
        _number_item("log_waits", "Waits for the log buffer", waits, "", _count_text(waits) + " in the window")
        if waits is not None else None,
    ]
    if not capacity:
        return _diagnostic(key, title, "info", "%s/s of redo" % _bytes_text(rate),
                           "InnoDB generated %s/s of redo, but this server does not report the redo capacity." % (
                               _bytes_text(rate)), why, None, value=rate, unit="B/s", numbers=numbers,
                           settings=settings, sources=sources, trend=trend)
    hours = _redo_hours()
    if coverage is None:
        headline = "No redo in this window"
        verdict = "InnoDB wrote no redo in the window."
        status = "ok"
    else:
        headline = "Covers %s of writes" % _about(coverage)
        verdict = ("At this window's rate (%s/s, about %s per hour) the %s redo capacity holds %s of "
                   "writes." % (_bytes_text(rate), _bytes_text(per_hour), _bytes_text(capacity), _about(coverage)))
        status = "watch" if coverage < hours * 3600 else "ok"
        if status == "watch":
            verdict += " That is less than the hour a common rule of thumb aims for at peak."
        else:
            verdict += " That meets the rule of thumb of about an hour, if this window is near the peak."
    if age is not None:
        verdict += " The checkpoint age is %s (%.0f%% of the capacity)" % (_bytes_text(age), 100.0 * age / capacity)
        verdict += ("; at this rate adaptive flushing reaches its age limit in %s." if w.engine == "mysql" else
                    "; at this rate InnoDB starts flushing ahead in %s.") % _about(until) if until is not None else "."
    if peak is not None and points["forced"] and peak >= points["forced"]:
        status = "problem"
        headline = "Checkpoint age at %.0f%% of the capacity" % (100.0 * peak / capacity)
        verdict += (" It reached %s, where InnoDB %s, so writes are limited by flushing."
                    % (_bytes_text(peak), "forces flushing" if w.engine == "mysql" else "makes writes wait"))
    elif peak is not None and pressure_at and peak >= pressure_at:
        if status == "ok":
            status = "watch"
        headline = "Checkpoint age at %.0f%% of the capacity" % (100.0 * peak / capacity)
        verdict += " It reached %s, where %s." % (
            _bytes_text(peak), "adaptive flushing is at its age limit" if w.engine == "mysql"
            else "InnoDB flushes dirty pages ahead to free redo space")
    if waits:
        if status == "ok":
            status = "watch"
        verdict += (" %s waits for space in the log buffer happened in the window; that points at "
                    "innodb_log_buffer_size, not the redo capacity." % _count_text(waits))
    recommendation = None
    if status in ("watch", "problem") and coverage is not None and (
            coverage < hours * 3600 or (peak is not None and pressure_at and peak >= pressure_at)):
        from .config_review import MIB
        target = max(int(rate * 3600 * hours), capacity)
        target = ((target + 256 * MIB - 1) // (256 * MIB)) * 256 * MIB
        if target > capacity:
            if w.engine == "mysql" and "innodb_redo_log_capacity" not in w.vars:
                how = ("set innodb_log_file_size x innodb_log_files_in_group to it in the configuration file and "
                       "restart (this MySQL has no innodb_redo_log_capacity)")
            elif w.engine == "mariadb" and _version_tuple(w.version) < (10, 9, 1):
                how = ("set innodb_log_file_size = %d under [mysqld] in the option file and restart "
                       "(the size is the total for its single redo file; innodb_log_file_size is read-only "
                       "before MariaDB 10.9.1)" % target)
            else:
                how = ("SET GLOBAL %s = %d; resizes it online%s; also set it in the configuration file%s so a restart "
                       "keeps it" % ("innodb_redo_log_capacity" if w.engine == "mysql" else "innodb_log_file_size", target,
                                     "" if w.engine == "mysql" else " (the statement waits until the resize finishes)",
                                     " (or use SET PERSIST)" if w.engine == "mysql" else ""))
            recommendation = {
                "text": "If this rate is typical of busy periods, about an hour of redo at it is %s (rounded up to "
                        "256 MiB): %s." % (_bytes_text(target), how),
                "caveat": "One short window may be a burst or a lull: compare several refreshes, or the Parameters "
                          "view's average since startup. A larger redo log uses more disk and makes crash recovery longer."}
    return _diagnostic(key, title, status, headline, verdict, why, rule, value=coverage,
                       unit="s", numbers=numbers, settings=settings, recommendation=recommendation,
                       sources=sources, trend=trend)


def _redo_hours():
    """config_review.REDO_HOURS: one rule of thumb for both tabs."""
    from .config_review import REDO_HOURS
    return REDO_HOURS


def _diag_table_cache(w):
    key, title = "table_cache", "Table open cache"
    names = ("Table_open_cache_hits", "Table_open_cache_misses", "Table_open_cache_overflows")
    hits = w.delta("Table_open_cache_hits")
    misses = w.delta("Table_open_cache_misses")
    overflows = w.delta("Table_open_cache_overflows")
    opened = w.delta("Opened_tables")
    open_now = w.gauge("Open_tables")
    cache = w.var("table_open_cache")
    instances = w.var("table_open_cache_instances")
    active = _number(w.b.get("Table_open_cache_active_instances")) if w.engine == "mariadb" else None
    # MySQL splits table_open_cache across the instances; on MariaDB each
    # active instance holds up to table_open_cache (see the module docstring).
    room = cache * (active or 1) if cache and w.engine == "mariadb" else cache
    sources = names + ("Opened_tables", "Open_tables", "table_open_cache", "table_open_cache_instances") + (
        ("Table_open_cache_active_instances",) if active is not None else ())
    settings = [_setting(w, "table_open_cache"), _setting(w, "table_open_cache_instances"),
                _setting(w, "open_files_limit")]
    why = ("Each statement needs an open instance of every table it uses. A hit reuses a free instance from the "
           "cache. A miss opens a new one (Opened_tables): the first use of a table, or another session needing "
           "its own instance of a busy table, so misses alone are normal. An overflow means the cache was full "
           "and an unused instance was closed to make room%s: tables are being opened and closed over and "
           "over. " % ("" if w.engine == "mysql" else " (if one existed; otherwise the instance grows)")
           + ("MySQL divides table_open_cache across table_open_cache_instances." if w.engine == "mysql"
              else "On MariaDB each active instance (Table_open_cache_active_instances, up to "
                   "table_open_cache_instances) holds up to table_open_cache tables, so Open_tables can "
                   "exceed table_open_cache."))
    if overflows is None and opened is None:
        return _unknown(w, key, title, why, names + ("Opened_tables",), sources, settings, link=_TOP)
    rate = w.rate(overflows)
    lookups = (hits or 0) + (misses or 0) if hits is not None and misses is not None else None
    miss_pct = 100.0 * misses / lookups if lookups else None
    used = 100.0 * open_now / room if open_now is not None and room else None
    numbers = [
        _number_item("opened", "Tables opened", w.rate(opened), "/s",
                     "%s/s (%s in the window)" % (_count_text(_round(w.rate(opened), 2)), _count_text(opened)))
        if opened is not None else None,
        _number_item("miss_pct", "Cache misses", miss_pct, "%", "%s (%s misses, %s hits)" % (
            _fmt(miss_pct, "%") if miss_pct is not None else "none", _count_text(misses), _count_text(hits)))
        if lookups is not None else None,
        _number_item("overflows", "Overflows", rate, "/s", "%s/s (%s in the window)" % (
            _count_text(_round(rate, 2)), _count_text(overflows))) if overflows is not None else None,
        _number_item("open_tables", "Open tables", used, "%", "%s of %s%s" % (
            _count_text(open_now), _count_text(room),
            " (table_open_cache %s x %s active instances)" % (_count_text(cache), _count_text(active))
            if active and active > 1 else "")) if used is not None else None,
    ]
    if overflows is None:
        status = "unknown"
        verdict = _missing_text(w, ("Table_open_cache_overflows",))
        headline = "Unknown"
    else:
        status = _in_moderation(overflows, rate)
        headline = "%s overflows per second" % _count_text(_round(rate, 2)) if overflows else (
            "%s misses, no overflows" % _fmt(miss_pct, "%") if miss_pct else "No misses")
        if overflows >= MIN_EVENTS and status != "ok":
            verdict = ("The table cache overflowed %s times in the window (%s per second): it is full and keeps "
                       "closing tables to open others." % (_count_text(overflows), _count_text(_round(rate, 2))))
        elif overflows:
            verdict = "%s overflows in the window, too few to judge." % _count_text(overflows)
        else:
            verdict = "No overflows in the window: the tables in use fit in the cache."
        if miss_pct is not None:
            verdict += " %s of %s lookups missed." % (_fmt(miss_pct, "%"), _count_text(lookups))
        if used is not None:
            verdict += " %s of %s cached instances are open." % (_count_text(open_now), _count_text(room))
    recommendation = None
    if status in ("watch", "problem"):
        recommendation = {
            "text": "Raise table_open_cache so the tables in use fit (SET GLOBAL table_open_cache = <n>; it is "
                    "dynamic, and the configuration file%s keeps it after a restart), and check that "
                    "open_files_limit covers it." % (" or SET PERSIST" if w.engine == "mysql" else ""),
            "caveat": "Each cached table uses memory, and %s tables also hold file descriptors. "
                      % ("MyISAM" if w.engine == "mysql" else "MyISAM and Aria")
                      + ("MySQL can lower table_open_cache at startup to fit open_files_limit." if w.engine == "mysql"
                         else "MariaDB sizes table_open_cache per instance, so the total can reach table_open_cache "
                              "x table_open_cache_instances.")}
    return _diagnostic(key, title, status, headline, verdict, why, DIAGNOSTIC_RULES["table_cache"], value=rate,
                       unit="/s", numbers=numbers, settings=settings, recommendation=recommendation,
                       sources=sources, link=_TOP)


def _diag_table_definitions(w):
    key, title = "table_definitions", "Table definition cache"
    opened = w.delta("Opened_table_definitions")
    open_now = w.gauge("Open_table_definitions")
    size = w.var("table_definition_cache")
    sources = ("Opened_table_definitions", "Open_table_definitions", "table_definition_cache")
    settings = [_setting(w, "table_definition_cache")]
    why = ("The definition cache keeps parsed table definitions (shares), separate from the open table instances. "
           "A definition is opened the first time a table is used, or again after it was evicted because the "
           "cache was full. Steady openings while the cache is full mean table_definition_cache is smaller than "
           "the number of tables in use.")
    if opened is None:
        return _unknown(w, key, title, why, ("Opened_table_definitions",), sources, settings)
    rate = w.rate(opened)
    full = open_now is not None and size and open_now >= _table_cache_full() * size
    status = _in_moderation(opened, rate) if full else "ok"
    numbers = [
        _number_item("opened", "Definitions opened", rate, "/s", "%s/s (%s in the window)" % (
            _count_text(_round(rate, 2)), _count_text(opened))),
        _number_item("open", "Cached definitions", open_now, "", "%s of table_definition_cache %s" % (
            _count_text(open_now), _count_text(size))) if open_now is not None else None,
    ]
    headline = "%s opened per second" % _count_text(_round(rate, 2))
    verdict = "%s table definitions were opened in the window (%s per second)" % (
        _count_text(opened), _count_text(_round(rate, 2)))
    if open_now is not None and size:
        verdict += "; %s of %s are cached%s." % (_count_text(open_now), _count_text(size),
                                                 ", so the cache is full" if full else "")
    else:
        verdict += "."
    recommendation = None
    if status in ("watch", "problem"):
        recommendation = {"text": "Raise table_definition_cache to cover the tables in use (it is dynamic; set "
                                  "it in the configuration file too so a restart keeps it).",
                          "caveat": "Each cached definition uses memory. A tool that touches every table "
                                    "(a backup, a schema scan) also opens many definitions once."}
    return _diagnostic(key, title, status, headline, verdict, why, DIAGNOSTIC_RULES["table_definitions"],
                       value=rate, unit="/s", numbers=numbers, settings=settings, recommendation=recommendation,
                       sources=sources)


def _table_cache_full():
    from .config_review import TABLE_CACHE_FULL
    return TABLE_CACHE_FULL


def _diag_thread_cache(w):
    key, title = "thread_cache", "Thread cache"
    handling = (w.vars.get("thread_handling") or "").lower()
    connections = w.delta("Connections")
    created = w.delta("Threads_created")
    cached = w.gauge("Threads_cached")
    size = w.var("thread_cache_size")
    sources = ("Connections", "Threads_created", "Threads_cached", "thread_cache_size", "thread_handling")
    settings = [_setting(w, "thread_cache_size"), _setting(w, "thread_handling")]
    why = ("With one thread per connection, the server keeps up to thread_cache_size idle threads to reuse for "
           "new connections; Threads_created counts the threads it had to create instead. Connections is the "
           "next thread id: every connection that got past the max_connections check (failed logins included), "
           "plus a few internal threads. Each window includes this tool's own connection.")
    if handling and handling != "one-thread-per-connection":
        return _diagnostic(key, title, "info", "Not used: thread_handling=%s" % handling,
                           ("thread_handling is %s, so a thread pool manages the threads and Threads_created counts "
                            "pool workers, not connections. This ratio does not apply." if w.engine == "mariadb" else
                            "thread_handling is %s, so a connection-handler plugin (such as a thread pool) manages "
                            "the threads. This ratio does not apply.") % handling, why, None,
                           settings=settings, sources=sources, state="not_applicable")
    if connections is None or created is None:
        return _unknown(w, key, title, why, ("Connections", "Threads_created"), sources, settings)
    rate = w.rate(created)
    hit = 100.0 * (1 - float(created) / connections) if connections else None
    if hit is not None:
        hit = max(0.0, hit)
    miss_pct = 100.0 - hit if hit is not None else None
    numbers = [
        _number_item("connections", "New connections", w.rate(connections), "/s", "%s/s (%s in the window)" % (
            _count_text(_round(w.rate(connections), 2)), _count_text(connections))),
        _number_item("created", "Threads created", rate, "/s", "%s/s (%s in the window)" % (
            _count_text(_round(rate, 2)), _count_text(created))),
        _number_item("hit_pct", "Connections that reused a cached thread", hit, "%") if hit is not None else None,
        _number_item("cached", "Idle threads in the cache now", cached, "", "%s of thread_cache_size %s" % (
            _count_text(cached), _count_text(size))) if cached is not None else None,
    ]
    if connections < MIN_EVENTS:
        status = "ok"
        headline = "%s new connection%s" % (_count_text(connections), "" if connections == 1 else "s")
        verdict = "Only %s new connection%s in the window, too few to judge the thread cache." % (
            _count_text(connections), "" if connections == 1 else "s")
    else:
        watch = created >= MIN_EVENTS and rate is not None and rate >= 1.0 and miss_pct > THREAD_MISS_PCT
        status = "watch" if watch else "ok"
        headline = "%.0f%% of connections reused a thread" % hit
        verdict = "%s of %s new connections needed a new thread (%s per second); %.1f%% reused a cached one." % (
            _count_text(created), _count_text(connections), _count_text(_round(rate, 2)), hit)
        if cached is not None and size is not None:
            verdict += " %s idle threads are cached now (thread_cache_size %s)." % (_count_text(cached),
                                                                                     _count_text(size))
    recommendation = None
    if status == "watch":
        recommendation = {"text": "Raise thread_cache_size toward the number of connections that arrive in a "
                                  "burst (it is dynamic; set it in the configuration file too so a restart keeps "
                                  "it).",
                          "caveat": "Creating a thread costs little, so the gain is modest. Many short connections "
                                    "are better served by a connection pool in the application."}
    return _diagnostic(key, title, status, headline, verdict, why, DIAGNOSTIC_RULES["thread_cache"], value=hit,
                       unit="%", numbers=numbers, settings=settings, recommendation=recommendation,
                       sources=sources)


def _diag_binlog_cache(w):
    key, title = "binlog_cache", "Binary log cache"
    log_bin = (w.vars.get("log_bin") or "").upper()
    sources = ("Binlog_cache_use", "Binlog_cache_disk_use", "Binlog_stmt_cache_use", "Binlog_stmt_cache_disk_use",
               "binlog_cache_size", "binlog_stmt_cache_size", "log_bin")
    settings = [_setting(w, "log_bin"), _setting(w, "binlog_cache_size"), _setting(w, "binlog_stmt_cache_size")]
    why = ("With the binary log on, each session collects a transaction's events in memory (binlog_cache_size; "
           "binlog_stmt_cache_size for changes to non-transactional tables) and writes them to the binary log at "
           "commit. Events that do not fit spill to a temporary file on disk (Binlog_cache_disk_use, "
           "Binlog_stmt_cache_disk_use)%s. The cache is per session, so a larger size is multiplied by the sessions "
           "that write." % ("" if w.engine == "mysql" else
                            "; on MariaDB 12.3 and later with binlog_storage_engine set, they spill into the binary "
                            "log itself and still count"))
    if not log_bin:
        return _unknown(w, key, title, why, ("log_bin",), sources, settings,
                        text="This server does not report log_bin.")
    if log_bin not in ("ON", "1"):
        return _diagnostic(key, title, "info", "Binary log off",
                           "The binary log is off (log_bin=OFF), so there is no binary log cache to check.", why,
                           None, settings=settings, sources=sources, state="not_applicable")
    pairs = (("trx", "Transactions", "Binlog_cache_use", "Binlog_cache_disk_use"),
             ("stmt", "Non-transactional statements", "Binlog_stmt_cache_use", "Binlog_stmt_cache_disk_use"))
    numbers = []
    worst = "ok"
    parts = []
    spilled_total = 0
    used_total = 0
    for short, label, use_name, disk_name in pairs:
        use, disk = w.delta(use_name), w.delta(disk_name)
        if use is None or disk is None:
            continue
        pct = 100.0 * disk / use if use else None
        numbers.append(_number_item(short + "_disk_pct", label + " that spilled to disk", pct, "%",
                                    "%s of %s%s" % (_count_text(disk), _count_text(use),
                                                    " (%s)" % _fmt(pct, "%") if pct is not None else "")))
        spilled_total += disk
        used_total += use
        if disk >= MIN_EVENTS and pct is not None and pct >= BINLOG_DISK_PCT:
            worst = "watch"
            parts.append("%s of %s %s spilled to disk (%s)" % (_count_text(disk), _count_text(use), label.lower(),
                                                               _fmt(pct, "%")))
    if not numbers:
        return _unknown(w, key, title, why, ("Binlog_cache_use", "Binlog_cache_disk_use"), sources, settings)
    pct = 100.0 * spilled_total / used_total if used_total else None
    headline = "%s of %s spilled to disk" % (_count_text(spilled_total), _count_text(used_total)) if used_total \
        else "No binary log writes"
    if worst == "watch":
        verdict = "; ".join(parts) + " in the window."
        verdict = verdict[0].upper() + verdict[1:]
    elif used_total:
        verdict = "%s of %s binary log cache uses spilled to disk in the window." % (
            _count_text(spilled_total), _count_text(used_total))
    else:
        verdict = "No transaction wrote to the binary log in the window."
    recommendation = None
    if worst == "watch":
        recommendation = {"text": "If these are routine transactions, raise binlog_cache_size (or "
                                  "binlog_stmt_cache_size) so they fit; it is dynamic, applies to new sessions, "
                                  "and needs the configuration file too to survive a restart.",
                          "caveat": "The cache is allocated per writing session. Very large transactions spill "
                                    "whatever the size; splitting them helps more."}
    return _diagnostic(key, title, worst, headline, verdict, why, DIAGNOSTIC_RULES["binlog_cache"], value=pct,
                       unit="%", numbers=numbers, settings=settings, recommendation=recommendation, sources=sources)


def _diag_open_files(w):
    key, title = "open_files", "Open files"
    files = w.gauge("Open_files")
    innodb = w.gauge("Innodb_num_open_files")
    connected = w.gauge("Threads_connected")
    limit = w.var("open_files_limit")
    sources = ("Open_files", "Innodb_num_open_files", "Threads_connected", "open_files_limit")
    settings = [_setting(w, "open_files_limit"), _setting(w, "table_open_cache")]
    why = ("open_files_limit is the most file descriptors the server process can hold. Open_files counts files "
           "the server opened through its own file layer (MyISAM and Aria tables, binary and relay logs, some "
           "temporary files), not InnoDB's, which Innodb_num_open_files counts; each client connection also uses "
           "a socket. myflames adds the three, a lower bound: other descriptors are not visible through SQL.")
    if files is None or not limit:
        return _unknown(w, key, title, why, ("Open_files",), sources, settings,
                        text=None if files is None else "This server does not report open_files_limit.")
    accounted = files + (innodb or 0) + (connected or 0)
    pct = 100.0 * accounted / limit if limit else None
    status = _grade(pct, OPEN_FILES_PCT) if pct is not None else "unknown"
    numbers = [
        _number_item("accounted", "Descriptors accounted for", pct, "%", "%s of open_files_limit %s" % (
            _count_text(accounted), _count_text(limit))),
        _number_item("open_files", "Open_files", files, ""),
        _number_item("innodb_files", "Innodb_num_open_files", innodb, "") if innodb is not None else None,
        _number_item("connections", "Client connections (sockets)", connected, "") if connected is not None else None,
    ]
    headline = "%s of open_files_limit" % _fmt(pct, "%") if pct is not None else "Unknown"
    verdict = "At least %s file descriptors are in use: %s through the server's file layer, %s InnoDB files and %s " \
              "connection%s, %s of open_files_limit %s." % (
                  _count_text(accounted), _count_text(files), _count_text(innodb or 0), _count_text(connected or 0),
                  "" if connected == 1 else "s", _fmt(pct, "%"), _count_text(limit))
    recommendation = None
    if status in ("watch", "problem"):
        recommendation = {"text": "Raise open_files_limit in the configuration file; it needs a restart, and the "
                                  "operating system limit (LimitNOFILE for systemd) must allow it.",
                          "caveat": "When the limit is reached, opening a table or accepting a connection fails "
                                    "with \"Too many open files\". A smaller table_open_cache also lowers the need."}
    return _diagnostic(key, title, status, headline, verdict, why, DIAGNOSTIC_RULES["open_files"], value=pct,
                       unit="%", numbers=numbers, settings=settings, recommendation=recommendation, sources=sources)


_HANDLER_READS = ("Handler_read_first", "Handler_read_key", "Handler_read_last", "Handler_read_next",
                  "Handler_read_prev", "Handler_read_rnd", "Handler_read_rnd_next")


def _diag_full_scans(w):
    key, title = "full_scans", "Rows read by full scans"
    scan = w.delta("Handler_read_rnd_next")
    rate = w.rate(scan)
    reads = None
    if all(n in w.a and n in w.b for n in _HANDLER_READS):
        reads = w.delta(*_HANDLER_READS)
    why = ("Handler_read_rnd_next counts requests for the next row of a sequential table scan, so a high rate "
           "points at full table scans. It also counts scans of internal temporary tables (GROUP BY, DISTINCT, "
           "derived tables, UNION), and this tool's own SHOW GLOBAL STATUS reads about %s rows that way per "
           "window (measured), so it is an upper bound on rows from full scans of user tables. Full index scans "
           "count in Handler_read_first and Handler_read_next instead. Top statements shows which statements scan."
           % ("1,000" if w.engine == "mysql" else "600"))
    sources = ("Handler_read_rnd_next",) + tuple(n for n in _HANDLER_READS if n != "Handler_read_rnd_next")
    if scan is None:
        return _unknown(w, key, title, why, ("Handler_read_rnd_next",), sources, learn="full_scan", link=_TOP)
    share = 100.0 * scan / reads if reads else None
    numbers = [
        _number_item("rnd_next", "Rows read by sequential scans", rate, "/s", "%s/s (%s in the window)" % (
            _count_text(_round(rate, 2)), _count_text(scan))),
        _number_item("share", "Share of all rows read through handlers", share, "%", "%s of %s" % (
            _fmt(share, "%"), _count_text(reads))) if share is not None else None,
    ]
    headline = "%s rows/s by sequential scans" % _count_text(_round(rate, 2))
    verdict = "Storage engines returned %s rows per second to sequential scans" % _count_text(_round(rate, 2))
    verdict += (", %s of all rows read through handlers." % _fmt(share, "%")) if share is not None else "."
    return _diagnostic(key, title, "info", headline, verdict, why, None, value=rate, unit="/s", numbers=numbers,
                       sources=sources, learn="full_scan", link=_TOP)


def _diag_tmp_tables(w, metrics):
    key, title = "tmp_tables", "Temporary tables on disk"
    from .config_review import tmp_table_limit
    metric = next(m for m in metrics if m["key"] == "tmp_disk_ratio")
    limit, names = tmp_table_limit(w.engine, w.vars)
    settings = [_setting(w, n) for n in names]
    counting = ("MariaDB counts a table that starts in memory and moves to disk twice in Created_tmp_tables, so "
                "the share understates moves to disk." if w.engine == "mariadb" else
                "TempTable overflow into memory-mapped files (temptable_max_mmap) is not counted as disk.")
    why = ("Internal temporary tables (GROUP BY, DISTINCT, UNION, derived tables) start in memory and move to disk "
           "when they outgrow the limit. %s%s. %s" % (
               limit, "; on disk they are InnoDB tables" if w.engine == "mysql" else "", counting))
    tmp = metric["inputs"].get("Created_tmp_tables")
    disk = metric["inputs"].get("Created_tmp_disk_tables")
    if metric["status"] == "unknown":
        return _unknown(w, key, title, why, ("Created_tmp_tables", "Created_tmp_disk_tables"),
                        ("Created_tmp_tables", "Created_tmp_disk_tables") + tuple(names), settings,
                        learn="tmp", link=_TOP)
    numbers = [
        _number_item("created", "Internal temporary tables", w.rate(tmp), "/s", "%s/s (%s in the window)" % (
            _count_text(_round(w.rate(tmp), 2)), _count_text(tmp))),
        _number_item("disk", "Created on disk or moved there", w.rate(disk), "/s", "%s/s (%s in the window)" % (
            _count_text(_round(w.rate(disk), 2)), _count_text(disk))),
    ]
    if tmp:
        headline = "%s of %s on disk" % (_fmt(metric["value"], "%"), _count_text(tmp))
        verdict = "%s of %s internal temporary tables in the window were on disk (%s)." % (
            _count_text(disk), _count_text(tmp), _fmt(metric["value"], "%"))
    else:
        headline = "None in this window"
        verdict = "No internal temporary tables were created in the window."
    # MySQL 9.1+: which limit sent them to disk (see the module docstring).
    hits = {}
    for name in ("Count_hit_tmp_table_size", "TempTable_count_hit_max_ram"):
        if name in w.a and name in w.b:
            hits[name] = w.delta(name)
    if hits.get("Count_hit_tmp_table_size") is not None:
        numbers.append(_number_item("hit_tmp_table_size", "Hit tmp_table_size (per table)",
                                    hits["Count_hit_tmp_table_size"], "",
                                    "%s in the window" % _count_text(hits["Count_hit_tmp_table_size"])))
    if hits.get("TempTable_count_hit_max_ram") is not None:
        numbers.append(_number_item("hit_max_ram", "Hit temptable_max_ram (all TempTable memory)",
                                    hits["TempTable_count_hit_max_ram"], "",
                                    "%s in the window" % _count_text(hits["TempTable_count_hit_max_ram"])))
    if hits:
        why += (" On MySQL 9.1 and later, Count_hit_tmp_table_size counts tables that reached tmp_table_size (user "
                "MEMORY tables that fill up count too) and TempTable_count_hit_max_ram counts each time TempTable's "
                "total memory ran out.")
    if any(hits.values()):
        parts = []
        if hits.get("Count_hit_tmp_table_size"):
            parts.append("%s tables reached the per-table limit (tmp_table_size)" % _count_text(
                hits["Count_hit_tmp_table_size"]))
        if hits.get("TempTable_count_hit_max_ram"):
            parts.append("TempTable's total memory (temptable_max_ram) was hit %s times%s" % (
                _count_text(hits["TempTable_count_hit_max_ram"]),
                " (with temptable_max_mmap above 0 the overflow goes to memory-mapped files first)"
                if (w.var("temptable_max_mmap") or 0) > 0 else ""))
        verdict += " In the window, " + " and ".join(parts) + "."
    recommendation = None
    temptable = w.engine == "mysql" and "temptable_max_ram" in names
    global_limit = temptable and ("tmp_table_size" not in names or (
        hits.get("TempTable_count_hit_max_ram") and not hits.get("Count_hit_tmp_table_size")))
    if temptable:
        tmp_caveat = ("The total memory limit is global: all TempTable tables share temptable_max_ram, so raising it increases "
                      "the server-wide memory budget. ")
        if "tmp_table_size" in names:
            tmp_caveat += "A larger tmp_table_size lets one table take more of that shared memory. "
        tmp_caveat += ("With TempTable, BLOB and TEXT columns stay in memory; big_tables=ON sends tables to "
                       "disk directly.")
    else:
        tmp_caveat = ("Each concurrent temporary table can use up to the limit, so a large global value risks "
                      "memory pressure. Tables with BLOB or TEXT columns go to disk regardless.")
    if metric["status"] in ("watch", "problem"):
        limit_text = ("Then raise the limit for the sessions that need it (SET SESSION tmp_table_size = <bytes>%s)."
                      % ("; max_heap_table_size too" if w.engine == "mariadb" or "max_heap_table_size" in names
                         else ""))
        if global_limit:
            limit_text = ("Here TempTable's total memory ran out, so temptable_max_ram (global) is the limit to "
                          "look at. " if hits.get("TempTable_count_hit_max_ram") else
                          "This TempTable version has only a global memory limit: temptable_max_ram. ")
            limit_text += "If memory headroom permits, use SET PERSIST temptable_max_ram = <bytes>;"
        recommendation = {"text": "Find the statements with the temporary-table-on-disk flag in Top statements; an "
                                  "index that avoids the temporary table helps more than a bigger limit. "
                                  + limit_text,
                          "caveat": tmp_caveat}
        if global_limit:
            recommendation["sql"] = "SET PERSIST temptable_max_ram = <bytes>;"
    return _diagnostic(key, title, metric["status"], headline, verdict, why, metric["rule"] or RULES["tmp_disk"],
                       value=metric["value"], unit="%", numbers=numbers, settings=settings,
                       recommendation=recommendation, sources=("Created_tmp_tables", "Created_tmp_disk_tables")
                       + tuple(hits) + tuple(names), learn="tmp", link=_TOP)


def _diag_sorts(w, metrics):
    key, title = "sorts", "Sorts that spill to disk"
    metric = next(m for m in metrics if m["key"] == "sort_merge_passes")
    passes = metric["delta"]
    sorts = w.delta("Sort_range", "Sort_scan")
    sort_rows = w.delta("Sort_rows")
    settings = [_setting(w, "sort_buffer_size")]
    why = ("A sort that cannot use an index (filesort) collects rows in sort_buffer_size. When they do not fit, it "
           "writes sorted runs to a temporary file and merges them, and each merge pass is counted in "
           "Sort_merge_passes. Sort_range and Sort_scan count the sorts themselves. sort_buffer_size is a limit "
           "for each sort in every session, so raise it for the sessions that need it rather than globally.")
    sources = ("Sort_merge_passes", "Sort_range", "Sort_scan", "Sort_rows", "sort_buffer_size")
    if metric["status"] == "unknown":
        return _unknown(w, key, title, why, ("Sort_merge_passes",), sources, settings, learn="filesort", link=_TOP)
    numbers = [
        _number_item("passes", "Merge passes", metric["rate"], "/s", "%s/s (%s in the window)" % (
            _count_text(metric["rate"]), _count_text(passes))),
        _number_item("sorts", "Sorts", w.rate(sorts), "/s", "%s/s (%s in the window)" % (
            _count_text(_round(w.rate(sorts), 2)), _count_text(sorts))) if sorts is not None else None,
        _number_item("rows", "Rows sorted", w.rate(sort_rows), "/s", "%s/s" % _count_text(
            _round(w.rate(sort_rows), 2))) if sort_rows is not None else None,
    ]
    headline = "%s merge passes for %s sorts" % (_count_text(passes), _count_text(sorts)) if sorts is not None \
        else "%s merge passes" % _count_text(passes)
    verdict = "%s merge passes in the window%s." % (
        _count_text(passes), " for %s sorts" % _count_text(sorts) if sorts is not None else "")
    if passes and metric["status"] == "ok":
        verdict += " Too few to judge."
    elif not passes:
        verdict += " Sorts fit in sort_buffer_size."
    recommendation = None
    if metric["status"] in ("watch", "problem"):
        recommendation = {"text": "Find the sorting statements in Top statements first: an index in ORDER BY or "
                                  "GROUP BY order removes the sort. For the rest, raise sort_buffer_size per "
                                  "session (SET SESSION sort_buffer_size = <bytes>).",
                          "caveat": "A large global sort_buffer_size lets every concurrent sort use up to that "
                                    "much memory."}
    return _diagnostic(key, title, metric["status"], headline, verdict, why, metric["rule"] or RULES["in_moderation"],
                       value=metric["rate"], unit="/s", numbers=numbers, settings=settings,
                       recommendation=recommendation, sources=sources, learn="filesort", link=_TOP)


def _diagnostics(w, metrics):
    return [_diag_buffer_pool(w, metrics), _diag_redo(w), _diag_table_cache(w), _diag_table_definitions(w),
            _diag_thread_cache(w), _diag_binlog_cache(w), _diag_open_files(w), _diag_full_scans(w),
            _diag_tmp_tables(w, metrics), _diag_sorts(w, metrics)]


def compute_health(first, second, variables, interval=DEFAULT_INTERVAL):
    """The ``health-1.0`` report from two samples and the variables. Pure.

    *first* and *second* are :func:`take_sample` results ({status, t,
    taken_at}); *variables* is :func:`read_variables`'s dict.
    """
    w = _Window(first, second, variables, interval)
    metrics = (_throughput(w) + _connections(w) + _buffer_pool(w) + _row_locks(w)
               + _temp_sorts(w) + _table_cache(w) + _redo_io(w))
    diagnostics = _diagnostics(w, metrics)
    notes = []
    if w.restarted:
        notes.append("The server restarted between the two samples (Uptime went from %s to %s seconds), "
                     "so counters started over and rates are not available. Refresh to sample again."
                     % (_count_text(_number(w.a.get("Uptime"))), _count_text(w.uptime)))
    if w.reset:
        notes.append("These counters went down between the samples: %s. The server restarted, a status "
                     "reset ran (FLUSH STATUS on MySQL and on MariaDB 11.4 or older, FLUSH GLOBAL STATUS on "
                     "MariaDB 11.5 and later), or a counter wrapped on a Windows build, so they have no "
                     "rate this time." % ", ".join(w.reset))
    if w.missing:
        notes.append("This server does not report: %s. Those metrics and diagnostics show as unknown."
                     % ", ".join(w.missing))
    if w.uptime is not None and w.uptime < WARMUP_SECONDS and not w.restarted:
        notes.append("The server started %s ago. Its buffer pool may still be warming up, so disk reads "
                     "are expected to be higher than usual." % _duration_text(w.uptime))
    if w.engine == "mysql" and (w.var("temptable_max_mmap") or 0) > 0 \
            and (w.vars.get("temptable_use_mmap") or "ON").upper() not in ("OFF", "0"):
        notes.append("temptable_max_mmap is %s: TempTable overflow into memory-mapped files is not counted "
                     "in Created_tmp_disk_tables." % _bytes_text(w.var("temptable_max_mmap")))
    notes.append("Statuses are rules of thumb, not verdicts; each metric and diagnostic says which rule it "
                 "uses. Each window includes this tool's own second sample: about 3 statements, with the "
                 "temporary table and scans they need, and its connection.")
    # The counts stay per metric (health-1.0); "diagnostics" counts the
    # diagnostics, and the overall status is the worst of both.
    summary = {"status": "ok", "problem": 0, "watch": 0, "ok": 0, "info": 0, "unknown": 0}
    for metric in metrics:
        summary[metric["status"]] += 1
    summary["diagnostics"] = dict((status, 0) for status in STATUSES)
    for diagnostic in diagnostics:
        summary["diagnostics"][diagnostic["status"]] += 1
    if summary["problem"] or summary["diagnostics"]["problem"]:
        summary["status"] = "problem"
    elif summary["watch"] or summary["diagnostics"]["watch"]:
        summary["status"] = "watch"
    page_size = w.var("innodb_page_size")
    return {
        "schema_version": SCHEMA_VERSION,
        "engine": w.engine,
        "server_version": w.version or None,
        "interval": interval,
        "elapsed_s": _round(w.elapsed, 3),
        "started_at": first.get("taken_at"),
        "taken_at": second.get("taken_at"),
        "uptime_s": w.uptime,
        "restarted": w.restarted,
        "server": {
            "engine": w.engine, "version": w.version or None, "uptime_s": w.uptime,
            "max_connections": w.var("max_connections"), "table_open_cache": w.var("table_open_cache"),
            "buffer_pool_bytes": w.var("innodb_buffer_pool_size"), "page_size": page_size,
            "redo_capacity_bytes": _redo_capacity(w)[0],
        },
        "summary": summary,
        "groups": [{"key": key, "label": label} for key, label in GROUPS],
        "diagnostics": diagnostics,
        "metrics": metrics,
        "notes": notes,
    }


def at_or_worse(report, level):
    """Diagnostics, then metrics, whose status is *level* (watch or problem)
    or worse. A diagnostic has a ``title`` and a ``verdict``; a metric has a
    ``label`` and a ``why``."""
    floor = SEVERITY[level]
    items = list(report.get("diagnostics") or []) + list(report["metrics"])
    return [m for m in items if SEVERITY.get(m["status"], -1) >= floor]

# ---- text output for the CLI -------------------------------------------------------

_MARK = {"ok": "ok", "watch": "WATCH", "problem": "PROBLEM", "info": "-", "unknown": "?"}


def format_value(metric):
    """The headline value with its unit, as the CLI prints it."""
    value, unit = metric["value"], metric["unit"]
    if value is None:
        return "—"
    if unit == "%":
        return "%.1f%%" % value
    if unit == "B/s":
        return _bytes_text(value) + "/s"
    if unit == "/s":
        return "%s/s" % _count_text(value)
    if unit in ("pages/s", "ms/s"):
        return "%s %s" % (_count_text(value), unit)
    if unit == "ms":
        return "%s ms" % _count_text(value)
    return _count_text(value)


def format_text(report):
    """Compact grouped table with status markers."""
    head = "%s %s" % ("MariaDB" if report["engine"] == "mariadb" else "MySQL", report["server_version"] or "?")
    if report["uptime_s"] is not None:
        head += ", up %s" % _duration_text(report["uptime_s"])
    head += ", sampled over %s s" % (_count_text(report["elapsed_s"]) if report["elapsed_s"] else "?")
    summary = report["summary"]
    status = "Status: %s (%d problem, %d watch, %d ok" % (
        summary["status"].upper() if summary["status"] != "ok" else "ok",
        summary["problem"], summary["watch"], summary["ok"])
    counts = summary.get("diagnostics")
    if counts:
        status += "; diagnostics: %d problem, %d watch, %d ok" % (counts["problem"], counts["watch"], counts["ok"])
    lines = [head, status + ")", ""]
    lines += _diagnostics_text(report.get("diagnostics") or [])
    rows = [(m["group"], _MARK[m["status"]], m["label"], format_value(m), m["detail"] or "")
            for m in report["metrics"]]
    widths = [max(len(r[i]) for r in rows) for i in range(1, 4)]
    labels = dict(GROUPS)
    group = None
    for g, mark, label, value, detail in rows:
        if g != group:
            if group is not None:
                lines.append("")
            lines.append(labels.get(g, g).upper())
            group = g
        lines.append(("  %s  %s  %s  %s" % (mark.ljust(widths[0]), label.ljust(widths[1]),
                                            value.rjust(widths[2]), detail)).rstrip())
    flagged = [m for m in report["metrics"] if m["status"] in ("watch", "problem")]
    if flagged:
        lines.append("")
        lines.append("WHY")
        for m in flagged:
            lines.append("  %s %s: %s" % (_MARK[m["status"]], m["label"], m["why"]))
    if any(item["key"] == "select_full_join" and item["status"] in ("watch", "problem")
           for item in report["metrics"] + (report.get("diagnostics") or [])):
        lines.append("Statements: myflames top --order full_join")
    return "\n".join(lines) + "\n"


def _diagnostics_text(diagnostics):
    """The DIAGNOSTICS block: one line per diagnostic (mark, title, the key
    number), its verdict, and for watch and problem the suggestion and caveat."""
    if not diagnostics:
        return []
    import textwrap
    width = max(len(_MARK[d["status"]]) for d in diagnostics)
    title = max(len(d["title"]) for d in diagnostics)
    indent = " " * (4 + width)
    lines = ["DIAGNOSTICS"]
    for d in diagnostics:
        lines.append(("  %s  %s  %s" % (_MARK[d["status"]].ljust(width), d["title"].ljust(title),
                                        d["headline"])).rstrip())
        wrap = textwrap.TextWrapper(width=100, initial_indent=indent, subsequent_indent=indent)
        lines += wrap.wrap(d["verdict"])
        if d["status"] in ("watch", "problem"):
            lines += wrap.wrap("Rule of thumb: %s." % d["rule"])
            if d.get("recommendation"):
                lines += wrap.wrap("Suggestion: " + d["recommendation"]["text"])
                lines += wrap.wrap("Caveat: " + d["recommendation"]["caveat"])
    lines.append("")
    return lines
