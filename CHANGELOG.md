# Changelog

All notable changes to myflames are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] — 2026-06-30

### Fixed

- **Advisor no longer recommends a no-op MRR setting.** When `optimizer_switch`
  has `mrr=off`, MRR is globally disabled and `mrr_cost_based` has no effect, so
  the advisor now suggests `mrr=on` (which re-enables MRR; `mrr_cost_based`, on
  by default, still gates it per query) instead of the previous `mrr_cost_based=on`.
- **Visual Explain diagram no longer drops branches of a 3+-way join/UNION.**
  `build_diagram_steps` now emits every child of a multi-input node (e.g. a
  multi-branch `UNION ALL` Append), not just the first two.
- **`parse_explain` raises a clear `ValueError` for valid-but-non-object JSON**
  (a scalar or array) instead of leaking an opaque `TypeError`. This also fixes
  the MCP tools crashing on such input.
- **`check` rejects an unrecognized `--fail-on` trigger** (exit code 2) instead
  of silently matching nothing and exiting 0 — a misspelled trigger no longer
  turns a CI gate green.
- **`render` exits 2 (bad input), not 1, when a plan has no flame-graph data**
  (all-zero self-time), keeping exit code 1 reserved for a tripped finding.
- Corrected MariaDB BNLH advice (the lever is `join_cache_level >= 3`, since
  `join_cache_hashed` is on by default), the `rowid_filter` version (MariaDB
  10.4+, not 10.5+), the non-sargable-join wording (a functional index on the
  exact expression *can* be used), and the `const`/`system` access-type
  complexity (O(1), read once, not O(log n) per outer row).
- Tooltip attribute escaping is now consistent across all renderers (newlines
  encoded, `'` escaped); long tooltips are truncated before escaping so a
  multi-byte entity can't be clipped mid-token.

### Documentation

- Documented the `--no-teach-bundle` and `--refresh-teach-bundle` render flags.

## [2.1.0] — 2026-06-05

### Changed

- **Renamed the `tokens` subcommand to `digest`.** The command's product is the
  digest — the compact, source-grounded projection of a plan you hand an LLM —
  so the name now advertises that. Bare `myflames digest plan.json` emits the
  digest text (previously `tokens --digest`); the token + $ savings report moved
  to `myflames digest --cost` (previously the bare `tokens` default). `--show`
  became `--show-prompts`. The old `--exact` flag was removed in favor of
  `--tokenizer claude`. The internal module `myflames/tokens.py` is now
  `myflames/digest.py`.
- **Renamed the `findings` subcommand to `advise`** (a verb, consistent with
  `compare` / `check` / `digest`, and with the advisor). Flags are unchanged.
- Both old names remain as **deprecated aliases**: `tokens` and `findings` still
  run, print a one-line deprecation notice to stderr (never stdout), and
  preserve their previous behavior (`tokens` defaults to the savings report).
  They will be removed in a future release.

### Added

- **Exact GPT token counts, keyless** — `myflames digest --cost --tokenizer gpt`
  uses tiktoken (optional extra `pip install 'myflames[gpt]'`) to count tokens
  exactly for GPT models. No API key or network needed.

## [2.0.2] — 2026-06-05

### Added / Verified

- **MySQL 9.7 support, verified against a live server** (and the checked-out
  9.6 source). A broad battery (scan / pk / range / join / group-by / filesort /
  semijoin / antijoin / index_merge / derived / window) was captured on real
  MySQL 9.7 under **both** the traditional and **hypergraph** optimizer — 24/24
  parse, validate, and detect correctly. The 9.x `query_plan` envelope
  (`{query, query_plan, query_type, json_schema_version}`) and new access types
  (`temp_table_aggregate`, `rows_fetched_before_execution`) are handled; the
  hypergraph optimizer's FirstMatch semijoin is now picked up. Real 9.7
  fixtures + regression tests added (`test/mysql-9.7-*.json`). Fully backward
  compatible — every 8.4 / 10.11 / 11.4 fixture still passes.
- **MariaDB 11.8 support, verified against a live server** (11.8.8). Same
  battery captured via `ANALYZE FORMAT=JSON` — 11/11 parse, validate, and
  detect; real fixtures + tests added (`test/mariadb-11.8-*.json`).

### Docs

- Documented how to use `--exact` with **your own** `ANTHROPIC_API_KEY`
  (export the env var; example uses a clearly-fake placeholder key), and stated
  explicitly that myflames reads the key from the environment at call time and
  **never stores it** anywhere. README + walkthrough + `--exact` help text.

### Fixed

- **`myflames tokens --exact` now degrades gracefully when no key is
  configured.** The Anthropic SDK does not validate auth at construction (it
  only fails when a request is made), so a regular user who had installed the
  `[tokens]` extra but had no `ANTHROPIC_API_KEY` got an uncaught `TypeError`
  mid-run. `make_counter` now probes `count_tokens` (a free endpoint) up front
  and falls back to the offline heuristic with a clear stderr note
  (`--exact unavailable (no ANTHROPIC_API_KEY)`) instead of crashing.

## [2.0.1] — 2026-06-05

Advisor correctness pass. Every claim was verified against real MySQL 8.4
plans (with optimizer switches toggled on and off on a live server) and the
MySQL server source, rather than against documentation or assumption.

### Fixed

- **Main finding is ranked by impact, not a fixed category order.** A trivial
  50-row scan no longer headlines over a 16,000-row sort: among scan / sort /
  temp-table findings, the one that touches the most rows (rows × loops) wins
  (cost is ~proportional to rows processed). Join-strategy findings (BNL, hash
  join) still lead when present.
- **Trivial full scans are no longer flagged.** A scan touching fewer than
  ~100 rows total (rows × loops) cannot be helped by an index — for a narrow
  InnoDB table it is one or two pages, and the optimizer itself declines an
  index there — so "full table scan, add an index" was noise. Gating on
  rows × loops keeps a small inner table of a large join flagged.
- **Advisor no longer flags scans of derived tables / CTEs / `<temporary>`
  results as base-table full scans.** A "table scan" over a materialized
  intermediate (the `GROUP BY`/`DISTINCT` temp table, a derived table, or a
  CTE) is not a missing-index problem — you cannot index a result that only
  exists at query time. The structural signal is that such a scan node has a
  sub-plan (children); a real base-table scan is a leaf. The `full_scans`
  detector now excludes both angle-bracket pseudo-tables (`<temporary>`,
  `<derived N>`) and named derived/CTE aliases (`agg`, `prod_stats`, …).
- **`use_index_extensions` is no longer reported.** It was keyed on
  `details["covering"] is True`, but a covering-index read is not evidence the
  switch was used — verified against MySQL 8.4 by toggling the switch on/off
  (the plan, including the covering read, is identical either way; the flag is
  not surfaced in EXPLAIN at all). A covering read is now surfaced as a plain
  `covering index` feature instead, never as an optimizer_switch claim.
- **`index_merge` sort-union is now detected on MySQL 8.4.** Real v2 output is
  `access_type=index_merge`, operation `"Sort-deduplicate by row ID"`
  (mysql-server `explain_access_path.cc:1382`) — myflames matched neither and
  detected nothing. It now recognizes the sort-union operation text and a
  generic `index_merge` access type as a fallback. Real-capture fixtures added.

## [2.0.0] — 2026-06-05

Major release. A full `EXPLAIN ANALYZE FORMAT=JSON` plan is large and
repetitive, which makes it expensive to pass to an LLM and unusable from an
SVG. 2.0 adds a compact, source-grounded text digest of a plan and the tooling
around it: a token and cost comparison, agent and CI subcommands, and an MCP
server, while keeping the core package dependency-free (Python stdlib only).
The same analysis is produced once and rendered several ways: SVG for people,
the digest or JSON for tools, an exit code for CI.

### Added

- **`tokens` subcommand** — quantifies the saving from handing an LLM the
  myflames digest instead of the raw plan: side-by-side token counts and USD
  cost (Opus 4.8 / Sonnet 4.6 / Haiku 4.5 input pricing), `--digest` to emit
  the digest itself, `--show` to print both prompts, `--exact` for real Claude
  counts via Anthropic `count_tokens` (optional `myflames[tokens]` extra),
  `--json` for machine output. New module `myflames/tokens.py`
  (`build_digest`, `build_compare_digest`, `estimate_tokens`, `compare`).
- **`findings` subcommand** — ranked warnings + suggestions, each with a
  `confidence`, as text or `--json`. New module `myflames/findings.py`.
- **`check` subcommand** — CI gate: `--fail-on <categories|severities|any>`
  exits non-zero when the plan trips a trigger. Lives in `myflames/findings.py`.
- **`diff` alias** for `compare`, plus `--digest` (token-cheap text diff) and
  `--json` (structured `compare-1.0` delta) modes.
- **MCP server** (`myflames-mcp`, optional `myflames[mcp]` extra) exposing
  `analyze_plan`, `digest_plan`, `compare_plans`, `explain_optimizer_switch`,
  and `explain_query` so agents call myflames directly. New module
  `myflames/mcp_server.py`; tool logic is pure stdlib and unit-tested.
- **"Agent-ready" panel** in every HTML report showing the token/cost saving,
  plus a "Made with myflames" footer (carrying the Brendan Gregg / Tanel Poder
  inspiration credit).
- **`ROADMAP.md`** — contributor-facing vision, shipped/next, and non-goals.
- **Browser playground scaffold** ([docs/playground/](docs/playground/)) —
  client-side Pyodide page (pending a published wheel).
- **Live, reproducible token-savings walkthrough**
  ([docs/examples/](docs/examples/)) backed by a real MySQL 8.4 Docker script.
- Optional dependency groups: `myflames[mcp]`, `myflames[tokens]`.

### Changed

- **Exit-code contract unified across all subcommands:** `0` success, `1` a
  gate/finding tripped (`check`), `2` bad input (unreadable/missing file,
  unparseable JSON). The default render path previously exited `1` on a parse
  error — it now exits `2`. (Behavior change → major bump.)
- `tokens` and `findings` gained `-o/--output`; machine output now goes to
  stdout while diagnostics (including `check`'s status line) go to stderr, so
  `| jq` and redirects stay clean.

### Internal

- `myflames[tokens]` exact counting uses Anthropic `count_tokens`, never
  `tiktoken` (which mis-counts Claude tokens). The offline heuristic is the
  zero-dependency default and is labelled as an estimate everywhere it surfaces.

## [1.5.0] — 2026-04-25

Minor release focused on **correctness, identity, accessibility, and
authoring ergonomics**. Highlights: advisor rules verified against MySQL 8.4
source (multiple incorrect claims fixed), every plan node gets a stable
`node_id` referenced across JSON / SVG / warnings, every report ships with a
JSON-LD envelope + cross-link to its sibling sidecar, the HTML report's
"Collected environment" panel is a clickable accordion with human-readable
byte values, and lesson JavaScript now lives in real `.js` files with a
headless-browser regression harness that catches the kind of silent JS bugs
`node --check` misses.

Schema bump: sidecar **`1.2` → `1.3`** (added `$schema` URL, `plan_tree`
identity graph, `node_id` references, `environment_findings` digest). New
**`compare-1.0`** schema for the diff sidecar emitted alongside the
comparison report.

### Added

#### Advisor correctness (verified against MySQL / MariaDB source)

- **M1 — BNL / `hash_join` advice rewritten.** The pre-1.5 advice
  recommended `optimizer_switch='hash_join=on,block_nested_loop=off'`. That's
  wrong on MySQL 8.0.20+: `hash_join` is defined in `sql/sql_const.h:221` but
  never checked anywhere in the planner, and `block_nested_loop=off` actually
  *disables* the BNL→hash rewrite the executor performs at runtime
  (`sql/sql_executor.cc:~2891`). The new rule explains directly and
  recommends an index instead. MariaDB plans (detected by `join_cache_hashed`
  in `optimizer_switch`) get a separate, correct branch.
- **M2 — MRR rule gated** on a detected secondary-index range scan; never
  forces `mrr=on` (only suggests `mrr_cost_based=on` so the cost-based gate
  stays in charge — important on SSDs where MRR is often a regression).
- **M3 — BKA + BNL glossary corrected.** BKA is **off by default**
  (verified in `OPTIMIZER_SWITCH_DEFAULT` in `sql/sys_vars.cc:213`).
- **M4 — MariaDB `range-checked-for-each-record` normalization** added.
- **M5 — `innodb_flush_log_at_trx_commit` durability split.** `=2` (OS
  crash only) and `=0` (any crash including mysqld) now have distinct
  rule_ids, severities, and Why explanations.
- **Advisor rule tagging.** Every finding carries a `rule_id` + `severity`
  so external CI can gate on specific rules without scraping prose.

#### Identity + schema backbone

- **Stable `node_id`** on every parse-tree node. Derived from the canonical
  tuple `(operation, table_name, access_type, key, sibling_position)` walked
  from root, hashed to `n:` + 12 hex chars. Same fixture → same ids on every
  parse. Test: `test/test_slice1_contracts.py::TestNodeIdStability`.
- **`$schema` URL** in every sidecar pointing at the published JSON Schema
  (`docs/schemas/sidecar-v1.json`).
- **`plan_tree` identity graph** — compact tree of `{node_id, short_label,
  children}` so external consumers can resolve any `node_id` reference back
  to a position + label without parsing SVG.
- **`operator_complexities[].node_id`** added.

#### HTML report polish

- **Design tokens** in `:root` — `--font-{ui,mono}`, `--sp-{1..6}`
  (4/8/12/16/24/32), `--rad-{sm,,lg}`, `--dur-{sm,md,lg}`,
  `--ease-{out,in-out}`.
- **Accessibility floor.** Global `:focus-visible` ring, darkened `--muted`
  for WCAG AA, skip-link.
- **`prefers-reduced-motion: reduce`** guard collapses every animation
  duration to ~0 across HTML, the standalone complexity SVG, and the Tier-1
  lesson runtime.
- **JSON-LD envelope** — sidecars wrap as `{ "@context":
  "https://myflames.dev/ns/v1", "@type": "QueryPlanAnalysis", "@id": ... }`
  so search crawlers and LLM retrieval pipelines parse correctly.
- **`<link rel="alternate" type="application/json">`** in every HTML
  `<head>` pointing at the sibling sidecar.
- **Header metadata strip** (D5) — two-row sticky header with title +
  toolbar above engine / version / operator-count / total-time / generated-at.
- **Glossary chips become anchors.** `<abbr>` upgraded to `<a
  href="#gloss-{key}">` with `:target` highlight + a JS shim that
  auto-opens the collapsed `<details>`. Sibling **"Learn → "** button on
  chips whose key has a matching teach lesson.
- **Primary-action card always carries a `why`** — `_why_fallback()`
  derives a non-empty rationale from the suggestion's `category`.
- **Concrete `CREATE INDEX` DDL** in the primary-action card when the
  primary suggestion has `category == "index"`.
- **myteach hub.** New `myflames teach --index -o PATH` emits the
  centralized algorithm catalog HTML; every report has a "myteach" section
  below the glossary linking to it and listing the lessons relevant to
  *this* plan.
- **Collected environment panel rewrite.**
  Bytes humanized (`134217728` → `128 MB`, raw bytes in tooltip).
  `optimizer_switch` collapsed into a 27-flag chip list, color-coded
  `=on` / `=off`. Tables card replaces the previous Stats + Schema cards
  with a clickable per-table accordion: click the table name → expands
  columns + indexes inline with PK / UNIQUE / INDEX badges.

#### Visualization

- **V1 — categorical flame palette** keyed to the 12 canonical access_type
  families. Severity red stays reserved for advisor chrome (D4).
- **V3 — squarified treemap.** Bruls/Huijsen/van Wijk replaces the old
  slice-and-dice; cells stay near 1:1 aspect ratio.
- **V4 — unified search counter.** Bargraph and treemap report honest
  `Match N/M`, scroll-to-next via `n` / `N` / `Esc`.
- **V5 — shared `fit_label()`** with Unicode middle-ellipsis and CJK-aware
  width estimation. Migrated bargraph, treemap, diagram.
- **viewBox** now emitted by bargraph and treemap (CLAUDE.md SVG rule was
  previously unenforced).
- **Hash chart winner annotation.** When a complexity chart sets
  `lowerIsBetter: true`, the y-label gets `(lower = better ↓)` and the lower
  curve at the cursor gets a green ✓ + a "N× cheaper" pill.
- **Full-scan scanner is stroke-only** (was a 0.95-opacity overlay that
  silently covered the row text underneath).

#### teach lessons (Tier 0 + Tier 1)

- **Tier 0 — JS authoring ergonomics.** Every lesson's JavaScript was
  extracted from Python raw-string literals into a sibling `<lesson>.js`
  file loaded at render time via `_html.load_lesson_js(__file__)`. Editor
  syntax highlighting, eslint, prettier, and `node --check` all work now.
- **Tier 1 — Motion One + d3 helpers via committed bundle.** New
  `assets/` workspace with esbuild + TypeScript builds
  `myflames/assets/anim-runtime.js` (68 KB minified, 25 KB gzipped) which
  installs additive helpers on `window.anim`: `flip`, `spring`, `squarify`,
  `smoothPath`. Bundle is checked in — end users never touch npm.
- **`anim.arrival(el, opts)` primitive** — universal "this just landed"
  pulse used in every lesson's `onComplete` for visual consistency.
- **`lesson_stage()` scaffold** in `_html.py` that returns the four blobs
  `render_page` expects.
- **New lessons.** `teach/scan_family/covering_index.py`,
  `teach/cache_family/buffer_pool_warmup.py`. Both verified against the
  MySQL 8.4 source tree (`storage/innobase/dict/dict0dict.cc:3149` for the
  PK-append behavior, `ha_innodb.cc:22692` for the dump_pct default).
- **`nested_loop` flagship rewrite** — replaced state-swaps with tweens,
  added arc'd probe pills via `anim.path` with 80 ms stagger, added
  per-driver match verdicts ("Acme id=1 → 2 orders ✓").
- **LRU lerpColor flashes + staged verdict fade-ins** (A4) — act
  boundaries fade their conclusion in over 400 ms instead of appearing
  instantly.
- **`buffer_pool_warmup` + `covering_index` Play-button wiring** fixed —
  were binding to `#btn-play` directly instead of via `wireToolbar`.
- **`BASELINE_SPEED_SCALE`** retuned 0.52 → 0.42 (~20% slower).

### Fixed

- **Critical: `%%` in lesson JS was emitted as invalid JavaScript.** Six
  lessons had `var col = i %% colsPerRow` in their raw-string templates —
  a Python `.format()` escape mistakenly applied to lessons that never went
  through `%`-formatting. Result: `SyntaxError` at script-parse time that
  silently killed the whole lesson's animation in the browser.
  `node --check` revealed it immediately.
- **`tl.tween` was never a thing.** `skip_scan.js` called
  `tl.tween(stage.cursor, {x,y}, 300, ease)` — that method doesn't exist
  on the timeline API. Rewritten to `tl.call`-then-`tl.add({onUpdate})`.
  Caught by the new headless harness on its first run.
- **`<circle cy="NaN">` in `non_unique_lookup.js`.** Stage object missing
  its `idxY` field; `resetStage()` did `setAttribute("cy", undefined +
  220)` → NaN. Caught by the harness.

### Test infrastructure

- **`assets/verify-animations.mjs`** — headless Chromium harness that
  loads every lesson, captures every `console.error` / `pageerror`, clicks
  Play, and verifies the SVG byte-snapshot diverges after ~1.5 s of
  animation. Three pre-existing silent JS bugs found on first run.
- **`test/test_slice1_contracts.py`** — 7 contracts (advisor digest
  goldens, MariaDB normalization invariants, `node_id` stability,
  sidecar `$schema`, `plan_tree`, `operator_complexities[].node_id`).
- **`test/test_compare_sidecar.py`** — round-trip + classification +
  node-id validation for the new `compare-1.0` sidecar.
- **`test/test_labels.py`** — pure-function tests for `fit_label()`
  including CJK and surrogate-pair cases.
- **`test/test_svg_contract.py`** — `assert_svg_contract()` helper:
  asserts viewBox presence, no `calcMode="linear"` on SMIL, no unbounded
  SMIL without a class/id/begin handle.

### CI

- **`.github/workflows/fixtures-drift.yml`** — nightly cron regenerates
  fixtures from `mysql:8.4` and `mariadb:11.4` Docker images and fails on
  drift.
- **`.github/workflows/integration.yml`** — matrix integration tests
  against live MySQL 8.4 + MariaDB 11.4 service containers with
  `MYFLAMES_REQUIRE_LIVE=1` so connector tests fail rather than silently
  skip.

### Tests

`1379` (1.4.0) → **`1423`** green at 1.5.0 (44 new). Headless harness
reports **20 / 20 lessons passing**.

### Build dependencies

The Tier-1 bundle pulls these at build time only — they don't ship with
the wheel and end users don't run npm:

- **Motion One** (MIT) — animation runtime.
- **d3-hierarchy** (ISC) — squarified treemap layout.
- **d3-shape** (ISC) — Catmull-Rom curve generators.
- **esbuild** (MIT, dev) — bundler.
- **TypeScript** (Apache-2.0, dev) — runtime types.
- **Puppeteer** (Apache-2.0, dev) — headless browser harness.

All licenses verified by fetching upstream LICENSE files.


## [1.4.0] — 2026-04-23

Minor release adding **Big O complexity annotations to every operator in every
view**, a **JSON sidecar schema bump** (1.2), and a refreshed README with
inline screenshots. People who use myflames to learn MySQL internals can now
see `O(n · log m)`, `O(n · m)`, `O(log n + k)`, …  on each operator, backed
by a vetted decision table and a shared severity palette.

### Added

- **`myflames/complexity.py`** — single source of truth for per-operator Big O
  annotations. `compute_complexity(node, parent=None)` returns a dict with
  `big_o`, `short`, `severity` (`good` / `medium` / `bad`), `rationale`,
  `confidence` (`exact` / `typical` / `worst_case`), and an optional glossary
  `learn_more` key. Returns `None` on uncertainty so renderers can omit
  chips rather than display a misleading value.
- **`myflames/complexity_legend.py`** — shared SVG legend fragment
  (`render_complexity_legend_svg`) embedded at the bottom of the diagram,
  bargraph, and treemap views so a newcomer can decode the chips without
  leaving the page.
- **Big O surfaces in every renderer**:
  - **Flame graph**: colored severity dot at the right edge of every bar;
    compact `O(...)` appended to the bar label when there is ≥ 120 px of
    width available; tooltip gains a `Complexity: O(...)` line.
  - **Bar chart**: dedicated **COMPLEXITY** column between the label and the
    loops count (auto-hidden below 900 px canvas width), with color-coded
    chips matching the severity palette.
  - **Treemap**: `data-complexity="O(...)"` attribute on every tile, plus a
    corner chip on tiles larger than `80 × 40 px`.
  - **Diagram**: colored chip below each access box and below each join
    diamond. The chip lives on the join frame only — inner children of a
    join never duplicate the chip — so the `O(n · m)` / `O(n · log m)`
    story sits in the right place.
- **JSON sidecar schema 1.2** — new optional top-level
  `operator_complexities` array, one entry per operator node that carries
  complexity metadata. Each entry is keyed by `folded_label` and
  `short_label` (matching the existing `teach_hooks` convention) and
  contains the full complexity dict. The array is omitted entirely when no
  node has complexity metadata, keeping payloads minimal and avoiding any
  shape surprise for consumers pinned to 1.1.
- **`test/test_complexity.py`** — 41 unit tests, one per row of the decision
  table plus contract and palette tests. The decision table is the
  authoritative reference for what Big O class we claim for each operator.
- **README refresh** — hero diagram, an "output types" table with thumbnail
  previews of each view, and a "What does the output look like?" section
  highlighting the Big O chips. Screenshots live under `docs/screenshots/`.
- **docs/VISUAL_EXPLAIN_REFERENCE.md** — new section documenting the
  complexity dict shape, the per-renderer surface area, the severity
  palette, and the schema 1.2 sidecar contract.

### Changed

- **`myflames/parser.py`** — `parse_node` now attaches
  `details["complexity"]` once per node via `compute_complexity(node)`;
  downstream consumers read the same field.
- **`myflames/output_sidecar.py`** — `SCHEMA_VERSION` bumped to `"1.2"`;
  `validate_sidecar()` extended to validate `operator_complexities` entries
  (severity enum, confidence enum, required string fields, optional
  `build_complexity` / `scan_complexity` sub-dicts for Materialize).
- **`myflames/flamegraph.py`** — `folded_to_svg` accepts a new optional
  `complexity_by_folded` mapping; `enhance_tooltip_flame` (in
  `parser.py`) appends a `Complexity: ...` line to flamegraph tooltips.
- **`.github/workflows/test.yml`** — broadened the discovery pattern from
  `test_myflames.py` to `test_*.py` so the new `test_complexity.py` and
  `test_sidecar.py` suites run in CI.

### Notes on correctness

The complexity claims were vetted against the MySQL 8.4 and MariaDB 11.x
cost model via the `/mysql-expert` skill before landing:

- Nested loop with indexed inner (`ref` / `eq_ref` / `range`) is reported
  as `O(n · log m)` (exact). Block Nested Loop is `O(n · m)` (exact) and
  Batched Key Access gets its own distinct `O(n · log m)` entry so the
  pedagogical difference is preserved.
- Hash join is `O(n + m)` (exact). Disk-spill does not change the
  asymptotic class, only the constants — the rationale says so.
- Filesort defaults to `O(n log n)` at `worst_case` confidence; a future
  release will narrow to `O(n log k)` for priority-queue filesort once
  outer-LIMIT parsing lands.
- Skip scan is `O(d · log n)` at `typical` confidence with `d = distinct
  prefix values, not exposed by EXPLAIN` called out in the rationale.
- Materialize emits two-phase `build_complexity` + `scan_complexity`
  rather than a single `big_o` string — collapsing into one would mislead
  students about how `actual_loops` compounds with the build phase.

### Deferred

- Priority-queue filesort detection (needs outer LIMIT parsing, which
  `parse_explain` does not walk today).
- Per-node disk-spill rationale for hash join (needs `join_buffer_size`
  awareness).

## [1.3.1] — 2026-04-13

Feature and maintenance release adding **interactive teach lessons**,
**modernized Visual Explain diagrams**, and a thorough repo cleanup.

### Added

- **Interactive algorithm teach lessons (`myflames/teach/`)** — five
  self-contained, animated HTML lessons that visualize how MySQL internals
  work: **B-tree index lookup**, **Block Nested Loop (BNL)**, **Hash Join**,
  **Join algorithm comparison**, and **LRU buffer pool cache**.
  - YouTube-style scrubber for stepping through animation frames.
  - Interactive complexity charts showing algorithmic cost curves.
  - Pause, speed control, and real SQL examples baked into each lesson.
  - Rendered demos served via GitHub Pages at `docs/teach/`.
- **Modernized Visual Explain diagram** — hotspot layer with viridis
  heat-map palette, borders-only hotspot marks, and WCAG-compliant text
  contrast.

### Removed

- **4 legacy root-level Python scripts** (`mysql_explain.py`,
  `mysql_explain_bargraph.py`, `mysql_explain_flamegraph.py`,
  `stackcollapse_mysql_explain_json.py`) — thin wrappers superseded by
  the unified `myflames` CLI entry point since v1.0.
- **91 legacy Linux perf test fixtures** (`test/perf-*.txt`,
  `test/results/`) — inherited from the original FlameGraph fork,
  unused by the MySQL-focused test suite.
- **Dead code**: `render_analysis_panel()` from `parser.py` (replaced by
  `render_info_panel()`), plus 6 associated tests.
- **Empty `requirements.txt`** — `pyproject.toml` is the authoritative
  dependency spec.

### Changed

- **Extracted inline CSS/JS from `output_html_report.py`** into separate
  `output_html_report.css` (369 lines) and `output_html_report.js`
  (138 lines) files for proper syntax highlighting, linting, and cleaner
  diffs. Module reduced from 1261 to 758 lines.
- **Moved internal dev artifacts** out of `docs/`:
  - `docs/prompts/` → `.claude/prompts/`
  - `docs/teach/ALGORITHM_ROADMAP.md` → `.claude/ALGORITHM_ROADMAP.md`

## [1.3.0] — 2026-04-10

A large quality-of-life release focused on **three audiences at once**:
newcomers, senior DBAs, and AI agents. The main additions are a live-
connection mode, a machine-readable sidecar, progressive-disclosure HTML
reports, a non-sargable join detector, and a +236 test growth (997 → 1233).

### Added

- **Live-connection mode (`-h host -P port -u user -p -D db -e "SELECT ..."`)** —
  myflames can now connect directly to a MySQL 8.4 or MariaDB 10.11/11.4 server
  using the same flags as the `mysql` CLI, run `EXPLAIN ANALYZE FORMAT=JSON` (or
  MariaDB's `ANALYZE FORMAT=JSON`) for you, collect server state, and render the
  result in one invocation. Replaces the need to pipe `mysql -e '...' | myflames`
  for simple cases.
  - Uses the real `mysql` / `mariadb` client binary under the hood — **no PyMySQL
    or driver install**, stays stdlib-only, authenticates via whatever plugin the
    server uses (`mysql_native_password`, `caching_sha2_password`, Kerberos, etc.).
  - Passwords go into a mode-0600 `--defaults-extra-file` temp file — never
    visible in `ps`, process env, or logs. Temp file is deleted on exit.
  - Supports every SSL flag the `mysql` client supports:
    `--ssl-mode`, `--ssl-ca`, `--ssl-cert`, `--ssl-key`. Verified against RDS
    with `--ssl-mode=VERIFY_IDENTITY --ssl-ca=global-bundle.pem`.
  - `-p` with no value prompts; `-p'password'` inline works; `-p` alone works.
  - `--mysql-binary /path/to/mysql` overrides the binary autodetection when
    you want a specific client version.
- **Live collectors (on by default; individually toggleable)** — when live mode
  is active, myflames also captures:
  - `SHOW CREATE TABLE` for every table the query touches (`--no-collect-schema`
    to disable)
  - Row counts / data length / index length from `information_schema.tables`
    (`--no-collect-stats` to disable)
  - A curated subset of `SHOW SESSION VARIABLES` the advisor inspects — buffer
    pool, sort/join/tmp buffers, `optimizer_switch`, durability settings
    (`--no-collect-variables` to disable)
- **Environment advisor (`myflames/advisor.py`)** — eight rules that combine the
  parsed plan with the collected environment data and emit concrete, copy-paste-able
  tuning suggestions with a `Why:` clause grounded in the MySQL cost model:
  - `innodb_buffer_pool_size` vs the working set of tables touched
  - `sort_buffer_size` vs filesorts (in-memory vs disk-merge trade-off)
  - `join_buffer_size` vs hash joins / BNL (separate explanations for each)
  - `tmp_table_size` + `max_heap_table_size` vs materialized temp tables
    (both must be raised together — MySQL always picks the smaller)
  - `optimizer_switch` overrides (`hash_join=off`, `mrr=off`,
    `derived_condition_pushdown=off`)
  - Missing indexes — cross-checks plan heuristics against the collected schema
    so we only flag indexes that **actually don't exist**
  - MyISAM / non-InnoDB engine detection
  - `innodb_flush_log_at_trx_commit` on UPDATE/DELETE/INSERT queries
- **Non-sargable join predicate detection** — a new advisor rule walks every
  join condition looking for function calls on column references
  (`CONCAT(col)`, `CAST(col AS ...)`, `LOWER(col)`, `DATE(col)`,
  `CONVERT(col USING ...)`, 30+ functions total) and flags them as the
  **primary action** because no index or buffer tweak can help until the
  predicate is rewritten. Emits severity `error`, category `nonsargable_join`.
  Cross-engine: works on MySQL hash joins (via `hash_condition`) and MariaDB
  BNL joins (via `block-nl-join.attached_condition`, now preserved through
  normalization).
- **JSON sidecar (v1 schema)** — every `--output` invocation now writes a
  `<base>.json` next to the SVG/HTML with a stable, versioned, schema-validated
  structured summary of the analysis. Consumable by AI agents, CI dashboards,
  or `jq` without parsing SVG or HTML. Key fields:
  `schema_version`, `generated_at`, `myflames_version`, `source.{type,engine,
  engine_version}`, `plan_summary.{total_time_ms,rows_sent,rows_examined_estimate,
  operator_count,max_depth}`, `optimizer_switches[]`, `warnings[]` (severity +
  category + source + node_labels), `suggestions[]` (severity + category +
  action + why + target_variable), `executive_summary`, `primary_action`,
  `collected.{variables,stats,schema}`. Fail-fast validation enforces enum
  discipline and required keys at write time.
- **Progressive-UX HTML report template** — the `--output foo.html` pipeline
  emits a redesigned self-contained report with:
  - **Executive summary strip** + **"Fix first" primary action card** above
    the fold (newcomer-friendly)
  - **Glossary chips** (`<abbr>` tooltips with zero JS) on every jargon term:
    filesort, hash join, BNL, materialize, ICP, MRR, sargable, etc.
  - **Copy-paste affordance**: every SET / CREATE INDEX / ALTER TABLE
    suggestion lives in a selectable `<pre><code>` block — never locked
    inside the SVG
  - **Collapsible `<details>` sections** for warnings, Why clauses, collected
    environment, and raw sidecar JSON (dense but navigable for DBAs)
  - **JSON-LD `<script>`** in `<head>` with the full sidecar payload — AI
    agents can answer "what's wrong with this query?" from the HTML alone
    without parsing SVG text nodes
  - **Semantic HTML landmarks** (`role="banner"`, `role="main"`, `<nav>`,
    `<aside>`) + ARIA labels so screen readers can navigate sections
  - **Responsive layout** — scales from 720px mobile to 1400px desktop
- **Responsive SVG output** — the CLI now post-processes every SVG it writes
  to inject `style="max-width: 100%; height: auto;"` and backfill a `viewBox`
  when one is missing. Standalone `.svg` files opened directly in a browser
  now scale to the viewport instead of overflowing at the fixed 1800px
  flamegraph width.
- **Glossary module (`myflames/glossary.py`)** — 30+ canonical EXPLAIN /
  optimizer terms with three tiers of explanation each:
  - `short`: ≤90-char tooltip text
  - `technical`: senior-DBA grade with cost-model citations
  - `newcomer`: plain English, no jargon, no analogies that distort
  Also exposes `find_terms_in_text()` for HTML glossary-chip wrapping and
  `generate_executive_summary()` for deterministic one-line plan summaries.
- **Complex-query demos** — 3 new example topics (15 files each):
  - **CTE + window functions** (`ROW_NUMBER() OVER (PARTITION BY ...)` top-N per group)
  - **Correlated subquery** (N+1 anti-pattern)
  - **Recursive CTE** (org-chart traversal)
- **105 new unit + integration tests** (parser, sidecar, glossary, advisor,
  collectors, connector, non-sargable, responsive SVG). Integration tests
  spin up live MySQL 8.4 + MariaDB 11.4 containers, create both
  `mysql_native_password` and `caching_sha2_password` users, and drive the
  whole pipeline end-to-end; they skip automatically when Docker is absent.
- **Three new specialized skills** (`.claude/skills/`):
  `mysql-expert` (domain correctness), `progressive-ux` (approachability),
  `structured-output` (machine consumption). Used to divide work cleanly
  when adding new rules, new output fields, or new glossary entries.

### Changed

- **`-h` is now `--host`, not `--help`** — to match the `mysql` CLI. Use
  `--help` if you want help. (`--version` still works.)
- **HTML report template rebuilt** — the old `output_html_report.py` layout
  has been replaced with the progressive-ux template described above. Every
  existing test assertion (`<!DOCTYPE html>`, `<svg`, `Export SVG`,
  `Export Analysis JSON`, `Print`, `Warnings`, `Suggestions`) still passes.
- **Advisor suggestions always include `Why:` explanations** — the contract
  is now enforced by a dedicated test
  (`test_every_suggestion_explains_why`) so future rules cannot ship
  without a cost-model justification. Rewrote every existing rule to
  explain the trade-off (in-memory sort vs tmpdir merge, hash-join probe
  re-reads on spill, MEMORY → InnoDB temp-table conversion cost, etc.).
- **`myflames/parser.py`**: `analyze_plan()` now also returns
  `nonsargable_joins`, `optimizer_switches`, and `node_highlights` entries
  for non-sargable predicates. `_detect_optimizer_switches()` promoted to
  a standalone function with a stable dict shape.
- **Docs**: `docs/index.html` reorganized with new sections for Complex
  queries, Optimizer switches, and Live mode — the 20 new demos are all
  linked. Every legacy demo was regenerated through the new template so
  the whole set is consistent.

### Removed

- Stale demo files with no matching fixture or broken naming
  (`mysql-query-analysis-derived-sort.*`, bare `mysql-query-analysis-*.svg`
  without a renderer suffix). The top-level orphan `demos/` directory was
  also removed; all demos live in `docs/demos/` as they should.

### Fixed

- **MariaDB BNL join condition lost during normalization** — the
  `block-nl-join.attached_condition` field was being dropped when the
  inner table was normalized, hiding non-sargable predicates on BNL
  plans. Now preserved on the normalized node so the advisor sees it.
- **Standalone SVG viewing overflow** — `width="1800"` SVGs no longer
  require horizontal scrolling when opened directly in a browser.

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
