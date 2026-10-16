"""A stand-in for the mysql/mariadb command-line client, for editor session tests.

test_editor_session.py copies this file into a temporary directory as an
executable named ``mysql`` and puts that directory first on PATH, so the
editor's real code starts it like the real client. It reproduces what the
editor relies on, as captured from MySQL 8.4 and MariaDB 11.4 clients (see
test/fixtures/editor/): ``-vv`` statement echoes between ``--------------``
lines, XML result sets (NULL as ``xsi:nil``, binary as 0x hex), ``N rows in
set`` / ``Query OK`` summary lines with the server's info line, errors on
stderr as ``ERROR <code> (<sqlstate>) at line <n>: ...``, the bare
``delimiter`` line answered with ``ERROR at line <n>: DELIMITER must be
followed by ...``, and ``--force`` behavior (the client keeps going after an
error). FAKE_ENGINE=mariadb also reproduces the MariaDB client's habit of
printing an error before flushing the echo of the failed statement.

A tiny engine behind it keeps per-session state (user variables, temporary
tables, autocommit, a transaction, sql_safe_updates, read-only transactions)
so session affinity can be tested. ``-e "KILL QUERY <id>"`` (the editor's
second connection) drops a file that the session process checks while it
runs SLEEP() or streams the big result.

Environment: FAKE_STATE (directory, required), FAKE_ENGINE (mysql or
mariadb), FAKE_PS (off: the transaction probe cannot tell), FAKE_COMMANDS
(1: --help lists --commands), FAKE_DENY (1: the connection is refused).
"""
import os
import re
import sys
import time

STATE = os.environ.get("FAKE_STATE", ".")
ENGINE = os.environ.get("FAKE_ENGINE", "mysql")
MARKER = "DELIMITER must be followed by a 'delimiter' character or string"


def log_argv():
    with open(os.path.join(STATE, "argv.log"), "a") as handle:
        handle.write(repr(sys.argv[1:]) + "\n")
        for arg in sys.argv[1:]:
            if arg.startswith("--defaults-extra-file="):
                path = arg.split("=", 1)[1]
                handle.write("defaults-exists=%s\n" % os.path.exists(path))


def next_connection_id():
    path = os.path.join(STATE, "conn-counter")
    value = int(open(path).read()) + 1 if os.path.exists(path) else 41
    with open(path, "w") as handle:
        handle.write(str(value))
    return value


def xml(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("\0", " "))


class Client(object):
    def __init__(self):
        self.conn_id = next_connection_id()
        self.out = []
        self.vars = {}
        self.tables = {}
        self.autocommit = True
        self.in_trx = False
        self.trx_seen = False
        self.snapshot = None
        self.safe_updates = False
        self.read_only = False
        self.sql_mode = "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION"
        self.lost = False
        with open(os.path.join(STATE, "session-%d.log" % self.conn_id), "w"):
            pass

    # -- output ----------------------------------------------------------------------
    def write(self, text):
        self.out.append(text)

    def flush(self):
        if self.out:
            sys.stdout.write("".join(self.out))
            self.out = []
        sys.stdout.flush()

    def error(self, line, code=None, state=None, message=""):
        if ENGINE == "mysql":
            self.flush()
        head = "ERROR"
        if code is not None:
            head += " %d (%s)" % (code, state or "HY000")
        sys.stderr.write("%s at line %d: %s\n" % (head, line, message))
        sys.stderr.flush()

    def result(self, statement, columns, rows, warnings=0):
        self.write('<?xml version="1.0"?>\n\n<resultset statement="%s" '
                   'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' % xml(statement))
        for row in rows:
            self.write("\n  <row>\n")
            for name, value in zip(columns, row):
                if value is None:
                    self.write('\t<field name="%s" xsi:nil="true" />\n' % xml(name))
                else:
                    self.write('\t<field name="%s">%s</field>\n' % (xml(name), xml(value)))
            self.write("  </row>\n")
        self.write("</resultset>\n")
        self.summary("%d %s in set%s" % (len(rows), "row" if len(rows) == 1 else "rows", self.warn(warnings)))

    def warn(self, count):
        return ", %d warning%s" % (count, "" if count == 1 else "s") if count else ""

    def summary(self, line, info=None):
        self.write(line + "\n")
        if info:
            self.write(info + "\n")
        self.write("\n")
        self.flush()

    def ok(self, affected=0, info=None, warnings=0):
        self.summary("Query OK, %d %s affected%s" % (affected, "row" if affected == 1 else "rows",
                                                    self.warn(warnings)), info)

    def killed(self):
        path = os.path.join(STATE, "kill-%d" % self.conn_id)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def log(self, statement):
        with open(os.path.join(STATE, "session-%d.log" % self.conn_id), "a") as handle:
            handle.write(statement.replace("\n", "\\n") + "\n")

    # -- barriers --------------------------------------------------------------------
    def gate(self, honor_kill=True):
        """Block until the test drops release-<id> (True) or, when
        *honor_kill*, a KILL QUERY arrives (False). No timing involved."""
        release = os.path.join(STATE, "release-%d" % self.conn_id)
        while True:
            if os.path.exists(release):
                os.remove(release)
                return True
            if honor_kill and self.killed():
                return False
            time.sleep(0.005)

    # -- the engine ------------------------------------------------------------------
    def run(self, statement, line):
        self.write("--------------\n%s\n--------------\n\n" % statement)
        self.log(statement)
        if self.lost:
            self.error(line, 2006, "HY000", "MySQL server has gone away")
            return
        text = re.sub(r"^/\*myflames:editor\*/ ", "", statement)
        gate = re.search(r"\s*/\*gate(:nokill)?\*/", text)
        if gate:
            # Flush what is printed so far (the echo), like a statement that runs.
            self.flush()
            text = text[:gate.start()] + text[gate.end():]
            if not self.gate(honor_kill=not gate.group(1)):
                self.error(line, 1317, "70100", "Query execution was interrupted")
                return
        if text.upper() == "CALL ROWS_THEN_WRITE()":
            # A procedure that returns rows, then writes: the row limit must not KILL it.
            self.write('<?xml version="1.0"?>\n\n<resultset statement="%s" '
                       'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' % xml(statement))
            for n in range(1, 151):
                self.write("\n  <row>\n\t<field name=\"n\">%d</field>\n  </row>\n" % n)
            self.write("</resultset>\n")
            self.summary("150 rows in set")
            if not self.gate():
                self.error(line, 1317, "70100", "Query execution was interrupted")
                return
            self.tables.setdefault("written", []).append(["1", "by the procedure"])
            self.ok(1)
            return
        if text.upper() == "SELECT SPOOF_ERROR()":
            # Table data quoted in an error message that looks like a lost connection.
            self.error(line, 1292, "22007", "Truncated incorrect DOUBLE value: 'abc\nERROR 2013 (HY000) at line %d: "
                                            "Lost connection to MySQL server during query'" % line)
            return
        upper = text.upper()
        if "CONNECTION_ID() AS CONNECTION_ID" in upper:
            names = re.findall(r"\bAS (\w+)", text)
            if "in_transaction" in names and ENGINE == "mysql":
                self.error(line, 1193, "HY000", "Unknown system variable 'in_transaction'")
                return
            token = re.search(r"SELECT '([^']*)' AS myflames_editor", text)
            values = {"myflames_editor": token.group(1) if token else "", "connection_id": str(self.conn_id),
                      "version": "11.4.13-MariaDB-fake" if ENGINE == "mariadb" else "8.4.11-fake",
                      "autocommit": "1" if self.autocommit else "0", "sql_mode": self.sql_mode,
                      "sql_safe_updates": "1" if self.safe_updates else "0",
                      "in_transaction": "1" if self.in_trx else "0"}
            self.result(statement, names, [[values.get(n) for n in names]])
            return
        if "PERFORMANCE_SCHEMA.SETUP_CONSUMERS" in upper:
            if ENGINE == "mariadb":
                self.error(line, 1305, "42000", "FUNCTION PS_CURRENT_THREAD_ID does not exist")
                return
            on = "NO" if os.environ.get("FAKE_PS") == "off" else "YES"
            state = "ACTIVE" if self.in_trx else ("COMMITTED" if self.trx_seen else None)
            self.result(statement, ["consumer", "instrument", "instrumented", "state"], [[on, "YES", "YES", state]])
            return
        m = re.match(r"SET SESSION sql_safe_updates = (\d)$", text)
        if m:
            self.safe_updates = m.group(1) == "1"
            self.ok()
            return
        if upper == "SET SESSION TRANSACTION READ ONLY":
            self.read_only = True
            self.ok()
            return
        m = re.match(r"SET (?:SESSION )?autocommit\s*=\s*(\d)$", text, re.I)
        if m:
            self.autocommit = m.group(1) == "1"
            self.ok()
            return
        m = re.match(r"SET (?:SESSION )?sql_mode\s*=\s*'([^']*)'$", text, re.I)
        if m:
            self.sql_mode = m.group(1)
            self.ok()
            return
        m = re.match(r"SET @(\w+)\s*=\s*(.+)$", text)
        if m:
            self.vars[m.group(1)] = m.group(2).strip("'")
            self.ok()
            return
        m = re.match(r"SELECT @(\w+)(?: AS (\w+))?$", text)
        if m:
            self.result(statement, [m.group(2) or "@" + m.group(1)], [[self.vars.get(m.group(1))]])
            return
        if upper in ("START TRANSACTION", "BEGIN"):
            self.begin()
            self.ok()
            return
        if upper.endswith("COMMIT"):
            self.in_trx, self.snapshot = False, None
            self.ok()
            return
        if upper.endswith("ROLLBACK"):
            if self.snapshot is not None:
                self.tables = self.snapshot
            self.in_trx, self.snapshot = False, None
            self.ok()
            return
        m = re.match(r"CREATE TEMPORARY TABLE (\w+)", text, re.I)
        if m:
            self.tables[m.group(1)] = []
            self.ok()
            return
        m = re.match(r"INSERT INTO (\w+) VALUES (.+)$", text, re.I | re.S)
        if m:
            if self.read_only:
                self.error(line, 1792, "25006", "Cannot execute statement in a READ ONLY transaction.")
                return
            if m.group(1) not in self.tables:
                self.error(line, 1146, "42S02", "Table 'test.%s' doesn't exist" % m.group(1))
                return
            self.touch()
            rows = [[None if v.strip() == "NULL" else v.strip().strip("'") for v in group.split(",")]
                    for group in re.findall(r"\(([^)]*)\)", m.group(2))]
            self.tables[m.group(1)].extend(rows)
            self.ok(len(rows), "Records: %d  Duplicates: 0  Warnings: 0" % len(rows))
            return
        m = re.match(r"UPDATE (\w+) SET (\w+) = '([^']*)'(?: WHERE id = (\d+))?$", text, re.I)
        if m:
            if self.safe_updates and not m.group(4):
                self.error(line, 1175, "HY000", "You are using safe update mode and you tried to update a table "
                                                "without a WHERE that uses a KEY column. ")
                return
            self.touch()
            changed = 0
            for row in self.tables.get(m.group(1), []):
                if not m.group(4) or row[0] == m.group(4):
                    row[1] = m.group(3)
                    changed += 1
            self.ok(changed, "Rows matched: %d  Changed: %d  Warnings: 0" % (changed, changed))
            return
        m = re.match(r"SELECT \* FROM (\w+)$", text, re.I)
        if m and m.group(1) in self.tables:
            self.result(statement, ["id", "v"], self.tables[m.group(1)])
            return
        if re.match(r"SELECT \* FROM nope\b", text, re.I):
            self.error(line, 1146, "42S02", "Table 'test.nope' doesn't exist")
            return
        if upper.startswith("SELEC "):
            near = text[:80]
            self.error(line, 1064, "42000", "You have an error in your SQL syntax; check the manual that corresponds "
                                            "to your MySQL server version for the right syntax to use near '%s' at "
                                            "line 1" % near)
            return
        if upper == "SELECT LOSE_CONNECTION()":
            self.lost = True
            self.error(line, 2013, "HY000", "Lost connection to MySQL server during query")
            return
        m = re.match(r"SELECT SLEEP\((\d+(?:\.\d+)?)\)", text, re.I)
        if m:
            end = time.time() + float(m.group(1))
            value = "0"
            while time.time() < end:
                if self.killed():
                    value = "1"
                    break
                time.sleep(0.01)
            self.result(statement, ["SLEEP(%s)" % m.group(1)], [[value]])
            return
        m = re.match(r"SELECT n FROM big_table(?: LIMIT (\d+))?$", text, re.I)
        if m:
            self.big(statement, line, int(m.group(1) or 200000))
            return
        if upper == "SELECT SPECIAL":
            self.result(statement, ["n", "e", "s", "b", "big", "dt", "esc", "ml", "dup", "dup"],
                        [[None, "", "NULL", "0x00FF41", "18446744073709551615", "2024-01-02 03:04:05",
                          'a<b>&"c"', "line1\nline2", "1", "2"]])
            return
        if upper == "SELECT 1/0":
            self.result(statement, ["1/0"], [[None]], warnings=1)
            return
        if upper.startswith("CALL TWO"):
            self.write('<?xml version="1.0"?>\n\n<resultset statement="%s" '
                       'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n  <row>\n\t<field name="one">1'
                       '</field>\n  </row>\n</resultset>\n' % xml(statement))
            self.summary("1 row in set")
            self.write('<?xml version="1.0"?>\n\n<resultset statement="%s" '
                       'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n  <row>\n\t<field name="two">2'
                       '</field>\n  </row>\n</resultset>\n' % xml(statement))
            self.summary("1 row in set")
            self.ok()
            return
        if upper.startswith("SELECT EMPTY"):
            self.result(statement, [], [])
            return
        m = re.match(r"SELECT (-?\d+)(?: AS (\w+))?$", text, re.I)
        if m:
            self.result(statement, [m.group(2) or m.group(1)], [[m.group(1)]])
            return
        self.ok()

    def begin(self):
        self.in_trx = True
        self.trx_seen = True
        self.snapshot = {k: [list(r) for r in v] for k, v in self.tables.items()}

    def touch(self):
        if not self.autocommit and not self.in_trx:
            self.begin()
        if self.in_trx or not self.autocommit:
            self.trx_seen = True

    def big(self, statement, line, count):
        self.write('<?xml version="1.0"?>\n\n<resultset statement="%s" '
                   'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' % xml(statement))
        sent = 0
        interrupted = False
        for n in range(1, count + 1):
            self.write("\n  <row>\n\t<field name=\"n\">%d</field>\n  </row>\n" % n)
            sent = n
            if n % 50 == 0:
                self.flush()
                if self.killed():
                    interrupted = True
                    break
                time.sleep(0.001)
        self.write("</resultset>\n")
        if interrupted:
            self.flush()
            self.error(line, 1317, "70100", "Query execution was interrupted")
        self.summary("%d rows in set" % sent)


def one_shot(sql):
    m = re.search(r"KILL QUERY (\d+)", sql)
    if m:
        with open(os.path.join(STATE, "kill-%s" % m.group(1)), "w") as handle:
            handle.write("1")
        with open(os.path.join(STATE, "kills.log"), "a") as handle:
            handle.write(sql + "\n")
    return 0


def main():
    args = sys.argv[1:]
    if "--help" in args:
        print("mysql  Ver fake")
        if os.environ.get("FAKE_COMMANDS") == "1":
            print("  --commands          Enable or disable processing of local mysql commands.")
        return 0
    log_argv()
    if "-e" in args:
        return one_shot(args[args.index("-e") + 1])
    if os.environ.get("FAKE_DENY") == "1":
        sys.stderr.write("ERROR 1045 (28000): Access denied for user 'app'@'localhost' (using password: YES)\n")
        return 1
    client = Client()
    delimiter = ";"
    buffer = []
    start_line = 0
    number = 0
    stdin = sys.stdin.buffer
    while True:
        raw = stdin.readline()
        if not raw:
            break
        number += 1
        line = raw.decode("utf-8").rstrip("\n")
        if not buffer:
            start_line = number
            match = re.match(r"^delimiter(?:\s+(\S+))?\s*$", line, re.I)
            if match:
                if match.group(1):
                    delimiter = match.group(1)
                else:
                    client.error(number, message=MARKER)
                continue
        if line == delimiter:
            statement = "\n".join(buffer).rstrip()
            buffer = []
            if statement:
                client.run(statement, start_line)
            continue
        buffer.append(line)
    client.write("Bye\n")
    client.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
