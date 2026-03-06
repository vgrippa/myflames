# Testing Protocol — myflames

This document describes the QA protocol for verifying all four SVG output types.
Use the `/test` slash command to run this automatically.

## What is tested

### Automated (run by `/test`)

| Check | What it verifies |
|-------|-----------------|
| Python unittest suite | Parser correctness, all renderers produce valid SVG, CLI integration, 38+ tests |
| No truncated text (`…`) | `_clip()` replaced by `_wrap()` — all info panel text word-wraps instead of cutting off |
| Info panel present | `fill="#f8f9fc"` card, "How to read", "Query Analysis" title visible in all output types |
| Warnings/Suggestions present | `analyze_plan()` output surfaced in panel for fixtures with known issues |
| Flamegraph height == viewBox | `cli.py` patches both attributes; panel never clipped by browser |
| Diagram JS features | `svgYFromEvent`, drag exclusion on `text`/`tspan`, pre-allocated `details-l*` elements |
| BNL regression | BNL warning and `join_buffer_size` suggestion appear in flamegraph for bnl fixture |

### Manual visual checks (browser)

| Graph | Key things to verify |
|-------|---------------------|
| Flamegraph | Panel visible below flames; text wraps; "How to read" is MySQL-specific (not generic call stacks) |
| Bargraph | Hover shows multi-line details; bars sorted slowest first; text selectable |
| Treemap | Hover shows details; scroll works; click-to-zoom; double-click resets |
| Diagram | Zoom scoped to graph area only; drag doesn't steal text clicks; pin-to-details works; Ctrl+F search |

## Canonical test fixtures

| Fixture | Analysis content |
|---------|----------------|
| `test/mysql-explain-hash-join.json` | Full scan (users + orders), hash join warning, join_buffer_size suggestion |
| `test/mysql-explain-bnl.json` | Full scan (t1 + t2), BNL join buffer warning, join_buffer_size suggestion |
| `test/mysql-explain-complex-join.json` | Full scan on `<temporary>` + dept_emp, filesort |
| `test/fixtures/explain-001-*.json` | Full scan on users (3000 rows) |
| `test/fixtures/explain-008-*.json` | ICP optimizer feature (index_condition_pushdown=on) |
| `test/fixtures/explain-052-*.json` | Temp table (Materialize) + filesort |

## Known SVG constraints

- Flamegraph SVGs from `flamegraph.py` include a `viewBox` attribute that must be updated alongside `height` whenever the panel is injected (`cli.py`).
- Info panel text uses word-wrap (`_wrap()` in `parser.py`) with ~6.2px/char for 11px Arial. Adjust `max_chars` calculation if font or size changes.
- Pre-allocated `<text id="details-l{i}">` elements (bargraph × 8, treemap × 5, diagram × 6) are populated by JS on hover/click; never use `innerHTML` for SVG text.
- Interactive events (zoom, drag) in diagram must be scoped via `svgYFromEvent()` to prevent capturing scroll events in the info panel.
