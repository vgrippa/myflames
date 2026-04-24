"""
Slice 3 / P4: ``assert_svg_contract(svg_text, **kwargs)``.

Shared assertion helper for SVG contract tests across the teach lessons
and output renderers. Centralizes the invariants the project commits to,
so individual test files don't re-implement the same brittle regex
checks.

What it asserts by default (every SVG we emit):

* Well-formed header: ``<svg>`` + ``</svg>``, non-empty body.
* Has a ``viewBox`` attribute (CLAUDE.md SVG rule: always update
  height / viewBox together).
* No ``calcMode="linear"`` on any ``<animate>`` / ``<animateMotion>``
  / ``<animateTransform>`` — linear motion is banned for anything
  except progress bars per the animation-expert skill (Slice 3 / A3).
* Every ``<animate>`` with ``repeatCount="indefinite"`` either carries
  a named ``class`` or a ``begin`` / ``end`` anchor so the page can
  pause / reset it — the "1-second rule" from the same skill.

Opt-in assertions (kwargs):

* ``require_table_names`` : iterable of table names expected to appear.
* ``require_node_count_ge`` : minimum number of ``<g>`` frame groups
  (rough check that the renderer emitted every plan node).
* ``forbid_triple_dot`` : ``True`` to assert the output never emits
  the literal ``...`` ellipsis (V5 rule: Unicode ``…`` only).

Usage
-----
::

    from _svg_contract import assert_svg_contract

    assert_svg_contract(svg, require_table_names=["users", "orders"],
                        forbid_triple_dot=True)

Calls ``unittest.TestCase.fail`` via the passed-in ``test`` object when
a check fails, so tracebacks point to the caller's line.
"""
import re


_ANIMATE_RE = re.compile(r"<animate(?:Motion|Transform)?\b[^>]*>", re.IGNORECASE)
_CALCMODE_LINEAR_RE = re.compile(r'calcMode\s*=\s*"linear"', re.IGNORECASE)
_REPEAT_INDEFINITE_RE = re.compile(r'repeatCount\s*=\s*"indefinite"', re.IGNORECASE)
_HAS_CLASS_OR_BEGIN_RE = re.compile(r'\b(class|begin|id)\s*=', re.IGNORECASE)
_VIEWBOX_RE = re.compile(r'viewBox\s*=\s*"[^"]+"', re.IGNORECASE)
_GROUP_RE = re.compile(r"<g\b", re.IGNORECASE)


def assert_svg_contract(
    svg,
    test,
    require_table_names=None,
    require_node_count_ge=None,
    forbid_triple_dot=False,
):
    """Run the standard SVG-contract checks.

    ``test`` is a ``unittest.TestCase`` instance — we call its
    ``fail`` / ``assertIn`` helpers so tracebacks stay readable.
    """
    if not svg or not isinstance(svg, str):
        test.fail("empty or non-string SVG")

    stripped = svg.strip()
    if not (stripped.startswith("<?xml") or stripped.startswith("<svg")):
        test.fail("SVG does not start with <?xml or <svg>: "
                  + stripped[:40])
    if "</svg>" not in stripped:
        test.fail("SVG missing closing </svg>")

    # viewBox invariant (CLAUDE.md).
    if not _VIEWBOX_RE.search(svg):
        test.fail("SVG missing viewBox attribute on <svg> root")

    # A3 — no calcMode="linear".
    for m in _ANIMATE_RE.finditer(svg):
        block = m.group(0)
        if _CALCMODE_LINEAR_RE.search(block):
            test.fail(
                "banned calcMode=\"linear\" on SMIL element: "
                + block[:120]
            )
        # repeatCount="indefinite" without a handle → can't be paused.
        if _REPEAT_INDEFINITE_RE.search(block):
            if not _HAS_CLASS_OR_BEGIN_RE.search(block):
                test.fail(
                    "unbounded SMIL without class/id/begin anchor: "
                    + block[:120]
                )

    # V5 — triple-dot ellipsis is banned when opted in.
    if forbid_triple_dot and "..." in svg:
        # Find the offending occurrence so the error message is useful.
        idx = svg.find("...")
        test.fail(
            "literal '...' ellipsis found (Unicode … required): "
            + svg[max(0, idx - 30): idx + 30]
        )

    # Table-name presence (bargraph/treemap semantic content).
    if require_table_names:
        for t in require_table_names:
            if t not in svg:
                test.fail(
                    "expected table name {!r} not present in SVG".format(t))

    # Node-group count floor.
    if require_node_count_ge is not None:
        count = len(_GROUP_RE.findall(svg))
        if count < require_node_count_ge:
            test.fail(
                "SVG has {} <g> groups, expected >= {}".format(
                    count, require_node_count_ge))
