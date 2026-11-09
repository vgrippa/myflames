"""Lesson: Filter operator (WHERE predicate).

Explains what a plan-stage filter means: rows arrive from the child operator,
the predicate is evaluated row-by-row, and only matching rows flow forward.
"""
from .. import _html


_LESSON_JS = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). filter.js marks starting the
# child scan, each row tested and the summary with tl.beat(kind). Function
# names were checked in mysql-server (8.4.9 and 9.7.0); MariaDB tests the
# same condition per row in evaluate_join_record (sql/sql_select.cc).
GUIDE = {
    "code": {
        "title": "Filter operator",
        "lines": [
            ("child", 0, "start the child: a table scan of orders (no index on total)",
             "ha_innobase::rnd_init"),
            ("loop", 0, "for each row the child returns:", "FilterIterator::Read"),
            ("test", 1, "evaluate WHERE total > 500 on that row"),
            ("pass", 2, "true: return the row to the parent operator"),
            ("drop", 2, "false: drop the row and ask the child for the next one"),
            ("end", 0, "the child has no rows left: the filter is done too"),
        ],
    },
    "beats": {
        "start": ["child"],
        "row-pass": ["loop", "test", "pass"],
        "row-drop": ["loop", "test", "drop"],
        "done": ["end"],
    },
    "quiz": [
        {
            "question": ("orders has 1,000,000 rows, no index on total, and 5% of them have "
                         "total > 500. How many rows does the filter test?"),
            "choices": [
                ("All 1,000,000", True,
                 "The table scan hands every row to the filter, and the filter tests each "
                 "one before it can keep or drop it."),
                ("50,000, only the rows it keeps", False,
                 "Those are the rows it returns; the other 950,000 were still read and "
                 "tested, then dropped."),
                ("None, the storage engine skips the small orders", False,
                 "Without an index on total the engine cannot tell which rows match, so it "
                 "returns all of them to the filter."),
            ],
        },
        {
            "question": ("MySQL's EXPLAIN ANALYZE shows the table scan returned 1,000,000 rows "
                         "and the Filter above it returned 50,000. What does that gap tell you?"),
            "choices": [
                ("950,000 rows were read and then thrown away", True,
                 "The filter kept 5% of what the scan read, so most of the reading was "
                 "wasted work (MariaDB's ANALYZE shows the same thing as r_filtered = 5)."),
                ("The filter returned wrong results", False,
                 "Dropping the rows that fail total > 500 is exactly what the filter is "
                 "for; the counts are supposed to differ."),
                ("The table scan stopped early", False,
                 "The scan returned 1,000,000 rows, the whole table, so it read every row."),
            ],
        },
        {
            "question": "What would make this query read far fewer rows?",
            "choices": [
                ("An index on orders(total), so a range scan starts at total > 500", True,
                 "The index keeps entries sorted by total, so the engine reads only the "
                 "matching entries instead of handing every row to a filter."),
                ("Selecting fewer columns", False,
                 "Smaller rows are cheaper to copy, but the scan still reads and tests "
                 "every row."),
                ("A bigger sort_buffer_size", False,
                 "That buffer is used for sorting; this query does not sort, and the filter "
                 "has no buffer to tune."),
            ],
        },
    ],
}


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (filter stage)</h2>
  <div class="control-grid">
    <div class="control">
      <label for="input_rows">Input rows</label>
      <input type="range" id="input_rows" name="input_rows" min="1000" max="10000000" step="1000" value="1000000">
      <div class="hint">Rows entering filter: <span data-pill-for="input_rows">1000000</span></div>
    </div>
    <div class="control">
      <label for="selectivity">Predicate selectivity (%)</label>
      <input type="range" id="selectivity" name="selectivity" min="0.1" max="100" step="0.1" value="5.0">
      <div class="hint">Rows kept by WHERE: <span data-pill-for="selectivity">5.0</span>%</div>
    </div>
  </div>
</section>
"""
    query_card_html = _html.query_card(
        "SELECT order_id, total FROM orders WHERE total > 500;",
        "No index on orders(total), so the child is a table scan that returns every row. "
        "The Filter node above it evaluates WHERE total > 500 on each row before passing "
        "the row upward.",
    )
    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left: the rows the child operator (a table scan of orders) returns, one at a time.",
            "Each row travels as a pill labelled with its order_id and total into the "
            "Filter box (middle), which tests total > 500 on every incoming row and shows "
            "the answer.",
            "Green rows satisfy the predicate and continue to the parent operator (right).",
            "Red rows drop out below the Filter: they were read and tested, then thrown away.",
            "Next step tests one row at a time; the simplified code beside the drawing "
            "highlights the lines each step runs.",
        ],
    )
    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="filter-svg" viewBox="0 0 800 390" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""
    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (filter operator)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Input rows {ht("Rows entering the filter stage from its child operator.")}</p><p class="value" id="out-in">—</p></div>
    <div class="item"><p class="label">Output rows {ht("Rows that satisfy the WHERE predicate and continue upward in the plan.")}</p><p class="value" id="out-out">—</p></div>
    <div class="item"><p class="label">Dropped rows {ht("Rows evaluated but rejected by the predicate.")}</p><p class="value" id="out-drop">—</p></div>
    <div class="item"><p class="label">Selectivity {ht("Percentage of input rows that survive the predicate.")}</p><p class="value" id="out-sel">—</p></div>
  </div>
  <div class="explanation" id="out-exp"></div>
  <div class="complexity-chart">
    <p class="chart-title">Rows evaluated vs rows returned (log–log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""
    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — why filter nodes still matter</summary>
  <div class="body">
    <p>A Filter node is not always bad — it is normal in many plans. The key question is how many rows arrive at this stage.</p>
    <p>If a selective predicate is pushed down into an index access path, far fewer rows reach this filter stage.</p>
    <p>When you see large input rows and tiny output rows, consider adding/adjusting indexes so filtering happens earlier.</p>
  </div>
</details>
"""
    return _html.render_page(
        lesson_id="filter",
        title="Filter operator: WHERE predicate row-by-row",
        subtitle="Understand the internal execution loop of a Filter stage: evaluate every input row, then keep or discard.",
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
        guide=GUIDE,
    )

