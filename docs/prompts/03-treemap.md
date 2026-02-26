# Task 3: Treemap

## Goal

Add a **treemap** output type: hierarchical rectangles where area is proportional to **total time** (including children). Support interactivity (e.g. click to zoom, search, tooltips) and reuse the same parser/tree.

## Context

- **Repo state:** Parser, flame graph, and bargraph implemented; shared tree from `parser.py`.
- **Key files:** `myflames/parser.py`, `myflames/output_treemap.py`; existing SVG/JS patterns from flame graph for zoom and search if applicable.
- **Constraints:** Single parser; treemap layout (e.g. squarified or slice-dice) in code; optional interactivity in SVG.

## Prompts

<!-- Paste the exact prompt(s) and important follow-ups you used -->

```

```

## Outcome

- **Module:** `myflames/output_treemap.py` — `render_treemap()`.
- **CLI:** `--type treemap`; hierarchy by total time, optional zoom/search/tooltips.
