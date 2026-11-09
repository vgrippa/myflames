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

Write the statement in the Query editor and choose **Explain** for the server's own plan, then **Explore** to open it in Plans as an estimated plan (see [Query editor](queries.md#explain-a-statement)). **Run analysis** is the timed choice: it executes the SELECT, so the query still consumes server resources. Check the [engine compatibility guide](compatibility.md) before choosing it.

When a measured plan has eligible base-table accesses or filters directly above them whose actual and estimated rows per loop differ by at least 10×, with at least 1,000 estimated or actual rows across loops, **Misestimates N** appears in the plan's measurements. Press it to visit each one, worst ratio first; Visual Explain, the flame graph, the execution tree and the bar chart mark them in violet (when the bar chart omits an operator because its self time is zero, a status message above the chart explains why and the inspector still opens it). Overestimates below an early-stop operator are skipped; MariaDB compares only eligible index lookups and only underestimates. The inspector's Row estimate section tells you how far off the estimate was and, when the plan was captured from a live server with an account that can read the server's statistics tables and the operator's table maps unambiguously to a single base table in the captured schema evidence, when the table's statistics were last refreshed and an `ANALYZE TABLE` statement to copy and run yourself.

## Compare before and after

Capture both plans around a change, keep the SQL and relevant execution conditions comparable, and open them side by side in **Plans › Compare**.

Plan changes are evidence to investigate. Cache warmth, data distribution, concurrent load and capture mode affect how timing comparisons should be read.

## Keep the investigation useful

Save captures in Investigations, inspect ranked findings and follow their supporting operators. Export a report for a colleague with **Hide literal values** enabled. Table and column names, measurements and versions remain visible.

For measurement definitions and operator encodings, open the [Visual Explain reference](../VISUAL_EXPLAIN_REFERENCE.md).
