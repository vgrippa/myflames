# Contributing to myflames

Thank you for your interest in contributing to myflames! This guide will help you get set up and make your first contribution.

## Development setup

```bash
git clone https://github.com/vgrippa/myflames.git
cd myflames
python3 -m pip install -e .   # editable install
```

No external dependencies are needed — myflames uses only the Python 3.7+ standard library.

## Running tests

```bash
# Full test suite
./run-tests.sh

# Specific test files
python3 -m unittest discover -s test -p "test_myflames.py" -v
python3 -m unittest discover -s test -p "test_teach.py" -v
```

Every new feature or logic change **must** include tests. The test suite has 1200+ tests and is the primary quality gate.

## Project structure

```
myflames/
  cli.py              # CLI entry point, argument parsing, live-connection
  parser.py           # Single parser for MySQL + MariaDB EXPLAIN JSON
  flamegraph.py       # Flame graph SVG renderer
  output_bargraph.py  # Bar chart SVG renderer
  output_treemap.py   # Treemap SVG renderer
  output_diagram.py   # Visual Explain diagram renderer
  output_tree.py      # Collapsible execution tree renderer
  output_html.py      # HTML wrapper with progressive-disclosure UI
  output_sidecar.py   # JSON sidecar generator (v1 schema)
  advisor.py          # Environment advisor (8 rules)
  glossary.py         # 31 glossary entries (short/technical/newcomer)
  teach/              # Interactive algorithm lessons
    __init__.py       # Lesson registry + render_lesson() API
    _anim.py          # Shared JS animation runtime
    _html.py          # Shared HTML chrome + CSS
    _cost_model.py    # Cost-model functions (MySQL 8.4 / MariaDB 11.4 defaults)
    join_family/      # bnl, hash, join_compare, nested_loop, bka_join, semijoin_weedout
    index_family/     # btree, non_unique_lookup, unique_lookup, icp, index_merge, skip_scan, rowid_filter
    scan_family/      # filesort, tmp, full_scan, filter (+ derived_table wrapper)
    cache_family/     # lru
    bka_join.py       # Top-level lessons not yet folded into a family package
    derived_table.py
    semijoin_weedout.py
    skip_scan.py
    rowid_filter.py
test/
  test_myflames.py    # Parser, renderer, advisor, HTML, sidecar tests
  test_teach.py       # Cost-model invariants + teach lesson tests
  fixtures/           # 140+ EXPLAIN JSON fixtures (MySQL + MariaDB)
docs/
  index.html          # GitHub Pages landing page
  guide/              # Documentation pages
  teach/              # Pre-generated lesson HTML files
  demos/              # Pre-generated query plan demo files
scripts/
  generate-fixtures.sh          # MySQL fixture generation (Docker)
  generate-mariadb-fixtures.sh  # MariaDB fixture generation (Docker)
  regenerate_docs_demos.py      # Rebuild docs/demos from fixture-backed sidecars (+ live restore)
```

## Key constraints

- **No external dependencies.** stdlib only. Python 3.7+ compatible (no walrus operator, no `match` statements).
- **Single parser.** Never parse JSON in output modules — always use `parser.parse_explain()`.
- **SVG rules.** Always update both `height` AND `viewBox` together.
- **Preserve input order.** Do not sort `inputs[]` in the JSON — order is semantically significant (outer vs inner table).
- **Preserve credits.** Never remove the "Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs" attribution.

## Adding a new teach lesson

1. **Cost model** — add cost functions to `teach/_cost_model.py` with constants tied to MySQL 8.4 / MariaDB 11.4 defaults.
2. **Lesson file** — add `teach/<family>/your_lesson.py` (or a top-level `teach/your_lesson.py` if it does not fit a family yet), following an existing lesson in that family.
3. **Register** — add the lesson to the `LESSONS` dict in `teach/<family>/__init__.py` (merged into `teach/__init__.py` automatically).
4. **Tests** — add cost-model unit tests and HTML content assertions to `test/test_teach.py`.
5. **Generate HTML** — run `python3 -m myflames teach your_lesson -o docs/teach/your_lesson.html`.
6. **Update docs** — add the lesson to `docs/teach/index.html` and the README.

## Adding a new advisor rule

1. Add the rule function to `advisor.py`.
2. Every rule must include a `Why:` clause (enforced by tests).
3. Add the rule to the README's Environment Advisor table.
4. Add test cases in `test/test_myflames.py`.

## Generating test fixtures

Fixtures are captured from live MySQL/MariaDB Docker containers:

```bash
./scripts/generate-fixtures.sh           # MySQL 8.4
./scripts/generate-mariadb-fixtures.sh   # MariaDB 11.4
```

These scripts create the test schema, run various query patterns, capture EXPLAIN ANALYZE output, and save it to `test/fixtures/`.

## Regenerating demo files

```bash
# Full wipe + rebuild of docs/demos (keep a backup tree first — live-* demos restore from it)
cp -a docs/demos /tmp/demos_bak
rm -f docs/demos/*
python3 scripts/regenerate_docs_demos.py /tmp/demos_bak

# Regenerate all teach lessons (excludes hand-edited docs/teach/index.html)
for lesson in $(python3 -c "from myflames.teach import LESSONS; print(' '.join(sorted(LESSONS)))"); do
  python3 -m myflames teach "$lesson" -o "docs/teach/$lesson.html"
done
```

## Code style

- No auto-formatters are enforced, but follow the existing style.
- Use descriptive variable names. Comments only where the logic isn't self-evident.
- Every advisor rule, cost-model constant, and rendered HTML string that references a MySQL version must be asserted by a test.

## License

By contributing, you agree that your contributions will be licensed under the same [CDDL 1.0](docs/cddl1.txt) license as the project.
