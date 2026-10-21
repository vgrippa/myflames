"""Pure server identity helpers, below connectors and performance collectors.

Two levels describe a server:

* the *engine*, ``mysql`` or ``mariadb``, decides which rules, SQL and
  variable names apply (:func:`detect_flavor`, historically named);
* the *flavor*, ``mysql``, ``percona`` or ``mariadb``, and the *product*
  name the server is shown with (:func:`product_identity`). Percona Server for
  MySQL and Percona XtraDB Cluster are MySQL builds: engine ``mysql``, flavor
  ``percona``, so every MySQL rule still applies and only the name and the
  features Percona adds differ.

Percona identifies itself only in ``@@version_comment``, read live from
Percona Server 8.0.46-37 ("Percona Server (GPL), Release 37, Revision
39e2b60e"), 8.4.11-11 ("Percona Server (GPL), Release 11, Revision
57878ff8") and Percona XtraDB Cluster 8.4.11-11.1 ("Percona XtraDB Cluster
(GPL), Release rel11, Revision 33f7510, WSREP version 26.1.4.3"). The
``-37`` in ``@@version`` is the Percona release, but Percona's dynamic
``version_suffix`` variable can change that suffix, so the version alone is
never used to decide.
"""
import re


def detect_flavor(version, version_comment=""):
    """The engine: MariaDB identifies itself in either version field, ignoring case.

    Percona Server is a MySQL build and returns ``mysql`` here; use
    :func:`product_identity` for the product.
    """
    return ("mariadb" if "mariadb" in (str(version or "") + " " +
                                      str(version_comment or "")).lower() else "mysql")


#: Product names by flavor; Percona XtraDB Cluster has its own name.
PRODUCT_NAMES = {"mysql": "MySQL", "mariadb": "MariaDB", "percona": "Percona Server"}
PXC_PRODUCT = "Percona XtraDB Cluster"


def product_identity(version, version_comment=""):
    """``{"engine", "flavor", "product"}`` from ``@@version`` and ``@@version_comment``.

    engine is ``mysql`` or ``mariadb`` (the rules that apply), flavor is
    ``mysql``, ``percona`` or ``mariadb``, and product is the name to show:
    MySQL, Percona Server, Percona XtraDB Cluster or MariaDB. An unknown
    comment is MySQL, as before.
    """
    engine = detect_flavor(version, version_comment)
    comment = str(version_comment or "").lower()
    if engine == "mariadb":
        return {"engine": engine, "flavor": "mariadb", "product": PRODUCT_NAMES["mariadb"]}
    if "percona xtradb cluster" in comment:
        return {"engine": engine, "flavor": "percona", "product": PXC_PRODUCT}
    if "percona server" in comment:
        return {"engine": engine, "flavor": "percona", "product": PRODUCT_NAMES["percona"]}
    return {"engine": engine, "flavor": "mysql", "product": PRODUCT_NAMES["mysql"]}


_PERCONA_RELEASE = re.compile(r"^\s*\d+\.\d+\.\d+-\d+(?:\.\d+)?\s*$")


def identity_from_version(version):
    """product_identity when only ``@@version`` is known (a relay log's format
    event records the version and nothing else).

    Percona numbers its releases after the upstream version ("8.4.11-11",
    "8.0.46-37", Percona XtraDB Cluster "8.4.11-11.1"); MySQL Community and
    Enterprise builds carry no numeric suffix ("8.4.11", "9.7.2-commercial").
    A numeric suffix is therefore read as Percona Server, the base product
    of both; PXC cannot be told apart without the comment.
    """
    identity = product_identity(version)
    if identity["engine"] == "mysql" and _PERCONA_RELEASE.match(str(version or "")):
        return {"engine": "mysql", "flavor": "percona", "product": PRODUCT_NAMES["percona"]}
    return identity


def product_name(report):
    """The product a report or summary names: its ``server_product`` when
    present, else MySQL or MariaDB from ``engine`` (older reports, fixtures)."""
    report = report or {}
    product = report.get("server_product")
    if product:
        return product
    return "MariaDB" if report.get("engine") == "mariadb" else "MySQL"


def server_label(report, version_key="server_version"):
    """"Percona Server 8.4.11-11", "MySQL 8.4.11", "MariaDB 11.8.9-MariaDB"."""
    report = report or {}
    return "%s %s" % (product_name(report), report.get(version_key) or "?")


#: Global variables Percona Server adds to MySQL. Read live on 2026-10-21 by
#: comparing performance_schema.variables_info on Percona Server 8.0.46-37
#: and 8.4.11-11 with MySQL 8.0.46 and 8.4.11 (official images, same base
#: versions), less the names MySQL defines itself (innodb_numa_interleave,
#: M/storage/innobase/handler/ha_innodb.cc:23666, built only with NUMA).
#: Names absent here are never marked, so a newer Percona variable is shown
#: as an ordinary one rather than claimed.
PERCONA_VARIABLES = frozenset((
    "binlog_ddl_skip_rewrite", "binlog_skip_flush_commands", "binlog_space_limit",
    "buffered_error_log_filename", "buffered_error_log_size", "encrypt_tmp_files",
    "enforce_storage_engine", "expand_fast_index_creation", "ft_query_extra_word_chars",
    "have_backup_locks", "have_backup_safe_binlog_info", "have_snapshot_cloning",
    "innodb_buffer_pool_lazy_latch_init", "innodb_buffer_pool_populate", "innodb_cleaner_lsn_age_factor",
    "innodb_compressed_columns_threshold", "innodb_compressed_columns_zip_level",
    "innodb_corrupt_table_action", "innodb_empty_free_list_algorithm", "innodb_encrypt_online_alter_logs",
    "innodb_force_index_records_in_range", "innodb_ft_ignore_stopwords", "innodb_lru_threads",
    "innodb_parallel_dblwr_encrypt", "innodb_parallel_doublewrite_path",
    "innodb_print_lock_wait_timeout_info", "innodb_records_in_range", "innodb_show_locks_held",
    "innodb_sys_tablespace_encrypt", "innodb_temp_tablespace_encrypt", "jemalloc_detected",
    "jemalloc_profiling", "kill_idle_transaction", "log_query_errors", "log_slow_filter",
    "log_slow_rate_limit", "log_slow_rate_type", "log_slow_sp_statements", "log_slow_verbosity",
    "max_slowlog_files", "max_slowlog_size", "net_buffer_shrink_interval", "percona_telemetry_disable",
    "proxy_protocol_networks", "replica_enable_event", "replica_translate_deprecated_priv",
    "secure_log_path", "slow_query_log_always_write_time", "slow_query_log_use_global_control",
    "table_open_cache_triggers", "tf_sequence_table_max_upper_bound", "thread_pool_high_prio_mode",
    "thread_pool_high_prio_tickets", "thread_pool_idle_timeout", "thread_pool_max_threads",
    "thread_pool_oversubscribe", "thread_pool_size", "thread_pool_stall_limit", "thread_statistics",
    "userstat", "version_suffix",
))
#: Percona variables that MySQL itself defines from this version on:
#: table_open_cache_triggers is in MySQL 9.x (M/sql/sys_vars.cc:4964).
_UPSTREAM_SINCE = {"table_open_cache_triggers": (9, 0)}


def percona_variable(name, flavor, version_tuple=None):
    """True when *name* is a variable Percona Server adds, on a Percona server.

    Always False on MySQL and MariaDB, where names such as thread_pool_size
    belong to the server itself (MariaDB's thread pool, MySQL Enterprise).
    """
    if flavor != "percona":
        return False
    name = str(name or "").lower()
    if name not in PERCONA_VARIABLES:
        return False
    since = _UPSTREAM_SINCE.get(name)
    return not (since and version_tuple and tuple(version_tuple)[:2] >= since)


def parse_version_tuple(version, leading=False, prefer_mariadb=False, components=3,
                        default=(0, 0, 0)):
    """Parse a version with the caller's existing input contract.

    Server variables require a leading version; tool banners and privilege
    metadata may contain a prefix. MariaDB tools can print a client version
    before their actual distribution version. Capture's gate uses just the
    leading major/minor and treats an unreadable version separately.
    """
    if not leading and isinstance(version, (tuple, list)):
        return tuple(version)[:components]
    # Leading callers historically accept strings (or an empty falsey value),
    # while the other callers accept values convertible to strings.
    text = (version or "") if leading else str(version)
    pattern = r"(\d+)" + r"\.(\d+)" * (components - 1)
    match = None
    if prefer_mariadb:
        match = re.search(pattern + r"[^\s,]*-MariaDB", text, re.I)
    if match is None:
        match = re.match(r"\s*" + pattern, text) if leading else re.search(pattern, text)
    return tuple(int(part) for part in match.groups()) if match else default


def server_version_tuple(text):
    """Compatibility adapter for leading three-part server variables."""
    return parse_version_tuple(text, leading=True)


def tool_version_tuple(text):
    """Compatibility adapter for server strings and external tool banners."""
    return parse_version_tuple(str(text or ""), prefer_mariadb=True)


#: Oldest releases myflames documents support for (README, AGENTS.md).
SUPPORTED_MINIMUM = {"mysql": (8, 4), "mariadb": (10, 11)}


def unsupported_version_note(version, version_comment=""):
    """One stderr line for a server below the documented floor, else None.

    MySQL below 8.4 and MariaDB below 10.11 can still answer many reads, but
    plan formats, Performance Schema columns and variables differ, so results
    may be missing or wrong. An unreadable version returns None. A leading
    "5.5.5-" (the handshake version some MariaDB builds send for old
    replication clients) is ignored.
    """
    text = re.sub(r"^\s*5\.5\.5-", "", str(version or ""))
    identity = product_identity(text, version_comment)
    engine = identity["engine"]
    found = parse_version_tuple(text, leading=True, components=2, default=None)
    if found is None or found >= SUPPORTED_MINIMUM[engine]:
        return None
    name = "MariaDB" if engine == "mariadb" else "MySQL"
    floor = SUPPORTED_MINIMUM[engine]
    if identity["flavor"] == "percona":
        # Percona Server follows MySQL's floor; its release number is part of the version.
        return ("myflames supports MySQL %d.%d or later, including %s %d.%d; this server is %s %s, so "
                "some results may be missing or wrong." % (floor[0], floor[1], identity["product"], floor[0],
                                                            floor[1], identity["product"], text.strip()))
    return ("myflames supports %s %d.%d or later; this server is %s %s, so some results "
            "may be missing or wrong." % (name, floor[0], floor[1], name, text.strip().split("-")[0]))
