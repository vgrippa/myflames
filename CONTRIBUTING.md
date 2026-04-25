# Contributing to myflames

Thank you for your interest in contributing to myflames! This guide will help you get set up and make your first contribution.

## Development setup

```bash
git clone https://github.com/vgrippa/myflames.git
cd myflames
python3 -m pip install -e .   # editable install
```

The Python package itself has zero external dependencies — Python 3.7+ stdlib only. Two **build-time / dev-time** toolchains live in `assets/`:

- **The Tier-1 lesson runtime bundle** ([assets/src/runtime.ts](assets/src/runtime.ts) → committed bundle at [myflames/assets/anim-runtime.js](myflames/assets/anim-runtime.js)). Only needed if you edit the TypeScript source — see [Rebuilding the Tier-1 animation bundle](#rebuilding-the-tier-1-animation-bundle).
- **The headless animation harness** (Puppeteer). Only needed if you edit a `<lesson>.js` file — see [Validating lesson animations](#validating-lesson-animations).

End users (anyone who only runs `pip install myflames`) never touch these.

## Running tests

```bash
# Full Python suite (1423 tests as of 1.5.0)
./run-tests.sh

# Specific modules
python3 -m unittest discover -s test -p "test_advisor.py" -v
python3 -m unittest discover -s test -p "test_slice1_contracts.py" -v
python3 -m unittest discover -s test -p "test_compare_sidecar.py" -v
python3 -m unittest discover -s test -p "test_teach.py" -v
```

Every new feature or logic change **must** include tests. The Python suite is the primary correctness gate. **For lesson `.js` changes**, also run the headless animation harness — Python tests cannot catch runtime bugs in the embedded JavaScript.

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
  output_html_report.py # HTML wrapper with progressive-disclosure UI
  output_sidecar.py    # JSON sidecar generator (schema v1.3)
  output_compare_sidecar.py  # Compare-1.0 diff sidecar
  advisor.py           # Environment advisor (rule_id-tagged findings)
  glossary.py          # Glossary entries (short/technical/newcomer)
  complexity.py        # Per-operator Big O classifier
  complexity_animation.py    # Big O log-log SVG chart
  assets/              # Tier-1 bundle output (committed, end users never rebuild)
    anim-runtime.js    # Motion One + d3-hierarchy + d3-shape (~68 KB minified)
  teach/               # Interactive algorithm lessons
    __init__.py        # Lesson registry + curriculum + render_catalog_html()
    _anim.py           # Hand-rolled tween/timeline/pulse/arrival JS runtime
    _html.py           # Shared HTML chrome + load_lesson_js() helper
    _cost_model.py     # Cost-model functions (MySQL 8.4 / MariaDB 11.4 defaults)
    <lesson>.py        # Each lesson: a thin Python wrapper that loads…
    <lesson>.js        # …its sibling .js file at render time (Tier 0)
    join_family/       # nested_loop, bnl, hash, join_compare, bka_join,
                       # semijoin_weedout
    index_family/      # btree, non_unique_lookup, unique_lookup, icp,
                       # index_merge, skip_scan, rowid_filter
    scan_family/       # full_scan, filter, filesort, tmp, derived_table,
                       # covering_index
    cache_family/      # lru, buffer_pool_warmup
assets/                # Build-time TypeScript workspace (not shipped)
  src/runtime.ts       # Tier-1 source — Motion One/d3 wrapper
  build.mjs            # esbuild bundler script
  verify-animations.mjs  # Puppeteer-based headless lesson harness
  package.json         # build/dev deps (esbuild, typescript, puppeteer)
test/
  test_myflames.py     # Parser, renderer, HTML, sidecar tests
  test_advisor.py      # Per-rule positive + negative tests
  test_slice1_contracts.py    # Advisor digest goldens, MariaDB invariants,
                       # node_id stability
  test_compare_sidecar.py     # compare-1.0 round-trip + classification
  test_labels.py       # fit_label() pure-function tests
  test_svg_contract.py # assert_svg_contract() helper + renderer self-tests
  test_teach.py        # Cost-model + lesson HTML content tests
  fixtures/            # 140+ EXPLAIN JSON fixtures (MySQL + MariaDB)
docs/
  index.html           # GitHub Pages landing
  schemas/sidecar-v1.json    # Published JSON Schema for the sidecar
  schemas/compare-v1.json    # Published JSON Schema for the diff sidecar
  guide/               # Documentation pages
  teach/               # Pre-generated lesson HTML + index.html (catalog hub)
  demos/               # Pre-generated query plan demo files
  screenshots/         # Inline images linked from README
scripts/
  generate-fixtures.sh          # MySQL 8.4 fixture generation (Docker)
  generate-mariadb-fixtures.sh  # MariaDB 11.4 fixture generation (Docker)
  regenerate_docs_demos.py      # Rebuild docs/demos from sidecars + live restore
.github/workflows/
  test.yml             # Python matrix unit tests (every PR)
  fixtures-drift.yml   # Nightly: regen fixtures, fail on drift vs pinned servers
  integration.yml      # PR: live MySQL/MariaDB connector tests via service containers
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

## Editing lesson JavaScript

Each `myflames/teach/<family>/<lesson>.py` has a sibling `<lesson>.js` file holding the lesson's animation code. Python loads it at render time via `_html.load_lesson_js(__file__)` — the JS file is **the** source. Edit it directly with full editor support (syntax highlighting, eslint, prettier, JSDoc types).

A handful of lessons (`hash`, `tmp`, `filesort`, `bka_join`, `bnl`, `btree`, `unique_lookup`) substitute Python constants into the JS via `_LESSON_JS_TEMPLATE % VALUE`. For those, double-percent (`%%`) in the `.js` is intentional — the `%`-substitution still runs after the file is loaded. Every other lesson uses a plain `%`. If unsure, run `node --check` after editing.

## Validating lesson animations

`node --check` only catches parse errors. It misses runtime issues like Motion One's CSS transform not composing with SVG's transform attribute on a `<g>` element — exactly the bug that took out an early `nested_loop` migration. Use the headless harness whenever you touch a lesson's `.js`:

```bash
cd assets
node verify-animations.mjs               # all 20 lessons
node verify-animations.mjs <lesson>      # one lesson
node verify-animations.mjs --headed      # show the browser window
```

The harness loads each lesson in real Chromium, captures every `console.error` / `pageerror`, clicks Play, and verifies the SVG actually moves over ~1.5 s. Three pre-existing silent JS bugs were caught on its first run; treat a non-zero exit as a release blocker.

## Rebuilding the Tier-1 animation bundle

`myflames/assets/anim-runtime.js` is a committed bundle (~68 KB minified) of Motion One + d3-hierarchy + d3-shape wrapped into additive helpers on `window.anim` (`flip`, `spring`, `squarify`, `smoothPath`). Only rebuild when you edit `assets/src/runtime.ts`:

```bash
cd assets
npm install          # first time only, or when package.json changes
npm run build        # emits ../myflames/assets/anim-runtime.js
npm run build:watch  # auto-rebuild during development
npm run typecheck    # tsc --noEmit (strict)
```

Runtime deps: Motion One (MIT), d3-hierarchy (ISC), d3-shape (ISC). Dev-only: esbuild (MIT), TypeScript (Apache-2.0), Puppeteer (Apache-2.0). All licenses verified by fetching upstream LICENSE files at the time each was added.

## Verifying MySQL / MariaDB internals claims

Any new advisor rule, glossary entry, or teach lesson that quotes a specific server default, configuration name, file path, or function name **must** be verified against the upstream source tree before merge — the project has hit incorrect claims sourced from priors three times in the past, and every time it was caught by a reviewer who actually opened the source.

Required workflow:

```bash
# Clone or pull the relevant tree
git clone --depth 1 https://github.com/mysql/mysql-server          # or
git clone --depth 1 https://github.com/MariaDB/server              # MariaDB

# Examples — what to grep for
grep -rn "OPTIMIZER_SWITCH_DEFAULT" sql/sys_vars.cc                # MySQL switches
grep -rn "innodb_buffer_pool_dump_pct" storage/innobase/           # InnoDB tunables
grep -rn "ET_USING_INDEX\b" sql/opt_explain.cc                     # EXPLAIN strings
grep -rn "dict_index_build_internal_non_clust" storage/innobase/dict/  # InnoDB dict
```

Cite the file:line(s) in the commit message **and** in the lesson / advisor rule body. The CHANGELOG entry for 1.5.0 has 6+ such citations — follow that pattern.

## Code style

- No auto-formatters are enforced, but follow the existing style.
- Use descriptive variable names. Comments only where the logic isn't self-evident.
- Every advisor rule, cost-model constant, and rendered HTML string that references a MySQL version must be asserted by a test.

## License

By contributing, you agree that your contributions will be licensed under the same [CDDL 1.0](docs/cddl1.txt) license as the project.
