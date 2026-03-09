# myflames — MySQL Query Plan Visualizer

Visualize MySQL `EXPLAIN ANALYZE FORMAT=JSON` output as interactive SVG charts. Four output types, one parser, no external dependencies.

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/).

---

## Output types

| Type | Best for | Command |
|------|----------|---------|
| **Flame graph** | Seeing the full execution hierarchy and time distribution | `python3 -m myflames explain.json` |
| **Bar chart** | Quickly finding the slowest individual operations | `python3 -m myflames --type bargraph explain.json` |
| **Treemap** | Comparing relative cost of all operations at a glance | `python3 -m myflames --type treemap explain.json` |
| **Diagram** | Understanding join order and access paths (like MySQL Workbench Visual Explain) | `python3 -m myflames --type diagram explain.json` |

Every view includes a **Query Analysis panel** below the chart with optimizer features detected, warnings (full table scans, hash joins, BNL join buffers, temp tables, filesorts), and tuning suggestions.

---

## Live demos

### Complex join — all four views

| View | Interactive demo |
|------|-----------------|
| Flame graph | [mysql-query-complex-flamegraph.html](https://vgrippa.github.io/myflames/demos/mysql-query-complex-flamegraph.html) |
| Bar chart | [mysql-query-complex-bargraph.html](https://vgrippa.github.io/myflames/demos/mysql-query-complex-bargraph.html) |
| Treemap | [mysql-query-complex-treemap.html](https://vgrippa.github.io/myflames/demos/mysql-query-complex-treemap.html) |
| Diagram | [mysql-query-complex-diagram.html](https://vgrippa.github.io/myflames/demos/mysql-query-complex-diagram.html) |

### Query Analysis panel demos

| Scenario | Flamegraph | Bar chart | Treemap | Diagram |
|----------|-----------|-----------|---------|---------|
| Full table scan | [fg](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-full-scan-flamegraph.html) | [bar](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-full-scan-bargraph.html) | [tree](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-full-scan-treemap.html) | [diag](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-full-scan-diagram.html) |
| Hash join | [fg](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-hash-join-flamegraph.html) | [bar](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-hash-join-bargraph.html) | [tree](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-hash-join-treemap.html) | [diag](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-hash-join-diagram.html) |
| BNL join buffer | [fg](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-bnl-flamegraph.html) | [bar](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-bnl-bargraph.html) | [tree](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-bnl-treemap.html) | [diag](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-bnl-diagram.html) |
| Index Condition Pushdown | [fg](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-icp-flamegraph.html) | [bar](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-icp-bargraph.html) | [tree](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-icp-treemap.html) | [diag](https://vgrippa.github.io/myflames/demos/mysql-query-analysis-icp-diagram.html) |

> **Note:** Interactive features (zoom, search, tooltips) require the SVG to be opened from an HTML wrapper or GitHub Pages — not from a raw GitHub URL, which blocks inline scripts.

[All demos index](https://vgrippa.github.io/myflames/)

---

## Requirements

- **Python 3.7+** — no extra packages
- **MySQL 8.4+** with `explain_json_format_version = 2`

Enable the required JSON format:

```sql
SET explain_json_format_version = 2;
-- or permanently in my.cnf:
-- [mysqld]
-- explain_json_format_version = 2
```

---

## Quick start

### 1. Get EXPLAIN ANALYZE output

```sql
EXPLAIN ANALYZE FORMAT=JSON
SELECT u.name, COUNT(o.id)
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE u.country = 'US'
GROUP BY u.id;
```

Save to a file:

```bash
mysql -u user -p mydb -N -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." > explain.json
```

Or pipe directly (stdin supported):

```bash
mysql -u user -p mydb -N -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." | python3 -m myflames > query.svg
```

### 2. Generate a visualization

```bash
# Flame graph (default)
python3 -m myflames explain.json > query.svg

# Bar chart — slowest operations first
python3 -m myflames --type bargraph explain.json > query-bar.svg

# Treemap — area proportional to total time
python3 -m myflames --type treemap explain.json > query-treemap.svg

# Diagram — Visual Explain-style flow
python3 -m myflames --type diagram explain.json > query-diagram.svg
```

### 3. Open in a browser

```bash
open query.svg        # macOS
xdg-open query.svg    # Linux
start query.svg       # Windows
```

---

## Installation

```bash
git clone https://github.com/vgrippa/myflames.git
cd myflames
```

Run from the repo root. No `pip install` needed.

---

## All options

```
python3 -m myflames [--type TYPE] [options] explain.json > output.svg
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type` | `flamegraph` | Output type: `flamegraph`, `bargraph`, `treemap`, `diagram` |
| `--diagram-engine` | `svg` | Diagram layout engine: `svg` (built-in) or `graphviz` (requires [Graphviz](https://graphviz.org/) on PATH) |
| `--width N` | 1800 (fg), 1200 (others) | SVG width in pixels |
| `--height N` | 32 | Frame height in pixels (flamegraph only) |
| `--colors SCHEME` | `hot` | Color scheme (flamegraph only): `hot`, `mem`, `io`, `red`, `green`, `blue` |
| `--title TEXT` | `MySQL Query Plan` | Chart title |
| `--inverted` | off | Icicle graph — flames grow downward (flamegraph only) |
| `--no-enhance` | off | Disable detailed tooltips (flamegraph only) |

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
| Scroll wheel (diagram area only) | Zoom in/out |
| Drag background | Pan the diagram |
| Double-click background | Reset zoom and pan |
| Ctrl+F | Search nodes by regex |

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

### Compare before/after optimization

```bash
python3 -m myflames --title "Before" before.json > before.svg
python3 -m myflames --title "After"  after.json  > after.svg

python3 -m myflames --type bargraph --title "Before" before.json > before-bar.svg
python3 -m myflames --type bargraph --title "After"  after.json  > after-bar.svg
```

### Generate folded stacks only

```bash
python3 stackcollapse_mysql_explain_json.py explain.json > stacks.txt
```

Useful for feeding into other FlameGraph-compatible tools.

---

## Troubleshooting

**"No module named 'myflames'"**
Run from the repo root, or use the wrapper scripts directly: `python3 mysql_explain.py`.

**Empty output or parse error**
Make sure you're using `EXPLAIN ANALYZE FORMAT=JSON` (not just `EXPLAIN FORMAT=JSON`). The `ANALYZE` keyword is required for timing data.

**Interactive features not working**
Open the `.html` wrapper file instead of the raw `.svg`. Browsers block inline scripts in SVGs loaded from `raw.githubusercontent.com`. Local `file://` access works fine.

**Graphviz diagram falls back to built-in**
Install [Graphviz](https://graphviz.org/) and ensure `dot` is on your PATH, then use `--diagram-engine graphviz`.

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
