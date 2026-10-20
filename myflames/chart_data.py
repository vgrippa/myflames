"""Versioned semantic payloads consumed by ``myflames-charts.js``.

Python keeps every MySQL/MariaDB decision (operator kind, reported join type,
input phase, operator family, metrics, teach index, details text) and emits it
as data. The TypeScript bundle only lays out, formats, and wires interaction.

The helpers below are imported from the renderers that own them, so the
semantic tests for those renderers keep covering what the bundle draws.
Schemas: ``docs/schemas/chart-data-1.0.json`` (HTML reports and explore
pages) and ``chart-data-2.0.json`` (the workspace's ``/api/analyze``, which
stores each analysis message once). The bundle accepts both. This payload is
deliberately separate from the slim sidecar ``plan_tree``.
"""
import math

from .exploration import METRICS, metric_value
from .output_workbench import (
    JOIN_LABELS, _arrow_width, _details, _join_type, _kind, _number, _phase,
)
from .parser import enhance_tooltip_flame, flatten_nodes, operator_family
from .plan_matching import timing_available

SCHEMA_VERSION = "chart-data-1.0"
COMPACT_SCHEMA_VERSION = "chart-data-2.0"
JOIN_KINDS = ("nested_loop", "hash", "bka")

#: Copy for the metric list. Assembled by the bundle for the visible
#: projection, so client-side focus/collapse can reuse it.
METRIC_NOTES = {
    "base": ("Native chart uses its original time or operation encoding. Metric bars "
             "compare the visible operators; total times overlap and must not be summed."),
    "collapsed": ("Collapsed descendants are excluded from the chart; displayed "
                  "measurements are unchanged."),
    "select_list": ("SELECT-list subqueries are separate execution branches; their "
                    "timings are not subtracted from the enclosing iterator."),
}


def _clean(value):
    """Return JSON-safe data: non-finite floats become null (never NaN)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _text(value):
    return "" if value is None else str(value)


def _highlights(analysis):
    messages = {}
    for item in (analysis or {}).get("node_highlights") or []:
        label = (item.get("short_label") or "").strip()
        if label and label not in messages:
            messages[label] = item.get("message") or ""
    return messages


def _flame_details(node):
    """Flame-graph tooltip detail lines for exactly this operator.

    Reuses :func:`parser.enhance_tooltip_flame` with a one-entry map, so the
    text is identical to the Python flame graph's but never fuzzy-matched
    to a different operator with a similar label.
    """
    label = node.get("folded_label") or ""
    enhanced = enhance_tooltip_flame(label, {label: node.get("details") or {}})
    prefix = label + "\n\n"
    return enhanced[len(prefix):] if enhanced.startswith(prefix) else ""


def _projection(nodes, projection):
    projection = dict(projection or {})
    focus = projection.get("focus_id") or ""
    collapsed = projection.get("collapsed_ids") or []
    selected = projection.get("selected_id") or ""
    metric = projection.get("metric") or "self_time"
    if not isinstance(focus, str) or (focus and focus not in nodes):
        raise ValueError("Unknown focus operator.")
    if not isinstance(collapsed, (list, tuple, set)) or any(
            not isinstance(x, str) or x not in nodes for x in collapsed):
        raise ValueError("Unknown collapsed operator.")
    if metric not in METRICS:
        raise ValueError("Unknown exploration metric.")
    return {"focus_id": focus, "collapsed_ids": list(collapsed),
            "selected_id": _text(selected), "metric": metric}


def build_chart_data(root, title="Query plan", engine=None,
                     teach_index_by_folded=None, analysis=None, projection=None,
                     compact=False):
    """Return versioned chart data for the canonical parsed tree.

    ``root`` must come from :func:`myflames.parser.parse_explain`; it is read,
    never mutated. Nodes are flat pre-order; ``children`` keeps input order.
    Measured milliseconds stay canonical; ``display_unit`` is only a hint.
    ``projection`` (focus/collapse/selection/metric) is applied by the bundle
    as a copy, so measurements are identical whatever is visible.
    ``compact=True`` returns ``chart-data-2.0`` (the workspace): each node's
    ``highlight_id`` indexes ``highlight_messages``, which holds every
    message once, in the order nodes first use it. A grouped warning names
    every affected table and applies to each of them, so repeating it per
    node (1.0's ``highlight``) grows with the square of the scan count. The
    default stays ``chart-data-1.0`` (HTML reports, explore pages). The bundle
    reads both.
    """
    teach = teach_index_by_folded or {}
    highlight = _highlights(analysis)
    highlight_messages = []
    highlight_ids = {}

    def highlight_field(node):
        message = highlight.get((node.get("short_label") or "").strip())
        if not compact:
            return "highlight", message
        if message is None:
            return "highlight_id", None
        if message not in highlight_ids:
            highlight_ids[message] = len(highlight_messages)
            highlight_messages.append(message)
        return "highlight_id", highlight_ids[message]

    by_id = {node["node_id"]: node for node in flatten_nodes(root)}
    rows = []

    def visit(node, parent, index, depth):
        d = node.get("details") or {}
        kind = _kind(node)
        measured = _number(d.get("actual_last_row_ms")) is not None
        children = node.get("children") or []
        highlight_key, highlight_value = highlight_field(node)
        rows.append({
            "id": node["node_id"],
            "parent_id": parent["node_id"] if parent is not None else None,
            "children": [child["node_id"] for child in children],
            "depth": depth,
            "input_kind": None if parent is None else (d.get("input_kind") or "input"),
            "short_label": _text(node.get("short_label")),
            "full_label": _text(node.get("full_label")),
            "folded_label": _text(node.get("folded_label")),
            "operation": _text(d.get("operation")),
            "family": operator_family(d),
            "wb_kind": kind,
            "join_type": _join_type(node) if kind in JOIN_KINDS else None,
            "phase": _phase(parent, node, index) if parent is not None else "",
            "arrow_width": _arrow_width(node),
            "measured": measured,
            "self_ms": _number(node.get("self_time")) if measured else None,
            "total_ms": _number(node.get("total_time")) if measured else None,
            "actual_rows": _number(d.get("actual_rows")),
            "estimated_rows": _number(d.get("estimated_rows")),
            "loops": _number(d.get("actual_loops")),
            "first_row_ms": _number(d.get("actual_first_row_ms")),
            "last_row_ms": _number(d.get("actual_last_row_ms")),
            "estimated_cost": _number(d.get("estimated_total_cost")),
            "table": _text(d.get("table_name")),
            "schema": _text(d.get("schema_name")),
            "index": _text(d.get("index_name")),
            "access_type": _text(d.get("access_type")),
            "index_access_type": _text(d.get("index_access_type")),
            "condition": _text(d.get("condition")),
            "ranges": [_text(r) for r in (d.get("ranges") or [])],
            "covering": d.get("covering") if isinstance(d.get("covering"), bool) else None,
            "join_algorithm": _text(d.get("join_algorithm")),
            "complexity": d.get("complexity") if isinstance(d.get("complexity"), dict) else None,
            "teach_index": teach.get((node.get("folded_label") or "").strip()),
            highlight_key: highlight_value,
            "metrics": {name: list(metric_value(node, name)) for name in METRICS},
            "details_text": _details(node),
            "flame_details": _flame_details(node),
        })
        for i, child in enumerate(children):
            visit(child, node, i, depth + 1)

    visit(root, None, 0, 0)
    total = _number(root.get("total_time"))
    payload = {
        "schema_version": COMPACT_SCHEMA_VERSION if compact else SCHEMA_VERSION,
        "title": _text(title),
        "engine": engine or root.get("engine") or "mysql",
        "display_unit": "µs" if total is not None and 0 < total < 1 else "ms",
        "timing_available": timing_available(root),
        "root_id": root["node_id"],
        "nodes": rows,
        "projection": _projection(by_id, projection),
        "metrics_meta": {name: {"label": label, "unit": unit}
                         for name, (label, unit) in METRICS.items()},
        "labels": {
            "join_types": {key: {"label": label, "meaning": meaning}
                           for key, (label, meaning) in JOIN_LABELS.items()},
            "metric_notes": dict(METRIC_NOTES),
        },
    }
    if compact:
        payload["highlight_messages"] = highlight_messages
    return _clean(payload)
