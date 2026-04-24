"""Lesson: Unique Key Lookup (single-row index lookup).

Shares the visual vocabulary of the flagship ``btree`` lesson: a real
B+tree (root / internal / leaf pages) with a red diamond query token
descending level by level, yellow page-pulses on arrival, and a dashed
orange PK-hop arrow when the lookup is non-covering. The UNIQUE
special-case is called out by a small chip that makes it obvious that
exactly one leaf entry can match — not a range.
"""
from .. import _html
from .._cost_model import (
    INNODB_PAGE_OVERHEAD_BYTES,
    INNODB_CHILD_POINTER_BYTES,
    INNODB_PAGE_SIZE_DEFAULT,
)


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (single-row unique lookup)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="rows">Rows in <code>users</code> table: <span class="value-pill" data-pill-for="rows">1000000</span></label>
      <input type="range" id="rows" name="rows" min="0" max="6" step="1" value="3">
      <div class="hint">Logarithmic: 1K, 10K, 100K, 1M, 10M, 100M, 1B</div>
    </div>

    <div class="control">
      <label for="covering">Covering unique index?</label>
      <select id="covering" name="covering">
        <option value="false" selected>No — one clustered-row fetch (PK hop)</option>
        <option value="true">Yes — index-only, no table fetch</option>
      </select>
      <div class="hint">Covering removes the final table-row read — watch the PK-hop arrow disappear.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Single-row lookup on a UNIQUE key\n"
            "SELECT id, email\n"
            "FROM   users\n"
            "WHERE  id = 42;   -- uq_users_id guarantees at most 1 row"
        ),
        note=(
            "In EXPLAIN this appears as Single-row index lookup (eq_ref / const): "
            "descend the B+tree to exactly one leaf entry; non-covering plans "
            "then do one clustered-row fetch."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The grey rectangles are B+tree pages of the UNIQUE index (uq_users_id). Top row is the root page, bottom row is the leaf.",
            "A red diamond 'query token' appears above the root and descends level by level — each step is one page read.",
            "When the token arrives at a page, the page pulses yellow. That's 'page is in the buffer pool now'.",
            "Because the index is UNIQUE, the leaf stores at most ONE matching entry for id=42 — a small chip next to the leaf says so. No range scan, no sibling-page walk.",
            "Covering mode: the leaf already has every column the query asked for, an 'Index-only' badge glows on the leaf, and the lookup stops there.",
            "Non-covering mode: the leaf stored the PK. A dashed orange PK-hop arrow appears to the clustered tree and the token rides that arrow across to fetch the full row.",
        ],
    )

    stage_html = f"""
<section class="stage" aria-labelledby="stage-h">
  <h2 id="stage-h" class="sr-only" style="position:absolute;left:-9999px">Unique key lookup animation</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Adjust parameters, then press Play")}
  <div class="stage-with-phases">
    <svg id="unique-svg" viewBox="0 0 800 400" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (single-row unique lookup)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Tree height {ht("Number of B+tree levels from root to leaf. InnoDB's high fan-out keeps this tiny: a billion-row table is only 4 levels deep.")}</p><p class="value" id="out-height">—</p></div>
    <div class="item"><p class="label">Index reads {ht("Pages touched while traversing the UNIQUE index to the matching entry. Equals tree height.")}</p><p class="value" id="out-index">—</p></div>
    <div class="item"><p class="label">Row fetches {ht("Clustered table-row fetches after the index hit. 0 if covering, 1 if non-covering.")}</p><p class="value" id="out-fetch">—</p></div>
    <div class="item"><p class="label">Total work (pages) {ht("Index reads + row fetches. O(log n) covering, O(log n + 1) non-covering.")}</p><p class="value" id="out-total">—</p></div>
  </div>
  <div class="explanation" id="out-exp"></div>
  <div class="complexity-chart">
    <p class="chart-title">Unique lookup vs full scan (log–log, InnoDB fan-out ≈ 800)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — unique vs non-unique lookup</summary>
  <div class="body">
    <p><strong>Unique lookup</strong> returns at most one row for a key value.
    MySQL's optimizer labels this access path <code>const</code> or
    <code>eq_ref</code>, and EXPLAIN ANALYZE prints
    <em>Single-row index lookup</em>. Because the upper bound is 1, the planner
    can skip a bunch of bookkeeping that range scans need — there's no
    sibling-leaf walk, no "stop when the next key changes" check, no MRR.</p>

    <p><strong>Non-unique lookup</strong> (see the sibling lesson) can match
    many rows for one value. That means a range of leaf entries on the
    secondary tree, and potentially many clustered-row fetches when
    non-covering. The I/O scales with the number of matches — not just with
    <code>log n</code>.</p>

    <p><strong>Covering unique index</strong> — when every column in the
    SELECT list is stored in the unique index's leaf, the lookup never
    touches the clustered B+tree. This is the fastest shape of point read
    that InnoDB can do: a single descent of <code>O(log n)</code> pages.</p>

    <p>Sources: MySQL 8.4 Reference Manual §8.2.1.1 (WHERE clause optimization),
    §17.6.2 (InnoDB Indexes).</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % (
        INNODB_PAGE_OVERHEAD_BYTES,
        INNODB_CHILD_POINTER_BYTES,
        INNODB_PAGE_SIZE_DEFAULT,
    )

    return _html.render_page(
        lesson_id="unique_lookup",
        title="Unique Key Lookup — single-row index lookup",
        subtitle=(
            "Descend a UNIQUE B+tree to exactly one leaf entry; if the "
            "index is non-covering, take one PK-hop to the clustered row."
        ),
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
