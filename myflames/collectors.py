"""
Metadata collectors used by the CLI when running against a live server.

Three collectors are exposed, each enabled by default and individually
toggleable via CLI flags (``--no-collect-schema``, ``--no-collect-stats``,
``--no-collect-variables``):

* :func:`collect_schema`    — ``SHOW CREATE TABLE`` + column / index parsing.
* :func:`collect_stats`     — ``information_schema.TABLES`` row and byte
                               counts, fragmentation ratio, auto_increment.
* :func:`collect_session_variables` — ``SHOW SESSION VARIABLES`` filtered to
                               the keys the advisor inspects (buffer pool,
                               sort/join buffers, tmp table, optimizer_switch,
                               etc.).

All parsers are regex-based and tolerant of MySQL and MariaDB dialect
differences. They never raise on malformed input — partial data is always
preferable to a hard failure when we're trying to *help* the user tune a
query.
"""
import re


# ---------------------------------------------------------------------------
# Table name extraction
# ---------------------------------------------------------------------------
#
# We need to know which tables the query touches so we can scope schema /
# stats collection to just those tables (instead of pulling every table in
# the database — which can be thousands on RDS).

def _strip_comments_and_literals(sql):
    """Mask comments and strings without treating their contents as SQL.

    Match tokens in one pass: a comment marker inside a string is text, and
    a quote inside a comment does not start a string. Backtick identifiers
    are kept intact. MySQL requires whitespace after a -- comment marker.
    """
    tokens = re.compile(
        r"/\*.*?\*/|--(?=\s|$)[^\r\n]*|\#[^\r\n]*|"
        r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|"
        r"`(?:``|[^`])*`",
        re.DOTALL,
    )

    def _mask(match):
        token = match.group(0)
        return token if token.startswith("`") else " " * len(token)

    return tokens.sub(_mask, sql)


def extract_table_references(sql, default_schema=None, limit=None):
    """Return ``(schema, table)`` pairs and caveats from statement text.

    This is a bounded text match, not SQL resolution or evidence of access.
    Reuses the collectors' literal/comment masking. CTE names are scoped to
    their query and nested queries; qualified names always name real objects.
    Pairs keep dots inside quoted names unambiguous for metadata predicates.
    """
    if not sql:
        return [], []
    notes = []
    clean = _strip_comments_and_literals(sql)
    tokens = re.findall(r"`(?:``|[^`])*`|[\w$]+|[^\s]", clean)
    if len(tokens) > 100000:
        tokens = tokens[:100000]
        notes.append("Statement text was limited to 100,000 tokens.")
    # Pair parentheses once. No recursion depends on SQL's unbounded depth.
    ends, stack = {}, []
    for i, token in enumerate(tokens):
        if token == "(":
            stack.append(i)
        elif token == ")" and stack:
            ends[stack.pop()] = i
    if stack:
        notes.append("Statement text has an unfinished parenthesized expression.")

    def word(i, value):
        return i < len(tokens) and tokens[i].upper() == value

    def identifier(i):
        if i >= len(tokens):
            return None
        token = tokens[i]
        if token.startswith("`") and token.endswith("`"):
            return token[1:-1].replace("``", "`") or None
        if re.fullmatch(r"[\w$]+", token) and not token.isdigit():
            return token
        return None

    found, seen = [], set()

    def note(text):
        if text not in notes:
            notes.append(text)

    def add(schema, table, ctes):
        if schema is None and table.casefold() in ctes:
            note("CTE names were skipped; their statement text was checked for tables.")
            return
        if schema is None and table.upper() == "DUAL":
            return
        schema = schema if schema is not None else default_schema
        # Do not fold names: case-distinct tables can coexist on both engines.
        key = (schema, table)
        if key in seen:
            return
        seen.add(key)
        if limit is not None and len(found) >= limit:
            note("Only the first %d tables found in the statement text are listed." % limit)
            return
        found.append(key)

    def walk(start, stop, inherited, depth=0, table_group=False):
        if depth > 64:
            note("Statement nesting beyond 64 levels was skipped.")
            return
        ctes = set(inherited)
        definitions = []
        pos = start
        if word(pos, "WITH"):
            pos += 1
            if word(pos, "RECURSIVE"):
                pos += 1
            while pos < stop:
                name = identifier(pos)
                if name is None:
                    break
                pos += 1
                # Optional CTE column names, then AS (query).
                if pos in ends:
                    pos = ends[pos] + 1
                if not word(pos, "AS") or pos + 1 not in ends:
                    break
                opening = pos + 1
                ctes.add(name.casefold())
                definitions.append((opening + 1, ends[opening]))
                pos = ends[opening] + 1
                if pos >= stop or tokens[pos] != ",":
                    break
                pos += 1
            for left, right in definitions:
                walk(left, right, ctes, depth + 1)
            if definitions:
                note("CTE names were skipped; their statement text was checked for tables.")
        in_sources, expecting, query = table_group, table_group, False
        first = pos
        # DELETE FROM aliases USING actual_tables names aliases before USING.
        # A JOIN's USING (columns) is a different construct.
        delete_using = False
        scan = pos
        while word(first, "DELETE") and scan < stop:
            if scan in ends:
                scan = ends[scan] + 1
                continue
            if word(scan, "USING") and scan + 1 < stop and tokens[scan + 1] != "(":
                delete_using = True
                break
            scan += 1
        while pos < stop:
            token = tokens[pos]
            upper = token.upper()
            if token == "(":
                end = ends.get(pos, stop)
                subquery = word(pos + 1, "SELECT") or word(pos + 1, "WITH")
                if expecting and subquery:
                    note("Derived tables were skipped as table names; their statement text was checked.")
                walk(pos + 1, end, ctes, depth + 1, table_group=expecting and not subquery)
                if expecting:
                    expecting = False
                pos = end + 1
                continue
            if upper in ("SELECT", "DELETE"):
                query = True
            if (upper == "FROM" and query and not delete_using
                    or upper in ("JOIN", "STRAIGHT_JOIN") and in_sources
                    or upper == "USING" and delete_using and pos + 1 < stop and tokens[pos + 1] != "("):
                in_sources, expecting = True, True
            elif pos == first and upper in ("UPDATE", "INSERT", "REPLACE"):
                in_sources = upper == "UPDATE"
                expecting = True
            elif upper == "INTO" and (word(first, "INSERT") or word(first, "REPLACE")):
                expecting = True
            elif token == "," and in_sources:
                expecting = True
            elif upper in ("WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION", "EXCEPT", "INTERSECT", "SET", "VALUES", "RETURNING", "FOR", "LOCK", "WINDOW"):
                in_sources, expecting = False, False
            elif expecting:
                # Join modifiers and LATERAL do not name tables. A table
                # function has no information_schema.TABLES entry.
                if upper in ("LATERAL", "LOW_PRIORITY", "IGNORE", "ONLY"):
                    pos += 1
                    continue
                name = identifier(pos)
                if name:
                    schema, table = None, name
                    if pos + 2 < stop and tokens[pos + 1] == "." and identifier(pos + 2):
                        schema, table = name, identifier(pos + 2)
                        pos += 2
                    if pos + 1 < stop and tokens[pos + 1] == "(":
                        # INSERT target column lists are not table functions.
                        if word(first, "INSERT") or word(first, "REPLACE"):
                            add(schema, table, ctes)
                        else:
                            note("Table functions were skipped as table names.")
                    else:
                        add(schema, table, ctes)
                expecting = False
            pos += 1

    # Separate statements must not inherit each other's CTE names.
    start = 0
    for pos, token in enumerate(tokens):
        if token == ";":
            walk(start, pos, set())
            start = pos + 1
    walk(start, len(tokens), set())
    return found, notes


def extract_table_names(sql, default_schema=None):
    """Deduplicated table names for the CLI's existing metadata collectors."""
    pairs, _ = extract_table_references(sql, default_schema)
    return [(schema + "." + table) if schema else table for schema, table in pairs]


# ---------------------------------------------------------------------------
# SHOW CREATE TABLE parser
# ---------------------------------------------------------------------------

_COL_RE = re.compile(
    r"""^\s*
        `(?P<name>[^`]+)`\s+            # `colname`
        (?P<type>[A-Za-z]+(?:\([^)]*\))?)  # type with optional (...)
        (?P<rest>.*?),?\s*$
    """,
    re.VERBOSE,
)
_KEY_RE = re.compile(
    r"""^\s*
        (?:(?P<kind>PRIMARY\ KEY|UNIQUE\ KEY|UNIQUE|FULLTEXT\ KEY|FULLTEXT|
                    SPATIAL\ KEY|SPATIAL|FOREIGN\ KEY|KEY|INDEX))
        (?:\s+`(?P<name>[^`]+)`)?       # optional index name
        \s*\(
    """,
    re.VERBOSE | re.IGNORECASE,
)
_ENGINE_RE = re.compile(r"ENGINE\s*=\s*(\w+)", re.IGNORECASE)
_CHARSET_RE = re.compile(r"DEFAULT\s+CHARSET\s*=\s*(\w+)", re.IGNORECASE)


_USING_RE = re.compile(r"\)\s+USING\s+(BTREE|HASH)\b", re.IGNORECASE)
_PART_RE = re.compile(r"^`(?P<name>(?:[^`]|``)+)`(?:\((?P<length>\d+)\))?(?:\s+(?P<dir>ASC|DESC))?$",
                      re.IGNORECASE)


def _parse_key_parts(line, start):
    """Parse the key-part list that opens at ``line[start - 1] == '('``.

    Returns ``[{"name", "length", "desc", "expression"}]`` or ``None`` when
    the parenthesis never closes. A part is a backquoted column name,
    optionally followed by a prefix length such as (10) and ASC or DESC, or a
    functional key part in parentheses (MySQL 8.0.13+), which is kept with
    ``expression`` true and ``name`` set to the expression text.
    """
    depth, quote, item, items = 1, None, [], []
    i = start
    while i < len(line):
        ch = line[i]
        if quote:
            item.append(ch)
            if ch == quote:
                quote = None
        elif ch in "`'\"":
            quote = ch
            item.append(ch)
        elif ch == "(":
            depth += 1
            item.append(ch)
        elif ch == ")":
            depth -= 1
            if depth == 0:
                items.append("".join(item).strip())
                break
            item.append(ch)
        elif ch == "," and depth == 1:
            items.append("".join(item).strip())
            item = []
        else:
            item.append(ch)
        i += 1
    else:
        return None
    parts = []
    for raw in items:
        m = _PART_RE.match(raw)
        if m:
            parts.append({
                "name": m.group("name").replace("``", "`"),
                "length": int(m.group("length")) if m.group("length") else None,
                "desc": (m.group("dir") or "").upper() == "DESC",
                "expression": False,
            })
        else:
            parts.append({"name": raw, "length": None, "desc": False, "expression": True})
    return parts


def parse_show_create_table(ddl):
    """Parse a ``SHOW CREATE TABLE`` DDL string into a structured dict.

    Returns: ``{"ddl", "table_name", "columns", "indexes", "engine",
    "charset"}``. Columns are ``[{"name","type","rest"}]``; indexes are
    ``[{"name","kind","columns","parts","using"}]`` where ``parts`` keeps
    prefix lengths, DESC, and functional key parts that ``columns`` (bare
    names, kept for existing callers) cannot express. Unparseable lines are
    dropped.
    """
    result = {
        "ddl": ddl,
        "table_name": None,
        "columns": [],
        "indexes": [],
        "engine": None,
        "charset": None,
    }
    if not ddl:
        return result

    # Extract the table name from the first line: CREATE TABLE `foo` (
    m = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([^`\s(]+)`?", ddl, re.IGNORECASE)
    if m:
        result["table_name"] = m.group(1)

    # Columns and indexes are on their own lines between the opening "(" and
    # the trailing ") ENGINE=...".
    inside = re.search(r"\(\n(.*)\n\)\s*(ENGINE.*)?$", ddl, re.DOTALL)
    if not inside:
        # Fall back to scanning between the first ( and the last )
        open_paren = ddl.find("(")
        close_paren = ddl.rfind(")")
        if open_paren == -1 or close_paren == -1:
            return result
        body = ddl[open_paren + 1:close_paren]
    else:
        body = inside.group(1)

    for raw_line in body.split("\n"):
        line = raw_line.strip().rstrip(",")
        if not line:
            continue
        key_match = _KEY_RE.match(line)
        if key_match:
            parts = _parse_key_parts(line, key_match.end())
            if parts is None:
                continue
            result["indexes"].append({
                "kind": (key_match.group("kind") or "KEY").upper().replace("  ", " "),
                "name": key_match.group("name"),
                "columns": [part["name"] for part in parts],
                "parts": parts,
                "using": (_USING_RE.search(line).group(1).upper()
                          if _USING_RE.search(line) else None),
            })
            continue
        col_match = _COL_RE.match(line)
        if col_match:
            result["columns"].append({
                "name": col_match.group("name"),
                "type": col_match.group("type"),
                "rest": (col_match.group("rest") or "").strip(),
            })

    em = _ENGINE_RE.search(ddl)
    if em:
        result["engine"] = em.group(1)
    cm = _CHARSET_RE.search(ddl)
    if cm:
        result["charset"] = cm.group(1)
    return result


# ---------------------------------------------------------------------------
# Collector: schema (SHOW CREATE TABLE per referenced table)
# ---------------------------------------------------------------------------

def collect_schema(conn, tables):
    """For each ``schema.table`` in *tables*, run ``SHOW CREATE TABLE`` and
    parse the result. Returns ``{fq_table_name: parsed_dict}``. Tables the
    user has no privilege on (or that do not exist) are silently skipped —
    the advisor should degrade gracefully on partial data.
    """
    from .connector import ConnectorError

    out = {}
    for fq in tables:
        quoted = _quote_fq(fq)
        try:
            # SHOW CREATE TABLE returns two columns: table_name, ddl. We
            # only want the second.
            rows = conn.query_rows("SHOW CREATE TABLE " + quoted)
        except ConnectorError:
            continue
        if not rows:
            continue
        ddl = rows[0][1] if len(rows[0]) > 1 else rows[0][0]
        parsed = parse_show_create_table(ddl)
        out[fq] = parsed
    return out


def _quote_fq(fq):
    """Quote a ``schema.table`` name using backticks on both sides.

    Splits on the *first* dot so a table literally named ``a.b`` would have
    to be passed already quoted — we don't try to be smart about that edge
    case here."""
    if "." in fq:
        schema, table = fq.split(".", 1)
        return "`" + schema.replace("`", "``") + "`.`" + table.replace("`", "``") + "`"
    return "`" + fq.replace("`", "``") + "`"


# ---------------------------------------------------------------------------
# Collector: table stats (information_schema.TABLES)
# ---------------------------------------------------------------------------

_STATS_COLUMNS = (
    "table_schema",
    "table_name",
    "table_rows",
    "data_length",
    "index_length",
    "data_free",
    "auto_increment",
    "engine",
    "row_format",
)
# One client call reads up to this many tables. The SQL travels on the
# client's command line (``mysql -e``), so each batch also stays under a
# length that fits every platform's limit (Windows allows 32,767 characters
# for the whole command).
_STATS_BATCH_SIZE = 100
_STATS_BATCH_CHARS = 24000


def _stats_select(index, fq):
    """One request: the original per-table SELECT, tagged with its position.

    Each request keeps its constant ``table_name`` and ``table_schema``
    equalities (``DATABASE()`` for an unqualified name), which both servers
    resolve as direct lookups on ``information_schema.TABLES`` instead of
    reading every table's metadata.
    """
    schema, table = fq.split(".", 1) if "." in fq else (None, fq)
    return ("SELECT %d AS request_id, %s FROM information_schema.tables WHERE table_name = %s "
            "AND table_schema = %s" % (index, ", ".join(_STATS_COLUMNS), _sql_string(table),
                                       _sql_string(schema) if schema else "DATABASE()"))


def _stats_batches(requests):
    """Split (index, SELECT) pairs by count and by statement length."""
    batch, size = [], 0
    for request in requests:
        extra = len(request[1]) + (len(" UNION ALL ") if batch else 0)
        if batch and (len(batch) >= _STATS_BATCH_SIZE or size + extra > _STATS_BATCH_CHARS):
            yield batch
            batch, size = [], 0
            extra = len(request[1])
        batch.append(request)
        size += extra
    if batch:
        yield batch


def collect_stats(conn, tables):
    """Return a ``{fq_table_name: dict}`` map with sizing info sourced from
    ``information_schema.TABLES``.

    Stats are approximate for InnoDB (they come from dictionary counters,
    not a live ``COUNT(*)``) but that is exactly what we want — we are
    advising on orders of magnitude, not exact row counts.

    Tables are read in batches (``_stats_batches``): one ``UNION ALL`` of the
    per-table SELECTs, each tagged with the table's position in *tables*, so
    every name keeps its own row (the first one the server returns, as
    before), whatever its spelling. When a batch fails, its tables are read
    one at a time, so only a table whose own read fails is left out.
    """
    from .connector import ConnectorError

    tables = list(tables or ())
    if not tables:
        return {}
    by_request = {}

    def read(batch):
        rows = conn.query_dicts(" UNION ALL ".join(sql for _, sql in batch), ("request_id",) + _STATS_COLUMNS)
        wanted = set(index for index, _ in batch)
        for row in rows:
            try:
                index = int(row.pop("request_id"))
            except (TypeError, ValueError):
                continue
            if index in wanted:
                by_request.setdefault(index, row)

    for batch in _stats_batches([(index, _stats_select(index, fq)) for index, fq in enumerate(tables)]):
        try:
            read(batch)
        except ConnectorError:
            if len(batch) == 1:
                continue
            for request in batch:
                try:
                    read([request])
                except ConnectorError:
                    continue
    out = {}
    for index, fq in enumerate(tables):
        row = by_request.get(index)
        if row is None:
            continue
        # Convert numeric columns so the advisor can do math directly.
        for k in ("table_rows", "data_length", "index_length", "data_free", "auto_increment"):
            try:
                row[k] = int(row[k]) if row[k] and row[k] != "NULL" else 0
            except ValueError:
                row[k] = 0
        out[fq] = row
    return out


def _sql_string(text):
    """Quote *text* as a SQL string literal.

    Names come from backquoted identifiers in user SQL, so they may contain
    quotes and backslashes. Doubling both is safe whether or not the session
    uses NO_BACKSLASH_ESCAPES: a backslash can never escape the closing quote.
    """
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _table_filter(fq, schema_col, table_col):
    """``schema_col = '..' AND table_col = '..'`` for one ``schema.table``
    (``DATABASE()`` when unqualified)."""
    if "." in fq:
        schema, table = fq.split(".", 1)
        schema_sql = _sql_string(schema)
    else:
        table, schema_sql = fq, "DATABASE()"
    return "({} = {} AND {} = {})".format(schema_col, schema_sql, table_col, _sql_string(table))


def collect_stats_freshness(conn, tables, mariadb=False):
    """Return ``{fq_table_name: {"last_update", "n_rows", "histograms"}}``.

    ``last_update`` and ``n_rows`` come from ``mysql.innodb_table_stats``
    (InnoDB persistent statistics, both engines). ``histograms`` lists the
    columns with a histogram: ``information_schema.COLUMN_STATISTICS`` on
    MySQL 8.0+, ``mysql.column_stats`` rows with ``hist_size > 0`` on
    MariaDB. Reading ``mysql.*`` needs SELECT on those tables, which is not
    granted by default, so each query degrades to nothing on error.
    """
    from .connector import ConnectorError

    if not tables:
        return {}
    out = {}
    where = " OR ".join(_table_filter(fq, "database_name", "table_name") for fq in tables)
    try:
        rows = conn.query_rows(
            "SELECT database_name, table_name, last_update, n_rows "
            "FROM mysql.innodb_table_stats WHERE " + where)
    except ConnectorError:
        rows = []
    for row in rows:
        if len(row) < 4:
            continue
        try:
            n_rows = int(row[3])
        except (TypeError, ValueError):
            n_rows = None
        out[row[0] + "." + row[1]] = {"last_update": row[2], "n_rows": n_rows, "histograms": []}
    if mariadb:
        sql = ("SELECT db_name, table_name, column_name FROM mysql.column_stats "
               "WHERE hist_size > 0 AND ("
               + " OR ".join(_table_filter(fq, "db_name", "table_name") for fq in tables) + ")")
    else:
        sql = ("SELECT SCHEMA_NAME, TABLE_NAME, COLUMN_NAME FROM "
               "information_schema.COLUMN_STATISTICS WHERE "
               + " OR ".join(_table_filter(fq, "SCHEMA_NAME", "TABLE_NAME") for fq in tables))
    try:
        rows = conn.query_rows(sql)
    except ConnectorError:
        rows = []
    for row in rows:
        if len(row) < 3:
            continue
        entry = out.setdefault(row[0] + "." + row[1],
                               {"last_update": None, "n_rows": None, "histograms": []})
        entry["histograms"].append(row[2])
    return out


# ---------------------------------------------------------------------------
# Collector: session variables
# ---------------------------------------------------------------------------
#
# We pull the *whole* session variables snapshot (one query) and let the
# advisor pick the keys it cares about — the shape of the output is stable
# across server versions and it's cheaper than issuing one query per var.

#: Variables the advisor actually inspects — used to filter the SVG display
#: so we don't dump 500 rows into the analysis panel.
ADVISOR_VARIABLES = (
    # InnoDB buffer pool / IO
    "innodb_buffer_pool_size",
    "innodb_buffer_pool_instances",
    "innodb_log_file_size",
    "innodb_log_buffer_size",
    "innodb_io_capacity",
    "innodb_flush_method",
    "innodb_flush_log_at_trx_commit",
    "innodb_file_per_table",
    # Query-level buffers
    "sort_buffer_size",
    "join_buffer_size",
    "read_buffer_size",
    "read_rnd_buffer_size",
    "tmp_table_size",
    "max_heap_table_size",
    "internal_tmp_mem_storage_engine",
    "bulk_insert_buffer_size",
    # Optimizer
    "optimizer_switch",
    "optimizer_search_depth",
    "optimizer_prune_level",
    "eq_range_index_dive_limit",
    "range_optimizer_max_mem_size",
    # Execution
    "max_allowed_packet",
    "max_connections",
    "thread_cache_size",
    "table_open_cache",
    # Character set / collation (often a silent perf issue)
    "character_set_connection",
    "character_set_server",
    "collation_connection",
    "collation_server",
    # Versioning
    "version",
    "version_comment",
    "version_compile_os",
)


def collect_session_variables(conn, names=None):
    """Return a ``{var_name: value}`` dict from ``SHOW SESSION VARIABLES``.

    If *names* is provided, only those keys are returned (case-insensitive).
    The default is :data:`ADVISOR_VARIABLES`, which is the list the
    :mod:`myflames.advisor` module inspects.
    """
    from .connector import ConnectorError

    try:
        all_vars = conn.query_kv("SHOW SESSION VARIABLES")
    except ConnectorError:
        return {}
    wanted = names if names is not None else ADVISOR_VARIABLES
    wanted_lc = {w.lower() for w in wanted}
    return {k: v for k, v in all_vars.items() if k.lower() in wanted_lc}
