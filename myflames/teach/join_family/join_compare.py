"""Lesson: BNL vs hash join side-by-side with shared sliders.

Same query run by both algorithms -- customers x orders. The confusing
'10.10B total' label has been replaced with 'row-pair comparisons' and
the '...and N more blocks' footer now explicitly says what the additional
blocks would do.

Concrete sample data: the BNL panel shows named customers packed into
blocks scanning named orders. The hash panel shows customers hashed
into buckets by month, then orders probed through -- following the same
pattern as hash_join.py.
"""
from .. import _html
from .._cost_model import JOIN_BUFFER_SIZE_DEFAULT, MYSQL_BNL_REMOVED_IN


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = f"""
<section class="controls">
  <h2>Shared parameters — both panels update together</h2>
  <div class="control-grid">

    <div class="control">
      <label for="outer_rows"><code>customers</code> rows: <span class="value-pill" data-pill-for="outer_rows">50000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="1000" max="5000000" step="1000" value="50000">
    </div>

    <div class="control">
      <label for="inner_rows"><code>orders</code> rows: <span class="value-pill" data-pill-for="inner_rows">200000</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="1000" max="10000000" step="1000" value="200000">
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
    </div>

    <div class="control">
      <label for="jbs">join_buffer_size (bytes): <span class="value-pill" data-pill-for="jbs">262144</span></label>
      <input type="range" id="jbs" name="jbs" min="8192" max="16777216" step="8192" value="{JOIN_BUFFER_SIZE_DEFAULT}">
      <div class="hint">Default is 256 KiB in both engines.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- The same non-indexed join run by both engines\n"
            "SELECT c.country, SUM(o.total) AS revenue\n"
            "FROM   customers c\n"
            "JOIN   orders    o  ON  c.signup_month = o.signup_month\n"
            "GROUP  BY c.country;   -- no index on signup_month"
        ),
        note="MariaDB 11.x runs this with BNL (join_cache_level=2). MySQL 8.4 runs it with a two-phase hash join. Same SQL, very different cost."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left panel — MariaDB BNL: customers rows pack into blocks; a yellow sweep bar crosses the orders table once per block. One sweep = one full scan of orders.",
            "Right panel — MySQL 8.4 hash join: orange build-tuples fly into hash buckets (phase 1), then teal probe-tuples fly into those buckets (phase 2). Only one pass of each side.",
            "Both panels run the same input in parallel so you can feel the asymptotic difference — BNL grows quadratically with customers, hash grows linearly.",
            "Each panel has its own row-pair comparison counter. The ratio is shown above as 'Speedup (hash vs BNL)'. At small sizes it is close to 1×; crank the sliders and watch it grow.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <div style="flex:1;min-width:0;display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start">
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#92400e;letter-spacing:0.4px;text-transform:uppercase">MariaDB 11.x BNL</p>
        <svg id="svg-bnl" viewBox="0 0 400 240" xmlns="http://www.w3.org/2000/svg"></svg>
        <p style="margin:4px 0 0;font-size:11px;color:#6b7280" id="bnl-phase">Idle</p>
      </div>
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#1e40af;letter-spacing:0.4px;text-transform:uppercase">MySQL 8.4 hash join</p>
        <svg id="svg-hash" viewBox="0 0 400 240" xmlns="http://www.w3.org/2000/svg"></svg>
        <p style="margin:4px 0 0;font-size:11px;color:#6b7280" id="hash-phase">Idle</p>
      </div>
    </div>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost comparison — row-pair comparisons</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">BNL row-pair comparisons {ht("Total (outer row, inner row) pairs checked by the BNL algorithm. Grows with blocks × inner_rows. This is the number that makes BNL expensive at scale.")}</p><p class="value" id="bnl-cmp">—</p></div>
    <div class="item"><p class="label">Hash row comparisons {ht("Total rows processed by the hash join: one read of the build side + one read of the probe side. Grows linearly — much flatter than BNL.")}</p><p class="value ok" id="hash-cmp">—</p></div>
    <div class="item"><p class="label">Speedup (hash vs BNL) {ht("How many times fewer row-pair comparisons the hash join needs compared to BNL. At large scale this is 100× to 10,000× — the whole reason MySQL removed BNL.")}</p><p class="value" id="speedup">—</p></div>
    <div class="item"><p class="label">BNL complexity {ht("BNL re-scans orders once per block of customers. The more blocks, the more rescans. Quadratic-ish growth.")}</p><p class="value">O(customers · orders / buffer) = O(n·m/b)</p></div>
    <div class="item"><p class="label">Hash complexity {ht("One pass through customers (build) + one pass through orders (probe). Linear growth no matter the size.")}</p><p class="value">O(customers + orders) = O(n + m)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Row-pair comparisons vs customers rows (log–log, orders fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = f"""
<details class="learn-more">
  <summary>Learn more — isn't MariaDB's "hash join" the same thing?</summary>
  <div class="body">
    <p><strong>No.</strong> MariaDB 11.x can use <code>join_cache_level = 4</code>
    for "hashed BNL" — which is still structurally a Block Nested Loop.
    Each outer block builds a small hash table and the inner is scanned
    once per block. It's faster than plain BNL but still
    <code>O(outer_blocks · inner_rows)</code>.</p>

    <p>MySQL 8.4's hash join (and PostgreSQL's, and most analytics
    engines') is a two-phase algorithm: <em>build</em> a single in-memory
    hash table from the smaller input, then stream the larger input
    through once. That's <code>O(build + probe)</code>. Hash join has
    existed in MariaDB in a limited form since 10.4 but is not the
    default, and its heuristics are different from MySQL's.</p>

    <p>Takeaway: when you see "hash join" in a MariaDB EXPLAIN, check
    which <code>join_cache_level</code> is active. In MySQL 8.4 there's
    only one kind — <strong>BNL is gone</strong> (removed in
    {MYSQL_BNL_REMOVED_IN}).</p>

    <p>Sources: MariaDB Knowledge Base "Block-based Join Algorithms",
    "Hash Join Support". MySQL 8.4 Reference Manual §10.2.1.4.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="join",
        title="BNL vs hash join — side by side",
        subtitle=(
            "Move the sliders and feel the asymptotic difference between "
            "MariaDB's Block Nested Loop and MySQL 8.4's hash join."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
