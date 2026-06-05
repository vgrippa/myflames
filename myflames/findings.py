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
    if severity in ("error", "high"):
        return "high"
    if category in _MEDIUM_CONFIDENCE_CATEGORIES:
        return "medium"
    return "high"


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

    # Stable sort: severity, then confidence. Python's sort is stable, so equal
    # keys preserve the warnings-before-suggestions, original-order ordering.
    findings.sort(key=lambda f: (
        _SEVERITY_RANK.get(f["severity"], 3),
        _CONFIDENCE_RANK.get(f["confidence"], 3),
    ))
    return findings


def available_triggers(payload):
    """The set of categories + severities present in this plan's warnings —
    what a meaningful ``--fail-on`` could match for this plan."""
    cats = set()
    for w in payload.get("warnings") or []:
        cats.add(w.get("category"))
        cats.add(w.get("severity"))
    cats.discard(None)
    return cats


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
