# myflames — Legacy Perl scripts

This document describes the **original Perl-based** myflames scripts. The project has been migrated to **Python**; for current usage see the main [README.md](README.md).

The Perl files are kept in the repository for users who prefer them or need a Perl-only environment. Functionality is the same: unified parser, flame graph, bar chart, and treemap from MySQL `EXPLAIN ANALYZE FORMAT=JSON` output.

---

## Prerequisites

- Perl 5.x (with `JSON::PP`, `Getopt::Long`, `File::Spec`, `IPC::Open2` — usually included or via `cpan`)
- MySQL 8.4+ with `EXPLAIN ANALYZE FORMAT=JSON` and JSON format version 2

## Installation

```bash
git clone https://github.com/vgrippa/myflames.git
cd myflames
chmod +x *.pl
```

Ensure `flamegraph.pl` is in the same directory as `mysql-explain.pl` (required for `--type flamegraph`).

## Quick Start

1. Get EXPLAIN output:
   ```bash
   mysql -u user -p database -e "EXPLAIN ANALYZE FORMAT=JSON SELECT ..." > explain.json
   ```

2. Generate visualizations:
   ```bash
   # Flame graph (default)
   ./mysql-explain.pl explain.json > query.svg

   # Bar chart
   ./mysql-explain.pl --type bargraph explain.json > query-bar.svg

   # Treemap
   ./mysql-explain.pl --type treemap explain.json > query-treemap.svg
   ```

3. Open the SVG in a browser (e.g. `open query.svg` on macOS).

## Unified command

```bash
./mysql-explain.pl [--type flamegraph|bargraph|treemap] [options] explain.json > output.svg
```

| Option | Default | Description |
|--------|---------|-------------|
| `--type TYPE` | flamegraph | Output: `flamegraph`, `bargraph`, or `treemap` |
| `--width N` | 1800 (fg), 1200 (bar/treemap) | SVG width in pixels |
| `--height N` | 32 | Frame height (flame graph) |
| `--colors SCHEME` | hot | Color scheme: hot, mem, io, red, green, blue |
| `--title TEXT` | "MySQL Query Plan" | Chart title |
| `--inverted` | off | Icicle graph (flame graph only) |
| `--enhance` / `--no-enhance` | on | Detailed tooltips (flame graph) |
| `--help` | | Show help |

## Legacy wrapper scripts

These call the unified script with a fixed `--type`:

- **Flame graph:** `./mysql-explain-flamegraph.pl [options] explain.json > output.svg`
- **Bar chart:** `./mysql-explain-bargraph.pl [options] explain.json > output.svg`

## Stack collapse (folded format)

To emit only folded stacks (e.g. for piping to another tool or a different flamegraph renderer):

```bash
./stackcollapse-mysql-explain-json.pl explain.json > stacks.txt
./stackcollapse-mysql-explain-json.pl explain.json | ./flamegraph.pl --colors hot --title "Query" --countname ms > query.svg
```

Options for `stackcollapse-mysql-explain-json.pl`:
- `--use-total` — use total time instead of self time
- `--time-unit=ms|us|s` — time unit (default: ms)
- `--help` — show help

## Flame graph script (upstream)

`flamegraph.pl` is the upstream Brendan Gregg FlameGraph script. It reads folded stack lines from stdin and writes SVG. It is used by `mysql-explain.pl` when `--type flamegraph`. See the script header or `./flamegraph.pl --help` for its full options.

## Troubleshooting (Perl)

- **"Cannot find flamegraph.pl"** — Ensure `flamegraph.pl` is in the same directory as `mysql-explain.pl` and is executable (`chmod +x flamegraph.pl`).
- **Empty or minimal output** — Use `EXPLAIN ANALYZE FORMAT=JSON`, not just `EXPLAIN FORMAT=JSON`.
- **JSON parse error** — Strip any leading text (e.g. `EXPLAIN:`) or use the MySQL client’s `-N` flag when piping.

## Migration to Python

The same workflows are available in Python with no Perl dependency. See [README.md](README.md) for:

- `python3 -m myflames explain.json > query.svg`
- `python3 -m myflames --type bargraph explain.json > query-bar.svg`
- `python3 -m myflames --type treemap explain.json > query-treemap.svg`

Perl scripts remain in the repo and continue to work alongside the Python implementation.
