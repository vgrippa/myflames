"""Split and normalize SHOW CREATE output for schema versions (stdlib only).

SHOW CREATE TABLE prints one column, key or constraint per line: both
servers separate them with ",\\n  " and close the list with "\\n)"
(mysql-server sql/sql_show.cc:2740,2805-2831, print_foreign_key_info at
1915-1920; mariadb-server sql/sql_show.cc:2474,2626), and string literals
are written by append_unescaped, which escapes line breaks. So every line
between the header and the closing parenthesis is one whole element, and
the line after it holds the table options. Partitioning follows on the next
lines (MySQL wraps it in a /*!50100 ... */ comment, MariaDB does not).

Definitions are read with SET SESSION sql_mode = '' (schema_versions.py),
so identifiers are quoted with backquotes; routines keep the quoting of
their own sql_mode, which can be double quotes (MariaDB packages in Oracle
mode), so identifier helpers accept both.
"""
import re

#: Element kinds of a table definition, in the order a report lists them.
KINDS = ('column', 'index', 'foreign_key', 'check', 'constraint', 'period', 'other')

_BARE = re.compile(r'[0-9A-Za-z_$\u0080-￿]+')
_TABLE_HEAD = re.compile(r'CREATE (?:TEMPORARY )?TABLE ')
_INDEX_HEAD = re.compile(r'(?:UNIQUE |FULLTEXT |SPATIAL |VECTOR )?(?:KEY|INDEX) ')
_AUTO_INCREMENT = re.compile(r' AUTO_INCREMENT=\d+')
_QUOTED = r'(?:`(?:[^`]|``)*`|"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|[^\s@`"\']+)'
# CREATE [OR REPLACE] [ALGORITHM=x] DEFINER=user[@host]: the definer of a
# view, routine, package, trigger or event, always before the object keyword.
_DEFINER = re.compile(r'\A(CREATE (?:OR REPLACE )?(?:ALGORITHM=\w+ )?)DEFINER=(%s(?:@%s)?) ' % (_QUOTED, _QUOTED))


def quote(name):
    """Backquote an identifier for generated SQL."""
    return '`' + name.replace('`', '``') + '`'


def read_identifier(text, pos):
    """(name, end) of the identifier at *pos*: `back``quoted`, "double""quoted"
    or a bare word. None when there is none or a quote never closes."""
    if pos >= len(text):
        return None
    mark = text[pos]
    if mark in '`"':
        out, i = [], pos + 1
        while i < len(text):
            if text[i] == mark:
                if text.startswith(mark, i + 1):
                    out.append(mark)
                    i += 2
                    continue
                return ''.join(out), i + 1
            out.append(text[i])
            i += 1
        return None
    match = _BARE.match(text, pos)
    return (match.group(0), match.end()) if match else None


def element(text):
    """{kind, name, text} for one element line of a table definition."""
    if text.startswith(('`', '"')):
        ident = read_identifier(text, 0)
        if ident:
            return {'kind': 'column', 'name': ident[0], 'text': text}
    elif text.startswith('PRIMARY KEY'):
        return {'kind': 'index', 'name': 'PRIMARY', 'text': text}
    elif text.startswith('CONSTRAINT '):
        ident = read_identifier(text, len('CONSTRAINT '))
        if ident:
            rest = text[ident[1]:]
            kind = 'foreign_key' if rest.startswith(' FOREIGN KEY') else 'check' if rest.startswith(' CHECK') \
                else 'constraint'
            return {'kind': kind, 'name': ident[0], 'text': text}
    elif text.startswith('PERIOD FOR '):
        ident = read_identifier(text, len('PERIOD FOR '))
        if ident:
            return {'kind': 'period', 'name': ident[0], 'text': text}
    else:
        head = _INDEX_HEAD.match(text)
        if head:
            ident = read_identifier(text, head.end())
            if ident:
                return {'kind': 'index', 'name': ident[0], 'text': text}
    return {'kind': 'other', 'name': text, 'text': text}


def element_key(item):
    """Column, index and constraint names compare without regard to case."""
    return item['kind'], item['name'].lower()


def _split(ddl):
    """(parts, offset of the closing "\\n)") or (None, -1); see split_table."""
    if not isinstance(ddl, str):
        return None, -1
    head = _TABLE_HEAD.match(ddl)
    if not head:
        return None, -1
    ident = read_identifier(ddl, head.end())
    if not ident or not ddl.startswith(' (\n', ident[1]):
        return None, -1
    start = ident[1] + 3
    close = ddl.find('\n)', start)
    if close < 0:
        return None, -1
    lines = ddl[start:close].split('\n')
    elements = []
    for index, line in enumerate(lines):
        last = index == len(lines) - 1
        if not line.startswith('  ') or (not last and not line.endswith(',')):
            return None, -1
        elements.append(element(line[2:] if last else line[2:-1]))
    options, _, partitioning = ddl[close + 2:].partition('\n')
    return {'name': ident[0], 'elements': elements, 'options': options.strip(),
            'partitioning': partitioning.strip()}, close


def split_table(ddl):
    """A table definition as {name, elements, options, partitioning}, or None
    when *ddl* does not have the shape SHOW CREATE TABLE prints. Elements
    keep their server order and text without the leading two spaces and the
    separating comma; options is the line after the closing parenthesis,
    partitioning the lines after that ('' when none)."""
    return _split(ddl)[0]


def strip_auto_increment(ddl):
    """A table definition without its AUTO_INCREMENT=N counter, which changes
    with every insert. Only the options line can hold it: element lines say
    AUTO_INCREMENT without "=" and literals cannot span lines."""
    parts, close = _split(ddl)
    if parts is None:
        return ddl
    end = ddl.find('\n', close + 2)
    end = len(ddl) if end < 0 else end
    # Both servers print AUTO_INCREMENT before any quoted option (COMMENT,
    # CONNECTION, COMPRESSION; mysql-server sql/sql_show.cc:2946,
    # mariadb-server sql/sql_show.cc:2066), so a literal that happens to
    # contain the words is kept.
    quote_at = ddl.find("'", close + 2, end)
    cut = end if quote_at < 0 else quote_at
    return ddl[:close + 2] + _AUTO_INCREMENT.sub('', ddl[close + 2:cut], count=1) + ddl[cut:]


def split_definer(ddl):
    """(definer, ddl with "DEFINER=... " removed) for a view, routine,
    trigger or event; (None, ddl) when the header names no definer."""
    match = _DEFINER.match(ddl or '')
    if not match:
        return None, ddl
    return match.group(2), match.group(1) + ddl[match.end():]


def with_definer_removed(ddl):
    return split_definer(ddl)[1]


def requalify(text, old, new, names=None):
    """*text* with every backquoted `old`. qualifier replaced by `new`. (the
    form the server writes view and table references in). String literals
    and comments are kept as they are: a quoted '`old`.' is data. A name
    after a dot is never a database (`db`.`old`.`column`). When *names* (the
    database's object names, lowercased) holds `old` itself, `old`.`x` can
    also be a column of the table `old`, so it is replaced only when `x` is
    one of *names* or a third part follows."""
    if not text or old == new or quote(old) + '.' not in text:
        return text
    ambiguous = names is not None and old.lower() in names
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in '\'"':
            end = _literal_end(text, i)
            out.append(text[i:end])
            i = end
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            end = n if end < 0 else end + 2
            out.append(text[i:end])
            i = end
        elif text.startswith('--', i) and (i + 2 >= n or text[i + 2] in ' \t\r\n') or c == '#':
            end = text.find('\n', i)
            end = n if end < 0 else end
            out.append(text[i:end])
            i = end
        elif c == '`':
            ident = read_identifier(text, i)
            if ident is None:
                out.append(text[i:])
                break
            if (ident[0] == old and text.startswith('.', ident[1]) and not (i and text[i - 1] == '.')
                    and (not ambiguous or _names_object(text, ident[1] + 1, names))):
                out.append(quote(new))
            else:
                out.append(text[i:ident[1]])
            i = ident[1]
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def _names_object(text, pos, names):
    """Whether the identifier at *pos* (after `old`.) is one of *names* or is
    itself followed by a dot (`old`.`table`.`column`)."""
    ident = read_identifier(text, pos)
    return ident is not None and (ident[0].lower() in names or text.startswith('.', ident[1]))


def _literal_end(text, pos):
    """The offset after the quoted literal that starts at *pos*: a doubled
    quote and a backslash escape (the server's own output uses both) stay
    inside it."""
    mark, i = text[pos], pos + 1
    while i < len(text):
        if text[i] == '\\':
            i += 2
            continue
        if text[i] == mark:
            if text.startswith(mark, i + 1):
                i += 2
                continue
            return i + 1
        i += 1
    return len(text)


def object_name_span(ddl, keyword):
    """(start, end) of the object name that follows " <keyword> " in a
    definition header, e.g. keyword "VIEW" in "CREATE ... VIEW `db`.`v` AS".
    A qualified name spans both parts. None when not found."""
    marker = ' %s ' % keyword
    pos = (ddl or '').find(marker)
    if pos < 0:
        return None
    start = pos + len(marker)
    ident = read_identifier(ddl, start)
    if not ident:
        return None
    end = ident[1]
    if ddl.startswith('.', end):
        second = read_identifier(ddl, end + 1)
        if not second:
            return None
        end = second[1]
    return start, end


def lines_diff(before, after, context=3):
    """A unified diff of two texts as [[op, text]]: op is ' ', '-', '+' or
    '@' for a hunk header. Empty when the texts are equal."""
    import difflib
    out = []
    a, b = (before or '').split('\n'), (after or '').split('\n')
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
        first, last = group[0], group[-1]
        out.append(['@', '@@ -%d,%d +%d,%d @@' % (first[1] + 1, last[2] - first[1], first[3] + 1, last[4] - first[3])])
        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                out.extend([' ', line] for line in a[i1:i2])
                continue
            if tag in ('replace', 'delete'):
                out.extend(['-', line] for line in a[i1:i2])
            if tag in ('replace', 'insert'):
                out.extend(['+', line] for line in b[j1:j2])
    return out


def _skip_space(text, pos):
    while pos < len(text) and text[pos] == ' ':
        pos += 1
    return pos


def name_list(text, pos):
    """([names], end) for "(`a`,`b`(10) DESC, ...)" at *pos*: the leading
    identifier of each part (a functional part gives no name). None when
    *text* has no list there."""
    pos = _skip_space(text, pos)
    if not text.startswith('(', pos):
        return None
    names, depth, quote_mark, start = [], 0, None, pos + 1
    i = pos
    while i < len(text):
        c = text[i]
        if quote_mark:
            if c == '\\' and quote_mark == "'":
                i += 2
                continue
            if c == quote_mark:
                if text.startswith(c, i + 1):
                    i += 2
                    continue
                quote_mark = None
        elif c in '`"\'':
            quote_mark = c
        elif c == '(':
            depth += 1
        elif c == ')' or (c == ',' and depth == 1):
            if depth == 1:
                ident = read_identifier(text, _skip_space(text, start))
                if ident:
                    names.append(ident[0])
                start = i + 1
            if c == ')':
                depth -= 1
                if depth == 0:
                    return names, i + 1
        i += 1
    return None


def index_columns(text):
    """Column names of an index line (PRIMARY KEY (...), KEY `i` (...))."""
    open_at = text.find('(')
    found = name_list(text, open_at) if open_at >= 0 else None
    return found[0] if found else []


def foreign_key(text):
    """{columns, schema, table, ref_columns} of a CONSTRAINT ... FOREIGN KEY
    line, or None."""
    at = text.find(' FOREIGN KEY ')
    if at < 0:
        return None
    first = name_list(text, at + len(' FOREIGN KEY '))
    if not first:
        return None
    pos = _skip_space(text, first[1])
    if not text.startswith('REFERENCES ', pos):
        return None
    ident = read_identifier(text, pos + len('REFERENCES '))
    if not ident:
        return None
    schema, table, pos = None, ident[0], ident[1]
    if text.startswith('.', pos):
        second = read_identifier(text, pos + 1)
        if not second:
            return None
        schema, table, pos = table, second[0], second[1]
    refs = name_list(text, pos)
    if not refs:
        return None
    return {'columns': first[0], 'schema': schema, 'table': table, 'ref_columns': refs[0]}


def _option_tokens(text):
    """Space-separated tokens of an options line; quotes and /* */ comments
    stay inside one token."""
    tokens, i, n = [], 0, len(text or '')
    while i < n:
        i = _skip_space(text, i)
        if i >= n:
            break
        start = i
        if text.startswith('/*', i):
            end = text.find('*/', i)
            i = n if end < 0 else end + 2
        else:
            quote_mark = None
            while i < n:
                c = text[i]
                if quote_mark:
                    if c == '\\' and quote_mark == "'":
                        i += 2
                        continue
                    if c == quote_mark:
                        if text.startswith(c, i + 1):
                            i += 2
                            continue
                        quote_mark = None
                elif c in '\'"`':
                    quote_mark = c
                elif c == ' ':
                    break
                i += 1
        tokens.append(text[start:i])
    return tokens


def table_options(text):
    """Table options as an ordered {NAME: "NAME=value"} map. Quoted values
    keep their quotes, a version comment (/*!50100 TABLESPACE ... */) is one
    option named by the option inside it, and "DEFAULT CHARSET=x" and "WITH SYSTEM
    VERSIONING" are one option each."""
    tokens, out, i = _option_tokens(text), {}, 0
    while i < len(tokens):
        token = tokens[i]
        if token == 'DEFAULT' and i + 1 < len(tokens) and '=' in tokens[i + 1]:
            token = token + ' ' + tokens[i + 1]
            i += 1
        elif token in ('DATA', 'INDEX') and i + 1 < len(tokens) and tokens[i + 1].startswith('DIRECTORY='):
            # "DATA DIRECTORY='/path'" is printed with a space (sql/sql_show.cc).
            token = token + ' ' + tokens[i + 1]
            i += 1
        elif token == 'WITH' and tokens[i + 1:i + 3] == ['SYSTEM', 'VERSIONING']:
            token = 'WITH SYSTEM VERSIONING'
            i += 2
        if token.startswith('/*'):
            # /*!80016 ENCRYPTION='N' */ is keyed by the option inside it.
            inner = re.sub(r'\A/\*!?\d* ?', '', token)
            inner = inner[:-2] if inner.endswith('*/') else inner
            name = inner.strip().split('=', 1)[0].split(' ', 1)[0] or token
        else:
            name = token.split('=', 1)[0]
        out[name] = token
        i += 1
    return out


_TYPE_WORD = re.compile(r'[A-Za-z0-9_]+')


def column_type_end(text):
    """The offset just after a column line's type (name, then the type word
    and its parenthesized arguments), or None."""
    ident = read_identifier(text, 0)
    if not ident:
        return None
    pos = _skip_space(text, ident[1])
    word = _TYPE_WORD.match(text, pos)
    if not word:
        return None
    pos = word.end()
    if text.startswith('(', pos):
        found = name_list(text, pos)
        if found is None:
            return None
        pos = found[1]
    # MariaDB prints a compressed column as "varchar(10) /*M!100301 COMPRESSED*/",
    # and CHARACTER SET may only follow it (sql/field.h, sql/sql_show.cc).
    if text.startswith(' /*M!', pos) and 'COMPRESSED' in text[pos:text.find('*/', pos) + 2]:
        end = text.find('*/', pos)
        if end > 0:
            pos = end + 2
    return pos


_CHARSET_CLAUSE = re.compile(r' CHARACTER SET [A-Za-z0-9_]+')
_COLLATE_CLAUSE = re.compile(r' COLLATE [A-Za-z0-9_]+')


def column_body(line):
    """A column line without the CHARACTER SET and COLLATE that follow its
    type. MySQL prints them from the column's history as well as its value
    (is_explicit_collation, mysql-server sql/sql_show.cc:2533-2560), so a
    comparison reads the effective values from information_schema instead."""
    end = column_type_end(line)
    if end is None:
        return line
    rest = line[end:]
    match = _CHARSET_CLAUSE.match(rest)
    if match:
        rest = rest[match.end():]
    match = _COLLATE_CLAUSE.match(rest)
    if match:
        rest = rest[match.end():]
    return line[:end] + rest


def with_charset(line, pair):
    """*line* with an explicit CHARACTER SET and COLLATE after its type."""
    end = column_type_end(line)
    if end is None or not pair or not pair[0] or not pair[1]:
        return line
    body = column_body(line)
    return '%s CHARACTER SET %s COLLATE %s%s' % (body[:end], pair[0], pair[1], body[end:])
