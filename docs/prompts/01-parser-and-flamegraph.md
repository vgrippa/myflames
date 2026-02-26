# Task 1: Parser and flame graph

## Goal

Parse MySQL `EXPLAIN ANALYZE FORMAT=JSON` output and produce an interactive flame graph (SVG) that shows the query execution plan as a hierarchy, with width proportional to time and Tanel Poder–style labels (e.g. `starts=X rows=Y`).

## Context

- **Repo state:** (e.g. empty repo, or existing Perl scripts to mirror)
- **Key files / references:** `myflames/parser.py`, `myflames/flamegraph.py`; MySQL EXPLAIN JSON format (version 2); Brendan Gregg FlameGraph / Tanel Poder SQL plan flame graph conventions
- **Constraints:** (e.g. Python 3.7+, no extra packages for core parser and flame graph, or list allowed deps)

## Prompts

<!-- Paste the exact prompt(s) and important follow-ups you used -->

```

```

## Outcome

- **Parser:** `myflames/parser.py` — `parse_explain()`, `build_flame_entries()`, `flatten_nodes()`, `enhance_tooltip_flame()`, shared tree structure.
- **Flame graph:** `myflames/flamegraph.py` — `folded_to_svg()`; optional icicle (inverted), color schemes, tooltips.
- **Entrypoint:** CLI or script that reads JSON (file or stdin) and outputs SVG.
