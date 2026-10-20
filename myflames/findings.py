"""
Findings + CI gate logic.

Two agent/CI-facing projections built from the sidecar payload:

- :func:`build_findings` — a single ranked list merging plan warnings and
  suggestions, each tagged with a ``confidence`` so an agent knows when to trust
  the rule vs escalate to its own reasoning.
- :func:`evaluate_check` — decide whether a plan should fail a CI gate given a
  set of ``--fail-on`` triggers (warning categories, severities, or ``any``).

Both read the dict produced by :func:`myflames.output_sidecar.build_sidecar`.
No new data is invented here — confidence is derived from the category/severity
the advisor already assigned (one source, several projections).
"""
import re

# Lower number sorts first (most urgent).
_SEVERITY_RANK = {
    "error": 0, "high": 0,
    "warn": 1, "medium": 1,
    "info": 2, "low": 2,
}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

# Categories triggered by a concrete plan signal (a specific access_type, a
# Sort node, a temp-table marker) are high-confidence. "other" is a catch-all
# the classifier fell through to, so it's medium. Environment findings depend
# on collected server vars, which may be stale — medium.
_MEDIUM_CONFIDENCE_CATEGORIES = frozenset({"other", "env"})


def _confidence(category, severity):
    # Two tiers are produced today: high (a concrete plan signal) and medium
    # (catch-all / env-derived, which can be stale). "low" is defined in
    # _CONFIDENCE_RANK for sort stability and forward compatibility, but the
    # current rules never emit it.
    if severity in ("error", "high"):
        return "high"
    if category in _MEDIUM_CONFIDENCE_CATEGORIES:
        return "medium"
    return "high"


def _rank_key(severity, category):
    """The single ranking policy: ``(severity_rank, confidence_rank)``.

    Shared by :func:`build_findings` (which ranks warnings + suggestions
    together) and :func:`primary_suggestion_index` (which picks the one "Fix
    first" action). Because both dereference this one key, the ranked ``advise``
    list and the HTML "Fix first" card can never disagree about relative
    priority — the divergence that used to be possible when the card had its own
    ad-hoc "first high, else first" heuristic.
    """
    return (
        _SEVERITY_RANK.get(severity, 3),
        _CONFIDENCE_RANK.get(_confidence(category, severity), 3),
    )


def primary_suggestion_index(payload):
    """Index into ``payload['suggestions']`` of the top-ranked actionable fix,
    or ``None`` when there are no suggestions.

    This is the one "Fix first" selector. The sidecar's ``primary_action.ref``
    and the HTML card both resolve to it, ranked by the same :func:`_rank_key`
    policy ``build_findings`` uses (severity, then confidence, then original
    order via the stable sort). A warning can outrank every suggestion in the
    unified list, but a warning is a *problem*, not an *action* — the card
    promotes the highest-priority thing to actually do.
    """
    suggestions = payload.get("suggestions") or []
    if not suggestions:
        return None
    order = sorted(
        range(len(suggestions)),
        key=lambda i: _rank_key(
            suggestions[i].get("severity", "low"),
            suggestions[i].get("category", "other"),
        ) + (i,),
    )
    return order[0]


def build_findings(payload):
    """Return a ranked list of findings from a sidecar *payload*.

    Each finding is a dict with stable keys: ``kind`` ("warning" | "suggestion"),
    ``severity``, ``category``, ``text``, ``source``, ``confidence``, and
    finding-specific extras (``node_labels``, ``why``, ``target_variable``).
    Ranked by severity, then by confidence, then by original order.
    """
    findings = []
    for w in payload.get("warnings") or []:
        findings.append({
            "kind": "warning",
            "severity": w.get("severity", "warn"),
            "category": w.get("category", "other"),
            "text": w.get("text", ""),
            "source": w.get("source", "plan"),
            "node_labels": list(w.get("node_labels") or []),
            "confidence": _confidence(w.get("category", "other"), w.get("severity", "warn")),
        })
        docs = _docs_for(w, payload)
        if docs:
            findings[-1]["docs"] = docs
    for s in payload.get("suggestions") or []:
        entry = {
            "kind": "suggestion",
            "severity": s.get("severity", "low"),
            "category": s.get("category", "other"),
            "text": s.get("action", ""),
            "source": s.get("source", "plan"),
            "confidence": _confidence(s.get("category", "other"), s.get("severity", "low")),
        }
        if s.get("why"):
            entry["why"] = s["why"]
        if s.get("target_variable"):
            entry["target_variable"] = s["target_variable"]
        findings.append(entry)

    # Stable sort by the shared ranking policy: severity, then confidence.
    # Python's sort is stable, so equal keys preserve the
    # warnings-before-suggestions, original-order ordering.
    findings.sort(key=lambda f: _rank_key(f["severity"], f["category"]))
    return findings


#: The rule catalog: one entry per warning category, in display order.
#: ``myflames check --list-rules`` / ``advise --list-rules`` print it, and
#: ``advise --json`` attaches the matching ``docs`` link to each finding.
#: Modeled on MySQL Shell's upgrade checker, where every check has a stable
#: ID, a title, and a documentation link.
RULES = (
    ("nonsargable_join", "error",
     "A function wraps a join column, so no index on it can be used.",
     "https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html"),
    ("full_scan", "warn",
     "A base table is read in full instead of through an index.",
     "https://dev.mysql.com/doc/refman/8.4/en/table-scan-avoidance.html"),
    ("misestimate", "warn",
     "The optimizer's row estimate is far from the rows actually read.",
     "https://dev.mysql.com/doc/refman/8.4/en/innodb-persistent-stats.html"),
    ("filesort", "warn",
     "Rows are sorted after they are read instead of read in index order.",
     "https://dev.mysql.com/doc/refman/8.4/en/order-by-optimization.html"),
    ("temp_table", "warn",
     "An intermediate result is materialized into an internal temporary table.",
     "https://dev.mysql.com/doc/refman/8.4/en/internal-temporary-tables.html"),
    ("hash_join", "warn",
     "A join is executed as a hash join, buffered by join_buffer_size.",
     "https://dev.mysql.com/doc/refman/8.4/en/hash-joins.html"),
    ("bnl", "warn",
     "A Block Nested-Loop join buffer is in use.",
     "https://dev.mysql.com/doc/refman/8.4/en/nested-loop-joins.html"),
    ("redundant_index", "warn",
     "An index duplicates the leading columns of another index on the same table.",
     "https://dev.mysql.com/doc/refman/8.4/en/sys-schema-redundant-indexes.html"),
    ("cold_cache", "info",
     "The first run read pages from disk that later runs found in the buffer pool.",
     "https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool.html"),
    ("semijoin", "info",
     "A subquery is executed with a semijoin strategy.",
     "https://dev.mysql.com/doc/refman/8.4/en/semijoins-antijoins.html"),
    ("index_merge", "info",
     "Several indexes on one table are scanned and merged.",
     "https://dev.mysql.com/doc/refman/8.4/en/index-merge-optimization.html"),
    ("env", "warn",
     "A server setting collected from a live connection limits this plan.",
     "https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html"),
    ("engine", "warn",
     "A table uses a storage engine other than InnoDB.",
     "https://dev.mysql.com/doc/refman/8.4/en/storage-engines.html"),
    ("durability", "warn",
     "The redo log flush setting trades crash durability for speed.",
     "https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html"
     "#sysvar_innodb_flush_log_at_trx_commit"),
    ("other", "warn",
     "A plan warning that does not fit a more specific category.",
     None),
)

_RULE_DOCS = dict((r[0], r[3]) for r in RULES)

#: MySQL Reference Manual versions with these pages (checked 2026-10-19:
#: dev.mysql.com/doc/refman/<v>/en/ answers for each; an unknown version
#: redirects away). Other MySQL servers get the version-neutral
#: ``refman/en/`` path, which redirects to the current manual.
_REFMAN_VERSIONS = frozenset({"8.0", "8.4", "9.7", "26.7"})
_REFMAN_PREFIX = "https://dev.mysql.com/doc/refman/8.4/en/"
#: Pages the 8.0 manual names differently (8.4+ split semijoins.html into
#: semijoins-antijoins.html; the old name redirects to 8.0).
_REFMAN_80_PAGES = {"semijoins-antijoins.html": "semijoins.html"}

#: The advisor's missing-index environment warning
#: (advisor._rule_missing_indexes) is about indexes, not server variables.
_MISSING_INDEX_DOCS = "https://dev.mysql.com/doc/refman/8.4/en/mysql-indexes.html"


_MARIADB_DOCS_BASE = "https://mariadb.com/docs/server/"
#: MariaDB documentation pages for the findings that have a MariaDB
#: equivalent (each checked 2026-10-19: 200 with a matching title; a missing
#: page answers 404). Findings without one get no link rather than a MySQL
#: page that describes different behavior (for example MySQL hash joins
#: spilling to disk, which MariaDB BNLH does not do).
_QO = "ha-and-performance/optimization-and-tuning/"
_MARIADB_DOCS = {
    "full_scan": _QO + "optimization-and-indexes/building-the-best-index-for-a-given-select",
    "missing_index": _QO + "optimization-and-indexes/building-the-best-index-for-a-given-select",
    "filesort": _QO + "optimization-and-indexes/building-the-best-index-for-a-given-select",
    "misestimate": _QO + "query-optimizations/statistics-for-optimizing-queries",
    "hash_join": _QO + "query-optimizer/block-based-join-algorithms",
    "bnl": _QO + "query-optimizer/block-based-join-algorithms",
    "redundant_index": _QO + "optimization-and-indexes/compound-composite-indexes",
    "semijoin": _QO + "query-optimizations/subquery-optimizations/semi-join-subquery-optimizations",
    "index_merge": _QO + "mariadb-internal-optimizations/fair-choice-between-range-and-index_merge-optimizations",
    "cold_cache": "server-usage/storage-engines/innodb/innodb-buffer-pool",
    "env": "server-management/variables-and-modes/server-system-variables",
    "engine": "server-usage/storage-engines",
    "durability": "server-usage/storage-engines/innodb/innodb-system-variables#innodb_flush_log_at_trx_commit",
}


def _docs_for(warning, payload):
    """Docs link for one warning: its category's catalog page, in the
    manual of the plan's MySQL version when the source names one; for a
    MariaDB plan the MariaDB page, or no link when none fits."""
    category = warning.get("category")
    url = _RULE_DOCS.get(category)
    text = warning.get("text") or ""
    key = category
    if category == "env" and text.startswith("Table ") and " has no index covering (" in text:
        url = _MISSING_INDEX_DOCS
        key = "missing_index"
    if ((payload or {}).get("source") or {}).get("engine") == "mariadb":
        page = _MARIADB_DOCS.get(key)
        return _MARIADB_DOCS_BASE + page if page else None
    if not url or not url.startswith(_REFMAN_PREFIX):
        return url
    source = (payload or {}).get("source") or {}
    version = source.get("engine_version")
    if source.get("engine") != "mysql" or not isinstance(version, str) or not version:
        return url
    match = re.match(r"(\d+)\.(\d+)", version)
    series = "{}.{}".format(match.group(1), match.group(2)) if match else ""
    rest = url[len(_REFMAN_PREFIX):]
    if series == "8.0":
        page, sep, anchor = rest.partition("#")
        rest = _REFMAN_80_PAGES.get(page, page) + sep + anchor
    if series in _REFMAN_VERSIONS:
        return "https://dev.mysql.com/doc/refman/{}/en/{}".format(series, rest)
    return "https://dev.mysql.com/doc/refman/en/" + rest


def rule_catalog():
    """The rule catalog as a list of dicts (stable keys: ``category``,
    ``default_severity``, ``description``, ``docs``)."""
    return [{"category": c, "default_severity": s, "description": d, "docs": u}
            for c, s, d, u in RULES]


def format_rule_catalog():
    """Human-readable rule catalog for ``--list-rules``."""
    width = max(len(r[0]) for r in RULES)
    lines = []
    for category, severity, description, docs in RULES:
        lines.append("{}  {:<5}  {}".format(category.ljust(width), severity, description))
        if docs:
            lines.append("{}         {}".format(" " * width, docs))
    return "\n".join(lines) + "\n"


def known_ignores():
    """Every token ``--ignore`` accepts: warning and suggestion categories."""
    from . import output_sidecar
    return set(output_sidecar._WARNING_CATEGORIES) | set(output_sidecar._SUGGESTION_CATEGORIES)


def unknown_ignores(ignore):
    """Sorted *ignore* tokens that are not categories (empty == all valid)."""
    known = known_ignores()
    return sorted(set(t.strip() for t in ignore
                      if t and t.strip() and t.strip().lower() not in known))


def apply_ignore(payload, ignore):
    """Return a shallow copy of *payload* without warnings or suggestions whose
    category is in *ignore*. The caller's payload is never mutated, and the
    ``primary_action`` pick is left to be recomputed from what remains."""
    drop = set(t.strip().lower() for t in ignore if t and t.strip())
    if not drop:
        return payload
    out = dict(payload)
    out["warnings"] = [w for w in payload.get("warnings") or []
                       if w.get("category") not in drop]
    out["suggestions"] = [s for s in payload.get("suggestions") or []
                          if s.get("category") not in drop]
    return out


def summarize(findings):
    """Counts by kind and severity, like the upgrade checker's summary."""
    counts = {}
    for f in findings:
        key = f["kind"] + "s"
        counts.setdefault(key, {})
        counts[key][f["severity"]] = counts[key].get(f["severity"], 0) + 1
    return counts


def available_triggers(payload):
    """The set of categories + severities present in this plan's warnings —
    what a meaningful ``--fail-on`` could match for this plan."""
    cats = set()
    for w in payload.get("warnings") or []:
        cats.add(w.get("category"))
        cats.add(w.get("severity"))
    cats.discard(None)
    return cats


def known_triggers():
    """Every ``--fail-on`` token that *could* match some plan: the full warning
    category and severity vocabulary plus the special ``any``.

    Distinct from :func:`available_triggers` (what is present in one plan): this
    is the universe used to reject a misspelled trigger, so ``--fail-on
    full_scan`` on a clean plan validates as a real (just-unmatched) trigger
    while ``--fail-on fullscan`` is rejected as a typo. Sourced from the same
    enum sets the sidecar schema enforces — one source of truth."""
    # Imported lazily to avoid a module-load cycle (output_sidecar imports
    # nothing from findings, but keep findings importable standalone).
    from . import output_sidecar
    return (set(output_sidecar._WARNING_CATEGORIES)
            | set(output_sidecar._WARNING_SEVERITIES)
            | {"any"})


def unknown_triggers(fail_on):
    """Return the sorted list of *fail_on* tokens that are not valid triggers
    (see :func:`known_triggers`). Empty == all triggers are recognized."""
    known = known_triggers()
    bad = set()
    for t in fail_on:
        if not t or not t.strip():
            continue
        if t.strip().lower() not in known:
            bad.add(t.strip())
    return sorted(bad)


def evaluate_check(payload, fail_on):
    """Decide whether *payload* trips a CI gate.

    *fail_on* is an iterable of triggers — warning categories (``full_scan``,
    ``filesort``, ``temp_table``, …), severities (``error``, ``warn``), or the
    special ``any`` (match every warning). Returns the list of matching warning
    dicts (empty == the gate passes).
    """
    triggers = set(t.strip().lower() for t in fail_on if t and t.strip())
    matched = []
    for w in payload.get("warnings") or []:
        if "any" in triggers \
                or w.get("category") in triggers \
                or w.get("severity") in triggers:
            matched.append(w)
    return matched
