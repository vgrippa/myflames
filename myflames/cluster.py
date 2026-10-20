"""Cluster status for the connection in use: MariaDB Galera, Percona XtraDB
Cluster, MySQL InnoDB Cluster and Group Replication. Read-only.

:func:`read_status` returns the ``cluster-1.0`` report (schema in
``docs/schemas/cluster-1.0.json``): the kind of cluster this server belongs
to (``galera``, ``pxc``, ``innodb_cluster``, ``group_replication`` or
``none``), its name and UUID, one status word (``ok`` / ``watch`` /
``problem`` / ``unknown``) with the findings behind it, the members as this
server sees them, the measured metrics, and for InnoDB Cluster the metadata
(cluster name, topology mode, registered routers). Nothing is claimed that
was not measured: a MariaDB Galera member other than this node has no state
here, because the server exposes only its own (``state`` is null and the
member is not judged).

Every statement is a tagged ``SELECT`` or ``SHOW`` (``/*myflames:cluster*/``)
through :class:`myflames.perf_common.Reader`, so the same isolated client and
the read-only checks of the other Server views apply.

Verified against server source (mariadb-server 13.0 trunk "Ma/", mysql-server
9.7 trunk "M/", mysql-shell trunk "Sh/") and live (2026-10-20: MariaDB
11.8.9 with Galera 26.4.27, Percona XtraDB Cluster 8.4.11-11.1 with Galera
4.27, MySQL 8.4.11 InnoDB Cluster with MySQL Router 8.4.10; see
test/fixtures/cluster):

* Galera node detection. MariaDB has a global ``wsrep_on``, forced off
  without a provider (Ma/sql/wsrep_mysqld.cc:886-890). On Percona XtraDB
  Cluster 8.4 ``wsrep_on`` is session-only (``SELECT @@global.wsrep_on``
  fails with error 1238, live), so SHOW GLOBAL VARIABLES does not list it and
  ``wsrep_provider`` (``none`` without a provider) decides.
* Galera status variables. MariaDB defines ``wsrep_connected``,
  ``wsrep_ready``, ``wsrep_cluster_state_uuid``, ``wsrep_cluster_conf_id``,
  ``wsrep_cluster_status``, ``wsrep_cluster_size``, ``wsrep_local_index``,
  ``wsrep_local_bf_aborts`` and the ``wsrep_provider_*`` names itself
  (Ma/sql/mysqld.cc:8069-8086); the rest (``wsrep_local_state_comment``,
  ``wsrep_incoming_addresses``, ``wsrep_local_recv_queue(_avg)``,
  ``wsrep_local_send_queue(_avg)``, ``wsrep_flow_control_paused``,
  ``wsrep_flow_control_active``, ``wsrep_cert_deps_distance``,
  ``wsrep_local_cert_failures``, ``wsrep_evs_state``, ``wsrep_gcomm_uuid``,
  ``wsrep_desync_count``) come from the Galera provider and are listed by
  Ma/mysql-test/suite/wsrep/r/variables.result. They exist only while a
  provider is loaded. The same names were read live on both MariaDB and PXC.
* ``wsrep_cluster_status`` is ``Primary`` in the Synced, Joined and Donor
  server states, ``Disconnected`` when disconnected, ``non-Primary``
  otherwise (Ma/sql/wsrep_server_service.cc:358-380). ``wsrep_ready`` prints
  ON/OFF (SHOW_MY_BOOL, Ma/sql/wsrep_mysqld.cc:593-600), ``wsrep_connected``
  too (SHOW_BOOL). ``wsrep_local_state_comment`` reads ``Synced``,
  ``Donor/Desynced`` after ``SET GLOBAL wsrep_desync=1``
  (Ma/mysql-test/suite/galera/r/galera_status_local_state.result) and
  ``Initialized`` with ``Disconnected`` and ``wsrep_ready`` OFF
  (galera_var_cluster_address.result:15-24).
* ``mysql.wsrep_cluster_members`` (``node_uuid``, ``cluster_uuid``,
  ``node_name``, ``node_incoming_address``; Ma/sql/wsrep_schema.cc:70-77) is
  written only for a Primary view (Ma/sql/wsrep_server_service.cc:250-273),
  so on a non-Primary node it still lists the last Primary view
  (Ma/mysql-test/suite/galera/r/MDEV-21479.result:44-59). It is read as
  membership, never as state.
* With ``wsrep_node_incoming_address`` AUTO the incoming address is built
  from the port mysqld had when wsrep started (Ma/sql/wsrep_mysqld.cc:
  671-704), and ``wsrep_init_server`` runs before ``network_init`` sets the
  ports (Ma/sql/mysqld.cc:5285, 6211); MariaDB 11.8.9 in Docker reported
  ``ip:0``, live. Port 0 is shown as not reported.
* ``performance_schema.pxc_cluster_view`` (``HOST_NAME``, ``UUID``,
  ``STATUS``, ``LOCAL_INDEX``, ``SEGMENT``) is Percona XtraDB Cluster only,
  verified live (SHOW CREATE TABLE and rows reading ``SYNCED``). Only
  ``SYNCED`` is judged ok; any other status is watch (not Synced).
* Galera provider claims, not in the server source (the provider is a
  separate project): ``wsrep_flow_control_paused`` is the fraction of time
  replication was paused by flow control since the statistics were last
  reset or since start (FLUSH STATUS calls the provider's reset_status(),
  Ma/sql/mysqld.cc:9650-9655); the Joining and Joined state comments;
  ``wsrep_flow_control_active``; and that ``wsrep_incoming_addresses`` lists
  the nodes in ``wsrep_local_index`` order (consistent with every live
  recording). No documented threshold exists for flow control; myflames says
  watch from :data:`FLOW_CONTROL_WATCH` (0.1, as the Full analysis's
  ``checks.wsrep`` does).
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
  ps_information.cc:46-77). The group has lost its majority when members
  minus UNREACHABLE members are at most half of the members
  (member_info.cc:1450-1464). ``replication_group_member_stats`` columns:
  M/storage/perfschema/table_replication_group_member_stats.cc:159-171.
* Failures a group tolerates: (ONLINE + RECOVERING - 1) / 2, as MySQL Shell's
  ``cluster.status()`` computes it (Sh/modules/adminapi/cluster/status.cc:
  160-187).
* InnoDB Cluster metadata: ``mysql_innodb_cluster_metadata`` views
  ``schema_version``, ``v2_gr_clusters`` (``cluster_id``, ``cluster_name``,
  ``primary_mode`` 'pm' or 'mm', ``group_name``), ``v2_instances``
  (``label``, ``mysql_server_uuid``, ``endpoint``; ``instance_type`` in
  ``attributes`` from 2.2.0, where read replicas are 'read-replica') and
  ``v2_routers`` in every 2.x model (Sh/modules/adminapi/common/metadata/
  metadata-model-2.0.0.sql to 2.4.0.sql). ``primary_mode`` is as last known
  by the shell, so the live ``group_replication_single_primary_mode`` wins.
  A router's ``last_check_in``: the schema comment says hourly, but MySQL
  Router writes it every tenth metadata refresh by default (about every 5 s
  with the default 0.5 s TTL), never by default in a ClusterSet, or as its
  ``stats_updates_frequency`` option sets (M/router/src/metadata_cache/src/
  metadata_cache.cc:813-824, router_options.cc:194-205; live ages 0-7 s).
  From metadata 2.4.0 it lives in ``router_stats`` and MySQL Shell reads
  ``COALESCE(router_stats.last_check_in, routers.last_check_in)``
  (Sh/modules/adminapi/common/metadata_storage.cc:137-155). It is shown,
  not judged. Router ports come from the ``attributes`` keys MySQL Shell
  reads (``RWEndpoint``, ``ROEndpoint``, ``RWSplitEndpoint``,
  ``RWXEndpoint``, ``ROXEndpoint``; same file :107-146).
"""
import re

from .connector import ConnectorError, terminal_text
from .perf_common import Reader, _number, denied, now, preflight, reason

SCHEMA_VERSION = "cluster-1.0"
CLUSTER_TAG = "/*myflames:cluster*/ "
KINDS = ("galera", "pxc", "innodb_cluster", "group_replication", "none")
STATUSES = ("ok", "watch", "problem", "unknown")

#: wsrep_flow_control_paused at or above this fraction is "watch" (see the
#: module docstring: the documentation gives no number; 0.1 means writes
#: waited a tenth of the measured time).
FLOW_CONTROL_WATCH = 0.1

#: Variables read with the standard preflight. A name the server does not
#: have is simply absent from the answer (SHOW GLOBAL VARIABLES WHERE ... IN).
EXTRA_VARIABLES = (
    "hostname", "port", "report_host", "report_port", "server_uuid",
    "wsrep_on", "wsrep_provider", "wsrep_cluster_name", "wsrep_cluster_address", "wsrep_node_name",
    "wsrep_node_address", "wsrep_node_incoming_address", "wsrep_desync", "pxc_strict_mode", "pxc_maint_mode",
    "group_replication_group_name", "group_replication_single_primary_mode", "group_replication_local_address",
    "group_replication_group_seeds",
)

_WSREP_STATUS_SQL = "SHOW GLOBAL STATUS WHERE Variable_name LIKE 'wsrep\\_%'"
_WSREP_MEMBERS_SQL = "SELECT node_uuid, node_name, node_incoming_address FROM mysql.wsrep_cluster_members"
_PXC_VIEW_SQL = "SELECT HOST_NAME, UUID, STATUS, LOCAL_INDEX, SEGMENT FROM performance_schema.pxc_cluster_view"
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
#: Router ports, read by key so the rest of the attributes (configuration,
#: the metadata account name) never leave the server.
ROUTER_PORTS = ("RWEndpoint", "ROEndpoint", "RWSplitEndpoint", "RWXEndpoint", "ROXEndpoint")
_ROUTER_PORT_SQL = ", ".join("r.attributes->>'$.%s'" % key for key in ROUTER_PORTS)


def _routers_sql(stats):
    """v2_routers; with metadata 2.4.0+ the check-in time from router_stats
    first, as MySQL Shell reads it."""
    check_in = "COALESCE(s.last_check_in, r.last_check_in)" if stats else "r.last_check_in"
    join = (" LEFT JOIN %s.router_stats AS s ON s.router_id = r.router_id" % METADATA_SCHEMA) if stats else ""
    return ("SELECT r.router_id, r.cluster_id, r.router_name, r.product_name, r.address, r.version, %s, "
            "TIMESTAMPDIFF(SECOND, %s, NOW()), %s FROM %s.v2_routers AS r%s"
            % (check_in, check_in, _ROUTER_PORT_SQL, METADATA_SCHEMA, join))


#: Galera local states (wsrep_local_state_comment) and how myflames judges them.
GALERA_STATES = {
    "synced": ("ok", "Synced: a full member, applying and accepting writes."),
    "donor/desynced": ("watch", "Donor/Desynced: serving a state transfer, or desynchronized with wsrep_desync; "
                                "it may fall behind the cluster."),
    "joined": ("watch", "Joined: state transfer received, catching up before Synced."),
    "initialized": ("problem", "Initialized: not in a Primary component (non-Primary or disconnected); it does not "
                               "apply or accept writes."),
}
#: Group Replication member states (MEMBER_STATE).
GR_STATES = {
    "ONLINE": ("ok", "ONLINE: a full member of the group."),
    "RECOVERING": ("watch", "RECOVERING: joining the group and applying what it missed (distributed recovery)."),
    "OFFLINE": ("problem", "OFFLINE: Group Replication is not running on this member."),
    "ERROR": ("problem", "ERROR: the member left the group after an error; it does not apply or accept writes."),
    "UNREACHABLE": ("problem", "UNREACHABLE: this server cannot reach the member; it may be down or partitioned."),
    "MISSING": ("problem", "MISSING: listed in the InnoDB Cluster metadata but not in the group."),
}
_RANK = {"ok": 0, "unknown": 1, "watch": 2, "problem": 3}
PORT_ZERO_NOTE = ("%s reports port 0 for %s: with wsrep_node_incoming_address AUTO the node takes the port mysqld "
                  "had when wsrep started. Set wsrep_node_incoming_address to host:port to report the client port.")


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


def _port_zero(address):
    address = (address or "").strip()
    return bool(re.search(r":0$", address))


def _addresses(text):
    """wsrep_incoming_addresses as a list (comma-separated host:port)."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def cluster_address_nodes(address):
    """Hosts named by wsrep_cluster_address ('gcomm://a,b,c?opts'); 0 for a
    bootstrap address ('gcomm://'), None when it is not a gcomm URL."""
    match = re.match(r"^\s*gcomm://([^?]*)", address or "")
    if not match:
        return None
    return len([h for h in match.group(1).split(",") if h.strip()])


def _host_only(text):
    host, _port = _split_address(text)
    return (host or "").strip().lower()


def configured_nodes(variables):
    """Nodes wsrep_cluster_address names, counting this node once whether or
    not the list names it (a node often lists only the others). None for a
    bootstrap address ('gcomm://') or no gcomm URL."""
    address = variables.get("wsrep_cluster_address") or ""
    if not cluster_address_nodes(address):
        return None
    # Each distinct entry is a node (two instances on one host differ by
    # port); this node is counted once more only when no entry names its host.
    entries = set(h.strip().lower() for h in re.match(r"^\s*gcomm://([^?]*)", address).group(1).split(",")
                  if h.strip())
    hosts = set(_host_only(h) for h in entries)
    own = set(name for name in (_host_only(variables.get("hostname")), _host_only(variables.get("report_host")),
                                _host_only(variables.get("wsrep_node_address")),
                                (variables.get("wsrep_node_name") or "").strip().lower()) if name)
    return len(entries) + (0 if hosts & own else 1)


def is_galera_node(variables):
    """True when the server runs with a Galera provider.

    MariaDB has a global ``wsrep_on`` (ON only with a provider loaded).
    Percona XtraDB Cluster 8.4 has ``wsrep_on`` as a session-only variable, so
    SHOW GLOBAL VARIABLES does not list it (verified live on PXC 8.4.11:
    ``SELECT @@global.wsrep_on`` fails with error 1238); there the provider
    path in ``wsrep_provider`` (``none`` when not loaded) decides.
    """
    wsrep_on = _on(variables.get("wsrep_on"))
    if wsrep_on is not None:
        return wsrep_on
    provider = (variables.get("wsrep_provider") or "").strip()
    return bool(provider) and provider.lower() != "none"


def _finding(report, status, key, title, text, member=None):
    item = {"id": key, "status": status, "title": title, "text": text}
    if member:
        item["member"] = member
    report["findings"].append(item)


def _member(key, name=None, address=None, uuid=None, state=None, state_source=None, role=None, version=None,
            local=False):
    host, port = _split_address(address)
    return {"key": key, "name": name or host or address or uuid or key, "address": address, "host": host,
            "port": port, "uuid": uuid, "state": state, "state_source": state_source, "role": role,
            "version": version, "local": bool(local), "status": "unknown", "status_why": [], "metrics": {}}


def _base(server):
    variables = server.variables
    return {
        "schema_version": SCHEMA_VERSION,
        "engine": server.engine,
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
    server = preflight(reader, EXTRA_VARIABLES)
    report = _base(server)
    report["statements"].append("SHOW GLOBAL VARIABLES")
    wsrep_on = _on(server.var("wsrep_on"))
    if is_galera_node(server.variables):
        _read_galera(reader, server, report)
    elif server.engine == "mariadb":
        report["message"] = ("Not part of a cluster: wsrep_on is %s, so Galera replication is off on this MariaDB "
                             "server and its wsrep status is hidden (MariaDB has no Group Replication)."
                             % ("OFF" if wsrep_on is False else "not set"))
    else:
        _read_group_replication(reader, server, report, wsrep_on)
    _finish(report)
    return report


# ---- Galera and Percona XtraDB Cluster ------------------------------------------------

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


def _galera_state(comment):
    """(status, why) for wsrep_local_state_comment."""
    text = (comment or "").strip()
    if not text:
        return "unknown", "wsrep_local_state_comment was not reported."
    known = GALERA_STATES.get(text.lower())
    if known:
        return known
    if text.lower().startswith("joining"):
        return "watch", "%s: joining the cluster (state transfer)." % text
    return "problem", "%s: not Synced." % text


def _read_galera(reader, server, report):
    variables = server.variables
    comment = (variables.get("version_comment") or "")
    report["kind"] = "pxc" if "xtradb cluster" in comment.lower() else "galera"
    try:
        status = reader.pairs(_WSREP_STATUS_SQL)
    except ConnectorError as exc:
        if denied(exc):
            report["available"] = False
            report["message"] = "This account cannot read the wsrep status variables: %s" % reason(exc)
            return
        raise
    report["statements"].append("SHOW GLOBAL STATUS (wsrep_%)")
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
    configured = configured_nodes(variables)
    if configured:
        cluster["expected_size"] = configured
        cluster["expected_source"] = "wsrep_cluster_address"
    report["galera"] = {
        "node_name": _text(variables.get("wsrep_node_name")),
        "node_address": _text(variables.get("wsrep_node_address")),
        "cluster_address": _text(variables.get("wsrep_cluster_address")),
        "incoming_addresses": _addresses(status.get("wsrep_incoming_addresses")),
        "gcomm_uuid": _text(status.get("wsrep_gcomm_uuid")),
        "desync": _text(variables.get("wsrep_desync")),
        "pxc_strict_mode": _text(variables.get("pxc_strict_mode")),
        "pxc_maint_mode": _text(variables.get("pxc_maint_mode")),
        "members_source": None,
        "metrics": metrics,
    }
    if not status:
        report["notes"].append("The server reported no wsrep status variables, so the Galera provider may not be "
                               "loaded (wsrep_provider is %s)." % (variables.get("wsrep_provider") or "not set"))
    _galera_members(reader, report, metrics)
    _judge_galera(report, metrics)


def _galera_members(reader, report, metrics):
    galera = report["galera"]
    notes = report["notes"]
    primary = report["cluster"]["component"] == "Primary"
    table = reader.optional(_WSREP_MEMBERS_SQL, "mysql.wsrep_cluster_members", notes)
    if table is not None:
        report["statements"].append("SELECT mysql.wsrep_cluster_members")
    pxc = None
    if report["kind"] == "pxc":
        pxc = reader.optional(_PXC_VIEW_SQL, "performance_schema.pxc_cluster_view", notes)
        if pxc is not None:
            report["statements"].append("SELECT performance_schema.pxc_cluster_view")
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
        address = "%s:%s" % (own["hostname"], own["port"] or 3306) if own["hostname"] else None
        members.insert(0, _member("node:local", node_name or galera["node_address"] or "This server", address,
                                  gcomm, local=True))
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
                member["state"] = state
                member["state_source"] = "pxc_cluster_view"
    zero = [m["name"] for m in members if _port_zero(m["address"])]
    if zero:
        notes.append(PORT_ZERO_NOTE % ("mysql.wsrep_cluster_members" if galera["members_source"] ==
                                       "wsrep_cluster_members" else "wsrep_incoming_addresses",
                                       ", ".join(zero)))
    report["members"] = members


def _judge_galera(report, metrics):
    cluster = report["cluster"]
    galera = report["galera"]
    component = cluster["component"]
    if component and component != "Primary":
        _finding(report, "problem", "non_primary", "Not in a Primary component",
                 "wsrep_cluster_status is %s: this node lost quorum or is not connected. Until it rejoins a "
                 "Primary component it refuses most queries (SET, SHOW, reads of no table and reads with "
                 "wsrep_dirty_reads still run)." % component)
    if metrics["connected"] is False and component == "Primary":
        _finding(report, "problem", "not_connected", "Not connected",
                 "wsrep_connected is OFF: this node has no group communication with the other nodes.")
    if metrics["ready"] is False and component == "Primary":
        # Ready is set when the node reaches Synced (wsrep_server_service.cc
        # log_state_change), so a joining node is expected to be not ready.
        joining = _galera_state(metrics["local_state_comment"])[0] == "watch"
        _finding(report, "watch" if joining else "problem", "not_ready", "Not ready",
                 "wsrep_ready is OFF: this node rejects almost every query%s."
                 % (" until it is Synced" if joining else ""))
    for member in report["members"]:
        if member["local"]:
            member["state"] = metrics["local_state_comment"]
            member["state_source"] = "wsrep_local_state_comment" if metrics["local_state_comment"] else None
            member["metrics"] = dict((k, v) for k, v in metrics.items() if v is not None)
            status, why = _galera_state(metrics["local_state_comment"])
            if component and component != "Primary":
                status, why = "problem", "In a %s component. %s" % (component, why)
            member["status"], member["status_why"] = status, [why]
            if status in ("watch", "problem") and not (component and component != "Primary"):
                _finding(report, status, "local_state", "This node is %s" % (metrics["local_state_comment"] or "?"),
                         why, member["key"])
            report["local"] = member["key"]
        elif member["state_source"] == "pxc_cluster_view":
            status, why = _pxc_state(member["state"])
            member["status"], member["status_why"] = status, [why]
            if status in ("watch", "problem"):
                _finding(report, status, "member_state", "%s is %s" % (member["name"], member["state"]), why,
                         member["key"])
        else:
            member["status_why"] = ["In the component as this node sees it. A node's own state is read on that "
                                    "node: connect to it to judge it."]
    size, expected = cluster["size"], cluster["expected_size"]
    if size is not None and expected and size < expected and component == "Primary":
        _finding(report, "watch", "size_below_configured", "Fewer nodes than configured",
                 "The component has %d %s; wsrep_cluster_address names %d, counting this one. A node may be down "
                 "or still joining (the addresses are the nodes this one contacts, not a required size)."
                 % (size, "node" if size == 1 else "nodes", expected))
    paused = metrics["flow_control_paused"]
    if paused is not None and paused >= FLOW_CONTROL_WATCH:
        _finding(report, "watch", "flow_control", "Flow control paused replication",
                 "wsrep_flow_control_paused is %.2f: replication was paused %d%% of the time since the statistics "
                 "were last reset (FLUSH STATUS) or since start." % (paused, round(paused * 100)))
    maint = galera.get("pxc_maint_mode")
    if report["kind"] == "pxc" and maint and maint.upper() != "DISABLED":
        _finding(report, "watch", "pxc_maint_mode", "Maintenance mode",
                 "pxc_maint_mode is %s: a proxy that reads it stops sending new traffic to this node." % maint)
    if paused is not None:
        report["notes"].append("wsrep_flow_control_paused and the queue averages cover the time since the "
                               "provider statistics were last reset (FLUSH STATUS) or since start.")
    if any(m["state_source"] == "pxc_cluster_view" for m in report["members"]):
        report["notes"].append("The other nodes' states come from performance_schema.pxc_cluster_view on this "
                               "node; this node's own from wsrep_local_state_comment.")
    elif len(report["members"]) > 1:
        report["notes"].append("Galera exposes only this node's own state: the other nodes are listed as members "
                               "of the component without a verdict. Connect to a node to judge it.")


def _pxc_state(state):
    text = (state or "").strip()
    if not text:
        return "unknown", "No state reported."
    if text.upper() == "SYNCED":
        return "ok", "SYNCED: a full member."
    return "watch", "%s: not Synced (joining, a state transfer, or desynchronized)." % text


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
        address = "%s:%s" % (host, port) if host and port else host
        member = _member("member:%s" % (member_id or address), host, address, member_id, _text(row[4]),
                         "replication_group_members", _text(row[5]), _text(row[6]),
                         local=bool(uuid and member_id == uuid))
        if len(columns) > 7:
            member["communication_stack"] = _text(row[7])
        members.append(member)
    local = next((m for m in members if m["local"]), None)
    if not members or (len(members) == 1 and local is not None and local["state"] == "OFFLINE" and not group_name):
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


def _read_metadata(reader, report, group_name, local):
    notes = report["notes"]
    found = reader.optional(_MD_SCHEMA_SQL, "information_schema.SCHEMATA", notes)
    if not found:
        report["metadata"] = {"available": False, "schema_version": None, "cluster_id": None, "cluster_name": None,
                              "primary_mode": None, "instances": [],
                              "reason": "No mysql_innodb_cluster_metadata schema is visible to this account, so "
                                        "this is a Group Replication group without InnoDB Cluster metadata (or "
                                        "the account cannot read it)."}
        return
    metadata = {"available": True, "schema_version": None, "cluster_id": None, "cluster_name": None,
                "primary_mode": None, "instances": [], "reason": None}
    report["metadata"] = metadata
    version = reader.optional(_MD_VERSION_SQL, "the InnoDB Cluster metadata version", notes)
    if version:
        metadata["schema_version"] = ".".join(str(v) for v in version[0][:3])
        if metadata["schema_version"] == "0.0.0":
            notes.append("The InnoDB Cluster metadata schema version is 0.0.0, which MySQL Shell sets while it "
                         "upgrades the metadata.")
    clusters = reader.optional(_MD_CLUSTERS_SQL, "the InnoDB Cluster metadata (v2_gr_clusters)", notes)
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
    row = match[0]
    metadata["cluster_id"], metadata["cluster_name"], metadata["primary_mode"] = (
        _text(row[0]), _text(row[1]), _text(row[2]))
    report["kind"] = "innodb_cluster"
    report["cluster"]["name"] = metadata["cluster_name"]
    if report["cluster"]["topology"] is None and metadata["primary_mode"] in ("pm", "mm"):
        report["cluster"]["topology"] = "single_primary" if metadata["primary_mode"] == "pm" else "multi_primary"
    instances = reader.optional(_MD_INSTANCES_SQL, "the InnoDB Cluster metadata (v2_instances)", notes) or []
    for inst in instances:
        inst = list(inst) + [None] * (7 - len(inst))
        if _text(inst[1]) != metadata["cluster_id"]:
            continue
        metadata["instances"].append({"label": _text(inst[2]), "server_uuid": _text(inst[3]),
                                      "address": _text(inst[4]), "endpoint": _text(inst[5]),
                                      "instance_type": _text(inst[6])})
    known = dict((m["uuid"], m) for m in report["members"] if m["uuid"])
    # Shell's cluster.status() calls a group member that is in the metadata
    # but not in the group (MISSING). A member that is itself OFFLINE or in
    # ERROR sees no group, so it cannot tell.
    blind = local is None or local["state"] in ("OFFLINE", "ERROR")
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
            replica = _member("member:%s" % (inst["server_uuid"] or inst["label"]), inst["label"],
                              inst["endpoint"] or inst["address"], inst["server_uuid"], None, "metadata",
                              "READ_REPLICA")
            replica["in_metadata"] = True
            replica["instance_type"] = "read-replica"
            report["members"].append(replica)
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
    stats = _version(metadata["schema_version"]) >= (2, 4, 0)
    routers = reader.optional(_routers_sql(stats), "the InnoDB Cluster metadata (v2_routers)", notes) or []
    for r in routers:
        r = list(r) + [None] * (8 + len(ROUTER_PORTS) - len(r))
        if _text(r[1]) not in (None, metadata["cluster_id"]):
            continue
        report["routers"].append(_router(r))


def _router(row):
    ports = {}
    for key, value in zip(ROUTER_PORTS, row[8:8 + len(ROUTER_PORTS)]):
        if _text(value):
            ports[key] = str(value)
    return {"id": _number(row[0]), "name": _text(row[2]), "product": _text(row[3]), "address": _text(row[4]),
            "version": _text(row[5]), "last_check_in": _text(row[6]), "last_check_in_age_s": _number(row[7]),
            "ports": ports}


def _judge_group(report, local):
    members = report["members"]
    in_group = [m for m in members if m["state_source"] == "replication_group_members"]
    cluster = report["cluster"]
    cluster["size"] = len(in_group)
    local_state = local["state"] if local else None
    if local is None:
        report["notes"].append("This server's own row (server_uuid) was not among the group members.")
    if local_state in ("OFFLINE", "ERROR"):
        cluster["quorum"] = None
        _finding(report, "problem", "local_not_in_group", "This member is %s" % local_state,
                 GR_STATES[local_state][1].split(": ", 1)[1] + " The group's state is not visible from here: "
                 "connect to an ONLINE member.", local["key"])
    else:
        unreachable = len([m for m in in_group if m["state"] == "UNREACHABLE"])
        cluster["quorum"] = (len(in_group) - unreachable) > len(in_group) // 2
        if not cluster["quorum"]:
            _finding(report, "problem", "no_quorum", "No quorum",
                     "%d of %d members are UNREACHABLE from this member, so the group has lost its majority and "
                     "cannot process writes." % (unreachable, len(in_group)))
        alive = len([m for m in in_group if m["state"] in ("ONLINE", "RECOVERING")])
        cluster["failures_tolerated"] = (alive - 1) // 2 if alive > 0 else 0
        if cluster["quorum"] and cluster["failures_tolerated"] == 0:
            _finding(report, "watch", "no_tolerance", "Cannot tolerate a failure",
                     "With %d ONLINE or RECOVERING %s, losing one more member loses the majority."
                     % (alive, "member" if alive == 1 else "members"))
    for member in members:
        if member.get("instance_type") == "read-replica":
            member["status_why"] = ["A read replica in the InnoDB Cluster metadata: it replicates from the "
                                    "cluster asynchronously and is not a group member. Its state is read on "
                                    "that instance."]
            continue
        known = GR_STATES.get((member["state"] or "").upper())
        if member["state"] is None and member["state_source"] == "metadata":
            member["status_why"] = ["In the InnoDB Cluster metadata. This member is not in the group, so it "
                                    "cannot see whether that instance is."]
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
        if member["local"] and local_state in ("OFFLINE", "ERROR"):
            continue
        if member["status"] != "ok":
            detail = known[1].split(": ", 1)[1]
            _finding(report, member["status"], "member_state", "%s is %s" % (member["name"], member["state"]),
                     detail[0].upper() + detail[1:], member["key"])
    versions = sorted(set(m["version"] for m in in_group if m["version"]))
    if len(versions) > 1:
        _finding(report, "watch", "version_mismatch", "Members run different versions",
                 "Member versions: %s. Expected during a rolling upgrade. Until it ends, a new primary is "
                 "chosen among the lowest-version members, and in multi-primary mode higher-version members "
                 "stay read-only unless every version is in the same LTS series." % ", ".join(versions))
    expected = cluster["expected_size"]
    if expected and len(in_group) < expected and local_state not in ("OFFLINE", "ERROR") \
            and not any(m["state"] == "MISSING" for m in members):
        _finding(report, "watch", "size_below_configured", "Fewer members than configured",
                 "The group has %d %s; the InnoDB Cluster metadata lists %d."
                 % (len(in_group), "member" if len(in_group) == 1 else "members", expected))
    if local is not None:
        report["local"] = local["key"]
    primaries = [m for m in in_group if m["role"] == "PRIMARY"]
    if cluster["topology"] == "single_primary" and local_state not in ("OFFLINE", "ERROR") and cluster["quorum"] \
            and len(primaries) != 1:
        _finding(report, "problem" if not primaries else "watch", "primary_count",
                 "No primary" if not primaries else "More than one primary",
                 "Single-primary mode, and %d members report the PRIMARY role." % len(primaries))


# ---- summary, text -----------------------------------------------------------------

def _natural(text):
    """Sort key that puts node-2 before node-10."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text or "")]


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
    report["status"] = status
    report["findings"].sort(key=lambda f: -_RANK[f["status"]])
    if report["findings"]:
        report["status_why"].extend(f["text"] for f in report["findings"])
    elif status == "ok":
        report["status_why"].append("Every judged member is healthy and the cluster has quorum.")
    else:
        report["status_why"].append("Nothing could be judged from this server.")


def at_or_worse(report, level):
    """Findings at *level* ('watch' or 'problem') or worse, for --fail-on."""
    floor = _RANK[level]
    return [f for f in report.get("findings") or () if _RANK.get(f["status"], 0) >= floor]


KIND_LABELS = {"galera": "MariaDB Galera Cluster", "pxc": "Percona XtraDB Cluster",
               "innodb_cluster": "MySQL InnoDB Cluster", "group_replication": "MySQL Group Replication",
               "none": "Not part of a cluster"}


def format_text(report):
    """Human text for a cluster-1.0 report (also the MCP digest)."""
    out = []
    engine = "MariaDB" if report.get("engine") == "mariadb" else "MySQL"
    if not report.get("available"):
        return terminal_text(report.get("message") or "Cluster status is not available.") + "\n"
    if report.get("kind") == "none":
        return terminal_text("%s %s: %s" % (engine, report.get("server_version") or "?",
                                            report.get("message") or "Not part of a cluster.")) + "\n"
    cluster = report["cluster"]
    head = "%s %s  [%s]" % (KIND_LABELS[report["kind"]], cluster.get("name") or "(unnamed)", report["status"])
    out.append(head)
    facts = []
    if cluster.get("component"):
        facts.append("component %s" % cluster["component"])
    if cluster.get("quorum") is not None:
        facts.append("quorum %s" % ("yes" if cluster["quorum"] else "NO"))
    if cluster.get("size") is not None:
        facts.append("size %s%s" % (cluster["size"], " of %s" % cluster["expected_size"]
                                    if cluster.get("expected_size") else ""))
    if cluster.get("topology"):
        facts.append(cluster["topology"].replace("_", "-"))
    if cluster.get("failures_tolerated") is not None:
        facts.append("tolerates %d failure%s" % (cluster["failures_tolerated"],
                                                 "" if cluster["failures_tolerated"] == 1 else "s"))
    out.append("  %s %s; %s" % (engine, report.get("server_version") or "?", ", ".join(facts) or "no facts"))
    if cluster.get("uuid"):
        out.append("  uuid %s" % cluster["uuid"])
    out.append("")
    out.append("Members:")
    for m in report["members"]:
        line = "  %s%s  %s  %s" % ("* " if m["local"] else "  ", m["name"], m.get("address") or "?",
                                   m.get("state") or "(state not readable here)")
        if m.get("role"):
            line += "  " + m["role"]
        if m.get("version"):
            line += "  " + m["version"]
        line += "  [%s]" % m["status"]
        out.append(line)
    galera = report.get("galera")
    if galera:
        mt = galera["metrics"]
        out.append("")
        out.append("This node: recv queue %s (avg %s), send queue %s (avg %s), flow control paused %s, "
                   "cert deps distance %s, cert failures %s, BF aborts %s" % tuple(
                       "?" if mt.get(k) is None else mt[k] for k in (
                           "local_recv_queue", "local_recv_queue_avg", "local_send_queue", "local_send_queue_avg",
                           "flow_control_paused", "cert_deps_distance", "local_cert_failures", "local_bf_aborts")))
    if report.get("routers"):
        out.append("")
        out.append("Routers:")
        for r in report["routers"]:
            ports = ", ".join("%s %s" % (k, r["ports"][k]) for k in ROUTER_PORTS if k in r["ports"])
            out.append("  %s  %s  %s%s" % (r.get("name") or r.get("address") or "?", r.get("version") or "?",
                                           ports or "ports not recorded",
                                           "  last check-in %s" % r["last_check_in"] if r.get("last_check_in")
                                           else ""))
    if report["findings"]:
        out.append("")
        out.append("Findings:")
        for f in report["findings"]:
            out.append("  [%s] %s: %s" % (f["status"], f["title"], f["text"]))
    return terminal_text("\n".join(out)) + "\n"
