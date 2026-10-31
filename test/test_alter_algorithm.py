"""Tests for myflames.alter_algorithm: one case per rule, each with the
server source it follows, the live recordings from MySQL 8.4 and MariaDB 11.4
(test/fixtures/alter_algorithm, scripts/record-alter-algorithm-fixtures.py),
the version and fact gates, and the editor's ALTER review payload.

Source locations: M = mysql-server 9.7 (trunk), Ma = mariadb-server 13.0
(trunk). handler0alter.cc is storage/innobase/handler/handler0alter.cc.
"""
import json
import os
import threading
import unittest

from myflames import alter_algorithm as aa
from myflames import editor_session as ed
from myflames.connector import ConnectorError

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "alter_algorithm")

MYSQL_BASE = """CREATE TABLE `t1` (
  `id` int NOT NULL AUTO_INCREMENT,
  `a` int DEFAULT NULL,
  `b` varchar(50) NOT NULL DEFAULT '',
  `c` varchar(10) CHARACTER SET latin1 COLLATE latin1_swedish_ci DEFAULT NULL,
  `d` varchar(60) DEFAULT NULL,
  `e` enum('x','y') DEFAULT NULL,
  `s` set('p','q') DEFAULT NULL,
  `n` int NOT NULL DEFAULT '0',
  `ts` datetime DEFAULT NULL,
  `u` varchar(20) CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci DEFAULT NULL,
  `ch` char(4) DEFAULT NULL,
  `tx` text,
  `dm` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ka` (`a`),
  KEY `kb` (`b`)
) ENGINE=InnoDB AUTO_INCREMENT=101 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""

MARIADB_BASE = """CREATE TABLE `t1` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `a` int(11) DEFAULT NULL,
  `b` varchar(50) NOT NULL DEFAULT '',
  `c` varchar(10) CHARACTER SET latin1 COLLATE latin1_swedish_ci DEFAULT NULL,
  `d` varchar(60) DEFAULT NULL,
  `e` enum('x','y') DEFAULT NULL,
  `s` set('p','q') DEFAULT NULL,
  `n` int(11) NOT NULL DEFAULT 0,
  `ts` datetime DEFAULT NULL,
  `u` varchar(20) CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci DEFAULT NULL,
  `ch` char(4) DEFAULT NULL,
  `tx` text DEFAULT NULL,
  `dm` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ka` (`a`),
  KEY `kb` (`b`)
) ENGINE=InnoDB AUTO_INCREMENT=101 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci"""

SMALL = {
    "mysql": "CREATE TABLE `t1` (\n  `k` int NOT NULL,\n  `v` int DEFAULT NULL,\n  `w` varchar(20) DEFAULT NULL%s\n"
             ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci",
    "mariadb": "CREATE TABLE `t1` (\n  `k` int(11) NOT NULL,\n  `v` int(11) DEFAULT NULL,\n  `w` varchar(20) DEFAULT "
               "NULL%s\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci",
}

VERSIONS = {"mysql": "8.4.11", "mariadb": "11.4.13-MariaDB-ubu2404-log"}
FACTS = {
    "mysql": {"foreign_key_checks": True, "innodb_page_size": 16384, "old_alter_table": False,
              "sql_mode": "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
                          "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION",
              "total_row_versions": 0, "fts_doc_id_index": False, "referenced_columns": []},
    "mariadb": {"foreign_key_checks": True, "innodb_page_size": 16384, "innodb_strict_mode": True,
                "sql_mode": "STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_AUTO_CREATE_USER,"
                            "NO_ENGINE_SUBSTITUTION",
                "innodb_instant_alter_column_allowed": "add_drop_reorder", "old_mode": "UTF8_IS_UTF8MB3",
                "innodb_file_per_table": True, "alter_algorithm": "DEFAULT", "fts_doc_id_index": False,
                "system_tablespace": False, "referenced_columns": [],
                "default_collations": {"utf8mb4": "utf8mb4_uca1400_ai_ci", "utf8mb3": "utf8mb3_uca1400_ai_ci",
                                       "latin1": "latin1_swedish_ci"}},
}
BOTH = ("mysql", "mariadb")


def table(engine, columns=(), keys=(), options=""):
    """The base table of the live recordings, with more columns, keys or
    table options, in the engine's SHOW CREATE TABLE spelling."""
    text = MYSQL_BASE if engine == "mysql" else MARIADB_BASE
    if columns:
        text = text.replace("  PRIMARY KEY (`id`),", "".join("  %s,\n" % c for c in columns) +
                            "  PRIMARY KEY (`id`),")
    if keys:
        text = text.replace("  KEY `kb` (`b`)\n", "  KEY `kb` (`b`),\n" + ",\n".join("  " + k for k in keys) + "\n")
    if options:
        text += " " + options
    return text


def small(engine, extra=""):
    return SMALL[engine] % extra


def predict(engine, clauses, ddl=None, facts=None, version=None, merge=True):
    all_facts = dict(FACTS[engine]) if merge else {}
    all_facts.update(facts or {})
    return aa.predict("ALTER TABLE t1 " + clauses, ddl or table(engine), engine, version or VERSIONS[engine],
                      all_facts)


def outcome(result):
    return result["algorithm"], result["rebuild"], result["concurrent_dml"]


INSTANT = ("INSTANT", False, True)
INPLACE = ("INPLACE", False, True)
INPLACE_LOCKED = ("INPLACE", False, False)
REBUILD = ("INPLACE", True, True)
REBUILD_LOCKED = ("INPLACE", True, False)
COPY = ("COPY", True, False)
COPY_ONLINE = ("COPY", True, True)  # MariaDB 11.2+ online ALGORITHM=COPY


class RuleTests(unittest.TestCase):
    def check(self, engine, clauses, expected, **kwargs):
        result = predict(engine, clauses, **kwargs)
        self.assertTrue(result["predicted"], "%s: %s" % (clauses, result["reason"]))
        self.assertEqual(outcome(result), expected, "%s %s: %s" % (engine, clauses, result["reason"]))
        self.assertTrue(result["reason"])
        self.assertTrue(result["source"])
        return result

    def test_add_column_is_instant_at_any_position(self):
        # M handler0alter.cc:157-165 INNOBASE_INSTANT_ALLOWED (ADD_STORED_BASE_COLUMN,
        # ALTER_STORED_COLUMN_ORDER since mysql-8.0.29), :829 innobase_support_instant.
        # Ma handler0alter.cc:1693 instant_alter_column_possible (add_drop_reorder).
        for engine in BOTH:
            for clause in ("ADD COLUMN x INT NULL", "ADD COLUMN x INT NULL FIRST", "ADD COLUMN x INT NULL AFTER a",
                           "ADD COLUMN x INT NOT NULL DEFAULT 7", "ADD COLUMN x VARCHAR(20) NOT NULL",
                           "ADD COLUMN x TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP",
                           "ADD COLUMN x INT NULL, ADD COLUMN y VARCHAR(10) NULL",
                           "ADD COLUMN (x INT, y INT)"):
                with self.subTest(engine=engine, clause=clause):
                    result = self.check(engine, clause, INSTANT)
                    self.assertIn("instant", result["reason"].lower())

    def test_mysql_instant_add_needs_innodb_facts(self):
        # A hidden FTS_DOC_ID (DICT_TF2_FTS_HAS_DOC_ID, dict0dict.ic:1248) or a used-up
        # row version budget (handler0alter.cc:1073-1083) is not in SHOW CREATE TABLE.
        result = predict("mysql", "ADD COLUMN x INT", facts={"total_row_versions": None, "fts_doc_id_index": None})
        self.assertFalse(result["predicted"])
        self.assertEqual(result["needs"], ["fts_doc_id_index", "total_row_versions"])
        self.assertIsNone(result["clauses"][0]["algorithm"])
        self.assertIn("PROCESS", result["reason"])

    def test_mysql_row_version_budget_by_version(self):
        # storage/innobase/include/rem0types.h:46 MAX_ROW_VERSION: 64 at mysql-8.4.0 and
        # mysql-9.0.0, 255 from mysql-9.1.0. When used up, INPLACE (handler0alter.cc:1073-1083).
        self.check("mysql", "ADD COLUMN x INT", INSTANT, facts={"total_row_versions": 63})
        result = self.check("mysql", "ADD COLUMN x INT", REBUILD, facts={"total_row_versions": 64})
        self.assertIn("row versions", result["reason"])
        self.check("mysql", "ADD COLUMN x INT", REBUILD, facts={"total_row_versions": 64}, version="9.0.1")
        self.check("mysql", "ADD COLUMN x INT", INSTANT, facts={"total_row_versions": 64}, version="9.1.0")
        self.check("mysql", "ADD COLUMN x INT", REBUILD, facts={"total_row_versions": 255}, version="9.7.0")

    def test_mysql_empty_table_note_by_version(self):
        # handler0alter.cc:1058-1063 "No records: prefer INPLACE" (mysql-8.4.4, mysql-9.2.0).
        note = "If the table has no rows"
        self.assertTrue(any(note in n for n in predict("mysql", "ADD COLUMN x INT")["notes"]))
        self.assertFalse(any(note in n for n in predict("mysql", "ADD COLUMN x INT", version="8.4.3")["notes"]))
        self.assertFalse(any(note in n for n in predict("mysql", "ADD COLUMN x INT", version="9.1.0")["notes"]))

    def test_add_column_expression_default(self):
        # M sql/sql_table.cc:13270-13281: a column with an expression DEFAULT is
        # ADD_STORED_GENERATED_COLUMN, which InnoDB cannot add in place (handler0alter.cc:1006).
        for clause in ("ADD COLUMN x INT DEFAULT (1 + 1)", "ADD COLUMN x INT DEFAULT (FLOOR(RAND() * 10))"):
            self.check("mysql", clause, COPY)
        # Ma handler0alter.cc:1999 is_non_const_value: constants, RAND (VCOL_SESSION_FUNC) and
        # NOW (VCOL_TIME_FUNC) are instant; a column reference or UUID() is not.
        self.check("mariadb", "ADD COLUMN x INT DEFAULT (1 + 1)", INSTANT)
        self.check("mariadb", "ADD COLUMN x INT DEFAULT (FLOOR(RAND() * 10))", INSTANT)
        self.check("mariadb", "ADD COLUMN x INT DEFAULT (n + 1)", COPY_ONLINE)
        self.check("mariadb", "ADD COLUMN x VARCHAR(36) DEFAULT (UUID())", COPY_ONLINE)
        unknown = predict("mariadb", "ADD COLUMN x INT DEFAULT (some_function(1))")
        self.assertFalse(unknown["predicted"])
        self.assertIn("constant", unknown["reason"])

    def test_generated_columns(self):
        # M handler0alter.cc:829 VIRTUAL_ADD_DROP_ONLY; sql_table.cc:13270 ADD_STORED_GENERATED_COLUMN.
        # Ma sql_table.cc:7336-7349; handler0alter.cc:148 INNOBASE_ALTER_INSTANT.
        for engine in BOTH:
            self.check(engine, "ADD COLUMN v INT AS (a + 1) VIRTUAL", INSTANT)
        self.check("mysql", "ADD COLUMN v INT AS (a + 1) STORED", COPY)
        self.check("mariadb", "ADD COLUMN v INT AS (a + 1) PERSISTENT", COPY_ONLINE)
        # An index on the new virtual column needs a shared lock (M :1258-1272, Ma :2542-2560).
        for engine in BOTH:
            self.check(engine, "ADD COLUMN v INT AS (a + 1) VIRTUAL, ADD INDEX kv (v)", INPLACE_LOCKED)
        # A new virtual column before an existing one changes their order (check_v_col_in_order).
        ddl_v = {e: table(e, ["`v` int GENERATED ALWAYS AS ((`n` + 1)) VIRTUAL" if e == "mysql" else
                               "`v` int(11) GENERATED ALWAYS AS (`n` + 1) VIRTUAL"]) for e in BOTH}
        self.check("mysql", "ADD COLUMN v2 INT AS (n + 2) VIRTUAL AFTER a", COPY, ddl=ddl_v["mysql"])
        self.check("mariadb", "ADD COLUMN v2 INT AS (n + 2) VIRTUAL AFTER a", COPY_ONLINE, ddl=ddl_v["mariadb"])
        # Ma sql_table.cc:7286-7291 compares a virtual column's position among all columns, so a
        # stored column added FIRST moves it; MySQL counts virtual columns among themselves.
        self.check("mysql", "ADD COLUMN x INT FIRST", INSTANT, ddl=ddl_v["mysql"])
        self.check("mariadb", "ADD COLUMN x INT FIRST", COPY_ONLINE, ddl=ddl_v["mariadb"])
        # Ma handler0alter.cc:2686-2717: ADD_STORED_BASE_COLUMN is not filtered (#if 0, MDEV-17468).
        self.check("mysql", "ADD COLUMN v INT AS (a + 1) VIRTUAL, ADD COLUMN x INT", INSTANT)
        self.check("mariadb", "ADD COLUMN v INT AS (a + 1) VIRTUAL, ADD COLUMN x INT", COPY_ONLINE)

    def test_column_and_index_together_rebuild(self):
        # M: ADD_INDEX is outside INNOBASE_INSTANT_ALLOWED, so ADD_STORED_BASE_COLUMN takes the
        # INNOBASE_ALTER_REBUILD path. Ma handler0alter.cc:1917-1940 disallows instant ADD with ADD INDEX.
        for engine in BOTH:
            result = self.check(engine, "ADD COLUMN x INT NULL, ADD INDEX kd (d)", REBUILD)
            self.assertTrue(any("Together these clauses" in n for n in result["notes"]))
            self.assertEqual([c["algorithm"] for c in result["clauses"]], ["INSTANT", "INPLACE"])
            result = self.check(engine, "ADD COLUMN x INT NULL, ADD INDEX kx (x)", REBUILD)
            self.assertIn("Together with ADD COLUMN x INT NULL", result["clauses"][1]["reason"])
        # Ma: an instant ADD with a DROP INDEX stays NOCOPY (supports_instant, :2849-2866).
        self.check("mysql", "ADD COLUMN x INT NULL, DROP INDEX ka", REBUILD)
        self.check("mariadb", "ADD COLUMN x INT NULL, DROP INDEX ka", INPLACE)

    def test_drop_column(self):
        # M INNOBASE_INSTANT_ALLOWED DROP_STORED_COLUMN (mysql-8.0.29+); Ma instant DROP (10.4+).
        for engine in BOTH:
            self.check(engine, "DROP COLUMN n", INSTANT)
            self.check(engine, "ADD COLUMN x INT NULL, DROP COLUMN n", INSTANT)
        # Dropping an indexed column also drops or changes the index (DROP_INDEX).
        self.check("mysql", "DROP COLUMN a", REBUILD)
        self.check("mariadb", "DROP COLUMN a", INPLACE)
        # Ma handler0alter.cc:1711-1721 (MDEV-17468): a table with an indexed virtual column.
        ddl = table("mariadb", ["`v` int(11) GENERATED ALWAYS AS (`n` + 1) VIRTUAL"], ["KEY `kv` (`v`)"])
        self.check("mariadb", "DROP COLUMN ts", COPY_ONLINE, ddl=ddl)
        ddl = table("mysql", ["`v` int GENERATED ALWAYS AS ((`n` + 1)) VIRTUAL"], ["KEY `kv` (`v`)"])
        self.check("mysql", "DROP COLUMN ts", INSTANT, ddl=ddl)

    def test_rename_column(self):
        # M handler0alter.cc:829 COLUMN_RENAME_ONLY; Ma INNOBASE_ALTER_INSTANT ALTER_COLUMN_NAME.
        for engine in BOTH:
            self.check(engine, "RENAME COLUMN n TO n2", INSTANT)
            self.check(engine, "CHANGE COLUMN n n2 INT NOT NULL DEFAULT 0", INSTANT)
        # M handler0alter.cc:784-817 ok_to_rename_column: referenced by another table's FK.
        result = self.check("mysql", "RENAME COLUMN id TO id2", INPLACE, facts={"referenced_columns": ["id"]})
        self.assertIn("foreign key", result["reason"])
        self.check("mariadb", "RENAME COLUMN id TO id2", INSTANT, facts={"referenced_columns": ["id"]})
        unknown = predict("mysql", "RENAME COLUMN id TO id2", facts={"referenced_columns": None})
        self.assertFalse(unknown["predicted"])
        self.assertEqual(unknown["needs"], ["referenced_columns"])

    def test_column_type_changes_copy(self):
        # M sql/field.cc:8685 Field_num::is_equal, :3349 decimal, :6151 Field_str, :7474 Field_blob;
        # Ma sql/field.cc:10010, :3899, :7622, :9444 and can_convert_nocopy (ha_innodb.cc:20941).
        for clause in ("MODIFY COLUMN n BIGINT NOT NULL DEFAULT 0", "MODIFY COLUMN n INT UNSIGNED NOT NULL DEFAULT 0",
                       "MODIFY COLUMN dm DECIMAL(12,2) NULL", "MODIFY COLUMN ch CHAR(8) NULL",
                       "MODIFY COLUMN tx MEDIUMTEXT NULL", "MODIFY COLUMN d TEXT NULL"):
            with self.subTest(clause=clause):
                result = self.check("mysql", clause, COPY)
                self.assertIn("column", result["reason"])
                self.check("mariadb", clause, COPY_ONLINE)

    def test_varchar_length_and_the_length_prefix(self):
        # M sql/field.cc:157 length_prevents_inplace: below 256 bytes to 256 or more is a type change;
        # otherwise ALTER_COLUMN_EQUAL_PACK_LENGTH (INNOBASE_ALTER_NOREBUILD).
        # utf8mb4: VARCHAR(60) = 240 bytes, (63) = 252, (64) = 256.
        result = self.check("mysql", "MODIFY COLUMN d VARCHAR(63) NULL", INPLACE)
        self.assertIn("240 to 252 bytes", result["reason"])
        result = self.check("mysql", "MODIFY COLUMN d VARCHAR(64) NULL", COPY)
        self.assertIn("240 to 256 bytes", result["reason"])
        self.check("mysql", "MODIFY COLUMN c VARCHAR(300) CHARACTER SET latin1 NULL", COPY)
        self.check("mysql", "MODIFY COLUMN b VARCHAR(60) NOT NULL DEFAULT ''", INPLACE)  # indexed
        # Ma ha_innodb.cc:20837 supports_enlarging: a column of at most 127 bytes may grow past 255
        # (its length always fits one byte); 128-255 bytes may not.
        self.check("mariadb", "MODIFY COLUMN d VARCHAR(63) NULL", INSTANT)
        self.check("mariadb", "MODIFY COLUMN d VARCHAR(64) NULL", COPY_ONLINE)
        self.check("mariadb", "MODIFY COLUMN c VARCHAR(300) CHARACTER SET latin1 NULL", INSTANT)
        self.check("mariadb", "MODIFY COLUMN b VARCHAR(60) NOT NULL DEFAULT ''", INSTANT)
        # A prefix index on the column keeps its length.
        self.check("mysql", "MODIFY COLUMN d VARCHAR(63) NULL", INPLACE, ddl=table("mysql", keys=["KEY `kp` (`d`(10))"]))
        self.check("mariadb", "MODIFY COLUMN d VARCHAR(63) NULL", INSTANT,
                   ddl=table("mariadb", keys=["KEY `kp` (`d`(10))"]))
        # Shrinking always copies.
        self.check("mysql", "MODIFY COLUMN d VARCHAR(30) NULL", COPY)
        self.check("mariadb", "MODIFY COLUMN d VARCHAR(30) NULL", COPY_ONLINE)

    def test_character_sets_and_collations(self):
        # M sql/field.cc:214 charset_prevents_inplace allows utf8mb3 to utf8mb4 only; a collation change
        # of an indexed column is ALTER_STORED_COLUMN_TYPE (sql_table.cc:13128-13148).
        self.check("mysql", "MODIFY COLUMN u VARCHAR(20) CHARACTER SET utf8mb4 NULL", INPLACE)
        self.check("mysql", "MODIFY COLUMN c VARCHAR(10) CHARACTER SET utf8mb4 NULL", COPY)
        self.check("mysql", "MODIFY COLUMN d VARCHAR(60) COLLATE utf8mb4_bin NULL", INPLACE)
        self.check("mysql", "MODIFY COLUMN b VARCHAR(50) COLLATE utf8mb4_bin NOT NULL DEFAULT ''", COPY)
        # Ma sql_type.cc:9775 encoding_allows_reinterpret_as; ha_innodb.cc:20963 compare_key_parts
        # rebuilds an index whose collation changes.
        self.check("mariadb", "MODIFY COLUMN u VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NULL",
                   INSTANT)
        self.check("mariadb", "MODIFY COLUMN c VARCHAR(10) CHARACTER SET utf8mb4 NULL", COPY_ONLINE)
        self.check("mariadb", "MODIFY COLUMN d VARCHAR(60) COLLATE utf8mb4_bin NULL", INSTANT)
        self.check("mariadb", "MODIFY COLUMN b VARCHAR(50) COLLATE utf8mb4_bin NOT NULL DEFAULT ''", INPLACE)
        # MariaDB picks the collation of a bare CHARACTER SET from character_set_collations.
        unknown = predict("mariadb", "MODIFY COLUMN u VARCHAR(20) CHARACTER SET utf8mb4 NULL",
                          facts={"default_collations": None})
        self.assertFalse(unknown["predicted"])
        self.assertEqual(unknown["needs"], ["default_collations"])

    def test_null_and_not_null(self):
        # M INNOBASE_ALTER_REBUILD ALTER_COLUMN_(NOT_)NULLABLE; handler0alter.cc:1110-1120 needs a
        # strict sql_mode for NOT NULL. Ma INNOBASE_DEFAULTS; :1945-1955 instant NULL only REDUNDANT.
        for engine in BOTH:
            self.check(engine, "MODIFY COLUMN a INT NOT NULL", REBUILD)
            self.check(engine, "MODIFY COLUMN n INT NULL DEFAULT 0", REBUILD)
        self.check("mysql", "MODIFY COLUMN a INT NOT NULL", COPY, facts={"sql_mode": "NO_ENGINE_SUBSTITUTION"})
        self.check("mysql", "MODIFY COLUMN n INT NULL DEFAULT 0", REBUILD,
                   ddl=table("mysql", options="ROW_FORMAT=REDUNDANT"))
        self.check("mariadb", "MODIFY COLUMN n INT NULL DEFAULT 0", INSTANT,
                   ddl=table("mariadb", options="ROW_FORMAT=REDUNDANT"))
        # NULL on a column of the clustering unique key (M :1132-1149, Ma :2376-2393).
        self.check("mysql", "MODIFY COLUMN k INT NULL", COPY, ddl=small("mysql", ",\n  UNIQUE KEY `uk` (`k`)"))
        self.check("mariadb", "MODIFY COLUMN k INT NULL", COPY_ONLINE,
                   ddl=small("mariadb", ",\n  UNIQUE KEY `uk` (`k`)"))

    def test_defaults_and_comments(self):
        # M INNOBASE_INPLACE_IGNORE ALTER_COLUMN_DEFAULT; Ma the same set (handler0alter.cc:109).
        for engine in BOTH:
            self.check(engine, "ALTER COLUMN n SET DEFAULT 5", INSTANT)
            self.check(engine, "ALTER COLUMN b DROP DEFAULT", INSTANT)
            self.check(engine, "MODIFY COLUMN n INT NOT NULL DEFAULT 0", INSTANT)
            self.check(engine, "MODIFY COLUMN n INT NOT NULL DEFAULT 0 COMMENT 'count'", INSTANT)

    def test_enum_and_set(self):
        # M sql/field.cc:8570 Field_enum::is_equal; Ma sql/field.cc:9941: members appended at the end
        # with the same storage size change only metadata.
        for engine in BOTH:
            self.check(engine, "MODIFY COLUMN e ENUM('x','y','z') NULL", INSTANT)
            self.check(engine, "MODIFY COLUMN s SET('p','q','r') NULL", INSTANT)
        self.check("mysql", "MODIFY COLUMN e ENUM('x','z','y') NULL", COPY)
        # SET of 9 members needs 2 bytes instead of 1 (sql/field.h get_set_pack_length).
        self.check("mysql", "MODIFY COLUMN s SET('p','q','m3','m4','m5','m6','m7','m8','m9') NULL", COPY)
        self.check("mariadb", "MODIFY COLUMN e ENUM('x','z','y') NULL", COPY_ONLINE)

    def test_indexes(self):
        # M INNOBASE_ONLINE_CREATE / INNOBASE_ALTER_NOREBUILD; RENAME_INDEX and index visibility are
        # not in INNOBASE_INSTANT_ALLOWED (sql_table.cc:13397 for RENAME INDEX, :13412-13424 for visibility).
        # Ma: ALTER_RENAME_INDEX and ALTER_INDEX_IGNORABILITY are instant.
        for engine in BOTH:
            self.check(engine, "ADD INDEX kd (d)", INPLACE)
            self.check(engine, "ADD UNIQUE INDEX ku (n, id)", INPLACE)
            self.check(engine, "ADD INDEX kt (tx(10))", INPLACE)
            self.check(engine, "DROP INDEX ka", INPLACE)
            self.check(engine, "DROP INDEX ka, ADD INDEX ka (a, n)", INPLACE)
        self.check("mysql", "RENAME INDEX ka TO ka2", INPLACE)
        self.check("mysql", "ALTER INDEX ka INVISIBLE", INPLACE)
        self.check("mariadb", "RENAME INDEX ka TO ka2", INSTANT)
        self.check("mariadb", "ALTER INDEX ka IGNORED", INSTANT)

    def test_fulltext(self):
        # M handler0alter.cc:1349-1366 FULLTEXT needs a lock; :5965-5980 the first adds FTS_DOC_ID
        # (rebuild); :4506-4509 refuses two at once. Ma :2766-2828.
        for engine in BOTH:
            result = self.check(engine, "ADD FULLTEXT INDEX ft (d)", REBUILD_LOCKED)
            self.assertIn("FTS_DOC_ID", result["reason"])
            unknown = predict(engine, "ADD FULLTEXT INDEX ft (d)", facts={"fts_doc_id_index": None})
            self.assertFalse(unknown["predicted"])
            self.assertIn("fts_doc_id_index", unknown["needs"])
            self.check(engine, "ADD FULLTEXT INDEX ft (d)", INPLACE_LOCKED, facts={"fts_doc_id_index": True})
            self.check(engine, "ADD FULLTEXT INDEX ft2 (b)", INPLACE_LOCKED,
                       ddl=table(engine, keys=["FULLTEXT KEY `ft` (`d`)"]))
        refused = predict("mysql", "ADD FULLTEXT INDEX ft1 (d), ADD FULLTEXT INDEX ft2 (b)")
        self.assertFalse(refused["predicted"])
        self.assertIn("refuses", refused["reason"])
        self.check("mariadb", "ADD FULLTEXT INDEX ft1 (d), ADD FULLTEXT INDEX ft2 (b)", COPY_ONLINE)
        # Rebuilding a table that keeps a FULLTEXT index: M copies (handler0alter.cc:1326-1347,
        # ER_INNODB_FT_LIMIT); Ma rebuilds with a lock while it has one FULLTEXT index (:2729-2760).
        for clause in ("ADD COLUMN x INT NULL", "FORCE"):
            self.check("mysql", clause, COPY, ddl=table("mysql", keys=["FULLTEXT KEY `ft` (`d`)"]))
            self.check("mariadb", clause, REBUILD_LOCKED, ddl=table("mariadb", keys=["FULLTEXT KEY `ft` (`d`)"]))
        # A hidden FTS_DOC_ID left by a dropped FULLTEXT index blocks instant ADD COLUMN on both
        # (live-checked on MySQL 8.4.11 and MariaDB 11.4.13).
        self.check("mysql", "ADD COLUMN x INT NULL", REBUILD, facts={"fts_doc_id_index": True})
        self.check("mariadb", "ADD COLUMN x INT NULL", REBUILD, facts={"fts_doc_id_index": True})

    def test_spatial(self):
        # M handler0alter.cc:1281-1284, :1326-1347; Ma :2729-2760, :2817-2826.
        point = {"mysql": "`g` point NOT NULL", "mariadb": "`g` point NOT NULL"}
        for engine in BOTH:
            self.check(engine, "ADD SPATIAL INDEX sg (g)", INPLACE_LOCKED, ddl=table(engine, [point[engine]]))
            with_index = table(engine, [point[engine]], ["SPATIAL KEY `sg` (`g`)"])
            self.check(engine, "ADD COLUMN x INT NULL", INSTANT, ddl=with_index)
            self.check(engine, "FORCE", REBUILD_LOCKED, ddl=with_index)

    def test_primary_keys(self):
        # M INNOBASE_ALTER_REBUILD ADD_PK_INDEX/DROP_PK_INDEX; :1122-1130 DROP alone copies.
        # Ma :88-103, :2367-2375. ADD PRIMARY KEY makes its columns NOT NULL (both servers).
        for engine in BOTH:
            self.check(engine, "ADD PRIMARY KEY (k)", REBUILD, ddl=small(engine))
            self.check(engine, "ADD PRIMARY KEY (v)", REBUILD, ddl=small(engine))
            self.check(engine, "ADD UNIQUE KEY uk (k)", REBUILD, ddl=small(engine))  # becomes the clustered key
            result = self.check(engine, "DROP PRIMARY KEY, ADD PRIMARY KEY (id, n)", REBUILD)
            self.assertIn("On its own this would be COPY", result["clauses"][0]["reason"])
            self.assertEqual(result["clauses"][0]["algorithm"], "INPLACE")
        self.check("mysql", "DROP PRIMARY KEY", COPY, ddl=small("mysql", ",\n  PRIMARY KEY (`k`)"))
        self.check("mariadb", "DROP PRIMARY KEY", COPY_ONLINE, ddl=small("mariadb", ",\n  PRIMARY KEY (`k`)"))
        # A promoted unique key is the clustered key: dropping it is DROP_PK_INDEX.
        self.check("mysql", "DROP INDEX uk", COPY, ddl=small("mysql", ",\n  UNIQUE KEY `uk` (`k`)"))

    def test_foreign_keys(self):
        # M handler0alter.cc:1023-1030 and Ma :2295-2302: in place only with foreign_key_checks=0.
        fk = "ADD CONSTRAINT fk1 FOREIGN KEY (a) REFERENCES p (id)"
        self.check("mysql", fk, COPY)
        self.check("mariadb", fk, COPY_ONLINE)
        assumed = predict("mysql", fk, facts={"foreign_key_checks": None})
        self.assertTrue(any("foreign_key_checks=ON" in n for n in assumed["notes"]))
        self.check("mysql", fk, INPLACE, facts={"foreign_key_checks": False})
        self.check("mariadb", fk, INSTANT, facts={"foreign_key_checks": False})
        # Without an index on the columns the server adds one (ADD_INDEX).
        new_index = "ADD CONSTRAINT fk1 FOREIGN KEY (n) REFERENCES p (id)"
        for engine in BOTH:
            self.check(engine, new_index, INPLACE, facts={"foreign_key_checks": False})
        child = {e: table(e, keys=["CONSTRAINT `fk1` FOREIGN KEY (`a`) REFERENCES `p` (`id`)"]) for e in BOTH}
        self.check("mysql", "DROP FOREIGN KEY fk1", INPLACE, ddl=child["mysql"])
        self.check("mariadb", "DROP FOREIGN KEY fk1", INSTANT, ddl=child["mariadb"])
        # The server refuses to drop the index a foreign key needs (ER_DROP_INDEX_FK).
        refused = predict("mysql", "DROP INDEX ka", ddl=child["mysql"])
        self.assertFalse(refused["predicted"])
        # MariaDB's online copy is off for a child table with ON DELETE CASCADE (sql_table.cc:10680-10697).
        cascade = table("mariadb", keys=["CONSTRAINT `fk1` FOREIGN KEY (`a`) REFERENCES `p` (`id`) ON DELETE CASCADE"])
        self.check("mariadb", "MODIFY COLUMN n BIGINT NOT NULL DEFAULT 0", COPY, ddl=cascade)

    def test_check_constraints(self):
        # M sql_table.cc:20736 is_any_check_constraints_evaluation_required; Ma
        # ALTER_ADD_CHECK_CONSTRAINT is not an in-place flag (handler0alter.cc:2275-2290).
        self.check("mysql", "ADD CONSTRAINT ck CHECK (n >= 0)", COPY)
        self.check("mysql", "ADD CONSTRAINT ck CHECK (n >= 0) NOT ENFORCED", INSTANT)
        self.check("mysql", "ADD COLUMN x INT CHECK (x > 0)", COPY)
        self.check("mariadb", "ADD CONSTRAINT ck CHECK (n >= 0)", COPY_ONLINE)
        self.check("mysql", "DROP CHECK ck", INSTANT, ddl=table("mysql", keys=["CONSTRAINT `ck` CHECK ((`n` >= 0))"]))
        self.check("mariadb", "DROP CONSTRAINT ck", INSTANT,
                   ddl=table("mariadb", keys=["CONSTRAINT `ck` CHECK (`n` >= 0)"]))
        self.check("mysql", "ALTER COLUMN n SET INVISIBLE", INSTANT)

    def test_table_options(self):
        # M innobase_need_rebuild (handler0alter.cc:930-950): CHANGE_CREATE_OPTION alone rebuilds
        # only for ROW_FORMAT, KEY_BLOCK_SIZE and TABLESPACE, and is not instant. Ma
        # alter_options_need_rebuild (:1541) and :2339-2353: instant.
        for clause in ("COMMENT = 'x'", "AUTO_INCREMENT = 1000", "DEFAULT CHARSET = latin1"):
            self.check("mysql", clause, INPLACE)
            self.check("mariadb", clause, INSTANT)
        unknown = predict("mariadb", "COMMENT = 'x'", facts={"system_tablespace": None})
        self.assertEqual(unknown["needs"], ["system_tablespace"])
        self.check("mariadb", "COMMENT = 'x'", REBUILD, facts={"system_tablespace": True})
        for engine in BOTH:
            for clause in ("ROW_FORMAT = DYNAMIC", "ROW_FORMAT = COMPACT", "ROW_FORMAT = COMPRESSED KEY_BLOCK_SIZE = 8",
                           "ENGINE = InnoDB", "FORCE"):
                self.check(engine, clause, REBUILD)
        self.check("mysql", "ENGINE = MyISAM", COPY)
        self.check("mariadb", "ENGINE = MyISAM", COPY_ONLINE)
        # Combined with another change, a table option rebuilds on MySQL only.
        result = self.check("mysql", "ADD INDEX kd (d), COMMENT = 'x'", REBUILD)
        self.assertIn("COMMENT", result["reason"])
        self.check("mariadb", "ADD INDEX kd (d), COMMENT = 'x'", INPLACE)
        self.check("mysql", "ADD COLUMN x INT NULL, COMMENT = 'x'", REBUILD)
        self.check("mariadb", "ADD COLUMN x INT NULL, COMMENT = 'x'", INSTANT)

    def test_convert_to_character_set(self):
        # Every character column takes the set: latin1 to utf8mb4 is a type change (COPY).
        self.check("mysql", "CONVERT TO CHARACTER SET utf8mb4", COPY)
        self.check("mariadb", "CONVERT TO CHARACTER SET utf8mb4", COPY_ONLINE)
        mb3 = {"mysql": "CREATE TABLE `t1` (\n  `id` int NOT NULL,\n  `a` varchar(20) DEFAULT NULL,\n  `b` varchar(100) "
                        "DEFAULT NULL,\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3",
               "mariadb": "CREATE TABLE `t1` (\n  `id` int(11) NOT NULL,\n  `a` varchar(20) DEFAULT NULL,\n  `b` "
                          "varchar(100) DEFAULT NULL,\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3 "
                          "COLLATE=utf8mb3_general_ci"}
        # M: CONVERT TO is ALTER_OPTIONS (sql/parse_tree_nodes.h:4648), so with column changes it
        # rebuilds. Ma: it sets only ALTER_CONVERT_TO (sql/sql_lex.h:4720), which InnoDB ignores.
        self.check("mysql", "CONVERT TO CHARACTER SET utf8mb4", REBUILD, ddl=mb3["mysql"])
        self.check("mariadb", "CONVERT TO CHARACTER SET utf8mb4", INSTANT, ddl=mb3["mariadb"])

    def test_rename_table(self):
        # M sql_table.cc:16759 is_simple_rename_or_index_change; Ma sql_table.cc:11330-11355.
        for engine in BOTH:
            self.check(engine, "RENAME TO t2", INSTANT)
            self.check(engine, "RENAME TO t2, ADD COLUMN x INT NULL", INSTANT)
            refused = predict(engine, "RENAME TO t2, LOCK=NONE")
            self.assertFalse(refused["predicted"])
            self.assertIn("LOCK=EXCLUSIVE", refused["reason"])

    def test_compressed_row_format_is_not_instant(self):
        # M dict0dict.ic:1248 support_instant_add_drop (DICT_TF_GET_ZIP_SSIZE); Ma supports_instant.
        for engine in BOTH:
            result = self.check(engine, "ADD COLUMN x INT NULL", REBUILD,
                                ddl=table(engine, options="ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8"))
            self.assertIn("COMPRESSED", result["reason"])

    def test_order_by_copies(self):
        # M sql_table.cc:13855 is_inplace_alter_impossible; Ma :7893 and online copy needs no ORDER BY.
        for engine in BOTH:
            self.check(engine, "ORDER BY n", COPY)

    def test_explicit_algorithm_and_lock(self):
        # M sql_table.cc:17360-17367, :18454-18530; Ma sql/sql_alter.cc:139 supports_algorithm, :183 supports_lock.
        self.check("mysql", "ADD COLUMN x INT, ALGORITHM=INPLACE", REBUILD)  # handler0alter.cc:1053-1056
        self.check("mariadb", "ADD COLUMN x INT, ALGORITHM=INPLACE", INSTANT)
        self.check("mysql", "ALGORITHM=INSTANT, ADD COLUMN x INT", INSTANT)
        for engine in BOTH:
            for clause in ("ADD INDEX kd (d), ALGORITHM=INSTANT", "MODIFY n BIGINT NOT NULL, ALGORITHM=INPLACE",
                           "MODIFY n BIGINT NOT NULL, LOCK=NONE" if engine == "mysql" else
                           "ADD FULLTEXT INDEX ft (d), ALGORITHM=INPLACE, LOCK=NONE"):
                with self.subTest(engine=engine, clause=clause):
                    refused = predict(engine, clause)
                    self.assertFalse(refused["predicted"])
                    self.assertIn("refuses", refused["reason"])
            self.check(engine, "ADD INDEX kd (d), LOCK=SHARED", INPLACE_LOCKED)
            self.check(engine, "ADD INDEX kd (d), ALGORITHM=COPY", COPY if engine == "mysql" else COPY_ONLINE)
        refused = predict("mysql", "ADD COLUMN x INT, ALGORITHM=INSTANT, LOCK=NONE")
        self.assertIn("refuses", refused["reason"])
        # Ma: LOCK=NONE alone turns a locking in-place change into an online copy (supports_lock).
        result = self.check("mariadb", "ADD FULLTEXT INDEX ft (d), LOCK=NONE", COPY_ONLINE)
        self.assertIn("LOCK=NONE", result["reason"])
        refused = predict("mariadb", "ADD FULLTEXT INDEX ft (d), LOCK=NONE", version="10.11.16-MariaDB")
        self.assertIn("refuses", refused["reason"])
        result = predict("mysql", "ADD COLUMN x INT, ALGORITHM=INPLACE")
        self.assertTrue(any("ALGORITHM=INPLACE" in n for n in result["notes"]))

    def test_mariadb_online_copy_by_version_and_setting(self):
        # Ma sql_table.cc:10650 online_alter_check_supported (absent at mariadb-11.1.6, present
        # at mariadb-11.2.1); :10905-10911 old_mode LOCK_ALTER_TABLE_COPY.
        clause = "MODIFY COLUMN n BIGINT NOT NULL DEFAULT 0"
        self.check("mariadb", clause, COPY_ONLINE)
        self.check("mariadb", clause, COPY, version="10.11.16-MariaDB")
        self.check("mariadb", clause, COPY_ONLINE, version="11.2.1-MariaDB")
        self.check("mariadb", clause, COPY, facts={"old_mode": "UTF8_IS_UTF8MB3,LOCK_ALTER_TABLE_COPY"})
        self.check("mariadb", "ADD COLUMN x INT NOT NULL AUTO_INCREMENT, ADD UNIQUE KEY ux (x)", REBUILD_LOCKED,
                   ddl=small("mariadb", ",\n  PRIMARY KEY (`k`)"))
        text = predict("mariadb", clause)["text"]
        self.assertIn("concurrent writes continue", text)
        self.assertIn("writes wait", predict("mysql", clause)["text"])

    def test_mariadb_alter_algorithm_session_variable(self):
        # Ma sql/sql_alter.cc:257: mariadb-11.4.10 reads alter_algorithm; mariadb-11.5.0 ignores it.
        # ALGORITHM=COPY from the session keeps the online copy (sql_table.cc:10905-10911).
        self.check("mariadb", "ADD INDEX kd (d)", COPY_ONLINE, facts={"alter_algorithm": "COPY"})
        self.check("mariadb", "ADD INDEX kd (d)", INPLACE, facts={"alter_algorithm": "COPY"}, version="11.8.6-MariaDB")

    def test_mysql_old_alter_table(self):
        # M sql_table.cc:17998-18020.
        self.check("mysql", "ADD INDEX kd (d)", COPY, facts={"old_alter_table": True})
        self.check("mysql", "ADD INDEX kd (d), ALGORITHM=INPLACE", INPLACE, facts={"old_alter_table": True})

    def test_wide_rows_are_not_guessed(self):
        # dict0inst.cc:42 is_instant_add_drop_possible: the row size limit (dict0dict.cc:2077).
        columns = ["`w%d` char(60) DEFAULT NULL" % i for i in range(30)]
        result = predict("mysql", "ADD COLUMN x INT", ddl=table("mysql", columns))
        self.assertFalse(result["predicted"])
        self.assertIn("record limit", result["reason"])

    def test_date_not_null_without_default(self):
        # M sql_table.cc:15840-15862, :14520-14560; Ma sql_table.cc:9018-9031 (NO_ZERO_DATE).
        for engine in BOTH:
            result = predict(engine, "ADD COLUMN x DATE NOT NULL",
                             facts={"sql_mode": "STRICT_TRANS_TABLES,NO_ZERO_DATE"})
            self.assertFalse(result["predicted"])
        self.check("mariadb", "ADD COLUMN x DATE NOT NULL", INSTANT)  # MariaDB's default sql_mode allows zero dates


class ReviewFindingTests(unittest.TestCase):
    """Rules from the source review of this module (not covered by the first
    live run)."""
    check = RuleTests.check

    def test_mariadb_nextval_and_autoinc_turn_online_copy_off(self):
        # Ma sql_table.cc:10706-10712 (VCOL_NEXTVAL), :10592 online_alter_check_autoinc.
        self.check("mariadb", "ADD COLUMN x INT DEFAULT (NEXTVAL(s1))", COPY)
        ddl = ("CREATE TABLE `t1` (\n  `id` int(11) NOT NULL,\n  `k` int(11) NOT NULL,\n  `v` int(11) DEFAULT NULL,\n"
               "  PRIMARY KEY (`id`),\n  KEY `kk` (`k`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
               "COLLATE=utf8mb4_uca1400_ai_ci")
        self.check("mariadb", "MODIFY COLUMN k INT NOT NULL AUTO_INCREMENT", COPY_ONLINE, ddl=ddl)
        no_key = ddl.replace("  PRIMARY KEY (`id`),\n", "")
        self.check("mariadb", "MODIFY COLUMN k INT NOT NULL AUTO_INCREMENT", COPY, ddl=no_key)
        self.check("mariadb", "MODIFY COLUMN k INT NOT NULL AUTO_INCREMENT", COPY_ONLINE, ddl=no_key,
                   facts={"sql_mode": "STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO"})
        self.check("mysql", "MODIFY COLUMN k INT NOT NULL AUTO_INCREMENT", COPY,
                   ddl=ddl.replace("int(11)", "int").replace("utf8mb4_uca1400_ai_ci", "utf8mb4_0900_ai_ci"))

    def test_mariadb_time_functions_without_parentheses(self):
        # Ma sql/item_timefunc.h:935, :970 (VCOL_TIME_FUNC) are constant for is_non_const_value.
        self.check("mariadb", "ADD COLUMN x DATETIME DEFAULT (CURRENT_TIMESTAMP + INTERVAL 1 DAY)", INSTANT)
        self.check("mariadb", "ADD COLUMN x DATETIME DEFAULT (UTC_TIMESTAMP)", COPY_ONLINE)

    def test_mariadb_timestamp_not_null_copies(self):
        # Ma handler0alter.cc:2609-2648: is_real_null() is never true of a NOT NULL field.
        ddl = table("mariadb", ["`t2` timestamp NULL DEFAULT NULL"])
        for clause in ("MODIFY COLUMN t2 TIMESTAMP NOT NULL",
                       "MODIFY COLUMN t2 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"):
            self.check("mariadb", clause, COPY_ONLINE, ddl=ddl)
        self.check("mysql", "MODIFY COLUMN t2 TIMESTAMP NOT NULL", REBUILD,
                   ddl=table("mysql", ["`t2` timestamp NULL DEFAULT NULL"]))

    def test_mariadb_session_copy_applies_to_rename(self):
        # mariadb-11.4.10 sql/sql_table.cc:11127-11130 tests algorithm(thd) for a plain rename.
        self.check("mariadb", "RENAME TO t2", COPY_ONLINE, facts={"alter_algorithm": "COPY"})
        self.check("mariadb", "RENAME TO t2", INSTANT, facts={"alter_algorithm": "COPY"}, version="11.5.2-MariaDB")

    def test_fts_doc_id_column_is_kept(self):
        # M handler0alter.cc:1286-1316, Ma :2571-2604: renaming or dropping FTS_DOC_ID copies.
        ddl = {
            "mysql": "CREATE TABLE `t1` (\n  `k` int NOT NULL,\n  `d` varchar(60) DEFAULT NULL,\n  `FTS_DOC_ID` bigint "
                     "unsigned NOT NULL AUTO_INCREMENT,\n  PRIMARY KEY (`k`),\n  UNIQUE KEY `FTS_DOC_ID_INDEX` "
                     "(`FTS_DOC_ID`),\n  FULLTEXT KEY `ft` (`d`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
                     "COLLATE=utf8mb4_0900_ai_ci",
            "mariadb": "CREATE TABLE `t1` (\n  `k` int(11) NOT NULL,\n  `d` varchar(60) DEFAULT NULL,\n  `FTS_DOC_ID` "
                       "bigint(20) unsigned NOT NULL AUTO_INCREMENT,\n  PRIMARY KEY (`k`),\n  UNIQUE KEY "
                       "`FTS_DOC_ID_INDEX` (`FTS_DOC_ID`),\n  FULLTEXT KEY `ft` (`d`)\n) ENGINE=InnoDB DEFAULT "
                       "CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci"}
        for engine in BOTH:
            result = self.check(engine, "RENAME COLUMN FTS_DOC_ID TO doc_id", COPY if engine == "mysql" else COPY_ONLINE,
                                ddl=ddl[engine])
            self.assertIn("FTS_DOC_ID", result["reason"])
            # A visible FTS_DOC_ID is not DICT_TF2_FTS_HAS_DOC_ID: a virtual column with an index copies
            # (M handler0alter.cc:1196-1203, Ma :2475-2484).
            self.check(engine, "ADD COLUMN v INT AS (k + 1) VIRTUAL, ADD INDEX kv (v)",
                       COPY if engine == "mysql" else COPY_ONLINE, ddl=ddl[engine])
        # With the hidden FTS_DOC_ID an existing FULLTEXT index brings, it stays in place.
        hidden = table("mysql", keys=["FULLTEXT KEY `ft` (`d`)"])
        self.check("mysql", "ADD COLUMN v INT AS (a + 1) VIRTUAL, ADD INDEX kv (v)", INPLACE_LOCKED, ddl=hidden)


class GateTests(unittest.TestCase):
    def test_versions(self):
        # MySQL 5.7 is named with why it is not predicted (no INSTANT there).
        result = predict("mysql", "ADD COLUMN x INT", version="5.7.44-log")
        self.assertFalse(result["predicted"])
        self.assertIsNone(result["algorithm"])
        self.assertEqual(result["reason"], "MySQL 5.7.44 is not predicted: it has no ALGORITHM=INSTANT (MySQL 8.0.12 "
                                           "added it), and the rules were checked for MySQL 8.4 and 9.x.")
        for engine, version in (("mysql", "8.0.36"), ("mariadb", "10.6.20-MariaDB"),
                                ("mariadb", "10.5.0")):
            result = predict(engine, "ADD COLUMN x INT", version=version)
            self.assertFalse(result["predicted"])
            self.assertIn("older servers: not predicted", result["reason"].lower())
        for engine, version in (("mysql", "26.7.0"), ("mariadb", "13.1.0-MariaDB")):
            result = predict(engine, "ADD COLUMN x INT", version=version)
            self.assertFalse(result["predicted"])
            self.assertIn("newer", result["reason"])
        self.assertTrue(predict("mysql", "ADD COLUMN x INT", version="9.7.0")["predicted"])
        self.assertTrue(predict("mariadb", "ADD COLUMN x INT", version="5.5.5-10.11.16-MariaDB")["predicted"])
        self.assertTrue(aa.predict("ALTER TABLE t1 ADD COLUMN x INT", MYSQL_BASE, "percona", "8.4.5-5",
                                   FACTS["mysql"])["predicted"])
        self.assertFalse(aa.predict("ALTER TABLE t1 ADD COLUMN x INT", MYSQL_BASE, "unknown", "8.4.5",
                                    FACTS["mysql"])["predicted"])

    def test_defaults_are_stated(self):
        result = predict("mysql", "ADD INDEX kd (d)", merge=False)
        self.assertTrue(result["predicted"])
        self.assertTrue(any(n.startswith("Assumes the server defaults") for n in result["notes"]))
        result = predict("mysql", "ADD INDEX kd (d)")
        self.assertFalse(any(n.startswith("Assumes") for n in result["notes"]))

    def test_outside_the_rules(self):
        cases = [
            ("mysql", "ADD COLUMN x INT", table("mysql").replace("ENGINE=InnoDB", "ENGINE=MyISAM"), "InnoDB"),
            ("mysql", "ADD COLUMN x INT", table("mysql") + "\n/*!50100 PARTITION BY HASH (`id`)\nPARTITIONS 4 */",
             "Partitioned"),
            ("mariadb", "ADD COLUMN x INT", table("mariadb") + " WITH SYSTEM VERSIONING", "System-versioned"),
            ("mysql", "ADD PARTITION (PARTITION p1 VALUES LESS THAN (10))", None, "PARTITION"),
            ("mysql", "DISCARD TABLESPACE", None, "tablespace"),
            ("mysql", "FROBNICATE", None, "outside"),
            ("mysql", "ADD COLUMN x INT REFERENCES p (id)", None, "REFERENCES"),
            ("mysql", "MODIFY COLUMN v INT AS (a + 2) VIRTUAL",
             table("mysql", ["`v` int GENERATED ALWAYS AS ((`a` + 1)) VIRTUAL"]), "generated"),
            ("mysql", "RENAME COLUMN a TO a2", table("mysql", ["`v` int GENERATED ALWAYS AS ((`a` + 1)) VIRTUAL"]),
             "renamed or dropped"),
            ("mysql", "DROP COLUMN zz", None, "zz"),
            ("mysql", "ADD COLUMN x VECTOR(3)", None, "VECTOR"),
        ]
        for engine, clause, ddl, word in cases:
            with self.subTest(clause=clause):
                result = predict(engine, clause, ddl=ddl)
                self.assertFalse(result["predicted"])
                self.assertIsNone(result["algorithm"])
                self.assertIn(word.lower(), (result["reason"] + " ".join(c["reason"] for c in result["clauses"]))
                              .lower())
        other = aa.predict("ALTER TABLE other ADD COLUMN x INT", MYSQL_BASE, "mysql", "8.4.11", FACTS["mysql"])
        self.assertIn("not t1", other["reason"])
        ignore = aa.predict("ALTER IGNORE TABLE t1 ADD UNIQUE KEY u (n)", MYSQL_BASE, "mysql", "8.4.11", FACTS["mysql"])
        self.assertFalse(ignore["predicted"])
        self.assertFalse(aa.predict("SELECT 1", MYSQL_BASE, "mysql", "8.4.11")["predicted"])
        self.assertFalse(aa.predict("ALTER TABLE t1 ADD COLUMN x INT", "not a table", "mysql", "8.4.11")["predicted"])
        self.assertFalse(aa.predict(None, None, None, None)["predicted"])

    def test_an_unknown_clause_leaves_the_others_predicted(self):
        result = predict("mysql", "ADD INDEX kd (d), FROBNICATE")
        self.assertFalse(result["predicted"])
        self.assertEqual(result["clauses"][0]["algorithm"], "INPLACE")
        self.assertIsNone(result["clauses"][1]["algorithm"])


class ParserTests(unittest.TestCase):
    def test_comments_quotes_and_order(self):
        sql = ("ALTER /* hint */ TABLE `t1` -- trailing\n ALGORITHM = DEFAULT, ADD COLUMN `x y` INT /*!80023 INVISIBLE */"
               " COMMENT 'a, b (c)', ADD INDEX `k,1` (`x y`) ; ")
        result = aa.predict(sql, MYSQL_BASE, "mysql", "8.4.11", FACTS["mysql"])
        self.assertTrue(result["predicted"], result["reason"])
        self.assertEqual([c["text"] for c in result["clauses"]],
                         ["ADD COLUMN `x y` INT /*!80023 INVISIBLE */ COMMENT 'a, b (c)'", "ADD INDEX `k,1` (`x y`)"])
        self.assertEqual(result["algorithm"], "INPLACE")

    def test_qualified_and_case(self):
        result = aa.predict("alter table db.T1 add column x int", MYSQL_BASE, "mysql", "8.4.11", FACTS["mysql"])
        self.assertTrue(result["predicted"])

    def test_mariadb_show_create_spellings(self):
        ddl = ("CREATE TABLE `t1` (\n  `id` int(11) NOT NULL,\n  `j` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin "
               "DEFAULT NULL CHECK (json_valid(`j`)),\n  `t` timestamp NOT NULL DEFAULT current_timestamp() ON UPDATE "
               "current_timestamp(),\n  `z` varchar(10) /*M!100301 COMPRESSED*/ DEFAULT NULL,\n  PRIMARY KEY (`id`)\n"
               ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci")
        result = aa.predict("ALTER TABLE t1 ADD COLUMN x INT", ddl, "mariadb", "11.4.13-MariaDB", FACTS["mariadb"])
        self.assertFalse(result["predicted"])  # compressed columns are outside the rules
        result = aa.predict("ALTER TABLE t1 ADD COLUMN x INT", ddl.replace(" /*M!100301 COMPRESSED*/", ""), "mariadb",
                            "11.4.13-MariaDB", FACTS["mariadb"])
        self.assertEqual(outcome(result), INSTANT)


class TextTests(unittest.TestCase):
    def test_outcome_sentences(self):
        self.assertEqual(aa.outcome_text("INSTANT", False, True), "Changes only table metadata; no rows are copied.")
        self.assertEqual(aa.outcome_text("INPLACE", True, True), "Rebuilds the table in place; concurrent writes continue.")
        self.assertIn("without rebuilding", aa.outcome_text("INPLACE", False, True))
        self.assertIn("writes wait until it finishes", aa.outcome_text("INPLACE", True, False))
        self.assertEqual(aa.outcome_text("COPY", True, False),
                         "Copies the table; writes wait until it finishes (reads continue).")
        self.assertIn("applied before", aa.outcome_text("COPY", True, True, online_copy=True))
        self.assertEqual(aa.outcome_text(None, None, None), "Not predicted.")

    def test_every_rule_has_reason_and_source(self):
        for name, (reason, source) in aa.RULES.items():
            self.assertTrue(reason.endswith("."), name)
            if not name.startswith("unknown."):
                self.assertIn("-server ", source, name)

    def test_summary_and_clause_fields(self):
        result = predict("mysql", "ADD INDEX kd (d)")
        self.assertEqual(result["summary"], "INPLACE, no rebuild")
        clause = result["clauses"][0]
        self.assertEqual(set(clause), {"text", "algorithm", "rebuild", "concurrent_dml", "summary", "outcome",
                                       "reason", "source"})
        self.assertEqual(predict("mysql", "FORCE")["summary"], "INPLACE, rebuilds the table")
        self.assertEqual(predict("mysql", "ORDER BY n")["summary"], "COPY, copies the table")
        self.assertEqual(predict("mysql", "DROP COLUMN n")["summary"], "INSTANT, metadata only")


def _facts_for(data, case):
    """The facts the editor would read, from the recording."""
    settings = data.get("settings") or {}
    facts = {"sql_mode": data["sql_mode"], "innodb_page_size": data["innodb_page_size"],
             "foreign_key_checks": not any("foreign_key_checks = 0" in s for s in case["session"]),
             "referenced_columns": case.get("referenced_columns", []), "fts_doc_id_index": False,
             "default_collations": settings.get("default_collations")}
    if data["engine"] == "mysql":
        facts["old_alter_table"] = settings.get("old_alter_table") in ("1", "ON")
        versions = case["attempts"]["default"].get("row_versions") or case["attempts"]["instant"].get("row_versions")
        facts["total_row_versions"] = versions[0] if versions else 0
    else:
        for name in ("innodb_instant_alter_column_allowed", "alter_algorithm", "old_mode"):
            facts[name] = settings.get(name)
        for statement in case["session"]:
            if "alter_algorithm" in statement:
                facts["alter_algorithm"] = statement.split("=", 1)[1].strip().strip("'")
        facts["innodb_strict_mode"] = settings.get("innodb_strict_mode") in ("1", "ON")
        facts["innodb_file_per_table"] = settings.get("innodb_file_per_table", "1") in ("1", "ON")
        space = case["attempts"]["default"].get("space_before")
        facts["system_tablespace"] = space == 0 if space is not None else False
    return facts


class LiveFixtureTests(unittest.TestCase):
    """Every case run on the lab servers: what the predictor says against
    what the server accepted (scripts/record-alter-algorithm-fixtures.py)."""

    def load(self):
        out = []
        for name in sorted(os.listdir(FIXTURES)):
            if name.startswith("live-") and name.endswith(".json"):
                with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
                    out.append(json.load(handle))
        self.assertEqual({d["engine"] for d in out}, {"mysql", "mariadb"})
        return out

    def test_statements_match_the_server(self):
        for data in self.load():
            self.assertGreater(len(data["cases"]), 80)
            for case in data["cases"]:
                with self.subTest(server=data["server"], case=case["id"]):
                    result = aa.predict(case["alter"], case["create_table"], data["engine"], data["version"],
                                        _facts_for(data, case))
                    observed = case["observed"]
                    if observed["algorithm"] is None:
                        self.assertFalse(result["predicted"], result["reason"])
                    else:
                        self.assertTrue(result["predicted"], result["reason"])
                        self.assertEqual(
                            outcome(result),
                            (observed["algorithm"], observed["rebuild"], observed["concurrent_dml"]),
                            result["reason"])

    def test_explicit_requests_match_the_server(self):
        for data in self.load():
            for case in data["cases"]:
                if case["observed"]["algorithm"] is None:
                    continue
                for name, attempt in sorted(case["attempts"].items()):
                    if name == "default":
                        continue
                    with self.subTest(server=data["server"], case=case["id"], attempt=name):
                        result = aa.predict(attempt["sql"], case["create_table"], data["engine"], data["version"],
                                            _facts_for(data, case))
                        if not attempt["ok"]:
                            self.assertFalse(result["predicted"], attempt["message"])
                            self.assertIn("refuses", result["reason"])
                        elif name == "instant":
                            self.assertEqual(result["algorithm"], "INSTANT", result["reason"])
                        elif name.startswith("inplace"):
                            self.assertIn(result["algorithm"], ("INSTANT", "INPLACE"), result["reason"])
                            self.assertEqual(bool(result["rebuild"]), bool(attempt["rebuilt"]), result["reason"])
                        else:
                            self.assertEqual(result["algorithm"], "COPY", result["reason"])


class _FakeStep(object):
    def __init__(self, rows=None, error=None):
        self.marker = True
        self.entries = [{"type": "rows", "rows": rows or []}]
        self.errors = [error] if error else []

    def real_errors(self):
        return list(self.errors)


class _FakeSession(object):
    """Just enough of EditorSession for _alter_review: answers helper
    statements from a table of (prefix, rows) pairs."""

    def __init__(self, engine, version, answers):
        self.engine, self.server_version, self.answers = engine, version, answers
        self.run_lock = threading.Lock()
        self.ended = False
        self.sent = []

    def _helper(self, sql):
        self.sent.append(sql)
        body = sql
        while body.startswith("/*myflames:"):
            body = body[body.index("*/") + 2:].lstrip()
        for prefix, rows in self.answers:
            if body.startswith(prefix):
                if isinstance(rows, Exception):
                    return _FakeStep(error={"code": 1227, "message": str(rows)})
                return _FakeStep(rows)
        return _FakeStep([])

    def _helper_problem(self, step):
        return step.errors[0]["message"] if step.errors else None


class EditorReviewTests(unittest.TestCase):
    """The ALTER review payload carries the prediction (editor_session)."""

    def review(self, engine, sql, answers, version=None):
        session = _FakeSession(engine, version or VERSIONS[engine], answers)
        plan = ed._plan_run({"sql": sql}, None)
        return ed._alter_review(plan, session), session

    def mysql_answers(self, innodb=((0, 0),), refs=()):
        return [
            ("SELECT DATABASE(), @@performance_schema", [["app", "1"]]),
            ("SELECT ENGINE, DATA_LENGTH, INDEX_LENGTH", [["InnoDB", "16384", "32768"]]),
            ("SHOW CREATE TABLE `app`.`t1`", [["t1", MYSQL_BASE]]),
            ("SELECT @@foreign_key_checks", [["1", FACTS["mysql"]["sql_mode"], "16384", "0"]]),
            ("SELECT t.TOTAL_ROW_VERSIONS", [list(r) for r in innodb]),
            ("SELECT DISTINCT REFERENCED_COLUMN_NAME", [[r] for r in refs]),
        ]

    def test_prediction_in_review_payload(self):
        review, session = self.review("mysql", "ALTER TABLE t1 ADD INDEX kd (d)", self.mysql_answers())
        prediction = review["prediction"]
        self.assertTrue(prediction["predicted"], prediction["reason"])
        self.assertEqual(prediction["summary"], "INPLACE, no rebuild")
        self.assertEqual(prediction["clauses"][0]["text"], "ADD INDEX kd (d)")
        self.assertIn(ed.TAG + "SHOW CREATE TABLE `app`.`t1`", session.sent)
        # Only what the prediction needs is read: no InnoDB dictionary row for an index.
        self.assertFalse(any("INNODB_TABLES" in s for s in session.sent))
        # The statement itself is never changed and nothing but reads is sent.
        for sent in session.sent:
            self.assertTrue(sent.startswith("/*myflames:"), sent)
            self.assertRegex(sent.split("*/", 1)[1].lstrip(), r"^(SELECT|SHOW CREATE TABLE) ")

    def test_instant_prediction_reads_innodb_facts_and_drops_inplace_suggestion(self):
        review, session = self.review("mysql", "ALTER TABLE t1 ADD COLUMN x INT", self.mysql_answers())
        self.assertEqual(review["prediction"]["summary"], "INSTANT, metadata only")
        self.assertTrue(any("information_schema.INNODB_TABLES" in s for s in session.sent))
        self.assertIsNone(review["suggestion"])
        self.assertIn("No rewrite offered: the server is predicted to run this ALTER instantly.", review["warnings"])

    def test_unreadable_facts_leave_the_statement_unpredicted(self):
        answers = self.mysql_answers()
        answers[4] = ("SELECT t.TOTAL_ROW_VERSIONS", ConnectorError("Access denied; you need the PROCESS privilege"))
        review, _ = self.review("mysql", "ALTER TABLE t1 ADD COLUMN x INT", answers)
        prediction = review["prediction"]
        self.assertFalse(prediction["predicted"])
        self.assertIn("PROCESS", prediction["reason"])
        # The existing INPLACE suggestion stays for a statement that is not predicted instant.
        self.assertTrue(review["suggestion"].endswith(", ALGORITHM=INPLACE, LOCK=NONE"))

    def test_referenced_columns_read_for_a_rename(self):
        review, session = self.review("mysql", "ALTER TABLE t1 RENAME COLUMN id TO id2", self.mysql_answers(refs=["id"]))
        self.assertEqual(review["prediction"]["summary"], "INPLACE, no rebuild")
        self.assertTrue(any("KEY_COLUMN_USAGE" in s for s in session.sent))
        self.assertTrue(any("cannot see" in n for n in review["prediction"]["notes"]))

    def test_mariadb_review(self):
        answers = [
            ("SELECT DATABASE(), @@performance_schema", [["app", "1"]]),
            ("SELECT ENGINE, DATA_LENGTH, INDEX_LENGTH", [["InnoDB", "16384", "32768"]]),
            ("SHOW CREATE TABLE `app`.`t1`", [["t1", MARIADB_BASE]]),
            ("SELECT @@foreign_key_checks", [["1", FACTS["mariadb"]["sql_mode"], "16384", "1", "add_drop_reorder",
                                              "UTF8_IS_UTF8MB3", "1", "DEFAULT"]]),
            ("SELECT t.SPACE", [["5", "0"]]),
        ]
        review, session = self.review("mariadb", "ALTER TABLE t1 COMMENT = 'x'", answers)
        self.assertEqual(review["prediction"]["summary"], "INSTANT, metadata only", review["prediction"]["reason"])
        self.assertTrue(any("@@alter_algorithm" in s for s in session.sent))
        review, session = self.review("mariadb", "ALTER TABLE t1 COMMENT = 'x'", answers, version="11.8.6-MariaDB")
        self.assertFalse(any("@@alter_algorithm" in s for s in session.sent))

    def test_missing_table_is_not_predicted(self):
        answers = self.mysql_answers()
        answers[2] = ("SHOW CREATE TABLE", ConnectorError("Table 'app.t1' doesn't exist"))
        review, _ = self.review("mysql", "ALTER TABLE t1 ADD INDEX kd (d)", answers)
        self.assertFalse(review["prediction"]["predicted"])
        self.assertIn("SHOW CREATE TABLE failed", review["prediction"]["reason"])

    def test_prediction_errors_never_block_the_review(self):
        from unittest import mock
        with mock.patch.object(ed, "_alter_prediction", side_effect=RuntimeError("boom")):
            review, _ = self.review("mysql", "ALTER TABLE t1 ADD INDEX kd (d)", self.mysql_answers())
        self.assertFalse(review["prediction"]["predicted"])
        self.assertIn("RuntimeError", review["prediction"]["reason"])
        self.assertIn("schema", review)


if __name__ == "__main__":
    unittest.main()
