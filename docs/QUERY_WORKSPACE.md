# Query investigations

Launch `myflames ui` to work with imported plans or capture plans from a database.
The workspace uses the same Python parser, analysis, renderers, and comparison
engine as the CLI. Its interface uses the [stacked-flame app mark](brand/README.md),
neutral surfaces, system typography, and a blue accent for selected items and
primary actions. Layout and hierarchy follow [Apple's layout guidance](https://developer.apple.com/design/human-interface-guidelines/layout).

## Explore a plan

Select an operator in any of the five charts, the operator table, or the metric
list inside the chart. Selection stays linked to the inspector when you switch
views. The inspector shows measured timings, actual and estimated rows,
related findings, raw fields, and a lesson when the existing Teach registry has
a matching topic. **Learn this operator** opens that lesson; **Back to
investigation** returns to the same plan and selection.

Use **Focus branch** to isolate a subtree. The breadcrumb returns to an ancestor
or the entire plan. **Collapse branch** hides descendants without changing the
recorded measurements; **Expand all** restores them. The chart's metric selector
controls a separate set of operator bars below the native chart:

- Self time: milliseconds across all loops.
- Total time: includes input work; overlapping totals must not be summed.
- Actual rows: rows per loop.
- Estimation mismatch: larger row count divided by smaller, with the direction
  labeled. Missing values and a zero/nonzero pair are shown explicitly.

Native charts keep their original visual meanings. Selecting rows does not turn
a time-width flame graph into a row-width chart. **Largest self time** and
**Largest estimate mismatch** select the relevant operator. Operators omitted
by a native view remain accessible in the full operator table and metric list.
MySQL SELECT-list subquery inputs are retained as separate branches; their
timing is not subtracted from the enclosing iterator's self time.

## Save and reopen work

The investigation notebook holds a name, notes, tags, named plan experiments,
and a baseline. **Save investigation** explicitly stores these on this computer
in `~/.myflames/workspace.sqlite3`. Saving is opt-in; importing a plan alone does
not write it to disk. The library supports search, reopening, and deletion.
Unsaved changes trigger the browser's leave-page prompt. Opening another bundle
while unsaved plans exist downloads a backup bundle before replacing them.

**Export investigation bundle** creates a portable `investigation-1.0` JSON
file containing raw plans, captured evidence, notes, and baseline references.
Import it through the same file picker used for plans. An investigation holds up
to 30 plans and 10 MiB; individual plans remain limited to 5 MiB. Credentials
are excluded from profiles, saved investigations, and capture output.

## Compare experiments

Choose before/after plans. Selecting either chart or an operator comparison row
highlights corresponding operators. Matches use semantic subtrees, operator
identity, and surrounding structure. Each match reports its method and
confidence. Added, removed, changed, and ambiguous operators are explicit.
Ambiguous repeated operators remain tentative; they do not count as regressions.

Estimated plans can be compared structurally. A timing comparison requires
measurements on both sides; missing measurements are not zero. Actual measured
zero is retained. Repeated captures expose median, range, standard deviation,
and conditions in **Measurements**. The comparison chart uses the last captured
plan and labels the run summary separately. Cache state is not reset between
runs. Differences across servers, datasets, cache state, and instrumentation can
affect measurements.

## Capture a live query

Open **Query laboratory**. Enter a host, port, user, schema, and connection
credentials. **Test connection** also lists visible schemas. Save a profile to
retain connection settings; the password stays in page/server memory only and
must be entered again after reopening. TLS mode and CA path are configurable.
A local `mysql` or `mariadb` client executable is required, as with the existing
CLI connection workflow.

For a practice server, run `./scripts/demo-db.sh` (requires Docker). It starts
MySQL 8.4 on `127.0.0.1:3406` and prints credentials for a read-only `demo`
user. It loads `testdb` (users, orders, order_items, products, categories,
reviews; about 9M rows with skewed values, from `scripts/demo-db-seed.sql`),
MySQL's `sakila` sample, and the `employees` sample. Set `DEMO_DATASETS` to
load fewer, for example `DEMO_DATASETS=testdb`.

Use query tabs to keep drafts and name an experiment. Parameters are typed
values in a JSON object, referenced as `:name` outside SQL literals/comments:

```sql
SELECT * FROM orders WHERE customer_id = :customer_id
```

```json
{"customer_id": 42}
```

- **Explain** captures an estimated JSON plan, once.
- **Run analysis** executes the SELECT and captures actual iterator timings.
- Choose 1–10 analysis runs and a 1–300 second limit per run.
- **Cancel capture** requests cancellation of this job's database query and
  terminates its client process. A time limit follows the same cancellation path.

Capture accepts one read-only SELECT (including a WITH…SELECT), without
assignments, file output, locking clauses, or client commands. SQL validation is
conservative; use the existing CLI workflow for unsupported statement forms.
Draft tabs are kept in the page session. Captured SQL is included when you save
an investigation. At most two captures run concurrently. Finished captures are
kept in server memory for up to one hour, with a maximum of 50 jobs.

## Understand execution context

**Tables & indexes** shows definitions, parsed columns/indexes, and table
statistics collected for referenced tables. **Settings** shows captured server
variables. Missing privileges produce collection notices; they do not fabricate
evidence.

**Measurements** includes each run's plan time, session status changes, and
Performance Schema statement statistics when available. Capture keeps these in
the same session as its EXPLAIN statement. Status deltas include capture and
instrumentation overhead. Performance Schema records the EXPLAIN/ANALYZE
statement, including its result row; these counters are not interchangeable
with the underlying SELECT's returned rows or iterator time. Statement history
requires suitable instruments, consumers, and privileges. See [MySQL statement
event tables](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-statement-tables.html).

Enable **Capture optimizer trace** before a run, then browse the expandable,
searchable trace tree. myflames disables tracing immediately after the target
statement so later collection queries do not replace its trace. Truncation and
privilege restrictions are displayed. A considered alternative is not
necessarily the selected plan. See [MySQL optimizer tracing](https://dev.mysql.com/doc/refman/8.4/en/optimizer-tracing.html).

## CLI equivalents

Capture uses the same execution engine as the UI. Bare `-p` prompts for a
password. The default mode produces an estimated plan; `--mode analyze` runs the
SELECT. Output is a versioned `capture-1.0` document that the UI can import.

```bash
myflames capture -h 127.0.0.1 -P 3306 -u analyst -p -D app \
  -e 'SELECT * FROM orders WHERE customer_id = :id' \
  --parameters '{"id": 42}' --mode analyze --repeat 3 --timeout 30 \
  --trace --conditions 'Warm cache, test dataset' -o capture.json

myflames explore explain.json --type workbench \
  --metric estimate_error -o exploration.html
```

`explore` accepts `--focus NODE_ID`, repeatable `--collapse NODE_ID`, and
`--selected NODE_ID`; IDs come from the canonical sidecar. `--type` accepts the
five chart types plus `diagram`, a compatibility alias for `workbench`. Both commands accept `--help`, send payloads to stdout
when `-o` is omitted, and return exit code 2 for invalid input or failed capture.

## API additions

All endpoints are POSTs using the existing same-origin token authentication.

| Endpoint | Request | Result |
| --- | --- | --- |
| `investigations/list` | `{}` | `items`: saved bundles |
| `investigations/save` | `value`: bundle, optional `id` | Saved bundle with `id`, `updated_at` |
| `investigations/delete` | `id` | `deleted` |
| `profiles/list`, `profiles/save`, `profiles/delete` | Same record pattern | Connection settings without credentials |
| `connection/test` | `connection` | `version`, visible `schemas` |
| `live/start` | `connection`, `query`, `parameters`, `mode`, `repeat`, `timeout`, `trace`, `conditions` | Job `id`, `state` |
| `live/status` | `id` | `state`, `progress`, `repeat`, `error`, optional `result` |
| `live/cancel` | `id` | Current job snapshot |

Paths have the `/api/` prefix. Existing `render` also accepts `selected_id`,
`focus_id`, `collapsed_ids`, and `metric`. Existing `analyze` returns parent/child
IDs and teaching hooks, and accepts captured schema/settings for analysis.
Comparison deltas add matching confidence/status and measurement-availability
flags; existing canonical node IDs and legacy summary fields remain available.
Consumers should honor `timing_available` before interpreting legacy timing
summary numbers. Unmeasured and uncertain classifications are explicit.
