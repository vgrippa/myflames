"""Lesson: Batched Key Access (BKA) join.

BKA collects a batch of outer rows in the join buffer and sends all their
join keys to the storage engine as one Multi-Range Read (MRR). InnoDB looks
each key up in the inner table's secondary index, sorts the primary keys
(row ids) it finds, then reads the rows in primary key order, so the reads
move forward through the table instead of jumping around.

Source notes (mysql-server 8.4.9 and 9.7.0): BKAIterator fills the buffer
(sql/iterators/bka_iterator.cc); DsMrr_impl::dsmrr_fill_buffer collects the
row ids from the index and sorts them, dsmrr_next reads each row with
rnd_pos (sql/handler.cc). DsMrr_impl::choose_mrr_impl keeps the default
one-by-one implementation for InnoDB's clustered primary key, and
setup_join_buffering (sql/sql_optimizer.cc) refuses BKA then, so the lesson
joins on a secondary index. MariaDB sorts the row ids the same way
(Mrr_ordered_rndpos_reader, sql/multi_range_read.cc) and uses BKA at
join_cache_level 5 to 8.
"""
from . import _html
from ._cost_model import JOIN_BUFFER_SIZE_DEFAULT


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). bka_join.js marks filling the
# buffer, the index lookups, the sort, each row read and the summary with
# tl.beat(kind). Function names are on _html.VERIFIED_SERVER_FUNCTIONS
# (checked in mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Batched key access (BKA)",
        "lines": [
            ("fill", 0, "fill the join buffer with a batch of orders rows",
             "BKAIterator::ReadOuterRows"),
            ("send", 0, "send all their dept_no keys to InnoDB as one Multi-Range Read:"),
            ("lookup", 1, "look up each key in the index on departments(dept_no)"),
            ("sort", 1, "sort the primary keys found (InnoDB's row ids)",
             "DsMrr_impl::dsmrr_fill_buffer"),
            ("read", 1, "for each primary key, in order: read the departments row",
             "DsMrr_impl::dsmrr_next"),
            ("match", 2, "join it with its orders row from the buffer"),
            ("end", 0, "batch done: refill the buffer until orders runs out"),
        ],
    },
    "beats": {
        "fill": ["fill"],
        "lookup": ["send", "lookup"],
        "sort": ["sort"],
        "read-row": ["read", "match"],
        "done": ["end"],
    },
    "quiz": [
        {
            "question": ("Without BKA, the nested loop looks up departments once per order, in "
                         "the order the orders arrive. What does BKA change?"),
            "choices": [
                ("It batches the orders, looks up all their keys at once, and reads the "
                 "departments rows in primary key order", True,
                 "The Multi-Range Read sorts the primary keys it found in the index before "
                 "reading the rows, which turns scattered reads into reads that move forward "
                 "through the table."),
                ("It no longer needs an index on dept_no", False,
                 "BKA batches index lookups, so it needs an index on the inner join column; "
                 "without one, MySQL uses a hash join and MariaDB a block nested loop by default."),
                ("It keeps departments rows cached for the next query", False,
                 "The join buffer lives only for this join; keeping pages between queries is the "
                 "buffer pool's job."),
            ],
        },
        {
            "question": "Is BKA used by default?",
            "choices": [
                ("No: MySQL needs batched_key_access=on, MariaDB needs join_cache_level 5 or "
                 "higher", True,
                 "MySQL leaves batched_key_access off in its default optimizer_switch, MariaDB's "
                 "default join_cache_level=2 stops below the BKA levels, and both also need "
                 "Multi-Range Read to be allowed."),
                ("Yes, in both engines", False,
                 "Neither engine turns it on by default; you opt in with optimizer_switch in "
                 "MySQL or join_cache_level in MariaDB."),
                ("Only in MariaDB", False,
                 "MariaDB's default join_cache_level=2 allows only block nested loop buffers, so "
                 "BKA is off there too."),
            ],
        },
        {
            "question": ("Why does this lesson join on dept_no, a secondary index, instead of "
                         "departments.id, the primary key?"),
            "choices": [
                ("In MySQL, InnoDB's Multi-Range Read does not batch primary key lookups, so BKA "
                 "would not be used", True,
                 "MySQL keeps one-by-one lookups for a clustered primary key, and BKA needs the "
                 "batched Multi-Range Read, so a join on the id primary key runs as one eq_ref "
                 "lookup per row."),
                ("Primary keys cannot be used in joins", False,
                 "Joining on a primary key is common and fast; it simply runs as one lookup per "
                 "row instead of a batch."),
                ("dept_no keys are smaller", False,
                 "Key size changes the index's fan-out and height, not whether the lookups can "
                 "be batched."),
            ],
        },
    ],
}


def render():
    # type: () -> str
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (Batched Key Access join)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="outer_rows"><code>orders</code> rows (outer): <span class="value-pill" data-pill-for="outer_rows">10000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="100" max="500000" step="100" value="10000">
      <div class="hint">Rows from the outer (driving) table. Keys are collected into the join buffer.</div>
    </div>

    <div class="control">
      <label for="inner_rows"><code>departments</code> rows (inner): <span class="value-pill" data-pill-for="inner_rows">100000</span></label>
      <input type="range" id="inner_rows" name="inner_rows" min="100" max="5000000" step="100" value="100000">
      <div class="hint">Rows in the inner table. Looked up via B+tree index.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Size of one outer row in the join buffer.</div>
    </div>

    <div class="control">
      <label for="jbs">join_buffer_size (bytes): <span class="value-pill" data-pill-for="jbs">262144</span></label>
      <input type="range" id="jbs" name="jbs" min="32768" max="16777216" step="8192" value="262144">
      <div class="hint">Buffer for collecting outer keys. Default 256 KiB.</div>
    </div>

    <div class="control">
      <label for="key_size">Index key size (bytes): <span class="value-pill" data-pill-for="key_size">8</span></label>
      <input type="range" id="key_size" name="key_size" min="4" max="128" step="4" value="8">
      <div class="hint">Size of the join key in the inner B+tree index. Affects fan-out and tree height.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- BKA: batch outer keys \u2192 one Multi-Range Read on the inner index\n"
            "SELECT o.order_id, d.name\n"
            "FROM   orders o\n"
            "JOIN   departments d ON d.dept_no = o.dept_no;\n"
            "-- index on departments(dept_no); departments.id is the primary key"
        ),
        note=(
            "BKA fills the join buffer with orders rows and sends all their "
            "dept_no keys to InnoDB as one Multi-Range Read. InnoDB finds each "
            "key in the dept_no index, sorts the primary keys it found, and "
            "reads the departments rows in primary key order instead of "
            "jumping around."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Purple zone (top): orders rows fill the join buffer one by one ('#1001 dept_no=30'), forming one batch.",
            "Blue zone (middle): the batch's dept_no keys go to InnoDB as one Multi-Range Read (MRR). The index on departments(dept_no) gives each key's primary key ('dept_no=30 \u2192 id=2'), and the primary keys (InnoDB's row ids) are then sorted.",
            "Green zone (bottom): InnoDB reads the departments rows in primary key order, so the reads move forward through the table instead of jumping around; each row is joined with its order from the buffer.",
            "A sweep bar moves across the green zone to show the reads moving forward in key order.",
            "The readout below compares random I/Os (without BKA) vs sequential I/Os (with BKA) and shows the speedup factor.",
            "Next step fills the buffer, looks up the keys, sorts them, then reads one row at a time; the simplified code beside the drawing highlights the lines each step runs.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  %(query_card)s\n"
        "  %(explainer)s\n"
        "  %(toolbar)s\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="bka-svg" viewBox="0 0 800 372" xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "    %(phase_nav)s\n"
        "  </div>\n"
        "</section>"
    ) % {
        "query_card": query_card_html,
        "explainer": explainer_html,
        "toolbar": _html.stage_toolbar("Ready: press Next step, or Play to run it all"),
        "phase_nav": _html.phase_nav(),
    }

    ht = _html.help_tip
    readout_html = (
        '<section class="readout">\n'
        "  <h2>BKA cost model</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Rows per batch '
        + ht("How many outer rows fit in one join_buffer_size chunk. More rows per batch = fewer batches = fewer MRR round-trips.")
        + '</p><p class="value" id="out-rpb">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Number of batches '
        + ht("ceil(outer_rows / rows_per_batch). Each batch triggers one MRR request to the storage engine.")
        + '</p><p class="value" id="out-batches">\u2014</p></div>\n'
        '    <div class="item"><p class="label">B+tree height '
        + ht("Levels in the inner index B+tree. Each batch traverses this many levels before reaching leaf pages.")
        + '</p><p class="value" id="out-height">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Random I/Os (without BKA) '
        + ht("Without BKA, each outer row does an independent random index lookup: outer_rows x height page reads.")
        + '</p><p class="value" id="out-random">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Sequential I/Os (with BKA) '
        + ht("With BKA + MRR, the row ids found in the index are sorted, so the engine reads the rows in primary key order: batches x (height + rows_per_batch).")
        + '</p><p class="value" id="out-seq">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Speedup factor '
        + ht("random_ios / sequential_ios. Higher = more benefit from BKA. Biggest gains on HDD; still helps on SSD.")
        + '</p><p class="value" id="out-speedup">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">I/O operations vs outer rows (log\u2013log)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — BKA vs simple Nested Loop, and when to enable it</summary>
  <div class="body">
    <p><strong>Simple Nested Loop</strong> does one random index lookup per outer row.
    If the inner table is large and the index is deep, each lookup may touch 3-4 random
    pages. With 100,000 outer rows that is 300,000-400,000 random I/Os — catastrophic
    on spinning disks and still expensive on SSDs.</p>

    <p><strong>BKA (Batched Key Access)</strong> changes the pattern: it collects a
    batch of outer rows in <code>join_buffer_size</code> and hands all their join keys
    to the storage engine at once as a <em>Multi-Range Read</em> (MRR). InnoDB looks
    each key up in the secondary index, sorts the primary keys (row ids) it finds, and
    then reads the rows in primary key order, so the reads move forward through the
    table instead of jumping around.</p>

    <p><strong>When to enable it:</strong> BKA is not on by default. In MySQL you need:</p>
    <pre><code>SET optimizer_switch = 'batched_key_access=on,mrr=on,mrr_cost_based=off';</code></pre>
    <p>With <code>mrr_cost_based=on</code> (the default) MySQL skips Multi-Range Read
    for a table it expects to find in memory, such as one smaller than the buffer
    pool, and without Multi-Range Read there is no BKA. MySQL also never uses BKA for
    lookups on InnoDB's clustered primary key, because its Multi-Range Read keeps
    the one-by-one lookups there: the join needs a secondary index, as dept_no has
    here. On MySQL 8.4 you can also set these in <code>my.cnf</code> globally.</p>

    <p>The secondary index must also leave the query something to read from the
    rows. When it covers every departments column the join needs (an index-only
    read), MySQL and MariaDB both refuse the batched Multi-Range Read, and with it
    BKA. Here <code>d.name</code> is not in the dept_no index, so the rows must be
    read and BKA can sort those reads.</p>

    <p><strong>Why disk-order reads are faster:</strong> HDDs have ~10 ms seek time per
    random read. Sequential reads bypass seeks entirely — the head just streams. On SSDs,
    sequential reads still win because of read-ahead, fewer syscalls, and better NAND
    page utilization. A 10-50x speedup is common on HDD; 2-5x on SSD.</p>

    <p>MariaDB supports BKA at <code>join_cache_level</code> 5 and 6 (7 and 8 add a
    hashed join buffer, BKAH), with Multi-Range Read switched on:
    <code>SET join_cache_level = 6; SET optimizer_switch = 'mrr=on';</code>. Its
    default <code>join_cache_level</code> is 2, so BKA is off there too. The
    principle is identical: batch, look up, sort the row ids, read in order.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % JOIN_BUFFER_SIZE_DEFAULT

    return _html.render_page(
        lesson_id="bka_join",
        title="Batched key access (BKA) join: batch, sort, MRR",
        subtitle=(
            "Watch outer keys collect into a batch, get sorted by rowid, "
            "and sweep the inner index sequentially via Multi-Range Read."
        ),
        version_chip="MySQL 8.4 \u2022 MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
        guide=GUIDE,
    )
