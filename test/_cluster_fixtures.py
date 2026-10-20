"""Server answers for the cluster view (myflames/cluster.py, cluster-1.0).

Three sources, in order of preference:

* **Recorded**: ``test/fixtures/cluster/cluster-<name>.json`` hold every
  statement one live read sent and the ``mysql --batch`` text it got back, in
  the replay shape of ``test/fixtures/perf`` (``{server, source, args, calls,
  report}``). Recorded 2026-10-20 from throwaway Docker clusters (MariaDB
  11.8.9 with Galera 26.4.27, Percona XtraDB Cluster 8.4.11-11.1, MySQL
  8.4.11 InnoDB Cluster with MySQL Router 8.4.10) with
  ``scratchpad/tools/record-cluster.py``: healthy, a donor (wsrep_desync),
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


def recorded(name, edit=None):
    """(ReplayConn, data) for a recorded fixture; *edit(sql, text, n)* may
    change an answer."""
    data = load(name)
    return ReplayConn(data["calls"], edit=edit, version=_version(data)), data


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


#: name -> (recording it starts from, edit factory(data) -> edit).
DERIVED = {
    "pxc-8.4-member-donor": ("pxc-8.4-healthy", _pxc_member_donor),
    "pxc-8.4-maintenance": ("pxc-8.4-healthy", _pxc_maint),
    "ic-8.4-degraded": ("ic-8.4-healthy", _ic_degraded),
    "ic-8.4-no-quorum": ("ic-8.4-healthy", _ic_no_quorum),
}


def derived(name):
    base, factory = DERIVED[name]
    data = load(base)
    return recorded(base, edit=factory(data))


# ---- synthetic scenarios -------------------------------------------------------------

GROUP_NAME = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SERVER_UUIDS = ["11111111-1111-11f0-8a00-0242ac130002", "22222222-2222-11f0-8a00-0242ac130003",
                "33333333-3333-11f0-8a00-0242ac130004"]


def _mariadb_vars(**extra):
    out = {"version": "11.8.9-MariaDB-ubu2404-log", "version_comment": "mariadb.org binary distribution",
           "performance_schema": "OFF", "hostname": "galera-1", "port": "3306", "wsrep_on": "OFF",
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
            "6446", "6447", "6450", "6448", "6449"]]


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
        "variables": _mysql_vars(),
        "tables": {"performance_schema.replication_group_members":
                   ConnectorError("mysql client exited 1: ERROR 1142 (42000) at line 1: SELECT command denied to "
                                  "user 'app'@'%' for table 'replication_group_members'")},
    },
    "gr-8.4-ps-off": {
        "variables": _mysql_vars(performance_schema="OFF"),
    },
    "ic-8.4-read-replica": _ic([_gr_member(i) for i in range(3)], [_gr_stats(i) for i in range(3)],
                               instances=_instances(read_replica="44444444-4444-11f0-8a00-0242ac130005"),
                               version=(2, 3, 0)),
    "ic-8.4-metadata-2.4": _ic([_gr_member(i) for i in range(3)], [_gr_stats(i) for i in range(3)],
                               version=(2, 4, 0)),
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


class ScenarioConn(object):
    """Answers the statements cluster.py sends from a scenario and records them."""

    def __init__(self, scenario, version=None):
        self.scenario = copy.deepcopy(scenario)
        self.calls = []
        self.version = version or self.scenario["variables"].get("version", "")

    def _answer(self, body):
        sc = self.scenario
        if body.startswith("SHOW GLOBAL VARIABLES WHERE Variable_name IN"):
            names = [n.strip().strip("'") for n in body.split("(", 1)[1].rstrip(")").split(",")]
            return batch([[n, sc["variables"][n]] for n in names if n in sc["variables"]])
        if body.startswith("SHOW GLOBAL STATUS WHERE Variable_name = 'Uptime'"):
            return batch([["Uptime", "3600"]])
        if body.startswith("SHOW GLOBAL STATUS WHERE Variable_name LIKE 'wsrep"):
            if sc.get("status_error"):
                raise sc["status_error"]
            return batch(sorted(sc.get("status", {}).items()))
        for table, answer in sc.get("tables", {}).items():
            if re.search(r" FROM %s( |$)" % re.escape(table), body):
                if isinstance(answer, Exception):
                    raise answer
                return batch(answer)
        raise ConnectorError("mysql client exited 1: ERROR 1146 (42S02) at line 1: Table doesn't exist: " + body)

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or [])))
        body = sql.split("*/ ", 1)[1] if sql.startswith("/*") else sql
        return self._answer(body)

    def server_version(self):
        return self.version

    def is_mariadb(self):
        return "mariadb" in self.version.lower()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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
