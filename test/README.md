# Test data

## MySQL EXPLAIN (myflames)

- **mysql-explain-*.json** – Sample `EXPLAIN ANALYZE FORMAT=JSON` output for testing the unified script.

To test:

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
