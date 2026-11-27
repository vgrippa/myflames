"""VillageSQL identity keeps MySQL rules and gates codebase-specific reads.

These are synthetic identity/metadata doubles, not live-server evidence.
The server variable names and EXTENSION_ADMIN registration are verified in
VillageSQL's villagesql/sql/initialize.cc and sql/sys_vars.cc.
"""
import datetime
import os
import re
import sys
import unittest

# Support both the module command and the repository's isolated test runner.
sys.path.insert(0, os.path.dirname(__file__))

from myflames import dbops, parameters, perf_common, privileges, server_checks, server_info, variables
from myflames.connector import ConnectorError, MySQLConnection, PartialReadError
from myflames.perf_common import Reader, Server


VERSION = "8.4.11-villagesql-0.0.8"
DEV_VERSION = "9.7.2-villagesql-0.0.9-dev-543f42e0601"
COMMENT = "VillageSQL Server - GPL"
PERCONA_COMMENT = "Percona Server (GPL), Release 11, Revision 57878ff8"
SOURCE_COMMENT = "Source distribution"
# Exact combinations captured by the coordinator from all three 0.0.8 images.
LIVE_IDENTITIES = (
    (VERSION, "mysql-8.4_0.0.8", "mysql", "VillageSQL 0.0.8 (MySQL 8.4.11)"),
    (VERSION, "percona-8.4_0.0.8", "percona", "VillageSQL 0.0.8 (Percona Server 8.4.11)"),
    ("9.7.2-villagesql-0.0.8", "mysql-9.7_0.0.8", "mysql", "VillageSQL 0.0.8 (MySQL 9.7.2)"),
)
VILLAGE_VARIABLES = frozenset((
    "villagesql_server_version", "villagesql_schema_version", "villagesql_build_info",
    "villagesql_vef_server_protocol", "vsql_allow_preview_extensions", "veb_dir",
))
DOCUMENTATION = "https://villagesql.com/docs/en/8.4/stable/reference.md"


def village_server(codebase=None, version=VERSION, comment=COMMENT):
    values = {"version": version, "version_comment": comment, "performance_schema": "ON"}
    if codebase is not None:
        values["villagesql_server_version"] = codebase
    return Server(values, {"Uptime": "100"})


class IdentityConnection(MySQLConnection):
    """Exercise real cached metadata methods without a client or connection."""

    def __init__(self, version=VERSION, comment=COMMENT, codebase="mysql-8.4_0.0.8", failure=None):
        super(IdentityConnection, self).__init__(host="identity.invalid", user="test")
        self.version = version
        self.comment = comment
        self.codebase = codebase
        self.failure = failure
        self.calls = []

    def server_version(self):
        return self.version

    def query_rows(self, sql):
        self.calls.append(sql)
        statement = re.sub(r"^/\*.*?\*/\s*", "", sql).strip()
        if statement == "SELECT @@version_comment":
            return [[self.comment]]
        if statement == "SELECT @@villagesql_server_version":
            if self.failure:
                raise ConnectorError(self.failure)
            return [] if self.codebase is None else [[self.codebase]]
        raise AssertionError("Unexpected metadata query: " + sql)

    def run(self, sql, extra_flags=None, **kwargs):
        self.calls.append(sql)
        statement = re.sub(r"^/\*.*?\*/\s*", "", sql).strip()
        if statement == perf_common.SERVER_STATE_SQL:
            return self.version + "\tON\n"
        if statement.startswith("SHOW GLOBAL VARIABLES"):
            values = {"version": self.version, "version_comment": self.comment,
                      "performance_schema": "ON", "userstat": "OFF"}
            if (self.codebase is not None and not self.failure and
                    server_info.product_identity(self.version, self.comment)["flavor"] == "villagesql"):
                values["villagesql_server_version"] = self.codebase
            names = re.findall(r"'([^']+)'", statement)
            return "".join("%s\t%s\n" % (name, values[name]) for name in names if name in values)
        if statement.startswith("SHOW GLOBAL STATUS"):
            return "Uptime\t100\n"
        if statement == "SELECT @@villagesql_server_version":
            if self.failure:
                raise ConnectorError(self.failure)
            return "" if self.codebase is None else self.codebase + "\n"
        raise AssertionError("Unexpected preflight query: " + sql)

    def codebase_reads(self):
        return [sql for sql in self.calls if "villagesql_server_version" in sql]


class TestVillageIdentity(unittest.TestCase):
    def test_live_image_identity_combinations_and_labels(self):
        for version, codebase, base, label in LIVE_IDENTITIES:
            with self.subTest(codebase=codebase):
                identity = server_info.product_identity(version, SOURCE_COMMENT, codebase)
                self.assertEqual(identity, {"engine": "mysql", "flavor": "villagesql",
                                            "product": "VillageSQL", "base": base})
                server = village_server(codebase, version, SOURCE_COMMENT)
                self.assertEqual(server.base, base)
                self.assertEqual(server_info.server_label(perf_common.base_report("identity-1.0", server)), label)

    def test_suffix_comment_and_percona_comment_name_villagesql(self):
        for version, comment, base in (
                (VERSION, COMMENT, None), (VERSION, PERCONA_COMMENT, "percona"),
                (VERSION, "", None), (DEV_VERSION, COMMENT, None),
                ("8.4.11", "VILLAGESQL SERVER - GPL", None)):
            with self.subTest(version=version, comment=comment):
                self.assertEqual(server_info.product_identity(version, comment),
                                 {"engine": "mysql", "flavor": "villagesql",
                                  "product": "VillageSQL", "base": base})
                self.assertEqual(server_info.detect_flavor(version, comment), "mysql")

    def test_all_three_codebases_and_codebase_precedence(self):
        for codebase, expected in (("mysql-8.4_0.0.8", "mysql"),
                                   ("percona-8.4_0.0.8", "percona"),
                                   ("mysql-9.7_0.0.9-dev", "mysql")):
            with self.subTest(codebase=codebase):
                identity = server_info.product_identity(VERSION, PERCONA_COMMENT, codebase)
                self.assertEqual(identity["base"], expected)
                self.assertEqual(identity["flavor"], "villagesql")
        self.assertIsNone(server_info.product_identity(VERSION, COMMENT, "unknown_0.0.8")["base"])

    def test_codebase_semver_accepts_source_supported_prerelease_and_build_identifiers(self):
        # villagesql/include/semver.h documents both optional Semver segments.
        for suffix in ("", "-dev", "-dev-543f42e0601", "-alpha.1", "+20130313144700",
                       "-beta+exp.sha.5114f85"):
            self.assertEqual(server_info.product_identity(VERSION, SOURCE_COMMENT,
                             "percona-8.4_0.0.8" + suffix)["base"], "percona")

    def test_existing_products_keep_exact_three_key_shape_and_priority(self):
        cases = (
            ("8.4.11", "MySQL Community Server - GPL", "mysql", "mysql", "MySQL"),
            ("8.4.11", "", "mysql", "mysql", "MySQL"),
            ("8.4.11", SOURCE_COMMENT, "mysql", "mysql", "MySQL"),
            ("9.7.2", SOURCE_COMMENT, "mysql", "mysql", "MySQL"),
            ("8.4.11-11", PERCONA_COMMENT, "mysql", "percona", "Percona Server"),
            ("8.4.11-11.1", "Percona XtraDB Cluster (GPL)", "mysql", "percona", "Percona XtraDB Cluster"),
            ("11.4.13-MariaDB", "MariaDB Server", "mariadb", "mariadb", "MariaDB"),
            ("11.4.13", "MariaDB Server", "mariadb", "mariadb", "MariaDB"),
            (VERSION, " Readyset ", "mysql", "readyset", "Readyset"),
            (VERSION, "MariaDB VillageSQL", "mariadb", "mariadb", "MariaDB"),
        )
        for version, comment, engine, flavor, product in cases:
            with self.subTest(version=version, comment=comment):
                self.assertEqual(server_info.product_identity(version, comment),
                                 {"engine": engine, "flavor": flavor, "product": product})

    def test_version_only_identity_never_confuses_village_suffix_with_percona_release(self):
        for version in (VERSION, DEV_VERSION):
            self.assertEqual(server_info.identity_from_version(version),
                             {"engine": "mysql", "flavor": "villagesql", "product": "VillageSQL", "base": None})
        for version, flavor in (("8.4.11-11", "percona"), ("8.4.11-11.1", "percona"),
                                ("8.4.11", "mysql"), ("9.7.2-commercial", "mysql"),
                                ("11.4.13-MariaDB", "mariadb")):
            self.assertEqual(server_info.identity_from_version(version)["flavor"], flavor)

    def test_release_and_base_versions_drop_only_the_development_sha(self):
        self.assertEqual(server_info.villagesql_version(VERSION), "0.0.8")
        self.assertEqual(server_info.villagesql_version(DEV_VERSION), "0.0.9-dev")
        self.assertEqual(server_info.base_version(VERSION), "8.4.11")
        self.assertEqual(server_info.base_version(DEV_VERSION), "9.7.2")
        for version in (None, "", "8.4.11", "8.4.11-11", "11.4.13-MariaDB"):
            self.assertIsNone(server_info.villagesql_version(version))

    def test_percona_capability_requires_proved_base_on_villagesql(self):
        for flavor, base, expected in (("percona", None, True), ("percona", "mysql", True),
                                       ("villagesql", "percona", True), ("villagesql", "mysql", False),
                                       ("villagesql", None, False), ("mysql", "percona", False),
                                       ("mariadb", None, False), ("readyset", None, False)):
            self.assertEqual(server_info.has_percona_features(flavor, base), expected, (flavor, base))
        self.assertTrue(server_info.percona_variable("USERSTAT", "villagesql", (8, 4, 11), "percona"))
        self.assertFalse(server_info.percona_variable("userstat", "villagesql", (8, 4, 11), "mysql"))
        self.assertFalse(server_info.percona_variable("userstat", "villagesql", (8, 4, 11)))

    def test_labels_keep_product_version_and_proved_base(self):
        for version, base, expected in (
                (VERSION, "mysql", "VillageSQL 0.0.8 (MySQL 8.4.11)"),
                (VERSION, "percona", "VillageSQL 0.0.8 (Percona Server 8.4.11)"),
                (VERSION, None, "VillageSQL 0.0.8 (8.4.11)"),
                (DEV_VERSION, "mysql", "VillageSQL 0.0.9-dev (MySQL 9.7.2)")):
            report = {"engine": "mysql", "flavor": "villagesql", "server_product": "VillageSQL",
                      "server_version": version, "server_base": base}
            self.assertEqual(server_info.product_name(report), "VillageSQL")
            self.assertEqual(server_info.server_label(report), expected)
            report["version"] = report.pop("server_version")
            self.assertEqual(server_info.server_label(report, "version"), expected)

    def test_supported_floor_uses_mysql_rules(self):
        for version in (VERSION, DEV_VERSION, "5.7.44-villagesql-0.0.8"):
            self.assertIsNone(server_info.unsupported_version_note(version, COMMENT))
        self.assertIn("MySQL 5.7", server_info.unsupported_version_note("5.6.51-villagesql-0.0.8", COMMENT))

    def test_hostile_codebase_metadata_cannot_supply_capabilities_or_label_text(self):
        for codebase in ("percona-" + "x" * 100000, "percona-8.4_0.0.8\x00",
                         "percona-8.4_0.0.8\n", "mysql-8.4_0.0.8\u202e",
                         "percona-８.４_０.０.８",
                         "percona-mysql-8.4_0.0.8", "percona-8.4_0.0.8<script>alert(1)</script>",
                         '<img src=x onerror="alert(1)">'):
            with self.subTest(codebase=codebase[:80]):
                identity = server_info.product_identity(VERSION, SOURCE_COMMENT, codebase)
                self.assertEqual(identity, {"engine": "mysql", "flavor": "villagesql",
                                            "product": "VillageSQL", "base": None})
                report = perf_common.base_report("identity-1.0", village_server(codebase, comment=SOURCE_COMMENT))
                self.assertFalse(server_info.has_percona_features(report["flavor"], report["server_base"]))
                self.assertEqual(server_info.server_label(report), "VillageSQL 0.0.8 (8.4.11)")
                # Even a hostile imported report's base never becomes label text.
                report["server_base"] = codebase
                self.assertEqual(server_info.server_label(report), "VillageSQL 0.0.8 (8.4.11)")

    def test_percona_metadata_prefix_cannot_reclassify_a_plain_mysql_build(self):
        for codebase in ("percona-8.4_0.0.8", "percona-<script>alert(1)</script>"):
            identity = server_info.product_identity("8.4.11", SOURCE_COMMENT, codebase)
            self.assertEqual(identity, {"engine": "mysql", "flavor": "mysql", "product": "MySQL"})
            server = village_server(codebase, "8.4.11", SOURCE_COMMENT)
            self.assertIsNone(server.base)
            self.assertEqual(server_info.server_label(perf_common.base_report("identity-1.0", server)), "MySQL 8.4.11")


class TestVillageReports(unittest.TestCase):
    def test_base_is_additive_only_for_villagesql_envelopes(self):
        for codebase, base in ((None, None), ("mysql-8.4_0.0.8", "mysql"), ("percona-8.4_0.0.8", "percona")):
            server = village_server(codebase)
            self.assertEqual((server.engine, server.flavor, server.product, server.base),
                             ("mysql", "villagesql", "VillageSQL", base))
            report = perf_common.base_report("identity-1.0", server)
            self.assertEqual(report["server_base"], base)
            self.assertEqual(server.describe()["server_base"], base)
            self.assertEqual(perf_common.identity_fields("mysql", VERSION, COMMENT, codebase),
                             {"flavor": "villagesql", "server_product": "VillageSQL", "server_base": base})
        for version, comment in (("8.4.11", "MySQL Community Server - GPL"),
                                 ("8.4.11-11", PERCONA_COMMENT),
                                 ("11.4.13-MariaDB", "MariaDB Server"), ("8.0.26", "Readyset")):
            server = Server({"version": version, "version_comment": comment}, {})
            self.assertNotIn("server_base", perf_common.base_report("identity-1.0", server))
            self.assertNotIn("server_base", server.describe())
            self.assertNotIn("server_base", perf_common.identity_fields(server.engine, version, comment))

    def test_lifecycle_never_inherits_mysql_or_percona_dates(self):
        for codebase in ("mysql-8.4_0.0.8", "percona-8.4_0.0.8", "mysql-9.7_0.0.8", None):
            server = village_server(codebase, DEV_VERSION if codebase == "mysql-9.7_0.0.8" else VERSION)
            check = server_checks._version_support(server, datetime.date(2026, 11, 27))
            self.assertEqual(check["status"], "unknown")
            self.assertIn("VillageSQL", check["why"])
            self.assertIn("pre-1.0", check["why"])
            self.assertNotIn("Percona publishes", check["why"])
            self.assertNotIn("past end of life", check["why"])


class TestVillageMetadataReads(unittest.TestCase):
    def test_old_preflight_replay_allows_only_the_optional_unknown_name(self):
        from _perf_replay import ReplayConn
        captured = ("/*myflames:test*/ SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                    "('performance_schema', 'userstat', 'version', 'version_comment')")
        response = "performance_schema\tON\nversion\t8.4.11\nversion_comment\tSource distribution\n"
        recording = [[captured, response]]
        conn = ReplayConn(recording)
        request = captured[:-1] + ", 'villagesql_server_version')"
        values = perf_common.show_variables(Reader(conn, "/*myflames:test*/ "), perf_common.PREFLIGHT_VARIABLES)
        self.assertEqual(values, {"performance_schema": "ON", "version": "8.4.11", "version_comment": SOURCE_COMMENT})
        self.assertEqual(conn.calls, [(request, ["--comments"])])
        self.assertEqual(recording, [[captured, response]], "the original recording remains unchanged")
        with self.assertRaisesRegex(AssertionError, "unexpected SQL"):
            conn.run(captured[:-1] + ", 'unrelated_variable')")
        # A VillageSQL recording must supply its own actual metadata response.
        village = ReplayConn([[captured, response.replace("8.4.11", VERSION)]])
        with self.assertRaisesRegex(AssertionError, "unexpected SQL"):
            village.run(request)

    def test_cached_reader_never_queries_other_products(self):
        for version, comment in (("8.4.11", "MySQL Community Server - GPL"),
                                 ("8.4.11-11", PERCONA_COMMENT),
                                 ("11.4.13-MariaDB", "MariaDB Server"), (VERSION, "Readyset")):
            conn = IdentityConnection(version, comment)
            self.assertIsNone(conn.villagesql_server_version())
            self.assertIsNone(conn.villagesql_server_version())
            self.assertEqual(conn.codebase_reads(), [], (version, comment))

    def test_cached_reader_reads_once_and_failures_are_optional_and_cached(self):
        for failure, codebase, expected in ((None, "percona-8.4_0.0.8", "percona-8.4_0.0.8"),
                                            (None, None, None),
                                            ("ERROR 1193 (HY000): Unknown system variable", None, None),
                                            ("ERROR 2013: Lost connection", None, None)):
            conn = IdentityConnection(codebase=codebase, failure=failure)
            self.assertEqual(conn.villagesql_server_version(), expected)
            self.assertEqual(conn.villagesql_server_version(), expected)
            self.assertEqual(len(conn.codebase_reads()), 1)

    def test_server_does_not_read_codebase_and_connection_identity_caches_it(self):
        conn = IdentityConnection(codebase="percona-8.4_0.0.8")
        engine, version, version_tuple, enabled = perf_common._server(conn)
        self.assertEqual((engine, version, version_tuple, enabled), ("mysql", VERSION, (8, 4, 11), True))
        self.assertEqual(conn.codebase_reads(), [])
        self.assertEqual(perf_common.conn_identity(conn, engine, version),
                         {"flavor": "villagesql", "server_product": "VillageSQL", "server_base": "percona"})
        self.assertEqual(len(conn.codebase_reads()), 1)
        perf_common.conn_identity(conn, engine, version)
        self.assertEqual(len(conn.codebase_reads()), 1)

    def test_server_and_connection_identity_skip_other_products(self):
        for version, comment in (("8.4.11", ""), ("8.4.11-11", PERCONA_COMMENT),
                                 ("11.4.13-MariaDB", "MariaDB Server"), ("8.0.26", "Readyset")):
            conn = IdentityConnection(version, comment)
            engine, found_version, _, _ = perf_common._server(conn)
            self.assertNotIn("server_base", perf_common.conn_identity(conn, engine, found_version))
            self.assertFalse(any("villagesql" in sql.lower() for sql in conn.calls), conn.calls)

    def test_failed_codebase_preserves_the_view_and_unknown_base(self):
        conn = IdentityConnection(failure="ERROR 1193: Unknown system variable")
        engine, version, _, _ = perf_common._server(conn)
        fields = perf_common.conn_identity(conn, engine, version)
        self.assertIsNone(fields["server_base"])
        self.assertEqual(server_info.server_label(dict(fields, server_version=version)),
                         "VillageSQL 0.0.8 (8.4.11)")

    def test_preflight_uses_exactly_one_variables_statement_on_every_product(self):
        for version, comment, expected in ((VERSION, COMMENT, "percona"), ("8.4.11", "", None),
                                            ("8.4.11-11", PERCONA_COMMENT, None),
                                            ("11.4.13-MariaDB", "MariaDB Server", None),
                                            (VERSION, "Readyset", None)):
            conn = IdentityConnection(version, comment, "percona-8.4_0.0.8")
            server = perf_common.preflight(Reader(conn, "/*myflames:test*/ "))
            self.assertEqual(server.base, expected)
            self.assertEqual(len(conn.calls), 2, "one variables statement and one status statement")
            self.assertEqual(len([sql for sql in conn.calls if "SHOW GLOBAL VARIABLES" in sql]), 1)
            self.assertEqual(len(conn.codebase_reads()), 1, "optional names share the existing SHOW")
            self.assertFalse(any("@@villagesql" in sql for sql in conn.calls))

    def test_show_variables_never_adds_a_follow_up_statement(self):
        conn = IdentityConnection()
        values = perf_common.show_variables(Reader(conn, "/*myflames:test*/ "), ("version", "version_comment"))
        self.assertEqual(values["version"], VERSION)
        self.assertEqual(len(conn.calls), 1)
        self.assertFalse(any("villagesql" in sql.lower() for sql in conn.calls))

    def test_preflight_codebase_failure_does_not_fail_the_view(self):
        conn = IdentityConnection(failure="ERROR 1193: Unknown system variable")
        server = perf_common.preflight(Reader(conn, "/*myflames:test*/ "))
        self.assertEqual(server.flavor, "villagesql")
        self.assertIsNone(server.base)


class VillageEvidenceTransport(object):
    """Synthetic packet replies; production target checks and parsing still run."""
    password = None
    _villagesql_version_cache = "transport cache must never be used"

    def __init__(self, failure=False):
        self.failure = failure
        self.events = []
        self.packets = []
        self.lookups = []
        self.after_read = None
        self.identity = {"server_uuid": "11111111-2222-3333-4444-555555555555",
                         "version": VERSION, "version_comment": SOURCE_COMMENT}

    def __getattr__(self, name):
        self.lookups.append(name)
        raise AssertionError("Metadata escaped the evidence scope: " + name)

    def villagesql_server_version(self, *args):
        raise AssertionError("Unbound transport metadata reader must never run")

    def run(self, sql, extra_flags=None, **kwargs):
        from _reviewed_replay import evidence_body, evidence_output
        self.events.append("read")
        self.packets.append(sql)
        body, marker = evidence_body(sql)
        assert marker is not None, "metadata requires an identity-bound packet"
        assert body in ("/*myflames:target-evidence*/ SELECT @@villagesql_server_version", "SELECT @@version")
        assert "--skip-reconnect" in extra_flags
        if self.after_read:
            self.after_read()
        if self.failure:
            raise PartialReadError("ERROR 1193 (HY000) at line 2: Unknown system variable",
                                   evidence_output("", marker, self.identity))
        output = self.identity["version"] if body == "SELECT @@version" else "percona-8.4_0.0.8"
        return evidence_output(output + "\n", marker, self.identity)


class CheckedVillageEvidence(dbops.TargetEvidenceConnection):
    """Observe check ordering without replacing or bypassing the real check."""

    def check(self):
        self.connection.events.append("check")
        return super(CheckedVillageEvidence, self).check()


class TestVillageBoundMetadata(unittest.TestCase):
    def test_metadata_is_bound_checked_before_and_after_and_cached_in_the_scope(self):
        transport = VillageEvidenceTransport()
        scope = CheckedVillageEvidence(transport)
        self.assertEqual(scope.villagesql_server_version(VERSION, SOURCE_COMMENT), "percona-8.4_0.0.8")
        self.assertEqual(transport.events, ["check", "check", "read", "check"])
        self.assertEqual(scope.variables, transport.identity, "the production binder validated the reply")
        transport.events[:] = []
        self.assertEqual(scope.villagesql_server_version(VERSION, SOURCE_COMMENT), "percona-8.4_0.0.8")
        self.assertEqual(transport.events, ["check", "check"])
        self.assertEqual(len(transport.packets), 1)
        self.assertEqual(transport.lookups, [], "neither the reader nor cache uses __getattr__")

    def test_preexisting_scope_failure_refuses_the_read_and_cached_results(self):
        for cached in (False, True):
            with self.subTest(cached=cached):
                transport = VillageEvidenceTransport()
                scope = CheckedVillageEvidence(transport)
                if cached:
                    scope.villagesql_server_version(VERSION, SOURCE_COMMENT)
                count = len(transport.packets)
                scope.problem = "The reviewed identity changed."
                with self.assertRaisesRegex(ConnectorError, "reviewed identity changed"):
                    scope.villagesql_server_version(VERSION, SOURCE_COMMENT)
                self.assertEqual(len(transport.packets), count)
                self.assertEqual(transport.lookups, [])

    def test_post_read_check_cannot_be_swallowed_as_optional_metadata(self):
        transport = VillageEvidenceTransport()
        scope = CheckedVillageEvidence(transport)
        def changed():
            scope.problem = "The reviewed identity changed during the metadata read."
        transport.after_read = changed
        with self.assertRaisesRegex(ConnectorError, "changed during the metadata read"):
            scope.villagesql_server_version(VERSION, SOURCE_COMMENT)
        self.assertEqual(transport.events[-1], "check")
        self.assertEqual(len(transport.packets), 1)

    def test_optional_server_error_keeps_validated_identity_and_unknown_base(self):
        transport = VillageEvidenceTransport(failure=True)
        scope = CheckedVillageEvidence(transport)
        self.assertIsNone(scope.villagesql_server_version(VERSION, SOURCE_COMMENT))
        fields = perf_common.conn_identity(scope, "mysql", VERSION)
        self.assertEqual(fields, {"flavor": "villagesql", "server_product": "VillageSQL", "server_base": None})
        self.assertIsNone(scope.problem)
        self.assertEqual(scope.variables, transport.identity)
        self.assertEqual(server_info.server_label(dict(fields, server_version=VERSION)),
                         "VillageSQL 0.0.8 (8.4.11)")
        self.assertEqual(transport.lookups, [])

    def test_router_identity_change_during_optional_metadata_is_still_fatal(self):
        transport = VillageEvidenceTransport()
        scope = CheckedVillageEvidence(transport)
        self.assertEqual(scope.server_version(), VERSION)
        transport.identity["server_uuid"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with self.assertRaisesRegex(ConnectorError, "identity check could not run"):
            scope.villagesql_server_version(VERSION, SOURCE_COMMENT)
        with self.assertRaises(ConnectorError):
            scope.check()
        self.assertEqual(len(transport.packets), 2)
        self.assertEqual(transport.lookups, [])

    def test_other_products_check_the_scope_without_reading_metadata(self):
        transport = VillageEvidenceTransport()
        scope = CheckedVillageEvidence(transport)
        self.assertIsNone(scope.villagesql_server_version("8.4.11", SOURCE_COMMENT))
        self.assertEqual(transport.events, ["check", "check"])
        self.assertEqual(transport.packets, [])
        self.assertEqual(transport.lookups, [])


class TestVillageParametersAndPrivileges(unittest.TestCase):
    def test_variables_are_vendor_marked_only_on_villagesql(self):
        self.assertEqual(server_info.VILLAGESQL_VARIABLES, VILLAGE_VARIABLES)
        rows = [[name, "value"] for name in VILLAGE_VARIABLES] + [["userstat", "ON"], ["max_connections", "151"]]
        for flavor, base in (("villagesql", "mysql"), ("villagesql", "percona"),
                             ("villagesql", None), ("mysql", None), ("percona", None)):
            report = parameters.compute_parameters("mysql", (8, 4, 11), rows=rows,
                                                   info_rows=[], persisted_rows=[], flavor=flavor, base=base)
            found = {item["name"]: item for item in report["items"]}
            self.assertEqual(report["schema_version"], "parameters-1.0")
            for name in VILLAGE_VARIABLES:
                self.assertEqual(found[name]["vendor"], "villagesql" if flavor == "villagesql" else None)
                self.assertEqual(server_info.villagesql_variable(name.upper(), flavor), flavor == "villagesql")
                if flavor == "villagesql":
                    self.assertEqual(found[name]["documentation"]["url"], DOCUMENTATION)
            self.assertIsNone(found["max_connections"]["vendor"])
            self.assertEqual(found["userstat"]["vendor"],
                             "percona" if flavor == "percona" or base == "percona" else None)

    def test_single_variable_description_carries_vendor_base_and_documentation(self):
        for name in VILLAGE_VARIABLES:
            info = {"name": name, "engine": "mysql", "flavor": "villagesql", "server_product": "VillageSQL",
                    "server_base": "mysql", "server_version": VERSION, "version_tuple": (8, 4, 11),
                    "in_global": True, "in_session": False, "global_value": "value", "session_value": None,
                    "catalog": None, "info": None, "metadata": None, "persisted_globals_load": "ON",
                    "persisted": {"present": False, "value": None, "read": True}, "notes": []}
            report = variables.describe(info)
            self.assertEqual(report["vendor"], "villagesql")
            self.assertEqual(report["server_base"], "mysql")
            self.assertEqual(report["documentation"]["url"], DOCUMENTATION)

    def test_extension_admin_is_dynamic_global_only_and_villagesql_only(self):
        for version, flavor, expected in ((VERSION, None, True), (VERSION, "villagesql", True),
                                          ("8.4.11", "villagesql", True), ("8.4.11", "mysql", False),
                                          ("8.4.11-11", "percona", False), ("8.0.26", "readyset", False)):
            catalog, _ = privileges.privilege_catalog("mysql", version, flavor=flavor)
            found = {entry["name"]: entry for entry in catalog["global"]}
            self.assertEqual("EXTENSION_ADMIN" in found, expected, (version, flavor))
            if expected:
                self.assertTrue(found["EXTENSION_ADMIN"]["dynamic"])
                self.assertTrue(found["EXTENSION_ADMIN"]["grantable_here"])
            for level, entries in catalog.items():
                if level != "global":
                    self.assertNotIn("EXTENSION_ADMIN", [entry["name"] for entry in entries])
        catalog, _ = privileges.privilege_catalog("mariadb", "11.4.13-MariaDB")
        self.assertNotIn("EXTENSION_ADMIN", [entry["name"] for entry in catalog["global"]])

    def test_server_presence_is_authoritative_and_extension_admin_fallback_is_villagesql_only(self):
        rows = [["SELECT", "Tables", ""], ["EXTENSION_ADMIN", "Server Admin", ""]]
        for flavor in ("villagesql", "mysql", "percona"):
            with self.subTest(flavor=flavor):
                catalog, meta = privileges.privilege_catalog("mysql", "8.4.11", rows, flavor=flavor)
                self.assertEqual(meta["source"], "server")
                extension = next(entry for entry in catalog["global"] if entry["name"] == "EXTENSION_ADMIN")
                self.assertTrue(extension["dynamic"])
                for level, entries in catalog.items():
                    if level != "global":
                        self.assertNotIn("EXTENSION_ADMIN", [entry["name"] for entry in entries])
                catalog, meta = privileges.privilege_catalog("mysql", "8.4.11", rows[:1], flavor=flavor)
                self.assertEqual(meta["source"], "server")
                self.assertNotIn("EXTENSION_ADMIN", [entry["name"] for entry in catalog["global"]])
                catalog, meta = privileges.privilege_catalog("mysql", "8.4.11", None, flavor=flavor)
                self.assertEqual(meta["source"], "static")
                self.assertEqual("EXTENSION_ADMIN" in [entry["name"] for entry in catalog["global"]],
                                 flavor == "villagesql")


if __name__ == "__main__":
    unittest.main()
