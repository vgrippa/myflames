# Learn the algorithms

**Learn** explains database execution algorithms through 23 guided interactive lessons, 16 of them arranged in a recommended learning path. Each lesson advances one idea at a time, highlighting lines in a **Simplified** code panel (with verified MySQL function names where applicable) beside the animation and ending with a short **Check yourself** quiz.

## Choose an algorithm family

Use **Search lessons**, filter by **Family** (**All families** by default), or choose **Start learning** to begin at Lesson 1 of the learning path:

- **Scan, sort and temp family**: full table scan, filter operator, filesort, in-memory and on-disk temporary tables, derived table materialization and covering index access.
- **Index access family**: InnoDB B+tree lookup, unique and non-unique key lookups, index condition pushdown (ICP), index merge, skip scan and rowid filter.
- **Join family**: nested loop join, hash join (including grace-hash spill), block nested loop (BNL), BNL versus hash join side by side, batched key access (BKA), semijoin FirstMatch and semijoin duplicate weedout.
- **Planner and optimizer family**: join-order search, pruning (`optimizer_prune_level`) and hypergraph join planning.
- **Cache and memory family**: InnoDB buffer pool midpoint-insertion LRU and buffer pool warmup (dump and load).

Inside a lesson, use **Back** and **Next step** to step through the explanation, or **Play** to run the remaining steps. **Explore** adds a step slider, **Replay step**, and **Inputs** when the lesson exposes tunable parameters. In the lesson toolbar, **Previous lesson** and **Next lesson** move along the learning path, **All lessons** returns to the catalog (and **Back to plan** returns when you opened the lesson from **Plans**), **Download lesson** saves a standalone `.html` copy, and **Full screen** expands the animation (**Exit full screen** or `Esc` leaves it). Quiz answers in **Check yourself** stay in the page; nothing is saved or sent.

## Build intuition, then return to evidence

The animations and **Simplified** code are teaching models. Real execution depends on your engine, release, optimizer switches, table statistics and data distribution: use a lesson to understand how an operator works, then verify behavior with captured plans and live server metrics.

Several lessons model engine differences directly. For example, the Filesort lesson has a **Server** input to compare MySQL's comparison sort (`256 KiB` default `sort_buffer_size`) with MariaDB's radix sort on short keys (`2 MiB` default `sort_buffer_size`), and the Buffer pool warmup lesson contrasts their buffer-pool dump defaults.
