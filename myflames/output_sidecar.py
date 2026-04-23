"""
Structured JSON sidecar emitter for myflames outputs.

Every SVG / HTML that myflames writes should get a ``<base>.json`` sibling
carrying all the analysis data in a stable, machine-readable form — so AI
agents and external tools can consume myflames outputs without OCR'ing SVG
``<text>`` nodes or parsing HTML.

The schema is versioned and validated on write; see
``.claude/skills/structured-output/SKILL.md`` for the v1 schema spec and
the rationale for every invariant enforced below.

Top-level invariants
--------------------
1. Every sidecar carries ``schema_version`` — callers version-gate against it.
2. Enum fields (severity, category, source, engine, source.type) are
   restricted to the short stable strings listed below. Fail fast on violations.
3. Optional fields are OMITTED when absent. Never ``null`` — ``null`` is
   reserved for "explicitly nothing there", which we never need.
4. Freeform strings (text, why, action, explanation) are the ONLY place
   arbitrary prose lives. Everything else is greppable.
5. Human and machine never drift — the HTML template MUST read from the
   same dict this module produces, never from a parallel source.
"""
import datetime
import json
import os
import re

from . import __version__
from .teach_hooks import build_teach_hooks, SUPPORTED_LESSONS


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.2"

# Allowed enum values. ``build_sidecar`` + ``validate_sidecar`` reject anything
# outside these sets to keep downstream agents stable.
_ENGINES = frozenset({"mysql", "mariadb", "unknown"})
_SOURCE_TYPES = frozenset({"file", "live", "stdin"})
_WARNING_SEVERITIES = frozenset({"error", "warn", "info"})
_WARNING_CATEGORIES = frozenset({
    "full_scan", "filesort", "temp_table",
    "hash_join", "bnl", "semijoin", "index_merge",
    "nonsargable_join",
    "env", "engine", "durability", "other",
})
_SUGGESTION_SEVERITIES = frozenset({"high", "medium", "low"})
_SUGGESTION_CATEGORIES = frozenset({
    "tuning_variable", "index", "optimizer_switch",
    "engine", "durability", "rewrite", "other",
})
_WARNING_SOURCES = frozenset({"plan", "environment"})
_COMPLEXITY_SEVERITIES = frozenset({"good", "medium", "bad"})
_COMPLEXITY_CONFIDENCES = frozenset({"exact", "typical", "worst_case"})

# Matches the advisor's convention: an action sentence followed by a ``Why:``
# clause. Used to split suggestion strings into structured {action, why}.
_WHY_RE = re.compile(r"\s+Why:\s*", re.IGNORECASE)


class SidecarValidationError(ValueError):
    """Raised when a sidecar payload violates the v1 schema.

    We treat this as a programmer error, not a user error: the sidecar
    emitter should NEVER produce an invalid payload, so if this fires it
    means a caller is passing something the schema doesn't support.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utc_now_iso():
    """ISO-8601 UTC timestamp, second precision (greppable, stable)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify_plan_warning(text):
    """Heuristically classify a plan-level warning string into ``(severity, category)``.

    The parser emits warnings as plain strings built in ``analyze_plan``; we
    do the string-match here so the schema enums stay well-defined.

    Non-sargable join predicates check first and get the highest severity
    (``error``) because they invalidate every other tuning suggestion —
    no index or buffer tweak will help until the predicate is rewritten.
    """
    t = (text or "").lower()
    if "non-sargable" in t or "nonsargable" in t:
        return ("error", "nonsargable_join")
    if "full table scan" in t or "full scan" in t:
        return ("warn", "full_scan")
    if "block nested" in t or "bnl" in t:
        return ("warn", "bnl")
    if "hash join" in t:
        return ("warn", "hash_join")
    if "temp table" in t or "materialize" in t or "tmp table" in t:
        return ("warn", "temp_table")
    if "sort operation" in t or "filesort" in t or ("sort" in t and "disk" in t):
        return ("warn", "filesort")
    if "semijoin" in t or "semi join" in t:
        return ("info", "semijoin")
    if "index merge" in t:
        return ("info", "index_merge")
    return ("warn", "other")


def _classify_suggestion(text):
    """Classify a suggestion string into ``(severity, category, target_variable)``.

    Returns a triple so the caller can populate the sidecar fields in one
    pass. ``target_variable`` is ``None`` when the suggestion isn't about a
    specific MySQL variable.
    """
    t = (text or "").lower()
    # Rewrite suggestions (non-sargable join predicate, IN→JOIN, etc.) are
    # high-severity because they change the query itself — the biggest
    # possible wins, ranked above any variable / index tweak.
    if "rewrite the join" in t or "rewrite the query" in t or "non-sargable" in t:
        return ("high", "rewrite", None)

    tuning_vars = (
        "innodb_buffer_pool_size", "sort_buffer_size", "join_buffer_size",
        "tmp_table_size", "max_heap_table_size", "read_buffer_size",
        "read_rnd_buffer_size", "innodb_log_buffer_size",
        "innodb_log_file_size",
    )
    for var in tuning_vars:
        if var in t:
            # Buffer pool is a much higher-stakes change than session vars.
            sev = "high" if var == "innodb_buffer_pool_size" else "medium"
            return (sev, "tuning_variable", var)
    if "innodb_flush_log_at_trx_commit" in t:
        return ("high", "durability", "innodb_flush_log_at_trx_commit")
    if "create index" in t or "add index" in t or "add indexes" in t:
        return ("high", "index", None)
    if "alter table" in t and "engine" in t:
        return ("high", "engine", None)
    if "optimizer_switch" in t:
        return ("medium", "optimizer_switch", None)
    return ("low", "other", None)


def _split_action_why(text):
    """Split the advisor's "action. Why: reason." pattern into two fields.

    Returns ``(action, why)``. ``why`` is empty string if the marker isn't
    present — callers should omit the key in that case, not emit ``""``.
    """
    if not text:
        return ("", "")
    parts = _WHY_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        action = parts[0].strip().rstrip(".")
        return (action + ".", parts[1].strip())
    return (text.strip(), "")


def _compute_plan_summary(root):
    """Walk the parsed EXPLAIN tree and return the ``plan_summary`` dict.

    Counts are approximations — ``rows_examined_estimate`` sums the
    actual_rows of leaf table-access nodes, which matches the intuition of
    "how many rows the storage engine actually had to touch" but doesn't
    include range-lookup amplification.
    """
    stats = {"op_count": 0, "max_depth": 0, "rows_examined": 0}

    def _walk(node, depth):
        stats["op_count"] += 1
        if depth > stats["max_depth"]:
            stats["max_depth"] = depth
        children = node.get("children") or []
        if not children:
            # Leaf — count as examined rows (approximates Handler_read_* totals)
            stats["rows_examined"] += int(node.get("rows") or 0)
        for c in children:
            _walk(c, depth + 1)

    _walk(root, 1)
    return {
        "total_time_ms": round(float(root.get("total_time") or 0), 3),
        "rows_sent": int(root.get("rows") or 0),
        "rows_examined_estimate": stats["rows_examined"],
        "operator_count": stats["op_count"],
        "max_depth": stats["max_depth"],
    }


def _collect_operator_complexities(root):
    """Walk the parsed tree and return per-operator complexity entries.

    One entry per node that carries ``details.complexity`` (attached by
    ``parser.parse_node`` via :mod:`myflames.complexity`). The list is
    emitted in depth-first pre-order to match the visual reading order of
    the diagram view — first index is the outermost operator.

    Entries are keyed by ``folded_label`` and ``short_label`` so HTML
    consumers can cross-reference with the ``teach_hooks`` list (same
    convention). Omitted entirely if no node has complexity metadata.
    """
    out = []

    def _walk(node):
        if not isinstance(node, dict):
            return
        details = node.get("details") or {}
        complexity = details.get("complexity")
        if isinstance(complexity, dict):
            entry = {
                "folded_label": node.get("folded_label") or "",
                "short_label": node.get("short_label") or "",
                "complexity": dict(complexity),
            }
            out.append(entry)
        for child in node.get("children") or []:
            _walk(child)

    _walk(root)
    return out


def _executive_summary_fallback(plan_summary, warnings, suggestions):
    """Minimal one-liner used when build_sidecar can't call the real
    generator (e.g., analysis dict is empty). The rich generator lives in
    :mod:`myflames.glossary` — see :func:`generate_executive_summary`.
    """
    ops = plan_summary.get("operator_count") or 0
    time_ms = plan_summary.get("total_time_ms") or 0
    if time_ms > 0:
        return "Plan has {} operator{} ({:.1f} ms).".format(
            ops, "" if ops == 1 else "s", time_ms,
        )
    return "Plan has {} operator{}.".format(ops, "" if ops == 1 else "s")


def _pick_primary_action(suggestions):
    """Pick the first ``high``-severity suggestion, else the first overall.

    Returns an index into ``suggestions``, or ``None`` if there are none.
    The sidecar exposes this as ``{"ref": "suggestions[<idx>]"}`` so HTML
    consumers can highlight the single most impactful action.
    """
    if not suggestions:
        return None
    for i, s in enumerate(suggestions):
        if s.get("severity") == "high":
            return i
    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_sidecar(
    root,
    analysis,
    *,
    source_type="file",
    engine=None,
    engine_version=None,
    fixture_path=None,
    query_raw=None,
    query_beautified=None,
    teach_hooks=None,
):
    """Build a v1 sidecar dict from a parsed EXPLAIN tree + analysis dict.

    Parameters
    ----------
    root : dict
        The tree returned by :func:`myflames.parser.parse_explain`.
    analysis : dict
        The dict returned by :func:`myflames.parser.analyze_plan` (optionally
        extended by :func:`myflames.advisor.advise`).
    source_type : {"file", "live", "stdin"}
        How myflames obtained the plan. Drives the ``source`` block.
    engine, engine_version, fixture_path, query_raw, query_beautified : str
        Optional metadata. Keys are omitted from the payload when absent.

    Returns
    -------
    dict
        The v1 sidecar payload, already validated. Safe to pass to
        :func:`write_sidecar` or ``json.dumps`` directly.
    """
    plan_summary = _compute_plan_summary(root)

    # optimizer_switches — shape-preserving copy with field-name normalization
    # (parser's ``short_labels`` → sidecar's ``node_labels``).
    optimizer_switches = []
    for sw in (analysis.get("optimizer_switches") or []):
        optimizer_switches.append({
            "name": sw["name"],
            "value": sw["value"],
            "explanation": sw["explanation"],
            "node_labels": list(sw.get("short_labels") or []),
        })

    # Warnings — merge plan-level and environment-level into one array with
    # a ``source`` discriminator. node_labels are pulled from node_highlights.
    warnings = []
    node_highlights = analysis.get("node_highlights") or []
    for text in (analysis.get("warnings") or []):
        severity, category = _classify_plan_warning(text)
        linked = [
            nh["short_label"]
            for nh in node_highlights
            if nh.get("message") == text and nh.get("short_label")
        ]
        entry = {
            "severity": severity,
            "category": category,
            "text": text,
            "source": "plan",
        }
        if linked:
            entry["node_labels"] = list(dict.fromkeys(linked))  # preserve order, dedupe
        warnings.append(entry)
    for text in (analysis.get("environment_warnings") or []):
        warnings.append({
            "severity": "warn",
            "category": "env",
            "text": text,
            "source": "environment",
        })

    # Suggestions — merge, split Why: out of the text, classify.
    suggestions = []
    for text in (analysis.get("suggestions") or []):
        severity, category, target_var = _classify_suggestion(text)
        action, why = _split_action_why(text)
        entry = {
            "severity": severity,
            "category": category,
            "action": action,
            "source": "plan",
        }
        if why:
            entry["why"] = why
        if target_var:
            entry["target_variable"] = target_var
        suggestions.append(entry)
    for text in (analysis.get("environment_suggestions") or []):
        severity, category, target_var = _classify_suggestion(text)
        action, why = _split_action_why(text)
        entry = {
            "severity": severity,
            "category": category,
            "action": action,
            "source": "environment",
        }
        if why:
            entry["why"] = why
        if target_var:
            entry["target_variable"] = target_var
        suggestions.append(entry)

    # Index suggestions — promoted to a top-level array with a stable shape.
    index_suggestions = []
    for hint in (analysis.get("index_suggestions") or []):
        index_suggestions.append({
            "table": hint.get("table") or "",
            "columns": list(hint.get("columns") or []),
            "ddl": hint.get("ddl") or "",
            "reason": hint.get("reason") or "",
        })

    # Rich executive summary comes from glossary.generate_executive_summary
    # — it's deterministic and reads from the same analysis dict we use for
    # warnings/suggestions. The fallback only runs if that import fails.
    try:
        from .glossary import generate_executive_summary
        exec_summary = generate_executive_summary(root, analysis, plan_summary=plan_summary)
    except Exception:
        exec_summary = _executive_summary_fallback(plan_summary, warnings, suggestions)
    primary_idx = _pick_primary_action(suggestions)

    # ---- Build the top-level payload -----------------------------------
    source = {"type": source_type}
    if engine:
        source["engine"] = engine
    if engine_version:
        source["engine_version"] = engine_version
    if fixture_path:
        source["fixture_path"] = fixture_path

    if teach_hooks is None:
        teach_hooks = build_teach_hooks(
            root,
            query_sql=query_raw or query_beautified,
            variables=(analysis or {}).get("collected_variables") or {},
            stats=(analysis or {}).get("collected_stats") or {},
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "myflames_version": __version__,
        "source": source,
        "plan_summary": plan_summary,
        "optimizer_switches": optimizer_switches,
        "warnings": warnings,
        "suggestions": suggestions,
        "executive_summary": exec_summary,
    }

    if query_raw or query_beautified:
        query = {}
        if query_raw:
            query["raw"] = query_raw
        if query_beautified:
            query["beautified"] = query_beautified
        payload["query"] = query

    if index_suggestions:
        payload["index_suggestions"] = index_suggestions

    if primary_idx is not None:
        payload["primary_action"] = {"ref": "suggestions[{}]".format(primary_idx)}

    # Collected environment data — only emitted when the advisor ran.
    collected = {}
    if analysis.get("collected_variables"):
        collected["variables"] = dict(analysis["collected_variables"])
    if analysis.get("collected_stats"):
        collected["stats"] = dict(analysis["collected_stats"])
    if analysis.get("collected_schema"):
        collected["schema"] = dict(analysis["collected_schema"])
    if collected:
        payload["collected"] = collected

    if teach_hooks:
        payload["teach_hooks"] = list(teach_hooks)

    # Big O complexity per operator (schema 1.2). Omitted if the tree has
    # no complexity metadata — keeps payloads minimal and keeps consumers
    # that pin to 1.1 shape-compatible (they just see an optional extra key).
    op_complexities = _collect_operator_complexities(root)
    if op_complexities:
        payload["operator_complexities"] = op_complexities

    validate_sidecar(payload)
    return payload


def validate_sidecar(payload):
    """Fail fast if *payload* violates the v1 schema.

    Raises :class:`SidecarValidationError` on any violation. We don't try
    to recover — a broken sidecar is a programmer error and we'd rather
    crash visibly than emit corrupt output that downstream agents would
    silently rely on.
    """
    if not isinstance(payload, dict):
        raise SidecarValidationError("payload must be a dict")

    required_top = (
        "schema_version", "generated_at", "myflames_version",
        "source", "plan_summary",
        "optimizer_switches", "warnings", "suggestions",
        "executive_summary",
    )
    for key in required_top:
        if key not in payload:
            raise SidecarValidationError("missing required key: " + key)

    if payload["schema_version"] != SCHEMA_VERSION:
        raise SidecarValidationError(
            "schema_version mismatch: got {}, expected {}".format(
                payload["schema_version"], SCHEMA_VERSION
            )
        )

    # source
    source = payload["source"]
    if not isinstance(source, dict):
        raise SidecarValidationError("source must be a dict")
    if source.get("type") not in _SOURCE_TYPES:
        raise SidecarValidationError("source.type invalid: " + str(source.get("type")))
    if "engine" in source and source["engine"] not in _ENGINES:
        raise SidecarValidationError("source.engine invalid: " + str(source["engine"]))

    # plan_summary
    ps = payload["plan_summary"]
    if not isinstance(ps, dict):
        raise SidecarValidationError("plan_summary must be a dict")
    for k in ("total_time_ms", "rows_sent", "rows_examined_estimate",
              "operator_count", "max_depth"):
        if k not in ps:
            raise SidecarValidationError("plan_summary missing: " + k)
        if not isinstance(ps[k], (int, float)):
            raise SidecarValidationError(
                "plan_summary.{} must be numeric, got {}".format(k, type(ps[k]).__name__)
            )

    # optimizer_switches
    if not isinstance(payload["optimizer_switches"], list):
        raise SidecarValidationError("optimizer_switches must be a list")
    for sw in payload["optimizer_switches"]:
        for k in ("name", "value", "explanation", "node_labels"):
            if k not in sw:
                raise SidecarValidationError("optimizer_switch missing: " + k)
        if not isinstance(sw["node_labels"], list):
            raise SidecarValidationError("optimizer_switch.node_labels must be a list")

    # warnings
    if not isinstance(payload["warnings"], list):
        raise SidecarValidationError("warnings must be a list")
    for w in payload["warnings"]:
        if not isinstance(w, dict):
            raise SidecarValidationError("warning must be a dict")
        if w.get("severity") not in _WARNING_SEVERITIES:
            raise SidecarValidationError(
                "warning.severity invalid: " + str(w.get("severity"))
            )
        if w.get("category") not in _WARNING_CATEGORIES:
            raise SidecarValidationError(
                "warning.category invalid: " + str(w.get("category"))
            )
        if w.get("source") not in _WARNING_SOURCES:
            raise SidecarValidationError(
                "warning.source invalid: " + str(w.get("source"))
            )
        if not w.get("text"):
            raise SidecarValidationError("warning missing text")
        if "node_labels" in w and not isinstance(w["node_labels"], list):
            raise SidecarValidationError("warning.node_labels must be a list")

    # suggestions
    if not isinstance(payload["suggestions"], list):
        raise SidecarValidationError("suggestions must be a list")
    for s in payload["suggestions"]:
        if not isinstance(s, dict):
            raise SidecarValidationError("suggestion must be a dict")
        if s.get("severity") not in _SUGGESTION_SEVERITIES:
            raise SidecarValidationError(
                "suggestion.severity invalid: " + str(s.get("severity"))
            )
        if s.get("category") not in _SUGGESTION_CATEGORIES:
            raise SidecarValidationError(
                "suggestion.category invalid: " + str(s.get("category"))
            )
        if s.get("source") not in _WARNING_SOURCES:
            raise SidecarValidationError(
                "suggestion.source invalid: " + str(s.get("source"))
            )
        if not s.get("action"):
            raise SidecarValidationError("suggestion missing action")

    # executive_summary
    if not isinstance(payload["executive_summary"], str) or not payload["executive_summary"]:
        raise SidecarValidationError("executive_summary must be a non-empty string")

    if "teach_hooks" in payload:
        hooks = payload["teach_hooks"]
        if not isinstance(hooks, list):
            raise SidecarValidationError("teach_hooks must be a list")
        for hook in hooks:
            if not isinstance(hook, dict):
                raise SidecarValidationError("teach_hook must be a dict")
            lesson = hook.get("lesson")
            if lesson not in SUPPORTED_LESSONS:
                raise SidecarValidationError("teach_hook.lesson invalid: " + str(lesson))
            match = hook.get("match")
            if not isinstance(match, dict):
                raise SidecarValidationError("teach_hook.match must be a dict")
            if not match.get("folded_label"):
                raise SidecarValidationError("teach_hook.match.folded_label missing")
            controls = hook.get("controls")
            if not isinstance(controls, dict):
                raise SidecarValidationError("teach_hook.controls must be a dict")
            for k, v in controls.items():
                if not isinstance(k, str):
                    raise SidecarValidationError("teach_hook.controls key must be string")
                if not isinstance(v, (int, float, bool, str)):
                    raise SidecarValidationError(
                        "teach_hook.controls value type invalid: {}".format(type(v).__name__)
                    )
            if "query_sql" in hook and not isinstance(hook["query_sql"], str):
                raise SidecarValidationError("teach_hook.query_sql must be string")
            if "note" in hook and not isinstance(hook["note"], str):
                raise SidecarValidationError("teach_hook.note must be string")

    if "operator_complexities" in payload:
        ops = payload["operator_complexities"]
        if not isinstance(ops, list):
            raise SidecarValidationError("operator_complexities must be a list")
        for op in ops:
            if not isinstance(op, dict):
                raise SidecarValidationError("operator_complexity must be a dict")
            if not op.get("folded_label") and not op.get("short_label"):
                raise SidecarValidationError(
                    "operator_complexity missing folded_label and short_label"
                )
            c = op.get("complexity")
            if not isinstance(c, dict):
                raise SidecarValidationError(
                    "operator_complexity.complexity must be a dict"
                )
            for required in ("big_o", "short", "severity", "rationale", "confidence"):
                if required not in c:
                    raise SidecarValidationError(
                        "operator_complexity.complexity missing: " + required
                    )
                if not isinstance(c[required], str) or not c[required]:
                    raise SidecarValidationError(
                        "operator_complexity.complexity.{} must be non-empty string".format(required)
                    )
            if c["severity"] not in _COMPLEXITY_SEVERITIES:
                raise SidecarValidationError(
                    "operator_complexity.complexity.severity invalid: " + c["severity"]
                )
            if c["confidence"] not in _COMPLEXITY_CONFIDENCES:
                raise SidecarValidationError(
                    "operator_complexity.complexity.confidence invalid: " + c["confidence"]
                )
            if "learn_more" in c and not isinstance(c["learn_more"], str):
                raise SidecarValidationError(
                    "operator_complexity.complexity.learn_more must be string"
                )
            # Materialize emits two-phase sub-complexities; validate shape if present.
            for sub_key in ("build_complexity", "scan_complexity"):
                if sub_key in c:
                    sub = c[sub_key]
                    if not isinstance(sub, dict):
                        raise SidecarValidationError(
                            "operator_complexity.complexity.{} must be a dict".format(sub_key)
                        )
                    for sub_req in ("big_o", "short", "severity", "rationale", "confidence"):
                        if sub_req not in sub:
                            raise SidecarValidationError(
                                "operator_complexity.complexity.{}.{} missing".format(
                                    sub_key, sub_req,
                                )
                            )

    return True


def write_sidecar(path, payload):
    """Write *payload* to *path* as pretty-printed UTF-8 JSON.

    Always validates before writing. ``sort_keys=False`` preserves the
    reading order built in :func:`build_sidecar` (schema → source → summary
    → switches → warnings → suggestions → collected).
    """
    validate_sidecar(payload)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")


def load_sidecar(path):
    """Load and re-validate a sidecar file. Used by HTML renderers and tests."""
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    validate_sidecar(payload)
    return payload


def sidecar_path_for(output_path):
    """Return the sidecar path for a given output path.

    Rules:
      - ``foo.svg``  → ``foo.json``
      - ``foo.html`` → ``foo.json`` (same base → both renderings share the sidecar)
      - anything else → ``foo.json`` (append, not replace)

    Returns ``None`` if *output_path* is falsy.
    """
    if not output_path:
        return None
    base, ext = os.path.splitext(output_path)
    if ext.lower() in (".svg", ".html", ".htm"):
        return base + ".json"
    return output_path + ".json"
