"""Change server variables: SET GLOBAL, SET PERSIST, SET PERSIST_ONLY, RESET PERSIST and SET SESSION.

The write side of the Parameters view and ``myflames config set`` /
``myflames config reset-persist``. Every change follows the write contract
in the db-operations skill (docs/DEVELOPER_REFERENCE.md, "Write safety and
audit"), reusing :mod:`myflames.dbops`:

* :func:`read_variable` reads one variable (``variable-1.0``): its global and
  session values, scope, whether it can change at runtime, type, range or
  values, engine default, where the value came from, and (MySQL) the value in
  ``mysqld-auto.cnf``. Reads only.
* :func:`plan_change` validates a requested value for a mode (``global``,
  ``persist``, ``persist_only``, ``session``, ``reset_persist``) and builds the
  exact statement, the privileges it needs, its effect, caveats, the undo
  statements and the ``expect`` identity a confirmation captures
  (``variable-plan-1.0``). Reads only; :func:`build_plan` is the pure part.
* :func:`apply_change` re-reads the variable, refuses when it changed since
  the plan or the statement would differ, goes through :func:`dbops.gate`,
  writes the audit record before sending, sends the one statement (tagged,
  with :data:`dbops.WRITE_CLIENT_FLAGS`), maps server errors to statuses and
  re-reads the result (``variable-change-1.0``).

Confirmation snapshots include engine/version, server identity, source
metadata and a persisted state (``present``, ``absent``, ``unreadable`` or
``unsupported``). A present value can be SQL NULL or empty text; neither
means absent. Persistence changes require a successful catalog read.
The UI additionally binds ``expect.connection_identity`` with dbops. Read
times are ISO-8601 UTC. JSON null requests SQL NULL only for a known nullable
variable; the string "NULL" remains text, while DEFAULT is a separate
keyword. ``applied_unverified`` means a statement was sent but its complete
effect was not verified: CLI exit 1, audit status unknown, never automatic
retry. PERSIST verifies both running and persisted values.

Facts verified against server source (M/ = mysql-server 9.7 and the 8.4.10
tag, Ma/ = mariadb-server 13.0 and the 10.11 and 11.4 tags):

* MySQL has no catalog column for read-only (dynamic) variables:
  ``performance_schema.variables_metadata`` (9.0+) has VARIABLE_SCOPE
  (``GLOBAL`` = global only, ``SESSION`` = global and session,
  ``SESSION_ONLY``), DATA_TYPE (Integer, Numeric, String, Enumeration,
  Boolean, Set), MIN_VALUE, MAX_VALUE and DOCUMENTATION
  (M/storage/perfschema/table_variables_metadata.cc:56-63, 101-136). So the
  read-only flag, the scope on 8.4 and the type come from
  :data:`MYSQL_VARIABLES`, a map read from M/sql/sys_vars.cc (READ_ONLY,
  NON_PERSIST, PERSIST_AS_READONLY, GLOBAL_VAR / SESSION_VAR / SESSION_ONLY)
  and M/storage/innobase/handler/ha_innodb.cc (PLUGIN_VAR_READONLY,
  PLUGIN_VAR_NOPERSIST, PLUGIN_VAR_PERSIST_AS_READ_ONLY), identical at the
  8.4.10 tag and 9.7 for every entry. The generated
  :mod:`mysql_sysvars` catalog supplements this authoritative hand map;
  ``scripts/gen_mysql_sysvars.py`` extracts both tags, including plugin
  declarations and constructor flags. Conflicting facts stay unknown.
  A MySQL variable outside both maps has ``dynamic: None``: the UI offers
  no change, the CLI tries and the server decides (1238 when read-only).
* ``performance_schema.global_variables`` lists global and both-scope
  variables; ``session_variables`` (like SHOW SESSION VARIABLES) also lists
  global-only ones (non-strict scope, M/storage/perfschema/pfs_variable.cc:
  188-208, M/sql/set_var.cc:1032-1070). So a variable in SHOW SESSION but not
  in SHOW GLOBAL has only a session value.
* ``variables_info`` MIN_VALUE / MAX_VALUE are printed for every variable
  (M/storage/perfschema/pfs_variable.cc:779-784); they are used only for
  integer variables. Double ranges are in the map.
* MariaDB: ``information_schema.SYSTEM_VARIABLES`` has SESSION_VALUE,
  GLOBAL_VALUE, GLOBAL_VALUE_ORIGIN, DEFAULT_VALUE, VARIABLE_SCOPE
  (GLOBAL, SESSION, SESSION ONLY), VARIABLE_TYPE, NUMERIC_MIN_VALUE,
  NUMERIC_MAX_VALUE, NUMERIC_BLOCK_SIZE, ENUM_VALUE_LIST and READ_ONLY
  (YES/NO) (Ma/sql/set_var.cc:1128-1290). Numbers are rounded down to the
  block size (Ma/mysys/my_getopt.c:1197-1230, the SET path too:
  Ma/sql/sys_vars.inl:227), or refused with 1231 under STRICT_ALL_TABLES
  (Ma/sql/set_var.cc:475-490).
* SET PERSIST_ONLY skips the value check (``var->check`` runs only for other
  types; M/sql/set_var.cc:1742-1770; booleans are still checked for ON/OFF,
  M/sql/persisted_variable.cc:445-457) and keeps the running value
  (M/sql/set_var.cc:1870-1890; 8.4.10 around :1858), so myflames validates the
  value itself.
  A read-only variable fails SET GLOBAL / SET PERSIST with 1238
  (M/sql/set_var.cc:1660-1667); 1228 / 1229 are the wrong-scope errors
  (:1676-1681); NON_PERSIST variables fail PERSIST with 1238 "non persistent"
  (:1696-1702). SET PERSIST_ONLY needs SYSTEM_VARIABLES_ADMIN and
  PERSIST_RO_VARIABLES_ADMIN (:1688-1692, check_priv :207-232).
* A PERSIST_AS_READONLY variable (innodb_buffer_pool_size,
  innodb_redo_log_capacity, log_error_verbosity, server_id, ...) is stored as
  a read-only entry even by SET PERSIST (M/sql/persisted_variable.cc:523-534),
  so RESET PERSIST of it needs both privileges too (:2260-2275). RESET
  PERSIST of a name not in the file is error 3615 (:2290-2300) and leaves the
  running value alone.
* innodb_buffer_pool_size: MySQL rounds a new size up to a multiple of
  innodb_buffer_pool_chunk_size x innodb_buffer_pool_instances
  (M/storage/innobase/include/buf0buf.ic:1109-1118), resizes in the
  background (Innodb_buffer_pool_resize_status), and with more than one
  instance ignores sizes under 1 GiB with only a warning
  (M/storage/innobase/handler/ha_innodb.cc:20728-20748,
  M/storage/innobase/include/srv0start.h:67).
* Restricted SET SESSION: MySQL variables whose check calls
  check_session_admin* (M/sql/sys_vars.cc:357-475 and callers; SUPER is not
  accepted by the ``_no_super`` ones, REPLICATION_APPLIER also is by the
  ``_or_replication_applier`` ones); MariaDB per-variable privileges from
  Ma/sql/privilege.h:336-650, the ``Sys_var_on_access*`` definitions in
  Ma/sql/sys_vars.cc, and ``check_has_super`` callers (Ma/sql/sys_vars.cc:618-629,
  Ma/sql/wsrep_var.cc:171).
"""
import datetime
import math
import re
from decimal import Decimal, DecimalException, localcontext

from . import dbops, mysql_sysvars
from .connector import ConnectorError, MySQLConnection
from .perf_common import _bytes_text, _number, _version_tuple, literal, reason
from .server_info import detect_flavor

#: Bound at import, so a test that replaces the connection class keeps it.
_unescape = MySQLConnection._unescape_mysql

READ_SCHEMA = "variable-1.0"
PLAN_SCHEMA = "variable-plan-1.0"
CHANGE_SCHEMA = "variable-change-1.0"
TAG = "/*myflames:variable*/ "
MODES = ("global", "persist", "persist_only", "session", "reset_persist")
#: The audit trail's operation name per mode (dbops.PRIVILEGE_OPERATIONS style).
OPERATIONS = {"global": "set_global", "persist": "set_persist", "persist_only": "set_persist_only",
              "session": "set_session", "reset_persist": "reset_persist"}
MODE_LABELS = {"global": "Now only", "persist": "Now and after restart", "persist_only": "After restart only",
               "session": "This session only", "reset_persist": "Remove the persisted value"}
MAX_VALUE_LENGTH = 4096
_NAME = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?$")
_INT = re.compile(r"^[+-]?\d+$")
_DECIMAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGTPE])(?:I?B)?$", re.I)
_UNITS = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50, "E": 1 << 60}
#: Text that needs no escaping inside '...' under either backslash reading.
_PLAIN_TEXT = re.compile(r"^[A-Za-z0-9 _.,:/=+@%*()\-]*$")
_BOOL_TRUE = ("ON", "1", "TRUE", "YES")
_BOOL_FALSE = ("OFF", "0", "FALSE", "NO")

# ---- source-verified facts ---------------------------------------------------------------

# (scope, type, flags). Flags: ro read-only at runtime; np NON_PERSIST; pro
# PERSIST_AS_READONLY (stored as a read-only entry by SET PERSIST); sro session
# value read-only (SET SESSION fails with 1621). Restricted SET SESSION is
# MYSQL_RESTRICTED_SESSION below.
_G, _B, _S = "global", "both", "session"
#: MySQL 8.4 and 9.x (identical for every entry; absent names are simply not on the server).
MYSQL_VARIABLES = {
    # memory
    "innodb_buffer_pool_size": (_G, "bytes", "pro"),
    "innodb_buffer_pool_instances": (_G, "integer", "ro"),
    "innodb_buffer_pool_chunk_size": (_G, "bytes", "ro"),
    "innodb_dedicated_server": (_G, "boolean", "ro np"),
    "innodb_log_buffer_size": (_G, "bytes", ""),
    "innodb_sort_buffer_size": (_G, "bytes", "ro"),
    "innodb_numa_interleave": (_G, "boolean", "ro"),
    "innodb_online_alter_log_max_size": (_G, "bytes", ""),
    "tmp_table_size": (_B, "bytes", ""),
    "max_heap_table_size": (_B, "bytes", ""),
    "internal_tmp_mem_storage_engine": (_B, "enum", ""),
    "temptable_max_ram": (_G, "bytes", ""),
    "temptable_max_mmap": (_G, "bytes", ""),
    "temptable_use_mmap": (_G, "boolean", ""),
    "sort_buffer_size": (_B, "bytes", ""),
    "join_buffer_size": (_B, "bytes", ""),
    "read_buffer_size": (_B, "bytes", ""),
    "read_rnd_buffer_size": (_B, "bytes", ""),
    "max_allowed_packet": (_B, "bytes", "sro"),
    "group_concat_max_len": (_B, "integer", ""),
    "binlog_cache_size": (_G, "bytes", ""),
    "max_binlog_size": (_G, "bytes", ""),
    # durability and redo
    "innodb_redo_log_capacity": (_G, "bytes", "pro"),
    "innodb_log_file_size": (_G, "bytes", "ro"),
    "innodb_log_files_in_group": (_G, "integer", "ro"),
    "innodb_flush_log_at_trx_commit": (_G, "integer", ""),
    "sync_binlog": (_G, "integer", ""),
    "log_bin": (_G, "boolean", "ro np"),
    "innodb_flush_method": (_G, "enum", "ro"),
    "innodb_doublewrite": (_G, "enum", "np"),
    "innodb_file_per_table": (_G, "boolean", ""),
    "binlog_expire_logs_seconds": (_G, "integer", ""),
    # connections and caches
    "max_connections": (_G, "integer", ""),
    "max_connect_errors": (_G, "integer", ""),
    "max_user_connections": (_B, "integer", "sro"),
    "table_open_cache": (_G, "integer", ""),
    "table_open_cache_instances": (_G, "integer", "ro"),
    "table_definition_cache": (_G, "integer", ""),
    "thread_cache_size": (_G, "integer", ""),
    "open_files_limit": (_G, "integer", "ro"),
    "back_log": (_G, "integer", "ro"),
    "skip_name_resolve": (_G, "boolean", "ro"),
    "thread_handling": (_G, "enum", "ro"),
    "wait_timeout": (_B, "integer", ""),
    "interactive_timeout": (_B, "integer", ""),
    "net_read_timeout": (_B, "integer", ""),
    "net_write_timeout": (_B, "integer", ""),
    "lock_wait_timeout": (_B, "integer", ""),
    "innodb_lock_wait_timeout": (_B, "integer", ""),
    "max_execution_time": (_B, "integer", ""),
    "transaction_isolation": (_B, "enum", ""),
    # InnoDB I/O and flushing
    "innodb_io_capacity": (_G, "integer", ""),
    "innodb_io_capacity_max": (_G, "integer", ""),
    "innodb_lru_scan_depth": (_G, "integer", ""),
    "innodb_flush_neighbors": (_G, "integer", ""),
    "innodb_max_dirty_pages_pct": (_G, "double", ""),
    "innodb_max_dirty_pages_pct_lwm": (_G, "double", ""),
    "innodb_adaptive_flushing": (_G, "boolean", ""),
    "innodb_adaptive_hash_index": (_G, "boolean", ""),
    "innodb_page_cleaners": (_G, "integer", "ro"),
    "innodb_read_io_threads": (_G, "integer", "ro"),
    "innodb_write_io_threads": (_G, "integer", "ro"),
    "innodb_purge_threads": (_G, "integer", "ro"),
    "innodb_thread_concurrency": (_G, "integer", ""),
    "innodb_deadlock_detect": (_G, "boolean", ""),
    "innodb_print_all_deadlocks": (_G, "boolean", ""),
    "innodb_stats_persistent": (_G, "boolean", ""),
    "innodb_rollback_on_timeout": (_G, "boolean", "ro"),
    "innodb_page_size": (_G, "integer", "ro np"),
    # observability
    "performance_schema": (_G, "boolean", "ro"),
    "slow_query_log": (_G, "boolean", ""),
    "long_query_time": (_B, "double", ""),
    "log_output": (_G, "set", ""),
    "log_slow_admin_statements": (_G, "boolean", ""),
    "log_queries_not_using_indexes": (_G, "boolean", ""),
    "min_examined_row_limit": (_B, "integer", ""),
    "general_log": (_G, "boolean", ""),
    "log_error_verbosity": (_G, "integer", "pro"),
    "read_only": (_G, "boolean", ""),
    "super_read_only": (_G, "boolean", ""),
    "persisted_globals_load": (_G, "boolean", "ro np"),
    # session only
    "sql_log_bin": (_S, "boolean", ""),
    "optimizer_switch": (_B, "flagset", ""),
    "character_set_results": (_B, "string", ""),
}
#: Enum and set values (M/sql/sys_vars.cc, M/sql/handler.cc tx_isolation_names,
#: M/storage/innobase/handler/ha_innodb.cc innodb_flush_method_names on Unix).
MYSQL_VALUES = {
    "internal_tmp_mem_storage_engine": ("MEMORY", "TempTable"),
    "transaction_isolation": ("READ-UNCOMMITTED", "READ-COMMITTED", "REPEATABLE-READ", "SERIALIZABLE"),
    "thread_handling": ("one-thread-per-connection", "no-threads", "loaded-dynamically"),
    "log_output": ("NONE", "FILE", "TABLE"),
    "innodb_flush_method": ("fsync", "O_DSYNC", "littlesync", "nosync", "O_DIRECT", "O_DIRECT_NO_FSYNC"),
    "innodb_doublewrite": ("OFF", "ON", "DETECT_ONLY", "DETECT_AND_RECOVER", "FALSE", "TRUE"),
}
#: Double ranges (variables_info prints doubles' limits as raw bits): LONG_TIMEOUT
#: is 31536000 (M/sql/sql_const.h:160).
MYSQL_DOUBLE_RANGES = {"long_query_time": (0.0, 31536000.0), "innodb_max_dirty_pages_pct": (0.0, 99.999),
                       "innodb_max_dirty_pages_pct_lwm": (0.0, 99.999)}
#: MySQL SET SESSION of these needs SESSION_VARIABLES_ADMIN, SYSTEM_VARIABLES_ADMIN
#: or SUPER (check_session_admin and its callers, M/sql/sys_vars.cc).
MYSQL_RESTRICTED_SESSION = frozenset((
    "binlog_direct_non_transactional_updates", "binlog_format", "binlog_row_image", "binlog_row_value_options",
    "binlog_rows_query_log_events", "binlog_transaction_compression", "binlog_transaction_compression_level_zstd",
    "bulk_insert_buffer_size", "character_set_database", "character_set_filesystem", "connection_memory_chunk_size",
    "connection_memory_limit", "debug", "debug_sync", "global_connection_memory_tracking", "gtid_next",
    "histogram_generation_max_mem_size", "immediate_server_version", "internal_tmp_mem_storage_engine",
    "low_priority_updates", "max_delayed_threads", "max_error_count", "max_insert_delayed_threads",
    "min_examined_row_limit", "original_commit_timestamp", "original_server_version", "preload_buffer_size",
    "pseudo_replica_mode", "pseudo_thread_id", "select_into_buffer_size", "select_into_disk_sync_delay",
    "sql_generate_invisible_primary_key", "sql_log_bin", "sql_log_off", "sql_require_primary_key",
    "xa_detach_on_prepare"))
#: The ones whose check (check_session_admin_no_super) does not accept SUPER.
MYSQL_RESTRICTED_NO_SUPER = frozenset((
    "internal_tmp_mem_storage_engine", "low_priority_updates", "max_delayed_threads", "max_error_count",
    "max_insert_delayed_threads", "min_examined_row_limit", "preload_buffer_size", "select_into_buffer_size",
    "select_into_disk_sync_delay"))
#: The ones whose check also accepts REPLICATION_APPLIER
#: (check_session_admin_or_replication_applier, M/sql/sys_vars.cc:357-375).
MYSQL_RESTRICTED_APPLIER = frozenset((
    "gtid_next", "immediate_server_version", "original_commit_timestamp", "original_server_version",
    "pseudo_replica_mode"))
#: MariaDB SET SESSION restrictions (Ma/sql/privilege.h, Ma/sql/sys_vars.cc check_has_super).
MARIADB_RESTRICTED_SESSION = {
    "sql_log_bin": "BINLOG ADMIN", "binlog_format": "BINLOG ADMIN", "binlog_row_image": "BINLOG ADMIN",
    "binlog_annotate_row_events": "BINLOG ADMIN", "binlog_direct_non_transactional_updates": "BINLOG ADMIN",
    "pseudo_thread_id": "BINLOG REPLAY", "gtid_seq_no": "BINLOG REPLAY", "server_id": "BINLOG REPLAY",
    "gtid_domain_id": "BINLOG REPLAY", "debug": "SUPER", "debug_dbug": "SUPER", "enforce_storage_engine": "SUPER",
    "debug_sync": "SUPER", "sql_log_off": "SUPER", "log_disabled_statements": "SUPER",
    "create_tmp_table_binlog_formats": "BINLOG ADMIN", "wsrep_on": "SUPER",
}
#: MariaDB SET GLOBAL of these needs this privilege instead of SUPER
#: (PRIV_SET_SYSTEM_GLOBAL_VAR_* and PRIV_SET_SYSTEM_VAR_*, Ma/sql/privilege.h:336-650).
MARIADB_GLOBAL_PRIVILEGE = {}
for _privilege, _names in (
        ("BINLOG ADMIN", "binlog_format binlog_direct_non_transactional_updates binlog_annotate_row_events "
                         "binlog_row_image sql_log_bin binlog_cache_size binlog_file_cache_size "
                         "binlog_stmt_cache_size binlog_commit_wait_count binlog_commit_wait_usec "
                         "binlog_row_metadata binlog_gtid_index binlog_gtid_index_page_size "
                         "binlog_gtid_index_span_min expire_logs_days log_bin_compress log_bin_compress_min_len "
                         "log_bin_trust_function_creators max_binlog_cache_size max_binlog_stmt_cache_size "
                         "max_binlog_size sync_binlog binlog_expire_logs_seconds max_binlog_total_size "
                         "binlog_space_limit slave_connections_needed_for_purge create_tmp_table_binlog_formats "
                         "binlog_legacy_event_pos binlog_do_db binlog_ignore_db"),
        ("READ_ONLY ADMIN", "read_only"),
        ("CONNECTION ADMIN", "connect_timeout disconnect_on_expired_password extra_max_connections init_connect "
                             "max_connections max_connect_errors max_password_errors proxy_protocol_networks "
                             "slow_launch_time thread_pool_min_threads thread_pool_mode thread_pool_priority "
                             "thread_pool_idle_timeout thread_pool_oversubscribe thread_pool_size "
                             "thread_pool_stall_limit thread_pool_max_threads thread_pool_prio_kickup_timer "
                             "thread_pool_exact_stats"),
        ("REPLICATION MASTER ADMIN", "rpl_semi_sync_master_enabled rpl_semi_sync_master_timeout "
                                     "rpl_semi_sync_master_wait_no_slave rpl_semi_sync_master_trace_level "
                                     "rpl_semi_sync_master_wait_point master_verify_checksum gtid_binlog_state "
                                     "server_id gtid_domain_id"),
        ("REPLICATION SLAVE ADMIN", "replicate_events_marked_for_skip replicate_rewrite_db replicate_do_db "
                                    "replicate_do_table replicate_ignore_db replicate_ignore_table "
                                    "replicate_wild_do_table replicate_wild_ignore_table read_binlog_speed_limit "
                                    "slave_compressed_protocol slave_ddl_exec_mode slave_domain_parallel_threads "
                                    "slave_exec_mode slave_max_allowed_packet slave_max_statement_time "
                                    "slave_abort_blocking_timeout slave_net_timeout slave_parallel_max_queued "
                                    "slave_parallel_mode slave_parallel_threads slave_parallel_workers "
                                    "slave_run_triggers_for_rbr slave_sql_verify_checksum "
                                    "slave_transaction_retry_interval slave_type_conversions init_slave "
                                    "rpl_semi_sync_slave_enabled rpl_semi_sync_slave_trace_level "
                                    "rpl_semi_sync_slave_delay_master rpl_semi_sync_slave_kill_conn_timeout "
                                    "relay_log_purge relay_log_recovery sync_master_info sync_relay_log "
                                    "sync_relay_log_info gtid_cleanup_batch_size gtid_ignore_duplicates "
                                    "gtid_pos_auto_engines gtid_slave_pos gtid_strict_mode")):
    for _name in _names.split():
        MARIADB_GLOBAL_PRIVILEGE[_name] = _privilege
#: Their constant is SUPER_ACL | BINLOG_ADMIN_ACL: either one is enough on every version.
MARIADB_GLOBAL_OR_SUPER = frozenset(("binlog_legacy_event_pos", "binlog_do_db", "binlog_ignore_db"))
#: MariaDB facts for the Parameters view's rows, so the inspector can label
#: Change before anything is read (Ma/sql/sys_vars.cc and ha_innodb.cc at the
#: 10.11, 11.4 and 13.0 sources; the catalog decides when the sheet opens).
MARIADB_READ_ONLY = frozenset(("innodb_buffer_pool_size_max", "innodb_buffer_pool_chunk_size", "innodb_flush_method",
                               "open_files_limit", "thread_handling", "performance_schema", "log_bin"))
MARIADB_DYNAMIC = frozenset((
    "innodb_buffer_pool_size", "innodb_log_file_size", "innodb_flush_log_at_trx_commit", "innodb_data_file_buffering",
    "innodb_log_file_buffering", "innodb_io_capacity", "innodb_io_capacity_max", "max_connections", "table_open_cache",
    "tmp_table_size", "tmp_memory_table_size", "max_heap_table_size", "tmp_disk_table_size", "sort_buffer_size",
    "join_buffer_size", "slow_query_log", "log_slow_query", "long_query_time", "log_slow_query_time", "log_output",
    "thread_pool_size", "sync_binlog", "innodb_lock_wait_timeout", "wait_timeout"))
#: Integer variables that hold a size in bytes (K, M, G suffixes are accepted and converted).
_BYTE_NAMES = frozenset(n for n, v in MYSQL_VARIABLES.items() if v[1] == "bytes") | frozenset((
    "tmp_memory_table_size", "tmp_disk_table_size", "innodb_buffer_pool_size_max", "key_buffer_size",
    "aria_pagecache_buffer_size", "query_cache_size", "thread_stack", "net_buffer_length",
    "bulk_insert_buffer_size", "preload_buffer_size", "max_binlog_cache_size", "binlog_stmt_cache_size"))


def check_name(name):
    """A system variable name (lower case): letters, digits and underscores,
    with one optional dot for component variables (validate_password.length)."""
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 64:
        raise ValueError("Name a system variable, for example max_connections.")
    text = name.strip().lower().replace("-", "_")
    if text.startswith("@@"):
        text = re.sub(r"^@@(?:global\.|session\.|persist\.|persist_only\.)?", "", text)
    if not _NAME.match(text):
        raise ValueError("%r is not a system variable name: use letters, digits and underscores." % name)
    return text


def check_mode(mode):
    if mode not in MODES:
        raise ValueError("Choose a mode: %s." % ", ".join(MODES))
    return mode


def _flags(name):
    entry = MYSQL_VARIABLES.get(name, mysql_sysvars.SYSVARS.get(name))
    return set(entry[2].split()) if entry and entry[2] is not None else set()


def static_dynamic(engine, name):
    """True, False or None (unknown) without reading the server: whether
    *name* can change at runtime. For the Parameters view's labels; the
    sheet reads the server before offering anything."""
    if engine == "mariadb":
        if name in MARIADB_READ_ONLY:
            return False
        return True if name in MARIADB_DYNAMIC else None
    entry = MYSQL_VARIABLES.get(name, mysql_sysvars.SYSVARS.get(name)) if engine == "mysql" else None
    if entry is None or entry[2] is None:
        return None
    return "ro" not in entry[2].split()


#: Names the size heuristic would call bytes but that count rows, pages, entries,
#: characters, threads or percent (sys_vars.cc and ha_innodb.cc help texts).
_COUNT_NAMES = frozenset((
    "innodb_page_size", "thread_pool_size", "histogram_size", "host_cache_size", "thread_cache_size",
    "max_join_size", "innodb_purge_batch_size", "innodb_sync_array_size", "innodb_ft_min_token_size",
    "innodb_ft_max_token_size", "ngram_token_size", "innodb_doublewrite_batch_size",
    "innodb_change_buffer_max_size", "binlog_transaction_dependency_history_size"))


def _is_bytes(name):
    if name in _BYTE_NAMES:
        return True
    # Performance Schema sizes and history sizes count rows, not bytes.
    if name in _COUNT_NAMES or name.startswith("performance_schema_") or \
            name.endswith(("_history_size", "_history_long_size")):
        return False
    return name.endswith(("_buffer_size", "_cache_size", "_size_max", "_size"))

# ---- reading (talks to the server) -----------------------------------------------------------

_MARK = "myflames:"


def _nullable_read(name, session=False):
    """Read NULL separately from text, including the literal string NULL."""
    scopes = ("SESSION",) if session else ("GLOBAL", "SESSION")
    return "SELECT " + ", ".join("@@%s.%s IS NULL, HEX(@@%s.%s)" % (scope, name, scope, name)
                                 for scope in scopes)


def _decode_value(row):
    if row[0] == "1":
        return None
    if row[0] != "0" or len(row) < 2 or row[1] is None:
        raise ValueError("The server returned an unreadable variable value.")
    try:
        return bytes.fromhex(row[1]).decode("utf-8")
    except (ValueError, UnicodeError):
        raise ValueError("The server returned an unreadable variable value.")


def _persisted_state(persisted, supported=True):
    """Keep absence, unreadability and a present SQL NULL distinct."""
    if not supported:
        return {"state": "unsupported", "read": False, "present": None, "value": None}
    if not persisted or not persisted.get("read"):
        return {"state": "unreadable", "read": False, "present": None, "value": None}
    present = bool(persisted.get("present"))
    return {"state": "present" if present else "absent", "read": True,
            "present": present, "value": persisted.get("value") if present else None}


def _sections(text):
    """Rows of a script whose result sets are separated by marker rows
    (``SELECT 'myflames:<name>'``): {name: rows}; rows before the first
    marker are under ''."""
    out, current = {"": []}, ""
    for line in (text or "").split("\n"):
        if not line:
            continue
        cells = [None if cell == "NULL" else _unescape(cell) for cell in line.split("\t")]
        if len(cells) == 1 and (cells[0] or "").startswith(_MARK):
            current = cells[0][len(_MARK):]
            out[current] = []
            continue
        out.setdefault(current, []).append(cells)
    return out


def _script(statements):
    return "; ".join(TAG + s for s in statements)


def _quote_name(name):
    return "'%s'" % name


def _read(conn, name):
    """Everything known about *name* on this server (not yet the public shape)."""
    name = check_name(name)
    extra = ("version", "version_comment", "persisted_globals_load", "version_compile_os",
             "server_uuid", "server_id", "hostname")
    if name == "innodb_buffer_pool_size":
        extra += ("innodb_buffer_pool_chunk_size", "innodb_buffer_pool_instances", "innodb_buffer_pool_size_max")
    names = ", ".join(_quote_name(n) for n in sorted(set(extra + (name,))))
    statements = ["SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)" % names,
                  "SELECT '%ssession'" % _MARK,
                  "SHOW SESSION VARIABLES WHERE Variable_name = %s" % _quote_name(name)]
    if name == "character_set_results":
        statements += ["SELECT '%snullable'" % _MARK, _nullable_read(name)]
    text = conn.run(_script(statements),
                    extra_flags=["--comments"])
    parts = _sections(text)
    global_rows = {r[0]: (r[1] if len(r) > 1 else "") for r in parts.get("", []) if r and r[0]}
    session_rows = {r[0]: (r[1] if len(r) > 1 else "") for r in parts.get("session", []) if r and r[0]}
    version = global_rows.get("version") or ""
    comment = global_rows.get("version_comment") or ""
    engine = detect_flavor(version, comment)
    identity_keys = ("server_id", "hostname", "version") if engine == "mariadb" else ("server_uuid",)
    server_identity = {key: global_rows.get(key) for key in identity_keys}
    if not all(server_identity.values()):
        server_identity = None
    if name == "character_set_results":
        rows = parts.get("nullable") or []
        if not rows or len(rows[0]) != 4:
            raise ValueError("Could not read character_set_results with its SQL NULL state.")
        global_rows[name], session_rows[name] = _decode_value(rows[0][:2]), _decode_value(rows[0][2:])
    info = {"name": name, "engine": engine, "server_version": version or None,
            "server_identity": server_identity, "platform": global_rows.get("version_compile_os"),
            "read_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "version_tuple": _version_tuple(version), "in_global": name in global_rows,
            "in_session": name in session_rows, "global_value": global_rows.get(name),
            "session_value": session_rows.get(name), "persisted_globals_load": global_rows.get("persisted_globals_load"),
            "related": {k: v for k, v in global_rows.items() if k not in ("version", "version_comment", name)},
            "catalog": None, "info": None, "persisted": None, "metadata": None, "notes": []}
    try:
        if engine == "mariadb":
            rows = _sections(conn.run(TAG + (
                "SELECT VARIABLE_NAME, SESSION_VALUE, GLOBAL_VALUE, GLOBAL_VALUE_ORIGIN, DEFAULT_VALUE, VARIABLE_SCOPE, "
                "VARIABLE_TYPE, NUMERIC_MIN_VALUE, NUMERIC_MAX_VALUE, NUMERIC_BLOCK_SIZE, ENUM_VALUE_LIST, READ_ONLY, "
                "GLOBAL_VALUE_PATH, VARIABLE_COMMENT, COMMAND_LINE_ARGUMENT FROM information_schema.SYSTEM_VARIABLES "
                "WHERE VARIABLE_NAME = "
                "%s" % _quote_name(name.upper())), extra_flags=["--comments"]))[""]
            if rows:
                keys = ("name", "session_value", "global_value", "origin", "default", "scope", "type", "min", "max",
                        "block", "values", "read_only", "path", "comment", "command_line_argument")
                info["catalog"] = dict(zip(keys, rows[0]))
        else:
            statements = ["SELECT '%sinfo'" % _MARK,
                          "SELECT VARIABLE_SOURCE, VARIABLE_PATH, MIN_VALUE, MAX_VALUE, SET_TIME, SET_USER, SET_HOST "
                          "FROM performance_schema.variables_info WHERE VARIABLE_NAME = %s" % _quote_name(name),
                          "SELECT '%spersisted'" % _MARK,
                          "SELECT VARIABLE_VALUE IS NULL, HEX(VARIABLE_VALUE) FROM "
                          "performance_schema.persisted_variables WHERE VARIABLE_NAME = %s"
                          % _quote_name(name)]
            if info["version_tuple"] >= (9, 0, 0):
                statements += ["SELECT '%smetadata'" % _MARK,
                               "SELECT VARIABLE_SCOPE, DATA_TYPE, MIN_VALUE, MAX_VALUE, DOCUMENTATION FROM "
                               "performance_schema.variables_metadata WHERE VARIABLE_NAME = %s" % _quote_name(name)]
            parts = _sections(conn.run(_script(statements), extra_flags=["--comments"]))
            rows = parts.get("info") or []
            if rows:
                info["info"] = dict(zip(("source", "path", "min", "max", "set_time", "set_user", "set_host"), rows[0]))
            rows = parts.get("persisted")
            info["persisted"] = {"present": bool(rows), "value": _decode_value(rows[0]) if rows else None,
                                 "read": rows is not None}
            rows = parts.get("metadata") or []
            if rows:
                info["metadata"] = dict(zip(("scope", "type", "min", "max", "documentation"), rows[0]))
    except ConnectorError as exc:
        text = str(exc)
        if "ERROR 2002" in text or "ERROR 2003" in text or "ERROR 2013" in text or "timed out" in text:
            raise
        info["notes"].append("Could not read the variable's details: %s" % reason(exc))
    return info


def _scope(info):
    """'global', 'both', 'session' or None, and where it came from."""
    catalog, metadata = info.get("catalog"), info.get("metadata")
    if catalog and catalog.get("scope"):
        return {"GLOBAL": "global", "SESSION": "both", "SESSION ONLY": "session"}.get(catalog["scope"].upper()), \
            "information_schema.SYSTEM_VARIABLES"
    if metadata and metadata.get("scope"):
        return {"GLOBAL": "global", "SESSION": "both", "SESSION_ONLY": "session"}.get(metadata["scope"].upper()), \
            "performance_schema.variables_metadata"
    if info["engine"] == "mysql" and info["name"] in MYSQL_VARIABLES:
        return MYSQL_VARIABLES[info["name"]][0], "myflames map (sys_vars.cc)"
    if info["engine"] == "mysql" and info["name"] in mysql_sysvars.SYSVARS:
        return mysql_sysvars.SYSVARS[info["name"]][0], "myflames generated MySQL catalog"
    if info["in_session"] and not info["in_global"]:
        return "session", "SHOW SESSION VARIABLES"
    return None, None


def _type(info):
    """(type, values) where type is bytes, integer, double, boolean, enum, set, string or None."""
    name = info["name"]
    catalog, metadata = info.get("catalog"), info.get("metadata")
    known = MYSQL_VARIABLES.get(name) or mysql_sysvars.SYSVARS.get(name) if info["engine"] == "mysql" else None
    if name == "optimizer_switch" or (known and known[1] == "flagset"):
        # The live flag names account for version and engine differences
        # (optimizer_switch, optimizer_trace, optimizer_trace_features).
        flags = _flag_values(info.get("global_value") or info.get("session_value"))
        return "flagset", list(flags) if flags else None
    if catalog and catalog.get("type"):
        kind = catalog["type"].upper()
        values = [v for v in (catalog.get("values") or "").split(",") if v] or None
        if kind == "BOOLEAN":
            return "boolean", ["OFF", "ON"]
        if kind.startswith(("INT", "BIGINT")):
            return ("bytes" if _is_bytes(name) else "integer"), None
        if kind == "DOUBLE":
            return "double", None
        if kind == "ENUM":
            return "enum", values
        if kind == "SET":
            return "set", values
        if kind == "FLAGSET":
            return "flagset", values
        return "string", None
    if info["engine"] == "mysql" and name in MYSQL_VARIABLES:
        kind = MYSQL_VARIABLES[name][1]
        if kind == "boolean":
            return kind, ["OFF", "ON"]
        if name == "innodb_flush_method":
            platform = info.get("platform")
            if not platform:
                return kind, None
            return kind, (["unbuffered", "normal"] if platform.lower().startswith("win")
                          else list(MYSQL_VALUES[name]))
        return kind, list(MYSQL_VALUES[name]) if name in MYSQL_VALUES else None
    if metadata and metadata.get("type"):
        kind = {"Integer": "integer", "Numeric": "double", "String": "string", "Enumeration": "enum",
                "Boolean": "boolean", "Set": "set"}.get(metadata["type"])
        if kind == "integer" and _is_bytes(name):
            kind = "bytes"
        values = mysql_sysvars.VALUES.get(name) if info["engine"] == "mysql" else None
        return kind, (["OFF", "ON"] if kind == "boolean" else list(values) if values else None)
    if info["engine"] == "mysql" and name in mysql_sysvars.SYSVARS:
        kind = mysql_sysvars.SYSVARS[name][1]
        if kind == "integer" and _is_bytes(name):
            kind = "bytes"
        values = mysql_sysvars.VALUES.get(name)
        return kind, (["OFF", "ON"] if kind == "boolean" else list(values) if values else None)
    return None, None


def _range(info, kind):
    """(min, max, block size) for numbers, from the server where it has them."""
    if kind not in ("bytes", "integer", "double"):
        return None, None, None
    catalog = info.get("catalog")
    if catalog:
        low, high, block = _number(catalog.get("min")), _number(catalog.get("max")), _number(catalog.get("block"))
        return low, high, (block if block and block > 1 else None)
    if kind == "double":
        found = MYSQL_DOUBLE_RANGES.get(info["name"])
        return (found[0], found[1], None) if found else (None, None, None)
    source = info.get("metadata") or info.get("info") or {}
    return _number(source.get("min")), _number(source.get("max")), None


def _dynamic(info):
    catalog = info.get("catalog")
    if catalog and catalog.get("read_only"):
        return catalog["read_only"].upper() != "YES"
    if info["engine"] == "mysql":
        return static_dynamic("mysql", info["name"])
    return None


def _set_by(info):
    from .config_review import _set_by
    if info.get("catalog"):
        return info["catalog"].get("origin"), _set_by(info["catalog"].get("origin"))
    if info.get("info"):
        return info["info"].get("source"), _set_by(info["info"].get("source"))
    return None, None


def _flag_values(text, allow_default=False):
    """Parse the server's complete flag set without guessing flag names."""
    if not isinstance(text, str) or not text:
        return None
    flags = {}
    allowed = ("on", "off", "default") if allow_default else ("on", "off")
    for item in text.split(","):
        pair = item.strip().lower().split("=")
        if len(pair) != 2 or not _NAME.match(pair[0]) or pair[1] not in allowed or pair[0] in flags:
            return None
        flags[pair[0]] = pair[1]
    return flags


def _source_metadata(v):
    return {key: v.get(key) for key in ("source", "source_path", "set_by", "set_time", "set_user", "set_host")}


def _can_validate_persist_only(v):
    """PERSIST_ONLY bypasses server checks, so incomplete metadata must fail closed."""
    kind = v.get("type")
    if kind == "boolean":
        return True
    if kind in ("integer", "bytes", "double"):
        return v.get("min") is not None and v.get("max") is not None
    if kind in ("enum", "set"):
        return bool(v.get("values"))
    if kind == "flagset":
        return v["name"] == "optimizer_switch" and bool(v.get("values"))
    # Free text may have a custom server checker (charsets, paths, plugins,
    # SQL modes). A catalog type alone cannot establish that it is safe.
    return False


#: Set through separately named mysqld options although the variable itself has
#: no startup option (M/sql/mysqld.cc:10979-11064; MariaDB uses the same names).
SEPARATE_OPTIONS = frozenset(("core_file", "log_bin", "log_bin_index", "relay_log_index",
                              "skip_external_locking", "locked_in_memory"))


def startup_option(info):
    """False when no startup option sets the variable (the server computes it:
    MariaDB COMMAND_LINE_ARGUMENT is NULL, MySQL catalog flag nc), True when
    one does, None when unknown. Hand-map MySQL entries stay None."""
    name = info["name"]
    if name in SEPARATE_OPTIONS:
        return True
    catalog = info.get("catalog")
    if catalog is not None and "command_line_argument" in catalog:
        return catalog["command_line_argument"] is not None
    if info["engine"] == "mysql" and name not in MYSQL_VARIABLES and "nc" in _flags(name):
        return False
    return None


def _default(info):
    if info.get("catalog"):
        return info["catalog"].get("default")
    from .config_review import DEFAULTS
    value = DEFAULTS.get(info["engine"], {}).get(info["name"])
    return str(value) if value is not None else None


def describe(info):
    """The public ``variable-1.0`` shape of a :func:`_read` result."""
    scope, scope_source = _scope(info)
    kind, values = _type(info)
    low, high, block = _range(info, kind)
    raw_source, set_by = _set_by(info)
    exists = bool(info["in_global"] or info["in_session"] or info.get("catalog"))
    persisted = info.get("persisted") if info["engine"] == "mysql" else None
    load = info.get("persisted_globals_load")
    flags = _flags(info["name"]) if info["engine"] == "mysql" else set()
    description = None
    if info.get("catalog"):
        description = info["catalog"].get("comment")
    elif info.get("metadata"):
        description = info["metadata"].get("documentation")
    out = {
        "schema_version": READ_SCHEMA, "engine": info["engine"], "server_version": info["server_version"],
        "server_identity": info.get("server_identity"), "platform": info.get("platform"),
        "read_at": info.get("read_at"), "nullable": info["name"] == "character_set_results",
        "name": info["name"], "exists": exists, "scope": scope, "scope_source": scope_source,
        "dynamic": _dynamic(info), "type": kind, "values": values, "min": low, "max": high, "block_size": block,
        "global_value": info["global_value"] if info["in_global"] else (
            info["catalog"].get("global_value") if info.get("catalog") else None),
        # SHOW SESSION VARIABLES also lists global-only variables (with the global value).
        "session_value": None if scope == "global" else info["session_value"],
        "default": _default(info), "set_by": set_by, "source": raw_source,
        "source_path": (info.get("info") or {}).get("path") or (info.get("catalog") or {}).get("path") or None,
        "set_time": (info.get("info") or {}).get("set_time"), "set_user": (info.get("info") or {}).get("set_user"),
        "set_host": (info.get("info") or {}).get("set_host"),
        "persisted": dict(_persisted_state(persisted, info["engine"] == "mysql"),
                          supported=info["engine"] == "mysql",
                          load=None if load is None else load.upper() in ("ON", "1")),
        "non_persistent": "np" in flags, "persist_as_read_only": "pro" in flags,
        "session_read_only": "sro" in flags, "startup_option": startup_option(info),
        "description": (description or "")[:600] or None,
        "notes": list(info.get("notes") or []),
    }
    if (info["engine"] == "mysql" and info["name"] not in MYSQL_VARIABLES
            and info["name"] not in mysql_sysvars.SYSVARS and exists):
        out["notes"].append("myflames has no source-verified record of whether %s can change while the server runs; "
                            "the server decides (error 1238 when it cannot)." % info["name"])
    if info["name"] == "innodb_buffer_pool_size":
        out["related"] = {k: v for k, v in info.get("related", {}).items() if k.startswith("innodb_buffer_pool")}
    out["modes"] = _modes(out)
    return out


def read_variable(conn, name):
    """The ``variable-1.0`` description of one variable. Reads only."""
    return describe(_read(conn, name))


def _modes(v):
    """Which modes this variable allows on this server, with the reason when not."""
    mysql = v["engine"] == "mysql"
    name = v["name"]
    out = {}

    def mode(key, available, why=None, recommended=False):
        out[key] = {"available": bool(available), "reason": None if available else why, "recommended": recommended}
    if not v["exists"]:
        for key in MODES:
            mode(key, False, "This server has no variable named %s." % name)
        return out
    read_only = v["dynamic"] is False
    session_only = v["scope"] == "session"
    mode("global", not read_only and not session_only,
         "%s is read-only while the server runs." % name if read_only else
         "%s has only a session value." % name)
    if not mysql:
        mode("persist", False, "MariaDB has no SET PERSIST.")
        mode("persist_only", False, "MariaDB has no SET PERSIST_ONLY.")
        mode("reset_persist", False, "MariaDB has no RESET PERSIST.")
    else:
        if v["non_persistent"]:
            why = "%s cannot be persisted (NON_PERSIST)." % name
            mode("persist", False, why)
            mode("persist_only", False, why)
        else:
            mode("persist", not read_only and not session_only,
                 ("%s is read-only while the server runs: use SET PERSIST_ONLY, then restart." % name)
                 if read_only else "%s has only a session value." % name)
            mode("persist_only", not session_only, "%s has only a session value." % name, recommended=read_only)
        mode("reset_persist", v["persisted"]["present"], "mysqld-auto.cnf has no value for %s." % name)
        if v.get("startup_option") is False:
            mode("persist_only", False, "The server sets %s; no option or statement changes it." % name)
        if not _can_validate_persist_only(v):
            mode("persist_only", False, "myflames cannot fully validate %s for SET PERSIST_ONLY; "
                 "its type, bounds or allowed values are unavailable." % name)
        if not v["persisted"]["read"]:
            for key in ("persist", "persist_only", "reset_persist"):
                mode(key, False, "Could not read the persisted state of %s. Read access to "
                     "performance_schema.persisted_variables is required before changing it." % name)
    scoped = v["scope"] in ("both", "session", None)
    mode("session", scoped and not read_only and not v["session_read_only"],
         "%s is read-only while the server runs." % name if read_only else
         "The session value of %s is read-only; change it globally." % name if v["session_read_only"] else
         "%s has only a global value." % name)
    return out

# ---- the plan (pure) ----------------------------------------------------------------------


def _problem(code, message):
    return {"code": code, "message": message}


def _text_literal(text):
    """A quoted SQL string: plain text as '...', anything else as a hex literal
    (perf_common.literal), so no escaping rule or sql_mode can change it."""
    if _PLAIN_TEXT.match(text):
        return "'%s'" % text
    return literal(text)


def _display_number(kind, number):
    if number is None:
        return None
    if kind == "bytes":
        return "%s (%s)" % ("{:,}".format(number), _bytes_text(number))
    return "{:,}".format(number) if isinstance(number, int) else repr(number)


def normalize_value(v, raw):
    """``{"literal", "value", "number", "display", "problems"}`` for *raw*
    (text, a number or a boolean) against the variable *v* (variable-1.0).
    ``literal`` is the SQL text after ``=``; ``value`` is the canonical text
    the server should show afterwards."""
    if raw is None:
        return {"literal": "NULL", "value": None, "number": None, "display": "SQL NULL", "sql_null": True,
                "default": False, "problems": [] if v.get("nullable") else [
                    _problem("invalid_value", "%s does not accept SQL NULL." % v["name"])]}
    if isinstance(raw, bool):
        raw = "ON" if raw else "OFF"
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = repr(raw) if isinstance(raw, float) else str(raw)
    if not isinstance(raw, str):
        raise ValueError("Pass the new value as text.")
    if len(raw) > MAX_VALUE_LENGTH:
        raise ValueError("The value is limited to %d characters." % MAX_VALUE_LENGTH)
    kind = v.get("type")
    text = raw if kind == "string" else raw.strip()
    name = v["name"]
    out = {"literal": None, "value": text, "number": None, "display": text, "problems": [],
           "sql_null": False, "default": False}

    def fail(code, message):
        out["problems"].append(_problem(code, message))
        return out
    if text.upper() == "DEFAULT":
        out.update(literal="DEFAULT", value=None, display="DEFAULT (the server's default)", default=True)
        return out
    if kind in ("bytes", "integer"):
        number = None
        try:
            if _INT.match(text):
                number = int(text)
            elif kind == "bytes":
                m = _SIZE.match(text)
                if m:
                    with localcontext() as context:
                        context.prec = len(text) + 32
                        exact = Decimal(m.group(1)) * _UNITS[m.group(2).upper()]
                        if exact != exact.to_integral_value():
                            return fail("invalid_value", "%s is not a whole number of bytes." % text)
                        number = int(exact)
        except (ValueError, OverflowError, DecimalException):
            return fail("invalid_value", "%s is not a valid finite number." % text)
        if number is None:
            return fail("invalid_value", "%s takes a whole number%s, not %r." % (
                name, " of bytes (K, M, G and T suffixes are converted)" if kind == "bytes" else "", text))
        low, high = v.get("min"), v.get("max")
        if number < -(1 << 63) or number > (1 << 64) - 1:
            return fail("out_of_range", "The value is outside the server's 64-bit integer range.")
        if low is not None and number < low or high is not None and number > high:
            return fail("out_of_range", "%s must be from %s to %s; %s is outside." % (
                name, _display_number(kind, low), _display_number(kind, high), "{:,}".format(number)))
        out.update(literal=str(number), value=str(number), number=number, display=_display_number(kind, number))
        return out
    if kind == "double":
        if not _DECIMAL.match(text):
            return fail("invalid_value", "%s takes a number, not %r." % (name, text))
        try:
            exact = Decimal(text)
            number = float(exact)
            if not exact.is_finite() or not math.isfinite(number) or (number == 0 and exact != 0):
                return fail("invalid_value", "%s must be a finite representable number." % name)
        except (ValueError, OverflowError, DecimalException):
            return fail("invalid_value", "%s is not a valid finite number." % text)
        low, high = v.get("min"), v.get("max")
        if low is not None and exact < Decimal(str(low)) or high is not None and exact > Decimal(str(high)):
            return fail("out_of_range", "%s must be from %s to %s; %s is outside." % (name, low, high, text))
        with localcontext() as context:
            context.prec = len(text) + 32
            literal_text = format(exact.normalize(), "f")
        out.update(literal=literal_text, value=literal_text, number=number, display=literal_text)
        return out
    if kind == "boolean":
        upper = text.upper()
        if upper in _BOOL_TRUE:
            out.update(literal="ON", value="ON", display="ON")
        elif upper in _BOOL_FALSE:
            out.update(literal="OFF", value="OFF", display="OFF")
        else:
            return fail("invalid_value", "%s takes ON or OFF, not %r." % (name, text))
        return out
    if kind == "enum":
        values = v.get("values")
        if values:
            match = [x for x in values if x.lower() == text.lower()]
            if not match:
                return fail("invalid_value", "%s takes one of %s, not %r." % (name, ", ".join(values), text))
            text = match[0]
        if not text:
            return fail("invalid_value", "Choose a value for %s." % name)
        out.update(literal=_text_literal(text), value=text, display=text)
        return out
    if kind == "set":
        values = v.get("values")
        items = [i.strip() for i in text.split(",") if i.strip()]
        if name == "log_output" and v.get("engine") == "mysql" and not items:
            return fail("invalid_value", "log_output cannot be empty; use NONE to disable log output.")
        if values:
            canonical = []
            for item in items:
                match = [x for x in values if x.lower() == item.lower()]
                if not match:
                    return fail("invalid_value", "%s takes a comma list of %s; %r is not one." % (
                        name, ", ".join(values), item))
                canonical.append(match[0])
            items = canonical
        text = ",".join(items)
        out.update(literal=_text_literal(text), value=text, display=text or "(empty)")
        return out
    if kind == "flagset":
        values = v.get("values") or []
        flags = {}
        for item in text.split(","):
            pair = [part.strip().lower() for part in item.split("=")]
            if len(pair) != 2 or pair[0] not in values or pair[1] not in ("on", "off", "default") or pair[0] in flags:
                return fail("invalid_value", "%s needs known, unique flags with on, off or default values; "
                            "%r is not valid." % (name, item))
            flags[pair[0]] = pair[1]
        text = ",".join("%s=%s" % pair for pair in flags.items())
        out.update(literal=_text_literal(text), value=text, display=text, flags=flags)
        return out
    if kind == "string":
        out.update(literal=_text_literal(text), value=text, display=text or "(empty)")
        return out
    # Unknown type (a MySQL variable outside the map, before 9.0): a number,
    # ON/OFF, or text; the server checks the rest.
    if _INT.match(text) or _DECIMAL.match(text):
        out.update(literal=text, value=text)
    elif text.upper() in ("ON", "OFF"):
        out.update(literal=text.upper(), value=text.upper())
    else:
        out.update(literal=_text_literal(text))
    return out


def statement_for(name, mode, literal_text=None):
    """The statement for *mode* (untagged). RESET PERSIST takes no value."""
    check_mode(mode)
    if mode == "reset_persist":
        return "RESET PERSIST %s" % name
    keyword = {"global": "GLOBAL", "persist": "PERSIST", "persist_only": "PERSIST_ONLY", "session": "SESSION"}[mode]
    return "SET %s %s = %s" % (keyword, name, literal_text if literal_text is not None else "0")


def config_line(name, value):
    """The option-file line that keeps *value* after a restart (``value`` None: DEFAULT)."""
    if value is None:
        return None
    text = str(value)
    if text == "" or re.search(r"[\s#;'\"]", text):
        text = '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')
    return "%s = %s" % (name, text)


def _undo(v, mode):
    name = v["name"]
    out = []
    current = v["global_value"] if mode != "session" else v["session_value"]
    persisted = v["persisted"]
    if mode in ("global", "persist", "session") and (current is not None or v.get("nullable")):
        literal_text = normalize_value(v, current).get("literal")
        if literal_text:
            out.append(statement_for(name, "global" if mode != "session" else "session", literal_text) + ";")
    if mode in ("persist", "persist_only") and persisted.get("read"):
        if persisted["present"]:
            normalized = normalize_value(v, persisted["value"])
            literal_text = normalized.get("literal")
            if literal_text and not normalized["problems"]:
                out.append(statement_for(name, "persist_only", literal_text) + ";")
        else:
            out.append("RESET PERSIST %s;" % name)
    if mode == "reset_persist" and persisted.get("read") and persisted["present"]:
        normalized = normalize_value(v, persisted["value"])
        literal_text = normalized.get("literal")
        if literal_text and not normalized["problems"]:
            out.append(statement_for(name, "persist_only", literal_text) + ";")
    return out


def _evaluate(any_of, note, grants, engine, cutoff="11.0.1"):
    """A privilege hint like dbops.privilege_hint for an explicit need."""
    parsed = dbops.parse_grants(grants, engine)
    have = set(parsed["global"])
    result = {"checked": True, "available": True, "allowed": None, "via": None,
              "need": " or ".join(" and ".join(a) or "no privilege" for a in any_of), "note": note}
    for alternative in any_of:
        if all(p in have for p in alternative):
            result["allowed"], result["via"] = True, " and ".join(alternative) or "no privilege needed"
            return result
    if have & {"ALL", "ALL PRIVILEGES"}:
        result["allowed"], result["via"] = True, "ALL PRIVILEGES"
    elif engine == "mariadb" and "SUPER" in have:
        result["note"] = (note + " On MariaDB %s and later, SUPER covers this only for grants stored by an "
                                 "earlier version." % cutoff).strip()
    elif not parsed["unparsed"]:
        # A line the parser did not understand may grant it: unknown, not denied.
        result["allowed"] = False
    return result


def privileges(v, mode, grants=None):
    """What *mode* needs for *v*, checked against SHOW GRANTS lines when given
    (a hint; the server decides). ``{"operation", "need", "note", "checked",
    "allowed", "via"}``."""
    engine, name = v["engine"], v["name"]
    version = _version_tuple(v.get("server_version") or "")
    persisted_read_only = v["dynamic"] is False or v.get("persist_as_read_only")
    # Since which MariaDB version SUPER alone no longer covers the privilege (grants stored before it still do).
    cutoff = "11.0.1"
    if mode == "session":
        restricted = name in (MYSQL_RESTRICTED_SESSION if engine == "mysql" else MARIADB_RESTRICTED_SESSION) or \
            name == "skip_replication" and engine == "mariadb"
        if not restricted:
            out = {"checked": grants is not None, "available": True, "allowed": True, "via": "no privilege needed",
                   "need": "no privilege", "note": "Any account can change its own session value of %s." % name}
            out["operation"] = "set_session"
            return out
        if engine == "mysql" and name in MYSQL_RESTRICTED_NO_SUPER:
            any_of, note = [["SESSION_VARIABLES_ADMIN"], ["SYSTEM_VARIABLES_ADMIN"]], "SUPER is not enough for %s." % name
        else:
            need = dbops.required_privileges("set_session_restricted", engine, version, name)
            any_of, note = need["any_of"], need["note"]
            if engine == "mysql" and name in MYSQL_RESTRICTED_APPLIER:
                any_of = any_of + [["REPLICATION_APPLIER"]]
    elif mode == "global" and engine == "mariadb" and name in MARIADB_GLOBAL_PRIVILEGE:
        privilege = MARIADB_GLOBAL_PRIVILEGE[name]
        any_of = [[privilege]]
        if name in MARIADB_GLOBAL_OR_SUPER or privilege == "READ_ONLY ADMIN" and version < dbops.MARIADB_SUPER_READ_ONLY_END or \
                privilege != "READ_ONLY ADMIN" and version < dbops.MARIADB_SUPER_SPLIT_END:
            any_of.append(["SUPER"])
        note = ""
        cutoff = "10.11.1" if privilege == "READ_ONLY ADMIN" else "11.0.1"
    else:
        operation = {"global": "set_global", "persist": "set_persist", "persist_only": "set_persist_only",
                     "reset_persist": "reset_persist"}[mode]
        need = dbops.required_privileges(operation, engine, version, name,
                                         read_only_variable=bool(persisted_read_only))
        if not need["available"]:
            return {"operation": OPERATIONS[mode], "checked": False, "available": False, "allowed": False,
                    "via": None, "need": "", "note": need["note"]}
        any_of, note = need["any_of"], need["note"]
        if mode == "reset_persist" and persisted_read_only:
            note = ("%s is stored as a read-only entry, so removing it needs both privileges." % name).strip()
    if grants is None:
        out = {"checked": False, "available": True, "allowed": None, "via": None,
               "need": " or ".join(" and ".join(a) for a in any_of), "note": note}
    else:
        out = _evaluate(any_of, note, grants, engine, cutoff)
    out["operation"] = OPERATIONS[mode]
    return out


def _effect(v, mode):
    name = v["name"]
    both = v["scope"] == "both"
    if mode == "global":
        text = "Changes the running value now, until the server restarts."
        if both:
            text += " Sessions that connect afterwards use it; sessions already connected keep their own value."
        return {"now": True, "after_restart": False, "needs_restart": False, "text": text}
    if mode == "persist":
        text = ("Changes the running value now and writes it to mysqld-auto.cnf in the data directory, so it also "
                "applies after a restart.")
        if both:
            text += " Sessions already connected keep their own value."
        return {"now": True, "after_restart": True, "needs_restart": False, "text": text}
    if mode == "persist_only":
        return {"now": False, "after_restart": True, "needs_restart": True,
                "text": "Writes the value to mysqld-auto.cnf only. The running value does not change; the new value "
                        "applies at the next restart."}
    if mode == "reset_persist":
        return {"now": False, "after_restart": True, "needs_restart": False,
                "text": "Removes %s from mysqld-auto.cnf. The running value stays; after a restart the server uses "
                        "the option files or its default." % name}
    return {"now": True, "after_restart": False, "needs_restart": False,
            "text": "Applies only to the session that runs it and ends with that session. myflames runs it in a "
                    "short session of its own to check the value the server takes; to use it for your queries, run "
                    "the statement in your own session (a Query editor tab)."}


def _caveats(v, mode, normalized):
    name, engine = v["name"], v["engine"]
    out = []
    if mode == "persist_only":
        out.append("The server does not check the value of SET PERSIST_ONLY beyond its type (it does check ON and "
                   "OFF for booleans); myflames checked it against what it knows. A value the server cannot use "
                   "shows up only at the next restart.")
    if mode in ("persist", "persist_only") and v["persisted"]["load"] is False:
        out.append("persisted_globals_load is OFF, so the server ignores mysqld-auto.cnf at startup and the "
                   "persisted value will not apply after a restart.")
    if mode in ("persist", "persist_only"):
        out.append("At startup the server applies mysqld-auto.cnf after the option files, so this value wins over "
                   "my.cnf until RESET PERSIST %s." % name)
    if name == "innodb_buffer_pool_size" and mode in ("global", "persist"):
        if engine == "mysql":
            related = v.get("related") or {}
            chunk = _number(related.get("innodb_buffer_pool_chunk_size"))
            instances = _number(related.get("innodb_buffer_pool_instances"))
            text = ("Resizing runs online in the background: the server rounds the size up to a multiple of "
                    "innodb_buffer_pool_chunk_size x innodb_buffer_pool_instances")
            number = normalized.get("number")
            if chunk and instances:
                unit = chunk * instances
                text += " (%s here)" % _bytes_text(unit)
                if number and number % unit:
                    text += ", so %s becomes %s" % (_bytes_text(number), _bytes_text((number // unit + 1) * unit))
            text += ", and reports progress in Innodb_buffer_pool_resize_status."
            if instances and instances > 1:
                text += " With more than one instance it ignores sizes under 1 GiB (only a warning)."
            out.append(text)
        else:
            out.append("Resizing runs online. Where innodb_buffer_pool_size_max exists (MariaDB 10.11.12, 11.4.6, "
                       "11.8.2 and later) it can grow only up to that limit; beyond it needs a restart.")
        out.append("Leave memory for connections, the OS and other processes: a pool that makes the server swap is "
                   "far worse than disk reads.")
    if name == "max_connections" and mode != "session":
        out.append("Every connection can allocate its own sort, join and temporary-table memory, so a higher limit "
                   "raises the worst-case memory.")
    if name in ("innodb_flush_log_at_trx_commit", "sync_binlog") and normalized.get("value") not in (None, "1") \
            and not (name == "innodb_flush_log_at_trx_commit" and engine == "mariadb" and normalized.get("value") == "3"):
        out.append("Values other than 1%s trade durability for commit speed: a crash or power loss can lose "
                   "committed transactions." % (" (and MariaDB's 3)" if name == "innodb_flush_log_at_trx_commit"
                                                and engine == "mariadb" else ""))
    if engine == "mariadb" and name in ("slow_query_log", "log_slow_query") and mode == "global":
        out.append("MariaDB logs a statement only when both the global value and the session's copy (taken when it "
                   "connects) are on: turning it on reaches only connections made afterwards, so pooled connections "
                   "keep not logging; turning it off stops logging for every connection.")
    if mode == "session" and name in dbops.SESSION_RISKY_VARIABLES:
        out.append(dbops.SESSION_RISKY_VARIABLES[name])
    block = v.get("block_size")
    number = normalized.get("number")
    if block and isinstance(number, int) and number % block:
        out.append("The server rounds %s down to a multiple of %s, so %s becomes %s (with STRICT_ALL_TABLES in "
                   "sql_mode it refuses the value instead)." % (
                       name, "{:,}".format(block), "{:,}".format(number), "{:,}".format(number // block * block)))
    return out


def _alternative(v, mode, line):
    """What to do instead when *mode* is not available for *v*."""
    name = v["name"]
    if not v["exists"]:
        return None
    if v.get("startup_option") is False and v["dynamic"] is False:
        return {"mode": None, "config_line": None,
                "text": "The server sets %s; no option or statement changes it." % name}
    cnf = ("add %s under [mysqld] (or [mariadb]) in an option file" % line) if line else "set it in an option file"
    if v["engine"] == "mariadb" and v["dynamic"] is False:
        return {"mode": None, "config_line": line,
                "text": "%s and restart the server; myflames never edits option files." % (cnf[0].upper() + cnf[1:])}
    if v["engine"] == "mariadb" and mode in ("persist", "persist_only"):
        now = v["modes"]["global"]["available"]
        return {"mode": "global" if now else None, "config_line": line,
                "text": "%sTo keep it after a restart, %s; myflames never edits option files." % (
                    "MariaDB has no SET PERSIST; use SET GLOBAL to change it now. " if now else "MariaDB has no SET PERSIST. ", cnf)}
    if v["engine"] == "mysql" and v["dynamic"] is False and v["modes"]["persist_only"]["available"]:
        return {"mode": "persist_only", "config_line": line,
                "text": "Use SET PERSIST_ONLY, then restart the server."}
    for other in ("global", "session"):
        if other != mode and v["modes"][other]["available"] and mode in ("global", "session", "persist"):
            return {"mode": other, "config_line": line,
                    "text": "Use %s instead." % {"global": "SET GLOBAL", "session": "SET SESSION"}[other]}
    return None


def build_plan(v, value, mode, grants=None):
    """The ``variable-plan-1.0`` for variable-1.0 *v*, the requested *value*
    and *mode*. Pure: *grants* are SHOW GRANTS lines (or None)."""
    check_mode(mode)
    name = v["name"]
    problems = []
    plan = {"schema_version": PLAN_SCHEMA, "engine": v["engine"], "server_version": v["server_version"],
            "name": name, "mode": mode, "mode_label": MODE_LABELS[mode], "requested": None, "value": None,
            "display": None, "status": "ready", "problems": problems, "statement": None, "config_line": None,
            "before": {"global": v["global_value"], "session": v["session_value"],
                       "persisted": v["persisted"]["value"] if v["persisted"]["present"] else None,
                       "persisted_state": _persisted_state(v["persisted"], v["engine"] == "mysql"),
                       "source_metadata": _source_metadata(v), "read_at": v.get("read_at")},
            "undo": [], "effect": None, "caveats": [], "privileges": None, "classification": None,
            "expect": None, "variable": v, "alternative": None}
    if mode == "reset_persist":
        normalized = {"literal": None, "value": None, "number": None, "display": None, "problems": []}
    else:
        normalized = normalize_value(v, value)
        plan["requested"] = value
        plan["value"], plan["display"] = normalized["value"], normalized["display"]
        problems.extend(normalized["problems"])
    available = v["modes"][mode]
    if not available["available"]:
        problems.insert(0, _problem("mode_unavailable", available["reason"]))
    if not v.get("server_identity"):
        problems.append(_problem("identity_unavailable", "Could not read the server identity. Nothing can be "
                                 "changed until the target server can be identified."))
    if mode == "persist_only" and not _can_validate_persist_only(v):
        problems.append(_problem("validation_unavailable", "Cannot fully validate this value for SET PERSIST_ONLY."))
    if mode in ("persist", "persist_only", "reset_persist") and v["engine"] == "mysql" \
            and not v["persisted"].get("read"):
        problems.append(_problem("persisted_unreadable", "The persisted state must be readable before changing it."))
    if mode == "persist_only" and normalized.get("flags", {}).get("hypergraph_optimizer") in ("on", "default"):
        # MySQL's check_optimizer_switch rejects ON in builds without support.
        # The current flag list includes it even in those builds.
        flags = _flag_values(v.get("global_value")) or {}
        if flags.get("hypergraph_optimizer") != "on":
            problems.append(_problem("validation_unavailable", "This server has not demonstrated support for "
                                     "enabling hypergraph_optimizer; SET PERSIST_ONLY cannot validate it."))
    line = None
    if v["exists"] and mode != "reset_persist" and not normalized["problems"]:
        line = plan["config_line"] = config_line(name, normalized.get("value"))
    plan["alternative"] = _alternative(v, mode, line) if not available["available"] else None
    if problems:
        plan["status"] = "refused"
        return plan
    statement = statement_for(name, mode, normalized["literal"])
    plan["statement"] = statement
    plan["undo"] = _undo(v, mode)
    plan["effect"] = _effect(v, mode)
    plan["caveats"] = _caveats(v, mode, normalized)
    if mode in ("global", "session") and v["dynamic"] is None and v["engine"] == "mysql":
        plan["caveats"].append("myflames does not know whether %s can change while the server runs; the server "
                               "refuses with error 1238 when it cannot (try SET PERSIST_ONLY then)." % name)
    plan["privileges"] = privileges(v, mode, grants)
    verdict = dbops.classify(statement)
    plan["classification"] = {"kind": verdict["kind"], "category": verdict["category"], "reasons": verdict["reasons"]}
    plan["expect"] = {"name": name, "mode": mode, "statement": statement, "global_value": v["global_value"],
                      "session_value": v["session_value"] if mode == "session" else None,
                      "persisted_value": plan["before"]["persisted"],
                      "persisted_state": plan["before"]["persisted_state"],
                      "source_metadata": plan["before"]["source_metadata"],
                      "engine": v["engine"], "server_version": v["server_version"],
                      "server_identity": v.get("server_identity")}
    return plan


def plan_change(conn, name, value, mode, grants=True):
    """Read the variable (and the account's grants unless *grants* is False)
    and return the ``variable-plan-1.0``. Reads only."""
    check_mode(mode)
    v = read_variable(conn, name)
    lines = None
    if grants:
        # Plain SHOW GRANTS includes the roles active at login (as dbops.read_grants reads it).
        try:
            lines = [row[0] for row in _sections(conn.run(TAG + "SHOW GRANTS", extra_flags=["--comments"]))[""]
                     if row and row[0]]
        except ConnectorError as exc:
            v["notes"].append("Could not read the account's grants (%s); the server decides." % reason(exc))
    return build_plan(v, value, mode, lines)

# ---- applying one change ------------------------------------------------------------------

#: Server errors as statuses (db-operations skill, section 3).
DENIED = (1227, 1095, 3630, 1142, 1044, 1045)
REFUSED_BY_SERVER = (1228, 1229, 1238, 1231, 1232, 1193, 1621, 3615, 3976, 1210, 1290)
INTERRUPTED = (1317, 1205, 1213)


def error_status(exc):
    """(status, message) for a server error from the change."""
    code = dbops.error_code(str(exc))
    text = re.sub(r"\s+at line \d+:", ":", reason(exc))
    if code in DENIED:
        return "denied", "The server refused it for lack of privileges: %s." % text
    if code in REFUSED_BY_SERVER:
        hint = ""
        if code == 1238 and "read only" in text and "persistent" not in text:
            hint = " Change it at the next restart instead (SET PERSIST_ONLY on MySQL, an option file on MariaDB)."
        elif code in (1228, 1229):
            hint = " Choose the other scope."
        return "refused_by_server", "The server refused it: %s.%s" % (text, hint)
    if code in INTERRUPTED:
        return "interrupted", "The statement was interrupted: %s." % text
    return "failed", "The server returned an error: %s." % text


def check_expect(expect, name, mode):
    """The identity the confirmation captured (a plan's ``expect``)."""
    if not isinstance(expect, dict):
        raise ValueError("Send the expect identity the plan returned.")
    for key in ("name", "mode", "statement"):
        if not isinstance(expect.get(key), str) or len(expect[key]) > MAX_VALUE_LENGTH * 3:
            raise ValueError("expect.%s must be text." % key)
    for key in ("global_value", "session_value", "persisted_value"):
        if expect.get(key) is not None and not isinstance(expect[key], str):
            raise ValueError("expect.%s must be text or null." % key)
    if expect["name"] != name or expect["mode"] != mode:
        raise ValueError("The confirmed change is for %s (%s), not %s (%s)." % (
            expect["name"], expect["mode"], name, mode))
    for key in ("persisted_state", "source_metadata", "server_identity"):
        if not isinstance(expect.get(key), dict):
            raise ValueError("expect.%s is missing. Review a new plan before applying it." % key)
    if expect.get("engine") not in ("mysql", "mariadb") or not isinstance(expect.get("server_version"), str):
        raise ValueError("The confirmed server identity is missing. Review a new plan before applying it.")
    return expect


def representative_statement(name, mode):
    """A statement with the same classification as any value for *name* and
    *mode*: the write policy runs on it before a connection is opened."""
    return statement_for(check_name(name), check_mode(mode), "0")


def typed_statement(name, mode, value):
    """The statement as typed, for the audit record of a change refused
    before the server was read (its types unknown): never sent."""
    if mode == "reset_persist":
        return statement_for(name, mode)
    text = "NULL" if value is None else str(value).strip()
    return statement_for(name, mode, text if re.match(r"^[A-Za-z0-9_.+-]+$", text) else _text_literal(text))


def unsent_result(name, mode, value, status, message):
    """A variable-change-1.0 result for a change myflames did not send."""
    return _result({"name": name, "mode": mode, "requested": value}, status, message)


def _same(a, b):
    return a == b


def _after(conn, name, mode, session_text=None):
    info = _read(conn, name)
    out = {"global": info["global_value"] if info["in_global"] else None,
           "persisted": (info.get("persisted") or {}).get("value") if info["engine"] == "mysql" else None,
           "persisted_state": _persisted_state(info.get("persisted"), info["engine"] == "mysql"),
           "global_read": info["in_global"], "session_read": False, "session": None,
           "engine": info["engine"], "server_identity": info.get("server_identity"),
           "server_version": info["server_version"]}
    if session_text is not None:
        rows = _sections(session_text).get("after") or []
        out["session_read"] = bool(rows and len(rows[0]) > 1)
        if out["session_read"]:
            out["session"] = _decode_value(rows[0]) if name == "character_set_results" else rows[0][1]
    if name == "innodb_buffer_pool_size":
        try:
            rows = _sections(conn.run(TAG + "SHOW GLOBAL STATUS WHERE Variable_name = 'Innodb_buffer_pool_resize_status'",
                                      extra_flags=["--comments"]))[""]
            out["resize_status"] = rows[0][1] if rows and len(rows[0]) > 1 else None
        except ConnectorError:
            out["resize_status"] = None
    return out


def _matches(v, mode, normalized, after):
    """Whether the re-read shows the requested value (None when it cannot tell)."""
    persisted = after.get("persisted_state") or {}
    if mode in ("persist", "persist_only", "reset_persist") and not persisted.get("read"):
        return None
    if mode == "reset_persist":
        return persisted.get("present") is False
    if normalized.get("default"):
        return None
    if v.get("type") == "flagset":
        wanted = normalized.get("flags") or {}
        if mode == "persist_only":
            # PERSIST_ONLY stores the literal assignment; extra assignments
            # would override unrelated settings at the next restart.
            return persisted.get("present") is True and _flag_values(persisted.get("value"), True) == wanted
        if "default" in wanted.values():
            return None  # The live flag list does not identify compiled defaults.
    results = []
    if mode in ("global", "persist", "session"):
        key = "session" if mode == "session" else "global"
        if not after.get(key + "_read", key in after):
            return None
        results.append(_value_matches(v, normalized, after.get(key)))
    if mode in ("persist", "persist_only"):
        results.append(persisted.get("present") is True and _value_matches(v, normalized, persisted.get("value")))
    return all(results)


def _value_matches(v, normalized, got):
    if normalized.get("sql_null"):
        return got is None
    if got is None or normalized.get("value") is None:
        return False
    want = normalized["value"]
    kind = v.get("type")
    if kind in ("bytes", "integer", "double"):
        try:
            return Decimal(str(got)) == Decimal(want)
        except (ValueError, DecimalException):
            return False
    if kind in ("enum", "boolean", "set"):
        return got.lower() == want.lower() or (kind == "boolean" and got.upper() in (
            _BOOL_TRUE if want == "ON" else _BOOL_FALSE))
    if kind == "flagset":
        flags = _flag_values(got)
        wanted = normalized.get("flags") or _flag_values(want)
        return bool(flags and wanted) and all(flags.get(key) == value for key, value in wanted.items())
    return got == want


def _persisted_matches_running(v, after):
    persisted = after.get("persisted_state") or {}
    if not persisted.get("read") or not persisted.get("present") or not after.get("global_read", "global" in after):
        return False
    if v.get("type") == "flagset":
        running = _flag_values(after.get("global"))
        return bool(running) and running == _flag_values(persisted.get("value"))
    normalized = normalize_value(v, after.get("global"))
    return not normalized["problems"] and _value_matches(v, normalized, persisted.get("value"))


def _shown(v, text):
    """A value as the server returned it, readable: sizes in KiB, MiB, GiB."""
    if text is None:
        return "SQL NULL" if v.get("nullable") else "(none)"
    if text == "":
        return "(empty)"
    number = _number(text)
    if v.get("type") == "bytes" and isinstance(number, int) and number >= 1024:
        return _bytes_text(number)
    return str(text)


def _change_message(v, plan, after, matched):
    name, mode = v["name"], plan["mode"]
    if mode == "reset_persist":
        return ("Removed %s from mysqld-auto.cnf. The running value stays %s." % (name, _shown(v, after.get("global")))
                if matched else "Sent RESET PERSIST %s, but mysqld-auto.cnf still has a value for it." % name)
    target = {"global": "globally", "persist": "globally and in mysqld-auto.cnf", "session": "in a myflames session",
              "persist_only": "in mysqld-auto.cnf for the next restart"}[mode]
    got = after.get("session") if mode == "session" else after.get("persisted") if mode == "persist_only" \
        else after.get("global")
    before = plan["before"]["persisted"] if mode == "persist_only" else plan["before"]["session"] \
        if mode == "session" else plan["before"]["global"]
    if matched is False:
        if _same(got, before):
            return ("The server kept %s at %s: it did not take %s. It may have refused with only a warning%s." % (
                name, _shown(v, got), plan["display"],
                " (innodb_buffer_pool_size under 1 GiB with several instances, or a resize already running)"
                if name == "innodb_buffer_pool_size" else ""))
        got_text, want_text = _shown(v, got), _shown(v, plan["value"])
        if got_text == want_text and _number(got) is not None and _number(plan["value"]) is not None:
            got_text, want_text = "{:,}".format(_number(got)), "{:,}".format(_number(plan["value"]))
        return "Set %s %s; the server made it %s instead of %s." % (name, target, got_text, want_text)
    text = "Set %s %s: %s -> %s." % (name, target, _shown(v, before),
                                      _shown(v, got) if got is not None else plan["display"])
    if mode == "session":
        text += " That session has ended; run the statement in your own session to use it."
    if name == "innodb_buffer_pool_size" and after.get("resize_status"):
        text += " Resize: %s" % after["resize_status"]
    return text


def _result(plan, status, message, **extra):
    out = {"schema_version": CHANGE_SCHEMA, "engine": plan.get("engine"), "server_version": plan.get("server_version"),
           "name": plan.get("name"), "mode": plan.get("mode"), "statement": None, "status": status,
           "message": message, "requested": plan.get("requested"), "value": plan.get("value"),
           "before": plan.get("before"), "after": None, "adjusted": False, "verified": [], "plan": plan}
    out.update(extra)
    return out


def _unsent(plan, audit, status, outcome, message, expect, verified=None):
    result = _result(plan, status, message, verified=list(verified or []))
    if audit is not None:
        statement = plan.get("statement") or (expect or {}).get("statement")
        if not statement and plan.get("name") and plan.get("mode") in MODES:
            statement = typed_statement(plan["name"], plan["mode"], plan.get("requested"))
        result["audit_id"] = audit.outcome([TAG + statement] if statement else [],
                                           status if status in ("declined", "refused") else "refused",
                                           outcome=outcome, message=message, target=expect, verified=verified)
    return result


def apply_change(conn, plan, expect, decision=None, confirmed=False, audit=None):
    """Send one confirmed change and report before and after (``variable-change-1.0``).

    *plan* names the change (``name``, ``mode``, ``requested``: a plan this
    module returned, or a dict with those keys); *expect* is the plan's
    ``expect`` as confirmed. *decision* is the connection's write policy
    (:func:`dbops.decide`); without it the change is judged as a Production
    write, so it needs ``confirmed=True``. The variable is read again and the
    plan rebuilt from that read: myflames refuses (and sends nothing) when the
    rebuilt plan is refused, its statement differs from the confirmed one, or
    the global, session or persisted value changed since the plan. Server
    errors become statuses (``denied``, ``refused_by_server``,
    ``interrupted``, ``failed``); a lost connection raises ValueError. *audit*
    (:class:`dbops.AuditTrail`) records every attempt.
    """
    if not isinstance(plan, dict):
        raise ValueError("Send the change to make (name, mode and value).")
    name, mode = check_name(plan.get("name")), check_mode(plan.get("mode"))
    requested = plan.get("requested") if mode != "reset_persist" else None
    expect = check_expect(expect, name, mode)
    stub = {"engine": None, "server_version": None, "name": name, "mode": mode, "requested": requested,
            "statement": expect["statement"], "before": None}
    if decision is None:
        decision = dbops.decide(dbops.classify(representative_statement(name, mode)))
    blocked = dbops.gate(decision, confirmed)
    if blocked:
        return _unsent(stub, audit, "refused" if blocked[0] == "refused" else "needs_confirmation",
                       "read_only" if blocked[0] == "refused" else "needs_confirmation", blocked[1], expect)
    v = read_variable(conn, name)
    if audit is not None:
        audit.server(v["engine"], v["server_version"])
    fresh = build_plan(v, requested, mode)
    if fresh["status"] != "ready":
        return _unsent(fresh, audit, "refused", "invalid", "Nothing was sent: %s" % " ".join(
            p["message"] for p in fresh["problems"]), expect)
    verified = []
    problem = None
    if fresh["statement"] != expect["statement"]:
        problem = ("The statement myflames would send now (%s) differs from the one you confirmed (%s), so nothing "
                   "was sent. Review it again." % (fresh["statement"], expect["statement"]))
    else:
        verified.append("statement")
    checks = [("engine", v["engine"], "engine"), ("server_version", v["server_version"], "server version"),
              ("server_identity", v.get("server_identity"), "server identity"),
              ("global_value", v["global_value"], "global value"),
              ("source_metadata", _source_metadata(v), "source metadata")]
    if mode == "session":
        checks.append(("session_value", v["session_value"], "session value"))
    if v["engine"] == "mysql":
        checks.append(("persisted_value", fresh["before"]["persisted"], "persisted value"))
        checks.append(("persisted_state", fresh["before"]["persisted_state"], "persisted state"))
    for key, now, label in checks:
        if problem:
            break
        if not _same(now, expect.get(key)):
            problem = ("The %s of %s changed since you reviewed it (%s, now %s), so nothing was sent. Review it "
                       "again." % (label, name, expect.get(key) if expect.get(key) is not None else "none",
                                   now if now is not None else "none"))
        else:
            verified.append(label)
    if problem:
        return _unsent(fresh, audit, "refused", "changed", problem, expect, verified)
    statement = fresh["statement"]
    result = _result(fresh, None, None, statement=statement, verified=verified)
    if audit is not None:
        try:
            result["audit_id"] = audit.attempt([TAG + statement], target=expect, verified=verified)
        except Exception as exc:
            raise ValueError("Could not write the audit record (%s), so nothing was sent." % exc)
    session_text = None
    try:
        if mode == "session":
            # One client call, so the read sees the same session.
            session_text = conn.run(_script([statement, "SELECT '%safter'" % _MARK,
                                             _nullable_read(name, session=True) if v.get("nullable") else
                                             "SHOW SESSION VARIABLES WHERE Variable_name = %s" % _quote_name(name)]),
                                    extra_flags=list(dbops.WRITE_CLIENT_FLAGS))
        else:
            conn.run(TAG + statement, extra_flags=list(dbops.WRITE_CLIENT_FLAGS))
    except KeyboardInterrupt:
        if audit is not None:
            audit.fail(KeyboardInterrupt())
        raise
    except ConnectorError as exc:
        code = dbops.error_code(str(exc))
        if code is None or code >= 2000 or dbops.may_have_run(exc):
            # A client error (connection refused or lost, a time limit): no server answer to map.
            unknown = dbops.may_have_run(exc)
            message = "Could not change %s: %s.%s" % (name, reason(exc), " The statement may have reached the "
                                                      "server, so the outcome is unknown; read the variable again."
                                                      if unknown else "")
            if audit is not None:
                audit.complete("unknown" if unknown else "error", outcome="timeout" if unknown else "error",
                               error=str(exc), message=message)
            raise ValueError(message)
        result["status"], result["message"] = error_status(exc)
        if audit is not None:
            audit.complete("error", outcome=result["status"], error=str(exc), message=result["message"])
        return result
    try:
        after = _after(conn, name, mode, session_text)
    except (ConnectorError, ValueError):
        after = None
    result["after"] = after
    normalized = normalize_value(v, requested) if mode != "reset_persist" else {}
    matched = _matches(v, mode, normalized, after) if after else None
    before = fresh["before"]["persisted"] if mode == "persist_only" else fresh["before"]["session"] \
        if mode == "session" else fresh["before"]["global"]
    got = None
    if after:
        got = after.get("session") if mode == "session" else after.get("persisted") \
            if mode in ("persist_only", "reset_persist") else after.get("global")
    expected_server = v
    if after and v["engine"] == "mariadb" and name == "server_id" and mode == "global" and matched is True:
        # This confirmed operation changes one component of MariaDB's identity.
        # The hostname and version must still match the server read beforehand.
        identity = dict(v["server_identity"], server_id=after["global"])
        expected_server = dict(v, server_identity=identity)
    identity_matches = after and all(after.get(key) == expected_server.get(key) for key in
                                     ("engine", "server_version", "server_identity"))
    unverified = matched is None or not identity_matches or (
        mode == "persist" and not _persisted_matches_running(v, after)) or (
        mode == "persist_only" and matched is not True)
    already_matched = matched is True and _matches(v, mode, normalized, fresh["before"]) is True
    if unverified:
        result["status"] = "applied_unverified"
    elif already_matched:
        result["status"] = "unchanged"
    elif mode == "reset_persist" and matched is False:
        result["status"] = "unchanged"
    elif after and matched is False and mode != "reset_persist" and _same(got, before) and \
            not _same(before, normalized.get("value")):
        result["status"] = "unchanged"
    else:
        result["status"] = "changed"
        result["adjusted"] = matched is False
    result["message"] = ("Sent %s, but could not verify the complete effect on this server. Read the variable "
                         "and its persisted state again before retrying." % statement if unverified else
                         _change_message(v, fresh, after, matched))
    if already_matched and not unverified:
        result["message"] = "%s is unchanged; the requested value was already set." % name
    if audit is not None:
        audit.complete("unknown" if unverified else "ok" if result["status"] == "changed" or already_matched else "error",
                       outcome=result["status"],
                       message=result["message"], after=after)
    return result

# ---- text output for the CLI ----------------------------------------------------------------


def describe_plan(plan, policy_text=None):
    """Lines that describe a planned change before confirming it (stderr)."""
    v = plan["variable"]
    operation = {"global": "SET GLOBAL", "persist": "SET PERSIST", "persist_only": "SET PERSIST_ONLY",
                 "session": "SET SESSION", "reset_persist": "RESET PERSIST"}[plan["mode"]]
    lines = ["%s on %s %s" % (operation, "MariaDB" if plan["engine"] == "mariadb" else "MySQL",
                              plan["server_version"] or "?"),
             "  %s;" % plan["statement"]]
    if plan["mode"] != "reset_persist":
        lines.append("  %s: %s -> %s" % (plan["name"], _before_text(plan), plan["display"]))
    else:
        lines.append("  persisted value: %s" % _persisted_text(v, plan["before"]["persisted_state"]))
    lines.append("  Effect: %s" % plan["effect"]["text"])
    hint = plan.get("privileges") or {}
    if hint.get("need"):
        state = {True: "your grants include %s" % hint.get("via"), False: "your grants do not seem to include it",
                 None: "not checked"}.get(hint.get("allowed"), "not checked")
        lines.append("  Needs %s (%s)." % (hint["need"], state))
    for caveat in plan.get("caveats") or []:
        lines.append("  Caveat: %s" % caveat)
    if plan.get("undo"):
        lines.append("  Undo: %s" % " ".join(plan["undo"]))
    if plan.get("config_line") and plan["engine"] == "mariadb":
        lines.append("  To keep it after a restart, add under [mysqld]: %s" % plan["config_line"])
    if policy_text:
        lines.append("  " + policy_text)
    for note in v.get("notes") or []:
        lines.append("  Note: %s" % note)
    return lines


def _before_text(plan):
    before = plan["before"]
    if plan["mode"] == "persist_only":
        return _persisted_text(plan["variable"], before["persisted_state"])
    if plan["mode"] == "session":
        return _shown(plan["variable"], before["session"])
    return _shown(plan["variable"], before["global"])


def _persisted_text(v, state):
    if not state.get("read"):
        return "(unreadable)"
    if not state.get("present"):
        return "(not persisted)"
    return "SQL NULL" if state.get("value") is None else _shown(v, state["value"])


def format_change(result):
    """Human text for a variable-change-1.0 result."""
    lines = [result["message"] or result["status"]]
    plan = result.get("plan") or {}
    alternative = plan.get("alternative") if result["status"] == "refused" else None
    if alternative:
        lines.append(alternative["text"])
        flag = {"global": "", "session": " --session", "persist_only": " --persist-only"}.get(alternative.get("mode"))
        if flag is not None:
            lines.append("Run: myflames config set %s %s%s" % (result["name"], plan.get("value") or "VALUE", flag))
        if alternative.get("config_line"):
            lines.append("Option file line: %s" % alternative["config_line"])
    after = result.get("after") or {}
    if after.get("persisted_state", {}).get("present") and result["mode"] in ("persist", "persist_only"):
        lines.append("mysqld-auto.cnf: %s = %s" % (
            result["name"], _persisted_text(plan.get("variable") or {}, after["persisted_state"])))
    return "\n".join(lines) + "\n"


def change_cli(conn, name, value, mode, assume_yes=False, quiet=False, stdin=None, stderr=None, audit=None,
               decision=None):
    """``myflames config set`` / ``reset-persist``: plan, describe, confirm, apply.

    Returns ``(exit code, variable-change-1.0 result)``: 0 changed or already set, 1
    declined, refused (by myflames or the server), denied, not applied or
    ``applied_unverified`` (sent, but the full effect could not be verified).
    Do not automatically retry an unverified write. Bad
    input and connection failures raise; the caller maps them to exit 2.
    Without a terminal it refuses unless *assume_yes*. A Development
    connection confirms only risky changes (*decision* from dbops.decide).
    """
    import sys
    stdin = stdin if stdin is not None else sys.stdin
    stderr = stderr if stderr is not None else sys.stderr
    plan = plan_change(conn, name, value, mode)
    if audit is not None:
        audit.server(plan["engine"], plan["server_version"])
    decision = decision or dbops.decide(dbops.classify(representative_statement(plan["name"], mode)))
    target = {"name": plan["name"], "mode": mode, "requested": plan["requested"]}
    blocked = dbops.gate(decision, confirmed=assume_yes)
    if blocked and blocked[0] == "refused":
        return 1, _unsent(plan, audit, "refused", "read_only", blocked[1], target)
    if plan["status"] != "ready":
        message = "Nothing was sent: %s" % " ".join(p["message"] for p in plan["problems"])
        return 1, _unsent(plan, audit, "refused", "invalid", message, target)
    lines = describe_plan(plan, decision["text"])
    if mode == "session":
        lines.insert(0, "SET SESSION applies only to the one-off connection myflames opens; "
                        "use it to test a value.")
    # CLI automation opts in even when Development policy permits the write.
    problem = dbops.cli_confirm(
        "\n".join(lines), "%s? [y/N] " % ("Remove it" if mode == "reset_persist" else "Change it"),
        assume_yes=assume_yes, quiet=quiet, stdin=stdin, stderr=stderr,
        ask=decision["action"] == "confirm", passwords=(audit.password,) if audit is not None else ())
    if problem:
        return 1, _unsent(plan, audit, problem[0], problem[1], problem[2], plan["expect"])
    result = apply_change(conn, plan, plan["expect"], decision=decision, confirmed=True, audit=audit)
    successful = result["status"] == "changed" or (
        result["status"] == "unchanged" and _matches(plan["variable"], mode,
            normalize_value(plan["variable"], value) if mode != "reset_persist" else {}, result["after"] or {}) is True)
    return (0 if successful else 1), result
