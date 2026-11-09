"""Lesson: Filesort — how MySQL sorts when there is no index.

Animates the in-memory sorts the servers use, checked in the source:

* **Comparison sort** — MySQL 8.4 and 9.x sort the buffer with
  ``std::sort`` (up to 100 rows, or variable-length keys) or
  ``std::stable_sort`` (``Filesort_buffer::sort_buffer`` in
  ``sql/filesort_utils.cc``). MariaDB uses its own quicksort
  (``my_qsort2``, ``mysys/mf_qsort.c``). The drawing shows quicksort.
* **Radix sort (MariaDB only)** — MariaDB radix sorts when the sort key
  (not the row) is at most 20 bytes, the buffer holds 1,000 to 99,999
  rows and the keys are not packed (``radixsort_is_applicable(count,
  param->sort_length)`` in ``sql/filesort_utils.cc``, ``mysys/mf_radix.c``;
  a fixed-size key such as order_date's DATE is never packed): one
  counting pass per key byte, least significant first, zero comparisons.
  MySQL has no radix sort. The lesson's Server input picks the engine.
* **Priority queue (bounded heap)** — both engines, for ``ORDER BY …
  LIMIT k`` with a small *k* (MySQL ``check_if_pq_applicable``; MariaDB
  picks it by cost in ``filesort()``, both in ``sql/filesort.cc``): only
  *k* rows stay in memory.

Also animates the spill path (sorted runs written to tmpdir, then a
merge) when the rows exceed ``sort_buffer_size``. Every move is a
timeline tween, so a guided step or a scrub lands on its final state.

Concrete sample data: 12 named order rows with visible amounts.
"""
from .. import _html
from .._cost_model import SORT_BUFFER_SIZE_DEFAULT

# MariaDB's sort_buffer_size default: DEFAULT(MAX_SORT_MEMORY) in
# sql/sys_vars.cc, MAX_SORT_MEMORY = 2048*1024 in sql/sql_const.h. MySQL's is
# SORT_BUFFER_SIZE_DEFAULT (256 KiB).
MARIADB_SORT_BUFFER_SIZE_DEFAULT = 2048 * 1024


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). filesort.js marks each step of
# the path the inputs select (priority queue, in-memory sort, MariaDB radix
# sort, or sorted runs and a merge) with tl.beat(kind). Function names are on
# _html.VERIFIED_SERVER_FUNCTIONS (checked in mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Filesort (ORDER BY with no usable index)",
        "lines": [
            ("pq", 0, "if LIMIT k is small and k rows fit in the sort buffer:",
             "check_if_pq_applicable"),
            ("pq-push", 1, "for each row: keep it if it beats the worst of the best k so far"),
            ("pq-out", 1, "send the k kept rows in order; nothing is written to tmpdir"),
            ("read", 0, "else, for each row read from orders:"),
            ("append", 1, "copy its order_date key and the row into the sort buffer"),
            ("full", 1, "if the sort buffer is full:"),
            ("run", 2, "sort the buffer and write it to tmpdir as one sorted run"),
            ("sort", 0, "sort the rows left in the buffer", "Filesort_buffer::sort_buffer"),
            ("mem", 0, "if no run was written: send the rows straight from memory"),
            ("merge", 0, "else write the last run and merge the runs, earliest head first",
             "merge_buffers"),
        ],
    },
    "beats": {
        "pq-start": ["pq"],
        "pq-row": ["pq-push"],
        "pq-done": ["pq-out"],
        "fill": ["read", "append"],
        "partition": ["sort"],
        "radix-pass": ["sort"],
        "radix-bucket": ["sort"],
        "radix-gather": ["sort"],
        "done-mem": ["mem"],
        "run-sort": ["full", "run"],
        "run-write": ["run"],
        "merge": ["merge"],
        "done-merge": ["merge"],
    },
    "quiz": [
        {
            "question": ("ORDER BY order_date must sort 10,000 rows of 200 bytes, and "
                         "sort_buffer_size is 256 KiB. What happens?"),
            "choices": [
                ("About 8 sorted runs are written to tmpdir, then merged", True,
                 "About 1,300 rows of 200 bytes fit in 256 KiB, so the buffer fills, is "
                 "sorted and is written out about 8 times before the merge."),
                ("All 10,000 rows are sorted in memory", False,
                 "10,000 rows of 200 bytes need about 1.9 MiB, far more than a 256 KiB "
                 "buffer holds."),
                ("orders is read 8 times", False,
                 "orders is read once; it is the sort buffer that fills and is written "
                 "out 8 times."),
            ],
        },
        {
            "question": "Why does ORDER BY order_date LIMIT 5 usually write nothing to tmpdir?",
            "choices": [
                ("Only the 5 best rows so far are kept, in a small priority queue", True,
                 "With a small LIMIT, MySQL and MariaDB can keep a bounded heap, so each new "
                 "row either replaces the worst kept row or is dropped."),
                ("LIMIT stops reading orders after 5 rows", False,
                 "Without an index on order_date the server must see every row to know "
                 "which 5 dates come first."),
                ("The client sorts the rows", False,
                 "The server sorts; the client only receives the 5 rows."),
            ],
        },
        {
            "question": ("The same 10,000-row sort runs with sort_buffer_size raised from "
                         "256 KiB to 4 MiB. What changes?"),
            "choices": [
                ("All rows fit in one buffer, so no runs are written and nothing is merged",
                 True,
                 "About 20,900 rows of 200 bytes fit in 4 MiB, so the whole sort finishes in "
                 "memory."),
                ("Nothing, the rows still go through tmpdir", False,
                 "Runs are written only when the buffer fills, and 4 MiB holds all 10,000 "
                 "rows here."),
                ("The sort is skipped", False,
                 "Every row is still sorted; only an index that returns rows in order "
                 "removes the sort itself."),
            ],
        },
        {
            "question": ("On MariaDB, with its default 2 MiB sort_buffer_size, ORDER BY order_date "
                         "sorts 10,000 rows of 200 bytes. Which sort runs in the buffer?"),
            "choices": [
                ("A radix sort, because the order_date key is 3 bytes and the buffer holds "
                 "10,000 rows", True,
                 "MariaDB radix sorts when the sort key is 20 bytes or less and the buffer "
                 "holds 1,000 to 99,999 rows, and a DATE key is never packed."),
                ("A quicksort, because each row is longer than 20 bytes", False,
                 "The 20-byte limit applies to the sort key, not the row; the row size only "
                 "decides how many rows fit in the buffer."),
                ("std::stable_sort, as in MySQL", False,
                 "std::stable_sort is MySQL's choice; MariaDB picks between its own radix sort "
                 "and its own quicksort."),
            ],
        },
    ],
}


def render() -> str:
    controls_html = f"""
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (MySQL 8.4 / MariaDB 11.4 filesort)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="server">Server</label>
      <select id="server" name="server">
        <option value="mysql" selected>MySQL 8.4 / 9.x: comparison sort</option>
        <option value="mariadb">MariaDB 11.4: radix sort for a short key</option>
      </select>
      <div class="hint">Decides the in-memory sort. Choosing a server moves sort_buffer_size to its default, unless you changed it.</div>
    </div>

    <div class="control">
      <label for="rows">Rows to sort: <span class="value-pill" data-pill-for="rows">10000</span></label>
      <input type="range" id="rows" name="rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Total rows returned by the table/index scan before sorting.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="4" max="4096" step="4" value="200">
      <div class="hint">Bytes per row in the sort buffer: the 3-byte order_date key plus the columns sent. It decides how many rows fit in sort_buffer_size; the sort key stays 3 B whatever the row size.</div>
    </div>

    <div class="control">
      <label for="sbs">sort_buffer_size (bytes): <span class="value-pill" data-pill-for="sbs">262144</span></label>
      <input type="range" id="sbs" name="sbs" min="32768" max="16777216" step="32768" value="{SORT_BUFFER_SIZE_DEFAULT}">
      <div class="hint">Default: 256 KiB on MySQL, 2 MiB on MariaDB. Bigger = fewer sorted runs.</div>
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
        note="No index covers the ORDER BY, so the server filesorts. A small LIMIT uses a bounded priority queue; otherwise rows go into the sort buffer and are sorted there (a comparison sort on MySQL; on MariaDB a radix sort, since order_date is a 3-byte key). If the rows exceed sort_buffer_size, sorted runs are written to tmpdir and merged."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Each pill is an orders row such as \"Alice Jan 15\", sorted by its order_date. "
            "Rows are copied into the sort buffer (purple zone).",
            "Comparison sort (MySQL, the default drawing): pivot, compare and swap until every "
            "row is in date order. MySQL uses std::sort or std::stable_sort; MariaDB its own "
            "quicksort when radix sort does not apply.",
            "MariaDB radix sort (sort key of 20 B or less, 1,000 to 99,999 rows in the buffer, "
            "keys not packed): rows drop into buckets by one key byte at a time, with zero "
            "comparisons. order_date is a 3-byte key, so MariaDB radix sorts any buffer of 1,000 "
            "to 99,999 rows here, whatever the row size; choose MariaDB under Server to see it. "
            "MySQL has no radix sort.",
            "Priority queue (LIMIT set): k seats hold the earliest dates seen so far; a "
            "later row is turned away, an earlier one evicts the latest seated date.",
            "If the rows overflow sort_buffer_size, each sorted buffer is written to tmpdir "
            "as a run (yellow), and a merge takes the earliest head across runs (green).",
            "Next step runs one step at a time; the simplified code beside the drawing "
            "highlights the lines each step runs.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="filesort-svg" viewBox="0 0 800 440" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (filesort)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Sort algorithm {ht("Priority queue for a small LIMIT; otherwise a comparison sort of the buffer (MySQL std::sort or std::stable_sort, MariaDB quicksort), or on MariaDB a radix sort when the sort key is 20 B or less and the buffer holds 1,000 to 99,999 rows. See sql/filesort.cc and sql/filesort_utils.cc.")}</p><p class="value" id="out-alg">\u2014</p></div>
    <div class="item"><p class="label">Rows per run {ht("How many rows fit in sort_buffer_size. More rows per run = fewer total runs = less disk I/O.")}</p><p class="value" id="out-rpr">\u2014</p></div>
    <div class="item"><p class="label">Sorted runs {ht("Each run is one sorted buffer of rows written to tmpdir. The merge reads all runs back in order.")}</p><p class="value" id="out-runs">\u2014</p></div>
    <div class="item"><p class="label">Merge passes {ht("How many times the merge re-reads all rows. Both engines merge 7 runs at a time while more than 15 remain (MERGEBUFF and MERGEBUFF2), then do a final merge. More runs = more passes.")}</p><p class="value" id="out-merges">\u2014</p></div>
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
    <p><strong>Algorithm selection (in <code>sql/filesort.cc</code> and
    <code>sql/filesort_utils.cc</code>):</strong></p>
    <p><strong>1. Comparison sort:</strong> MySQL 8.4 and 9.x sort the
    buffer with <code>std::sort</code> (an introsort: quicksort with a
    heapsort fallback) when it holds 100 rows or fewer or the keys have
    variable length, and with <code>std::stable_sort</code> (a merge sort)
    otherwise. MariaDB uses its own quicksort, <code>my_qsort2</code>. All are
    O(n log n) comparisons; the drawing shows quicksort.</p>
    <p><strong>2. Radix sort (MariaDB only):</strong> MariaDB radix sorts the
    buffer when the sort key is at most 20 bytes, the buffer holds 1,000 to
    99,999 rows and the keys are not packed (<code>radixsort_is_applicable</code>
    in <code>mysys/mf_radix.c</code>). It makes one counting pass per key byte,
    least significant byte first, with <em>zero comparisons</em>. MySQL 8.4
    and 9.x have no radix sort in filesort and always use a comparison sort.</p>
    <p>The length that counts is the <strong>sort key's</strong>, not the
    row's: <code>ORDER BY order_date</code> sorts on a 3-byte DATE key (one
    byte more if the column allows NULL; when MariaDB sorts row IDs instead
    of rows it appends the primary key, 7 bytes in all with an INT id). A
    fixed-size key such as a DATE is never packed. So whatever the row size,
    MariaDB radix sorts each buffer that holds 1,000 to 99,999 rows: with
    its default <code>sort_buffer_size</code> of 2 MiB the lesson's 10,000
    rows of 200 bytes fit in one buffer and are radix sorted, and a sort that
    spills radix sorts each full run.</p>
    <p><strong>3. Priority queue (bounded heap):</strong> used by both
    engines when the query has <code>ORDER BY … LIMIT k</code>, k rows fit in
    the sort buffer and it is expected to beat a full sort (MySQL
    <code>check_if_pq_applicable</code>; MariaDB compares the sort costs in
    <code>filesort()</code>). The server keeps a heap of the k
    best rows. Each incoming row is compared to the worst of them; if it is
    better, it replaces it. After all n rows, the heap holds the k earliest.
    O(n·log k), much faster than fully sorting when k ≪ n, and nothing is
    written to tmpdir.</p>
    <hr>
    <p><strong>Single-pass vs two-pass filesort:</strong></p>
    <p><strong>Single-pass (sort the rows):</strong> The sort buffer holds the
    columns the query needs alongside the sort key. After sorting, rows are
    already complete, with no second read. This is faster but uses more
    buffer space per row.</p>
    <p><strong>Two-pass (sort row IDs):</strong> The sort buffer holds only the
    sort key and a row pointer. After sorting, the server re-reads each row
    from the table by its row ID. Less memory per entry, but a second
    random-I/O pass.</p>
    <p><strong>How they choose:</strong> MySQL 8.4 and 9.x sort full rows
    whenever they can (<code>Sort_param::decide_addon_fields</code>) and fall
    back to row IDs mainly when the query reads a MEDIUMBLOB, LONGBLOB,
    MEDIUMTEXT or LONGTEXT column, or uses full-text search; <code>max_length_for_sort_data</code> is deprecated
    there and has no effect. MariaDB still uses
    <code>max_length_for_sort_data</code> (default 1024 bytes): longer rows
    are sorted by row ID.</p>
    <p>The animation above shows the general sort buffer → sorted runs →
    merge pattern, which is common to both strategies. The difference is
    what lives inside each sort-buffer entry.</p>
    <p>Sources: MySQL 8.4 reference manual §10.2.1.16 "ORDER BY Optimization";
    <code>sql/filesort.cc</code> and <code>sql/filesort_utils.cc</code> in the
    MySQL and MariaDB source trees; <code>mysys/mf_radix.c</code> and
    <code>mysys/mf_qsort.c</code> in MariaDB.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % (SORT_BUFFER_SIZE_DEFAULT, MARIADB_SORT_BUFFER_SIZE_DEFAULT)

    return _html.render_page(
        lesson_id="filesort",
        title="Filesort: how MySQL sorts without an index",
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
        guide=GUIDE,
    )
