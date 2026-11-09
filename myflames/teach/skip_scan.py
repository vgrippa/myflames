"""Lesson: Skip Scan (range access without the leading index column).

Given a composite index (A, B), a query with ``WHERE B > 100`` (no equality
on A) normally cannot use the index. Skip Scan scans for each distinct value
of A, then does a range scan on B within each A-group — converting one full
table scan into N range scans (N = NDV of the leading column).

Real query: ``SELECT id, gender, age FROM employees WHERE age BETWEEN 25 AND
30`` with a composite index ``idx_gender_age (gender, age)`` and no condition
on ``gender``. MySQL plans a skip scan only when the index covers the query
(``sql/range_optimizer/index_skip_scan_plan.cc``), so the query reads only
indexed columns (InnoDB keeps the primary key id in every secondary index).
"""
from . import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). skip_scan.js marks finding each
# gender value, the seek to age 25, the range read and the end with
# tl.beat(kind). Function names are on _html.VERIFIED_SERVER_FUNCTIONS
# (checked in mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Skip scan (MySQL)",
        "lines": [
            ("first", 0, "entry = first entry of the index: g = its gender",
             "IndexSkipScanIterator::Read"),
            ("loop", 0, "while there is a gender value g:"),
            ("seek", 1, "seek to the first entry with gender = g and age >= 25"),
            ("range", 1, "read entries while age <= 30, sending each one",
             "IndexSkipScanIterator::Read"),
            ("past", 1, "the entry after age 30 ends this range"),
            ("next", 1, "seek past every g entry: g = the next entry's gender",
             "index_next_different"),
            ("end", 0, "no entry left: the skip scan is done"),
        ],
    },
    "beats": {
        "first-prefix": ["first"],
        "seek": ["loop", "seek"],
        "range": ["range", "past"],
        "next-prefix": ["next"],
        "end": ["next", "end"],
    },
    "quiz": [
        {
            "question": ("Why can't a plain range scan of idx_gender_age (gender, age) serve "
                         "WHERE age BETWEEN 25 AND 30?"),
            "choices": [
                ("Entries are sorted by gender first, so ages 25 to 30 sit in one stretch per "
                 "gender", True,
                 "A range scan reads one stretch of entries; here the matches are split into "
                 "one stretch per gender, which the skip scan visits one by one."),
                ("The index does not store age", False,
                 "Every entry holds both gender and age; the problem is their order, not what "
                 "is stored."),
                ("BETWEEN can never use an index", False,
                 "BETWEEN is a range condition and can use an index whose first column is age."),
            ],
        },
        {
            "question": ("The first column has 1,000 distinct values instead of 2. What happens "
                         "to the skip scan's cost?"),
            "choices": [
                ("It grows: the scan makes 2 seeks for every distinct value", True,
                 "MySQL's cost model counts two repositionings per group, so with many distinct "
                 "values a full scan can be cheaper and the optimizer picks that instead."),
                ("It stays the same, because only ages 25 to 30 are read", False,
                 "The range is read once per distinct value, so each new value adds its own "
                 "seeks and its own range."),
                ("It drops, because each group is smaller", False,
                 "Smaller groups hold fewer entries, but the 2 seeks per value add up to more "
                 "work."),
            ],
        },
        {
            "question": ("Can MySQL use a skip scan for SELECT * FROM employees WHERE age "
                         "BETWEEN 25 AND 30?"),
            "choices": [
                ("No: the index must hold every column the query reads", True,
                 "MySQL only plans a skip scan on an index that covers the query, and SELECT * "
                 "reads columns idx_gender_age does not hold."),
                ("Yes: it reads the other columns from the clustered index", False,
                 "A skip scan is planned only for a covering index, so SELECT * gets another "
                 "plan."),
                ("Only on MariaDB", False,
                 "MariaDB has no skip scan access method; it is a MySQL feature."),
            ],
        },
    ],
}


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
            "-- idx_gender_age (gender, age), no condition on gender\n"
            "SELECT id, gender, age FROM employees\n"
            "WHERE  age BETWEEN 25 AND 30;"
        ),
        note=(
            "Skip scan visits each distinct gender value and reads the "
            "25 to 30 range of age inside it, instead of reading the "
            "whole index. MySQL plans it only when the index holds every "
            "column the query reads (InnoDB adds the primary key id to every "
            "index), so SELECT * would not qualify. EXPLAIN's Extra column "
            "shows Using index for skip scan."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Each box is the run of idx_gender_age entries for one gender, in index order: all F entries, then all M entries, each sorted by age.",
            "The orange bar is the scan's position. It first reads one entry to learn the gender value (orange), then seeks straight to age 25 inside that gender.",
            "Entries with ages 25 to 30 turn green and are sent. The first entry past 30 (blue) ends that gender's range.",
            "Dashed grey entries are never read: the seeks jump over them. Then the scan seeks past the whole gender to the next one.",
            "Next step shows one seek or one range at a time, and the simplified code beside the drawing highlights the lines each step runs.",
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
        "toolbar": _html.stage_toolbar("Ready: press Next step, or Play to run it all"),
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
    <p>3. The query reads only columns in the index (here <code>id</code>,
    <code>gender</code> and <code>age</code>; InnoDB keeps the primary key in
    every index). <code>SELECT *</code> cannot use a skip scan.</p>
    <p>4. The optimizer estimates that doing N sub-range-scans (one per distinct
    leading value, each starting with two seeks) is cheaper than a full table
    scan or a full index scan.</p>
    <p><strong>When it does NOT help:</strong></p>
    <p>1. The leading column has <strong>high NDV</strong> (thousands of distinct
    values) &mdash; too many sub-scans make it slower than a full scan.</p>
    <p>2. The range on the trailing column is <strong>not selective</strong>
    (most rows match) &mdash; you end up reading almost everything anyway.</p>
    <p>3. A better single-column index on the trailing column exists.</p>
    <p><strong>Note:</strong> Skip Scan is a <strong>MySQL-only</strong>
    optimization; MariaDB has no skip scan access method. In EXPLAIN output
    you will see <code>Using index for skip scan</code> in the Extra column.</p>
    <p>Source: MySQL 8.4 Reference Manual &sect;10.2.1.2
    &ldquo;Range Optimization &mdash; Skip Scan Range Access Method&rdquo;.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="skip_scan",
        title="Skip scan: range access without the leading index column",
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
        guide=GUIDE,
    )
