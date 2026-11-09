"""Lesson: Nested Loop Join — flagship-quality rewrite.

EXPLAIN shows ``Nested loop`` whenever MySQL's classical join iterator
runs: pick one outer row, probe the inner side, emit matches, repeat.
Verified in ``sql/iterators/composite_iterators.h:318`` —
``NestedLoopIterator`` "may scan the inner iterator many times" by design.

Upgrade from the prior three-phase state-swap animation to flagship
btree vocabulary (Slice 3 / A1 + A2 + T2):

* **A1 — tween-based arrivals**: outer-row highlight transitions via
  ``anim.tween`` + ``easeOutCubic`` + ``anim.arrival`` pulse instead
  of a bare ``setAttribute`` swap.
* **A2 — arc'd probe pills**: each matching ``order_id`` spawns a
  labelled pill ``<g>`` at the outer row, arcs to the inner panel via
  ``anim.path`` with ~80 ms stagger, then lands with a pulse. The
  pedagogical point (driver → probe tuple flow) is now *visible*
  frame-to-frame.
* **T2 — match verdict pills**: each outer row's status line names
  the match count ("Acme id=1 → 2 orders ✓" / "Globex id=2 → 1 order ✓").
  The consequence line ties the observed cost back to "inner side
  indexed or not" so the takeaway lands.

Uses the shared A5 ``anim.arrival`` primitive so pulses look identical
to btree / hash / unique_lookup. Reduced-motion respected via the
helper's built-in ``reducedMotion()`` gate.
"""
from .. import _html


_LESSON_JS = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). nested_loop.js marks reading each
# outer row, its probe of orders, and the summary with tl.beat(kind). The
# function name was checked in mysql-server (8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Nested loop join",
        "lines": [
            ("outer", 0, "for each row c from customers (the outer input):",
             "NestedLoopIterator::Read"),
            ("probe", 1, "start a probe of orders where customer_id = c.customer_id"),
            ("inner", 1, "for each matching row o from orders:"),
            ("emit", 2, "return the joined row (c.customer_id, o.order_id)"),
            ("end", 0, "no customers left: the join is done"),
        ],
    },
    "beats": {
        "outer-row": ["outer"],
        "probe": ["probe", "inner", "emit"],
        "done": ["end"],
    },
    "quiz": [
        {
            "question": ("The outer input returns 5 customers and each has about 2 orders. How "
                         "many times does the nested loop start a probe of orders?"),
            "choices": [
                ("5 times, once per customer", True,
                 "A nested loop starts a new probe of the inner input for every row the "
                 "outer input returns."),
                ("Once, for all customers together", False,
                 "Reading the inner input once for many outer rows is what a hash join or "
                 "a block nested loop does; a plain nested loop probes once per outer row."),
                ("10 times, once per order", False,
                 "Orders are what each probe returns; the probes are counted per customer."),
            ],
        },
        {
            "question": "Why does an index on orders(customer_id) matter so much here?",
            "choices": [
                ("Each probe becomes a short index lookup instead of a read of all orders", True,
                 "Without it a plain nested loop would read every order for each customer, "
                 "which is why MySQL then uses a hash join and MariaDB a block nested loop "
                 "by default."),
                ("It sorts customers so the outer loop runs fewer times", False,
                 "The outer loop still runs once per customer; the index only makes each "
                 "probe cheaper."),
                ("It removes the need to read customers", False,
                 "The customers rows still drive the loop; the index only speeds up "
                 "finding their orders."),
            ],
        },
        {
            "question": ("Both tables have the join column indexed. Which table should usually "
                         "drive the loop?"),
            "choices": [
                ("The one with fewer rows left after its WHERE filter", True,
                 "Every driving row costs one probe, so fewer driving rows means fewer "
                 "probes, and both optimizers estimate this for you."),
                ("Always the first table written in FROM", False,
                 "MySQL and MariaDB reorder inner joins by estimated cost unless you force "
                 "the order, for example with STRAIGHT_JOIN."),
                ("The larger table, so each probe finds more rows", False,
                 "More driving rows means more probes, and the probes are the cost that grows."),
            ],
        },
    ],
}


def render() -> str:
    controls_html = """
<section class="controls">
  <h2>Join shape controls</h2>
  <div class="control-grid">
    <div class="control">
      <label for="outer_rows">Outer rows (customers): <span class="value-pill" data-pill-for="outer_rows">50000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="1000" max="5000000" step="1000" value="50000">
      <div class="hint">The driving side: each of these rows triggers an inner probe.</div>
    </div>
    <div class="control">
      <label for="inner_rows">Inner rows per probe (orders): <span class="value-pill" data-pill-for="inner_rows">8</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="1" max="200" step="1" value="8">
      <div class="hint">Average orders rows checked for each customer row.</div>
    </div>
  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "SELECT c.customer_id, o.order_id\n"
            "FROM   customers c\n"
            "JOIN   orders o ON o.customer_id = c.customer_id\n"
            "WHERE  c.country = 'US';"
        ),
        note=(
            "This lesson isolates the Nested loop operator: one outer "
            "row becomes the driver, match pills arc from the outer row "
            "into the probe panel, then the next driver takes over. "
            "It's exactly what EXPLAIN's Nested loop node does at runtime."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left side = outer (driving) customers. Orange = currently "
            "driving the join.",
            "For each driver, <strong>match pills</strong> spawn at the "
            "outer row and arc into the probe panel — one per matching "
            "inner row. That arc IS the tuple flow — the point of the "
            "operator.",
            "Bottom-right verdict names the driver and its match count "
            "('Acme id=1 → 2 orders ✓').",
            "Cost scales with outer_rows × inner_rows_per_probe. "
            "Indexed inner = tiny per-probe cost; un-indexed inner = "
            "full-scan per driver and cost explodes.",
            "Next step shows one outer row, then its probe; the simplified "
            "code beside the drawing highlights the lines each step runs.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="nlj-svg" viewBox="0 0 800 380" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Nested loop cost model</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Outer rows {ht("Rows from the driving side of the join. The nested loop runs once per outer row.")}</p><p class="value" id="out-outer">—</p></div>
    <div class="item"><p class="label">Inner rows per probe {ht("Average rows checked on the inner side for each outer row.")}</p><p class="value" id="out-inner">—</p></div>
    <div class="item"><p class="label">Row-pair comparisons {ht("Approximate work for this operator: outer_rows × inner_rows_per_probe.")}</p><p class="value" id="out-cmp">—</p></div>
    <div class="item"><p class="label">Complexity {ht("Nested loop cost grows multiplicatively with both inputs.")}</p><p class="value">O(n · m)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Nested loop growth curve (log-log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — when Nested loop is fast and when it isn't</summary>
  <div class="body">
    <p>The iterator itself is simple — one outer row, one inner probe,
    repeat. The difference between fast and catastrophic is <em>what
    inner_rows_per_probe looks like</em>:</p>

    <ul>
      <li><strong>Indexed inner side</strong> (eq_ref / ref access):
      inner_rows_per_probe ≈ 1. Total work is ~linear in outer rows —
      this is the fast path and exactly what "add an index on the join
      column" achieves.</li>

      <li><strong>Un-indexed inner side</strong> (type=ALL): the inner
      side is <em>re-scanned</em> per outer row, so
      inner_rows_per_probe = total inner rows. Work grows as
      outer × inner — the curve on the chart above is the pain.</li>
    </ul>

    <p>MySQL 8.0.20+ rewrites this at <em>execution time</em> into a
    hash join when no usable index exists (see
    <code>sql/sql_executor.cc:~2891</code>
    <code>replace_with_hash_join</code>). Which is why you'll see
    plans labelled "Nested loop" that are actually running as hash
    join under the hood — the EXPLAIN string is descriptive of the
    optimizer's decision, not always the executor's behaviour.</p>

    <p>Source: <code>sql/iterators/composite_iterators.h:318</code>
    — <code>NestedLoopIterator</code>. "Currently the only form of
    join we have" (at the logical level).</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="nested_loop",
        title="Nested loop join: outer row drives inner probe",
        subtitle=(
            "Dedicated operator view for EXPLAIN's Nested loop nodes. "
            "Watch each driver fire match pills into the probe panel — "
            "the flow that makes the operator's cost visible."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
        guide=GUIDE,
    )
