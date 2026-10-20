# myflames 3D lesson views

3D views of the myflames teach lessons, offered as an extra option beside the
2D lesson (which stays the default and is never replaced). Today: the B+tree
lesson. The 3D view is inspired by [PGSimCity](https://github.com/NikolayS/PGSimCity)
by Nikolay Samokhvalov; no PGSimCity code is used. myflames itself is inspired
by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs.

## What it shows

The `users` clustered index (and, in chapter 3, `idx_users_email`) at its real
size: every tile is one InnoDB page, a page's children sit in a block under
it, and the tree spreads out from one root tile to thousands of leaf tiles.

1. **Shape**: the levels appear one at a time, with one page's pointers drawn.
2. **Primary key lookup**: `WHERE id = 42`, one page read per level.
3. **Secondary index**: `WHERE email = ...` down `idx_users_email`, then the
   primary key is carried to the table's root for a second descent (or, with
   the covering switch, the lookup stops at the index leaf).
4. **Range scan**: `id BETWEEN 42 AND 4000`, root to leaf, then along the
   leaf level's next-page links.

The rows, row size, email size, and page size controls rebuild the tree.
Levels with more than 20,000 pages are drawn as one block per parent page; the
blocks the query reads open into single pages. Without WebGL the page keeps
its narration and readout and says so.

## Where the numbers come from

Python is the source of truth: `innodb_index_shapes` and `leaf_range_pages`
in `myflames/teach/_cost_model.py`. `src/model.ts` mirrors them, and
`scripts/export-btree-shape.py` (repository root) writes
`test/fixtures/btree-shape.json` with every combination the controls offer.
After changing the Python model, rerun that script, then `npm test`.

## Build

```bash
npm ci
npm test          # model.ts against the Python fixture
npm run build     # writes ../../myflames/assets/teach3d-btree.html
npm run test:smoke  # build, then check the page in headless Chrome
```

`npm run build` runs `tsc`, builds `src/main.ts` into one IIFE with Vite, and
`scripts/assemble.mjs` inlines it and `src/style.css` into `src/page.html`.
The result is one offline file that also works inside the workspace's
sandboxed lesson frame (CSP `default-src 'none'; script-src 'unsafe-inline'`).
It keeps three.js's MIT license notice and records a
`myflames-sources-sha256` meta tag; `test/test_teach.py` recomputes the hash
and fails when the committed page is stale. Commit the rebuilt page with the
source change. Python users never run Node.

`test:smoke` saves screenshots to `$SHOTS` (default: a temporary folder). It
launches Chrome with `headless: "shell"`, like the workspace suites.

## Files

| File | Role |
| --- | --- |
| `src/model.ts` | Page counts per level, fan-out, records per page, range pages (mirrors Python) |
| `src/scene.ts` | three.js stage: tree layout, level of detail, token, paths, labels, camera |
| `src/timeline.ts` | Seekable timeline and player (play, pause, speed, scrubber, marks, reduced motion) |
| `src/chapters.ts` | The four chapters and their narration |
| `src/main.ts` | Controls, chapter strip, toolbar, readout |
| `src/page.html`, `src/style.css` | Page shell and styles (same tokens as the 2D lessons) |
