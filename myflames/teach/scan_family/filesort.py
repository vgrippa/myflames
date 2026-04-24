"""Lesson: Filesort — how MySQL sorts when there is no index.

Animates all three in-memory sort algorithms MySQL actually uses:

* **Radix sort** — for fixed-length keys ≤ 16 bytes (``BIGINT``,
  ``INT``, ``DATE``). O(n·k) with zero comparisons. Selected when
  ``filesort.cc`` sees ``use_radixsort()`` return true.
* **Introsort (std::sort)** — quicksort with heapsort fallback after
  O(log n) recursion depth.  The general-case algorithm for
  variable-length keys or keys > 16 bytes.
* **Priority queue (bounded heap)** — when ``ORDER BY … LIMIT k``
  with a small *k*. MySQL keeps a max-heap of *k* elements and
  pushes/pops each incoming row, so only *k* rows stay in memory.

Also animates the spill path (sorted-run flush to tmpdir, k-way merge)
which is common to both radix sort and introsort when the data set
exceeds ``sort_buffer_size``.

Concrete sample data: 12 named order rows with visible amounts.
"""
from .. import _html
from .._cost_model import SORT_BUFFER_SIZE_DEFAULT


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = f"""
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (MySQL 8.4 / MariaDB 11.4 filesort)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="rows">Rows to sort: <span class="value-pill" data-pill-for="rows">10000</span></label>
      <input type="range" id="rows" name="rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Total rows returned by the table/index scan before sorting.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="4" max="4096" step="4" value="200">
      <div class="hint">Sort key + payload. \u2264 16 B triggers radix sort (INT/BIGINT/DATE keys); &gt; 16 B triggers introsort (VARCHAR, composites).</div>
    </div>

    <div class="control">
      <label for="sbs">sort_buffer_size (bytes): <span class="value-pill" data-pill-for="sbs">262144</span></label>
      <input type="range" id="sbs" name="sbs" min="32768" max="16777216" step="32768" value="{SORT_BUFFER_SIZE_DEFAULT}">
      <div class="hint">Default: {SORT_BUFFER_SIZE_DEFAULT} B (256 KiB). Bigger = fewer sorted runs.</div>
    </div>

    <div class="control">
      <label for="limit_rows">LIMIT (0 = no limit): <span class="value-pill" data-pill-for="limit_rows">0</span></label>
      <input type="range" id="limit_rows" name="limit_rows" min="0" max="1000" step="1" value="0">
      <div class="hint">Small LIMIT triggers priority queue (bounded heap) instead of full sort.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- No index on order_date \u2192 filesort\n"
            "SELECT customer, order_date, total\n"
            "FROM   orders\n"
            "ORDER  BY order_date;"
        ),
        note="No index covers the ORDER BY, so MySQL filesorts. The in\u2011memory algorithm depends on context: radix sort for short fixed keys, introsort for the general case, or a bounded priority queue when LIMIT is small. If the data exceeds sort_buffer_size, sorted runs spill to tmpdir and are k\u2011way merged."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Rows flow into the sort_buffer (purple/blue zone).",
            "MySQL picks one of three in-memory algorithms based on the key and query:",
            "\u2022 Radix sort (blue) \u2014 fixed-length key \u2264 16 B (INT, BIGINT, DATE). Distributes rows into digit buckets with zero comparisons. O(n\u00b7k).",
            "\u2022 Introsort (purple) \u2014 general case (VARCHAR keys, composites > 16 B). Quicksort with heapsort fallback after O(log n) depth. Guaranteed O(n log n).",
            "\u2022 Priority queue (blue) \u2014 ORDER BY \u2026 LIMIT k with small k. Maintains a bounded max-heap of k rows; each incoming row is pushed/popped. O(n\u00b7log k).",
            "If all rows fit in sort_buffer_size, the result streams from memory \u2014 zero disk I/O.",
            "If rows overflow, each sorted chunk is flushed as a run to tmpdir (yellow). Final phase: k-way merge across runs (green).",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="filesort-svg" viewBox="0 0 800 440" xmlns="http://www.w3.org/2000/svg"></svg>
    <svg id="radix-svg" viewBox="0 0 800 340" xmlns="http://www.w3.org/2000/svg" style="display:none"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (filesort)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Sort algorithm {ht("MySQL picks: radix sort (key ≤ 16 B), introsort (general case), or priority queue (small LIMIT). See sql/filesort.cc.")}</p><p class="value" id="out-alg">\u2014</p></div>
    <div class="item"><p class="label">Rows per run {ht("How many rows fit in sort_buffer_size. More rows per run = fewer total runs = less disk I/O.")}</p><p class="value" id="out-rpr">\u2014</p></div>
    <div class="item"><p class="label">Sorted runs {ht("Each run is a sorted chunk written to tmpdir. The inner table is merged across all runs in the merge phase.")}</p><p class="value" id="out-runs">\u2014</p></div>
    <div class="item"><p class="label">Merge passes {ht("How many times the merge must re-read all rows. MySQL merges up to ~15 runs per pass. More runs = more passes.")}</p><p class="value" id="out-merges">\u2014</p></div>
    <div class="item"><p class="label">Disk spill? {ht("If all rows fit in sort_buffer_size, no disk I/O needed. Otherwise, sorted runs are spilled to tmpdir.")}</p><p class="value ok" id="out-spill">\u2014</p></div>
    <div class="item"><p class="label">Total I/O rows {ht("Total rows read + written across all merge passes. Each pass reads and writes all rows once.")}</p><p class="value" id="out-io">\u2014</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Total I/O rows vs row count (log\u2013log, row size + sort_buffer_size fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — the three sort algorithms + single-pass vs two-pass</summary>
  <div class="body">
    <p><strong>Algorithm selection (in <code>sql/filesort.cc</code>):</strong></p>
    <p><strong>1. Radix sort</strong> — Used when the sort key is fixed-length
    and ≤ 16 bytes on 64-bit systems (e.g. <code>INT</code>, <code>BIGINT</code>,
    <code>DATE</code>, <code>DATETIME</code>). It distributes rows into
    digit-buckets with <em>zero comparisons</em>. O(n·k) where k is the key
    width in bytes. Cache-friendly because it processes all rows in sequential
    passes.</p>
    <p><strong>2. Introsort (<code>std::sort</code>)</strong> — The general-case
    algorithm for variable-length keys (<code>VARCHAR</code>) or composite keys
    exceeding 16 bytes. Introsort starts as quicksort (cache-friendly, in-place,
    small constant factor) but switches to heapsort if recursion depth exceeds
    O(log n), guaranteeing O(n log n) worst case. This is what MySQL actually
    uses — not plain quicksort.</p>
    <p><strong>3. Priority queue (bounded heap)</strong> — Used when the query
    has <code>ORDER BY … LIMIT k</code> and k×row_size fits in the sort buffer.
    MySQL maintains a max-heap of k elements. Each incoming row is compared to
    the heap's maximum; if smaller, it replaces it. After scanning all n rows,
    the heap holds the k smallest. O(n·log k) — much faster than fully sorting
    when k ≪ n. <em>No disk spill possible</em> because only k rows are ever in
    memory.</p>
    <hr>
    <p><strong>Single-pass vs two-pass filesort:</strong></p>
    <p><strong>Single-pass (original algorithm):</strong> The sort buffer holds
    the <em>entire row</em> alongside the sort key. After sorting, rows are
    already complete — no second read. This is faster but uses more buffer
    space per row.</p>
    <p><strong>Two-pass (rowid sort):</strong> The sort buffer holds only the
    sort key + row pointer. After sorting, MySQL must re-read each row from
    the table by rowid. Uses less memory per entry but requires a second
    random-I/O pass.</p>
    <p>MySQL chooses between them based on <code>max_length_for_sort_data</code>
    (default 4096 in MySQL 8.4). If the total row length exceeds this
    threshold, the two-pass algorithm is used.</p>
    <p>The animation above shows the general sort buffer → sorted runs →
    merge pattern, which is common to both strategies. The difference is
    what lives inside each sort-buffer entry.</p>
    <p>Sources: MySQL 8.4 reference manual §10.2.1.16 "ORDER BY Optimization";
    <code>sql/filesort.cc</code> in the MySQL source tree.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % SORT_BUFFER_SIZE_DEFAULT

    return _html.render_page(
        lesson_id="filesort",
        title="Filesort — how MySQL sorts without an index",
        subtitle=(
            "Watch sort_buffer_size fill, spill sorted runs to tmpdir, "
            "and merge them back. Bigger buffer = fewer runs = less I/O."
        ),
        version_chip="MySQL 8.4 \u2022 MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
