"""Independent, bounded client/server lexers for comment differential tests.

This is a test-only transcription of the state transitions, not a SQL parser
or a replacement for live client tests. Inputs are ASCII, --comments and
noninteractive --binary-mode are enabled, sql_mode is empty, and the private
delimiter has already been selected. Named commands, dollar quotes, charset
conversion and SQL execution are outside this oracle's domain.

Client sources (the branch order matters):
  mysql-8.4.0:client/mysql.cc:2735-2741,2771-2823,2824-2986;
  mariadb-11.8.9:client/mysql.cc:2613-2620,2659-2727,2729-2900.
Both reset ss_comment on each line but retain ml_comment and in_string.
MySQL has SSC_HINT; MariaDB 11.8 treats /*+ as an ordinary comment.

Server sources for the separate statement counter:
  mysql-8.4.0:sql/sql_lex.cc:1037-1066,1311-1344,1900-2017;
  mariadb-11.8.9:sql/sql_lex.cc:1639-1694,1824-1861,2401-2562.
Ordinary/hint comments end at the first */; skipped conditional comments
permit one nested level, and executed conditional bodies are SQL.
"""
import re


SPACE = " \t\r\n\f\v"
FRAME = "__oracle_end__"


def client_packets(sql, family, delimiter=FRAME):
    """Return COM_QUERY texts, remaining buffer, and executed short commands.

    Feed exactly the editor's <send> newline <private delimiter> newline
    frame. remove_cntrl trims trailing ASCII whitespace before COM_QUERY
    (mysql-8.4.0:client/mysql.cc:3639-3641,5454-5458;
    mariadb-11.8.9:client/mysql.cc:3614,5522-5529).
    """
    if family not in ("mysql", "mariadb") or not sql.isascii() or "$" in sql:
        raise ValueError("Outside the ASCII comment oracle's domain")
    buffer, packets, commands = "", [], []
    ml_comment, in_string = False, ""
    for line in (sql + "\n" + delimiter + "\n").split("\n")[:-1]:
        ss_comment, need_space, pos = 0, False, 0
        while pos < len(line):
            ch = line[pos]
            tail = line[pos:]
            # mysql:2771-2823; mariadb:2659-2727. Only \C takes
            # parameters in this domain. Disabled commands are copied.
            if not ml_comment and ch == "\\" and not (family == "mariadb" and in_string == "`"):
                pos += 1
                following = line[pos:pos + 1]
                if not following:
                    if family == "mariadb" and in_string:
                        buffer += "\\\x00"
                    break
                if in_string or following == "N":
                    buffer += "\\"
                    if following == "`" and in_string == "`" and family == "mysql":
                        continue  # MySQL reexamines this backtick.
                    buffer += following
                    pos += 1
                    continue
                if following == "C":
                    commands.append("C")
                    pos += 1
                    stop = line.find("*/" if ss_comment else delimiter, pos)
                    pos = len(line) if stop < 0 else stop + (0 if ss_comment else len(delimiter))
                    continue
                if following == "-" and family == "mariadb":
                    commands.append("-")
                    pos += 1
                    continue
                buffer += "\\" + following
                pos += 1
                continue
            # mysql:2824-2858; mariadb:2729-2771.
            if not ml_comment and not in_string and ss_comment != 2 and tail.startswith(delimiter):
                pos += len(delimiter)
                while pos < len(line) and line[pos] in SPACE:
                    buffer += line[pos]
                    pos += 1
                rest = line[pos:]
                if rest.startswith("#") or (rest.startswith("--") and rest[2:3] in SPACE and rest[2:3]):
                    buffer += rest
                    pos = len(line)
                if buffer.rstrip(SPACE):
                    packets.append(buffer.rstrip(SPACE))
                buffer = ""
                continue
            # mysql:2859-2894 predates the hint #/-- fix. MariaDB:2772-2815.
            dash = tail.startswith("--") and (len(tail) == 2 or tail[2] in SPACE or
                                               (family == "mariadb" and not buffer))
            if not ml_comment and not in_string and (ch == "#" or dash):
                empty = not buffer
                buffer += tail
                if empty:
                    packets.append(buffer.rstrip(SPACE))
                    buffer = ""
                break
            # No !ml_comment guard here. Consuming /* hides the overlapping
            # */ in /*/ from the next iteration. mysql:2895-2906;
            # mariadb:2817-2833. Do not "correct" this client behavior.
            ordinary = tail.startswith("/*") and not tail.startswith("/*!")
            if family == "mysql":
                ordinary = ordinary and not tail.startswith("/*+") and ss_comment != 2
            else:
                ordinary = ordinary and not tail.startswith("/*M!")
            if not in_string and ordinary:
                buffer += "/*"
                pos += 2
                ml_comment = True
                continue
            # mysql:2907-2921; mariadb:2834-2851.
            if ml_comment and not ss_comment and tail.startswith("*/"):
                buffer += "*/"
                pos += 2
                ml_comment, need_space = False, True
                continue
            # mysql:2922-2963; mariadb:2853-2875. Conditional open/close
            # examines one byte per iteration, unlike ordinary comments.
            if not in_string and tail.startswith("/*!"):
                ss_comment = 1
            elif not in_string and tail.startswith("/*+") and family == "mysql":
                ss_comment = 2
            elif not in_string and ss_comment and tail.startswith("*/"):
                ss_comment = 0
            if ch == in_string:
                in_string = ""
            elif not ml_comment and not in_string and ss_comment != 2 and ch in "'\"`":
                in_string = ch
            if need_space and ch not in SPACE:
                buffer += " "
            need_space = False
            buffer += ch
            pos += 1
        if buffer:
            buffer += "\n"
    return packets, buffer, commands


def server_statement_count(sql, family):
    """Count lexical statements independently, with known 8.4/11.8 facts.

    This does not decide whether those statements are valid SQL. It checks
    comment/quote boundaries and all semicolon-separated parts exposed to
    CLIENT_MULTI_STATEMENTS, rather than borrowing myflames tokens. Syntax
    validation and execution/error behavior remain outside this oracle.
    """
    version = 80400 if family == "mysql" else 110809
    count, pending, pos, active = 0, False, 0, False
    while pos < len(sql):
        ch = sql[pos]
        if active and sql.startswith("*/", pos):
            active = False
            pos += 2
        elif ch in "'\"`":
            pending = True
            quote = ch
            pos += 1
            while pos < len(sql):
                if sql[pos] == quote:
                    pos += 1
                    if sql[pos:pos + 1] == quote:
                        pos += 1
                        continue
                    break
                pos += 2 if sql[pos] == "\\" and quote != "`" else 1
        elif ch == "#" or (sql.startswith("--", pos) and
                            (pos + 2 == len(sql) or sql[pos + 2] in SPACE)):
            end = sql.find("\n", pos)
            pos = len(sql) if end < 0 else end
        elif sql.startswith("/*", pos):
            maria = sql.startswith("/*M!", pos)
            executable = sql.startswith("/*!", pos) or (maria and family == "mariadb")
            start = pos + (4 if maria else 3)
            match = re.match(r"[0-9]+", sql[start:]) if executable else None
            gate = int(match.group()) if match else 0
            body = start + (len(match.group()) if match else 0)
            executes = executable and gate <= version and not (
                family == "mariadb" and not maria and 50700 <= gate <= 99999)
            if executes:
                pos, active = body, True
                continue
            pos += 2
            # A skipped executable body permits one nested ordinary level.
            while pos < len(sql) and not sql.startswith("*/", pos):
                if executable and sql.startswith("/*", pos):
                    end = sql.find("*/", pos + 2)
                    pos = len(sql) if end < 0 else end + 2
                else:
                    pos += 1
            pos += 2
        elif ch == ";":
            count += int(pending)
            pending = False
            pos += 1
        else:
            pending = pending or ch not in SPACE
            pos += 1
    return count + int(pending)
