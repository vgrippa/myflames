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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, sql, extra_flags=None, timeout=None):
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
                if self.cfg.get("admin"):
                    return xml_rows([])
                raise ConnectorError("ERROR 1146 (42S02): Table 'replication_status' doesn't exist")
            if upper == "SHOW READYSET STATUS":
                return xml_rows([
                    {"name": "Status", "value": self.cfg.get("status", "Online")},
                    {"name": "Replication Status", "value": self.cfg.get("repl_status", "Online")},
                    {"name": "CDC Server", "value": self.cfg.get("cdc_server", "mysql-primary:3306")},
                ])
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
            vmap = {
                "version": self.cfg.get("version", "8.4.11"),
                "version_comment": self.cfg.get("version_comment", "MySQL Community Server - GPL"),
                "hostname": self.cfg.get("hostname", "mysql-primary"),
                "port": str(self.cfg.get("port", 3306)),
                "report_host": self.cfg.get("report_host", self.cfg.get("hostname", "mysql-primary")),
                "report_port": str(self.cfg.get("port", 3306)),
                "server_id": str(self.cfg.get("server_id", 1)),
                "server_uuid": self.cfg.get("uuid", "11111111-1111-1111-1111-111111111111"),
                "read_only": "ON" if self.cfg.get("read_only") else "OFF",
                "super_read_only": "ON" if self.cfg.get("read_only") else "OFF",
                "wsrep_on": "ON" if self.cfg.get("wsrep") else "OFF",
                "wsrep_cluster_name": self.cfg.get("cluster_name", ""),
                "group_replication_group_name": self.cfg.get("gr_group", ""),
                "gtid_mode": "ON",
            }
            return batch_rows([[k, v] for k, v in vmap.items() if v != ""])
        if "INNODB_CLUSTER_METADATA" in upper:
            return batch_rows([["mysql_innodb_cluster_metadata"]]) if self.cfg.get("innodb_cluster") else ""
        if "REPLICATION_GROUP_MEMBERS" in upper:
            return batch_rows(self.cfg.get("gr_members", []))
        if "WSREP_CLUSTER_MEMBERS" in upper:
            return batch_rows(self.cfg.get("wsrep_members", []))
        if "SHOW REPLICA STATUS" in upper or "SHOW ALL SLAVES STATUS" in upper:
            return xml_rows(self.cfg.get("channels", []))
        if "SHOW REPLICAS" in upper or "SHOW SLAVE HOSTS" in upper:
            return xml_rows(self.cfg.get("replicas", []))
        if "SESSION_CONNECT_ATTRS" in upper:
            return batch_rows(self.cfg.get("connect_attrs", []))
        if "_READYSET_HEARTBEAT" in upper:
            return batch_rows([[1]]) if self.cfg.get("heartbeat") else ""
        return ""


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
            {"id": "p-px", "name": "ProxySQL Admin", "host": "127.0.0.1", "port": 16032, "remember_password": True},
            {"id": "p-rs", "name": "Readyset Cache", "host": "127.0.0.1", "port": 13307, "remember_password": True},
            {"id": "p-pri", "name": "MySQL Primary", "host": "127.0.0.1", "port": 13306, "remember_password": True},
            {"id": "p-rep", "name": "MySQL Replica", "host": "127.0.0.1", "port": 13308, "remember_password": True},
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
            {"Status": "Online", "Replication Status": "Online", "CDC Server": "mysql-primary:3306"},
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
            {"id": "p-galera", "name": "Galera Node 1", "host": "127.0.0.1", "port": 13316, "remember_password": True},
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
            {"Status": "Online", "Replication Status": "Online", "CDC Server": "galera-1:3306"},
            [{"host": "galera-1", "port": 3306, "purpose": "cdc, query", "active_cdc": True, "health": "up", "connections": 2}],
            [],
            "adapter",
            "stable-250605",
            profiles=profiles,
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
            "ssh_host": "bastion.example.com", "remember_password": True,
        }
        db_conn = FakeProbeConn("database", hostname="remote-db", port=3306)
        probed, _ = topology_links.probe_saved_profiles([ssh_profile], lambda p: db_conn)
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
            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError("Failed using super-secret-probe-pw on mysql://u:url-pw@db:3306/s")

        bad_profile = {"id": "p-bad", "name": "Broken RS", "host": "127.0.0.1", "port": 3307, "remember_password": True}
        _, notes = topology_links.probe_saved_profiles([bad_profile], lambda p: FailingConn())
        joined = "\n".join(notes)
        self.assertNotIn("super-secret-probe-pw", joined)
        self.assertNotIn("url-pw", joined)
        self.assertIn("[redacted]", joined)

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
                    "hostgroup_roles": [],
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
            {"id": "p-px", "name": "rs-proxysql", "host": "127.0.0.1", "port": 34532, "remember_password": True},
            {"id": "p-rs", "name": "rs-adapter", "host": "127.0.0.1", "port": 34507, "database": "shop", "remember_password": True},
            {"id": "p-rs-admin", "name": "rs-admin", "host": "127.0.0.1", "port": 34536, "remember_password": True},
            {"id": "p-pri", "name": "rs-primary", "host": "127.0.0.1", "port": 34505, "remember_password": True},
            {"id": "p-rep-db", "name": "rs-replica", "host": "127.0.0.1", "port": 34506, "remember_password": True},
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
        unrelated_px = {"id": "p-px-other", "name": "Other ProxySQL", "host": "10.99.0.10", "port": 6032, "remember_password": True}
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


if __name__ == "__main__":
    unittest.main()
