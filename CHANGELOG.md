# Changelog

All notable changes to myflames are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] — 2026-04-09

### Added

- **MariaDB 10.11 and 11.4 support** — myflames now auto-detects and visualizes
  MariaDB `ANALYZE FORMAT=JSON` output alongside MySQL `EXPLAIN ANALYZE FORMAT=JSON`.
  All five view types (flame graph, bar chart, treemap, diagram, tree) work with both
  databases. Supported MariaDB input methods:
  - `ANALYZE FORMAT=JSON SELECT ...`
  - `SHOW ANALYZE FORMAT=JSON FOR <connection_id>` (live query analysis)
  - `SHOW EXPLAIN FORMAT=JSON FOR <connection_id>`
- **MariaDB normalization layer** — transparent conversion of MariaDB's
  `query_block/nested_loop/table` JSON structure into the existing MySQL tree format.
  Handles nested loops, filesort, unions, window functions, materialized subqueries,
  derived tables, covering indexes, and correlated subqueries.
- **MariaDB fixture generation** — `scripts/generate-mariadb-fixtures.sh` generates 60
  test fixtures (30 per version) from Docker containers running MariaDB 10.11 and 11.4.
- **28 new MariaDB-specific unit tests** covering format detection, normalization,
  timing accuracy, access type mapping, and analysis plan detection.

## [1.1.0] — 2026-04-01

### Added

- **HTML report output** — `myflames --output report.html explain.json` generates a
  self-contained HTML file with embedded interactive SVG, analysis sidebar, stat cards,
  and export buttons (SVG, JSON, Print/PDF). Works with all five view types.
- **Before vs after comparison** — `myflames compare before.json after.json` produces
  an HTML diff report with total time delta, per-operator changes (self-time, rows,
  loops), new/removed full scans, resolved/new warnings, and a color-coded summary.
- **Guided onboarding** — `myflames guide` prints which view to pick based on what you
  want to learn (time distribution, slowest operator, join order, etc.).
- **`--version` flag** — `myflames --version` prints the installed version.
- **`--output` / `-o` flag** — write output to a file instead of stdout. Automatically
  produces an HTML report when the path ends in `.html`.
- **`sample.json`** — included in the repo root so new users can try the tool in 30
  seconds: `myflames sample.json > query.svg`.
- **Diagram zoom buttons** — +/− and reset (↺) buttons in the bottom-right corner of
  the diagram view replace the previous Ctrl+scroll-wheel zoom. Drag-to-pan and
  double-click-to-reset are unchanged.
- **Installable via pip** — `pip install myflames` installs the `myflames` console
  command. No external dependencies.

### Fixed

- **MySQL CLI output parsing** — the parser now handles all common `mysql` command-line
  output formats automatically:
  - Escaped newlines/tabs (`mysql -N -e` without `-r`)
  - `EXPLAIN` column header (`mysql -e` without `-N`)
  - Table-formatted output (`+---+` borders and `| ... |` rows)
  - UTF-8 BOM (files saved from Windows editors)
  - Junk text before JSON (e.g. MySQL warnings on stderr leaking to stdout)

  Users no longer need `-s -r` flags — any `mysql -e` invocation works. When input
  cannot be parsed, the error message now suggests the correct flags.

  Reported by [Anil Joshi](https://github.com/aniljoshi) — thanks for catching this!

## [1.0.0] — 2025-12-15

### Added

- **Five visualization types** — flame graph (default), bar chart, treemap, Visual
  Explain–style diagram, and collapsible execution tree.
- **Query Analysis panel** — every view includes warnings (full table scans, hash joins,
  BNL join buffers, temp tables, filesorts), optimizer feature detection, tuning
  suggestions, and heuristic index suggestions with DDL.
- **Interactive features** — click-to-zoom, hover details, search (Ctrl+F), pin/unpin,
  drag-to-pan (diagram), collapse/expand (tree).
- **MySQL 9.7 support** — `query_plan` envelope and hypergraph optimizer plans.
- **SQL embedding** — `--query` and `--query-file` flags, plus auto-extraction from
  the EXPLAIN JSON.
- **Time unit auto-detection** — switches between ms and µs based on total query time.
- **Zero dependencies** — pure Python 3.7+ stdlib.
