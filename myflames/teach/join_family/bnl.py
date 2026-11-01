"""Lesson: Block Nested Loop join (MariaDB 11.x default).

Real-world join: `customers × orders`. Polished animation with curved
tuple paths, shared toolbar, query card, explainer, and complexity
chart. Labels clearly say "row-pair comparisons", not the confusing
"rows examined" bare number.

Concrete sample data: 5 named customers and 8 orders with visible
labels, block contents, and match highlights — following the same
pattern as hash_join.py.
"""
from .. import _html
from .._cost_model import JOIN_BUFFER_SIZE_DEFAULT, MYSQL_BNL_REMOVED_IN


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). bnl.js marks filling the join
# buffer with a block, the scan of orders for that block, and the summary
# with tl.beat(kind). Block nested loop is MariaDB's executor (MySQL 8.0.20
# and later have none), so no line carries a MySQL function note.
GUIDE = {
    "code": {
        "title": "Block nested loop (MariaDB)",
        "lines": [
            ("block", 0, "while customers has rows left:"),
            ("fill", 1, "fill the join buffer with the next customers rows that fit"),
            ("scan", 1, "for each row o in orders (one full scan per block):"),
            ("each", 2, "for each customer c in the join buffer:"),
            ("test", 3, "if c.signup_month = o.signup_month: return (c, o)"),
            ("end", 0, "no customers left: the join is done"),
        ],
    },
    "beats": {
        "fill": ["block", "fill"],
        "scan": ["scan", "each", "test"],
        "done": ["end"],
    },
    "quiz": [
        {
            "question": ("customers fills 3 blocks of the join buffer. How many full scans of "
                         "orders does the block nested loop make?"),
            "choices": [
                ("3, one per block", True,
                 "Each time the join buffer is full, MariaDB reads orders from start to end and "
                 "compares every order with every customer in the buffer."),
                ("1, however many blocks there are", False,
                 "One read of orders is what a hash join needs; a block nested loop reads orders "
                 "again for every block."),
                ("One per customer", False,
                 "That is a plain nested loop without an index; packing customers into blocks "
                 "cuts the scans to one per block."),
            ],
        },
        {
            "question": "You double join_buffer_size from 256 KiB to 512 KiB. What changes?",
            "choices": [
                ("About half as many blocks, so orders is scanned about half as often", True,
                 "Twice the buffer holds about twice the customers rows, so customers splits into "
                 "about half as many blocks, and each block costs one scan of orders."),
                ("MariaDB switches to a hash join", False,
                 "The algorithm comes from join_cache_level, not from the buffer size; a bigger "
                 "buffer only packs more customers into each block."),
                ("Nothing, since orders is the table being scanned", False,
                 "orders is scanned once per block, so fewer and larger blocks mean fewer scans."),
            ],
        },
        {
            "question": "Which engine runs this join as a block nested loop by default?",
            "choices": [
                ("MariaDB; MySQL 8.4 runs it as a hash join", True,
                 "MariaDB's default join_cache_level=2 uses block nested loop buffers for joins "
                 "without a usable index, and MySQL replaced block nested loop with hash join "
                 "in 8.0.20."),
                ("Both engines", False,
                 "Since 8.0.20 MySQL runs the joins it used to buffer this way as a hash join "
                 "instead."),
                ("Only MySQL", False,
                 "It is the other way round: MySQL replaced block nested loop in 8.0.20, while "
                 "MariaDB still uses it at the default join_cache_level=2."),
            ],
        },
    ],
}


def render() -> str:
    banner_html = f"""
<div class="banner">
  <strong>Heads up:</strong> BNL is <strong>not used by MySQL 8.4</strong> —
  MySQL {MYSQL_BNL_REMOVED_IN} removed it in favour of hash join for non-indexed
  equi-joins. This lesson shows <strong>MariaDB 11.x</strong>, where BNL is
  still the default (<code>join_cache_level = 2</code>). Compare it with hash
  join in the <a href="join.html">BNL vs hash</a> lesson.
</div>
"""

    controls_html = f"""
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (MariaDB 11.x Block Nested Loop)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="outer_rows"><code>customers</code> rows: <span class="value-pill" data-pill-for="outer_rows">10000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Rows from the outer (driving) table.</div>
    </div>

    <div class="control">
      <label for="inner_rows"><code>orders</code> rows: <span class="value-pill" data-pill-for="inner_rows">50000</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="100" max="10000000" step="100" value="50000">
      <div class="hint">Rows in the inner table. Re-scanned once per outer block.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Size of one <code>customers</code> row in the join buffer.</div>
    </div>

    <div class="control">
      <label for="jbs">join_buffer_size (bytes): <span class="value-pill" data-pill-for="jbs">262144</span></label>
      <input type="range" id="jbs" name="jbs" min="8192" max="16777216" step="8192" value="{JOIN_BUFFER_SIZE_DEFAULT}">
      <div class="hint">MariaDB 11.4 default is {JOIN_BUFFER_SIZE_DEFAULT} B (256 KiB).</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Non-indexed join executed by MariaDB 11.x BNL\n"
            "SELECT c.country, SUM(o.total) AS revenue\n"
            "FROM   customers c\n"
            "JOIN   orders    o  ON  c.signup_month = o.signup_month\n"
            "GROUP  BY c.country;   -- no index on signup_month → BNL"
        ),
        note="BNL kicks in whenever the ON clause has no usable index on the inner side."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Yellow blocks on top = chunks of customers rows packed into the join buffer (one block holds as many rows as join_buffer_size allows).",
            "Blue table below = the orders table. It is a full table — every row lives here.",
            "Labelled pills ('Acme Corp (Jan)') flow from the active block down above the orders table: the customers rows now in the join buffer, each one compared with every order.",
            "A yellow sweep bar moves left-to-right across the orders table. That sweep is one full scan of orders.",
            "Each block triggers exactly one full scan of orders. 10 blocks = 10 full scans. That is why bigger join_buffer_size → fewer blocks → less work.",
            "Next step fills the buffer with one block, then scans orders once for it; the simplified code beside the drawing highlights the lines each step runs.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="bnl-svg" viewBox="0 0 800 360" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (MariaDB 11.x BNL)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">customers per block {ht("How many outer rows fit in one join_buffer_size chunk. Bigger buffer = more rows per block = fewer blocks = fewer inner rescans.")}</p><p class="value" id="out-rpb">—</p></div>
    <div class="item"><p class="label">Blocks {ht("The outer table is split into this many blocks. Each block triggers one complete re-scan of the inner table.")}</p><p class="value" id="out-blocks">—</p></div>
    <div class="item"><p class="label">Inner re-scans of orders {ht("The orders table is read from disk (or buffer pool) this many times — once per outer block. This is the main cost driver of BNL.")}</p><p class="value" id="out-scans">—</p></div>
    <div class="item"><p class="label">Row-pair comparisons {ht("Total number of (outer row, inner row) pairs compared. For BNL this is blocks × inner_rows × rows_per_block. Grows fast!")}</p><p class="value" id="out-cmp">—</p></div>
    <div class="item"><p class="label">Complexity {ht("BNL re-scans orders once per block of customers. Doubling join_buffer_size halves the blocks and the rescans.")}</p><p class="value" id="out-complexity">O(customers · orders / buffer) = O(n·m/b)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Row-pair comparisons vs customer rows (log–log, orders fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = f"""
<details class="learn-more">
  <summary>Learn more — why does MariaDB still use BNL?</summary>
  <div class="body">
    <p>MariaDB controls block-based join algorithms mainly with
    <code>join_cache_level</code> (0–8); the <code>optimizer_switch</code>
    flags <code>join_cache_incremental</code>, <code>join_cache_hashed</code>
    and <code>join_cache_bka</code> (on by default) can turn individual
    algorithms off. The <code>join_cache_level</code> default is
    <strong>2</strong>: block nested loop with incremental (linked) buffers
    and no hashing. Levels 3 and 4 add <em>hashed</em>
    buffers (BNLH; 3 is flat, 4 incremental), and levels 5 to 8 add batched
    key access for joins that have an index.</p>

    <p>MariaDB's "hashed BNL" (levels 3 and 4) is <strong>not</strong> the same
    algorithm as MySQL 8.4's hash join. It's still BNL structurally — each
    outer block builds a tiny hash table, then the inner is scanned once
    per block and probed into that hash table. It's faster than plain BNL
    but still O(outer_blocks × inner_rows), not O(outer + inner). See the
    <a href="join.html">BNL vs hash</a> lesson for the visual.</p>

    <p>MySQL 8.0.20 removed the block nested loop executor: a join the
    optimizer plans with a join buffer now runs as a two-phase hash join.
    In MySQL 8.4 the <code>block_nested_loop</code> switch (and the
    <code>BNL</code> and <code>NO_BNL</code> hints) still decide whether that
    hash join may be used; the <code>hash_join</code> switch no longer has
    any effect.</p>

    <p>One more MariaDB limit: while <code>optimize_join_buffer_size</code>
    is on (the default), all the join buffers of one query share
    <code>join_buffer_space_limit</code> (2 MiB by default), so raising
    <code>join_buffer_size</code> past that changes nothing unless you raise
    the limit too. The model above leaves that cap out.</p>

    <p>Sources: MariaDB Knowledge Base "Block-based Join Algorithms";
    "What's New in MySQL 8.0.20" release notes.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % JOIN_BUFFER_SIZE_DEFAULT

    return _html.render_page(
        lesson_id="bnl",
        title="Block nested loop join: MariaDB's default",
        subtitle=(
            "Watch join_buffer_size decide how many times the inner table is "
            "re-scanned. Bigger buffer, fewer blocks, less I/O."
        ),
        version_chip="MariaDB 11.4",
        banner_html=banner_html,
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
        guide=GUIDE,
    )
