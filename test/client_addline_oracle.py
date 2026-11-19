"""Independent, bounded client/server lexers for comment differential tests.

This is a test-only transcription of the state transitions, not a SQL parser
or a replacement for live client tests. Inputs are ASCII, --comments and
noninteractive --binary-mode are enabled, sql_mode is empty, and the private
delimiter has already been selected. The binary-mode DELIMITER command is
modelled; other named commands are disabled by those flags. MySQL 8.1+
dollar quotes are modelled with the server capability probe enabled.
Charset conversion and SQL execution are outside this oracle's domain.

Client sources (the branch order matters):
  mysql-8.4.0:client/mysql.cc:2735-2741,2771-2823,2824-2986;
  mariadb-11.8.9:client/mysql.cc:2613-2620,2659-2727,2729-2900.
Both reset ss_comment on each line but retain ml_comment and in_string.
MySQL has SSC_HINT; MariaDB 11.8 treats /*+ as an ordinary comment. The
optional client_version also selects MySQL's pre-8.2 empty-buffer command
lookup and MariaDB 12.0.1's persistent special-comment state and atomic
closer for /*! and /*+ (not /*M!). See the source references below.

Server sources for the separate statement counter:
  mysql-8.4.0:sql/sql_lex.cc:1037-1066,1311-1344,1900-2017;
  mariadb-11.8.9:sql/sql_lex.cc:1639-1694,1824-1861,2401-2562.
Ordinary/hint comments end at the first */; skipped conditional comments
permit one nested level, and executed conditional bodies are SQL.
"""
import re


SPACE = " \t\r\n\f\v"
FRAME = "__oracle_end__"


def skip_over_comments_and_space(text):
    """Transcribe the MySQL 8.2+ lookup, including its unusual loop bounds.

    mysql-8.4.0:client/mysql.cc:2577-2593;
    mysql-9.7.0:client/mysql.cc:2591-2607. Do not replace this with a SQL
    comment parser: an unclosed opener followed by trailing whitespace can
    consume the whole buffer, while a '*' or '/' can stop the inner loop.
    """
    pos, end = 0, len(text)
    while True:
        while pos < end and text[pos] in SPACE:
            pos += 1
        if pos + 2 < end and text.startswith("/*", pos):
            pos += 2
            while pos + 2 < end and text[pos] != "*" and text[pos + 1] != "/":
                pos += 1
            if pos + 2 < end and text.startswith("*/", pos):
                pos += 2
        if not (pos < end and (text[pos] in SPACE or
                               (pos + 2 < end and text.startswith("/*", pos)))):
            return pos


def delimiter_command(line, family):
    """Binary-mode find_command + get_arg, independent of the app lexer.

    Only DELIMITER is a named command with these flags; charset and sandbox
    are SQL text. mysql-8.4.0:client/mysql.cc:2640-2685,4615-4635,4861-4900;
    mariadb-11.8.9:client/mysql.cc:2580-2584,4881-4905,5023-5080.
    Return (recognized, usable value). CR is retained in an unquoted value.
    com_delimiter first copies 255 bytes of the command, then 15 bytes of
    the parsed argument: mysql-8.4.0:237,4617-4632;
    mariadb-11.8.9:302,4883-4902. The oracle's input is ASCII, so character
    and byte offsets coincide. Unlike the production guard, model actual
    truncation here so an overlong argument cannot silently pass the test.
    """
    command = line.lstrip(SPACE)
    if not command[:9].lower() == "delimiter":
        return False, None
    command = line[:255].lstrip(SPACE)
    pos = 0
    while pos < len(command) and command[pos] not in SPACE:
        pos += 1
    while pos < len(command) and command[pos] in SPACE:
        pos += 1
    quote = command[pos:pos + 1]
    quote = quote if quote in ("'", '"', "`") else ""
    pos += bool(quote)
    value, closed = "", not quote
    while pos < len(command):
        ch = command[pos]
        if (ch == "\\" and pos + 1 < len(command) and
                (not quote if family == "mysql" else quote != "`")):
            pos += 1
            value += command[pos]
        elif quote and ch == quote and family == "mariadb" and command[pos + 1:pos + 2] == quote:
            value += ch
            pos += 1
        elif ch == (quote or " "):
            closed = True
            break
        else:
            value += ch
        pos += 1
    if not value or "\\" in value or (family == "mariadb" and not closed):
        return True, None
    return True, value[:15]


def client_packets(sql, family, delimiter=FRAME, client_version=None, windows=False,
                   dollar_quote_supported=True, append_delimiter=True):
    """Return COM_QUERY texts, remaining buffer, and executed short commands.

    Feed the editor's <send> without its statement tag, followed by newline,
    <private delimiter>, newline. The transport adds the tag on the wire;
    this oracle tests the untagged send text. remove_cntrl trims trailing
    ASCII whitespace before COM_QUERY
    (mysql-8.4.0:client/mysql.cc:3639-3641,5454-5458;
    mariadb-11.8.9:client/mysql.cc:3614,5522-5529).
    append_delimiter=False instead reads an already terminated user script,
    including its DELIMITER commands, without adding a private frame.
    """
    if family not in ("mysql", "mariadb") or not sql.isascii():
        raise ValueError("Outside the ASCII comment oracle's domain")
    version = tuple(int(part) for part in (client_version or
                    ("8.4.0" if family == "mysql" else "11.8.9")).split("."))
    comment_lookup = family == "mysql" and version >= (8, 2, 0)
    persistent_special = family == "mariadb" and version >= (12, 0, 1)
    dollars = family == "mysql" and version >= (8, 1, 0) and dollar_quote_supported
    hints = family == "mysql" or persistent_special
    # MariaDB 12.0.1:2694-2708 has no SSC_HINT exclusion here.
    hint_line_guard = family == "mysql" and (
                      (version >= (9, 2, 0) or (8, 0, 43) <= version < (8, 1, 0) or
                       (8, 4, 6) <= version < (9, 0, 0)))
    buffer, packets, commands = "", [], []
    ml_comment, in_string, ss_comment, dollar_tag = False, "", 0, ""
    stream = sql + "\n" + (delimiter + "\n" if append_delimiter else "")
    for line in stream.split("\n")[:-1]:
        # binary-mode batch_readline keeps CR on Unix and in MariaDB.
        # MySQL Windows removes one CR before LF in every checked tag:
        # mysql-8.4.0:client/readline.cc:75-95; MariaDB 11.8.9:38-44.
        if windows and family == "mysql" and line.endswith("\r"):
            line = line[:-1]
        # read_and_execute runs BEFORE add_line resets its local ss_comment.
        # MySQL 8.4.0:2455-2481; 5.7.44:2355-2369; 8.0.46:2318-2329;
        # 8.1.0:2387-2398; MariaDB 10.11.16:2403-2414; 11.8.9:2432-2443;
        # 12.0.1:2352-2363. ml_comment and in_string still guard dispatch.
        only_comments = comment_lookup and skip_over_comments_and_space(buffer) == len(buffer)
        if not ml_comment and not in_string and (not buffer or only_comments):
            head = skip_over_comments_and_space(line) if comment_lookup else 0
            found, value = delimiter_command(line[head:], family)
            if found:
                commands.append("DELIMITER")
                if only_comments:
                    buffer = ""
                if value:
                    delimiter = value
                continue
        if not persistent_special:
            ss_comment = 0
        need_space, pos = False, 0
        # buffer contains both the flushed glob_buffer and the current
        # add_line output. Track the latter: newline handling inspects only
        # the bytes since the last flush (out = line), not the raw input.
        out_start = len(buffer)
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
                    out_start = len(buffer)
                    pos += 1
                    stop = line.find("*/" if ss_comment else delimiter, pos)
                    pos = len(line) if stop < 0 else stop + (0 if ss_comment else len(delimiter))
                    continue
                if following == "-" and family == "mariadb":
                    commands.append("-")
                    out_start = len(buffer)
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
                head = skip_over_comments_and_space(buffer) if comment_lookup else 0
                found, value = delimiter_command(buffer[head:], family)
                if found:
                    commands.append("DELIMITER")
                    if value:
                        delimiter = value
                elif buffer.rstrip(SPACE):
                    packets.append(buffer.rstrip(SPACE))
                buffer = ""
                out_start = 0
                continue
            # mysql:2859-2894 predates the hint #/-- fix. MariaDB:2772-2815.
            dash = tail.startswith("--") and (len(tail) == 2 or tail[2] in SPACE or
                                               (family == "mariadb" and not buffer))
            if not ml_comment and not in_string and not (hint_line_guard and ss_comment == 2) and (ch == "#" or dash):
                empty = not buffer
                buffer += tail
                if empty:
                    packets.append(buffer.rstrip(SPACE))
                    buffer = ""
                out_start = len(buffer)
                break
            # No !ml_comment guard here. Consuming /* hides the overlapping
            # */ in /*/ from the next iteration. mysql:2895-2906;
            # mariadb:2817-2833. Do not "correct" this client behavior.
            ordinary = tail.startswith("/*") and not tail.startswith("/*!")
            if hints:
                ordinary = ordinary and not tail.startswith("/*+") and ss_comment != 2
            if family == "mariadb":
                ordinary = ordinary and not tail.startswith("/*M!")
            if not in_string and ordinary:
                buffer += "/*"
                pos += 2
                ml_comment = True
                out_start = len(buffer)
                continue
            # mysql:2907-2921; mariadb:2834-2851.
            if ml_comment and not ss_comment and tail.startswith("*/"):
                buffer += "*/"
                pos += 2
                ml_comment, need_space = False, True
                out_start = len(buffer)
                continue
            # mysql:2922-2963; mariadb:2853-2875. Conditional open/close
            # examines one byte per iteration, unlike ordinary comments.
            if not in_string and tail.startswith("/*!"):
                ss_comment = 1
            elif not in_string and tail.startswith("/*+") and hints:
                ss_comment = 2
            elif not in_string and ss_comment and tail.startswith("*/"):
                ss_comment = 0
                # MariaDB 12.0.1:2779-2792 sets this state for /*! and
                # /*+ only, then consumes both closer bytes (not /*M!).
                # Older clients reexamine '/', which may reopen a comment.
                if persistent_special:
                    buffer += "*/"
                    pos += 2
                    continue
            if ch == in_string:
                if ch != "$":
                    in_string = ""
                elif line.startswith(dollar_tag, pos):
                    in_string = ""
                    buffer += dollar_tag[:-1]
                    pos += len(dollar_tag) - 1
            elif not ml_comment and not in_string and ss_comment != 2:
                if ch in "'\"`":
                    in_string = ch
                elif ch == "$" and dollars:
                    # check_for_dollar_quote rejects an identifier prefix
                    # and scans the tag on this physical line only. The
                    # closing tag is case-sensitive. Copy all but its last
                    # byte before need_space, as add_line actually does.
                    # mysql-8.1.0:2541-2577,2764-2798;
                    # mysql-8.4.0:2701-2733,2931-2962;
                    # mysql-9.7.0:2715-2747,2949-2981.
                    prefix = line[pos - 1:pos] if pos else ""
                    tag = re.match(r"\$[a-zA-Z0-9_]*\$", tail)
                    if tag and not (prefix and (prefix.isalnum() or prefix in "_$")):
                        dollar_tag, in_string = tag.group(), "$"
                        buffer += dollar_tag[:-1]
                        pos += len(dollar_tag) - 1
            if need_space and ch not in SPACE:
                buffer += " "
            need_space = False
            buffer += ch
            pos += 1
        # is_delimiter_command checks the first nine bytes, without a word
        # boundary. Special-comment state does not protect the newline.
        # mysql-8.4.0:1233-1244,2966-2982; mariadb-11.8.9:1185-1195,2874-2892.
        if buffer and (buffer[out_start:out_start + 9].lower() != "delimiter" or
                       in_string or ml_comment):
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
