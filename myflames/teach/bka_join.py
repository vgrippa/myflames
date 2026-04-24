"""Lesson: Batched Key Access (BKA) join.

BKA collects a batch of outer-row join keys into the join buffer, sorts
them by rowid for disk-order access, then sends the batch to the storage
engine as a Multi-Range Read (MRR). This converts random I/O into
sequential I/O — dramatically faster on spinning disks and still
beneficial on SSDs due to read-ahead and reduced syscall overhead.
"""
from . import _html
from ._cost_model import JOIN_BUFFER_SIZE_DEFAULT


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


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
            "-- BKA: batch outer keys \u2192 MRR on inner\n"
            "SELECT o.*, d.name\n"
            "FROM   orders o\n"
            "JOIN   departments d ON d.id = o.dept_id\n"
            "ORDER  BY o.order_date;"
        ),
        note=(
            "BKA collects outer keys into join_buffer, sorts by rowid, "
            "then does a Multi-Range Read on the inner index \u2014 converting "
            "random I/O into sequential."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Purple zone (top): outer-row join keys flow into the join buffer one by one, forming a batch.",
            "Blue zone (middle): the batch is sorted by rowid so the storage engine can read in disk order. The sorted keys are dispatched as an MRR request.",
            "Green zone (bottom): the storage engine reads matching inner rows sequentially in rowid order \u2014 no random seeks.",
            "A sweep bar moves across the green zone to show the sequential nature of the disk reads.",
            "The readout below compares random I/Os (without BKA) vs sequential I/Os (with BKA) and shows the speedup factor.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  %(query_card)s\n"
        "  %(explainer)s\n"
        "  %(toolbar)s\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="bka-svg" viewBox="0 0 800 440" xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "    %(phase_nav)s\n"
        "  </div>\n"
        "</section>"
    ) % {
        "query_card": query_card_html,
        "explainer": explainer_html,
        "toolbar": _html.stage_toolbar("Ready \u2014 press Play"),
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
        + ht("With BKA + MRR, keys are sorted by rowid so the engine reads pages in disk order: batches x (height + rows_per_batch).")
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
    batch of outer keys into <code>join_buffer_size</code>, sorts them by the inner
    table's rowid (primary key order), and hands the sorted batch to the storage engine
    as a <em>Multi-Range Read</em> (MRR). The engine reads the matching rows in disk
    order — sequential I/O instead of random.</p>

    <p><strong>When to enable it:</strong> BKA is not on by default. You need:</p>
    <pre><code>SET optimizer_switch = 'batched_key_access=on,mrr=on,mrr_cost_based=off';</code></pre>
    <p><code>mrr_cost_based=off</code> forces MRR even when the optimizer's cost model
    thinks random I/O is cheap (which it often misjudges on HDD). On MySQL 8.4 you can
    also set these in <code>my.cnf</code> globally.</p>

    <p><strong>Why disk-order reads are faster:</strong> HDDs have ~10 ms seek time per
    random read. Sequential reads bypass seeks entirely — the head just streams. On SSDs,
    sequential reads still win because of read-ahead, fewer syscalls, and better NAND
    page utilization. A 10-50x speedup is common on HDD; 2-5x on SSD.</p>

    <p>MariaDB 11.4 also supports BKA (called BKA in join_cache_level 5-6).
    The principle is identical: batch, sort, MRR.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % JOIN_BUFFER_SIZE_DEFAULT

    return _html.render_page(
        lesson_id="bka",
        title="Batched Key Access (BKA) join \u2014 batch, sort, MRR",
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
    )
