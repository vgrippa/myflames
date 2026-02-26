# Test data

Use the sample JSON files below to verify the [unified CLI](../../README.md#quick-start) and all output types (flame graph, bar chart, treemap, diagram).

## MySQL EXPLAIN (myflames)

- **mysql-explain-*.json** – Sample `EXPLAIN ANALYZE FORMAT=JSON` output for testing the unified script.

### Python (recommended)

From the project root:

```bash
# Flame graph (default)
python3 -m myflames test/mysql-explain-json-sample.json > out.svg

# Bar chart
python3 -m myflames --type bargraph test/mysql-explain-json-join.json > out.svg

# Treemap
python3 -m myflames --type treemap test/mysql-explain-complex-join.json > out.svg

# Diagram (Visual Explain style)
python3 -m myflames --type diagram test/mysql-explain-json-sample.json > diagram.svg
python3 -m myflames --type diagram test/mysql-explain-json-join.json > diagram-join.svg
python3 -m myflames --type diagram test/mysql-explain-complex-join.json > diagram-complex.svg
```

Open the generated SVG in a browser to verify. No extra dependencies required.

### Perl (legacy)

```bash
./mysql-explain.pl test/mysql-explain-json-sample.json > /dev/null
./mysql-explain.pl --type bargraph test/mysql-explain-json-join.json > /dev/null
./mysql-explain.pl --type treemap test/mysql-explain-complex-join.json > /dev/null
```

## Upstream FlameGraph (stackcollapse-perf.pl)

- **perf-*.txt** – Sample `perf script` output.
- **results/** – Expected collapsed output for regression testing.

Run the upstream test suite:

```bash
./test.sh
```

To regenerate expected results after changing stackcollapse-perf.pl:

```bash
./record-test.sh
```
