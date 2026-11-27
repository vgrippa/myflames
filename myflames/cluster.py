"""Cluster status for the connection in use: MariaDB Galera, Galera Cluster for
MySQL, Percona XtraDB Cluster, MySQL InnoDB Cluster and Group Replication.
:func:`read_status` only reads; the reviewed changes the workspace's Cluster
place offers (switchover, Group Replication stop and start, MariaDB Galera
desync, resync and pc.bootstrap) are at the end of this module.

:func:`read_status` returns the ``cluster-1.1`` report (schema in
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
* MySQL 5.7 (Group Replication from 5.7.17, tags mysql-5.7.16 and 5.7.17):
  replication_group_members has no MEMBER_ROLE or MEMBER_VERSION (5.7.44
  storage/perfschema/table_replication_group_members.cc), and
  replication_group_member_stats has this member's row only, without the
  applier, remote-applied, local-proposed and local-rollback counts
  (table_replication_group_member_stats.cc:240-246). The single primary is
  the status variable ``group_replication_primary_member``, its UUID while
  Group Replication runs in single-primary mode, else empty (5.7.44
  rapid/plugin/group_replication/src/plugin.cc:2930-2955), or "UNDEFINED"
  when no member is primary or this member is in ERROR (member_info.cc:
  887-890), which names no member. Roles follow
  8.0's rule from it: PRIMARY for that member, SECONDARY for the other
  ONLINE, RECOVERING or UNREACHABLE members, PRIMARY for each of them in
  multi-primary mode, none for OFFLINE and ERROR. Other members' versions
  are unknown there.
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
import json
import re
import time

from .connection_groups import grouped_read
from .connector import ConnectorError, terminal_text
from .perf_common import PREFLIGHT_VARIABLES, Reader, Server, _LOST_SQL, _number, denied, now, preflight, reason, show_variables
from .server_info import galera_enabled, galera_provider_loaded, product_name

SCHEMA_VERSION = "cluster-1.1"
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
#: Variables read with the initial preflight. A name the server does not
#: have is simply absent from the answer (SHOW GLOBAL VARIABLES WHERE ... IN).
EXTRA_VARIABLES = GALERA_VARIABLES + (
    "report_port", "server_uuid", "server_id", "partial_revokes",
    "group_replication_group_name", "group_replication_single_primary_mode", "group_replication_local_address",
    "group_replication_group_seeds", "group_replication_bootstrap_group",
    "group_replication_preemptive_garbage_collection",
)
_CURRENT_USER_MARKER = "myflames_current_user"


def _preflight_variables_sql():
    names = sorted(set(PREFLIGHT_VARIABLES + EXTRA_VARIABLES))
    return ("SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s); %sSELECT '%s', CURRENT_USER()"
            % (", ".join("'%s'" % n for n in names), CLUSTER_TAG, _CURRENT_USER_MARKER))


def _preflight(reader):
    """Read preflight variables and CURRENT_USER() in one client invocation,
    then Uptime and Performance Schema lost counters.
    """
    variables = {}
    current_user = None
    for row in reader.rows(_preflight_variables_sql()):
        if len(row) == 2 and row[0] == _CURRENT_USER_MARKER:
            current_user = row[1]
        elif row:
            variables[row[0]] = row[1] if len(row) > 1 else ""
    status = reader.pairs(_LOST_SQL)
    return Server(variables, status), current_user

WSREP_STATUS_SQL = "SHOW GLOBAL STATUS WHERE Variable_name LIKE 'wsrep\\_%'"
WSREP_MEMBERS_SQL = "SELECT node_uuid, node_name, node_incoming_address FROM mysql.wsrep_cluster_members"
PXC_VIEW_SQL = "SELECT HOST_NAME, UUID, STATUS, LOCAL_INDEX, SEGMENT FROM performance_schema.pxc_cluster_view"
_GR_MEMBERS_COLUMNS = ("CHANNEL_NAME", "MEMBER_ID", "MEMBER_HOST", "MEMBER_PORT", "MEMBER_STATE", "MEMBER_ROLE",
                       "MEMBER_VERSION")
#: MySQL 5.7: the same positions, with NULL where 5.7 has no column.
_GR_STATS_SQL_57 = (
    "SELECT MEMBER_ID, VIEW_ID, COUNT_TRANSACTIONS_IN_QUEUE, COUNT_TRANSACTIONS_CHECKED, "
    "COUNT_CONFLICTS_DETECTED, COUNT_TRANSACTIONS_ROWS_VALIDATING, NULL, NULL, NULL, NULL "
    "FROM performance_schema.replication_group_member_stats")
_GR_PRIMARY_SQL = "SHOW GLOBAL STATUS WHERE Variable_name = 'group_replication_primary_member'"
#: Group Replication shipped with MySQL 5.7.17.
GR_MIN_VERSION = (5, 7, 17)
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
#: InnoDB ClusterSet (metadata 2.1.0 and later, MySQL Shell 8.0.27): the
#: clusters of the set from the latest view (v2_cs_members: member_role
#: PRIMARY or REPLICA, invalidated when the cluster is not in the latest
#: view) and the set's name (v2_cs_clustersets.domain_name),
#: Sh/modules/adminapi/common/metadata/metadata-model-2.1.0.sql:589-631.
CLUSTERSET_MIN_METADATA = (2, 1, 0)
_MD_CLUSTERSET_SQL = ("SELECT m.clusterset_id, m.cluster_id, m.cluster_name, m.member_role, m.invalidated, "
                      "s.domain_name, s.view_id, s.router_options FROM %s.v2_cs_members m JOIN %s.v2_cs_clustersets s "
                      "ON s.clusterset_id = m.clusterset_id" % (METADATA_SCHEMA, METADATA_SCHEMA))
#: The channel a replica cluster's primary replicates from the primary
#: cluster over (Sh/modules/adminapi/common/common.h:172-173).
CLUSTERSET_CHANNEL = "clusterset_replication"
_CS_CONNECTION_SQL = ("SELECT SERVICE_STATE, LAST_ERROR_NUMBER, LAST_ERROR_MESSAGE, SOURCE_UUID FROM "
                      "performance_schema.replication_connection_status WHERE CHANNEL_NAME = '%s'"
                      % CLUSTERSET_CHANNEL)
_CS_APPLIER_SQL = _RR_APPLIER_SQL.replace(READ_REPLICA_CHANNEL, CLUSTERSET_CHANNEL)
#: Where the channel connects: the primary cluster's primary (MySQL Shell
#: points it there and moves it on a switchover).
_CS_SOURCE_SQL = ("SELECT HOST, PORT FROM performance_schema.replication_connection_configuration "
                  "WHERE CHANNEL_NAME = '%s'" % CLUSTERSET_CHANNEL)
# Column definitions: mysql-server/storage/perfschema/
# table_replication_applier_status_by_worker.cc:55 (factsheet, SQL verification).
_CS_WORKERS_SQL = ("SELECT WORKER_ID, SERVICE_STATE, LAST_ERROR_NUMBER, LAST_ERROR_MESSAGE "
                   "FROM performance_schema.replication_applier_status_by_worker "
                   "WHERE CHANNEL_NAME = '%s'" % CLUSTERSET_CHANNEL)
# Stored configuration, never Router refresh or traffic evidence. Both
# columns exist in mysql-shell/modules/adminapi/common/metadata/
# metadata-model-2.1.0.sql:589-600,648-660 (also 2.4.0.sql:658,717).
_CS_ROUTERS_SQL = ("SELECT r.router_id, r.clusterset_id, r.cluster_id, r.router_name, r.address, r.version, "
                   "r.options FROM %s.v2_routers AS r" % METADATA_SCHEMA)
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
    return galera_enabled(variables)


def provider_loaded(variables):
    """wsrep_provider names a provider library (not empty, not 'none')."""
    return galera_provider_loaded(variables)


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


@grouped_read
def read_status(conn, _identity=None, profiles=(), open_profile=None, reached_host=None, current_profile_id=None):
    """The ``cluster-1.1`` report for the server *conn* is connected to.

    Read-only. Connection failures raise ConnectorError; an answer myflames
    cannot read raises ValueError. A server that is not part of a cluster
    returns ``kind: "none"`` with the reason in ``message``; an account that
    cannot read the cluster tables returns ``available: false``.
    """
    reader = Reader(conn, CLUSTER_TAG)
    try:
        server, current_user = _preflight(reader)
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
            try:
                from . import readyset
                is_rs, rs_version = readyset.identify(conn)
            except Exception:
                is_rs, rs_version = False, None
            if is_rs:
                report = _base(Server({"version": rs_version or ""}, {}))
                report["server_product"] = "Readyset"
                report["server_version"] = rs_version
                report["readyset_hint"] = True
                report["message"] = (
                    "Not part of a cluster: this connection is a Readyset instance (open Readyset to inspect caches "
                    "and upstream topology)."
                )
                report["statements"] = ["SELECT @@version_comment", "SHOW READYSET VERSION"]
                _finish(report)
                return report
        raise
    report = _ActionReport(_base(server), conn, server.variables, current_user=current_user)
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
    # The first cluster/metadata reads use separate client logins. Probe the
    # UUID-guarded snapshot even when one of those preliminary reads missed
    # this server or its ClusterSet entirely.
    if (server.engine == "mysql" and tuple(server.version_tuple) >= (8, 0, 27) and
            report.get("available") and report.get("server", {}).get("server_uuid") and
            report.get("kind") not in ("galera", "pxc")):
        initial = _ClusterSetReader(conn, server_uuid=report["server"]["server_uuid"])
        try:
            if _clusterset_may_exist(initial, report):
                _clusterset_initial_metadata(initial, report, report["server"]["server_uuid"])
        except _ClusterSetIdentityError:
            raise ValueError("The ClusterSet metadata could not be verified on this server.") from None
        except (ConnectorError, ValueError, OSError):
            if report.get("clusterset"):
                raise ValueError("The ClusterSet metadata could not be verified on this server.") from None
            # Keep the diagnostic report, but never infer that a Group is
            # outside a ClusterSet when its membership could not be read.
            report._clusterset_discovery_error = True
            report["notes"].append("ClusterSet membership could not be verified on this server; cluster changes "
                                   "are disabled until its metadata can be read.")
    if report.get("clusterset"):
        _read_clusterset_routes(reader, report, profiles, open_profile, current_profile_id)
        _clusterset_findings(report)
        _judge_clusterset(report["clusterset"])
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


def _partial_revokes_for_server(server):
    """Database-pattern matching mode for advisory schema GRANTs.

    MariaDB and MySQL before 8.0.16 always use wildcard matching (False).
    MySQL 8.0.16+ uses the server's partial_revokes variable; absence or an
    incomplete/unknown version leaves it None so grant_sql refuses names with
    '_' or '%' rather than guessing.
    """
    if server.engine == "mariadb":
        return False
    version = tuple(server.version_tuple)
    if len(version) >= 3 and (5, 7, 0) <= version[:3] < (8, 0, 16):
        return False
    raw = server.variables.get("partial_revokes")
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text in ("ON", "1"):
        return True
    if text in ("OFF", "0"):
        return False
    return None


def _denied_read_grant_sql(server, schema=None, report=None):
    """Advisory SELECT GRANT for a denied table read, built from the
    CURRENT_USER() kept privately on *report* (captured in the initial
    preflight without an extra login) through privileges.grant_sql
    (printable-ASCII allowlist without backslash).

    Never opens another connection or login, and never derives anything from
    error text. Returns None when no table schema applies, CURRENT_USER() was
    not read or lacks '@', or grant_sql refuses the names.
    """
    if not schema:
        return None
    account = getattr(report, "_current_user", None)
    if not isinstance(account, str) or "@" not in account:
        return None
    from . import privileges
    partial = _partial_revokes_for_server(server)
    try:
        return privileges.grant_sql([(("SELECT",), schema, False)], account, partial, engine=server.engine)
    except ValueError:
        return None


def _read_galera(reader, server, report):
    report["kind"] = galera_kind(server.variables)
    try:
        status = reader.pairs(WSREP_STATUS_SQL)
    except ConnectorError as exc:
        if denied(exc):
            report["available"] = False
            report["message"] = "This account cannot read the wsrep status variables: %s" % reason(exc)
            report["grant_sql"] = _denied_read_grant_sql(server, report=report)
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
        report["grant_sql"] = _denied_read_grant_sql(server, report=report)
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
    """The finished cluster-1.1 report of a Galera or PXC node from rows
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
    if tuple(server.version_tuple) < GR_MIN_VERSION:
        report["message"] = ("Not part of a cluster: Group Replication needs MySQL 5.7.17 or later, and %s."
                             % no_galera)
        return
    # MySQL 5.7: no MEMBER_ROLE or MEMBER_VERSION (module docstring).
    mysql57 = tuple(server.version_tuple) < (8, 0, 0)
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
    selected = ["NULL" if mysql57 and c in ("MEMBER_ROLE", "MEMBER_VERSION") else c for c in columns]
    try:
        rows = reader.rows("SELECT %s FROM performance_schema.replication_group_members" % ", ".join(selected))
    except ConnectorError as exc:
        if denied(exc):
            report["available"] = False
            report["kind"] = "group_replication" if group_name else "none"
            report["cluster"]["uuid"] = group_name
            report["message"] = ("This account cannot read performance_schema.replication_group_members (it needs "
                                 "SELECT on performance_schema): %s" % reason(exc))
            report["grant_sql"] = _denied_read_grant_sql(server, "performance_schema", report=report)
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
    if mysql57 and members:
        _roles_57(reader, server, report, members)
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
    # SHOW omits variables this version/plugin does not have. Keep absence
    # distinct from OFF, especially for the START safety check.
    for variable, field in (("group_replication_bootstrap_group", "bootstrap_group"),
                            ("group_replication_preemptive_garbage_collection", "preemptive_garbage_collection")):
        if variable in variables:
            report["group_replication"][field] = _text(variables[variable])
    stats = reader.optional(_GR_STATS_SQL_57 if mysql57 else _GR_STATS_SQL,
                            "performance_schema.replication_group_member_stats", report["notes"])
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
        if stats and mysql57:
            report["notes"].append("Queue lengths and rows validating are current; the other counts are "
                                   "cumulative. MySQL 5.7 reports them for this member only.")
        elif stats:
            report["notes"].append("Queue lengths and rows validating are current; the other counts are "
                                   "cumulative. Another member's numbers are the ones it last sent to this member.")
    _read_metadata(reader, report, group_name, local)
    _judge_group(report, local)


def _roles_57(reader, server, report, members):
    """MySQL 5.7: each member's role from group_replication_primary_member
    (module docstring), and the local member's version from @@version."""
    single = _on(server.variables.get("group_replication_single_primary_mode"))
    primary = None
    try:
        rows = reader.rows(_GR_PRIMARY_SQL)
        report["statements"].append("SHOW GLOBAL STATUS group_replication_primary_member")
        primary = _text(rows[0][1]) if rows and len(rows[0]) > 1 else None
        if primary and not _UUID.match(primary):
            primary = None  # "UNDEFINED": no member is primary, or this member is in ERROR
    except ConnectorError as exc:
        if not denied(exc):
            raise
    for member in members:
        if member["local"]:
            member["version"] = server.version.split("-")[0] or None
        if member["state"] not in ("ONLINE", "RECOVERING", "UNREACHABLE"):
            continue
        if single is False:
            member["role"] = "PRIMARY"
        elif single and primary:
            member["role"] = "PRIMARY" if member["uuid"] == primary else "SECONDARY"
    report["notes"].append("MySQL 5.7 has no MEMBER_ROLE or MEMBER_VERSION: roles come from "
                           "group_replication_primary_member (the primary's UUID in single-primary mode), and only "
                           "this member's version is known.")


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
    rows = reader.optional(_CLUSTERSET_SNAPSHOT_QUERIES[3][1],
                           "the InnoDB Cluster metadata (v2_gr_clusters)", notes)
    return _decode_metadata_names(rows, (1,))


def _read_instances(reader, metadata, notes):
    instances = reader.optional(_MD_INSTANCES_SQL, "the InnoDB Cluster metadata (v2_instances)", notes) or []
    return _instances_from_rows(instances)


def _instances_from_rows(instances):
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
    every = _read_instances(reader, metadata, notes)
    # Every cluster of a ClusterSet shares one metadata (replicated from the
    # primary cluster), so the other clusters' instances are listed here too.
    others = {}
    for inst in every:
        cluster_id = inst.pop("cluster_id")
        if cluster_id == metadata["cluster_id"]:
            metadata["instances"].append(inst)
        else:
            others.setdefault(cluster_id, []).append(inst)
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
    _read_clusterset(reader, report, metadata, local, others, clusters)
    _read_routers(reader, report, metadata)


def _cs_instance(inst):
    return {"label": inst.get("label"), "address": inst.get("endpoint") or inst.get("address"),
            "server_uuid": inst.get("server_uuid"), "instance_type": inst.get("instance_type"),
            "source": False}


# A request opens each consented profile at most once, without background
# workers. The normal profile_opener also bounds connection setup (replication.py).
CLUSTERSET_ROUTE_TIMEOUT = 5.0
CLUSTERSET_ROUTE_MAX_OPENS = 3
# A failover check has its own budget. The saved group itself is capped at
# 16 members; it must not inherit the three-open display-search limit.
CLUSTERSET_FAILOVER_TIMEOUT = 50.0
CLUSTERSET_FAILOVER_MAX_OPENS = 16
CLUSTERSET_FAILOVER_PROBE_TIMEOUT = 3.0


class _ClusterSetIdentityError(ValueError):
    """A route changed servers, or did not return its identity with the read."""


class _ClusterSetClient(object):
    """Keep the UUID and subsequent SQL on one uninterrupted login."""
    def __init__(self, conn):
        self.conn = conn

    def run(self, sql, extra_flags=None, **options):
        flags = list(extra_flags or ()) + ["--skip-reconnect"]
        return self.conn.run(sql, extra_flags=flags, **options)


class _ClusterSetReader(Reader):
    """One route's shared SQL budget, including its identity query."""
    def __init__(self, conn, deadline=None, server_uuid=None):
        super(_ClusterSetReader, self).__init__(conn, CLUSTER_TAG)
        self._client_reader = Reader(_ClusterSetClient(conn), CLUSTER_TAG)
        self.deadline = deadline if deadline is not None else time.monotonic() + CLUSTERSET_ROUTE_TIMEOUT
        self.route = {}
        self.cluster_name = None
        self.server_uuid = server_uuid
        self.identity_error = False

    def optional(self, sql, what, notes, timeout=None):
        # As Reader.optional, but identify the saved member and omit untrusted
        # client diagnostics, which can contain credentials or arbitrary SQL.
        try:
            return self.rows(sql, timeout=timeout)
        except ConnectorError as exc:
            if any(code in str(exc) for code in ("ERROR 2002", "ERROR 2003", "ERROR 2013", "timed out")):
                raise
            notes.append(_clusterset_route_problem(self.route, self.cluster_name, "reading " + what, exc))
            return None

    def rows(self, sql, timeout=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectorError("ClusterSet route read timed out.")
        if self.identity_error:
            raise _ClusterSetIdentityError("The connection reached a different server during this read.")
        if self.server_uuid is None:
            # Bind on an identity-only query. Other callers, including the
            # cleanup verifier, must never use this reader unbound for facts.
            if sql not in ("SELECT @@server_uuid",
                           "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('server_uuid')"):
                raise _ClusterSetIdentityError("The server UUID must be read before route facts.")
            rows = self._client_reader.rows(sql, timeout=remaining)
            if sql == "SELECT @@server_uuid":
                uuid = rows[0][0] if len(rows) == 1 and len(rows[0]) == 1 else None
            else:
                matches = [r[1] for r in rows if len(r) == 2 and r[0] == "server_uuid"]
                uuid = matches[0] if len(matches) == 1 else None
            self.server_uuid = _text(uuid)
            return rows
        # Reader starts a new client for every call. Put the UUID in the
        # same client session as the facts, including empty result sets.
        rows = self._client_reader.rows("SELECT @@GLOBAL.server_uuid; " + sql, timeout=remaining)
        if (not rows or len(rows[0]) != 1 or not rows[0][0] or
                rows[0][0].lower() != self.server_uuid.lower()):
            self.identity_error = True
            raise _ClusterSetIdentityError("The connection reached a different server during this read.")
        return rows[1:]


def _clusterset_explicit_endpoint(address):
    """A metadata address is evidence only when it contains a valid port."""
    address = (address or "").strip()
    if not _ADDRESS.fullmatch(address):
        return None
    host, port = _split_address(address)
    if not host or port is None or not 1 <= port <= 65535:
        return None
    if ":" in host:
        import ipaddress
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return None
    return host.strip().lower(), port


_CLUSTERSET_SNAPSHOT_QUERIES = (
    ("version", _MD_VERSION_SQL, 3),
    # mysql --batch prints both SQL NULL and the literal text "NULL" as
    # NULL. Prefix names inside the server projection so those two values
    # remain distinguishable before Reader decodes the result.
    ("membership", "SELECT clusterset_id, cluster_id, CONCAT('=', cluster_name), "
     "member_role, invalidated, CONCAT('=', domain_name), view_id, router_options "
     "FROM (" + _MD_CLUSTERSET_SQL + ") AS clusterset_names", 8),
    ("instances", _MD_INSTANCES_SQL, 7),
    ("groups", "SELECT cluster_id, CONCAT('=', cluster_name), primary_mode, group_name "
     "FROM (" + _MD_CLUSTERS_SQL + ") AS cluster_names", 4),
    ("routers", _CS_ROUTERS_SQL, 7),
)


def _decode_metadata_names(rows, columns):
    """Undo SQL name prefixes without conflating literal NULL with SQL NULL."""
    if rows is None:
        return None
    decoded = [list(row) for row in rows]
    for row in decoded:
        for column in columns:
            if column >= len(row):
                raise _ClusterSetIdentityError("The ClusterSet metadata name projection was incomplete.")
            value = row[column]
            if value is not None:
                if not isinstance(value, str) or not value.startswith("="):
                    raise _ClusterSetIdentityError("The ClusterSet metadata name encoding was invalid.")
                row[column] = value[1:]
    return decoded


def _clusterset_metadata_snapshot(reader):
    """Read related metadata on one UUID-guarded, repeatable-read snapshot.

    MySQL can ignore WITH CONSISTENT SNAPSHOT at other isolation levels. A
    marker before each result set distinguishes projections with equal column
    counts, including empty result sets. Router privileges are optional: on a
    failed Router read retry the required projections and leave Routers unread.
    """
    def read(include_routers):
        queries = _CLUSTERSET_SNAPSHOT_QUERIES if include_routers else _CLUSTERSET_SNAPSHOT_QUERIES[:-1]
        parts = ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                 "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"]
        for name, sql, _ in queries:
            parts.extend(("SELECT '__myflames_clusterset_%s__'" % name, sql))
        parts.append("COMMIT")
        # Separate statements by input line so an optional Router privilege
        # failure can be attributed to that projection, not another read.
        data = reader.rows(";\n".join(parts))
        result, index = {}, -1
        for row in data:
            next_index = index + 1
            if (next_index < len(queries) and row == ["__myflames_clusterset_%s__" % queries[next_index][0]]):
                index = next_index
                result[queries[index][0]] = []
            elif index < 0 or len(row) != queries[index][2]:
                raise _ClusterSetIdentityError("The ClusterSet metadata snapshot had an unexpected shape.")
            else:
                result[queries[index][0]].append(row)
        if index != len(queries) - 1:
            raise _ClusterSetIdentityError("The ClusterSet metadata snapshot was incomplete.")
        for projection, columns in (("membership", (2, 5)), ("groups", (1,))):
            result[projection] = _decode_metadata_names(result[projection], columns)
        return result

    try:
        return read(True), None
    except _ClusterSetIdentityError:
        raise
    except ConnectorError as exc:
        # A denied Router projection must not prevent topology discovery.
        # The second transaction still binds every accepted row to one view;
        # no Router state from a different snapshot is used for actions.
        from .dbops import error_code
        from .reviewed_writes import failed_line
        router_line = 2 + 2 * len(_CLUSTERSET_SNAPSHOT_QUERIES)
        if failed_line(str(exc)) != router_line or error_code(str(exc)) not in (1044, 1142, 1143, 1227):
            raise
        result = read(False)
        result["routers"] = None
        return result, exc


class _ClusterSetRouterSnapshot(object):
    def __init__(self, rows, error=None):
        self._rows, self._error = rows, error

    def rows(self, sql):
        if self._error is not None:
            raise self._error
        if self._rows is None:
            raise ValueError("The Router configuration was not read in the metadata snapshot.")
        return self._rows


class _ClusterSetDiscoveryReader(object):
    """Replay the metadata snapshot while keeping all other reads UUID-guarded."""
    def __init__(self, reader, snapshot):
        self._reader = reader
        self._results = {
            _MD_SCHEMA_SQL: [[METADATA_SCHEMA]],
            _MD_VERSION_SQL: snapshot["version"],
            _MD_CLUSTERSET_SQL: snapshot["membership"],
            _MD_INSTANCES_SQL: snapshot["instances"],
            _MD_CLUSTERS_SQL: snapshot["groups"],
            _CLUSTERSET_SNAPSHOT_QUERIES[3][1]: [
                [row[0], "=" + row[1] if row[1] is not None else None] + list(row[2:])
                for row in snapshot["groups"]],
        }

    def rows(self, sql, timeout=None):
        if sql in self._results:
            return [list(row) for row in self._results[sql]]
        return self._reader.rows(sql, timeout=timeout)

    def optional(self, sql, what, notes, timeout=None):
        if sql in self._results:
            return self.rows(sql)
        return self._reader.optional(sql, what, notes, timeout=timeout)


def _clusterset_may_exist(reader, report):
    """Skip only an absent schema or a verified pre-ClusterSet version."""
    prior = _version((report.get("metadata") or {}).get("schema_version"))
    if prior >= CLUSTERSET_MIN_METADATA:
        return True
    found = reader.rows(_MD_SCHEMA_SQL)
    try:
        # SCHEMATA is privilege-filtered: an empty result alone does not
        # distinguish an absent schema from one this account cannot see.
        versions = reader.rows(_MD_VERSION_SQL)
    except ConnectorError as exc:
        from .dbops import error_code
        if not found and error_code(str(exc)) in (1049, 1146):
            return False
        raise
    if len(versions) != 1 or len(versions[0]) != 3 or any(v is None for v in versions[0]):
        raise _ClusterSetIdentityError("The metadata schema version could not be verified.")
    version = _version(".".join(str(v) for v in versions[0]))
    if len(version) != 3:
        raise _ClusterSetIdentityError("The metadata schema version could not be verified.")
    return version >= CLUSTERSET_MIN_METADATA


def _clusterset_validate_membership(rows, set_id):
    """Reject ambiguous identities before a view can enable a change."""
    members = [row for row in rows if _text(row[0]) == set_id]
    ids = [_text(row[1]) for row in members]
    names = [_text(row[2]) for row in members]
    if (not set_id or not members or any(not value for value in ids + names) or
            len(set(ids)) != len(ids) or len(set(names)) != len(names) or
            len({_text(row[5]) for row in members}) != 1 or
            len({_text(row[6]) for row in members}) != 1 or
            any((str(row[4]).strip().upper() not in ("0", "1", "OFF", "ON", "NO", "YES", "FALSE", "TRUE")) or
                (_text(row[3]) not in ("PRIMARY", "REPLICA") and
                 not (_text(row[3]) is None and _on(row[4]) is True)) or
                _on(row[4]) is None for row in members) or
            sum(_text(row[3]) == "PRIMARY" and _on(row[4]) is False for row in members) > 1):
        raise _ClusterSetIdentityError("The ClusterSet membership was ambiguous.")


def _clusterset_inventory(values):
    """Metadata fields already used to build the top-level member report."""
    keys = ("server_uuid", "instance_type", "label", "address", "endpoint")
    rows = [tuple(item.get(key) for key in keys) for item in values]
    return sorted(rows, key=lambda row: tuple(str(value or "") for value in row))


def _clusterset_initial_metadata(reader, report, uuid):
    """Replace discovery-time metadata with reads verified on this server.

    The general cluster reader discovers a ClusterSet using separate client
    logins. Those results locate this follow-up; they cannot themselves bind
    its view, members or routing options to the connection's server UUID.
    """
    if not uuid:
        raise _ClusterSetIdentityError("This connection did not report server_uuid.")
    snapshot, router_error = _clusterset_metadata_snapshot(reader)
    versions = snapshot["version"]
    rows = snapshot["membership"]
    instances = _instances_from_rows(snapshot["instances"])
    groups = snapshot["groups"]
    if (len(versions) != 1 or len(versions[0]) != 3 or any(v is None for v in versions[0])):
        raise _ClusterSetIdentityError("The metadata schema version could not be verified.")
    schema_version = ".".join(str(v) for v in versions[0])
    if _version(schema_version) < CLUSTERSET_MIN_METADATA:
        raise _ClusterSetIdentityError("The metadata version no longer supports ClusterSet.")
    local = [i for i in instances if isinstance(i.get("server_uuid"), str) and
             i["server_uuid"].lower() == uuid.lower()]
    original = report.get("clusterset")
    local_rows = [r for r in rows if len(r) >= 7 and local and _text(r[1]) == local[0].get("cluster_id")]
    local_groups = [r for r in groups if len(r) >= 4 and local and _text(r[0]) == local[0].get("cluster_id")]
    if len(local) > 1:
        raise _ClusterSetIdentityError("The server UUID matches multiple metadata instances.")
    if not original and not local:
        observed_group = (report.get("cluster") or {}).get("uuid")
        group_ids = {_text(g[0]) for g in groups if len(g) >= 4 and observed_group and
                     isinstance(g[3], str) and g[3].lower() == observed_group.lower()}
        if any(_text(row[1]) in group_ids for row in rows):
            raise _ClusterSetIdentityError("This group belongs to a ClusterSet but its server UUID is not registered.")
        return False
    if not original and not local_rows:
        return False  # A normal standalone server or a cluster without a ClusterSet.
    if (len(local) != 1 or len(local_rows) != 1 or
            (original and _text(local_rows[0][0]) != original.get("id")) or
            len(local_groups) != 1):
        raise _ClusterSetIdentityError("The ClusterSet metadata does not match this server.")
    _clusterset_validate_membership(rows, _text(local_rows[0][0]))
    group_uuid = _text(local_groups[0][3])
    observed_group = (report.get("cluster") or {}).get("uuid")
    if (local[0].get("instance_type") != "read-replica" and observed_group and
            (not group_uuid or group_uuid.lower() != observed_group.lower())):
        raise _ClusterSetIdentityError("The metadata group does not match this server.")
    if report.get("kind") != "innodb_cluster":
        discovery = _ClusterSetDiscoveryReader(reader, snapshot)
        if local[0].get("instance_type") == "read-replica" and report.get("kind") == "none":
            if not _read_read_replica(discovery, report):
                raise _ClusterSetIdentityError("The read replica's metadata could not be identified.")
            report["message"] = None
        elif report.get("kind") == "group_replication" and observed_group and group_uuid:
            own_member = next((m for m in report.get("members") or () if m.get("local")), None)
            _read_metadata(discovery, report, observed_group, own_member)
            # The initial Group Replication verdict preceded the metadata.
            # Rebuild it once with the now-known metadata members.
            if any(m.get("state_source") == "replication_group_members" for m in report.get("members") or ()):
                report["findings"] = []
                _judge_group(report, own_member)
        else:
            raise _ClusterSetIdentityError("The server's group could not be matched to its metadata.")
    metadata = report.get("metadata") or {}
    if local[0].get("cluster_id") != metadata.get("cluster_id"):
        raise _ClusterSetIdentityError("The ClusterSet metadata does not match this server.")
    # The top-level member labels, metadata flags and expected size were
    # already built from the preliminary inventory. Do not combine those
    # with a different guarded inventory from this snapshot.
    snapshot_local = [item for item in instances if item.get("cluster_id") == metadata.get("cluster_id")]
    if _clusterset_inventory(metadata.get("instances") or ()) != _clusterset_inventory(snapshot_local):
        raise _ClusterSetIdentityError("The local instance inventory changed during the ClusterSet read.")
    metadata["schema_version"] = schema_version
    metadata["cluster_name"] = _text(local_groups[0][1])
    metadata["primary_mode"] = _text(local_groups[0][2])
    report["cluster"]["name"] = metadata["cluster_name"]
    if local[0].get("instance_type") == "read-replica":
        report["cluster"]["uuid"] = group_uuid
    by_cluster = {}
    for inst in instances:
        cid = inst.pop("cluster_id")
        by_cluster.setdefault(cid, []).append(inst)
    metadata["instances"] = by_cluster.pop(metadata["cluster_id"], [])
    previous_statements = len(report["statements"])
    _use_clusterset_metadata(report, metadata, rows, by_cluster, groups)
    if (len(report["statements"]) > previous_statements and
            report["statements"][-1] in report["statements"][:previous_statements]):
        report["statements"].pop()
    # Callers keep a reference to this object while a primary refreshes it.
    fresh = report["clusterset"]
    if original is not None:
        original.clear()
        original.update(fresh)
        report["clusterset"] = original
    report._clusterset_router_snapshot = _ClusterSetRouterSnapshot(snapshot["routers"], router_error)
    return True


def _init_clusterset_cluster(cluster, instances):
    cluster.update(route=None, read="metadata_only", read_error=None,
                   members=[dict(_cs_instance(i), state=None, role=None) for i in instances],
                   primary=None, quorum=None, super_read_only=None, fenced_writes=None, channel=None)


def _clusterset_route_problem(route, cluster_name, step, exc):
    """Name the coordinator's failing step without exposing client/server text."""
    from .connection_groups import safe_problem
    member = route.get("profile_name") or route.get("profile_id") or "This connection"
    return "%s could not be read for cluster %s during %s: %s" % (
        member, cluster_name or "unknown", step, safe_problem(exc))


def _clusterset_current_metadata(cluster, uuid):
    """Only the primary cluster's ONLINE primary with quorum supplies current metadata.

    Group Replication defaults to EVENTUAL consistency, so an ONLINE
    secondary can still have unapplied metadata transactions. The route must
    be this connection or a verified saved route.
    """
    primary = cluster.get("primary") or {}
    online = primary.get("state") == "ONLINE" or any(
        m.get("server_uuid") == uuid and m.get("state") == "ONLINE" for m in cluster.get("members") or ())
    return (cluster.get("read") in ("this_connection", "route") and cluster["role"] == "PRIMARY" and
            not cluster["invalidated"] and cluster["quorum"] is True and
            primary.get("server_uuid") == uuid and online)


def _clusterset_initial_connection_failure(exc):
    """True only for an initial, non-timeout connection or DNS failure."""
    from .dbops import error_code, target_not_connected
    message = str(exc).lower()
    # ETIMEDOUT is commonly returned as a bare OS number by the mysql
    # client: macOS 60, Linux 110 and Windows WSAETIMEDOUT 10060.
    timed_out = (re.search(r"time[\s_-]*out|timed[\s_-]*out|etimedout|wsaetimedout|deadline", message) or
                 re.search(r"\((?:60|110|10060)\)", message) or
                 re.search(r"\b(?:errno|winerror|error|code)(?:\s+code)?\s*[:=]?\s*"
                           r"(?:60|110|10060)\b", message))
    return (isinstance(exc, ConnectorError) and not timed_out and target_not_connected(exc) and
            error_code(str(exc)) in (2002, 2003, 2005))


def _read_clusterset_routes(reader, report, profiles, open_profile, current_profile_id):
    """Addresses and previous observations order probes; only a fresh UUID proves a route.

    Keep a secondary's live group view while trying its primary. All attempts,
    including that extra hop, share the cluster's open count and SQL deadline.
    A probe that reaches another cluster is useful evidence for that cluster.
    """
    from .connection_groups import record_read
    cs = report["clusterset"]
    local_uuid = report["server"].get("server_uuid")
    if not hasattr(report, "_clusterset_router_snapshot"):
        raise ValueError("The ClusterSet metadata could not be verified on this server.")
    for cluster in cs["clusters"]:
        _init_clusterset_cluster(cluster, report._clusterset_instances.get(report._clusterset_ids[cluster["name"]], []))
    current = getattr(open_profile, "current_profile", None) or {}
    current_route = {"profile_id": current_profile_id, "profile_name": current.get("name"), "self": True}
    cs["metadata_read_on"] = current_route
    identities = {p["id"]: (p.get("topology_identity") or {}).get("server_uuid") for p in profiles}
    scope = getattr(open_profile, "group_scope", None)
    report._clusterset_scope = scope
    report._clusterset_profiles = tuple(profiles)
    report._clusterset_opener = open_profile
    for read in getattr(scope, "read", ()):
        uuid = getattr(read, "identity", {}).get("server_uuid")
        if uuid:
            identities[read["id"]] = uuid
    seen, processed, observed, budgets = {current_profile_id}, set(), {}, {}
    local_read_done = [False]
    # Retained only on this request's report, for the later tier pass.
    report._clusterset_attempted = seen
    report._clusterset_reachability = {}

    def record_reachability(profile, reachable, uuid=None):
        """A failed SQL/authentication read is never evidence of an outage.

        Only a connection-level refusal on an exact saved metadata endpoint
        can mark its previously identified instance unreachable. A successful
        UUID read records the instance that actually answered.
        """
        all_instances = [(cid, inst) for cid, instances in report._clusterset_instances.items()
                         for inst in instances]
        if uuid is not None:
            matches = [(cid, inst) for cid, inst in all_instances
                       if isinstance(inst.get("server_uuid"), str) and
                       inst["server_uuid"].lower() == uuid.lower()]
        else:
            # A refusal at an SSH tunnel's local end does not establish that
            # the metadata endpoint refused a connection.
            if profile.get("ssh_host") and reachable is False:
                return
            host = str(profile.get("host") or "").strip().strip("[]").lower()
            raw_port = profile.get("port")
            port = 3306 if raw_port in (None, "") else _number(raw_port)
            endpoint = (host, port)
            if not host or port is None or not 1 <= port <= 65535:
                endpoint = None
            matches = [(cid, inst) for cid, inst in all_instances if endpoint and endpoint in {
                _clusterset_explicit_endpoint(inst.get(key) or "") for key in ("address", "endpoint")}]
            # A host-only address of another instance could name this port.
            # Cached UUIDs cannot disambiguate an endpoint that did not answer.
            if len(matches) == 1:
                matched_inst = matches[0][1]
                for _, inst in all_instances:
                    if inst is matched_inst:
                        continue
                    for key in ("address", "endpoint"):
                        other_host, other_port = _split_address(inst.get(key) or "")
                        if other_host and other_host.strip().strip("[]").lower() == host and other_port is None:
                            if reachable is False:
                                return
                            matches.append((_, inst))
            hint = identities.get(profile["id"])
            if reachable is None and isinstance(hint, str):
                # An ambiguous attempt on a known alias also vetoes a prior
                # refusal at the metadata address for the same member.
                matches.extend((cid, inst) for cid, inst in all_instances
                               if isinstance(inst.get("server_uuid"), str) and
                               inst["server_uuid"].lower() == hint.lower() and (cid, inst) not in matches)
        if reachable is not None and len(matches) != 1:
            return
        recorded = False
        for cid, inst in matches:
            member_uuid = inst.get("server_uuid")
            if not isinstance(member_uuid, str) or not _UUID.fullmatch(member_uuid):
                continue
            if sum(isinstance(i.get("server_uuid"), str) and i["server_uuid"].lower() == member_uuid.lower()
                   for _, i in all_instances) != 1:
                continue
            if (uuid is None and reachable is False and identities.get(profile["id"]) is not None and
                    str(identities[profile["id"]]).lower() != member_uuid.lower()):
                continue
            item = next((c for c in cs["clusters"] if report._clusterset_ids[c["name"]] == cid), None)
            if item is not None:
                recorded = True
                rows = report._clusterset_reachability.setdefault(item["name"], [])
                old = next((r for r in rows if r["server_uuid"] == member_uuid), None)
                if old is None:
                    rows.append({"server_uuid": member_uuid, "reachable": reachable})
                elif reachable is True or (reachable is None and old["reachable"] is not True):
                    old["reachable"] = reachable
        return recorded

    def cluster_id(cluster):
        return report._clusterset_ids[cluster["name"]]

    def known_clusters():
        # A duplicated UUID is ambiguous even if the other instance belongs
        # to a metadata cluster outside this ClusterSet.
        counts = {}
        for instances in report._clusterset_instances.values():
            for inst in instances:
                uuid = inst.get("server_uuid")
                if isinstance(uuid, str) and uuid:
                    key = uuid.lower()
                    counts[key] = counts.get(key, 0) + 1
        known = {}
        for item in cs["clusters"]:
            for inst in report._clusterset_instances.get(cluster_id(item), []):
                uuid = inst.get("server_uuid")
                if isinstance(uuid, str) and uuid and counts.get(uuid.lower()) == 1:
                    known[uuid.lower()] = item
        return known

    def budget(cluster):
        return budgets.setdefault(cluster_id(cluster),
                                  {"opens": 0, "deadline": time.monotonic() + CLUSTERSET_ROUTE_TIMEOUT})

    def candidates(cluster, primary_uuid=None):
        instances = report._clusterset_instances.get(cluster_id(cluster), [])
        if primary_uuid:
            instances = [i for i in instances if isinstance(i.get("server_uuid"), str) and
                         i["server_uuid"].lower() == primary_uuid.lower()]
        uuids = {i["server_uuid"].lower() for i in instances if isinstance(i.get("server_uuid"), str)}
        endpoints = {_clusterset_explicit_endpoint(i.get(key) or "")
                     for i in instances for key in ("address", "endpoint")}
        endpoints.discard(None)

        def rank(profile):
            uuid = identities.get(profile["id"])
            if isinstance(uuid, str) and uuid.lower() in uuids:
                return 0
            host = (profile.get("host") or "").strip().strip("[]").lower()
            raw_port = profile.get("port")
            port = 3306 if raw_port in (None, "") else _number(raw_port)
            if port is not None and 1 <= port <= 65535 and (host, port) in endpoints:
                return 1
            return 2 if not uuid else 3

        for profile in sorted(profiles, key=rank):
            if profile["id"] in seen:
                continue
            tier = rank(profile)
            # Unknown group profiles may be standalone servers. A previous
            # UUID or an exact metadata endpoint is required to contact one.
            if tier >= 2:
                continue
            yield profile

    def upgrade_primary(cluster):
        primary = cluster["primary"]
        if not open_profile or not primary or cluster["read"] == "failed":
            return
        primary_uuid = primary["server_uuid"]
        if observed.get(cluster_id(cluster)) == primary_uuid:
            return
        if cluster["local"] and primary_uuid == local_uuid and not local_read_done[0]:
            # The active connection will be read below. Do not spend another
            # cluster's search budget trying unrelated profiles to find it.
            return
        for profile in candidates(cluster, primary_uuid):
            probe(cluster, profile, primary_uuid)
            if (cluster not in cs["clusters"] or cluster["route"] is None or
                    cluster["read"] == "failed" or observed.get(cluster_id(cluster)) == primary_uuid):
                break

    def has_live_group(item):
        return item["route"] is not None and item["read"] != "failed" and item["quorum"] is not None

    def reject_mixed(item, route_reader):
        if not route_reader.identity_error:
            return False
        note = "%s could not be read for cluster %s: server_uuid changed during the read." % (
            (item.get("route") or {}).get("profile_name") or "This connection", item["name"])
        item.update(read="failed", read_error=note, primary=None, quorum=None,
                    super_read_only=None, fenced_writes=None, channel=None,
                    members=[dict(_cs_instance(i), state=None, role=None) for i in
                             report._clusterset_instances.get(cluster_id(item), [])])
        report["notes"].append(note)
        return True

    def read_live_checked(route_reader, item, uuid):
        # A later channel query can fail after the earlier queries updated the
        # report. Discard all of that route's facts on a server change.
        old_channel = cs.get("channel")
        old_findings = list(report["findings"])
        sources = [(inst, inst.get("source")) for cluster in cs["clusters"]
                   for inst in cluster.get("instances") or ()]
        result = _read_clusterset_live(route_reader, report, item, uuid)
        if reject_mixed(item, route_reader):
            cs["channel"] = old_channel
            report["findings"][:] = old_findings
            for inst, source in sources:
                inst["source"] = source
        return result

    def probe(wanted, profile, primary_uuid=None):
        limits = budget(wanted)
        if limits["opens"] >= CLUSTERSET_ROUTE_MAX_OPENS or time.monotonic() >= limits["deadline"]:
            return
        limits["opens"] += 1
        seen.add(profile["id"])
        route = {"profile_id": profile["id"], "profile_name": profile.get("name") or profile["id"], "self": False}
        step = "opening the saved connection"
        try:
            with open_profile(profile) as conn:
                peer = _ClusterSetReader(conn, limits["deadline"])
                step = "reading server_uuid"
                uuid = _text(show_variables(peer, ("server_uuid",)).get("server_uuid"))
                peer.server_uuid = uuid
                # A successful login to a different or unidentified server
                # contradicts an earlier refusal for the expected member.
                # Keep the saved hint until that ambiguity has been recorded.
                record_reachability(profile, None)
                if uuid:
                    record_reachability(profile, True, uuid)
                identities[profile["id"]] = uuid
                record_read(open_profile, profile, "read", identity={"server_uuid": uuid} if uuid else None)
                actual = known_clusters().get(uuid.lower() if uuid else None)
                if actual is None:
                    if uuid and not any(inst.get("server_uuid") == uuid for item in cs["clusters"]
                                        for inst in report._clusterset_instances.get(cluster_id(item), [])):
                        report["notes"].append("%s is not a member of any cluster in this ClusterSet (%s), "
                                               "according to the metadata read here." % (
                                                   route["profile_name"], cs["name"]))
                    else:
                        report["notes"].append("%s could not verify a route to cluster %s: server_uuid did not match "
                                               "a unique eligible metadata member." % (route["profile_name"], wanted["name"]))
                    return
                if actual is wanted and primary_uuid and uuid != primary_uuid:
                    report["notes"].append("%s could not verify the primary route for cluster %s: server_uuid "
                                           "did not match the primary read in the group." % (
                                               route["profile_name"], wanted["name"]))
                    return
                if has_live_group(actual) and not (actual is wanted and primary_uuid):
                    return
                if actual is not wanted:
                    # The identity probe belongs to the initiating search.
                    # Charge a reused route to its own cluster too, so its
                    # subsequent primary hop still fits that cluster's cap.
                    other_limits = budget(actual)
                    other_limits["deadline"] = min(other_limits["deadline"], limits["deadline"])
                    if other_limits["opens"] >= CLUSTERSET_ROUTE_MAX_OPENS:
                        note = _clusterset_route_problem(route, actual["name"], "reading group members",
                                                        ValueError("The read budget was reached."))
                        actual.update(route=route, read="failed", read_error=note)
                        observed[cluster_id(actual)] = uuid
                        report["notes"].append(note)
                        return
                    other_limits["opens"] += 1
                    peer.deadline = other_limits["deadline"]
                # A failed primary hop must not erase the secondary's group
                # rows or replace a working route with an unreadable one.
                trial = dict(actual, route=route, primary=None, quorum=None,
                             super_read_only=None, fenced_writes=None, channel=None)
                super_read_only = read_live_checked(peer, trial, uuid)
                primary_read = (trial["read"] != "failed" and trial["primary"] is not None and
                                trial["primary"]["server_uuid"] == uuid and trial["quorum"] is not None)
                improved = (trial["read"] != "failed" and trial["quorum"] is not None and not has_live_group(actual))
                if actual["route"] is None or primary_read or improved:
                    actual.update(trial)
                    observed[cluster_id(actual)] = uuid
                if primary_uuid is None or (actual is not wanted and cluster_id(actual) in processed):
                    upgrade_primary(actual)
                if actual in cs["clusters"] and observed.get(cluster_id(actual)) == uuid:
                    if _clusterset_current_metadata(actual, uuid):
                        _refresh_clusterset_metadata(peer, report, actual, uuid, observed)
                    elif (actual["quorum"] is True and
                          any(m["server_uuid"] == uuid and m["state"] == "ONLINE" for m in actual["members"]) and
                          (super_read_only is False or (actual.get("channel") or {}).get("present") is False)):
                        # A stale old primary still records the new primary as a
                        # replica. Check for a strictly newer primary view when a
                        # reachable replica primary has SRO OFF or lacks a channel.
                        _refresh_clusterset_metadata(peer, report, actual, uuid, observed, speculative=True)
                    if reject_mixed(actual, peer) and cs.get("metadata_read_on") == route:
                        cs["from_local_copy"] = True
        except (ConnectorError, ValueError, OSError) as exc:
            if (step in ("opening the saved connection", "reading server_uuid") and
                    _clusterset_initial_connection_failure(exc)):
                if not record_reachability(profile, False):
                    record_reachability(profile, None)
            else:
                record_reachability(profile, None)
            note = _clusterset_route_problem(route, wanted["name"], step, exc)
            report["notes"].append(note)
            record_read(open_profile, profile, "failed", note)
            if wanted["route"] is None:
                wanted.update(read="failed", read_error=note)

    while open_profile:
        # A primary-route metadata refresh can add, remove or rename clusters.
        cluster = next((c for c in cs["clusters"] if not c["local"] and cluster_id(c) not in processed), None)
        if cluster is None:
            break
        processed.add(cluster_id(cluster))
        if has_live_group(cluster):
            upgrade_primary(cluster)
            continue
        for profile in candidates(cluster):
            probe(cluster, profile)
            if cluster not in cs["clusters"] or has_live_group(cluster):
                break

    # Read this connection after primary metadata selection, so a changed
    # local role cannot reuse a channel read for the previous role.
    mine = next(c for c in cs["clusters"] if c["local"])
    local_reader = _ClusterSetReader(reader.conn, server_uuid=local_uuid)
    if known_clusters().get(local_uuid.lower() if local_uuid else None) is mine:
        trial = dict(mine, route=current_route, primary=None, quorum=None,
                     super_read_only=None, fenced_writes=None, channel=None)
        read_live_checked(local_reader, trial, local_uuid)
        local_is_primary = (trial["read"] != "failed" and trial["primary"] is not None and
                            trial["primary"]["server_uuid"] == local_uuid and trial["quorum"] is not None)
        if mine["route"] is None or local_is_primary or (not has_live_group(mine) and trial["quorum"] is not None):
            mine.update(trial)
            observed[cluster_id(mine)] = local_uuid
        if cs["metadata_read_on"]["self"]:
            cs["from_local_copy"] = not (observed.get(cluster_id(mine)) == local_uuid and
                                         _clusterset_current_metadata(mine, local_uuid))
    local_read_done[0] = True
    if open_profile and not has_live_group(mine):
        # A read replica or an offline local member cannot discover a primary
        # by itself. Search the same bounded, consented candidates for its
        # cluster, including peers already identified while seeking another.
        for profile in candidates(mine):
            probe(mine, profile)
            if mine not in cs["clusters"] or has_live_group(mine):
                break
    if mine in cs["clusters"]:
        upgrade_primary(mine)
    if cs["metadata_read_on"]["self"]:
        _read_clusterset_routers(report._clusterset_router_snapshot, report)
        if local_reader.identity_error:
            if (mine.get("route") or {}).get("self"):
                reject_mixed(mine, local_reader)
            cs["from_local_copy"] = True


def _refresh_clusterset_metadata(reader, report, cluster, uuid, observed=None, speculative=False):
    """Prefer the primary cluster's metadata without reopening its route.

    Membership and view_id are the primary's records, not the replica's
    replicated copy: mysql-shell/modules/adminapi/common/metadata/
    metadata-model-2.1.0.sql:588,606 and metadata-model-2.4.0.sql:763.
    """
    cs = report["clusterset"]
    try:
        snapshot, router_error = _clusterset_metadata_snapshot(reader)
        rows = snapshot["membership"]
        _clusterset_validate_membership(rows, cs["id"])
        versions = snapshot["version"]
        if (len(versions) != 1 or len(versions[0]) != 3 or any(v is None for v in versions[0]) or
                _version(".".join(str(v) for v in versions[0])) !=
                _version((report.get("metadata") or {}).get("schema_version")) or
                _version(".".join(str(v) for v in versions[0])) < CLUSTERSET_MIN_METADATA):
            raise ValueError("The primary route's metadata version differs from this server's copy.")
        target_id = report._clusterset_ids[cluster["name"]]
        target = next((r for r in rows if len(r) >= 7 and _text(r[0]) == cs["id"] and
                       _text(r[1]) == target_id and _text(r[3]) == "PRIMARY" and not _on(r[4])), None)
        if speculative:
            old_view = _number(cs.get("view_id"))
            new_view = _number(_text(target[6])) if target is not None else None
            if target is None or old_view is None or new_view is None or new_view <= old_view:
                return
        instances = _instances_from_rows(snapshot["instances"])
        groups = snapshot["groups"]
        local_id = (report.get("metadata") or {})["cluster_id"]
        mine = next((r for r in rows if len(r) >= 7 and _text(r[0]) == cs["id"] and _text(r[1]) == local_id), None)
        matches = [i for i in instances if i["server_uuid"] == uuid]
        if target is None or mine is None or len(matches) != 1 or matches[0]["cluster_id"] != target_id:
            if speculative:
                return
            raise ValueError("The primary route's metadata no longer matches this ClusterSet.")
        local_instances = [item for item in instances if item.get("cluster_id") == local_id]
        if _clusterset_inventory(local_instances) != _clusterset_inventory((report.get("metadata") or {}).get("instances") or ()):
            raise ValueError("The primary route's local instance inventory differs from this server's copy.")
        group_names = {cid: [_text(row[3]) for row in groups if _text(row[0]) == cid]
                       for cid in (local_id, target_id)}
        expected_groups = getattr(report, "_clusterset_groups", {})
        for cid, name in ((local_id, cs["local_cluster"]), (target_id, cluster["name"])):
            expected = expected_groups.get(name)
            found = group_names[cid]
            if (len(found) != 1 or not expected or not found[0] or found[0].lower() != expected.lower()):
                raise ValueError("The primary route's group identity differs from this server's copy.")
        others = {}
        for inst in instances:
            others.setdefault(inst["cluster_id"], []).append(inst)
        metadata = dict(report["metadata"], instances=others.pop(local_id, []), local_copy=None)
        old_ids = report._clusterset_ids
        old_groups = dict(report._clusterset_groups)
        old_instances = report._clusterset_instances
        old_clusters = {old_ids[c["name"]]: c for c in cs["clusters"]}
        _use_clusterset_metadata(report, metadata, rows, others, groups)
        fresh = report["clusterset"]
        # Failed probes refer to the old metadata endpoint. A refreshed view
        # can retain a UUID while moving its address, so do not carry those
        # failures forward. A successful UUID read or an ambiguous attempt
        # remains contrary evidence for the same UUID after the refresh.
        observations = {}
        for entries in report._clusterset_reachability.values():
            for entry in entries:
                member_uuid = entry.get("server_uuid")
                state = entry.get("reachable")
                key = member_uuid.lower() if isinstance(member_uuid, str) else None
                if key and (state is True or (state is None and key not in observations)):
                    observations[key] = state
        report._clusterset_reachability = {}
        names = {cid: name for name, cid in report._clusterset_ids.items()}
        for observed_uuid, state in observations.items():
            found = [(cid, inst) for cid, members in report._clusterset_instances.items() for inst in members
                     if isinstance(inst.get("server_uuid"), str) and inst["server_uuid"].lower() == observed_uuid]
            if len(found) == 1 and found[0][0] in names:
                report._clusterset_reachability.setdefault(names[found[0][0]], []).append(
                    {"server_uuid": found[0][1]["server_uuid"], "reachable": state})
        for index, item in enumerate(fresh["clusters"]):
            cid = report._clusterset_ids[item["name"]]
            old = old_clusters.get(cid)
            verified_uuid = (observed or {}).get(cid)
            matches = [i for members in report._clusterset_instances.values() for i in members
                       if verified_uuid and i.get("server_uuid") == verified_uuid]
            belongs = any(i.get("server_uuid") == verified_uuid
                          for i in report._clusterset_instances.get(cid, []))
            old_group = old_groups.get(old["name"]) if old else None
            new_group = report._clusterset_groups.get(item["name"])
            same_group = (isinstance(old_group, str) and isinstance(new_group, str) and
                          old_group.lower() == new_group.lower())
            if old is cluster or (old and same_group and old["role"] == item["role"] and len(matches) == 1 and belongs):
                # Retain live reads obtained earlier in this pass, including a
                # candidate first reached while looking for another cluster.
                # Rebuild its inventory from the new snapshot, not the old
                # metadata. Carry live state only for an unambiguous UUID.
                previous_members = old.get("members") or []
                previous_ids = {i.get("server_uuid") for i in old_instances.get(cid, [])}
                current_instances = report._clusterset_instances.get(cid, [])
                current_ids = {i.get("server_uuid") for i in current_instances}
                members = []
                for instance in current_instances:
                    member = dict(_cs_instance(instance), state=None, role=None)
                    prior = [m for m in previous_members if member["server_uuid"] and
                             m.get("server_uuid") == member["server_uuid"]]
                    if len(prior) == 1:
                        member.update(state=prior[0].get("state"), role=prior[0].get("role"),
                                      source=prior[0].get("source", False))
                    members.append(member)
                # An actually observed GR member outside both metadata views
                # is live evidence, not a stale metadata member.
                members.extend(dict(m) for m in previous_members if m.get("state") is not None and
                               m.get("server_uuid") not in previous_ids and
                               m.get("server_uuid") not in current_ids)
                previous_primary = (old.get("primary") or {}).get("server_uuid")
                removed_live = any(m.get("state") is not None and m.get("server_uuid") in previous_ids and
                                   m.get("server_uuid") not in current_ids for m in previous_members)
                old.update(item)
                old["members"] = members
                if previous_primary:
                    old["primary"] = next((m for m in members if m.get("server_uuid") == previous_primary), None)
                if removed_live or (previous_primary and old["primary"] is None):
                    old.update(read="failed", read_error="The member inventory changed after the live group read.",
                               primary=None, quorum=None, super_read_only=None, fenced_writes=None,
                               disable_super_read_only_if_primary=None, channel=None)
                fresh["clusters"][index] = old
            else:
                _init_clusterset_cluster(item, report._clusterset_instances.get(cid, []))
                if old is not None:
                    # A route probe can still hold this object while another
                    # cluster supplies fresh metadata. Keep that reference
                    # current even when its previous live evidence is reset.
                    old.clear()
                    old.update(item)
                    fresh["clusters"][index] = old
        cs.update(fresh)
        cs.update(metadata_read_on=cluster["route"], from_local_copy=False)
        report["clusterset"] = cs
        _read_clusterset_routers(_ClusterSetRouterSnapshot(snapshot["routers"], router_error), report)
    except (ConnectorError, ValueError, OSError) as exc:
        if speculative:
            return
        report["notes"].append(_clusterset_route_problem(
            cluster["route"], cluster["name"], "reading primary cluster metadata", exc) +
            " Using the previously read metadata copy.")


def _read_clusterset_live(reader, report, cluster, uuid):
    """Use one verified route; primary-only facts stay unknown on a secondary.

    Group rows and quorum: mysql-server/plugin/group_replication/src/
    member_info.cc:1450-1464; primary channel: mysql-shell/modules/adminapi/
    cluster_set/status.cc:118,423 (factsheet, SQL verification).
    Return raw primary SRO for metadata discovery: absence of write fencing
    alone does not prove a writable primary.
    """
    route = cluster["route"]
    # Bind the route to the UUID observed in this pass, not a saved address.
    route["server_uuid"] = uuid
    reader.route, reader.cluster_name = route, cluster["name"]
    step = "reading group members and super_read_only"
    super_read_only = None
    cluster["super_read_only"] = None
    cluster["fenced_writes"] = None
    cluster["disable_super_read_only_if_primary"] = None
    cluster["read"] = "this_connection" if route["self"] else "route"
    cluster["read_error"] = None
    try:
        # Even the current connection's earlier group rows were read by a
        # different client login. Read them again with this route's UUID.
        # Read SRO alongside group membership, independently of the optional
        # member-action SELECT below. This preserves it when that SELECT is
        # denied without adding another mysql login to the route budget.
        rows = reader.rows("SELECT %s FROM performance_schema.replication_group_members;\n"
                           "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('super_read_only')" %
                           ", ".join(_GR_MEMBERS_COLUMNS))
        variables = [row for row in rows if len(row) == 2 and row[0] == "super_read_only"]
        rows = [row for row in rows if not (len(row) == 2 and row[0] == "super_read_only")]
        known = {m["server_uuid"]: m for m in cluster["members"]}
        live = []
        for row in rows:
            row = list(row) + [None] * (7 - len(row))
            member = dict(known.get(_text(row[1])) or _cs_instance({
                "server_uuid": _text(row[1]), "address": join_address(_text(row[2]), _number(row[3])),
                "instance_type": "group-member"}))
            member.update(state=_text(row[4]), role=_text(row[5]))
            live.append(member)
        by_uuid = {m["server_uuid"]: m for m in live}
        cluster["members"] = live + [m for m in cluster["members"] if m["server_uuid"] not in by_uuid]
        mine = by_uuid.get(uuid)
        if not mine or mine["state"] not in ("ONLINE", "RECOVERING"):
            cluster["read_error"] = "The route is not an online group member, so the group's state is not visible."
            member = route.get("profile_name") or route.get("profile_id") or "This connection"
            report["notes"].append("%s could not read group membership for cluster %s: %s" % (
                member, cluster["name"], cluster["read_error"]))
            return
        cluster["quorum"] = sum(m["state"] != "UNREACHABLE" for m in live) > len(live) // 2
        primary = [m for m in live if m["role"] == "PRIMARY" and m["state"] == "ONLINE"]
        cluster["primary"] = primary[0] if len(primary) == 1 else None
        if cluster["primary"] is None:
            return
        if cluster["primary"]["server_uuid"] != uuid:
            cluster["read_error"] = "The route is a secondary member; the primary's fencing and channel were not read."
            return
        # Shell requires SRO ON and automatic SRO removal disabled on this
        # same primary (cluster/cluster_impl.cc:2043-2067). The member-action
        # table exists from MySQL 8.0.26; an absent or denied read is unknown.
        if len(variables) == 1 and str(variables[0][1]).strip().upper() in ("0", "1", "OFF", "ON"):
            super_read_only = _on(variables[0][1])
            cluster["super_read_only"] = super_read_only
        else:
            cluster["read_error"] = "The primary's super_read_only was not read, so its writability is unknown."
            report["notes"].append(cluster["read_error"])
        step = "reading primary fencing (super_read_only and replication_group_member_actions)"
        fencing_notes = []
        fencing = reader.optional(
            "SELECT enabled FROM performance_schema.replication_group_member_actions "
            "WHERE name = 'mysql_disable_super_read_only_if_primary'",
            "primary fencing (super_read_only and replication_group_member_actions)", fencing_notes)
        if fencing is None:
            cluster["read_error"] = " ".join(filter(None, (cluster["read_error"], fencing_notes[-1])))
            report["notes"].extend(fencing_notes)
        else:
            if len(fencing) != 1 or len(fencing[0]) != 1 or str(fencing[0][0]) not in ("0", "1"):
                reason = (
                    "The primary's fencing was not read: replication_group_member_actions did not return "
                    "an enabled value for mysql_disable_super_read_only_if_primary.")
                cluster["read_error"] = " ".join(filter(None, (cluster["read_error"], reason)))
                report["notes"].append(reason)
            else:
                disable_sro = str(fencing[0][0]) == "1"
                cluster["disable_super_read_only_if_primary"] = disable_sro
                if super_read_only is not None:
                    cluster["fenced_writes"] = super_read_only and not disable_sro
        is_primary = cluster["role"] == "PRIMARY" and not cluster["invalidated"]
        # Keep the original local channel and findings for the existing replica
        # cluster view and action lane; also inspect the primary cluster's
        # clusterset_replication channel (Shell requires it absent or stopped).
        channel_cs = (report["clusterset"] if (route["self"] and not is_primary)
                      else {"clusters": report["clusterset"]["clusters"]})
        local = (next((m for m in report["members"] if m["uuid"] == uuid), None)
                 if (route["self"] and not is_primary) else None)
        step = "reading the clusterset_replication channel"
        channel = _read_clusterset_channel(reader, report, channel_cs, local, primary_cluster=is_primary)
        cluster["channel"] = channel
    except (ConnectorError, ValueError, OSError) as exc:
        cluster.update(read="failed", read_error=_clusterset_route_problem(route, cluster["name"], step, exc))
        report["notes"].append(cluster["read_error"])
    return super_read_only


def _primary_channel_problem(channel):
    """Describe an unexpected active or errored clusterset_replication channel on the primary."""
    if not isinstance(channel, dict) or channel.get("present") is not True:
        return None
    receiver, applier = channel.get("receiver_state"), channel.get("applier_state")
    last_err = channel.get("last_error_number")
    worker_errors = [w for w in channel.get("workers") or () if w.get("last_error_number")]
    worker_active = [w for w in channel.get("workers") or () if w.get("state") not in (None, "OFF")]
    if (receiver in ("ON", "CONNECTING", "ERROR") or applier in ("ON", "ERROR") or
            last_err or worker_errors or worker_active):
        detail = (", receiver error %s" % last_err if last_err else
                  (", worker error %s" % worker_errors[0].get("last_error_number") if worker_errors else
                   (", active applier worker" if worker_active and applier != "ON" else "")))
        return ("Unexpected clusterset_replication channel on the primary cluster (receiver %s, applier %s%s)."
                % (receiver or "unknown", applier or "unknown", detail))
    return None


def _clusterset_status(cluster, primary_uuid=None):
    """Application evidence words, not a claim to Shell's GTID/global status.

    Shell distinguishes global and channel status (mysql-shell/modules/
    adminapi/cluster_set/status.cc:55-88,484-495 and cluster_set_impl.cc:435-458,
    541-588). In particular CONNECTING is not enough for this view's
    receiver-ON gate.
    """
    if cluster["invalidated"]:
        return "INVALIDATED", "The metadata marks this cluster invalidated; its live replication and writes may continue."
    if cluster["read"] == "metadata_only":
        return "NOT_READ", "No saved connection in this group was verified as a route into this cluster."
    if cluster["read"] == "failed":
        return "NOT_READ", cluster["read_error"] or "The route could not read this cluster."
    if cluster["quorum"] is None:
        return "NOT_READ", cluster["read_error"] or "The route cannot see the group's quorum."
    if not cluster["quorum"]:
        return "NOT_OK", "The group members read on this route do not form a reachable majority."
    if cluster["primary"] is None:
        return "NOT_OK", "The group members read on this route name no unique ONLINE primary."
    if cluster["role"] == "PRIMARY":
        channel = cluster.get("channel")
        channel_problem = _primary_channel_problem(channel)
        if channel_problem and isinstance(channel, dict):
            channel["status"] = "MISCONFIGURED"
        elif isinstance(channel, dict) and channel.get("present") is False:
            channel["status"] = "MISSING"
        elif (isinstance(channel, dict) and channel.get("present") is True and not channel.get("read_error") and
              channel.get("receiver_state") == "OFF" and channel.get("applier_state") == "OFF"):
            channel["status"] = "STOPPED"
        if cluster.get("super_read_only") is None:
            # Quorum and an ONLINE primary do not establish writability.
            # A secondary route or a missing variable leaves it unknown,
            # even when the channel or member action was readable.
            reason = "The primary's super_read_only was not read, so its writability cannot be confirmed."
            if cluster["read_error"]:
                reason += " " + cluster["read_error"]
            if channel_problem:
                reason += " " + channel_problem
            return "NOT_READ", reason
        if cluster["fenced_writes"] is True:
            reason = ("The cluster has quorum and its primary reports super_read_only ON "
                      "with mysql_disable_super_read_only_if_primary disabled.")
            if channel_problem:
                reason += " " + channel_problem
            return "FENCED_WRITES", reason
        if cluster.get("super_read_only") is True:
            if cluster["fenced_writes"] is False:
                reason = ("The cluster has quorum and an ONLINE primary, but the primary reports super_read_only ON "
                          "so writes are currently blocked (mysql_disable_super_read_only_if_primary is enabled, "
                          "so it is not write-fenced by MySQL Shell's rule).")
            else:
                reason = ("The cluster has quorum and an ONLINE primary, but the primary reports super_read_only ON "
                          "so writes are currently blocked. The primary's fencing was not read.")
                if cluster["read_error"]:
                    reason += " " + cluster["read_error"]
            if channel_problem:
                reason += " " + channel_problem
            return "NOT_OK", reason
        if channel_problem:
            reason = channel_problem
            if cluster["fenced_writes"] is None:
                reason += " The primary's fencing was not read."
                if cluster["read_error"]:
                    reason += " " + cluster["read_error"]
            return "OK_MISCONFIGURED", reason
        reason = "The group members read on this route show quorum and an ONLINE primary."
        if cluster["fenced_writes"] is None:
            reason += " The primary's fencing was not read."
            if cluster["read_error"]:
                reason += " " + cluster["read_error"]
        if ((channel is None or channel.get("present") is None or channel.get("read_error") or
             (channel.get("present") is True and channel.get("status") != "STOPPED")) and
                "channel" not in reason):
            reason += " The primary's clusterset_replication channel was not read in full."
        return "OK", reason
    if cluster["role"] != "REPLICA":
        return "NOT_READ", "The metadata does not name this cluster's role."
    channel = cluster["channel"]
    if channel is None:
        return "NOT_READ", cluster["read_error"] or "The replica cluster's primary channel was not read."
    if channel["present"] is False:
        channel["status"] = "MISSING"
        return "NOT_REPLICATING", "The replica primary has no clusterset_replication receiver row."
    receiver, applier = channel.get("receiver_state"), channel.get("applier_state")
    source_uuid = channel.get("source_uuid")
    wrong_source = (bool(primary_uuid) and source_uuid is not None and
                    source_uuid.lower() != primary_uuid.lower())
    if receiver in ("OFF", "CONNECTING", "ERROR") or applier in ("OFF", "ERROR") or channel.get("last_error_number"):
        if receiver == "ERROR" or applier == "ERROR" or channel.get("last_error_number"):
            channel["status"] = "ERROR"
        elif receiver == "CONNECTING" and applier != "OFF":
            channel["status"] = "CONNECTING"
        else:
            channel["status"] = "STOPPED"
        reason = "The primary's clusterset_replication channel reports receiver %s, applier %s%s." % (
            receiver or "unknown", applier or "unknown",
            ", receiver error %s" % channel["last_error_number"] if channel.get("last_error_number") else "")
        if wrong_source and source_uuid:
            reason += " Connected source UUID %s does not match the current primary %s." % (source_uuid, primary_uuid)
        return "NOT_REPLICATING", reason
    if wrong_source and receiver == "ON":
        channel["status"] = "MISCONFIGURED"
        reason = ("The replica primary's clusterset_replication channel is misconfigured: connected to source UUID %s "
                  "instead of the current primary %s." % (source_uuid or "(empty)", primary_uuid))
        if channel["read_error"]:
            reason += " " + channel["read_error"]
        return "OK_MISCONFIGURED", reason
    if channel["read_error"] or receiver != "ON" or applier != "ON":
        return "NOT_READ", channel["read_error"] or "The primary's receiver and applier state were not both readable."
    channel["status"] = "OK"
    reason = "The cluster has quorum and an ONLINE primary with its replication receiver and applier ON."
    if cluster["fenced_writes"] is None and cluster["read_error"]:
        reason += " The primary's fencing was not read. " + cluster["read_error"]
    return "OK", reason


def _judge_clusterset(cs):
    primary = next((c for c in cs["clusters"] if c["role"] == "PRIMARY" and not c["invalidated"]), None)
    primary_uuid = (
        (primary.get("primary") or {}).get("server_uuid")
        if primary and primary.get("quorum") is True else None
    )
    for cluster in cs["clusters"]:
        cluster.setdefault("disable_super_read_only_if_primary", None)
        cluster["status"], cluster["status_reason"] = _clusterset_status(cluster, primary_uuid=primary_uuid)
        cluster["gtid_consistency"] = None
    if primary is None:
        status, why = "UNAVAILABLE", "The metadata names no valid primary cluster."
    elif primary["status"] == "FENCED_WRITES":
        status, why = "UNAVAILABLE", "Primary Cluster is fenced from write traffic."
    elif primary["status"] == "NOT_OK":
        if (primary.get("quorum") is True and primary.get("primary") is not None and
                primary.get("super_read_only") is True):
            status, why = "UNAVAILABLE", "Primary Cluster is not writable: its primary reports super_read_only ON."
        else:
            status, why = "UNAVAILABLE", "The primary cluster lacks quorum or an ONLINE primary."
    elif primary["status"] == "OK_MISCONFIGURED":
        status, why = "AVAILABLE", "Primary Cluster available but requires attention."
    elif all(c["status"] == "OK" for c in cs["clusters"]):
        status, why = "HEALTHY", "Every cluster was read with quorum, an ONLINE primary and the required replication state."
    elif primary["status"] == "OK":
        status, why = "AVAILABLE", "The primary cluster is OK; at least one other cluster is not OK or was not read."
    else:
        status, why = "UNKNOWN", "The primary cluster's availability could not be established from these reads."
    cs.update(status=status, status_reason=why, gtid_consistency=None)


def _read_clusterset(reader, report, metadata, local, others=None, cluster_rows=None):
    """The InnoDB ClusterSet this cluster belongs to, from the metadata
    (``report["clusterset"]``; absent outside a ClusterSet). A replica
    cluster's metadata is a copy replicated from the primary cluster over the
    clusterset_replication channel, so what it says about the other clusters
    can be behind: MySQL Shell's clusterSet.status() reads the primary
    cluster. On the primary of a replica cluster the channel itself is read."""
    if _version(metadata.get("schema_version")) < CLUSTERSET_MIN_METADATA:
        return
    rows = reader.optional(_MD_CLUSTERSET_SQL, "the InnoDB ClusterSet metadata (v2_cs_members)", report["notes"])
    if not rows:
        return
    _use_clusterset_metadata(report, metadata, rows, others or {}, cluster_rows or [])


def _use_clusterset_metadata(report, metadata, rows, others, cluster_rows):
    rows = [list(r) + [None] * (8 - len(r)) for r in rows]
    mine = next((r for r in rows if _text(r[1]) == metadata.get("cluster_id")), None)
    if mine is None:
        return
    report["statements"].append("SELECT mysql_innodb_cluster_metadata.v2_cs_members, v2_cs_clustersets")
    groups = {_text(row[0]): _text(row[3]) for row in cluster_rows or () if len(row) > 3}
    group_rows = [row for row in cluster_rows or () if len(row) > 3]
    clusters = []
    for r in rows:
        if _text(r[0]) != _text(mine[0]):
            continue
        # The other clusters' instances, from the shared metadata: who is in
        # them, not their state (that is read on them).
        instances = [] if r is mine else [_cs_instance(i) for i in (others or {}).get(_text(r[1])) or ()]
        instances.sort(key=lambda i: _natural(i["label"] or i["address"] or ""))
        group_uuid = groups.get(_text(r[1]))
        # A malformed or ambiguous group cannot map a stored Router target to
        # one cluster. Do not publish an invented or arbitrary mapping.
        if (not isinstance(group_uuid, str) or not _UUID.fullmatch(group_uuid) or
                sum(_text(row[0]) == _text(r[1]) for row in group_rows) != 1 or
                sum(isinstance(row[3], str) and row[3].lower() == group_uuid.lower()
                    for row in group_rows) != 1):
            group_uuid = None
        clusters.append({"name": _text(r[2]), "uuid": group_uuid,
                         "role": _text(r[3]), "invalidated": _on(r[4]) is True,
                         "local": r is mine, "instances": instances})
    # The private map also binds complete group identity into action reviews.
    report._clusterset_groups = {_text(r[2]): groups.get(_text(r[1])) for r in rows
                                if _text(r[0]) == _text(mine[0])}
    report._clusterset_ids = {_text(r[2]): _text(r[1]) for r in rows if _text(r[0]) == _text(mine[0])}
    report._clusterset_instances = dict(others)
    report._clusterset_instances[metadata["cluster_id"]] = metadata["instances"]
    clusters.sort(key=lambda c: (c["role"] != "PRIMARY", _natural(c["name"] or "")))
    me = next(c for c in clusters if c["local"])
    primary = next((c for c in clusters if c["role"] == "PRIMARY" and not c["invalidated"]), None)
    cs = {"id": _text(mine[0]), "name": _text(mine[5]), "local_cluster": me["name"], "local_role": me["role"],
          "invalidated": me["invalidated"], "primary_cluster": primary["name"] if primary else None,
          "clusters": clusters, "channel": None, "view_id": _text(mine[6]), "metadata_read_on": None,
          "routers": None,
          "routing_options": {"stored_configuration": True,
                              "options": _clusterset_options(mine[7], report["notes"])},
          # Only the selected metadata source's live membership and quorum
          # can clear this caveat; a metadata role alone cannot.
          "from_local_copy": True}
    report["clusterset"] = cs


# Shell setRoutingOption keys, factsheet section 5 / common/router.h.
# Stored Router Configuration and unrelated settings are deliberately absent.
CLUSTERSET_ROUTING_KEYS = ("target_cluster", "invalidated_cluster_policy", "stats_updates_frequency",
                         "use_replica_primary_as_rw", "read_only_targets", "tags", "guideline")


def _clusterset_options(value, notes):
    if value is None:
        # SQL NULL is an empty override, not a failed metadata read:
        # mysql-shell/modules/adminapi/common/metadata/metadata-model-2.4.0.sql:1130-1133.
        # Invalid JSON still returns None below, with a diagnostic.
        return {}
    try:
        options = json.loads(value)
        if isinstance(options, dict):
            # Reject non-JSON numbers accepted by Python's decoder, including
            # overflowed exponents; the report must serialize as strict JSON.
            json.dumps(options, allow_nan=False)
            return {key: options[key] for key in CLUSTERSET_ROUTING_KEYS if key in options}
    except (TypeError, ValueError, RecursionError):
        pass
    notes.append("Stored Router options were not a readable JSON object.")
    return None


def _read_clusterset_routers(reader, report):
    from .connection_groups import safe_problem
    cs = report["clusterset"]
    try:
        rows = reader.rows(_CS_ROUTERS_SQL)
        names = {cid: name for name, cid in report._clusterset_ids.items()}
        routers = []
        for row in rows:
            row = list(row) + [None] * (7 - len(row))
            set_id, cluster_id = _text(row[1]), _text(row[2])
            if set_id != cs["id"] and not (set_id is None and cluster_id in names):
                continue
            # Shell's Router identifier is address::router_name, including
            # the trailing :: when router_name is empty (metadata-model-2.4.0
            # v2_cs_router_options). _text alone would lose that empty name.
            address, router_name = _text(row[4]), _text(row[3]) or ""
            name = "%s::%s" % (address, router_name) if address is not None else None
            routers.append({"id": _number(row[0]), "name": name, "address": address,
                            "version": _text(row[5]), "cluster_name": names.get(cluster_id),
                            "options": _clusterset_options(row[6], report["notes"]), "stored_configuration": True})
        cs["routers"] = sorted(routers, key=lambda r: _natural(r["name"] or r["address"] or ""))
    except (ConnectorError, ValueError, OSError) as exc:
        cs["routers"] = None
        report["notes"].append("Stored Router configuration could not be read: %s" % safe_problem(exc))


def _clusterset_findings(report):
    cs = report["clusterset"]
    clusters = cs["clusters"]
    me = next(c for c in clusters if c["local"])
    if cs["from_local_copy"] and me["role"] != "PRIMARY":
        report["notes"].append("This cluster is not the ClusterSet's primary cluster, so the ClusterSet read here "
                               "is its replicated copy of the metadata and may be behind. Connect to the primary "
                               "cluster for the current view.")
    elif cs["from_local_copy"] and (report.get("metadata") or {}).get("local_copy") == "read_replica":
        report["notes"].append("This server is a read replica, so the ClusterSet read here is its replicated copy "
                               "of the metadata and may be behind. Connect to a group member of the primary "
                               "cluster for the current view.")
    elif cs["from_local_copy"]:
        report["notes"].append("The ClusterSet metadata was not read on the primary cluster's ONLINE primary "
                               "with quorum. This is a replicated copy and may be behind; connect to the "
                               "primary cluster's ONLINE primary with quorum for the current view.")
    name = me["name"] or "This cluster"
    if me["invalidated"]:
        _finding(report, "problem", "clusterset_invalidated", "%s is invalidated in the ClusterSet" % name,
                 "%s is not in the ClusterSet's latest view (invalidated, for example after a failover); "
                 "this does not prove that replication or writes stopped. Check its live state and consistency "
                 "before rejoining it (clusterSet.rejoinCluster)." % name)
    others = [c["name"] for c in clusters if c["invalidated"] and not c["local"]]
    if others and me["role"] == "PRIMARY":
        _finding(report, "watch", "clusterset_invalidated", "%s invalidated in the ClusterSet" % _names(others),
                 "%s %s not in the ClusterSet's latest view; this does not prove that replication or writes stopped."
                 % (_names(others), "is" if len(others) == 1 else "are"))
    if cs["primary_cluster"] is None:
        _finding(report, "problem", "clusterset_no_primary", "No primary cluster in the ClusterSet",
                 "The ClusterSet metadata read here names no valid primary cluster.")


def _mark_source(cs, address, source_uuid=None):
    """Mark the primary cluster's instance the channel connects to (preferring
    SOURCE_UUID when read, else falling back to the configured address)."""
    host, port = _split_address(address) if address else ("", None)
    for cluster in cs["clusters"]:
        if cluster["role"] != "PRIMARY":
            continue
        for inst in cluster["instances"]:
            if source_uuid:
                if (inst.get("server_uuid") or "").lower() == source_uuid.lower():
                    inst["source"] = True
                continue
            h, p = _split_address(inst["address"] or "")
            if h and host and h.lower() == host.lower() and (p or 3306) == (port or 3306):
                inst["source"] = True


def _read_clusterset_channel(reader, report, cs, local, primary_cluster=False):
    """The clusterset_replication channel on a cluster primary (replica or primary).

    Every configured channel appears in performance_schema.replication_connection_status
    (mysql-server/storage/perfschema/table_replication_connection_status.cc:206),
    so an empty result on the primary cluster proves the channel is absent.
    """
    connection = reader.optional(_CS_CONNECTION_SQL, "performance_schema.replication_connection_status",
                                 report["notes"])
    if (primary_cluster and not connection) or (connection == [] and local is None):
        report["statements"].append("SELECT performance_schema.replication_connection_status (%s)" % CLUSTERSET_CHANNEL)
        return {
            "present": None if connection is None else False,
            "workers": [],
            "read_error": "The replication channel could not be read in full." if connection is None else None,
        }
    applier = reader.optional(_CS_APPLIER_SQL, "performance_schema.replication_applier_status", report["notes"])
    report["statements"].append("SELECT performance_schema.replication_connection_status, replication_applier_status "
                                "(%s)" % CLUSTERSET_CHANNEL)
    raw_row = list(connection[0]) if connection else []
    has_source_uuid = len(raw_row) >= 4
    row = (raw_row + [None] * 4)[:4]
    receiver, error, message, source_uuid = _text(row[0]), _number(row[1]), _text(row[2]), _text(row[3])
    apply_state = _text(((applier or [[]])[0] + [None])[0]) if applier else None
    source = reader.optional(_CS_SOURCE_SQL, "performance_schema.replication_connection_configuration",
                             report["notes"])
    source_row = (list(source[0]) + [None, None])[:2] if source else [None, None]
    source_address = join_address(_text(source_row[0]), _number(source_row[1])) if _text(source_row[0]) else None
    base = dict((k, v) for k, v in (("receiver_state", receiver), ("applier_state", apply_state),
                                    ("last_error_number", error), ("last_error_message", message),
                                    ("source", source_address), ("source_uuid", source_uuid)) if v is not None)
    if not primary_cluster:
        cs["channel"] = base
        if source_address or source_uuid:
            _mark_source(cs, source_address, source_uuid)
    workers = reader.optional(_CS_WORKERS_SQL, "performance_schema.replication_applier_status_by_worker",
                              report["notes"])
    worker_rows = [list(w) + [None] * (4 - len(w)) for w in workers or ()]
    channel_report = dict(base, present=None if connection is None else bool(connection),
                          source_uuid=(source_uuid if source_uuid is not None
                                       else ("" if (has_source_uuid and receiver == "ON") else None)),
                          workers=[dict(worker_id=_number(w[0]), state=_text(w[1]),
                                        last_error_number=_number(w[2]), last_error_message=_text(w[3]))
                                   for w in worker_rows],
                          read_error=("The replication channel could not be read in full."
                                      if any(rows is None for rows in (connection, applier, source, workers)) else None))
    if any(_number(w[2]) for w in worker_rows):
        channel_report["applier_state"] = "ERROR"
    elif (worker_rows and any(_text(w[1]) == "OFF" for w in worker_rows) and
          any(_text(w[1]) == "ON" for w in worker_rows)):
        channel_report["applier_state"] = "ERROR"
    elif any(_text(w[1]) == "OFF" for w in worker_rows):
        channel_report["applier_state"] = "OFF"
    elif worker_rows and all(_text(w[1]) == "ON" for w in worker_rows) and apply_state == "ON":
        channel_report["applier_state"] = "ON"
    if local is None:
        return channel_report
    channel, last = CLUSTERSET_CHANNEL, " Its last error: %s %s." % (error, message or "") if error else ""
    if connection == [] and applier == []:
        status, why = "problem", ("This member is the primary of a replica cluster but has no %s channel: the "
                                  "cluster does not replicate from the primary cluster." % channel)
    elif receiver == "OFF" or apply_state == "OFF":
        status, why = "problem", ("The %s channel is not running (receiver %s, applier %s): this replica cluster "
                                  "does not replicate from the primary cluster.%s" % (
                                      channel, receiver or "not readable", apply_state or "not readable", last))
    elif receiver == "CONNECTING":
        status, why = "watch", "The %s channel's receiver is connecting to the primary cluster (retrying).%s" % (
            channel, last)
    elif error and receiver is not None:
        status, why = "problem", "The %s channel reports error %s: %s" % (channel, error, message or "no message")
    else:
        return channel_report
    _finding(report, status, "clusterset_channel", "The ClusterSet replication channel is %s" % (
        "not healthy" if status == "problem" else "reconnecting"), why, local["key"])
    return channel_report


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
    others = {}
    group_instances = 0
    for inst in instances:
        cluster_id = inst.pop("cluster_id")
        if cluster_id != metadata["cluster_id"]:
            others.setdefault(cluster_id, []).append(inst)
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
    _read_clusterset(reader, report, metadata, local, others, clusters)
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
    """Human text for a cluster-1.1 report (also the MCP digest)."""
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
# * InnoDB Cluster and InnoDB ClusterSet through MySQL Shell (cluster_shell.py;
#   enabled only with a sufficiently recent mysqlsh; Sh/modules/adminapi/
#   cluster/cluster_impl.cc and cluster_set/cluster_set_impl.cc):
#   - mode switches: Cluster.switchToMultiPrimaryMode() and
#     switchToSinglePrimaryMode() (:2840-2907): an InnoDB Cluster that is
#     not in a ClusterSet, every member ONLINE (All_online), a primary.
#     Besides the UDF they set auto_increment on every member and the
#     metadata's mode (switch_to_multi_primary_mode.cc:50-91).
#   - Cluster.rejoinInstance('<host:port>') (:1143-1176) for a member in the
#     metadata and not in the group (MISSING), from an ONLINE member with
#     quorum; MySQL Shell connects to the instance with the same account.
#   - Cluster.setPrimaryInstance('<host:port>') (:2909-2945) instead of the
#     switchover UDF in a ClusterSet replica cluster (see the UDF above).
#   - ClusterSet.setPrimaryCluster(name) (cluster_set_impl.cc:2396), for a
#     REPLICA cluster that is not invalidated; forcePrimaryCluster(name)
#     (:3005), from a member of a replica cluster, typed (MySQL Shell refuses
#     while any instance of the primary cluster is reachable,
#     verify_primary_cluster_not_recoverable); rejoinCluster(name) (:3324)
#     for an invalidated cluster. A replica cluster's metadata is a
#     replicated copy: MySQL Shell checks the primary cluster's before each.
#   - Every Shell call is one reviewed statement (JavaScript), checked again
#     from a fresh read like the SQL ones; an exception after part of an
#     operation ran is judged from a read after it (reviewed_writes,
#     verify_on_error).

ACTION_SCHEMA_VERSION = "cluster-action-1.1"
CLUSTER_ACTION_TAG = "/*myflames:cluster_op*/ "
GR_UDF_MIN = (8, 0, 13)
#: START GROUP_REPLICATION and the UDFs wait for the group (a join, the old
#: primary's transactions), so a send waits longer than a read.
ACTION_TIMEOUT = 120
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_GR_PRIVILEGE = "GROUP_REPLICATION_ADMIN or SUPER"


class _ActionReport(dict):
    """Request-local inputs, never fields in the public JSON report.

    Plain status/CLI reads do not need grants. Keep the originating connection
    only for a lazy catalog read, shared by every member and overview action.
    The session's CURRENT_USER() (used for denied-read GRANT suggestions without
    an extra login) and MariaDB's additional reviewed identity come from the
    same initial preflight as hostname/port and the wsrep variables, without
    changing cluster-1.1.
    """
    def __init__(self, report, conn, variables, current_user=None):
        super().__init__(report)
        self._action_conn = conn
        self._action_grants = None
        self._current_user = current_user if isinstance(current_user, str) and "@" in current_user else None
        self._clusterset_groups = {}
        self._galera_identity = ({"server_id": _number(variables.get("server_id")),
                                  "wsrep_node_name": _text(variables.get("wsrep_node_name"))}
                                 if report.get("engine") == "mariadb" else {})

    def action_grants(self):
        from . import dbops
        if self._action_grants is None:
            self._action_grants = _grant_model(self, dbops.read_grants(self._action_conn, CLUSTER_TAG))
        return self._action_grants


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


#: What myflames passes to MySQL Shell: an instance as host:port (IPv6
#: bracketed), a cluster name in the characters MySQL Shell allows (letters,
#: digits, _ - ., not starting with a digit: Sh/modules/adminapi/common/
#: common.cc validate_name). Anything else is not offered.
_ADDRESS = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9_.-]+):[0-9]{1,5}")
_CLUSTER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,62}")
#: The actions MySQL Shell runs; ids with a cluster name end in ":<name>".
CLUSTERSET_ACTIONS = ("set_primary_cluster", "force_primary_cluster", "rejoin_cluster", "repair_cluster",
                      "remove_cluster", "remove_cluster_keep", "set_routing_option", "create_replica_cluster",
                      "dissolve_clusterset", "check_clusterset")
SHELL_ACTIONS = ("rejoin_instance",) + CLUSTERSET_ACTIONS
#: All three topology methods first appear in Shell 8.0.14 (absent in
#: 8.0.13): mysql-shell tag 8.0.14, modules/adminapi/mod_dba_cluster.cc:132-142.
#: The session server and every group member need MySQL 8.0.13:
#: modules/adminapi/cluster/topology_configuration_command.cc:49-54, 231-245;
#: mysql-server tag mysql-8.0.13, plugin/group_replication/src/udf/
#: udf_multi_primary.cc:87-94 registers the UDF (absent in mysql-8.0.12).
# Option-file loading first appears in Shell 8.0.32 (shell_options.cc:
# 205-214,887-891 at that tag). Earlier methods cannot use our credentials.
SHELL_MIN_VERSION = (8, 0, 32)
# Capability gates are independent of the credential-file floor above.
# Untagged mysql-shell source citations below refer to commit 9104d2f0b.
# mysql-shell @8.0.27 modules/adminapi/mod_dba_cluster_set.cc:90-115;
# @9.0.0 modules/adminapi/mod_dba_cluster_set.cc:91 and cluster_set/api_options.cc:195-202.
SHELL_CLUSTERSET_MIN_VERSION = (8, 0, 27)
CLUSTERSET_METHOD_MINIMUM = dict((name, SHELL_CLUSTERSET_MIN_VERSION) for name in CLUSTERSET_ACTIONS)
CLUSTERSET_METHOD_MINIMUM.update(remove_cluster_keep=(9, 0, 0), dissolve_clusterset=(9, 0, 0))
# mysql-shell common/router.h at tags 8.0.27:35-47, 8.0.31:45-51,
# 8.0.32:48-55, 8.1.0:51-70, and 9.2.0:62,87-95.
CLUSTERSET_ROUTING_MINIMUM = {
    "target_cluster": (8, 0, 27), "invalidated_cluster_policy": (8, 0, 27),
    "stats_updates_frequency": (8, 0, 31), "use_replica_primary_as_rw": (8, 0, 32),
    "tags": (8, 1, 0), "read_only_targets": (8, 1, 0), "guideline": (9, 2, 0),
}
#: WL#15056 added rejoinInstance's recoveryMethod option in MySQL Shell 8.1.0.
SHELL_REJOIN_MIN_VERSION = (8, 1, 0)
#: An AdminAPI operation waits for recovery or a channel catching up.
SHELL_TIMEOUT = 600
_SHELL_PRIVILEGE = ("The cluster administrator account: MySQL Shell connects to the members with this "
                    "connection's user and password")
_SHELL_SSH_NOTE = ("MySQL Shell changes are not offered over an SSH connection: MySQL Shell connects to the other "
                   "members directly, outside the tunnel, with this connection's user and password. Run them in "
                   "MySQL Shell from a host that reaches the members.")

# One engine table maps action id and execution route to versioned requirements.
# Each requirement is a tuple of acceptable alternatives, least privilege first.
# MySQL: udf_utils.cc:35-87 and sql_parse.cc START/STOP_GROUP_REPLICATION;
# Shell: accounts.cc:122-175,244-324, Topology_configuration_command::prepare.
# A Shell topology entry uses the full list below, never a SUPER substitute.
MYSQL_ACTION_PRIVILEGES = {
    "switchover": {"udf": ((GR_UDF_MIN, (("GROUP_REPLICATION_ADMIN", "SUPER"),)),),
                   "mysqlsh": "shell_admin"},
    "switch_to_single_primary": {"udf": ((GR_UDF_MIN, (("GROUP_REPLICATION_ADMIN", "SUPER"),)),),
                                 "mysqlsh": "shell_admin"},
    "switch_to_multi_primary": {"udf": ((GR_UDF_MIN, (("GROUP_REPLICATION_ADMIN", "SUPER"),)),),
                                "mysqlsh": "shell_admin"},
    "start_member": {"sql": ((GR_MIN_VERSION, (("SUPER",),)),
                              ((8, 0, 0), (("GROUP_REPLICATION_ADMIN", "SUPER"),)))},
    "stop_member": {"sql": ((GR_MIN_VERSION, (("SUPER",),)),
                             ((8, 0, 0), (("GROUP_REPLICATION_ADMIN", "SUPER"),)))},
    # These AdminAPI methods have no ensure_user_privileges call (review B5).
    "rejoin_instance": {"mysqlsh": "shell_runtime"},
    "set_primary_cluster": {"mysqlsh": "shell_runtime"},
    "force_primary_cluster": {"mysqlsh": "shell_runtime"},
    "rejoin_cluster": {"mysqlsh": "shell_runtime"},
    # The setupAdminAccount baseline is not a per-method up-front requirement:
    # mysql-shell/modules/adminapi/common/accounts.cc:118-175;
    # cluster_set/create_replica_cluster.cc:156-168 validates the target itself.
    "repair_cluster": {"mysqlsh": "shell_runtime"},
    "remove_cluster": {"mysqlsh": "shell_runtime"},
    "remove_cluster_keep": {"mysqlsh": "shell_runtime"},
    "set_routing_option": {"mysqlsh": "shell_runtime"},
    "create_replica_cluster": {"mysqlsh": "shell_runtime"},
    "dissolve_clusterset": {"mysqlsh": "shell_runtime"},
    "check_clusterset": {"mysqlsh": "shell_runtime"},
}
MYSQL_SHELL_GLOBAL_PRIVILEGES = (
    ((0, 0, 0), ("RELOAD", "SHUTDOWN", "PROCESS", "FILE", "SELECT", "REPLICATION SLAVE",
                 "REPLICATION CLIENT", "CREATE USER")),
    ((8, 0, 0), ("SYSTEM_VARIABLES_ADMIN", "PERSIST_RO_VARIABLES_ADMIN", "REPLICATION_SLAVE_ADMIN",
                 "GROUP_REPLICATION_ADMIN", "ROLE_ADMIN", "CONNECTION_ADMIN")),
    ((8, 0, 17), ("EXECUTE", "CLONE_ADMIN")),
    ((8, 0, 18), ("REPLICATION_APPLIER",)),
    ((8, 3, 0), ("TRANSACTION_GTID_TAG",)),
)
MYSQL_SHELL_SCHEMA_PRIVILEGES = (
    ("mysql", ("DELETE", "INSERT", "UPDATE")),
) + tuple((schema, ("ALTER", "ALTER ROUTINE", "CREATE", "CREATE ROUTINE", "CREATE TEMPORARY TABLES",
                    "CREATE VIEW", "DELETE", "DROP", "EVENT", "EXECUTE", "INDEX", "INSERT", "LOCK TABLES",
                    "REFERENCES", "SHOW VIEW", "TRIGGER", "UPDATE"))
          for schema in (METADATA_SCHEMA, METADATA_SCHEMA + "_bkp", METADATA_SCHEMA + "_previous"))

# MariaDB: sys_vars.cc Sys_wsrep_desync / Sys_wsrep_provider_options and
# set_var.cc on_check_access_global -> privilege.h SUPER_ACL (review D1/E1).
# Resolve through the existing variable-specific dbops table, not a new rule.
MARIADB_ACTION_PRIVILEGES = {"desync_node": "wsrep_desync", "resync_node": "wsrep_desync",
                             "bootstrap_pc": "wsrep_provider_options"}
_PRIVILEGES_UNREADABLE = ("Privileges could not be read; the server refuses the change if one is missing.")
_SHELL_RUNTIME_PRIVILEGES = "MySQL Shell checks the privileges it needs when it runs."
_GRANT_FIX = "Run the statement below as an administrator, then Refresh."
_GRANT_MODE_UNKNOWN = ("The GRANT could not be built because partial_revokes could not be read "
                       "in this account's session. Refresh and try again.")
_GRANT_UNAVAILABLE = ("The GRANT could not be built for this account name. Grant the missing "
                      "privileges as an administrator, then Refresh.")


def _grant_model(report, grants):
    """Normalize dbops.read_grants or an existing privileges model once.

    An absent, failed, empty or unparsed read is unknown, never a denial.
    CURRENT_USER(), not the login name or a role's grantee, names the GRANT.
    """
    from . import privileges
    if not isinstance(grants, dict) or not grants.get("checked", True):
        return None
    if "global" in grants and "dynamic" in grants:
        model = dict(grants)
        partial = model.get("grant_partial_revokes", model.get("partial_revokes"))
    else:
        partial = grants.get("partial_revokes")
        model = privileges.parse_grants(grants.get("grants"), report.get("engine"), report.get("server_version"),
                                         partial_revokes=partial is True)
        model["account"] = grants.get("account")
    if not isinstance(partial, bool):
        version = _version_of(report)
        if report.get("engine") == "mariadb" or (
                report.get("engine") in ("mysql", "percona", "percona_server", "pxc") and
                len(version) == 3 and (5, 7, 0) <= version < (8, 0, 16)):
            # MySQL 5.7.44 sql/auth/sql_auth_cache.cc:1033 and 8.0.15:1163
            # always wild_compare; 8.0.16:1181 adds mysqld_partial_revokes().
            # MariaDB sql/sql_acl.cc:3257 also always wild_compare.
            partial = False
    # A successful read on an engine without partial_revokes has known
    # wildcard semantics. On versions with the variable, absence is unknown.
    model["grant_partial_revokes"] = partial if isinstance(partial, bool) else None
    if not model.get("grants") or model.get("unparsed") or not model.get("fresh_session", True):
        return None
    account = model.get("account")
    if not isinstance(account, str) or "@" not in account:
        return None
    try:
        privileges.quote_account(*account.rsplit("@", 1))
    except ValueError:
        return None
    model.update(engine=report.get("engine"), version=report.get("server_version"))
    return model


def _catalog_grants(report, checks):
    if "grants" in checks:
        return _grant_model(report, checks["grants"])
    return report.action_grants() if isinstance(report, _ActionReport) else None


def _privilege_requirements(report, action):
    """(requirements, Shell-runtime-only). Specs retain scope/grant option.

    Alternatives stay tuples internally; the catalog displays each privilege
    together with its scope and WITH GRANT OPTION when those are required.
    """
    from . import dbops
    base, _ = _action_name(action["id"])
    version = _version_of(report)
    if report.get("engine") == "mariadb":
        variable = MARIADB_ACTION_PRIVILEGES.get(base)
        if not variable:
            return [], False
        need = dbops.required_privileges("set_global", "mariadb", version, variable)
        # Both wsrep variables have a single-privilege alternative today.
        # Preserve the existing helper's alternatives without parsing grants.
        alternatives = tuple(a[0] for a in need["any_of"])
        return [(alternatives, None, False)], False
    rules = MYSQL_ACTION_PRIVILEGES.get(base, {}).get(action["via"], ())
    if rules == "shell_runtime":
        return [], True
    if rules == "shell_admin":
        names = sorted(p for since, names in MYSQL_SHELL_GLOBAL_PRIVILEGES if version >= since for p in names)
        return ([((p,), None, True) for p in names] +
                [((p,), schema, True) for schema, names in MYSQL_SHELL_SCHEMA_PRIVILEGES for p in names]), False
    requirements = ()
    for since, needed in rules:
        if version >= since:
            requirements = needed
    return [(alternatives, None, False) for alternatives in requirements], False


def _privilege_label(privilege, database, grantable):
    from .privileges import quote_identifier
    return (privilege + (" ON %s.*" % quote_identifier(database) if database else "") +
            (" WITH GRANT OPTION" if grantable else ""))


def _grant_sql(missing, account, partial_revokes=None):
    """Advisory text only. No statement built here enters the write plan."""
    from .privileges import grant_sql
    return grant_sql(missing, account, partial_revokes)


from .reviewed_writes import ActionUnavailable  # re-exported; ui.py imports it from here


def _shell(checks, minimum=SHELL_MIN_VERSION):
    # AdminAPI opens its own member sessions outside this connection's tunnel.
    if (checks or {}).get("ssh"):
        return None
    shell = (checks or {}).get("shell")
    return shell if (isinstance(shell, dict) and shell.get("path")
                     and tuple(shell.get("version_tuple") or ()) >= minimum) else None


def _missing(report):
    """Members in the InnoDB Cluster metadata but not in the group."""
    return [m for m in _members(report) if m.get("state_source") == "metadata" and m.get("state") == "MISSING"]


def _action_name(action):
    """("set_primary_cluster", "east") for "set_primary_cluster:east"."""
    base, _, name = (action or "").partition(":")
    return base, name or None


def _shell_minimum(report, action):
    base, _ = _action_name(action)
    if base == "rejoin_instance":
        return SHELL_REJOIN_MIN_VERSION
    if base in CLUSTERSET_METHOD_MINIMUM:
        return max(SHELL_MIN_VERSION, CLUSTERSET_METHOD_MINIMUM[base])
    return SHELL_MIN_VERSION


def _tool_issue(report, action, checks):
    """Tool/version checks precede state checks; never expose client error text."""
    if action["via"] == "mysqlsh":
        minimum = _shell_minimum(report, action["id"])
        version = ".".join(str(n) for n in minimum)
        if checks.get("ssh"):
            return _SHELL_SSH_NOTE, "Connect directly to a group member from a host that reaches the members."
        shell = checks.get("shell")
        fix = "Install MySQL Shell %s or later, then Refresh." % version
        if not isinstance(shell, dict) or not shell.get("path"):
            return "MySQL Shell was not found on this computer.", fix
        if not shell.get("version_tuple"):
            return "The installed MySQL Shell version could not be read.", fix
        if tuple(shell["version_tuple"]) < minimum:
            found = shell.get("version") or ".".join(str(n) for n in shell["version_tuple"])
            return "This action needs MySQL Shell %s or later; found %s." % (version, found), fix + " Get MySQL Shell."
    if report.get("kind") in ("group_replication", "innodb_cluster"):
        base, _ = _action_name(action["id"])
        usable, why = _gr_usable(report)
        if not usable:
            return why, "Use a MySQL Group Replication member running MySQL 8.0.13 or later."
        if base in ("switchover", "switch_to_multi_primary", "switch_to_single_primary"):
            # Shell's shared Topology_configuration_command::prepare checks
            # every group member, but a MISSING metadata row is not a member
            # and does not block setPrimaryInstance (cluster_impl.cc:2911-2915).
            for member in _group_members(report):
                version = _version_of({"server_version": member.get("version")})
                if not version:
                    return ("A group member's MySQL version could not be read.",
                            "Refresh from an ONLINE member to read the group versions.")
                if version < GR_UDF_MIN:
                    return ("Every group member must run MySQL 8.0.13 or later for this change.",
                            "Upgrade the group members to MySQL 8.0.13 or later, then Refresh.")
    return None


def _group_issue(report):
    local = _local_member(report)
    if local and (local.get("role") == "READ_REPLICA" or local.get("instance_type") == "read-replica"):
        return ("This connection is to a read replica, not a Group Replication member.",
                "Connect to an ONLINE Group Replication member, then Refresh.")
    if not local or local.get("state_source") != "replication_group_members":
        return ("This connection is not to a Group Replication member.",
                "Connect to an ONLINE Group Replication member, then Refresh.")
    why = _group_ready(report)
    if why:
        return why, "Restore an ONLINE member with quorum and wait for recovery or connectivity to settle, then Refresh."
    return None


_LOCAL_ACTIONS = {
    "stop_member": ("Stop Group Replication runs on the server it stops", "stop it"),
    "start_member": ("Start Group Replication runs on the server it starts", "start it"),
    "desync_node": ("Desync runs on the node it changes", "desync it"),
    "resync_node": ("Resync runs on the node it changes", "resync it"),
    "bootstrap_pc": ("Bootstrap runs on the node it changes", "bootstrap its component"),
}


def _member_profiles(report, member, checks):
    """All current profiles claiming this UUID, including unsaved passwords.

    Credential availability cannot resolve conflicting identity evidence.
    The opened member's live identity can confirm its own stored endpoint.
    """
    field = "wsrep_node_uuid" if report.get("kind") in ("galera", "pxc") else "server_uuid"
    uuid = member.get("uuid")
    if not isinstance(uuid, str) or not _UUID.fullmatch(uuid):
        return []
    current = checks.get("current_profile") or {}
    candidates = {p["id"]: p for p in checks.get("group_profiles") or () if isinstance(p, dict) and p.get("id")
                  and p.get("group") == checks.get("group_name")
                  and (p.get("topology_identity") or {}).get(field) == uuid}
    if (member.get("local") and current.get("group") == checks.get("group_name") and
            current.get("id") and target_server_identity(report).get(field) == uuid):
        candidates[current["id"]] = current
    return list(candidates.values())


def _member_address_profiles(report, member, checks):
    """Saved endpoints Review may try, without logging in or proving identity.

    Use the topology matcher's host, port and loopback rules. Check each
    profile separately so duplicate addresses remain ambiguous, including
    profiles without a saved password.
    """
    from .replication import reached_host
    from .topology_links import match_database_endpoint, split_host_port
    uuid = member.get("uuid")
    if not isinstance(uuid, str) or not _UUID.fullmatch(uuid):
        return []
    host, port = split_host_port(member.get("address"), 3306)
    host, port = member.get("host") or host, member.get("port") or port
    current = checks.get("current_profile") or {}
    observer = reached_host(current)
    candidates = {}
    for profile in checks.get("group_profiles") or ():
        if (not isinstance(profile, dict) or not profile.get("id") or
                profile.get("group") != checks.get("group_name")):
            continue
        endpoint = {"kind": "database", "profile_host": profile.get("host"),
                    "profile_port": profile.get("port") or 3306, "ssh_host": profile.get("ssh_host")}
        match, _ = match_database_endpoint(host, port, [endpoint], observer_host=observer)
        if match is not None:
            candidates[profile["id"]] = profile
    return list(candidates.values())


def _member_profile(report, member, checks):
    """Prefer confirmed identity; a unique address only chooses Review's login."""
    candidates = _member_profiles(report, member, checks)
    if not candidates:
        candidates = _member_address_profiles(report, member, checks)
    if len(candidates) != 1:
        return None
    profile = candidates[0]
    current = checks.get("current_profile") or {}
    if profile.get("remember_password") or (member.get("local") and profile["id"] == current.get("id")):
        return profile
    return None


def _member_route_issue(report, member, checks):
    """Explain ambiguity or missing credentials without promising extra probes."""
    candidates = _member_profiles(report, member, checks)
    if len(candidates) > 1:
        names = sorted(p.get("name") or p["id"] for p in candidates)
        return ("%s %s reach this member." %
                (" and ".join(names), "both" if len(names) == 2 else "all"),
                "A MySQL Router or ProxySQL connection reaches whichever server is primary. "
                "Connect to %s to act on this member, or keep only one of them in the group." % names[0])
    if not candidates:
        candidates = _member_address_profiles(report, member, checks)
        if len(candidates) > 1:
            names = sorted(p.get("name") or p["id"] for p in candidates)
            return ("%s %s use this member's address." %
                    (" and ".join(names), "both" if len(names) == 2 else "all"),
                    "Keep one saved connection for this member in the group, or correct their addresses.")
    if candidates:
        return ("The saved connection %s has no saved password." % (candidates[0].get("name") or candidates[0]["id"]),
                "Save its password before reviewing a cluster action.")
    name = member.get("name") or "this member"
    # Reuse stored and live identity evidence, including the active member,
    # rather than asking the user to confirm connections already accounted for.
    confirmed = {p["id"] for known_member in _members(report)
                 for p in _member_profiles(report, known_member, checks)}
    unconfirmed = {p["id"]: p for p in checks.get("group_profiles") or ()
                   if isinstance(p, dict) and p.get("id") and p.get("group") == checks.get("group_name")
                   and p["id"] not in confirmed}
    if unconfirmed:
        names = sorted(p.get("name") or p["id"] for p in unconfirmed.values())
        choices = " or ".join(names[:3])
        if len(names) > 3:
            choices += " and %d more" % (len(names) - 3)
        return ("No saved connection in this group is confirmed as %s yet." % name,
                "Open the one that reaches %s (%s) on its own and view Cluster once to confirm it." % (name, choices))
    fix = "Add a saved connection for %s to this group." % name
    return fix, fix


def _primary_clusterset_rejoin_ok(report):
    """Prove primary-cluster OK without inferring replica GTID consistency.

    Shell's primary branch does not compare GTIDs (mysql-shell/modules/
    adminapi/cluster_set/cluster_set_impl.cc:517-557); the replica branch
    does (:592-600). Require complete local primary observations rather
    than promoting the application's general status words to globalStatus.
    Shell still repeats its own preconditions inside rejoinInstance.
    """
    cs = report.get("clusterset") or {}
    uuid = (report.get("server") or {}).get("server_uuid")
    if (not uuid or cs.get("invalidated") is not False or cs.get("from_local_copy") is not False or
            cs.get("local_role") != "PRIMARY" or not cs.get("local_cluster") or
            cs.get("local_cluster") != cs.get("primary_cluster")):
        return False
    matches = [item for item in cs.get("clusters") or () if item.get("name") == cs["local_cluster"]]
    if len(matches) != 1:
        return False
    item = matches[0]
    route, primary = item.get("route") or {}, item.get("primary") or {}
    if (item.get("local") is not True or item.get("role") != "PRIMARY" or item.get("invalidated") is not False or
            item.get("read") != "this_connection" or item.get("read_error") or item.get("quorum") is not True or
            route.get("self") is not True or route.get("server_uuid") != uuid or
            primary.get("server_uuid") != uuid or primary.get("state") != "ONLINE" or
            item.get("super_read_only") is not False or item.get("fenced_writes") is not False):
        return False
    channel = item.get("channel")
    if not isinstance(channel, dict) or channel.get("read_error"):
        return False
    # OFF/OFF alone omits coordinator errors that Shell also checks. Only
    # a positively absent channel is sufficient for this SQL-only proof.
    return channel.get("present") is False


def _member_issue(report, action, member, checks):
    base = action["id"]
    kind = report.get("kind")
    state = member.get("state")
    name = member.get("name") or "This member"
    if kind in ("innodb_cluster", "group_replication"):
        if member.get("role") == "READ_REPLICA" or member.get("instance_type") == "read-replica":
            return ("%s is a read replica, not a Group Replication member." % name,
                    "Choose a Group Replication member.")
        if base == "rejoin_instance":
            if _version((report.get("metadata") or {}).get("schema_version")) < (2, 0, 0):
                return ("Rejoin needs InnoDB Cluster metadata 2.0.0 or later.",
                        "Upgrade the cluster metadata in MySQL Shell, then Refresh.")
            if not member.get("in_metadata"):
                return "This member is not recorded in the cluster metadata.", "Add it to the cluster in MySQL Shell first."
            if state not in ("MISSING", "OFFLINE", "ERROR"):
                return ("This member has not left the group.",
                        "Rejoin a member only after it has left the group and its fault has been checked.")
            if not _ADDRESS.fullmatch(member.get("address") or ""):
                return "This member has no valid MySQL Shell address.", "Correct its address in the cluster metadata, then Refresh."
            issue = _group_issue(report)
            if issue:
                return issue
            if not any(m.get("role") == "PRIMARY" and m.get("state") == "ONLINE" for m in _group_members(report)):
                return "No ONLINE primary is visible in this cluster.", "Restore an ONLINE primary before rejoining a member."
            if (report.get("clusterset") or {}).get("invalidated"):
                return ("This cluster is invalidated in its ClusterSet.",
                        "Rejoin this cluster to the ClusterSet before rejoining a member.")
            cs = report.get("clusterset") or {}
            status = cs.get("global_status")
            if cs and status is None and _primary_clusterset_rejoin_ok(report):
                status = "OK"
            if cs and status != "OK":
                return (("This cluster's ClusterSet status is %s; Rejoin needs exactly OK." % status if status else
                         "This cluster's ClusterSet status could not be verified as OK."),
                        "Check the cluster's globalStatus with dba.getClusterSet().status() in MySQL Shell; "
                        "restore an OK status before rejoining the member in MySQL Shell.")
            return None
        if member.get("state_source") != "replication_group_members":
            return "This member is not in the group view.", "Connect to this member and Refresh to read its state."
        if base == "stop_member":
            if state not in ("ONLINE", "RECOVERING", "ERROR"):
                return ("Group Replication is already stopped or its state is unknown on this member.",
                        "Refresh on this member to read its state before stopping Group Replication.")
            return None
        if base == "start_member":
            if state == "ERROR":
                return ("Group Replication is still running on this member in ERROR.",
                        "Stop Group Replication on this member first.")
            if state != "OFFLINE":
                return "This member is not OFFLINE.", "Stop Group Replication on this member before starting it again."
            bootstrap = checks.get("bootstrap_group", (report.get("group_replication") or {}).get("bootstrap_group"))
            bootstrap = str(bootstrap or "").upper()
            if bootstrap in ("ON", "1"):
                return ("group_replication_bootstrap_group is ON, so starting would create a new group.",
                        "Set group_replication_bootstrap_group to OFF, then Refresh.")
            if bootstrap not in ("OFF", "0"):
                return ("The Group Replication bootstrap setting could not be read.",
                        "Read group_replication_bootstrap_group and confirm it is OFF, then Refresh.")
            return None
        if (report.get("cluster") or {}).get("topology") != "single_primary":
            return "Make primary needs single-primary mode.", "Switch the group to single-primary mode first."
        if member.get("role") == "PRIMARY":
            return "%s is already the primary." % name, "Choose an ONLINE secondary."
        if state != "ONLINE":
            return "%s is not ONLINE." % name, "Wait for recovery or rejoin this member, then Refresh."
        if member.get("role") != "SECONDARY":
            return "%s is not a secondary." % name, "Choose an ONLINE secondary."
        if not _UUID.fullmatch(member.get("uuid") or ""):
            return "This member has no valid Group Replication UUID.", "Refresh the cluster and verify this member's identity."
        if action["via"] == "mysqlsh" and not _ADDRESS.fullmatch(member.get("address") or ""):
            return "This member has no valid MySQL Shell address.", "Correct its address in the cluster metadata, then Refresh."
        return _group_issue(report)
    if checks.get("group_name") and not member.get("local") and base in ("desync_node", "resync_node"):
        # Peer-local wsrep_desync/state is not part of this node's report.
        # Catalog availability invites Review on the peer's own saved login.
        return None
    galera = report.get("galera") or {}
    metrics = galera.get("metrics") or {}
    if (galera.get("wsrep_on") or "").upper() != "ON":
        return "Galera replication is not enabled on this node.", "Enable Galera replication on this node, then Refresh."
    comment = (metrics.get("local_state_comment") or "").strip()
    desync = (galera.get("desync") or "").upper()
    if base == "desync_node":
        if desync == "ON":
            return "This node is already desynced.", "Resync this node before desyncing it again."
        if desync != "OFF" or comment != "Synced":
            return ("This node is not Synced with wsrep_desync OFF.",
                    "Wait for the node to become Synced and confirm wsrep_desync is OFF, then Refresh.")
    elif base == "resync_node":
        if desync != "ON":
            return ("wsrep_desync is not ON on this node.",
                    "Resync only a node desynced with wsrep_desync; let a state transfer or backup finish on its own.")
    else:
        if (report.get("cluster") or {}).get("component") != "non-Primary":
            return "This node is not in a non-Primary component.", "Bootstrap only after verifying this component has lost its Primary status."
        if metrics.get("connected") is not True:
            return "This node is not connected to its Galera component.", "Restore the node's Galera connection, then Refresh."
        if _joining(metrics):
            return "This node is joining the component.", "Wait for the state transfer to finish, then Refresh."
    return None


def _overview_issue(report, action):
    issue = _group_issue(report)
    if issue:
        return issue
    base, name = _action_name(action["id"])
    cs = report.get("clusterset") or {}
    if base in ("switch_to_multi_primary", "switch_to_single_primary"):
        if report.get("kind") == "innodb_cluster":
            # Shell forbids BOTH switches in any ClusterSet, including its
            # primary cluster (cluster_impl.cc:2838-2880; common/preconditions.cc).
            if cs:
                return ("Mode switches are not supported for a cluster in a ClusterSet.",
                        "Keep this ClusterSet cluster in single-primary mode.")
            if _missing(report) or any(m.get("state") != "ONLINE" for m in _group_members(report)):
                return "Every cluster member must be ONLINE for a mode switch.", "Rejoin offline or missing members and wait for recovery, then Refresh."
            if not any(m.get("state") == "ONLINE" and m.get("role") == "PRIMARY" for m in _group_members(report)):
                return "No ONLINE primary is visible in this cluster.", "Restore an ONLINE primary before changing the cluster mode."
        topology = (report.get("cluster") or {}).get("topology")
        desired = "multi_primary" if base == "switch_to_multi_primary" else "single_primary"
        if topology == desired:
            return ("The group is already in %s mode." % desired.replace("_", "-"),
                    "Choose the other mode to change the topology.")
        if topology not in ("single_primary", "multi_primary"):
            return "The group mode could not be read.", "Refresh from an ONLINE Group Replication member."
        # MySQL 8.4+/9.x rejects the multi-primary UDF while this is ON:
        # mysql-server/plugin/group_replication/src/udf/udf_multi_primary.cc:118-121.
        if base == "switch_to_multi_primary" and _on(
                (report.get("group_replication") or {}).get("preemptive_garbage_collection")):
            return ("group_replication_preemptive_garbage_collection is ON.",
                    "Set group_replication_preemptive_garbage_collection to OFF, then Refresh.")
        return None
    return None


def _action_privileges(report, action, checks):
    """Always evaluate privileges; only override eligibility when it passed.

    Shell's up-front validation requires affirmative scope/grant-option
    evidence, including for schema privileges and partially revoked grants.
    Other actions have only global requirements. Parsing and effective role /
    partial-revoke semantics remain entirely in privileges.py.
    """
    from . import privileges
    action.update(missing_privileges=[], grant_sql=None, privileges_unreadable=False)
    requirements, shell_runtime = _privilege_requirements(report, action)
    note = _SHELL_RUNTIME_PRIVILEGES if shell_runtime else None
    if not requirements:
        if note:
            action["why_not"] = " ".join(n for n in (action["why_not"], note) if n)
        return action
    model = _catalog_grants(report, checks)
    if model is None:
        action["privileges_unreadable"] = True
        note = " ".join(n for n in (_PRIVILEGES_UNREADABLE, note) if n)
    else:
        missing = []
        for alternatives, database, grantable in requirements:
            held = [privileges.has(model, p, level="database" if database else "global", database=database,
                                   grantable=grantable) for p in alternatives]
            if True not in held:
                missing.append((alternatives, database, grantable))
        action["missing_privileges"] = [[_privilege_label(p, database, grantable) for p in alternatives]
                                         for alternatives, database, grantable in missing]
        if missing:
            try:
                action["grant_sql"] = _grant_sql(missing, model["account"], model.get("grant_partial_revokes"))
            except ValueError:
                action["grant_sql"] = None
            if not action["grant_sql"]:
                account = model.get("account")
                unsafe_name = (not isinstance(account, str) or bool(privileges._UNSAFE_GRANT_SQL.search(account)) or
                               any(database is not None and (not isinstance(database, str) or
                                                             bool(privileges._UNSAFE_GRANT_SQL.search(database)))
                                   for _, database, _ in missing))
                fallback = (_GRANT_MODE_UNKNOWN
                            if not unsafe_name and model.get("grant_partial_revokes") is None
                            and any(database and any(c in database for c in "_%") for _, database, _ in missing)
                            else _GRANT_UNAVAILABLE)
            if action["enabled"]:
                action.update(enabled=False,
                              why_not="Needs " + "; and ".join(" or ".join(a) for a in action["missing_privileges"]) + ".",
                              fix=_GRANT_FIX if action["grant_sql"] else fallback)
            elif not action["grant_sql"]:
                note = " ".join(n for n in (note, fallback) if n)
    if note:
        action["why_not"] = " ".join(n for n in (action["why_not"], note) if n)
    return action


def _evaluate_action(report, aid, label, danger, description, scope, checks, member=None, cluster_name=None,
                     target_issue=None):
    """One eligibility result shared by the catalog and fresh-read planning.

    First failure wins: target, tool/version, readable report, then state.
    A Rejoin names a member but runs from the active member with quorum.
    Other Group member actions use that member's saved connection. The plan
    proves the route against a fresh target report.
    """
    local = _local_member(report)
    grouped = bool(checks.get("group_name"))
    member_route = scope == "member" and aid != "rejoin_instance"
    target = member if member_route and grouped or aid in _LOCAL_ACTIONS else local
    profile = _member_profile(report, target, checks) if grouped and target else None
    via = _action_via(report, aid)
    # All these SQL/AdminAPI changes have dbops' risky classification. danger
    # remains the existing presentation hint, independently of write policy.
    action = {"schema_version": ACTION_SCHEMA_VERSION, "id": aid, "label": label, "scope": scope,
              "kind": "risky", "via": via, "danger": danger, "description": description,
              "member_key": member.get("key") if member else None,
              "member_name": member.get("name") if member else None,
              "member_uuid": member.get("uuid") if member else None, "cluster_name": cluster_name,
              "enabled": True, "why_not": None, "fix": None,
              "target": {"self": bool(target and target.get("local")),
                         "member_name": target.get("name") if target else (report.get("server") or {}).get("hostname")}}
    issue = target_issue
    if grouped:
        action["target"]["profile_id"] = profile.get("id") if profile else None
        if profile and member_route and not _member_profiles(report, target, checks):
            from .topology_links import format_endpoint
            action["target"].update(confirm=True, note="Review checks that %s (%s) is this member." %
                                    (profile.get("name") or profile["id"],
                                     format_endpoint(profile.get("host") or "127.0.0.1", profile.get("port") or 3306)))
        if target and profile is None:
            issue = _member_route_issue(report, target, checks)
        if member_route and not member.get("local"):
            # The active member's grants and SSH transport say nothing about
            # another saved login. Read its grants only when planning there.
            checks = dict(checks, grants=None, ssh=bool((profile or {}).get("ssh_host")))
    if aid in _LOCAL_ACTIONS and not member.get("local") and not grouped:
        where = (local or {}).get("name") or (report.get("server") or {}).get("hostname") or "an unidentified server"
        operation, fix = _LOCAL_ACTIONS[aid]
        name = member.get("name") or "the selected member"
        issue = ("%s, %s; this connection is to %s." % (operation, name, where),
                 "Connect to %s to %s." % (name, fix))
    if issue is None:
        issue = _tool_issue(report, action, checks)
    if issue is None and not report.get("available"):
        issue = ("The cluster could not be read on this connection.", "Refresh with an account that can read the cluster state.")
    if issue is None and getattr(report, "_clusterset_discovery_error", False):
        issue = ("ClusterSet membership could not be verified on this server.",
                 "Read the InnoDB Cluster metadata before reviewing a cluster change.")
    if issue is None:
        issue = (_member_issue(report, action, member, checks) if scope == "member" else
                 _overview_issue(report, action))
    if issue:
        action.update(enabled=False, why_not=issue[0], fix=issue[1])
    elif grouped and member is not None and not member.get("local") and aid in ("desync_node", "resync_node"):
        action["why_not"] = "Review checks wsrep_desync and the state on this node's own connection."
    _action_privileges(report, action, checks)
    if grouped and target and profile is None:
        action["why_not"] = issue[0]
    return action


def _clusterset_actions(report, checks):
    """Complete set-level catalog; evaluating it never opens a connection.

    Live routes come from this report's read pass. Additional preflight
    evidence in checks is supplied by the backend, never action parameters.
    Missing evidence disables an action instead of treating a read failure
    as proof that a server is down or that a spare instance is standalone.
    """
    cs = report.get("clusterset") or {}
    clusters = [c for c in cs.get("clusters") or () if isinstance(c.get("name"), str) and c["name"]]
    replicas = [c for c in clusters if c.get("role") == "REPLICA" and not c.get("invalidated")]
    invalidated = [c for c in clusters if c.get("invalidated")]
    out = []
    families = (
        ("set_primary_cluster", "Make %s the primary cluster…", "Make primary cluster…", replicas,
         "Switches the primary cluster after synchronizing the selected replica."),
        ("force_primary_cluster", "Fail over to %s…", "Fail over to a replica cluster…", replicas,
         "Promotes a surviving replica only when every old-primary member is unreachable."),
        ("rejoin_cluster", "Rejoin %s to the ClusterSet…", "Rejoin cluster to the ClusterSet…", invalidated,
         "Rejoins an invalidated cluster after MySQL Shell checks its transactions."),
        ("repair_cluster", "Repair replication on %s…", "Repair cluster replication…", replicas,
         "Refreshes a replica's replication account and settings with rejoinCluster."),
        ("remove_cluster", "Remove and dissolve the cluster %s…", "Remove and dissolve the cluster…",
         replicas + invalidated, "Removes the replica cluster and dissolves it into standalone instances."),
        ("remove_cluster_keep", "Remove %s, keep the cluster running…", "Remove, keep the cluster running…",
         replicas + invalidated, "Removes the replica from this set and keeps a standalone InnoDB Cluster."),
    )
    for base, label, empty_label, targets, description in families:
        for cluster in targets or [None]:
            name = cluster["name"] if cluster else None
            out.append(_clusterset_entry(report, checks, base, name, label % name if name else empty_label,
                                         description, cluster=cluster))
    for router in [None] + list(cs.get("routers") or ()):
        name = router.get("name") if router else None
        # A Router without its full address::name identifier is still listed,
        # but cannot accidentally turn into a change to global defaults.
        key = name if name else ("#%s" % router.get("id") if router else None)
        entry = _clusterset_entry(report, checks, "set_routing_option", key,
                                  "Change routing for %s…" % (name or "this Router") if router else
                                  "Change ClusterSet routing defaults…",
                                  "Stores routing configuration that Routers apply on their next refresh.",
                                  router=router)
        entry["options"] = _clusterset_routing_options(checks, report)
        out.append(entry)
    candidates = {c.get("profile_id"): c for c in checks.get("replica_candidates") or ()}
    known = _clusterset_known_instances(report)
    metadata_instances = [i for instances in getattr(report, "_clusterset_instances", {}).values() for i in instances]
    metadata_instances += list((report.get("metadata") or {}).get("instances") or ())
    for cluster in cs.get("clusters") or ():
        metadata_instances.extend(cluster.get("instances") or ())
        metadata_instances.extend(cluster.get("members") or ())
    metadata_addresses = [_split_address(i.get(key) or "") for i in metadata_instances
                          for key in ("address", "endpoint")]

    def metadata_endpoint(profile):
        host = str(profile.get("host") or "").strip().strip("[]").lower()
        port = _number(profile.get("port")) or 3306
        return bool(host and any(h and h.strip().lower() == host and (p is None or p == port)
                                 for h, p in metadata_addresses))

    profiles = [p for p in checks.get("group_profiles") or () if checks.get("group_name") and p.get("id") and
                (p.get("group") == checks["group_name"] or p["id"] in candidates) and
                ((candidates.get(p["id"]) or {}).get("server_uuid") or
                 (p.get("topology_identity") or {}).get("server_uuid")) not in known and
                not metadata_endpoint(p)]
    for profile in profiles or [None]:
        pid = profile["id"] if profile else None
        candidate = candidates.get(pid)
        out.append(_clusterset_entry(report, checks, "create_replica_cluster", pid,
                                     "Create replica cluster on %s…" % (profile.get("name") or pid) if profile else
                                     "Create replica cluster…",
                                     "Creates a replica on a standalone server read in this Group connection.",
                                     candidate=candidate, profile=profile))
    out.append(_clusterset_entry(report, checks, "dissolve_clusterset", None, "Dissolve ClusterSet…",
                                 "Removes ClusterSet metadata and dissolves its clusters, keeping user data."))
    out.append(_clusterset_entry(report, checks, "check_clusterset", None,
                                 "Check with MySQL Shell (connects to every member)",
                                 "Reads the authoritative MySQL Shell status, including GTID consistency."))
    return out


def _clusterset_members(cluster):
    return (cluster or {}).get("members") or (cluster or {}).get("instances") or []


def _clusterset_known_instances(report):
    """Every instance in the shared metadata, including clusters outside the set."""
    known = {m.get("server_uuid") for members in getattr(report, "_clusterset_instances", {}).values()
             for m in members if m.get("server_uuid")}
    known.update(m.get("server_uuid") for c in (report.get("clusterset") or {}).get("clusters") or ()
                 for m in _clusterset_members(c) if m.get("server_uuid"))
    return known


def _clusterset_route(report, cluster):
    """Only a fresh UUID, never a topology label or a saved identity hint."""
    if not cluster or cluster.get("read") not in ("this_connection", "route"):
        return None
    route = dict(cluster.get("route") or {})
    if route.get("self"):
        own = (report.get("server") or {}).get("server_uuid")
        if route.get("server_uuid") not in (None, own):
            return None
        route["server_uuid"] = own
    uuid = route.get("server_uuid")
    if not isinstance(uuid, str) or not _UUID.fullmatch(uuid):
        return None
    if not any(m.get("server_uuid") == uuid for m in _clusterset_members(cluster)):
        return None
    return route


def _clusterset_unreachable(report, cluster, checks):
    """All metadata members were probed and none answered in this pass.

    A failed SQL read, absent group route or an authentication error does not
    establish this fact. The execution lane supplies classified per-instance
    connectivity observations; incomplete observations deliberately fail shut.
    mysql-shell/modules/adminapi/cluster_set/cluster_set_impl.cc:2875-2918 rejects
    even an OFFLINE or NO_QUORUM primary when any member remains reachable.
    """
    if not cluster or cluster.get("route"):
        return False
    members = _clusterset_members(cluster)
    uuids = {m.get("server_uuid") for m in members}
    if not uuids or None in uuids or len(uuids) != len(members):
        return False
    if (report.get("server") or {}).get("server_uuid") in uuids:
        return False  # This successful connection is itself contrary evidence.
    evidence = checks.get("clusterset_reachability", getattr(
        report, "_clusterset_reachability", None) or (report.get("clusterset") or {}).get("reachability", {}))
    observations = (evidence or {}).get(cluster.get("name")) or []
    return (len(observations) == len(uuids) and
            {o.get("server_uuid") for o in observations} == uuids and
            all(o.get("reachable") is False for o in observations))


def _clusterset_failover_preflight(report, profiles, open_profile):
    """Independently check saved routes for every old-primary instance.

    A cached UUID is only a reason to look for contrary evidence. A failed
    connection attempt proves an instance unreachable only at its uniquely
    attributable, explicit metadata endpoint. Ambiguity, SSH, authentication failures,
    timeouts and missing profiles remain unknown. Never construct a route from
    metadata: the group-scoped opener supplies the only permitted connections.
    """
    primary = _clusterset_primary(report)
    if not primary:
        return {}
    evidence = {primary["name"]: []}
    scope = getattr(open_profile, "group_scope", None)
    current = getattr(scope, "current", None) or {}
    group = current.get("group")
    if not open_profile or scope is None or not group:
        return evidence
    required = _clusterset_members(primary)
    all_instances = [i for entries in getattr(report, "_clusterset_instances", {}).values() for i in entries]
    if not required or not all_instances:
        return evidence
    uuids = [m.get("server_uuid") for m in required]
    if any(not isinstance(u, str) or not _UUID.fullmatch(u) for u in uuids):
        return evidence
    normalized = [u.lower() for u in uuids]
    if len(set(normalized)) != len(normalized):
        return evidence
    counts = {}
    for inst in all_instances:
        value = inst.get("server_uuid")
        if isinstance(value, str):
            counts[value.lower()] = counts.get(value.lower(), 0) + 1
    states = {u: {"reachable": False, "unconfirmed": counts.get(u) != 1, "exact_failure": False}
              for u in normalized}
    by_uuid = dict(zip(normalized, uuids))
    # A success or an ambiguous observation in the immediately preceding
    # pass remains contrary evidence if a route changes before preflight.
    for item in getattr(report, "_clusterset_reachability", {}).get(primary["name"], ()):
        value = item.get("server_uuid")
        key = value.lower() if isinstance(value, str) else None
        if key in states and item.get("reachable") is True:
            states[key]["reachable"] = True
        elif key in states and item.get("reachable") is None:
            states[key]["unconfirmed"] = True
    fresh_hints = {}
    for read in getattr(scope, "read", ()):
        value = getattr(read, "identity", {}).get("server_uuid")
        if read.get("outcome") == "read" and isinstance(value, str) and value.lower() in states:
            fresh_hints[read["id"]] = value.lower()
            states[value.lower()]["reachable"] = True
    allowed = {p.get("id") for p in getattr(scope, "profiles", ())}
    candidates = []
    for profile in profiles or ():
        if (not isinstance(profile, dict) or profile.get("id") not in allowed or
                profile.get("group") != group or profile.get("id") == current.get("id")):
            continue
        host = str(profile.get("host") or "").strip().strip("[]").lower()
        raw_port = profile.get("port")
        port = 3306 if raw_port in (None, "") else _number(raw_port)
        endpoint = (host, port) if host and port is not None and 1 <= port <= 65535 else None
        matches = [i for i in all_instances if endpoint and endpoint in {
            _clusterset_explicit_endpoint(i.get(key) or "") for key in ("address", "endpoint")}]
        ambiguous_host = False
        if endpoint and len(matches) == 1:
            for inst in all_instances:
                if inst is matches[0]:
                    continue
                for key in ("address", "endpoint"):
                    other_host, other_port = _split_address(inst.get(key) or "")
                    if other_host and other_host.strip().strip("[]").lower() == host and other_port is None:
                        ambiguous_host = True
        exact_uuid = (matches[0].get("server_uuid") if len(matches) == 1 and not ambiguous_host else None)
        exact_uuid = exact_uuid.lower() if isinstance(exact_uuid, str) else None
        if exact_uuid not in states or counts.get(exact_uuid) != 1:
            exact_uuid = None
        hinted = (profile.get("topology_identity") or {}).get("server_uuid")
        hinted = hinted.lower() if isinstance(hinted, str) and hinted.lower() in states else None
        hints = {u for u in (hinted, fresh_hints.get(profile.get("id"))) if u}
        associated = hints | ({exact_uuid} if exact_uuid else set())
        if associated:
            candidates.append((profile, associated, exact_uuid, hints))
    deadline = time.monotonic() + CLUSTERSET_FAILOVER_TIMEOUT
    opened = set()
    for profile, associated, exact_uuid, hints in candidates:
        pid = profile["id"]
        if pid in opened:
            continue
        if profile.get("ssh_host"):
            # Tunnel establishment is outside the mysql query deadline, and
            # a tunnel failure cannot prove the remote member is down.
            for uuid in associated:
                states[uuid]["unconfirmed"] = True
            continue
        if len(opened) >= CLUSTERSET_FAILOVER_MAX_OPENS or time.monotonic() >= deadline:
            for uuid in associated:
                states[uuid]["unconfirmed"] = True
            continue
        opened.add(pid)
        if len(hints) > 1 or (exact_uuid and hints and hints != {exact_uuid}):
            # A stale identity conflicts with the endpoint; even a failed
            # login cannot prove which of the two instances was contacted.
            for uuid in associated:
                states[uuid]["unconfirmed"] = True
        try:
            with open_profile(profile) as conn:
                if time.monotonic() >= deadline:
                    for uuid in associated:
                        states[uuid]["unconfirmed"] = True
                    continue
                per_probe = min(deadline, time.monotonic() + CLUSTERSET_FAILOVER_PROBE_TIMEOUT)
                reader = _ClusterSetReader(conn, per_probe)
                rows = reader.rows("SELECT @@server_uuid")
                actual = rows[0][0] if len(rows) == 1 and len(rows[0]) == 1 else None
                actual = actual.lower() if isinstance(actual, str) else None
                for uuid in associated:
                    # A different UUID, including one returned by a load
                    # balancer, cannot establish that the member is down.
                    states[uuid]["reachable" if actual == uuid else "unconfirmed"] = True
                if actual in states:
                    states[actual]["reachable"] = True
        except (ConnectorError, ValueError, OSError) as exc:
            failed = _clusterset_initial_connection_failure(exc)
            for uuid in associated:
                if (failed and uuid == exact_uuid and not profile.get("ssh_host") and
                        (not hints or hints == {exact_uuid})):
                    states[uuid]["exact_failure"] = True
                elif not (failed and uuid in hints and len(hints) == 1 and not profile.get("ssh_host") and
                          (exact_uuid is None or exact_uuid == uuid)):
                    states[uuid]["unconfirmed"] = True
    for uuid in normalized:
        state = states[uuid]
        if state["reachable"]:
            evidence[primary["name"]].append({"server_uuid": by_uuid[uuid], "reachable": True})
        elif state["exact_failure"] and not state["unconfirmed"]:
            evidence[primary["name"]].append({"server_uuid": by_uuid[uuid], "reachable": False})
    return evidence


def _clusterset_failover_evidence(report, profiles, open_profile):
    """Replace primary reachability while retaining other clusters' reads."""
    evidence = dict(getattr(report, "_clusterset_reachability", None) or {})
    primary = _clusterset_primary(report)
    if primary:
        evidence.pop(primary["name"], None)
    evidence.update(_clusterset_failover_preflight(report, profiles, open_profile))
    return evidence


def _clusterset_ready(report, cluster):
    return bool(_clusterset_route(report, cluster) and cluster.get("quorum") is True and
                (cluster.get("primary") or {}).get("state") == "ONLINE")


def _clusterset_primary(report):
    return next((c for c in (report.get("clusterset") or {}).get("clusters") or ()
                 if c.get("role") == "PRIMARY" and not c.get("invalidated")), None)


def _shell_connection_issue(profile, needs_password=False):
    """Check the connection Shell will use, without opening it or reading secrets."""
    if profile.get("ssh_host"):
        return _SHELL_SSH_NOTE, "Use a direct saved connection that MySQL Shell can reach."
    mode = (profile.get("ssl_mode") or "PREFERRED").upper()
    # Some AdminAPI member sessions copy CA settings without ssl_mode:
    # mysql-shell connection_options.cc:152-184; cluster_impl.cc:654-670;
    # mysql/session.cc:470-486. Do not silently weaken the reviewed TLS mode.
    if mode == "VERIFY_IDENTITY":
        return ("MySQL Shell actions cannot preserve TLS mode VERIFY_IDENTITY on connections to other members.",
                "Use a saved connection with VERIFY_CA, or run the action in MySQL Shell.")
    if mode == "REQUIRED":
        return ("MySQL Shell actions cannot preserve TLS mode REQUIRED on connections to other members.",
                "Use a saved connection with VERIFY_CA, or run the action in MySQL Shell.")
    if needs_password and not profile.get("remember_password"):
        return ("The saved connection %s has no saved password." % (profile.get("name") or profile.get("id")),
                "Save its password before reviewing a cluster action.")
    return None


def _clusterset_route_issue(report, route, checks):
    """Use the selected route's transport and credentials, not the active peer's."""
    current = checks.get("current_profile")
    current = current if isinstance(current, dict) else {}
    profiles = checks.get("group_profiles") or (checks.get("read_options") or {}).get("profiles") or ()
    profile = next((p for p in profiles if p.get("id") == route.get("profile_id")), None)
    if not route or route.get("self"):
        options = dict(current or profile or {})
        conn = getattr(report, "_action_conn", None)
        if getattr(conn, "ssl_mode", None):
            options["ssl_mode"] = conn.ssl_mode
        # Fresh planning knows the actual session options, which take
        # precedence over the saved profile (a request password is allowed).
        if "ssh" in checks:
            options["ssh_host"] = "SSH" if checks["ssh"] else None
        if "ssl_mode" in checks:
            options["ssl_mode"] = checks["ssl_mode"]
        return _shell_connection_issue(options)
    if checks.get("group_name"):
        if not profile or profile.get("group") != checks["group_name"]:
            return ("The selected ClusterSet route has no saved connection in the active group.",
                    "Add a direct saved connection for this route to the active group, then Refresh.")
    # Report-only callers may have no saved-connection context. Actual
    # grouped catalogs and planning always supply the store's profile list.
    if profile:
        return _shell_connection_issue(profile, needs_password=True)
    return _shell_connection_issue({"ssh_host": "SSH" if checks.get("ssh") else None,
                                    "ssl_mode": checks.get("ssl_mode")})


def _clusterset_candidate_issue(report, checks, candidate, profile):
    if not checks.get("group_name") or not profile:
        return ("No standalone saved connection in the active group is available.",
                "Add a standalone MySQL server to this Group connection and read it before review.")
    if profile.get("read_only") or (candidate or {}).get("read_only"):
        return ("This standalone saved connection is Read-only in myflames.",
                "Turn off Read-only for this saved connection or choose another standalone server.")
    issue = _shell_connection_issue(profile, needs_password=True)
    if issue:
        return issue
    # Catalog reads do not open spare servers. A known saved identity outside
    # the metadata is enough to offer a review; preparation reads it afresh.
    if candidate is None:
        uuid = (profile.get("topology_identity") or {}).get("server_uuid")
        known = _clusterset_known_instances(report)
        if isinstance(uuid, str) and _UUID.fullmatch(uuid) and uuid not in known:
            return None
    if (not candidate or candidate.get("read") not in ("route", "this_connection") or
            candidate.get("read_error")):
        return ("This saved connection was not read as a standalone server in this pass.",
                "Read this saved connection in the active group before reviewing creation.")
    route = candidate.get("route") or {}
    uuid = candidate.get("server_uuid")
    if (not isinstance(uuid, str) or not _UUID.fullmatch(uuid) or route.get("server_uuid") != uuid or
            route.get("profile_id") != profile["id"]):
        return ("The standalone server's route identity is missing or inconsistent.",
                "Refresh this saved connection to confirm its server UUID.")
    known = _clusterset_known_instances(report)
    if candidate.get("kind") != "none" or candidate.get("group_name") != "" or uuid in known:
        return "This server is already in a cluster or its membership was not read.", "Choose a verified standalone server."
    if candidate.get("engine") != "mysql" or _version_of(candidate) < SHELL_CLUSTERSET_MIN_VERSION:
        return "The standalone server needs MySQL 8.0.27 or later.", "Upgrade or choose another standalone server."
    from .clusterset_execution import candidate_binding
    binding = candidate_binding(profile)
    address = join_address(binding["endpoint"]["host"], binding["endpoint"]["port"] or 3306)
    if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
        return ("The standalone saved connection has no valid direct MySQL address.",
                "Save its direct host and port before reviewing creation.")
    if (candidate.get("address") != address or candidate.get("reported_address") != address or
            any(candidate.get(key) != value for key, value in binding.items())):
        return ("The standalone server's reported address or saved connection changed since its preflight.",
                "Make report_host and report_port match the saved direct endpoint, then read and review again.")
    if candidate.get("gtid_subset") is not True:
        return ("The standalone server's GTIDs are not verified as a subset of the primary cluster's.",
                "Compare its executed GTIDs with the current primary before reviewing creation.")
    if not isinstance(candidate.get("super_read_only"), bool):
        return "The standalone server's super_read_only state was not read.", "Read its fencing state before reviewing creation."
    if candidate.get("configuration_error"):
        return candidate["configuration_error"], "Configure this instance for InnoDB Cluster in MySQL Shell, then review again."
    return None


def _clusterset_issue(report, checks, base, cluster=None, router=None, candidate=None, profile=None):
    cs = report.get("clusterset") or {}
    if not cs:
        return ("This cluster is not part of an InnoDB ClusterSet.",
                "Create or join a ClusterSet in MySQL Shell, then Refresh.")
    if not report.get("available") or not cs.get("name") or cs.get("view_id") is None:
        return "The ClusterSet name and metadata view could not be read.", "Refresh with an account that can read ClusterSet metadata."
    if _version_of(report) < SHELL_CLUSTERSET_MIN_VERSION:
        return "ClusterSet actions need MySQL 8.0.27 or later.", "Use a MySQL 8.0.27 or later ClusterSet member."
    if _version((report.get("metadata") or {}).get("schema_version")) < CLUSTERSET_MIN_METADATA:
        return "ClusterSet actions need metadata 2.1.0 or later.", "Upgrade the ClusterSet metadata in MySQL Shell, then Refresh."
    primary = _clusterset_primary(report)
    if base == "check_clusterset":
        if not primary or cs.get("primary_cluster") != primary.get("name"):
            return ("No primary cluster is recorded in this ClusterSet view.",
                    "Refresh from a member with complete ClusterSet metadata before checking with MySQL Shell.")
        return None
    # mysql-shell modules/adminapi/cluster_set/cluster_set_impl.cc:833-837,
    # 868-871,2397-2402,3006-3012 exclude an INVALIDATED calling cluster;
    # :3324-3330 permits it for rejoin/refresh, :2099-2103 for dissolve.
    if cs.get("invalidated") and base not in ("rejoin_cluster", "repair_cluster", "dissolve_clusterset"):
        return "This connection's cluster is invalidated in its ClusterSet.", "Use a non-invalidated ClusterSet member for this change."
    if base == "force_primary_cluster":
        # The catalog offers an on-demand check, not proof that failover is
        # safe. Only review and send may consume the complete preflight.
        catalog_check = checks.get("clusterset_catalog") and checks.get("group_name")
        evidence = checks.get("clusterset_reachability", getattr(
            report, "_clusterset_reachability", None) or cs.get("reachability", {}))
        reachable = bool(primary and (
            primary.get("route") or
            (report.get("server") or {}).get("server_uuid") in {
                member.get("server_uuid") for member in _clusterset_members(primary)} or
            any(item.get("reachable") is True for item in (evidence or {}).get(primary["name"], ()))))
        if not primary or reachable or (not catalog_check and not _clusterset_unreachable(report, primary, checks)):
            return ("Every member of the primary cluster must be confirmed unreachable before failover.",
                    "Check every primary-cluster member through this group; use switchover if one is reachable.")
    elif not _clusterset_ready(report, primary):
        return ("The primary cluster was not read with quorum and an ONLINE primary.",
                "Read an available primary-cluster member through this Group connection, then Refresh.")
    elif base != "dissolve_clusterset" and primary.get("fenced_writes") is not False:
        # The read model verifies both SRO and the member action, matching
        # Shell (modules/adminapi/cluster/cluster_impl.cc:2043-2067). Missing
        # evidence must not pass as a confirmation that writes are unfenced.
        if primary.get("fenced_writes") is True:
            return "The primary reports that writes are fenced.", "Resolve the primary cluster's write fencing before this change."
        return ("Cannot confirm whether the primary cluster's writes are fenced.",
                "Refresh from its ONLINE primary with an account that can read the fencing state.")
    elif base != "dissolve_clusterset" and primary.get("super_read_only") is True:
        return ("The primary cluster is not writable: its primary reports super_read_only ON.",
                "Resolve the primary cluster's blocked writes, then Refresh before this change.")
    if base in ("set_primary_cluster", "force_primary_cluster", "set_routing_option") and _clusterset_routing_configuration(cs) is None:
        return ("Global and per-Router routing options could not all be read.",
                "Read global and per-Router routing options before reviewing this change.")
    if base in ("remove_cluster", "remove_cluster_keep") and _clusterset_routing_configuration(cs) is None:
        return ("The routing targets that removal would affect could not all be read.",
                "Read global and per-Router routing options before reviewing removal.")
    if base in ("set_primary_cluster", "force_primary_cluster", "rejoin_cluster", "repair_cluster",
                "remove_cluster", "remove_cluster_keep"):
        if cluster is None:
            return "No eligible target cluster is recorded in this ClusterSet.", "Refresh after adding or rejoining an eligible replica cluster."
        if base in ("remove_cluster", "remove_cluster_keep") and _clusterset_unreachable(report, cluster, checks):
            if base == "remove_cluster_keep":
                # Shell's dissolve:false branch commits metadata removal,
                # then dereferences an unavailable target's null session:
                # cluster_set_impl.cc:1579-1597; async_topology.cc:126-128.
                return ("An unreachable cluster cannot be kept running after removal.",
                        "Restore its quorum and ONLINE primary, or review Remove and dissolve instead.")
            return None  # Planning requires explicit force:true, bound in the review.
        if not _clusterset_ready(report, cluster):
            fix = ("Restore quorum and an ONLINE primary on %s, then Refresh." % cluster["name"]
                   if _clusterset_route(report, cluster) else
                   "Add a saved connection for a %s member to this group and Refresh." % cluster["name"])
            return "Cluster %s was not read with quorum and an ONLINE primary." % cluster["name"], fix
        if base == "set_primary_cluster" and cluster.get("status") != "OK":
            return ("Cluster %s was not read as OK." % cluster["name"],
                    "Restore its replication and quorum, then Refresh before switching over.")
        channel = cluster.get("channel") or {}
        channel_ok = (channel.get("present") is True and channel.get("receiver_state") == "ON" and
                      channel.get("applier_state") == "ON" and not channel.get("read_error") and
                      not channel.get("last_error_number"))
        if base in ("remove_cluster", "remove_cluster_keep") and not channel_ok:
            # mysql-shell/modules/adminapi/cluster_set/cluster_set_impl.cc:983-1019 requires
            # a healthy channel; this app only offers force for unreachable targets.
            return ("The target replica was not read with healthy ClusterSet replication.",
                    "Repair its replication before removing the reachable cluster.")
        if base == "repair_cluster":
            channel = cluster.get("channel") or {}
            broken = (cluster.get("status") in ("NOT_REPLICATING", "OK_NOT_REPLICATING", "OK_MISCONFIGURED") or
                      channel.get("present") is False or channel.get("last_error_number") or
                      channel.get("status") in ("STOPPED", "ERROR", "MISSING", "MISCONFIGURED", "CONNECTING") or
                      channel.get("receiver_state") in ("OFF", "ERROR", "CONNECTING") or
                      channel.get("applier_state") in ("OFF", "ERROR"))
            if not broken:
                return ("This replica has no observed replication problem to repair.",
                        "Read its primary's replication channel and review repair only when it is not OK.")
    if base == "set_routing_option":
        if (cs.get("routing_options") or {}).get("options") is None or cs.get("routers") is None:
            return "Stored Router configuration could not be read.", "Read ClusterSet and Router metadata, then Refresh."
        if router is not None and not router.get("name"):
            return "This Router has no address::name identifier.", "Correct the Router registration in MySQL Shell, then Refresh."
        if router is not None and router.get("options") is None:
            return "This Router's stored options could not be read.", "Read this Router's stored options before reviewing routing changes."
    if base == "create_replica_cluster":
        return _clusterset_candidate_issue(report, checks, candidate, profile)
    return None


def _clusterset_entry(report, checks, base, name, label, description, cluster=None, router=None,
                       candidate=None, profile=None):
    aid = base + (":" + name if name is not None else "")
    local_cluster = next((c for c in (report.get("clusterset") or {}).get("clusters") or () if c.get("local")), None)
    primary_cluster = _clusterset_primary(report)
    if base == "force_primary_cluster":
        route = _clusterset_route(report, cluster) or _clusterset_route(report, local_cluster) or {}
    else:
        # AdminAPI ClusterSet operations (other than failover) require the
        # primary cluster and its authoritative view_id; running Shell on a
        # replica or invalidated cluster can hit stale local metadata.
        route = (_clusterset_route(report, primary_cluster) or _clusterset_route(report, local_cluster) or
                 _clusterset_route(report, cluster) or {})
    action = {"schema_version": ACTION_SCHEMA_VERSION, "id": aid, "label": label, "scope": "clusterset",
              "kind": "read" if base == "check_clusterset" else "risky", "via": "mysqlsh",
              "danger": base not in ("check_clusterset", "set_routing_option"), "description": description,
              "member_key": None, "member_name": None, "member_uuid": None,
              "cluster_name": cluster.get("name") if cluster else None,
              "enabled": True, "why_not": None, "fix": None,
              "target": {"self": bool(route.get("self")), "member_name": route.get("profile_name"),
                         "profile_id": route.get("profile_id"), "server_uuid": route.get("server_uuid")}}
    # _tool_issue normally checks the active connection's SSH transport.
    # ClusterSet routes can select a different saved connection entirely.
    issue = (_clusterset_route_issue(report, route, checks) or
             _tool_issue(report, action, dict(checks, ssh=False)) or
             _clusterset_issue(report, checks, base, cluster, router, candidate, profile))
    if issue:
        action.update(enabled=False, why_not=issue[0], fix=issue[1].replace(" Get MySQL Shell.", ""))
    _action_privileges(report, action, checks)
    if issue:
        # Keep the click message one reason and one fix; runtime privilege
        # guidance belongs in an enabled review, not appended to a refusal.
        action["why_not"] = issue[0]
    elif base == "force_primary_cluster" and checks.get("clusterset_catalog"):
        action["label"] = "Fail over to %s: check reachability, then review…" % (name or "a replica cluster")
        action["description"] = ("Check reachability, then review. Every old-primary member is checked through "
                                 "saved connections in this group before failover can be reviewed.")
    return action


def _clusterset_routing_options(checks, report=None):
    version = tuple(((checks or {}).get("shell") or {}).get("version_tuple") or ())
    out = {}
    for name, minimum in CLUSTERSET_ROUTING_MINIMUM.items():
        needed = max(SHELL_MIN_VERSION, minimum)
        label = ".".join(str(n) for n in needed)
        enabled = version >= needed
        out[name] = {"minimum_shell": list(minimum), "enabled": enabled,
                     "why_not": None if enabled else "%s needs MySQL Shell %s or later." % (name, label),
                     "fix": None if enabled else "Install MySQL Shell %s or later, then Refresh." % label}
    if report is not None and out["guideline"]["enabled"] and _version(
            (report.get("metadata") or {}).get("schema_version")) < (2, 3, 0):
        out["guideline"].update(enabled=False, why_not="Routing guidelines need metadata 2.3.0 or later.",
                                fix="Upgrade the ClusterSet metadata in MySQL Shell, then Refresh.")
    return out


def available_actions(report, member_key=None, checks=None):
    """Every member-scope action for this kind, including disabled actions."""
    if not report:
        return []
    member = next((m for m in _members(report) if m.get("key") == member_key), None)
    if member is None:
        return []
    kind = report.get("kind")
    definitions = []
    if kind in ("innodb_cluster", "group_replication"):
        definitions = [
            ("switchover", "Make primary…", True, "Makes this member the primary; the current primary becomes a read-only secondary."),
            ("stop_member", "Stop Group Replication…", True, "This member leaves the group and becomes read-only (super_read_only ON)."),
            ("start_member", "Start Group Replication…", False, "Joins this member to the group. Recovery may replace its data with a clone."),
        ]
        if kind == "innodb_cluster":
            definitions.append(("rejoin_instance", "Rejoin to the cluster…", True,
                                "MySQL Shell rejoins this member with incremental recovery from a donor (Cluster.rejoinInstance)."))
    elif kind == "galera" and report.get("engine") == "mariadb":
        definitions = [
            ("desync_node", "Desync this node…", False, "Takes this node out of flow control (wsrep_desync = ON), for example for a backup."),
            ("resync_node", "Resync this node…", False, "Puts this node back in flow control (wsrep_desync = OFF)."),
            ("bootstrap_pc", "Bootstrap a Primary component…", True, "Makes this node, and the nodes it reaches, a Primary component that accepts writes."),
        ]
    return [_evaluate_action(report, aid, label, danger, description, "member", checks or {}, member=member)
            for aid, label, danger, description in definitions]


def overview_actions(report, checks=None):
    """Every overview action, evaluated against the connected member."""
    if not report or report.get("kind") not in ("innodb_cluster", "group_replication"):
        return []
    checks = checks or {}
    ic = report.get("kind") == "innodb_cluster"
    out = [
        _evaluate_action(report, "switch_to_single_primary", "Switch to single-primary…", True,
                         "The group elects one primary; the other members become read-only." +
                         (" MySQL Shell updates auto_increment and the cluster metadata." if ic else ""),
                         "overview", checks),
        _evaluate_action(report, "switch_to_multi_primary", "Switch to multi-primary…", True,
                         "Every member accepts writes, and the mode is persisted." +
                         (" MySQL Shell updates auto_increment and the cluster metadata." if ic else ""),
                         "overview", checks),
    ]
    if ic:
        out.extend(_clusterset_actions(report, checks))
    return out


def with_actions(report, shell=None, ssh=False, group_profiles=(), current_profile=None, group_name=None):
    """Attach the workspace catalog once per scope; CLI and MCP stay read-only."""
    if shell is None and report.get("kind") == "innodb_cluster":
        from .cluster_shell import find_shell
        shell = find_shell()
    checks = {"shell": shell, "ssh": ssh, "group_profiles": group_profiles, "clusterset_catalog": True,
              "current_profile": current_profile, "group_name": group_name}
    # Catalog eligibility uses only the bounded read's existing observations.
    # A failover review and its pre-send check each repeat the full preflight.
    if shell and report.get("kind") == "innodb_cluster" and ssh:
        notes = report.setdefault("notes", [])
        if _SHELL_SSH_NOTE not in notes:
            notes.append(_SHELL_SSH_NOTE)
    for member in report.get("members") or ():
        member["actions"] = available_actions(report, member.get("key"), checks)
    report["actions"] = overview_actions(report, checks)
    return report


@grouped_read
def attach_topology_tiers(conn, report, profiles=(), open_profile=None, reached_host=None, current_profile_id=None):
    """Attach verified upstream Readyset and ProxySQL tiers from saved connections."""
    if not report or not report.get("available"):
        return report
    from .replication import reached_host as _reached_host
    from . import topology_links
    from .topology_links import discover_database_tiers
    prior = getattr(report, "_clusterset_scope", None)
    scope = getattr(open_profile, "group_scope", None)
    if prior is not None and scope is not None and prior is not scope:
        # grouped_read creates a new scope for this call. Preserve the read
        # outcomes and private identity evidence from the preceding pass.
        for read in prior.read:
            scope.record(read.profile, read.get("outcome"), read.get("message", ""), read.identity)
        scope.attempted.update(prior.attempted)
    if report.get("clusterset"):
        # Unclassified profiles can be standalone spares. Ordinary ClusterSet
        # reads must not open them just to discover upstream tiers. Reuse a
        # completed upstream cache entry only when its current settings still
        # have the same fingerprint; the wrapper cannot open on a cache miss.
        attempted = getattr(report, "_clusterset_attempted", set())
        profiles = [p for p in profiles if p.get("id") not in attempted and
                    topology_links._cached_probe_kind(p.get("id")) in ("readyset", "proxysql")]
        original_opener = open_profile
        if original_opener is not None and profiles:
            from .replication import _settings_fingerprint

            def never_open(profile):
                raise ValueError("This read uses only previously observed upstream tiers.")

            def cached_prepare(profile):
                prepare = getattr(original_opener, "prepare", None)
                if prepare is not None:
                    fingerprint, _ = prepare(profile)
                else:
                    check = getattr(original_opener, "check", None)
                    if check is not None:
                        check(profile)
                    fingerprint = _settings_fingerprint(profile)
                # _claim_probe calls prepare under the topology cache lock.
                entry = topology_links._probe_cache.get((profile.get("id"), fingerprint))
                if (not entry or not entry["done"].is_set() or not entry.get("data") or
                        entry["data"].get("kind") not in ("readyset", "proxysql")):
                    raise ValueError("This upstream tier has not been read with the current connection settings.")
                return fingerprint, lambda: never_open(profile)

            never_open.prepare = cached_prepare
            never_open.group_scope = scope
            never_open.current_profile = getattr(original_opener, "current_profile", None)
            open_profile = never_open
    else:
        attempted = getattr(report, "_clusterset_attempted", set())
        profiles = [p for p in profiles if p.get("id") not in attempted]
    srv = report.get("server") or {}
    obs_host = reached_host if reached_host is not None else _reached_host(conn)
    ssh_host = getattr(conn, "ssh_host", None)
    ssh_host = ssh_host.strip() if isinstance(ssh_host, str) and ssh_host.strip() else None
    if ssh_host is None and isinstance(obs_host, str) and obs_host.strip().lower().startswith("ssh:"):
        ssh_host = obs_host.strip()[4:].strip() or None
    local_ident = {
        "hostname": srv.get("hostname"),
        "port": srv.get("port") or getattr(conn, "port", None) or 3306,
        "report_host": srv.get("report_host"),
        "server_uuid": srv.get("server_uuid"),
        "profile_host": obs_host,
        "profile_port": getattr(conn, "port", None) or srv.get("port") or 3306,
        "ssh_host": ssh_host,
    }
    peers = [
        {"host": m.get("host"), "port": m.get("port") or 3306, "server_uuid": m.get("uuid")}
        for m in (report.get("members") or ())
        if m.get("host") or m.get("uuid")
    ]
    report["topology_tiers"] = discover_database_tiers(
        conn, local_ident, peer_endpoints=peers, profiles=profiles, open_profile=open_profile,
        observer_host=obs_host, current_profile_id=current_profile_id, check_footprint=True,
        performance_schema=report.get("performance_schema"))
    return report


def action_checks(conn, report, action, check_grants=True):
    """Check the bootstrap flag and MySQL Shell, plus fresh grants for planning.

    Sending skips grants: the same-session identity guard and the server's
    privilege checks enforce the reviewed write. All other checks still run.
    """
    from . import dbops
    requirements, _ = _privilege_requirements(report, {"id": action, "via": _action_via(report, action)})
    # An explicit unknown prevents sibling catalog entries from triggering a
    # lazy grants read during send or when Shell validates the privileges.
    out = {"grants": None}
    if check_grants and requirements:
        out["grants"] = _grant_model(report, dbops.read_grants(conn, CLUSTER_TAG))
    if (report or {}).get("kind") == "innodb_cluster" and _via_shell(report, action):
        from .cluster_shell import find_shell
        out["shell"] = find_shell()
        out["ssh"] = bool(getattr(conn, "ssh_host", None))
        if getattr(conn, "ssl_mode", None):
            out["ssl_mode"] = conn.ssl_mode
    if action == "start_member":
        reader = Reader(conn, CLUSTER_TAG)
        try:
            rows = reader.rows("SELECT @@global.group_replication_bootstrap_group")
            out["bootstrap_group"] = _text(rows[0][0]) if rows and rows[0] else None
        except ConnectorError:
            out["bootstrap_group"] = None
    return out


def prepare_action(conn, action, group_checks=None, check_grants=True):
    """Fresh action evidence, including consent-scoped ClusterSet routes.

    Standalone preflight is deliberately a plan/apply operation, never part
    of an ordinary report or catalog read.
    """
    group = group_checks() if group_checks is not None else {}
    base = _action_name(action)[0] if isinstance(action, str) else None
    read_options = group.get("read_options") or {}
    report = read_status(conn, **read_options) if base in CLUSTERSET_ACTIONS else read_status(conn)
    checks = action_checks(conn, report, action, check_grants=check_grants)
    checks.update(group)
    if base == "force_primary_cluster":
        checks["clusterset_reachability"] = _clusterset_failover_evidence(
            report, read_options.get("profiles") or (), read_options.get("open_profile"))
    if base == "create_replica_cluster":
        # Apply the same route, Shell/version and spare transport gates as
        # the catalog before preflight can open the target or run mysqlsh.
        selected = next((entry for entry in _clusterset_actions(report, checks) if entry["id"] == action), None)
        if selected is None:
            raise ValueError("That change is not offered for this member now. Refresh and try again.")
        if not selected["enabled"]:
            raise ActionUnavailable(" ".join(text for text in
                                            (selected["why_not"], selected["fix"], selected["grant_sql"]) if text))
        from .clusterset_execution import read_candidate
        candidate = read_candidate(conn, report, action, checks)
        checks["replica_candidates"] = [candidate]
    return report, checks


def _server_identity(report):
    server = dict(report.get("server") or {})
    server.update(getattr(report, "_galera_identity", {}))
    if report.get("kind") in ("galera", "pxc"):
        server["wsrep_gcomm_uuid"] = (report.get("galera") or {}).get("gcomm_uuid")
    return {"engine": report.get("engine"), "server_version": report.get("server_version"),
            "server": server, "kind": report.get("kind")}


def _connected_text(report, local):
    server = report.get("server") or {}
    where = join_address(server.get("hostname"), server.get("port")) if server.get("hostname") else None
    return "%s%s" % (local.get("name") if local else "This server", " (%s)" % where if where and local and
                     where != local.get("name") else "")


def target_server_identity(report):
    """The node identity persisted as route evidence, without topology labels."""
    if report.get("kind") in ("galera", "pxc"):
        return {"wsrep_node_uuid": (report.get("galera") or {}).get("gcomm_uuid")}
    return {"server_uuid": (report.get("server") or {}).get("server_uuid")}


def _check_member_target(report, member, target):
    """A Group member action must be local by identity, never by host:port."""
    own = next(iter(target_server_identity(report).values()), None)
    if (not member or not member.get("local") or not isinstance(own, str) or not _UUID.fullmatch(own)
            or own != member.get("uuid")):
        from .topology_links import format_endpoint
        endpoint = target["endpoint"]
        server = "%s (%s)" % (target["profile_name"], format_endpoint(endpoint["host"], endpoint["port"]))
        raise ValueError("%s does not reach %s: it reports a different server." %
                         (server, (member or {}).get("name") or "the selected member"))


def plan_action(report, action, member_key=None, checks=None, target=None, parameters=None):
    """The reviewed plan for *action* on *member_key* from *report* (a fresh
    read) and *checks* (:func:`action_checks`). Raises ValueError when the action is
    disabled now, with exactly the catalog's reason and fix. Overview actions
    use the connected member; their member_key may be omitted."""
    if not report or report.get("kind") in (None, "none"):
        raise ValueError("No cluster was read on this connection.")
    if not isinstance(action, str) or (member_key is not None and not isinstance(member_key, str)):
        raise ValueError("Choose a member and an action.")
    # A catalog's check-and-review entry must never waive the strict review
    # gate, including when a caller reuses its catalog evaluation context.
    checks = dict(checks or {})
    checks.pop("clusterset_catalog", None)
    local = _local_member(report)
    catalog = available_actions(report, member_key, checks)
    # Keep member identity checks for legacy callers that pass a key for an
    # overview action. Never let that key move a group-wide operation.
    if member_key is None or (local and member_key == local.get("key")):
        catalog += overview_actions(report, checks)
    selected = next((a for a in catalog if a["id"] == action), None)
    if selected is None and action == "force_primary_cluster" and (report.get("clusterset") or {}).get("local_cluster"):
        # Deprecated plan-only alias; the catalog always names every target.
        lookup = action + ":" + report["clusterset"]["local_cluster"]
        selected = next((a for a in catalog if a["id"] == lookup), None)
    if selected is None:
        raise ValueError("That change is not offered for this member now. Refresh and try again.")
    if target is not None and selected["scope"] == "member":
        routed_member = (local if action == "rejoin_instance" else
                         next((m for m in _members(report) if m.get("key") == member_key), None))
        _check_member_target(report, routed_member, target)
    if not selected["enabled"]:
        raise ActionUnavailable(" ".join(text for text in
                                        (selected["why_not"], selected["fix"], selected["grant_sql"]) if text))
    if selected["scope"] == "clusterset":
        return _plan_clusterset(report, dict(selected, id=action), checks or {}, parameters, target)
    if parameters:
        raise ValueError("This action does not accept parameters.")
    if selected["target"].get("confirm"):
        # An address suggestion is catalog-only. The caller must reopen the
        # saved target and supply its binding; that fresh local identity then
        # replaces the suggestion before any write plan can be produced.
        raise ValueError("Review this action through the member's saved connection to confirm its identity.")
    if action in ("desync_node", "resync_node") and member_key != (local or {}).get("key"):
        raise ValueError("Review this action through the node's saved connection to check its own wsrep_desync state.")
    if selected["scope"] == "overview":
        member_key = local["key"]
    checks = checks or {}
    member = next(m for m in _members(report) if m.get("key") == member_key)
    cluster = report.get("cluster") or {}
    server_name = None
    if target is not None:
        from .topology_links import format_endpoint
        endpoint = target["endpoint"]
        server_name = "%s (%s)" % (target["profile_name"], format_endpoint(endpoint["host"], endpoint["port"]))
    member_name = server_name if selected["scope"] == "member" and action != "rejoin_instance" else None
    expect = dict(_server_identity(report), member=_identity(member), local=_identity(local),
                  component=cluster.get("component"), quorum=cluster.get("quorum"), topology=cluster.get("topology"))
    plan = {"schema_version": ACTION_SCHEMA_VERSION, "action": action, "member_key": member_key, "typed_guard": None,
            "warnings": [], "next": None, "verified": ["server identity", "member state and role"],
            "facts": [["Runs on", _connected_text(report, local)],
                      ["Server", "%s %s" % (product_name(report), report.get("server_version") or "")],
                      ["Cluster", "%s%s" % (kind_label(report), " %s" % cluster["name"] if cluster.get("name") else "")]]}
    kind = report.get("kind")
    if kind in ("innodb_cluster", "group_replication") and _via_shell(report, action):
        shell = _shell(checks, _shell_minimum(report, action))
        expect["members"] = [[m.get("key"), m.get("state"), m.get("role")] for m in _group_members(report)]
        expect["missing"] = sorted(m.get("key") for m in _missing(report))
        expect["shell"] = shell["path"]
        if report.get("clusterset"):
            expect["clusterset"] = [[c.get("name"), c.get("role"), c.get("invalidated")]
                                    for c in report["clusterset"].get("clusters") or ()]
        plan["via"] = "mysqlsh"
        plan["verified"].append("MySQL Shell installed")
        plan["facts"] += [["Runs with", "MySQL Shell %s (%s)" % (shell.get("version") or "", shell["path"])],
                          ["Privilege", _SHELL_PRIVILEGE]]
        _plan_shell(report, plan, action, member, local, expect, server_name=member_name)
    elif kind in ("innodb_cluster", "group_replication"):
        expect["members"] = [[m.get("key"), m.get("state"), m.get("role")] for m in _group_members(report)]
        plan["facts"].append(["Privilege", "Needs %s" %
                              ("SUPER" if _version_of(report) < (8, 0, 0) else _GR_PRIVILEGE)])
        _plan_group(report, plan, action, member, local, checks, expect, server_name=member_name)
    else:
        galera = report.get("galera") or {}
        expect.update(desync=galera.get("desync"), wsrep_on=galera.get("wsrep_on"),
                      local_state=(galera.get("metrics") or {}).get("local_state_comment"))
        plan["facts"].append(["Privilege", "Needs SUPER"])
        _plan_galera(report, plan, action, member, expect, server_name=member_name)
    plan["expect"] = expect
    if target is not None:
        # The caller supplies only a store-built binding. Node identity comes
        # from this fresh report and is persisted before a token is issued.
        expect["target"] = dict(target, server_identity=target_server_identity(report))
        plan["server_name"] = server_name
        plan["facts"][0] = ["Runs on", server_name]
        identity = target_server_identity(report)
        listed = local if action == "rejoin_instance" else member
        plan["facts"] += [["Server UUID" if "server_uuid" in identity else "Galera node UUID",
                           next(iter(identity.values()))],
                          ["Listed in the cluster as", (listed or {}).get("name")]]
        if selected["scope"] == "member" and action != "rejoin_instance":
            # Topology labels are untrusted display data. Keep them on their
            # own fact line, never in place of the saved server's name.
            plan["target_text"] = server_name
            if plan.get("typed_guard") == server_name:
                plan["typed_guard"] = target["profile_name"]
        if plan.get("via") == "mysqlsh":
            plan["warnings"].append("MySQL Shell connects to every member with %s's account." % target["profile_name"])
    if selected["why_not"]:
        plan["warnings"].append(selected["why_not"])
    plan["statements"] = [CLUSTER_ACTION_TAG + s for s in plan["statements"]]
    plan["timeout_s"] = SHELL_TIMEOUT if plan.get("via") == "mysqlsh" else ACTION_TIMEOUT
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
        "rejoin_instance": "Refresh the Cluster place and look at %s's member state." % name,
        "set_primary_cluster": "Refresh the Cluster place on a member of the primary cluster, or run "
                               "dba.getClusterSet().status() in MySQL Shell.",
        "force_primary_cluster": "Run dba.getClusterSet().status() in MySQL Shell to see which cluster is primary "
                                 "and which are invalidated.",
        "rejoin_cluster": "Run dba.getClusterSet().status() in MySQL Shell to see whether the cluster is still "
                          "invalidated.",
    }.get(_action_name(action)[0], "Refresh the Cluster place and look at %s." % name)
    return [first, "The Audit log keeps this attempt as Outcome unknown; nothing is sent again."]


def _effect(plan, before, after):
    """What changed: (what, before, now) rows from the reads before and after
    the send. A missing value says why it is missing: a read that failed is
    told apart from a server that reported no value, and no cell ever reads
    "Not readable"."""
    def word(value, missing):
        return missing if value in (None, "") else str(value).replace("_", "-")

    def name(side, key):
        return ((side or {}).get(key) or {}).get("name")

    def state(side, key):
        item = (side or {}).get(key) or {}
        return " ".join(x for x in (item.get("state"), item.get("role")) if x) or None

    def channel(side):
        item = side.get("cs_channel") or {}
        states = ", ".join("%s %s" % (label, item[key]) for label, key in
                           (("receiver", "receiver_state"), ("applier", "applier_state")) if item.get(key))
        return " ".join(x for x in (item.get("source"), states) if x) or None

    # In multi-primary mode every ONLINE member reports PRIMARY, so the
    # snapshot's primary is a member; None means no ONLINE or RECOVERING
    # member reported PRIMARY in the view read here, or that read failed.
    no_primary = "No member reported the PRIMARY role in the view read here"
    action = _action_name(plan["action"])[0]
    if action in CLUSTERSET_ACTIONS:
        # An unread post-action report is not an observed removal. In
        # particular, dissolve verifies individual cleanup facts and does
        # not return a new ClusterSet report at all.
        rows = []
        readable = after.get("cs_present", True) is not None
        if readable:
            rows = [["Primary cluster", word(before.get("cs_primary"), "No primary cluster in the view read here"),
                     word(after.get("cs_primary"), "No primary cluster in the view read here")],
                    ["Invalidated", ", ".join(before.get("cs_invalidated") or ()) or "none",
                     ", ".join(after.get("cs_invalidated") or ()) or "none"]]
            if before.get("cs_channel") is not None or after.get("cs_channel") is not None:
                rows.append(["ClusterSet channel", word(channel(before), "No channel in the view read here"),
                             word(channel(after), "No channel in the view read here")])
            for key, label in (("cs_present", "ClusterSet metadata present"),
                               ("cs_clusters", "Member clusters"), ("cs_view_id", "Metadata view")):
                if before.get(key) is not None and after.get(key) is not None and before[key] != after[key]:
                    rows.append([label, json.dumps(before[key], ensure_ascii=False, sort_keys=True),
                                 json.dumps(after[key], ensure_ascii=False, sort_keys=True)])
            old_channels, new_channels = before.get("cs_channels") or {}, after.get("cs_channels") or {}
            for cluster_name in sorted(set(old_channels) & set(new_channels)):
                if old_channels[cluster_name] != new_channels[cluster_name]:
                    rows.append(["ClusterSet channel · " + cluster_name,
                                 json.dumps(old_channels[cluster_name], ensure_ascii=False, sort_keys=True),
                                 json.dumps(new_channels[cluster_name], ensure_ascii=False, sort_keys=True)])
            if action == "set_routing_option":
                old, new = before.get("cs_routing") or {}, after.get("cs_routing") or {}
                for key, label in (("defaults", "Routing defaults"), ("routers", "Router options")):
                    if old.get(key) is not None and new.get(key) is not None and old[key] != new[key]:
                        rows.append([label, json.dumps(old[key], ensure_ascii=False, sort_keys=True),
                                     json.dumps(new[key], ensure_ascii=False, sort_keys=True)])
        # Cleanup has no after report. Compare its actual observations with
        # known pre-action state, never with the desired state of a verifier.
        uuid, server = None, None
        for fact in (after.get("verification") or {}).get("facts") or ():
            if fact.get("fact") == "route server UUID":
                uuid = fact.get("actual") if fact.get("status") == "verified" else None
                server = fact.get("server")
            if not uuid or fact.get("server") != server or fact.get("status") == "missing":
                continue
            state = (before.get("cs_member_states") or {}).get(uuid)
            if (fact.get("fact") == "Group Replication stopped" and state in ("ONLINE", "RECOVERING") and
                    isinstance(fact.get("actual"), list) and uuid not in fact["actual"]):
                rows.append(["Group Replication on " + server, state, "Stopped"])
            if (fact.get("fact") == "metadata schema present" and fact.get("actual") is False and
                    uuid == before.get("cs_metadata_server_uuid")):
                rows.append(["Cluster metadata on " + server, "Present", "Removed"])
        return rows
    if action == "switchover":
        rows = [["Primary", word(name(before, "primary"), no_primary), word(name(after, "primary"), no_primary)]]
        if before.get("cs_channel") is not None or after.get("cs_channel") is not None:
            rows.append(["ClusterSet channel", word(channel(before), "No channel in the view read here"),
                         word(channel(after), "No channel in the view read here")])
        return rows
    if action == "rejoin_instance":
        return [[name(after, "member") or name(before, "member") or "The instance",
                 word(state(before, "member"), "Not in the view read before the action"),
                 word(state(after, "member"), "Not in the view read after the action")]]
    if action in ("stop_member", "start_member"):
        # The after state comes from the member's own row; None there means
        # the read after the action failed, not that the server said nothing.
        return [[name(after, "local") or name(before, "local") or "This member",
                 word(state(before, "local"), "Not in the view read before the action"),
                 word(state(after, "local"), "The read after the action failed")]]
    if action in ("desync_node", "resync_node"):
        return [["wsrep_desync", word(before.get("desync"), "No value reported before the action"),
                 word(after.get("desync"), "No value reported after the action")],
                ["State", word(before.get("local_state"), "No value reported before the action"),
                 word(after.get("local_state"), "No value reported after the action")]]
    if action == "bootstrap_pc":
        return [["Component", word(before.get("component"), "Component status not reported"),
                 word(after.get("component"), "Component status not reported")]]
    return [["Group mode", word(before.get("topology"), "Mode not reported by the server"),
             word(after.get("topology"), "Mode not reported by the server")]]


def _plan_group(report, plan, action, member, local, checks, expect, server_name=None):
    name = server_name or member.get("name")
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
        bootstrap = (checks.get("bootstrap_group", (report.get("group_replication") or {}).get("bootstrap_group"))
                     or "").upper()
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


def _action_via(report, action):
    base, _ = _action_name(action)
    return ("mysqlsh" if _via_shell(report, action) else "udf" if base in
            ("switchover", "switch_to_multi_primary", "switch_to_single_primary") else "sql")


def _via_shell(report, action):
    """True when MySQL Shell runs *action* (offered only with MySQL Shell)."""
    base, _ = _action_name(action)
    if base in SHELL_ACTIONS:
        return True
    if report.get("kind") != "innodb_cluster":
        return False
    if base in ("switch_to_multi_primary", "switch_to_single_primary"):
        return True
    return base == "switchover" and (report.get("clusterset") or {}).get("local_role") == "REPLICA"


def _js(value):
    """Strict JSON as a single JavaScript expression, including hostile text.

    JSON.parse preserves object keys such as __proto__; a JS object literal
    silently treats that key as a prototype setter. Escaping '<' also keeps
    the exact reviewed line safe when copied into an HTML script element.
    """
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
    if isinstance(value, (dict, list)):
        return "JSON.parse(%s)" % json.dumps(encoded, ensure_ascii=True)
    return encoded


def _clusterset_shell_version(version, minimum):
    if tuple(version or ()) < minimum:
        raise ValueError("This MySQL Shell call needs version %s or later." % ".".join(str(n) for n in minimum))


def _clusterset_shell_options(options, allowed, version, gates=None):
    if not isinstance(options, dict) or any(not isinstance(key, str) for key in options):
        raise ValueError("MySQL Shell options must be an object.")
    if set(options) - set(allowed):
        raise ValueError("Unsupported MySQL Shell option: %s." % ", ".join(sorted(set(options) - set(allowed))))
    for name, value in options.items():
        _clusterset_shell_version(version, (gates or {}).get(name, SHELL_CLUSTERSET_MIN_VERSION))
        if name in ("timeout", "recoveryProgress"):
            if type(value) is not int or value < 0 or value > 2147483647:
                raise ValueError("%s must be a nonnegative integer." % name)
            if name == "timeout" and value >= SHELL_TIMEOUT:
                raise ValueError("timeout must be less than the MySQL Shell time limit (%d seconds)." % SHELL_TIMEOUT)
            if name == "recoveryProgress" and value not in (0, 1, 2):
                raise ValueError("recoveryProgress must be 0, 1 or 2.")
        elif name in ("force", "dissolve") and not isinstance(value, bool):
            raise ValueError("%s must be true or false." % name)
        elif name == "invalidateReplicaClusters":
            if (not isinstance(value, list) or not value or
                    any(not isinstance(n, str) or not n for n in value) or len(set(value)) != len(value)):
                raise ValueError("invalidateReplicaClusters must be a nonempty list of distinct cluster names.")
        elif name == "recoveryMethod" and value not in ("clone", "incremental"):
            # mysql-shell/modules/adminapi/common/gtid_validations.cc:238-247:
            # ambiguous automatic recovery fails with --no-wizard.
            raise ValueError("Choose recoveryMethod clone or incremental explicitly.")
    return dict(options)


def _clusterset_call(method, arguments, options=None):
    values = [_js(value) for value in arguments]
    if options:
        values.append(_js(options))
    return "myflamesClusterSet.%s(%s);" % (method, ", ".join(values))


def _shell_set_primary_cluster(name, options, version):
    # mysql-shell @8.0.27 modules/adminapi/cluster_set/api_options.cc:153-162.
    _clusterset_shell_version(version, SHELL_CLUSTERSET_MIN_VERSION)
    options = _clusterset_shell_options(options, ("timeout", "invalidateReplicaClusters"), version)
    return _clusterset_call("setPrimaryCluster", [name], options)


def _shell_force_primary_cluster(name, options, version):
    # mysql-shell @8.0.32 modules/adminapi/cluster_set/api_options.cc:168-175 added timeout.
    _clusterset_shell_version(version, SHELL_CLUSTERSET_MIN_VERSION)
    options = _clusterset_shell_options(options, ("timeout", "invalidateReplicaClusters"), version,
                                        {"timeout": (8, 0, 32)})
    return _clusterset_call("forcePrimaryCluster", [name], options)


def _shell_rejoin_cluster(name, version):
    # mysql-shell/modules/adminapi/cluster_set/api_options.cc:298 registers only
    # the preview option; this review never runs a preview call.
    _clusterset_shell_version(version, SHELL_CLUSTERSET_MIN_VERSION)
    return _clusterset_call("rejoinCluster", [name])


def _shell_repair_cluster(name, version):
    # mysql-shell/modules/adminapi/cluster_set/cluster_set_impl.cc:3378-3387,3439-3453:
    # refresh_only repairs the account/channel without a membership view.
    return _shell_rejoin_cluster(name, version)


def _shell_remove_cluster(name, options, version):
    # mysql-shell @8.0.27 modules/adminapi/cluster_set/api_options.cc:92-101; @9.0.0:195-202.
    _clusterset_shell_version(version, SHELL_CLUSTERSET_MIN_VERSION)
    options = _clusterset_shell_options(options, ("force", "timeout", "dissolve"), version,
                                        {"dissolve": (9, 0, 0)})
    return _clusterset_call("removeCluster", [name], options)


def _shell_set_routing_option(router, option, value, version):
    # mysql-shell/modules/adminapi/common/router.cc:367-496 accepts null, validates
    # values, and resolves cluster names; metadata_storage.cc:3543-3551
    # matches the complete Router address::name identifier.
    if not isinstance(option, str):
        raise ValueError("Choose a supported Router routing option.")
    tag = option.startswith("tag:") and len(option) > 4
    minimum = CLUSTERSET_ROUTING_MINIMUM.get("tags" if tag else option)
    if minimum is None:
        raise ValueError("Choose a supported Router routing option.")
    _clusterset_shell_version(version, minimum)
    if value is not None and not tag:
        valid = {
            "target_cluster": lambda: isinstance(value, str) and bool(value),
            "invalidated_cluster_policy": lambda: value in ("drop_all", "accept_ro"),
            "stats_updates_frequency": lambda: type(value) is int and 0 <= value <= 2147483647,
            "use_replica_primary_as_rw": lambda: isinstance(value, bool),
            "tags": lambda: isinstance(value, dict),
            "read_only_targets": lambda: value in ("all", "read_replicas", "secondaries"),
            "guideline": lambda: isinstance(value, str) and bool(value),
        }[option]()
        if not valid:
            raise ValueError("The value for %s is not valid." % option)
    return _clusterset_call("setRoutingOption", ([router] if router is not None else []) + [option, value])


def _shell_create_replica_cluster(address, name, options, version):
    # mysql-shell @8.0.27 modules/adminapi/cluster_set/api_options.cc:65-86 and common/common.h:239;
    # 9104d2f0b common/api_options.cc:52-74: 1 is textual recovery progress.
    _clusterset_shell_version(version, SHELL_CLUSTERSET_MIN_VERSION)
    options = dict(options)
    options.setdefault("recoveryProgress", 1)
    if "recoveryMethod" not in options:
        raise ValueError("Choose recoveryMethod clone or incremental explicitly.")
    options = _clusterset_shell_options(options, ("recoveryMethod", "recoveryProgress", "timeout"), version)
    return _clusterset_call("createReplicaCluster", [address, name], options)


def _shell_dissolve_clusterset(options, version):
    # mysql-shell @9.0.0 modules/adminapi/mod_dba_cluster_set.cc:91; current api_options.cc:227.
    _clusterset_shell_version(version, (9, 0, 0))
    options = _clusterset_shell_options(options, ("force", "timeout"), version)
    return _clusterset_call("dissolve", [], options)


def _shell_check_clusterset(version):
    # mysql-shell/modules/adminapi/cluster_set/api_options.cc:207, status.cc:340,423:
    # extended level 1 includes topology, channel and GTID consistency detail.
    _clusterset_shell_version(version, SHELL_CLUSTERSET_MIN_VERSION)
    call = _clusterset_call("status", [], {"extended": 1})
    return "print(JSON.stringify(%s, null, 2));" % call.rstrip(";")


def _clusterset_plan_parameters(base, parameters):
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict) or any(not isinstance(key, str) for key in parameters):
        raise ValueError("ClusterSet parameters must be an object with named options.")
    allowed = {
        "set_primary_cluster": ("timeout", "invalidateReplicaClusters"),
        "force_primary_cluster": ("timeout", "invalidateReplicaClusters"),
        "rejoin_cluster": (), "repair_cluster": (),
        "remove_cluster": ("timeout", "force"), "remove_cluster_keep": ("timeout", "force"),
        "set_routing_option": ("option", "value"),
        "create_replica_cluster": ("name", "recoveryMethod", "recoveryProgress", "timeout"),
        "dissolve_clusterset": ("timeout", "force"), "check_clusterset": (),
    }[base]
    if set(parameters) - set(allowed):
        raise ValueError("Unsupported ClusterSet parameter: %s." % ", ".join(sorted(set(parameters) - set(allowed))))
    # Detach nested options from the request, and refuse non-JSON values.
    return json.loads(json.dumps(parameters, allow_nan=False))


def _clusterset_router_targets(report, name):
    """Name explicit and inherited pins; Shell checks only per-Router pins.

    mysql-shell/modules/adminapi/common/metadata_storage.cc:3433-3448 omits the
    global target. Values in metadata may be names or Group Replication UUIDs.
    """
    cs = report["clusterset"]
    identifiers = (name, getattr(report, "_clusterset_groups", {}).get(name))
    global_target = ((cs.get("routing_options") or {}).get("options") or {}).get("target_cluster")
    labels = []
    if global_target is not None and global_target in identifiers:
        labels.append("ClusterSet routing default")
    for router in cs.get("routers") or ():
        options = router.get("options") or {}
        target = options.get("target_cluster")
        if target is None:
            target = global_target
        if target is not None and target in identifiers:
            labels.append(router.get("name") or "Router with an unreadable name")
    return labels


def _clusterset_routing_configuration(cs):
    """Return the stored global and per-Router options, or None on read failure.

    Router rows have no SQL order. Sort their JSON representations so rows
    with equal natural names cannot make an unchanged review appear stale.
    """
    defaults, routers = cs.get("routing_options"), cs.get("routers")
    if (not isinstance(defaults, dict) or not isinstance(defaults.get("options"), dict) or
            not isinstance(routers, list) or
            any(not isinstance(router, dict) or not isinstance(router.get("options"), dict) for router in routers)):
        return None
    return {"defaults": defaults,
            "routers": sorted(routers, key=lambda router: json.dumps(router, sort_keys=True, allow_nan=False))}


def _plan_clusterset(report, selected, checks, parameters, target_binding=None):
    """Plan-only contract for the ClusterSet execution/verification lane.

    reviewed_writes.review_details binds the entire result, including exact
    JS, effects, confirmation, parameters, route UUID and metadata view_id.
    Existing server/AdminAPI group-UUID guards remain in server_identity.
    """
    base, suffix = _action_name(selected["id"])
    cs = report["clusterset"]
    local = _local_member(report)
    # The catalog already checked the selected route's SSH eligibility.
    # The active connection can be a different, tunneled replica.
    shell = _shell(dict(checks, ssh=False), _shell_minimum(report, selected["id"]))
    version = tuple(shell["version_tuple"])
    parameters = _clusterset_plan_parameters(base, parameters)
    clusters = cs.get("clusters") or []
    name = selected.get("cluster_name")
    cluster = next((c for c in clusters if c.get("name") == name), None)
    primary = _clusterset_primary(report)
    mine = next((c for c in clusters if c.get("local")), None)
    route = _clusterset_route(report, cluster) or _clusterset_route(report, mine)
    if route is None and base == "check_clusterset":
        # Status is useful even when the SQL group view failed. The live
        # connection identity still guards the read-only Shell session.
        uuid = (report.get("server") or {}).get("server_uuid")
        if isinstance(uuid, str) and _UUID.fullmatch(uuid):
            current = checks.get("current_profile")
            route = {"self": True, "profile_id": current.get("id") if isinstance(current, dict) else current,
                     "server_uuid": uuid}
    if route is None:
        raise ValueError("A fresh ClusterSet route UUID is required before review.")
    expect = dict(_server_identity(report), member=_identity(local), local=_identity(local),
                  component=(report.get("cluster") or {}).get("component"),
                  quorum=(report.get("cluster") or {}).get("quorum"),
                  topology=(report.get("cluster") or {}).get("topology"),
                  members=[[m.get("key"), m.get("state"), m.get("role")] for m in _group_members(report)],
                  missing=sorted(m.get("key") for m in _missing(report)), shell=shell["path"],
                  clusterset=[[c.get("name"), c.get("role"), c.get("invalidated")] for c in clusters],
                  clusterset_view_id=cs["view_id"])
    # Only addresses in this set's reviewed metadata can identify Shell's
    # internal metadata server. Do not add profile aliases or instances of
    # unrelated clusters that happen to share the metadata schema.
    cluster_ids = getattr(report, "_clusterset_ids", {})
    instances = getattr(report, "_clusterset_instances", {})
    metadata_addresses = sorted({instance["address"] for c in clusters
                                 for instance in instances.get(cluster_ids.get(c.get("name")), ())
                                 if isinstance(instance.get("address"), str) and instance["address"]})
    # v2_cs_members has a NULL member_role for an invalidated cluster. Shell
    # decodes that as primary_cluster=false and status emits REPLICA
    # (common/metadata_storage.cc:3604; cluster_set/status.cc:118-120).
    roles = {c["name"]: ("REPLICA" if c.get("invalidated") is True and c.get("role") is None else c.get("role"))
             for c in clusters}
    expect["server_identity"] = dict(expect["server"], adminapi={
        "kind": "clusterset", "name": cs["name"], "groups": dict(getattr(report, "_clusterset_groups", {})),
        "roles": roles,
        "invalidated": {c["name"]: c.get("invalidated") for c in clusters},
        "primary_cluster": cs.get("primary_cluster"), "metadata_addresses": metadata_addresses})
    plan = {"schema_version": ACTION_SCHEMA_VERSION, "action": selected["id"], "scope": "clusterset",
            "member_key": (local or {}).get("key"), "via": "mysqlsh", "kind": selected["kind"],
            "typed_guard": None, "warnings": [], "next": None, "parameters": parameters,
            "verified": ["server identity", "ClusterSet membership and view", "target route UUID", "MySQL Shell installed"],
            "facts": [["Runs on", _connected_text(report, local)], ["ClusterSet", cs["name"]],
                      ["Metadata view", cs["view_id"]],
                      ["Runs with", "MySQL Shell %s (%s)" % (shell.get("version") or "", shell["path"])],
                      ["Privilege", _SHELL_PRIVILEGE]],
            "timeout_s": SHELL_TIMEOUT, "target_text": name or cs["name"]}
    if base in ("set_primary_cluster", "force_primary_cluster"):
        # Router options are stored independently of the ClusterSet view_id.
        # Bind both the defaults and every Router override to this review.
        expect["routing_configuration"] = _clusterset_routing_configuration(cs)
        options = _clusterset_shell_options(parameters, ("timeout", "invalidateReplicaClusters"), version,
                                            {"timeout": (8, 0, 32)} if base == "force_primary_cluster" else None)
        for invalidated in options.get("invalidateReplicaClusters") or ():
            other = next((c for c in clusters if c.get("name") == invalidated), None)
            if (not other or other.get("role") != "REPLICA" or other.get("invalidated") or invalidated == name or
                    not _clusterset_unreachable(report, other, checks)):
                raise ValueError("Only another replica confirmed unreachable may be explicitly invalidated.")
        old = cs.get("primary_cluster") or "the previous primary cluster"
        if base == "set_primary_cluster":
            call = _shell_set_primary_cluster(name, options, version)
            plan.update(eyebrow="ClusterSet.setPrimaryCluster", title="Make %s the primary cluster?" % name,
                        confirm_label="Make %s the primary cluster" % name,
                        description="MySQL Shell synchronizes %s with %s, promotes it and repoints replica channels; "
                                    "writes wait during the switch. Routers targeting primary follow the new primary "
                                    "after refreshing; named targets and routing guidelines can route elsewhere." % (name, old),
                        undo="dba.getClusterSet().setPrimaryCluster(%s);" % _js(old))
        else:
            # mysql-shell/modules/adminapi/mod_dba_cluster_set.cc:689-701: the isolated old
            # primary and its Routers can continue from an obsolete view.
            call = _shell_force_primary_cluster(name, options, version)
            plan.update(eyebrow="ClusterSet.forcePrimaryCluster", title="Fail over the ClusterSet to %s?" % name,
                        confirm_label="Fail over to %s" % name, typed_guard=name,
                        description="MySQL Shell promotes %s without %s and invalidates the old primary in metadata. "
                                    "Transactions committed there that %s did not receive are lost from this ClusterSet. "
                                    "An isolated old primary and Routers that only see it can keep taking writes." % (name, old, name),
                        undo="Cannot be undone; fence the old primary and resolve missing or divergent transactions before rejoining it.")
            plan["warnings"].append("MySQL Shell checks every old-primary member is unreachable and that the target has "
                                    "the most recent GTID set among surviving candidates.")
        plan["facts"] += [["New primary cluster", name], ["Previous primary cluster", old]]
    elif base in ("rejoin_cluster", "repair_cluster"):
        repair = base == "repair_cluster"
        call = (_shell_repair_cluster if repair else _shell_rejoin_cluster)(name, version)
        plan.update(eyebrow="ClusterSet.rejoinCluster", title=("Repair replication on %s?" if repair else
                    "Rejoin %s to the ClusterSet?") % name, confirm_label="Repair replication" if repair else "Rejoin cluster",
                    description=("MySQL Shell refreshes %s's replication account and channel settings without adding "
                                 "a membership view; this does not guarantee that an unreachable source will recover." % name
                                 if repair else "MySQL Shell checks %s's transactions, rejoins it in a new membership view "
                                 "and points its replication channel at the current primary cluster." % name),
                    undo="There is no automatic undo; inspect the cluster's transactions and replication state before making another change.")
        plan["warnings"].append("Errant or purged transactions can prevent rejoin; metadata membership alone does not prove replication is healthy.")
    elif base in ("remove_cluster", "remove_cluster_keep"):
        options = dict(parameters)
        unreachable = _clusterset_unreachable(report, cluster, checks)
        keep = base == "remove_cluster_keep"
        if keep and (unreachable or options.get("force") is True):
            raise ValueError("Keeping a removed cluster running requires a reachable ONLINE primary and quorum; "
                             "force is not supported.")
        if options.get("force") is True and not unreachable:
            raise ValueError("Force removal is offered only for a cluster confirmed unreachable.")
        if unreachable and options.get("force") is not True:
            raise ValueError("Removing this unreachable cluster requires an explicit force choice in the review.")
        if version >= (9, 0, 0):
            options["dissolve"] = not keep
        call = _shell_remove_cluster(name, options, version)
        routers = _clusterset_router_targets(report, name)
        expect["routing_configuration"] = _clusterset_routing_configuration(cs)
        # Put affected routing first, before the effects of the removal.
        plan["facts"].insert(0, ["Routing targets to change first", ", ".join(routers) if routers else
                                "No explicit or inherited target was found in the configuration read here"])
        if routers:
            plan["warnings"].append("Change these routing targets before removal: %s." % ", ".join(routers))
        plan.update(eyebrow="ClusterSet.removeCluster", title="Remove %s%s?" % (name, " and keep it running" if keep else " and dissolve it"),
                    confirm_label="Remove, keep running" if keep else "Remove and dissolve", typed_guard=name,
                    description=("MySQL Shell removes %s from this set and keeps it running as a standalone InnoDB Cluster. "
                                 "This introduces divergent transactions, so it cannot simply be re-added to this set." % name
                                 if keep else "MySQL Shell removes %s from this set, resets its ClusterSet channel and "
                                 "dissolves the cluster into standalone instances by default; user data is retained." % name),
                    undo="Removal has no automatic undo; rebuilding or reprovisioning the removed cluster may be necessary.")
        if unreachable:
            plan["warnings"].append("Forced removal can leave stale metadata and replication configuration on unreachable servers.")
    elif base == "set_routing_option":
        if "option" not in parameters or "value" not in parameters:
            raise ValueError("Choose a routing option and its value before review.")
        option, value = parameters["option"], parameters["value"]
        if option == "target_cluster" and value is not None and value != "primary" and value not in [c.get("name") for c in clusters]:
            raise ValueError("Choose primary or a cluster name in the current ClusterSet.")
        # mysql-shell/modules/adminapi/common/metadata/metadata-model-2.3.0.sql:35,66 adds
        # routing guidelines; an older schema cannot store a guideline ID.
        if option == "guideline" and value is not None and _version(
                (report.get("metadata") or {}).get("schema_version")) < (2, 3, 0):
            raise ValueError("Routing guidelines need ClusterSet metadata 2.3.0 or later.")
        call = _shell_set_routing_option(suffix, option, value, version)
        plan.update(eyebrow="ClusterSet.setRoutingOption", title="Change routing for %s?" % (suffix or cs["name"]),
                    confirm_label="Store routing option", target_text=suffix or "ClusterSet routing defaults",
                    description="This changes stored configuration; Routers apply it on their next refresh. "
                                "A successful metadata write does not prove that traffic follows the new setting.",
                    undo="Review the previous stored value and restore it, or clear a per-Router override with null.")
        plan["warnings"].append("Active routing guidelines take precedence over target_cluster and read_only_targets; "
                                "use_replica_primary_as_rw has no effect with an active guideline.")
        if option == "guideline":
            plan["warnings"].append("MySQL Shell requires an existing guideline and compatible Router versions.")
        expect["routing_configuration"] = _clusterset_routing_configuration(cs)
    elif base == "create_replica_cluster":
        name = parameters.get("name")
        if not isinstance(name, str) or not _CLUSTER_NAME.fullmatch(name):
            raise ValueError("Choose a new cluster name of at most 63 letters, digits, underscores, hyphens or dots, starting with a letter or underscore.")
        if name.casefold() in [c["name"].casefold() for c in clusters]:
            raise ValueError("A cluster with that name already belongs to this ClusterSet.")
        candidate = next((c for c in checks.get("replica_candidates") or () if c.get("profile_id") == suffix), None)
        if candidate is None:
            raise ValueError("Read the standalone server's preflight before reviewing creation. Nothing was sent.")
        route = candidate["route"]
        options = {key: value for key, value in parameters.items() if key != "name"}
        expect["server_identity"]["target_instance"] = {
            "address": candidate["address"], "server_uuid": candidate["server_uuid"]}
        if options.get("recoveryMethod") == "clone":
            from .cluster_shell import CLONE_IDLE_TIMEOUT, CLONE_TIMEOUT
            plan.update(timeout_s=CLONE_TIMEOUT, idle_timeout_s=CLONE_IDLE_TIMEOUT)
            plan["warnings"].append("Clone replaces the selected server's data and restarts it. Stop is unavailable "
                                    "once cloning starts. MySQL Shell is stopped after 60 minutes without output "
                                    "or 24 hours overall; even a healthy silent clone stage can reach that limit. "
                                    "Stopping the client does not undo a server clone; inspect recovery before retrying.")
        plan["warnings"].append("Immediately before creation, a Shell session checks the reviewed target address "
                                "against the spare's UUID. Shell opens its own connections afterwards, including "
                                "another connection for clone; a changed address can still reach another server. "
                                "Use a direct server address, without a router or load balancer.")
        call = _shell_create_replica_cluster(candidate["address"], name, options, version)
        plan.update(eyebrow="ClusterSet.createReplicaCluster", title="Create replica cluster %s?" % name,
                    confirm_label="Create replica cluster", target_text=candidate["address"], typed_guard=name,
                    description="MySQL Shell creates %s on %s using %s recovery, creates its accounts and replication "
                                "channel, and makes it read-only. Recovery progress is streamed while Shell runs." %
                                (name, candidate["address"], options.get("recoveryMethod")),
                    undo="Creation has no automatic undo; removing metadata does not undo recovery.")
        plan["facts"] += [["Instance", candidate["address"]], ["Recovery method", options["recoveryMethod"]],
                          ["Reported address", candidate["reported_address"]],
                          ["Saved connection Read-only", "Yes" if candidate["read_only"] else "No"],
                          ["Target super_read_only", "ON" if candidate["super_read_only"] else "OFF"],
                          ["Target GTIDs subset of primary", "Yes"]]
        expect["replica_candidate"] = {key: candidate.get(key) for key in
                                       ("profile_id", "server_uuid", "address", "reported_address", "endpoint", "read_only",
                                        "saved_group", "group_generation", "engine", "server_version", "kind",
                                        "group_name", "gtid_subset", "super_read_only", "configuration")}
    elif base == "dissolve_clusterset":
        if parameters.get("force") is True and not any(_clusterset_unreachable(report, c, checks) for c in clusters):
            raise ValueError("Force dissolve is offered only when a cluster is confirmed unreachable.")
        call = _shell_dissolve_clusterset(parameters, version)
        # mysql-shell/modules/adminapi/cluster_set/dissolve.cc:374 skips the wizard
        # prompt; :483-484 discards get_cluster_server(), so :542,588,592
        # skip the intended primary-last/reconciliation steps (upstream bug).
        plan.update(eyebrow="ClusterSet.dissolve", title="Dissolve ClusterSet %s?" % cs["name"],
                    confirm_label="Dissolve ClusterSet", typed_guard=cs["name"],
                    description="MySQL Shell drops metadata and internal recovery accounts, stops replication and "
                                "dissolves the clusters while keeping user data. The --no-wizard flag skips Shell's "
                                "own confirmation, so this typed review is the confirmation gate.",
                    undo="Dissolving the ClusterSet is irreversible; its topology must be rebuilt to restore it.")
        plan["warnings"].append("Shell's implementation skips its intended primary-last cleanup step; every reachable "
                                "instance must be checked afterwards, even if Shell prints success.")
    else:
        call = _shell_check_clusterset(version)
        plan.update(eyebrow="ClusterSet.status", title="Check with MySQL Shell", confirm_label="Check status",
                    read_only=True, description="MySQL Shell opens connections to every member and reads "
                                               "ClusterSet status with extended level 1; this call changes no server state.",
                    undo="No change is made by this status call.")
    if cs.get("from_local_copy"):
        plan["warnings"].append("The metadata read here is a replicated copy and may be behind; " +
                                ("the old primary is unavailable, so failover checks the surviving clusters." if
                                 base == "force_primary_cluster" else
                                 "MySQL Shell resolves the ClusterSet from the available members before the call."))
    if selected.get("why_not"):
        plan["warnings"].append(selected["why_not"])
    expect["clusterset_target"] = {"cluster_name": name, "server_uuid": route["server_uuid"],
                                   "route": route, "view_id": cs["view_id"],
                                   "cluster_id": getattr(report, "_clusterset_ids", {}).get(name),
                                   "group_name": getattr(report, "_clusterset_groups", {}).get(name),
                                   "role": cluster.get("role") if cluster else None,
                                   "invalidated": cluster.get("invalidated") if cluster else None}
    expect["server_identity"]["adminapi"]["view_id"] = str(cs["view_id"])
    expect["clusterset_metadata_read_on"] = cs.get("metadata_read_on")
    expect["clusterset_parameters"] = parameters
    if base in ("force_primary_cluster", "remove_cluster", "remove_cluster_keep", "dissolve_clusterset") or parameters.get("invalidateReplicaClusters"):
        expect["clusterset_reachability"] = checks.get("clusterset_reachability", getattr(
            report, "_clusterset_reachability", None) or cs.get("reachability", {}))
    if target_binding is not None:
        expect["target"] = dict(target_binding, server_identity=target_server_identity(report))
    plan["facts"].append(["Route server UUID", route["server_uuid"]])
    plan["statements"] = [call if base == "check_clusterset" else CLUSTER_ACTION_TAG + call]
    plan["checks"] = [
        "Read the ClusterSet view_id, roles and invalidation flags on the current primary.",
        "Read Group Replication, clusterset_replication source/receiver/applier and super_read_only on every reachable cluster primary.",
        "Name every member that could not be read; a Shell success message does not verify those servers.",
        "Use dba.getClusterSet().status() for the Shell view and compare it with the direct SQL reads.",
    ]
    if base == "dissolve_clusterset":
        plan["checks"].append("On every reachable instance, check metadata removal, Group Replication, both managed "
                              "channels, failover configuration and persisted startup settings.")
    if base == "set_routing_option":
        plan["checks"].append("Read stored global and per-Router options; check Router refresh and application traffic separately.")
    plan["expect"] = expect
    # Keep review evidence independent of later report/checks mutation.
    return json.loads(json.dumps(plan, allow_nan=False))


def _plan_shell(report, plan, action, member, local, expect, server_name=None):
    """The plan for a change MySQL Shell runs: one AdminAPI call."""
    base, target = _action_name(action)
    name = server_name or member.get("name")
    cs = report.get("clusterset") or {}
    # Bind the object AdminAPI resolves, as well as Shell's global session.
    # Its connections may follow a router or primary to a different cluster.
    adminapi = {"kind": "cluster", "group_name": (report.get("group_replication") or {}).get("group_name")}
    expect["server_identity"] = dict(expect["server"], adminapi=adminapi)
    cluster = (report.get("cluster") or {}).get("name") or "the cluster"
    primary = next((m for m in _group_members(report) if m.get("role") == "PRIMARY"), None)
    if base == "switchover":
        address = member.get("address")
        uuid = member.get("uuid")
        if not isinstance(uuid, str) or not _UUID.fullmatch(uuid):
            raise ValueError("The new primary's server UUID is missing or invalid. Refresh before reviewing it.")
        # Bind the member behind Shell's address, using the same pre-launch
        # UUID probe as rejoin before AdminAPI opens its own target connection.
        expect["server_identity"]["target_instance"] = {"address": address, "server_uuid": uuid}
        plan.update(eyebrow="Cluster.setPrimaryInstance", title="Make %s the primary?" % name,
                    confirm_label="Make %s primary" % name, target_text=name,
                    description="This cluster is a replica cluster of the ClusterSet %s, so MySQL Shell switches "
                                "the primary (setPrimaryInstance): it stops the clusterset_replication channel on "
                                "the current primary and starts it on %s." % (cs.get("name") or "", name))
        plan["statements"] = ["dba.getCluster().setPrimaryInstance(%s);" % _js(address)]
        plan["facts"] += [["New primary", "%s (%s)" % (name, address)],
                          ["Current primary", primary.get("name") if primary else "None"]]
        if primary and _ADDRESS.fullmatch(primary.get("address") or ""):
            plan["undo"] = ("dba.getCluster().setPrimaryInstance(%s); makes %s the primary again."
                            % (_js(primary.get("address")), primary.get("name")))
        else:
            plan["undo"] = "Make the previous primary the primary again."
    elif base in ("switch_to_multi_primary", "switch_to_single_primary"):
        multi = base == "switch_to_multi_primary"
        call = "switchToMultiPrimaryMode" if multi else "switchToSinglePrimaryMode"
        plan.update(eyebrow="Cluster.%s" % call,
                    title="Switch the cluster %s to %s mode?" % (cluster, "multi-primary" if multi else
                                                                 "single-primary"),
                    confirm_label="Switch to %s" % ("multi-primary" if multi else "single-primary"),
                    target_text="the cluster (%d members)" % len(_group_members(report)),
                    description=("MySQL Shell runs the Group Replication switch, then sets auto_increment_increment "
                                 "and auto_increment_offset on every member (increment 7, or the group size when "
                                 "larger) and the cluster's mode in the metadata. Every member accepts writes; "
                                 "SERIALIZABLE transactions and writes to tables with cascading foreign keys are "
                                 "refused, and conflicting writes on different members roll back." if multi else
                                 "MySQL Shell runs the Group Replication switch, then resets auto_increment on every "
                                 "member (increment 1, offset 2) and the cluster's mode in the metadata. The group "
                                 "elects one primary; the other members become read-only."))
        plan["statements"] = ["dba.getCluster().%s();" % call]
        plan["warnings"].append("MySQL Shell connects to every member with this connection's account and refuses "
                                "unless every member is ONLINE.")
        if multi:
            plan["warnings"].append("Clients that write must be able to reach every member: MySQL Router sends "
                                    "writes to all of them in multi-primary mode.")
        plan["typed_guard"] = cluster if _CLUSTER_NAME.fullmatch(cluster) else (local.get("name") if local else None)
        plan["undo"] = "dba.getCluster().%s();" % ("switchToSinglePrimaryMode" if multi else
                                                   "switchToMultiPrimaryMode")
    elif base == "rejoin_instance":
        address = member.get("address")
        uuid = member.get("uuid")
        if not isinstance(uuid, str) or not _UUID.fullmatch(uuid):
            raise ValueError("The rejoin target's server UUID is missing or invalid. Refresh before reviewing it.")
        # Shell accepts a changed UUID at a metadata address (common/common.cc:
        # 950-963). Bind that exact address and UUID for the transport's read-only
        # probe before launch; Shell subsequently opens its own connection there.
        expect["server_identity"]["target_instance"] = {"address": address, "server_uuid": uuid}
        plan.update(eyebrow="Cluster.rejoinInstance", title="Rejoin %s to %s?" % (name, cluster),
                    confirm_label="Rejoin %s" % name, target_text=name,
                    description="MySQL Shell rejoins %s with incremental recovery from a donor, restores its "
                                "recovery account and updates the cluster metadata." % address)
        plan["statements"] = ['dba.getCluster().rejoinInstance(%s, {recoveryMethod: "incremental"});' % _js(address)]
        plan["facts"] += [["Instance", "%s (%s)" % (name, address)],
                          ["Target server UUID", uuid],
                          ["Seen here as", member.get("state") or "Not reported"]]
        plan["warnings"].append("MySQL Shell reaches %s with this connection's user and password: the account "
                                "must exist there too." % address)
        plan["warnings"].append("Find out why %s left the group (its error log) before you rejoin it: MySQL Shell "
                                "refuses an instance with transactions the cluster does not have." % name)
        plan["warnings"].append("When the transactions this instance misses are no longer in any member's binary "
                                "logs, MySQL Shell refuses and nothing changes. Run cluster.rejoinInstance(%s, "
                                '{recoveryMethod: "clone"}) in MySQL Shell then.' % _js(address))
        plan["typed_guard"] = name
        plan["undo"] = "dba.getCluster().removeInstance(%s); or STOP GROUP_REPLICATION on it." % _js(address)
    # Reuse the checked object; another dba.get* call would open unchecked
    # sessions. The derived guard is shown and bound by reviewed_writes.
    plan["statements"] = [statement.replace("dba.getClusterSet()", "myflamesClusterSet").replace(
        "dba.getCluster()", "myflamesCluster") for statement in plan["statements"]]


def _plan_galera(report, plan, action, member, expect, server_name=None):
    name = server_name or member.get("name")
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
    out = {"member": _identity(member), "local": _identity(local), "primary": _identity(primary),
           "component": (report.get("cluster") or {}).get("component"),
           "topology": (report.get("cluster") or {}).get("topology"),
           "desync": galera.get("desync"), "local_state": (galera.get("metrics") or {}).get("local_state_comment")}
    cs = report.get("clusterset")
    out["cs_present"] = bool(cs) if report.get("available") else None
    if (report.get("metadata") or {}).get("schema_version") is not None:
        out["cs_metadata_server_uuid"] = (report.get("server") or {}).get("server_uuid")
    if cs:
        import copy
        out["cs_clusters"] = sorted([[c.get("name"), c.get("role"), c.get("invalidated")]
                                      for c in cs.get("clusters") or ()], key=lambda row: row[0] or "")
        out["cs_view_id"] = cs.get("view_id")
        out["cs_routing"] = copy.deepcopy({"defaults": cs.get("routing_options"), "routers": cs.get("routers")})
        out["cs_channels"] = {
            c["name"]: {key: c["channel"].get(key) for key in
                        ("source", "receiver_state", "applier_state", "last_error_number")}
            for c in cs.get("clusters") or () if c.get("channel") is not None and not c.get("local")
            and not c["channel"].get("read_error")}
        out["cs_member_states"] = {m.get("server_uuid"): m.get("state")
                                    for c in cs.get("clusters") or () for m in _clusterset_members(c)
                                    if m.get("server_uuid") and m.get("state")}
        out["cs_primary"] = cs.get("primary_cluster")
        out["cs_invalidated"] = sorted(c.get("name") or "?" for c in cs.get("clusters") or () if c.get("invalidated"))
        if cs.get("channel") is not None:
            out["cs_channel"] = {key: cs["channel"].get(key) for key in
                                 ("source", "receiver_state", "applier_state")}
    return out


def _done_text(plan, after):
    member = (after or {}).get("member") or {}
    local = (after or {}).get("local") or {}
    action = _action_name(plan["action"])[0]
    if action == "rejoin_instance":
        return "Sent. %s is %s now." % (member.get("name") or "The instance", member.get("state") or "not readable")
    if action in ("set_primary_cluster", "force_primary_cluster", "rejoin_cluster"):
        return "Sent. The primary cluster is %s as read here." % ((after or {}).get("cs_primary") or "not readable")
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


def execute_action(conn, reviewed, audit=None, typed=None, before_send=None, progress=None, target=None,
                   group_checks=None):
    """Send a reviewed plan: read the cluster again, refuse with nothing sent
    when the member, the server or the group changed, then send it.
    *progress* follows the steps for the sheet (reviewed_writes.Progress)."""
    from . import reviewed_writes
    reviewed = reviewed_writes.check_reviewed(reviewed)
    expect = reviewed.get("expect") or {}
    if audit is not None:
        audit.server(expect.get("engine"), expect.get("server_version"))
    clusterset = _action_name(reviewed["action"])[0] in CLUSTERSET_ACTIONS
    state = {"progress": progress}
    if clusterset and audit is not None:
        from .clusterset_execution import DeferredAudit
        audit = DeferredAudit(audit)

    def fresh():
        report, checks = prepare_action(conn, reviewed["action"], group_checks=group_checks,
                                        check_grants=False)
        if _server_identity(report) != {k: expect.get(k) for k in ("engine", "server_version", "server", "kind")}:
            raise ValueError("This connection now reaches another server or cluster.")
        if "routing_configuration" in expect:
            current = _clusterset_routing_configuration(report.get("clusterset") or {})
            if current is None:
                raise ValueError("Router routing could not be read after review. Review the action again.")
            # Python considers False == 0 and True == 1, but they are distinct
            # JSON values and may route differently, including in Router tags.
            current_json = json.dumps(current, sort_keys=True, separators=(",", ":"), allow_nan=False)
            reviewed_json = json.dumps(expect.get("routing_configuration"), sort_keys=True,
                                       separators=(",", ":"), allow_nan=False)
            if current_json != reviewed_json:
                raise ValueError("Router routing changed after review. Review the action again.")
        binding = target() if callable(target) else target
        plan = plan_action(report, reviewed["action"], reviewed.get("member_key"), checks, target=binding,
                           parameters=reviewed.get("parameters"))
        state["before"] = _after(report, reviewed)
        state["before_report"], state["read_options"] = report, checks.get("read_options") or {}
        state["plan"], state["shell"] = plan, _shell(checks)
        return plan

    # What the client says only picks the time limit and how a failure is
    # judged: the statements sent, and the program that sends them, come
    # from the plan made again here (a mismatch sends nothing).
    shell = reviewed.get("via") == "mysqlsh"

    def refused_text(statement, error):
        action = _action_name(reviewed["action"])[0]
        status = ("dba.getClusterSet().status()" if action in
                  ("set_primary_cluster", "force_primary_cluster", "rejoin_cluster", "switchover") else
                  "dba.getCluster().status()")
        return ("MySQL Shell refused %s: %s. No change is visible here; check %s in MySQL Shell before you try "
                "again." % (statement, error, status))

    def effect(plan, after):
        rows = _effect(plan, state.get("before") or {}, after)
        if plan.get("via") != "mysqlsh":
            return rows
        # After an AdminAPI error only what changed tells a partial change
        # from none (reviewed_writes, verify_on_error).
        return [r for r in rows if r[1] != r[2]] or None

    def read_after():
        from .clusterset_execution import verification_reads
        try:
            with verification_reads(conn, state.get("read_options"), state) as (source, options):
                if not clusterset:
                    return _after(read_status(source), reviewed)
                from .clusterset_verify import verify
                verification = verify(source, reviewed, state["before_report"], options)
        except ConnectorError:
            if state.get("workspace_shutdown"):
                return None
            raise
        state["verification"] = {k: v for k, v in verification.items() if k != "after"}
        after = _after(verification.get("after") or {}, reviewed)
        after["verification"] = state["verification"]
        return after

    result = reviewed_writes.execute(_Sender(conn, state), reviewed, fresh, ACTION_SCHEMA_VERSION, audit=audit,
                                   typed=typed, before_send=before_send,
                                   read_after=read_after,
                                   timeout=SHELL_TIMEOUT if shell else ACTION_TIMEOUT, done_text=_done_text,
                                   progress=progress, effect=effect, verify_on_error=shell,
                                   refused_text=refused_text if shell else None)
    if clusterset:
        from .clusterset_execution import finish
        return finish(result, reviewed, state, audit=audit, progress=progress)
    if shell:
        from .clusterset_execution import SHELL_SHUTDOWN_MESSAGE, bounded_output
        if state.get("workspace_shutdown"):
            result.update(status="unknown", states=["unknown"] * len(reviewed["statements"]),
                          message=SHELL_SHUTDOWN_MESSAGE, after=None)
            if audit is not None:
                audit.complete("unknown", outcome="unknown or partial", message=SHELL_SHUTDOWN_MESSAGE,
                               after={"observation": "shutdown"})
        for key in ("shell_output", "shell_stderr"):
            result[key], result[key + "_truncated"] = bounded_output(state.get(key, ""))
    return result


class _Sender(object):
    """*conn* that sends a plan through the program its fresh plan names:
    MySQL Shell (cluster_shell.ShellWriter) or the mysql client."""

    def __init__(self, conn, state):
        self._conn, self._state = conn, state

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def run_write(self, sql, extra_flags=None, timeout=None):
        plan = self._state.get("plan") or {}
        if plan.get("via") == "mysqlsh":
            from .cluster_shell import ShellWriter
            from .dbops import redact
            from .reviewed_writes import Refusal
            try:
                writer = ShellWriter(self._conn, self._state.get("shell"), progress=self._state.get("progress"))
            except (ConnectorError, ValueError, OSError) as exc:
                # Construction cannot spawn a process. Use that stage as
                # proof, rather than interpreting arbitrary error-message text.
                raise Refusal(redact(str(exc), getattr(self._conn, "password", None)) +
                              " Nothing was sent.", "shell_unavailable") from None
            try:
                limits = {"timeout": timeout}
                if plan.get("idle_timeout_s") is not None:
                    # Limits come from the freshly derived clone plan, never
                    # from a client-supplied timeout or unreviewed script text.
                    limits.update(timeout=plan["timeout_s"], idle_timeout=plan["idle_timeout_s"])
                output = writer.run_write(sql, **limits)
                self._state["shell_output"] = output
                return output
            except ConnectorError as exc:
                self._state["shell_error"] = str(exc)
                raise
            finally:
                if "shell_output" not in self._state:
                    self._state["shell_output"] = writer.stdout
                self._state["shell_stderr"] = writer.stderr
        return self._conn.run_write(sql, extra_flags=extra_flags, timeout=timeout)
