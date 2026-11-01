"""Pure parts of scripts/record-sysvar-catalog.py (no Docker, no server).

The lab script starts every cataloged MySQL and MariaDB release and reads its
variables; these tests cover what decides the result without a server: the
version scope from tag files, --help parsing, the row mappings, the SET probe
reading and the runtime JSON writer.
"""
import importlib.util
import json
import re
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "record_sysvar_catalog", str(Path(__file__).resolve().parents[1] / "scripts/record-sysvar-catalog.py"))
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)


def _files(table):
    def read(tag, path):
        return table.get((tag, path))
    return read


class TestScope(unittest.TestCase):
    MYSQL_TAGS = ["mysql-5.6.51", "mysql-5.7.6", "mysql-5.7.8", "mysql-5.7.9", "mysql-5.7.10-2", "mysql-5.7.40-testing",
                  "mysql-5.7.44", "mysql-8.0.4", "mysql-8.0.11", "mysql-8.0.13", "mysql-8.0.37-fc40",
                  "mysql-8.1.0-labs-mrs5-preview", "mysql-8.4.0", "mysql-9.5.2", "mysql-9.5.2-respin",
                  "mysql-9.0.0-release", "mysql-26.10.0"]
    MYSQL_FILES = {
        ("mysql-5.6.51", "VERSION"): "MYSQL_VERSION_MAJOR=5\nMYSQL_VERSION_EXTRA=\n",
        ("mysql-5.7.6", "VERSION"): "MYSQL_VERSION_MAJOR=5\nMYSQL_VERSION_PATCH=6\nMYSQL_VERSION_EXTRA=-m16\n",
        ("mysql-5.7.8", "VERSION"): "MYSQL_VERSION_EXTRA=-rc\n",
        ("mysql-5.7.9", "VERSION"): "MYSQL_VERSION_EXTRA=\n",
        ("mysql-5.7.44", "VERSION"): "MYSQL_VERSION_EXTRA=\n",
        ("mysql-8.0.4", "VERSION"): "MYSQL_VERSION_MAJOR=8\nMYSQL_VERSION_MINOR=0\nMYSQL_VERSION_PATCH=4\n"
                                    "MYSQL_VERSION_EXTRA=-rc\n",
        ("mysql-8.0.11", "VERSION"): "MYSQL_VERSION_MAJOR=8\nMYSQL_VERSION_PATCH=11\nMYSQL_VERSION_EXTRA=\n",
        # MYSQL_CLUSTER_VERSION_EXTRA must not be read as the server's extra.
        ("mysql-8.0.13", "VERSION"): "MYSQL_VERSION_EXTRA=\nMYSQL_CLUSTER_VERSION_EXTRA=-dmr\n",
        ("mysql-8.4.0", "MYSQL_VERSION"): 'MYSQL_VERSION_EXTRA=\nMYSQL_VERSION_MATURITY="LTS"\n',
        ("mysql-9.5.2", "MYSQL_VERSION"): 'MYSQL_VERSION_MATURITY="INNOVATION"\n',
        ("mysql-9.5.2-respin", "MYSQL_VERSION"): 'MYSQL_VERSION_MATURITY="INNOVATION"\n',
        ("mysql-26.10.0", "MYSQL_VERSION"): 'MYSQL_VERSION_MATURITY="INNOVATION"\nMYSQL_PREVIOUS_LTS_VERSION=9.7.0\n',
    }

    def test_mysql_scope(self):
        dates = {"mysql-8.0.11": "2018-04-08T08:17:02+02:00"}
        in_scope, excluded = rec.compute_scope("mysql", self.MYSQL_TAGS, _files(self.MYSQL_FILES), dates)
        # 5.7 GA releases (5.7.9 on) and 8.0.11 on; 5.6, milestones, RCs and odd tags are left out.
        self.assertEqual([e["version"] for e in in_scope],
                         ["5.7.9", "5.7.44", "8.0.11", "8.0.13", "8.4.0", "9.5.2", "26.10.0"])
        self.assertEqual([e["maturity"] for e in in_scope], ["GA", "GA", "GA", "GA", "LTS", "INNOVATION", "INNOVATION"])
        self.assertEqual(in_scope[2]["tag_date"], "2018-04-08T08:17:02+02:00")
        # The respin tag stands for its version.
        self.assertEqual(in_scope[5]["tag"], "mysql-9.5.2-respin")
        self.assertEqual([(e["version"], e["maturity"], e["reason"]) for e in excluded],
                         [("5.7.6", "M16", "Maturity M16"), ("5.7.8", "RC", "Maturity RC"),
                          ("8.0.4", "RC", "Before 8.0.11 and RC")])

    def test_mysql_maturity_words(self):
        self.assertEqual(rec.mysql_maturity(rec.parse_version_file("MYSQL_VERSION_EXTRA=-dmr")), "DMR")
        self.assertEqual(rec.mysql_maturity(rec.parse_version_file("MYSQL_VERSION_EXTRA=")), "GA")
        self.assertEqual(rec.mysql_maturity(rec.parse_version_file('MYSQL_VERSION_MATURITY="LTS"')), "LTS")

    def test_mariadb_scope(self):
        tags = ["mariadb-10.5.29", "mariadb-10.6.0", "mariadb-10.6.3", "mariadb-10.10.2-release",
                "mariadb-11.4.11b", "mariadb-11.4.2", "mariadb-13.1.1"]
        files = {("mariadb-10.5.29", "VERSION"): "SERVER_MATURITY=stable\n",
                 ("mariadb-10.6.0", "VERSION"): "SERVER_MATURITY=alpha\n",
                 ("mariadb-10.6.3", "VERSION"): "MYSQL_VERSION_MAJOR=10\nSERVER_MATURITY=stable\n",
                 ("mariadb-11.4.2", "VERSION"): "SERVER_MATURITY=stable\n",
                 ("mariadb-13.1.1", "VERSION"): "SERVER_MATURITY=gamma\n"}
        in_scope, excluded = rec.compute_scope("mariadb", tags, _files(files))
        self.assertEqual([e["version"] for e in in_scope], ["10.6.3", "11.4.2"])
        self.assertEqual(sorted(e["version"] for e in excluded), ["10.6.0", "13.1.1"])
        self.assertTrue(all(e["maturity"] == "stable" for e in in_scope))

    def test_select_tags_plain_only(self):
        chosen = rec.select_tags("mysql", ["mysql-9.1.2-preview", "mysql-9.1.2", "mysql-8.4.1-labs-mrs8"])
        self.assertEqual(chosen, {"9.1.2": "mysql-9.1.2"})


HELP = """mysqld  Ver 8.4.0 for Linux on aarch64 (MySQL Community Server - GPL)
  --skip-name-resolve Don't resolve hostnames.

Variables (--variable-name=value)
and boolean options {FALSE|TRUE}                             Value (after reading options)
------------------------------------------------------------ -------------
activate-all-roles-on-login                                  FALSE
host-cache-size                                              279
init-connect
log-error-services                                           log_filter_internal; log_sink_internal
long-query-time                                              10
pid-file                                                     (No default value)
replica-parallel-type-but-with-a-very-long-option-name-here-x 42

To see what values a running MySQL server is using, type
'mysqladmin variables' instead of 'mysqld --verbose --help'.
"""


class TestHelp(unittest.TestCase):
    def test_parse_help(self):
        values = rec.parse_help(HELP)
        self.assertEqual(values["activate_all_roles_on_login"], "FALSE")
        self.assertEqual(values["host_cache_size"], "279")
        self.assertEqual(values["init_connect"], "")
        self.assertEqual(values["log_error_services"], "log_filter_internal; log_sink_internal")
        self.assertIsNone(values["pid_file"])
        self.assertEqual(values["replica_parallel_type_but_with_a_very_long_option_name_here_x"], "42")
        self.assertNotIn("skip_name_resolve", values)
        self.assertNotIn("to", values)

    def test_help_values_in_server_text(self):
        self.assertEqual(rec.server_text_from_help("FALSE", "ON"), "OFF")
        self.assertEqual(rec.server_text_from_help("TRUE", "OFF"), "ON")
        self.assertEqual(rec.server_text_from_help("10", "2.000000"), "10.000000")
        self.assertEqual(rec.server_text_from_help("279", "0"), "279")
        self.assertIsNone(rec.server_text_from_help(None, "x"))


def _bits(number):
    return str(struct.unpack("<Q", struct.pack("<d", number))[0])


class TestMySQL57(unittest.TestCase):
    """MySQL 5.7: no variables_info or variables_metadata; the --no-defaults start keeps option files out."""

    GLOBAL = [{"VARIABLE_NAME": "max_connections", "VARIABLE_VALUE": "151"},
              {"VARIABLE_NAME": "socket", "VARIABLE_VALUE": "/tmp/myflames.sock"},
              {"VARIABLE_NAME": "query_cache_type", "VARIABLE_VALUE": "OFF"},
              {"VARIABLE_NAME": "autocommit", "VARIABLE_VALUE": "ON"},
              {"VARIABLE_NAME": "long_query_time", "VARIABLE_VALUE": "10.000000"},
              {"VARIABLE_NAME": "table_open_cache_instances", "VARIABLE_VALUE": "16"},
              {"VARIABLE_NAME": "validate_password_length", "VARIABLE_VALUE": "8"}]
    THREAD = [{"VARIABLE_NAME": "autocommit", "VARIABLE_VALUE": "ON"},
              {"VARIABLE_NAME": "long_query_time", "VARIABLE_VALUE": "10.000000"},
              {"VARIABLE_NAME": "timestamp", "VARIABLE_VALUE": "1793224300.560536"}]
    HELP = {"max_connections": "151", "socket": "/var/run/mysqld/mysqld.sock", "query_cache_type": "OFF",
            "autocommit": "TRUE", "long_query_time": "10", "table_open_cache_instances": "1"}

    def test_no_variables_info(self):
        variables, notes = rec.mysql_variables(self.GLOBAL, self.THREAD, None, None, self.HELP,
                                               plugin_of={"validate_password_length": "validate_password"})
        mc = variables["max_connections"]
        self.assertEqual((mc["scope"], mc["default"], mc["default_from"], mc["origin"]),
                         ("global", "151", "runtime", None))
        self.assertEqual((mc["min"], mc["max"], mc["type"]), (None, None, None))
        # Set on the lab's command line: --help gives the default, not the running value.
        self.assertEqual((variables["socket"]["default"], variables["socket"]["default_from"]),
                         ("/var/run/mysqld/mysqld.sock", "help"))
        self.assertIsNone(variables["socket"]["origin"])
        self.assertEqual(variables["autocommit"]["type"], "boolean")
        self.assertIsNone(variables["query_cache_type"]["type"])  # OFF but not a TRUE/FALSE option: never guessed
        self.assertEqual(variables["long_query_time"]["scope"], "both")
        self.assertIsNone(variables["long_query_time"]["max"])
        self.assertEqual(variables["timestamp"]["scope"], "session")
        self.assertEqual(variables["validate_password_length"]["plugin"], "validate_password")
        self.assertIn(rec.NO_INFO_NOTE, notes)
        # A value the server sized at startup is kept, and listed against --help.
        self.assertEqual(variables["table_open_cache_instances"]["default"], "16")
        differ = [n for n in notes if n.startswith("The running value differs from --help's")]
        self.assertEqual(len(differ), 1)
        self.assertIn("table_open_cache_instances", differ[0])
        self.assertNotIn("socket", differ[0])
        self.assertNotIn("long_query_time", differ[0])  # 10 and 10.000000 are the same value
        # The guard has no VARIABLE_SOURCE to check on 5.7.
        self.assertEqual(rec.origin_problems("mysql", variables), [])

    def test_information_schema_fallback_names_and_scope(self):
        # information_schema names are upper case, and SESSION_VARIABLES also lists global-only variables.
        global_rows = [{"VARIABLE_NAME": "MAX_CONNECTIONS", "VARIABLE_VALUE": "151"},
                       {"VARIABLE_NAME": "SQL_MODE", "VARIABLE_VALUE": "STRICT_TRANS_TABLES"}]
        session_rows = global_rows + [{"VARIABLE_NAME": "TIMESTAMP", "VARIABLE_VALUE": "1.000000"}]
        variables, notes = rec.mysql_variables(global_rows, session_rows, None, None, {})
        rec.unclear_scopes(variables, notes)
        self.assertEqual(sorted(variables), ["max_connections", "sql_mode", "timestamp"])
        self.assertIsNone(variables["max_connections"]["scope"])
        self.assertIsNone(variables["sql_mode"]["scope"])
        self.assertEqual(variables["timestamp"]["scope"], "session")
        self.assertTrue(any("scope of 2 variables" in n for n in notes))

    def test_query_cache_size_not_probed(self):
        variables, _ = rec.mysql_variables(
            [{"VARIABLE_NAME": "query_cache_size", "VARIABLE_VALUE": "1048576"}], [], None, None, {})
        sql, steps = rec.probe_plan("mysql", variables)
        self.assertIn(("read", "query_cache_size"), steps)
        self.assertNotIn(("set", "query_cache_size"), steps)
        self.assertNotIn("SET GLOBAL query_cache_size", sql)

    def test_validate_password_both_forms(self):
        # The component (8.0+) and the plugin (5.7, still shipped later) are separate entries with their own labels.
        labels = dict((p[0], p[1]) for p in rec.MYSQL_PLUGINS)
        self.assertEqual(labels["component_validate_password"],
                         ("INSTALL COMPONENT 'file://component_validate_password'",))
        self.assertEqual(labels["validate_password"], ("INSTALL PLUGIN validate_password SONAME 'validate_password.so'",))


class Fake57(object):
    """Just enough of a MySQL 5.7 server for load_plugins: Group Replication refuses
    to install while server_id is 0 or the replica repositories are FILE."""

    engine = "mysql"

    def __init__(self):
        self.lab_set = set()
        self.globals = {"server_id": "0", "master_info_repository": "FILE", "relay_log_info_repository": "FILE"}
        self.names = {"server_id", "master_info_repository", "relay_log_info_repository", "max_connections"}
        self.log = []

    def sql(self, statement, timeout=300, check=True):
        self.log.append(statement)
        if statement.startswith("SELECT COUNT(*) FROM performance_schema.global_variables") or \
                statement.startswith(rec.THREAD_SQL):
            return 0, "", ""
        match = re.match(r"SET GLOBAL (\w+) = '?([^']*)'?$", statement)
        if match:
            self.globals[match.group(1)] = match.group(2)
            return 0, "", ""
        if "group_replication" in statement:
            if self.globals["server_id"] == "0" or self.globals["master_info_repository"] != "TABLE":
                return 1, "", "ERROR 1123 (HY000) at line 1: Can't initialize function 'group_replication'"
            self.names |= {"group_replication_group_name", "group_replication_start_on_boot"}
            return 0, "", ""
        return 1, "", "ERROR 1126 (HY000) at line 1: Can't open shared library"

    def query_xml(self, sql, timeout=300):
        match = re.match(r"SELECT @@GLOBAL\.(\w+) AS v", sql)
        if match:
            return [{"v": self.globals[match.group(1)]}]
        if "global_variables" in sql:
            return [{"VARIABLE_NAME": n, "VARIABLE_VALUE": "x"} for n in sorted(self.names)]
        return []


class TestPluginSets(unittest.TestCase):
    def test_group_replication_on_57(self):
        server = Fake57()
        notes = []
        plugins = [p for p in rec.MYSQL_PLUGINS if p[0] in ("group_replication", "clone")]
        loaded, plugin_of = rec.load_plugins(server, plugins, notes)
        self.assertEqual(loaded, ["group_replication"])
        self.assertEqual(plugin_of, {"group_replication_group_name": "group_replication",
                                     "group_replication_start_on_boot": "group_replication"})
        # The three settings are put back, and their defaults will come from --help.
        self.assertEqual(server.globals, {"server_id": "0", "master_info_repository": "FILE",
                                          "relay_log_info_repository": "FILE"})
        self.assertEqual(server.lab_set, {"server_id", "master_info_repository", "relay_log_info_repository"})
        self.assertIn("SET GLOBAL master_info_repository = 'FILE'", server.log)
        self.assertTrue(any("installs here only with" in n for n in notes))
        self.assertTrue(any(n.startswith("Plugin not loaded (clone") for n in notes))

    def test_lab_set_defaults_come_from_help(self):
        variables, _ = rec.mysql_variables(
            [{"VARIABLE_NAME": "server_id", "VARIABLE_VALUE": "1"},
             {"VARIABLE_NAME": "master_info_repository", "VARIABLE_VALUE": "TABLE"}], [], None, None,
            {"server_id": "0", "master_info_repository": "FILE"}, lab_set={"server_id", "master_info_repository"})
        self.assertEqual(variables["server_id"]["default"], "0")
        self.assertEqual(variables["master_info_repository"]["default"], "FILE")
        self.assertEqual(variables["server_id"]["default_from"], "help")

    def test_schema1_refusal(self):
        self.assertTrue(rec.is_schema1_refusal('not implemented: media type '
                                               '"application/vnd.docker.distribution.manifest.v1+prettyjws" is no '
                                               'longer supported since containerd v2.1'))
        self.assertFalse(rec.is_schema1_refusal("manifest unknown"))
        unpack = ('failed to unpack image on snapshotter overlayfs: apply layer error for "docker.io/library/'
                  'mysql:5.7.13": failed to extract layer sha256:4275: NotFound: failed to get reader from content store')
        self.assertIn("could not unpack", rec.skopeo_reason(unpack))
        self.assertIsNone(rec.skopeo_reason("pull access denied"))

    def test_transient_pull_errors(self):
        self.assertTrue(rec.is_transient_pull_error("unknown: failed to copy: httpReadSeeker: failed open: unexpected "
                                                    "status from GET request to https://registry-1.docker.io/v2/"))
        self.assertFalse(rec.is_transient_pull_error("toomanyrequests: You have reached your pull rate limit"))
        self.assertFalse(rec.is_transient_pull_error("manifest unknown"))


class TestMySQLRows(unittest.TestCase):
    def test_double_range_is_decoded(self):
        self.assertEqual(rec.decode_double_bits(_bits(31536000.0)), "31536000.000000")
        self.assertEqual(rec.decode_double_bits("0"), "0.000000")
        self.assertIsNone(rec.decode_double_bits("abc"))

    def test_assemble(self):
        global_rows = [{"VARIABLE_NAME": "max_connections", "VARIABLE_VALUE": "151"},
                       {"VARIABLE_NAME": "sql_mode", "VARIABLE_VALUE": "STRICT_TRANS_TABLES"},
                       {"VARIABLE_NAME": "long_query_time", "VARIABLE_VALUE": "10.000000"},
                       {"VARIABLE_NAME": "skip_name_resolve", "VARIABLE_VALUE": "ON"},
                       {"VARIABLE_NAME": "autocommit", "VARIABLE_VALUE": "ON"},
                       {"VARIABLE_NAME": "ssl_ca", "VARIABLE_VALUE": None},
                       {"VARIABLE_NAME": "foreign_key_checks", "VARIABLE_VALUE": "ON"},
                       {"VARIABLE_NAME": "group_replication_group_name", "VARIABLE_VALUE": ""}]
        thread_rows = [{"VARIABLE_NAME": "sql_mode", "VARIABLE_VALUE": "STRICT_TRANS_TABLES"},
                       {"VARIABLE_NAME": "autocommit", "VARIABLE_VALUE": "ON"},
                       {"VARIABLE_NAME": "long_query_time", "VARIABLE_VALUE": "10.000000"},
                       {"VARIABLE_NAME": "foreign_key_checks", "VARIABLE_VALUE": "ON"},
                       {"VARIABLE_NAME": "timestamp", "VARIABLE_VALUE": "1793224300.560536"}]
        info = [{"VARIABLE_NAME": "max_connections", "VARIABLE_SOURCE": "COMPILED", "MIN_VALUE": "1",
                 "MAX_VALUE": "100000"},
                {"VARIABLE_NAME": "sql_mode", "VARIABLE_SOURCE": "COMPILED", "MIN_VALUE": "0", "MAX_VALUE": "0"},
                {"VARIABLE_NAME": "long_query_time", "VARIABLE_SOURCE": "COMPILED", "MIN_VALUE": "0",
                 "MAX_VALUE": _bits(31536000.0)},
                {"VARIABLE_NAME": "skip_name_resolve", "VARIABLE_SOURCE": "GLOBAL", "MIN_VALUE": "0",
                 "MAX_VALUE": "0"},
                {"VARIABLE_NAME": "autocommit", "VARIABLE_SOURCE": "COMPILED", "MIN_VALUE": "0", "MAX_VALUE": "0"},
                {"VARIABLE_NAME": "ssl_ca", "VARIABLE_SOURCE": "COMPILED", "MIN_VALUE": "0", "MAX_VALUE": "0"},
                {"VARIABLE_NAME": "foreign_key_checks", "VARIABLE_SOURCE": "DYNAMIC", "MIN_VALUE": "0",
                 "MAX_VALUE": "0"},
                {"VARIABLE_NAME": "group_replication_group_name", "VARIABLE_SOURCE": "COMPILED"},
                {"VARIABLE_NAME": "timestamp", "VARIABLE_SOURCE": "COMPILED", "MIN_VALUE": "0", "MAX_VALUE": "0"},
                {"VARIABLE_NAME": "ghost", "VARIABLE_SOURCE": "COMPILED"}]
        help_values = {"skip_name_resolve": "FALSE", "autocommit": "TRUE", "max_connections": "151"}
        variables, notes = rec.mysql_variables(global_rows, thread_rows, info, None, help_values,
                                               plugin_of={"group_replication_group_name": "group_replication"})
        self.assertEqual(variables["max_connections"]["scope"], "global")
        self.assertEqual(variables["sql_mode"]["scope"], "both")
        self.assertEqual(variables["timestamp"]["scope"], "session")
        self.assertEqual((variables["max_connections"]["min"], variables["max_connections"]["max"]), ("1", "100000"))
        self.assertIsNone(variables["max_connections"]["type"])  # no variables_metadata: never guessed
        self.assertEqual((variables["sql_mode"]["min"], variables["sql_mode"]["max"]), (None, None))
        self.assertEqual(variables["long_query_time"]["max"], "31536000.000000")
        self.assertEqual(variables["autocommit"]["type"], "boolean")
        # Not COMPILED: the default comes from --help, written like the server writes it.
        self.assertEqual(variables["skip_name_resolve"]["default"], "OFF")
        self.assertEqual(variables["skip_name_resolve"]["default_from"], "help")
        self.assertEqual(variables["foreign_key_checks"]["default_note"], "unknown")
        self.assertIsNone(variables["foreign_key_checks"]["default"])
        self.assertIsNone(variables["ssl_ca"]["default"])
        self.assertEqual(variables["ssl_ca"]["default_note"], "null")
        self.assertEqual(variables["group_replication_group_name"]["plugin"], "group_replication")
        self.assertEqual(variables["group_replication_group_name"]["default"], "")
        self.assertTrue(any("ghost" in note for note in notes))

    def test_metadata_types(self):
        global_rows = [{"VARIABLE_NAME": "optimizer_switch", "VARIABLE_VALUE": "index_merge=on,mrr=off"},
                       {"VARIABLE_NAME": "sql_mode", "VARIABLE_VALUE": "ANSI_QUOTES"},
                       {"VARIABLE_NAME": "long_query_time", "VARIABLE_VALUE": "10.000000"}]
        meta = [{"VARIABLE_NAME": "optimizer_switch", "VARIABLE_SCOPE": "SESSION", "DATA_TYPE": "Set"},
                {"VARIABLE_NAME": "sql_mode", "VARIABLE_SCOPE": "SESSION", "DATA_TYPE": "Set"},
                {"VARIABLE_NAME": "long_query_time", "VARIABLE_SCOPE": "GLOBAL", "DATA_TYPE": "Numeric"}]
        info = [{"VARIABLE_NAME": "long_query_time", "VARIABLE_SOURCE": "COMPILED", "MIN_VALUE": "0",
                 "MAX_VALUE": _bits(31536000.0)}]
        variables, notes = rec.mysql_variables(global_rows, [], info, meta, {})
        self.assertEqual(variables["optimizer_switch"]["type"], "flagset")
        self.assertEqual(variables["sql_mode"]["type"], "set")
        self.assertEqual(variables["long_query_time"]["type"], "double")
        self.assertEqual(variables["long_query_time"]["min"], "0.000000")
        # The tables say global only; variables_metadata says SESSION: noted, tables kept.
        self.assertEqual(variables["sql_mode"]["scope"], "global")
        self.assertTrue(any("variables_metadata" in note for note in notes))


class TestMariaDBRows(unittest.TestCase):
    ROWS = [
        {"VARIABLE_NAME": "MAX_CONNECTIONS", "VARIABLE_SCOPE": "GLOBAL", "VARIABLE_TYPE": "BIGINT UNSIGNED",
         "DEFAULT_VALUE": "151", "NUMERIC_MIN_VALUE": "10", "NUMERIC_MAX_VALUE": "100000", "ENUM_VALUE_LIST": None,
         "READ_ONLY": "NO"},
        {"VARIABLE_NAME": "SQL_MODE", "VARIABLE_SCOPE": "SESSION", "VARIABLE_TYPE": "SET",
         "DEFAULT_VALUE": "STRICT_TRANS_TABLES", "NUMERIC_MIN_VALUE": None, "NUMERIC_MAX_VALUE": None,
         "ENUM_VALUE_LIST": "REAL_AS_FLOAT,PIPES_AS_CONCAT", "READ_ONLY": "NO"},
        {"VARIABLE_NAME": "TIMESTAMP", "VARIABLE_SCOPE": "SESSION ONLY", "VARIABLE_TYPE": "DOUBLE",
         "DEFAULT_VALUE": "0.000000", "NUMERIC_MIN_VALUE": "0", "NUMERIC_MAX_VALUE": "2147483647",
         "ENUM_VALUE_LIST": None, "READ_ONLY": "NO"},
        {"VARIABLE_NAME": "INNODB_LOG_FILE_SIZE", "VARIABLE_SCOPE": "GLOBAL", "VARIABLE_TYPE": "BIGINT UNSIGNED",
         "DEFAULT_VALUE": "100663296", "NUMERIC_MIN_VALUE": "1048576", "NUMERIC_MAX_VALUE": "512",
         "ENUM_VALUE_LIST": None, "READ_ONLY": "YES"},
        {"VARIABLE_NAME": "BIG_TABLES", "VARIABLE_SCOPE": "SESSION", "VARIABLE_TYPE": "BOOLEAN",
         "DEFAULT_VALUE": "OFF", "NUMERIC_MIN_VALUE": None, "NUMERIC_MAX_VALUE": None,
         "ENUM_VALUE_LIST": "OFF,ON", "READ_ONLY": "NO"},
        {"VARIABLE_NAME": "OPTIMIZER_SWITCH", "VARIABLE_SCOPE": "SESSION", "VARIABLE_TYPE": "FLAGSET",
         "DEFAULT_VALUE": "index_merge=on", "NUMERIC_MIN_VALUE": None, "NUMERIC_MAX_VALUE": None,
         "ENUM_VALUE_LIST": "index_merge,default", "READ_ONLY": "NO"},
        {"VARIABLE_NAME": "SERVER_AUDIT_FILE_PATH", "VARIABLE_SCOPE": "GLOBAL", "VARIABLE_TYPE": "VARCHAR",
         "DEFAULT_VALUE": None, "NUMERIC_MIN_VALUE": None, "NUMERIC_MAX_VALUE": None, "ENUM_VALUE_LIST": None,
         "READ_ONLY": "NO"},
        {"VARIABLE_NAME": "ODD", "VARIABLE_SCOPE": "GLOBAL", "VARIABLE_TYPE": "MYSTERY", "DEFAULT_VALUE": "1",
         "READ_ONLY": "NO"},
    ]

    def test_row_mapping(self):
        variables, notes = rec.mariadb_variables(self.ROWS, plugin_of={"server_audit_file_path": "server_audit"})
        mc = variables["max_connections"]
        self.assertEqual((mc["scope"], mc["type"], mc["default"], mc["min"], mc["max"]),
                         ("global", "integer", "151", "10", "100000"))
        self.assertEqual((mc["dynamic"], mc["dynamic_how"]), (True, "read_only_column"))
        self.assertEqual(variables["sql_mode"]["scope"], "both")
        self.assertEqual(variables["sql_mode"]["values"], ["REAL_AS_FLOAT", "PIPES_AS_CONCAT"])
        self.assertEqual(variables["timestamp"]["scope"], "session")
        self.assertEqual(variables["timestamp"]["type"], "double")
        self.assertIs(variables["innodb_log_file_size"]["dynamic"], False)
        self.assertEqual(variables["big_tables"]["type"], "boolean")
        self.assertIsNone(variables["big_tables"]["values"])
        self.assertEqual(variables["optimizer_switch"]["type"], "flagset")
        self.assertEqual(variables["server_audit_file_path"]["default_note"], "null")
        self.assertEqual(variables["server_audit_file_path"]["plugin"], "server_audit")
        self.assertIsNone(variables["odd"]["type"])
        self.assertTrue(any("MYSTERY" in note for note in notes))


class TestOrigins(unittest.TestCase):
    def test_mysql_origins(self):
        variables = {"socket": dict(rec.blank_facts(), origin="COMMAND_LINE"),
                     "foreign_key_checks": dict(rec.blank_facts(), origin="DYNAMIC"),
                     "max_connections": dict(rec.blank_facts(), origin="COMPILED"),
                     "plugin_var": dict(rec.blank_facts(), origin=None)}
        self.assertEqual(rec.origin_problems("mysql", variables), [])
        variables["skip_name_resolve"] = dict(rec.blank_facts(), origin="GLOBAL")
        variables["datadir"] = dict(rec.blank_facts(), origin="COMMAND_LINE")
        variables["sql_mode"] = dict(rec.blank_facts(), origin="DYNAMIC")
        self.assertEqual(rec.origin_problems("mysql", variables),
                         ["datadir (COMMAND_LINE)", "skip_name_resolve (GLOBAL)", "sql_mode (DYNAMIC)"])

    def test_mariadb_origins(self):
        variables, _ = rec.mariadb_variables([
            {"VARIABLE_NAME": "CHARACTER_SET_SERVER", "VARIABLE_SCOPE": "SESSION", "VARIABLE_TYPE": "ENUM",
             "DEFAULT_VALUE": "utf8mb4", "GLOBAL_VALUE_ORIGIN": "CONFIG", "READ_ONLY": "NO"},
            {"VARIABLE_NAME": "THREAD_POOL_SIZE", "VARIABLE_SCOPE": "GLOBAL", "VARIABLE_TYPE": "INT UNSIGNED",
             "DEFAULT_VALUE": "8", "GLOBAL_VALUE_ORIGIN": "AUTO", "READ_ONLY": "NO"},
            {"VARIABLE_NAME": "SOCKET", "VARIABLE_SCOPE": "GLOBAL", "VARIABLE_TYPE": "VARCHAR",
             "DEFAULT_VALUE": None, "GLOBAL_VALUE_ORIGIN": "COMMAND-LINE", "READ_ONLY": "YES"}])
        self.assertEqual(variables["thread_pool_size"]["origin"], "AUTO")
        # The image's option file sets character-set-server: that release must fail, not be cataloged.
        self.assertEqual(rec.origin_problems("mariadb", variables), ["character_set_server (CONFIG)"])


class TestProbe(unittest.TestCase):
    def _vars(self, engine):
        if engine == "mysql":
            variables, _ = rec.mysql_variables(
                [{"VARIABLE_NAME": n, "VARIABLE_VALUE": "1"} for n in
                 ("binlog_checksum", "binlog_format", "innodb_log_file_size", "max_connections", "sql_log_bin")],
                [{"VARIABLE_NAME": "sql_log_bin", "VARIABLE_VALUE": "ON"},
                 {"VARIABLE_NAME": "timestamp", "VARIABLE_VALUE": "1.000000"}],
                [], None, {})
            return variables
        variables, _ = rec.mariadb_variables(TestMariaDBRows.ROWS)
        return variables

    def test_plan_mysql(self):
        variables = self._vars("mysql")
        sql, steps = rec.probe_plan("mysql", variables)
        self.assertIn("SET GLOBAL max_connections = @@GLOBAL.max_connections;", sql)
        self.assertIn("SET SESSION timestamp = @@SESSION.timestamp;", sql)
        self.assertIn("SET @myflames_probe = @@GLOBAL.binlog_checksum;", sql)
        self.assertNotIn("SET GLOBAL binlog_checksum", sql)
        self.assertIn(rec.CLEAR_WARNINGS["mysql"], sql)
        self.assertEqual(steps[0], ("read", "binlog_checksum"))
        self.assertNotIn(("set", "binlog_checksum"), steps)

    def test_plan_mariadb_skips_read_only(self):
        variables = self._vars("mariadb")
        sql, steps = rec.probe_plan("mariadb", variables)
        self.assertIn(("read", "innodb_log_file_size"), steps)
        self.assertNotIn(("set", "innodb_log_file_size"), steps)
        self.assertIn(("set", "max_connections"), steps)
        self.assertIn(rec.CLEAR_WARNINGS["mariadb"], sql)

    def test_apply_probe_mysql(self):
        variables = self._vars("mysql")
        _, steps = rec.probe_plan("mysql", variables)
        index = dict((step, i) for i, step in enumerate(steps))
        out = []
        for i, step in enumerate(steps):
            out.append("%s %d" % (rec.PROBE_MARK, i))
            if step == ("set", "innodb_log_file_size"):
                out.append("Error\t1238\tVariable 'innodb_log_file_size' is a read only variable")
            if step == ("set", "sql_log_bin"):
                out.append("Error\t1238\tVariable 'sql_log_bin' is a SESSION variable")
            if step[1] == "binlog_format":
                out.append("Warning\t1287\t'@@binlog_format' is deprecated and will be removed in a future release.")
        results = rec.parse_probe_output("\n".join(out[:-1]))  # the last step never ran
        notes = []
        rec.apply_probe("mysql", variables, steps, results, notes)
        self.assertEqual((variables["max_connections"]["dynamic"], variables["max_connections"]["dynamic_how"]),
                         (True, "set_probe"))
        self.assertIs(variables["innodb_log_file_size"]["dynamic"], False)
        self.assertIsNone(variables["sql_log_bin"]["dynamic"])
        self.assertIsNone(variables["binlog_checksum"]["dynamic"])
        self.assertEqual(variables["binlog_format"]["deprecated_warning"],
                         "'@@binlog_format' is deprecated and will be removed in a future release.")
        self.assertTrue(any("binlog_checksum" in n for n in notes))
        self.assertTrue(any("sql_log_bin: error 1238" in n for n in notes))
        self.assertTrue(any("did not reach 1 of" in n for n in notes))
        self.assertIn(index[("set", "sql_log_bin")], results)
        retry = rec.session_retry_names("mysql", variables, steps, results)
        # Listed in both tables, so SET GLOBAL was tried; its "SESSION variable" error asks for SET SESSION.
        self.assertEqual(retry, ["sql_log_bin"])

    def test_session_retry(self):
        variables = {"sql_log_bin": dict(rec.blank_facts(), scope="both")}
        steps = [("set", "sql_log_bin")]
        results = {0: [("Error", 1238, "Variable 'sql_log_bin' is a SESSION variable")]}
        names = rec.session_retry_names("mysql", variables, steps, results)
        self.assertEqual(names, ["sql_log_bin"])
        self.assertIn("SET SESSION sql_log_bin = @@SESSION.sql_log_bin;", rec.session_probe_plan(names))
        notes = []
        rec.apply_session_probe(variables, names, {0: []}, notes)
        self.assertIs(variables["sql_log_bin"]["dynamic"], True)

    def test_unescape(self):
        rows = rec.parse_probe_output("%s 0\nWarning\t1287\ta\\tb\\\\c" % rec.PROBE_MARK)
        self.assertEqual(rows[0], [("Warning", 1287, "a\tb\\c")])


class TestHostAndRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_mark_host(self):
        variables = {"a": dict(rec.blank_facts(), default="8"), "b": dict(rec.blank_facts(), default="1"),
                     "c": dict(rec.blank_facts(), default=None, default_note="unknown"),
                     "d": dict(rec.blank_facts(), default="x")}
        changed = rec.mark_host(variables, rec.mysql_host_values(variables), {"a": "4", "b": "1", "c": "2"})
        self.assertEqual(changed, ["a"])
        self.assertEqual(variables["a"]["default_note"], "host")
        self.assertIsNone(variables["b"]["default_note"])
        self.assertEqual(variables["c"]["default_note"], "unknown")

    def test_mariadb_host_values(self):
        rows = [{"VARIABLE_NAME": "THREAD_POOL_SIZE", "DEFAULT_VALUE": "8", "GLOBAL_VALUE": "16",
                 "GLOBAL_VALUE_ORIGIN": "AUTO"},
                {"VARIABLE_NAME": "MAX_CONNECTIONS", "DEFAULT_VALUE": "151", "GLOBAL_VALUE": "500",
                 "GLOBAL_VALUE_ORIGIN": "CONFIG"}]
        small = [dict(rows[0], GLOBAL_VALUE="1"), dict(rows[1], GLOBAL_VALUE="100")]
        variables = {"thread_pool_size": dict(rec.blank_facts(), default="8"),
                     "max_connections": dict(rec.blank_facts(), default="151")}
        changed = rec.mark_host(variables, rec.mariadb_host_values(rows), rec.mariadb_host_values(small))
        # A value set in an option file is not the server's own choice.
        self.assertEqual(changed, ["thread_pool_size"])
        self.assertEqual(variables["thread_pool_size"]["default"], "8")

    def test_secure_file_priv_folder(self):
        text = ("server exited during startup: [ERROR] [MY-010095] [Server] Failed to access directory for "
                "--secure-file-priv. Please make sure that directory exists and is accessible by MySQL Server. "
                "Supplied value : /var/lib/mysql-files")
        self.assertEqual(rec.missing_secure_file_priv(text), "/var/lib/mysql-files")
        self.assertIsNone(rec.missing_secure_file_priv("Supplied value : /x"))

    def test_record_writer(self):
        variables = {"zeta": dict(rec.blank_facts(), scope="global", plugin="clone", dynamic=True),
                     "alpha": dict(rec.blank_facts(), scope="both", default_note="host")}
        meta = {"tag": "mysql-8.4.0", "maturity": "LTS", "method": "image", "image": "x", "platform": "linux/arm64",
                "server_version": "8.4.0", "version_comment": "MySQL Community Server - GPL",
                "read_at": "2026-10-28T12:00:00Z"}
        record = rec.build_record("mysql", "8.4.0", meta, variables, ["clone"], ["n"])
        path = os.path.join(self.tmp, "mysql", "8.4.0.json")
        rec.write_json(path, record)
        with open(path) as handle:
            text = handle.read()
        loaded = json.loads(text)
        self.assertEqual(loaded["schema"], "sysvar-runtime-1.0")
        self.assertEqual(list(loaded["variables"]), ["alpha", "zeta"])
        self.assertEqual(tuple(loaded["variables"]["zeta"]), rec.FIELDS)
        self.assertEqual(loaded["counts"], {"variables": 2, "plugin_variables": 1, "dynamic_known": 1,
                                            "deprecated_warning": 0, "host_defaults": 1})
        self.assertEqual(loaded["plugins_loaded"], ["clone"])
        self.assertFalse(os.path.exists(path + ".tmp"))
        # Deterministic: the same record writes the same bytes.
        rec.write_json(path, record)
        with open(path) as handle:
            self.assertEqual(handle.read(), text)
        self.assertEqual(rec.read_record(self.tmp, "mysql", "8.4.0")["version"], "8.4.0")

    def test_skipped_record_has_no_variables(self):
        record = rec.skipped_record("mysql", "9.1.1", {"tag": "mysql-9.1.1", "maturity": "INNOVATION"},
                                    "No image. No package.", "unavailable")
        self.assertNotIn("variables", record)
        self.assertEqual(record["skipped"], "No image. No package.")

    def test_aggregate_notes(self):
        notes = rec.aggregate_notes([
            ("8.0.12", ["SET probe errors (dynamic left null): a: error 1231 (x); b: error 1210 (y)",
                        "SET probe skipped for binlog_checksum: z."]),
            ("8.0.11", ["SET probe errors (dynamic left null): a: error 1231 (x)"])])
        self.assertEqual(notes, [("SET probe errors (dynamic left null): a: error 1231 (x)", "8.0.11, 8.0.12"),
                                 ("SET probe errors (dynamic left null): b: error 1210 (y)", "8.0.12")])
        self.assertEqual(rec.version_list(["9.0.0", "9.0.1", "9.1.0", "9.2.0"]), "9.0.0 to 9.2.0 (4 releases)")

    def test_ubuntu_codename(self):
        self.assertEqual(rec.ubuntu_codename("11.4.5-MariaDB-ubu2404"), "noble")
        self.assertEqual(rec.ubuntu_codename("10.11.11-MariaDB-ubu2204"), "jammy")
        self.assertIsNone(rec.ubuntu_codename("8.4.0"))

    def test_neighbour_codename(self):
        for version, server, method in (("11.4.5", "11.4.5-MariaDB-ubu2204", "image"),
                                         ("11.4.7", "11.4.7-MariaDB-ubu2404", "image"),
                                         ("11.4.2", "11.4.2-MariaDB-ubu2004", "package"),
                                         ("10.11.5", "10.11.5-MariaDB-ubu2004", "image")):
            rec.write_json(os.path.join(self.tmp, "mariadb", version + ".json"),
                           {"version": version, "server_version": server, "method": method})
        # The nearest image below in the same series wins; package runs and other series are ignored.
        self.assertEqual(rec.neighbour_codename(self.tmp, "11.4.6"), "jammy")
        self.assertEqual(rec.neighbour_codename(self.tmp, "11.4.3"), "jammy")
        self.assertIsNone(rec.neighbour_codename(self.tmp, "12.0.1"))

    def test_xml_rows(self):
        text = ('<?xml version="1.0"?>\n\n<resultset statement="select" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n  <row>\n'
                '\t<field name="VARIABLE_NAME">ssl_ca</field>\n\t<field name="VARIABLE_VALUE" xsi:nil="true" />\n'
                '  </row>\n  <row>\n\t<field name="VARIABLE_NAME">init_connect</field>\n'
                '\t<field name="VARIABLE_VALUE"></field>\n  </row>\n</resultset>\n')
        self.assertEqual(rec.parse_xml_rows(text), [{"VARIABLE_NAME": "ssl_ca", "VARIABLE_VALUE": None},
                                                    {"VARIABLE_NAME": "init_connect", "VARIABLE_VALUE": ""}])



class TestRunSafety(unittest.TestCase):
    def test_container_names_carry_this_runs_token(self):
        server = rec.Server("mysql", "8.4.0", "main", "img", None, "pw", tempfile.mkdtemp())
        self.assertTrue(server.name.startswith(rec.PREFIX))
        self.assertTrue(server.name.endswith("-" + rec.RUN_TOKEN))

    def test_a_stopped_run_starts_no_queued_release_and_writes_nothing(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)

        class Args(object):
            pass
        args = Args()
        args.out = out
        rec._STOP.set()
        self.addCleanup(rec._STOP.clear)
        entry = {"version": "8.4.0", "tag": "mysql-8.4.0", "maturity": "LTS"}
        self.assertEqual(rec.read_release(entry, "mysql", args, set(), {}), "stopped")
        self.assertFalse(os.path.exists(os.path.join(out, "mysql", "8.4.0.json")))


if __name__ == "__main__":
    unittest.main()
