"""
End-to-end integration tests that run the connector and collectors against
real MySQL 8.4 and MariaDB 11.4 containers.

This module is skipped automatically when Docker is not available so CI can
run the rest of the suite in plain Python environments. When Docker IS
available the whole class takes roughly 60–90 seconds because it boots the
containers, seeds them, creates two different user accounts (one with
``mysql_native_password``, one with ``caching_sha2_password``), and then
drives the real ``myflames.connector`` against them.

Run directly:
    python3 -m unittest test.test_connector_integration -v
"""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames.connector import MySQLConnection, ConnectorError
from myflames.collectors import (
    collect_schema,
    collect_stats,
    collect_session_variables,
    extract_table_names,
)
from myflames.advisor import advise
from myflames.parser import parse_explain, analyze_plan


# ---------------------------------------------------------------------------
# Docker availability probe
# ---------------------------------------------------------------------------

def _docker_available():
    """Return True if the Docker daemon is reachable AND we have a mysql
    client binary on PATH (the connector needs one to talk to the container
    even though the server runs inside Docker)."""
    if not shutil.which("docker"):
        return False
    if not (shutil.which("mysql") or shutil.which("mariadb")):
        return False
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        )
    except Exception:
        return False
    return r.returncode == 0


_HAVE_DOCKER = _docker_available()


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def _docker(*args, check=True, capture=True):
    """Run a docker command and return the CompletedProcess."""
    kw = {}
    if capture:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    r = subprocess.run(["docker"] + list(args), **kw)
    if check and r.returncode != 0:
        msg = ""
        if r.stderr is not None:
            msg = r.stderr.decode("utf-8", errors="replace")
        raise RuntimeError("docker {} failed: {}".format(args[0], msg))
    return r


def _wait_ready(container, cmd, timeout=90):
    """Poll a container until *cmd* returns zero or *timeout* expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "exec", container] + cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            return True
        time.sleep(1)
    return False


def _free_port():
    """Return a free TCP port on localhost."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAVE_DOCKER, "Docker or mysql client not available")
class BaseLiveServerTest(unittest.TestCase):
    """Base class that spins up ONE container per subclass.

    Subclasses override :attr:`IMAGE`, :attr:`IS_MARIADB`, and :meth:`_setup_auth`
    to create the two user accounts we test with.
    """

    IMAGE = None            # e.g. "mysql:8.4"
    IS_MARIADB = False
    ROOT_PASSWORD = "rootpass-" + uuid.uuid4().hex[:6]
    #: Extra mysqld args appended to the docker image entrypoint. MySQL 8.4
    #: disables ``mysql_native_password`` by default — we explicitly enable
    #: it so the native-password user can actually authenticate.
    MYSQLD_ARGS = ()

    @classmethod
    def setUpClass(cls):
        cls.container = "myflames-it-" + uuid.uuid4().hex[:8]
        cls.port = _free_port()
        env_var = "MARIADB_ROOT_PASSWORD" if cls.IS_MARIADB else "MYSQL_ROOT_PASSWORD"
        _docker(
            "run", "-d", "--rm",
            "--name", cls.container,
            "-e", env_var + "=" + cls.ROOT_PASSWORD,
            "-p", "{}:3306".format(cls.port),
            cls.IMAGE, *cls.MYSQLD_ARGS,
        )
        # Wait for the server to accept connections (use docker exec to bypass
        # the "first user created without TLS" issue on caching_sha2).
        ready_cmd = (
            ["mariadb-admin", "--protocol=TCP", "-h127.0.0.1",
             "-uroot", "-p" + cls.ROOT_PASSWORD, "ping"]
            if cls.IS_MARIADB
            else ["mysqladmin", "--protocol=TCP", "-h127.0.0.1",
                  "-uroot", "-p" + cls.ROOT_PASSWORD, "ping"]
        )
        if not _wait_ready(cls.container, ready_cmd, timeout=120):
            _docker("logs", cls.container, check=False)
            raise unittest.SkipTest(
                "Server in container {} did not become ready".format(cls.container)
            )
        cls._setup_auth()
        cls._seed_data()

    @classmethod
    def tearDownClass(cls):
        try:
            _docker("rm", "-f", cls.container, check=False)
        except Exception:
            pass

    # ---- subclass hooks ------------------------------------------------

    @classmethod
    def _exec_root_sql(cls, sql):
        """Run SQL via ``docker exec`` as root inside the container.

        Uses ``--protocol=TCP --host=127.0.0.1`` so both MySQL and MariaDB
        use the TCP listener rather than the unix socket — MariaDB's root
        is bound to ``unix_socket`` plugin by default and MySQL's image
        sometimes has no socket at the path the client looks for.
        """
        bin_ = "mariadb" if cls.IS_MARIADB else "mysql"
        cmd = [
            "docker", "exec", cls.container,
            bin_, "--protocol=TCP", "-h127.0.0.1",
            "-uroot", "-p" + cls.ROOT_PASSWORD, "-N", "-s", "-r",
            "-e", sql,
        ]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError(
                "root SQL failed: {}".format(r.stderr.decode("utf-8", errors="replace"))
            )
        return r.stdout.decode("utf-8", errors="replace")

    @classmethod
    def _setup_auth(cls):
        """Create two test users — one native, one caching_sha2."""
        cls.native_pw = "native-" + uuid.uuid4().hex[:8]
        cls.sha2_pw   = "sha2-"   + uuid.uuid4().hex[:8]
        # Both users get all privs on testdb so the advisor has something
        # to look at. We also grant PROCESS so SHOW VARIABLES works cleanly
        # across versions.
        cls._exec_root_sql("CREATE DATABASE IF NOT EXISTS testdb;")
        if cls.IS_MARIADB:
            # MariaDB doesn't implement caching_sha2_password; fall back to
            # the native equivalent and mark the user as such. The test still
            # exercises a second independent account so it's meaningful.
            cls._exec_root_sql(
                "CREATE USER IF NOT EXISTS 'user_native'@'%' IDENTIFIED BY '{}';"
                .format(cls.native_pw)
            )
            cls._exec_root_sql(
                "CREATE USER IF NOT EXISTS 'user_sha2'@'%' IDENTIFIED BY '{}';"
                .format(cls.sha2_pw)
            )
        else:
            cls._exec_root_sql(
                "CREATE USER IF NOT EXISTS 'user_native'@'%' "
                "IDENTIFIED WITH mysql_native_password BY '{}';".format(cls.native_pw)
            )
            cls._exec_root_sql(
                "CREATE USER IF NOT EXISTS 'user_sha2'@'%' "
                "IDENTIFIED WITH caching_sha2_password BY '{}';".format(cls.sha2_pw)
            )
        cls._exec_root_sql("GRANT ALL ON testdb.* TO 'user_native'@'%';")
        cls._exec_root_sql("GRANT ALL ON testdb.* TO 'user_sha2'@'%';")
        cls._exec_root_sql("GRANT PROCESS ON *.* TO 'user_native'@'%';")
        cls._exec_root_sql("GRANT PROCESS ON *.* TO 'user_sha2'@'%';")
        cls._exec_root_sql("FLUSH PRIVILEGES;")

    @classmethod
    def _seed_data(cls):
        """Populate a small schema the tests can run EXPLAIN ANALYZE against."""
        cls._exec_root_sql("USE testdb;")
        cls._exec_root_sql(
            "CREATE TABLE IF NOT EXISTS testdb.users ("
            "  id INT PRIMARY KEY AUTO_INCREMENT,"
            "  country VARCHAR(2),"
            "  status VARCHAR(20),"
            "  KEY idx_country (country)"
            ") ENGINE=InnoDB;"
        )
        cls._exec_root_sql(
            "CREATE TABLE IF NOT EXISTS testdb.orders ("
            "  id INT PRIMARY KEY AUTO_INCREMENT,"
            "  user_id INT,"
            "  amount DECIMAL(10,2),"
            "  KEY idx_user (user_id)"
            ") ENGINE=InnoDB;"
        )
        # A few hundred rows is enough to make EXPLAIN ANALYZE meaningful
        # without making the test slow.
        if cls.IS_MARIADB:
            cls._exec_root_sql("SET max_recursive_iterations=20000;")
        else:
            cls._exec_root_sql("SET cte_max_recursion_depth=20000;")
        cls._exec_root_sql(
            "INSERT INTO testdb.users (country, status) "
            "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n<500) "
            "SELECT IF(n%3=0,'US','UK'), 'active' FROM s;"
        )
        cls._exec_root_sql(
            "INSERT INTO testdb.orders (user_id, amount) "
            "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n<500) "
            "SELECT (n%500)+1, n*1.0 FROM s;"
        )
        cls._exec_root_sql("ANALYZE TABLE testdb.users, testdb.orders;")

    # ---- shared assertions --------------------------------------------

    def _connect(self, user, password):
        return MySQLConnection(
            host="127.0.0.1",
            port=self.port,
            user=user,
            password=password,
            database="testdb",
            # Disable TLS so we don't need to generate certs for these
            # throwaway containers. Real-world users pass --ssl-mode=REQUIRED
            # (or VERIFY_IDENTITY) and the same code path handles it.
            ssl_mode="DISABLED",
        )

    def _assert_pipeline(self, user, password):
        """Run the full pipeline end-to-end against the given user and
        assert every collector and the advisor produced something."""
        with self._connect(user, password) as conn:
            # 1. Version round-trip confirms auth works
            version = conn.server_version()
            self.assertTrue(version)

            # 2. EXPLAIN ANALYZE returns something parse_explain can digest.
            # We use parse_explain (instead of json.loads directly) because
            # that's exactly the path the CLI takes in live mode — it tolerates
            # MySQL ``EXPLAIN:`` prefixes, MariaDB ``ANALYZE`` column headers,
            # and escaped newlines automatically.
            sql = (
                "SELECT u.country, COUNT(*) FROM users u "
                "JOIN orders o ON o.user_id = u.id "
                "WHERE u.status = 'active' GROUP BY u.country"
            )
            raw = conn.explain_analyze(sql)
            self.assertTrue(raw.strip(), "EXPLAIN ANALYZE returned empty output")

            # 3. Parser digests it and analyze_plan runs
            root = parse_explain(raw)
            self.assertIsInstance(root, dict)
            self.assertIn("children", root)
            analysis = analyze_plan(root)

            # 4. Collectors pull schema, stats, variables
            tables = extract_table_names(sql, default_schema="testdb")
            self.assertIn("testdb.users", tables)
            self.assertIn("testdb.orders", tables)

            schema = collect_schema(conn, tables)
            self.assertIn("testdb.users", schema)
            self.assertEqual(schema["testdb.users"]["engine"], "InnoDB")
            # The index we created must be discovered
            idx_names = [i.get("name") for i in schema["testdb.users"]["indexes"]]
            self.assertIn("idx_country", idx_names)

            stats = collect_stats(conn, tables)
            self.assertIn("testdb.users", stats)
            self.assertGreater(stats["testdb.users"]["table_rows"], 0)

            variables = collect_session_variables(conn)
            self.assertIn("innodb_buffer_pool_size", variables)
            self.assertIn("sort_buffer_size", variables)
            self.assertIn("optimizer_switch", variables)

            # 5. Advisor populates the environment hints
            advise(analysis, schema=schema, stats=stats, variables=variables)
            self.assertIn("environment_warnings", analysis)
            self.assertIn("environment_suggestions", analysis)
            self.assertIn("collected_variables", analysis)

    # ---- test methods (override IMAGE/IS_MARIADB in subclasses) -------

    def test_native_password_user(self):
        self._assert_pipeline("user_native", self.native_pw)

    def test_caching_sha2_password_user(self):
        self._assert_pipeline("user_sha2", self.sha2_pw)

    def test_wrong_password_fails_cleanly(self):
        with self._connect("user_native", "definitely-wrong") as conn:
            with self.assertRaises(ConnectorError) as ctx:
                conn.run("SELECT 1")
            self.assertIn("access", str(ctx.exception).lower())

    def test_no_collect_schema_toggle(self):
        """Verify the collector functions honour being skipped (they're
        called conditionally by the CLI; we simulate the skip directly)."""
        with self._connect("user_native", self.native_pw) as conn:
            tables = ["testdb.users"]
            # When the CLI passes --no-collect-schema, schema == {} is the
            # expected contract. Here we just verify the empty path is safe.
            advise({"full_scans": []}, schema={}, stats={}, variables={})


# ---------------------------------------------------------------------------
# Concrete server classes
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAVE_DOCKER, "Docker not available")
class TestLiveMySQL84(BaseLiveServerTest):
    IMAGE = "mysql:8.4"
    IS_MARIADB = False
    # mysql_native_password is NOT loaded by default on MySQL 8.4 — enable
    # it via mysqld startup args so we can test a native-password user.
    MYSQLD_ARGS = ("--mysql-native-password=ON",)


@unittest.skipUnless(_HAVE_DOCKER, "Docker not available")
class TestLiveMariaDB114(BaseLiveServerTest):
    IMAGE = "mariadb:11.4"
    IS_MARIADB = True


# Prevent the abstract base class from being collected by unittest's loader.
del BaseLiveServerTest


if __name__ == "__main__":
    unittest.main()
