# Task 2: Bar graph

## Goal

Add a **bar chart** output type that shows operations sorted by **self-time** (time spent in that operation alone), so users can quickly see the slowest operations. Reuse the same parser and tree as the flame graph.

## Context

- **Repo state:** Parser and flame graph already implemented; single tree structure in `parser.py`.
- **Key files:** `myflames/parser.py` (e.g. `flatten_nodes()`, node with `self_time_ms` / `total_time_ms`), `myflames/output_bargraph.py`.
- **Constraints:** Same parser, no duplicate parsing logic; output SVG consistent with existing style (title, width, tooltips if applicable).

## Prompts

<!-- Paste the exact prompt(s) and important follow-ups you used -->

```

```

## Outcome

- **Module:** `myflames/output_bargraph.py` — `render_bargraph()`.
- **CLI:** `--type bargraph` in unified CLI; horizontal bars sorted by self-time, optional title/width.
