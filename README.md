<p align="center">
  <img src="myflames.jpeg" alt="myflames logo" width="200">
</p>

<h1 align="center">myflames</h1>
<p align="center"><strong>MySQL & MariaDB Query Plan Visualizer</strong></p>

<p align="center">
Visualize MySQL <code>EXPLAIN ANALYZE FORMAT=JSON</code> and MariaDB <code>ANALYZE FORMAT=JSON</code> output as interactive SVG charts. Five views, one parser, zero external dependencies.
</p>

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/).

<p align="center">
  <img src="docs/screenshots/hero-diagram.svg" alt="myflames diagram view with Big O complexity chips on every operator" width="920">
  <br><em>Every operator now carries a Big O chip: <code>O(log n + k)</code>, <code>O(n · log m)</code>, <code>O(n · m)</code>, …  with a color-coded severity ramp.</em>
</p>

---

## What does the output look like?

Four views of the same query, each annotated with Big O complexity:

<table>
<tr>
<td align="center"><strong>Flame graph</strong> — time hierarchy + severity dots<br><img src="docs/screenshots/complex-flamegraph.svg" alt="flame graph with severity dots" width="420"></td>
<td align="center"><strong>Bar chart</strong> — slowest ops with a complexity column<br><img src="docs/screenshots/complex-bargraph.svg" alt="bar chart with complexity column" width="420"></td>
</tr>
<tr>
<td align="center"><strong>Treemap</strong> — corner chips on larger tiles<br><img src="docs/screenshots/complex-treemap.svg" alt="treemap with complexity chips" width="420"></td>
<td align="center"><strong>Diagram</strong> — Visual Explain style, Big O per node<br><img src="docs/screenshots/complex-diagram.svg" alt="visual explain diagram with complexity chips" width="420"></td>
</tr>
</table>

> **New in 1.4.0** — every operator carries a vetted Big O complexity chip (see the [shared complexity legend](myflames/complexity_legend.py) that renders at the bottom of every view). Open any HTML demo below to hover and inspect: `O(log n + k)` for index lookups, `O(n log n)` for filesort, `O(n · m)` when a nested loop has no inner index, and so on.

---

## Install

```bash
pip install myflames          # or: pipx install myflames  (Homebrew / PEP 668)
```

Pure Python 3.7+ stdlib. No external dependencies.

## Try it in 30 seconds

```bash
myflames sample.json > query.svg                  # SVG flame graph
myflames --output report.html sample.json         # self-contained HTML report
```

Or connect straight to a live MySQL / MariaDB server — same flags as the `mysql` CLI:

```bash
myflames -h db.example.com -u admin -p -D mydb \
  -e 'SELECT * FROM orders WHERE user_id = 1' \
  --output report.html
# → report.html  — progressive-UX HTML with advisor warnings
# → report.json  — v1 schema sidecar for AI agents / CI / jq
```

---

## Output types

| Preview | Type | Best for | Command |
|---|------|----------|---------|
| <img src="docs/screenshots/complex-flamegraph.svg" alt="flame graph preview" width="160"> | **Flame graph** | Full execution hierarchy, time distribution | `myflames explain.json` |
| <img src="docs/screenshots/complex-bargraph.svg" alt="bar chart preview" width="160"> | **Bar chart** | Finding the slowest individual operations | `myflames --type bargraph explain.json` |
| <img src="docs/screenshots/complex-treemap.svg" alt="treemap preview" width="160"> | **Treemap** | Comparing relative cost at a glance | `myflames --type treemap explain.json` |
| <img src="docs/screenshots/complex-diagram.svg" alt="diagram preview" width="160"> | **Diagram** | Join order & access paths (Visual Explain style) | `myflames --type diagram explain.json` |
| — | **Execution tree** | Collapsible per-subtree with self/total time | `myflames --type tree explain.json` |

Not sure which view? Run `myflames guide`.

Every view includes a **Query Analysis panel** with optimizer features detected, warnings (full table scans, hash joins, BNL buffers, temp tables, filesorts) and concrete tuning suggestions.

---

## Live demos

| View | Interactive demo |
|------|-----------------|
| Flame graph | [mysql-query-complex-flamegraph.html](https://vgrippa.github.io/myflames/demos/mysql-complex/mysql-query-complex-flamegraph.html) |
| Bar chart | [mysql-query-complex-bargraph.html](https://vgrippa.github.io/myflames/demos/mysql-complex/mysql-query-complex-bargraph.html) |
| Treemap | [mysql-query-complex-treemap.html](https://vgrippa.github.io/myflames/demos/mysql-complex/mysql-query-complex-treemap.html) |
| Diagram | [mysql-query-complex-diagram.html](https://vgrippa.github.io/myflames/demos/mysql-complex/mysql-query-complex-diagram.html) |
| Execution tree | [mysql-query-complex-tree.html](https://vgrippa.github.io/myflames/demos/mysql-complex/mysql-query-complex-tree.html) |
| HTML report | [mysql-query-report.html](https://vgrippa.github.io/myflames/demos/mysql-basic/mysql-query-report.html) |
| Before vs After | [mysql-query-compare.html](https://vgrippa.github.io/myflames/demos/mysql-basic/mysql-query-compare.html) |

[All demos →](https://vgrippa.github.io/myflames/)

> Interactive features (zoom, search, tooltips) need the HTML wrapper or GitHub Pages — raw GitHub URLs block inline scripts.

---

## Learn the algorithms — `myflames teach`

Interactive, offline-first HTML lessons that animate MySQL 8.4 and MariaDB 11.x internals with correct cost models. Every lesson ships with in-page sliders — no CLI flags, no re-running:

```bash
myflames teach btree -o btree.html && open btree.html
```

**19 lessons** in four families:

### Join family

| Lesson | What you learn |
|--------|---------------|
| [`teach nested_loop`](https://vgrippa.github.io/myflames/teach/join/nested_loop.html) | Nested Loop Join — the outer-driver/inner-probe loop shape from EXPLAIN. |
| [`teach bnl`](https://vgrippa.github.io/myflames/teach/join/bnl.html) | Block Nested Loop join (MariaDB 11.x default). Warning banner: MySQL removed BNL in 8.0.20. |
| [`teach hash`](https://vgrippa.github.io/myflames/teach/join/hash.html) | MySQL 8.4 hash join — build phase, probe phase, and grace-hash spill when the build side overflows `join_buffer_size`. |
| [`teach join`](https://vgrippa.github.io/myflames/teach/join/join.html) | BNL vs hash join side-by-side with shared sliders. See the asymptotic difference at scale. |
| [`teach bka_join`](https://vgrippa.github.io/myflames/teach/join/bka_join.html) | Batched Key Access join — batch outer keys, sort by rowid, and sweep the inner index sequentially via Multi-Range Read. |
| [`teach semijoin_weedout`](https://vgrippa.github.io/myflames/teach/join/semijoin_weedout.html) | Semijoin Duplicate Weedout — IN/EXISTS rewritten as inner join; a temp table keyed on outer rowid removes duplicates. |

### Index family

| Lesson | What you learn |
|--------|---------------|
| [`teach btree`](https://vgrippa.github.io/myflames/teach/index/btree.html) | InnoDB B+tree lookup — clustered PK, covering vs non-covering secondary, 16 KiB page fan-out. Move the row-count slider from 10 to 1 billion and watch the tree height update. |
| [`teach unique_lookup`](https://vgrippa.github.io/myflames/teach/index/unique_lookup.html) | Unique Key Lookup — exact-key lookup path and covering vs non-covering single-row access. |
| [`teach non_unique_lookup`](https://vgrippa.github.io/myflames/teach/index/non_unique_lookup.html) | Non-Unique Key Lookup — explains “Index lookup” / “Index range scan” and why non-covering lookups fetch base rows by row-id. |
| [`teach icp`](https://vgrippa.github.io/myflames/teach/index/icp.html) | Index Condition Pushdown — see how ICP checks trailing index columns before fetching the row, saving unnecessary clustered-index lookups. |
| [`teach index_merge`](https://vgrippa.github.io/myflames/teach/index/index_merge.html) | Index Merge — two separate indexes scanned and combined via union, intersection, or sort-union instead of a full table scan. |
| [`teach skip_scan`](https://vgrippa.github.io/myflames/teach/index/skip_scan.html) | Skip Scan — low-NDV leading column lets MySQL do N small range scans instead of a full table scan. |
| [`teach rowid_filter`](https://vgrippa.github.io/myflames/teach/index/rowid_filter.html) | Rowid Filter (MariaDB) — bitmap pre-filter before table access; scans a filtering index to build a rowid bitmap, skipping table fetches for non-matching rows. |

### Scan / sort / temp family

| Lesson | What you learn |
|--------|---------------|
| [`teach full_scan`](https://vgrippa.github.io/myflames/teach/scan/full_scan.html) | Full table scan — what it means when MySQL reads every row, then filters. Compare O(n) scan work against indexed access O(log n + k). |
| [`teach filter`](https://vgrippa.github.io/myflames/teach/scan/filter.html) | Filter operator — row-by-row predicate evaluation and why filter cost scales with incoming rows. |
| [`teach filesort`](https://vgrippa.github.io/myflames/teach/scan/filesort.html) | How MySQL sorts without an index: `sort_buffer_size` fills, sorted runs spill to tmpdir, k-way merge. Bigger buffer = fewer runs = less I/O. |
| [`teach tmp`](https://vgrippa.github.io/myflames/teach/scan/tmp.html) | Temporary tables — watch GROUP BY fill a MEMORY temp table, hit the limit, and convert to on-disk InnoDB. That cliff is why your query suddenly slows down. |
| [`teach derived_table`](https://vgrippa.github.io/myflames/teach/scan/derived_table.html) | Derived Table Materialization — FROM-clause subquery materialized into temp table, auto-indexed, then probed. |

### Cache family

| Lesson | What you learn |
|--------|---------------|
| [`teach lru`](https://vgrippa.github.io/myflames/teach/cache/lru.html) | InnoDB's midpoint-insertion LRU — why MySQL's buffer pool survives full-scan pollution while a textbook LRU gets wiped. |

Each lesson is a single self-contained HTML file: no external scripts, no external stylesheets, no external fonts. Drop one in a Slack DM or attach to a ticket and it just works. Hosted separately from the query-plan demos at [vgrippa.github.io/myflames/teach/](https://vgrippa.github.io/myflames/teach/).

---

## Requirements

- **Python 3.7+** (no extra packages)
- **MySQL 8.4+** with `SET explain_json_format_version = 2`, **or**
- **MariaDB 10.11+** / **11.4+** (supports `ANALYZE FORMAT=JSON` and `SHOW ANALYZE FORMAT=JSON FOR <conn_id>` out of the box)

---

## Quick start (file mode)

```sql
-- MySQL
EXPLAIN ANALYZE FORMAT=JSON SELECT ... ;
-- MariaDB
ANALYZE FORMAT=JSON SELECT ... ;
```

```bash
# Save to a file and render
mysql -u user -p mydb -s -N -r -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." > explain.json
myflames explain.json > query.svg

# Or pipe directly
mysql -u user -p mydb -N -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." | myflames > query.svg

# Self-contained HTML report
myflames --output report.html explain.json
```

myflames auto-strips MySQL CLI quirks (table borders, `EXPLAIN` headers, escaped newlines, BOM), so plain `-e` also works.

---

## Live-connection mode

Skip the two-step workflow — connect directly. Same flags for MySQL 8.4 and MariaDB:

```bash
# Local
myflames -h 127.0.0.1 -u root -p'password' -D mydb -e 'SELECT ...' --output report.html

# AWS RDS with full TLS verification
myflames -h my-db.rds.amazonaws.com -u admin -p \
  --ssl-mode=VERIFY_IDENTITY --ssl-ca=/path/to/global-bundle.pem \
  -D prod -e 'SELECT ...' --output report.html
```

In live mode myflames (1) connects through the real `mysql` / `mariadb` client binary (every auth plugin the server supports — no PyMySQL), (2) runs `EXPLAIN ANALYZE FORMAT=JSON`, (3) collects `SHOW CREATE TABLE`, row/byte counts, and a filtered `SHOW SESSION VARIABLES` snapshot, (4) feeds everything through the **environment advisor**, and (5) emits the HTML report + JSON sidecar.

**Password handling:** the password is written to a mode-0600 `--defaults-extra-file` and never appears on argv or in env vars. Skip any collection step with `--no-collect-schema`, `--no-collect-stats`, `--no-collect-variables`.

---

## HTML report

```bash
myflames --output report.html explain.json
myflames --type diagram --output report.html explain.json
```

A self-contained file you can attach to a ticket or paste into Confluence. Built for three audiences at once:

- **Newcomers** — plain-English executive summary, a single "Fix first" primary action card above the fold, glossary chips on every jargon term (`filesort`, `hash join`, `BNL`, `MRR`, `ICP`, …).
- **Senior DBAs** — every metric, warning and `SET` / `CREATE INDEX` / `ALTER TABLE` recommendation in copy-paste-able `<pre><code>` blocks.
- **AI agents / tools** — a `<script type="application/ld+json">` block in `<head>` with the full v1 sidecar payload, plus a `<base>.json` file written alongside. No SVG OCR needed.

---

## JSON sidecar

Every `--output` writes a **stable, versioned, machine-readable sidecar** next to the main file:

```bash
myflames --output report.html explain.json
# → report.html  report.json
```

```jsonc
{
  "schema_version": "1.0",
  "source": {"type": "live", "engine": "mysql", "engine_version": "8.4.8"},
  "plan_summary": { "total_time_ms": 12.4, "operator_count": 12, ... },
  "warnings":    [ {"severity": "error", "category": "nonsargable_join", ...} ],
  "suggestions": [ {"severity": "high", "category": "rewrite", "action": "...", "why": "..."} ],
  "primary_action": {"ref": "suggestions[0]"},
  "collected": { "variables": {...}, "stats": {...}, "schema": {...} }
}
```

Read it with `jq` — no HTML parsing needed:

```bash
jq '.suggestions[0] | .action + " — Why: " + .why' report.json
jq '.warnings[] | select(.category == "env")'      report.json
```

Suppress with `--no-sidecar`, or point at an explicit path with `--sidecar /tmp/plan.json`. See [myflames/output_sidecar.py](myflames/output_sidecar.py) for the full schema.

---

## Environment advisor

With access to server state (live mode, or any caller populating `analysis`), myflames runs rules matching plan signals against collected server state and emits tuning suggestions grounded in the MySQL cost model:

| Rule | Fires when… |
|------|-------------|
| **Non-sargable join predicate** | Join uses `CONCAT(col)`, `CAST(col)`, `LOWER(col)`, `DATE(col)`, … on a column |
| **Buffer pool vs working set** | `innodb_buffer_pool_size` < 25–50% of referenced tables' data+index length |
| **Sort buffer vs filesort** | Filesort detected and `sort_buffer_size` < 2 MB |
| **Join buffer vs hash-join / BNL** | Hash join or BNL detected and `join_buffer_size` < 2 MB |
| **Tmp table size** | Temp table materialized and `min(tmp_table_size, max_heap_table_size)` < 32 MB |
| **`optimizer_switch` overrides** | `hash_join=off` + BNL, `mrr=off` + filesort, `derived_condition_pushdown=off` + materialize |
| **Missing indexes** | Parser heuristic flags a missing index AND collected schema confirms no covering index |
| **Engine ≠ InnoDB/Aria** | Referenced table is MyISAM/other |
| **`innodb_flush_log_at_trx_commit` ≠ 1** | On a mutating query |

Every suggestion carries a `Why:` clause — enforced by a test so no rule ships without a cost-model justification.

---

## Compare before/after

```bash
myflames compare before.json after.json --output diff.html
```

Shows total time delta, per-operator self-time/rows/loops changes, new or removed full table scans, and new/resolved warnings.

---

## CLI reference

```
myflames [options] [explain.json]
myflames -h HOST [-P PORT] -u USER [-p[PASS]] -D DB -e 'SQL' -o OUT
```

### Rendering

| Option | Default | Description |
|--------|---------|-------------|
| `--type` | `flamegraph` | `flamegraph`, `bargraph`, `treemap`, `diagram`, `tree` |
| `--output` / `-o` | stdout | `.html` → self-contained report; `.svg` → responsive SVG. JSON sidecar auto-written. |
| `--width N` | 1800 / 1200 | SVG width in pixels |
| `--height N` | 32 | Frame height (flamegraph only) |
| `--colors` | `hot` | `hot`, `mem`, `io`, `red`, `green`, `blue` (flamegraph only) |
| `--title TEXT` | `MySQL Query Plan` | Chart title |
| `--inverted` | off | Icicle graph (flamegraph only) |
| `--no-enhance` | off | Disable detailed tooltips (flamegraph only) |
| `--query SQL` | — | Embed the original SQL text in the output |
| `--query-file PATH` | — | Read the original SQL from a file to embed in the output |

### Live connection — same flags as the `mysql` CLI

| Option | Description |
|--------|-------------|
| `-h HOST` / `--host` | Connect to this host (enables live mode) |
| `-P PORT`, `-u USER`, `-p[PASS]`, `-D DB` | Standard `mysql` flags |
| `--ssl-mode MODE` | `DISABLED`, `PREFERRED`, `REQUIRED`, `VERIFY_CA`, `VERIFY_IDENTITY` |
| `--ssl-ca`, `--ssl-cert`, `--ssl-key` | TLS paths |
| `--mysql-binary PATH` | Override `mysql`/`mariadb` autodetection |
| `-e SQL` / `--execute` | Query to `EXPLAIN ANALYZE` (required in live mode) |
| `--no-collect-schema` / `--no-collect-stats` / `--no-collect-variables` | Skip collection steps |

### Sidecar

| Option | Description |
|--------|-------------|
| *(default)* | Auto-write `<output>.json` |
| `--sidecar PATH` / `--no-sidecar` | Explicit path or opt-out |

### Subcommands

```bash
myflames compare before.json after.json --output diff.html
myflames teach btree -o btree.html   # interactive algorithm lesson
myflames guide                        # which view should I use?
```

Full help: `myflames --help`.

---

## Interactive features

All views support **Ctrl+F** regex search. The bar chart, treemap, diagram, and execution tree use click-to-pin details strips (text is always selectable). The diagram has +/− zoom buttons, drag-to-pan, and double-click to reset. Execution tree has Expand/Collapse All. See each demo for the full interaction set.

---

## Troubleshooting

**"Failed to parse EXPLAIN JSON"** — use `EXPLAIN ANALYZE FORMAT=JSON`, not just `EXPLAIN FORMAT=JSON`. The `ANALYZE` keyword is required for timing data.

**Interactive features not working** — open the `.html` wrapper, not the raw `.svg`. Browsers block inline scripts in SVGs loaded from `raw.githubusercontent.com`.

**macOS PEP 668** — use `pipx install myflames` instead of `pip install`.

---

## Documentation

| Page | Contents |
|------|----------|
| [Getting Started](https://vgrippa.github.io/myflames/guide/getting-started.html) | Installation, first flame graph, live connection mode |
| [View Types](https://vgrippa.github.io/myflames/guide/views.html) | When to use each of the 5 visualization types |
| [CLI Reference](https://vgrippa.github.io/myflames/guide/cli.html) | Every command, flag, and option |
| [Architecture](https://vgrippa.github.io/myflames/guide/architecture.html) | Parser, renderers, advisor, teach module internals |
| [Teach Lessons](https://vgrippa.github.io/myflames/teach/index.html) | All 19 interactive algorithm lessons with descriptions |
| [Contributing](CONTRIBUTING.md) | Development setup, testing, adding lessons/rules |
| [Visual Explain Reference](docs/VISUAL_EXPLAIN_REFERENCE.md) | Diagram layout conventions |
| [test/README.md](test/README.md) | Running tests and fixture generation |

---

## Credits

- [Brendan Gregg](https://github.com/brendangregg/FlameGraph) — FlameGraph implementation (pure-Python port in `myflames/flamegraph.py`)
- [Tanel Poder](https://tanelpoder.com/) — SQL Plan FlameGraph concept and label format

## License

Extends Brendan Gregg's FlameGraph project. See [docs/cddl1.txt](docs/cddl1.txt) (CDDL 1.0).
