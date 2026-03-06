"""
Unified parser for MySQL EXPLAIN ANALYZE FORMAT=JSON.
Builds a single tree structure used by flamegraph, bargraph, and treemap.
"""
import re
import json


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
    self_time = max(0, total_time - children_time)
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
    }
    return {
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


def load_explain_json(text):
    """Load and strip EXPLAIN: prefix if present."""
    text = text.strip()
    text = re.sub(r"^.*?EXPLAIN:\s*", "", text, flags=re.DOTALL)
    return json.loads(text)


def parse_explain(text):
    """Parse EXPLAIN JSON text into root tree node."""
    data = load_explain_json(text)
    root = parse_node(data)
    if not root:
        raise ValueError("Failed to parse EXPLAIN JSON")
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
    lines = [original, ""]
    if best.get("table_name"):
        t = (best.get("schema_name") or "") + "." + best["table_name"] if best.get("schema_name") else best["table_name"]
        idx = f" (index: {xml_escape(best['index_name'])})" if best.get("index_name") else ""
        lines.append(f"Table: {xml_escape(t)}{idx}")
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
    cond = xml_escape(best.get("condition") or "")
    if len(cond) > 83:
        cond = cond[:80] + "..."
    if best.get("condition"):
        lines.append(f"Condition: {cond}")
    if best.get("ranges"):
        lines.append("Ranges: " + xml_escape(", ".join(best["ranges"])))
    if best.get("covering") is not None:
        lines.append("Covering: " + ("Yes" if best["covering"] else "No"))
    return "\n".join(lines)


def analyze_plan(root):
    """Scan parsed EXPLAIN tree and return analysis dict.

    Returns a dict with:
      full_scans   - list of {table, rows, loops} for table-access nodes
      hash_joins   - list of {rows} for hash join nodes
      temp_tables  - list of {rows} for Materialize nodes
      filesorts    - list of {rows} for Sort nodes
      optimizer_features - list of inferred optimizer feature strings
      warnings     - list of human-readable warning strings
      suggestions  - list of actionable suggestion strings
    """
    full_scans = []
    hash_joins = []
    temp_tables = []
    filesorts = []
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

    features = []
    if hash_joins:
        features.append("hash_join=on")
    if has_bnl:
        features.append("block_nested_loop=on (join buffer in use)")
    elif has_nested_loop:
        features.append("nested loop join")
    if has_icp:
        features.append("index_condition_pushdown=on")
    if has_bka:
        features.append("batched_key_access=on")
    if has_semijoin:
        features.append("semijoin=on")
    if has_antijoin:
        features.append("antijoin=on")
    if has_covering:
        features.append("use_index_extensions=on (covering index)")

    warnings = []
    suggestions = []

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

    return {
        "full_scans": full_scans,
        "hash_joins": hash_joins,
        "temp_tables": temp_tables,
        "filesorts": filesorts,
        "bnl_nodes": bnl_nodes,
        "optimizer_features": features,
        "warnings": warnings,
        "suggestions": suggestions,
        "node_highlights": node_highlights,
    }


def render_analysis_panel(analysis, x, y, width):
    """Return (svg_lines, panel_height) for the query analysis info panel."""
    pad_x = 14
    pad_y = 8
    line_h = 15
    title_h = 22

    features = analysis.get("optimizer_features") or []
    warnings = analysis.get("warnings") or []
    suggestions = analysis.get("suggestions") or []

    # Build a flat list of (kind, text) to render
    rows = []
    rows.append(("section", "Optimizer features active in this plan:"))
    if features:
        for f in features:
            rows.append(("feature", f))
    else:
        rows.append(("feature", "None detected"))

    if warnings:
        rows.append(("section", "\u26a0  Warnings:"))
        for w in warnings:
            rows.append(("warning", "\u26a0  " + w))

    if suggestions:
        rows.append(("section", "\U0001f4a1  Suggestions:"))
        for s in suggestions:
            rows.append(("suggest", "\u2022  " + s))

    panel_height = pad_y * 2 + title_h + len(rows) * line_h + 6
    panel_height = max(panel_height, 60)

    lines = []
    lines.append(
        f'<rect x="{x}" y="{y}" width="{width}" height="{panel_height}" '
        f'fill="#f0f4ff" stroke="#c5cae9" stroke-width="1" rx="4"/>'
    )
    lines.append(
        f'<text x="{x + pad_x}" y="{y + pad_y + 14}" '
        f'font-family="Arial,sans-serif" font-size="12" font-weight="bold" fill="#303f9f">'
        f'Query Analysis</text>'
    )

    cy = y + pad_y + title_h + 4
    for kind, text in rows:
        if kind == "section":
            fill = "#444"
            weight = "bold"
            indent = pad_x
        elif kind == "warning":
            fill = "#b71c1c"
            weight = "normal"
            indent = pad_x + 16
        elif kind == "suggest":
            fill = "#1b5e20"
            weight = "normal"
            indent = pad_x + 16
        else:  # feature
            fill = "#1a237e"
            weight = "normal"
            indent = pad_x + 16
        lines.append(
            f'<text x="{x + indent}" y="{cy}" '
            f'font-family="Arial,sans-serif" font-size="10" font-weight="{weight}" fill="{fill}">'
            f'{xml_escape(text)}</text>'
        )
        cy += line_h

    return lines, panel_height


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
            "Left\u2192right = execution order. Darker = more self-time. Click a node to pin details.",
            "Where a warning applies: hover or click the node — details show \u201cIn Query Analysis\u201d; the panel below lists the node label.",
        ]
    if view_type == "flamegraph":
        return "How to read", [
            "Width = time for that operation (wider = slower). Bottom = query root; top = leaf table access.",
            "Click a frame to zoom in on that operation. Ctrl+F to search. Hover for time, rows and loops.",
            "Warnings below refer to specific operations by label (e.g. TABLE SCAN [users]).",
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

    sections = []
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

    # Section style map: (header_bg, header_text_color, content_text_color)
    _STYLES = {
        "how":     ("#f1f8e9", "#33691e", "#455a64"),
        "feature": ("#e3f2fd", "#0d47a1", "#1565c0"),
        "warning": ("#fff3f3", "#b71c1c", "#c62828"),
        "suggest": ("#e8f5e9", "#1b5e20", "#2e7d32"),
    }

    for sec_title, content, kind in wrapped_sections:
        hdr_bg, hdr_color, content_color = _STYLES.get(kind, ("#f5f5f5", "#333", "#555"))
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
                f'<text x="{x + pad_x + 10}" y="{cy + line_h - 4}" font-family="Arial,sans-serif" '
                f'font-size="11" fill="{content_color}">{xml_escape(line_text)}</text>'
            )
            cy += line_h
        cy += section_gap

    return lines, panel_height
