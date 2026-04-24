"""Lesson: Skip Scan (range access without the leading index column).

Given a composite index (A, B), a query with ``WHERE B > 100`` (no equality
on A) normally cannot use the index. Skip Scan scans for each distinct value
of A, then does a range scan on B within each A-group — converting one full
table scan into N range scans (N = NDV of the leading column).

Real query: ``SELECT * FROM employees WHERE age BETWEEN 25 AND 30`` with a
composite index on ``(gender, age)`` and no equality predicate on ``gender``.
"""
from . import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render():
    # type: () -> str
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (Skip Scan)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="table_rows">Table rows: <span class="value-pill" data-pill-for="table_rows">100000</span></label>
      <input type="range" id="table_rows" name="table_rows" min="1000" max="5000000" step="1000" value="100000">
      <div class="hint">Total number of rows in the table.</div>
    </div>

    <div class="control">
      <label for="ndv_leading">Distinct values (leading col): <span class="value-pill" data-pill-for="ndv_leading">5</span></label>
      <input type="range" id="ndv_leading" name="ndv_leading" min="2" max="1000" step="1" value="5">
      <div class="hint">Number of distinct values in column A (e.g. gender). Lower = better for Skip Scan.</div>
    </div>

    <div class="control">
      <label for="selectivity">Selectivity on B (%): <span class="value-pill" data-pill-for="selectivity">10</span></label>
      <input type="range" id="selectivity" name="selectivity" min="1" max="100" step="1" value="10">
      <div class="hint">Percentage of rows matching WHERE condition on trailing column B.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Index on (gender, age) but no equality on gender\n"
            "SELECT * FROM employees\n"
            "WHERE  age BETWEEN 25 AND 30;"
        ),
        note=(
            "Skip Scan iterates over each distinct gender value, "
            "doing a range scan on age within each group \u2014 "
            "avoids reading the whole table."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "A composite index on (gender, age) is shown as colored groups.",
            "The cursor pill jumps to the first distinct gender value.",
            "Within each group, a range scan checks age BETWEEN 25 AND 30.",
            "Matching rows are highlighted green; non-matching rows are greyed out.",
            "The cursor hops to the next group and repeats until all groups are scanned.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  %(query_card)s\n"
        "  %(explainer)s\n"
        "  %(toolbar)s\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="skip-scan-svg" viewBox="0 0 800 400"'
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
        "  <h2>Cost readout (Skip Scan)</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Distinct values (leading col) '
        + ht("Number of distinct values in the leading index column. Skip Scan does one sub-range-scan per distinct value.")
        + '</p><p class="value" id="out-ndv">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Rows per group '
        + ht("table_rows / NDV of leading column. Each group is scanned separately.")
        + '</p><p class="value" id="out-rows-per-group">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Matching rows per group '
        + ht("Rows per group that satisfy the WHERE condition on the trailing column.")
        + '</p><p class="value" id="out-matching-per-group">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Total matching rows '
        + ht("Sum of matching rows across all groups.")
        + '</p><p class="value" id="out-total-matching">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Skip scan reads '
        + ht("Total reads: for each distinct value, one B+tree seek plus a range read of matching rows.")
        + '</p><p class="value" id="out-skip-scan-reads">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Full scan reads '
        + ht("A full table scan reads every row \u2014 the baseline without Skip Scan.")
        + '</p><p class="value" id="out-full-scan-reads">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Savings '
        + ht("Reads saved compared to a full table scan: full_scan_reads minus skip_scan_reads.")
        + '</p><p class="value ok" id="out-savings">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">Reads vs table size (log\u2013log)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more &mdash; when does the optimizer choose Skip Scan?</summary>
  <div class="body">
    <p>Skip Scan was introduced in <strong>MySQL 8.0.13</strong> and is
    controlled by <code>optimizer_switch='skip_scan=on'</code> (on by default).</p>
    <p><strong>When it helps:</strong></p>
    <p>1. The leading column of a composite index has <strong>low NDV</strong>
    (number of distinct values) &mdash; e.g. gender, status, boolean flags.</p>
    <p>2. The trailing column has a <strong>selective range condition</strong>
    (e.g. <code>age BETWEEN 25 AND 30</code>).</p>
    <p>3. The optimizer estimates that doing N sub-range-scans (one per distinct
    leading value) is cheaper than a full table scan or a full index scan.</p>
    <p><strong>When it does NOT help:</strong></p>
    <p>1. The leading column has <strong>high NDV</strong> (thousands of distinct
    values) &mdash; too many sub-scans make it slower than a full scan.</p>
    <p>2. The range on the trailing column is <strong>not selective</strong>
    (most rows match) &mdash; you end up reading almost everything anyway.</p>
    <p>3. A better single-column index on the trailing column exists.</p>
    <p><strong>Note:</strong> Skip Scan is a <strong>MySQL-only</strong>
    optimization. MariaDB does not implement it as of 11.x. In EXPLAIN output
    you will see <code>Using index for skip scan</code> in the Extra column.</p>
    <p>Source: MySQL 8.4 Reference Manual &sect;10.2.1.2
    &ldquo;Range Optimization &mdash; Skip Scan Range Access Method&rdquo;.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="skip_scan",
        title="Skip Scan \u2014 range access without the leading index column",
        subtitle=(
            "How MySQL turns a full table scan into N small range scans "
            "by iterating over distinct values of the leading index column."
        ),
        version_chip="MySQL 8.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
