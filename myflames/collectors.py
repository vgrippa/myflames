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

_TABLE_KEYWORDS = ("FROM", "JOIN", "INTO", "UPDATE")
# Matches an identifier possibly prefixed with a schema and possibly
# backtick-quoted. Captures ``schema.table`` or ``table``.
_IDENT_RE = re.compile(
    r'(?:`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))'
    r'(?:\s*\.\s*(?:`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*)))?'
)


def _strip_comments_and_literals(sql):
    """Remove string literals and /* */ or -- comments from *sql* so the
    table-name regex cannot match inside them (e.g. a literal 'FROM x')."""
    # /* ... */
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # -- ...
    sql = re.sub(r"--[^\n]*", " ", sql)
    # '...' and "..." (handle doubled-quote escape)
    def _mask(m):
        return '"' + " " * (len(m.group(0)) - 2) + '"'
    sql = re.sub(r"'(?:''|\\'|[^'])*'", _mask, sql)
    sql = re.sub(r'"(?:""|\\"|[^"])*"', _mask, sql)
    return sql


#: Single identifier (optionally backtick-quoted), optionally schema-qualified.
_IDENT = r"(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)"
_QUALIFIED = r"(?:" + _IDENT + r"\s*\.\s*)?" + _IDENT

#: Clause boundaries that terminate a comma-separated FROM list.
_CLAUSE_BOUNDARY = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|UNION|"
    r"INNER\s+JOIN|LEFT\s+JOIN|RIGHT\s+JOIN|FULL\s+OUTER\s+JOIN|"
    r"CROSS\s+JOIN|STRAIGHT_JOIN|JOIN|ON|USING|FOR\s+UPDATE)"
    r"(?![A-Za-z0-9_])"
)


def _parse_comma_list(text, offset):
    """Parse a comma-separated list of table refs starting at *offset*.

    Returns the list of ``(schema, table)`` tuples found before the next
    clause boundary or end of string. Aliases (``users u`` or ``users AS u``)
    are ignored. Derived subqueries (``(...)``) are skipped entirely.
    """
    out = []
    i = offset
    n = len(text)
    while i < n:
        # Skip leading whitespace
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        # Hit a clause boundary? Stop.
        m_boundary = _CLAUSE_BOUNDARY.match(text, i)
        if m_boundary:
            break
        # Derived-table subquery: "(...)"  — skip balanced parens.
        if text[i] == "(":
            depth = 0
            while i < n:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            # After the subquery, consume an optional alias.
            while i < n and text[i].isspace():
                i += 1
            m_alias = re.match(_IDENT, text[i:])
            if m_alias:
                i += m_alias.end()
        else:
            m_tbl = re.match(_QUALIFIED, text[i:])
            if not m_tbl:
                break
            token = m_tbl.group(0)
            i += m_tbl.end()
            # Parse schema.table
            if "." in token:
                schema, table = [p.strip("` \t") for p in token.split(".", 1)]
            else:
                schema, table = None, token.strip("` \t")
            # Skip synthetic / dual references
            if table.lower() != "dual" and not (table.startswith("<") and table.endswith(">")):
                out.append((schema, table))
            # Consume optional alias: "AS alias" or "alias"
            while i < n and text[i].isspace():
                i += 1
            if i + 2 <= n and text[i:i+2].upper() == "AS" and (i + 2 == n or not text[i+2].isalnum()):
                i += 2
                while i < n and text[i].isspace():
                    i += 1
                m_alias = re.match(_IDENT, text[i:])
                if m_alias:
                    i += m_alias.end()
            else:
                # Bare alias — but only if the next token is a bare identifier
                # and not a reserved clause keyword.
                if i < n and not _CLAUSE_BOUNDARY.match(text, i) and text[i] != ",":
                    m_alias = re.match(_IDENT, text[i:])
                    if m_alias:
                        alias = m_alias.group(0).strip("`").upper()
                        if alias not in {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT",
                                         "JOIN", "INNER", "LEFT", "RIGHT", "CROSS",
                                         "STRAIGHT_JOIN", "UNION", "ON", "USING", "FOR"}:
                            i += m_alias.end()
        # Next item in the comma list?
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] == ",":
            i += 1
            continue
        break
    return out


def extract_table_names(sql, default_schema=None):
    """Return a deduplicated list of ``schema.table`` references in *sql*.

    Parses FROM / JOIN / INTO / UPDATE clauses, including comma-separated
    ``FROM a, b, c`` lists. Derived-table wrappers (``(SELECT ...)``) are
    skipped because they aren't real tables. When *default_schema* is set,
    unqualified tables get that prefix.
    """
    if not sql:
        return []
    clean = _strip_comments_and_literals(sql)
    found = []
    seen = set()

    def _add(schema, table):
        if not table:
            return
        if default_schema and not schema:
            schema = default_schema
        fq = (schema + "." + table) if schema else table
        key = fq.lower()
        if key in seen:
            return
        seen.add(key)
        found.append(fq)

    # FROM + comma list
    for m in re.finditer(r"(?i)(?<![A-Za-z0-9_])FROM(?![A-Za-z0-9_])", clean):
        for schema, table in _parse_comma_list(clean, m.end()):
            _add(schema, table)

    # JOIN (each JOIN keyword takes one table; comma-lists not valid after JOIN)
    for kw in ("INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL OUTER JOIN",
               "CROSS JOIN", "STRAIGHT_JOIN", "JOIN"):
        rx = re.compile(
            r"(?i)(?<![A-Za-z0-9_])" + kw.replace(" ", r"\s+") + r"(?![A-Za-z0-9_])"
        )
        for m in rx.finditer(clean):
            refs = _parse_comma_list(clean, m.end())
            if refs:
                schema, table = refs[0]
                _add(schema, table)

    # INTO (INSERT/REPLACE)
    for m in re.finditer(r"(?i)(?<![A-Za-z0-9_])INTO(?![A-Za-z0-9_])", clean):
        refs = _parse_comma_list(clean, m.end())
        if refs:
            schema, table = refs[0]
            _add(schema, table)

    # UPDATE
    for m in re.finditer(r"(?i)(?<![A-Za-z0-9_])UPDATE(?![A-Za-z0-9_])", clean):
        refs = _parse_comma_list(clean, m.end())
        for schema, table in refs:
            _add(schema, table)

    return found


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
        \s*\((?P<cols>[^)]+)\)
    """,
    re.VERBOSE | re.IGNORECASE,
)
_ENGINE_RE = re.compile(r"ENGINE\s*=\s*(\w+)", re.IGNORECASE)
_CHARSET_RE = re.compile(r"DEFAULT\s+CHARSET\s*=\s*(\w+)", re.IGNORECASE)


def parse_show_create_table(ddl):
    """Parse a ``SHOW CREATE TABLE`` DDL string into a structured dict.

    Returns: ``{"ddl", "table_name", "columns", "indexes", "engine",
    "charset"}``. Columns are ``[{"name","type","rest"}]``; indexes are
    ``[{"name","kind","columns"}]``. Unparseable lines are dropped.
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
            cols_raw = key_match.group("cols")
            cols = [c.strip().strip("`").split("(")[0].strip("`") for c in cols_raw.split(",")]
            result["indexes"].append({
                "kind": (key_match.group("kind") or "KEY").upper().replace("  ", " "),
                "name": key_match.group("name"),
                "columns": cols,
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


def collect_stats(conn, tables):
    """Return a ``{fq_table_name: dict}`` map with sizing info sourced from
    ``information_schema.TABLES``.

    Stats are approximate for InnoDB (they come from dictionary counters,
    not a live ``COUNT(*)``) but that is exactly what we want — we are
    advising on orders of magnitude, not exact row counts.
    """
    from .connector import ConnectorError

    if not tables:
        return {}
    out = {}
    for fq in tables:
        if "." in fq:
            schema, table = fq.split(".", 1)
        else:
            schema, table = None, fq
        where = "table_name = '" + table.replace("'", "''") + "'"
        if schema:
            where += " AND table_schema = '" + schema.replace("'", "''") + "'"
        else:
            where += " AND table_schema = DATABASE()"
        sql = (
            "SELECT "
            + ", ".join(_STATS_COLUMNS)
            + " FROM information_schema.tables WHERE " + where
        )
        try:
            rows = conn.query_dicts(sql, _STATS_COLUMNS)
        except ConnectorError:
            continue
        if not rows:
            continue
        # Convert numeric columns so the advisor can do math directly.
        row = rows[0]
        for k in ("table_rows", "data_length", "index_length", "data_free", "auto_increment"):
            try:
                row[k] = int(row[k]) if row[k] and row[k] != "NULL" else 0
            except ValueError:
                row[k] = 0
        out[fq] = row
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
