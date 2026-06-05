# Real example: the token saving, step by step

Everything below is **real output** from a live MySQL 8.4 (in Docker), not a mock-up. Reproduce it yourself in ~30 seconds:

```bash
./docs/examples/run-live-example.sh    # needs Docker
```

It boots MySQL, seeds a small shop schema (3,000 users · 12,000 orders · 40,000 order_items · 1,500 products), runs the query below, and prints the comparison. The captured plan lives at [orders-revenue-plan.json](orders-revenue-plan.json).

## The query you think is slow

A normal analytics query — top 20 products by revenue among shipped orders:

```sql
SELECT p.name, SUM(oi.quantity * oi.unit_price) AS revenue
FROM order_items oi
JOIN orders o   ON o.id = oi.order_id
JOIN products p ON p.id = oi.product_id
WHERE o.status = 'shipped'
GROUP BY p.id
ORDER BY revenue DESC
LIMIT 20;
```

## Step 1 — get the plan from MySQL

As a human you run one of these:

```sql
-- in the mysql client:
SET explain_json_format_version = 2;
EXPLAIN ANALYZE FORMAT=JSON SELECT ... ;   -- (the query above)
```

…or let myflames pull it for you against a live server:

```bash
myflames -h db.example.com -u admin -p -D shop \
  -e "SELECT p.name, SUM(oi.quantity*oi.unit_price) ..." > plan.json
```

Either way you get **169 lines / ~7.6 KB of deeply nested JSON**. The first dozen lines already show the problem — it's all structure, no answer:

```json
{
    "limit": 20,
    "query": "/* select#1 */ select `p`.`name` ... limit 20",
    "inputs": [
        { "inputs": [
            { "inputs": [
                { "inputs": [
                    { "inputs": [
                        { "alias": "o",
                          "covering": true,
                          "operation": "Covering index lookup on o using idx_status (status='shipped')",
                          ...
```

## Step 2a — the usual way: paste the whole plan into your AI

You select the entire JSON blob, paste it into ChatGPT/Claude, and type *"why is this query slow, and how do I make it faster?"* That prompt is about **2,182 tokens** — and the model still has to parse all that nesting before it can think.

## Step 2b — the myflames way: paste the digest instead

```bash
myflames tokens orders-revenue-plan.json --digest | pbcopy   # now paste
```

The digest is **~335 tokens** and already contains the diagnosis (a temp-table scan from the `GROUP BY`, plus a filesort) and the fixes — this is the real output:

```
# Query plan analysis (myflames digest)
engine mysql | 11 ops | depth 8 | 12.049 ms | rows 20 sent / 2,404 examined

SUMMARY: Query scans 1 table and sorts the result; examines ~2,404 rows to
return 20 in 12 ms. Main finding: full scan of <temporary> (300 rows).

WARNINGS (2):
- [warn/full_scan] Full table scan: <temporary> (300 rows) (@ Table scan [<temporary>])
- [warn/filesort] 1 sort operation(s) — 20 rows; may use disk-based filesort (@ Sort (limit 20))

SUGGESTIONS (2):
- [high/index] Add indexes on filter/join columns to avoid full table scans
- [medium/tuning_variable] Increase sort_buffer_size or add an ordered index to avoid filesort

OPTIMIZER_SWITCHES: use_index_extensions=on

PLAN:
Limit: 20 rows
`- Sort (limit 20)
   `- Table scan [<temporary>]
      `- Aggregate
         `- Nested loop inner join
            |- Nested loop inner join
            |  |- Filter: ((o.status = 'shipped'))
            |  |  `- Covering index [orders.idx_status]
            |  `- Filter: ((oi.product_id is not null))
            |     `- Index lookup [order_items.idx_order]
            `- Single-row lookup [products.PRIMARY]
```

## Step 3 — see the saving

```bash
myflames tokens orders-revenue-plan.json
```

```
  What you'd paste                          Tokens     Cost (Sonnet 4.6 input)
  BEFORE  raw plan JSON + your question      2,182     $0.0065
  AFTER   myflames digest + your question      335     $0.0010
  ------------------------------------------------------------
  SAVED                                      1,847     $0.0055   (84.6% fewer, 6.5x smaller)
```

On this small, realistic plan it's **6.5× fewer tokens**; on a complex multi-join plan it's **10×+** (see the README). Bigger and messier plans — the ones you actually need help with — save the most. Counts are an offline estimate; add `--exact` for real Claude tokens (`pip install myflames[tokens]` + `ANTHROPIC_API_KEY`).

## Even less effort: let the agent do it for you (MCP)

If you drive MySQL through an AI agent (Claude Code, Cursor), you don't copy anything at all. Register the server once:

```bash
pip install 'myflames[mcp]'
claude mcp add myflames -- myflames-mcp
```

Now when you ask your agent *"why is this query slow?"*, it calls the `analyze_plan` / `digest_plan` tool directly and reasons over the 335-token digest — never the 2,182-token raw plan.

## Clean up

```bash
docker rm -f myflames-live-example
```
