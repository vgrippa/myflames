"""Server answers for the cluster view (myflames/cluster.py, cluster-1.0).

Three sources, in order of preference:

* **Recorded**: ``test/fixtures/cluster/cluster-<name>.json`` hold every
  statement one live read sent and the ``mysql --batch`` text it got back, in
  the replay shape of ``test/fixtures/perf`` (``{server, source, args, calls,
  report}``). Recorded 2026-10-20 from throwaway Docker clusters (MariaDB
  11.8.9 with Galera 26.4.27, Percona XtraDB Cluster 8.4.11-11.1, MySQL
  8.4.11 InnoDB Cluster with MySQL Router 8.4.10) with
  ``scripts/record-cluster-fixtures.py`` (see test/README.md, Cluster
  matrix): healthy, a donor (wsrep_desync),
  a node stopped (two of three), an isolated node (non-Primary), a member
  with Group Replication stopped (MISSING from the primary, OFFLINE on
  itself) and an UNREACHABLE member (paused, with
  group_replication_member_expel_timeout raised to 60 for the recording).
  :func:`recorded` replays one.
* **Derived**: :data:`DERIVED` turns a healthy recording into a state the
  recordings do not show (another PXC node not Synced, maintenance mode, a
  RECOVERING member with queues, no quorum) by editing the recorded
  answers, so names and formats stay the server's own. Each edit follows the source cited in cluster.py:
  ``wsrep_cluster_status`` values from Ma/sql/wsrep_server_service.cc:358-380,
  ``Donor/Desynced`` from galera_status_local_state.result, Group
  Replication states and roles from M/plugin/group_replication/src/
  member_info.cc. A recording of the same state in
  ``test/fixtures/cluster/`` replaces its derived twin automatically
  (:func:`conn`).
* **Synthetic**: :data:`SCENARIOS` answer by statement shape for servers the
  matrix does not run (a standalone server, Performance Schema off, a denied
  account, a multi-primary group, a read replica, metadata 2.4.0).
"""
import copy
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from myflames.connector import ConnectorError  # noqa: E402
from _perf_replay import ReplayConn, _Refused, batch  # noqa: E402


def refused(message):
    """An edit result that ReplayConn raises as the server's refusal."""
    return _Refused(message)

FIXTURES = os.path.join(HERE, "fixtures", "cluster")


def fixture_path(name):
    return os.path.join(FIXTURES, "cluster-%s.json" % name)


def has_recording(name):
    return os.path.exists(fixture_path(name))


def load(name):
    with open(fixture_path(name), encoding="utf-8") as handle:
        return json.load(handle)


def recordings():
    """Names of every recorded fixture."""
    return sorted(f[len("cluster-"):-len(".json")] for f in os.listdir(FIXTURES)
                  if f.startswith("cluster-") and f.endswith(".json"))


def _version(data):
    for call in data["calls"]:
        if "SHOW GLOBAL VARIABLES" in call[0] and call[1]:
            for line in call[1].split("\n"):
                if line.startswith("version\t"):
                    return line.split("\t", 1)[1]
    return ""


#: The InnoDB ClusterSet read (cluster.py _read_clusterset) came after the
#: recordings. None of the recorded clusters is in a ClusterSet, and the
#: metadata view lists only clusters that are (v2_cs_members: WHERE
#: c.clusterset_id IS NOT NULL, Sh/modules/adminapi/common/metadata/
#: metadata-model-2.1.0.sql:606-631), so its answer there is no rows. Supplied
#: here, never written into a recording.
CLUSTERSET_SQL = ("/*myflames:cluster*/ SELECT m.clusterset_id, m.cluster_id, m.cluster_name, m.member_role, "
                  "m.invalidated, s.domain_name, s.view_id, s.router_options FROM "
                  "mysql_innodb_cluster_metadata.v2_cs_members m JOIN "
                  "mysql_innodb_cluster_metadata.v2_cs_clustersets s ON s.clusterset_id = m.clusterset_id")

# Synthetic catalog inputs, not part of the historical live captures. Keep
# their admin behavior explicit while privilege-denial tests supply their own
# grants. ALL expands only static privileges, so dynamic grants are separate.
ADMIN_GRANTS = [
    "GRANT ALL PRIVILEGES ON *.* TO `mf_admin`@`localhost` WITH GRANT OPTION",
    "GRANT CLONE_ADMIN, CONNECTION_ADMIN, GROUP_REPLICATION_ADMIN, PERSIST_RO_VARIABLES_ADMIN, "
    "REPLICATION_APPLIER, REPLICATION_SLAVE_ADMIN, ROLE_ADMIN, SYSTEM_VARIABLES_ADMIN, "
    "TRANSACTION_GTID_TAG ON *.* TO `mf_admin`@`localhost` WITH GRANT OPTION",
]


def _with_clusterset_answer(calls):
    if any(c[0] == CLUSTERSET_SQL for c in calls) or \
            not any("mysql_innodb_cluster_metadata.v2_gr_clusters" in c[0] for c in calls):
        return calls
    return list(calls) + [[CLUSTERSET_SQL, ""]]


def recorded(name, edit=None):
    """(ReplayConn, data) for a recorded fixture; *edit(sql, text, n)* may
    change an answer."""
    data = load(name)
    from myflames import topology_links as tl
    # Explicit synthetic denial: these captures predate local client footprints.
    # The catalog now reads optional bootstrap/GC variables in the same
    # preflight. Adapt only the SELECT list in older captures, preserving
    # their answers (absence is unknown, never a fabricated OFF value).
    from myflames.cluster import CLUSTER_TAG, EXTRA_VARIABLES, _CURRENT_USER_MARKER, _preflight_variables_sql
    from myflames.perf_common import PREFLIGHT_VARIABLES
    preflight_sql = "/*myflames:cluster*/ SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)" % ", ".join(
        "'%s'" % n for n in sorted(set(PREFLIGHT_VARIABLES + EXTRA_VARIABLES)))
    cluster_preflight_sql = CLUSTER_TAG + _preflight_variables_sql()
    maria = "mariadb" in _version(data).lower()
    calls = []
    for call in data["calls"]:
        if call[0].startswith("/*myflames:cluster*/ SHOW GLOBAL VARIABLES WHERE"):
            var_text = set_pairs(call[1], {"server_id": "1"}) if maria else call[1]
            calls.append([preflight_sql, var_text] + call[2:])
            calls.append([cluster_preflight_sql,
                          var_text + batch([[_CURRENT_USER_MARKER, "mf_admin@localhost"]])] + call[2:])
        else:
            calls.append(call)
    from _grants_fixtures import grants_batch
    from myflames import dbops
    calls = _with_clusterset_answer(calls) + [
        [tl.TOPOLOGY_TAG + tl._LIVE_ATTRS_SQL, None, 'ERROR 1142: SELECT command denied'],
        [tl.TOPOLOGY_TAG + tl._HEARTBEAT_TABLE_SQL, ''],
        ["/*myflames:cluster*/ SHOW GRANTS", batch([[line] for line in ADMIN_GRANTS])],
        [dbops._grants_read_sql("/*myflames:cluster*/ "), grants_batch(ADMIN_GRANTS, version=_version(data))]]
    def replay_edit(sql, text, n):
        # Topology edits target recorded two-column variables/status rows.
        # The framed catalog reply is synthetic and must retain its markers.
        if edit is None or "myflames_grants_begin" in sql:
            return text
        return edit(sql, text, n)
    conn = ReplayConn(calls, edit=replay_edit, version=_version(data))
    recorded_run = conn.run

    def run(sql, extra_flags=None, timeout=None):
        """Replay one guarded metadata snapshot using the recorded projections.

        These captures predate ClusterSet; the fixture already supplements an
        empty membership result. Do not invent a live ClusterSet or split a
        snapshot into separately recorded calls.
        """
        from myflames import cluster
        groups = cluster._CLUSTERSET_SNAPSHOT_QUERIES[3][1]
        if sql == cluster.CLUSTER_TAG + groups:
            # Standalone cluster reads use the same lossless name projection
            # as ClusterSet snapshots. These old captures retain raw names.
            text = recorded_run(cluster.CLUSTER_TAG + cluster._MD_CLUSTERS_SQL,
                                extra_flags=extra_flags, timeout=timeout)
            conn.calls[-1] = (sql, list(extra_flags or []))
            rows = parse(text)
            for row in rows:
                if row[1] is not None:
                    row[1] = '=' + row[1]
            return batch(rows)
        prefix = cluster.CLUSTER_TAG + 'SELECT @@GLOBAL.server_uuid; '
        statements = [part.strip() for part in sql[len(prefix):].split(';')] if sql.startswith(prefix) else []
        if statements[:2] != ['SET TRANSACTION ISOLATION LEVEL REPEATABLE READ',
                              'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY']:
            return recorded_run(sql, extra_flags=extra_flags, timeout=timeout)
        conn.calls.append((sql, list(extra_flags or [])))
        if conn.error is not None:
            raise conn.error
        if conn.clock is not None:
            conn.clock.now += conn.clock.query_s

        def answer(query, advance=True):
            answers = conn.answers.get(query)
            if not answers:
                raise AssertionError('missing recorded snapshot projection: ' + query)
            n = conn.seen.get(query, 0)
            if advance:
                conn.seen[query] = n + 1
            else:
                n = max(0, n - 1)
            text = answers[min(n, len(answers) - 1)]
            if conn.edit is not None:
                text = conn.edit(query, text, n)
            if isinstance(text, _Refused):
                raise ConnectorError(str(text))
            return text

        variables = dict((row[0], row[1]) for row in parse(answer(cluster_preflight_sql, advance=False))
                         if len(row) >= 2)
        result = [batch([[variables.get('server_uuid')]])]
        if not statements or statements[-1] != 'COMMIT':
            raise AssertionError('snapshot was not committed: ' + sql)
        projections = {item[1] for item in cluster._CLUSTERSET_SNAPSHOT_QUERIES}
        markers = {"SELECT '__myflames_clusterset_%s__'" % item[0] for item in cluster._CLUSTERSET_SNAPSHOT_QUERIES}
        for statement in statements[2:-1]:
            if statement in markers:
                result.append(batch([[statement[len("SELECT '"):-1]]]))
            elif statement == cluster._CS_ROUTERS_SQL:
                # No captured server belonged to a ClusterSet.
                result.append('')
            elif statement in projections:
                membership = cluster._CLUSTERSET_SNAPSHOT_QUERIES[1][1]
                groups = cluster._CLUSTERSET_SNAPSHOT_QUERIES[3][1]
                original = (cluster._MD_CLUSTERSET_SQL if statement == membership else
                            cluster._MD_CLUSTERS_SQL if statement == groups else statement)
                output = answer(cluster.CLUSTER_TAG + original)
                if statement in (membership, groups):
                    rows = parse(output)
                    columns = (2, 5) if statement == membership else (1,)
                    for row in rows:
                        for column in columns:
                            if row[column] is not None:
                                row[column] = '=' + row[column]
                    output = batch(rows)
                result.append(output)
            else:
                raise AssertionError('unexpected snapshot statement: ' + statement)
        return ''.join(result)

    conn.run = run
    return conn, data


# ---- editing recorded answers --------------------------------------------------

def parse(text):
    """``mysql --batch`` text as rows (cells kept as text, NULL as None)."""
    rows = []
    for line in (text or "").split("\n"):
        if line:
            rows.append([None if cell == "NULL" else cell for cell in line.split("\t")])
    return rows


def set_pairs(text, updates):
    """SHOW output with *updates* applied (a None value removes the name)."""
    rows = parse(text)
    seen = set()
    out = []
    for row in rows:
        if row[0] in updates:
            seen.add(row[0])
            if updates[row[0]] is None:
                continue
            row = [row[0], updates[row[0]]]
        out.append(row)
    out.extend([k, v] for k, v in sorted(updates.items()) if k not in seen and v is not None)
    return batch(out)


def editor(*rules):
    """An edit(sql, text, n) applying *rules*: (fragment of the SQL, fn(text))."""
    def edit(sql, text, n):
        for fragment, fn in rules:
            if fragment in sql:
                text = fn(text)
        return text
    return edit


def pairs(**updates):
    return lambda text: set_pairs(text, updates)


def rows(fn):
    """Rewrite the rows of an answer with fn(list of rows) -> list of rows."""
    return lambda text: batch(fn(parse(text)))


WSREP = "LIKE 'wsrep"
VARIABLES = "SHOW GLOBAL VARIABLES"
WSREP_MEMBERS = "mysql.wsrep_cluster_members"
PXC_VIEW = "pxc_cluster_view"
GR_MEMBERS = "FROM performance_schema.replication_group_members"
GR_STATS = "replication_group_member_stats"


def _gr_state(index, state, role=None):
    """Member *index* (0-based, recorded order) in *state*. OFFLINE and ERROR
    have no role (member_info.cc:620-630); UNREACHABLE only overrides the
    state, so the member keeps the role of its status (ps_information.cc:
    121-138, and the live ic-8.4-unreachable recording)."""
    def fn(rs):
        rs = [list(r) for r in rs]
        rs[index][4] = state
        if state in ("OFFLINE", "ERROR"):
            rs[index][5] = ""
        elif role is not None:
            rs[index][5] = role
        return rs
    return fn


def _primary_index(data):
    for call in data["calls"]:
        if GR_MEMBERS in call[0]:
            return next(i for i, r in enumerate(parse(call[1])) if r[5] == "PRIMARY")
    raise AssertionError("no PRIMARY in the recording")


def _local_index(data):
    uuid = None
    for call in data["calls"]:
        if VARIABLES in call[0]:
            uuid = dict((r[0], r[1]) for r in parse(call[1])).get("server_uuid")
        if GR_MEMBERS in call[0]:
            return next(i for i, r in enumerate(parse(call[1])) if r[1] == uuid)
    raise AssertionError("this server is not in the recording")


def _others(data):
    """Indexes of the recorded members other than the connected one."""
    local = _local_index(data)
    for call in data["calls"]:
        if GR_MEMBERS in call[0]:
            return [i for i in range(len(parse(call[1]))) if i != local]
    return []


def _local_primary(data):
    """A rule making the connected member the PRIMARY and the others
    SECONDARY (any ONLINE member can be elected), so a derived state that
    takes members away never removes the primary by accident."""
    local = _local_index(data)

    def fn(rs):
        rs = [list(r) for r in rs]
        for i, r in enumerate(rs):
            if r[4] in ("ONLINE", "RECOVERING"):
                r[5] = "PRIMARY" if i == local else "SECONDARY"
        return rs
    return (GR_MEMBERS, rows(fn))


def _ic_degraded(data):
    """One secondary is RECOVERING with queued transactions, another member
    is UNREACHABLE; the group keeps its majority."""
    first, second = _others(data)[:2]
    uuid = parse([c for c in data["calls"] if GR_MEMBERS in c[0]][0][1])[first][1]
    return editor(_local_primary(data), (GR_MEMBERS, rows(lambda rs: _gr_state(second, "UNREACHABLE")(
        _gr_state(first, "RECOVERING", "SECONDARY")(rs)))),
        (GR_STATS, rows(lambda rs: [r[:2] + ["4", r[3], r[4], r[5], "12"] + r[7:] if r[0] == uuid else r
                                    for r in rs])))


def _ic_no_quorum(data):
    first, second = _others(data)[:2]
    return editor(_local_primary(data),
                  (GR_MEMBERS, rows(lambda rs: _gr_state(second, "UNREACHABLE")(_gr_state(first, "UNREACHABLE")(rs)))))


def _pxc_member_donor(data):
    """Another node serves a state transfer: pxc_cluster_view shows it DONOR."""
    def fn(rs):
        rs = [list(r) for r in rs]
        rs[-1][2] = "DONOR"
        return rs
    return editor((PXC_VIEW, rows(fn)))


def _pxc_maint(data):
    return editor((VARIABLES, pairs(pxc_maint_mode="MAINTENANCE")))


def _pxc_member(status):
    """Another node in pxc_cluster_view STATUS *status* (the Galera
    provider's node states: SYNCED, DONOR, JOINER, JOINED, PRIMARY,
    NON-PRIMARY; all seen live in the lab's pxc_cluster_view)."""
    def factory(data):
        def fn(rs):
            rs = [list(r) for r in rs]
            rs[-1][2] = status
            return rs
        return editor((PXC_VIEW, rows(fn)))
    return factory


def _galera_address(address, **extra):
    """wsrep_cluster_address (and other variables) as another deployment
    writes it; wsrep_node_address stays empty, MariaDB's default
    (Ma/sql/sys_vars.cc:6423-6431)."""
    def factory(data):
        return editor((VARIABLES, pairs(wsrep_cluster_address=address, **extra)))
    return factory


#: The wsrep status names MariaDB still lists while the global wsrep_on is
#: OFF: the server's own (Ma/sql/mysqld.cc:8070-8085); the provider's are
#: replaced by a bare "wsrep" row (wsrep_var.cc wsrep_show_status), which
#: LIKE 'wsrep\_%' does not match.
SERVER_WSREP_NAMES = ("wsrep_connected", "wsrep_ready", "wsrep_cluster_state_uuid", "wsrep_cluster_conf_id",
                      "wsrep_cluster_status", "wsrep_cluster_size", "wsrep_local_index", "wsrep_local_bf_aborts",
                      "wsrep_provider_name", "wsrep_provider_version", "wsrep_provider_vendor",
                      "wsrep_provider_capabilities", "wsrep_thread_count", "wsrep_applier_thread_count",
                      "wsrep_rollbacker_thread_count", "wsrep_cluster_capabilities")


def _galera_wsrep_off(data):
    """SET GLOBAL wsrep_on=OFF on a joined node (Ma/sql/wsrep_var.cc
    wsrep_on_check allows it and keeps the provider connected)."""
    return editor((VARIABLES, pairs(wsrep_on="OFF")),
                  (WSREP, rows(lambda rs: [r for r in rs if r[0] in SERVER_WSREP_NAMES])))


def _galera_joiner(data):
    """A node receiving its state transfer: the server state is s_joiner, so
    wsrep_cluster_status reads non-Primary and wsrep_ready OFF
    (Ma/sql/wsrep_server_service.cc:356-380), connected, Joining."""
    return editor((WSREP, pairs(wsrep_cluster_status="non-Primary", wsrep_ready="OFF", wsrep_connected="ON",
                                wsrep_local_state="1", wsrep_local_state_comment="Joining: receiving State Transfer")))


def _galera_non_primary_unsigned_conf_id(data):
    """A non-Primary node on MariaDB 10.11/11.4 (and PXC), where
    wsrep_cluster_conf_id is SHOW_LONGLONG and prints -1 unsigned as
    18446744073709551615 (mariadb-10.11.19/11.4.13 sql/mysqld.cc, sql/sql_show.cc)."""
    return editor((WSREP, pairs(wsrep_cluster_conf_id="18446744073709551615")))


#: name -> (recording it starts from, edit factory(data) -> edit).
DERIVED = {
    "pxc-8.4-member-donor": ("pxc-8.4-healthy", _pxc_member_donor),
    "pxc-8.4-member-joiner": ("pxc-8.4-healthy", _pxc_member("JOINER")),
    "pxc-8.4-member-primary": ("pxc-8.4-healthy", _pxc_member("PRIMARY")),
    "pxc-8.4-maintenance": ("pxc-8.4-healthy", _pxc_maint),
    # Galera Cluster for MySQL (Codership): a MySQL server with a wsrep
    # provider that is not Percona XtraDB Cluster.
    "galera-mysql-8.4-healthy": ("pxc-8.4-healthy", lambda data: editor((VARIABLES, pairs(
        version_comment="MySQL Community Server - GPL, wsrep_26.22")))),
    # The address list as other deployments write it: node IPs (MariaDB's
    # basic example), FQDNs against a short host name, a seed list naming one
    # peer, a single DNS service name (Kubernetes headless service).
    "galera-11.8-ip-address": ("galera-11.8-healthy",
                               _galera_address("gcomm://192.168.80.2,192.168.80.3,192.168.80.4")),
    "galera-11.8-ip-port-two-of-three": ("galera-11.8-two-of-three",
                                         _galera_address("gcomm://192.168.80.2:4567,192.168.80.3:4567,192.168.80.4:4567")),
    "galera-11.8-fqdn": ("galera-11.8-healthy", _galera_address(
        "gcomm://mf-galera-1.lab.example,mf-galera-2.lab.example,mf-galera-3.lab.example")),
    "galera-11.8-partial-seeds": ("galera-11.8-healthy", _galera_address("gcomm://mf-galera-2")),
    "galera-11.8-dns-service": ("galera-11.8-healthy", _galera_address("gcomm://galera-headless.db.svc.cluster.local")),
    # A deliberate two-node cluster: one more failure loses the majority.
    "galera-11.8-two-nodes": ("galera-11.8-two-of-three", _galera_address("gcomm://mf-galera-1,mf-galera-3")),
    "galera-11.8-wsrep-off": ("galera-11.8-healthy", _galera_wsrep_off),
    "galera-11.8-joiner": ("galera-11.8-healthy", _galera_joiner),
    "galera-11.8-non-primary-unsigned": ("galera-11.8-non-primary", _galera_non_primary_unsigned_conf_id),
    "ic-8.4-degraded": ("ic-8.4-healthy", _ic_degraded),
    "ic-8.4-no-quorum": ("ic-8.4-healthy", _ic_no_quorum),
}


def derived(name):
    base, factory = DERIVED[name]
    data = load(base)
    return recorded(base, edit=factory(data))


# ---- synthetic scenarios -------------------------------------------------------------

GROUP_NAME = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RR_UUID = "44444444-4444-11f0-8a00-0242ac130005"
SERVER_UUIDS = ["11111111-1111-11f0-8a00-0242ac130002", "22222222-2222-11f0-8a00-0242ac130003",
                "33333333-3333-11f0-8a00-0242ac130004"]


def _mariadb_vars(**extra):
    out = {"version": "11.8.9-MariaDB-ubu2404-log", "version_comment": "mariadb.org binary distribution",
           "performance_schema": "OFF", "hostname": "galera-1", "port": "3306", "server_id": "1", "wsrep_on": "OFF",
           "wsrep_provider": "none", "wsrep_cluster_name": "my_wsrep_cluster", "wsrep_cluster_address": "",
           "wsrep_node_name": "galera-1", "wsrep_node_address": "", "wsrep_node_incoming_address": "AUTO",
           "wsrep_desync": "OFF"}
    out.update(extra)
    return dict((k, v) for k, v in out.items() if v is not None)


def _mysql_vars(**extra):
    out = {"version": "8.4.11", "version_comment": "MySQL Community Server - GPL", "performance_schema": "ON",
           "hostname": "mf-ic-1", "port": "3306", "report_host": "mf-ic-1", "server_uuid": SERVER_UUIDS[0],
           "group_replication_group_name": GROUP_NAME, "group_replication_single_primary_mode": "ON",
           "group_replication_local_address": "mf-ic-1:3306",
           "group_replication_group_seeds": "mf-ic-2:3306,mf-ic-3:3306"}
    out.update(extra)
    return dict((k, v) for k, v in out.items() if v is not None)


def _gr_member(i, state="ONLINE", role=None, version="8.4.11", host=None):
    role = role if role is not None else ("PRIMARY" if i == 0 else "SECONDARY")
    if state in ("OFFLINE", "ERROR"):
        role = ""
    return ["group_replication_applier", SERVER_UUIDS[i], host or "mf-ic-%d" % (i + 1), "3306", state, role,
            version, "MySQL"]


def _gr_stats(i, queue=0, applier=0, conflicts=0):
    return [SERVER_UUIDS[i], "17608000000000000:3", queue, 120, conflicts, 0, applier, 80, 40, 0]


def _instances(n=3, read_replica=None):
    out = [[i + 1, "cid-1", "mf-ic-%d:3306" % (i + 1), SERVER_UUIDS[i], "mf-ic-%d:3306" % (i + 1),
            "mf-ic-%d:3306" % (i + 1), "group-member"] for i in range(n)]
    if read_replica:
        out.append([9, "cid-1", "mf-rr-1:3306", read_replica, "mf-rr-1:3306", "mf-rr-1:3306", "read-replica"])
    return out


ROUTERS = [[1, "cid-1", "", "MySQL Router", "mf-ic-router", "8.4.10", "2026-10-20 12:00:00", 120,
            "6446", "6447", "6450", "6448", "6449", 1792497600]]


def _ic(members, stats, instances=None, routers=None, variables=None, version=(2, 2, 0)):
    return {
        "variables": variables or _mysql_vars(),
        "tables": {
            "performance_schema.replication_group_members": members,
            "performance_schema.replication_group_member_stats": stats,
            "information_schema.SCHEMATA": [["mysql_innodb_cluster_metadata"]],
            "mysql_innodb_cluster_metadata.schema_version": [list(version)],
            "mysql_innodb_cluster_metadata.v2_gr_clusters": [["cid-1", "mfcluster", "pm", GROUP_NAME]],
            "mysql_innodb_cluster_metadata.v2_instances": instances if instances is not None else _instances(),
            "mysql_innodb_cluster_metadata.v2_routers": routers if routers is not None else ROUTERS,
            "mysql_innodb_cluster_metadata.v2_cs_members": [],
        },
    }


def group(total, unreachable):
    """A single-primary group of *total* members seen from the first one,
    with the last *unreachable* members UNREACHABLE (roles kept)."""
    uuids = ["%08d-1111-11f0-8a00-0242ac130002" % (i + 1) for i in range(total)]
    rows = [["group_replication_applier", uuids[i], "mf-gr-%d" % (i + 1), "3306",
             "UNREACHABLE" if i >= total - unreachable else "ONLINE", "PRIMARY" if i == 0 else "SECONDARY",
             "8.4.11", "MySQL"] for i in range(total)]
    return {"variables": _mysql_vars(server_uuid=uuids[0]),
            "tables": {"performance_schema.replication_group_members": rows,
                       "performance_schema.replication_group_member_stats": [],
                       "information_schema.SCHEMATA": []}}


SCENARIOS = {
    "gr-8.4-multi-primary": {
        # Every ONLINE member reports PRIMARY in multi-primary mode
        # (member_info.cc:620-630); no InnoDB Cluster metadata.
        "variables": _mysql_vars(group_replication_single_primary_mode="OFF"),
        "tables": {
            "performance_schema.replication_group_members": [_gr_member(i, role="PRIMARY") for i in range(3)],
            "performance_schema.replication_group_member_stats": [_gr_stats(i) for i in range(3)],
            "information_schema.SCHEMATA": [],
        },
    },
    "gr-8.4-mixed-versions": {
        "variables": _mysql_vars(),
        "tables": {
            "performance_schema.replication_group_members": [_gr_member(0, version="8.4.11"),
                                                             _gr_member(1, version="8.4.12"),
                                                             _gr_member(2, version="8.4.11")],
            "performance_schema.replication_group_member_stats": [_gr_stats(i) for i in range(3)],
            "information_schema.SCHEMATA": [],
        },
    },
    "gr-8.4-two-members": {
        "variables": _mysql_vars(),
        "tables": {
            "performance_schema.replication_group_members": [_gr_member(0), _gr_member(1)],
            "performance_schema.replication_group_member_stats": [_gr_stats(0), _gr_stats(1)],
            "information_schema.SCHEMATA": [],
        },
    },
    "gr-8.4-denied": {
        "variables": _mysql_vars(partial_revokes="OFF"),
        "account": "app@%",
        "tables": {"performance_schema.replication_group_members":
                   ConnectorError("mysql client exited 1: ERROR 1142 (42000) at line 1: SELECT command denied to "
                                  "user 'app'@'%' for table 'replication_group_members'")},
    },
    "gr-8.4-ps-off": {
        "variables": _mysql_vars(performance_schema="OFF"),
    },
    "ic-8.4-read-replica": _ic([_gr_member(i) for i in range(3)], [_gr_stats(i) for i in range(3)],
                               instances=_instances(read_replica=RR_UUID),
                               version=(2, 3, 0)),
    "ic-8.4-metadata-2.4": _ic([_gr_member(i) for i in range(3)], [_gr_stats(i) for i in range(3)],
                               version=(2, 4, 0)),
    # Connected to the read replica itself: no Group Replication plugin (MySQL
    # Shell's addReplicaInstance never installs it), its replicated copy of
    # the metadata, and the read_replica_replication channel running.
    "ic-8.4-read-replica-local": dict(_ic([], [], instances=_instances(read_replica=RR_UUID), version=(2, 3, 0),
                                          variables=_mysql_vars(server_uuid=RR_UUID, hostname="mf-rr-1",
                                                                report_host="mf-rr-1", group_replication_group_name=None,
                                                                group_replication_single_primary_mode=None,
                                                                group_replication_local_address=None,
                                                                group_replication_group_seeds=None)),
                                     extra={"performance_schema.replication_connection_status": [["ON", "0", ""]],
                                            "performance_schema.replication_applier_status": [["ON"]]}),
    "ic-8.4-read-replica-stopped": dict(_ic([], [], instances=_instances(read_replica=RR_UUID), version=(2, 3, 0),
                                            variables=_mysql_vars(server_uuid=RR_UUID, hostname="mf-rr-1",
                                                                  group_replication_group_name=None)),
                                       extra={"performance_schema.replication_connection_status":
                                              [["OFF", "2003", "error connecting to source 'mf-ic-1:3306'"]],
                                              "performance_schema.replication_applier_status": [["ON"]]}),
    # The receiver retrying keeps its last connection error (Codex review):
    # CONNECTING is watch, never Error.
    "ic-8.4-read-replica-connecting": dict(_ic([], [], instances=_instances(read_replica=RR_UUID), version=(2, 3, 0),
                                               variables=_mysql_vars(server_uuid=RR_UUID, hostname="mf-rr-1",
                                                                     group_replication_group_name=None)),
                                          extra={"performance_schema.replication_connection_status":
                                                 [["CONNECTING", "2003", "error connecting to source 'mf-ic-1:3306'"]],
                                                 "performance_schema.replication_applier_status": [["ON"]]}),
    # The applier table refused: unknown, not stopped.
    "ic-8.4-read-replica-applier-denied": dict(_ic([], [], instances=_instances(read_replica=RR_UUID), version=(2, 3, 0),
                                                   variables=_mysql_vars(server_uuid=RR_UUID, hostname="mf-rr-1",
                                                                         group_replication_group_name=None)),
                                              extra={"performance_schema.replication_connection_status": [["ON", "0", ""]],
                                                     "performance_schema.replication_applier_status":
                                                     ConnectorError("mysql client exited 1: ERROR 1142 (42000) at line 1: "
                                                                    "SELECT command denied to user 'app'@'%' for table "
                                                                    "'replication_applier_status'")}),
    # MEMBER_HOST and MEMBER_PORT are separate columns; an IPv6 host.
    "gr-8.4-ipv6": {
        "variables": _mysql_vars(),
        "tables": {
            "performance_schema.replication_group_members": [_gr_member(0, host="2001:db8::1"),
                                                             _gr_member(1, host="2001:db8::2"),
                                                             _gr_member(2, host="2001:db8::3")],
            "performance_schema.replication_group_member_stats": [_gr_stats(i) for i in range(3)],
            "information_schema.SCHEMATA": [],
        },
    },
    "mysql-8.4-standalone": {
        "variables": _mysql_vars(group_replication_group_name=None, group_replication_single_primary_mode=None,
                                 group_replication_local_address=None, group_replication_group_seeds=None),
        "tables": {"performance_schema.replication_group_members": []},
    },
    "mysql-8.4-plugin-idle": {
        # The plugin is loaded but never started: one OFFLINE row, no group
        # name (ps_information.cc:46-77).
        "variables": _mysql_vars(group_replication_group_name=""),
        "tables": {"performance_schema.replication_group_members": [_gr_member(0, "OFFLINE")]},
    },
    "mariadb-11.8-standalone": {"variables": _mariadb_vars()},
    "pxc-8.4-standalone": {
        # PXC without a provider: wsrep_on is session-only (absent from SHOW
        # GLOBAL VARIABLES) and wsrep_provider is none.
        "variables": {"version": "8.4.11-11.1", "version_comment": "Percona XtraDB Cluster (GPL), Release rel11, "
                      "Revision 33f7510, WSREP version 26.1.4.3", "performance_schema": "ON",
                      "hostname": "pxc-solo", "port": "3306", "server_uuid": SERVER_UUIDS[0],
                      "wsrep_provider": "none", "wsrep_cluster_address": "", "pxc_strict_mode": "ENFORCING",
                      "pxc_maint_mode": "DISABLED"},
        "tables": {"performance_schema.replication_group_members": []},
    },
    "galera-11.8-denied": {
        "variables": _mariadb_vars(wsrep_on="ON", wsrep_provider="/usr/lib/galera/libgalera_smm.so",
                                   wsrep_cluster_address="gcomm://a,b,c"),
        "status_error": ConnectorError("mysql client exited 1: ERROR 1227 (42000) at line 1: Access denied; you "
                                       "need (at least one of) the PROCESS privilege(s) for this operation"),
    },
}


# ---- InnoDB ClusterSet (synthetic) -----------------------------------------------------
#
# Rows of v2_cs_members joined to v2_cs_clustersets (cluster.py
# _MD_CLUSTERSET_SQL): clusterset_id, cluster_id, cluster_name, member_role
# (PRIMARY or REPLICA, Sh/modules/adminapi/common/metadata/
# metadata-model-2.1.0.sql:606-631), invalidated (0 or 1: csm.view_id IS
# NULL), domain_name, view_id, router_options. Invalidated rows have a NULL member_role.
# This member is mf-ic-1, the PRIMARY of its group.

def _cs(*clusters):
    return [["cs-1", cid, name, None if invalid else role, "1" if invalid else "0", "mfcs", "4294967297", '{}']
            for cid, name, role, invalid in clusters]


def _cs_instances(cid, prefix, start):
    """Another cluster's three instances in the shared metadata (v2_instances)."""
    return [[start + i, cid, "%s-%d:3306" % (prefix, i + 1), "%08d-2222-11f0-8a00-0242ac130002" % (start + i),
             "%s-%d:3306" % (prefix, i + 1), "%s-%d:3306" % (prefix, i + 1), "group-member"] for i in range(3)]


def _clusterset(rows, extra=None):
    instances = _instances() + _cs_instances("cid-0", "mf-main", 20) + _cs_instances("cid-2", "mf-dr", 30)
    scenario = _ic([_gr_member(i) for i in range(3)], [_gr_stats(i) for i in range(3)], version=(2, 2, 0),
                   instances=instances)
    scenario["tables"]["mysql_innodb_cluster_metadata.v2_cs_members"] = rows
    scenario["variables"]["super_read_only"] = "OFF" if any(
        r[1] == "cid-1" and r[3] == "PRIMARY" for r in rows) else "ON"
    scenario["tables"]["performance_schema.replication_applier_status_by_worker"] = [[1, "ON", 0, ""]]
    # ClusterSet metadata contains every cluster's group identity, even
    # though this connection can observe live GR state only in its group.
    cluster_rows = scenario["tables"]["mysql_innodb_cluster_metadata.v2_gr_clusters"]
    for index, row in enumerate(rows):
        if not any(cluster[0] == row[1] for cluster in cluster_rows):
            cluster_rows.append([row[1], row[2], "pm", "22222222-3333-4444-5555-%012d" % index])
    if extra:
        scenario["extra"] = extra
    return scenario


_CS_CHANNEL_ON = {"performance_schema.replication_connection_status": [["ON", "0", ""]],
                  "performance_schema.replication_applier_status": [["ON"]],
                  "performance_schema.replication_connection_configuration": [["mf-main-1", "3306"]]}


def _read_replica_clusterset():
    """Connected to a read replica of the ClusterSet's primary cluster: no
    group, its replicated copy of the metadata, the channel running."""
    instances = _instances(read_replica=RR_UUID) + _cs_instances("cid-2", "mf-dr", 30)
    scenario = _ic([], [], version=(2, 2, 0), instances=instances,
                   variables=_mysql_vars(server_uuid=RR_UUID, hostname="mf-rr-1", report_host="mf-rr-1",
                                         group_replication_group_name=None,
                                         group_replication_single_primary_mode=None,
                                         group_replication_local_address=None,
                                         group_replication_group_seeds=None))
    scenario["tables"]["mysql_innodb_cluster_metadata.v2_cs_members"] = _cs(("cid-1", "mfcluster", "PRIMARY", False),
                                                                            ("cid-2", "drcluster", "REPLICA", False))
    scenario["extra"] = {"performance_schema.replication_connection_status": [["ON", "0", ""]],
                         "performance_schema.replication_applier_status": [["ON"]]}
    return scenario

SCENARIOS.update({
    "ic-8.4-clusterset-primary": _clusterset(_cs(("cid-1", "mfcluster", "PRIMARY", False),
                                                 ("cid-2", "drcluster", "REPLICA", False))),
    "ic-8.4-clusterset-replica": _clusterset(_cs(("cid-0", "maincluster", "PRIMARY", False),
                                                 ("cid-1", "mfcluster", "REPLICA", False)), _CS_CHANNEL_ON),
    "ic-8.4-clusterset-three": _clusterset(_cs(("cid-0", "maincluster", "PRIMARY", False),
                                                ("cid-1", "mfcluster", "REPLICA", False),
                                                ("cid-2", "drcluster", "REPLICA", False)), _CS_CHANNEL_ON),
    "ic-8.4-clusterset-replica-channel-off": _clusterset(
        _cs(("cid-0", "maincluster", "PRIMARY", False), ("cid-1", "mfcluster", "REPLICA", False)),
        {"performance_schema.replication_connection_status":
         [["OFF", "2003", "error connecting to source 'mf-main-1:3306'"]],
         "performance_schema.replication_applier_status": [["ON"]],
         "performance_schema.replication_connection_configuration": [["mf-main-1", "3306"]]}),
    # After a forcePrimaryCluster to drcluster, seen from the new primary.
    "ic-8.4-clusterset-invalidated": _clusterset(_cs(("cid-1", "mfcluster", "PRIMARY", False),
                                                     ("cid-2", "drcluster", "REPLICA", True))),
    # This cluster itself invalidated, in its own copy of the metadata.
    "ic-8.4-clusterset-local-invalidated": _clusterset(
        _cs(("cid-0", "maincluster", "PRIMARY", False), ("cid-1", "mfcluster", "REPLICA", True)), _CS_CHANNEL_ON),
    # Connected to a read replica of the ClusterSet's primary cluster (the
    # ic-8.4-read-replica-local shape: no Group Replication plugin, the
    # read_replica_replication channel running): the ClusterSet read comes
    # from its replicated copy of the metadata (cluster.py _read_read_replica).
    # Read replicas in a ClusterSet are a metadata 2.2.0 shape, on 8.0 and 8.4.
    "ic-8.4-read-replica-clusterset": _read_replica_clusterset(),
})


class ScenarioConn(object):
    """Answers the statements cluster.py sends from a scenario and records them."""

    def __init__(self, scenario, version=None):
        self.scenario = copy.deepcopy(scenario)
        self.calls = []
        self.timeouts = []
        self.closed = False
        self.version = version or self.scenario["variables"].get("version", "")

    def _answer(self, body):
        sc = self.scenario
        for fragment, error in sc.get("query_errors", {}).items():
            if fragment in body:
                raise error
        if (body == "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE "
                    "SCHEMA_NAME = 'mysql_innodb_cluster_metadata'" and
                'information_schema.SCHEMATA' not in sc.get('tables', {})):
            # A standalone server has information_schema even if a synthetic
            # scenario omits its empty metadata-schema result.
            return ''
        if body.startswith("SELECT r.router_id, r.clusterset_id"):
            routers = sc.get("clusterset_routers", [])
            if isinstance(routers, Exception):
                raise routers
            return batch(routers)
        if body.startswith("SELECT 'myflames_grants_begin'"):
            from _grants_fixtures import grants_batch
            return grants_batch(ADMIN_GRANTS, version=self.version)
        if body == "SHOW GRANTS":
            return batch([[line] for line in ADMIN_GRANTS])
        if body.startswith("SHOW GLOBAL VARIABLES WHERE Variable_name IN"):
            var_part, _, rest = body.partition(";")
            names = [n.strip().strip("'") for n in var_part.split("(", 1)[1].rstrip(")").split(",")]
            rows = [[n, sc["variables"][n]] for n in names if n in sc["variables"]]
            if "SELECT 'myflames_current_user', CURRENT_USER()" in rest:
                if isinstance(sc.get("account"), Exception):
                    raise sc["account"]
                if not sc.get("omit_current_user"):
                    rows.append(["myflames_current_user", sc.get("account", "mf_admin@localhost")])
            return batch(rows)
        if body.startswith("SHOW GLOBAL STATUS WHERE Variable_name = 'Uptime'"):
            return batch([["Uptime", "3600"]])
        if body.startswith("SHOW GLOBAL STATUS WHERE Variable_name LIKE 'wsrep"):
            if sc.get("status_error"):
                raise sc["status_error"]
            return batch(sorted(sc.get("status", {}).items()))
        tables = dict(sc.get("tables", {}))
        tables.update(sc.get("extra", {}))
        for table, answer in tables.items():
            if re.search(r" FROM %s( |$)" % re.escape(table), body):
                if isinstance(answer, Exception):
                    raise answer
                return batch(answer)
        raise ConnectorError("mysql client exited 1: ERROR 1146 (42S02) at line 1: Table doesn't exist: " + body)

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or [])))
        self.timeouts.append(timeout)
        body = sql.split("*/ ", 1)[1] if sql.startswith("/*") else sql
        return self._answer(body)

    def server_version(self):
        return self.version

    def is_mariadb(self):
        return "mariadb" in self.version.lower()

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def install_clusterset_batch_replay():
    """Replay multiple result sets from a single synthetic mysql login.

    Share the guarded discovery, snapshot and fencing replay between Python
    tests and the browser-fixture generator. Keep archived recordings unchanged;
    synthetic batches answer each statement in order and propagate failures.
    """
    from myflames import cluster as m

    original = ScenarioConn._answer
    if getattr(original, '_clusterset_batch_replay', False):
        return

    def answer(conn, body):
        markers = ('__myflames_clusterset_version__', '__myflames_clusterset_membership__',
                   '__myflames_clusterset_instances__', '__myflames_clusterset_groups__',
                   '__myflames_clusterset_routers__')
        for marker in markers:
            if body.strip() == "SELECT '%s'" % marker:
                return batch([[marker]])
        if body.strip().lower() == 'select @@server_uuid':
            return batch([[conn.scenario['variables'].get('server_uuid')]])
        membership = m._CLUSTERSET_SNAPSHOT_QUERIES[1][1]
        groups = m._CLUSTERSET_SNAPSHOT_QUERIES[3][1]
        if body in (membership, groups):
            # Preserve SQL NULL separately from the literal string "NULL".
            for fragment, error in conn.scenario.get('query_errors', {}).items():
                if fragment in body:
                    raise error
            original(conn, m._MD_CLUSTERSET_SQL if body == membership else m._MD_CLUSTERS_SQL)
            table = ('mysql_innodb_cluster_metadata.v2_cs_members' if body == membership else
                     'mysql_innodb_cluster_metadata.v2_gr_clusters')
            answers = dict(conn.scenario.get('tables', {}))
            answers.update(conn.scenario.get('extra', {}))
            rows = [list(row) for row in answers[table]]
            for row in rows:
                for column in ((2, 5) if body == membership else (1,)):
                    if row[column] is not None:
                        row[column] = '=' + str(row[column])
            return batch(rows)
        identity, sep, rest = body.partition(';')
        if sep and identity.strip().lower() == 'select @@global.server_uuid':
            uuid = conn.scenario['variables'].get('server_uuid')
            return batch([[uuid]]) + answer(conn, rest.strip())
        statements = [statement.strip() for statement in body.split(';')]
        if (statements[0].upper() == 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'
                and statements[1].upper() == 'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY'
                and statements[-1].upper() == 'COMMIT'):
            # START and COMMIT produce no result rows. Each SELECT still uses
            # the individual fixture answer, in statement order.
            output = []
            for index, statement in enumerate(statements[2:-1], start=2):
                try:
                    output.append(answer(conn, statement))
                except ConnectorError as exc:
                    if statement == m._CS_ROUTERS_SQL and 'ERROR 1142' in str(exc) and 'at line' not in str(exc):
                        raise ConnectorError('%s at line %d' % (exc, index + 1))
                    raise
            return ''.join(output)
        if statements == [m._MD_VERSION_SQL, m._MD_CLUSTERSET_SQL, m._MD_INSTANCES_SQL, m._MD_CLUSTERS_SQL]:
            return ''.join(original(conn, statement) for statement in statements)
        group, sep, variables = body.partition(';')
        if (sep and group.strip().startswith('SELECT ' + ', '.join(m._GR_MEMBERS_COLUMNS) +
                                               ' FROM performance_schema.replication_group_members') and
                variables.strip() == "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('super_read_only')"):
            return original(conn, group.strip()) + original(conn, variables.strip())
        variables, sep, action = body.partition(';')
        if (sep and variables.strip() == "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('super_read_only')"
                and action.strip() == "SELECT enabled FROM performance_schema.replication_group_member_actions "
                                      "WHERE name = 'mysql_disable_super_read_only_if_primary'"):
            return original(conn, variables.strip()) + original(conn, action.strip())
        return original(conn, body)

    answer._clusterset_batch_replay = True
    ScenarioConn._answer = answer


install_clusterset_batch_replay()


def clusterset_peer(cid="cid-2", prefix="mf-dr", start=30, scenario_name="ic-8.4-clusterset-primary", member_index=0):
    """A selected member of another cluster, sharing the synthetic metadata."""
    scenario = copy.deepcopy(SCENARIOS[scenario_name])
    instances = _cs_instances(cid, prefix, start)
    scenario["variables"]["server_uuid"] = instances[member_index][3]
    scenario["variables"]["super_read_only"] = "OFF" if member_index == 0 and any(
        row[1] == cid and row[3] == "PRIMARY"
        for row in scenario["tables"]["mysql_innodb_cluster_metadata.v2_cs_members"]) else "ON"
    scenario["extra"] = copy.deepcopy(_CS_CHANNEL_ON)
    scenario["extra"]["performance_schema.replication_connection_configuration"] = [["mf-ic-1", "3306"]]
    members = scenario["tables"]["performance_schema.replication_group_members"]
    for index, row in enumerate(members):
        row[1], row[2] = instances[index][3], "%s-%d" % (prefix, index + 1)
    return ScenarioConn(scenario)


def conn(name):
    """A connection for *name*: its recording when there is one (a live
    recording of a derived state wins over the derived twin), else the
    derived or synthetic scenario."""
    if has_recording(name):
        return recorded(name)[0]
    if name in DERIVED:
        return derived(name)[0]
    return ScenarioConn(SCENARIOS[name])


def report(name):
    from myflames.cluster import read_status
    return read_status(conn(name))


ALL = sorted(set(recordings()) | set(DERIVED) | set(SCENARIOS))
