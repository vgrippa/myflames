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

# Guided steps (see _html.normalize_guide). icp.js marks the start of the
# range scan, one step per index entry (both sides handle the same entry)
# and the summary with tl.beat(kind). Function names are on
# _html.VERIFIED_SERVER_FUNCTIONS (checked in mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Index condition pushdown",
        "lines": [
            ("open", 0, "range scan (last_name, first_name) for last_name LIKE 'S%'"),
            ("each", 0, "for each index entry in the range:"),
            ("off", 1, "without ICP: read the row, then the server tests first_name"),
            ("icp", 1, "with ICP: InnoDB tests first_name LIKE 'J%' on the entry",
             "row_search_idx_cond_check"),
            ("skip", 2, "no match: skip the entry, no row read"),
            ("fetch", 2, "match: read the row from the clustered index by id",
             "Row_sel_get_clust_rec_for_mysql"),
            ("send", 1, "send the row if first_name LIKE 'J%'"),
            ("end", 0, "the next entry is past 'S%': the range scan ends"),
        ],
    },
    "beats": {
        "start": ["open"],
        "entry-match": ["each", "off", "icp", "fetch", "send"],
        "entry-skip": ["each", "off", "icp", "skip"],
        "done": ["end"],
    },
    "quiz": [
        {
            "question": ("10 entries match last_name LIKE 'S%' and 5 of those first names start "
                         "with J. With ICP on, how many rows does InnoDB read from the clustered "
                         "index for SELECT *?"),
            "choices": [
                ("5, one per entry that passes first_name LIKE 'J%'", True,
                 "InnoDB tests the pushed condition on each index entry and reads the row only "
                 "for entries that pass."),
                ("10, one per entry in the range", False,
                 "That is the count without ICP, when every entry's row is read before the "
                 "server tests first_name."),
                ("0, because the index holds both names", False,
                 "SELECT * asks for columns the index does not hold, so each passing entry "
                 "still needs its row."),
            ],
        },
        {
            "question": "With ICP on, where is first_name LIKE 'J%' tested?",
            "choices": [
                ("Inside InnoDB, on the index entry, before the row is read", True,
                 "The server hands the condition to the storage engine, which checks it on the "
                 "entry's first_name before it looks the row up by id."),
                ("In the server, after InnoDB returns the full row", False,
                 "That is how the condition is tested without ICP, which is why every row in "
                 "the range is read."),
                ("In the clustered index, while the row is read", False,
                 "The test happens before the clustered index is touched; skipping that read "
                 "is the whole point."),
            ],
        },
        {
            "question": "Which text in EXPLAIN's Extra column shows that ICP is used?",
            "choices": [
                ("Using index condition", True,
                 "MySQL and MariaDB both print Using index condition when a condition is "
                 "pushed down to the storage engine."),
                ("Using index", False,
                 "Using index means a covering index: every column comes from the index and no "
                 "row is read at all."),
                ("Using where", False,
                 "Using where means the server tests a condition on rows it has already read."),
            ],
        },
    ],
}


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
            "Each row of the two panels is one entry of the (last_name, first_name) index that matches last_name LIKE 'S%'. The entry holds both names and the primary key id, not the rest of the row.",
            "Left, without ICP: every entry's row is read from the clustered index (red), then the server tests first_name LIKE 'J%' and throws away the rows that fail.",
            "Right, with ICP: InnoDB tests first_name on the entry itself (blue) and reads the row only when it starts with J (green), so the other entries cost no row read.",
            "The counters under each panel show the row reads so far; watch them diverge.",
            "Next step handles one entry on both sides, and the simplified code beside the drawing highlights the lines each step runs.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
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
    <p>1. The query reads through a <strong>secondary index</strong> with a
    range scan or a lookup (<strong>range</strong>, <strong>ref</strong>,
    <strong>eq_ref</strong> or <strong>ref_or_null</strong>). It is not used for
    a const lookup, and InnoDB does not use it on the clustered primary key,
    where the whole row is already in hand.</p>
    <p>2. There are additional WHERE conditions that reference columns
    <strong>present in the index</strong> but not used by the access method
    (e.g. trailing columns of a composite index, or conditions that can't
    form a range but can be checked against the index entry).</p>
    <p>3. The engine supports it: InnoDB or MyISAM in MySQL; also Aria and
    MyRocks in MariaDB. InnoDB benefits most because avoiding a
    clustered-index lookup is expensive: it's a random I/O for
    non-covering indexes.</p>
    <p>You can see ICP in action in <code>EXPLAIN</code> output: the
    <code>Extra</code> column shows <strong>Using index condition</strong>
    (with <strong>Using where</strong> next to it when part of the condition
    still runs in the server).</p>
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
        title="Index condition pushdown: filtering inside InnoDB",
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
        guide=GUIDE,
    )
