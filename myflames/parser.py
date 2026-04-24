"""
Unified parser for MySQL and MariaDB EXPLAIN ANALYZE FORMAT=JSON.
Builds a single tree structure used by flamegraph, bargraph, and treemap.

Supports:
  - MySQL 8.0+ EXPLAIN ANALYZE FORMAT=JSON
  - MySQL 9.7+ query_plan envelope
  - MariaDB 10.5+ / 11.x ANALYZE FORMAT=JSON
  - MariaDB SHOW ANALYZE FORMAT=JSON FOR <connection_id>
"""
import hashlib
import re
import json

from .complexity import compute_complexity


# ---------------------------------------------------------------------------
# node_id — stable identity for every tree node
# ---------------------------------------------------------------------------
#
# Slice 2 primitive. Derived from
#   (operation, table_name, access_type, key, sibling_position)
# along the path from the root. The tuple is canonicalized to lowercase
# and concatenated with a separator so the sha1 digest is stable across
# reruns of the same fixture and across MySQL ↔ MariaDB variants whose
# normalized trees share operation + table shape.
#
# NOT derived from: preorder index, JSON offset, `short_label` prose, or
# any other field that churns with renderer/formatter changes.

def _node_id_component(node, sibling_idx):
    det = node.get("details") or {}
    op = (det.get("operation") or "").strip().lower()
    tbl = (det.get("table_name") or "").strip().lower()
    at = (det.get("access_type") or "").strip().lower()
    key = (det.get("index_name") or "").strip().lower()
    return "{}|{}|{}|{}|{}".format(op, tbl, at, key, sibling_idx)


def _assign_node_ids(root, _parent_path="", _sibling_idx=0):
    """Post-parse walk that attaches a ``node_id`` to every tree node.

    The id is ``n:`` + 12 hex chars of sha1(path-through-tree). Stable
    across runs; survives MariaDB normalization because we key on the
    normalized shape, not the raw JSON.
    """
    if not isinstance(root, dict):
        return
    my_path = _parent_path + "/" + _node_id_component(root, _sibling_idx)
    digest = hashlib.sha1(my_path.encode("utf-8")).hexdigest()[:12]
    root["node_id"] = "n:" + digest
    for i, child in enumerate(root.get("children") or []):
        _assign_node_ids(child, my_path, i)


# ---------------------------------------------------------------------------
# SQL formatting
# ---------------------------------------------------------------------------

def format_sql(sql):
    """Best-effort SQL beautifier. Returns a list of display lines.

    Handles MySQL's ``/* select#N */`` comment prefix and backtick-quoted
    identifiers. No external dependencies required.
    """
    if not sql:
        return []
    # Strip MySQL's /* select#N */ comment prefix
    sql = re.sub(r'^/\*.*?\*/\s*', '', sql.strip(), flags=re.DOTALL)
    # Remove backtick quoting for readability: `table` -> table
    sql = re.sub(r'`([^`]+)`', r'\1', sql)
    # Normalize whitespace
    sql = re.sub(r'\s+', ' ', sql.strip())

    # Keywords that begin a new line (longer variants first to avoid partial matches)
    BREAK_BEFORE = sorted([
        "SELECT", "FROM",
        "INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL OUTER JOIN", "CROSS JOIN", "JOIN",
        "WHERE", "GROUP BY", "HAVING", "ORDER BY",
        "LIMIT", "OFFSET", "UNION ALL", "UNION", "WITH",
        "INSERT INTO", "UPDATE", "DELETE FROM", "SET", "VALUES",
    ], key=len, reverse=True)

    for kw in BREAK_BEFORE:
        sql = re.sub(
            r'(?<![.\w])(' + re.escape(kw) + r')(?![.\w])',
            r'\n\1', sql, flags=re.IGNORECASE,
        )

    raw_lines = [l.strip() for l in sql.split('\n') if l.strip()]
    result = []
    for line in raw_lines:
        upper = line.upper()
        if any(upper.startswith(kw) for kw in ('AND ', 'OR ', 'ON ')):
            result.append('    ' + line)
        elif any(upper.startswith(kw) for kw in (
            'INNER JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'CROSS JOIN', 'FULL OUTER JOIN', 'JOIN',
        )):
            result.append('  ' + line)
        else:
            result.append(line)
    return result


# ---------------------------------------------------------------------------
# Index suggestion heuristics
# ---------------------------------------------------------------------------

def _col_refs_for_table(condition, table_name):
    """Extract column names referenced for *table_name* in a condition string.

    Tries the exact table name first (``table.col`` or `` `table`.`col` ``),
    then falls back to a single-letter alias matching the first letter of the
    table name (common in MySQL auto-generated EXPLAIN output, e.g. ``o.status``
    when the table is ``orders``).  Returns a deduped, order-preserving list.
    """
    if not condition or not table_name:
        return []
    def _find(prefix):
        pat = r'`?' + re.escape(prefix) + r'`?\.`?(\w+)`?'
        return re.findall(pat, condition, re.IGNORECASE)

    cols = _find(table_name)
    if not cols and len(table_name) >= 2:
        cols = _find(table_name[0])  # single-char alias heuristic
    # Deduplicate, preserve order, skip obvious non-column names
    seen = set()
    result = []
    for c in cols:
        lc = c.lower()
        if lc not in seen:
            seen.add(lc)
            result.append(c)
    return result


def _suggest_indexes(root):
    """Walk the parsed EXPLAIN tree and return concrete index suggestions.

    Looks for Filter nodes whose conditions sit above full-table-scan nodes.
    Returns a list of dicts: ``{"table", "columns", "ddl", "reason"}``.
    """
    suggestions = []
    seen_ddl = set()

    def _walk(node, inherited_condition=None):
        details = node.get("details") or {}
        access_type = (details.get("access_type") or "").lower()
        condition = details.get("condition") or ""
        table_name = details.get("table_name") or ""
        children = node.get("children") or []

        # Effective condition: own condition takes priority; fall back to parent filter
        effective_cond = condition or inherited_condition or ""

        if access_type == "table" and table_name and effective_cond:
            cols = _col_refs_for_table(effective_cond, table_name)
            if cols:
                idx_name = "idx_{}_{}".format(table_name, "_".join(cols[:3]))
                cols_ddl = ", ".join(cols[:3])
                ddl = "CREATE INDEX {} ON {} ({});".format(idx_name, table_name, cols_ddl)
                if ddl not in seen_ddl:
                    seen_ddl.add(ddl)
                    suggestions.append({
                        "table": table_name,
                        "columns": cols[:3],
                        "ddl": ddl,
                        "reason": "Full scan on {} with filter on ({})".format(
                            table_name, cols_ddl
                        ),
                    })

        # Pass filter condition down so child table-scan nodes can see it
        child_cond = condition if condition else inherited_condition
        for child in children:
            _walk(child, inherited_condition=child_cond)

    _walk(root)
    return suggestions


def xml_escape(s):
    if s is None:
        return ""
    s = str(s)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return s


def build_short_label(op, table=None, index=None, condition=None):
    table = table or ""
    index = index or ""
    condition = condition or ""
    op = (op or "").replace("`", "")
    if re.match(r"^Table scan", op, re.I):
        return "Table scan" + (f" [{table}]" if table else "")
    if re.match(r"^Index range scan", op, re.I):
        return "Index range scan" + (f" [{table}.{index}]" if (table and index) else "")
    if re.match(r"^Index scan", op, re.I):
        return "Index scan" + (f" [{table}.{index}]" if (table and index) else "")
    if re.match(r"^Index lookup", op, re.I):
        return "Index lookup" + (f" [{table}.{index}]" if (table and index) else "")
    if re.match(r"^Single-row index lookup", op, re.I):
        return "Single-row lookup" + (f" [{table}.{index}]" if (table and index) else "")
    if re.match(r"^Covering index", op, re.I):
        return "Covering index" + (f" [{table}.{index}]" if (table and index) else "")
    if re.match(r"^Filter", op, re.I):
        cond = (condition or "").replace("`", "")
        if len(cond) > 43:
            cond = cond[:40] + "..."
        return f"Filter: ({cond})"
    if re.match(r"^Sort", op, re.I):
        m = re.search(r"limit input to (\d+)", op, re.I)
        return f"Sort (limit {m.group(1)})" if m else "Sort"
    if re.match(r"^Nested loop", op, re.I):
        if "inner" in op.lower():
            return "Nested loop inner join"
        if "left" in op.lower():
            return "Nested loop left join"
        if "semi" in op.lower():
            return "Nested loop semi join"
        return "Nested loop join"
    if re.match(r"^Aggregate", op, re.I):
        return "Aggregate"
    if re.match(r"^Group", op, re.I):
        return "Group"
    if re.match(r"^Materialize", op, re.I):
        return "Materialize"
    if re.match(r"^Stream results", op, re.I):
        return "Stream results"
    if re.match(r"^Limit", op, re.I):
        m = re.search(r"(\d+) row", op, re.I)
        return f"Limit: {m.group(1)} rows" if m else "Limit"
    if re.match(r"^Intersect", op, re.I):
        return "Intersect (row ID)"
    if re.match(r"^Union", op, re.I):
        return "Union"
    label = op[:50] + "..." if len(op) > 53 else op
    return label


def build_folded_label(node):
    op = (node.get("operation") or "unknown").replace("`", "")
    table = node.get("table_name") or ""
    index = node.get("index_name") or ""
    rows = node.get("actual_rows")
    loops = node.get("actual_loops", 1)
    if re.match(r"^Table scan", op, re.I):
        label = f"TABLE SCAN [{table}]"
    elif re.match(r"^Index range scan", op, re.I):
        label = f"INDEX RANGE SCAN [{table}.{index}]"
    elif re.match(r"^Index scan", op, re.I):
        label = f"INDEX SCAN [{table}.{index}]"
    elif re.match(r"^Index lookup", op, re.I):
        label = f"INDEX LOOKUP [{table}.{index}]"
    elif re.match(r"^Single-row index lookup", op, re.I):
        label = f"SINGLE ROW LOOKUP [{table}.{index}]"
    elif re.match(r"^Covering index", op, re.I):
        label = f"COVERING INDEX [{table}.{index}]"
    elif re.match(r"^Filter", op, re.I):
        cond = (node.get("condition") or "").replace("`", "")
        if len(cond) > 52:
            cond = cond[:50] + ".."
        label = f"FILTER ({cond})"
    elif re.match(r"^Sort", op, re.I):
        label = "SORT"
        if "row IDs" in op:
            label += " (row IDs)"
        if "filesort" in op:
            label += " (filesort)"
    elif re.match(r"^Nested loop", op, re.I):
        if "inner" in op.lower():
            label = "NESTED LOOP INNER"
        elif "left" in op.lower():
            label = "NESTED LOOP LEFT"
        elif "semi" in op.lower():
            label = "NESTED LOOP SEMI"
        elif "anti" in op.lower():
            label = "NESTED LOOP ANTI"
        else:
            label = "NESTED LOOP"
    elif re.match(r"^Aggregate", op, re.I):
        label = "AGGREGATE"
    elif re.match(r"^Group", op, re.I):
        label = "GROUP"
    elif re.match(r"^Materialize", op, re.I):
        label = "MATERIALIZE"
    elif re.match(r"^Stream results", op, re.I):
        label = "STREAM"
    elif re.match(r"^Limit", op, re.I):
        label = "LIMIT"
    elif re.match(r"^Intersect", op, re.I):
        label = "Intersect rows sorted by row ID"
    elif re.match(r"^Union", op, re.I):
        label = "UNION"
    else:
        label = op[:60] + ".." if len(op) > 62 else op
    metrics = []
    if loops is not None:
        metrics.append(f"starts={loops}")
    if rows is not None:
        metrics.append(f"rows={int(rows + 0.5)}")
    if metrics:
        label += " " + " ".join(metrics)
    return label.replace(";", "_")


def parse_node(node):
    if not isinstance(node, dict):
        return None
    op = (node.get("operation") or "unknown").replace("`", "")
    op = re.sub(r"DATE'(\d{4}-\d{2}-\d{2})'", r"\1", op)
    loops = node.get("actual_loops", 1) or 1
    total_time = (node.get("actual_last_row_ms") or 0) * loops
    inputs = node.get("inputs")
    # Preserve execution order (inputs[0]=outer, inputs[1]=inner for joins). Do not sort;
    # reordering breaks the diagram's left-to-right join order per VISUAL_EXPLAIN_PLAN_CONTEXT.md.
    children_refs = list(inputs) if isinstance(inputs, list) else []
    children = []
    children_time = 0
    for c in children_refs:
        child = parse_node(c)
        if child:
            children.append(child)
            children_time += child["total_time"]
    self_time = max(0.0, total_time - children_time)
    short = build_short_label(
        op,
        node.get("table_name"),
        node.get("index_name"),
        node.get("condition"),
    )
    folded = build_folded_label(node)
    details = {
        "operation": node.get("operation") or "",
        "table_name": node.get("table_name") or "",
        "index_name": node.get("index_name") or "",
        "access_type": node.get("access_type") or "",
        "actual_rows": node.get("actual_rows"),
        "actual_loops": node.get("actual_loops", 1),
        "estimated_rows": node.get("estimated_rows"),
        "actual_last_row_ms": node.get("actual_last_row_ms"),
        "actual_first_row_ms": node.get("actual_first_row_ms"),
        "estimated_total_cost": node.get("estimated_total_cost"),
        "condition": node.get("condition") or "",
        "ranges": node.get("ranges") or [],
        "covering": node.get("covering"),
        "schema_name": node.get("schema_name") or "",
        "join_algorithm": node.get("join_algorithm") or "",
        "pushed_index_condition": bool(node.get("pushed_index_condition")),
        "using_join_buffer": (node.get("using_join_buffer") or "").strip(),
        # Fields used by optimizer_switch detection (see _detect_optimizer_switches).
        "index_access_type": node.get("index_access_type") or "",
        "hash_condition": node.get("hash_condition") or [],
        "using_mrr": bool(node.get("using_mrr")),
        "using_rowid_filter": bool(node.get("using_rowid_filter")),
        "mariadb_block_nl_join": node.get("mariadb_block_nl_join") or {},
        "mariadb_index_merge": node.get("mariadb_index_merge") or {},
    }
    result = {
        "short_label": short,
        "folded_label": folded,
        "full_label": op,
        "details": details,
        "self_time": self_time,
        "total_time": total_time,
        "rows": node.get("actual_rows") or 0,
        "loops": loops,
        "children": children,
    }
    # Attach Big O complexity once so every downstream consumer (flamegraph,
    # bargraph, treemap, diagram, JSON sidecar) reads the same field. Returns
    # None for unknowns; we only insert the key when we have a real answer.
    complexity = compute_complexity(result)
    if complexity is not None:
        details["complexity"] = complexity
    return result


def load_explain_json(text):
    """Load EXPLAIN JSON, tolerating common MySQL CLI output quirks.

    Handles:
    - ``EXPLAIN:`` prefix
    - ``EXPLAIN`` column header (from ``mysql -e`` without ``-N``)
    - Table-formatted output (``+---+`` borders and ``| ... |`` rows)
    - Escaped newlines/tabs (from ``mysql -e`` without ``-r``)
    - UTF-8 BOM
    """
    # Strip UTF-8 BOM
    text = text.lstrip("\ufeff")
    text = text.strip()

    # Strip EXPLAIN: prefix (e.g. from MySQL shell output)
    text = re.sub(r"^.*?EXPLAIN:\s*", "", text, flags=re.DOTALL)

    # Strip MySQL table borders:  +----...----+
    text = re.sub(r"^\+[-+]+\+\s*", "", text, flags=re.MULTILINE)

    # Strip leading/trailing pipe from table rows:  | ... |
    lines = text.split("\n")
    stripped = []
    for line in lines:
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            line = line[1:-1].strip()
        stripped.append(line)
    text = "\n".join(stripped)

    # Strip EXPLAIN/ANALYZE column header (from mysql/mariadb -e without -N)
    text = text.strip()
    text = re.sub(r"^(?:EXPLAIN|ANALYZE)\s*\n", "", text)

    # Try parsing as-is first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Handle escaped newlines/tabs from mysql -e without -r:
    # MySQL outputs literal \n and \t instead of real newlines/tabs.
    # Replace literal backslash-n/t (outside of JSON string values) with
    # real newlines — but only if the text looks like single-line JSON.
    if "\n" not in text.strip() and "\\n" in text:
        unescaped = text.replace("\\n", "\n").replace("\\t", "\t")
        try:
            return json.loads(unescaped)
        except json.JSONDecodeError:
            pass

    # Nothing worked — raise a helpful error
    # Find where the JSON likely starts
    json_start = re.search(r'[{\[]', text)
    if json_start and json_start.start() > 0:
        try:
            return json.loads(text[json_start.start():])
        except json.JSONDecodeError:
            pass

    raise ValueError(
        "Cannot parse as JSON. If using mysql CLI, add -s -N -r flags: "
        "mysql -s -N -r -e 'EXPLAIN ANALYZE FORMAT=JSON ...' or "
        "mariadb -s -N -r -e 'ANALYZE FORMAT=JSON ...'"
    )


def _is_mariadb_format(data):
    """Detect MariaDB ANALYZE FORMAT=JSON by its distinctive structure."""
    return isinstance(data, dict) and "query_block" in data


def _mariadb_access_type_to_operation(access_type, table_name, key, using_index=False):
    """Map MariaDB access_type + context to a MySQL-style operation string."""
    at = (access_type or "").upper()
    tbl = table_name or ""
    idx = key or ""
    if at == "ALL":
        return "Table scan on {}".format(tbl) if tbl else "Table scan"
    if at in ("EQ_REF", "REF", "REF_OR_NULL"):
        if idx:
            return "Index lookup on {} using {}".format(tbl, idx)
        return "Index lookup on {}".format(tbl) if tbl else "Index lookup"
    if at == "RANGE":
        if idx:
            return "Index range scan on {} using {}".format(tbl, idx)
        return "Index range scan on {}".format(tbl) if tbl else "Index range scan"
    if at == "INDEX":
        if using_index:
            if idx:
                return "Covering index scan on {} using {}".format(tbl, idx)
            return "Covering index scan on {}".format(tbl) if tbl else "Covering index scan"
        if idx:
            return "Index scan on {} using {}".format(tbl, idx)
        return "Index scan on {}".format(tbl) if tbl else "Index scan"
    if at == "CONST" or at == "SYSTEM":
        if idx:
            return "Single-row index lookup on {} using {}".format(tbl, idx)
        return "Constant table lookup on {}".format(tbl) if tbl else "Constant lookup"
    if at == "UNIQUE_SUBQUERY":
        return "Index lookup on {} using {}".format(tbl, idx) if idx else "Unique subquery"
    if at == "INDEX_SUBQUERY":
        return "Index range scan on {} using {}".format(tbl, idx) if idx else "Index subquery"
    if at == "FULLTEXT":
        return "Fulltext index scan on {} using {}".format(tbl, idx) if idx else "Fulltext scan"
    # Fallback
    if tbl:
        return "Table scan on {}".format(tbl)
    return access_type or "unknown"


def _normalize_mariadb_table(tbl):
    """Convert a MariaDB ``table`` object into a MySQL-compatible node dict."""
    table_name = tbl.get("table_name") or ""
    access_type_raw = (tbl.get("access_type") or "").upper()
    key = tbl.get("key") or ""
    using_index = bool(tbl.get("using_index"))
    condition = tbl.get("attached_condition") or ""
    # Preserve optimizer-switch evidence that MariaDB exposes on the table node
    # so _detect_optimizer_switches() can report it after normalization.
    mariadb_index_merge = tbl.get("index_merge") or {}
    has_rowid_filter = bool(tbl.get("rowid_filter"))
    mrr_type = tbl.get("mrr_type") or ""
    has_pushed_index_cond = bool(tbl.get("index_condition") or tbl.get("index_condition_bka"))

    r_table_time = tbl.get("r_table_time_ms") or 0
    r_other_time = tbl.get("r_other_time_ms") or 0
    total_ms = r_table_time + r_other_time

    r_rows = tbl.get("r_rows")
    r_loops = tbl.get("r_loops") or tbl.get("loops") or 1

    # MariaDB r_table_time_ms is TOTAL across all loops.  parse_node()
    # multiplies actual_last_row_ms * actual_loops, so store per-loop time.
    per_loop_ms = total_ms / r_loops if r_loops > 0 else total_ms
    est_rows = tbl.get("rows")
    cost = tbl.get("cost")

    operation = _mariadb_access_type_to_operation(access_type_raw, table_name, key, using_index)

    # Map MariaDB access_type to MySQL's internal access_type categories
    mysql_access_type = "table"  # default for ALL
    if access_type_raw in ("EQ_REF", "REF", "REF_OR_NULL", "CONST", "SYSTEM"):
        mysql_access_type = "ref"
    elif access_type_raw == "RANGE":
        mysql_access_type = "range"
    elif access_type_raw == "INDEX":
        mysql_access_type = "index"
    elif access_type_raw in ("UNIQUE_SUBQUERY", "INDEX_SUBQUERY"):
        mysql_access_type = "ref"
    elif access_type_raw == "FULLTEXT":
        mysql_access_type = "fulltext"
    elif access_type_raw == "INDEX_MERGE":
        # MariaDB's own access_type for index_merge. Use a MySQL-compatible marker
        # ("rowid_union" / "rowid_intersection" / "rowid_sort_union") so both
        # engines go through the same index_merge detection branch.
        if "sort_intersect" in mariadb_index_merge:
            mysql_access_type = "rowid_sort_intersection"
            operation = "Intersect rows sorted by row ID on {}".format(table_name) if table_name else "Sort intersect index merge"
        elif "intersect" in mariadb_index_merge:
            mysql_access_type = "rowid_intersection"
            operation = "Intersect rows sorted by row ID on {}".format(table_name) if table_name else "Intersect index merge"
        elif "sort_union" in mariadb_index_merge:
            mysql_access_type = "rowid_sort_union"
            operation = "Deduplicate rows sorted by row ID on {}".format(table_name) if table_name else "Sort union index merge"
        else:
            mysql_access_type = "rowid_union"
            operation = "Deduplicate rows sorted by row ID on {}".format(table_name) if table_name else "Union index merge"

    node = {
        "operation": operation,
        "table_name": table_name,
        "index_name": key,
        "access_type": mysql_access_type,
        "actual_rows": float(r_rows) if r_rows is not None else 0.0,
        "actual_loops": int(r_loops),
        "actual_last_row_ms": per_loop_ms,
        "estimated_rows": float(est_rows) if est_rows is not None else None,
        "estimated_total_cost": float(cost) if cost is not None else None,
        "condition": condition,
        "covering": using_index and access_type_raw == "INDEX",
        "schema_name": "",
        "inputs": [],
        # Optimizer-switch evidence preserved for _detect_optimizer_switches.
        # mariadb_index_merge carries the variant name (union/sort_union/intersect/sort_intersect);
        # using_rowid_filter signals MariaDB's rowid_filter pruning;
        # using_mrr signals Multi-Range Read on the range access.
        "mariadb_index_merge": mariadb_index_merge,
        "using_rowid_filter": has_rowid_filter,
        "using_mrr": bool(mrr_type),
        "pushed_index_condition": has_pushed_index_cond,
    }

    # Handle materialized subqueries/derived tables
    mat = tbl.get("materialized")
    if mat and isinstance(mat, dict):
        qb = mat.get("query_block")
        if qb:
            child = _normalize_mariadb_query_block(qb)
            if child:
                node["operation"] = "Materialize"
                node["access_type"] = "materialize"
                node["inputs"] = [child]

    return node


def _normalize_mariadb_nested_loop(nested_loop):
    """Convert MariaDB nested_loop array into a MySQL-compatible tree.

    MariaDB stores the join as a flat array: [table1, table2, ...].
    MySQL stores it as nested: {operation: "Nested loop", inputs: [outer, inner]}.
    We fold the flat list into a right-deep nested tree to match MySQL style.

    Entries can be ``{"table": {...}}``, ``{"read_sorted_file": {...}}``, or
    other wrapper objects.
    """
    if not nested_loop:
        return None

    # Normalize each entry — can be table, read_sorted_file, block-nl-join, etc.
    table_nodes = []
    for entry in nested_loop:
        if not isinstance(entry, dict):
            continue
        if "table" in entry:
            table_nodes.append(_normalize_mariadb_table(entry["table"]))
        elif "read_sorted_file" in entry:
            node = _normalize_mariadb_read_sorted_file(entry["read_sorted_file"])
            if node:
                table_nodes.append(node)
        elif "block-nl-join" in entry:
            # MariaDB wraps the inner side of a join-buffer join here. The inner
            # "table" can be reached via a classic BNL, BNLH (hash), BKA or BKAH
            # depending on optimizer_switch (join_cache_* / outer_join_with_cache).
            bnl_wrapper = entry["block-nl-join"] or {}
            inner_tbl = bnl_wrapper.get("table")
            if isinstance(inner_tbl, dict):
                node = _normalize_mariadb_table(inner_tbl)
                join_type = (bnl_wrapper.get("join_type") or "").upper()
                buffer_type = bnl_wrapper.get("buffer_type") or ""
                # Map MariaDB join_type ∈ {BNL, BNLH, BKA, BKAH} to the MySQL
                # semantics that _detect_optimizer_switches already understands.
                if "BNLH" in join_type or "hash" in (buffer_type or "").lower():
                    node["join_algorithm"] = "hash"
                    node["using_join_buffer"] = "Block Nested Loop (hash)"
                elif join_type.startswith("BKA"):
                    node["join_algorithm"] = "batch_key_access"
                    node["using_join_buffer"] = "Batched Key Access"
                else:
                    node["using_join_buffer"] = "Block Nested Loop"
                node["mariadb_block_nl_join"] = {
                    "join_type": join_type,
                    "buffer_type": buffer_type,
                    "buffer_size": bnl_wrapper.get("buffer_size") or "",
                }
                # Preserve the BNL wrapper's attached_condition (the real ON
                # predicate) so the advisor's non-sargable detection rule can
                # see it. Without this, the condition is lost in the
                # normalized tree and any CONCAT/CAST/LOWER join key goes
                # undetected on MariaDB.
                wrapper_cond = bnl_wrapper.get("attached_condition") or ""
                if wrapper_cond and not node.get("condition"):
                    node["condition"] = wrapper_cond
                table_nodes.append(node)
        elif "duplicates_removal" in entry:
            # MariaDB's DuplicateWeedout semijoin strategy.
            inner = _normalize_mariadb_nested_loop(entry["duplicates_removal"])
            if inner:
                weedout = {
                    "operation": "Remove duplicates using temporary table (weedout)",
                    "access_type": "weedout",
                    "actual_last_row_ms": inner.get("actual_last_row_ms", 0),
                    "actual_loops": inner.get("actual_loops", 1),
                    "actual_rows": inner.get("actual_rows", 0),
                    "inputs": [inner],
                }
                table_nodes.append(weedout)
        elif "firstmatch" in entry:
            inner = _normalize_mariadb_nested_loop(entry["firstmatch"])
            if inner:
                fm = {
                    "operation": "FirstMatch semijoin",
                    "access_type": "semijoin",
                    "actual_last_row_ms": inner.get("actual_last_row_ms", 0),
                    "actual_loops": inner.get("actual_loops", 1),
                    "actual_rows": inner.get("actual_rows", 0),
                    "inputs": [inner],
                }
                table_nodes.append(fm)
        elif "loosescan" in entry:
            inner = _normalize_mariadb_nested_loop(entry["loosescan"])
            if inner:
                ls = {
                    "operation": "LooseScan semijoin",
                    "access_type": "semijoin",
                    "actual_last_row_ms": inner.get("actual_last_row_ms", 0),
                    "actual_loops": inner.get("actual_loops", 1),
                    "actual_rows": inner.get("actual_rows", 0),
                    "inputs": [inner],
                }
                table_nodes.append(ls)
        elif "range-checked-for-each-record" in entry:
            # MariaDB emits this when the optimizer decided at plan time that
            # a single access method wasn't stable across outer rows — so it
            # re-picks per outer row between a full scan, an index_merge, or
            # one of several named ranges. Verified in
            # sql/sql_explain.cc:3111 (Explain_range_checked_fer::print_json).
            # The wrapper contains the candidate "keys" and an optional
            # "table" child with the default access; surface it so advisor /
            # UI can flag the "re-decide per outer row" cost.
            rcf = entry["range-checked-for-each-record"] or {}
            inner_tbl = rcf.get("table")
            if isinstance(inner_tbl, dict):
                node = _normalize_mariadb_table(inner_tbl)
                node["mariadb_range_checked"] = {
                    "keys": list(rcf.get("keys") or []),
                    "r_keys": rcf.get("r_keys") or {},
                }
                # Force the access_type signal so advisor rules that key on
                # "this plan re-decides per row" can match it.
                node["range_checked_per_record"] = True
                table_nodes.append(node)

    if not table_nodes:
        return None
    if len(table_nodes) == 1:
        return table_nodes[0]

    # Fold into a right-deep tree: ((t1 NL t2) NL t3) NL t4...
    result = table_nodes[0]
    for i in range(1, len(table_nodes)):
        inner = table_nodes[i]
        # Estimate total time from the query_block level (will be overridden later)
        nl_time = max(
            result.get("actual_last_row_ms", 0) * (result.get("actual_loops", 1) or 1),
            inner.get("actual_last_row_ms", 0) * (inner.get("actual_loops", 1) or 1),
        )
        join_node = {
            "operation": "Nested loop inner join",
            "access_type": "join",
            "join_algorithm": "nested_loop",
            "actual_last_row_ms": 0.0,  # will be set from query_block r_total_time_ms
            "actual_loops": 1,
            "actual_rows": float(inner.get("actual_rows", 0)),
            "inputs": [result, inner],
        }
        result = join_node

    return result


def _normalize_mariadb_read_sorted_file(rsf):
    """Convert MariaDB read_sorted_file into a Sort node.

    Structure: ``read_sorted_file: { r_rows, filesort: { table: {...} } }``
    Used when MariaDB reads a sorted temp file (e.g. ORDER BY ... LIMIT).
    """
    if not isinstance(rsf, dict):
        return None
    filesort = rsf.get("filesort")
    if not filesort or not isinstance(filesort, dict):
        return None

    # The table is directly inside the filesort (not in nested_loop)
    child = None
    tbl = filesort.get("table")
    if tbl and isinstance(tbl, dict):
        child = _normalize_mariadb_table(tbl)
    elif filesort.get("nested_loop"):
        child = _normalize_mariadb_nested_loop(filesort["nested_loop"])

    sort_key = filesort.get("sort_key") or ""
    r_time = filesort.get("r_total_time_ms") or 0
    r_output_rows = filesort.get("r_output_rows") or rsf.get("r_rows") or 0

    return {
        "operation": "Sort: {}".format(sort_key) if sort_key else "Sort",
        "access_type": "sort",
        "actual_last_row_ms": float(r_time),
        "actual_loops": filesort.get("r_loops") or 1,
        "actual_rows": float(r_output_rows),
        "inputs": [child] if child else [],
    }


def _normalize_mariadb_filesort(filesort, query_block_time=0):
    """Convert MariaDB filesort structure into MySQL-compatible Sort node."""
    child = None
    temp = filesort.get("temporary_table")
    if temp and isinstance(temp, dict):
        nl = temp.get("nested_loop")
        if nl:
            child = _normalize_mariadb_nested_loop(nl)
    elif filesort.get("nested_loop"):
        child = _normalize_mariadb_nested_loop(filesort["nested_loop"])

    sort_key = filesort.get("sort_key") or ""
    r_time = filesort.get("r_total_time_ms") or 0
    r_output_rows = filesort.get("r_output_rows") or 0

    sort_node = {
        "operation": "Sort: {}".format(sort_key) if sort_key else "Sort",
        "access_type": "sort",
        "actual_last_row_ms": float(r_time),
        "actual_loops": filesort.get("r_loops") or 1,
        "actual_rows": float(r_output_rows),
        "inputs": [child] if child else [],
    }
    return sort_node


def _normalize_mariadb_query_block(qb):
    """Convert a MariaDB query_block into a MySQL-compatible tree node."""
    if not isinstance(qb, dict):
        return None

    qb_time = qb.get("r_total_time_ms") or 0
    qb_loops = qb.get("r_loops") or 1

    # UNION handling
    union_result = qb.get("union_result")
    if union_result and isinstance(union_result, dict):
        specs = union_result.get("query_specifications") or []
        children = []
        for spec in specs:
            inner_qb = spec.get("query_block") if isinstance(spec, dict) else None
            if inner_qb:
                child = _normalize_mariadb_query_block(inner_qb)
                if child:
                    children.append(child)
        if children:
            # Union query_block often lacks r_total_time_ms; use sum of children
            union_time = qb_time
            if union_time == 0:
                union_time = sum(
                    c.get("actual_last_row_ms", 0) * (c.get("actual_loops", 1) or 1)
                    for c in children
                )
            return {
                "operation": "Union",
                "access_type": "union",
                "actual_last_row_ms": float(union_time),
                "actual_loops": qb_loops,
                "actual_rows": float(sum(c.get("actual_rows", 0) for c in children)),
                "inputs": children,
            }
        return None

    # Window functions: sorts[] + temporary_table with nested_loop
    wfc = qb.get("window_functions_computation")
    if wfc and isinstance(wfc, dict):
        child = None
        temp = wfc.get("temporary_table")
        if temp and isinstance(temp, dict):
            nl = temp.get("nested_loop")
            if nl:
                child = _normalize_mariadb_nested_loop(nl)

        # Window sorts are overhead; sum their times
        sorts = wfc.get("sorts") or []
        sort_time = 0.0
        for s in sorts:
            fs = s.get("filesort") if isinstance(s, dict) else None
            if fs:
                sort_time += fs.get("r_total_time_ms") or 0

        wf_node = {
            "operation": "Window functions",
            "access_type": "sort",
            "actual_last_row_ms": float(qb_time),
            "actual_loops": qb_loops,
            "actual_rows": float(child.get("actual_rows", 0) if child else 0),
            "inputs": [child] if child else [],
        }
        return wf_node

    # Filesort wrapping
    filesort = qb.get("filesort")
    if filesort and isinstance(filesort, dict):
        sort_node = _normalize_mariadb_filesort(filesort, qb_time)
        # The sort node's time is just the sort overhead; propagate qb total time
        if sort_node.get("inputs"):
            # Set nested loop total time from qb level
            _propagate_mariadb_time(sort_node, qb_time, qb_loops)
        return sort_node

    # Grouping (GROUP BY without filesort via temporary table)
    group_by = qb.get("grouping_operation")
    if group_by and isinstance(group_by, dict):
        child = None
        nl = group_by.get("nested_loop")
        if nl:
            child = _normalize_mariadb_nested_loop(nl)
        group_node = {
            "operation": "Group",
            "access_type": "group",
            "actual_last_row_ms": float(group_by.get("r_total_time_ms") or 0),
            "actual_loops": group_by.get("r_loops") or 1,
            "actual_rows": 0.0,
            "inputs": [child] if child else [],
        }
        return group_node

    # Plain nested_loop
    nl = qb.get("nested_loop")
    if nl:
        result = _normalize_mariadb_nested_loop(nl)
        if result:
            _propagate_mariadb_time(result, qb_time, qb_loops)
        return result

    return None


def _propagate_mariadb_time(node, qb_time, qb_loops):
    """Set the root join node's actual_last_row_ms from the query_block total time.

    MariaDB provides per-table r_table_time_ms but the join node itself has no
    inherent timing; we use the query_block's r_total_time_ms.  parse_node()
    will multiply by actual_loops, so store per-loop time.
    """
    if not node:
        return
    if node.get("access_type") in ("join", "sort", "group", "union"):
        loops = node.get("actual_loops") or qb_loops or 1
        # qb_time is already total across all qb_loops; store per-loop value
        node["actual_last_row_ms"] = qb_time / loops if loops > 0 else qb_time


def _normalize_mariadb(data):
    """Convert MariaDB ANALYZE FORMAT=JSON into MySQL-compatible tree.

    MariaDB uses a radically different JSON schema:
      - query_block.nested_loop[].table instead of operation/inputs tree
      - r_table_time_ms/r_other_time_ms instead of actual_last_row_ms
      - r_rows/rows instead of actual_rows/estimated_rows
      - attached_condition instead of condition
      - filesort/temporary_table wrappers for ORDER BY
      - materialized/query_block for subqueries and derived tables
      - union_result/query_specifications for UNION

    This function translates MariaDB's format into MySQL's tree format so the
    rest of the parser works unchanged.
    """
    qb = data.get("query_block")
    if not isinstance(qb, dict):
        raise ValueError("MariaDB ANALYZE JSON missing query_block")
    result = _normalize_mariadb_query_block(qb)
    if not result:
        raise ValueError("Failed to normalize MariaDB ANALYZE JSON")
    return result


def parse_explain(text):
    """Parse EXPLAIN JSON text into root tree node."""
    data = load_explain_json(text)
    # MySQL 9.7+ wraps the plan in a "query_plan" key
    if "query_plan" in data and isinstance(data["query_plan"], dict):
        data = data["query_plan"]
    # MariaDB 10.5+ / 11.x uses a completely different JSON schema
    if _is_mariadb_format(data):
        data = _normalize_mariadb(data)
    root = parse_node(data)
    if not root:
        raise ValueError("Failed to parse EXPLAIN JSON")
    _assign_node_ids(root)
    return root


def build_flame_entries(node, path=None):
    """Yield (path_list, self_time) for flame graph."""
    path = list(path or [])
    path.append(node["folded_label"])
    yield (path, node["self_time"])
    for child in node["children"]:
        yield from build_flame_entries(child, path)


def flatten_nodes(node):
    """Yield all nodes in tree (for bargraph)."""
    yield node
    for child in node["children"]:
        yield from flatten_nodes(child)


def build_diagram_steps(node):
    """
    Build a left-to-right list of steps for a Visual Explain–style diagram.
    Returns list of {"type": "access"|"join", "node": node}.
    Single-child nodes (e.g. Filter) are skipped; joins emit outer chain + join + inner.
    """
    children = node.get("children") or []
    if not children:
        return [{"type": "access", "node": node}]
    if len(children) == 1:
        return build_diagram_steps(children[0])
    # Nested loop (or similar): outer, then join, then inner(s)
    outer = children[0]
    inner = children[1]
    result = build_diagram_steps(outer)
    result.append({"type": "join", "node": node})
    result.extend(build_diagram_steps(inner))
    return result


def enhance_tooltip_flame(original, op_details):
    """Enhance tooltip using op_details { folded_label -> details }."""
    best = None
    best_score = 0
    for label, d in op_details.items():
        score = 0
        if label.lower() in (original or "").lower():
            score += 20
        if d.get("index_name") and (d["index_name"] in (original or "") or f"using {d['index_name']}" in (original or "").lower()):
            score += 10
        if d.get("table_name") and d["table_name"] in (original or ""):
            score += 3
        if d.get("actual_rows") is not None:
            m = re.search(r"rows[=:]?\s*(\d+)\b", (original or ""), re.I)
            if m and int(float(d["actual_rows"]) + 0.5) == int(m.group(1)):
                score += 5
        m = re.search(r"starts[=:]?\s*(\d+)\b", (original or ""), re.I)
        if m and (d.get("actual_loops") or 1) == int(m.group(1)):
            score += 3
        if score > best_score and score >= 5:
            best_score = score
            best = d
    if not best:
        return original or ""
    # NOTE: we return RAW text; every caller re-escapes the whole blob via
    # ``xml_escape(enhanced)`` before wrapping it in a ``<title>``. Pre-
    # escaping here would produce ``&amp;lt;temporary&amp;gt;`` in the SVG
    # and the browser would display the literal ``&lt;`` / ``&gt;``.
    lines = [original, ""]
    if best.get("table_name"):
        t = (best.get("schema_name") or "") + "." + best["table_name"] if best.get("schema_name") else best["table_name"]
        idx = f" (index: {best['index_name']})" if best.get("index_name") else ""
        lines.append(f"Table: {t}{idx}")
    if best.get("access_type"):
        lines.append(f"Access: {best['access_type']}")
    if best.get("actual_rows") is not None:
        ri = f"Rows: {float(best['actual_rows']):.0f} actual"
        if best.get("estimated_rows") is not None:
            ri += f" ({float(best['estimated_rows']):.0f} estimated)"
        est = best.get("estimated_rows") or 0
        r = (best["actual_rows"] / est) if est > 0 else 0
        if r > 2:
            ri += " [UNDERESTIMATE]"
        if 0 < r < 0.5:
            ri += " [OVERESTIMATE]"
        lines.append(ri)
    if best.get("actual_loops") and best["actual_loops"] > 1:
        lines.append(f"Loops: {best['actual_loops']}")
    if best.get("actual_last_row_ms") is not None:
        lines.append(f"Time: {best['actual_last_row_ms']:.3f} ms (last row)")
    if best.get("estimated_total_cost") is not None:
        lines.append(f"Cost: {best['estimated_total_cost']:.2f}")
    cond = best.get("condition") or ""
    if len(cond) > 83:
        cond = cond[:80] + "..."
    if best.get("condition"):
        lines.append(f"Condition: {cond}")
    if best.get("ranges"):
        lines.append("Ranges: " + ", ".join(best["ranges"]))
    if best.get("covering") is not None:
        lines.append("Covering: " + ("Yes" if best["covering"] else "No"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# optimizer_switch detection
# ---------------------------------------------------------------------------
#
# Each entry maps an @@optimizer_switch flag to a short explanation of what
# happens when the optimizer actually *uses* it in a query. Explanations are
# single-sentence and avoid buzzwords so they stay useful inside the SVG
# analysis panel.
#
# Verified against live instances:
#   - MySQL 8.4.8  (@@optimizer_switch — all default MySQL 8.4 switches)
#   - MariaDB 11.4 (@@optimizer_switch — all default MariaDB 11.4 switches)
#
# Only flags with a plan-level signature we can actually detect are listed.
OPTIMIZER_SWITCH_EXPLANATIONS = {
    # Both engines
    "hash_join": (
        "Builds an in-memory hash table on the smaller (build) input and "
        "probes it from the other side. Uses join_buffer_size; spills to a "
        "tmp file (and tmpdir) if the build side does not fit."
    ),
    "block_nested_loop": (
        "Buffers a batch of outer rows in a join buffer and scans the inner "
        "table once per batch instead of once per row. Inner still does a "
        "full/index/range scan — a missing index on the join column is the "
        "root cause."
    ),
    "batched_key_access": (
        "Collects keys from the outer table into the join buffer, sorts them, "
        "then uses MRR to fetch inner rows in (mostly) index order — cuts "
        "random I/O on large indexed joins."
    ),
    "mrr": (
        "Multi-Range Read: sorts row IDs before fetching rows so InnoDB reads "
        "pages in roughly primary-key order, turning random I/O into "
        "sequential I/O on range scans."
    ),
    "index_condition_pushdown": (
        "Parts of the WHERE clause are evaluated inside the storage engine "
        "using index columns before fetching the full row — reduces rows "
        "handed to the server layer."
    ),
    "index_merge": (
        "Uses two or more index range scans on the same table and combines "
        "row IDs (union/intersection/sort_union) instead of falling back to a "
        "full scan. A composite index is usually faster."
    ),
    "index_merge_union": (
        "Union variant of index_merge: OR'ed predicates are satisfied by "
        "scanning each index separately, then UNION'ing their row IDs."
    ),
    "index_merge_sort_union": (
        "Sort-union variant: like index_merge_union but row IDs are sorted "
        "before de-duplication (used when scans are not already ordered)."
    ),
    "index_merge_intersection": (
        "Intersection variant: AND'ed predicates are satisfied by scanning "
        "each index and intersecting row IDs — useful when no single "
        "composite index covers both predicates."
    ),
    "skip_scan": (
        "Uses a composite index even though the query does not filter on the "
        "leading column, by probing each distinct leading-column value. "
        "Only pays off when the leading column has very low cardinality."
    ),
    "materialization": (
        "A subquery or derived table is executed once and its result stored "
        "in a temporary table for reuse. Watch tmp_table_size / "
        "max_heap_table_size — it may spill to disk."
    ),
    "semijoin": (
        "IN / EXISTS subqueries are rewritten into a semi-join with the outer "
        "query and then resolved via one of the semijoin strategies "
        "(firstmatch, loosescan, duplicateweedout, or materialization)."
    ),
    "firstmatch": (
        "Semijoin strategy: stops scanning the inner table as soon as the "
        "first matching row is found for each outer row."
    ),
    "loosescan": (
        "Semijoin strategy: scans an index on the inner table exactly once, "
        "skipping ahead per distinct group instead of emitting duplicates."
    ),
    "duplicateweedout": (
        "Semijoin strategy: runs the subquery as a normal inner join and "
        "then removes duplicate outer rows using a temporary table keyed on "
        "their row IDs."
    ),
    "derived_merge": (
        "A derived table or view is merged into the outer query block "
        "instead of being materialized — the derived SELECT disappears from "
        "the plan."
    ),
    "use_index_extensions": (
        "The optimizer treats the primary-key columns appended to every "
        "secondary index as additional key parts — enables covering reads "
        "that would otherwise need a row fetch."
    ),
    # MySQL-only
    "hash_set_operations": (
        "UNION / INTERSECT / EXCEPT is resolved with an in-memory hash set "
        "instead of a sorted temporary table (MySQL 8.4+)."
    ),
    # MariaDB-only
    "rowid_filter": (
        "Before the index lookup, MariaDB builds a sorted row-id filter from "
        "a secondary index and uses it to prune non-matching rows — avoids "
        "dereferencing rows that would fail the final filter."
    ),
    "join_cache_bka": (
        "MariaDB variant of batched_key_access: inner rows are fetched in "
        "batches keyed from the join buffer, using MRR to reorder I/O."
    ),
    "join_cache_hashed": (
        "MariaDB variant of block_nested_loop: the join buffer is built as a "
        "hash table (buffer_type='incremental, hashed') — effectively a hash "
        "join on an indexed inner side."
    ),
    "outer_join_with_cache": (
        "MariaDB allows LEFT / RIGHT joins to use the join cache; without it "
        "MariaDB would fall back to per-row lookups on the inner side."
    ),
}


def _detect_optimizer_switches(root):
    """Walk the parsed EXPLAIN tree and return a list of detected
    @@optimizer_switch flags with evidence and explanation.

    Each entry is:
      {"name": "<flag_name>", "value": "on",
       "explanation": "<human readable impact>",
       "short_labels": [<node labels where flag is observed>]}

    Only flags with a concrete plan-level signature are reported — there is
    no guessing.  Both MySQL (8.0+, 8.4+, 9.x) and MariaDB (10.11+, 11.x)
    plans are supported because the MariaDB normalizer stashes MariaDB-
    specific markers onto ``details`` before parse_node() runs.
    """
    found = {}  # name -> {"short_labels": [...]}

    def _mark(name, label=None):
        entry = found.setdefault(name, {"short_labels": []})
        if label and label not in entry["short_labels"]:
            entry["short_labels"].append(label)

    def _walk(node):
        details = node.get("details") or {}
        op = (details.get("operation") or "").lower()
        access = (details.get("access_type") or "").lower()
        idx_access = (details.get("index_access_type") or "").lower()
        join_algo = (details.get("join_algorithm") or "").lower()
        buf = (details.get("using_join_buffer") or "").lower()
        label = node.get("short_label") or ""

        # hash_join — MySQL join_algorithm == "hash", or operation text.
        if join_algo == "hash" or "hash join" in op:
            _mark("hash_join", label)

        # batched_key_access — MySQL join_algorithm == "batch_key_access",
        # or MariaDB block-nl-join with BKA/BKAH buffer_type. The BKA plan
        # also shows "multi_range_read" on the inner side.
        if join_algo in ("batch_key_access", "batched_key_access"):
            _mark("batched_key_access", label)
            _mark("join_cache_bka", label)  # MariaDB equivalent
        elif "batched key access" in op or "batch_key_access" in op:
            _mark("batched_key_access", label)

        # block_nested_loop — "Using join buffer (Block Nested Loop)" (MySQL)
        # or MariaDB block-nl-join without BKA/BNLH (classic BNL).
        if "block" in buf and "nested" in buf and "hash" not in buf:
            _mark("block_nested_loop", label)
        elif "block" in buf and "hash" in buf:
            # MariaDB BNLH — buffer built as a hash table
            _mark("join_cache_hashed", label)
            _mark("hash_join", label)

        mdb_bnl = details.get("mariadb_block_nl_join") or {}
        if mdb_bnl:
            if "OUTER" in (mdb_bnl.get("join_type") or "").upper() or access == "left_join":
                _mark("outer_join_with_cache", label)

        # mrr — MySQL "multi_range_read" index access, or MariaDB using_mrr.
        if idx_access == "multi_range_read" or details.get("using_mrr") or "multi-range" in op:
            _mark("mrr", label)

        # index_condition_pushdown — pushed_index_condition or MariaDB index_condition.
        if details.get("pushed_index_condition"):
            _mark("index_condition_pushdown", label)

        # index_merge and variants — MySQL rowid_* access types, or MariaDB
        # mariadb_index_merge dict preserved by the normalizer.
        mdb_im = details.get("mariadb_index_merge") or {}
        if access == "rowid_union":
            _mark("index_merge", label)
            _mark("index_merge_union", label)
        elif access == "rowid_sort_union":
            _mark("index_merge", label)
            _mark("index_merge_sort_union", label)
        elif access == "rowid_intersection":
            _mark("index_merge", label)
            _mark("index_merge_intersection", label)
        elif access == "rowid_sort_intersection":
            _mark("index_merge", label)
            _mark("index_merge_intersection", label)
        elif "deduplicate rows sorted by row id" in op:
            _mark("index_merge", label)
            _mark("index_merge_union", label)
        elif "intersect rows sorted by row id" in op:
            _mark("index_merge", label)
            _mark("index_merge_intersection", label)
        if mdb_im:
            _mark("index_merge", label)
            if "intersect" in mdb_im:
                _mark("index_merge_intersection", label)
            if "sort_intersect" in mdb_im:
                _mark("index_merge_intersection", label)
            if "sort_union" in mdb_im:
                _mark("index_merge_sort_union", label)
            if "union" in mdb_im and "sort_union" not in mdb_im:
                _mark("index_merge_union", label)

        # skip_scan — index_access_type "index_skip_scan" or operation text.
        if idx_access == "index_skip_scan" or "skip scan" in op:
            _mark("skip_scan", label)

        # materialization — Materialize op / access_type == "materialize".
        if access == "materialize" or op.startswith("materialize"):
            _mark("materialization", label)

        # semijoin strategies. MySQL writes them as operations / access types;
        # MariaDB also exposes distinct wrappers that we normalize earlier.
        if "semi" in op and "join" in op:
            _mark("semijoin", label)
        if "first match" in op or "firstmatch" in op.replace(" ", ""):
            _mark("semijoin", label)
            _mark("firstmatch", label)
        if "loose scan" in op or "loosescan" in op.replace(" ", ""):
            _mark("semijoin", label)
            _mark("loosescan", label)
        if access == "weedout" or "weedout" in op or "remove duplicate" in op:
            _mark("semijoin", label)
            _mark("duplicateweedout", label)

        # hash_set_operations — MySQL 8.4+ Hash Union / Intersect / Except.
        if "hash union" in op or "hash intersect" in op or "hash except" in op:
            _mark("hash_set_operations", label)

        # use_index_extensions — covering reads on secondary indexes.
        if details.get("covering") is True:
            _mark("use_index_extensions", label)

        # MariaDB rowid_filter
        if details.get("using_rowid_filter"):
            _mark("rowid_filter", label)

        for child in node.get("children") or []:
            _walk(child)

    _walk(root)

    # Materialize to an ordered list so tests and rendering are deterministic.
    ordered_names = [
        "hash_join", "block_nested_loop", "batched_key_access",
        "join_cache_bka", "join_cache_hashed", "outer_join_with_cache",
        "mrr", "index_condition_pushdown",
        "index_merge", "index_merge_union", "index_merge_sort_union",
        "index_merge_intersection",
        "skip_scan", "materialization",
        "semijoin", "firstmatch", "loosescan", "duplicateweedout",
        "hash_set_operations", "use_index_extensions",
        "rowid_filter",
    ]
    result = []
    for name in ordered_names:
        if name in found:
            result.append({
                "name": name,
                "value": "on",
                "explanation": OPTIMIZER_SWITCH_EXPLANATIONS.get(name, ""),
                "short_labels": found[name]["short_labels"],
            })
    return result


# ---------------------------------------------------------------------------
# Non-sargable join-predicate detection
# ---------------------------------------------------------------------------
#
# "Sargable" = **S**earch **ARG**ument-**able**. A predicate is sargable
# when an index can be used to evaluate it; wrapping a column in a function
# (CONCAT, CAST, LOWER, DATE, …) breaks sargability because the optimizer
# would need a *functional index* on the exact expression to use one.
#
# myflames flags these because they're one of the single most impactful
# issues a plan can have: a non-sargable join predicate forces a per-row
# expression evaluation (O(outer × inner) CPU time), no index can help, and
# the slowdown is often invisible on the flamegraph because MariaDB/MySQL
# attribute the cost to the server layer instead of to any storage-engine
# operator — producing a "wide base, no pyramid" shape that looks like a
# rendering bug but is actually the server layer doing the work.

#: Functions that break sargability when applied to a column reference.
#: This set is intentionally MySQL-centric but works for MariaDB (same
#: function names) and covers the cases the advisor actually sees in the
#: wild. Extending it is additive — safe to do over time.
_NONSARGABLE_FUNCS = (
    "CONCAT", "CONCAT_WS", "CAST", "CONVERT",
    "LOWER", "UPPER", "LCASE", "UCASE",
    "SUBSTRING", "SUBSTR", "LEFT", "RIGHT", "MID", "TRIM",
    "LTRIM", "RTRIM", "REPLACE", "REVERSE",
    "DATE", "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND",
    "DATE_FORMAT", "DATE_ADD", "DATE_SUB", "FROM_UNIXTIME", "UNIX_TIMESTAMP",
    "MD5", "SHA1", "SHA2", "UNHEX", "HEX", "CRC32",
    "COALESCE", "IFNULL", "NULLIF",
    "ABS", "ROUND", "FLOOR", "CEILING", "CEIL",
)

# Join-predicate regex: a function call that mentions a qualified column
# reference (``table.col`` / ``alias.col``) anywhere inside its top-level
# arg list. We require at least one dot because constant-only calls
# (``CONCAT('a','b')``, ``DATE('2024-01-01')``) are compile-time folded
# and don't actually break sargability.
#
# The column can appear at ANY position in the arg list — ``CONCAT('u', o.id)``
# is just as non-sargable as ``CONCAT(o.id, 'u')`` — so we use a non-greedy
# ``[^)]*?`` to walk past any literals/whitespace up to the first dotted
# identifier inside the parens.
_NONSARGABLE_RE = re.compile(
    r"\b(" + "|".join(_NONSARGABLE_FUNCS) + r")\s*\("
    r"[^)]*?"                                         # any other args
    r"[`]?[A-Za-z_][A-Za-z0-9_]*[`]?\s*\.\s*"         # REQUIRED table/alias.
    r"[`]?[A-Za-z_][A-Za-z0-9_]*[`]?",                # column
    re.IGNORECASE,
)


def _detect_nonsargable_joins(root):
    """Walk the tree and return join predicates that use non-sargable
    function calls on column references.

    Each entry is ``{"function", "predicate", "short_label"}``. The rule
    fires when:

      * A node is part of a join (operation or access_type mentions
        ``join``) OR it has a ``hash_condition`` / BNL condition, AND
      * Its condition / operation / hash_condition text contains a
        ``_NONSARGABLE_FUNCS`` call wrapping a column reference.

    We don't try to correlate across multiple nodes in the same plan —
    one match per node is enough to flag the issue, and the downstream
    advisor rule only ever reports the first hit anyway.
    """
    results = []
    seen_predicates = set()

    def _walk(node):
        details = node.get("details") or {}
        op = details.get("operation") or ""
        access = (details.get("access_type") or "").lower()
        cond = details.get("condition") or ""
        join_algo = (details.get("join_algorithm") or "").lower()
        buf = (details.get("using_join_buffer") or "").lower()
        is_join = (
            "join" in op.lower() or access == "join"
            or bool(join_algo) or ("block" in buf and "nested" in buf)
        )
        if is_join:
            # Build a single text blob from every field that could carry the
            # join predicate. MySQL hash joins put it on ``hash_condition``,
            # MySQL nested loops inline it in ``operation`` ("… (u.id = o.u_id)"),
            # MariaDB BNL wrappers put it in ``attached_condition`` which our
            # normalizer now copies into ``condition``.
            text_parts = [op, cond]
            hc = details.get("hash_condition") or []
            if isinstance(hc, list):
                text_parts.extend(hc)
            elif isinstance(hc, str):
                text_parts.append(hc)
            text = " ".join(p for p in text_parts if p)
            m = _NONSARGABLE_RE.search(text)
            if m:
                predicate = cond or op
                key = (m.group(1).upper(), predicate)
                if key not in seen_predicates:
                    seen_predicates.add(key)
                    results.append({
                        "function": m.group(1).upper(),
                        "predicate": predicate,
                        "short_label": node.get("short_label") or "",
                    })
        for c in node.get("children") or []:
            _walk(c)

    _walk(root)
    return results


def analyze_plan(root):
    """Scan parsed EXPLAIN tree and return analysis dict.

    Returns a dict with:
      full_scans   - list of {table, rows, loops} for table-access nodes
      hash_joins   - list of {rows} for hash join nodes
      temp_tables  - list of {rows} for Materialize nodes
      filesorts    - list of {rows} for Sort nodes
      optimizer_features  - list of inferred optimizer feature strings
      optimizer_switches  - structured [{"name","value","explanation",...}]
      warnings     - list of human-readable warning strings
      suggestions  - list of actionable suggestion strings
    """
    full_scans = []
    hash_joins = []
    temp_tables = []
    filesorts = []
    range_scans = []  # secondary-index range scans (gate for the MRR advisor rule)
    has_icp = False
    has_bka = False
    has_semijoin = False
    has_antijoin = False
    has_nested_loop = False
    has_bnl = False  # Block Nested Loop: join buffer in use (Extra: "Using join buffer (Block Nested Loop)", type ALL/index/range)
    bnl_nodes = []   # list of {short_label, rows} for BNL-affected nodes
    has_covering = False

    def _scan(node):
        nonlocal has_icp, has_bka, has_semijoin, has_antijoin, has_nested_loop, has_bnl, has_covering
        details = node.get("details") or {}
        op = (details.get("operation") or "").lower()
        access_type = (details.get("access_type") or "").lower()
        join_algo = (details.get("join_algorithm") or "").lower()
        using_join_buffer = (details.get("using_join_buffer") or "").lower()
        rows = float(node.get("rows") or 0)
        short_label = (node.get("short_label") or "").strip()

        if access_type == "table" and details.get("table_name"):
            full_scans.append({
                "table": details["table_name"],
                "rows": float(details.get("actual_rows") or 0),
                "loops": int(details.get("actual_loops") or 1),
                "short_label": short_label,
            })

        if join_algo == "hash" or "hash join" in op:
            hash_joins.append({"rows": rows, "short_label": short_label})

        if access_type == "materialize" or op.startswith("materialize"):
            temp_tables.append({"rows": rows, "short_label": short_label})

        if access_type == "sort":
            filesorts.append({"rows": rows, "short_label": short_label})

        # Secondary-index range scan: the optimizer only considers MRR for
        # range access over a non-primary key.
        # Sources for the range signal:
        #   - MySQL 8.4 FORMAT=JSON: `index_access_type == "index_range_scan"`
        #   - MariaDB: normalized to `access_type == "range"` by
        #     _normalize_mariadb_table when raw access_type is "RANGE".
        # We intentionally DON'T include primary-key range scans — rows
        # already come out in PK order so MRR has nothing to do.
        idx_access_type = (details.get("index_access_type") or "").lower()
        is_range_access = (
            access_type == "range"
            or idx_access_type == "index_range_scan"
        )
        if is_range_access and not details.get("using_primary_key"):
            range_scans.append({
                "table": details.get("table_name") or "",
                "key": details.get("key") or details.get("used_key") or "",
                "rows": rows,
                "short_label": short_label,
            })

        if details.get("pushed_index_condition"):
            has_icp = True

        if "batch_key_access" in op or "batched_key_access" in op:
            has_bka = True

        if "semi" in op and "join" in op:
            has_semijoin = True

        if "anti" in op and "join" in op:
            has_antijoin = True

        if join_algo == "nested_loop" or ("nested loop" in op and "join" in op):
            has_nested_loop = True

        # BNL: "Using join buffer (Block Nested Loop)" in Extra; type ALL, index, or range (per MySQL docs)
        if using_join_buffer and "block" in using_join_buffer and "nested" in using_join_buffer:
            if access_type in ("table", "index", "range"):
                has_bnl = True
                bnl_nodes.append({"short_label": short_label, "rows": rows})

        if details.get("covering") is True:
            has_covering = True

        for child in node.get("children") or []:
            _scan(child)

    _scan(root)

    # Detect @@optimizer_switch flags that are actually visible in this plan
    # and surface a one-line explanation per flag.
    optimizer_switches = _detect_optimizer_switches(root)

    # Detect non-sargable join predicates (CONCAT/CAST/LOWER/… applied to
    # a join column). These are a common cause of "why is my plan so slow"
    # questions because the cost lives in the server layer and shows up as
    # a wide-base-no-pyramid shape on the flamegraph.
    nonsargable_joins = _detect_nonsargable_joins(root)

    features = []
    # Carry over the structured detections as display strings. Each feature
    # line is "<flag>=on — <explanation>" so callers that match by substring
    # (e.g. 'hash_join' in feature) still pass.
    for sw in optimizer_switches:
        features.append("{}={} — {}".format(sw["name"], sw["value"], sw["explanation"]))
    # Preserve the legacy "nested loop join" / "antijoin=on" lines that had
    # no dedicated optimizer_switch entry — keeps existing display behaviour.
    if not any(s["name"] == "block_nested_loop" for s in optimizer_switches) and has_nested_loop:
        features.append("nested loop join")
    if has_antijoin:
        features.append("antijoin=on")

    warnings = []
    suggestions = []

    # Non-sargable join predicate — this is usually the single most
    # impactful finding because it makes every index useless. We put it
    # FIRST in the warnings/suggestions lists so the advisor's primary-
    # action picker surfaces it above every other finding.
    if nonsargable_joins:
        preds = []
        for nsj in nonsargable_joins[:3]:
            # Trim long predicates so the warning line stays readable.
            p = nsj["predicate"]
            if len(p) > 80:
                p = p[:77] + "..."
            preds.append("{}(...) in {}".format(nsj["function"], p))
        warnings.append(
            "Non-sargable join predicate: "
            + "; ".join(preds)
            + " — a function wrapped around the join column prevents index use."
        )
        suggestions.append(
            "Rewrite the join condition to compare the bare column on both "
            "sides (e.g. 'a.id = b.other_id' instead of 'CONCAT(a.id)=CONCAT(b.other_id)'). "
            "Why: wrapping a column in a function means the optimizer cannot use any index "
            "on that column — every row-pair is evaluated in the server layer, making the "
            "cost O(outer × inner). Dropping the function lets MySQL/MariaDB pick an index "
            "lookup or hash join with real selectivity, typically 100–1000× faster on "
            "mid-sized tables."
        )

    if full_scans:
        parts = [f"{s['table']} ({int(s['rows'])} rows)" for s in full_scans]
        warnings.append("Full table scan: " + ", ".join(parts))
        suggestions.append("Add indexes on filter/join columns to avoid full table scans")

    if hash_joins:
        max_rows = max(h["rows"] for h in hash_joins)
        warnings.append(
            f"{len(hash_joins)} hash join(s) — uses join_buffer_size"
            + (f" (~{int(max_rows)} rows in build phase)" if max_rows > 0 else "")
        )
        suggestions.append("Increase join_buffer_size if hash joins are slow or spill to disk")

    if temp_tables:
        max_rows = max(t["rows"] for t in temp_tables)
        warnings.append(
            f"{len(temp_tables)} temp table(s) (Materialize)"
            + (f" — up to {int(max_rows)} rows; may spill to disk" if max_rows > 0 else " — may spill to disk")
        )
        suggestions.append("Increase tmp_table_size / max_heap_table_size to keep temp tables in memory")

    if filesorts:
        max_rows = max(f["rows"] for f in filesorts)
        warnings.append(
            f"{len(filesorts)} sort operation(s)"
            + (f" — {int(max_rows)} rows; may use disk-based filesort" if max_rows > 0 else " — may use disk-based filesort")
        )
        suggestions.append("Increase sort_buffer_size or add an ordered index to avoid filesort")

    # BNL (Block Nested-Loop): per MySQL docs, signified by Extra="Using join buffer (Block Nested Loop)"
    # and type ALL/index/range. block_nested_loop optimizer_switch controls hash joins in MySQL 8.0.20+.
    if has_bnl:
        warnings.append(
            "Block Nested-Loop (BNL) join buffer detected — uses join_buffer_size "
            "(Extra: 'Using join buffer (Block Nested Loop)', type ALL/index/range)"
        )
        suggestions.append(
            "Add indexes to eliminate BNL full/index scans, or increase join_buffer_size. "
            "In MySQL 8.0.20+, set block_nested_loop=off to force hash join instead"
        )

    # Map each warning to the plan node(s) it refers to (for "where" hints and hover highlight)
    node_highlights = []  # list of {"short_label": str, "message": str}
    if full_scans:
        msg = "Full table scan: " + ", ".join(f"{s['table']} ({int(s['rows'])} rows)" for s in full_scans)
        for s in full_scans:
            lbl = s.get("short_label") or ("Table scan [" + s["table"] + "]")
            node_highlights.append({"short_label": lbl, "message": msg})
    if hash_joins:
        max_rows = max(h["rows"] for h in hash_joins)
        msg = (
            f"{len(hash_joins)} hash join(s) — uses join_buffer_size"
            + (f" (~{int(max_rows)} rows in build phase)" if max_rows > 0 else "")
        )
        for h in hash_joins:
            lbl = h.get("short_label") or "Nested loop inner join"
            node_highlights.append({"short_label": lbl, "message": msg})
    if temp_tables:
        max_rows = max(t["rows"] for t in temp_tables)
        msg = (
            f"{len(temp_tables)} temp table(s) (Materialize)"
            + (f" — up to {int(max_rows)} rows; may spill to disk" if max_rows > 0 else " — may spill to disk")
        )
        for t in temp_tables:
            lbl = t.get("short_label") or "Materialize"
            node_highlights.append({"short_label": lbl, "message": msg})
    if filesorts:
        max_rows = max(f["rows"] for f in filesorts)
        msg = (
            f"{len(filesorts)} sort operation(s)"
            + (f" — {int(max_rows)} rows; may use disk-based filesort" if max_rows > 0 else " — may use disk-based filesort")
        )
        for f in filesorts:
            lbl = f.get("short_label") or "Sort"
            node_highlights.append({"short_label": lbl, "message": msg})

    if bnl_nodes:
        bnl_msg = warnings[-1] if has_bnl and warnings else "Block Nested-Loop join buffer"
        # Find the actual BNL warning string
        for w in warnings:
            if "Block Nested-Loop" in w:
                bnl_msg = w
                break
        for b in bnl_nodes:
            lbl = b.get("short_label") or "Table scan"
            node_highlights.append({"short_label": lbl, "message": bnl_msg})

    # Map the non-sargable warning to the plan nodes that triggered it
    # so the HTML "Labeled" hint points at the right join(s).
    if nonsargable_joins:
        nsj_msg = next((w for w in warnings if "Non-sargable" in w), None)
        if nsj_msg:
            for nsj in nonsargable_joins:
                lbl = nsj.get("short_label") or "Join"
                node_highlights.append({"short_label": lbl, "message": nsj_msg})

    index_suggestions = _suggest_indexes(root)

    return {
        "full_scans": full_scans,
        "hash_joins": hash_joins,
        "temp_tables": temp_tables,
        "filesorts": filesorts,
        "range_scans": range_scans,
        "bnl_nodes": bnl_nodes,
        "nonsargable_joins": nonsargable_joins,
        "optimizer_features": features,
        "optimizer_switches": optimizer_switches,
        "warnings": warnings,
        "suggestions": suggestions,
        "node_highlights": node_highlights,
        "index_suggestions": index_suggestions,
        "query_text_lines": [],   # populated by CLI via load_explain_json + format_sql
    }


def _how_to_read_lines(view_type):
    """Return (title, lines) for the 'How to read' section of the consolidated info panel."""
    if view_type == "bargraph":
        return "How to read", [
            "Bar width = self-time (time in this operation only). Darker bars = slower.",
            "Where a warning applies: hover the bar — it will show \u201cIn Query Analysis\u201d and the info panel lists the bar label.",
        ]
    if view_type == "treemap":
        return "How to read", [
            "Cell area = total time (including children). Click a cell to zoom.",
            "Where a warning applies: hover the cell — the details bar shows \u201cIn Query Analysis\u201d and the label that appears in the panel below.",
        ]
    if view_type == "diagram":
        return "How to read", [
            "Left\u2192right = execution order. Heat scale (yellow = fast \u2192 purple = slow) encodes self-time.",
            "The red SLOWEST badge + red border mark the contention point — the single operator to optimize first.",
            "Click any node to pin its details. Ctrl+F to search. Dbl-click the background to reset zoom.",
        ]
    if view_type == "flamegraph":
        return "How to read", [
            "Width = time for that operation (wider = slower). Bottom = query root; top = leaf table access.",
            "Click a frame to zoom in on that operation. Ctrl+F to search. Hover for time, rows and loops.",
            "Warnings below refer to specific operations by label (e.g. TABLE SCAN [users]).",
        ]
    if view_type == "tree":
        return "How to read", [
            "Each row = one plan operation. Indentation shows parent\u2192child (outer\u2192inner). Self = time in this op only; Total = including children.",
            "Click \u25be/\u25b8 to collapse/expand a subtree. Click a row to pin its details. Ctrl+F to search.",
            "Orange rows \u26a0 have a warning in the Query Analysis panel below.",
        ]
    return "How to read", ["Hover or select elements to see details. Warnings below refer to specific bars, cells, or nodes by label."]


def render_info_panel(analysis, x, y, width, view_type="bargraph"):
    """Return (svg_lines, panel_height) for a single consolidated info block.

    Combines: How to read (view-specific), Optimizer features, Warnings (with
    node labels), and Suggestions. Each section has a distinct background band.
    """
    pad_x = 16
    pad_y = 10
    line_h = 17
    section_hdr_h = 22
    title_h = 26
    section_gap = 6

    features = analysis.get("optimizer_features") or []
    warnings = analysis.get("warnings") or []
    suggestions = analysis.get("suggestions") or []
    node_highlights = analysis.get("node_highlights") or []

    query_text_lines = analysis.get("query_text_lines") or []
    index_suggestions = analysis.get("index_suggestions") or []

    sections = []

    # SQL query section — shown first so the artifact is self-contained
    if query_text_lines:
        sections.append(("Query", list(query_text_lines), "query"))

    how_title, how_lines = _how_to_read_lines(view_type)
    sections.append((how_title, how_lines, "how"))
    sections.append(("Optimizer features in this plan", features if features else ["None detected"], "feature"))

    if warnings:
        warning_with_where = []
        for w in warnings:
            warning_with_where.append(w)
            labels = [nh.get("short_label") or "" for nh in node_highlights if nh.get("message") == w]
            unique = list(dict.fromkeys(l for l in labels if l))
            if unique:
                where = "\u2192 Labeled: \u201c" + unique[0] + "\u201d"
                if len(unique) > 1:
                    where = "\u2192 Labeled: \u201c" + "\u201d, \u201c".join(unique[:3]) + ("\u201d\u2026" if len(unique) > 3 else "\u201d")
                warning_with_where.append(where)
        sections.append(("\u26a0  Warnings", warning_with_where, "warning"))

    if suggestions:
        sections.append(("Suggestions", ["\u2022  " + s for s in suggestions], "suggest"))

    if index_suggestions:
        idx_lines = []
        for hint in index_suggestions:
            idx_lines.append("\u2022  " + hint["reason"])
            idx_lines.append("    " + hint["ddl"])
        sections.append(("\U0001f4c8  Index suggestions (heuristic — verify before applying)", idx_lines, "index"))

    # Live-connection advisor output (only present when myflames was run
    # with connection flags). Each section is independent so sessions
    # without collected data don't get empty cards.
    env_warnings = analysis.get("environment_warnings") or []
    env_suggestions = analysis.get("environment_suggestions") or []
    collected_vars = analysis.get("collected_variables") or {}
    collected_schema = analysis.get("collected_schema") or {}
    collected_stats = analysis.get("collected_stats") or {}

    if env_warnings:
        sections.append((
            "\U0001f50d  Environment warnings (from collected server state)",
            ["\u26a0  " + w for w in env_warnings],
            "warning",
        ))
    if env_suggestions:
        sections.append((
            "\u2699  Environment tuning suggestions",
            ["\u2022  " + s for s in env_suggestions],
            "suggest",
        ))
    if collected_vars:
        lines_out = []
        # Only show variables that are non-empty and commonly-tuned — keep
        # the panel readable.  ``optimizer_switch`` is noisy but
        # load-bearing, so we include it.
        SHOW = (
            "version", "innodb_buffer_pool_size", "innodb_log_file_size",
            "innodb_flush_log_at_trx_commit",
            "sort_buffer_size", "join_buffer_size",
            "tmp_table_size", "max_heap_table_size",
            "optimizer_switch",
        )
        for name in SHOW:
            if name in collected_vars and collected_vars[name]:
                lines_out.append(name + " = " + str(collected_vars[name]))
        if lines_out:
            sections.append((
                "Collected session variables",
                lines_out, "feature",
            ))
    if collected_stats:
        lines_out = []
        for t, s in collected_stats.items():
            lines_out.append("{}: rows={}, data={}, idx={}".format(
                t,
                s.get("table_rows", 0),
                s.get("data_length", 0),
                s.get("index_length", 0),
            ))
        sections.append((
            "Collected table stats (information_schema.tables)",
            lines_out, "feature",
        ))
    if collected_schema:
        lines_out = []
        for t, p in collected_schema.items():
            cols = len(p.get("columns") or [])
            idxs = ", ".join((i.get("name") or "PRIMARY") for i in (p.get("indexes") or []))
            lines_out.append("{} — {} cols, indexes: {}".format(t, cols, idxs or "none"))
        sections.append((
            "Collected schema (SHOW CREATE TABLE)",
            lines_out, "feature",
        ))

    # Word-wrap text to fit panel width (approx 6.2px per char for 11px Arial)
    max_chars = max(60, int((width - pad_x * 2 - 30) / 6.2))

    def _wrap(t):
        """Word-wrap a single string into a list of lines fitting max_chars."""
        if len(t) <= max_chars:
            return [t]
        words = t.split(" ")
        result, current = [], ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= max_chars:
                current += " " + word
            else:
                result.append(current)
                current = word
        if current:
            result.append(current)
        return result or [t]

    # Pre-compute wrapped content so height calculation is accurate
    wrapped_sections = []
    for sec_title, content, kind in sections:
        wrapped_content = []
        for line_text in content:
            wrapped_content.extend(_wrap(line_text))
        wrapped_sections.append((sec_title, wrapped_content, kind))

    total_content_lines = sum(len(c) for _, c, _ in wrapped_sections)
    panel_height = (pad_y * 2 + title_h + 8
                    + len(wrapped_sections) * (section_hdr_h + 4)
                    + total_content_lines * line_h
                    + len(wrapped_sections) * section_gap + 8)
    panel_height = max(panel_height, 80)

    lines = []
    # Outer card
    lines.append(
        f'<rect x="{x}" y="{y}" width="{width}" height="{panel_height}" '
        f'fill="#f8f9fc" stroke="#c5cae9" stroke-width="1.5" rx="8"/>'
    )

    cy = y + pad_y
    # Title bar
    lines.append(
        f'<rect x="{x}" y="{cy}" width="{width}" height="{title_h + 4}" fill="#e8eaf6" rx="6"/>'
    )
    lines.append(
        f'<rect x="{x}" y="{cy + 6}" width="{width}" height="{title_h - 2}" fill="#e8eaf6"/>'
    )
    lines.append(
        f'<text x="{x + pad_x}" y="{cy + 19}" font-family="Arial,sans-serif" font-size="13" '
        f'font-weight="bold" fill="#283593">How to read \u00b7 Query Analysis</text>'
    )
    cy += title_h + 12

    # Section style map: (header_bg, header_text_color, content_text_color, content_font)
    _STYLES = {
        "how":     ("#f1f8e9", "#33691e", "#455a64",  "Arial,sans-serif"),
        "feature": ("#e3f2fd", "#0d47a1", "#1565c0",  "Arial,sans-serif"),
        "warning": ("#fff3f3", "#b71c1c", "#c62828",  "Arial,sans-serif"),
        "suggest": ("#e8f5e9", "#1b5e20", "#2e7d32",  "Arial,sans-serif"),
        "query":   ("#f5f5f5", "#37474f", "#263238",  "Courier New,Courier,monospace"),
        "index":   ("#fff8e1", "#e65100", "#bf360c",  "Courier New,Courier,monospace"),
    }

    for sec_title, content, kind in wrapped_sections:
        style = _STYLES.get(kind, ("#f5f5f5", "#333", "#555", "Arial,sans-serif"))
        hdr_bg, hdr_color, content_color, content_font = style
        # Section header band
        lines.append(
            f'<rect x="{x + 4}" y="{cy}" width="{width - 8}" height="{section_hdr_h}" '
            f'fill="{hdr_bg}" rx="4"/>'
        )
        lines.append(
            f'<text x="{x + pad_x}" y="{cy + 15}" font-family="Arial,sans-serif" '
            f'font-size="11" font-weight="bold" fill="{hdr_color}">{xml_escape(sec_title)}</text>'
        )
        cy += section_hdr_h + 4
        for line_text in content:
            lines.append(
                f'<text x="{x + pad_x + 10}" y="{cy + line_h - 4}" font-family="{content_font}" '
                f'font-size="11" fill="{content_color}">{xml_escape(line_text)}</text>'
            )
            cy += line_h
        cy += section_gap

    return lines, panel_height
