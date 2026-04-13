# Task 4: Diagram (Visual Explain style)

## Goal

Add a **diagram** output that resembles **MySQL Workbench Visual Explain**: left-to-right flow, table access boxes (operation, table, index), nested-loop diamonds (outer/inner), row counts on edges/boxes, time or cost in tooltips. Optionally support a Graphviz-based layout engine.

## Context

- **Repo state:** Parser and other output types (flamegraph, bargraph, treemap) in place; tree structure with parent/child and timing.
- **Key files:** `myflames/parser.py`, `myflames/output_diagram.py`, `myflames/output_diagram_graphviz.py`; `docs/VISUAL_EXPLAIN_REFERENCE.md` for Workbench conventions.
- **Constraints:** Reuse same parser; built-in SVG layout and/or Graphviz (`dot`) as optional engine; match Visual Explain semantics (access type, nested loops, row counts).

## Prompts

<!-- Paste the exact prompt(s) and important follow-ups you used -->

```

```

## Outcome

- **Modules:** `myflames/output_diagram.py` — `render_diagram()` (built-in SVG); `myflames/output_diagram_graphviz.py` — Graphviz path when `dot` is available.
- **CLI:** `--type diagram`, `--diagram-engine svg|graphviz`; left-to-right plan with boxes/diamonds and optional tooltips.
