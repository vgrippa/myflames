"""Cluster status for the connection in use: MariaDB Galera, Galera Cluster for
MySQL, Percona XtraDB Cluster, MySQL InnoDB Cluster and Group Replication.
:func:`read_status` only reads; the reviewed changes the workspace's Cluster
place offers (switchover, Group Replication stop and start, MariaDB Galera
desync, resync and pc.bootstrap) are at the end of this module.

:func:`read_status` returns the ``cluster-1.0`` report (schema in
``docs/schemas/cluster-1.0.json``): the kind of cluster this server belongs
to (``galera``, ``pxc``, ``innodb_cluster``, ``group_replication`` or
``none``), its name and UUID, one status word (``ok`` / ``watch`` /
``problem`` / ``unknown``) with the findings behind it, the members as this
server sees them, the measured metrics, and for InnoDB Cluster the metadata
(cluster name, topology mode, registered routers). Nothing is claimed that
was not measured: a Galera node other than this one has no state here,
because the server exposes only its own (``state`` is null and the member is
not judged).

One severity scale for every kind (the CLI, JSON, MCP, the place, the sidebar
and ``--fail-on`` all read it): ``ok`` with quorum and every expected member
present and healthy; ``watch`` while quorum holds but a member is missing (a
Galera node not in the component, an InnoDB Cluster member MISSING from the
group or UNREACHABLE), a member is in a transitional state (Donor/Desynced,
Joiner, Joined, Joining, RECOVERING), or the cluster cannot survive another
failure; ``problem`` without quorum or outside a Primary component, for a
member in ERROR, and when the connected member itself is OFFLINE or in ERROR
or (Galera) does not replicate (wsrep_on OFF).

Every statement is a tagged ``SELECT`` or ``SHOW`` (``/*myflames:cluster*/``)
through :class:`myflames.perf_common.Reader`, so the same isolated client and
the read-only checks of the other Server views apply. The Full analysis's
``checks.wsrep`` (server_checks.py) judges Galera through
:func:`galera_status`, the same rules as this module.

Verified against server source (mariadb-server 13.0 trunk "Ma/", mysql-server
9.7 trunk "M/", mysql-shell trunk "Sh/") and live (2026-10-20: MariaDB
11.8.9 with Galera 26.4.27, Percona XtraDB Cluster 8.4.11-11.1 with Galera
4.27, MySQL 8.4.11 InnoDB Cluster with MySQL Router 8.4.10; see
test/fixtures/cluster):

* Galera node detection. MariaDB has a global ``wsrep_on``, forced off
  without a provider (Ma/sql/wsrep_mysqld.cc:886-890). On Percona XtraDB
  Cluster 8.4 ``wsrep_on`` is session-only (``SELECT @@global.wsrep_on``
  fails with error 1238, live), so SHOW GLOBAL VARIABLES does not list it and
  ``wsrep_provider`` (``none`` without a provider) decides. ``SET GLOBAL
  wsrep_on=OFF`` is allowed with a provider loaded and does not disconnect
  it (Ma/sql/wsrep_var.cc wsrep_on_check); the server-level status names stay
  listed while the provider's are hidden (Ma/sql/wsrep_var.cc
  wsrep_show_status, Ma/sql/mysqld.cc:8069-8086), and a session started then
  does not replicate its writes (Ma/mysql-test/suite/galera/t/
  galera_var_wsrep_on_off.test), so such a node is still read as Galera, as a
  problem.
* Galera status variables. MariaDB defines ``wsrep_connected``,
  ``wsrep_ready``, ``wsrep_cluster_state_uuid``, ``wsrep_cluster_conf_id``,
  ``wsrep_cluster_status``, ``wsrep_cluster_size``, ``wsrep_local_index``,
  ``wsrep_local_bf_aborts`` and the ``wsrep_provider_*`` names itself
  (Ma/sql/mysqld.cc:8069-8086); the rest (``wsrep_local_state_comment``,
  ``wsrep_incoming_addresses``, ``wsrep_local_recv_queue(_avg)``,
  ``wsrep_local_send_queue(_avg)``, ``wsrep_flow_control_paused``,
  ``wsrep_flow_control_active``, ``wsrep_cert_deps_distance``,
  ``wsrep_local_cert_failures``, ``wsrep_evs_state``, ``wsrep_gcomm_uuid``,
  ``wsrep_desync_count``, ``wsrep_cluster_weight``) come from the Galera
  provider and are listed by Ma/mysql-test/suite/wsrep/r/variables.result.
  They exist only while a provider is loaded. The same names were read live
  on both MariaDB and PXC.
* ``wsrep_cluster_status`` is ``Primary`` in the Synced, Joined and Donor
  server states, ``Disconnected`` when disconnected, ``non-Primary``
  otherwise, which includes a joiner receiving its state transfer
  (Ma/sql/wsrep_server_service.cc:356-380, s_joiner in wsrep_sst.cc:384), so
  a node that is connected and Joining is reported as joining, never as
  having lost quorum. ``wsrep_ready`` prints ON/OFF (SHOW_MY_BOOL,
  Ma/sql/wsrep_mysqld.cc:593-600) and is ON only once Synced.
  ``wsrep_local_state_comment`` reads ``Synced``, ``Donor/Desynced`` after
  ``SET GLOBAL wsrep_desync=1``, which lasts until it is set OFF
  (Ma/mysql-test/suite/galera/r/galera_status_local_state.result,
  Ma/sql/wsrep_var.cc wsrep_desync_check), also while FLUSH TABLES WITH READ
  LOCK or BACKUP STAGE BLOCK_DDL holds (Ma/sql/lock.cc, Ma/sql/backup.cc) and
  during an RSU schema change (Ma/mysql-test/suite/galera/r/MW-258.result),
  and ``Initialized`` with ``Disconnected`` and ``wsrep_ready`` OFF
  (galera_var_cluster_address.result:15-24).
* A node outside a Primary component refuses most statements; SET, SHOW,
  SELECT without tables, reads of only information_schema and
  performance_schema tables (on MariaDB also USE and the mysql.wsrep_*
  tables) and reads with wsrep_dirty_reads still run (Ma/sql/sql_parse.cc
  wsrep_tables_accessible_when_detached and the not-ready gate,
  Ma/sql/table.cc table categories).
* ``mysql.wsrep_cluster_members`` (``node_uuid``, ``cluster_uuid``,
  ``node_name``, ``node_incoming_address``; Ma/sql/wsrep_schema.cc:70-77) is
  written only for a Primary view (Ma/sql/wsrep_server_service.cc:250-273),
  so on a non-Primary node it still lists the last Primary view
  (Ma/mysql-test/suite/galera/r/MDEV-21479.result:44-59). It is read as
  membership, never as state; its ``node_uuid`` is the node's gcomm UUID
  (Ma/sql/wsrep_schema.cc stores view member ids), so this node keeps one
  key whether its component is Primary or not.
* With ``wsrep_node_incoming_address`` AUTO the incoming address is built
  from the port mysqld had when wsrep started (Ma/sql/wsrep_mysqld.cc:
  671-704), and ``wsrep_init_server`` runs before ``network_init`` sets the
  ports (Ma/sql/mysqld.cc:5285, 6211); MariaDB 11.8.9 in Docker reported
  ``ip:0``, live. Port 0 is shown as the host alone, with one note.
* Expected membership. ``wsrep_cluster_address`` lists the peers a node
  contacts, often not all of them (one reachable peer is enough, and an
  arbitrator counts in ``wsrep_cluster_size`` without being listed), so it
  is used as an expected size only when this node is identified: in the list
  (by ``wsrep_node_address`` host and port when both carry a port, else by a
  host or name that only one entry has: ``@@hostname`` and its first DNS
  label, ``report_host``, ``wsrep_node_name``, ``wsrep_node_incoming_address``
  when not AUTO, and this node's own incoming address), or proved absent
  (every entry is another member, or on this host with another port). The
  MTR configurations name the same group-communication port in both
  variables (Ma/mysql-test/suite/galera_3nodes/galera_3nodes.cnf).
  ``wsrep_node_address`` defaults to "" and is not written back with the
  address the server guesses (Ma/sql/sys_vars.cc:6423-6431,
  Ma/sql/wsrep_mysqld.cc:637-649). A component larger than the list keeps
  its size without an "of N".
* Failures a Galera component tolerates: a Primary component needs more
  than half of the total weight (``wsrep_cluster_weight`` is the sum of
  every node's ``pc.weight``; nodes without a weighted majority go
  non-Primary: Ma/mysql-test/suite/galera_3nodes/t/galera_pc_weight.test,
  where a non-Primary node reports a weight of 0; one of two nodes killed
  leaves the other non-Primary, Ma/mysql-test/suite/galera/t/
  galera_kill_nochanges.test, unless ``pc.ignore_sb``, galera_pc_ignore_sb.test,
  while a clean shutdown leaves it Primary, galera_events2.test).
  With equal weights that is (size - 1) / 2; myflames says it assumes equal
  pc.weight, and computes nothing when the weight differs from the size.
* ``performance_schema.pxc_cluster_view`` (``HOST_NAME``, ``UUID``,
  ``STATUS``, ``LOCAL_INDEX``, ``SEGMENT``) is Percona XtraDB Cluster only,
  verified live (SHOW CREATE TABLE and rows reading ``SYNCED``, ``DONOR``,
  ``JOINER``, ``PRIMARY``). Its STATUS words are the Galera provider's node
  states (in libgalera_smm.so, not in the server source): only ``SYNCED`` is
  ok; the others are shown in the words the node itself uses (DONOR as
  Donor/Desynced, PRIMARY as Joining) and judged watch.
* Galera provider claims, not in the server source (the provider is a
  separate project): ``wsrep_flow_control_paused`` is the fraction of time
  replication was paused by flow control since the statistics were last
  reset or since start (the server calls the provider's reset_status() on
  FLUSH GLOBAL STATUS, Ma/sql/mysqld.cc:9650-9655; from MariaDB 11.5.1 a
  plain FLUSH STATUS resets only the session, MDEV-33145, mysqld.cc:
  9615-9628, and before it, or with old_mode OLD_FLUSH_STATUS, it reaches the
  provider too); ``wsrep_local_cert_failures`` counts this node's
  transactions that certification rolled back (whether a reset clears it is
  up to the provider); the Joining and Joined state comments;
  ``wsrep_flow_control_active``; and that ``wsrep_incoming_addresses`` lists
  the nodes in ``wsrep_local_index`` order (consistent with every live
  recording). No documented threshold exists for flow control; myflames says
  watch from :data:`FLOW_CONTROL_WATCH` (0.1).
* Group Replication: ``performance_schema.replication_group_members`` columns
  (M/storage/perfschema/table_replication_group_members.cc:134-143;
  ``MEMBER_COMMUNICATION_STACK`` from 8.0.27, commit 8a8893469de), states ONLINE, OFFLINE,
  RECOVERING, ERROR, UNREACHABLE (M/plugin/group_replication/src/
  member_info.cc:835-849). MEMBER_ROLE is empty for an OFFLINE or ERROR
  member and PRIMARY for every ONLINE or RECOVERING member in multi-primary
  mode (member_info.cc:618-631). UNREACHABLE only overrides the state shown, so
  an unreachable member keeps the role of its status (M/plugin/
  group_replication/src/ps_information.cc:121-138; live, an unreachable
  SECONDARY). With the plugin never started the table has one
  OFFLINE row for this server (M/plugin/group_replication/src/
  ps_information.cc:46-77); a member that left the group (OFFLINE or ERROR)
  keeps only its own row (gcs_event_handlers.cc:1017-1042, member_info.cc:
  1198-1221), so it cannot see the group and no size is claimed. The group
  has lost its majority when members minus UNREACHABLE members are at most
  half of the members (member_info.cc:1450-1464). ``replication_group_member_stats``
  columns: M/storage/perfschema/table_replication_group_member_stats.cc:159-171.
* Failures a group tolerates, over the current view by the same majority
  rule: (members - UNREACHABLE) - (members / 2 + 1), never below 0, and
  null without quorum. With no UNREACHABLE member it is MySQL Shell's
  (ONLINE + RECOVERING - 1) / 2 (Sh/modules/adminapi/cluster/status.cc:
  160-187), which prints no tolerance without quorum (status.cc:193-206).
* InnoDB Cluster metadata: ``mysql_innodb_cluster_metadata`` views
  ``schema_version``, ``v2_gr_clusters`` (``cluster_id``, ``cluster_name``,
  ``primary_mode`` 'pm' or 'mm', ``group_name``), ``v2_instances``
  (``label``, ``mysql_server_uuid``, ``endpoint``; ``instance_type`` in
  ``attributes`` from 2.2.0, where read replicas are 'read-replica') and
  ``v2_routers`` in every 2.x model (Sh/modules/adminapi/common/metadata/
  metadata-model-2.0.0.sql to 2.4.0.sql). ``primary_mode`` is as last known
  by the shell, so the live ``group_replication_single_primary_mode`` wins.
  A read replica has no group of its own (MySQL Shell's addReplicaInstance
  never installs the plugin) and replicates over the
  ``read_replica_replication`` channel (Sh/modules/adminapi/common/common.h);
  it is found by its ``@@server_uuid`` in its replicated copy of
  ``v2_instances`` and judged by that channel
  (``performance_schema.replication_connection_status`` and
  ``replication_applier_status``).
  A router's ``last_check_in``: the schema comment says hourly, but MySQL
  Router writes it every tenth metadata refresh by default (about every 5 s
  with the default 0.5 s TTL), never by default in a ClusterSet, or as its
  ``stats_updates_frequency`` option sets (M/router/src/metadata_cache/src/
  metadata_cache.cc:813-824, router_options.cc:194-205; live ages 0-7 s),
  and only on the writable member (metadata_cache.cc:785-789), so a member
  that left the group holds a frozen copy. From metadata 2.4.0 it lives in
  ``router_stats`` and MySQL Shell reads
  ``COALESCE(router_stats.last_check_in, routers.last_check_in)``
  (Sh/modules/adminapi/common/metadata_storage.cc:137-155). It is shown with
  its age and as UTC (``UNIX_TIMESTAMP`` of the TIMESTAMP value; through a
  view it passes the session time zone, so it can be an hour off only in
  the repeated hour of a daylight-saving change), not judged. Router ports come from the ``attributes`` keys MySQL Shell reads
  (``RWEndpoint``, ``ROEndpoint``, ``RWSplitEndpoint``, ``RWXEndpoint``,
  ``ROXEndpoint``; same file :107-146).
"""
import datetime
import re

from .connector import ConnectorError, terminal_text
from .perf_common import Reader, Server, _number, denied, now, preflight, reason
from .server_info import product_name

SCHEMA_VERSION = "cluster-1.0"
CLUSTER_TAG = "/*myflames:cluster*/ "
KINDS = ("galera", "pxc", "innodb_cluster", "group_replication", "none")
STATUSES = ("ok", "watch", "problem", "unknown")

#: wsrep_flow_control_paused at or above this fraction is "watch" (see the
#: module docstring: the documentation gives no number; 0.1 means writes
#: waited a tenth of the measured time).
FLOW_CONTROL_WATCH = 0.1

#: The Galera variables the membership and judgement read (server_checks.py
#: reads the same names for the Full analysis).
GALERA_VARIABLES = (
    "hostname", "port", "report_host", "wsrep_on", "wsrep_provider", "wsrep_cluster_name", "wsrep_cluster_address",
    "wsrep_node_name", "wsrep_node_address", "wsrep_node_incoming_address", "wsrep_desync", "pxc_strict_mode",
    "pxc_maint_mode",
)
#: Variables read with the standard preflight. A name the server does not
#: have is simply absent from the answer (SHOW GLOBAL VARIABLES WHERE ... IN).
EXTRA_VARIABLES = GALERA_VARIABLES + (
    "report_port", "server_uuid",
    "group_replication_group_name", "group_replication_single_primary_mode", "group_replication_local_address",
    "group_replication_group_seeds",
)

WSREP_STATUS_SQL = "SHOW GLOBAL STATUS WHERE Variable_name LIKE 'wsrep\\_%'"
WSREP_MEMBERS_SQL = "SELECT node_uuid, node_name, node_incoming_address FROM mysql.wsrep_cluster_members"
PXC_VIEW_SQL = "SELECT HOST_NAME, UUID, STATUS, LOCAL_INDEX, SEGMENT FROM performance_schema.pxc_cluster_view"
_GR_MEMBERS_COLUMNS = ("CHANNEL_NAME", "MEMBER_ID", "MEMBER_HOST", "MEMBER_PORT", "MEMBER_STATE", "MEMBER_ROLE",
                       "MEMBER_VERSION")
_GR_STATS_SQL = (
    "SELECT MEMBER_ID, VIEW_ID, COUNT_TRANSACTIONS_IN_QUEUE, COUNT_TRANSACTIONS_CHECKED, "
    "COUNT_CONFLICTS_DETECTED, COUNT_TRANSACTIONS_ROWS_VALIDATING, COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE, "
    "COUNT_TRANSACTIONS_REMOTE_APPLIED, COUNT_TRANSACTIONS_LOCAL_PROPOSED, COUNT_TRANSACTIONS_LOCAL_ROLLBACK "
    "FROM performance_schema.replication_group_member_stats")
METADATA_SCHEMA = "mysql_innodb_cluster_metadata"
_MD_SCHEMA_SQL = ("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = '%s'"
                  % METADATA_SCHEMA)
_MD_VERSION_SQL = "SELECT major, minor, patch FROM %s.schema_version" % METADATA_SCHEMA
_MD_CLUSTERS_SQL = ("SELECT cluster_id, cluster_name, primary_mode, group_name FROM %s.v2_gr_clusters"
                    % METADATA_SCHEMA)
_MD_INSTANCES_SQL = ("SELECT instance_id, cluster_id, label, mysql_server_uuid, address, endpoint, "
                     "attributes->>'$.instance_type' FROM %s.v2_instances" % METADATA_SCHEMA)
#: The channel MySQL Shell sets up on a read replica (Sh/modules/adminapi/common/common.h).
READ_REPLICA_CHANNEL = "read_replica_replication"
_RR_CONNECTION_SQL = ("SELECT SERVICE_STATE, LAST_ERROR_NUMBER, LAST_ERROR_MESSAGE FROM "
                      "performance_schema.replication_connection_status WHERE CHANNEL_NAME = '%s'"
                      % READ_REPLICA_CHANNEL)
_RR_APPLIER_SQL = ("SELECT SERVICE_STATE FROM performance_schema.replication_applier_status WHERE CHANNEL_NAME = '%s'"
                   % READ_REPLICA_CHANNEL)
#: Router ports, read by key so the rest of the attributes (configuration,
#: the metadata account name) never leave the server.
ROUTER_PORTS = ("RWEndpoint", "ROEndpoint", "RWSplitEndpoint", "RWXEndpoint", "ROXEndpoint")
_ROUTER_PORT_SQL = ", ".join("r.attributes->>'$.%s'" % key for key in ROUTER_PORTS)


def _routers_sql(stats):
    """v2_routers; with metadata 2.4.0+ the check-in time from router_stats
    first, as MySQL Shell reads it. The check-in as the server shows it, its
    age, the ports, then its UNIX time (UTC, whatever the session time zone)."""
    check_in = "COALESCE(s.last_check_in, r.last_check_in)" if stats else "r.last_check_in"
    join = (" LEFT JOIN %s.router_stats AS s ON s.router_id = r.router_id" % METADATA_SCHEMA) if stats else ""
    return ("SELECT r.router_id, r.cluster_id, r.router_name, r.product_name, r.address, r.version, %s, "
            "TIMESTAMPDIFF(SECOND, %s, NOW()), %s, UNIX_TIMESTAMP(%s) FROM %s.v2_routers AS r%s"
            % (check_in, check_in, _ROUTER_PORT_SQL, check_in, METADATA_SCHEMA, join))


#: Galera local states (wsrep_local_state_comment) and how myflames judges
#: them; the sentence never repeats the state word (the title carries it).
#: Donor/Desynced is worded from wsrep_desync (_donor_text).
GALERA_STATES = {
    "synced": ("ok", "A full member, applying and accepting writes."),
    "joined": ("watch", "State transfer received, catching up before Synced."),
    "initialized": ("problem", "Not in a Primary component (non-Primary or disconnected); it does not apply or "
                               "accept writes."),
}
#: performance_schema.pxc_cluster_view STATUS (the Galera provider's node
#: states): (the word the node itself would use, status, sentence).
PXC_STATES = {
    "SYNCED": ("Synced", "ok", "A full member, applying and accepting writes."),
    "DONOR": ("Donor/Desynced", "watch", "Serving a state transfer to a joining node, or desynchronized "
                                         "(pxc_cluster_view reads DONOR for both); it may fall behind the cluster. "
                                         "Connect to it to see which."),
    "JOINER": ("Joiner", "watch", "Receiving a state transfer (SST or IST) to join the cluster."),
    "JOINED": ("Joined", "watch", "State transfer received, catching up before Synced."),
    "PRIMARY": ("Joining", "watch", "In the Primary component but not joined yet: its state transfer has not "
                                    "started (the Galera provider's PRIMARY node state)."),
    "NON-PRIMARY": ("Non-Primary", "watch", "Not in a Primary component in this node's view; it does not apply or "
                                            "accept writes."),
}
PXC_STATES["DONOR/DESYNCED"] = PXC_STATES["DESYNCED"] = PXC_STATES["DONOR"]
PXC_STATES["NON_PRIMARY"] = PXC_STATES["NON-PRIMARY"]
#: Group Replication member states (MEMBER_STATE), on the one scale above.
GR_STATES = {
    "ONLINE": ("ok", "A full member of the group."),
    "RECOVERING": ("watch", "Joining the group and applying what it missed (distributed recovery)."),
    "OFFLINE": ("problem", "Group Replication is not running on this member."),
    "ERROR": ("problem", "The member left the group after an error; it does not apply or accept writes."),
    "UNREACHABLE": ("watch", "This server cannot reach the member; it may be down or partitioned. The group "
                             "expels it after group_replication_member_expel_timeout."),
    "MISSING": ("watch", "Listed in the InnoDB Cluster metadata but not in the group: it may be stopped, or "
                         "Group Replication is not running on it."),
}
_RANK = {"ok": 0, "unknown": 1, "watch": 2, "problem": 3}
PORT_ZERO_NOTE = ("%s reports port 0 for %s, so only the host is shown: with wsrep_node_incoming_address AUTO the "
                  "node takes the port mysqld had when wsrep started. Set wsrep_node_incoming_address to host:port "
                  "to report the client port.")
#: Certification failures: one rule and one sentence for the Cluster place
#: and the Full analysis (checks.wsrep). The counter is the provider's.
CERT_FAILURES_TEXT = ("wsrep_local_cert_failures is %s: transactions from this node that certification rolled "
                      "back because they conflicted with a write from another node. It is a cumulative count kept "
                      "by the Galera provider, not a current rate, and applications may have retried them.")
CERT_FAILURES_CAVEAT = ("Certification failures count this node's transactions that certification rolled back, "
                        "cumulative since the Galera provider started; the server asks the provider to reset its "
                        "statistics on FLUSH GLOBAL STATUS (also on FLUSH STATUS before MariaDB 11.5 and on MySQL), "
                        "and whether this counter resets is up to the provider. It is not a current failure rate, "
                        "and applications may retry them.")
NOT_PRIMARY_RUNS = ("SET, SHOW, SELECT without tables, reads of only information_schema or performance_schema "
                    "tables, and reads with wsrep_dirty_reads still run")


def _text(value):
    return value if value not in (None, "") else None


def _on(value):
    if value is None:
        return None
    return str(value).strip().upper() in ("ON", "1", "YES", "TRUE")


def _worse(a, b):
    return a if _RANK[a] >= _RANK[b] else b


def _split_address(address):
    """('host', port or None) from 'host:port', '[v6]:port' or 'host'."""
    address = (address or "").strip()
    if not address:
        return None, None
    match = re.match(r"^\[([^\]]+)\](?::(\d+))?$", address)
    if match:
        return match.group(1), (_number(match.group(2)) or None) if match.group(2) else None
    if address.count(":") == 1:
        host, port = address.rsplit(":", 1)
        # Port 0 means the node did not know its client port when it
        # registered the address (see PORT_ZERO_NOTE).
        return host or None, (_number(port) or None) if port.isdigit() else None
    return address, None


def join_address(host, port):
    """'host:port', with an IPv6 host in brackets; the host alone without a port."""
    if not host:
        return None
    shown = "[%s]" % host if ":" in host and not host.startswith("[") else host
    return "%s:%s" % (shown, port) if port else shown


def _port_zero(address):
    address = (address or "").strip()
    return bool(re.search(r":0$", address))


def _addresses(text):
    """wsrep_incoming_addresses as a list (comma-separated host:port)."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def cluster_address_entries(address):
    """The distinct (host, port) entries of wsrep_cluster_address
    ('gcomm://a,b:4567,[v6]:4567?opts'), hosts in lower case and port None
    when not given; [] for a bootstrap address ('gcomm://'), None when it is
    not a gcomm URL."""
    match = re.match(r"^\s*gcomm://([^?]*)", address or "")
    if not match:
        return None
    out = []
    for part in match.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        host, port = _split_address(part)
        entry = ((host or part).lower(), port)
        if entry not in out:
            out.append(entry)
    return out


def cluster_address_nodes(address):
    """How many nodes wsrep_cluster_address names; 0 for a bootstrap
    address, None when it is not a gcomm URL."""
    entries = cluster_address_entries(address)
    return None if entries is None else len(entries)


_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _is_ip(host):
    return bool(_IPV4.match(host or "")) or ":" in (host or "")


def _label(host):
    """The first DNS label of a host name (an address unchanged), lower case."""
    host = (host or "").strip().lower()
    return host if not host or _is_ip(host) else host.split(".", 1)[0]


def _named_by(host, names):
    """The entry host equals one of *names*, or a short host name matches the
    first DNS label of a fully qualified one (db1 and db1.example.com); two
    different domains (db1.red.example, db1.blue.example) never match."""
    if host in names:
        return True
    if _is_ip(host):
        return False
    short = "." not in host
    for name in names:
        if not name or _is_ip(name) or ("." not in name) == short:
            continue
        if (_label(name) == host) if short else (name == _label(host)):
            return True
    return False


def _entry_text(entry):
    return join_address(entry[0], entry[1])


def galera_membership(variables, status, members=()):
    """What wsrep_cluster_address says about the expected membership.

    *members* are the component's members as built for the report (``host``,
    ``name``, ``local``). Returns ``entries`` (distinct (host, port)),
    ``names_this_node`` (True, False when proved absent, None when this node
    cannot be identified), ``expected`` (the expected size, or None when the
    list cannot give one) and ``unmatched`` (entries that match no member).
    One rule for the Cluster place and the Full analysis's checks.wsrep.
    """
    entries = cluster_address_entries(variables.get("wsrep_cluster_address"))
    out = {"entries": entries, "names_this_node": None, "expected": None, "unmatched": []}
    if not entries:
        return out
    own_names = set()
    for name in ("hostname", "report_host", "wsrep_node_name"):
        value = (variables.get(name) or "").strip().lower()
        if value:
            own_names.add(value)
    own_endpoints = set()
    host, port = _split_address(variables.get("wsrep_node_address"))
    if host:
        own_names.add(host.lower())
        if port:
            own_endpoints.add((host.lower(), port))
    incoming = (variables.get("wsrep_node_incoming_address") or "").strip()
    if incoming and incoming.upper() != "AUTO":
        host, _port = _split_address(incoming)
        if host:
            own_names.add(host.lower())
    others = []
    for m in members or ():
        names = set(n.strip().lower() for n in (m.get("host"), m.get("name")) if n)
        if m.get("local"):
            own_names |= names
        else:
            others.append(names)
    # Entries this node's names match (by host or name, not by port): a match
    # that several entries share (instances on one host, two domains under
    # one short name) does not say which one is this node.
    candidates = [e for e in entries if _named_by(e[0], own_names)]

    def own(entry):
        h, p = entry
        if p and any(eh == h for eh, _ep in own_endpoints):
            # Instances on one host differ by their group-communication port.
            return (h, p) in own_endpoints
        if entry in candidates:
            return True if len(candidates) == 1 else None
        return False

    def other(entry):
        return any(_named_by(entry[0], names) for names in others)

    def excluded(entry):
        h, p = entry
        return other(entry) or bool(p and any(eh == h and ep != p for eh, ep in own_endpoints))

    verdicts = [own(e) for e in entries]
    if any(v is True for v in verdicts):
        listed = True
    elif any(v is None for v in verdicts) or not all(excluded(e) for e in entries):
        listed = None
    else:
        listed = False
    out["names_this_node"] = listed
    if listed is not None:
        out["expected"] = len(entries) + (0 if listed else 1)
    out["unmatched"] = [e for e, v in zip(entries, verdicts) if v is not True and not other(e)]
    return out


def configured_nodes(variables, status=None, members=()):
    """The expected size wsrep_cluster_address gives, or None (bootstrap
    address, no gcomm URL, or this node not identified)."""
    return galera_membership(variables, status or {}, members)["expected"]


def is_galera_node(variables):
    """True when the server runs with a Galera provider and wsrep on.

    MariaDB has a global ``wsrep_on`` (ON only with a provider loaded).
    Percona XtraDB Cluster 8.4 has ``wsrep_on`` as a session-only variable, so
    SHOW GLOBAL VARIABLES does not list it (verified live on PXC 8.4.11:
    ``SELECT @@global.wsrep_on`` fails with error 1238); there the provider
    path in ``wsrep_provider`` (``none`` when not loaded) decides.
    """
    wsrep_on = _on(variables.get("wsrep_on"))
    if wsrep_on is not None:
        return wsrep_on
    return provider_loaded(variables)


def provider_loaded(variables):
    """wsrep_provider names a provider library (not empty, not 'none')."""
    provider = (variables.get("wsrep_provider") or "").strip()
    return bool(provider) and provider.lower() != "none"


def flow_percent(value):
    """wsrep_flow_control_paused as one percent text everywhere: one decimal,
    cut rather than rounded up (a value under the watch line never reads as
    it), '<0.1%' for a small non-zero value."""
    if value is None:
        return "?"
    pct = float(value) * 100
    if pct <= 0:
        return "0%"
    if pct < 0.1:
        return "<0.1%"
    tenths = int(round(pct * 10, 6))
    text = "%d.%d" % divmod(tenths, 10)
    return (text[:-2] if text.endswith(".0") else text) + "%"


def age_text(seconds):
    """'4 s ago', '3 min ago', '2 h ago', '5 days ago' (the workspace's ageText)."""
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    if seconds < 120:
        return "%d s ago" % seconds
    if seconds < 7200:
        return "%d min ago" % int(round(seconds / 60.0))
    if seconds < 172800:
        return "%d h ago" % int(round(seconds / 3600.0))
    return "%d days ago" % int(round(seconds / 86400.0))


def _finding(report, status, key, title, text, member=None):
    item = {"id": key, "status": status, "title": title, "text": text}
    if member:
        item["member"] = member
    report["findings"].append(item)


def _member(key, name=None, address=None, uuid=None, state=None, state_source=None, role=None, version=None,
            local=False, host=None, port=None):
    if host is None:
        host, port = _split_address(address)
    return {"key": key, "name": name or host or address or uuid or key, "address": address, "host": host,
            "port": port, "uuid": uuid, "state": state, "state_source": state_source, "role": role,
            "version": version, "local": bool(local), "status": "unknown", "status_why": [], "metrics": {}}


def _base(server):
    variables = server.variables
    return {
        "schema_version": SCHEMA_VERSION,
        "engine": server.engine,
        "flavor": server.flavor,
        "server_product": server.product,
        "server_version": server.version or None,
        "version_comment": _text(variables.get("version_comment")),
        "captured_at": now(),
        "uptime_s": server.uptime,
        "performance_schema": server.ps_on,
        "available": True,
        "message": None,
        "kind": "none",
        "server": {
            "hostname": _text(variables.get("hostname")),
            "port": _number(variables.get("port")),
            "report_host": _text(variables.get("report_host")),
            "server_uuid": _text(variables.get("server_uuid")),
        },
        "cluster": {"name": None, "uuid": None, "size": None, "expected_size": None, "expected_source": None,
                    "component": None, "quorum": None, "failures_tolerated": None, "topology": None,
                    "provider": None},
        "status": "unknown",
        "status_why": [],
        "local": None,
        "members": [],
        "galera": None,
        "group_replication": None,
        "metadata": None,
        "routers": [],
        "findings": [],
        "summary": {"members": 0, "ok": 0, "watch": 0, "problem": 0, "unknown": 0},
        "statements": [],
        "notes": [],
    }


def read_status(conn):
    """The ``cluster-1.0`` report for the server *conn* is connected to.

    Read-only. Connection failures raise ConnectorError; an answer myflames
    cannot read raises ValueError. A server that is not part of a cluster
    returns ``kind: "none"`` with the reason in ``message``; an account that
    cannot read the cluster tables returns ``available: false``.
    """
    reader = Reader(conn, CLUSTER_TAG)
    try:
        server = preflight(reader, EXTRA_VARIABLES)
    except ConnectorError as exc:
        if "Access denied" not in str(exc):
            from . import proxysql
            try:
                is_proxy, proxy_version = proxysql.identify(conn)
            except ConnectorError:
                is_proxy, proxy_version = False, None
            if is_proxy:
                report = _base(Server({"version": proxy_version or ""}, {}))
                report["server_product"] = "ProxySQL Admin"
                report["server_version"] = proxy_version
                report["message"] = "Not part of a cluster: this connection is a ProxySQL Admin interface."
                report["statements"] = [
                    "SELECT variable_value FROM global_variables WHERE variable_name = 'admin-version'",
                    "SELECT @@version",
                ]
                _finish(report)
                return report
        raise
    report = _base(server)
    report["statements"].append("SHOW GLOBAL VARIABLES")
    wsrep_on = _on(server.var("wsrep_on"))
    if is_galera_node(server.variables):
        _read_galera(reader, server, report)
    elif server.engine == "mariadb":
        if wsrep_on is False and provider_loaded(server.variables):
            _read_galera_off(reader, server, report)
        else:
            report["message"] = ("Not part of a cluster: wsrep_on is %s and no Galera provider is loaded "
                                 "(MariaDB has no Group Replication)."
                                 % ("OFF" if wsrep_on is False else "not set"))
    else:
        _read_group_replication(reader, server, report, wsrep_on)
    _finish(report)
    return report


# ---- Galera and Percona XtraDB Cluster ------------------------------------------------

def galera_kind(variables):
    return "pxc" if "xtradb cluster" in (variables.get("version_comment") or "").lower() else "galera"


#: Server-level wsrep status the Galera rules need; without one of them (or
#: with a flow-control fraction out of range) nothing is judged, here and in
#: the Full analysis's checks.wsrep.
GALERA_REQUIRED_STATUS = ("wsrep_cluster_status", "wsrep_connected", "wsrep_ready", "wsrep_cluster_size")


def galera_incomplete(status):
    """What the shared Galera rules cannot do without: missing required names
    and invalid values (empty when the status is complete enough)."""
    status = status or {}
    out = [name for name in GALERA_REQUIRED_STATUS if _text(status.get(name)) is None]
    size = _number(status.get("wsrep_cluster_size"))
    if _text(status.get("wsrep_cluster_size")) is not None and not (isinstance(size, int) and size >= 0):
        out.append("wsrep_cluster_size (%s, not a node count)" % status.get("wsrep_cluster_size"))
    paused = _number(status.get("wsrep_flow_control_paused"))
    if _text(status.get("wsrep_flow_control_paused")) is not None and not (paused is not None and 0 <= paused <= 1):
        out.append("wsrep_flow_control_paused (%s, not a fraction)" % status.get("wsrep_flow_control_paused"))
    return out


def _galera_metrics(status):
    def num(name):
        return _number(status.get(name))
    return {
        "local_state": num("wsrep_local_state"),
        "local_state_comment": _text(status.get("wsrep_local_state_comment")),
        "ready": _on(status.get("wsrep_ready")),
        "connected": _on(status.get("wsrep_connected")),
        "evs_state": _text(status.get("wsrep_evs_state")),
        "local_recv_queue": num("wsrep_local_recv_queue"),
        "local_recv_queue_avg": num("wsrep_local_recv_queue_avg"),
        "local_send_queue": num("wsrep_local_send_queue"),
        "local_send_queue_avg": num("wsrep_local_send_queue_avg"),
        "flow_control_paused": num("wsrep_flow_control_paused"),
        "flow_control_active": _on(status.get("wsrep_flow_control_active")),
        "flow_control_sent": num("wsrep_flow_control_sent"),
        "flow_control_recv": num("wsrep_flow_control_recv"),
        "cert_deps_distance": num("wsrep_cert_deps_distance"),
        "local_cert_failures": num("wsrep_local_cert_failures"),
        "local_bf_aborts": num("wsrep_local_bf_aborts"),
        "desync_count": num("wsrep_desync_count"),
        "last_committed": num("wsrep_last_committed"),
        "local_index": num("wsrep_local_index"),
        "cluster_conf_id": num("wsrep_cluster_conf_id"),
        "cluster_weight": num("wsrep_cluster_weight"),
    }


def _donor_text(desync, kind):
    if _on(desync):
        return ("wsrep_desync is ON: this node was desynchronized on purpose. It stays Donor/Desynced, and can "
                "fall behind the cluster, until wsrep_desync is set to OFF.")
    if desync is not None and kind == "galera":
        return ("Serving a state transfer to a joining node, or desynchronized while FLUSH TABLES WITH READ LOCK or "
                "BACKUP STAGE BLOCK_DDL holds or an RSU schema change runs (wsrep_desync is OFF); it may fall "
                "behind the cluster.")
    if desync is not None:
        return ("Serving a state transfer to a joining node, or desynchronized for a lock or a schema change "
                "(wsrep_desync is OFF); it may fall behind the cluster.")
    return ("Serving a state transfer to a joining node, or desynchronized (wsrep_desync, a lock or a schema "
            "change); it may fall behind the cluster.")


def _galera_state(comment, desync=None, kind="galera"):
    """(status, sentence) for wsrep_local_state_comment; the sentence does
    not repeat the state."""
    text = (comment or "").strip()
    if not text:
        return "unknown", "wsrep_local_state_comment was not reported."
    low = text.lower()
    if low == "donor/desynced":
        return "watch", _donor_text(desync, kind)
    known = GALERA_STATES.get(low)
    if known:
        return known
    if low.startswith("joining") or low == "joiner":
        return "watch", "Joining the cluster through a state transfer."
    return "problem", "Not Synced."


def reset_statement(report):
    """The statement that resets the Galera provider's statistics here: FLUSH
    GLOBAL STATUS from MariaDB 11.5.1 (a plain FLUSH STATUS resets only the
    session there), FLUSH STATUS before and on MySQL."""
    version = tuple(int(p) for p in re.findall(r"\d+", report.get("server_version") or "")[:3])
    if report.get("engine") == "mariadb" and version >= (11, 5, 1):
        return "FLUSH GLOBAL STATUS"
    return "FLUSH STATUS"


def _joining(metrics):
    comment = (metrics.get("local_state_comment") or "").strip().lower()
    return metrics.get("connected") is not False and (comment.startswith("joining") or comment == "joiner")


def _pxc_state(state):
    """(word, status, sentence) for a pxc_cluster_view STATUS."""
    text = (state or "").strip()
    if not text:
        return None, "unknown", "No state reported."
    known = PXC_STATES.get(text.upper())
    if known:
        return known
    return text[:1].upper() + text[1:].lower(), "watch", "Not Synced (pxc_cluster_view reads %s)." % text


def _read_galera(reader, server, report):
    report["kind"] = galera_kind(server.variables)
    try:
        status = reader.pairs(WSREP_STATUS_SQL)
    except ConnectorError as exc:
        if denied(exc):
            report["available"] = False
            report["message"] = "This account cannot read the wsrep status variables: %s" % reason(exc)
            return
        raise
    report["statements"].append("SHOW GLOBAL STATUS (wsrep_%)")
    table = reader.optional(WSREP_MEMBERS_SQL, "mysql.wsrep_cluster_members", report["notes"])
    if table is not None:
        report["statements"].append("SELECT mysql.wsrep_cluster_members")
    pxc = None
    if report["kind"] == "pxc":
        pxc = reader.optional(PXC_VIEW_SQL, "performance_schema.pxc_cluster_view", report["notes"])
        if pxc is not None:
            report["statements"].append("SELECT performance_schema.pxc_cluster_view")
    build_galera(report, server.variables, status, table, pxc)


def _read_galera_off(reader, server, report):
    """MariaDB with the global wsrep_on OFF and a provider loaded: still a
    member while the provider stays connected, but new sessions do not
    replicate (module docstring)."""
    try:
        status = reader.pairs(WSREP_STATUS_SQL)
    except ConnectorError as exc:
        if not denied(exc):
            raise
        # Unreadable is not disconnected: the node may still be in a cluster.
        report["kind"] = "galera"
        report["available"] = False
        report["message"] = ("wsrep_on is OFF with a Galera provider loaded, and this account cannot read the wsrep "
                             "status variables, so whether this node is still in a cluster is not known: %s"
                             % reason(exc))
        return
    connected = _on(status.get("wsrep_connected"))
    size = _number(status.get("wsrep_cluster_size"))
    if not (connected or size):
        report["message"] = ("Not part of a cluster: wsrep_on is OFF, and the Galera provider (wsrep_provider %s) "
                             "is not connected." % server.variables.get("wsrep_provider"))
        return
    report["statements"].append("SHOW GLOBAL STATUS (wsrep_%)")
    report["kind"] = "galera"
    table = reader.optional(WSREP_MEMBERS_SQL, "mysql.wsrep_cluster_members", report["notes"])
    if table is not None:
        report["statements"].append("SELECT mysql.wsrep_cluster_members")
    build_galera(report, server.variables, status, table, None, wsrep_off=True)


def build_galera(report, variables, status, table=None, pxc=None, wsrep_off=False):
    """Fill *report* (from _base) for a Galera or PXC node from what was
    read: the wsrep status, mysql.wsrep_cluster_members and
    pxc_cluster_view rows (None when not read). Pure."""
    report["kind"] = "galera" if wsrep_off else galera_kind(variables)
    metrics = _galera_metrics(status)
    cluster = report["cluster"]
    cluster["name"] = _text(variables.get("wsrep_cluster_name"))
    cluster["uuid"] = _text(status.get("wsrep_cluster_state_uuid"))
    cluster["size"] = _number(status.get("wsrep_cluster_size"))
    cluster["component"] = _text(status.get("wsrep_cluster_status"))
    cluster["quorum"] = None if cluster["component"] is None else cluster["component"] == "Primary"
    cluster["provider"] = {"name": _text(status.get("wsrep_provider_name")),
                           "version": _text(status.get("wsrep_provider_version")),
                           "vendor": _text(status.get("wsrep_provider_vendor"))}
    report["galera"] = {
        "node_name": _text(variables.get("wsrep_node_name")),
        "node_address": _text(variables.get("wsrep_node_address")),
        "cluster_address": _text(variables.get("wsrep_cluster_address")),
        "incoming_addresses": _addresses(status.get("wsrep_incoming_addresses")),
        "gcomm_uuid": _text(status.get("wsrep_gcomm_uuid")),
        "desync": _text(variables.get("wsrep_desync")),
        "pxc_strict_mode": _text(variables.get("pxc_strict_mode")),
        "pxc_maint_mode": _text(variables.get("pxc_maint_mode")),
        "wsrep_on": _text(variables.get("wsrep_on")),
        "members_source": None,
        "address_nodes": None,
        "address_names_this_node": None,
        "missing": [],
        "metrics": metrics,
    }
    if not status:
        report["notes"].append("The server reported no wsrep status variables, so the Galera provider may not be "
                               "loaded (wsrep_provider is %s)." % (variables.get("wsrep_provider") or "not set"))
    report["galera"]["incomplete"] = galera_incomplete(status)
    if status and report["galera"]["incomplete"]:
        report["notes"].append("Not judged: the server did not report %s." % ", ".join(report["galera"]["incomplete"]))
    _galera_members(report, metrics, table, pxc, variables)
    membership = galera_membership(variables, status, report["members"])
    _expected_size(report, membership)
    _judge_galera(report, metrics, membership, wsrep_off)
    incomplete = report["galera"]["incomplete"]
    if incomplete:
        # The rules cannot run on what the server reported: nothing is judged
        # (no finding, no member verdict), here and in checks.wsrep.
        report["findings"] = []
        report["cluster"]["failures_tolerated"] = None
        for member in report["members"]:
            member["status"] = "unknown"
            member["status_why"] = ["Not judged: the server did not report %s." % ", ".join(incomplete)]


def galera_status(server, status, table=None, pxc=None):
    """The finished cluster-1.0 report of a Galera or PXC node from rows
    already read (the Full analysis's checks.wsrep uses it, so both judge
    alike)."""
    report = _base(server)
    build_galera(report, server.variables, status, table, pxc,
                 wsrep_off=_on(server.variables.get("wsrep_on")) is False)
    _finish(report)
    return report


def _galera_members(report, metrics, table, pxc, variables):
    galera = report["galera"]
    notes = report["notes"]
    primary = report["cluster"]["component"] == "Primary"
    gcomm = galera["gcomm_uuid"]
    node_name = galera["node_name"]
    members = []
    by_uuid = {}
    if table and primary:
        for row in table:
            row = list(row) + [None] * (3 - len(row))
            uuid, name, address = _text(row[0]), _text(row[1]), _text(row[2])
            member = _member("node:%s" % (uuid or name or address), name, address, uuid,
                             local=bool(gcomm and uuid == gcomm))
            members.append(member)
            if uuid:
                by_uuid[uuid] = member
        galera["members_source"] = "wsrep_cluster_members"
    else:
        if table and not primary:
            notes.append("mysql.wsrep_cluster_members is written only for a Primary component, so on this node it "
                         "lists the last Primary view; the members shown come from wsrep_incoming_addresses.")
        incoming = galera["incoming_addresses"]
        index = metrics.get("local_index")
        for i, address in enumerate(incoming):
            # The provider lists the incoming addresses in view order, the
            # same order wsrep_local_index counts in.
            local = index is not None and len(incoming) == (report["cluster"]["size"] or -1) and i == index
            if local and gcomm:
                # The same key as in a Primary view (node_uuid is the gcomm UUID).
                members.append(_member("node:%s" % gcomm, node_name, address, gcomm, local=True))
            else:
                members.append(_member("node:%s" % address, node_name if local else None, address, local=local))
        galera["members_source"] = "wsrep_incoming_addresses"
    if not any(m["local"] for m in members) and node_name:
        named = [m for m in members if m["name"] == node_name]
        if len(named) == 1:
            named[0]["local"] = True
    if not any(m["local"] for m in members):
        # This node is a member of whatever component it is in; when its own
        # row cannot be matched it is still shown, once.
        own = report["server"]
        address = join_address(own["hostname"], own["port"] or 3306) if own["hostname"] else None
        members.insert(0, _member("node:%s" % gcomm if gcomm else "node:local",
                                  node_name or galera["node_address"] or "This server", address, gcomm, local=True))
    for m in members:
        if m["local"]:
            m["uuid"] = m["uuid"] or gcomm
    if pxc:
        # Members read from wsrep_incoming_addresses carry no UUID and name;
        # pxc_cluster_view's LOCAL_INDEX counts in the same view order, so a
        # row is matched by its index when both describe the whole component.
        by_index = galera["members_source"] == "wsrep_incoming_addresses" and \
            len(members) == (report["cluster"]["size"] or -1) == len(pxc)
        for row in pxc:
            row = list(row) + [None] * (5 - len(row))
            host, uuid, state = _text(row[0]), _text(row[1]), _text(row[2])
            member = by_uuid.get(uuid) if uuid else None
            if member is None:
                member = next((m for m in members if m["name"] == host), None)
            index = _number(row[3])
            if member is None and by_index and isinstance(index, int) and 0 <= index < len(members):
                member = members[index]
                member["name"] = host or member["name"]
            if member is None:
                continue
            member["uuid"] = member["uuid"] or uuid
            member["pxc_status"] = state
            member["segment"] = _number(row[4])
            if not member["local"]:
                # The node's own words (Synced, Donor/Desynced); the raw
                # STATUS stays in pxc_status.
                member["state"] = _pxc_state(state)[0]
                member["state_source"] = "pxc_cluster_view"
    zero = sorted((m["name"] for m in members if _port_zero(m["address"])), key=_natural)
    if zero:
        notes.append(PORT_ZERO_NOTE % ("mysql.wsrep_cluster_members" if galera["members_source"] ==
                                       "wsrep_cluster_members" else "wsrep_incoming_addresses",
                                       ", ".join(zero)))
    report["members"] = members


def _names(items):
    items = list(items)
    return ", ".join(items[:10]) + (" and %d more" % (len(items) - 10) if len(items) > 10 else "")


def _expected_size(report, membership):
    cluster, galera = report["cluster"], report["galera"]
    entries = membership["entries"]
    if entries is None:
        return
    galera["address_nodes"] = len(entries)
    galera["address_names_this_node"] = membership["names_this_node"]
    if not entries:
        return
    size, expected = cluster["size"], membership["expected"]
    listed = ", ".join(_entry_text(e) for e in entries[:10])
    if expected is None:
        report["notes"].append(
            "This node is not identified in wsrep_cluster_address (%s): no entry matches its host name, "
            "wsrep_node_address, node name or incoming address alone, so the %d %s it names %s only a lower bound "
            "and the component size is not compared with it." % (
                listed, len(entries), "node" if len(entries) == 1 else "nodes", "is" if len(entries) == 1 else "are"))
        return
    if size is not None and size > expected:
        report["notes"].append(
            "wsrep_cluster_address names %d %s (%s), fewer than the %d in the component: a node needs only one "
            "reachable peer there, and an arbitrator (garbd) counts in the component without being listed, so the "
            "list does not give the cluster's size." % (expected, "node" if expected == 1 else "nodes", listed, size))
        return
    cluster["expected_size"] = expected
    cluster["expected_source"] = "wsrep_cluster_address"


def _judge_galera(report, metrics, membership, wsrep_off=False):
    cluster = report["cluster"]
    galera = report["galera"]
    component = cluster["component"]
    joining = _joining(metrics)
    outside = bool(component and component != "Primary")
    if wsrep_off:
        _finding(report, "problem", "wsrep_off", "This node does not replicate",
                 "wsrep_on is OFF globally: this node is still in the cluster and applies the other nodes' "
                 "writes, but writes from sessions started since are not replicated, so the data can diverge. SET "
                 "GLOBAL wsrep_on=ON replicates new sessions again; check the tables written meanwhile.")
    if outside and joining:
        _finding(report, "watch", "state_transfer", "This node is joining",
                 "wsrep_cluster_status is %s while this node receives its state transfer (%s): it is outside the "
                 "Primary component until the transfer ends, and refuses most queries until it is Synced (%s)."
                 % (component, metrics["local_state_comment"], NOT_PRIMARY_RUNS))
    elif outside:
        _finding(report, "problem", "non_primary", "Not in a Primary component",
                 "wsrep_cluster_status is %s: this node lost quorum or is not connected. Until it rejoins a "
                 "Primary component it refuses most queries (%s)." % (component, NOT_PRIMARY_RUNS))
    if metrics["connected"] is False and component == "Primary":
        _finding(report, "problem", "not_connected", "Not connected",
                 "wsrep_connected is OFF: this node has no group communication with the other nodes.")
    if metrics["ready"] is False and component == "Primary":
        # Ready is set when the node reaches Synced (wsrep_server_service.cc
        # log_state_change), so a joining node is expected to be not ready.
        transition = _galera_state(metrics["local_state_comment"])[0] == "watch"
        _finding(report, "watch" if transition else "problem", "not_ready", "Not ready",
                 "wsrep_ready is OFF: this node rejects almost every query%s."
                 % (" until it is Synced" if transition else ""))
    for member in report["members"]:
        if member["local"]:
            comment = metrics["local_state_comment"]
            member["state"] = comment
            member["state_source"] = "wsrep_local_state_comment" if comment else None
            member["metrics"] = dict((k, v) for k, v in metrics.items() if v is not None)
            status, why = _galera_state(comment, galera["desync"], report["kind"])
            if wsrep_off:
                # The provider's status (state, queues) is hidden while wsrep_on is OFF.
                member["state"] = comment or "Not replicating"
                status, why = "problem", ("wsrep_on is OFF: writes from new sessions on this node are not "
                                          "replicated. The provider's status (its state and queues) is hidden "
                                          "while wsrep_on is OFF.")
            elif outside and joining:
                status = "watch"
            elif outside:
                status, why = "problem", "In a %s component. %s" % (component, why)
            member["status"], member["status_why"] = status, [why]
            if status in ("watch", "problem") and not outside and not wsrep_off:
                _finding(report, status, "local_state", "This node is %s" % (comment or "?"), why, member["key"])
            report["local"] = member["key"]
        elif member["state_source"] == "pxc_cluster_view":
            _word, status, why = _pxc_state(member.get("pxc_status"))
            member["status"], member["status_why"] = status, [why]
            if status in ("watch", "problem"):
                _finding(report, status, "member_state", "%s is %s" % (member["name"], member["state"]), why,
                         member["key"])
        else:
            member["status_why"] = ["In the component as this node sees it. A node's state and queues are read on "
                                    "that node: connect to it to inspect them."]
    size, expected = cluster["size"], cluster["expected_size"]
    if size is not None and expected and size < expected and component == "Primary":
        missing = expected - size
        unmatched = membership["unmatched"]
        listed = membership["names_this_node"]
        names = "wsrep_cluster_address names %d%s" % (len(membership["entries"]), " (this node included)"
                                                       if listed else ", so %d with this node" % expected)
        if unmatched and len(unmatched) == missing:
            galera["missing"] = [_entry_text(e) for e in unmatched]
            text = "%s, and %s %s not in the component: %s down or still joining." % (
                names, _names(galera["missing"]), "is" if missing == 1 else "are",
                "it may be" if missing == 1 else "they may be")
        else:
            text = ("%s. %d configured %s not in the component and cannot be named here: the member names and "
                    "addresses do not match every entry (%s). %s down or still joining." % (
                        names, missing, "node is" if missing == 1 else "nodes are",
                        _names(_entry_text(e) for e in membership["entries"]),
                        "It may be" if missing == 1 else "They may be"))
        _finding(report, "watch", "size_below_configured", "Fewer nodes than configured",
                 "The component has %d %s; %s" % (size, "node" if size == 1 else "nodes", text))
    weight = metrics.get("cluster_weight")
    if component == "Primary" and size:
        if weight is not None and weight != size:
            report["notes"].append("wsrep_cluster_weight is %s for %d nodes: the nodes' pc.weight differ, so the "
                                   "failures the component tolerates are not computed here." % (weight, size))
        else:
            tolerated = (size - 1) // 2
            cluster["failures_tolerated"] = tolerated
            report["notes"].append("Failures tolerated assumes equal pc.weight on every node (the default): a "
                                   "Primary component needs more than half of the total weight.")
            if tolerated == 0:
                _finding(report, "watch", "no_tolerance", "Cannot survive another failure",
                         "A single node is the whole Primary component: if it fails, the cluster stops."
                         if size == 1 else
                         "With %d nodes in the Primary component, losing one more by a crash or a network partition "
                         "leaves the rest without a majority, so they go non-Primary and stop accepting writes "
                         "(assumes equal pc.weight)." % size)
    paused = metrics["flow_control_paused"]
    if paused is not None and FLOW_CONTROL_WATCH <= paused <= 1:
        _finding(report, "watch", "flow_control", "Flow control paused replication",
                 "wsrep_flow_control_paused is %s: replication waited for slow nodes %s of the time since the "
                 "provider's statistics were last reset (%s) or since start." % (
                     paused, flow_percent(paused), reset_statement(report)))
    failures = metrics["local_cert_failures"]
    if failures:
        _finding(report, "watch", "cert_failures", "Certification failures", CERT_FAILURES_TEXT % failures)
    maint = galera.get("pxc_maint_mode")
    if report["kind"] == "pxc" and maint and maint.upper() != "DISABLED":
        _finding(report, "watch", "pxc_maint_mode", "Maintenance mode",
                 "pxc_maint_mode is %s: a proxy that reads it stops sending new traffic to this node." % maint)
    if paused is not None:
        report["notes"].append("wsrep_flow_control_paused and the queue averages cover the time since the "
                               "provider's statistics were last reset (%s) or since start." % reset_statement(report))
    if any(m["state_source"] == "pxc_cluster_view" for m in report["members"]):
        report["notes"].append("The other nodes' states come from performance_schema.pxc_cluster_view on this "
                               "node; this node's own from wsrep_local_state_comment.")
    elif len(report["members"]) > 1:
        report["notes"].append("Galera exposes only this node's own state: the other nodes are listed as members "
                               "of the component without a verdict. Connect to a node to inspect it.")


# ---- Group Replication and InnoDB Cluster ------------------------------------------------

def _read_group_replication(reader, server, report, wsrep_on=None):
    variables = server.variables
    group_name = _text(variables.get("group_replication_group_name"))
    provider = (variables.get("wsrep_provider") or "").strip()
    no_galera = ("Galera is off (wsrep_on is OFF)" if wsrep_on is False else
                 "this server has no Galera provider" + (" (wsrep_provider is %s)" % provider if provider else ""))
    if tuple(server.version_tuple) < (8, 0, 0):
        report["message"] = ("Not part of a cluster: Group Replication status needs MySQL 8.0 or later, and %s."
                             % no_galera)
        return
    if not server.ps_on:
        if group_name:
            report["kind"] = "group_replication"
            report["cluster"]["uuid"] = group_name
            report["notes"].append("Performance Schema is off, so the Group Replication members cannot be read "
                                   "(performance_schema.replication_group_members).")
            report["status_why"].append("Group members not readable: Performance Schema is off.")
        else:
            report["message"] = ("Not part of a cluster: %s, there is no Group Replication group name, and "
                                 "Performance Schema is off, so no group could be read." % no_galera)
        return
    columns = list(_GR_MEMBERS_COLUMNS)
    if tuple(server.version_tuple) >= (8, 0, 27):
        columns.append("MEMBER_COMMUNICATION_STACK")
    try:
        rows = reader.rows("SELECT %s FROM performance_schema.replication_group_members" % ", ".join(columns))
    except ConnectorError as exc:
        if denied(exc):
            report["available"] = False
            report["kind"] = "group_replication" if group_name else "none"
            report["cluster"]["uuid"] = group_name
            report["message"] = ("This account cannot read performance_schema.replication_group_members (it needs "
                                 "SELECT on performance_schema): %s" % reason(exc))
            return
        raise
    report["statements"].append("SELECT performance_schema.replication_group_members")
    uuid = report["server"]["server_uuid"]
    members = []
    for row in rows:
        row = list(row) + [None] * (len(columns) - len(row))
        member_id, host, port = _text(row[1]), _text(row[2]), _number(row[3])
        # MEMBER_HOST and MEMBER_PORT are separate columns: an IPv6 host is
        # bracketed only when an address is written.
        address = join_address(host, port)
        member = _member("member:%s" % (member_id or address), host, address, member_id, _text(row[4]),
                         "replication_group_members", _text(row[5]), _text(row[6]),
                         local=bool(uuid and member_id == uuid), host=host, port=port or None)
        if len(columns) > 7:
            member["communication_stack"] = _text(row[7])
        members.append(member)
    local = next((m for m in members if m["local"]), None)
    if not members or (len(members) == 1 and local is not None and local["state"] == "OFFLINE" and not group_name):
        if _read_read_replica(reader, report):
            return
        report["message"] = ("Not part of a cluster: %s, and Group Replication is %s."
                             % (no_galera, "not running on this server" if members else "not installed or not running"))
        return
    report["kind"] = "group_replication"
    report["members"] = members
    report["cluster"]["uuid"] = group_name
    single = _on(variables.get("group_replication_single_primary_mode"))
    report["cluster"]["topology"] = None if single is None else ("single_primary" if single else "multi_primary")
    report["group_replication"] = {
        "group_name": group_name,
        "single_primary_mode": _text(variables.get("group_replication_single_primary_mode")),
        "local_address": _text(variables.get("group_replication_local_address")),
        "group_seeds": _text(variables.get("group_replication_group_seeds")),
        "view_id": None,
    }
    stats = reader.optional(_GR_STATS_SQL, "performance_schema.replication_group_member_stats", report["notes"])
    if stats is not None:
        report["statements"].append("SELECT performance_schema.replication_group_member_stats")
        by_id = dict((m["uuid"], m) for m in members if m["uuid"])
        for row in stats:
            row = list(row) + [None] * (10 - len(row))
            member = by_id.get(_text(row[0]))
            if member is None:
                continue
            report["group_replication"]["view_id"] = report["group_replication"]["view_id"] or _text(row[1])
            member["metrics"] = {
                "transactions_in_queue": _number(row[2]), "transactions_checked": _number(row[3]),
                "conflicts_detected": _number(row[4]), "rows_validating": _number(row[5]),
                "applier_queue": _number(row[6]), "remote_applied": _number(row[7]),
                "local_proposed": _number(row[8]), "local_rollback": _number(row[9]),
            }
        if stats:
            report["notes"].append("Queue lengths and rows validating are current; the other counts are "
                                   "cumulative. Another member's numbers are the ones it last sent to this member.")
    _read_metadata(reader, report, group_name, local)
    _judge_group(report, local)


def _version(text):
    try:
        return tuple(int(part) for part in (text or "").split("."))
    except ValueError:
        return ()


def _metadata_base():
    return {"available": True, "schema_version": None, "cluster_id": None, "cluster_name": None,
            "primary_mode": None, "instances": [], "reason": None, "local_copy": None}


def _read_metadata_tables(reader, metadata, notes):
    """schema_version, v2_gr_clusters, v2_instances; the clusters (None when unreadable)."""
    version = reader.optional(_MD_VERSION_SQL, "the InnoDB Cluster metadata version", notes)
    if version:
        metadata["schema_version"] = ".".join(str(v) for v in version[0][:3])
        if metadata["schema_version"] == "0.0.0":
            notes.append("The InnoDB Cluster metadata schema version is 0.0.0, which MySQL Shell sets while it "
                         "upgrades the metadata.")
    return reader.optional(_MD_CLUSTERS_SQL, "the InnoDB Cluster metadata (v2_gr_clusters)", notes)


def _read_instances(reader, metadata, notes):
    instances = reader.optional(_MD_INSTANCES_SQL, "the InnoDB Cluster metadata (v2_instances)", notes) or []
    out = []
    for inst in instances:
        inst = list(inst) + [None] * (7 - len(inst))
        out.append({"cluster_id": _text(inst[1]), "label": _text(inst[2]), "server_uuid": _text(inst[3]),
                    "address": _text(inst[4]), "endpoint": _text(inst[5]), "instance_type": _text(inst[6])})
    return out


def _use_cluster(report, metadata, row):
    metadata["cluster_id"], metadata["cluster_name"], metadata["primary_mode"] = (
        _text(row[0]), _text(row[1]), _text(row[2]))
    report["kind"] = "innodb_cluster"
    report["cluster"]["name"] = metadata["cluster_name"]
    if report["cluster"]["topology"] is None and metadata["primary_mode"] in ("pm", "mm"):
        report["cluster"]["topology"] = "single_primary" if metadata["primary_mode"] == "pm" else "multi_primary"


def _read_metadata(reader, report, group_name, local):
    notes = report["notes"]
    found = reader.optional(_MD_SCHEMA_SQL, "information_schema.SCHEMATA", notes)
    if not found:
        report["metadata"] = dict(_metadata_base(), available=False,
                                  reason="No mysql_innodb_cluster_metadata schema is visible to this account, so "
                                         "this is a Group Replication group without InnoDB Cluster metadata (or "
                                         "the account cannot read it).")
        return
    metadata = _metadata_base()
    report["metadata"] = metadata
    clusters = _read_metadata_tables(reader, metadata, notes)
    if clusters is None:
        metadata["available"] = False
        metadata["reason"] = "The InnoDB Cluster metadata could not be read; see the notes."
        return
    report["statements"].append("SELECT mysql_innodb_cluster_metadata.v2_gr_clusters, v2_instances, v2_routers")
    match = [row for row in clusters if len(row) > 3 and group_name and _text(row[3]) == group_name]
    if not match:
        metadata["available"] = False
        metadata["reason"] = ("The InnoDB Cluster metadata lists no cluster for this group (%s)."
                              % (group_name or "no group name"))
        return
    _use_cluster(report, metadata, match[0])
    for inst in _read_instances(reader, metadata, notes):
        if inst.pop("cluster_id") == metadata["cluster_id"]:
            metadata["instances"].append(inst)
    known = dict((m["uuid"], m) for m in report["members"] if m["uuid"])
    # Shell's cluster.status() calls a group member that is in the metadata
    # but not in the group (MISSING). A member that is itself OFFLINE or in
    # ERROR sees no group, so it cannot tell.
    blind = local is None or local["state"] in ("OFFLINE", "ERROR")
    if blind and local is not None:
        metadata["local_copy"] = "left_group"
        notes.append("This member is not in the group, so the InnoDB Cluster metadata read here (instances, "
                     "routers and their check-in times) is its last copy from the group and may be out of date. "
                     "Connect to an ONLINE member for current values.")
    group_instances = 0
    for inst in metadata["instances"]:
        member = known.get(inst["server_uuid"])
        if member is not None:
            # MySQL Shell names instances by their metadata label.
            member["label"] = inst["label"]
            member["name"] = inst["label"] or member["name"]
            member["in_metadata"] = True
            if inst["endpoint"]:
                member["endpoint"] = inst["endpoint"]
            group_instances += 1
            continue
        if inst["instance_type"] == "read-replica":
            report["members"].append(_replica_member(inst))
            continue
        group_instances += 1
        missing = _member("member:%s" % (inst["server_uuid"] or inst["label"]), inst["label"],
                          inst["endpoint"] or inst["address"], inst["server_uuid"], None if blind else "MISSING",
                          "metadata")
        missing["in_metadata"] = True
        report["members"].append(missing)
    if group_instances:
        report["cluster"]["expected_size"] = group_instances
        report["cluster"]["expected_source"] = "mysql_innodb_cluster_metadata"
    _read_routers(reader, report, metadata)


def _replica_member(inst, local=False):
    replica = _member("member:%s" % (inst["server_uuid"] or inst["label"]), inst["label"],
                      inst["endpoint"] or inst["address"], inst["server_uuid"], None, "metadata", "READ_REPLICA",
                      local=local)
    replica["in_metadata"] = True
    replica["instance_type"] = "read-replica"
    return replica


def _read_routers(reader, report, metadata):
    stats = _version(metadata["schema_version"]) >= (2, 4, 0)
    routers = reader.optional(_routers_sql(stats), "the InnoDB Cluster metadata (v2_routers)", report["notes"]) or []
    for r in routers:
        r = list(r) + [None] * (9 + len(ROUTER_PORTS) - len(r))
        if _text(r[1]) not in (None, metadata["cluster_id"]):
            continue
        report["routers"].append(_router(r, metadata.get("local_copy") == "left_group"))


def _utc_text(epoch):
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return None
    when = datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _router(row, local_copy=None):
    ports = {}
    for key, value in zip(ROUTER_PORTS, row[8:8 + len(ROUTER_PORTS)]):
        if _text(value):
            ports[key] = str(value)
    age = _number(row[7])
    return {"id": _number(row[0]), "name": _text(row[2]), "product": _text(row[3]), "address": _text(row[4]),
            "version": _text(row[5]), "last_check_in": _text(row[6]),
            "last_check_in_age_s": None if age is None else max(0, age),
            "last_check_in_utc": _utc_text(row[8 + len(ROUTER_PORTS)]),
            "from_local_copy": bool(local_copy), "ports": ports}


def _read_read_replica(reader, report):
    """An InnoDB Cluster read replica: no group, but its @@server_uuid is a
    read-replica instance in its replicated copy of the metadata. Fills the
    report and returns True; False (nothing changed) otherwise."""
    notes = []
    found = reader.optional(_MD_SCHEMA_SQL, "information_schema.SCHEMATA", notes)
    if not found:
        return False
    metadata = _metadata_base()
    clusters = _read_metadata_tables(reader, metadata, notes) or []
    instances = _read_instances(reader, metadata, notes)
    uuid = report["server"]["server_uuid"]
    me = next((i for i in instances if uuid and i["server_uuid"] == uuid and i["instance_type"] == "read-replica"),
              None)
    row = next((r for r in clusters if me and len(r) > 3 and _text(r[0]) == me["cluster_id"]), None)
    if me is None or row is None:
        return False
    report["notes"].extend(notes)
    report["statements"].append("SELECT mysql_innodb_cluster_metadata.v2_gr_clusters, v2_instances, v2_routers")
    metadata["local_copy"] = "read_replica"
    report["metadata"] = metadata
    _use_cluster(report, metadata, row)
    report["cluster"]["uuid"] = _text(row[3])
    group_instances = 0
    for inst in instances:
        if inst.pop("cluster_id") != metadata["cluster_id"]:
            continue
        metadata["instances"].append(inst)
        if inst is me:
            continue
        if inst["instance_type"] == "read-replica":
            report["members"].append(_replica_member(inst))
            continue
        group_instances += 1
        member = _member("member:%s" % (inst["server_uuid"] or inst["label"]), inst["label"],
                         inst["endpoint"] or inst["address"], inst["server_uuid"], None, "metadata")
        member["in_metadata"] = True
        member["status_why"] = ["In the InnoDB Cluster metadata (this read replica's replicated copy). A read "
                                "replica is not in the group, so it cannot see the members' states: connect to a "
                                "group member to judge them."]
        report["members"].append(member)
    if group_instances:
        report["cluster"]["expected_size"] = group_instances
        report["cluster"]["expected_source"] = "mysql_innodb_cluster_metadata"
    local = _replica_member(me, local=True)
    report["members"].append(local)
    report["local"] = local["key"]
    report["notes"].append("This server is a read replica of the InnoDB Cluster: it replicates asynchronously over "
                           "the %s channel and is not a group member, so the group's quorum and members are not "
                           "visible here. The metadata read here is its replicated copy." % READ_REPLICA_CHANNEL)
    _judge_read_replica(reader, report, local)
    _read_routers(reader, report, metadata)
    return True


def _judge_read_replica(reader, report, local):
    """The read replica's own state from its read_replica_replication channel.
    A table that cannot be read leaves the state unknown unless what was read
    shows a failure on its own; a receiver that is CONNECTING (retrying, even
    with its last error kept) is watch, as MySQL Shell tells it apart from a
    stopped one (Sh/mysqlshdk/libs/mysql/replication.cc)."""
    connection = reader.optional(_RR_CONNECTION_SQL, "performance_schema.replication_connection_status",
                                 report["notes"])
    applier = reader.optional(_RR_APPLIER_SQL, "performance_schema.replication_applier_status", report["notes"])
    report["statements"].append("SELECT performance_schema.replication_connection_status, replication_applier_status "
                                "(%s)" % READ_REPLICA_CHANNEL)
    local["state_source"] = "replication_channel"
    channel = READ_REPLICA_CHANNEL
    if connection == [] and applier == []:
        why = "The InnoDB Cluster metadata lists this server as a read replica, but it has no %s channel." % channel
        local["state"], local["status"], local["status_why"] = "Not replicating", "problem", [why]
        _finding(report, "problem", "read_replica", "This read replica is not replicating", why, local["key"])
        return
    row = ((connection or [[]])[0] + [None] * 3)[:3] if connection else [None, None, None]
    receiver, error, message = _text(row[0]), _number(row[1]), _text(row[2])
    apply_state = _text(((applier or [[]])[0] + [None])[0]) if applier else None
    local["metrics"] = dict((k, v) for k, v in (("receiver_state", receiver), ("applier_state", apply_state),
                                                ("last_error_number", error)) if v is not None)
    last = " Its last error: %s %s." % (error, message or "") if error else ""
    if receiver == "OFF" or apply_state == "OFF":
        state, status, why = "Stopped", "problem", ("The %s channel is not running (receiver %s, applier %s): this "
                                                    "read replica does not replicate.%s" % (
                                                        channel, receiver or "not readable",
                                                        apply_state or "not readable", last))
    elif receiver == "CONNECTING":
        state, status, why = "Connecting", "watch", ("The %s channel's receiver is connecting to its source "
                                                     "(retrying).%s" % (channel, last))
    elif error and receiver is not None:
        state, status, why = "Error", "problem", "The %s channel reports error %s: %s" % (
            channel, error, message or "no message")
    elif receiver == "ON" and apply_state == "ON":
        state, status, why = "Replicating", "ok", ("Replicating from the cluster over the %s channel (receiver and "
                                                   "applier ON)." % channel)
    else:
        local["state"], local["status"] = None, "unknown"
        local["status_why"] = ["The %s channel could not be read in full (receiver %s, applier %s); see the notes."
                               % (channel, receiver or "not readable", apply_state or "not readable")]
        return
    local["state"], local["status"], local["status_why"] = state, status, [why]
    if status != "ok":
        _finding(report, status, "read_replica", "This read replica is %s" % state.lower(), why, local["key"])


def _judge_group(report, local):
    members = report["members"]
    in_group = [m for m in members if m["state_source"] == "replication_group_members"]
    cluster = report["cluster"]
    local_state = local["state"] if local else None
    blind = local_state in ("OFFLINE", "ERROR")
    # A member that left the group lists only itself: no size is claimed.
    cluster["size"] = None if blind else len(in_group)
    if local is None:
        report["notes"].append("This server's own row (server_uuid) was not among the group members.")
    if blind:
        cluster["quorum"] = None
        _finding(report, "problem", "local_not_in_group", "This member is %s" % local_state,
                 "%s It cannot see the group from here, so connect to an ONLINE member to judge the cluster."
                 % GR_STATES[local_state][1], local["key"])
    else:
        n = len(in_group)
        unreachable = len([m for m in in_group if m["state"] == "UNREACHABLE"])
        cluster["quorum"] = (n - unreachable) > n // 2
        if not cluster["quorum"]:
            _finding(report, "problem", "no_quorum", "No quorum",
                     "%d of %d members are UNREACHABLE from this member, so the group has lost its majority and "
                     "cannot process writes." % (unreachable, n))
        else:
            # The same majority rule over the current view (member_info.cc).
            cluster["failures_tolerated"] = max(0, (n - unreachable) - (n // 2 + 1))
            if cluster["failures_tolerated"] == 0:
                _finding(report, "watch", "no_tolerance", "Cannot survive another failure",
                         ("%d of %d members are reachable, so losing one more member loses the majority."
                          % (n - unreachable, n)) if unreachable else
                         ("With %d %s in the group, losing one more member loses the majority."
                          % (n, "member" if n == 1 else "members")))
    for member in members:
        if member.get("instance_type") == "read-replica":
            if not member["status_why"]:
                member["status_why"] = ["A read replica in the InnoDB Cluster metadata: it replicates from the "
                                        "cluster asynchronously and is not a group member. Connect to it to see its "
                                        "replication channel."]
            continue
        known = GR_STATES.get((member["state"] or "").upper())
        if member["state"] is None and member["state_source"] == "metadata":
            member["status_why"] = ["Listed in the InnoDB Cluster metadata. The connected member (%s) is %s and "
                                    "cannot see the group, so this instance's state is not visible here: connect "
                                    "to an ONLINE member." % (local["name"], local_state)
                                    if local is not None else
                                    "Listed in the InnoDB Cluster metadata. This server is not among the group "
                                    "members, so this instance's state is not visible here."]
            continue
        if known is None:
            member["status"], member["status_why"] = "unknown", ["State %s is not one myflames knows."
                                                                % (member["state"] or "not reported")]
            continue
        member["status"], member["status_why"] = known[0], [known[1]]
        metrics = member["metrics"]
        if metrics.get("transactions_in_queue") or metrics.get("applier_queue"):
            member["status_why"].append("Queues: %s in certification, %s in the applier." % (
                metrics.get("transactions_in_queue"), metrics.get("applier_queue")))
        if member["local"] and blind:
            continue
        if member["status"] != "ok":
            _finding(report, member["status"], "member_state", "%s is %s" % (member["name"], member["state"]),
                     known[1], member["key"])
    versions = sorted(set(m["version"] for m in in_group if m["version"]))
    if len(versions) > 1:
        _finding(report, "watch", "version_mismatch", "Members run different versions",
                 "Member versions: %s. Expected during a rolling upgrade. Until it ends, a new primary is "
                 "chosen among the lowest-version members, and in multi-primary mode higher-version members "
                 "stay read-only unless every version is in the same LTS series." % ", ".join(versions))
    expected = cluster["expected_size"]
    if expected and len(in_group) < expected and not blind and not any(m["state"] == "MISSING" for m in members):
        _finding(report, "watch", "size_below_configured", "Fewer members than configured",
                 "The group has %d %s; the InnoDB Cluster metadata lists %d."
                 % (len(in_group), "member" if len(in_group) == 1 else "members", expected))
    if local is not None:
        report["local"] = local["key"]
    primaries = [m for m in in_group if m["role"] == "PRIMARY"]
    if cluster["topology"] == "single_primary" and not blind and cluster["quorum"] and len(primaries) != 1:
        _finding(report, "problem" if not primaries else "watch", "primary_count",
                 "No primary" if not primaries else "More than one primary",
                 "Single-primary mode, and %d members report the PRIMARY role." % len(primaries))


# ---- summary, text -----------------------------------------------------------------

def _natural(text):
    """Sort key that puts node-2 before node-10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text or "")]


def _read_replica_here(report):
    """True when the connected server is an InnoDB Cluster read replica."""
    return any(m.get("local") and m.get("instance_type") == "read-replica" for m in report["members"])


def _ok_text(report):
    """Why the report is OK, from what was measured: quorum is claimed only
    when this server saw it. A read replica is not a group member, so the
    group's quorum and the failures it tolerates stay unknown (null)."""
    if _read_replica_here(report):
        return ("This read replica is replicating from the cluster over the %s channel. It is not a group member, "
                "so the group's quorum and the failures it tolerates are not visible here: connect to a group "
                "member to judge them." % READ_REPLICA_CHANNEL)
    if report["cluster"].get("quorum") is True:
        return "Every judged member is healthy and the cluster has quorum."
    return "Every judged member is healthy; quorum could not be measured from this server."


def _finish(report):
    # Members in name order (MySQL Shell lists its topology by label); the
    # server's own order follows UUIDs, which means nothing to a reader.
    report["members"].sort(key=lambda m: _natural(m["name"]))
    summary = report["summary"]
    summary["members"] = len(report["members"])
    for member in report["members"]:
        summary[member["status"]] += 1
    if report["kind"] == "none" or not report["available"]:
        report["status"] = "unknown"
        if report["message"] and not report["status_why"]:
            report["status_why"].append(report["message"])
        return
    status = "ok" if any(m["status"] != "unknown" for m in report["members"]) else "unknown"
    for item in report["findings"]:
        status = _worse(item["status"], status)
    if (report.get("galera") or {}).get("incomplete"):
        # The rules could not run on what the server reported (see notes).
        status = "unknown"
    report["status"] = status
    report["findings"].sort(key=lambda f: -_RANK[f["status"]])
    if report["findings"]:
        report["status_why"].extend(f["text"] for f in report["findings"])
    elif status == "ok":
        report["status_why"].append(_ok_text(report))
    else:
        report["status_why"].append("Nothing could be judged from this server.")


def at_or_worse(report, level):
    """Findings at *level* ('watch' or 'problem') or worse, for --fail-on."""
    floor = _RANK[level]
    return [f for f in report.get("findings") or () if _RANK.get(f["status"], 0) >= floor]


KIND_LABELS = {"galera": "MariaDB Galera Cluster", "pxc": "Percona XtraDB Cluster",
               "innodb_cluster": "MySQL InnoDB Cluster", "group_replication": "MySQL Group Replication",
               "none": "Not part of a cluster"}


def kind_label(report):
    """The product name: Galera on MySQL that is not Percona XtraDB Cluster is
    Codership's Galera Cluster for MySQL."""
    if report.get("kind") == "galera" and report.get("engine") == "mysql":
        return "Galera Cluster for MySQL"
    return KIND_LABELS[report.get("kind") or "none"]


def address_text(member):
    """A member's address as every surface shows it: host:port, or the host
    alone when no port is reported (Galera port 0, explained in a note)."""
    if member.get("host"):
        return join_address(member["host"], member.get("port"))
    return member.get("address")


def _size_fact(report):
    cluster = report["cluster"]
    if cluster.get("size") is not None:
        return "size %s%s" % (cluster["size"], " of %s" % cluster["expected_size"]
                              if cluster.get("expected_size") and cluster["expected_size"] != cluster["size"] else "")
    me = next((m for m in report["members"] if m["local"]), None)
    if me is not None and me.get("instance_type") == "read-replica":
        return "this server is a read replica (%s in the metadata)" % (cluster.get("expected_size") or "?")
    if me is not None and me.get("state") in ("OFFLINE", "ERROR"):
        return "group not visible from this member (%s in the metadata)" % (cluster.get("expected_size") or "?")
    return None


def format_text(report):
    """Human text for a cluster-1.0 report (also the MCP digest)."""
    out = []
    engine = product_name(report)
    if not report.get("available"):
        return terminal_text(report.get("message") or "Cluster status is not available.") + "\n"
    if report.get("kind") == "none":
        return terminal_text("%s %s: %s" % (engine, report.get("server_version") or "?",
                                            report.get("message") or "Not part of a cluster.")) + "\n"
    cluster = report["cluster"]
    head = "%s %s  [%s]" % (kind_label(report), cluster.get("name") or "(unnamed)", report["status"])
    out.append(head)
    facts = []
    if cluster.get("component"):
        facts.append("component %s" % cluster["component"])
    if cluster.get("quorum") is not None:
        facts.append("quorum %s" % ("yes" if cluster["quorum"] else "NO"))
    elif _read_replica_here(report):
        facts.append("quorum and failures tolerated unknown (not a group member)")
    size = _size_fact(report)
    if size:
        facts.append(size)
    if cluster.get("topology"):
        facts.append(cluster["topology"].replace("_", "-"))
    if cluster.get("failures_tolerated") is not None:
        facts.append("tolerates %d failure%s" % (cluster["failures_tolerated"],
                                                 "" if cluster["failures_tolerated"] == 1 else "s"))
    out.append("  %s %s; %s" % (engine, report.get("server_version") or "?", ", ".join(facts) or "no facts"))
    if cluster.get("uuid"):
        out.append("  uuid %s" % cluster["uuid"])
    galera = report.get("galera")
    if galera and galera.get("cluster_address"):
        out.append("  wsrep_cluster_address %s" % galera["cluster_address"])
    out.append("")
    out.append("Members:")
    for m in report["members"]:
        address = address_text(m)
        line = "  %s%s%s  %s" % (m["name"], "  (this connection)" if m["local"] else "",
                                 "  " + address if address and address != m["name"] else ("" if address else "  ?"),
                                 state_text(report, m))
        if m.get("role"):
            line += "  " + m["role"]
        if m.get("version"):
            line += "  " + m["version"]
        line += "  [%s]" % m["status"]
        out.append(line)
    if galera and galera.get("missing"):
        out.append("  %s  (in wsrep_cluster_address, not in the component)" % ", ".join(galera["missing"]))
    if galera:
        mt = galera["metrics"]
        out.append("")
        out.append("This node: recv queue %s (avg %s), send queue %s (avg %s), flow control paused %s, "
                   "cert deps distance %s, cert failures %s, BF aborts %s" % (
                       tuple("?" if mt.get(k) is None else mt[k] for k in (
                           "local_recv_queue", "local_recv_queue_avg", "local_send_queue", "local_send_queue_avg"))
                       + (flow_percent(mt.get("flow_control_paused")),)
                       + tuple("?" if mt.get(k) is None else mt[k] for k in (
                           "cert_deps_distance", "local_cert_failures", "local_bf_aborts"))))
    if report.get("routers"):
        out.append("")
        out.append("Routers:")
        for r in report["routers"]:
            ports = ", ".join("%s %s" % (k, r["ports"][k]) for k in ROUTER_PORTS if k in r["ports"])
            out.append("  %s  %s  %s  %s" % (r.get("name") or r.get("address") or "?", r.get("version") or "?",
                                             ports or "ports not recorded", check_in_text(r)))
    if report["findings"]:
        out.append("")
        out.append("Findings:")
        for f in report["findings"]:
            out.append("  [%s] %s: %s" % (f["status"], f["title"], f["text"]))
    return terminal_text("\n".join(out)) + "\n"


def state_text(report, member):
    """A member's state as the text output names it; a state this server
    cannot read says why instead of a guess."""
    if member.get("state"):
        return member["state"]
    if member.get("instance_type") == "read-replica":
        return "read replica (its state is read on it)"
    if report.get("kind") in ("galera", "pxc"):
        return "in the component (state read on that node)"
    if member.get("state_source") == "metadata":
        return "not visible from here"
    return "(state not readable here)"


def check_in_text(router):
    """'last check-in 4 s ago (2026-10-20 15:47:23 UTC)', the one format for
    the CLI and MCP text (the workspace shows the same parts)."""
    if not router.get("last_check_in"):
        return "last check-in not recorded"
    age = age_text(router.get("last_check_in_age_s"))
    utc = router.get("last_check_in_utc")
    when = "%s UTC" % utc.replace("T", " ").rstrip("Z") if utc else "%s server time" % router["last_check_in"]
    text = "last check-in %s (%s)" % (age, when) if age else "last check-in %s" % when
    if router.get("from_local_copy"):
        text += ", from this member's metadata copy, possibly out of date"
    return text


# ---- reviewed actions ------------------------------------------------------------------
#
# Changes the Cluster place offers (db-operations contract; the send half is
# myflames.reviewed_writes). Each one is offered only where the statement does
# what its label says, verified against the server source (mysql-server 9.7
# "M/", mariadb-server 13.0 trunk and the 10.11.16, 11.4.9 and 12.3.1 tags
# "Ma/", mysql-shell "Sh/"):
#
# * Group Replication (MySQL 8.0.13+, the UDFs' first release):
#   - switchover: SELECT group_replication_set_as_primary('<uuid>') takes the
#     member's MEMBER_ID (a UUID: the UDF validates it, M/plugin/
#     group_replication/src/udf/udf_single_primary.cc:171-181, udf_utils.cc:
#     169-171). It runs on any ONLINE member of the majority, refuses while a
#     member is RECOVERING or UNREACHABLE (:153-169, 209-215) and while the
#     current primary runs a replica channel (primary_election_validation_
#     handler.cc:184-193: an InnoDB ClusterSet replica cluster, where MySQL
#     Shell's setPrimaryInstance stops that channel first).
#   - mode switches, plain Group Replication only: group_replication_switch_
#     to_multi_primary_mode() and group_replication_switch_to_single_primary_
#     mode() (udf_multi_primary.cc:64-123, udf_single_primary.cc:237-293),
#     persisted by the plugin (multi_primary_migration_action.cc:340-357,
#     primary_election_action.cc:739-752). Not offered for InnoDB Cluster:
#     MySQL Shell's switchToMultiPrimaryMode also sets auto_increment on
#     every member and the metadata's primary_mode (Sh/modules/adminapi/
#     cluster/switch_to_multi_primary_mode.cc:49-80), which the UDF does not.
#   - STOP GROUP_REPLICATION on the connected member: it leaves the group and
#     turns super_read_only ON (M/plugin/group_replication/src/plugin.cc:
#     1371-1379); on the primary the group elects another; on the last
#     member the group ends.
#   - START GROUP_REPLICATION on the connected member while OFFLINE only: a
#     member in ERROR still has the plugin running, so START fails with
#     ER_GROUP_REPLICATION_RUNNING (plugin.cc:574-576, 891, 1363). Refused
#     while group_replication_bootstrap_group is ON (START would create a
#     second group, plugin.cc:2628-2631). Recovery may clone the server
#     (remote_clone_handler.cc:285-296).
#   - START, STOP and the UDFs need SUPER or GROUP_REPLICATION_ADMIN
#     (M/sql/sql_parse.cc:3560-3564, 3653-3657; udf_utils.cc:66-102).
# * MariaDB Galera (Ma/; not Percona XtraDB Cluster or Galera for MySQL,
#   whose sources were not checked):
#   - desync and resync: SET GLOBAL wsrep_desync = ON takes the node out of
#     flow control and OFF puts it back, waiting until it caught up
#     (Ma/sql/wsrep_var.cc:846-863; sys_vars.cc:6659-6665;
#     mysql-test/suite/galera/t/galera_var_desync_on.test). A node that is
#     Donor/Desynced with wsrep_desync OFF is serving a state transfer or
#     holds a backup lock or an RSU change; neither action applies then
#     (SET to the current value only warns, wsrep_var.cc:832-844).
#   - pc.bootstrap: SET GLOBAL wsrep_provider_options = 'pc.bootstrap=YES'
#     makes this node and the nodes it reaches a Primary component
#     (mysql-test/suite/galera_3nodes/t/galera_pc_bootstrap.test:30-66).
#     Offered only on a connected, non-joining node whose component is
#     non-Primary, with a typed name: if another component is Primary, or a
#     second node is bootstrapped, two components take writes.
#   - SET GLOBAL wsrep_on is not offered: it stops replicating writes from
#     sessions opened later only (Ma/sql/wsrep_var.cc:112-215), which
#     silently diverges the data.

ACTION_SCHEMA_VERSION = "cluster-action-1.0"
CLUSTER_ACTION_TAG = "/*myflames:cluster_op*/ "
GR_UDF_MIN = (8, 0, 13)
#: START GROUP_REPLICATION and the UDFs wait for the group (a join, the old
#: primary's transactions), so a send waits longer than a read.
ACTION_TIMEOUT = 120
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_GR_PRIVILEGE = "SUPER or GROUP_REPLICATION_ADMIN"


def _version_of(report):
    return tuple(int(p) for p in re.findall(r"\d+", report.get("server_version") or "")[:3])


def _members(report):
    return report.get("members") or []


def _local_member(report):
    return next((m for m in _members(report) if m.get("local")), None)


def _group_members(report):
    return [m for m in _members(report) if m.get("state_source") == "replication_group_members"]


def _identity(member):
    if not member:
        return None
    return {"key": member.get("key"), "uuid": member.get("uuid"), "name": member.get("name"),
            "role": member.get("role"), "state": member.get("state")}


def _gr_usable(report):
    """(True, None) when the Group Replication actions can run here."""
    if report.get("kind") not in ("innodb_cluster", "group_replication") or report.get("engine") != "mysql":
        return False, "Not a Group Replication member."
    if _version_of(report) < GR_UDF_MIN:
        return False, "Needs MySQL 8.0.13 or later."
    return True, None


def _group_ready(report):
    """Why a group-wide change cannot run now, or None: the connected member
    ONLINE with quorum, and no member joining or unreachable."""
    local = _local_member(report)
    if not local or local.get("state") != "ONLINE":
        return "The connected member is not ONLINE."
    if report.get("cluster", {}).get("quorum") is not True:
        return "The group has no quorum here."
    busy = [m["name"] for m in _group_members(report) if m.get("state") in ("RECOVERING", "UNREACHABLE")]
    if busy:
        return "%s %s recovering or unreachable." % (", ".join(busy), "is" if len(busy) == 1 else "are")
    return None


def _action(aid, label, danger, description):
    return {"id": aid, "label": label, "danger": danger, "description": description}


def available_actions(report, member_key=None, checks=None):
    """The reviewed changes offered on the member *member_key* (group-wide
    changes are offered on the connected member). Empty for a report that
    is not available, not a cluster, or a member this server cannot act on."""
    if not report or not report.get("available") or report.get("kind") in (None, "none"):
        return []
    member = next((m for m in _members(report) if m.get("key") == member_key), None)
    if member is None:
        return []
    kind = report.get("kind")
    out = []
    if kind in ("innodb_cluster", "group_replication"):
        usable, _ = _gr_usable(report)
        if not usable or member.get("state_source") != "replication_group_members":
            return []
        topology = report.get("cluster", {}).get("topology")
        if (topology == "single_primary" and member.get("role") == "SECONDARY" and member.get("state") == "ONLINE"
                and _UUID.match(member.get("uuid") or "") and _group_ready(report) is None):
            out.append(_action("switchover", "Make primary…", True,
                               "Makes %s the primary; the current primary becomes a read-only secondary once its "
                               "running transactions finish." % member.get("name")))
        if member.get("local"):
            if member.get("state") in ("ONLINE", "RECOVERING", "ERROR"):
                out.append(_action("stop_member", "Stop Group Replication…", True,
                                   "This member leaves the group and becomes read-only (super_read_only ON)."))
            if member.get("state") == "OFFLINE":
                out.append(_action("start_member", "Start Group Replication…", False,
                                   "Joins this member to the group. Recovery may replace its data with a clone."))
            if kind == "group_replication" and _group_ready(report) is None:
                if topology == "single_primary":
                    out.append(_action("switch_to_multi_primary", "Switch the group to multi-primary…", True,
                                       "Every member accepts writes, and the mode is persisted."))
                elif topology == "multi_primary":
                    out.append(_action("switch_to_single_primary", "Switch the group to single-primary…", True,
                                       "The group elects one primary; the others become read-only."))
    elif kind == "galera" and report.get("engine") == "mariadb" and member.get("local"):
        galera = report.get("galera") or {}
        metrics = galera.get("metrics") or {}
        comment = (metrics.get("local_state_comment") or "").strip()
        wsrep_on = (galera.get("wsrep_on") or "").upper()
        desync = (galera.get("desync") or "").upper()
        if wsrep_on != "ON":
            return []
        if desync == "OFF" and comment == "Synced":
            out.append(_action("desync_node", "Desync this node…", False,
                               "Takes this node out of flow control (wsrep_desync = ON), for example for a backup."))
        elif desync == "ON":
            out.append(_action("resync_node", "Resync this node…", False,
                               "Puts this node back in flow control (wsrep_desync = OFF)."))
        component = report.get("cluster", {}).get("component")
        if component == "non-Primary" and metrics.get("connected") is True and not _joining(metrics):
            out.append(_action("bootstrap_pc", "Bootstrap a Primary component…", True,
                               "Makes this node, and the nodes it reaches, a Primary component that accepts writes."))
    return out


def with_actions(report):
    """*report* with each member's ``actions`` (the workspace's Cluster place
    reads it this way; the CLI and MCP report has no actions)."""
    for member in report.get("members") or ():
        member["actions"] = available_actions(report, member.get("key"))
    return report


def action_checks(conn, report, action):
    """Extra reads one action needs at plan and send time (a dict)."""
    out = {}
    if action == "start_member":
        reader = Reader(conn, CLUSTER_TAG)
        try:
            rows = reader.rows("SELECT @@global.group_replication_bootstrap_group")
            out["bootstrap_group"] = _text(rows[0][0]) if rows and rows[0] else None
        except ConnectorError:
            out["bootstrap_group"] = None
    return out


def _server_identity(report):
    return {"engine": report.get("engine"), "server_version": report.get("server_version"),
            "server": dict(report.get("server") or {}), "kind": report.get("kind")}


def _connected_text(report, local):
    server = report.get("server") or {}
    where = join_address(server.get("hostname"), server.get("port")) if server.get("hostname") else None
    return "%s%s" % (local.get("name") if local else "This server", " (%s)" % where if where and local and
                     where != local.get("name") else "")


def plan_action(report, action, member_key=None, checks=None):
    """The reviewed plan for *action* on *member_key* from *report* (a fresh
    read) and *checks* (:func:`action_checks`). Raises ValueError when the action is
    not offered for that member now."""
    if not report or not report.get("available") or report.get("kind") in (None, "none"):
        raise ValueError("No cluster was read on this connection.")
    if not isinstance(action, str) or not isinstance(member_key, str):
        raise ValueError("Choose a member and an action.")
    if action not in [a["id"] for a in available_actions(report, member_key, checks)]:
        raise ValueError("That change is not offered for this member now. Refresh and try again.")
    checks = checks or {}
    member = next(m for m in _members(report) if m.get("key") == member_key)
    local = _local_member(report)
    cluster = report.get("cluster") or {}
    expect = dict(_server_identity(report), member=_identity(member), local=_identity(local),
                  component=cluster.get("component"), quorum=cluster.get("quorum"), topology=cluster.get("topology"))
    plan = {"schema_version": ACTION_SCHEMA_VERSION, "action": action, "member_key": member_key, "typed_guard": None,
            "warnings": [], "next": None, "verified": ["server identity", "member state and role"],
            "facts": [["Runs on", _connected_text(report, local)],
                      ["Server", "%s %s" % (product_name(report), report.get("server_version") or "")],
                      ["Cluster", "%s%s" % (kind_label(report), " %s" % cluster["name"] if cluster.get("name") else "")]]}
    kind = report.get("kind")
    if kind in ("innodb_cluster", "group_replication"):
        expect["members"] = [[m.get("key"), m.get("state"), m.get("role")] for m in _group_members(report)]
        plan["facts"].append(["Privilege", "Needs %s" % _GR_PRIVILEGE])
        _plan_group(report, plan, action, member, local, checks, expect)
    else:
        galera = report.get("galera") or {}
        expect.update(desync=galera.get("desync"), wsrep_on=galera.get("wsrep_on"),
                      local_state=(galera.get("metrics") or {}).get("local_state_comment"))
        plan["facts"].append(["Privilege", "Needs SUPER"])
        _plan_galera(report, plan, action, member, expect)
    plan["expect"] = expect
    plan["statements"] = [CLUSTER_ACTION_TAG + s for s in plan["statements"]]
    plan["timeout_s"] = ACTION_TIMEOUT
    plan["checks"] = _checks(action, member, local)
    return plan


def _checks(action, member, local):
    """What to look at when a send got no answer (shown with Outcome unknown)."""
    name = (member or {}).get("name") or "the member"
    here = (local or {}).get("name") or "this node"
    first = {
        "switchover": "Refresh the Cluster place and see which member is PRIMARY now.",
        "stop_member": "Refresh the Cluster place and look at %s's member state." % here,
        "start_member": "Refresh the Cluster place and look at %s's member state." % here,
        "switch_to_single_primary": "Refresh the Cluster place: the group mode says single-primary or "
                                    "multi-primary.",
        "switch_to_multi_primary": "Refresh the Cluster place: the group mode says single-primary or "
                                   "multi-primary.",
        "desync_node": "Refresh the Cluster place: %s's wsrep_desync and state." % here,
        "resync_node": "Refresh the Cluster place: %s's wsrep_desync and state." % here,
        "bootstrap_pc": "Refresh the Cluster place: whether %s's component is Primary." % here,
    }.get(action, "Refresh the Cluster place and look at %s." % name)
    return [first, "The Audit log keeps this attempt as Outcome unknown; nothing is sent again."]


def _effect(plan, before, after):
    """What changed: (what, before, now) rows from the reads before and after
    the send."""
    def word(value):
        return "Not readable" if value in (None, "") else str(value).replace("_", "-")

    def name(side, key):
        return ((side or {}).get(key) or {}).get("name")

    def state(side, key):
        item = (side or {}).get(key) or {}
        return " ".join(x for x in (item.get("state"), item.get("role")) if x) or None

    action = plan["action"]
    if action == "switchover":
        return [["Primary", word(name(before, "primary")), word(name(after, "primary"))]]
    if action in ("stop_member", "start_member"):
        return [[name(after, "local") or name(before, "local") or "This member", word(state(before, "local")),
                 word(state(after, "local"))]]
    if action in ("desync_node", "resync_node"):
        return [["wsrep_desync", word(before.get("desync")), word(after.get("desync"))],
                ["State", word(before.get("local_state")), word(after.get("local_state"))]]
    if action == "bootstrap_pc":
        return [["Component", word(before.get("component")), word(after.get("component"))]]
    return [["Group mode", word(before.get("topology")), word(after.get("topology"))]]


def _plan_group(report, plan, action, member, local, checks, expect):
    name = member.get("name")
    primary = next((m for m in _group_members(report) if m.get("role") == "PRIMARY"), None)
    online = [m for m in _group_members(report) if m.get("state") == "ONLINE"]
    if action == "switchover":
        uuid = member.get("uuid") or ""
        plan.update(eyebrow="SELECT group_replication_set_as_primary", title="Make %s the primary?" % name,
                    confirm_label="Make %s primary" % name, target_text=name,
                    description="The current primary becomes a read-only secondary once its running transactions "
                                "finish. Clients that write must reach the new primary (MySQL Router follows it).")
        plan["statements"] = ["SELECT group_replication_set_as_primary('%s');" % uuid]
        plan["facts"] += [["New primary", "%s (%s)" % (name, uuid)],
                          ["Current primary", primary.get("name") if primary else "None"]]
        if report.get("kind") == "innodb_cluster":
            plan["warnings"].append("In an InnoDB ClusterSet replica cluster the server refuses while the primary "
                                    "replicates from the primary cluster; use MySQL Shell's setPrimaryInstance there.")
        undo = primary.get("uuid") if primary and _UUID.match(primary.get("uuid") or "") else None
        plan["undo"] = ("SELECT group_replication_set_as_primary('%s'); makes %s the primary again." %
                        (undo, primary.get("name"))) if undo else "Make the previous primary the primary again."
    elif action == "stop_member":
        plan.update(eyebrow="STOP GROUP_REPLICATION", title="Stop Group Replication on %s?" % name,
                    confirm_label="Stop Group Replication", target_text=name,
                    description="This member leaves the group and becomes read-only (super_read_only ON).")
        plan["statements"] = ["STOP GROUP_REPLICATION;"]
        last = member.get("state") == "ONLINE" and len(online) == 1
        if member.get("role") == "PRIMARY" and not last:
            plan["warnings"].append("%s is the primary: the group elects another primary, and writes sent here "
                                    "fail." % name)
        if last:
            plan["warnings"].append("%s is the last ONLINE member: the group stops, and starting it again needs a "
                                    "bootstrap." % name)
        if member.get("state") == "ERROR":
            plan["warnings"].append("In ERROR, the plugin is still running: stop it, fix the cause in the error log, "
                                    "then start it.")
        if member.get("role") == "PRIMARY" or last:
            plan["typed_guard"] = name
        plan["undo"] = "START GROUP_REPLICATION;" + (" Not enough after the last member: the group must be "
                                                     "bootstrapped again." if last else "")
    elif action == "start_member":
        bootstrap = (checks.get("bootstrap_group") or "").upper()
        if bootstrap not in ("0", "OFF"):
            raise ValueError("group_replication_bootstrap_group is %s, so START GROUP_REPLICATION would %s. Nothing "
                             "is offered." % (bootstrap or "not readable",
                                              "create a new group instead of joining" if bootstrap in ("1", "ON")
                                              else "do something myflames cannot verify"))
        expect["bootstrap_group"] = bootstrap
        plan["verified"].append("group_replication_bootstrap_group OFF")
        plan.update(eyebrow="START GROUP_REPLICATION", title="Start Group Replication on %s?" % name,
                    confirm_label="Start Group Replication", target_text=name,
                    description="This member joins the group and catches up from a donor. When too many "
                                "transactions are missing, or the donors purged them, recovery replaces this "
                                "server's data with a clone and restarts it.")
        plan["statements"] = ["START GROUP_REPLICATION;"]
        plan["facts"].append(["group_replication_bootstrap_group", "OFF"])
        plan["undo"] = "STOP GROUP_REPLICATION;"
    else:
        multi = action == "switch_to_multi_primary"
        plan.update(eyebrow="SELECT group_replication_switch_to_%s_mode" % ("multi_primary" if multi else
                                                                             "single_primary"),
                    title="Switch the group to %s mode?" % ("multi-primary" if multi else "single-primary"),
                    confirm_label="Switch to %s" % ("multi-primary" if multi else "single-primary"),
                    target_text="the group (%d members)" % len(_group_members(report)),
                    description=("Every member accepts writes. From now on SERIALIZABLE transactions and writes to "
                                 "tables with cascading foreign keys are refused, and conflicting writes on "
                                 "different members roll back. The mode is persisted." if multi else
                                 "The group elects one primary; the other members become read-only. The mode is "
                                 "persisted."))
        plan["statements"] = ["SELECT group_replication_switch_to_multi_primary_mode();" if multi else
                              "SELECT group_replication_switch_to_single_primary_mode();"]
        if multi:
            plan["warnings"].append("Set auto_increment_increment and auto_increment_offset per member first: the "
                                    "switch does not change them, so inserts on different members can collide.")
        plan["typed_guard"] = local.get("name") if local else None
        plan["undo"] = ("SELECT group_replication_switch_to_single_primary_mode();" if multi else
                        "SELECT group_replication_switch_to_multi_primary_mode();")


def _plan_galera(report, plan, action, member, expect):
    name = member.get("name")
    metrics = (report.get("galera") or {}).get("metrics") or {}
    if action == "desync_node":
        plan.update(eyebrow="SET GLOBAL", title="Desync %s?" % name, confirm_label="Desync this node",
                    target_text=name,
                    description="Takes this node out of flow control, so the cluster keeps committing when it falls "
                                "behind (for example during a backup). It keeps receiving and applying writes, "
                                "reports Donor/Desynced, and its reads can be stale.")
        plan["statements"] = ["SET GLOBAL wsrep_desync = ON;"]
        plan["warnings"].append("A proxy that routes by node state takes a Donor/Desynced node out of traffic until "
                                "it is Synced again (ProxySQL's Galera monitor moved it to its offline hostgroup, "
                                "seen live on 3.0.11).")
        plan["undo"] = "SET GLOBAL wsrep_desync = OFF; (Resync this node)"
        plan["next"] = None
    elif action == "resync_node":
        plan.update(eyebrow="SET GLOBAL", title="Resync %s?" % name, confirm_label="Resync this node",
                    target_text=name,
                    description="Puts this node back in flow control. The statement waits until the node has "
                                "caught up.")
        plan["statements"] = ["SET GLOBAL wsrep_desync = OFF;"]
        plan["undo"] = "SET GLOBAL wsrep_desync = ON; (Desync this node)"
    else:
        cluster = report.get("cluster") or {}
        expect["last_committed"] = metrics.get("last_committed")
        # The component as this node sees it: another node joining or leaving
        # (even while it stays non-Primary) needs another review.
        expect["component_view"] = {
            "cluster_uuid": cluster.get("uuid"), "conf_id": metrics.get("cluster_conf_id"), "size": cluster.get("size"),
            "members": sorted([m.get("key"), m.get("address")] for m in _members(report)),
        }
        plan.update(eyebrow="SET GLOBAL", title="Bootstrap a Primary component on %s?" % name,
                    confirm_label="Bootstrap the Primary component", target_text=name,
                    description="Makes this node, and only the nodes it can reach, a new Primary component that "
                                "accepts writes. Run it on one node only, after making sure no other node is in a "
                                "Primary component and that this node has the highest wsrep_last_committed of all "
                                "nodes. Otherwise two components take writes (split brain) and one side's writes "
                                "are lost.")
        plan["statements"] = ["SET GLOBAL wsrep_provider_options = 'pc.bootstrap=YES';"]
        plan["facts"] += [["wsrep_cluster_status", cluster.get("component") or "Not reported"],
                          ["Nodes this node sees", str(cluster.get("size")) if cluster.get("size") is not None
                           else "Not reported"],
                          ["wsrep_last_committed here", str(metrics.get("last_committed"))
                           if metrics.get("last_committed") is not None else "Not reported"]]
        plan["warnings"].append("Compare wsrep_last_committed on every node first: bootstrap the node with the "
                                "highest value.")
        plan["typed_guard"] = name
        plan["undo"] = "Cannot be undone."


def _after(report, plan):
    member = next((m for m in _members(report) if m.get("key") == plan.get("member_key")), None)
    local = _local_member(report)
    primary = next((m for m in _group_members(report) if m.get("role") == "PRIMARY"), None)
    galera = report.get("galera") or {}
    return {"member": _identity(member), "local": _identity(local), "primary": _identity(primary),
            "component": (report.get("cluster") or {}).get("component"),
            "topology": (report.get("cluster") or {}).get("topology"),
            "desync": galera.get("desync"), "local_state": (galera.get("metrics") or {}).get("local_state_comment")}


def _done_text(plan, after):
    member = (after or {}).get("member") or {}
    local = (after or {}).get("local") or {}
    action = plan["action"]
    if action == "switchover":
        primary = ((after or {}).get("primary") or {}).get("name")
        return "Sent. The primary is now %s." % primary if primary else "Sent."
    if action in ("stop_member", "start_member"):
        return "Sent. %s is %s now." % (local.get("name") or "This member", local.get("state") or "not readable")
    if action in ("desync_node", "resync_node"):
        return "Sent. wsrep_desync is %s; %s is %s." % ((after or {}).get("desync") or "not readable",
                                                       local.get("name") or "this node",
                                                       (after or {}).get("local_state") or "not readable")
    if action == "bootstrap_pc":
        return "Sent. The component is %s." % ((after or {}).get("component") or "not readable")
    return "Sent. The group is %s." % ((after or {}).get("topology") or "not readable").replace("_", "-")


def execute_action(conn, reviewed, audit=None, typed=None, before_send=None, progress=None):
    """Send a reviewed plan: read the cluster again, refuse with nothing sent
    when the member, the server or the group changed, then send it.
    *progress* follows the steps for the sheet (reviewed_writes.Progress)."""
    from . import reviewed_writes
    reviewed = reviewed_writes.check_reviewed(reviewed)
    expect = reviewed.get("expect") or {}
    if audit is not None:
        audit.server(expect.get("engine"), expect.get("server_version"))
    state = {}

    def fresh():
        report = read_status(conn)
        if _server_identity(report) != {k: expect.get(k) for k in ("engine", "server_version", "server", "kind")}:
            raise ValueError("This connection now reaches another server or cluster.")
        plan = plan_action(report, reviewed["action"], reviewed.get("member_key"),
                           action_checks(conn, report, reviewed["action"]))
        state["before"] = _after(report, reviewed)
        return plan

    return reviewed_writes.execute(conn, reviewed, fresh, ACTION_SCHEMA_VERSION, audit=audit, typed=typed,
                                   before_send=before_send, read_after=lambda: _after(read_status(conn), reviewed),
                                   timeout=ACTION_TIMEOUT, done_text=_done_text, progress=progress,
                                   effect=lambda plan, after: _effect(plan, state.get("before") or {}, after))
