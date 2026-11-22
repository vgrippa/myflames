"""Multi-tier topology discovery tests (:mod:`myflames.topology_links`).

Exercises ProxySQL + Readyset + MySQL/MariaDB (standalone, async primary/replica,
MariaDB Galera, Percona XtraDB Cluster, MySQL InnoDB Cluster) across all four
topology views, plus loopback/SSH safety, connection caps, and redaction.
"""
import json
import os
import tempfile
import unittest
from unittest import mock
from xml.sax.saxutils import escape as xml_escape

from myflames import topology_links
from myflames.connector import ConnectorError
from _reviewed_replay import evidence_options


def xml_rows(rows):
    body = []
    for row in rows:
        fields = []
        for k, v in row.items():
            if v is None:
                fields.append('<field name="%s" xsi:nil="true" />' % xml_escape(str(k)))
            else:
                fields.append('<field name="%s">%s</field>' % (xml_escape(str(k)), xml_escape(str(v))))
        body.append("<row>%s</row>" % "".join(fields))
    return (
        '<?xml version="1.0"?>'
        '<resultset xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">%s</resultset>'
        % "".join(body)
    )


def batch_rows(rows):
    return "".join("\t".join("NULL" if c is None else str(c) for c in r) + "\n" for r in rows)


class FakeProbeConn(object):
    def __init__(self, kind, **cfg):
        self.kind = kind
        self.cfg = cfg
        self.password = cfg.get("password", "secret-probe-pw")
        self.database = cfg.get("database")
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _variables(self):
        return {
            "version": self.cfg.get("version", "8.4.11"),
            "version_comment": self.cfg.get("version_comment", "MySQL Community Server - GPL"),
            "hostname": self.cfg.get("hostname", "mysql-primary"),
            "port": str(self.cfg.get("port", 3306)),
            "report_host": self.cfg.get("report_host", self.cfg.get("hostname", "mysql-primary")),
            "report_port": str(self.cfg.get("port", 3306)),
            "server_id": str(self.cfg.get("server_id", 1)),
            "server_uuid": self.cfg.get("uuid", "11111111-1111-1111-1111-111111111111"),
            "read_only": self.cfg.get("read_only_value", "ON" if self.cfg.get("read_only") else "OFF"),
            "super_read_only": "ON" if self.cfg.get("read_only") else "OFF",
            "wsrep_on": "ON" if self.cfg.get("wsrep") else "OFF",
            "wsrep_cluster_name": self.cfg.get("cluster_name", ""),
            "group_replication_group_name": self.cfg.get("gr_group", ""),
            "gtid_mode": "ON",
        }

    @evidence_options
    def run(self, sql, extra_flags=None, timeout=None):
        # A review evidence packet gets the marker row and this server's
        # identity projection in the same output, like a real server.
        from _reviewed_replay import answer_evidence
        from myflames.dbops import _EVIDENCE_VARIABLES
        values = {key: value for key, value in self._variables().items()
                  if key in _EVIDENCE_VARIABLES and value != ""}
        packet = answer_evidence(sql, extra_flags, timeout, self._answer, values)
        if packet is not None:
            return packet
        return self._answer(sql, extra_flags, timeout)

    def _answer(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, self.database, timeout))
        raw_sql = sql.strip()
        s = sql.replace(topology_links.TOPOLOGY_TAG, "").strip()
        upper = s.upper()
        if self.kind == "readyset":
            if s == "SELECT @@version_comment":
                return xml_rows([{"@@version_comment": "Readyset"}])
            if upper == "SHOW READYSET VERSION":
                return xml_rows([
                    {"name": "release version", "value": self.cfg.get("version", "stable-250605")},
                    {"name": "commit_id", "value": "6d2adde9b0"},
                ])
            if "SELECT 1 FROM REPLICATION_STATUS LIMIT 0" in upper:
                if (self.database or "").lower() == "readyset":
                    return xml_rows([])
                if self.cfg.get("probe_error"):
                    raise ConnectorError(self.cfg["probe_error"])
                if self.cfg.get("admin"):
                    return xml_rows([])
                raise ConnectorError("ERROR 1146 (42S02): Table 'replication_status' doesn't exist")
            if upper == "SHOW DATABASES":
                if self.cfg.get("admin") or (self.database or "").lower() == "readyset":
                    return xml_rows([{"Database": db} for db in ("information_schema", "public", "readyset")])
                return xml_rows([{"Database": "shop"}])
            if upper == "SHOW READYSET STATUS":
                if self.cfg.get("no_status"):
                    raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
                rows = [
                    {"name": "Status", "value": self.cfg.get("status", "Online")},
                    # Readyset only ever reports Running, Stopped or Disabled here.
                    {"name": "Replication Status", "value": self.cfg.get("repl_status", "Running")},
                    {"name": "Database Connection", "value": self.cfg.get("db_connection", "Connected")},
                    {"name": "CDC Server", "value": self.cfg.get("cdc_server", "mysql-primary:3306")},
                ]
                if self.cfg.get("last_replicator_error"):
                    rows.append({"name": "Last replicator error", "value": self.cfg["last_replicator_error"]})
                return xml_rows(rows)
            if "UPSTREAM_SERVERS" in upper:
                return xml_rows(self.cfg.get("upstreams", []))
            if upper == "SHOW CACHES":
                return xml_rows([{"query_id": "q_1"} for _ in range(self.cfg.get("caches", 2))])
            return xml_rows([])

        if self.kind == "proxysql":
            # Real ProxySQL 3.0.11 Admin only intercepts bare `SELECT @@version`;
            # a leading comment falls through to SQLite and raises syntax error.
            if raw_sql.startswith("/*"):
                raise ConnectorError("ProxySQL Admin Error: near \"%s\": syntax error" % s.split()[0])
            if s == "SELECT @@version_comment":
                raise ConnectorError("near \"@@version_comment\": syntax error")
            if "admin-version" in s:
                return xml_rows([{"variable_value": self.cfg.get("version", "3.0.11")}])
            if s == "SELECT @@version":
                if extra_flags and "--xml" in extra_flags:
                    return xml_rows([{"@@version": self.cfg.get("version", "3.0.11")}])
                return batch_rows([[self.cfg.get("version", "3.0.11")]])
            if "RUNTIME_MYSQL_SERVERS" in upper and self.cfg.get("no_runtime"):
                raise ConnectorError("ERROR 1146 (42S02): Table 'runtime_mysql_servers' doesn't exist")
            if "MYSQL_SERVERS" in upper:
                return xml_rows(self.cfg.get("servers", []))
            if "MYSQL_QUERY_RULES" in upper:
                return xml_rows(self.cfg.get("rules", []))
            return xml_rows([])

        # MySQL / MariaDB database
        if s == "SELECT @@version_comment":
            return xml_rows([{"@@version_comment": self.cfg.get("version_comment", "MySQL Community Server - GPL")}])
        if "admin-version" in s:
            raise ConnectorError("ERROR 1146 (42S02): Table 'global_variables' doesn't exist")
        if s.startswith("SHOW GLOBAL VARIABLES"):
            vmap = self._variables()
            return batch_rows([[k, v] for k, v in vmap.items() if v != ""])
        if "INNODB_CLUSTER_METADATA" in upper:
            return batch_rows([["mysql_innodb_cluster_metadata"]]) if self.cfg.get("innodb_cluster") else ""
        if "REPLICATION_GROUP_MEMBERS" in upper:
            return batch_rows(self.cfg.get("gr_members", []))
        if "WSREP_CLUSTER_MEMBERS" in upper:
            return batch_rows(self.cfg.get("wsrep_members", []))
        if s == topology_links._WSREP_STATUS_SQL:
            return batch_rows([["wsrep_gcomm_uuid", self.cfg["gcomm_uuid"]]]) if self.cfg.get("gcomm_uuid") else ""
        if upper == 'SHOW REPLICA STATUS' and self.cfg.get('legacy_channels'):
            raise ConnectorError('ERROR 1064 (42000): SHOW REPLICA STATUS is unavailable')
        if upper in ('SHOW REPLICA STATUS', 'SHOW SLAVE STATUS', 'SHOW ALL SLAVES STATUS'):
            return xml_rows(self.cfg.get("channels", []))
        if "SHOW REPLICAS" in upper or "SHOW SLAVE HOSTS" in upper:
            return xml_rows(self.cfg.get("replicas", []))
        if "SESSION_CONNECT_ATTRS" in upper:
            return batch_rows(self.cfg.get("connect_attrs", []))
        if "_READYSET_HEARTBEAT" in upper:
            return batch_rows([[1]]) if self.cfg.get("heartbeat") else ""
        return ""


class TestProbeChannels(unittest.TestCase):
    """Probe identity fields preserve the engine's UUID/server-ID distinction."""

    def probe_channel(self, row, **config):
        conn = FakeProbeConn('database', channels=[row], **config)
        result = topology_links._probe_connection(conn, {'id': 'replica', 'host': 'replica.example', 'port': 3306})
        self.assertEqual(len(result['channels']), 1)
        return result['channels'][0]

    def test_modern_mysql_source_uuid_and_server_id_are_separate(self):
        uuid = 'aaaaaaaa-0000-4000-8000-000000000001'
        row = self.probe_channel({'Source_Host': 'source.example', 'Source_Port': '3306',
                                  'Source_UUID': uuid, 'Source_Server_Id': '17'})
        self.assertEqual(row['source_uuid'], uuid)
        self.assertEqual(row['source_server_id'], 17)

    def test_legacy_mysql_reads_master_uuid_after_show_replica_status_fallback(self):
        uuid = 'bbbbbbbb-0000-4000-8000-000000000002'
        row = self.probe_channel({'Master_Host': 'source.example', 'Master_Port': '3306',
                                  'Master_UUID': uuid, 'Master_Server_Id': '42'},
                                 version='8.0.21', legacy_channels=True)
        self.assertEqual(row['source_uuid'], uuid)
        self.assertEqual(row['source_server_id'], 42)

    def test_mariadb_master_server_id_never_becomes_a_uuid(self):
        row = self.probe_channel({'Master_Host': 'maria-source.example', 'Master_Port': '3306',
                                  'Master_Server_Id': '231'},
                                 version='11.4.9-MariaDB', version_comment='MariaDB Server')
        self.assertIsNone(row['source_uuid'])
        self.assertEqual(row['source_server_id'], 231)

    def test_show_replicas_current_and_legacy_server_id_column_spellings(self):
        for column, version in (('Server_Id', '8.4.11'), ('Server_id', '8.0.21')):
            with self.subTest(column=column, version=version):
                conn = FakeProbeConn('database', version=version,
                    replicas=[{column: '42', 'Host': 'replica.example', 'Port': '3306'}])
                report = topology_links._probe_connection(conn, {'id': 'source', 'host': 'source.example', 'port': 3306})
                self.assertEqual(len(report['replicas']), 1)
                self.assertEqual(report['replicas'][0]['server_id'], 42)

    def test_mariadb_read_only_enum_protects_every_non_off_mode(self):
        for value, expected in (('NO_LOCK', True), ('NO_LOCK_NO_ADMIN', True), ('ON', True),
                                ('1', True), ('TRUE', True), ('OFF', False), ('0', False), ('FALSE', False)):
            with self.subTest(value=value):
                conn = FakeProbeConn('database', version='12.0.1-MariaDB',
                    version_comment='MariaDB Server', read_only_value=value)
                report = topology_links._probe_connection(conn, {'id': 'source', 'host': 'source.example', 'port': 3306})
                self.assertIs(report['read_only'], expected)

    def test_malformed_or_absent_uuid_is_null_without_losing_server_id(self):
        for column in ('Source_UUID', 'Master_UUID'):
            for invalid in ('42', 'not-a-uuid', 'aaaaaaaa-0000-4000-8000-000000000001 extra', '', None):
                with self.subTest(column=column, invalid=invalid):
                    row = self.probe_channel({'Master_Host': 'source.example', 'Master_Server_Id': '42',
                                              column: invalid})
                    self.assertIsNone(row['source_uuid'])
                    self.assertEqual(row['source_server_id'], 42)


class TestMultiTierTopology(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = dict(os.environ, HOME=self.temp.name, USERPROFILE=self.temp.name, MYFLAMES_SECRET_BACKEND="vault")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        topology_links._reset_cache_for_tests()

    def test_full_proxysql_readyset_mysql_primary_replica_topology(self):
        profiles = [
            {"id": "p-px", "name": "ProxySQL Admin", "host": "127.0.0.1", "port": 16032, "remember_password": True, "group": "lab"},
            {"id": "p-rs", "name": "Readyset Cache", "host": "127.0.0.1", "port": 13307, "remember_password": True, "group": "lab"},
            {"id": "p-pri", "name": "MySQL Primary", "host": "127.0.0.1", "port": 13306, "remember_password": True, "group": "lab"},
            {"id": "p-rep", "name": "MySQL Replica", "host": "127.0.0.1", "port": 13308, "remember_password": True, "group": "lab"},
        ]
        conns = {
            "p-px": FakeProbeConn(
                "proxysql",
                servers=[
                    {"hostgroup_id": "10", "hostname": "mysql-primary", "port": "3306", "status": "ONLINE", "weight": "100", "comment": "writer"},
                    {"hostgroup_id": "20", "hostname": "mysql-replica", "port": "3306", "status": "ONLINE", "weight": "100", "comment": "reader"},
                    {"hostgroup_id": "30", "hostname": "readyset", "port": "3307", "status": "ONLINE", "weight": "100", "comment": "readyset cache"},
                ],
                rules=[
                    {"rule_id": "100", "active": "1", "destination_hostgroup": "30", "mirror_hostgroup": None,
                     "comment": 'readyset-meta: {"query_id":"q_1111","mode":"cached"}'},
                    {"rule_id": "101", "active": "1", "destination_hostgroup": "20", "mirror_hostgroup": "30",
                     "comment": 'readyset-meta: {"mode":"mirror"}'},
                ],
            ),
            "p-rs": FakeProbeConn(
                "readyset",
                upstreams=[
                    {"host": "mysql-primary", "port": "3306", "database": "shop", "user": "rs", "weight": "100",
                     "purpose": "cdc", "active_cdc": "true", "health": "up", "connections": "2"},
                    {"host": "mysql-replica", "port": "3306", "database": "shop", "user": "rs", "weight": "100",
                     "purpose": "query", "active_cdc": "false", "health": "up", "connections": "4"},
                ],
                caches=5,
            ),
            "p-pri": FakeProbeConn(
                "database", hostname="mysql-primary", uuid="aaaa-1111", server_id=1, read_only=False,
                replicas=[{"Host": "mysql-replica", "Port": "3306", "Replica_UUID": "bbbb-2222", "Server_id": "2"}],
            ),
            "p-rep": FakeProbeConn(
                "database", hostname="mysql-replica", uuid="bbbb-2222", server_id=2, read_only=True,
                channels=[{
                    "Channel_Name": "", "Source_Host": "mysql-primary", "Source_Port": "3306",
                    "Source_UUID": "aaaa-1111", "Replica_IO_Running": "Yes", "Replica_SQL_Running": "Yes",
                    "Seconds_Behind_Source": "0",
                }],
            ),
        }
        opener = lambda p: conns[p["id"]]

        # 1. From Readyset's perspective: sees ProxySQL upstream AND MySQL Primary + Replica downstream!
        rs_topo = topology_links.build_readyset_topology(
            {"Status": "Online", "Replication Status": "Running", "CDC Server": "mysql-primary:3306"},
            [
                {"host": "mysql-primary", "port": 3306, "purpose": "cdc", "active_cdc": True, "health": "up", "connections": 2},
                {"host": "mysql-replica", "port": 3306, "purpose": "query", "active_cdc": False, "health": "up", "connections": 4},
            ],
            [],
            "adapter",
            "stable-250605",
            profiles=profiles,
            open_profile=opener,
            observer_host="127.0.0.1",
            current_profile_id="p-rs",
            current_endpoint=("127.0.0.1", 13307),
        )
        self.assertEqual(len(rs_topo["proxysql_nodes"]), 1)
        self.assertEqual(rs_topo["proxysql_nodes"][0]["profile_id"], "p-px")
        self.assertEqual(rs_topo["proxysql_nodes"][0]["querypilot_rule_count"], 2)
        self.assertEqual(rs_topo["proxysql_nodes"][0]["hostgroup_id"], 30)
        self.assertEqual(rs_topo["proxysql_nodes"][0]["routing_mode"], "cached_and_mirrored")
        db_ids = {n["profile_id"] for n in rs_topo["upstreams"] if n.get("profile_id")}
        self.assertEqual(db_ids, {"p-pri", "p-rep"})

        # 2. From ProxySQL's perspective: classifies HG 30 as Readyset cached_and_mirrored and links to p-rs!
        topology_links._reset_cache_for_tests()
        px_report = {
            "servers": [
                {"key": "10|mysql-primary|3306", "hostgroup_id": 10, "hostname": "mysql-primary", "port": 3306,
                 "runtime_status": "ONLINE", "weight": 100, "comment": "writer", "hostgroup_roles": ["writer"], "pool": {}},
                {"key": "20|mysql-replica|3306", "hostgroup_id": 20, "hostname": "mysql-replica", "port": 3306,
                 "runtime_status": "ONLINE", "weight": 100, "comment": "reader", "hostgroup_roles": ["reader"], "pool": {}},
                {"key": "30|readyset|3307", "hostgroup_id": 30, "hostname": "readyset", "port": 3307,
                 "runtime_status": "ONLINE", "weight": 100, "comment": "readyset cache", "hostgroup_roles": [], "pool": {}},
            ],
            "query_rules": [
                {"rule_id": 100, "active": True, "destination_hostgroup": 30, "mirror_hostgroup": None,
                 "comment": 'readyset-meta: {"query_id":"q_1111","mode":"cached"}'},
                {"rule_id": 101, "active": True, "destination_hostgroup": 20, "mirror_hostgroup": 30,
                 "comment": 'readyset-meta: {"mode":"mirror"}'},
            ],
        }
        px_topo = topology_links.classify_proxysql_topology(
            px_report, profiles=profiles, open_profile=opener, observer_host="127.0.0.1", current_profile_id="p-px"
        )
        self.assertTrue(px_topo["has_readyset"])
        self.assertEqual(px_topo["querypilot_rule_count"], 2)
        self.assertEqual(len(px_topo["readyset_nodes"]), 1)
        self.assertEqual(px_topo["readyset_nodes"][0]["routing_mode"], "cached_and_mirrored")
        self.assertEqual(px_topo["readyset_nodes"][0]["profile_id"], "p-rs")
        self.assertEqual(px_topo["readyset_nodes"][0]["cache_count"], 5)
        self.assertEqual(px_topo["readyset_nodes"][0]["hostgroup_roles"], [])

        # The source is deliberately outside the replica's consent group.
        profiles[2]["group"] = "sources"

        # 3. From MySQL Replica's perspective: discovers both ProxySQL and Readyset WITHOUT opening p-pri!
        topology_links._reset_cache_for_tests()
        opened_from_db = []

        def tracking_opener(p):
            opened_from_db.append(p["id"])
            return conns[p["id"]]

        tiers = topology_links.discover_database_tiers(
            conns["p-rep"],
            {"profile_host": "127.0.0.1", "profile_port": 13308, "hostname": "mysql-replica", "port": 3306, "server_uuid": "bbbb-2222"},
            peer_endpoints=[{"host": "mysql-primary", "port": 3306, "server_uuid": "aaaa-1111"}],
            profiles=profiles,
            open_profile=tracking_opener,
            observer_host="127.0.0.1",
            current_profile_id="p-rep",
        )
        self.assertNotIn("p-pri", opened_from_db, "known MySQL source is not probed as a proxy/cache tier")
        self.assertEqual([r["profile_id"] for r in tiers["readyset"]], ["p-rs"])
        self.assertEqual([x["profile_id"] for x in tiers["proxysql"]], ["p-px"])

    def test_galera_and_innodb_cluster_downstream_of_readyset(self):
        profiles = [
            {"id": "p-galera", "name": "Galera Node 1", "host": "127.0.0.1", "port": 13316, "remember_password": True, "group": "lab"},
        ]
        galera_conn = FakeProbeConn(
            "database",
            version="11.4.13-MariaDB",
            version_comment="mariadb.org binary distribution",
            hostname="galera-1",
            wsrep=True,
            cluster_name="shop_galera",
            wsrep_members=[
                ["u-1", "g1", "galera-1:3306"],
                ["u-2", "g2", "galera-2:3306"],
                ["u-3", "g3", "galera-3:3306"],
            ],
        )
        topo = topology_links.build_readyset_topology(
            {"Status": "Online", "Replication Status": "Running", "CDC Server": "galera-1:3306"},
            [{"host": "galera-1", "port": 3306, "purpose": "cdc, query", "active_cdc": True, "health": "up", "connections": 2}],
            [],
            "adapter",
            "stable-250605",
            profiles=profiles + [{"id": "current", "group": "lab"}],
            current_profile_id="current",
            open_profile=lambda p: galera_conn,
            observer_host="127.0.0.1",
        )
        self.assertEqual(topo["cluster"]["kind"], "galera")
        self.assertEqual(topo["cluster"]["name"], "shop_galera")
        self.assertGreaterEqual(len(topo["upstreams"]) + len(topo["database_peers"]), 3)

    def test_loopback_ssh_isolation_and_secret_redaction_on_probe_failure(self):
        # A saved profile over an SSH tunnel to 127.0.0.1:3306 must never match local 127.0.0.1:3306.
        ssh_profile = {
            "id": "p-ssh", "name": "Remote Tunnel", "host": "127.0.0.1", "port": 3306,
            "ssh_host": "bastion.example.com", "remember_password": True, "group": "lab",
        }
        db_conn = FakeProbeConn("database", hostname="remote-db", port=3306)
        probed, _ = topology_links.probe_saved_profiles([ssh_profile], lambda p: db_conn,
            skip_profile_id="current", current={"id": "current", "group": "lab"})
        match, ev = topology_links.match_database_endpoint("127.0.0.1", 3306, probed, observer_host="127.0.0.1")
        self.assertIsNone(match)
        self.assertIsNone(ev)

        # Password and URL credentials in failing probe error are redacted.
        topology_links._reset_cache_for_tests()

        class FailingConn(object):
            password = "super-secret-probe-pw"
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            @evidence_options
            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError("Failed using super-secret-probe-pw on mysql://u:url-pw@db:3306/s")

        bad_profile = {"id": "p-bad", "name": "Broken RS", "host": "127.0.0.1", "port": 3307, "remember_password": True, "group": "lab"}
        _, notes = topology_links.probe_saved_profiles([bad_profile], lambda p: FailingConn(),
            skip_profile_id="current", current={"id": "current", "group": "lab"})
        joined = "\n".join(notes)
        self.assertNotIn("super-secret-probe-pw", joined)
        self.assertNotIn("url-pw", joined)
        self.assertIn("could not be read: the connection could not be read.", joined)
        self.assertNotIn("mysql://", joined)

    def test_classify_proxysql_topology_with_real_proxysql_nested_shape(self):
        # proxysql.read_status nests comment/weight under settings/config,
        # lowercase pool keys, and mirror_hostgroup under rule["fields"].
        px_report = {
            "servers": [
                {
                    "key": "rt|10|mysql-primary|3306",
                    "hostgroup_id": 10,
                    "hostname": "mysql-primary",
                    "port": 3306,
                    "runtime_status": "ONLINE",
                    "hostgroup_roles": ["Replication writer"],
                    "settings": {"weight": 1000, "comment": "primary"},
                    "config": {"weight": 1000, "comment": "primary"},
                    "pool": {"queries": 420, "conn_used": 2, "conn_free": 6, "latency_us": 350},
                },
                {
                    "key": "rt|30|readyset|3307",
                    "hostgroup_id": 30,
                    "hostname": "readyset",
                    "port": 3307,
                    "runtime_status": "ONLINE",
                    # A runtime ONLINE row in a monitor's offline hostgroup:
                    # the role must reach the Readyset node so the topology
                    # does not treat it as serving traffic (CX-M18).
                    "hostgroup_roles": ["offline"],
                    "settings": {"weight": 1000, "comment": "readyset cache"},
                    "config": {"weight": 1000, "comment": "readyset cache"},
                    "pool": {"queries": 910, "conn_used": 1, "conn_free": 7, "latency_us": 120},
                },
            ],
            "query_rules": [
                {
                    "rule_id": 10,
                    "active": True,
                    "destination_hostgroup": 30,
                    "comment": 'readyset-meta: {"query_id":"q_abc","mode":"cached"}',
                    "fields": {"mirror_hostgroup": None},
                },
                {
                    "rule_id": 11,
                    "active": True,
                    "destination_hostgroup": 10,
                    "comment": "mirror traffic",
                    "fields": {"mirror_hostgroup": 30},
                },
            ],
        }
        topo = topology_links.classify_proxysql_topology(px_report, profiles=[], open_profile=None)
        self.assertTrue(topo["has_readyset"])
        self.assertEqual(topo["querypilot_rule_count"], 1)
        self.assertEqual(len(topo["readyset_nodes"]), 1)
        rs = topo["readyset_nodes"][0]
        self.assertEqual(rs["routing_mode"], "cached_and_mirrored")
        self.assertEqual(rs["hostgroup_roles"], ["offline"])
        self.assertEqual(rs["queries"], 910)
        self.assertEqual(rs["conn_used"], 1)
        self.assertEqual(rs["latency_us"], 120)
        self.assertEqual(topo["database_nodes"][0]["queries"], 420)
        self.assertEqual(topo["database_nodes"][0]["weight"], 1000)

    def test_adapter_plus_admin_profiles_unsaved_replica_and_binlog_follower_filter(self):
        # Version parser handles both 2-column (name, value) and 1-column shapes.
        self.assertEqual(
            topology_links.parse_readyset_version([
                {"name": "release version", "value": "stable-261022"},
                {"name": "commit_id", "value": "6d2adde9b0"},
            ]),
            "stable-261022",
        )
        self.assertEqual(
            topology_links.parse_readyset_version([{"version": "stable-250605"}]),
            "stable-250605",
        )

        profiles = [
            {"id": "p-px", "name": "rs-proxysql", "host": "127.0.0.1", "port": 34532, "remember_password": True, "group": "lab"},
            {"id": "p-rs", "name": "rs-adapter", "host": "127.0.0.1", "port": 34507, "database": "shop", "remember_password": True, "group": "lab"},
            {"id": "p-rs-admin", "name": "rs-admin", "host": "127.0.0.1", "port": 34536, "remember_password": True, "group": "lab"},
            {"id": "p-pri", "name": "rs-primary", "host": "127.0.0.1", "port": 34505, "remember_password": True, "group": "lab"},
            {"id": "p-rep-db", "name": "rs-replica", "host": "127.0.0.1", "port": 34506, "remember_password": True, "group": "lab"},
        ]
        rs_upstreams = [
            {"host": "mf-rs-primary", "port": "3306", "database": "shop", "user": "rs", "weight": "100",
             "purpose": "cdc, query", "active_cdc": "true", "health": "healthy", "connections": "2"},
        ]
        conns = {
            "p-px": FakeProbeConn(
                "proxysql",
                servers=[
                    {"hostgroup_id": "10", "hostname": "mf-rs-primary", "port": "3306", "status": "ONLINE", "weight": "100", "comment": "writer"},
                    {"hostgroup_id": "12", "hostname": "mf-rs-replica-unsaved", "port": "3306", "status": "ONLINE", "weight": "100", "comment": "reader"},
                    {"hostgroup_id": "20", "hostname": "mf-rs-readyset", "port": "3307", "status": "ONLINE", "weight": "100", "comment": "Readyset cache"},
                ],
                rules=[
                    {"rule_id": "100", "active": "1", "destination_hostgroup": "20", "mirror_hostgroup": None,
                     "comment": 'readyset-meta: {"query_id":"q_1111","mode":"cached"}'},
                ],
            ),
            "p-rs": FakeProbeConn("readyset", version="stable-261022", upstreams=rs_upstreams, caches=3, admin=False),
            "p-rs-admin": FakeProbeConn("readyset", version="stable-261022", upstreams=rs_upstreams, caches=3, admin=True),
            "p-pri": FakeProbeConn(
                "database", hostname="mf-rs-primary", uuid="aaaa-1111", server_id=1, read_only=False,
                replicas=[
                    {"Host": "mf-rs-replica-unsaved", "Port": "3306", "Replica_UUID": "bbbb-2222", "Server_id": "2"},
                    {"Host": "mf-rs-readyset", "Port": "3307", "Replica_UUID": "cccc-3333", "Server_id": "999"},
                ],
            ),
            "p-rep-db": FakeProbeConn(
                "database", hostname="mf-rs-unrelated-replica", uuid="dddd-4444", server_id=4, read_only=True,
            ),
        }
        opener = lambda p: conns[p["id"]]

        # 1. ProxySQL resolves Readyset to SQL adapter profile p-rs and keeps unsaved MySQL replica in database_nodes.
        px_report = {
            "servers": [
                {"key": "10|mf-rs-primary|3306", "hostgroup_id": 10, "hostname": "mf-rs-primary", "port": 3306,
                 "runtime_status": "ONLINE", "weight": 100, "comment": "writer", "hostgroup_roles": ["writer"], "pool": {}},
                {"key": "12|mf-rs-replica-unsaved|3306", "hostgroup_id": 12, "hostname": "mf-rs-replica-unsaved", "port": 3306,
                 "runtime_status": "ONLINE", "weight": 100, "comment": "reader", "hostgroup_roles": ["reader"], "pool": {}},
                {"key": "20|mf-rs-readyset|3307", "hostgroup_id": 20, "hostname": "mf-rs-readyset", "port": 3307,
                 "runtime_status": "ONLINE", "weight": 100, "comment": "Readyset cache", "hostgroup_roles": [], "pool": {}},
            ],
            "query_rules": [
                {"rule_id": 100, "active": True, "destination_hostgroup": 20, "mirror_hostgroup": None,
                 "comment": 'readyset-meta: {"query_id":"q_1111","mode":"cached"}'},
            ],
        }
        px_topo = topology_links.classify_proxysql_topology(
            px_report, profiles=profiles, open_profile=opener, observer_host="127.0.0.1", current_profile_id="p-px"
        )
        self.assertEqual([n["profile_id"] for n in px_topo["readyset_nodes"]], ["p-rs"])
        self.assertEqual(px_topo["readyset_nodes"][0]["version"], "stable-261022")
        self.assertEqual({n["host"] for n in px_topo["database_nodes"]}, {"mf-rs-primary", "mf-rs-replica-unsaved"})

        profiles[-1]["group"] = "another-topology"
        # 2. Replication / Cluster tier discovery collapses p-rs + p-rs-admin for the same deployment into p-rs
        # and does not probe rs-replica.
        topology_links._reset_cache_for_tests()
        probed_ids = []
        def tracking_opener(p):
            probed_ids.append(p["id"])
            return conns[p["id"]]
        tiers = topology_links.discover_database_tiers(
            conns["p-pri"],
            {"profile_host": "127.0.0.1", "profile_port": 34505, "hostname": "mf-rs-primary", "port": 3306, "server_uuid": "aaaa-1111"},
            profiles=profiles,
            open_profile=tracking_opener,
            observer_host="127.0.0.1",
            current_profile_id="p-pri",
        )
        self.assertNotIn("p-rep-db", probed_ids)
        self.assertEqual([r["profile_id"] for r in tiers["readyset"]], ["p-rs"])
        self.assertEqual([x["profile_id"] for x in tiers["proxysql"]], ["p-px"])

        # Replica whose Readyset connects only to its primary (peer_endpoints uses "host") still discovers p-rs!
        profiles[-1]["group"] = "lab"
        rep_tiers = topology_links.discover_database_tiers(
            conns["p-rep-db"],
            {"profile_host": "127.0.0.1", "profile_port": 34506, "hostname": "mf-rs-replica-unsaved", "port": 3306, "server_uuid": "bbbb-2222"},
            peer_endpoints=[{"host": "mf-rs-primary", "port": 3306, "server_uuid": "aaaa-1111"}],
            profiles=profiles,
            open_profile=opener,
            observer_host="127.0.0.1",
            current_profile_id="p-rep-db",
        )
        self.assertEqual([r["profile_id"] for r in rep_tiers["readyset"]], ["p-rs"])
        self.assertEqual([x["profile_id"] for x in rep_tiers["proxysql"]], ["p-px"])

        # 3. Readyset topology sees ProxySQL upstream and filters its own 3307 binlog follower out of database_peers.
        topology_links._reset_cache_for_tests()
        rs_topo = topology_links.build_readyset_topology(
            {"Status": "Online", "Replication Status": "Running", "CDC Server": "mf-rs-primary:3306"},
            [{"host": "mf-rs-primary", "port": 3306, "purpose": "cdc, query", "active_cdc": True, "health": "up", "connections": 2}],
            [],
            "adapter",
            "stable-261022",
            profiles=profiles,
            open_profile=opener,
            observer_host="127.0.0.1",
            current_profile_id="p-rs",
            current_endpoint=("127.0.0.1", 34507),
        )
        self.assertEqual([p["profile_id"] for p in rs_topo["proxysql_nodes"]], ["p-px"])
        self.assertEqual([p["host"] for p in rs_topo["database_peers"]], ["mf-rs-replica-unsaved"])

        # 4. An unrelated ProxySQL with a "readyset" comment on another stack's backend must not attach here.
        topology_links._reset_cache_for_tests()
        unrelated_px = {"id": "p-px-other", "name": "Other ProxySQL", "host": "10.99.0.10", "port": 6032, "remember_password": True, "group": "lab"}
        other_conns = dict(conns)
        other_conns["p-px-other"] = FakeProbeConn(
            "proxysql",
            servers=[
                {"hostgroup_id": "10", "hostname": "other-db", "port": "3306", "status": "ONLINE", "weight": "100", "comment": "writer"},
                {"hostgroup_id": "30", "hostname": "other-readyset", "port": "3307", "status": "ONLINE", "weight": "100", "comment": "readyset cache"},
            ],
            rules=[],
        )
        scoped_tiers = topology_links.discover_database_tiers(
            other_conns["p-pri"],
            {"profile_host": "127.0.0.1", "profile_port": 34505, "hostname": "mf-rs-primary", "port": 3306, "server_uuid": "aaaa-1111"},
            profiles=profiles + [unrelated_px],
            open_profile=lambda p: other_conns[p["id"]],
            observer_host="127.0.0.1",
            current_profile_id="p-pri",
        )
        self.assertEqual([x["profile_id"] for x in scoped_tiers["proxysql"]], ["p-px"])
    def test_readyset_probe_role_uses_evidence_and_preserves_other_outputs(self):
        # Both listeners route SHOW DATABASES to DataFusion with readyset
        # selected (backend.rs:1494-1548), so only a no-database probe can
        # distinguish them. Port 6036 itself is never evidence.
        cases = [
            (3307, "readyset", False, None, "adapter"),
            (6036, "readyset", False, None, "adapter"),
            (13307, "readyset", True, None, "admin"),
        ]
        for database in (None, "readyset"):
            for port in (3307, 6036):
                cases.extend([
                    (port, database, False, "query timed out", None),
                    (port, database, False, "ERROR 2013 (HY000): Lost connection", None),
                    (port, database, False, "ERROR 1046 (3D000): No database selected", "adapter"),
                    (port, database, False, "ERROR 1146 (42S02): Table does not exist", "adapter"),
                ])
        for port, database, admin, error, role in cases:
            with self.subTest(port=port, database=database, admin=admin, error=error):
                profile = {"id": "p-rs", "name": "Readyset", "host": "127.0.0.1",
                           "port": port, "database": database}
                upstreams = [{"host": "mysql-primary", "port": "3306", "purpose": "cdc"}]
                conn = FakeProbeConn("readyset", database=database, admin=admin, probe_error=error,
                                     upstreams=upstreams, caches=3)
                result = topology_links._probe_connection(conn, profile)
                self.assertEqual(result["port_role"], role)
                self.assertEqual(conn.database, database)
                probes = [(sql, db, timeout) for sql, db, timeout in conn.calls
                          if sql == "SELECT 1 FROM replication_status LIMIT 0"]
                self.assertEqual(probes, [("SELECT 1 FROM replication_status LIMIT 0", None,
                                           topology_links.QUERY_TIMEOUT_S)])
                self.assertFalse(any(sql == "SHOW DATABASES" for sql, _, _ in conn.calls))
                self.assertEqual(result["kind"], "readyset")
                self.assertEqual(result["version"], "stable-250605")
                self.assertEqual(result["status_text"], "Online")
                self.assertEqual(result["replication_status"], "Running")
                self.assertEqual(result["cache_count"], 3)
                self.assertEqual(result["upstreams"], upstreams)

    def test_unknown_readyset_role_never_suppresses_admin_tier(self):
        profiles = [
            {"id": "current", "group": "lab"},
            {"id": "rs-admin", "name": "Readyset admin", "host": "127.0.0.1", "port": 6036,
             "remember_password": True, "group": "lab"},
            {"id": "rs-other", "name": "Readyset other", "host": "127.0.0.1", "port": 3307,
             "remember_password": True, "group": "lab"},
        ]
        for error, expected in (("query timed out", ["rs-admin", "rs-other"]), (None, ["rs-other"])):
            with self.subTest(error=error):
                topology_links._reset_cache_for_tests()
                conns = {
                    "rs-admin": FakeProbeConn("readyset", admin=True),
                    "rs-other": FakeProbeConn("readyset", probe_error=error),
                }
                tiers = topology_links.discover_database_tiers(
                    FakeProbeConn("database"),
                    {"profile_host": "127.0.0.1", "profile_port": 13306,
                     "hostname": "mysql-primary", "port": 3306},
                    profiles=profiles, open_profile=lambda p: conns[p["id"]],
                    observer_host="127.0.0.1", current_profile_id="current")
                self.assertEqual([node["profile_id"] for node in tiers["readyset"]], expected)

    def test_proxysql_duplicate_readyset_matches_require_proven_adapter(self):
        # Exercise both endpoint and upstream-match tie breakers: an unknown
        # role is neither a proven adapter nor a reason to discard the admin.
        for direct in (True, False):
            for error, expected in (("query timed out", None), (None, "rs-other")):
                with self.subTest(direct=direct, error=error):
                    topology_links._reset_cache_for_tests()
                    profiles = [
                        {"id": "current", "group": "lab"},
                        {"id": "db", "name": "MySQL", "host": "mysql-primary", "port": 3306,
                         "remember_password": True, "group": "lab"},
                    ] + [{"id": pid, "name": "Readyset " + pid, "host": "readyset-saved", "port": 3307,
                          "remember_password": True, "group": "lab"} for pid in ("rs-admin", "rs-other")]
                    conns = {"db": FakeProbeConn("database"),
                             "rs-admin": FakeProbeConn("readyset", admin=True),
                             "rs-other": FakeProbeConn("readyset", probe_error=error)}
                    report = {"servers": [
                        {"hostgroup_id": 10, "hostname": "mysql-primary", "port": 3306,
                         "runtime_status": "ONLINE"},
                        {"hostgroup_id": 20, "hostname": "readyset-saved" if direct else "readyset-routed",
                         "port": 3307, "runtime_status": "ONLINE", "comment": "readyset cache"},
                    ]}
                    topology = topology_links.classify_proxysql_topology(
                        report, profiles=profiles, open_profile=lambda p: conns[p["id"]],
                        current_profile_id="current")
                    self.assertEqual(len(topology["readyset_nodes"]), 1)
                    node = topology["readyset_nodes"][0]
                    self.assertEqual(node["profile_id"], expected)
                    self.assertEqual(node["verified"], expected is not None)

    def test_probe_connection_unreported_status_is_none_never_online(self):
        # A probe whose SHOW READYSET STATUS fails reports no Status: an
        # unreported state is None, never a default "Online" (S3).
        profile = {"id": "p-rs", "name": "Readyset", "host": "127.0.0.1", "port": 13307}
        data = topology_links._probe_connection(FakeProbeConn("readyset", no_status=True), profile)
        self.assertIsNone(data["status_text"])
        self.assertIsNone(data["replication_status"])

        # Through probe_saved_profiles into tier discovery, the tier status stays None.
        probed, _ = topology_links.probe_saved_profiles(
            [dict(profile, remember_password=True, group="lab")],
            lambda p: FakeProbeConn("readyset", no_status=True),
            skip_profile_id="current", current={"id": "current", "group": "lab"})
        self.assertEqual(len(probed), 1)
        self.assertIsNone(probed[0]["status_text"])
        topology_links._reset_cache_for_tests()
        tiers = topology_links.discover_database_tiers(
            FakeProbeConn("database", hostname="mysql-primary", uuid="aaaa-1111"),
            {"profile_host": "127.0.0.1", "profile_port": 13306, "hostname": "mysql-primary",
             "port": 3306, "server_uuid": "aaaa-1111"},
            profiles=[dict(profile, remember_password=True, group="lab"),
                      {"id": "current", "group": "lab"}],
            open_profile=lambda p: FakeProbeConn(
                "readyset", no_status=True,
                upstreams=[{"host": "mysql-primary", "port": "3306", "database": "shop", "user": "rs",
                            "weight": "100", "purpose": "cdc", "active_cdc": "true", "health": "up",
                            "connections": "2"}]),
            observer_host="127.0.0.1",
            current_profile_id="current",
        )
        self.assertEqual(len(tiers["readyset"]), 1)
        self.assertIsNone(tiers["readyset"][0]["status"])

        # A reported status still comes through verbatim.
        ok = topology_links._probe_connection(
            FakeProbeConn("readyset", status="Snapshot In Progress"), profile)
        self.assertEqual(ok["status_text"], "Snapshot In Progress")

    def test_cdc_fallback_health_stays_unread_without_host_evidence(self):
        # With no upstream_servers rows, the CDC Server fallback host gets no
        # health: Database Connection is an aggregate any proxy upstream can
        # satisfy, not evidence about the CDC host itself.
        topo = topology_links.build_readyset_topology(
            {"Status": "Online", "Replication Status": "Running",
             "Database Connection": "Connected", "CDC Server": "mysql-primary:3306"},
            [],
            [],
            "adapter",
            "stable-250605",
            profiles=[{"id": "current", "group": "lab"}],
            open_profile=None,
            current_profile_id="current",
        )
        self.assertEqual(len(topo["upstreams"]), 1)
        fallback = topo["upstreams"][0]
        self.assertEqual(fallback["host"], "mysql-primary")
        self.assertIsNone(fallback["health"])
        self.assertTrue(fallback["active_cdc"])
        self.assertTrue(fallback["is_cdc"])

        topo_down = topology_links.build_readyset_topology(
            {"Status": "Online", "Database Connection": "Unreachable", "CDC Server": "mysql-primary:3306"},
            [], [], "adapter", "stable-250605",
            profiles=[{"id": "current", "group": "lab"}],
            open_profile=None, current_profile_id="current")
        self.assertIsNone(topo_down["upstreams"][0]["health"])

    def test_active_cdc_null_is_preserved_through_both_topologies(self):
        # Readyset returns a null active_cdc when its controller cannot answer;
        # null must reach both topology representations, never become false.
        topo = topology_links.build_readyset_topology(
            {"Status": "Online", "Replication Status": "Running", "CDC Server": "mysql-primary:3306"},
            [{"host": "mysql-primary", "port": 3306, "purpose": "cdc, query",
              "active_cdc": None, "health": "up", "connections": 2}],
            [], "adapter", "stable-250605",
            profiles=[{"id": "current", "group": "lab"}],
            open_profile=None, current_profile_id="current")
        self.assertIsNone(topo["upstreams"][0]["active_cdc"])
        self.assertTrue(topo["upstreams"][0]["is_cdc"])

        profiles = [{"id": "p-rs", "name": "Readyset", "host": "127.0.0.1", "port": 3307,
                     "remember_password": True, "group": "lab"},
                    {"id": "current", "group": "lab"}]
        rs_conn = FakeProbeConn(
            "readyset",
            upstreams=[{"host": "mysql-primary", "port": "3306", "database": "shop", "user": "rs",
                        "weight": "100", "purpose": "cdc, query", "active_cdc": None,
                        "health": "up", "connections": "2"}],
        )
        px_report = {
            "servers": [
                {"key": "30|127.0.0.1|3307", "hostgroup_id": 30, "hostname": "127.0.0.1", "port": 3307,
                 "runtime_status": "ONLINE", "weight": 100, "comment": "readyset cache",
                 "hostgroup_roles": [], "pool": {}},
            ],
            "query_rules": [],
        }
        px_topo = topology_links.classify_proxysql_topology(
            px_report, profiles=profiles, open_profile=lambda p: rs_conn,
            observer_host="127.0.0.1", current_profile_id="current")
        self.assertEqual(len(px_topo["readyset_nodes"]), 1)
        self.assertIsNone(px_topo["readyset_nodes"][0]["upstreams"][0]["active_cdc"])

    def test_proxysql_config_only_status_is_not_runtime_health(self):
        # A row read only from main.mysql_servers keeps its runtime status null.
        px_report = {
            "servers": [
                {"key": "cfg|10|mysql-primary|3306", "hostgroup_id": 10, "hostname": "mysql-primary",
                 "port": 3306, "runtime_status": None, "status_detail": "configuration only",
                 "config": {"hostgroup_id": 10, "hostname": "mysql-primary", "port": 3306,
                            "status": "ONLINE", "weight": 100},
                 "settings": {}, "pool": None, "hostgroup_roles": ["writer"]},
            ],
            "query_rules": [],
        }
        topo = topology_links.classify_proxysql_topology(px_report, profiles=[], open_profile=None)
        node = topo["database_nodes"][0]
        self.assertIsNone(node["status"])
        self.assertEqual(node["status_source"], "config")
        self.assertEqual(node["configured_status"], "ONLINE")

        # A runtime row keeps its status and is marked runtime.
        rt_report = {
            "servers": [
                {"key": "rt|10|mysql-primary|3306", "hostgroup_id": 10, "hostname": "mysql-primary",
                 "port": 3306, "runtime_status": "SHUNNED", "config": {"status": "ONLINE"},
                 "settings": {}, "pool": {}, "hostgroup_roles": []},
            ],
            "query_rules": [],
        }
        rt_node = topology_links.classify_proxysql_topology(rt_report, profiles=[], open_profile=None)["database_nodes"][0]
        self.assertEqual(rt_node["status"], "SHUNNED")
        self.assertEqual(rt_node["status_source"], "runtime")

    def test_proxysql_probe_config_fallback_marks_backend_status_unread(self):
        # When the topology probe can only read mysql_servers (configuration),
        # the Readyset topology's ProxySQL backend status stays null.
        profiles = [
            {"id": "p-px", "name": "ProxySQL Admin", "host": "127.0.0.1", "port": 16032,
             "remember_password": True, "group": "lab"},
            {"id": "p-pri", "name": "MySQL Primary", "host": "127.0.0.1", "port": 13306,
             "remember_password": True, "group": "lab"},
            {"id": "current", "group": "lab"},
        ]
        conns = {
            "p-px": FakeProbeConn(
                "proxysql", no_runtime=True,
                servers=[
                    {"hostgroup_id": "30", "hostname": "127.0.0.1", "port": "3307", "status": "ONLINE",
                     "weight": "100", "comment": "readyset cache"},
                    {"hostgroup_id": "10", "hostname": "mysql-primary", "port": "3306", "status": "ONLINE",
                     "weight": "100", "comment": "writer"},
                ],
                rules=[{"rule_id": "100", "active": "1", "destination_hostgroup": "30",
                        "mirror_hostgroup": None, "comment": 'readyset-meta: {"mode":"cached"}'}],
            ),
            "p-pri": FakeProbeConn("database", hostname="mysql-primary", uuid="aaaa-1111"),
        }
        topo = topology_links.build_readyset_topology(
            {"Status": "Online", "Replication Status": "Running", "CDC Server": "mysql-primary:3306"},
            [{"host": "mysql-primary", "port": 3306, "purpose": "cdc, query",
              "active_cdc": True, "health": "up", "connections": 2}],
            [], "adapter", "stable-250605",
            profiles=profiles,
            open_profile=lambda p: conns[p["id"]],
            observer_host="127.0.0.1",
            current_profile_id="current",
            current_endpoint=("127.0.0.1", 3307),
        )
        self.assertEqual(len(topo["proxysql_nodes"]), 1)
        self.assertIsNone(topo["proxysql_nodes"][0]["backend_status"])
        self.assertEqual(topo["proxysql_nodes"][0]["status_source"], "config")

        # The runtime table gives a real runtime status.
        topology_links._reset_cache_for_tests()
        conns["p-px"] = FakeProbeConn(
            "proxysql",
            servers=[
                {"hostgroup_id": "30", "hostname": "127.0.0.1", "port": "3307", "status": "ONLINE",
                 "weight": "100", "comment": "readyset cache"},
                {"hostgroup_id": "10", "hostname": "mysql-primary", "port": "3306", "status": "ONLINE",
                 "weight": "100", "comment": "writer"},
            ],
            rules=[{"rule_id": "100", "active": "1", "destination_hostgroup": "30",
                    "mirror_hostgroup": None, "comment": 'readyset-meta: {"mode":"cached"}'}],
        )
        topo_rt = topology_links.build_readyset_topology(
            {"Status": "Online", "Replication Status": "Running", "CDC Server": "mysql-primary:3306"},
            [{"host": "mysql-primary", "port": 3306, "purpose": "cdc, query",
              "active_cdc": True, "health": "up", "connections": 2}],
            [], "adapter", "stable-250605",
            profiles=profiles,
            open_profile=lambda p: conns[p["id"]],
            observer_host="127.0.0.1",
            current_profile_id="current",
            current_endpoint=("127.0.0.1", 3307),
        )
        self.assertEqual(topo_rt["proxysql_nodes"][0]["backend_status"], "ONLINE")
        self.assertEqual(topo_rt["proxysql_nodes"][0]["status_source"], "runtime")

    def test_readyset_tier_carries_problem_signals(self):
        # topology_tiers.readyset[] copies Database Connection and a boolean
        # replication_error so the tiers can draw problems Status never shows.
        profile = {"id": "p-rs", "name": "Readyset", "host": "127.0.0.1", "port": 13307}
        data = topology_links._probe_connection(
            FakeProbeConn("readyset", db_connection="Unreachable",
                          last_replicator_error="binlog connection lost"), profile)
        self.assertEqual(data["database_connection"], "Unreachable")
        self.assertTrue(data["replication_error"])

        probed, _ = topology_links.probe_saved_profiles(
            [dict(profile, remember_password=True, group="lab")],
            lambda p: FakeProbeConn("readyset", db_connection="Unreachable",
                                    last_replicator_error="binlog connection lost"),
            skip_profile_id="current", current={"id": "current", "group": "lab"})
        self.assertTrue(probed[0]["replication_error"])

        topology_links._reset_cache_for_tests()
        tiers = topology_links.discover_database_tiers(
            FakeProbeConn("database", hostname="mysql-primary", uuid="aaaa-1111"),
            {"profile_host": "127.0.0.1", "profile_port": 13306, "hostname": "mysql-primary",
             "port": 3306, "server_uuid": "aaaa-1111"},
            profiles=[dict(profile, remember_password=True, group="lab"),
                      {"id": "current", "group": "lab"}],
            open_profile=lambda p: FakeProbeConn(
                "readyset", db_connection="Unreachable", last_replicator_error="binlog connection lost",
                upstreams=[{"host": "mysql-primary", "port": "3306", "database": "shop", "user": "rs",
                            "weight": "100", "purpose": "cdc", "active_cdc": "true", "health": "up",
                            "connections": "2"}]),
            observer_host="127.0.0.1",
            current_profile_id="current",
        )
        self.assertEqual(len(tiers["readyset"]), 1)
        tier = tiers["readyset"][0]
        self.assertEqual(tier["database_connection"], "Unreachable")
        self.assertTrue(tier["replication_error"])

        # A healthy Readyset reports Connected and no replication error.
        topology_links._reset_cache_for_tests()
        ok_tiers = topology_links.discover_database_tiers(
            FakeProbeConn("database", hostname="mysql-primary", uuid="aaaa-1111"),
            {"profile_host": "127.0.0.1", "profile_port": 13306, "hostname": "mysql-primary",
             "port": 3306, "server_uuid": "aaaa-1111"},
            profiles=[dict(profile, remember_password=True, group="lab"),
                      {"id": "current", "group": "lab"}],
            open_profile=lambda p: FakeProbeConn(
                "readyset",
                upstreams=[{"host": "mysql-primary", "port": "3306", "database": "shop", "user": "rs",
                            "weight": "100", "purpose": "cdc", "active_cdc": "true", "health": "up",
                            "connections": "2"}]),
            observer_host="127.0.0.1",
            current_profile_id="current",
        )
        self.assertEqual(ok_tiers["readyset"][0]["database_connection"], "Connected")
        self.assertFalse(ok_tiers["readyset"][0]["replication_error"])

    def test_channel_source_contrary_uuid_blocks_address_attachment(self):
        uuid_primary = "aaaaaaaa-0000-4000-8000-000000000001"
        uuid_replica = "bbbbbbbb-0000-4000-8000-000000000002"
        uuid_other = "cccccccc-0000-4000-8000-000000000003"
        profiles = [
            {"id": "p-pri", "name": "MySQL Primary", "host": "127.0.0.1", "port": 13306,
             "remember_password": True, "group": "lab"},
            {"id": "p-rep", "name": "MySQL Replica", "host": "127.0.0.1", "port": 13308,
             "remember_password": True, "group": "lab"},
            {"id": "current", "group": "lab"},
        ]

        def run_case(source_uuid):
            topology_links._reset_cache_for_tests()
            channel = {
                "Channel_Name": "", "Source_Host": "mysql-primary", "Source_Port": "3306",
                "Replica_IO_Running": "Yes", "Replica_SQL_Running": "Yes", "Seconds_Behind_Source": "0",
            }
            if source_uuid is not None:
                channel["Source_UUID"] = source_uuid
            conns = {
                "p-pri": FakeProbeConn("database", hostname="mysql-primary", uuid=uuid_primary, server_id=1),
                "p-rep": FakeProbeConn("database", hostname="mysql-replica", uuid=uuid_replica, server_id=2,
                                       read_only=True, channels=[channel]),
            }
            return topology_links.build_readyset_topology(
                {"Status": "Online", "Replication Status": "Running", "CDC Server": "mysql-primary:3306"},
                [{"host": "mysql-primary", "port": 3306, "purpose": "cdc, query",
                  "active_cdc": True, "health": "up", "connections": 2}],
                [], "adapter", "stable-261022",
                profiles=profiles, open_profile=lambda p: conns[p["id"]],
                observer_host="127.0.0.1", current_profile_id="current",
                current_endpoint=("127.0.0.1", 3307),
            )

        # Contrary source UUID at the same address must never attach the replica.
        contrary = run_case(uuid_other)
        self.assertEqual(contrary["database_peers"], [])

        # Matching source UUID attaches the replica with identity evidence.
        matched = run_case(uuid_primary)
        self.assertEqual(len(matched["database_peers"]), 1)
        self.assertEqual(matched["database_peers"][0]["profile_id"], "p-rep")
        self.assertEqual(matched["database_peers"][0]["evidence"], "server_uuid")

        # Absent source UUID still falls back to address matching.
        no_uuid = run_case(None)
        self.assertEqual(len(no_uuid["database_peers"]), 1)
        self.assertEqual(no_uuid["database_peers"][0]["profile_id"], "p-rep")
        self.assertEqual(no_uuid["database_peers"][0]["evidence"], "channel_source")

    def test_match_database_endpoint_and_peers_reject_contrary_uuid(self):
        uuid_a = "aaaaaaaa-0000-4000-8000-000000000001"
        uuid_b = "bbbbbbbb-0000-4000-8000-000000000002"
        uuid_c = "cccccccc-0000-4000-8000-000000000003"
        gcomm_a = "11111111-0000-4000-8000-000000000001"
        gcomm_b = "22222222-0000-4000-8000-000000000002"
        gcomm_c = "33333333-0000-4000-8000-000000000003"
        probed = [{
            "kind": "database", "profile_id": "p-db", "profile_name": "DB",
            "profile_host": "10.0.0.10", "profile_port": 3306, "ssh_host": None,
            "hostname": "db-internal", "port": 3306, "report_host": "db-internal", "report_port": 3306,
            "server_uuid": uuid_a,
        }]
        # Both direct endpoint and hostname_port fallbacks reject a contrary UUID.
        self.assertEqual(topology_links.match_database_endpoint("10.0.0.10", 3306, probed, uuid=uuid_b), (None, None))
        self.assertEqual(topology_links.match_database_endpoint("db-internal", 3306, probed, uuid=uuid_b), (None, None))
        self.assertEqual(topology_links.match_database_endpoint("10.0.0.10", 3306, probed, uuid=uuid_a)[1], "server_uuid")
        self.assertEqual(topology_links.match_database_endpoint("10.0.0.10", 3306, probed, uuid=None)[1], "endpoint")
        self.assertEqual(topology_links.match_database_endpoint("db-internal", 3306, probed, uuid=None)[1], "hostname_port")

        # SHOW REPLICAS and GR cluster_members reject a saved server at the same address when UUIDs differ.
        profiles = [
            {"id": "p-pri", "name": "Primary", "host": "10.0.0.11", "port": 3306, "remember_password": True, "group": "lab"},
            {"id": "p-peer", "name": "Other Server", "host": "10.0.0.12", "port": 3306, "remember_password": True, "group": "lab"},
            {"id": "current", "group": "lab"},
        ]
        for rep_uuid, member_uuid, expected_ids in (
            (uuid_c, uuid_c, (None, None)),
            (uuid_b, uuid_b, ("p-peer", "p-peer")),
        ):
            with self.subTest(rep_uuid=rep_uuid):
                topology_links._reset_cache_for_tests()
                conns = {
                    "p-pri": FakeProbeConn(
                        "database", hostname="10.0.0.11", uuid=uuid_a, gr_group="dddddddd-0000-4000-8000-000000000004",
                        gr_members=[
                            [uuid_a, "10.0.0.11", "3306", "ONLINE", "PRIMARY"],
                            [member_uuid, "10.0.0.12", "3306", "ONLINE", "SECONDARY"],
                        ],
                        replicas=[{"Host": "10.0.0.12", "Port": "3306", "Replica_UUID": rep_uuid, "Server_id": "2"}],
                    ),
                    "p-peer": FakeProbeConn("database", hostname="10.0.0.12", uuid=uuid_b),
                }
                topo = topology_links.build_readyset_topology(
                    {"Status": "Online", "Replication Status": "Running", "CDC Server": "10.0.0.11:3306"},
                    [{"host": "10.0.0.11", "port": 3306, "purpose": "cdc", "active_cdc": True, "health": "up"}],
                    [], "adapter", "stable-261022",
                    profiles=profiles, open_profile=lambda p: conns[p["id"]],
                    observer_host="127.0.0.1", current_profile_id="current",
                )
                cluster_peers = [p for p in topo["database_peers"] if p["relation"] == "cluster_member"]
                self.assertEqual(len(cluster_peers), 1)
                self.assertEqual(cluster_peers[0]["profile_id"], expected_ids[0])
                if expected_ids[0]:
                    self.assertEqual(cluster_peers[0]["evidence"], "server_uuid")

                # Separately exercise SHOW REPLICAS without a cluster member hiding the same host.
                topology_links._reset_cache_for_tests()
                conns["p-pri"] = FakeProbeConn(
                    "database", hostname="10.0.0.11", uuid=uuid_a,
                    replicas=[{"Host": "10.0.0.12", "Port": "3306", "Replica_UUID": rep_uuid, "Server_id": "2"}],
                )
                rep_topo = topology_links.build_readyset_topology(
                    {"Status": "Online", "Replication Status": "Running", "CDC Server": "10.0.0.11:3306"},
                    [{"host": "10.0.0.11", "port": 3306, "purpose": "cdc", "active_cdc": True, "health": "up"}],
                    [], "adapter", "stable-261022",
                    profiles=profiles, open_profile=lambda p: conns[p["id"]],
                    observer_host="127.0.0.1", current_profile_id="current",
                )
                self.assertEqual(len(rep_topo["database_peers"]), 1)
                self.assertEqual(rep_topo["database_peers"][0]["profile_id"], expected_ids[1])

        # PXC wsrep cluster_members compare wsrep_node_uuid (not MySQL server_uuid).
        for peer_gcomm, expected_id in ((gcomm_c, None), (gcomm_b, "p-peer")):
            with self.subTest(pxc_gcomm=peer_gcomm):
                topology_links._reset_cache_for_tests()
                conns = {
                    "p-pri": FakeProbeConn(
                        "database", hostname="10.0.0.11", uuid=uuid_a, wsrep=True,
                        version_comment="Percona XtraDB Cluster (GPL)", cluster_name="pxc",
                        gcomm_uuid=gcomm_a,
                        wsrep_members=[
                            [gcomm_a, "pxc1", "10.0.0.11:3306"],
                            [peer_gcomm, "pxc2", "10.0.0.12:3306"],
                        ],
                    ),
                    "p-peer": FakeProbeConn(
                        "database", hostname="10.0.0.12", uuid=uuid_b, wsrep=True,
                        version_comment="Percona XtraDB Cluster (GPL)", cluster_name="pxc",
                        gcomm_uuid=gcomm_b,
                    ),
                }
                pxc_topo = topology_links.build_readyset_topology(
                    {"Status": "Online", "Replication Status": "Running", "CDC Server": "10.0.0.11:3306"},
                    [{"host": "10.0.0.11", "port": 3306, "purpose": "cdc", "active_cdc": True, "health": "up"}],
                    [], "adapter", "stable-261022",
                    profiles=profiles, open_profile=lambda p: conns[p["id"]],
                    observer_host="127.0.0.1", current_profile_id="current",
                )
                self.assertEqual(len(pxc_topo["database_peers"]), 1)
                self.assertEqual(pxc_topo["database_peers"][0]["profile_id"], expected_id)


if __name__ == "__main__":
    unittest.main()
