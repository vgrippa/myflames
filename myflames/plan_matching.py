"""Conservative structural matching of canonical plan operators.

Timing and row measurements never establish identity. Unique semantic subtrees
are matched first, followed by operator identity, matched-parent context and
unique table access. Indistinguishable repeats are tentative pairs: their order
is deterministic for presentation, but they cannot establish a regression.
"""
import hashlib
import json
import math
from collections import defaultdict, deque


_SEMANTIC_FIELDS = (
    "operation", "table_name", "schema_name", "index_name", "access_type",
    "index_access_type", "condition", "ranges", "covering", "join_algorithm",
    "hash_condition", "sort_fields", "pushed_index_condition", "using_mrr",
    "using_rowid_filter", "using_join_buffer",
)


def timing_available(node):
    """Whether the canonical node carries a finite, measured elapsed time.

    The parser uses numeric zero for absent timings; inspect the underlying
    measurement instead. An actual measured zero remains available.
    """
    if not node:
        return False
    value = (node.get("details") or {}).get("actual_last_row_ms")
    try:
        return value is not None and math.isfinite(float(value)) and float(value) >= 0
    except (ValueError, TypeError, OverflowError):
        return False


def self_time_available(node):
    """Self time requires measured total time for each subtracted input."""
    return timing_available(node) and all(
        timing_available(child) for child in node.get("children", [])
        if (child.get("details") or {}).get("input_kind") != "select_list")


def _identity(node):
    details = node.get("details") or {}
    values = {key: details[key] for key in _SEMANTIC_FIELDS if key in details}
    if not values.get("operation"):
        values["operation"] = node.get("full_label") or node.get("short_label", "")
    return json.dumps(values, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _index(nodes):
    parents, identities, fingerprints = {}, {}, {}
    for node in nodes:
        identities[id(node)] = _identity(node)
        for child in node.get("children", []):
            parents[id(child)] = id(node)

    def fingerprint(node):
        key = id(node)
        if key not in fingerprints:
            content = [identities.get(key, _identity(node))]
            content.extend(fingerprint(child) for child in node.get("children", []))
            fingerprints[key] = hashlib.sha256(json.dumps(content).encode("utf-8")).hexdigest()
        return fingerprints[key]

    for node in nodes:
        fingerprint(node)
    return parents, identities, fingerprints


def match_plan_nodes(nodes_before, nodes_after):
    """Return ordered matching records containing original canonical nodes.

    Each record has ``label``, ``before``, ``after`` and ``matching``. Matching
    metadata contains ``method``, ``confidence`` (high/medium/uncertain), and
    ``status`` (unchanged/changed/added/removed/uncertain). Unchanged here means
    equal semantic subtrees, not equal measurements. Node IDs are kept intact.
    This function does not mutate the supplied nodes.
    """
    before, after = list(nodes_before), list(nodes_after)
    pb, ib, fb = _index(before)
    pa, ia, fa = _index(after)
    remaining_b = {id(node): node for node in before}
    remaining_a = {id(node): node for node in after}
    matched, pairs, paired_after = {}, {}, set()

    def pair(b, a, method, confidence):
        bkey, akey = id(b), id(a)
        pairs[bkey] = akey
        paired_after.add(akey)
        matched[bkey] = (a, method, confidence)
        remaining_b.pop(bkey)
        remaining_a.pop(akey)

    def unique_groups(key_before, key_after, method, confidence):
        groups_b, groups_a = defaultdict(list), defaultdict(list)
        for key, node in remaining_b.items():
            value = key_before(key, node)
            if value is not None:
                groups_b[value].append(node)
        for key, node in remaining_a.items():
            value = key_after(key, node)
            if value is not None:
                groups_a[value].append(node)
        count = 0
        for value, bs in groups_b.items():
            aa = groups_a.get(value, [])
            if len(bs) == len(aa) == 1:
                pair(bs[0], aa[0], method, confidence)
                count += 1
        return count

    unique_groups(lambda key, node: fb[key], lambda key, node: fa[key],
                  "unique_subtree", "high")
    unique_groups(lambda key, node: ib[key], lambda key, node: ia[key],
                  "unique_operator", "high")

    # A repeated leaf can be distinguished by its already matched parent. Do
    # not use sibling ordinal: it can change when a join is reordered.
    while unique_groups(
            lambda key, node: (ib[key], pairs[pb[key]]) if pb.get(key) in pairs else None,
            lambda key, node: (ia[key], pa[key]) if pa.get(key) in paired_after else None,
            "parent_context", "high"):
        pass

    def table_key(node):
        details = node.get("details") or {}
        table = details.get("table_name")
        return (details.get("schema_name", ""), table) if table else None

    # A scan becoming an index lookup is a meaningful changed access path.
    unique_groups(lambda key, node: table_key(node), lambda key, node: table_key(node),
                  "unique_table", "medium")

    # Unary operations whose predicate changes can be tied to the same already
    # matched input. Include operation family so Sort is not paired with Limit.
    from .parser import operator_family

    def child_context(node, is_before):
        children = node.get("children", [])
        if not children:
            return None
        ids = [pairs.get(id(child)) if is_before else id(child) for child in children]
        if any(key is None for key in ids):
            return None
        family = operator_family(node.get("details") or {})
        if family == "other":
            return None
        return family, tuple(ids)

    while unique_groups(lambda key, node: child_context(node, True),
                        lambda key, node: child_context(node, False),
                        "child_context", "medium"):
        pass

    # Keep all occurrences visible without claiming arbitrary order is identity.
    by_identity = defaultdict(deque)
    for node in remaining_a.values():
        by_identity[ia[id(node)]].append(node)
    for node in list(remaining_b.values()):
        candidates = by_identity.get(ib[id(node)], [])
        if candidates:
            pair(node, candidates.popleft(), "ambiguous_operator", "uncertain")

    result = []
    for node in before:
        key = id(node)
        if key in matched:
            other, method, confidence = matched[key]
            status = "uncertain" if confidence == "uncertain" else (
                "unchanged" if fb[key] == fa[id(other)] else "changed")
        else:
            other, method, confidence, status = None, "unmatched", "high", "removed"
        result.append({"label": node.get("short_label", ""), "before": node,
                       "after": other, "matching": {"method": method,
                       "confidence": confidence, "status": status}})
    for node in after:
        if id(node) in remaining_a:
            result.append({"label": node.get("short_label", ""), "before": None,
                           "after": node, "matching": {"method": "unmatched",
                           "confidence": "high", "status": "added"}})
    return result
