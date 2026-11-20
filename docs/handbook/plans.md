# Execution plans & comparison

An execution plan describes how the engine will get a result. A timed plan also records what happened while the statement ran. myflames turns exported JSON into multiple views of the same plan. Step-by-step guides live in [Plans and schemas](howto-plans.md).

## Five views of the same tree

**Plans > Explore** shows the open plan in five views:

| View | Useful for |
| --- | --- |
| **Visual Explain** | Following operators, access paths and joins in a diagram |
| **Flame graph** | Seeing how measured execution work is distributed through the operator hierarchy |
| **Execution tree** | Reading the hierarchy and measurements as an expandable tree |
| **Bar chart** | Comparing the work associated with individual operators |
| **Stats** | Totals by table and operator type; all occurrences of an operator |

Estimated plans contain cost and cardinality estimates, not measured operator timings. A cost estimate is not elapsed time. Compare compatible capture modes and check loops when interpreting per-operator measurements.

## Four metrics

The **Metric** selector chooses the measurement the charts show: **Self time**, **Total time**, **Actual rows / loop** or **Estimation mismatch**. The choice applies to the view on screen and to both sides of a comparison.

## Capture a plan

Write the statement in the Query editor and choose **Explain** for the server's own plan, then **Explore** to open it in Plans as an estimated plan (see [Explain a statement](queries.md#explain-a-statement)). **Run analysis** is the timed choice: it executes the SELECT, so the query still consumes server resources. Check the [engine compatibility guide](compatibility.md) before choosing it.

**Run analysis options** set the capture: the statement runs the **Analysis runs** number of times (1 to 10) in a read-only transaction, each run limited to **Time limit per run**. **Capture optimizer trace** also records the optimizer trace, and **Parameters and execution conditions** fills `:name` values and notes the conditions of the capture. Ordinary runs follow the tab's **Run options** (**Rows**, **Time limit**, **Safe updates**, **On error: Stop / Continue**); see [Work with SQL and results](queries.md#work-with-sql-and-results).

From a statement worth investigating, **Workload > Top statements** and **Query digest** offer **Explain**, **Run analysis** (asks first) and **Use this query** per row, and **Explain top N** captures estimated plans for the first runnable SELECTs, at most 20. See [Find slow queries](queries.md#find-slow-queries).

**Import plan** in the Plans top bar (or on Home) opens a plan saved as JSON.

## Operator inspector, Focus branch and Go to

Selecting an operator opens the inspector with its measurements and **Execution context**. **Focus branch** keeps only the branch below the operator; **All occurrences** lists every occurrence of it in the plan, which the **Stats** view also totals. When the plan was captured from a live server with an account that can read the server's statistics tables, and the operator's table maps unambiguously to a single base table in the captured schema evidence, the Row estimate section shows when the table's statistics were last refreshed and an **ANALYZE TABLE statement** to copy and run yourself; myflames never runs it. The **Go to** menu jumps to **Largest self time** or **Largest estimate mismatch**, and **Expand all** restores collapsed branches.

## Plan findings

The plan header opens with **Investigate first** when the advisor has a primary finding, naming it and why it matters; without one it shows **Plan summary**. **Plan findings** lists every finding with its severity and an **Inspect operator** link to the operator behind it.

When a measured plan has eligible base-table accesses or filters directly above them whose actual and estimated rows per loop differ by at least 10×, with at least 1,000 estimated or actual rows across loops, **Misestimates N** appears in the plan's measurements. Press it to visit each one, worst ratio first; **Visual Explain**, the **Flame graph**, the **Execution tree** and the **Bar chart** mark them in violet (when the bar chart omits an operator because its self time is zero, a status message above the chart explains why and the inspector still opens it). Overestimates below an early-stop operator are skipped; MariaDB compares only eligible index lookups and only underestimates. The inspector's Row estimate section tells you how far off the estimate was.

## Compare two plans

Capture both plans around a change, keep the SQL and relevant execution conditions comparable, and open them side by side in **Plans > Compare**. The **Before** and **After** slots each take an open plan or an imported one (**Import plan…**). The **Operator changes** table lists what differs between the two trees, and **Export comparison** saves the comparison as JSON or an HTML report, with the same **Hide literal values** choice as a plan export.

Plan changes are evidence to investigate. Cache warmth, data distribution, concurrent load and capture mode affect how timing comparisons should be read.

## Save and manage investigations

**Save investigation** in the top bar keeps the open plans on this computer with a name, notes and tags, and marks one plan as the baseline. The top bar shows **Unsaved changes** while edits are pending and **Investigation saved** once they are kept.

**Library > Investigations** lists the saved investigations. **Search investigations** filters by name, notes or tags. **Open all** opens an investigation's plans, **Compare to baseline** compares a chosen plan with the investigation's baseline plan, **Export** saves the investigation as a file and **Delete** removes it.

## Share a plan safely

**Export plan** saves the open plan as **JSON** or an **HTML report**. Turn on **Hide literal values** to share a plan without the literal values in its SQL: table and column names, measurements and versions remain visible.

For measurement definitions and operator encodings, open the [Visual Explain reference](../VISUAL_EXPLAIN_REFERENCE.md).
