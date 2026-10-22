"""Documentation links for system variables: the generated map, the lookup
by engine, version and flavor, and the generator's page readers."""
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

from myflames import parameters as P, sysvar_docs as D


_SPEC = importlib.util.spec_from_file_location(
    "gen_sysvar_docs", str(Path(__file__).resolve().parents[1] / "scripts/gen_sysvar_docs.py"))
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)

#: The desktop app opens only these hosts (apps/desktop/backend.cjs EXTERNAL_HOSTS).
_URL = re.compile(r"^https://(dev\.mysql\.com|mariadb\.com|docs\.percona\.com)/[A-Za-z0-9_./-]+#[A-Za-z0-9_.-]+$")


def url(engine, version, name, flavor=None):
    found = P.documentation(engine, version, name, flavor)
    return found and found["url"]


class TestLookup(unittest.TestCase):
    def test_mysql_manual_follows_the_server_version(self):
        self.assertEqual(url("mysql", (8, 4, 10), "innodb_buffer_pool_size"),
                         "https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html#sysvar_innodb_buffer_pool_size")
        self.assertEqual(url("mysql", (8, 0, 44), "max_connections"),
                         "https://dev.mysql.com/doc/refman/8.0/en/server-system-variables.html#sysvar_max_connections")
        # Innovation releases are documented by the next manual; a newer
        # server than every saved manual uses the newest one.
        self.assertIn("/refman/8.4/", url("mysql", (8, 3, 0), "max_connections"))
        self.assertIn("/refman/9.7/", url("mysql", (9, 2, 0), "max_connections"))
        self.assertIn("/refman/%s/" % D.MYSQL_VERSIONS[-1], url("mysql", (99, 1, 0), "max_connections"))
        self.assertIn("/refman/8.0/", url("mysql", (5, 7, 44), "max_connections"))

    def test_mysql_pages_and_anchors(self):
        self.assertEqual(url("mysql", (8, 4, 0), "binlog_format"),
                         "https://dev.mysql.com/doc/refman/8.4/en/replication-options-binary-log.html#sysvar_binlog_format")
        self.assertEqual(url("mysql", (8, 4, 0), "gtid_mode"),
                         "https://dev.mysql.com/doc/refman/8.4/en/replication-options-gtids.html#sysvar_gtid_mode")
        self.assertEqual(url("mysql", (8, 4, 0), "replica_parallel_workers"),
                         "https://dev.mysql.com/doc/refman/8.4/en/replication-options-replica.html#sysvar_replica_parallel_workers")
        # The reference links sql_mode to an option anchor; its sysvar_
        # anchor on server-system-variables is the variable's own entry.
        self.assertEqual(url("mysql", (8, 4, 0), "sql_mode"),
                         "https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html#sysvar_sql_mode")
        self.assertEqual(url("mysql", (8, 4, 0), "validate_password.length"),
                         "https://dev.mysql.com/doc/refman/8.4/en/validate-password-options-variables.html"
                         "#sysvar_validate_password.length")
        self.assertEqual(P.documentation("mysql", (8, 4, 0), "INNODB_BUFFER_POOL_SIZE")["source"],
                         "MySQL 8.4 Reference Manual")

    def test_a_name_the_server_manual_leaves_out_uses_the_nearest_manual(self):
        # innodb_log_file_size is in the 8.0 and 8.4 manuals only.
        self.assertIn("/refman/8.4/", url("mysql", (9, 7, 0), "innodb_log_file_size"))
        found = P.documentation("mysql", (8, 4, 0), "activate_mandatory_roles")
        self.assertIn("/refman/9.7/", found["url"])
        self.assertEqual(found["source"], "MySQL 9.7 Reference Manual")

    def test_mariadb_documentation(self):
        self.assertEqual(url("mariadb", (11, 4, 5), "innodb_buffer_pool_size"),
                         "https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables"
                         "#innodb_buffer_pool_size")
        # The full list sends this to the GTID overview; the variable page documents it.
        self.assertEqual(url("mariadb", (11, 4, 5), "gtid_domain_id"),
                         "https://mariadb.com/docs/server/ha-and-performance/standard-replication/gtid/"
                         "gtid-system-variables#gtid_domain_id")
        self.assertIn("galera-cluster-system-variables#wsrep_on", url("mariadb", (11, 4, 5), "wsrep_on"))
        self.assertEqual(P.documentation("mariadb", (11, 4), "max_connections")["source"], "MariaDB documentation")
        # MariaDB never links to the MySQL manual.
        self.assertIsNone(url("mariadb", (11, 4), "replica_parallel_workers"))

    def test_percona_only_variables_link_to_percona_on_percona_servers(self):
        self.assertIsNone(url("mysql", (8, 4, 0), "innodb_corrupt_table_action"))
        found = P.documentation("mysql", (8, 4, 11), "innodb_corrupt_table_action", "percona")
        self.assertEqual(found["url"], "https://docs.percona.com/percona-server/8.4/innodb-corrupt-table-action.html"
                                       "#innodb_corrupt_table_action")
        self.assertEqual(found["source"], "Percona Server 8.4 documentation")
        # A variable MySQL documents keeps the MySQL manual on Percona too.
        self.assertIn("dev.mysql.com", url("mysql", (8, 4, 11), "max_connections", "percona"))
        pxc = P.documentation("mysql", (8, 4, 11), "pxc_strict_mode", "percona")
        self.assertEqual(pxc["url"], "https://docs.percona.com/percona-xtradb-cluster/8.4/wsrep-system-index.html"
                                     "#pxc_strict_mode")
        self.assertEqual(pxc["source"], "Percona XtraDB Cluster 8.4 documentation")

    def test_unknown_and_invalid_names(self):
        for name in ("no_such_variable", "", None, "x;y", "a b", "../x", "max_connections#x"):
            self.assertIsNone(P.documentation("mysql", (8, 4, 0), name), name)
            self.assertIsNone(P.documentation("mariadb", (11, 4, 0), name), name)

    def test_every_generated_link_is_an_allowed_https_url(self):
        checked = 0
        for version in ((8, 0), (8, 4), (9, 7), (26, 7)):
            for name in D.MYSQL:
                link = P.documentation("mysql", version, name)
                self.assertRegex(link["url"], _URL)
                checked += 1
            for name in list(D.PERCONA) + list(D.PXC):
                link = P.documentation("mysql", version, name, "percona")
                self.assertRegex(link["url"], _URL)
                checked += 1
        for name in D.MARIADB:
            self.assertRegex(P.documentation("mariadb", (11, 4), name)["url"], _URL)
            checked += 1
        self.assertGreater(len(D.MYSQL), 900)
        self.assertGreater(len(D.MARIADB), 900)
        self.assertGreater(checked, 5000)


class TestParametersCarryLinks(unittest.TestCase):
    def test_items_have_documentation_and_the_report_its_flavor(self):
        rows = [["max_connections", "151"], ["innodb_corrupt_table_action", "assert"],
                ["version_comment", "Percona Server (GPL), Release 11, Revision 57878ff8"]]
        report = P.compute_parameters("mysql", (8, 4, 11), rows=rows, info_rows=[], persisted_rows=[])
        self.assertEqual(report["flavor"], "percona")
        found = {item["name"]: item for item in report["items"]}
        self.assertIn("percona-server/8.4/", found["innodb_corrupt_table_action"]["documentation"]["url"])
        self.assertIn("refman/8.4/", found["max_connections"]["documentation"]["url"])
        plain = P.compute_parameters("mysql", (8, 4, 11), rows=rows[:2], info_rows=[], persisted_rows=[])
        self.assertEqual(plain["flavor"], "mysql")
        self.assertIsNone({i["name"]: i for i in plain["items"]}["innodb_corrupt_table_action"]["documentation"])
        # The caller's flavor (perf_common's server) wins over the comment.
        told = P.compute_parameters("mysql", (8, 4, 11), rows=rows, info_rows=[], persisted_rows=[], flavor="mysql")
        self.assertEqual(told["flavor"], "mysql")
        maria = P.compute_parameters("mariadb", (11, 4, 5), catalog_rows=[[
            "MAX_CONNECTIONS", "151", "COMPILE-TIME", "151", "GLOBAL", "INT UNSIGNED", "10", "100000", "1",
            None, "NO", None, "The number of simultaneous clients allowed.", "REQUIRED"]])
        self.assertEqual(maria["flavor"], "mariadb")
        self.assertIn("mariadb.com/docs/", maria["items"][0]["documentation"]["url"])


class TestGenerator(unittest.TestCase):
    MYSQL = ('<tr><th scope="row"><a class="link" href="server-system-variables.html#sysvar_max_connections">'
             'max_connections</a></th>\n<td>Yes</td>\n<td>Yes</td>\n<td>Yes</td>\n<td></td>\n<td>Global</td>\n<td>Yes</td>\n</tr>'
             '<tr><th scope="row"><a class="link" href="server-options.html#option_mysqld_sql-mode">sql_mode</a> (note 1)</th>'
             '<td>Yes</td><td>Yes</td><td>Yes</td><td></td><td>Both</td><td>Yes</td></tr>'
             '<tr><th scope="row"><a class="link" href="server-status-variables.html#statvar_Aborted_clients">'
             'Aborted_clients</a></th><td></td><td></td><td></td><td>Yes</td><td>Global</td><td>No</td></tr>'
             '<tr><th scope="row"><a class="link" href="innodb-parameters.html#sysvar_innodb_gone">innodb_gone</a></th>'
             '<td>Yes</td><td>Yes</td><td>Yes</td><td></td><td>Global</td><td>No</td></tr>')

    def test_mysql_rows_with_and_without_saved_pages(self):
        plain = gen.mysql_links(self.MYSQL)
        self.assertEqual(plain["max_connections"], ("server-system-variables", "sysvar_max_connections"))
        self.assertEqual(plain["sql_mode"], ("server-options", "option_mysqld_sql-mode"))
        self.assertNotIn("aborted_clients", plain)
        self.assertNotIn("Aborted_clients", plain)
        with tempfile.TemporaryDirectory() as pages:
            Path(pages, "server-system-variables.html").write_text(
                '<a name="sysvar_max_connections"></a><a name="sysvar_sql_mode"></a>')
            Path(pages, "server-options.html").write_text('<a name="option_mysqld_sql-mode"></a>')
            Path(pages, "innodb-parameters.html").write_text('<a name="sysvar_innodb_other"></a>')
            verified = gen.mysql_links(self.MYSQL, pages)
        self.assertEqual(verified["sql_mode"], ("server-system-variables", "sysvar_sql_mode"))
        self.assertNotIn("innodb_gone", verified)

    def test_mariadb_list_skips_status_variables_and_moves_stale_anchors(self):
        text = ('<a href="/docs/server/x/server-system-variables#max_connections">max_connections</a>'
                '<a href="/docs/server/x/server-status-variables#aborted_clients">Aborted_clients</a>'
                '<a href="/docs/server/x/server-status-variables#threads_running">threads_running</a>'
                '<a href="/docs/server/x/mariadbd-options#abort-slave-event-count">--abort-slave-event-count</a>'
                '<a href="/docs/server/r/gtid">gtid_domain_id</a>')
        self.assertEqual(sorted(gen.mariadb_links(text)), ["gtid_domain_id", "max_connections"])
        with tempfile.TemporaryDirectory() as pages:
            base = Path(pages, "docs", "server")
            (base / "x").mkdir(parents=True)
            (base / "r" / "gtid").mkdir(parents=True)
            (base / "x" / "server-system-variables.html").write_text(
                '<h4 id="max_connections">max_connections</h4><h4 id="time_format">time_format</h4>')
            (base / "r" / "gtid.html").write_text('<h2 id="the-domain-id">The domain ID</h2>')
            (base / "r" / "gtid" / "gtid-system-variables.html").write_text(
                '<h4 id="gtid_domain_id">gtid_domain_id</h4>')
            found = gen.mariadb_links(text, pages)
        self.assertEqual(found["gtid_domain_id"], ("server/r/gtid/gtid-system-variables", "gtid_domain_id"))
        # Not in the list, but a heading on a variables page.
        self.assertEqual(found["time_format"], ("server/x/server-system-variables", "time_format"))

    def test_percona_headings_need_the_option_table(self):
        with tempfile.TemporaryDirectory() as manual:
            Path(manual, "feature.html").write_text(
                '<h3 id="innodb_corrupt_table_action"><code>innodb_corrupt_table_action</code></h3>'
                '<table><tr><td>Scope:</td><td>Global</td></tr></table>'
                '<h2 id="see_also">see_also</h2><p>No table.</p>'
                '<h3 id="audit_log_filterbuffer_size">audit_log_filter.buffer_size</h3>'
                '<table><tr><td>Dynamic</td><td>No</td></tr></table>'
                '<h3 id="wsrep_osu_method"><code>wsrep_OSU_method</code></h3>'
                '<table><tr><td>Command Line:</td><td>--wsrep-OSU-method</td></tr></table>')
            Path(manual, "release-notes").mkdir()
            Path(manual, "release-notes", "8.4.0.html").write_text(
                '<h3 id="old_var">old_var</h3><table><tr><td>Scope</td></tr></table>')
            found = gen.percona_links(manual)
        self.assertEqual(found, {"innodb_corrupt_table_action": ("feature.html", "innodb_corrupt_table_action"),
                                 "audit_log_filter.buffer_size": ("feature.html", "audit_log_filterbuffer_size"),
                                 "wsrep_osu_method": ("feature.html", "wsrep_osu_method")})

    def test_render_round_trips_through_the_lookup(self):
        text = gen.render({"8.4": {"a_var": ("server-system-variables", "sysvar_a_var")},
                           "9.7": {"a_var": ("server-system-variables", "sysvar_a_var"),
                                   "b_var": ("innodb-parameters", "option_b")}},
                          {"m_var": ("server/p/server-system-variables", "m_var")},
                          {"8.4": {"p_var": ("f.html", "p_var"), "a_var": ("g.html", "a_var")}},
                          {"8.4": {"wsrep_x": ("wsrep-system-index.html", "wsrep_x")}}, "2026-10-21")
        data = {}
        exec(compile(text, "sysvar_docs", "exec"), data)
        self.assertEqual(data["MYSQL"], {"a_var": 0, "b_var": (None, (1, "option_b"))})
        self.assertEqual(data["PERCONA"], {"p_var": 0})
        self.assertEqual(data["PXC"], {"wsrep_x": 0})
        self.assertIn("pages saved on 2026-10-21", text)


if __name__ == "__main__":
    unittest.main()
