"""Schema versions against real MySQL 8.4 and MariaDB 11.4 containers.

The round trip that proves the sync SQL: seed a database (Target), a
changed copy of it (Source) and a second copy of Target, compare Target with
Source, run the generated script on the copy with the server's own client,
read the copy again and compare it with Source: nothing may differ except what
the script lists as not generated. Also checks that a connection's default
database does not change the definitions read (SHOW CREATE VIEW drops the
view's own database name when it is the default).

Skipped without Docker and a mysql or mariadb client, like
test_connector_integration.py (whose helpers it uses). About a minute per
engine when Docker is available.
"""
import os
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_connector_integration import _HAVE_DOCKER, _docker, _free_port, _wait_ready  # noqa: E402
from myflames import schema_versions  # noqa: E402
from myflames.connector import MySQLConnection  # noqa: E402

BEFORE = """
CREATE TABLE customers (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  email VARCHAR(100) NOT NULL,
  name VARCHAR(80) CHARACTER SET latin1 DEFAULT NULL,
  score DECIMAL(10,2) DEFAULT '0.00',
  status ENUM('a','b','c') NOT NULL DEFAULT 'a',
  PRIMARY KEY (id),
  UNIQUE KEY uq_email (email),
  KEY idx_name (name(10)),
  CONSTRAINT chk_score CHECK (score >= 0)
) ENGINE=InnoDB COMMENT='Customers';
INSERT INTO customers (email, name) VALUES ('a@x', 'A');
CREATE TABLE orders (
  id BIGINT NOT NULL AUTO_INCREMENT,
  customer_id INT UNSIGNED NOT NULL,
  total DECIMAL(12,2) NOT NULL,
  note TEXT,
  PRIMARY KEY (id),
  KEY fk_customer (customer_id),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE logs (id INT NOT NULL, at DATE NOT NULL, PRIMARY KEY (id, at))
  PARTITION BY RANGE (YEAR(at)) (PARTITION p2025 VALUES LESS THAN (2026), PARTITION p2026 VALUES LESS THAN (2027));
CREATE TABLE items (id INT UNSIGNED NOT NULL PRIMARY KEY, label VARCHAR(40) NOT NULL) ENGINE=InnoDB;
CREATE TABLE item_tags (item_id INT UNSIGNED NOT NULL, tag VARCHAR(20) NOT NULL, PRIMARY KEY (item_id, tag),
  CONSTRAINT fk_tag_item FOREIGN KEY (item_id) REFERENCES items (id)) ENGINE=InnoDB;
CREATE TABLE notes (id INT NOT NULL PRIMARY KEY, body VARCHAR(100), title CHAR(10) NOT NULL DEFAULT '')
  ENGINE=InnoDB DEFAULT CHARSET=latin1;
CREATE TABLE legacy (id INT PRIMARY KEY) ENGINE=InnoDB;
CREATE VIEW v_orders AS SELECT o.id, c.email FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.total > 10;
CREATE PROCEDURE p_touch(IN cid INT) UPDATE customers SET name = 'x' WHERE id = cid;
CREATE FUNCTION f_double(x INT) RETURNS INT DETERMINISTIC RETURN x * 2;
CREATE TRIGGER trg_bi BEFORE INSERT ON orders FOR EACH ROW SET NEW.total = ROUND(NEW.total, 2);
CREATE TRIGGER trg_bi2 BEFORE INSERT ON orders FOR EACH ROW SET NEW.note = COALESCE(NEW.note, '');
CREATE EVENT ev_cleanup ON SCHEDULE EVERY 1 DAY STARTS '2026-01-01 00:00:00' DISABLE DO DELETE FROM logs WHERE at < '2000-01-01';
CREATE TABLE parent2 (id INT NOT NULL PRIMARY KEY) ENGINE=InnoDB;
CREATE TABLE child2 (pid INT NOT NULL, KEY k_pid (pid), CONSTRAINT fk_c2 FOREIGN KEY (pid) REFERENCES parent2 (id)) ENGINE=InnoDB;
CREATE TABLE ca (v INT NOT NULL) ENGINE=InnoDB;
CREATE TABLE cz (v INT NOT NULL, CONSTRAINT chk_pos CHECK (v > 0)) ENGINE=InnoDB;
CREATE TABLE parts (id INT NOT NULL, d DATE NOT NULL, PRIMARY KEY (id, d)) ENGINE=InnoDB
  PARTITION BY RANGE (YEAR(d)) (PARTITION p1 VALUES LESS THAN (2026), PARTITION p2 VALUES LESS THAN MAXVALUE);
"""

# Applied to Source only: one of each kind of change the sync handles.
CHANGES = """
ALTER DATABASE {db} COLLATE utf8mb4_bin;
ALTER TABLE customers {drop_check} chk_score;
ALTER TABLE customers DROP COLUMN score, MODIFY email VARCHAR(255) NOT NULL COMMENT 'login',
  ADD COLUMN phone VARCHAR(20) DEFAULT NULL AFTER email, DROP INDEX idx_name, ADD INDEX idx_name (name(20)),
  ADD INDEX idx_status (status), MODIFY status ENUM('a','b','c') NOT NULL DEFAULT 'a' AFTER id,
  COMMENT='Customer accounts';
ALTER TABLE orders DROP FOREIGN KEY fk_orders_customer;
ALTER TABLE orders MODIFY note MEDIUMTEXT, ADD COLUMN currency CHAR(3) NOT NULL DEFAULT 'USD',
  ADD CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE RESTRICT;
ALTER TABLE logs ADD PARTITION (PARTITION p2027 VALUES LESS THAN (2028));
ALTER TABLE item_tags DROP FOREIGN KEY fk_tag_item;
ALTER TABLE items MODIFY id BIGINT UNSIGNED NOT NULL;
ALTER TABLE item_tags MODIFY item_id BIGINT UNSIGNED NOT NULL,
  ADD CONSTRAINT fk_tag_item FOREIGN KEY (item_id) REFERENCES items (id);
ALTER TABLE notes DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
DROP TABLE legacy;
CREATE TABLE audit (id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, customer_id INT UNSIGNED NOT NULL,
  at DATETIME NOT NULL, KEY k_cust (customer_id),
  CONSTRAINT fk_audit_customer FOREIGN KEY (customer_id) REFERENCES customers (id)) ENGINE=InnoDB;
CREATE OR REPLACE VIEW v_orders AS SELECT o.id, c.email, o.total FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.total > 10;
CREATE VIEW v_big AS SELECT id, email FROM v_orders WHERE total > 1000;
DROP TRIGGER trg_bi2;
DROP PROCEDURE p_touch;
CREATE PROCEDURE p_touch(IN cid INT) UPDATE customers SET name = 'touched' WHERE id = cid;
CREATE FUNCTION f_triple(x INT) RETURNS INT DETERMINISTIC RETURN x * 3;
CREATE TRIGGER trg_bi0 BEFORE INSERT ON orders FOR EACH ROW PRECEDES trg_bi SET NEW.currency = UPPER(NEW.currency);
ALTER EVENT ev_cleanup ON SCHEDULE EVERY 2 DAY STARTS '2026-01-01 00:00:00';
DROP TABLE child2;
ALTER TABLE parent2 MODIFY id BIGINT NOT NULL;
CREATE TABLE child3 (pid BIGINT NOT NULL, KEY k_pid (pid), CONSTRAINT fk_c2 FOREIGN KEY (pid) REFERENCES parent2 (id)) ENGINE=InnoDB;
ALTER TABLE cz {drop_check} chk_pos;
ALTER TABLE ca ADD CONSTRAINT chk_pos CHECK (v > 0);
ALTER TABLE parts DROP PRIMARY KEY, DROP COLUMN d, ADD PRIMARY KEY (id) PARTITION BY HASH (id) PARTITIONS 2;
ALTER TABLE customers ADD FULLTEXT KEY ft_email (email);
ALTER TABLE customers ADD FULLTEXT KEY ft_name (name);
"""

# MariaDB only: a column default that calls a new sequence needs it first.
MARIADB_CHANGES = """
CREATE SEQUENCE seq_new;
CREATE TABLE uses_seq (id BIGINT NOT NULL DEFAULT NEXT VALUE FOR seq_new PRIMARY KEY) ENGINE=InnoDB;
ALTER TABLE items ADD COLUMN serial BIGINT DEFAULT NEXT VALUE FOR seq_new;
"""


@unittest.skipUnless(_HAVE_DOCKER, "Docker or mysql client not available")
class _LiveSchemaVersions(unittest.TestCase):
    IMAGE = None
    IS_MARIADB = False
    ROOT_PASSWORD = "schemaver-" + uuid.uuid4().hex[:6]

    @classmethod
    def setUpClass(cls):
        cls.container = "myflames-schemaver-" + uuid.uuid4().hex[:8]
        cls.port = _free_port()
        env = "MARIADB_ROOT_PASSWORD" if cls.IS_MARIADB else "MYSQL_ROOT_PASSWORD"
        try:
            # Native AIO off: a host running many containers can run out of
            # Linux AIO contexts, which stops InnoDB from starting.
            _docker("run", "-d", "--rm", "--name", cls.container, "-e", env + "=" + cls.ROOT_PASSWORD,
                    "-p", "127.0.0.1:{}:3306".format(cls.port), cls.IMAGE, "--innodb-use-native-aio=0")
        except RuntimeError as exc:
            raise unittest.SkipTest("could not start {}: {}".format(cls.IMAGE, exc))
        client = "mariadb-admin" if cls.IS_MARIADB else "mysqladmin"
        if not _wait_ready(cls.container, [client, "--protocol=TCP", "-h127.0.0.1", "-uroot",
                                           "-p" + cls.ROOT_PASSWORD, "ping"], timeout=150):
            _docker("rm", "-f", "-v", cls.container, check=False)
            raise unittest.SkipTest("{} did not become ready".format(cls.IMAGE))
        drop_check = "DROP CONSTRAINT" if cls.IS_MARIADB else "DROP CHECK"
        for db in ("sv_before", "sv_after", "sv_copy", "sv_after_copy"):
            cls.sql("CREATE DATABASE {db} DEFAULT CHARACTER SET utf8mb4;\nUSE {db};\n{body}".format(db=db, body=BEFORE))
        for db in ("sv_after", "sv_after_copy"):
            cls.sql("USE {db};\n".format(db=db) + CHANGES.format(db=db, drop_check=drop_check)
                    + (MARIADB_CHANGES if cls.IS_MARIADB else ""))

    @classmethod
    def tearDownClass(cls):
        _docker("rm", "-f", "-v", cls.container, check=False)

    @classmethod
    def sql(cls, text):
        """Run a script with the server's own client (it handles DELIMITER)."""
        client = "mariadb" if cls.IS_MARIADB else "mysql"
        run = subprocess.run(["docker", "exec", "-i", cls.container, client, "--protocol=TCP", "-h127.0.0.1",
                              "-uroot", "-p" + cls.ROOT_PASSWORD, "--comments"],
                             input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if run.returncode != 0:
            raise AssertionError("script failed: %s" % run.stderr.decode("utf-8", "replace"))

    def capture(self, database, default_database=None):
        with MySQLConnection(host="127.0.0.1", port=self.port, user="root", password=self.ROOT_PASSWORD,
                             database=default_database, query_timeout=60) as conn:
            return schema_versions.capture(conn, database, {"host": "127.0.0.1", "port": self.port})

    @staticmethod
    def every_key(source, target):
        """A full round trip includes the drops the default choice leaves out."""
        return [o["key"] for o in schema_versions.compare(source, target, sync=False)["objects"]]

    def test_sync_round_trip(self):
        target, source = self.capture("sv_copy"), self.capture("sv_after")
        report = schema_versions.compare(source, target, chosen_keys=self.every_key(source, target))
        self.assertFalse(report["identical"])
        self.assertEqual(report["sync"]["skipped"], [])
        self.sql(report["sync"]["script"])
        again = schema_versions.compare(source, self.capture("sv_copy"), sync=False)
        left = [(o["type"], o["name"], o.get("changes")) for o in again["objects"] if o["status"] != "identical"]
        self.assertEqual(left, [], "the sync left differences")

    def test_reverse_sync_round_trip(self):
        """Restore the original schema by syncing the changed Target to its Source."""
        target, source = self.capture("sv_after_copy"), self.capture("sv_before")
        report = schema_versions.compare(source, target, chosen_keys=self.every_key(source, target))
        self.assertFalse(report["identical"])
        self.sql(report["sync"]["script"])
        again = schema_versions.compare(source, self.capture("sv_after_copy"), sync=False)
        left = [(o["type"], o["name"], o.get("changes")) for o in again["objects"] if o["status"] != "identical"]
        self.assertEqual(left, [], "the reverse sync left differences")

    def test_default_database_does_not_change_the_definitions(self):
        plain, scoped = self.capture("sv_before"), self.capture("sv_before", default_database="sv_before")
        self.assertEqual(plain["checksum"], scoped["checksum"])
        view = next(o for o in scoped["objects"] if o["name"] == "v_orders")
        self.assertIn("`sv_before`.`v_orders`", view["ddl"])

    def test_identical_databases_compare_identical_without_statements(self):
        report = schema_versions.compare(self.capture("sv_before"), self.capture("sv_before"))
        self.assertTrue(report["identical"])
        self.assertEqual(report["sync"]["statements"], [])

    def test_default_sync_keeps_target_only_objects_without_failing(self):
        """Retained child2 must not break the parent ALTER or reuse fk_c2."""
        self.sql("CREATE DATABASE sv_default DEFAULT CHARACTER SET utf8mb4;\nUSE sv_default;\n" + BEFORE)
        if not self.IS_MARIADB:
            self.sql("ALTER DATABASE sv_default READ ONLY = 1")
        target, source = self.capture("sv_default"), self.capture("sv_after")
        report = schema_versions.compare(source, target)
        for name in ("parent2", "child3"):
            entry = next(o for o in report["objects"] if o["name"] == name)
            self.assertEqual(entry["blocked_by"], ["relation:child2"])
        self.sql(report["sync"]["script"])
        again = schema_versions.compare(source, self.capture("sv_default"), sync=False)
        statuses = {o["name"]: o["status"] for o in again["objects"]}
        self.assertEqual(statuses["child2"], "only_in_target")
        self.assertEqual(statuses["parent2"], "different")
        self.assertEqual(statuses["child3"], "only_in_source")
        self.assertEqual(statuses["customers"], "identical", "unrelated changes should still run")


class TestLiveSchemaVersionsMySQL84(_LiveSchemaVersions):
    IMAGE = "mysql:8.4"

    def test_read_only_target_is_opened_for_the_script(self):
        """MySQL 8.0.22+ refuses DDL in a READ ONLY database (ER_SCHEMA_READ_ONLY)."""
        self.sql("CREATE DATABASE sv_ro DEFAULT CHARACTER SET utf8mb4;\nUSE sv_ro;\n" + BEFORE
                 + "\nALTER DATABASE sv_ro READ ONLY = 1;")
        target, source = self.capture("sv_ro"), self.capture("sv_after")
        report = schema_versions.compare(source, target, chosen_keys=self.every_key(source, target))
        self.assertIn("ALTER DATABASE `sv_ro` READ ONLY = 0", [s["sql"] for s in report["sync"]["statements"]])
        self.sql(report["sync"]["script"])
        again = schema_versions.compare(source, self.capture("sv_ro"), sync=False)
        left = [(o["type"], o["name"], o.get("changes")) for o in again["objects"] if o["status"] != "identical"]
        self.assertEqual(left, [])


class TestLiveSchemaVersionsMariaDB114(_LiveSchemaVersions):
    IMAGE = "mariadb:11.4"
    IS_MARIADB = True


del _LiveSchemaVersions


if __name__ == "__main__":
    unittest.main()
