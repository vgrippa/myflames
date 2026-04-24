"""Lesson: Rowid Filter (MariaDB).

Animates MariaDB's rowid pre-filter optimisation: scan a filtering index
to build a bitmap of qualifying rowids, then scan the main index and
skip table-row fetches for rowids NOT in the bitmap.

Real query: ``SELECT * FROM orders WHERE created_date > '2024-01-01'
AND status = 'shipped'`` with indexes on ``(status)`` and
``(created_date)``.
"""
from . import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


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
      <div class="hint">Percentage of rows passing the filtering index (idx_status bitmap).</div>
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
            "MariaDB scans idx_status first to build a bitmap, then uses "
            "idx_date for the main scan \u2014 skipping table-row fetches "
            "for rows not in the bitmap."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Phase 1 scans the filtering index (idx_status) and sets a 1/0 bit per rowid.",
            "Phase 2 scans the main index (idx_date) and checks the bitmap for each rowid.",
            "Rows with bit=1 proceed to a full table fetch (green check).",
            "Rows with bit=0 are skipped entirely (red cross) \u2014 no random I/O.",
            "Watch the skipped-reads counter grow: every skip saves one random page read.",
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
        "toolbar": _html.stage_toolbar("Ready \u2014 press Play"),
        "phase_nav": _html.phase_nav(),
    }

    ht = _html.help_tip
    readout_html = (
        '<section class="readout">\n'
        "  <h2>Cost readout (Rowid Filter)</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Main index rows '
        + ht("Total rows returned by the main index scan (idx_date). Without a rowid filter all would trigger table fetches.")
        + '</p><p class="value" id="out-main-rows">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Filter selectivity '
        + ht("Percentage of rows whose rowid appears in the bitmap (status=shipped). Lower is better for the filter.")
        + '</p><p class="value" id="out-selectivity">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Rows after filter '
        + ht("Rows passing the bitmap check. Only these trigger a full table-row fetch.")
        + '</p><p class="value" id="out-after-filter">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Rows skipped '
        + ht("Rows NOT in the bitmap. Each skip avoids one random I/O page read.")
        + '</p><p class="value ok" id="out-skipped">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Table fetches (without filter) '
        + ht("Without rowid filter, every main-index row triggers a table fetch.")
        + '</p><p class="value" id="out-without">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Table fetches (with filter) '
        + ht("With rowid filter, only bitmap-passing rows trigger a table fetch.")
        + '</p><p class="value" id="out-with">\u2014</p></div>\n'
        '    <div class="item"><p class="label">I/O saved '
        + ht("Random I/Os saved by skipping non-matching rowids. Equal to rows_skipped.")
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
    in-memory rowid bitmap and checking it per row is cheaper than
    fetching every row from the table.</p>
    <p>3. The bitmap fits in memory. MariaDB allocates a compact
    bit-array keyed by rowid, so even millions of rows consume only
    a few megabytes.</p>

    <p>Rowid filter works best when the <strong>filter index is very
    selective</strong> (few rows match) but the <strong>main index returns
    many rows</strong>. The more rows the bitmap can eliminate, the more
    random I/O is saved.</p>

    <p>In <code>EXPLAIN</code> output you will see
    <strong>Rowid-ordered scan</strong> or <strong>Using rowid filter</strong>
    in the <code>Extra</code> column.</p>

    <p>Sources: MariaDB Knowledge Base &ldquo;Rowid Filtering Optimization&rdquo;;
    MariaDB Server 10.4 Release Notes.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="rowid_filter",
        title="Rowid Filter \u2014 bitmap pre-filter before table access",
        subtitle=(
            "See how MariaDB scans a filtering index to build a rowid bitmap, "
            "then skips table-row fetches for non-matching rows."
        ),
        version_chip="MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
