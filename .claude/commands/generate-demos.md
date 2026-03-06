# Generate Demos — myflames

Regenerate all SVG demo files in `docs/demos/` from their canonical fixtures.
Run every command below, then confirm all files were updated.

## Fixture → SVG mapping

```bash
D=docs/demos

# Basic (json-sample: simple single-table query, no warnings)
python3 -m myflames --type bargraph test/mysql-explain-json-sample.json > $D/mysql-query-bargraph.svg
python3 -m myflames --type diagram  test/mysql-explain-json-sample.json > $D/mysql-query-diagram.svg

# Complex join (complex-join: 4-table join with sort + full scan)
python3 -m myflames --type flamegraph test/mysql-explain-complex-join.json > $D/mysql-query-complex-flamegraph.svg
python3 -m myflames --type bargraph   test/mysql-explain-complex-join.json > $D/mysql-query-complex-bargraph.svg
python3 -m myflames --type treemap    test/mysql-explain-complex-join.json > $D/mysql-query-complex-treemap.svg
python3 -m myflames --type diagram    test/mysql-explain-complex-join.json > $D/mysql-query-complex-diagram.svg

# Full table scan (001: table scan on users, 3000 rows)
python3 -m myflames --type flamegraph test/fixtures/explain-001-table-scan-users-no-filter.json > $D/mysql-query-analysis-full-scan.svg
python3 -m myflames --type flamegraph test/fixtures/explain-001-table-scan-users-no-filter.json > $D/mysql-query-analysis-full-scan-flamegraph.svg
python3 -m myflames --type bargraph   test/fixtures/explain-001-table-scan-users-no-filter.json > $D/mysql-query-analysis-full-scan-bargraph.svg
python3 -m myflames --type treemap    test/fixtures/explain-001-table-scan-users-no-filter.json > $D/mysql-query-analysis-full-scan-treemap.svg
python3 -m myflames --type diagram    test/fixtures/explain-001-table-scan-users-no-filter.json > $D/mysql-query-analysis-full-scan-diagram.svg

# Hash join (hash-join: users + orders with hash join warning)
python3 -m myflames --type flamegraph test/mysql-explain-hash-join.json > $D/mysql-query-analysis-hash-join.svg
python3 -m myflames --type flamegraph test/mysql-explain-hash-join.json > $D/mysql-query-analysis-hash-join-flamegraph.svg
python3 -m myflames --type bargraph   test/mysql-explain-hash-join.json > $D/mysql-query-analysis-hash-join-bargraph.svg
python3 -m myflames --type treemap    test/mysql-explain-hash-join.json > $D/mysql-query-analysis-hash-join-treemap.svg
python3 -m myflames --type diagram    test/mysql-explain-hash-join.json > $D/mysql-query-analysis-hash-join-diagram.svg

# BNL — Block Nested Loop join buffer
python3 -m myflames --type flamegraph test/mysql-explain-bnl.json > $D/mysql-query-analysis-bnl-flamegraph.svg
python3 -m myflames --type bargraph   test/mysql-explain-bnl.json > $D/mysql-query-analysis-bnl-bargraph.svg
python3 -m myflames --type treemap    test/mysql-explain-bnl.json > $D/mysql-query-analysis-bnl-treemap.svg
python3 -m myflames --type diagram    test/mysql-explain-bnl.json > $D/mysql-query-analysis-bnl-diagram.svg

# ICP — Index Condition Pushdown (008: index scan on users by country)
python3 -m myflames --type flamegraph test/fixtures/explain-008-index-scan-users-by-country.json > $D/mysql-query-analysis-icp.svg
python3 -m myflames --type flamegraph test/fixtures/explain-008-index-scan-users-by-country.json > $D/mysql-query-analysis-icp-flamegraph.svg
python3 -m myflames --type bargraph   test/fixtures/explain-008-index-scan-users-by-country.json > $D/mysql-query-analysis-icp-bargraph.svg
python3 -m myflames --type treemap    test/fixtures/explain-008-index-scan-users-by-country.json > $D/mysql-query-analysis-icp-treemap.svg
python3 -m myflames --type diagram    test/fixtures/explain-008-index-scan-users-by-country.json > $D/mysql-query-analysis-icp-diagram.svg

# Derived table + sort (052: top spenders with temp table + filesort)
python3 -m myflames --type flamegraph test/fixtures/explain-052-derived-table-top-spenders.json > $D/mysql-query-analysis-derived-sort.svg
```

## Verify

```bash
python3 -c "
import os, glob, re
svgs = glob.glob('docs/demos/*.svg')
print(f'Total SVG files: {len(svgs)}')
errors = []
for f in svgs:
    svg = open(f).read()
    if '<svg' not in svg:
        errors.append(f + ': missing <svg tag')
    if 'How to read' not in svg:
        errors.append(f + ': missing info panel')
    # Flamegraphs must have matching height and viewBox
    if 'viewBox' in svg:
        m_h = re.search(r'height=\"(\d+)\"', svg)
        m_vb = re.search(r'viewBox=\"0 0 \d+ (\d+)\"', svg)
        if m_h and m_vb and m_h.group(1) != m_vb.group(1):
            errors.append(f + f': height/viewBox mismatch ({m_h.group(1)} vs {m_vb.group(1)})')
if errors:
    print('ERRORS:')
    for e in errors: print(' ', e)
else:
    print('All SVGs OK')
"
```
