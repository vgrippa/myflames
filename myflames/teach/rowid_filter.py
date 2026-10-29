"""Lesson: Rowid Filter (MariaDB).

Animates MariaDB's rowid pre-filter optimisation: range-scan a filtering
index to collect the primary keys (rowids) that qualify into a sorted
array (``Rowid_filter_sorted_array`` in ``sql/rowid_filter.h``; the Bloom
filter container is declared but not used), then scan the main index and,
inside InnoDB, skip the clustered index read for every entry whose key a
binary search does not find in the array.

Real query: ``SELECT * FROM orders WHERE created_date > '2024-01-01'
AND status = 'shipped'`` with indexes on ``(status)`` and
``(created_date)``.
"""
from . import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). rowid_filter.js marks building
# the filter, sorting it, one step per idx_date entry and the summary with
# tl.beat(kind). This lesson is MariaDB only, and the code panel's function
# notes are MySQL names, so it has none.
GUIDE = {
    "code": {
        "title": "Rowid filter (MariaDB)",
        "lines": [
            ("build", 0, "range scan idx_status for status = 'shipped', index only"),
            ("add", 1, "add each entry's primary key to the filter"),
            ("sort", 0, "sort the filter's keys"),
            ("scan", 0, "for each idx_date entry with created_date > '2024-01-01':"),
            ("check", 1, "binary search the filter for the entry's primary key"),
            ("skip", 2, "not found: skip the entry, no clustered index read"),
            ("fetch", 2, "found: read the row from the clustered index"),
            ("send", 2, "send the row"),
            ("end", 0, "no idx_date entries left: done"),
        ],
    },
    "beats": {
        "build": ["build", "add"],
        "sort": ["sort"],
        "row-pass": ["scan", "check", "fetch", "send"],
        "row-skip": ["scan", "check", "skip"],
        "done": ["end"],
    },
    "quiz": [
        {
            "question": "What does MariaDB's rowid filter hold after it reads idx_status?",
            "choices": [
                ("A sorted array of the shipped orders' primary keys", True,
                 "MariaDB keeps the primary key of each matching idx_status entry in an array "
                 "and sorts it, so each later check is a binary search."),
                ("One bit for every row in orders", False,
                 "The filter stores the matching primary keys themselves, not a bit per row, "
                 "and max_rowid_filter_size caps how large it may grow."),
                ("The full rows of the shipped orders", False,
                 "The filter scan reads only the index and keeps each entry's primary key, "
                 "never the row."),
            ],
        },
        {
            "question": "When is an idx_date entry checked against the filter?",
            "choices": [
                ("Inside InnoDB, before the row is read from the clustered index", True,
                 "InnoDB checks the entry's primary key against the filter right after any "
                 "pushed index condition, so only entries that pass cost a clustered index read."),
                ("After the row is read, like any WHERE condition", False,
                 "Checking after the read would save nothing; the point is to skip the clustered "
                 "index read."),
                ("Before idx_date is read at all", False,
                 "The filter is checked once per idx_date entry, because each entry's primary "
                 "key is what gets looked up in it."),
            ],
        },
        {
            "question": "How does MariaDB's EXPLAIN show that a rowid filter is used?",
            "choices": [
                ("type range|filter, key idx_date|idx_status, Extra Using rowid filter", True,
                 "The filter's index follows a bar in the type and key columns, and the Extra "
                 "column says Using rowid filter."),
                ("Extra Using index condition", False,
                 "That is index condition pushdown, which tests columns stored in the index "
                 "entry itself."),
                ("Extra Rowid-ordered scan", False,
                 "That text comes from Multi-Range Read, which reads rows in primary key order."),
            ],
        },
    ],
}


def render():
    # type: () -> str
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (Rowid Filter)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="main_rows">Main index rows: <span class="value-pill" data-pill-for="main_rows">10000</span></label>
      <input type="range" id="main_rows" name="main_rows" min="100" max="500000" step="100" value="10000">
      <div class="hint">Rows returned by the main index scan (idx_date).</div>
    </div>

    <div class="control">
      <label for="filter_selectivity">Filter selectivity (%): <span class="value-pill" data-pill-for="filter_selectivity">20</span></label>
      <input type="range" id="filter_selectivity" name="filter_selectivity" min="1" max="100" step="1" value="20">
      <div class="hint">Percentage of idx_date entries whose primary key is in the filter (status = 'shipped').</div>
    </div>

    <div class="control">
      <label for="row_size">Average row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="8" value="200">
      <div class="hint">Larger rows make skipped fetches more valuable (more I/O saved).</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- idx_status on (status), idx_date on (created_date)\n"
            "-- Main access via idx_date, rowid filter from idx_status\n"
            "SELECT * FROM orders\n"
            "WHERE  created_date > '2024-01-01'\n"
            "AND    status = 'shipped';"
        ),
        note=(
            "MariaDB scans idx_status first and keeps the shipped orders' "
            "primary keys in a sorted array, then uses idx_date for the main "
            "scan \u2014 skipping the clustered index read for every entry "
            "whose key is not in the array."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Step 1 reads only the 'shipped' entries of idx_status and puts each one's primary key (id) into the rowid filter: the blue slots at the top, a sorted array.",
            "Step 2 reads idx_date entries one by one. Each entry holds a date and an id; the order's status is only in the row.",
            "For each entry InnoDB looks its id up in the filter (binary search). Found: the row is read from the clustered index (green check).",
            "Not found: the entry is skipped and the row is never read (red cross), which saves one clustered index lookup.",
            "Next step shows one entry at a time, and the simplified code beside the drawing highlights the lines each step runs.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  %(query_card)s\n"
        "  %(explainer)s\n"
        "  %(toolbar)s\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="rowid-filter-svg" viewBox="0 0 800 400"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
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
        "  <h2>Cost readout (Rowid Filter)</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Main index rows '
        + ht("Entries returned by the main index scan (idx_date). Without a rowid filter each one costs a clustered index read.")
        + '</p><p class="value" id="out-main-rows">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Filter selectivity '
        + ht("Percentage of idx_date entries whose primary key is in the filter (status = 'shipped'). Lower is better for the filter.")
        + '</p><p class="value" id="out-selectivity">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Rows after filter '
        + ht("Entries whose primary key the filter holds. Only these are read from the clustered index.")
        + '</p><p class="value" id="out-after-filter">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Rows skipped '
        + ht("Entries whose primary key is not in the filter. Each skip avoids one clustered index lookup.")
        + '</p><p class="value ok" id="out-skipped">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Table fetches (without filter) '
        + ht("Without a rowid filter, every idx_date entry costs a clustered index read.")
        + '</p><p class="value" id="out-without">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Table fetches (with filter) '
        + ht("With the rowid filter, only entries found in it cost a clustered index read.")
        + '</p><p class="value" id="out-with">\u2014</p></div>\n'
        '    <div class="item"><p class="label">I/O saved '
        + ht("Clustered index reads saved by skipping entries not in the filter. Equal to rows skipped.")
        + '</p><p class="value ok" id="out-saved">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">Table fetches vs main index rows (log\u2013log, selectivity fixed)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more &mdash; when does MariaDB use rowid filter?</summary>
  <div class="body">
    <p>Rowid filtering was introduced in <strong>MariaDB 10.4</strong> and is
    enabled by default
    (<code>optimizer_switch='rowid_filter=on'</code>).
    This optimisation is <strong>not available in MySQL</strong>.</p>

    <p>The optimizer considers rowid filter when:</p>
    <p>1. The query accesses a table through one index (the <em>main</em>
    access path) but another index could filter out a large fraction of
    rows before the expensive table-row fetch.</p>
    <p>2. The filtering index is selective enough that building the
    in-memory filter and checking it per entry is cheaper than reading
    every row from the table.</p>
    <p>3. The filter fits in memory. It is a sorted array of the
    qualifying primary keys, at most <code>max_rowid_filter_size</code>
    bytes (128 KiB by default), so it suits a filter index that matches
    few rows.</p>

    <p>Rowid filter works best when the <strong>filter index is very
    selective</strong> (few rows match) but the <strong>main index returns
    many rows</strong>. The more entries the filter can eliminate, the more
    clustered index reads are saved. The check happens inside InnoDB, before
    the row is read, right after any pushed index condition.</p>

    <p>In <code>EXPLAIN</code> output the <code>type</code> column shows
    <code>range|filter</code> (or <code>ref|filter</code>), the
    <code>key</code> column <code>idx_date|idx_status</code>, and the
    <code>Extra</code> column <strong>Using rowid filter</strong>.</p>

    <p>Sources: MariaDB Knowledge Base &ldquo;Rowid Filtering Optimization&rdquo;;
    MariaDB Server 10.4 Release Notes.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="rowid_filter",
        title="Rowid filter: a sorted key pre-filter before table access",
        subtitle=(
            "See how MariaDB scans a filtering index to collect the matching "
            "primary keys, then skips the row read for entries not among them."
        ),
        version_chip="MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
        guide=GUIDE,
    )
