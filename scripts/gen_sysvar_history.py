#!/usr/bin/env python3
"""Write myflames/sysvar_history_mysql.py and myflames/sysvar_history_mariadb.py:
the system variables of every cataloged MySQL and MariaDB release, as running
servers reported them, plus a validation report.

Inputs (this script never opens a network connection):

* Runtime records (``sysvar-runtime-1.0``), one per release, written by the lab
  script scripts/record-sysvar-catalog.py: --runtime DIR holds
  <engine>/<version>.json. Names, scope, type, default, min, max, allowed
  values and (where the server can say) dynamic come only from these records.
  A release without a usable record is listed in SKIPPED, never cataloged.
* Source tags, read from read-only git checkouts with ``git ls-tree`` and
  ``git cat-file --batch`` (never a checkout): --mysql-source and
  --mariadb-source. They add what a running server cannot report: the tag's
  maturity and date, deprecation markers with their substitute (MySQL
  DEPRECATED_VAR/DEPRECATED and Sys_var_deprecated_alias, MariaDB DEPRECATED()
  and PLUGIN_VAR_DEPRECATED), the alias base, and the read-only flag used only
  when a record could not say whether a variable is dynamic. The C++
  declaration reader is scripts/gen_mysql_sysvars.py, imported here.
* Saved official MySQL manual pages ("Server and Status Variables and Options
  Added, Deprecated, or Removed"): --manual DIR holds
  added-deprecated-removed-<series>.html and SAVED.json (when each was saved).
  Only "Deprecated in X.Y.Z" facts for names that running servers report as
  system variables are kept.

Lineage (VERSIONS previous_index, used for history events): versions sort
numerically. Within a series a release follows the previous release of that
series. The first release of a series follows the latest-dated release (tag
date not after its own; tag date = the tagged commit's date, as
``git log -1 --format=%cI TAG``) of the NEAREST earlier series in version
order that has one; if that series has none, the next earlier series. So
8.1.0 follows the 8.0.x tagged before it, 8.4.0 follows 8.3.0, 9.1.0 follows
9.0.1, 26.10.0 follows 26.7.0, MariaDB 11.4.2 follows 11.3.2. The first
cataloged release has None.

Deprecation evidence per release (a sorted tuple, or None): "server" when the
record's deprecated_warning deprecates the variable itself (a warning that
deprecates changing it at runtime, such as MySQL's "Updating 'x' is
deprecated. It will be made read-only in a future release.", or a deprecated
value, such as MariaDB's old_mode flags, is not evidence and is listed in the
report), "source" when the tag carries a marker, "manual" when a saved page
says deprecated in a version of the same series at or before this one, or of
an earlier series tagged no later than this release. replacement: the source
substitute or alias base, then the manual's "use X instead", then the server
warning's "Please use X", whichever first names a variable the records know.

default_note, besides the record's own (null, unknown, host): "state" for the
reviewed STATE_NAMES (session or server state such as pseudo_thread_id and
gtid_executed), "release" for the reviewed RELEASE_NAMES (version, build_id
...), "build" for the reviewed BUILD_NAMES (paths of the build or
installation layout), and "host" for the
reviewed HOST_REVIEW entries (defaults the server computes at startup from the
CPUs, memory or OS limits, checked against a scan of every tag) and, on
MariaDB, for GLOBAL_VALUE_ORIGIN AUTO unless reviewed as not host. `default`
always stays as read. MySQL data types before 9.0 are left unknown: those
servers do not report them. BUILD_ONLY names a variable whose presence depends
on build options. Records of different lab runs (some with the origin field,
some without) are refused unless --allow-mixed-origin is given.

Example (from the repository root):
  python3 scripts/gen_sysvar_history.py \\
      --runtime local/param-versions-2026-10-28/runtime \\
      --manual local/param-versions-2026-10-28/manual \\
      --mysql-source ../mysql-server --mariadb-source ../mariadb-server \\
      --report local/param-versions-2026-10-28/VALIDATION.md
"""
import argparse
import bisect
import datetime
import hashlib
import html
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import gen_mysql_sysvars as reader  # noqa: E402  (the shared C++ declaration reader)

ENGINES = ("mysql", "mariadb")
PRODUCTS = {"mysql": "MySQL", "mariadb": "MariaDB"}
SOURCE_URLS = {"mysql": "https://github.com/mysql/mysql-server",
               "mariadb": "https://github.com/MariaDB/server"}
RUNTIME_SCHEMA = "sysvar-runtime-1.0"
FIELDS = ("scope", "type", "dynamic", "default", "default_note", "min", "max", "values",
          "deprecated", "replacement", "alias_of", "plugin")
NOT_READ = "Not read from a running server yet"


def short_reason(reason):
    """The SKIPPED sentence users see; the lab's full text stays in the
    runtime record and VALIDATION.md."""
    text = str(reason)
    if text.startswith("No image") and "No package" in text:
        checked = re.search(r"\(checked (\d{4}-\d{2}-\d{2})\)", text)
        return "No official image or package of this release could be found%s." % (
            " (checked %s)" % checked.group(1) if checked else "")
    return text if len(text) <= 160 else text[:157].rstrip() + "..."

# Variables that report the release or the build rather than a default
# (reviewed list): default_note "release", `default` kept as read.
RELEASE_NAMES = {
    # performance_schema_error_size: its default is the build's count of error
    # messages (DEFAULT(PFS_MAX_GLOBAL_SERVER_ERRORS) in sql/sys_vars.cc).
    "mysql": frozenset(("build_id", "innodb_version", "performance_schema_error_size", "version",
                        "version_comment", "version_compile_machine", "version_compile_os", "version_compile_zlib",
                        "version_with_update")),
    "mariadb": frozenset(("innodb_version", "version", "version_comment", "version_compile_machine",
                          "version_compile_os", "version_malloc_library", "version_source_revision",
                          "version_ssl_library", "wsrep_patch_version")),
}

# Variables that hold session or server state rather than a default
# (reviewed list): what the lab read is its own session's or server's state
# (its connection id, statement counter, GTID set, clock, UUID), so
# default_note "state" on every release, never compared.
STATE_NAMES = {
    "mysql": frozenset(("error_count", "external_user", "gtid_executed", "gtid_owned", "gtid_purged", "identity",
                        "insert_id", "last_insert_id", "original_commit_timestamp", "original_server_version",
                        "proxy_user", "pseudo_thread_id", "rand_seed1", "rand_seed2", "server_uuid",
                        "statement_id", "timestamp", "warning_count")),
    "mariadb": frozenset(("error_count", "external_user", "gtid_binlog_pos", "gtid_binlog_state",
                          "gtid_current_pos", "gtid_seq_no", "gtid_slave_pos", "identity", "in_transaction",
                          "insert_id", "last_gtid", "last_insert_id", "proxy_user", "pseudo_thread_id",
                          "rand_seed1", "rand_seed2", "server_uid", "timestamp", "warning_count")),
}

# Variables whose default is a path of the build or installation layout
# (reviewed list): default_note "build" on every release, `default` kept as
# read. MySQL's images moved from Debian (8.0.11 to 8.0.28) to Oracle Linux
# (8.0.29 on), which changes socket and plugin_dir with no server change.
BUILD_NAMES = {
    "mysql": frozenset(("basedir", "character_sets_dir", "datadir", "lc_messages_dir", "log_bin_basename",
                        "log_bin_index", "mysqlx_socket", "plugin_dir", "replica_load_tmpdir", "secure_file_priv",
                        "slave_load_tmpdir", "socket", "tmpdir")),
    "mariadb": frozenset(("aria_log_dir_path", "basedir", "character_sets_dir", "datadir", "lc_messages_dir",
                          "plugin_dir", "secure_file_priv", "slave_load_tmpdir", "socket", "tmpdir",
                          "wsrep_data_home_dir")),
}

# Names whose presence in the records depends on build options while the
# source still declares them (reviewed against the source-versus-runtime
# comparison); written to the module as BUILD_ONLY = {name: sentence}.
# sql/sys_vars.cc and sql/auth/sql_authentication.cc up to 5.7.27:
# #if defined(HAVE_OPENSSL) && !defined(HAVE_YASSL); 5.7.28 dropped yaSSL.
_OPENSSL_ONLY = ("Exists only in builds that use OpenSSL: MySQL 5.7 builds with the bundled yaSSL, as the official "
                 "images up to 5.7.27 are, lack it; 5.7.28 dropped yaSSL.")
_SEMISYNC_OLD = ("Belongs to the rpl_semi_sync_master and rpl_semi_sync_slave plugins, which MySQL 8.0.26 "
                 "deprecated in favour of rpl_semi_sync_source and rpl_semi_sync_replica (whose "
                 "rpl_semi_sync_source_* and rpl_semi_sync_replica_* variables replace these) and no longer "
                 "ships from 9.5.0; the lab loaded the new plugins from 8.0.26.")
# plugin/semisync/CMakeLists.txt builds semisync_master and semisync_slave
# (DEPRECATED_SEMISYNC_LIBRARY) from 8.0.26 to 9.4.2 and drops them in 9.5.0.
BUILD_ONLY = {
    "mysql": dict([
        ("innodb_numa_interleave", "Exists only in builds with NUMA support (HAVE_LIBNUMA): the official images of "
                                   "5.7.9, 5.7.10 and 5.7.17 to 8.0.28 have it; those of 5.7.11 to 5.7.16 and the "
                                   "Oracle Linux images from 8.0.29 do not."),
        ("keyring_file_data", "Belongs to the keyring_file plugin, which only the MySQL 5.7.11 build loads at "
                              "startup by default (its early-plugin-load default names keyring_file.so); other "
                              "releases load it only when configured."),
    ] + [(name, _OPENSSL_ONLY) for name in (
        "auto_generate_certs", "sha256_password_auto_generate_rsa_keys", "sha256_password_private_key_path",
        "sha256_password_public_key_path")] + [(name, _SEMISYNC_OLD) for name in (
        "rpl_semi_sync_master_enabled", "rpl_semi_sync_master_timeout", "rpl_semi_sync_master_trace_level",
        "rpl_semi_sync_master_wait_for_slave_count", "rpl_semi_sync_master_wait_no_slave",
        "rpl_semi_sync_master_wait_point", "rpl_semi_sync_slave_enabled", "rpl_semi_sync_slave_trace_level")]),
    "mariadb": {},
}

# Source deprecation evidence the marker scan cannot see (reviewed list):
# (name, first version, replacement or None, evidence). MySQL marks InnoDB
# variables deprecated only in help texts and error-log messages.
SOURCE_DEPRECATIONS = {
    "mysql": (
        ("innodb_log_file_size", "8.0.30", "innodb_redo_log_capacity",
         "ha_innodb.cc help text \"... Deprecated.\"; share/messages_to_error_log.txt "
         "ER_IB_MSG_LOG_PARAMS_LEGACY_USAGE \"Please use innodb_redo_log_capacity instead.\""),
        ("innodb_log_files_in_group", "8.0.30", "innodb_redo_log_capacity",
         "ha_innodb.cc help text \"... Deprecated.\"; share/messages_to_error_log.txt "
         "ER_IB_MSG_LOG_PARAMS_LEGACY_USAGE \"Please use innodb_redo_log_capacity instead.\""),
        ("innodb_undo_tablespaces", "5.7.21", None,
         "ha_innodb.cc help text \"Number of undo tablespaces to use. (deprecated)\""),
    ),
    "mariadb": (),
}

# Scope disagreements between a running server and its source tag that the
# source explains (reviewed list): (name, first version, last version, reason).
SCOPE_REVIEW = {
    "mysql": (
        ("gtid_executed", "5.7.9", "5.7", "5.7 also declares a session value, deprecated (session_value_ptr warns); "
         "the server lists only the global one."),
        ("sql_log_bin", "5.7.9", "5.7", "5.7 declares it with SESSION_VAR, but check_sql_log_bin refuses SET GLOBAL "
         "and the server lists it as session only (8.0 declares SESSION_ONLY)."),
    ),
    "mariadb": (),
}

# Manual deprecations rejected for some releases (reviewed list):
# (name, first version the manual's evidence applies to, reason).
MANUAL_REJECT = {
    "mysql": (
        ("group_replication_communication_stack", "26.7.0",
         "Before 26.7.0 the source deprecates only the value XCOM (plugin.cc push_deprecated_warn("
         "\"group_replication_communication_stack=XCOM\")); 26.7.0 deprecates the variable."),
    ),
    "mariadb": (),
}

_PFS_AUTOSIZED = ("performance_schema_digests_size", "performance_schema_events_stages_history_long_size",
                  "performance_schema_events_stages_history_size",
                  "performance_schema_events_statements_history_long_size",
                  "performance_schema_events_statements_history_size",
                  "performance_schema_events_transactions_history_long_size",
                  "performance_schema_events_transactions_history_size",
                  "performance_schema_events_waits_history_long_size",
                  "performance_schema_events_waits_history_size", "performance_schema_session_connect_attrs_size")
_INNODB_STARTUP = ("storage/innobase/handler/ha_innodb.cc: the declared default, from 9.6.0 (9.5.0 for "
                   "log_writer_threads) update_sysvars_default()")
# Owner decision 2026-10-28: a default lowered only in that edge case keeps its declared
# value in the catalog (unmarked), so a real change of it is still compared.
_OPEN_FILES_EDGE = ("Lowered at startup only when the operating system grants too few open files; "
                    "with normal limits it keeps its declared default.")
_CASE_FS = "The server sets it at startup from whether the data directory's file system ignores letter case."
_POOL_MAX = "storage/innobase/handler/ha_innodb.cc innodb_init_params()"
_POOL_MAX_ZERO = "Declared as 0, which makes the server set it at startup to innodb_buffer_pool_size."
_POOL_MAX_LIMIT = ("The server lowers it at startup to a quarter of the address-space limit (ulimit -v) when one is "
                   "set, and raises it to innodb_buffer_pool_size when that is larger.")

# Defaults the server computes at startup from the host (CPUs, memory, OS
# limits), reviewed against the source scan (fact["host"]) of every tag:
# (name, first version, last version or None, marked, source location, reason).
# A marked entry sets default_note "host" (when the record has no note); an
# unmarked one records a scan hit or an origin AUTO reviewed as not host.
# Values that only follow other settings with all defaults are not marked.
HOST_REVIEW = {
    "mysql": (
        ("open_files_limit", "5.7.9", None, True, "sql/mysqld.cc adjust_open_files_limit()",
         "The server sets it at startup to the number of open files the operating system grants."),
        ("max_connections", "5.7.9", None, False, "sql/mysqld.cc adjust_max_connections()", _OPEN_FILES_EDGE),
        ("table_open_cache", "5.7.9", None, False, "sql/mysqld.cc adjust_table_cache_size()", _OPEN_FILES_EDGE),
        ("table_definition_cache", "5.7.9", None, False, "sql/mysqld.cc adjust_table_def_size()",
         "The server sets it at startup to 400 + table_open_cache / 2, at most 2000, which is fixed while "
         "table_open_cache keeps its default."),
        ("lower_case_file_system", "5.7.9", None, True,
         "sql/mysqld.cc lower_case_file_system = test_if_case_insensitive(mysql_real_data_home)", _CASE_FS),
    ) + tuple((name, "5.7.9", None, False, "sql/sys_vars.cc DEFAULT(PFS_AUTOSIZE_VALUE), storage/perfschema/"
               "pfs_autosize.cc", "Performance Schema sizes it at startup from max_connections, table_open_cache "
               "and table_definition_cache, which is fixed while those keep their defaults.")
              for name in _PFS_AUTOSIZED) + (
        ("innodb_purge_threads", "8.4.0", None, True, _INNODB_STARTUP,
         "The server sets it at startup to 1 when it sees 16 CPUs or fewer, else 4."),
        ("innodb_parallel_read_threads", "8.4.0", None, True, _INNODB_STARTUP,
         "The server sets it at startup to the number of CPUs divided by 8, at least 4."),
        ("innodb_read_io_threads", "8.4.0", None, True, _INNODB_STARTUP,
         "The server sets it at startup to the number of CPUs divided by 2, between 4 and 64."),
        ("innodb_log_writer_threads", "9.1.0", "9.4", True, _INNODB_STARTUP,
         "The server sets it at startup to ON with 32 CPUs or more, else OFF."),
        ("innodb_log_writer_threads", "9.5.0", None, True, _INNODB_STARTUP,
         "The server sets it at startup to ON with 32 CPUs or more when binary logging is on (the default), "
         "or with more than 4 CPUs when it is off."),
        ("innodb_flush_method", "8.4.0", None, True,
         "storage/innobase/handler/ha_innodb.cc innodb_flush_method = os_is_o_direct_supported() ? ...",
         "The server sets it at startup to O_DIRECT when the data directory's file system supports it, "
         "else fsync."),
        ("innodb_buffer_pool_instances", "5.7.9", "8.3", False,
         "storage/innobase/srv/srv0start.cc (5.7), storage/innobase/handler/ha_innodb.cc "
         "innodb_buffer_pool_size_init() (8.0 to 8.3)",
         "The server sets it at startup to 8 when innodb_buffer_pool_size is 1 GiB or more, else 1."),
        ("innodb_buffer_pool_instances", "8.4.0", None, False,
         "storage/innobase/handler/ha_innodb.cc innodb_buffer_pool_size_init()",
         "The server sets it at startup to 1, or, when innodb_buffer_pool_size is 1 GiB or more, to the smaller "
         "of the pool size divided by twice the chunk size and the number of CPUs divided by 4, between 1 and 64."),
        ("innodb_page_cleaners", "5.7.9", "8.3", False,
         "storage/innobase/srv/srv0start.cc (5.7), storage/innobase/handler/ha_innodb.cc innodb_init_params() "
         "(8.0 to 8.3)",
         "Declared as 4; the server lowers it at startup to innodb_buffer_pool_instances when that is smaller."),
        ("innodb_page_cleaners", "8.4.0", None, False, "storage/innobase/handler/ha_innodb.cc innodb_init_params()",
         "The server sets it at startup to innodb_buffer_pool_instances, also when a larger value is given."),
        ("temptable_max_ram", "8.4.0", "9.5.2", True, "sql/sys_vars.cc: the declared default",
         "The server sets it at startup to 3% of physical memory, between 1 GiB and 4 GiB."),
        ("temptable_max_ram", "9.6.0", None, False, "sql/sys_vars.cc update_temptable_max_ram_default()",
         "Declared as 1 GiB; the server sets it to 3% of server_memory, between 1 GiB and 4 GiB, only when "
         "server_memory is set."),
    ),
    "mariadb": (
        ("open_files_limit", "10.6.3", None, True,
         "sql/mysqld.cc SYSVAR_AUTOSIZE_IF_CHANGED(open_files_limit) after my_set_max_open_files()",
         "The server sets it at startup to the number of open files the operating system grants."),
        ("max_connections", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE_IF_CHANGED(max_connections)",
         _OPEN_FILES_EDGE),
        ("table_open_cache", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE_IF_CHANGED(tc_size)",
         _OPEN_FILES_EDGE),
        ("table_open_cache_instances", "10.6.3", None, False,
         "sql/mysqld.cc SYSVAR_AUTOSIZE_IF_CHANGED(tc_instances)", _OPEN_FILES_EDGE),
        ("thread_pool_size", "10.6.3", None, True, "sql/mysqld.cc SYSVAR_AUTOSIZE(threadpool_size, my_getncpus())",
         "The server sets it at startup to the number of CPUs."),
        ("log_tc_size", "10.6.3", None, True, "sql/sys_vars.cc DEFAULT(my_getpagesize() * 6)",
         "The declared default is six pages of the operating system's memory page size."),
        ("lower_case_file_system", "10.6.3", None, True,
         "sql/mysqld.cc SYSVAR_AUTOSIZE(lower_case_file_system, test_if_case_insensitive(...))", _CASE_FS),
        ("innodb_io_capacity_max", "10.6.3", None, True,
         "storage/innobase/handler/ha_innodb.cc: SRV_MAX_IO_CAPACITY_DUMMY_DEFAULT replaced at startup",
         "The declared default is a placeholder; at startup the server sets 2 x innodb_io_capacity, at least 2000."),
        ("innodb_buffer_pool_size_max", "10.11.12", "10.11.16", False, _POOL_MAX, _POOL_MAX_ZERO),
        ("innodb_buffer_pool_size_max", "10.11.17", "10.11", False, _POOL_MAX, _POOL_MAX_LIMIT),
        ("innodb_buffer_pool_size_max", "11.4.6", "11.4.10", False, _POOL_MAX, _POOL_MAX_ZERO),
        ("innodb_buffer_pool_size_max", "11.4.11", "11.4", False, _POOL_MAX, _POOL_MAX_LIMIT),
        ("innodb_buffer_pool_size_max", "11.8.2", "11.8.6", False, _POOL_MAX, _POOL_MAX_ZERO),
        ("innodb_buffer_pool_size_max", "11.8.7", "11.8", False, _POOL_MAX, _POOL_MAX_LIMIT),
        ("innodb_buffer_pool_size_max", "12.0.1", "12.2", False, _POOL_MAX, _POOL_MAX_ZERO),
        ("innodb_buffer_pool_size_max", "12.3.2", None, False, _POOL_MAX, _POOL_MAX_LIMIT),
        ("innodb_log_write_ahead_size", "10.11.9", None, False,
         "storage/innobase/include/log0log.h log_t::set_block_size() from os_file (the def_val update in "
         "ha_innodb.cc is compiled out with #if 0)",
         "The server raises the value at startup to the redo log device's block size when that is larger; "
         "the reported default stays 512."),
        ("port", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE(mysqld_port)",
         "The server takes it at startup from the build's port (3306) or the system's services entry for mysql."),
        ("report_port", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE(report_port, mysqld_port)",
         "The server sets it at startup to port."),
        ("character_sets_dir", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE(charsets_dir)",
         "The server sets it at startup to the character set directory of the installation."),
        ("slave_load_tmpdir", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE(slave_load_tmpdir, mysql_tmpdir)",
         "The server sets it at startup to tmpdir."),
        ("max_relay_log_size", "10.6.3", None, False,
         "sql/mysqld.cc SYSVAR_AUTOSIZE(max_relay_log_size, max_binlog_size)",
         "The server sets it at startup to max_binlog_size while it is 0."),
        ("table_definition_cache", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE(tdc_size)",
         "The server autosizes it at startup to 400 + table_definition_cache / 2, at most 2000 (600 with the "
         "declared 400)."),
        ("thread_cache_size", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE(thread_cache_size)",
         "The server sets it at startup from max_connections."),
        ("back_log", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE(back_log)",
         "The server sets it at startup to 50 + max_connections / 5, at most 900."),
        ("host_cache_size", "10.6.3", None, False, "sql/mysqld.cc SYSVAR_AUTOSIZE(host_cache_size)",
         "The server sets it at startup from max_connections."),
    ),
}

# Which builds the values come from (GENERATED["builds"]).
BUILDS = {
    "mysql": ("Values come from Oracle's official MySQL Docker images%s; other builds can compile other "
              "defaults."),
    "mariadb": ("Values come from MariaDB's own Ubuntu packages, as in the official MariaDB Docker image%s; "
                "other builds can compile other defaults."),
}
PACKAGES = {"mysql": "Oracle's generic Linux packages", "mariadb": "MariaDB's generic Linux packages"}

# Plugin files the MySQL reader (gen_mysql_sysvars.parse_sources) does not
# cover, and the prefix their variables get: the clone plugin the lab loads,
# and the built-in sha256_password and ngram full-text parser plugins.
MYSQL_EXTRA_PLUGINS = (("plugin/clone/", "clone_"), ("sql/auth/sql_authentication.cc", "sha256_password_"),
                       ("plugin/fulltext/ngram_parser/", "ngram_"),
                       # 5.7: Group Replication and X Plugin under rapid/, the
                       # validate_password plugin (the component came in 8.0).
                       ("rapid/plugin/group_replication/", "group_replication_"),
                       ("rapid/plugin/x/", "mysqlx_"), ("plugin/password_validation/", "validate_password_"),
                       # keyring_file: loaded by default only in 5.7.11 (early-plugin-load).
                       ("plugin/keyring/", "keyring_file_"))

# MariaDB files that declare system variables, with the plugin prefix of
# their MYSQL_SYSVAR/MYSQL_THDVAR declarations ("" = sql/sys_vars.cc core).
# Built-in plugins (InnoDB, Aria, MyISAM, binlog, mhnsw) and the optional
# plugins the lab loads (server_audit, simple_password_check, sql_errlog,
# query_response_time) plus feedback.
MARIADB_PLUGINS = (
    ("storage/innobase/", "innodb_"),
    ("storage/maria/ha_maria.cc", "aria_"),
    ("storage/myisam/ha_myisam.cc", "myisam_"),
    ("sql/log.cc", "binlog_"),
    ("sql/vector_mhnsw.cc", "mhnsw_"),
    ("plugin/server_audit/", "server_audit_"),
    ("plugin/simple_password_check/", "simple_password_check_"),
    ("plugin/sql_errlog/", "sql_error_log_"),
    ("plugin/query_response_time/", "query_response_time_"),
    ("plugin/feedback/", "feedback_"),
)
MARIADB_CORE = ("sql/sys_vars.cc", "sql/sys_vars.ic", "sql/sys_vars.inl")
# MySQL files with core Sys_var declarations (every in-scope tag checked): sys_vars.cc,
# and the TLS variables (ssl_*, tls_*, admin_ssl_*, admin_tls_*) in
# ssl_acceptor_context.cc (8.0.16 to 8.0.20), then ssl_init_callback.cc.
MYSQL_CORE_FILES = ("sql/sys_vars.cc", "sql/ssl_acceptor_context.cc", "sql/ssl_init_callback.cc")
_MACRO = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)\(([^)]*)\)[ \t]*([^\n]*)$", re.M)
# Startup sizing lives in sql/mysqld.cc (read for host evidence only).
MYSQLD = "sql/mysqld.cc"
# Calls that read the host: CPUs, physical memory, the OS open-files limit.
_HOST_CALL = re.compile(r"\b(?:std::thread::hardware_concurrency|hardware_concurrency|my_physical_memory|my_num_vcpus|"
                        r"get_sys_mem|my_getncpus|my_getphysmem|num_online_cpus|my_set_max_open_files|"
                        r"my_getpagesize|test_if_case_insensitive|os_is_o_direct_supported)\s*\(|"
                        r"\b_SC_(?:NPROCESSORS_\w+|PHYS_PAGES)\b")
_ASSIGN = re.compile(r"\b([A-Za-z_][\w.]*)\s*=(?!=)\s*([^;{}]*);")
_DEF_VAL = re.compile(r"\bmysql_sysvar_(\w+)\.def_val\s*=\s*([^;]*);")
_IF_NOT_SET = re.compile(r"\bif\s*\(\s*!\s*innodb_variable_is_set\(\s*\"(\w+)\"\s*\)\s*\)\s*\{")
# The same through a wrapper, `if (!innodb_flush_method_is_set() ...) {` (8.4 on).
_IF_NOT_SET_WRAPPER = re.compile(r"\bif\s*\(\s*!\s*innodb_(\w+)_is_set\(\s*\)[^{};]*\{")
# MariaDB help texts that name the variable an alias stands for.
# A declared placeholder default the server replaces at startup (MariaDB
# innodb_io_capacity_max: SRV_MAX_IO_CAPACITY_DUMMY_DEFAULT).
_PLACEHOLDER = re.compile(r"\bif\s*\(\s*(\w+)\s*==\s*(\w*DUMMY_DEFAULT)\s*\)")
_ALIAS_HELP = re.compile(r"\b(?:[Aa]lias|[Ss]ynonym) (?:for|of) (?:the )?(?:@@)?([a-z_][a-z0-9_]*)")
_AUTOSIZE = re.compile(r"\bSYSVAR_AUTOSIZE(?:_IF_CHANGED)?\s*\(")
_VAR_ARG = re.compile(r"\b(?:GLOBAL_VAR|SESSION_VAR|SESSION_ONLY)\s*\(\s*([\w.]+)\s*\)")
# mysqld.cc functions that size variables at startup from the open-files limit.
_ADJUST = (("adjust_open_files_limit", "open_files_limit"), ("adjust_table_cache_size", "table_open_cache"),
           ("adjust_table_def_size", "table_definition_cache"), ("adjust_max_connections", "max_connections"))
_MARIADB_PLUGIN = re.compile(r"\bMYSQL_(SYSVAR|THDVAR)_(BOOL|INT|UINT|LONG|ULONG|LONGLONG|ULONGLONG|"
                             r"UINT64_T|SIZE_T|DOUBLE|STR|CONST_STR|ENUM|SET)\s*\(")
# Every form in the in-scope tags: MySQL DEPRECATED("x") (8.0.11 to 8.0.2x) and
# DEPRECATED_VAR("x"); MariaDB DEPRECATED("x") (10.6 to 11.3), DEPRECATED(V, "x")
# and DEPRECATED_NO_REPLACEMENT(V) (11.5 on).
_DEPRECATED = re.compile(r"^\s*DEPRECATED(_VAR|_NO_REPLACEMENT)?\s*\(")
_DIRECTIVE = re.compile(r"^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|else|endif)\b([^\n]*)", re.M)
_GUARD = re.compile(r"^!defined\(\w*(?:_H|_H_|_INCLUDED|_IC|_INL)\)$")
_DEBUG = re.compile(r"!defined\((?:NDEBUG|DBUG_OFF)\)|\b(?:\w*_DEBUG\w*|DEBUG\w*|ENABLED_DEBUG_SYNC|SAFE_MUTEX)\b")
# Conditions every server build (the lab images) meets: they explain nothing.
_SERVER_BUILD = frozenset((
    "!defined(UNIV_HOTBACKUP)", "!defined(EMBEDDED_LIBRARY)", "defined(HAVE_REPLICATION)",
    "defined(WITH_PERFSCHEMA_STORAGE_ENGINE)", "defined(WITH_WSREP)",
    "defined(USE_OLD_SEMI_SYNC_TERMINOLOGY)", "else of defined(USE_OLD_SEMI_SYNC_TERMINOLOGY)"))
_MEMO = {}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")
_WARNING_USE = re.compile(r"\b[Pp]lease use\s+(?:'?@@)?'?([A-Za-z_][A-Za-z0-9_.]*)")
_MANUAL_USE = re.compile(r"\b[Uu]se\s+([A-Za-z_][A-Za-z0-9_.]*)\s+instead\b")


# ---------------------------------------------------------------------------
# Versions, tags and lineage


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def series_of(version):
    return ".".join(version.split(".")[:2])


def _fields(text):
    return {key: value.strip() for key, value in re.findall(r"^[ \t]*(\w+)[ \t]*=[ \t]*([^\n]*)$", text, re.M)}


def mysql_release(tag, text):
    """(version, maturity) of a MySQL tag in scope, else None.

    Scope: plain numeric tags (a -respin tag stands for its version) of the
    5.7 series and from 8.0.11 whose MYSQL_VERSION (VERSION in 5.7 and early
    8.0 tags) has no extra suffix (no -m, -dmr or -rc: 5.7.9 is the first 5.7
    GA). Maturity is MYSQL_VERSION_MATURITY when the file has it (LTS,
    INNOVATION); 5.7 and 8.0.x tags without it are GA, and 8.1 to 8.3 tags
    without it are INNOVATION (Oracle published them as Innovation releases;
    the field arrived in 8.4.0).
    """
    match = re.fullmatch(r"mysql-(\d+)\.(\d+)\.(\d+)(?:-respin)?", tag)
    if not match or text is None:
        return None
    fields = _fields(text)
    numbers = tuple(int(part) for part in match.groups())
    declared = tuple(fields.get("MYSQL_VERSION_" + part, "").strip() for part in ("MAJOR", "MINOR", "PATCH"))
    if declared != tuple(str(number) for number in numbers):
        return None
    if numbers[:2] != (5, 7) and numbers < (8, 0, 11):
        return None
    if fields.get("MYSQL_VERSION_EXTRA", "").strip('"'):
        return None
    maturity = fields.get("MYSQL_VERSION_MATURITY", "").strip('"').upper()
    if not maturity:
        # MYSQL_VERSION_MATURITY arrived in 8.4.0; Oracle published 8.1.0,
        # 8.2.0 and 8.3.0 as Innovation releases although their tags lack it.
        maturity = "INNOVATION" if (8, 1) <= numbers[:2] <= (8, 3) else "GA"
    if maturity not in ("GA", "LTS", "INNOVATION"):
        return None
    return "%d.%d.%d" % numbers, maturity


def mariadb_release(tag, text):
    """(version, "stable") of a MariaDB tag in scope (plain numeric, 10.6 and
    later, VERSION says SERVER_MATURITY=stable), else None."""
    match = re.fullmatch(r"mariadb-(\d+)\.(\d+)\.(\d+)", tag)
    if not match or text is None:
        return None
    fields = _fields(text)
    numbers = tuple(int(part) for part in match.groups())
    declared = tuple(fields.get("MYSQL_VERSION_" + part, "").strip() for part in ("MAJOR", "MINOR", "PATCH"))
    if declared != tuple(str(number) for number in numbers) or numbers < (10, 6, 0):
        return None
    if fields.get("SERVER_MATURITY", "").strip('"').lower() != "stable":
        return None
    return "%d.%d.%d" % numbers, "stable"


def releases_from(engine, files, dates):
    """In-scope releases from {tag: version file text} and {tag: ISO date}.

    Returns [{"version", "tag", "maturity", "date"}] in numeric order; a
    -respin tag replaces the plain tag of its version.
    """
    judge = mysql_release if engine == "mysql" else mariadb_release
    found = {}
    for tag in sorted(files):
        result = judge(tag, files[tag])
        if result is None or tag not in dates:
            continue
        version, maturity = result
        if version in found and not tag.endswith("-respin"):
            continue
        found[version] = {"version": version, "tag": tag, "maturity": maturity, "date": dates[tag]}
    return [found[version] for version in sorted(found, key=version_key)]


def parse_date(text):
    """An aware datetime from git's ISO 8601 output (Python 3.7 has no 'Z')."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    value = datetime.datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


def lineage(releases):
    """previous_index for each release (same order as *releases*, numeric).

    Within a series, the previous release of that series. The first release
    of a series follows the latest-dated release (tag date not after its
    own) of the NEAREST earlier series in version order that has one; when
    that series has none dated before it, the next earlier series is tried,
    and so on (ties on the date: the higher version). So on the real tags
    8.1.0 follows the 8.0.x tagged before it, 8.4.0 follows 8.3.0, 9.1.0
    follows 9.0.1, 26.10.0 follows 26.7.0, MariaDB 11.4.2 follows 11.3.2 and
    12.0.2 follows the 11.8.x tagged before it. The first release, or a
    first-of-series release with no earlier release dated before it, has
    None.
    """
    result = []
    dates = [parse_date(release["date"]) for release in releases]
    by_series = {}
    for index, release in enumerate(releases):
        by_series.setdefault(series_of(release["version"]), []).append(index)
    for index, release in enumerate(releases):
        series = series_of(release["version"])
        own = [other for other in by_series[series]
               if version_key(releases[other]["version"]) < version_key(release["version"])]
        if own:
            result.append(max(own, key=lambda other: version_key(releases[other]["version"])))
            continue
        previous = None
        earlier = sorted((name for name in by_series if version_key(name) < version_key(series)),
                         key=version_key, reverse=True)
        for name in earlier:
            dated = [other for other in by_series[name] if dates[other] <= dates[index]]
            if dated:
                previous = max(dated, key=lambda other: (dates[other], version_key(releases[other]["version"])))
                break
        result.append(previous)
    return result


# ---------------------------------------------------------------------------
# Git access (read-only)


class Git(object):
    """Read-only access to one checkout: tags, tag dates, trees and blobs.

    Blob texts are cached by object id, so a file that did not change between
    tags is read once.
    """

    def __init__(self, repo):
        self.repo = str(repo)
        self._blobs = {}

    def _run(self, args, data=None):
        return subprocess.check_output(["git", "-C", self.repo] + list(args), input=data)

    def tag_dates(self, pattern):
        """{tag: committer date of the tagged commit}, like git log -1 --format=%cI TAG."""
        out = self._run(["for-each-ref", "--format=%(refname:short)%09%(*committerdate:iso-strict)%09"
                         "%(committerdate:iso-strict)", "refs/tags/" + pattern]).decode("utf-8")
        dates = {}
        for line in out.splitlines():
            tag, peeled, direct = (line.split("\t") + ["", ""])[:3]
            if peeled or direct:
                dates[tag] = peeled or direct
        return dates

    def batch(self, specs):
        """{spec: bytes or None} for "<rev>:<path>" or blob ids, one git process."""
        specs = list(specs)
        if not specs:
            return {}
        out = self._run(["cat-file", "--batch"], ("".join(spec + "\n" for spec in specs)).encode("utf-8"))
        result, offset = {}, 0
        for spec in specs:
            end = out.index(b"\n", offset)
            header = out[offset:end].split()
            if len(header) < 3 or header[-1] == b"missing":
                result[spec] = None
                offset = end + 1
                continue
            size = int(header[-1])
            result[spec] = out[end + 1:end + 1 + size]
            offset = end + size + 2
        return result

    def tree(self, tag, roots):
        """{path: blob id} of the files under *roots* at *tag*."""
        out = self._run(["ls-tree", "-r", tag, "--"] + [root.rstrip("/") for root in roots]).decode("utf-8")
        files = {}
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                files[path] = parts[2]
        return files

    def texts(self, files):
        """{path: text} for {path: blob id}, reading only blobs not seen before."""
        missing = sorted({blob for blob in files.values() if blob not in self._blobs})
        for blob, data in self.batch(missing).items():
            self._blobs[blob] = data.decode("utf-8", errors="replace") if data is not None else ""
        return {path: self._blobs[blob] for path, blob in files.items()}


def scan_releases(engine, git):
    """In-scope releases of *engine* read from tags of the checkout."""
    prefix = "mysql-" if engine == "mysql" else "mariadb-"
    dates = git.tag_dates(prefix + "*")
    pattern = r"mysql-\d+\.\d+\.\d+(?:-respin)?" if engine == "mysql" else r"mariadb-\d+\.\d+\.\d+"
    tags = sorted(tag for tag in dates if re.fullmatch(pattern, tag))
    names = ("MYSQL_VERSION", "VERSION") if engine == "mysql" else ("VERSION",)
    blobs = git.batch("%s:%s" % (tag, name) for tag in tags for name in names)
    files = {}
    for tag in tags:
        for name in names:
            data = blobs.get("%s:%s" % (tag, name))
            if data is not None:
                files[tag] = data.decode("utf-8", errors="replace")
                break
    return releases_from(engine, files, dates)


# ---------------------------------------------------------------------------
# Source facts


def condition_points(text):
    """Change points [(offset, conditions)] of the #if nesting in *text*."""
    stack, points = [], [(0, ())]
    for match in _DIRECTIVE.finditer(text):
        directive, rest = match.group(1), " ".join(match.group(2).split())
        if directive == "ifdef":
            stack.append("defined(%s)" % rest)
        elif directive == "ifndef":
            stack.append("!defined(%s)" % rest)
        elif directive == "if":
            stack.append(rest)
        elif directive == "elif" and stack:
            stack[-1] = stack[-1] + " elif " + rest
        elif directive == "else" and stack:
            stack[-1] = "else of " + stack[-1]
        elif directive == "endif" and stack:
            stack.pop()
        points.append((match.end(), tuple(c for c in stack if not _GUARD.match(c))))
    return points


def conditions_at(points, offset):
    index = bisect.bisect_right([point[0] for point in points], offset) - 1
    return points[max(index, 0)][1]


def condition_kind(conditions):
    """"" for an unconditional declaration, else "debug" or "build"."""
    conditions = [condition for condition in conditions if condition not in _SERVER_BUILD]
    if not conditions:
        return ""
    if any(_DEBUG.search(condition) and not condition.startswith("else of") for condition in conditions):
        return "debug"
    return "build"


def merge_kind(old, new):
    """A name declared once unconditionally is unconditional; debug only (or
    hidden) when every declaration is."""
    if old is None:
        return new
    if "" in (old, new):
        return ""
    return old if old == new else "build"


def clean_name(text):
    """A variable name from a substitute text such as "'@@old_mode'" or
    "--log_error_services" or "'@@wsrep_sync_wait=1'"; None otherwise."""
    if text is None:
        return None
    value = text.strip().strip("'\"` ").replace("@@", "").lstrip("-").strip("'\"` ")
    value = value.split("=", 1)[0].strip()
    if value.startswith(("global.", "session.")):
        value = value.split(".", 1)[1]
    return value if _NAME.fullmatch(value) else None


def _deprecation(args):
    """(True, substitute or None) when a declaration's arguments carry
    DEPRECATED(...)/DEPRECATED_VAR(...), else (False, None)."""
    for arg in args:
        match = _DEPRECATED.match(arg)
        if not match:
            continue
        if match.group(1) == "_NO_REPLACEMENT":
            return True, ""
        inner, _ = reader.arguments(arg, match.end())
        if not inner:
            return True, None
        value = reader.string(inner[-1])
        if value is None and inner[-1].strip() in ("0", "nullptr", "NULL", "NullS"):
            return False, None
        return True, value
    return False, None


def _fact(scope, ro, deprecated, substitute, alias_of, group, kind):
    return {"scope": scope, "ro": ro, "deprecated": deprecated, "substitute": substitute,
            "alias_of": alias_of, "group": group, "kind": kind}


def _merge_fact(facts, name, fact):
    old = facts.get(name)
    if old is None:
        facts[name] = fact
        return
    for key in ("scope", "ro", "substitute", "alias_of"):
        if old[key] != fact[key]:
            old[key] = old[key] if fact[key] is None else (fact[key] if old[key] is None else None)
    old["deprecated"] = old["deprecated"] or fact["deprecated"]
    old["kind"] = merge_kind(old["kind"], fact["kind"])


def _ro(flags):
    if flags is None:
        return None
    return "ro" in flags.split()


def _clean(text):
    """reader.uncomment then reader.expand_wrappers, memoized by text: most
    files are identical from one tag to the next."""
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if key not in _MEMO:
        _MEMO[key] = reader.expand_wrappers(reader.uncomment(text))
    return _MEMO[key]


def _prepared(sources):
    sources = reader.semisync_variants({path: reader.uncomment(text) if path.startswith("plugin/semisync/")
                                        else text for path, text in sources.items()})
    return {path: reader.expand_wrappers(text) if path.startswith("plugin/semisync/") else _clean(text)
            for path, text in sources.items()}


def _define_names(sources):
    return {m[1]: reader.string(m[2]) for text in sources.values()
            for m in re.finditer(r"^\s*#define\s+(\w+)\s+(\"[^\n]+)", text, re.M)}


def plugin_macros(texts, pattern):
    """{macro: (parameters, body)} for multi-parameter #define wrappers whose
    body declares a plugin variable (MySQL 26.x InnoDB ATOMIC_SYSVAR_ULONG).
    The shared reader's expand_wrappers handles one-parameter wrappers."""
    macros = {}
    for text in texts:
        for match in _MACRO.finditer(text):
            params = [param.strip() for param in match.group(2).split(",")]
            if len(params) > 1 and pattern.search(match.group(3)):
                macros[match.group(1)] = (params, match.group(3))
    return macros


def expand_macros(text, macros):
    """Replace invocations of *macros* by their bodies (parameters substituted,
    ## pasted) and drop their #define lines."""
    if not macros:
        return text
    text = _MACRO.sub(lambda match: "" if match.group(1) in macros else match.group(), text)
    for name, (params, body) in sorted(macros.items()):
        parts, pos = [], 0
        for match in re.finditer(r"\b%s\s*\(" % re.escape(name), text):
            if match.start() < pos:
                continue
            try:
                args, end = reader.arguments(text, match.end())
            except ValueError:
                continue
            values = dict(zip(params, args))
            expanded = re.sub(r"\b\w+\b", lambda token: values.get(token.group(), token.group()), body)
            parts += [text[pos:match.start()], re.sub(r"\s*##\s*", "", expanded)]
            pos = end
        text = "".join(parts) + text[pos:]
    return text


def _plugin_declarations(text, prefix, pattern):
    """[(name, scope, ro, deprecated, offset, experimental)] of MYSQL_SYSVAR/THDVAR
    declarations. PLUGIN_VAR_EXPERIMENTAL variables (MySQL 8.0) are hidden in
    release builds."""
    found = []
    for match in pattern.finditer(text):
        args, _ = reader.arguments(text, match.end())
        name = args[0].strip()
        if not re.fullmatch(r"\w+", name):
            continue
        flags = args[1 if match[1] == "THDVAR" else 2] if len(args) > 2 else ""
        if "PLUGIN_VAR_NOSYSVAR" in flags:
            continue
        found.append((prefix + name, "both" if match[1] == "THDVAR" else "global",
                      "PLUGIN_VAR_READONLY" in flags, "PLUGIN_VAR_DEPRECATED" in flags, match.start(),
                      "PLUGIN_VAR_EXPERIMENTAL" in flags))
    return found


def _prefix_for(path, table):
    best = None
    for root, prefix in table:
        if path == root or (root.endswith("/") and path.startswith(root)):
            if best is None or len(root) > len(best[0]):
                best = (root, prefix)
    return best[1] if best else None


def _blank_strings(text):
    """*text* with the contents of C string literals blanked (same length)."""
    return re.sub(r'"(?:\\.|[^"\\\n])*"', lambda match: '"' + " " * (len(match.group()) - 2) + '"', text)


def _host_tokens(text):
    """Identifiers assigned from a host call anywhere in *text* (num_vcpus,
    files, server_mem): a file-wide approximation, reviewed by hand."""
    text = _blank_strings(text)
    tokens = set()
    for match in _HOST_CALL.finditer(text):
        # A statement starts after the last ';' or a brace that ends its line
        # (brace initializers such as ulonglong{...} stay inside the statement).
        window = text[max(0, match.start() - 2000):match.start()]
        ends = [end.end() for end in re.finditer(r";|[{}][ \t]*\n", window)]
        assigned = re.search(r"\b(\w+)\s*=(?!=)[^=;]*$", window[ends[-1] if ends else 0:])
        if assigned and assigned.group(1) != "def_val":
            tokens.add(assigned.group(1))
    return tokens


def _uses_host(expression, tokens):
    expression = _blank_strings(expression)
    match = _HOST_CALL.search(expression)
    if match:
        return match.group(0).rstrip("( ")
    for token in sorted(tokens):
        if re.search(r"\b%s\b" % re.escape(token), expression):
            return token
    return None


def _add_host(facts, name, evidence):
    fact = facts.get(name)
    if fact is None:
        return
    found = set(fact.get("host") or ())
    found.add(evidence)
    fact["host"] = sorted(found)


def startup_assignments(path, text, cvars, facts):
    """Host evidence for `variable = <host call or token>;` statements in
    *text* (sql/mysqld.cc) whose variable a core declaration names."""
    tokens = _host_tokens(text)
    for match in _ASSIGN.finditer(text):
        name = cvars.get(match.group(1))
        if name is None:
            continue
        used = _uses_host(match.group(2), tokens)
        if used:
            _add_host(facts, name, "%s: set at startup from %s" % (path, used))


def host_evidence(path, text, prefix, facts):
    """Source evidence that a variable's default is computed at startup from
    the host, added to facts[name]["host"] as "path: how" strings:
    a declared default that calls a host function; an InnoDB def_val set at
    startup from one; an InnoDB `if (!innodb_variable_is_set("x")) {...}`
    block that reads one. The reviewed lists decide what is marked."""
    tokens = _host_tokens(text)
    if prefix is not None:
        for match in _DEF_VAL.finditer(text):
            used = _uses_host(match.group(2), tokens)
            if used:
                _add_host(facts, prefix + match.group(1), "%s: def_val set at startup from %s" % (path, used))
    blocks = [(match.group(1), match.end()) for match in _IF_NOT_SET.finditer(text)]
    blocks += [("innodb_" + match.group(1), match.end()) for match in _IF_NOT_SET_WRAPPER.finditer(text)]
    for name, end in blocks:
        try:
            body, _ = reader.arguments(text, end, "}")
        except ValueError:
            continue
        used = _uses_host(",".join(body), tokens)
        if used:
            _add_host(facts, name, "%s: set at startup when not given, from %s" % (path, used))


def mysql_source_facts(sources):
    """{name: fact} from a {repository path: C++ text} MySQL snapshot.

    fact: scope, ro (read-only flag or None), deprecated (a DEPRECATED_VAR or
    DEPRECATED marker, or a Sys_var_deprecated_alias), substitute (the
    marker's text or the alias base), alias_of, group ("core" or the plugin
    prefix without "_"), kind ("" unconditional, "debug", "build").
    """
    mysqld = sources.get(MYSQLD)
    sources = {path: text for path, text in sources.items() if path != MYSQLD}
    catalog = reader.parse_sources(sources)
    prepared = _prepared(sources)
    names = _define_names(prepared)
    constructors = reader.constructor_flags(prepared.get("sql/sys_vars.h", ""))
    macros = plugin_macros(prepared.values(), reader._PLUGIN)
    facts, symbols, aliases, host, updates, cvars = {}, {}, [], [], [], {}
    for path, text in sorted(prepared.items()):
        text = expand_macros(text, macros)
        points = condition_points(text)
        if path in MYSQL_CORE_FILES:
            for match in reader._CORE.finditer(text):
                args, _ = reader.arguments(text, match.end(), "}" if match["opener"] == "{" else ")")
                name = reader.string(args[0]) or names.get(args[0])
                if name is None:
                    continue
                symbols[match["symbol"]] = name
                kind = condition_kind(conditions_at(points, match.start()))
                if match["kind"] == "deprecated_alias" and len(args) > 1:
                    aliases.append((name, args[1].strip(), kind))
                    continue
                deprecated, substitute = _deprecation(args)
                if len(args) > 2 and re.search(r"\bNOT_VISIBLE\b", args[2]):
                    kind = "hidden"
                entry = catalog.get(name)
                if entry is None:
                    # Outside sys_vars.cc the shared reader does not look.
                    scope, _, flags = reader.core_facts(match["kind"], args, constructors)
                    entry = (scope, None, flags)
                _merge_fact(facts, name, _fact(entry[0], _ro(entry[2]), deprecated, substitute, None, "core",
                                               kind))
                variable = _VAR_ARG.search(args[2]) if len(args) > 2 else None
                if variable and "SESSION" not in args[2]:
                    cvars.setdefault(variable.group(1), name)
                rest = ",".join(args[1:])
                if re.search(r"\bPFS_AUTOSIZE_VALUE\b", rest):
                    host.append((name, "%s: DEFAULT(PFS_AUTOSIZE_VALUE), sized at startup" % path))
                used = _HOST_CALL.search(rest)
                if used:
                    host.append((name, "%s: declared default calls %s" % (path, used.group(0).rstrip("( "))))
            tokens = _host_tokens(text)
            for match in re.finditer(r"\b(Sys_\w+)\.update_default\s*\(([^;]*)\)\s*;", text):
                used = _uses_host(match.group(2), tokens)
                if used:
                    updates.append((match.group(1), "%s: update_default() at startup from %s" % (path, used)))
            continue
        prefix = reader.plugin_prefix(path)
        extra = _prefix_for(path, MYSQL_EXTRA_PLUGINS)
        if prefix is not None or extra is not None:
            for name, scope, ro, _, offset, experimental in _plugin_declarations(text, prefix or extra,
                                                                                 reader._PLUGIN):
                entry = catalog.get(name) if prefix is not None else None
                kind = "build" if experimental else condition_kind(conditions_at(points, offset))
                _merge_fact(facts, name, _fact(entry[0] if entry else scope,
                                               (_ro(entry[2]) if entry else ro), False, None, None,
                                               (prefix or extra).rstrip("_"), kind))
            for match in reader._PLUGIN.finditer(text):
                try:
                    args, _ = reader.arguments(text, match.end())
                except ValueError:
                    continue
                used = _HOST_CALL.search(",".join(args[1:]))
                if used and re.fullmatch(r"\w+", args[0].strip()):
                    host.append(((prefix or extra) + args[0].strip(),
                                 "%s: declared default calls %s" % (path, used.group(0).rstrip("( "))))
        if path.startswith(("storage/innobase/", "storage/temptable/")) or path in MYSQL_CORE_FILES:
            host_evidence(path, text, prefix or extra, facts)
        if path.startswith("components/validate_password/"):
            for name in catalog:
                if name.startswith("validate_password.") and name not in facts:
                    entry = catalog[name]
                    _merge_fact(facts, name, _fact(entry[0], _ro(entry[2]), False, None, None,
                                                   "component_validate_password", ""))
    for name, target, kind in aliases:
        base = symbols.get(target)
        entry = catalog.get(name)
        _merge_fact(facts, name, _fact(entry[0] if entry else None, _ro(entry[2]) if entry else None,
                                       True, base, base, "core", kind))
    for name, evidence in host:
        _add_host(facts, name, evidence)
    for symbol, evidence in updates:
        if symbol in symbols:
            _add_host(facts, symbols[symbol], evidence)
    if mysqld:
        text = _clean(mysqld)
        for function, name in _ADJUST:
            if re.search(r"\b(?:static\s+)?void\s+%s\s*\(" % function, text):
                _add_host(facts, name, "%s: %s() at startup" % (MYSQLD, function))
        startup_assignments(MYSQLD, text, cvars, facts)
    # parse_sources reads a multi-parameter wrapper's own #define body as a
    # declaration (innodb_sysvar_name_suffix in 26.x); the scan above expands
    # those wrappers instead, so its names are the source facts.
    return facts


def mariadb_source_facts(sources):
    """{name: fact} from a {repository path: C++ text} MariaDB snapshot (same
    fact shape as mysql_source_facts). Core variables come from
    sql/sys_vars.cc (scope and read-only through the shared reader's
    core_facts, with the constructors of sql/sys_vars.ic or .inl); plugin
    variables from MARIADB_PLUGINS with MYSQL_SYSVAR/THDVAR declarations.
    """
    prepared = {path: _clean(text) for path, text in sources.items()}
    header = prepared.get("sql/sys_vars.inl") or prepared.get("sql/sys_vars.ic") or ""
    constructors = reader.constructor_flags(header)
    names = _define_names(prepared)
    macros = plugin_macros(prepared.values(), _MARIADB_PLUGIN)
    facts, cvars = {}, {}
    for path, text in sorted(prepared.items()):
        text = expand_macros(text, macros)
        points = condition_points(text)
        if path == "sql/sys_vars.cc":
            for match in reader._CORE.finditer(text):
                args, _ = reader.arguments(text, match.end(), "}" if match["opener"] == "{" else ")")
                name = reader.string(args[0]) or names.get(args[0])
                if name is None or len(args) < 2:
                    continue
                scope, _, flags = reader.core_facts(match["kind"], args, constructors)
                deprecated, substitute = _deprecation(args)
                alias = _ALIAS_HELP.search(reader.string(args[1]) or "")
                _merge_fact(facts, name, _fact(scope, _ro(flags), deprecated, substitute,
                                               alias.group(1) if alias else None, "core",
                                               condition_kind(conditions_at(points, match.start()))))
                variable = _VAR_ARG.search(args[2]) if len(args) > 2 else None
                if variable:
                    cvar = variable.group(1)
                    cvars[cvar if "." in cvar or "SESSION" not in args[2] else "global_system_variables." + cvar] = name
                used = _HOST_CALL.search(",".join(args[1:]))
                if used:
                    _add_host(facts, name, "%s: declared default calls %s" % (path, used.group(0).rstrip("( ")))
            continue
        if path == MYSQLD:
            continue
        prefix = _prefix_for(path, MARIADB_PLUGINS)
        if prefix is None:
            continue
        for name, scope, ro, deprecated, offset, _ in _plugin_declarations(text, prefix, _MARIADB_PLUGIN):
            _merge_fact(facts, name, _fact(scope, ro, deprecated, None, None, prefix.rstrip("_"),
                                           condition_kind(conditions_at(points, offset))))
        for match in _MARIADB_PLUGIN.finditer(text):
            try:
                args, _ = reader.arguments(text, match.end())
            except ValueError:
                continue
            if match[1] == "SYSVAR" and len(args) > 1 and re.fullmatch(r"\w+", args[0].strip()):
                cvars.setdefault(args[1].strip(), prefix + args[0].strip())
            used = _HOST_CALL.search(",".join(args[1:]))
            if used and re.fullmatch(r"\w+", args[0].strip()):
                _add_host(facts, prefix + args[0].strip(),
                          "%s: declared default calls %s" % (path, used.group(0).rstrip("( ")))
    for path, text in sorted(prepared.items()):
        if path != MYSQLD and not path.startswith("storage/innobase/"):
            continue
        if path == MYSQLD:
            startup_assignments(path, text, {cvar: name for cvar, name in cvars.items() if "." not in cvar}, facts)
        for match in _PLACEHOLDER.finditer(text):
            if match.group(1) in cvars:
                _add_host(facts, cvars[match.group(1)], "%s: placeholder default %s replaced at startup" % (
                    path, match.group(2)))
        tokens = _host_tokens(text)
        for match in _AUTOSIZE.finditer(text):
            try:
                args, _ = reader.arguments(text, match.end())
            except ValueError:
                continue
            name = cvars.get(args[0].strip().lstrip("&")) if args else None
            used = _uses_host(",".join(args[1:]), tokens) if name else None
            if used:
                _add_host(facts, name, "%s: SYSVAR_AUTOSIZE at startup from %s" % (path, used))
    return facts


def _wanted(engine, path):
    if not path.endswith((".cc", ".c", ".h", ".ic", ".inl")):
        return False
    if any(part in ("test", "tests", "unittest", "mysql-test") for part in path.split("/")):
        return False
    if path == MYSQLD:
        return True
    if engine == "mysql":
        return (path.startswith(reader._ROOTS) or path in MYSQL_CORE_FILES
                or any(path.startswith(root) for root, _ in MYSQL_EXTRA_PLUGINS))
    if path in MARIADB_CORE:
        return True
    return _prefix_for(path, MARIADB_PLUGINS) is not None


def _roots(engine):
    if engine == "mysql":
        return (tuple(reader._ROOTS) + MYSQL_CORE_FILES + (MYSQLD,)
                + tuple(root for root, _ in MYSQL_EXTRA_PLUGINS))
    return MARIADB_CORE + (MYSQLD,) + tuple(root for root, _ in MARIADB_PLUGINS)


def _code_key():
    """A digest of the shared reader and of the code and tables here that turn
    sources into facts: changing either invalidates the facts cached on disk
    (report and output changes do not)."""
    digest = hashlib.sha1(Path(reader.__file__).read_bytes())
    for part in (condition_points, conditions_at, condition_kind, merge_kind, _deprecation, _fact,
                 _merge_fact, _ro, _clean, _prepared, _define_names, plugin_macros, expand_macros,
                 _plugin_declarations, _prefix_for, mysql_source_facts, mariadb_source_facts, _wanted, _roots,
                 _blank_strings, _host_tokens, _uses_host, _add_host, host_evidence, startup_assignments):
        digest.update(inspect.getsource(part).encode("utf-8"))
    digest.update(repr((MYSQL_EXTRA_PLUGINS, MARIADB_PLUGINS, MARIADB_CORE, MYSQL_CORE_FILES, _MARIADB_PLUGIN.pattern,
                        _DEPRECATED.pattern, _DIRECTIVE.pattern, _GUARD.pattern, _DEBUG.pattern,
                        sorted(_SERVER_BUILD), _MACRO.pattern, _HOST_CALL.pattern, _DEF_VAL.pattern,
                        _IF_NOT_SET.pattern, _IF_NOT_SET_WRAPPER.pattern, _ALIAS_HELP.pattern, _PLACEHOLDER.pattern, _AUTOSIZE.pattern, _VAR_ARG.pattern, _ADJUST, MYSQLD,
                        _ASSIGN.pattern)).encode("utf-8"))
    return digest.hexdigest()


class SourceFacts(object):
    """Source facts per tag, cached by the blob ids of the files they come
    from (in memory, and in *cache_dir* when given)."""

    def __init__(self, engine, git, cache_dir=None):
        self.engine, self.git = engine, git
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._memo = {}

    def facts(self, tag):
        files = {path: blob for path, blob in self.git.tree(tag, _roots(self.engine)).items()
                 if _wanted(self.engine, path)}
        key = hashlib.sha1(json.dumps([self.engine, sorted(files.items()), _code_key()]).encode("utf-8")).hexdigest()
        if key in self._memo:
            return self._memo[key]
        cached = self.cache_dir / ("%s-%s.json" % (self.engine, key)) if self.cache_dir else None
        if cached is not None and cached.is_file():
            facts = json.loads(cached.read_text(encoding="utf-8"))
        else:
            sources = self.git.texts(files)
            parse = mysql_source_facts if self.engine == "mysql" else mariadb_source_facts
            facts = parse(sources)
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(facts, sort_keys=True), encoding="utf-8")
        self._memo[key] = facts
        return facts


# ---------------------------------------------------------------------------
# Manual


def parse_manual(text):
    """[(name, deprecated version, replacement text or None)] from a saved
    "Added, Deprecated, or Removed" page: its "deprecated" list only."""
    start = text.find('name="optvars-deprecated"')
    end = text.find('name="optvars-removed"', start + 1)
    if start < 0:
        return []
    section = text[start:end if end > start else len(text)]
    entries = []
    for item in re.findall(r'<li class="listitem">(.*?)</li>', section, re.S):
        code = re.search(r"<code[^>]*>(.*?)</code>", item, re.S)
        if not code:
            continue
        name = " ".join(html.unescape(re.sub(r"<[^>]+>", "", code.group(1))).split())
        words = " ".join(html.unescape(re.sub(r"<[^>]+>", "", item)).split())
        version = re.search(r"Deprecated in MySQL (\d+\.\d+\.\d+)", words)
        if not version:
            continue
        use = _MANUAL_USE.search(words)
        entries.append((name, version.group(1), use.group(1) if use else None))
    return entries


def parse_manual_text(text):
    """The same list from the text of the page's section extracted from the
    official PDF manual (5.7: its HTML manual now redirects to 9.7). Bullets
    start with "•"; page headers and numbers between them are ignored."""
    start = re.search(r"^Options and Variables Deprecated in MySQL [\d.]+$", text, re.M)
    if not start:
        return []
    end = re.search(r"^Options and Variables Removed in MySQL [\d.]+$", text[start.end():], re.M)
    section = text[start.end():start.end() + end.start()] if end else text[start.end():]
    entries = []
    for item in section.split("\u2022")[1:]:
        words = " ".join(item.split())
        found = re.match(r"([A-Za-z0-9_.-]+): ", words)
        version = re.search(r"Deprecated in MySQL (\d+\.\d+\.\d+)", words)
        if not found or not version:
            continue
        use = _MANUAL_USE.search(words)
        entries.append((found.group(1), version.group(1), use.group(1) if use else None))
    return entries


def read_manual(directory):
    """({name: [(version, replacement)]}, {series: date saved}) from a
    directory of saved pages (added-deprecated-removed-<series>.html, or .txt
    for a section taken from a PDF manual)."""
    entries, saved = {}, {}
    if not directory:
        return entries, saved
    directory = Path(directory)
    meta = {}
    if (directory / "SAVED.json").is_file():
        meta = json.loads((directory / "SAVED.json").read_text(encoding="utf-8")).get("pages", {})
    for path in sorted(list(directory.glob("added-deprecated-removed-*.html"))
                       + list(directory.glob("added-deprecated-removed-*.txt"))):
        series = path.stem.rsplit("-", 1)[1]
        if not re.fullmatch(r"\d+\.\d+", series):
            continue
        stamp = (meta.get(series) or {}).get("saved_at")
        if not stamp:
            stamp = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).strftime("%Y-%m-%d")
        saved[series] = stamp[:10]
        parse = parse_manual_text if path.suffix == ".txt" else parse_manual
        for name, version, replacement in parse(path.read_text(encoding="utf-8", errors="replace")):
            pair = (version, replacement)
            if pair not in entries.setdefault(name, []):
                entries[name].append(pair)
    for name in entries:
        entries[name].sort(key=lambda pair: version_key(pair[0]))
    return entries, saved


# ---------------------------------------------------------------------------
# Runtime records


def load_records(directory, engine):
    """({version: record with variables}, {version: reason it cannot be used})."""
    usable, unusable = {}, {}
    folder = Path(directory) / engine if directory else None
    if folder is None or not folder.is_dir():
        return usable, unusable
    for path in sorted(folder.glob("*.json")):
        if not re.fullmatch(r"\d+\.\d+\.\d+", path.stem):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            unusable[path.stem] = "The runtime record could not be read: %s" % exc
            continue
        version = record.get("version") or path.stem
        if record.get("schema") != RUNTIME_SCHEMA or record.get("engine") != engine or version != path.stem:
            unusable[path.stem] = "The runtime record is not a %s %s record for this release." % (
                RUNTIME_SCHEMA, engine)
        elif record.get("skipped"):
            unusable[version] = str(record["skipped"])
        elif not record.get("variables"):
            unusable[version] = "The runtime record has no variables."
        else:
            usable[version] = record
    return usable, unusable


def harvest_scope(directory, engine):
    """The in-scope versions the lab run listed in versions-<engine>.json
    (a list, or {"versions": [...]}, of versions or {"version": ...}), or None."""
    path = Path(directory) / ("versions-%s.json" % engine) if directory else None
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("versions", data.get("releases", [])) if isinstance(data, dict) else data
    found = set()
    for item in items if isinstance(items, list) else ():
        version = item.get("version") if isinstance(item, dict) else item
        if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
            found.add(version)
    return found


# ---------------------------------------------------------------------------
# Merge


def manual_applies(deprecated_in, release, dates):
    """Whether a manual deprecation in *deprecated_in* covers *release*: an
    earlier or equal version of the same series, or a version of an earlier
    series whose tag date (when known) is not after the release's."""
    series, own = series_of(deprecated_in), series_of(release["version"])
    if series == own:
        return version_key(deprecated_in) <= version_key(release["version"])
    if version_key(series) > version_key(own):
        return False
    date = dates.get(deprecated_in)
    return date is None or parse_date(date) <= parse_date(release["date"])


_QUOTED = r"'(?:@@|--)?(?:global\.|session\.)?([a-z0-9_.$-]+)'"
_RUNTIME_CHANGE = re.compile(r"^(?:updating|changing|setting|modifying)\s+" + _QUOTED + r"\s+(?:is|has been)\s+"
                             r"deprecated")
_SUBJECT = re.compile(_QUOTED + r"\s+(?:is|has been)\s+deprecated")
# MySQL 5.7 InnoDB: "Using innodb_large_prefix is deprecated and the parameter may be removed ...",
# "The parameter innodb_undo_logs is deprecated and may be removed ...".
_USING = re.compile(r"^(?:using|the (?:parameter|variable|option))\s+([a-z0-9_.]+)\s+is\s+deprecated")


def warning_kinds(name, text):
    """Split a record's deprecated_warning (one warning per line) by meaning.

    "variable": the warning deprecates the variable itself; its subject is the
    variable ("'@@tx_isolation' is deprecated ...", "'init_slave' is
    deprecated ...", "The syntax '@@x' is deprecated ...").
    "runtime_change": changing it while the server runs is deprecated, not the
    variable (MySQL ER_WARN_DEPRECATED_SYSVAR_UPDATE: "Updating 'x' is
    deprecated. It will be made read-only in a future release."). MariaDB
    has no such message.
    "other": a deprecated value or syntax, not the variable (MariaDB's
    old_mode prints "'NO_PROGRESS_INFO' is deprecated ..." for a value;
    MySQL's "Changing sql mode 'X' is deprecated ...").
    Only "variable" lines are deprecation evidence.
    """
    kinds = {"variable": [], "runtime_change": [], "other": []}
    target = name.lower()
    for line in (text or "").splitlines():
        line = line.strip()
        low = line.lower()
        if not low:
            continue
        change = _RUNTIME_CHANGE.search(low)
        if (change and change.group(1) == target) or (
                "read-only in a future release" in low and target in low):
            kinds["runtime_change"].append(line)
        elif any(match.group(1).replace("-", "_") == target for match in _SUBJECT.finditer(low)) or (
                _USING.match(low) and _USING.match(low).group(1) == target):
            kinds["variable"].append(line)
        else:
            kinds["other"].append(line)
    return kinds


def warning_replacement(text):
    if not text:
        return None
    match = _WARNING_USE.search(text)
    return match.group(1) if match else None


def in_range(version, first, last):
    """first <= version <= last; a two-part *last* ("10.11") means the end of
    that series, None no upper bound."""
    key = version_key(version)
    if key < version_key(first):
        return False
    if last is None:
        return True
    bound = version_key(last)
    return key[:2] <= bound if len(bound) == 2 else key <= bound


def review_entry(engine, name, version):
    """The HOST_REVIEW entry of *name* that covers *version*, or None."""
    for entry in HOST_REVIEW.get(engine, ()):
        if entry[0] == name and in_range(version, entry[1], entry[2]):
            return entry
    return None


def source_deprecation(engine, name, version):
    """The SOURCE_DEPRECATIONS entry of *name* that covers *version*, or None."""
    for entry in SOURCE_DEPRECATIONS.get(engine, ()):
        if entry[0] == name and in_range(version, entry[1], None):
            return entry
    return None


def manual_rejected(engine, name, version):
    """The MANUAL_REJECT reason when the manual's evidence for *name* does not
    apply to *version*, else None."""
    for entry in MANUAL_REJECT.get(engine, ()):
        if entry[0] == name and version_key(version) < version_key(entry[1]):
            return entry[2]
    return None


def default_rules(engine, catalog):
    """{name: ((first, last or None, sentence), ...)}: each HOST_REVIEW
    entry's sentence over the runs of consecutive cataloged releases that it
    covers and that have the name; last is None when the run reaches the
    newest cataloged release and the entry has no upper bound."""
    releases, rows = catalog["cataloged"], catalog["rows"]
    rules = {}
    for entry in HOST_REVIEW.get(engine, ()):
        runs, start = [], None
        for index, release in enumerate(releases):
            covered = entry[0] in rows[index] and review_entry(engine, entry[0], release["version"]) is entry
            if covered and start is None:
                start = index
            if not covered and start is not None:
                runs.append((start, index - 1))
                start = None
        if start is not None:
            runs.append((start, len(releases) - 1))
        for first, last in runs:
            open_end = last == len(releases) - 1 and entry[2] is None
            rules.setdefault(entry[0], []).append(
                (releases[first]["version"], None if open_end else releases[last]["version"], entry[5]))
    return {name: tuple(sorted(periods, key=lambda period: version_key(period[0])))
            for name, periods in sorted(rules.items())}


def has_origin(record):
    """Whether a runtime record carries the per-variable origin field
    (MySQL VARIABLE_SOURCE, MariaDB GLOBAL_VALUE_ORIGIN)."""
    return any(isinstance(var, dict) and "origin" in var for var in (record.get("variables") or {}).values())


def mixed_origin(records):
    """Versions whose record lacks the origin field while others of the same
    engine have it (records of different lab runs), else []. MySQL 5.7 has no
    variables_info, so its records carry no origin by design and never count."""
    without = sorted((version for version, record in records.items()
                      if not has_origin(record) and version_key(version) >= (8, 0, 0)), key=version_key)
    with_origin = [version for version, record in records.items() if has_origin(record)]
    return without if without and with_origin else []


def build(engine, releases, records, unusable, source, manual, manual_dates=None, tag_dates=None):
    """Merge runtime records, source facts and manual deprecations.

    releases: in-scope releases [{"version", "tag", "maturity", "date"}].
    records: {version: runtime record}; unusable: {version: reason}.
    source: callable(tag) -> {name: fact}, or None (no source facts).
    manual: {name: [(version, replacement)]}.
    tag_dates: {version: ISO date} of every tagged version, used for manual
    deprecations in a version that is not cataloged.
    Returns a dict with versions, skipped, history and the report data.
    """
    manual_dates = manual_dates or {}
    known_dates = dict(tag_dates or {})
    known_dates.update({release["version"]: release["date"] for release in releases})
    cataloged = [release for release in releases if release["version"] in records]
    skipped = [(release["version"], unusable.get(release["version"], NOT_READ), release["date"][:10])
               for release in releases if release["version"] not in records]
    outside = sorted((set(records) | set(unusable)) - {release["version"] for release in releases},
                     key=version_key)
    previous = lineage(cataloged)
    names_all = set()
    for release in cataloged:
        names_all.update(records[release["version"]]["variables"])
    per_version, source_by_version, notes = [], [], []
    filled = 0
    warnings = {"runtime_change": {}, "other": {}}
    host_marks, auto_excluded = {}, {}
    bad_substitutes, rejected = {}, {}
    for release in cataloged:
        record = records[release["version"]]
        facts_source = {}
        if source is not None:
            try:
                facts_source = source(release["tag"]) or {}
            except (subprocess.CalledProcessError, ValueError) as exc:
                notes.append("%s: source facts could not be read (%s)." % (release["tag"], exc))
                facts_source = {}
        source_by_version.append(facts_source)
        # Variable names are case-insensitive (MariaDB declares wsrep_slave_FK_checks).
        folded = {key.lower(): value for key, value in facts_source.items()}
        row = {}
        for name in sorted(record["variables"]):
            var = record["variables"][name] or {}
            fact = facts_source.get(name) or folded.get(name.lower())
            evidence = []
            kinds = warning_kinds(name, var.get("deprecated_warning"))
            if kinds["variable"]:
                evidence.append("server")
            for kind in ("runtime_change", "other"):
                if kinds[kind]:
                    warnings[kind].setdefault((name, kinds[kind][0]), []).append(release["version"])
            reviewed_source = source_deprecation(engine, name, release["version"])
            if (fact and fact.get("deprecated")) or reviewed_source:
                evidence.append("source")
            manual_hit = [pair for pair in manual.get(name, ())
                          if manual_applies(pair[0], release, known_dates)]
            reason = manual_rejected(engine, name, release["version"]) if manual_hit else None
            if reason:
                rejected.setdefault((name, reason), []).append(release["version"])
                manual_hit = []
            if manual_hit:
                evidence.append("manual")
            deprecated = tuple(sorted(evidence)) or None
            replacement = None
            if deprecated:
                candidates = []
                if reviewed_source and reviewed_source[2]:
                    candidates.append(("reviewed", reviewed_source[2]))
                if fact:
                    candidates.append(("source", fact.get("alias_of") or fact.get("substitute")))
                candidates.extend(("manual", pair[1]) for pair in manual_hit)
                candidates.append(("server", warning_replacement("\n".join(kinds["variable"]))))
                for origin, text in candidates:
                    candidate = clean_name(text)
                    if candidate and candidate != name and candidate in names_all:
                        replacement = candidate
                        break
                    if origin == "source" and text and text.strip():
                        bad_substitutes.setdefault((name, text), []).append(release["version"])
            # An alias names a variable of the same release (MySQL
            # Sys_var_deprecated_alias; MariaDB "Alias for X" help texts).
            alias_of = fact.get("alias_of") if fact else None
            if alias_of not in record["variables"] or alias_of == name:
                alias_of = None
            dynamic = var.get("dynamic")
            if dynamic is None and fact is not None and fact.get("ro") is not None:
                dynamic = not fact["ro"]
                filled += 1
            values = var.get("values")
            note = var.get("default_note")
            if name in STATE_NAMES.get(engine, ()):
                note = "state"
            elif name in RELEASE_NAMES.get(engine, ()):
                note = "release"
            elif name in BUILD_NAMES.get(engine, ()):
                note = "build"
            else:
                entry = review_entry(engine, name, release["version"])
                auto = str(var.get("origin") or "").upper() == "AUTO"
                # A computed default replaces "no note" and "SQL NULL"; unknown stays unknown.
                if entry is not None and entry[3]:
                    if note in (None, "null", "host"):
                        note = "host"
                        host_marks.setdefault((name, "reviewed"), []).append(release["version"])
                elif entry is None and auto and note in (None, "null", "host"):
                    note = "host"
                    host_marks.setdefault((name, "origin AUTO"), []).append(release["version"])
                elif entry is not None and auto:
                    auto_excluded.setdefault(name, []).append(release["version"])
            # MySQL reports data types from 9.0 (variables_metadata); an
            # earlier type would be the lab's guess from the values.
            typ = var.get("type") if engine != "mysql" or version_key(release["version"]) >= (9, 0, 0) else None
            row[name] = (var.get("scope"), typ, dynamic, var.get("default"), note,
                         var.get("min"), var.get("max"), tuple(values) if values is not None else None,
                         deprecated, replacement, alias_of, var.get("plugin"))
        per_version.append(row)
    history = {}
    for name in sorted(names_all):
        runs, last = [], object()
        for index, row in enumerate(per_version):
            facts = row.get(name)
            if facts != last:
                if facts is not None or runs:
                    runs.append((index, facts))
                last = facts
        history[name] = tuple(runs)
    versions = tuple((release["version"], release["tag"], release["maturity"], release["date"][:10],
                      previous[index], records[release["version"]].get("method"))
                     for index, release in enumerate(cataloged))
    result = {"engine": engine, "versions": versions, "skipped": tuple(skipped), "history": history,
            "cataloged": cataloged, "records": records, "source": source_by_version,
            "rows": per_version, "manual": manual, "manual_dates": manual_dates, "outside": outside,
            "notes": notes, "bad_substitutes": bad_substitutes, "known_dates": known_dates,
            "dynamic_from_source": filled, "warnings": warnings, "host_marks": host_marks,
            "auto_excluded": auto_excluded, "manual_rejected": rejected}
    result["default_rules"] = default_rules(engine, result)
    return result


# ---------------------------------------------------------------------------
# Module output


def _literal(value):
    return repr(value)


def render_module(catalog, generated):
    """The text of myflames/sysvar_history_<engine>.py.

    Distinct facts tuples are written once in _F and HISTORY refers to them,
    so at import time HISTORY holds facts tuples exactly as the format says.
    """
    engine = catalog["engine"]
    table, index = [], {}
    lists, list_index = [], {}
    strings = {}
    for name in sorted(catalog["history"]):
        for _, facts in catalog["history"][name]:
            if facts is not None and facts not in index:
                index[facts] = len(table)
                table.append(facts)
                for value in facts:
                    if isinstance(value, str):
                        strings[value] = strings.get(value, 0) + 1
                values = facts[FIELDS.index("values")]
                if values and values not in list_index:
                    list_index[values] = len(lists)
                    lists.append(values)
    # Strings used in many facts tuples get a short module-level name.
    common = sorted((text for text, count in strings.items() if count >= 8 and len(text) >= 3),
                    key=lambda text: (-strings[text], text))[:200]
    short = {text: "_S%d" % number for number, text in enumerate(common)}

    def value_text(value, top=False):
        if isinstance(value, str) and value in short:
            return short[value]
        if top and isinstance(value, tuple) and value in list_index:
            return "_V[%d]" % list_index[value]
        if isinstance(value, tuple):
            if not value:
                return "()"
            return "(" + ", ".join(value_text(item) for item in value) + ("," if len(value) == 1 else "") + ")"
        return _literal(value)

    lines = [
        "# Generated by scripts/gen_sysvar_history.py; do not edit.",
        "# %s system variables of every cataloged release, read from running servers in lab" % PRODUCTS[engine],
        "# containers. Source tags add maturity, tag date, deprecation markers and aliases; the",
        "# official manual adds deprecations. Releases no one could run are listed in SKIPPED.",
        "# HISTORY: for each name, run-length entries over VERSIONS: (first_index, facts or None).",
        "# Before its first entry a name is absent; None means absent from that version on.",
        "# default_note: null (SQL NULL), unknown, host (computed at startup from the host), release",
        "# (reports the release or build), build (a path of the build or installation layout).",
        "",
        "ENGINE = %r" % engine,
        "GENERATED = %s" % _literal(generated),
        "# (version, tag, maturity, tag_date YYYY-MM-DD, previous_index or None, method \"image\"|\"package\")",
        "VERSIONS = (",
    ]
    lines += ["    %s," % _literal(version) for version in catalog["versions"]]
    lines += [")", "# Releases with a source tag that could not be read from a running server:",
              "# (version, reason, tag date YYYY-MM-DD).", "SKIPPED = ("]
    lines += ["    %s," % _literal((version, short_reason(reason), date)) for version, reason, date in catalog["skipped"]]
    lines += [")", "FIELDS = %s" % _literal(FIELDS)]
    lines += ["# Names whose presence depends on build options while the source still declares them:",
              "# {name: sentence naming the build condition}. Their HISTORY stays as read.", "BUILD_ONLY = {"]
    lines += ["    %r: %r," % (name, text) for name, text in sorted(BUILD_ONLY.get(engine, {}).items())]
    lines += ["}",
              "# How the server sets a default it computes at startup (reviewed against the source):",
              "# {name: ((first_version, last_version or None, sentence), ...)} over cataloged releases.",
              "DEFAULT_RULES = {"]
    for name, periods in sorted(catalog.get("default_rules", {}).items()):
        lines.append("    %r: (%s%s)," % (name, ", ".join(_literal(period) for period in periods),
                                         "," if len(periods) == 1 else ""))
    lines += ["}", ""]
    lines += ["%s = %r" % (short[text], text) for text in common]
    lines += ["", "# Distinct allowed-value lists, shared by _F.", "_V = ("]
    lines += ["    %s," % value_text(values) for values in lists]
    lines += [")", "", "# Distinct facts tuples (FIELDS order), shared by HISTORY.", "_F = ("]
    lines += ["    (%s)," % ", ".join(value_text(value, top=True) for value in facts) for facts in table]
    lines += [")", "", "HISTORY = {"]
    for name in sorted(catalog["history"]):
        runs = catalog["history"][name]
        parts = ["(%d, %s)" % (first, "None" if facts is None else "_F[%d]" % index[facts])
                 for first, facts in runs]
        lines.append("    %r: (%s%s)," % (name, ", ".join(parts), "," if len(parts) == 1 else ""))
    lines += ["}", ""]
    private = ["_F", "_V"] + [short[text] for text in common]
    for start in range(0, len(private), 12):
        lines.append("del " + ", ".join(private[start:start + 12]))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validation report


def _ranges(versions, order):
    """Compress a list of versions into "a to b" runs over *order*."""
    positions = sorted(order.index(version) for version in versions)
    runs, start, last = [], None, None
    for position in positions:
        if start is None:
            start = last = position
        elif position == last + 1:
            last = position
        else:
            runs.append((start, last))
            start = last = position
    if start is not None:
        runs.append((start, last))
    return ", ".join(order[a] if a == b else "%s to %s" % (order[a], order[b]) for a, b in runs)


def compare_release(record, facts, engine=None, version=None):
    """Disagreements between one runtime record and its source facts; scope
    disagreements that SCOPE_REVIEW explains for *version* go to
    "scope_explained"."""
    # Variable names are case-insensitive: compare them folded to lower case.
    runtime = {name.lower(): value for name, value in record["variables"].items()}
    facts = {name.lower(): value for name, value in facts.items()}
    result = {"only_runtime": {}, "only_source": {}, "scope": [], "read_only": [], "scope_explained": []}
    # A plugin is loaded when any variable the source gives it is on the server.
    active = {fact.get("group") for name, fact in facts.items() if name in runtime}
    for name in sorted(set(runtime) - set(facts)):
        var = runtime[name] or {}
        if var.get("plugin"):
            group = "plugin variable the source reader does not cover"
        elif "." in name:
            group = "component variable"
        else:
            group = "unexplained"
        result["only_runtime"].setdefault(group, []).append(name)
    for name in sorted(set(facts) - set(runtime)):
        fact = facts[name]
        group = fact.get("group") or "core"
        if group != "core" and group not in active:
            reason = "plugin not loaded"
        elif fact.get("kind") == "hidden":
            reason = "not visible"
        elif fact.get("kind") == "debug":
            reason = "debug only"
        elif fact.get("kind") == "build":
            reason = "platform or build only"
        else:
            reason = "unexplained"
        result["only_source"].setdefault(reason, []).append(name)
    for name in sorted(set(facts) & set(runtime)):
        var, fact = runtime[name] or {}, facts[name]
        if var.get("scope") and fact.get("scope") and var["scope"] != fact["scope"]:
            explained = [entry for entry in SCOPE_REVIEW.get(engine, ())
                         if entry[0] == name and version and in_range(version, entry[1], entry[2])]
            result["scope_explained" if explained else "scope"].append((name, var["scope"], fact["scope"]))
        if var.get("dynamic") is not None and fact.get("ro") is not None and var["dynamic"] == fact["ro"]:
            result["read_only"].append((name, var["dynamic"], fact["ro"]))
    return result


def row_has(catalog, release, name):
    return name in catalog["rows"][catalog["cataloged"].index(release)]


def flip_flops(catalog):
    """[(name, version, before, value)]: a default that changes at a release
    and changes back at the next release of the same series (notes host,
    release and build excluded)."""
    found = []
    releases, rows = catalog["cataloged"], catalog["rows"]
    for index in range(1, len(releases) - 1):
        series = {series_of(releases[i]["version"]) for i in (index - 1, index, index + 1)}
        if len(series) != 1:
            continue
        before, here, after = rows[index - 1], rows[index], rows[index + 1]
        for name in sorted(set(before) & set(here) & set(after)):
            if here[name][4] in ("host", "release", "build", "state") or before[name][4] in ("host", "release", "build", "state"):
                continue
            if before[name][3] == after[name][3] != here[name][3]:
                found.append((name, releases[index]["version"], before[name][3], here[name][3]))
    return found


def flip_flop_lines(catalog, order):
    found = flip_flops(catalog)
    lines = ["### Defaults that change and change back at the next release of the series (%d)" % len(found), ""]
    packages = [item[0] for item in catalog["versions"] if item[5] == "package"]
    if packages:
        flipped = sorted({version for _, version, _, _ in found if version in packages}, key=version_key)
        lines.append("Releases read from packages instead of images: %s; %s." % (
            ", ".join(packages), "with a default that changes and changes back: " + ", ".join(flipped)
            if flipped else "none has a default that changes and changes back"))
    if not found:
        return lines + ["None.", ""]
    by_version = {}
    for name, version, before, value in found:
        by_version.setdefault(version, []).append("%s (%s, then %s)" % (name, before, value))
    for version in sorted(by_version, key=version_key):
        items = by_version[version]
        lines.append("- %s: %s%s" % (version, "; ".join(items[:12]),
                                       " and %d more" % (len(items) - 12) if len(items) > 12 else ""))
    return lines + [""]


def host_lines(catalog, order):
    """The host-dependent defaults section: names marked per series, the
    reviewed list, origin AUTO, and the source scan against the review."""
    engine = catalog["engine"]
    lines = ["### Host-dependent defaults marked from source or origin (default_note host)", ""]
    marked = {}
    for (name, how), versions in catalog.get("host_marks", {}).items():
        for version in versions:
            marked.setdefault(series_of(version), set()).add(name)
    if marked:
        lines.append("Per series (names whose default the server computes at startup from the host; series with "
                     "the same names share a line; the lab's two-host comparison may mark more, counted above):")
        groups = []
        for series in sorted(marked, key=version_key):
            if groups and groups[-1][1] == marked[series]:
                groups[-1][0].append(series)
            else:
                groups.append(([series], marked[series]))
        for serieses, names in groups:
            lines.append("- %s: %s (%d)" % (", ".join(serieses), ", ".join(sorted(names)), len(names)))
    else:
        lines.append("No names marked.")
    lines.append("")
    lines.append("Reviewed source list (%s):" % ("marked, then reviewed and not marked"))
    for name, first, last, flag, location, reason in sorted(HOST_REVIEW.get(engine, ()),
                                                            key=lambda entry: (not entry[3], entry[0], entry[1])):
        lines.append("- %s%s, %s %s: %s. %s" % ("" if flag else "not marked: ", name, first,
                                               "to " + last if last else "on", location, reason))
    by_origin = {name: versions for (name, how), versions in catalog.get("host_marks", {}).items()
                 if how == "origin AUTO"}
    if by_origin:
        lines.append("Marked from origin AUTO and not in the reviewed list: %s." % "; ".join(
            "%s (%s)" % (name, _ranges(versions, order)) for name, versions in sorted(by_origin.items())))
    if catalog.get("auto_excluded"):
        lines.append("Origin AUTO reviewed as not host: %s." % "; ".join(
            "%s (%s)" % (name, _ranges(versions, order)) for name, versions in sorted(catalog["auto_excluded"].items())))
    unreviewed, unseen = {}, {}
    for release, facts, row in zip(catalog["cataloged"], catalog["source"], catalog["rows"]):
        if not facts:
            continue
        version = release["version"]
        for name, fact in facts.items():
            if fact.get("host") and review_entry(engine, name, version) is None:
                unreviewed.setdefault("%s (%s)" % (name, "; ".join(fact["host"])), []).append(version)
        for entry in HOST_REVIEW.get(engine, ()):
            if entry[3] and entry[0] in row and review_entry(engine, entry[0], version) is entry and not (
                    facts.get(entry[0]) or {}).get("host"):
                unseen.setdefault(entry[0], []).append(version)
    lines.append("Source scan against the reviewed list: %s; %s." % (
        "scan hits with no reviewed entry: " + "; ".join("%s at %s" % (text, _ranges(versions, order))
                                                         for text, versions in sorted(unreviewed.items()))
        if unreviewed else "every scan hit is reviewed",
        "marked entries without scan evidence: " + "; ".join("%s at %s" % (name, _ranges(versions, order))
                                                             for name, versions in sorted(unseen.items()))
        if unseen else "every marked entry has scan evidence at each release"))
    lines.append("")
    for note, title in (("release", "Release and build identity (default_note release)"),
                        ("build", "Paths of the build or installation layout (default_note build)"),
                        ("state", "Session or server state, not a default (default_note state)")):
        names = sorted({name for row in catalog["rows"] for name, facts in row.items() if facts[4] == note})
        lines.append("### %s" % title)
        lines.append("")
        lines.append(("%s: %s." % (len(names), ", ".join(names))) if names else "None.")
        lines.append("")
    lines += build_only_lines(catalog, order)
    rules = catalog.get("default_rules", {})
    lines.append("### How computed defaults are set (DEFAULT_RULES, %d names)" % len(rules))
    lines.append("")
    for name, periods in sorted(rules.items()):
        lines.append("- %s: %s" % (name, " ".join("%s to %s: %s" % (first, last or "now", sentence)
                                                  for first, last, sentence in periods)))
    lines.append("")
    return lines


def build_only_candidates(catalog):
    """{name: (present versions, absent versions)} for names the source
    declares under a build conditional that some records have and others
    lack while the source still declares them."""
    found = {}
    for release, facts, row in zip(catalog["cataloged"], catalog["source"], catalog["rows"]):
        folded = {name.lower(): name for name in row}
        for name, fact in (facts or {}).items():
            if fact.get("kind") != "build":
                continue
            present, absent = found.setdefault(name.lower(), ([], []))
            (present if name.lower() in folded else absent).append(release["version"])
    return {name: pair for name, pair in found.items() if pair[0] and pair[1]}


def build_only_lines(catalog, order):
    reviewed = BUILD_ONLY.get(catalog["engine"], {})
    candidates = build_only_candidates(catalog)
    lines = ["### Presence that depends on build options (BUILD_ONLY, %d)" % len(reviewed), ""]
    for name in sorted(reviewed):
        lines.append("- %s: %s" % (name, reviewed[name]))
    for name in sorted(candidates):
        present, absent = candidates[name]
        lines.append("- Source and records: %s is in the records of %s and missing from %s while declared." % (
            name, _ranges(present, order), _ranges(absent, order)))
    missing = sorted(set(candidates) - set(reviewed))
    extra = sorted(set(reviewed) - set(candidates))
    lines.append("Candidates not in BUILD_ONLY: %s. BUILD_ONLY names without that evidence here (their sentence "
                 "gives the reason): %s." % (", ".join(missing) or "none", ", ".join(extra) or "none"))
    lines.append("")
    return lines


def report_lines(catalog, examples=12):
    engine = catalog["engine"]
    product = PRODUCTS[engine]
    order = [release["version"] for release in catalog["cataloged"]]
    lines = ["## %s" % product, ""]
    lines.append("Cataloged releases: %d%s. Skipped releases: %d." % (
        len(order), " (%s to %s)" % (order[0], order[-1]) if order else "", len(catalog["skipped"])))
    odd = ["%s reports %s" % (version, catalog["records"][version].get("server_version"))
           for version in order if not str(catalog["records"][version].get("server_version") or "").startswith(version)]
    if odd:
        lines.append("Records whose server_version does not start with the release: %s." % "; ".join(odd))
    methods = {}
    for version in order:
        methods.setdefault(catalog["records"][version].get("method") or "unknown", []).append(version)
    if order:
        lines.append("Read by method: %s." % "; ".join("%s %d" % (method, len(versions))
                                                       for method, versions in sorted(methods.items())))
        pairs = [facts for row in catalog["rows"] for facts in row.values()]
        no_type = [version for version, row in zip(order, catalog["rows"])
                   if row and sum(1 for facts in row.values() if facts[1] is None) * 2 > len(row)]
        lines.append("Name-release pairs: %d; type unknown %d; dynamic unknown %d (after the source fallback "
                     "%d were filled); default notes: host %d, release %d, build %d, unknown %d, SQL NULL %d." % (
                         len(pairs), sum(1 for facts in pairs if facts[1] is None),
                         sum(1 for facts in pairs if facts[2] is None), catalog.get("dynamic_from_source", 0),
                         sum(1 for facts in pairs if facts[4] == "host"),
                         sum(1 for facts in pairs if facts[4] == "release"),
                         sum(1 for facts in pairs if facts[4] == "build"),
                         sum(1 for facts in pairs if facts[4] == "unknown"),
                         sum(1 for facts in pairs if facts[4] == "null")))
        if no_type:
            lines.append("Releases where most types are unknown (the server does not report them): %s." %
                         _ranges(no_type, order))
        origins, without = {}, []
        for version in order:
            record = catalog["records"][version]
            if not has_origin(record):
                without.append(version)
                continue
            for var in record["variables"].values():
                key = str((var or {}).get("origin"))
                origins[key] = origins.get(key, 0) + 1
        lines.append("Origin in the records: %s%s." % (
            ", ".join("%s %d" % (key, origins[key]) for key in sorted(origins)) or "none",
            "; records without the origin field: %s" % _ranges(without, order) if without else ""))
        lines.append("Builds: %s" % builds_sentence(engine, catalog))
    if catalog["outside"]:
        lines.append("Records outside the source scope (not cataloged): %s." % ", ".join(catalog["outside"]))
    harvest = catalog.get("harvest_scope")
    if harvest is not None:
        mine = {item[0] for item in catalog["skipped"]} | set(order)
        only_lab, only_here = sorted(harvest - mine, key=version_key), sorted(mine - harvest, key=version_key)
        if only_lab or only_here:
            lines.append("The lab run's in-scope list differs: only in the lab list %s; only in the tag scan %s." % (
                ", ".join(only_lab) or "none", ", ".join(only_here) or "none"))
        else:
            lines.append("The lab run's in-scope list matches the tag scan (%d releases)." % len(harvest))
    if catalog["skipped"]:
        # Group reasons that differ only in version numbers, without their parenthetical
        # details; the module keeps each full reason.
        reasons = {}
        for version, reason, _ in catalog["skipped"]:
            key = re.sub(r"\s*\([^()]*\)", "", re.sub(r"\d+\.\d+(?:\.\d+)?", "X", reason)).strip()
            key = key if len(key) <= 300 else key[:297].rstrip() + "..."
            reasons.setdefault(key, []).append(version)
        lines.append("Skipped releases by reason (X stands for the release; details in SKIPPED):")
        for reason, versions in sorted(reasons.items(), key=lambda item: (-len(item[1]), item[0])):
            lines.append("- %s (%d): %s" % (reason, len(versions), ", ".join(versions)))
    for note in catalog["notes"]:
        lines.append("- " + note)
    lines.append("")
    if not order:
        return lines
    lines += ["### Running server versus source tag", "",
              "Columns: names in the record (R) and in the source tag (S); names only in the record "
              "(plugin or component, unexplained); names only in the source (plugin not loaded, "
              "platform or build only, including MySQL 8.0's experimental InnoDB variables, debug only, "
              "declared NOT_VISIBLE (startup options), unexplained); scope and read-only disagreements.", "",
              "| Release | R | S | Only R: plugin/comp | Only R: unexpl. | Only S: plugin | Only S: build | "
              "Only S: debug | Only S: hidden | Only S: unexpl. | Scope | Read-only |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    unexplained = {"only in the record": {}, "only in the source": {}, "scope": {}, "read-only": {}}
    explained_scope = {}
    totals = {}
    for release, facts in zip(catalog["cataloged"], catalog["source"]):
        record = catalog["records"][release["version"]]
        if not facts:
            lines.append("| %s | %d | not read | | | | | | | | | |" % (release["version"],
                                                                        len(record["variables"])))
            continue
        result = compare_release(record, facts, engine, release["version"])
        only_r = result["only_runtime"]
        only_s = result["only_source"]
        counts = (len(only_r.get("plugin variable the source reader does not cover", ()))
                  + len(only_r.get("component variable", ())),
                  len(only_r.get("unexplained", ())), len(only_s.get("plugin not loaded", ())),
                  len(only_s.get("platform or build only", ())), len(only_s.get("debug only", ())),
                  len(only_s.get("not visible", ())), len(only_s.get("unexplained", ())), len(result["scope"]),
                  len(result["read_only"]))
        for key, value in zip(("plugin_comp", "unexpl_r", "plugin", "build", "debug", "hidden", "unexpl_s",
                               "scope", "ro"), counts):
            totals[key] = totals.get(key, 0) + value
        lines.append("| %s | %d | %d | %s |" % (release["version"], len(record["variables"]), len(facts),
                                                " | ".join(str(count) for count in counts)))
        version = release["version"]
        for name in only_r.get("unexplained", ()):
            unexplained["only in the record"].setdefault(name, []).append(version)
        for name in only_s.get("unexplained", ()):
            unexplained["only in the source"].setdefault(name, []).append(version)
        for name, runtime_scope, source_scope in result["scope"]:
            unexplained["scope"].setdefault("%s (server %s, source %s)" % (name, runtime_scope, source_scope),
                                            []).append(version)
        for name, runtime_scope, source_scope in result["scope_explained"]:
            explained_scope.setdefault(name, []).append(version)
        for name, dynamic, ro in result["read_only"]:
            text = "%s (server %s, source %s)" % (name, "dynamic" if dynamic else "not dynamic",
                                                   "READ_ONLY" if ro else "not READ_ONLY")
            unexplained["read-only"].setdefault(text, []).append(version)
    lines.append("")
    lines.append("Totals over releases: %s." % ", ".join("%s %d" % (key, totals[key]) for key in sorted(totals)))
    for name, versions in sorted(explained_scope.items()):
        reason = [entry[3] for entry in SCOPE_REVIEW.get(engine, ()) if entry[0] == name]
        lines.append("Scope disagreement the source explains (SCOPE_REVIEW), not counted above: %s at %s: %s" % (
            name, _ranges(versions, order), reason[0] if reason else ""))
    lines.append("")
    for title, items in unexplained.items():
        if not items:
            continue
        lines.append("### Unexplained: %s (%d names)" % (title, len(items)))
        lines.append("")
        ranked = sorted(items.items(), key=lambda item: (-len(item[1]), item[0]))
        for name, versions in ranked[:examples]:
            lines.append("- %s: %s" % (name, _ranges(versions, order)))
        if len(ranked) > examples:
            lines.append("- and %d more." % (len(ranked) - examples))
        lines.append("")
    # Names that disappear and come back within one series.
    gaps = []
    by_series = {}
    for index, release in enumerate(catalog["cataloged"]):
        by_series.setdefault(series_of(release["version"]), []).append(index)
    for name in sorted(catalog["history"]):
        present = [row.get(name) is not None for row in catalog["rows"]]
        for series, indexes in by_series.items():
            flags = [present[i] for i in indexes]
            if True in flags:
                first = flags.index(True)
                last = len(flags) - 1 - flags[::-1].index(True)
                missing = [order[indexes[i]] for i in range(first, last + 1) if not flags[i]]
                if missing:
                    gaps.append("%s in %s: absent from %s%s" % (
                        name, series, ", ".join(missing),
                        " (BUILD_ONLY: presence depends on the build)" if name in BUILD_ONLY.get(engine, {}) else ""))
    lines.append("### Names that disappear and come back within one series (%d)" % len(gaps))
    lines.append("")
    lines += ["- " + gap for gap in gaps[:30]] or ["None."]
    if len(gaps) > 30:
        lines.append("- and %d more." % (len(gaps) - 30))
    lines.append("")
    # Defaults that change at most releases look host or build dependent.
    churn = []
    for name, runs in sorted(catalog["history"].items()):
        defaults = [run[1][3] for run in runs if run[1] is not None]
        notes = {run[1][4] for run in runs if run[1] is not None}
        changes = sum(1 for a, b in zip(defaults, defaults[1:]) if a != b)
        versioned = all(release["version"] in (default or "") for release, default in zip(
            catalog["cataloged"], (row.get(name, (None,) * 4)[3] for row in catalog["rows"]))
            if row_has(catalog, release, name))
        if len(order) >= 6 and changes >= len(order) // 2 and not notes & {"host", "release", "build", "state"} and not versioned:
            churn.append("%s (%d default changes)" % (name, changes))
    lines.append("### Defaults that change in at least half of the releases (%d)" % len(churn))
    lines.append("")
    lines.append(("- " + "; ".join(churn)) if churn else "None.")
    lines.append("")
    lines += flip_flop_lines(catalog, order)
    lines += host_lines(catalog, order)
    # Default changes at a switch between an image and a package release.
    switches, switch_names = [], {}
    for index in range(1, len(order)):
        before, after = catalog["cataloged"][index - 1], catalog["cataloged"][index]
        methods = (catalog["records"][before["version"]].get("method"),
                   catalog["records"][after["version"]].get("method"))
        if methods[0] == methods[1] or series_of(before["version"]) != series_of(after["version"]):
            continue
        changed = sorted(name for name, facts in catalog["rows"][index].items()
                         if name in catalog["rows"][index - 1] and catalog["rows"][index - 1][name][3] != facts[3])
        switches.append("%s (%s) to %s (%s): %d" % (before["version"], methods[0], after["version"], methods[1],
                                                   len(changed)))
        for name in changed:
            switch_names[name] = switch_names.get(name, 0) + 1
    if switches:
        lines.append("### Default changes where the method switches between image and package")
        lines.append("")
        lines.append("Releases: %s." % "; ".join(switches))
        ranked = sorted(switch_names.items(), key=lambda item: (-item[1], item[0]))
        lines.append("Names: %s." % ", ".join("%s (%d)" % item for item in ranked[:30]))
        lines.append("")
    # Deprecation evidence.
    counts = {}
    for row in catalog["rows"]:
        for facts in row.values():
            if facts[8]:
                counts[facts[8]] = counts.get(facts[8], 0) + 1
    lines.append("### Deprecation evidence")
    lines.append("")
    if counts:
        lines.append("Name-release pairs by evidence: %s." % ", ".join(
            "%s %d" % ("+".join(key), counts[key]) for key in sorted(counts)))
    else:
        lines.append("No deprecated names.")
    server_only = sorted({name for row in catalog["rows"] for name, facts in row.items()
                          if facts[8] == ("server",)})
    if server_only:
        lines.append("Deprecated by the running server only (no source marker, not in the manual): %s." %
                     ", ".join(server_only[:40]) + (" and %d more" % (len(server_only) - 40)
                                                     if len(server_only) > 40 else ""))
    if catalog["bad_substitutes"]:
        lines.append("Source substitutes that are not a variable name (no replacement kept): %s." % "; ".join(
            "%s: %s" % (name, text) for name, text in sorted(catalog["bad_substitutes"])))
    for name, first, replacement, evidence in SOURCE_DEPRECATIONS.get(engine, ()):
        versions = [version for version, row in zip(order, catalog["rows"])
                    if name in row and row[name][8] and "source" in row[name][8] and in_range(version, first, None)]
        lines.append("Reviewed source deprecation: %s from %s%s (%s); in the catalog at %s." % (
            name, first, ", replacement %s" % replacement if replacement else "", evidence,
            _ranges(versions, order) if versions else "no release"))
    for (name, reason), versions in sorted(catalog.get("manual_rejected", {}).items()):
        lines.append("Manual evidence rejected for %s at %s: %s" % (name, _ranges(versions, order), reason))
    aliases = {}
    for version, row in zip(order, catalog["rows"]):
        for name, facts in row.items():
            if facts[10]:
                aliases.setdefault("%s -> %s" % (name, facts[10]), []).append(version)
    lines.append("Aliases (alias_of), %d: %s." % (len(aliases), "; ".join(
        "%s (%s)" % (text, _ranges(versions, order)) for text, versions in sorted(aliases.items())) or "none"))
    lines.append("")
    titles = {"runtime_change": "Changing at runtime is deprecated (not counted as deprecation evidence)",
              "other": "Other server deprecation warnings: a value or syntax, not the variable (not counted)"}
    for kind in ("runtime_change", "other"):
        items = catalog.get("warnings", {}).get(kind) or {}
        if not items:
            continue
        lines.append("### %s" % titles[kind])
        lines.append("")
        for (name, text), versions in sorted(items.items()):
            lines.append("- %s: %s (\"%s\")" % (name, _ranges(versions, order), text))
        lines.append("")
    if engine == "mysql":
        lines.append("### Manual deprecations")
        lines.append("")
        dates = catalog["manual_dates"]
        if dates:
            lines.append("Manual pages saved: %s." % ", ".join("%s on %s" % (series, dates[series])
                                                               for series in sorted(dates, key=version_key)))
        manual = catalog["manual"]
        kept = sorted(name for name in manual if name in catalog["history"])
        lines.append("Manual entries: %d names; %d are system variables in the records." % (len(manual), len(kept)))
        lacking, never = [], []
        for name in kept:
            hits = []
            for index, release in enumerate(catalog["cataloged"]):
                facts = catalog["rows"][index].get(name)
                if facts and facts[8] and "manual" in facts[8]:
                    hits.append(facts[8])
            if not hits:
                never.append(name)
            elif not any(("server" in hit or "source" in hit) for hit in hits):
                lacking.append("%s (deprecated in %s)" % (name, ", ".join(pair[0] for pair in manual[name])))
        lines.append("Deprecated in the manual with no server or source evidence in any cataloged release "
                     "(%d): %s." % (len(lacking), "; ".join(lacking) if lacking else "none"))
        if never:
            lines.append("Manual deprecations that cover no cataloged release where the name exists (%d): %s." % (
                len(never), ", ".join(never)))
        lines.append("")
    return lines


def write_report(catalogs, path, inputs):
    lines = ["# Parameter version catalog: validation report", "",
             "Generated by scripts/gen_sysvar_history.py on %s." % inputs["on"],
             "Every fact about names, scope, type, default, range, allowed values and dynamic comes from the "
             "running-server records; this report compares those records with the source tags and the manual.",
             ""]
    for key in ("runtime", "manual", "mysql_source", "mariadb_source"):
        if inputs.get(key):
            lines.append("- %s: %s" % (key.replace("_", " "), inputs[key]))
    lines.append("")
    for catalog in catalogs:
        lines += report_lines(catalog)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Command line


def builds_sentence(engine, catalog=None):
    """One sentence on which builds the values come from."""
    records = (catalog or {}).get("records", {})
    # MariaDB packages from its own Ubuntu repository are the image's build.
    packages = [item[0] for item in (catalog or {}).get("versions", ()) if item[5] == "package" and not (
        engine == "mariadb" and "/repo/ubuntu" in str((records.get(item[0]) or {}).get("package") or ""))]
    extra = " (%s from %s)" % (", ".join(packages), PACKAGES[engine]) if packages else ""
    return BUILDS[engine] % extra


def generated_info(engine, on, manual_dates, catalog=None):
    return {"on": on, "runtime": "read from running servers in lab containers",
            "builds": builds_sentence(engine, catalog),
            "source": SOURCE_URLS[engine],
            "manual": {series: manual_dates[series] for series in sorted(manual_dates, key=version_key)}
            if engine == "mysql" else {}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--runtime", required=True, type=Path, help="Directory with <engine>/<version>.json records")
    parser.add_argument("--mysql-source", type=Path, help="Read-only MySQL git checkout")
    parser.add_argument("--mariadb-source", type=Path, help="Read-only MariaDB git checkout")
    parser.add_argument("--manual", type=Path, help="Directory of saved MySQL manual pages")
    parser.add_argument("--engine", action="append", choices=ENGINES, help="Engine to generate (default: both)")
    parser.add_argument("--output-dir", type=Path, default=root / "myflames", help="Where the modules are written")
    parser.add_argument("--report", type=Path, help="Write the validation report (Markdown) here")
    parser.add_argument("--cache", type=Path, help="Cache parsed source facts in this directory")
    parser.add_argument("--generated-on", help="Date written in GENERATED (default: today, UTC)")
    parser.add_argument("--allow-mixed-origin", action="store_true",
                        help="Generate even when some records of an engine have the origin field and others "
                             "do not (records from different lab runs); prints a warning")
    args = parser.parse_args(argv)
    mixed = {}
    for engine in args.engine or ENGINES:
        records, _ = load_records(args.runtime, engine)
        without = mixed_origin(records)
        if without:
            mixed[engine] = without
            sys.stderr.write("WARNING: %s: %d of %d records have no origin field (an earlier lab run): %s.\n" % (
                PRODUCTS[engine], len(without), len(records), ", ".join(without)))
    if mixed and not args.allow_mixed_origin:
        sys.stderr.write("Refusing to generate from records of different lab runs. Wait for the lab run to "
                         "finish, or pass --allow-mixed-origin.\n")
        return 2
    on = args.generated_on or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    manual, manual_dates = read_manual(args.manual)
    catalogs = []
    for engine in args.engine or ENGINES:
        repo = args.mysql_source if engine == "mysql" else args.mariadb_source
        if repo is None:
            parser.error("--%s-source is required for %s" % (engine, engine))
        git = Git(repo)
        releases = scan_releases(engine, git)
        prefix = "mysql-" if engine == "mysql" else "mariadb-"
        all_dates = {}
        for tag, date in git.tag_dates(prefix + "*").items():
            match = re.fullmatch(re.escape(prefix) + r"(\d+\.\d+\.\d+)(?:-respin)?", tag)
            if match:
                all_dates.setdefault(match.group(1), date)
        records, unusable = load_records(args.runtime, engine)
        facts = SourceFacts(engine, git, args.cache)
        catalog = build(engine, releases, records, unusable, facts.facts,
                        manual if engine == "mysql" else {}, manual_dates, all_dates)
        catalog["harvest_scope"] = harvest_scope(args.runtime, engine)
        if engine in mixed:
            catalog["notes"].insert(0, "WARNING: generated from records of different lab runs; these have no "
                                       "origin field: %s." % ", ".join(mixed[engine]))
        catalogs.append(catalog)
        versions = [version[0] for version in catalog["versions"]]
        print("%s: %d in-scope releases, %d cataloged (%s), %d skipped." % (
            PRODUCTS[engine], len(releases), len(versions),
            _ranges(versions, versions) if versions else "none", len(catalog["skipped"])))
        if not versions:
            print("%s: no release could be cataloged; the module was not written." % PRODUCTS[engine])
            continue
        text = render_module(catalog, generated_info(engine, on, manual_dates, catalog))
        output = args.output_dir / ("sysvar_history_%s.py" % engine)
        output.write_text(text, encoding="utf-8")
        print("Wrote %s (%d names, %.0f KB)" % (output, len(catalog["history"]), len(text.encode("utf-8")) / 1024.0))
    if args.report:
        write_report(catalogs, args.report, {
            "on": on, "runtime": str(args.runtime), "manual": str(args.manual) if args.manual else None,
            "mysql_source": str(args.mysql_source) if args.mysql_source else None,
            "mariadb_source": str(args.mariadb_source) if args.mariadb_source else None})
        print("Wrote %s" % args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
