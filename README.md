<p align="center">
  <img src="myflames.jpeg" alt="myflames logo" width="200">
</p>

<h1 align="center">myflames</h1>
<p align="center"><strong>MySQL & MariaDB Query Plan Visualizer</strong></p>

<p align="center">
Visualize MySQL <code>EXPLAIN ANALYZE FORMAT=JSON</code> and MariaDB <code>ANALYZE FORMAT=JSON</code> output as interactive SVG charts. Five output types, one parser, no external dependencies.
</p>

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/).

---

## Installation

```bash
pip install myflames
```

Or install from source:

```bash
git clone https://github.com/vgrippa/myflames.git
cd myflames
pip install .
```

No external dependencies — pure Python 3.7+ stdlib.

### macOS (Homebrew Python)

Modern macOS with Homebrew Python blocks system-wide `pip install` (PEP 668). Use `pipx` instead:

```bash
brew install pipx
pipx install myflames
```

Or install from source:

```bash
brew install pipx
git clone https://github.com/vgrippa/myflames.git
cd myflames
pipx install .
```

The `myflames` command will be available globally without affecting your system Python.

---

## Try it in 30 seconds

A sample EXPLAIN JSON is included in the repo:

```bash
myflames sample.json > query.svg
open query.svg
```

Or generate a self-contained HTML report:

```bash
myflames --output report.html sample.json
```

---

## Output types

| Type | Best for | Command |
|------|----------|---------|
| **Flame graph** | Seeing the full execution hierarchy and time distribution | `myflames explain.json` |
| **Bar chart** | Quickly finding the slowest individual operations | `myflames --type bargraph explain.json` |
| **Treemap** | Comparing relative cost of all operations at a glance | `myflames --type treemap explain.json` |
| **Diagram** | Understanding join order and access paths (like MySQL Workbench Visual Explain) | `myflames --type diagram explain.json` |
| **Execution tree** | Navigating complex plans — collapsible per-subtree with self/total time per row | `myflames --type tree explain.json` |

Not sure which view to pick? Run `myflames guide` for a quick recommendation.

Every view includes a **Query Analysis panel** below the chart with optimizer features detected, warnings (full table scans, hash joins, BNL join buffers, temp tables, filesorts), and tuning suggestions.

---

## Live demos

### Complex join — all five views

| View | Interactive demo |
|------|-----------------|
| Flame graph | [mysql-query-complex-flamegraph.html](https://vgrippa.github.io/myflames/demos/mysql-query-complex-flamegraph.html) |
| Bar chart | [mysql-query-complex-bargraph.html](https://vgrippa.github.io/myflames/demos/mysql-query-complex-bargraph.html) |
| Treemap | [mysql-query-complex-treemap.html](https://vgrippa.github.io/myflames/demos/mysql-query-complex-treemap.html) |
| Diagram | [mysql-query-complex-diagram.html](https://vgrippa.github.io/myflames/demos/mysql-query-complex-diagram.html) |
| Execution tree | [mysql-query-complex-tree.html](https://vgrippa.github.io/myflames/demos/mysql-query-complex-tree.html) |

### Query Analysis panel demos

| Scenario | Flamegraph | Bar chart | Treemap | Diagram |
|----------|-----------|-----------|---------|---------|
| Full table scan | [fg](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-full-scan-flamegraph.html) | [bar](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-full-scan-bargraph.html) | [tree](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-full-scan-treemap.html) | [diag](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-full-scan-diagram.html) |
| Hash join | [fg](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-hash-join-flamegraph.html) | [bar](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-hash-join-bargraph.html) | [tree](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-hash-join-treemap.html) | [diag](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-hash-join-diagram.html) |
| BNL join buffer | [fg](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-bnl-flamegraph.html) | [bar](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-bnl-bargraph.html) | [tree](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-bnl-treemap.html) | [diag](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-bnl-diagram.html) |
| Index Condition Pushdown | [fg](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-icp-flamegraph.html) | [bar](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-icp-bargraph.html) | [tree](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-icp-treemap.html) | [diag](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-icp-diagram.html) |

### HTML report and comparison demos

| Demo | Link |
|------|------|
| HTML report (diagram) | [mysql-query-report.html](https://vgrippa.github.io/myflames/demos/mysql-query-report.html) |
| Before vs After comparison | [mysql-query-compare.html](https://vgrippa.github.io/myflames/demos/mysql-query-compare.html) |

> **Note:** Interactive features (zoom, search, tooltips) require the SVG to be opened from an HTML wrapper or GitHub Pages — not from a raw GitHub URL, which blocks inline scripts.

[All demos index](https://vgrippa.github.io/myflames/)

---

## Requirements

- **Python 3.7+** — no extra packages
- **MySQL 8.4+** with `explain_json_format_version = 2`, **or**
- **MariaDB 10.11+** / **11.4+**

### MySQL setup

Enable the required JSON format:

```sql
SET explain_json_format_version = 2;
-- or permanently in my.cnf:
-- [mysqld]
-- explain_json_format_version = 2
```

### MariaDB setup

No special configuration needed. MariaDB 10.5+ supports `ANALYZE FORMAT=JSON` natively.
MariaDB also supports `SHOW ANALYZE FORMAT=JSON FOR <connection_id>` and
`SHOW EXPLAIN FORMAT=JSON FOR <connection_id>` for live query analysis.

---

## Quick start

### 1. Get EXPLAIN ANALYZE output

**MySQL:**

```sql
EXPLAIN ANALYZE FORMAT=JSON
SELECT u.name, COUNT(o.id)
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE u.country = 'US'
GROUP BY u.id;
```

**MariaDB:**

```sql
ANALYZE FORMAT=JSON
SELECT u.name, COUNT(o.id)
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE u.country = 'US'
GROUP BY u.id;
```

Save to a file — any of these work:

```bash
# MySQL: Recommended: -s -N -r gives clean JSON
mysql -u user -p mydb -s -N -r -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." > explain.json

# MySQL: Also works: myflames auto-strips table borders, headers, and escaped newlines
mysql -u user -p mydb -N -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." > explain.json
mysql -u user -p mydb -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." > explain.json

# MariaDB:
mariadb -u user -p mydb -s -N -r -e "ANALYZE FORMAT=JSON SELECT ..." > explain.json

# MariaDB: SHOW ANALYZE for a running query (from another session)
mariadb -u user -p mydb -s -N -r -e "SHOW ANALYZE FORMAT=JSON FOR <connection_id>" > explain.json
```

Or pipe directly (stdin supported):

```bash
mysql -u user -p mydb -N -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." | myflames > query.svg
mariadb -u user -p mydb -N -e "ANALYZE FORMAT=JSON SELECT ..." | myflames > query.svg
```

### 2. Generate a visualization

```bash
# Flame graph (default)
myflames explain.json > query.svg

# Bar chart — slowest operations first
myflames --type bargraph explain.json > query-bar.svg

# Treemap — area proportional to total time
myflames --type treemap explain.json > query-treemap.svg

# Diagram — Visual Explain-style flow
myflames --type diagram explain.json > query-diagram.svg

# Execution tree — collapsible per-subtree
myflames --type tree explain.json > query-tree.svg

# Self-contained HTML report (attach to a ticket, send to a teammate)
myflames --output report.html explain.json
```

### 3. Open in a browser

```bash
open query.svg        # macOS
xdg-open query.svg    # Linux
start query.svg       # Windows
```

---

## All options

```
myflames [--type TYPE] [--output PATH] [options] explain.json
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type` | `flamegraph` | Output type: `flamegraph`, `bargraph`, `treemap`, `diagram`, `tree` |
| `--output PATH` / `-o` | stdout | Write to file. Use `.html` extension for a self-contained HTML report |
| `--width N` | 1800 (fg), 1200 (others) | SVG width in pixels |
| `--height N` | 32 | Frame height in pixels (flamegraph only) |
| `--colors SCHEME` | `hot` | Color scheme (flamegraph only): `hot`, `mem`, `io`, `red`, `green`, `blue` |
| `--title TEXT` | `MySQL Query Plan` | Chart title |
| `--inverted` | off | Icicle graph — flames grow downward (flamegraph only) |
| `--no-enhance` | off | Disable detailed tooltips (flamegraph only) |
| `--version` | | Show version and exit |

### Subcommands

```bash
# Compare before/after optimization
myflames compare before.json after.json
myflames compare before.json after.json --output diff.html

# Which view should I use?
myflames guide
```

---

## HTML report output

Generate a self-contained HTML file you can attach to a ticket, send to a teammate, or publish:

```bash
myflames --output report.html explain.json
myflames --type diagram --output report.html explain.json
```

The HTML report includes:
- Embedded interactive SVG chart
- Analysis sidebar with warnings, suggestions, and optimizer features
- SQL query display
- Export buttons (SVG, JSON, Print/PDF)

---

## Compare before/after optimization

```bash
myflames compare before.json after.json
myflames compare before.json after.json --output diff.html
```

The comparison report shows:
- Total query time delta
- Per-operator self-time, rows, and loop count changes
- New or removed full table scans
- New or resolved warnings
- Color-coded "what got better / worse" summary

---

## Interactive features

### Flame graph
| Action | Result |
|--------|--------|
| Hover frame | Shows operation details: rows, loops, time, cost, index conditions |
| Click frame | Zoom into that operation |
| Click Reset Zoom button | Reset zoom |
| Ctrl+F | Search frames by regex |

### Bar chart
| Action | Result |
|--------|--------|
| Hover bar | Shows multi-line details in the strip below the chart |
| Click bar | Pins the details in the strip (click again or click background to unpin) |
| Ctrl+F | Search bars by regex |

### Treemap
| Action | Result |
|--------|--------|
| Hover cell | Shows multi-line details below the chart |
| Click cell | Zoom into that node and pin its details |
| Click Reset Zoom button | Reset zoom |
| Ctrl+F | Search cells by regex |

### Diagram
| Action | Result |
|--------|--------|
| Hover node | Shows details in the strip below the diagram |
| Click node | Pins the details (stays visible while you scroll) |
| Click pinned node | Unpins |
| +/− buttons (bottom-right) | Zoom in/out |
| ↺ button | Reset zoom |
| Drag background | Pan the diagram |
| Double-click background | Reset zoom and pan |
| Ctrl+F | Search nodes by regex |

### Execution tree
| Action | Result |
|--------|--------|
| Hover row | Shows multi-line details in the strip below the tree |
| Click row | Pins the details (click again or click background to unpin) |
| Click ▾ / ▸ | Collapse / expand that subtree |
| Expand All / Collapse All | Expand or collapse the entire tree at once |
| Ctrl+F | Search rows by regex |

> Text in the details strip is always selectable — you can copy/paste it freely.

---

## Query Analysis panel

Every output type includes a panel below the chart with:

- **How to read** — view-specific guide
- **Optimizer features** — e.g. `index_condition_pushdown=on`, `batched_key_access`
- **Warnings** — issues that affect performance:
  - Full table scans (with row count)
  - Hash joins (with estimated build phase size)
  - Block Nested-Loop (BNL) join buffers
  - Temp tables / Materialize operations
  - Filesorts
- **Suggestions** — concrete tuning actions (add indexes, increase `join_buffer_size`, enable hash join, etc.)

Warnings also show which node label in the chart they refer to, so you can find the slow operation quickly.

---

## How to read each view

### Flame graph
- **Width = time** — wider frames consumed more time (including children)
- **Bottom = root** — the query entry point; table accesses are at the top
- **Self-time** — the visible "tip" of each frame is time spent in that operation alone

Frame labels follow [Tanel Poder's format](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/):
```
OPERATION [table.index] starts=X rows=Y
```
Example: `INDEX LOOKUP [orders.idx_user] starts=1000 rows=5`
→ This lookup ran 1000 times in a nested loop, returning 5 rows each time.

### Bar chart
- Sorted slowest first by **self-time** (time in that operation, not counting children)
- Percentage shows each operation's share of total query time

### Treemap
- **Area = total time** (including all descendants)
- Nested rectangles show the parent/child relationship
- Color intensity indicates relative cost

### Diagram
- Left-to-right execution flow (table accesses → join → result)
- **Darker color = more time** spent at that step
- Arrows show row flow with estimated row counts
- Diamonds represent nested-loop join decision points

---

## Time unit auto-detection

All views automatically switch units based on total query time:

| Total time | Unit |
|------------|------|
| ≥ 1 ms | ms (milliseconds) |
| < 1 ms | µs (microseconds) |

---

## Advanced usage

### Generate folded stacks only

```bash
python3 stackcollapse_mysql_explain_json.py explain.json > stacks.txt
```

Useful for feeding into other FlameGraph-compatible tools.

---

## Troubleshooting

**"No module named 'myflames'"**
Run `pip install myflames` or `pip install .` from the repo root.

**"Failed to parse EXPLAIN JSON"**
Make sure you're using `EXPLAIN ANALYZE FORMAT=JSON` (not just `EXPLAIN FORMAT=JSON`). The `ANALYZE` keyword is required for timing data. myflames automatically handles common MySQL CLI output quirks (escaped newlines, table borders, `EXPLAIN` column headers, BOM), so you don't need `-s -r` flags — but adding them gives the cleanest output: `mysql -s -N -r -e "EXPLAIN ANALYZE FORMAT=JSON ..." > explain.json`.

**Interactive features not working**
Open the `.html` wrapper file instead of the raw `.svg`. Browsers block inline scripts in SVGs loaded from `raw.githubusercontent.com`. Local `file://` access works fine. Or use `--output report.html` to generate a self-contained HTML report.

---

## Documentation

| File | Contents |
|------|----------|
| [LEGACY-PERL.md](LEGACY-PERL.md) | Original Perl scripts (legacy, not primary) |
| [docs/VISUAL_EXPLAIN_REFERENCE.md](docs/VISUAL_EXPLAIN_REFERENCE.md) | Diagram layout conventions and MySQL Workbench Visual Explain mapping |
| [docs/prompts/](docs/prompts/) | Prompts and context used to build each feature (for contributors) |
| [test/README.md](test/README.md) | Running tests and fixture generation |

---

## Credits

- [Brendan Gregg](https://github.com/brendangregg/FlameGraph) — FlameGraph implementation (pure-Python port in `myflames/flamegraph.py`)
- [Tanel Poder](https://tanelpoder.com/) — SQL Plan FlameGraph concept and label format

## License

Extends Brendan Gregg's FlameGraph project. See [docs/cddl1.txt](docs/cddl1.txt) (CDDL 1.0).
