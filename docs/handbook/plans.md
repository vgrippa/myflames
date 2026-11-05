# Execution plans & comparison

An execution plan describes how the engine will get a result. A timed plan also records what happened while the statement ran. myflames turns exported JSON into multiple views of the same plan.

## Choose a view for the question

| View | Useful for |
| --- | --- |
| Visual Explain | Following operators, access paths and joins in a diagram |
| Flame graph | Seeing how measured execution work is distributed through the operator hierarchy |
| Execution tree | Reading the hierarchy and measurements as an expandable tree |
| Bar chart | Comparing the work associated with individual operators |
| Stats | Totals by table and operator type; all occurrences of an operator |

Estimated plans contain cost and cardinality estimates, not measured operator timings. A cost estimate is not elapsed time. Compare compatible capture modes and check loops when interpreting per-operator measurements.

## Capture, then investigate

This example assumes a saved `prod` CLI profile and an `orders` table. `-p` prompts for the password. Capture defaults to an estimated plan:

```bash
myflames capture -p --profile prod \
  -e "SELECT * FROM orders WHERE customer_id = 42" \
  -o before.json
myflames explore before.json --hide-literals -o before.html
```

Timed capture is an explicit choice: `--mode analyze`. It executes the SELECT in a rolled-back read-only transaction. The query still consumes server resources. Check the [engine compatibility guide](compatibility.md) before choosing it.

## Compare before and after

Capture both plans around a change, keep the SQL and relevant execution conditions comparable, and use Compare in the app or the CLI:

```bash
myflames compare before.json after.json -o comparison.html
myflames compare before.json after.json --digest
```

Plan changes are evidence to investigate. Cache warmth, data distribution, concurrent load and capture mode affect how timing comparisons should be read.

## Keep the investigation useful

Save captures in Investigations, inspect ranked findings and follow their supporting operators. Export a report for a colleague with **Hide literal values** enabled. Table and column names, measurements and versions remain visible.

For measurement definitions and operator encodings, open the [Visual Explain reference](../VISUAL_EXPLAIN_REFERENCE.md).
