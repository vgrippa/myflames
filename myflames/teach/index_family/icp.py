"""Lesson: Index Condition Pushdown (ICP).

Side-by-side animation: without ICP, every index-matching row triggers
a clustered-index row fetch before the server filters. With ICP, the
storage engine evaluates the pushed condition on the index entry first,
skipping row fetches for non-matching rows.

Real query: ``SELECT * FROM employees WHERE last_name LIKE 'S%' AND
first_name LIKE 'J%'`` with a composite index on ``(last_name, first_name)``.
"""
from .. import _html
from .._cost_model import INNODB_PAGE_SIZE_DEFAULT


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (Index Condition Pushdown)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="index_rows">Index rows matching range: <span class="value-pill" data-pill-for="index_rows">5000</span></label>
      <input type="range" id="index_rows" name="index_rows" min="100" max="1000000" step="100" value="5000">
      <div class="hint">Rows matching the leading index condition (last_name LIKE 'S%').</div>
    </div>

    <div class="control">
      <label for="selectivity">ICP selectivity (%): <span class="value-pill" data-pill-for="selectivity">20</span></label>
      <input type="range" id="selectivity" name="selectivity" min="1" max="100" step="1" value="20">
      <div class="hint">Percentage of rows also matching the pushed condition (first_name LIKE 'J%').</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Composite index on (last_name, first_name)\n"
            "SELECT *\n"
            "FROM   employees\n"
            "WHERE  last_name  LIKE 'S%'\n"
            "  AND  first_name LIKE 'J%';"
        ),
        note=(
            "Without ICP: range scan on last_name and fetch every row before checking first_name. "
            "With ICP: InnoDB evaluates first_name on index entries first, so many clustered-row fetches are skipped."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Range scan first finds entries by last_name LIKE 'S%' in the secondary index.",
            "Without ICP, every entry triggers clustered-row fetch, then first_name predicate is tested.",
            "With ICP, first_name predicate is tested on index payload before fetch.",
            "Only entries that pass predicate trigger clustered-row reads in ICP path.",
            "Watch fetch counters diverge to see saved random I/O.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="icp-svg" viewBox="0 0 800 480" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (Index Condition Pushdown)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Index rows scanned {ht("Total rows matching the range condition on the leading index column. All of these would need a row fetch without ICP.")}</p><p class="value" id="out-scanned">\u2014</p></div>
    <div class="item"><p class="label">Row fetches without ICP {ht("Without ICP, every index row triggers a clustered-index lookup to fetch the full row, then the server filters.")}</p><p class="value" id="out-without">\u2014</p></div>
    <div class="item"><p class="label">Row fetches with ICP {ht("With ICP, only rows matching the pushed condition on trailing index columns trigger a clustered-index fetch.")}</p><p class="value" id="out-with">\u2014</p></div>
    <div class="item"><p class="label">Row fetches saved {ht("The difference: how many unnecessary clustered-index lookups ICP eliminates by checking the condition at the index level.")}</p><p class="value ok" id="out-saved">\u2014</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Row fetches vs index scan size (log\u2013log, selectivity fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — when does ICP activate?</summary>
  <div class="body">
    <p>Index Condition Pushdown was introduced in MySQL 5.6 and is enabled by
    default (<code>optimizer_switch='index_condition_pushdown=on'</code>).</p>
    <p>ICP applies when:</p>
    <p>1. The query uses a <strong>range</strong>, <strong>ref</strong>, or
    <strong>eq_ref</strong> access type on a composite index.</p>
    <p>2. There are additional WHERE conditions that reference columns
    <strong>present in the index</strong> but not used by the access method
    (e.g. trailing columns of a composite index, or conditions that can't
    form a range but can be checked against the index entry).</p>
    <p>3. The table is InnoDB or MyISAM (InnoDB benefits most because
    avoiding a clustered-index lookup is expensive — it's a random I/O
    for non-covering indexes).</p>
    <p>You can see ICP in action in <code>EXPLAIN</code> output: the
    <code>Extra</code> column will show <strong>Using index condition</strong>
    instead of the usual <strong>Using where</strong>.</p>
    <p>ICP does <strong>not</strong> help covering indexes (the row fetch
    is already avoided) or full table scans (there's no index to push to).</p>
    <p>Sources: MySQL 8.4 reference manual §10.2.1.6 "Index Condition Pushdown
    Optimization"; MariaDB Knowledge Base "Index Condition Pushdown".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="icp",
        title="Index Condition Pushdown — filtering inside InnoDB",
        subtitle=(
            "See how ICP checks trailing index columns before fetching the row, "
            "saving unnecessary clustered-index lookups."
        ),
        version_chip="MySQL 5.6+ \u2022 MariaDB 5.3+",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
