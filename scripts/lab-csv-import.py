#!/usr/bin/env python3
"""Run Import CSV end to end against throwaway MySQL and MariaDB servers.

Lab use only. Nothing here connects to a server this script did not start.
For each server in scope the script runs the official image from
public.ecr.aws/docker/library as throwaway containers named
``myflames-csvlab-<token>-<server>-<on|off>`` (``--rm``, removed with their
anonymous volumes, the port published on 127.0.0.1 only): one started with
``--local-infile=1`` for the import matrix and one with ``--local-infile=0``
to see LOAD DATA refused. root gets a random password (from an env file
readable by this user only, never printed). An image this run pulled is
removed at the end; an image present before is never removed, and no other
container is touched.

Every import goes through the real workspace API (``ui.api_response`` with
``/api/import/csv/*``) as the desktop app sends it, with a workspace store in
a temporary HOME. What the lab script itself sends to the server (CREATE
DATABASE, the target tables, SELECT to read rows back, SET GLOBAL sql_mode
for the NO_BACKSLASH_ESCAPES pass) runs only in these containers.

Scenarios per server: existing and new tables; INSERT batches and LOAD DATA
LOCAL; all or nothing with a duplicate key (table unchanged) and commit every
N (rows kept, the stop line right); Skip and report; Stop during INSERT and
during LOAD DATA; separators comma, semicolon, tab, pipe and a custom one;
UTF-8 with BOM, Latin-1 / Windows-1252 and UTF-16 LE and BE files; quoted
separators, quotes and newlines, CRLF; NULL markers; dates, decimals,
unsigned limits, ENUM, SET, BIT, JSON; a MyISAM table; NO_BACKSLASH_ESCAPES;
Read-only refused; local_infile OFF refused.

Examples:

    python3 scripts/lab-csv-import.py
    python3 scripts/lab-csv-import.py --servers mysql-8.4 --keep-images
    python3 scripts/lab-csv-import.py --out local/csv-import-2026-10-29/LAB.md
"""
import argparse
import codecs
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ECR = "public.ecr.aws/docker/library/"
SERVERS = {
    "mysql-8.4": ("mysql", "8.4.11"),
    "mysql-9.7": ("mysql", "9.7.2"),
    "mariadb-10.11": ("mariadb", "10.11.19"),
    "mariadb-11.4": ("mariadb", "11.4.13"),
}
TOKEN = secrets.token_hex(3)
PREFIX = "myflames-csvlab-%s-" % TOKEN
DEFAULT_OUT = os.path.join(REPO, "local", "csv-import-2026-10-29", "LAB.md")
_ACTIVE = set()


class LabError(Exception):
    pass


def log(text):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), text))
    sys.stderr.flush()


def run(cmd, timeout=600, check=True):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LabError("timed out: %s" % " ".join(shlex.quote(c) for c in cmd[:5]))
    out, err = proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
    if check and proc.returncode:
        raise LabError("%s exited %d: %s" % (" ".join(shlex.quote(c) for c in cmd[:5]), proc.returncode,
                                             (err or out).strip()[-400:]))
    return proc.returncode, out, err


def images():
    _, out, _ = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], 60)
    return set(line.strip() for line in out.splitlines() if line.strip())


# ---- containers ---------------------------------------------------------------------------------

class Server(object):
    def __init__(self, key, local_infile, password, workdir):
        self.key = key
        self.engine, self.version = SERVERS[key]
        self.image = ECR + "%s:%s" % (self.engine, self.version)
        self.local_infile = local_infile
        self.password = password
        self.name = PREFIX + "%s-%s" % (key, "on" if local_infile else "off")
        self.workdir = workdir
        self.port = None

    def start(self):
        env = os.path.join(self.workdir, self.name + ".env")
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            key = "MYSQL_ROOT_PASSWORD" if self.engine == "mysql" else "MARIADB_ROOT_PASSWORD"
            handle.write("%s=%s\n" % (key, self.password))
        _ACTIVE.add(self.name)
        try:
            run(["docker", "run", "-d", "--rm", "--name", self.name, "-p", "127.0.0.1::3306", "--env-file", env,
                 self.image, "--local-infile=%d" % (1 if self.local_infile else 0)], 300)
        finally:
            os.unlink(env)
        _, out, _ = run(["docker", "port", self.name, "3306/tcp"], 60)
        self.port = int(out.strip().splitlines()[0].rsplit(":", 1)[1])
        deadline = time.time() + 300
        last = None
        while time.time() < deadline:
            try:
                rows = self.conn().query_rows("SELECT @@version, @@local_infile")
                self.server_version, self.server_local_infile = rows[0][0], rows[0][1]
                return
            except Exception as exc:  # the entrypoint restarts the server once
                last = exc
                time.sleep(2)
        raise LabError("%s did not accept connections: %s" % (self.name, last))

    def conn(self):
        from myflames.connector import MySQLConnection
        return MySQLConnection(host="127.0.0.1", port=self.port, user="root", password=self.password,
                               connect_timeout=5, query_timeout=120)

    def admin(self, sql):
        with self.conn() as conn:
            return conn.run_stdin(sql)

    def rows(self, sql):
        with self.conn() as conn:
            return conn.query_rows(sql)

    def stop(self):
        run(["docker", "rm", "-f", "-v", self.name], 120, check=False)
        _ACTIVE.discard(self.name)


# ---- the API, as the workspace calls it ---------------------------------------------------------

class Workspace(object):
    def __init__(self):
        from myflames.workspace_store import WorkspaceStore
        self.store = WorkspaceStore()


SCHEMAS = {"/api/import/csv/inspect": "csv-inspect-1.0.json", "/api/import/csv/plan": "csv-import-plan-1.0.json",
           "/api/import/csv/apply": "csv-import-result-1.0.json",
           "/api/import/csv/progress": "csv-import-progress-1.0.json"}


def api(ws, path, payload):
    """The workspace API; every answer is checked against its JSON Schema."""
    from myflames import ui
    answer = ui.api_response(path, payload, ws)
    if path in SCHEMAS:
        sys.path.insert(0, os.path.join(REPO, "test"))
        from test_chart_data import validate
        with open(os.path.join(REPO, "docs", "schemas", SCHEMAS[path]), encoding="utf-8") as handle:
            validate(json.loads(json.dumps(answer)), json.load(handle))
    return answer


def write(folder, name, text, encoding="utf-8", newline="\n"):
    path = os.path.join(folder, name)
    data = text.replace("\n", newline)
    with open(path, "wb") as handle:
        handle.write(data.encode(encoding) if isinstance(encoding, str) else encoding(data))
    return path


def import_file(ws, server, path, target, fmt=None, method="insert", transaction=None, on_error="stop",
                connection=None, expect_ready=True, stop_after=None, action=None, plan_only=False):
    """open-path, inspect, plan, apply; returns (inspect, plan, result)."""
    opened = api(ws, "/api/import/csv/open-path", {"path": path})
    inspected = api(ws, "/api/import/csv/inspect", {"file_id": opened["file_id"], "format": fmt})
    connection = connection or {"host": "127.0.0.1", "port": server.port, "user": "root", "password": server.password}
    if target.get("mode") == "new" and "new_columns" not in target:
        target = dict(target)
        target["new_columns"] = [{"name": c["name"], "sql_type": c["inferred"]["sql_type"],
                                  "nullable": c["inferred"]["nullable"]} for c in inspected["columns"]]
        target["mapping"] = [{"csv_index": c["index"], "column": c["name"]} for c in inspected["columns"]]
    plan = api(ws, "/api/import/csv/plan", {"connection": connection, "file_id": opened["file_id"],
                                            "format": inspected["format"], "target": target, "method": method,
                                            "transaction": transaction or {"mode": "all"}, "on_error": on_error,
                                            "progress_id": "plan" + secrets.token_hex(10)})
    if not plan["ready"] or plan_only:
        if expect_ready and not plan["ready"]:
            raise LabError("plan not ready: %s" % plan["blockers"])
        return inspected, plan, None
    progress_id = "apply" + secrets.token_hex(10)
    holder = {}

    def go():
        try:
            holder["result"] = api(ws, "/api/import/csv/apply", {"connection": connection, "token": plan["token"],
                                                                 "progress_id": progress_id})
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=go)
    thread.start()
    stop_info = None
    if stop_after is not None:
        deadline = time.time() + 300
        while thread.is_alive() and time.time() < deadline:
            snap = api(ws, "/api/import/csv/progress", {"progress_id": progress_id})
            if stop_after(snap):
                if action is not None:
                    stop_info = {"action": action(), "at": snap}
                else:
                    stop_info = api(ws, "/api/import/csv/stop", {"progress_id": progress_id})
                    stop_info["at"] = snap
                break
            time.sleep(0.05)
    thread.join(900)
    if "error" in holder:
        raise holder["error"]
    result = holder["result"]
    if stop_info is not None:
        result["_stop"] = stop_info
    return inspected, plan, result


# ---- data ---------------------------------------------------------------------------------------

TYPED_DDL = """CREATE TABLE {t} (
  `id` INT UNSIGNED NOT NULL PRIMARY KEY,
  `name` VARCHAR(40) NULL,
  `note` TEXT NULL,
  `price` DECIMAL(10,2) NULL,
  `qty` SMALLINT UNSIGNED NULL,
  `big` BIGINT UNSIGNED NULL,
  `neg` TINYINT NULL,
  `ratio` DOUBLE NULL,
  `day` DATE NULL,
  `seen_at` DATETIME(3) NULL DEFAULT CURRENT_TIMESTAMP(3),
  `dur` TIME NULL,
  `yr` YEAR NULL,
  `flag` TINYINT(1) NULL,
  `bits` BIT(4) NULL,
  `color` ENUM('red','green','blue') NULL,
  `tags` SET('a','b','c') NULL,
  `doc` JSON NULL,
  `bin` VARBINARY(16) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
TYPED_COLUMNS = ["id", "name", "note", "price", "qty", "big", "neg", "ratio", "day", "seen_at", "dur", "yr", "flag",
                 "bits", "color", "tags", "doc", "bin"]
TYPED_CSV = (
    'id,name,note,price,qty,big,neg,ratio,day,seen_at,dur,yr,flag,bits,color,tags,doc,bin\n'
    '1,plain,"",1.50,0,0,-128,0.5,2024-01-02,2024-01-02 03:04:05.678,12:30:00,2024,true,15,red,"a,b","{""k"": 1}",abc\n'
    '2,"comma, inside","line one\nline two ""quoted""",12345678.99,65535,18446744073709551615,127,-1.5e10,'
    '1999-12-31,1999-12-31T23:59:59,-838:59:59,1901,false,0,Blue,,"[1, 2, {""x"": null}]",\\N\n'
    '3,\\N,back\\slash and tab\tinside,0.01,\\N,\\N,0,\\N,\\N,\\N,\\N,2155,1,1,green,c,\\N,\n'
    '4,"caf\u00e9 \u00fc\u00f1\u00ee\u00e7\u00f8d\u00e9",emoji \U0001F600 here,-99999999.99,1,1,-1,1e-5,2000-02-29,'
    '2000-02-29 00:00:00,838:59:59,2000,0,8,blue,"b,a",{},x\n'
    '5,"quote "" and \'single\'",NULL,7,7,7,7,7,2024-06-30,2024-06-30 12:00:00.5,00:00:01,1999,yes,7,red,a,"""str""",\n')
TYPED_ROWS = 5


def setup_database(server, schema):
    server.admin("CREATE DATABASE %s; " % schema)


def _in_flight(seconds):
    """Stop once a data statement has been in flight for *seconds*."""
    seen = {}

    def check(snap):
        if snap.get("in_flight"):
            seen.setdefault("at", time.time())
            return time.time() - seen["at"] >= seconds
        seen.pop("at", None)
        return False
    return check


def table_hex(server, schema, table, columns, order="id"):
    """Every value as hex (NULL as N), for exact comparisons."""
    exprs = ", ".join("IFNULL(HEX(CAST(%s AS BINARY)), 'N')" % ("`%s`" % c if c != "bits" else "`bits`+0")
                      for c in columns)
    return server.rows("SELECT %s FROM `%s`.`%s` ORDER BY %s" % (exprs, schema, table, order))


def count(server, schema, table):
    return int(server.rows("SELECT COUNT(*) FROM `%s`.`%s`" % (schema, table))[0][0])


def hexed(text):
    return text.encode("utf-8").hex().upper()


# ---- scenarios ----------------------------------------------------------------------------------

class Report(object):
    def __init__(self, key):
        self.key = key
        self.rows = []
        self.observations = []

    def add(self, name, ok, detail):
        self.rows.append((name, ok, detail))
        log("%s %s: %s %s" % (self.key, name, "PASS" if ok else "FAIL", detail))


def scenario(report, name, function):
    try:
        ok, detail = function()
    except Exception as exc:
        ok, detail = False, "error: %s" % exc
        traceback.print_exc()
    report.add(name, ok, detail)


def run_server(key, args, workdir, ws):
    report = Report(key)
    password = secrets.token_hex(16)
    on = Server(key, True, password, workdir)
    off = Server(key, False, secrets.token_hex(16), workdir)
    files = os.path.join(workdir, key)
    os.makedirs(files, exist_ok=True)
    schema = "csvlab"
    try:
        log("%s: starting %s (local_infile ON)" % (key, on.image))
        on.start()
        report.version = on.server_version
        report.observations.append("Started with --local-infile=1: @@local_infile = %s." % on.server_local_infile)
        setup_database(on, schema)
        for table in ("typed_insert", "typed_load"):
            on.admin(TYPED_DDL.format(t="`%s`.`%s`" % (schema, table)))
        typed = write(files, "typed.csv", TYPED_CSV)
        # Empty fields are empty strings here (row 1's note, row 3's bin); NULL
        # comes from \\N and the word NULL.
        fmt = {"null_markers": ["\\N", "NULL"], "empty": "empty"}
        target = {"mode": "existing", "schema": schema, "table": "typed_insert",
                  "mapping": [{"csv_index": i, "column": c} for i, c in enumerate(TYPED_COLUMNS)]}

        def typed_insert():
            inspected, plan, result = import_file(ws, on, typed, target, fmt)
            values = table_hex(on, schema, "typed_insert", TYPED_COLUMNS)
            checks = [
                result["outcome"] == "done", result["rows"]["committed"] == TYPED_ROWS,
                count(on, schema, "typed_insert") == TYPED_ROWS,
                values[1][2] == hexed('line one\nline two "quoted"'),   # quoted newline and quotes
                values[0][2] == "" and values[2][17] == "",             # empty string and empty bytes, not NULL
                values[2][1] == "N",                                      # \N is NULL
                values[4][2] == "N",                                      # NULL marker
                values[2][2] == hexed("back\\slash and tab\tinside"),
                values[3][2] == hexed("emoji \U0001F600 here"),
                values[1][5] == hexed("18446744073709551615"),
                values[0][12] == hexed("1") and values[1][12] == hexed("0") and values[4][12] == hexed("1"),
                values[0][13] == hexed("15"),
                values[1][14] == hexed("blue"),                           # Blue matched case-insensitively
                values[3][15] == hexed("a,b"),
            ]
            return all(checks), "outcome %s, %s rows committed, read back %d, %s statements, checks %s" % (
                result["outcome"], result["rows"]["committed"], count(on, schema, "typed_insert"), plan["batches"],
                "".join("1" if c else "0" for c in checks))
        scenario(report, "existing table, INSERT, all or nothing, types and quoting", typed_insert)

        def typed_load():
            t = dict(target, table="typed_load")
            inspected, plan, result = import_file(ws, on, typed, t, fmt, method="load_data")
            same = table_hex(on, schema, "typed_load", TYPED_COLUMNS) == table_hex(on, schema, "typed_insert",
                                                                                  TYPED_COLUMNS)
            return result["outcome"] == "done" and same and result["warnings"]["count"] == 0, \
                "outcome %s, %s rows, identical to the INSERT table: %s, warnings %s" % (
                    result["outcome"], result["rows"]["committed"], same, result["warnings"]["count"])
        scenario(report, "existing table, LOAD DATA LOCAL, same rows as INSERT", typed_load)

        # New tables from inferred types.
        new_csv = write(files, "orders.csv", "order id,customer,total,placed,paid\n" + "".join(
            "%d,Customer %d,%d.%02d,2024-%02d-%02d 10:%02d:00,%s\n" % (
                i, i % 37, i * 3, i % 100, 1 + i % 12, 1 + i % 28, i % 60, "true" if i % 2 else "false")
            for i in range(1, 2501)))

        def new_insert():
            inspected, plan, result = import_file(ws, on, new_csv, {"mode": "new", "schema": schema,
                                                                    "table": "orders_insert", "add_id_key": True})
            create = on.rows("SHOW CREATE TABLE `%s`.`orders_insert`" % schema)[0][1]
            types = [c["inferred"]["sql_type"] for c in inspected["columns"]]
            return result["outcome"] == "done" and count(on, schema, "orders_insert") == 2500, \
                "inferred %s; %s rows in %d statements; read back %d" % (
                    types, result["rows"]["committed"], plan["batches"], count(on, schema, "orders_insert")) + \
                ("" if "AUTO_INCREMENT" in create else "; no AUTO_INCREMENT key?")
        scenario(report, "new table (inferred types, id key), INSERT", new_insert)

        def new_load():
            inspected, plan, result = import_file(ws, on, new_csv, {"mode": "new", "schema": schema,
                                                                    "table": "orders_load", "add_id_key": False},
                                                  method="load_data", transaction={"mode": "every", "every": 1000})
            n = count(on, schema, "orders_load")
            return result["outcome"] == "done" and n == 2500 and plan["batches"] == 3, \
                "%s rows in %d LOAD DATA statements (commit every 1,000); read back %d" % (
                    result["rows"]["committed"], plan["batches"], n)
        scenario(report, "new table, LOAD DATA LOCAL, commit every 1,000", new_load)

        # A value the file check lets through but the server refuses: CREATE
        # TABLE has committed, so the table stays (empty) and the result offers
        # the DROP TABLE text.
        late = write(files, "late.csv", "ts,v\n2024-01-01 00:00:00,a\n2038-06-01 00:00:00,b\n")

        def new_table_stays():
            target = {"mode": "new", "schema": schema, "table": "ts_new", "add_id_key": True,
                      "new_columns": [{"name": "ts", "sql_type": "TIMESTAMP", "nullable": True},
                                      {"name": "v", "sql_type": "VARCHAR(10)", "nullable": True}],
                      "mapping": [{"csv_index": 0, "column": "ts"}, {"csv_index": 1, "column": "v"}]}
            _, plan, result = import_file(ws, on, late, target)
            exists = on.rows("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='%s' AND "
                             "TABLE_NAME='ts_new'" % schema)[0][0]
            n = count(on, schema, "ts_new") if exists == "1" else None
            ok = (result["outcome"] == "failed" and result["table_created"] and exists == "1" and n == 0
                  and result["drop_hint"] == "DROP TABLE `%s`.`ts_new`;" % schema)
            return ok, "outcome %s, server_error %s, table exists %s with %s rows, drop_hint %s" % (
                result["outcome"], result["server_error"], exists, n, result["drop_hint"])
        scenario(report, "new table, all or nothing, server refuses a TIMESTAMP after 2038-01-19: the table stays "
                         "empty", new_table_stays)

        bad_json = write(files, "json.csv", 'id,doc\n1,"{""a"": 1}"\n2,{a: 1}\n3,[1\n')

        def json_check():
            on.admin("CREATE TABLE `%s`.`docs` (id INT NOT NULL PRIMARY KEY, doc JSON NULL) ENGINE=InnoDB;" % schema)
            target = {"mode": "existing", "schema": schema, "table": "docs",
                      "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "doc"}]}
            _, plan, result = import_file(ws, on, bad_json, target, on_error="skip")
            kind = on.rows("SELECT DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='%s' AND "
                           "TABLE_NAME='docs' AND COLUMN_NAME='doc'" % schema)[0][0]
            lines = [p["line"] for p in plan["problems"]]
            return (lines == [3, 4] and result["outcome"] == "done" and count(on, schema, "docs") == 1), \
                "column type %s; problems on lines %s (%s); imported %s" % (
                    kind, lines, plan["problems"][0]["message"] if plan["problems"] else "-",
                    result["rows"]["committed"])
        scenario(report, "JSON column (MariaDB: LONGTEXT with json_valid CHECK) refuses invalid JSON", json_check)

        # Wide rows: INSERT statements near the 1 MiB budget.
        wide = write(files, "wide.csv", "id,body\n" + "".join(
            "%d,%s\n" % (i, ("row %d " % i) * 250) for i in range(1, 3001)))

        def wide_rows():
            on.admin("CREATE TABLE `%s`.`wide` (id INT NOT NULL PRIMARY KEY, body TEXT NOT NULL) ENGINE=InnoDB;"
                     % schema)
            target = {"mode": "existing", "schema": schema, "table": "wide",
                      "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "body"}]}
            _, plan, result = import_file(ws, on, wide, target)
            got = on.rows("SELECT COUNT(*), SUM(LENGTH(body)) FROM `%s`.`wide`" % schema)[0]
            want = sum(len(("row %d " % i) * 250) for i in range(1, 3001))
            return (result["outcome"] == "done" and got[0] == "3000" and int(got[1]) == want), \
                "%d statements for %s rows (%s MiB of rows); read back %s rows, %s bytes (expected %d)" % (
                    plan["batches"], got[0], round(want / 1048576.0, 1), got[0], got[1], want)
        scenario(report, "wide rows: INSERT statements near the 1 MiB budget", wide_rows)

        # Server-side failures: a duplicate key the file check cannot see.
        on.admin("CREATE TABLE `%s`.`keyed` (id INT NOT NULL PRIMARY KEY, v VARCHAR(20) NOT NULL) ENGINE=InnoDB; "
                 "INSERT INTO `%s`.`keyed` VALUES (250, 'already here');" % (schema, schema))
        dup = write(files, "dup.csv", "id,v\n" + "".join("%d,v%d\n" % (i, i) for i in range(1, 401)))
        keyed = {"mode": "existing", "schema": schema, "table": "keyed",
                 "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "v"}]}

        def dup_all():
            _, plan, result = import_file(ws, on, dup, keyed)
            n = count(on, schema, "keyed")
            from myflames import dbops
            record = dbops.audit_report(ws.store, limit=1)["records"][0]
            exact = result["failed_at"] and (result["failed_at"]["line"], result["failed_at"]["first_line"],
                                             result["failed_at"]["last_line"]) == (251, 2, 401)
            clean = "Duplicate entry" not in json.dumps(record)
            return (result["outcome"] == "failed" and n == 1 and result["rows"]["committed"] == 0 and exact
                    and clean), "outcome %s, committed %s, table rows %d (was 1), failed_at %s, server_error %s; " \
                "audit message %r (no row value: %s)" % (result["outcome"], result["rows"]["committed"], n,
                                                          result["failed_at"], result["server_error"],
                                                          record["message"], clean)
        scenario(report, "all or nothing, duplicate key: table unchanged", dup_all)

        def dup_load():
            _, plan, result = import_file(ws, on, dup, keyed, method="load_data")
            n = count(on, schema, "keyed")
            return result["outcome"] == "failed" and n == 1, "outcome %s, table rows %d (was 1), message: %s" % (
                result["outcome"], n, result["message"])
        scenario(report, "LOAD DATA, duplicate key (IGNORE semantics) detected and rolled back", dup_load)

        def dup_every():
            _, plan, result = import_file(ws, on, dup, keyed, transaction={"mode": "every", "every": 100})
            n = count(on, schema, "keyed")
            ok = (result["outcome"] == "failed" and result["rows"]["committed"] == 200 and n == 201
                  and result["last_committed_line"] == 201 and result["next_line"] == 202
                  and result["failed_at"]["line"] == 251)
            return ok, "outcome %s, committed %s, table rows %d (200 + 1), last_committed_line %s, next_line %s, " \
                       "failed_at %s" % (result["outcome"], result["rows"]["committed"], n,
                                         result["last_committed_line"], result["next_line"], result["failed_at"])
        scenario(report, "commit every 100, duplicate key at row 250: 200 kept", dup_every)
        on.admin("DELETE FROM `%s`.`keyed` WHERE id <> 250;" % schema)

        bad = write(files, "bad.csv", "id,v\n1,ok\nx,not a number\n3,ok\n250,duplicate\n5,%s\n6,ok\n" % ("y" * 30))

        def skip():
            _, plan, result = import_file(ws, on, bad, keyed, on_error="skip")
            n = count(on, schema, "keyed")
            lines = sorted(p["line"] for p in result["problems"])
            ok = result["outcome"] == "done" and result["rows"]["committed"] == 3 and n == 4 and \
                plan["rows"]["invalid"] == 2 and lines == [3, 5, 6]
            return ok, "plan invalid %s (lines %s); outcome %s, committed %s, skipped %s, problem lines %s, table " \
                       "rows %d" % (plan["rows"]["invalid"], [p["line"] for p in plan["problems"]], result["outcome"],
                                    result["rows"]["committed"], result["rows"]["skipped"], lines, n)
        scenario(report, "Skip and report: two bad values and a duplicate key", skip)

        def blocked():
            _, plan, result = import_file(ws, on, bad, keyed, expect_ready=False)
            return (not plan["ready"] and plan["token"] is None and result is None
                    and any("problems" in b for b in plan["blockers"])), "blockers: %s" % plan["blockers"]
        scenario(report, "bad rows with Stop on error: no token", blocked)

        # Stop.
        on.admin("CREATE TABLE `%s`.`big` (id INT NOT NULL PRIMARY KEY, v VARCHAR(60) NOT NULL) ENGINE=InnoDB;"
                 % schema)
        big = write(files, "big.csv", "id,v\n" + "".join("%d,value %d with some padding text\n" % (i, i)
                                                          for i in range(1, 400001)))
        bigt = {"mode": "existing", "schema": schema, "table": "big",
                "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "v"}]}

        def stop_every():
            _, plan, result = import_file(ws, on, big, bigt, transaction={"mode": "every", "every": 1000},
                                          stop_after=lambda s: (s.get("rows_committed") or 0) >= 20000)
            n = count(on, schema, "big")
            from myflames import dbops
            record = dbops.audit_report(ws.store, limit=1)["records"][0]
            ok = result["outcome"] == "stopped" and n == result["rows"]["committed"] and n % 1000 == 0 and \
                result["last_committed_line"] == n + 1 and result["next_line"] == n + 2 and \
                (record["status"], record["rows_affected"]) == ("stopped", n)
            on.admin("TRUNCATE TABLE `%s`.`big`;" % schema)
            return ok, "stopped at %s committed; table rows %d; last_committed_line %s, next_line %s; %s" % (
                result["rows"]["committed"], n, result["last_committed_line"], result["next_line"],
                result.get("_stop", {}).get("message"))
        scenario(report, "Stop during INSERT, commit every 1,000", stop_every)

        def stop_all():
            _, plan, result = import_file(ws, on, big, bigt,
                                          stop_after=lambda s: (s.get("rows_sent") or 0) >= 20000)
            n = count(on, schema, "big")
            return result["outcome"] == "stopped" and n == 0, "outcome %s after %s rows sent; table rows %d" % (
                result["outcome"], result["rows"]["sent"], n)
        scenario(report, "Stop during INSERT, all or nothing: nothing kept", stop_all)

        def kill_session():
            # The server ends the import's connection (KILL CONNECTION from the
            # lab): the client prints 2013 or 2006, and the outcome is unknown.
            def kill():
                ids = on.rows("SELECT ID FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() AND "
                              "USER = 'root' AND COMMAND NOT IN ('Daemon', 'Binlog Dump')")
                for row in ids:
                    on.admin("KILL %d;" % int(row[0]))
                return len(ids)
            _, plan, result = import_file(ws, on, big, bigt, stop_after=lambda s: (s.get("rows_sent") or 0) >= 20000,
                                          action=kill)
            n = count(on, schema, "big")
            from myflames import dbops
            record = dbops.audit_report(ws.store, limit=1)["records"][0]
            return (result["outcome"] == "unknown" and n == 0 and record["status"] == "unknown"), \
                "killed %s connection(s); outcome %s; table rows %d; audit %s; message: %s" % (
                    result.get("_stop", {}).get("action"), result["outcome"], n, record["status"],
                    result["message"][:140])
        scenario(report, "connection killed during INSERT, all or nothing: outcome unknown, nothing kept",
                 kill_session)

        def stop_load():
            from myflames import csv_import_api
            saved = csv_import_api.LOAD_CHUNK_ROWS
            csv_import_api.LOAD_CHUNK_ROWS = 1000000   # one long LOAD DATA, so Stop meets it in flight
            try:
                _, plan, result = import_file(ws, on, big, bigt, method="load_data",
                                              stop_after=_in_flight(0.1))
            finally:
                csv_import_api.LOAD_CHUNK_ROWS = saved
            n = count(on, schema, "big")
            if "KILL QUERY ended" not in result["message"]:
                report.observations.append("Stop during LOAD DATA: the KILL QUERY did not end the statement (it had "
                                           "finished on the server); the Stop check before COMMIT rolled it back.")
            return result["outcome"] == "stopped" and n == 0, "outcome %s; table rows %d; stop: %s; result: %s" % (
                result["outcome"], n, result.get("_stop", {}).get("message"), result["message"])
        scenario(report, "Stop during one LOAD DATA statement (KILL QUERY), all or nothing", stop_load)

        # Separators, encodings, line endings.
        # `added` is not in the file: its expression default fills it.
        on.admin("CREATE TABLE `%s`.`enc` (id INT NOT NULL PRIMARY KEY, txt VARCHAR(100) NULL, n INT NULL, "
                 "added TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;" % schema)
        words = ["caf\u00e9", "na\u00efve \u20ac5", "quote \" inside", "sep inside"]
        enc_target = {"mode": "existing", "schema": schema, "table": "enc",
                      "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "txt"},
                                  {"csv_index": 2, "column": "n"}]}

        def body(sep):
            def q(text):
                return '"%s"' % text.replace('"', '""')
            lines = ["id%stxt%sn" % (sep, sep)]
            for i, w in enumerate(words):
                w = w.replace("sep", "sep %s" % sep)
                lines.append("%d%s%s%s%d" % (i + 1, sep, q(w), sep, i * 10))
            return "\n".join(lines) + "\n"

        cases = [("comma", ",", "utf-8", None), ("semicolon", ";", "utf-8", None), ("tab", "\t", "utf-8", None),
                 ("pipe", "|", "utf-8", None), ("custom ^", "^", "utf-8", {"separator": "^"}),
                 ("UTF-8 with BOM", ",", "utf-8-sig", None), ("Latin-1 / cp1252", ";", "cp1252", None),
                 ("UTF-16 LE with BOM", "\t", "utf-16", None), ("UTF-16 BE with BOM", ",", "utf-16-be-bom", None),
                 ("CRLF line endings", ",", "utf-8-crlf", None)]
        for label, sep, encoding, fmt_override in cases:
            def case(label=label, sep=sep, encoding=encoding, fmt_override=fmt_override):
                on.admin("DELETE FROM `%s`.`enc`;" % schema)
                text = body(sep)
                newline = "\r\n" if encoding == "utf-8-crlf" else "\n"
                codec = {"utf-8-crlf": "utf-8", "utf-16-be-bom": lambda t: codecs.BOM_UTF16_BE + t.encode("utf-16-be")
                         }.get(encoding, encoding)
                path = write(files, "enc-%s.csv" % "".join(c if c.isalnum() else "-" for c in label), text, codec,
                             newline)
                for method in ("insert", "load_data"):
                    on.admin("DELETE FROM `%s`.`enc`;" % schema)
                    inspected, plan, result = import_file(ws, on, path, enc_target, fmt_override, method=method)
                    got = [r[1] for r in on.rows("SELECT id, HEX(txt) FROM `%s`.`enc` ORDER BY id" % schema)]
                    want = [hexed(w.replace("sep", "sep %s" % sep)) for w in words]
                    if result["outcome"] != "done" or got != want:
                        return False, "%s: detected %s/%r, outcome %s, rows %s" % (
                            method, inspected["detected"]["encoding"], inspected["detected"]["separator"],
                            result["outcome"], got)
                chosen = " (chosen: separator %r, header row %s)" % (
                    inspected["format"]["separator"], inspected["format"]["header"]) if fmt_override else ""
                return True, "detected encoding %s, separator %r, header %s%s; INSERT and LOAD DATA read back %d " \
                             "rows exactly" % (inspected["detected"]["encoding"], inspected["detected"]["separator"],
                                               inspected["detected"]["header"], chosen, len(words))
            scenario(report, "separator/encoding: %s" % label, case)

        # MyISAM: all or nothing refused; commit every N works.
        on.admin("CREATE TABLE `%s`.`plain` (id INT NOT NULL PRIMARY KEY, v VARCHAR(20)) ENGINE=MyISAM;" % schema)
        small = write(files, "small.csv", "id,v\n1,a\n2,b\n3,c\n")
        plain = {"mode": "existing", "schema": schema, "table": "plain",
                 "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "v"}]}

        def myisam():
            _, plan, _ = import_file(ws, on, small, plain, expect_ready=False)
            _, plan2, result = import_file(ws, on, small, plain, transaction={"mode": "every", "every": 2})
            return (not plan["ready"] and any("not transactional" in b for b in plan["blockers"])
                    and result["outcome"] == "done" and count(on, schema, "plain") == 3), \
                "all or nothing: %s; every 2: %s, %d rows" % (plan["blockers"][:1], result["outcome"],
                                                              count(on, schema, "plain"))
        scenario(report, "MyISAM: all or nothing refused with the reason, commit every N works", myisam)

        # Read-only refuses, audited, before any connection opens.
        def read_only():
            connection = {"host": "127.0.0.1", "port": on.port, "user": "root", "password": on.password}
            _, plan, _ = import_file(ws, on, small, {"mode": "new", "schema": schema, "table": "ro_check"},
                                     connection=dict(connection, read_only=True), expect_ready=False)
            opened = api(ws, "/api/import/csv/open-path", {"path": small})
            ready = api(ws, "/api/import/csv/plan", {"connection": connection, "file_id": opened["file_id"],
                                                     "format": None, "target": dict(plain, table="keyed"),
                                                     "method": "insert", "transaction": {"mode": "all"}})
            try:
                api(ws, "/api/import/csv/apply", {"connection": dict(connection, read_only=True),
                                                  "token": ready["token"], "progress_id": "ro" + secrets.token_hex(10)})
                refused = False
            except ValueError as exc:
                refused = "read-only" in str(exc)
            from myflames import dbops
            audit = dbops.audit_report(ws.store, limit=5)["records"][0]
            exists = on.rows("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='%s' AND "
                             "TABLE_NAME='ro_check'" % schema)[0][0]
            return (plan["policy"]["action"] == "refuse" and plan["token"] is None and refused
                    and audit["status"] == "refused" and exists == "0"), \
                "plan policy %s, token %s; apply refused: %s; audit %s/%s" % (
                    plan["policy"]["action"], plan["token"], refused, audit["status"], audit["outcome"])
        scenario(report, "Read-only connection: no token, apply refused and audited", read_only)

        # NO_BACKSLASH_ESCAPES in the server's sql_mode (lab container only).
        def no_backslash():
            mode = on.rows("SELECT @@GLOBAL.sql_mode")[0][0]
            on.admin("SET GLOBAL sql_mode = '%s';" % ",".join([m for m in mode.split(",") if m] +
                                                               ["NO_BACKSLASH_ESCAPES"]))
            try:
                for table in ("nb_insert", "nb_load"):
                    on.admin("CREATE TABLE `%s`.`%s` (id INT NOT NULL PRIMARY KEY, txt TEXT NULL) ENGINE=InnoDB;"
                             % (schema, table))
                path = write(files, "backslash.csv", 'id,txt\n1,"C:\\temp\\new"\n2,"it\'s \\N not null"\n'
                                                     '3,"two\nlines"\n4,\\\\\n')
                outcomes = []
                for table, method in (("nb_insert", "insert"), ("nb_load", "load_data")):
                    _, plan, result = import_file(ws, on, path, {"mode": "existing", "schema": schema,
                                                                 "table": table, "mapping": [
                                                                     {"csv_index": 0, "column": "id"},
                                                                     {"csv_index": 1, "column": "txt"}]},
                                                  {"null_markers": []}, method=method)
                    outcomes.append(result["outcome"])
                got = [r[1] for r in on.rows("SELECT id, HEX(txt) FROM `%s`.`nb_insert` ORDER BY id" % schema)]
                got2 = [r[1] for r in on.rows("SELECT id, HEX(txt) FROM `%s`.`nb_load` ORDER BY id" % schema)]
                want = [hexed("C:\\temp\\new"), hexed("it's \\N not null"), hexed("two\nlines"), hexed("\\\\")]
                return outcomes == ["done", "done"] and got == want and got2 == want, \
                    "outcomes %s; values exact: INSERT %s, LOAD DATA %s" % (outcomes, got == want, got2 == want)
            finally:
                on.admin("SET GLOBAL sql_mode = '%s';" % mode)
        scenario(report, "NO_BACKSLASH_ESCAPES on: backslashes, quotes, newlines exact", no_backslash)

        # The client sends only files from the folder (MySQL clients with
        # --load-data-local-dir): what the LOAD DATA session uses.
        def restricted_client():
            from myflames import csv_import_api
            folder = csv_import_api.chunk_folder()
            outside = write(files, "outside.tsv", "8\tout\n")
            inside = os.path.join(folder, "chunk-000001.tsv")
            with open(inside, "w") as handle:
                handle.write("7\tin\n")
            on.admin("CREATE TABLE `%s`.`files` (id INT NOT NULL PRIMARY KEY, v VARCHAR(10)) ENGINE=InnoDB;" % schema)
            flags, limited = csv_import_api.load_flags({"host": "127.0.0.1", "port": on.port}, folder)
            answers = []
            try:
                for path in (inside, outside):
                    with on.conn() as conn:
                        try:
                            conn.run_stdin("LOAD DATA LOCAL INFILE '%s' INTO TABLE `%s`.`files`" % (path, schema),
                                           extra_flags=list(flags))
                            answers.append("loaded")
                        except Exception as exc:
                            answers.append(str(exc).strip().splitlines()[-1][:150])
            finally:
                import shutil
                shutil.rmtree(folder, ignore_errors=True)
            rows = [r[0] for r in on.rows("SELECT id FROM `%s`.`files` ORDER BY id" % schema)]
            return (limited and answers[0] == "loaded" and answers[1] != "loaded" and rows == ["7"]), \
                "client flags %s; inside the folder: %s; outside: %s; rows %s" % (list(flags), answers[0], answers[1],
                                                                                 rows)
        scenario(report, "LOAD DATA client sends only files from the private folder", restricted_client)

        def case_only_names():
            on.admin("CREATE TABLE `%s`.`CaseT` (a INT) ENGINE=InnoDB; CREATE TABLE `%s`.`caset` (b INT NOT NULL "
                     "PRIMARY KEY, c VARCHAR(5)) ENGINE=InnoDB;" % (schema, schema))
            lctn = on.rows("SELECT @@lower_case_table_names")[0][0]
            path = write(files, "case.csv", "b,c\n1,x\n2,y\n")
            target = {"mode": "existing", "schema": schema, "table": "caset",
                      "mapping": [{"csv_index": 0, "column": "b"}, {"csv_index": 1, "column": "c"}]}
            _, plan, result = import_file(ws, on, path, target)
            columns = [c["name"] for c in plan["target"]["columns"]]
            return (columns == ["b", "c"] and result["outcome"] == "done" and count(on, schema, "caset") == 2
                    and count(on, schema, "CaseT") == 0), "lower_case_table_names %s; columns of caset %s; %s" % (
                lctn, columns, result["outcome"])
        scenario(report, "tables `CaseT` and `caset`: names compare exactly", case_only_names)

        def myisam_load_kept():
            on.admin("CREATE TABLE `%s`.`plain_load` (id INT NOT NULL PRIMARY KEY, v VARCHAR(20)) ENGINE=MyISAM; "
                     "INSERT INTO `%s`.`plain_load` VALUES (3, 'there');" % (schema, schema))
            path = write(files, "plain.csv", "id,v\n1,a\n2,b\n3,c\n4,d\n5,e\n6,f\n")
            target = {"mode": "existing", "schema": schema, "table": "plain_load",
                      "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "v"}]}
            _, plan, result = import_file(ws, on, path, target, method="load_data",
                                          transaction={"mode": "every", "every": 3})
            n = count(on, schema, "plain_load")
            return (result["outcome"] == "failed" and result["rows"]["committed"] == 2 and n == 3), \
                "outcome %s, committed %s, table rows %d (1 before + 2 kept); %s" % (
                    result["outcome"], result["rows"]["committed"], n, result["message"][:150])
        scenario(report, "MyISAM LOAD DATA with a duplicate: the rows kept are reported", myisam_load_kept)

        def triggers_unknown():
            on.admin("CREATE TABLE `%s`.`trig` (id INT NOT NULL PRIMARY KEY, v VARCHAR(10)) ENGINE=InnoDB; "
                     "CREATE TABLE `%s`.`trig_log` (id INT) ENGINE=InnoDB; "
                     "CREATE TRIGGER `%s`.`trig_ai` AFTER INSERT ON `%s`.`trig` FOR EACH ROW "
                     "INSERT INTO `%s`.`trig_log` VALUES (NEW.id); "
                     "CREATE USER 'csvapp'@'%%' IDENTIFIED BY '%s'; "
                     "GRANT SELECT, INSERT ON `%s`.* TO 'csvapp'@'%%';" % (
                         schema, schema, schema, schema, schema, on.password, schema))
            path = write(files, "trig.csv", "id,v\n1,a\n")
            target = {"mode": "existing", "schema": schema, "table": "trig",
                      "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "v"}]}
            app = {"host": "127.0.0.1", "port": on.port, "user": "csvapp", "password": on.password}
            _, plan, result = import_file(ws, on, path, target, connection=app)
            _, plan_root, _ = import_file(ws, on, path, target, plan_only=True)
            warned = any("TRIGGER privilege" in w for w in plan["warnings"])
            # MySQL lists triggers only with TRIGGER; MariaDB also with INSERT,
            # UPDATE or DELETE on the table, so there the import account sees it.
            expected = (None, True) if on.engine == "mysql" else (1, False)
            return ((plan["target"]["triggers"], warned) == expected and plan_root["target"]["triggers"] == 1
                    and result["outcome"] == "done" and count(on, schema, "trig_log") == 1), \
                "account with SELECT, INSERT only: triggers %s, warned %s; root sees %s; the trigger ran %d time(s)" % (
                    plan["target"]["triggers"], warned, plan_root["target"]["triggers"], count(on, schema, "trig_log"))
        scenario(report, "triggers unknown without the TRIGGER privilege", triggers_unknown)

        def server_rules():
            notes = []
            ok = True
            path = write(files, "rules.csv", "n,at\n1,2024-01-01 00:00:00\n")
            target = {"mode": "new", "schema": schema, "table": "rules_new", "add_id_key": False,
                      "new_columns": [{"name": "n", "sql_type": "INT", "nullable": True},
                                      {"name": "at", "sql_type": "TIMESTAMP", "nullable": False}],
                      "mapping": [{"csv_index": 0, "column": "n"}, {"csv_index": 1, "column": "at"}]}
            saved = on.rows("SELECT @@GLOBAL.explicit_defaults_for_timestamp")[0][0]
            on.admin("SET GLOBAL explicit_defaults_for_timestamp = OFF;")
            try:
                _, plan, _ = import_file(ws, on, path, target, expect_ready=False, plan_only=True)
                warned = any("explicit_defaults_for_timestamp is OFF" in w for w in plan["warnings"])
                ok = ok and warned
                notes.append("explicit_defaults_for_timestamp OFF warned: %s" % warned)
            finally:
                on.admin("SET GLOBAL explicit_defaults_for_timestamp = %s;" % ("ON" if saved in ("1", "ON") else "OFF"))
            if on.engine == "mysql":
                on.admin("SET GLOBAL sql_require_primary_key = ON; SET GLOBAL sql_generate_invisible_primary_key = ON;")
                try:
                    _, plan, result = import_file(ws, on, path, target)
                    create = on.rows("SHOW CREATE TABLE `%s`.`rules_new`" % schema)[0][1]
                    gipk = "my_row_id" in create
                    ok = ok and result["outcome"] == "done" and gipk
                    notes.append("sql_require_primary_key + generated invisible key: %s, my_row_id %s"
                                 % (result["outcome"], gipk))
                finally:
                    on.admin("SET GLOBAL sql_require_primary_key = OFF; "
                             "SET GLOBAL sql_generate_invisible_primary_key = OFF;")
            else:
                mode = on.rows("SELECT @@GLOBAL.sql_mode")[0][0]
                on.admin("CREATE TABLE `%s`.`esn` (id INT NOT NULL PRIMARY KEY, v VARCHAR(10) NOT NULL) "
                         "ENGINE=InnoDB; SET GLOBAL sql_mode = '%s';" % (
                             schema, ",".join([m for m in mode.split(",") if m] + ["EMPTY_STRING_IS_NULL"])))
                try:
                    empty = write(files, "esn.csv", "id,v\n1,\n2,x\n")
                    esn = {"mode": "existing", "schema": schema, "table": "esn",
                           "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "v"}]}
                    _, plan, result = import_file(ws, on, empty, esn, {"empty": "empty"})
                    values = [r[1] for r in on.rows("SELECT id, IFNULL(HEX(v), 'N') FROM `%s`.`esn` ORDER BY id"
                                                    % schema)]
                    noted = any("EMPTY_STRING_IS_NULL" in n for n in plan["notes"])
                    ok = ok and result["outcome"] == "done" and values == ["", "78"] and noted
                    notes.append("EMPTY_STRING_IS_NULL on: noted %s, outcome %s, values %s"
                                 % (noted, result["outcome"], values))
                finally:
                    on.admin("SET GLOBAL sql_mode = '%s';" % mode)
            return ok, "; ".join(notes)
        scenario(report, "server settings: explicit_defaults_for_timestamp, invisible primary key or "
                         "EMPTY_STRING_IS_NULL", server_rules)

        if on.engine == "mariadb":
            _, out, _ = run(["docker", "exec", on.name, "sh", "-c",
                             "mariadb --help 2>/dev/null | grep -i -E 'local-infile|load-data-local-dir' || true"],
                            60, check=False)
            report.observations.append("The MariaDB client in the image offers: %s (no --load-data-local-dir, so a "
                                       "MariaDB client would run LOAD DATA with --local-infile=1)."
                                       % (" / ".join(l.strip() for l in out.splitlines() if l.strip()) or "nothing"))

        on.stop()
        # local_infile OFF: LOAD DATA is not offered, INSERT works.
        log("%s: starting %s (local_infile OFF)" % (key, off.image))
        off.start()
        report.observations.append("Started with --local-infile=0: @@local_infile = %s." % off.server_local_infile)
        off.admin("CREATE DATABASE %s; CREATE TABLE `%s`.`t` (id INT NOT NULL PRIMARY KEY, v VARCHAR(20)) "
                  "ENGINE=InnoDB;" % (schema, schema))
        t_off = {"mode": "existing", "schema": schema, "table": "t",
                 "mapping": [{"csv_index": 0, "column": "id"}, {"csv_index": 1, "column": "v"}]}

        def infile_off():
            _, plan, _ = import_file(ws, off, small, t_off, method="load_data", expect_ready=False)
            _, plan2, result = import_file(ws, off, small, t_off)
            # What the server says to a LOAD DATA LOCAL anyway (lab client only).
            path = write(files, "direct.tsv", "9\tz\n")
            with off.conn() as conn:
                try:
                    conn.run_stdin("LOAD DATA LOCAL INFILE '%s' INTO TABLE `%s`.`t`" % (path, schema),
                                   extra_flags=["--local-infile=1"])
                    server_says = "accepted"
                except Exception as exc:
                    server_says = str(exc).strip().splitlines()[-1][:160]
            report.observations.append("local_infile OFF, LOAD DATA LOCAL sent by a client with --local-infile=1: %s"
                                       % server_says)
            return (not plan["ready"] and plan["method"]["load_data_available"] is False and
                    plan["method"]["local_infile"] is False and result["outcome"] == "done"
                    and count(off, schema, "t") == 3), "LOAD DATA blocked (%s); INSERT %s; server: %s" % (
                plan["method"]["why"][:60], result["outcome"], server_says)
        scenario(report, "local_infile OFF: LOAD DATA not offered, INSERT works", infile_off)
    finally:
        on.stop()
        off.stop()
    return report


# ---- report -------------------------------------------------------------------------------------

#: Appended to the report: what the scenarios check about the servers, and
#: what earlier runs of this lab changed in the code.
SERVER_BEHAVIOUR = """## What ran

Per server: two containers (local_infile ON for the matrix, OFF for the refusal), database `csvlab`, the
scenarios above. Each import went open-path, inspect, plan, apply (and progress and stop where named) through
`ui.api_response`, the dispatch the desktop app uses, with an unsaved connection (Production, so every import is
reviewed) and a workspace store in a temporary HOME. Every answer was checked against docs/schemas/csv-*.json.
Rows were read back with `SELECT COUNT(*)` and `HEX(CAST(col AS BINARY))` per value, so NULL, an empty string, a
quoted newline, a backslash, an emoji and the largest BIGINT UNSIGNED were compared byte for byte, and the
INSERT and LOAD DATA copies of the typed table row by row.

## Server behaviour the scenarios check, and what the code does about it

- LOAD DATA LOCAL is IGNORE on both engines (mysql-server sql/parse_tree_nodes.cc, mariadb-server
  sql/sql_load.cc): a duplicate key is skipped with warning 1062 and the statement succeeds with one row fewer.
  myflames loads a checked copy, reads SHOW WARNINGS after every LOAD DATA, compares the affected rows with the
  rows it wrote and rolls back on any difference. On a MyISAM table nothing can be rolled back: the result
  reports the rows that stay (2 of 3 here). INSERT fails the statement instead (1062); InnoDB rolls back only
  that statement, which Skip and report relies on, and which lets myflames find the exact refused row of an
  all-or-nothing statement by sending its rows one at a time before rolling back (line 251, not the
  statement's first line 2).
- The LOAD DATA session runs the MySQL client with --local-infile=0 and --load-data-local-dir set to the private
  folder of checked copies: a file there loads; a file outside it is refused by the client (2068 "LOAD DATA LOCAL
  INFILE file request rejected due to restrictions on access"). The MariaDB client has no such option (its
  --help lists only --local-infile), so with it the plan warns that the session would send any file the server
  asks for.
- A KILL QUERY that reaches the server as LOAD DATA finishes does nothing (both servers clear a pending KILL QUERY
  at the next command); Stop is checked again right before every COMMIT and rolls back instead.
- A connection the server ends mid-import (KILL CONNECTION from the lab) makes the client print a lost-connection
  error: the outcome is unknown, never retried, and the table held no row afterwards (InnoDB rolled back the
  uncommitted transaction).
- local_infile OFF answers MySQL 3948 ("Loading local data is disabled ...") and MariaDB 4166 ("... has disabled
  the local infile capability"). myflames reads @@GLOBAL.local_infile at plan and at apply, never sends LOAD DATA
  while it is OFF and never changes it. Defaults: MySQL OFF, MariaDB ON (sql/sys_vars.cc Sys_local_infile).
- information_schema compares names case- and accent-insensitively against a utf8mb4 literal; with
  lower_case_table_names = 0 myflames compares with COLLATE utf8mb4_bin, so tables `CaseT` and `caset` stay
  apart.
- Triggers: MySQL lists them in information_schema.TRIGGERS only to an account with TRIGGER on the table, so the
  plan says unknown and warns; MariaDB also lists them to an account with INSERT, UPDATE or DELETE
  (sql/sql_show.cc), so the import account sees them there.
- MariaDB's JSON column is LONGTEXT with CHECK (json_valid(`col`)); myflames reads CHECK_CONSTRAINTS to treat it
  as JSON. EMPTY_STRING_IS_NULL in MariaDB's sql_mode is taken out of the import session, so an empty string
  stays one. With explicit_defaults_for_timestamp OFF the plan warns that the created table gets automatic
  TIMESTAMP defaults; with sql_generate_invisible_primary_key ON (MySQL) a table without a key is allowed and
  gets my_row_id.
- A TIMESTAMP after 2038-01-19 passes myflames' year check and the server refuses it in strict mode (1292 "... at
  row 2", mapped to CSV line 3). CREATE TABLE committed on its own, so the new table stays, empty, and the
  result carries the DROP TABLE text (never sent).
- A custom separator such as `^` is not a detection candidate; inspect decides the header row again whenever the
  request names a separator, quote or encoding without a header choice.

## Not run here

Percona Server, MySQL 8.0, MariaDB 11.5 and later (TIMESTAMP up to 2106 there; a unit test only), NDB and MyRocks
(their rules are unit tests), connections through SSH, the MariaDB client itself as the import client, and the
Windows file chooser."""


def write_report(path, reports, started, pulled):
    lines = ["# Import CSV lab results", "",
             "Run on %s by `scripts/lab-csv-import.py` (token `%s`): throwaway containers from %s, the imports sent "
             "through the real workspace API (`/api/import/csv/*`) with the host's `mysql` client (%s). Rows were "
             "read back with SELECT (hex for exact values)." % (started, TOKEN, ECR, client_version()), ""]
    for report in reports:
        passed = sum(1 for _, ok, _ in report.rows if ok)
        lines += ["## %s (%s): %d of %d passed" % (report.key, getattr(report, "version", "?"), passed,
                                                    len(report.rows)), "",
                  "| Scenario | Result | Details |", "| --- | --- | --- |"]
        for name, ok, detail in report.rows:
            lines.append("| %s | %s | %s |" % (name, "PASS" if ok else "FAIL",
                                               str(detail).replace("|", "\\|").replace("\n", " ")))
        if report.observations:
            lines += ["", "Observations:", ""] + ["- " + o for o in report.observations]
        lines.append("")
    lines += ["Images pulled by this run and removed after it: %s." % (", ".join(sorted(pulled)) or "none"), ""]
    lines += [SERVER_BEHAVIOUR, ""]
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def client_version():
    try:
        _, out, _ = run(["mysql", "--version"], 30)
        return out.strip()
    except LabError:
        return "unknown"


def main():
    parser = argparse.ArgumentParser(description="Import CSV lab against throwaway MySQL and MariaDB containers.")
    parser.add_argument("--servers", nargs="+", default=list(SERVERS), choices=list(SERVERS))
    parser.add_argument("--out", default=DEFAULT_OUT, help="Markdown report path (default: %(default)s)")
    parser.add_argument("--json", help="Also write the results as JSON here.")
    parser.add_argument("--keep-images", action="store_true", help="Keep images this run pulled.")
    args = parser.parse_args()
    home = tempfile.mkdtemp(prefix="myflames-csvlab-home-")
    os.environ.update(HOME=home, USERPROFILE=home, MYFLAMES_SECRET_BACKEND="vault")
    os.environ.pop("MYFLAMES_VAULT_PASSPHRASE", None)
    sys.path.insert(0, REPO)
    workdir = tempfile.mkdtemp(prefix="myflames-csvlab-")
    started = time.strftime("%Y-%m-%d %H:%M %Z")
    before = images()
    pulled = set()
    reports = []
    try:
        ws = Workspace()
        for key in args.servers:
            engine, version = SERVERS[key]
            ref = ECR + "%s:%s" % (engine, version)
            if ref not in before:
                log("pulling %s" % ref)
                run(["docker", "pull", "-q", ref], 1800)
                pulled.add(ref)
            reports.append(run_server(key, args, workdir, ws))
            write_report(args.out, reports, started, pulled)
    finally:
        for name in list(_ACTIVE):
            run(["docker", "rm", "-f", "-v", name], 120, check=False)
        if not args.keep_images:
            for ref in pulled:
                run(["docker", "rmi", ref], 300, check=False)
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
    write_report(args.out, reports, started, pulled if not args.keep_images else set())
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump([{"server": r.key, "version": getattr(r, "version", None),
                        "results": [{"scenario": n, "pass": ok, "detail": d} for n, ok, d in r.rows],
                        "observations": r.observations} for r in reports], handle, indent=2)
    failed = sum(1 for r in reports for _, ok, _ in r.rows if not ok)
    log("done: %d failures; report %s" % (failed, args.out))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
