# Learn the algorithms

Learn explains query-engine algorithms through guided animations. A lesson advances one idea at a time, with simplified code beside the drawing and a short Check yourself quiz at the end.

## Choose an algorithm family

- **Joins:** nested loops, hash joins, block nested loops, batched key access and semijoin strategies.
- **Indexes:** B-tree lookup, index condition pushdown, index merge, skip scan and rowid filtering.
- **Scans and sorting:** full scans, filters, filesort, temporary tables, derived tables and covering indexes.
- **Caching:** LRU and buffer pool warmup.

Use Back and Next step to follow the explanation. Explore lets you move across the lesson steps and adjust inputs. Replay step shows the current step again.

## Build intuition, then return to evidence

The diagrams and simplified code are teaching models. Server behavior depends on the engine, version, optimizer choices and data. A lesson helps explain an operator; use captured plans and live evidence to judge a specific query.

Some lessons expose engine-specific behavior. For example, Filesort has a MySQL/MariaDB input, and Buffer pool warmup distinguishes their dump rules.
