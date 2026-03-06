# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**myflames** visualizes MySQL `EXPLAIN ANALYZE FORMAT=JSON` output as interactive SVG flame graphs, bar charts, treemaps, and Visual Explain–style diagrams. It requires MySQL 8.4+ with `explain_json_format_version = 2` and Python 3.7+ with no external dependencies.

The project also contains the upstream [Brendan Gregg FlameGraph](https://github.com/brendangregg/FlameGraph) Perl scripts (`flamegraph.pl`, `stackcollapse-*.pl`, etc.) which are separate from the Python myflames package.

## Commands

### Run (from repo root)

```bash
python3 -m myflames explain.json > output.svg
python3 -m myflames --type bargraph explain.json > output.svg
python3 -m myflames --type treemap explain.json > output.svg
python3 -m myflames --type diagram explain.json > output.svg
python3 -m myflames --type diagram --diagram-engine graphviz explain.json > output.svg

# stdin is also accepted (- is the default input)
cat explain.json | python3 -m myflames - > output.svg
```

The `mysql_explain.py`, `mysql_explain_flamegraph.py`, and `mysql_explain_bargraph.py` files in the root are thin wrappers that call `myflames.cli.main()`.

### Test

```bash
# Full automated test suite (Python unittest + Perl regression)
./run-tests.sh

# Python tests only (38+ tests across parser, renderers, and CLI)
python3 -m unittest discover -s test -p "test_myflames.py" -v

# Run a single test class
python3 -m unittest test.test_myflames.TestParser -v

# Perl regression tests only (stackcollapse-perf.pl)
./test.sh
# Regenerate expected Perl results after changing stackcollapse-perf.pl
./record-test.sh
```

### Generate EXPLAIN fixtures (one-time, requires Docker)

```bash
# Spins up MySQL 8.4, creates a schema, seeds data, runs 60+ EXPLAIN ANALYZE queries,
# and saves each JSON to test/fixtures/. Commit the resulting files.
./scripts/generate-fixtures.sh
```

The fixture files in `test/fixtures/` are committed to the repo so tests run
with no external dependencies after generation.

## Architecture

### Python package: `myflames/`

All myflames logic lives here. The data flow is:

1. **`parser.py`** — Single entry point for all output types. `parse_explain(json_text)` builds a unified tree of nodes. Each node contains `total_time`, `self_time`, `folded_label`, `details` (tooltip text, including `join_algorithm`, `pushed_index_condition`), and a `children` list. `load_explain_json` strips an optional `EXPLAIN:` prefix before JSON parsing. **`analyze_plan(root)`** scans the tree and returns structured data: full_scans, hash_joins, temp_tables, filesorts, optimizer_features, warnings, suggestions. **`render_analysis_panel(analysis, x, y, width)`** returns (svg_lines, height) for the "Query Analysis" info box. Helper functions: `build_flame_entries`, `flatten_nodes`, `build_diagram_steps`, `enhance_tooltip_flame`.

2. **`cli.py`** — Argument parsing (`--type`, `--width`, `--height`, `--colors`, `--title`, `--inverted`, `--no-enhance`, `--diagram-engine`). Reads JSON from file or stdin, calls `parse_explain`, then `analyze_plan(root)`; passes `analysis` to all renderers. For flamegraph, injects the analysis panel by patching SVG height and inserting panel markup before `</svg>`. Width defaults: 1800px for flamegraph, 1200px for all others.

3. **`flamegraph.py`** — Pure-Python port of Brendan Gregg's `flamegraph.pl`. Takes folded stack text and returns an SVG string. Called only by the `flamegraph` path.

4. **`output_bargraph.py`** — Renders a horizontal bar chart sorted by self-time (slowest first).

5. **`output_treemap.py`** — Renders a hierarchical treemap where area ∝ total time. Supports click-to-zoom and search.

6. **`output_diagram.py`** — Built-in SVG Visual Explain diagram: left-to-right flow, table access boxes, nested-loop diamonds.

7. **`output_diagram_graphviz.py`** — Same diagram via Graphviz `dot`; falls back to `output_diagram.py` if `dot` is not on PATH.

### Key design constraints

- **No external Python dependencies** — everything uses stdlib only.
- **Single parser for all renderers** — never parse the JSON in output modules; always use `parser.parse_explain`.
- **`explain_json_format_version = 2` only** — the JSON schema used by the parser is MySQL 8.4+ format v2. The Perl scripts share this constraint.
- **Preserve input order** — `inputs[0]` is the outer table, `inputs[1]` is inner for joins. Do not sort children; order matters for diagram layout.

### Legacy Perl scripts

`mysql-explain.pl`, `mysql-explain-flamegraph.pl`, `mysql-explain-bargraph.pl`, `stackcollapse-mysql-explain-json.pl` are the original Perl implementation. They are not the primary target for changes. See `LEGACY-PERL.md` for their options. The upstream `flamegraph.pl` and `stackcollapse-perf.pl` (and variants) are Brendan Gregg's originals and should not be modified.

### Test fixtures

- `test/mysql-explain-*.json` — hand-crafted MySQL EXPLAIN JSON samples (always present).
- `test/fixtures/explain-*.json` — 60+ real MySQL EXPLAIN JSONs generated by `scripts/generate-fixtures.sh`; commit after generation.
- `test/test_myflames.py` — Python `unittest` suite; auto-discovers all fixtures in both locations.
- `test/perf-*.txt` — `perf script` samples for the upstream Perl stackcollapse regression suite (`./test.sh`).
- `test/results/` — Expected collapsed output; regenerate with `./record-test.sh` after changing `stackcollapse-perf.pl`.

## Coding Standards

### Before writing code
- Always read the file before editing it. Understand existing patterns before adding new ones.
- Never parse JSON in output modules — always use `parser.parse_explain` and `analyze_plan`.
- Grep for existing helpers before writing new ones (`xml_escape`, `attr_escape`, `_format_time`, `_wrap`, etc. in `parser.py`).

### SVG output rules
- Always update both `height` AND `viewBox` together when extending an SVG. Missing one causes the browser to clip content silently.
- Use word-wrap (`_wrap()` in `parser.py`) for info panel text — never truncate with `…`.
- Use pre-allocated `<text id="details-l{i}">` elements populated by JS, not `innerHTML` or dynamic element creation.
- Scope interactive events (wheel/mousedown) to their target area using `svgYFromEvent()` coordinate mapping.
- All user-visible strings must pass through `xml_escape()` (SVG text content) or `attr_escape()` (HTML attributes).

### Testing
- Run `python3 -m unittest discover -s test -p "test_myflames.py" -v` before considering any change done.
- Add a test for every new behavior. If it can break silently, it needs a test.
- Use `/test` (slash command) for the full QA protocol including visual checks.
- Use `/generate-demos` (slash command) to regenerate all demo SVGs after any renderer change.

### Python style
- stdlib only — no external dependencies, ever.
- Python 3.7+ compatible — no walrus operator, no `str | None` union syntax, no `match` statements.
- Keep functions focused. If a renderer needs a helper, put shared helpers in `parser.py`.

### What not to do
- Do not add docstrings, comments, or type annotations to code you didn't change.
- Do not add error handling for scenarios that cannot happen in normal operation.
- Do not modify `flamegraph.pl`, `stackcollapse-perf.pl`, or other Brendan Gregg originals.
- Do not sort `inputs[]` — order is semantically significant (outer vs inner table in joins).
