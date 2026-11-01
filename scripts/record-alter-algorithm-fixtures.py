#!/usr/bin/env python3
"""Record which ALTER TABLE algorithms a lab server accepts, as a fixture.

Lab use only. For every case below this script creates a small table in its
own database ``myflames_alter_probe`` (never any other), fills it with 100
rows, and runs the case's ALTER once per request: ``ALGORITHM=INSTANT``,
``ALGORITHM=INPLACE, LOCK=NONE``, ``ALGORITHM=INPLACE``, ``ALGORITHM=COPY,
LOCK=NONE``, ``ALGORITHM=COPY`` and no clause, each time on a fresh table.
Binary logging is off in these sessions (sql_log_bin = 0), so nothing reaches
the lab's replicas. A request the server cannot honour fails with an error and changes nothing.
The InnoDB table id read before and after tells whether the table was
rebuilt (MySQL information_schema.INNODB_TABLES, MariaDB INNODB_SYS_TABLES).
The database is dropped at the end.

Start the GA matrix first (scripts/ga-matrix.sh up), then for example:

    python3 scripts/record-alter-algorithm-fixtures.py --port 33084 --server mysql-8.4
    python3 scripts/record-alter-algorithm-fixtures.py --port 33114 --server mariadb-11.4

The root password comes from the matrix credentials file (``CREDS_FILE``,
else local/ga-matrix/credentials.env), goes to the client in a 0600 defaults
file and is never printed. Output: test/fixtures/alter_algorithm/live-<server>.json
with each case's SHOW CREATE TABLE, its ALTER, the session settings and every
attempt's outcome, which test/test_alter_algorithm.py compares with
myflames.alter_algorithm.predict.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "myflames_alter_probe"
ROWS = 100

BASE = ("CREATE TABLE t1 ("
        "id INT NOT NULL AUTO_INCREMENT, a INT NULL, b VARCHAR(50) NOT NULL DEFAULT '', "
        "c VARCHAR(10) CHARACTER SET latin1 NULL, d VARCHAR(60) NULL, e ENUM('x','y') NULL, "
        "s SET('p','q') NULL, n INT NOT NULL DEFAULT 0, ts DATETIME NULL, "
        "u VARCHAR(20) CHARACTER SET utf8mb3 NULL, ch CHAR(4) NULL, tx TEXT NULL, dm DECIMAL(10,2) NULL, "
        "PRIMARY KEY (id), KEY ka (a), KEY kb (b)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
BASE_ROW = "(%(i)d, 'b%(i)d', 'c', 'd%(i)d', 'x', 'p', 0, '2026-01-01 00:00:00', 'u', 'ab', 't', 1.50)"
BASE_COLS = "(a, b, c, d, e, s, n, ts, u, ch, tx, dm)"

PARENT = "CREATE TABLE p (id INT NOT NULL PRIMARY KEY) ENGINE=InnoDB"
PARENT_ROWS = "INSERT INTO p VALUES " + ",".join("(%d)" % i for i in range(1, ROWS + 1))


def base_with(extra_columns="", extra_keys="", options=""):
    """The base table with more columns, keys or table options."""
    ddl = BASE
    if extra_columns:
        ddl = ddl.replace(" PRIMARY KEY (id)", " " + extra_columns + ", PRIMARY KEY (id)")
    if extra_keys:
        ddl = ddl.replace("KEY kb (b))", "KEY kb (b), " + extra_keys + ")")
    if options:
        ddl += " " + options
    return ddl


SMALL = "CREATE TABLE t1 (k INT NOT NULL, v INT NULL, w VARCHAR(20) NULL%s) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
SMALL_ROW = "(%(i)d, %(i)d, 'w')"
SMALL_COLS = "(k, v, w)"

MB3 = ("CREATE TABLE t1 (id INT NOT NULL PRIMARY KEY, a VARCHAR(20) NULL, b VARCHAR(100) NULL) "
       "ENGINE=InnoDB DEFAULT CHARSET=utf8mb3")
MB3_ROW = "(%(i)d, 'a', 'b')"
MB3_COLS = "(id, a, b)"


def case(cid, alter, table=None, rows=None, cols=None, session=(), setup=(), post=(), only=None, after="t1"):
    return {"id": cid, "alter": alter, "create": table or BASE, "row": rows or BASE_ROW,
            "cols": cols or BASE_COLS, "session": list(session), "setup": list(setup), "post": list(post),
            "only": only, "after_name": after}


FK_OFF = ("SET SESSION foreign_key_checks = 0",)
CASES = [
    case("add_column_last", "ADD COLUMN x INT NULL"),
    case("add_column_first", "ADD COLUMN x INT NULL FIRST"),
    case("add_column_after", "ADD COLUMN x INT NULL AFTER a"),
    case("add_column_not_null_default", "ADD COLUMN x INT NOT NULL DEFAULT 7"),
    case("add_column_not_null_no_default", "ADD COLUMN x VARCHAR(20) NOT NULL"),
    case("add_column_default_expression", "ADD COLUMN x INT DEFAULT (FLOOR(RAND() * 10))"),
    case("add_column_default_constant_expression", "ADD COLUMN x INT DEFAULT (1 + 1)"),
    case("add_column_timestamp_now", "ADD COLUMN x TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP"),
    case("add_column_virtual", "ADD COLUMN v INT AS (a + 1) VIRTUAL"),
    case("add_column_stored", "ADD COLUMN v INT AS (a + 1) STORED"),
    case("add_column_auto_increment", "ADD COLUMN x INT NOT NULL AUTO_INCREMENT, ADD UNIQUE KEY ux (x)",
         table=SMALL % ", PRIMARY KEY (k)", rows=SMALL_ROW, cols=SMALL_COLS),
    case("add_two_columns", "ADD COLUMN x INT NULL, ADD COLUMN y VARCHAR(10) NULL"),
    case("add_column_and_index", "ADD COLUMN x INT NULL, ADD INDEX kx (x)"),
    case("add_column_drop_column", "ADD COLUMN x INT NULL, DROP COLUMN n"),
    case("add_virtual_and_index", "ADD COLUMN v INT AS (a + 1) VIRTUAL, ADD INDEX kv (v)"),
    case("add_column_default_uuid", "ADD COLUMN x VARCHAR(36) DEFAULT (UUID())"),
    case("add_column_default_column_reference", "ADD COLUMN x INT DEFAULT (n + 1)"),
    case("add_column_not_null_date_no_default", "ADD COLUMN x DATE NOT NULL"),
    case("add_column_and_comment", "ADD COLUMN x INT NULL, COMMENT = 'probe'"),
    case("add_index_and_comment", "ADD INDEX kd (d), COMMENT = 'probe'"),
    case("add_column_drop_index", "ADD COLUMN x INT NULL, DROP INDEX ka"),
    case("add_virtual_and_stored", "ADD COLUMN v INT AS (a + 1) VIRTUAL, ADD COLUMN x INT NULL"),
    case("add_column_redundant_table", "ADD COLUMN x INT NULL", table=base_with(options="ROW_FORMAT=REDUNDANT")),
    case("add_column_first_virtual_table", "ADD COLUMN x INT NULL FIRST",
         table=base_with(extra_columns="v INT AS (n + 1) VIRTUAL")),
    case("add_virtual_before_virtual", "ADD COLUMN v2 INT AS (n + 2) VIRTUAL AFTER a",
         table=base_with(extra_columns="v INT AS (n + 1) VIRTUAL")),
    case("add_column_compressed_table", "ADD COLUMN x INT NULL",
         table=base_with(options="ROW_FORMAT=COMPRESSED KEY_BLOCK_SIZE=8")),
    case("add_column_fulltext_table", "ADD COLUMN x INT NULL", table=base_with(extra_keys="FULLTEXT KEY ft (d)")),
    case("add_column_spatial_table", "ADD COLUMN x INT NULL",
         table=base_with(extra_columns="g POINT NOT NULL", extra_keys="SPATIAL KEY sg (g)"),
         rows="(%(i)d, 'b%(i)d', 'c', 'd%(i)d', 'x', 'p', 0, '2026-01-01 00:00:00', 'u', 'ab', 't', 1.50, POINT(%(i)d, %(i)d))",
         cols="(a, b, c, d, e, s, n, ts, u, ch, tx, dm, g)"),
    case("drop_column", "DROP COLUMN n"),
    case("drop_column_indexed", "DROP COLUMN a"),
    case("drop_column_indexed_virtual_table", "DROP COLUMN ts",
         table=base_with(extra_columns="v INT AS (n + 1) VIRTUAL", extra_keys="KEY kv (v)")),
    case("drop_virtual_column", "DROP COLUMN v", table=base_with(extra_columns="v INT AS (n + 1) VIRTUAL")),
    case("rename_column", "RENAME COLUMN n TO n2"),
    case("rename_column_referenced", "RENAME COLUMN id TO id2",
         post=("CREATE TABLE ch (id INT NOT NULL PRIMARY KEY, pid INT NULL, KEY kp (pid), "
               "CONSTRAINT fkc FOREIGN KEY (pid) REFERENCES t1 (id)) ENGINE=InnoDB",)),
    case("change_column_rename", "CHANGE COLUMN n n2 INT NOT NULL DEFAULT 0"),
    case("modify_no_change", "MODIFY COLUMN n INT NOT NULL DEFAULT 0"),
    case("modify_comment_only", "MODIFY COLUMN n INT NOT NULL DEFAULT 0 COMMENT 'count'"),
    case("modify_reorder", "MODIFY COLUMN n INT NOT NULL DEFAULT 0 FIRST"),
    case("modify_int_to_bigint", "MODIFY COLUMN n BIGINT NOT NULL DEFAULT 0"),
    case("modify_int_unsigned", "MODIFY COLUMN n INT UNSIGNED NOT NULL DEFAULT 0"),
    case("modify_decimal_precision", "MODIFY COLUMN dm DECIMAL(12,2) NULL"),
    case("modify_char_length", "MODIFY COLUMN ch CHAR(8) NULL"),
    case("modify_text_to_mediumtext", "MODIFY COLUMN tx MEDIUMTEXT NULL"),
    case("modify_varchar_to_text", "MODIFY COLUMN d TEXT NULL"),
    case("varchar_extend_same_prefix", "MODIFY COLUMN d VARCHAR(63) NULL"),
    case("varchar_extend_cross_256", "MODIFY COLUMN d VARCHAR(64) NULL"),
    case("varchar_extend_small_cross_256", "MODIFY COLUMN c VARCHAR(300) CHARACTER SET latin1 NULL"),
    case("varchar_extend_indexed", "MODIFY COLUMN b VARCHAR(60) NOT NULL DEFAULT ''"),
    case("varchar_extend_prefix_indexed", "MODIFY COLUMN d VARCHAR(63) NULL", table=base_with(extra_keys="KEY kp (d(10))")),
    case("varchar_shrink", "MODIFY COLUMN d VARCHAR(30) NULL"),
    case("varchar_utf8mb3_to_utf8mb4", "MODIFY COLUMN u VARCHAR(20) CHARACTER SET utf8mb4 NULL"),
    case("varchar_collation_unindexed", "MODIFY COLUMN d VARCHAR(60) COLLATE utf8mb4_bin NULL"),
    case("varchar_collation_indexed", "MODIFY COLUMN b VARCHAR(50) COLLATE utf8mb4_bin NOT NULL DEFAULT ''"),
    case("varchar_latin1_to_utf8mb4", "MODIFY COLUMN c VARCHAR(10) CHARACTER SET utf8mb4 NULL"),
    case("null_to_not_null", "MODIFY COLUMN a INT NOT NULL"),
    case("not_null_to_null", "MODIFY COLUMN n INT NULL DEFAULT 0"),
    case("not_null_to_null_promoted_key", "MODIFY COLUMN k INT NULL",
         table=SMALL % ", UNIQUE KEY uk (k)", rows=SMALL_ROW, cols=SMALL_COLS),
    case("not_null_to_null_redundant", "MODIFY COLUMN n INT NULL DEFAULT 0", table=base_with(options="ROW_FORMAT=REDUNDANT")),
    case("timestamp_not_null", "MODIFY COLUMN t2 TIMESTAMP NOT NULL",
         table=base_with(extra_columns="t2 TIMESTAMP NULL DEFAULT NULL"),
         rows="(%(i)d, 'b%(i)d', 'c', 'd%(i)d', 'x', 'p', 0, '2026-01-01 00:00:00', 'u', 'ab', 't', 1.50, "
              "'2026-01-01 00:00:00')",
         cols="(a, b, c, d, e, s, n, ts, u, ch, tx, dm, t2)"),
    case("timestamp_not_null_default", "MODIFY COLUMN t2 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
         table=base_with(extra_columns="t2 TIMESTAMP NULL DEFAULT NULL"),
         rows="(%(i)d, 'b%(i)d', 'c', 'd%(i)d', 'x', 'p', 0, '2026-01-01 00:00:00', 'u', 'ab', 't', 1.50, "
              "'2026-01-01 00:00:00')",
         cols="(a, b, c, d, e, s, n, ts, u, ch, tx, dm, t2)"),
    case("modify_add_auto_increment", "MODIFY COLUMN k INT NOT NULL AUTO_INCREMENT",
         table="CREATE TABLE t1 (id INT NOT NULL PRIMARY KEY, k INT NOT NULL, v INT NULL, KEY kk (k)) ENGINE=InnoDB",
         rows="(%(i)d, %(i)d, %(i)d)", cols="(id, k, v)"),
    case("rename_table_session_copy", "RENAME TO t1r", session=("SET SESSION alter_algorithm = 'COPY'",),
         only="mariadb", after="t1r"),
    case("set_default", "ALTER COLUMN n SET DEFAULT 5"),
    case("drop_default", "ALTER COLUMN b DROP DEFAULT"),
    case("enum_append", "MODIFY COLUMN e ENUM('x','y','z') NULL"),
    case("enum_insert", "MODIFY COLUMN e ENUM('x','z','y') NULL"),
    case("set_append", "MODIFY COLUMN s SET('p','q','r') NULL"),
    case("set_append_wider", "MODIFY COLUMN s SET('p','q','m3','m4','m5','m6','m7','m8','m9') NULL"),
    case("add_index", "ADD INDEX kd (d)"),
    case("add_unique_index", "ADD UNIQUE INDEX ku (n, id)"),
    case("add_prefix_index", "ADD INDEX kt (tx(10))"),
    case("drop_index", "DROP INDEX ka"),
    case("drop_promoted_unique", "DROP INDEX uk", table=SMALL % ", UNIQUE KEY uk (k)", rows=SMALL_ROW, cols=SMALL_COLS),
    case("rename_index", "RENAME INDEX ka TO ka2"),
    case("drop_and_add_index", "DROP INDEX ka, ADD INDEX ka (a, n)"),
    case("index_invisible", "ALTER INDEX ka INVISIBLE", only="mysql"),
    case("index_ignored", "ALTER INDEX ka IGNORED", only="mariadb"),
    case("add_fulltext_first", "ADD FULLTEXT INDEX ft (d)"),
    case("add_two_fulltext", "ADD FULLTEXT INDEX ft1 (d), ADD FULLTEXT INDEX ft2 (b)"),
    case("add_fulltext_second", "ADD FULLTEXT INDEX ft2 (b)", table=base_with(extra_keys="FULLTEXT KEY ft (d)")),
    case("add_spatial_index", "ADD SPATIAL INDEX sg (g)",
         table=base_with(extra_columns="g POINT NOT NULL"),
         rows="(%(i)d, 'b%(i)d', 'c', 'd%(i)d', 'x', 'p', 0, '2026-01-01 00:00:00', 'u', 'ab', 't', 1.50, POINT(%(i)d, %(i)d))",
         cols="(a, b, c, d, e, s, n, ts, u, ch, tx, dm, g)"),
    case("add_primary_key", "ADD PRIMARY KEY (k)", table=SMALL % "", rows=SMALL_ROW, cols=SMALL_COLS),
    case("add_primary_key_nullable_column", "ADD PRIMARY KEY (v)", table=SMALL % "", rows=SMALL_ROW, cols=SMALL_COLS),
    case("add_unique_promoted", "ADD UNIQUE KEY uk (k)", table=SMALL % "", rows=SMALL_ROW, cols=SMALL_COLS),
    case("drop_primary_key", "DROP PRIMARY KEY", table=SMALL % ", PRIMARY KEY (k)", rows=SMALL_ROW, cols=SMALL_COLS),
    case("drop_and_add_primary_key", "DROP PRIMARY KEY, ADD PRIMARY KEY (id, n)"),
    case("add_foreign_key_checks_on", "ADD CONSTRAINT fk1 FOREIGN KEY (a) REFERENCES p (id)", setup=(PARENT, PARENT_ROWS)),
    case("add_foreign_key_checks_off", "ADD CONSTRAINT fk1 FOREIGN KEY (a) REFERENCES p (id)",
         setup=(PARENT, PARENT_ROWS), session=FK_OFF),
    case("add_foreign_key_new_index_checks_off", "ADD CONSTRAINT fk1 FOREIGN KEY (n) REFERENCES p (id)",
         setup=(PARENT, PARENT_ROWS), session=FK_OFF),
    case("drop_foreign_key", "DROP FOREIGN KEY fk1", setup=(PARENT, PARENT_ROWS),
         table=base_with(extra_keys="CONSTRAINT fk1 FOREIGN KEY (a) REFERENCES p (id)")),
    case("add_check_constraint", "ADD CONSTRAINT ck CHECK (n >= 0)"),
    case("drop_check_constraint", "DROP CHECK ck", table=base_with(extra_keys="CONSTRAINT ck CHECK (n >= 0)"),
         only="mysql"),
    case("drop_check_constraint_mariadb", "DROP CONSTRAINT ck",
         table=base_with(extra_keys="CONSTRAINT ck CHECK (n >= 0)"), only="mariadb"),
    case("column_invisible", "ALTER COLUMN n SET INVISIBLE", only="mysql"),
    case("table_comment", "COMMENT = 'probe'"),
    case("table_auto_increment", "AUTO_INCREMENT = 1000"),
    case("table_default_charset", "DEFAULT CHARSET = latin1"),
    case("table_row_format_same", "ROW_FORMAT = DYNAMIC"),
    case("table_row_format_compact", "ROW_FORMAT = COMPACT"),
    case("table_key_block_size", "ROW_FORMAT = COMPRESSED KEY_BLOCK_SIZE = 8"),
    case("table_engine_same", "ENGINE = InnoDB"),
    case("table_engine_other", "ENGINE = MyISAM"),
    case("table_force", "FORCE"),
    case("table_force_fulltext", "FORCE", table=base_with(extra_keys="FULLTEXT KEY ft (d)")),
    case("table_force_spatial", "FORCE",
         table=base_with(extra_columns="g POINT NOT NULL", extra_keys="SPATIAL KEY sg (g)"),
         rows="(%(i)d, 'b%(i)d', 'c', 'd%(i)d', 'x', 'p', 0, '2026-01-01 00:00:00', 'u', 'ab', 't', 1.50, POINT(%(i)d, %(i)d))",
         cols="(a, b, c, d, e, s, n, ts, u, ch, tx, dm, g)"),
    case("convert_latin1_columns", "CONVERT TO CHARACTER SET utf8mb4"),
    case("convert_utf8mb3_to_utf8mb4", "CONVERT TO CHARACTER SET utf8mb4", table=MB3, rows=MB3_ROW, cols=MB3_COLS),
    case("rename_table", "RENAME TO t1r", after="t1r"),
    case("rename_table_and_add_column", "RENAME TO t1r, ADD COLUMN x INT NULL", after="t1r"),
    case("order_by", "ORDER BY n"),
]

ATTEMPTS = [
    ("instant", "ALGORITHM=INSTANT"),
    ("inplace_lock_none", "ALGORITHM=INPLACE, LOCK=NONE"),
    ("inplace", "ALGORITHM=INPLACE"),
    ("copy_lock_none", "ALGORITHM=COPY, LOCK=NONE"),
    ("copy", "ALGORITHM=COPY"),
    ("default", ""),
]


def die(message):
    sys.stderr.write("record-alter-algorithm-fixtures: %s\n" % message)
    sys.exit(2)


def read_password():
    path = os.environ.get("CREDS_FILE") or os.path.join(REPO, "local", "ga-matrix", "credentials.env")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        die("cannot read the credentials file %s (%s)" % (path, exc.strerror or exc.__class__.__name__))
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == "ROOT_PASSWORD" and value.strip():
            return value.strip().strip("'\"")
    die("%s has no ROOT_PASSWORD" % path)


class Client(object):
    def __init__(self, client, host, port, password):
        self.dir = tempfile.mkdtemp(prefix="myflames-alter-probe-")
        self.defaults = os.path.join(self.dir, "client.cnf")
        fd = os.open(self.defaults, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("[client]\nuser=root\npassword=\"%s\"\nhost=%s\nport=%d\nprotocol=TCP\n"
                         % (password.replace("\\", "\\\\").replace('"', '\\"'), host, port))
        self.client = client
        self.password = password

    def run(self, lines):
        """(exit code, stdout, stderr) of a script, one statement per line."""
        proc = subprocess.run([self.client, "--defaults-extra-file=" + self.defaults, "--batch", "--raw",
                               "--skip-column-names"], input="\n".join(lines) + "\n",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        for text in (proc.stdout, proc.stderr):
            if self.password and self.password in text:
                die("the client output contains the password; nothing written")
        return proc.returncode, proc.stdout, proc.stderr

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def table_id_sql(engine, name):
    source = "information_schema.INNODB_TABLES" if engine == "mysql" else "information_schema.INNODB_SYS_TABLES"
    extra = "TOTAL_ROW_VERSIONS" if engine == "mysql" else "N_COLS"
    return "SELECT '@@%%s', TABLE_ID, %s, SPACE FROM %s WHERE NAME = '%s/%s';" % (extra, source, DB, name)


def attempt(client, engine, spec, clause):
    rows = ",".join(spec["row"] % {"i": i} for i in range(1, ROWS + 1))
    # sql_log_bin = 0: nothing here reaches the lab's replicas.
    lines = ["SET SESSION sql_log_bin = 0;", "SET SESSION foreign_key_checks = 0;", "DROP DATABASE IF EXISTS %s;" % DB, "CREATE DATABASE %s;" % DB,
             "USE %s;" % DB, "SET SESSION foreign_key_checks = 1;"]
    lines += [s + ";" for s in spec["setup"]]
    lines += [spec["create"] + ";", "INSERT INTO t1 %s VALUES %s;" % (spec["cols"], rows)]
    lines += [s + ";" for s in spec["post"]]
    lines += ["SELECT '@@CREATE@@';", "SHOW CREATE TABLE t1;", "SELECT '@@END@@';"]
    lines += [s + ";" for s in spec["session"]]
    lines.append("SELECT '@@REFS', IFNULL(GROUP_CONCAT(DISTINCT REFERENCED_COLUMN_NAME ORDER BY REFERENCED_COLUMN_NAME), '') "
                 "FROM information_schema.KEY_COLUMN_USAGE WHERE REFERENCED_TABLE_SCHEMA = '%s' "
                 "AND REFERENCED_TABLE_NAME = 't1';" % DB)
    lines.append(table_id_sql(engine, "t1") % "before")
    # The requested algorithm comes first: MariaDB accepts nothing after ORDER BY.
    alter = "ALTER TABLE t1 " + ((clause + ", ") if clause else "") + spec["alter"] + ";"
    lines.append(alter)
    alter_line = len(lines)
    lines.append(("SELECT '@@ROWS', ROW_COUNT();"))
    lines.append(table_id_sql(engine, spec["after_name"]) % "after")
    code, out, err = client.run(lines)
    result = {"sql": alter[:-1]}
    refs = None
    create = None
    if "@@CREATE@@" in out and "@@END@@" in out:
        body = out.split("@@CREATE@@\n", 1)[1].split("\n@@END@@", 1)[0]
        create = body.split("\t", 1)[1] if "\t" in body else body
    facts = {}
    for line in out.splitlines():
        if line.startswith("@@"):
            parts = line.split("\t")
            facts[parts[0][2:]] = parts[1:]
    if facts.get("REFS") is not None:
        refs = [name for name in (facts["REFS"][0] if facts["REFS"] else "").split(",") if name]
    if code != 0:
        message = err.strip().splitlines()[-1] if err.strip() else "client exit %d" % code
        at = " at line %d:" % alter_line
        if at not in message:
            return None, {"setup_error": message}, None
        result.update(ok=False, error=message.split(at, 1)[0].replace("ERROR ", ""),
                      message=message.split(at, 1)[1].strip())
        return create, result, refs
    before, after = facts.get("before"), facts.get("after")
    result.update(ok=True, rows_affected=int(facts["ROWS"][0]) if facts.get("ROWS") else None,
                  table_id_before=before[0] if before else None, table_id_after=after[0] if after else None,
                  rebuilt=(before[0] != after[0]) if before and after else None)
    if engine == "mysql" and before and after:
        result["row_versions"] = [int(before[1]), int(after[1])]
    if before:
        result["space_before"] = int(before[2])
    return create, result, refs


def observed(engine, spec, attempts):
    """What the attempts show: the cheapest algorithm the server accepted.

    MariaDB 11.2+ refuses COPY with LOCK=NONE only when it first planned an
    online copy (sql/sql_table.cc online_alter_check_supported); with ORDER BY
    it never plans one (``online= order == NULL``) and accepts LOCK=NONE while
    blocking writes, so that acceptance does not show concurrent writes."""
    get = lambda name: attempts.get(name) or {}
    if not get("default").get("ok"):
        return {"algorithm": None, "rebuild": None, "concurrent_dml": None}  # the statement as written fails
    copied = (get("default").get("rows_affected") or 0) > 0  # only ALGORITHM=COPY reports copied rows
    if get("instant").get("ok") and not copied:
        return {"algorithm": "INSTANT", "rebuild": False, "concurrent_dml": True}
    for name, concurrent in (("inplace_lock_none", True), ("inplace", False)):
        if get(name).get("ok") and not copied:
            return {"algorithm": "INPLACE", "rebuild": get(name).get("rebuilt"), "concurrent_dml": concurrent}
    if get("copy").get("ok") or get("default").get("ok"):
        online = bool(get("copy_lock_none").get("ok"))
        if engine == "mariadb" and spec["alter"].upper().startswith("ORDER BY"):
            online = False
        return {"algorithm": "COPY", "rebuild": True, "concurrent_dml": online}
    return {"algorithm": None, "rebuild": None, "concurrent_dml": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--server", required=True, help="fixture name, e.g. mysql-8.4 or mariadb-11.4")
    parser.add_argument("--client", default=shutil.which("mysql") or "mysql")
    parser.add_argument("--only", help="comma-separated case ids")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        die("lab servers only: --host must be a loopback address")
    client = Client(args.client, args.host, args.port, read_password())
    try:
        code, out, err = client.run(["SELECT VERSION(), @@version_comment, @@sql_mode, @@innodb_page_size;"])
        if code != 0:
            die("cannot connect: %s" % err.strip())
        version, comment, sql_mode, page = out.strip().split("\t")
        engine = "mariadb" if "mariadb" in (version + comment).lower() else "mysql"
        names = (["innodb_strict_mode", "innodb_instant_alter_column_allowed", "alter_algorithm", "old_mode",
                  "innodb_file_per_table"]
                 if engine == "mariadb" else ["innodb_strict_mode", "old_alter_table"])
        code, out, err = client.run(["SELECT %s;" % ", ".join("@@" + n for n in names)])
        if code != 0:
            die("cannot read settings: %s" % err.strip())
        settings = dict(zip(names, out.strip().split("\t")))
        code, out, err = client.run(["SELECT CHARACTER_SET_NAME, DEFAULT_COLLATE_NAME FROM "
                                     "information_schema.CHARACTER_SETS ORDER BY 1;"])
        if code != 0:
            die("cannot read character sets: %s" % err.strip())
        settings["default_collations"] = dict(line.split("\t") for line in out.strip().splitlines())
        wanted = set(args.only.split(",")) if args.only else None
        cases = []
        for spec in CASES:
            if spec["only"] not in (None, engine) or (wanted and spec["id"] not in wanted):
                continue
            entry = {"id": spec["id"], "alter": "ALTER TABLE t1 " + spec["alter"], "session": spec["session"],
                     "create_table": None, "attempts": {}}
            for name, clause in ATTEMPTS:
                create, result, refs = attempt(client, engine, spec, clause)
                if create and entry["create_table"] is None:
                    entry["create_table"] = create
                if refs is not None:
                    entry["referenced_columns"] = refs
                entry["attempts"][name] = result
            entry["observed"] = observed(engine, spec, entry["attempts"])
            cases.append(entry)
            sys.stderr.write("%-40s %s\n" % (spec["id"], entry["observed"]))
        client.run(["SET SESSION sql_log_bin = 0;", "DROP DATABASE IF EXISTS %s;" % DB])
    finally:
        client.close()
    data = {"server": args.server, "engine": engine, "version": version, "version_comment": comment,
            "sql_mode": sql_mode, "innodb_page_size": int(page), "settings": settings,
            "captured": datetime.date.today().isoformat(), "rows": ROWS, "cases": cases}
    out_dir = os.path.join(REPO, "test", "fixtures", "alter_algorithm")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "live-%s.json" % args.server)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.write("\n")
    sys.stderr.write("wrote %s (%d cases)\n" % (path, len(cases)))


if __name__ == "__main__":
    main()
