"""Lesson: Derived Table Materialization.

Animates how a FROM-clause subquery (derived table) that cannot be
merged into the outer query is materialized into a temporary table,
optionally auto-indexed, and then probed by the outer query.

Concrete example: departments joined to a grouped subquery computing
average salary per department.

Both engines plan the derived table's key while optimizing the join
(MySQL Table_ref::generate_keys and JOIN::finalize_derived_keys; MariaDB
generate_derived_keys), so the temp table is created with that key and
each row is indexed as it is written. The lesson draws it that way.
scan_family/derived_table.py re-exports this module's render().
"""
from . import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). derived_table.js marks creating
# stats, each subquery row written, the spill, the read of stats without a
# key, each departments probe and the summary with tl.beat(kind). Function
# names are on _html.VERIFIED_SERVER_FUNCTIONS (checked in mysql-server 8.4.9
# and 9.7.0).
GUIDE = {
    "code": {
        "title": "Derived table: materialize, then probe",
        "lines": [
            ("create", 0, "create temp table stats (with a key on dept_id, if planned)",
             "Table_ref::generate_keys"),
            ("run", 0, "run the subquery: AVG(salary) per dept_id over employees"),
            ("write", 1, "write each result row into stats (its key is filled as it goes)",
             "MaterializeIterator::Init"),
            ("spill", 1, "if stats outgrows tmp_table_size: move it to an on-disk table",
             "create_ondisk_from_heap"),
            ("scan", 0, "no key on stats: read all of stats once for the join"),
            ("loop", 0, "for each row d of departments:"),
            ("probe", 1, "find the stats row with dept_id = d.id"),
            ("emit", 1, "return (d.dept_name, stats.avg_salary)"),
            ("end", 0, "done: stats was filled once, however many rows probed it"),
        ],
    },
    "beats": {
        "create": ["create"],
        "write": ["run", "write"],
        "spill": ["spill"],
        "scan": ["scan"],
        "probe": ["loop", "probe", "emit"],
        "done": ["end"],
    },
    "quiz": [
        {
            "question": ("Why is the stats subquery materialized instead of merged into the "
                         "outer query?"),
            "choices": [
                ("It has GROUP BY and AVG(), so its result must exist before the join", True,
                 "Neither MySQL nor MariaDB can merge a derived table that groups or "
                 "aggregates, so they compute it into a temp table first."),
                ("derived_merge is off by default", False,
                 "derived_merge is on by default in both engines; this subquery is "
                 "materialized because of its GROUP BY and AVG()."),
                ("departments has no index", False,
                 "Merging depends on the shape of the subquery, not on the indexes of the "
                 "outer tables."),
            ],
        },
        {
            "question": "When is the key on stats.dept_id built?",
            "choices": [
                ("While stats is filled: the table is created with the key, and each row is "
                 "indexed as it is written", True,
                 "The optimizer adds the key while it plans the join on dept_id, so the temp "
                 "table already has it when the first row arrives."),
                ("After all rows are written, in a separate step", False,
                 "There is no build step afterwards; each row goes into the key when it is "
                 "inserted."),
                ("Only after you run CREATE INDEX on the derived table", False,
                 "You cannot index a derived table yourself; the optimizer adds the key, and "
                 "MariaDB lets you turn that off with derived_with_keys=off."),
            ],
        },
        {
            "question": "EXPLAIN shows key <auto_key0> on the table <derived2>. What is that key?",
            "choices": [
                ("The index the optimizer added to the derived table's temp table", True,
                 "MySQL names generated keys <auto_keyN> (MariaDB shows key0), and each "
                 "departments row uses it for one lookup in stats."),
                ("An index on employees that the subquery uses", False,
                 "The key belongs to the derived table <derived2>, the temp table stats, "
                 "not to employees."),
                ("A hint that you should create an index", False,
                 "It is an index that already exists for this query, made and dropped by "
                 "the server."),
            ],
        },
    ],
}


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
      <div class="hint">On by default in both engines: the optimizer adds a key on the join column (MariaDB can turn it off with derived_with_keys=off).</div>
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
            "The grouped subquery cannot be merged, so the server materializes it into "
            "a temp table (with a key on dept_id), then probes it once per departments row."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Top: the temp table stats. The key on dept_id is in place first; then each "
            "subquery row (dept_id and its average salary) fills a dashed slot.",
            "The bar under stats shows its size against tmp_table_size (16 MiB). Past it, "
            "stats moves to an on-disk table.",
            "Lower left: departments. A purple pill such as \"Eng id=3\" travels up to "
            "the stats row with that dept_id, one lookup through the key.",
            "A green pill carries the joined row down to the result rows (lower right).",
            "Next step writes or probes one row at a time; the simplified code beside the "
            "drawing highlights the lines each step runs.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  " + query_card_html + "\n"
        "  " + explainer_html + "\n"
        "  " + _html.stage_toolbar("Ready: press Next step, or Play to run it all") + "\n"
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
        + ht("The key the optimizer adds on the join column (dept_id) so each outer row does one lookup.") +
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
    <p><strong>Auto-key generation:</strong> when the outer query joins the
    derived table with an equality, the optimizer adds an index on the join
    column to the temp table (EXPLAIN shows key <code>&lt;auto_key0&gt;</code>
    in MySQL, <code>key0</code> in MariaDB). The key is planned while the join
    is optimized, so the table is created with it and every row is indexed as
    it is written. This turns a read of the whole temp table into one indexed
    lookup per outer row. MariaDB can turn it off with
    <code>optimizer_switch='derived_with_keys=off'</code>; MySQL has no switch
    for it.</p>
    <p><strong>MariaDB split materialization:</strong> for this exact shape (a
    grouped derived table joined on its GROUP BY column), MariaDB may instead
    compute the average only for the departments the outer query needs, if
    <code>employees(dept_id)</code> has an index. EXPLAIN then says
    <code>LATERAL DERIVED</code> (<code>optimizer_switch</code>
    <code>split_materialized</code>, on by default).</p>
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
      <li>Consider rewriting the query to avoid the derived table, or make sure
      the outer query joins it with an equality so the optimizer can add a key.</li>
    </ul>
    <p>Sources: MySQL 8.4 reference manual &sect;10.2.2.4 &ldquo;Optimizing
    Derived Tables&rdquo;; MySQL 8.4 reference manual &sect;7.1.8 &ldquo;Server
    System Variables&rdquo;.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="derived_table",
        title="Derived table materialization: a subquery becomes a temp table",
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
        guide=GUIDE,
    )
