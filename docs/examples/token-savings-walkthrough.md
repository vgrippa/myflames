# Real example: ask an AI to fix a slow query, for 4× fewer tokens

Every number here is **measured**, against a live MySQL 8.4 (Docker) and a real Claude API key. Reproduce it in ~30 seconds:

```bash
./docs/examples/run-live-example.sh    # needs Docker
```

It boots MySQL, seeds a small shop schema (3,000 users · 12,000 orders · 40,000 order_items · 1,500 products), runs the query below, captures its plan to [slow-query-plan.json](slow-query-plan.json), and prints the token comparison.

## The slow query

`orders.total` has no index, so MySQL scans every order, joins out, and sorts the result:

```sql
SELECT o.id, o.total, oi.quantity, p.name
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
JOIN products p     ON p.id = oi.product_id
WHERE o.total > 450
ORDER BY o.total DESC
LIMIT 50;
```

## Step 1 — get the plan from MySQL

```sql
SET explain_json_format_version = 2;
EXPLAIN ANALYZE FORMAT=JSON SELECT ... ;   -- the query above
```

…or let myflames pull it from a live server: `myflames -h host -u user -p -D shop -e "SELECT ..." > plan.json`. Either way you get **~5.5 KB of deeply nested JSON**.

## Step 2a — without myflames: paste the whole plan in

You copy the entire JSON blob, paste it into ChatGPT/Claude, and ask *"why is this slow, and how do I make it faster?"* That prompt is **2,110 tokens** measured on Claude (1,420 on GPT-4o). The model has to parse all the nesting before it can reason.

## Step 2b — with myflames: paste the digest

```bash
myflames tokens slow-query-plan.json --digest | pbcopy   # then paste
```

The digest is **521 tokens** (Claude) and already contains the diagnosis *and* the fix — real output:

```text
# Query plan analysis (myflames digest)
engine mysql | 9 ops | depth 6 | 1.819 ms | rows 50 sent / 12,004 examined

SUMMARY: Query scans 1 table and sorts the result; examines ~12,004 rows to
return 50 in 1.8 ms. Main finding: no index covers (total) on orders.

WARNINGS (2):
- [warn/full_scan] Full table scan: orders (12000 rows) (@ Table scan [orders])
- [warn/filesort] 1 sort operation(s) — 15 rows; may use disk-based filesort (@ Sort)

INDEXES:
- CREATE INDEX idx_orders_total ON orders (total);

PLAN:
Limit: 50 rows
`- Nested loop inner join
   |- Nested loop inner join
   |  |- Sort
   |  |  `- Filter: ((o.total > 450.00))
   |  |     `- Table scan [orders]
   |  `- Filter: ((oi.product_id is not null))
   |     `- Index lookup [order_items.idx_order]
   `- Single-row lookup [products.PRIMARY]
```

## Step 3 — same answer, 4× cheaper

We asked **Claude Opus 4.8 the question both ways** (raw plan vs digest). It gave the **same diagnosis and the same fix** either way. From the digest:

> The bottleneck is the first access path on `orders`: a **full table scan** — MySQL reads all ~12,000 rows because there's no index on `total`. You return 50 rows but examine 12,004, a ~240:1 read-to-return ratio. The surviving rows are then **filesorted**… The joins are fine (`order_items` uses `idx_order`, `products` uses its PRIMARY key). Fix: `CREATE INDEX idx_orders_total ON orders (total);`

| Measured with | Raw + question | Digest + question | Saving |
|---|--:|--:|--:|
| Claude Opus 4.8 (real API `count_tokens`) | 2,110 | 521 | **4.0× · 75% fewer** |
| GPT-4o / 4.1 / 5 (tiktoken) | 1,420 | 320 | **4.4× · 78% fewer** |
| Offline heuristic (`myflames tokens`) | 1,655 | 300 | 5.5× |

The whole live demo (two `count_tokens` calls + two `messages.create` calls on Opus 4.8) cost **about $0.05**. On Opus input pricing you save ~$0.008 per query (~$7.90 per 1,000); on Sonnet 4.6, ~$4.80 per 1,000 — plus output tokens and round-trips, since the answer is already in the digest.

> The offline heuristic (`myflames tokens`) is the zero-dependency default (no key, no network) and slightly over-counts JSON (5.5× vs the measured ~4×). For exact Claude counts, add `--exact` with **your own** key:
>
> ```bash
> pip install 'myflames[tokens]'
> export ANTHROPIC_API_KEY="sk-ant-api03-REPLACE-WITH-YOUR-OWN-KEY-0000000000000000000000000000"   # placeholder
> myflames tokens slow-query-plan.json --exact
> ```
>
> myflames reads `ANTHROPIC_API_KEY` from the environment and passes it to the Anthropic SDK; it never writes your key anywhere. Without it, `--exact` falls back to the estimate. `count_tokens` is free, so exact counts cost nothing.

## Even less effort: let the agent do it (MCP)

If you drive MySQL through an AI agent, you don't copy anything at all:

```bash
pip install 'myflames[mcp]'
claude mcp add myflames -- myflames-mcp
```

Ask *"why is this query slow?"* and the agent calls `analyze_plan` / `digest_plan` directly, reasoning over the 521-token digest instead of the 2,110-token raw plan.

## Clean up

```bash
docker rm -f myflames-live-example
```
