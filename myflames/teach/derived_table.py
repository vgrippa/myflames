"""Lesson: Derived Table Materialization.

Animates how a FROM-clause subquery (derived table) that cannot be
merged into the outer query is materialized into a temporary table,
optionally auto-indexed, and then probed by the outer query.

Concrete example: departments joined to a grouped subquery computing
average salary per department.
"""
from . import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render():
    # type: () -> str
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (derived table materialization)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="subquery_rows">Subquery rows: <span class="value-pill" data-pill-for="subquery_rows">10000</span></label>
      <input type="range" id="subquery_rows" name="subquery_rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Rows produced by the FROM-clause subquery.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Average size of each materialized row.</div>
    </div>

    <div class="control">
      <label for="outer_rows">Outer query rows: <span class="value-pill" data-pill-for="outer_rows">1000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="100" max="500000" step="100" value="1000">
      <div class="hint">Rows in the outer query that probe the temp table.</div>
    </div>

    <div class="control">
      <label for="has_index">Auto-index on temp table:</label>
      <input type="checkbox" id="has_index" name="has_index" checked>
      <div class="hint">MySQL can auto-generate a B+tree index on the join key of the temp table.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Derived table: subquery materialized into tmp\n"
            "SELECT d.dept_name, stats.avg_salary\n"
            "FROM   departments d\n"
            "JOIN   (SELECT dept_id, AVG(salary) AS avg_salary\n"
            "        FROM employees\n"
            "        GROUP BY dept_id) AS stats\n"
            "  ON   d.id = stats.dept_id;"
        ),
        note=(
            "MySQL materializes the grouped subquery into a temp table, "
            "optionally adds an auto-index, then probes it from the outer query."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The subquery executes fully, writing rows into a temporary table (yellow).",
            "If the temp table exceeds 16 MiB (tmp_table_size), it spills to disk.",
            "MySQL may auto-generate a B+tree index on the join column for fast lookups.",
            "The outer query probes the temp table for each of its rows.",
            "Matched results flow out as green result tuples.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  " + query_card_html + "\n"
        "  " + explainer_html + "\n"
        "  " + _html.stage_toolbar("Ready \u2014 press Play") + "\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="derived-svg" viewBox="0 0 800 440"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "    " + _html.phase_nav() + "\n"
        "  </div>\n"
        "</section>"
    )

    ht = _html.help_tip
    readout_html = (
        '<section class="readout">\n'
        "  <h2>Cost readout (derived table materialization)</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Subquery rows '
        + ht("Total rows produced by the FROM-clause subquery.") +
        '</p><p class="value" id="out-subquery-rows">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Materialization writes '
        + ht("Rows written into the temp table during materialization.") +
        '</p><p class="value" id="out-mat-writes">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Temp table size '
        + ht("Estimated size of the materialized temp table (rows \u00d7 row_size).") +
        '</p><p class="value" id="out-tmp-size">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Spills to disk? '
        + ht("If the temp table exceeds 16 MiB, it spills to an on-disk temp table.") +
        '</p><p class="value ok" id="out-spills">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Auto-index? '
        + ht("MySQL can auto-generate a B+tree index on the join key for faster lookups.") +
        '</p><p class="value ok" id="out-auto-index">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Probe reads '
        + ht("Read operations when the outer query probes the temp table.") +
        '</p><p class="value" id="out-probe-reads">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Total I/O '
        + ht("Materialization writes + probe reads.") +
        '</p><p class="value" id="out-total-io">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">Materialized (indexed vs scan) vs merged (log\u2013log)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more &mdash; derived table materialization vs merging</summary>
  <div class="body">
    <p>The <code>derived_merge</code> optimizer switch (ON by default since MySQL 5.7)
    lets the optimizer merge a derived table into the outer query, eliminating the
    temp table entirely. When merge is possible, there is <strong>zero materialization
    overhead</strong> &mdash; the subquery's tables are accessed directly.</p>
    <p><strong>When can't MySQL merge?</strong> Materialization is the fallback when
    the subquery contains: <code>GROUP BY</code>, <code>DISTINCT</code>,
    <code>LIMIT</code>, <code>UNION</code>, aggregate functions, or
    user-defined variables. In these cases, the result must be fully computed
    before the outer query can use it.</p>
    <p><strong>Auto-key generation</strong> (since MySQL 5.7, refined in 8.0):
    when the outer query has an equi-join condition on the derived table,
    MySQL automatically creates a hash or B+tree index on the temp table's
    join column. This turns an O(n) full-scan probe into an O(log n) indexed
    lookup per outer row.</p>
    <p><strong>Temp table sizing:</strong> The materialized temp table starts in
    memory. If it exceeds <code>tmp_table_size</code> (default 16 MiB), it spills
    to disk. Large derived tables with many rows or wide rows are more likely
    to spill, adding disk I/O overhead.</p>
    <p><strong>Optimization tips:</strong></p>
    <ul>
      <li>Check <code>EXPLAIN</code> for <code>&lt;derived2&gt;</code> or
      <code>MATERIALIZED</code> to confirm materialization.</li>
      <li>If <code>derived_merge=on</code> and it still materializes, the subquery
      likely has GROUP BY/DISTINCT/LIMIT preventing merge.</li>
      <li>Consider rewriting the query to avoid the derived table, or ensure
      the join column has an index hint for the auto-key.</li>
    </ul>
    <p>Sources: MySQL 8.4 reference manual &sect;10.2.2.4 &ldquo;Optimizing
    Derived Tables&rdquo;; MySQL 8.4 reference manual &sect;7.1.8 &ldquo;Server
    System Variables&rdquo;.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="derived_table",
        title="Derived Table Materialization",
        subtitle=(
            "Watch a FROM-clause subquery materialize into a temp table, "
            "get an auto-index, and then get probed by the outer query."
        ),
        version_chip="MySQL 8.4 \u2022 MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS_TEMPLATE,
    )
