"""
Structured diff sidecar for ``output_compare.render_compare`` (Slice 6 / S4).

A separate module because ``output_compare.py`` currently only emits
HTML — we don't want to couple HTML rendering to the JSON emission
(structured-output Round 2: "one serializer, two consumers"). Callers
that want both call each function; CI systems that only want the JSON
can import this module and skip the HTML.

Schema ``compare-1.0`` (tracked separately from the main sidecar
``1.3`` because a diff's shape is substantially different from a
single-plan sidecar). The document references the shared ``node_id``
primitive from Slice 2 so it cross-references plan_tree entries.

::

    {
      "schema_version": "compare-1.0",
      "$schema":        "https://myflames.dev/schemas/compare-v1.json",
      "generated_at":   "...Z",
      "myflames_version": "...",
      "before": {"total_time_ms": 123.4, "operator_count": 12},
      "after":  {"total_time_ms":  45.2, "operator_count": 12},
      "summary": {
        "time_delta_ms": -78.2,
        "time_delta_pct": -63.4,
        "regressions": 0,
        "improvements": 3,
        "unchanged":   9
      },
      "deltas": [
        {
          "short_label":   "Table scan [users]",
          "before_node_id":"n:...",
          "after_node_id": "n:...",
          "self_time_ms":  {"before": 10.0, "after": 1.2, "change_pct": -88.0},
          "rows":          {"before": 100,  "after": 100, "change_pct":  0.0},
          "classification":"improved"
        },
        ...
      ]
    }

External tools can gate CI on ``summary.regressions == 0`` or on any
specific delta's ``change_pct`` without parsing prose.
"""
import datetime
import json
import os

from . import __version__
from .parser import parse_explain, analyze_plan, flatten_nodes


COMPARE_SCHEMA_VERSION = "compare-1.0"
COMPARE_SCHEMA_URL = "https://myflames.dev/schemas/compare-v1.json"

#: Relative change threshold (fraction) that flags a delta as a
#: regression / improvement vs "unchanged". 5% by default — smaller
#: swings are noise on EXPLAIN ANALYZE runs.
_CHANGE_THRESHOLD = 0.05


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _pct(before, after):
    """Percentage change from *before* → *after*.

    Returns ``None`` when *before* is zero (percent-of-zero is not a
    meaningful number); callers render this as "new" or "—".
    """
    if before is None or after is None:
        return None
    if abs(before) < 1e-9:
        return None
    return round(((after - before) / before) * 100.0, 2)


def _classify_delta(change_pct):
    if change_pct is None:
        return "new_or_removed"
    if change_pct <= -_CHANGE_THRESHOLD * 100:
        return "improved"
    if change_pct >= _CHANGE_THRESHOLD * 100:
        return "regressed"
    return "unchanged"


def _node_index(root):
    """Return ``{short_label: node}`` for a parsed root (first-wins to
    match the key choice used by output_compare.render_compare)."""
    out = {}
    for n in flatten_nodes(root):
        lbl = n.get("short_label", "")
        if lbl not in out:
            out[lbl] = n
    return out


def build_compare_sidecar(json_before, json_after):
    """Produce the ``compare-1.0`` sidecar dict.

    Parameters
    ----------
    json_before, json_after : str
        Raw EXPLAIN ANALYZE FORMAT=JSON text for the two plans.

    Returns
    -------
    dict
        Validated sidecar payload ready for ``json.dumps``.
    """
    root_b = parse_explain(json_before)
    root_a = parse_explain(json_after)

    total_b = float(root_b.get("total_time") or 0)
    total_a = float(root_a.get("total_time") or 0)

    analysis_b = analyze_plan(root_b)
    analysis_a = analyze_plan(root_a)

    idx_b = _node_index(root_b)
    idx_a = _node_index(root_a)

    all_labels = list(idx_b.keys())
    for lbl in idx_a:
        if lbl not in idx_b:
            all_labels.append(lbl)

    deltas = []
    regressions = 0
    improvements = 0
    unchanged = 0
    for lbl in all_labels:
        nb = idx_b.get(lbl)
        na = idx_a.get(lbl)
        self_b = float((nb or {}).get("self_time") or 0) if nb else None
        self_a = float((na or {}).get("self_time") or 0) if na else None
        rows_b = float((nb or {}).get("rows") or 0) if nb else None
        rows_a = float((na or {}).get("rows") or 0) if na else None
        change_pct = _pct(self_b, self_a)
        classification = _classify_delta(change_pct)
        if classification == "regressed":
            regressions += 1
        elif classification == "improved":
            improvements += 1
        elif classification == "unchanged":
            unchanged += 1
        deltas.append({
            "short_label":     lbl,
            "before_node_id":  (nb or {}).get("node_id") or "",
            "after_node_id":   (na or {}).get("node_id") or "",
            "self_time_ms": {
                "before":     self_b,
                "after":      self_a,
                "change_pct": change_pct,
            },
            "rows": {
                "before":     rows_b,
                "after":      rows_a,
                "change_pct": _pct(rows_b, rows_a),
            },
            "classification": classification,
        })

    return {
        "$schema":         COMPARE_SCHEMA_URL,
        "schema_version":  COMPARE_SCHEMA_VERSION,
        "generated_at":    _utc_now_iso(),
        "myflames_version": __version__,
        "before": {
            "total_time_ms":   round(total_b, 3),
            "operator_count":  len(list(flatten_nodes(root_b))),
            "root_node_id":    root_b.get("node_id", ""),
        },
        "after": {
            "total_time_ms":   round(total_a, 3),
            "operator_count":  len(list(flatten_nodes(root_a))),
            "root_node_id":    root_a.get("node_id", ""),
        },
        "summary": {
            "time_delta_ms":   round(total_a - total_b, 3),
            "time_delta_pct":  _pct(total_b, total_a),
            "regressions":     regressions,
            "improvements":    improvements,
            "unchanged":       unchanged,
        },
        "deltas": deltas,
    }


def write_compare_sidecar(path, json_before, json_after):
    """Convenience: build + write with stable formatting."""
    payload = build_compare_sidecar(json_before, json_after)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return payload
