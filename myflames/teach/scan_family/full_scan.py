"""Lesson: Full table scan — what it means and why it hurts.

Shows that the storage engine must touch every row page to evaluate the
predicate when no useful index exists. Contrasts O(n) full scan work with an
indexed range path that is closer to O(log n + k), where k is matching rows.
"""
from .. import _html


_LESSON_JS = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). full_scan.js marks opening the
# scan, each row read and the summary with tl.beat(kind). Function names
# were checked in mysql-server (8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Full table scan",
        "lines": [
            ("open", 0, "open a scan of users (no index on country)", "ha_innobase::rnd_init"),
            ("read", 0, "for each row in users, in primary key order:", "ha_innobase::rnd_next"),
            ("test", 1, "if row.country = 'US':", "FilterIterator::Read"),
            ("send", 2, "send the row to the client"),
            ("else", 1, "else:"),
            ("discard", 2, "discard the row (it was still read)"),
            ("end", 0, "no rows left: the scan ends"),
        ],
    },
    "beats": {
        "start": ["open"],
        "row-match": ["read", "test", "send"],
        "row-skip": ["read", "test", "else", "discard"],
        "done": ["end"],
    },
    "quiz": [
        {
            "question": ("users has 1,000,000 rows, no index on country, and 2% of them are "
                         "in the US. How many rows does WHERE country = 'US' read?"),
            "choices": [
                ("All 1,000,000", True,
                 "Without an index on country the engine cannot tell which rows match "
                 "until it has read each one."),
                ("20,000, only the matching rows", False,
                 "Those are the rows returned; finding them still means reading every row "
                 "and discarding the other 980,000."),
                ("About 3, one per B+tree level", False,
                 "That is the cost of an index lookup, which needs an index on country."),
            ],
        },
        {
            "question": "When can reading the whole table be the cheaper plan?",
            "choices": [
                ("When the query needs a large share of the rows, such as half the table", True,
                 "Reading pages in order beats one index lookup per matching row once most "
                 "rows match, so both MySQL and MariaDB may choose the scan."),
                ("Never, because an index is always faster", False,
                 "The optimizer compares estimated costs, and a scan wins when an index "
                 "would touch most of the table anyway."),
                ("Only on tables without a primary key", False,
                 "Every InnoDB table has a clustered index, and a full scan reads its leaf "
                 "pages in order whether or not you declared a primary key."),
            ],
        },
        {
            "question": "Why does an index on users(country) help this query?",
            "choices": [
                ("It keeps entries sorted by country, so the engine can jump to 'US'", True,
                 "A B+tree finds the first 'US' entry in a few page reads and stops after the "
                 "last one, so it reads about 20,000 entries instead of 1,000,000 rows."),
                ("It makes every row smaller", False,
                 "Rows keep their size; the index is a separate sorted structure that lets "
                 "the engine skip rows that cannot match."),
                ("It stores the query's result for next time", False,
                 "An index holds the indexed column and the primary key of every row, not "
                 "the results of queries."),
            ],
        },
    ],
}


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (full table scan cost model)</h2>
  <div class="control-grid">
    <div class="control">
      <label for="rows">Table rows</label>
      <input type="range" id="rows" name="rows" min="1000" max="10000000" step="1000" value="1000000">
      <div class="hint">Rows in the table: <span data-pill-for="rows">1000000</span></div>
    </div>
    <div class="control">
      <label for="row_size">Average row size (bytes)</label>
      <input type="range" id="row_size" name="row_size" min="64" max="2048" step="32" value="256">
      <div class="hint">Row size: <span data-pill-for="row_size">256</span> bytes</div>
    </div>
    <div class="control">
      <label for="selectivity">Predicate selectivity (%)</label>
      <input type="range" id="selectivity" name="selectivity" min="0.1" max="100" step="0.1" value="2.0">
      <div class="hint">Expected matches: <span data-pill-for="selectivity">2.0</span>% of rows</div>
    </div>
  </div>
</section>
"""

    query_card_html = _html.query_card(
        "SELECT id, name, country FROM users WHERE country = 'US';",
        "No index on users(country) in this scenario, so MySQL must examine each row.",
    )
    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The scan head moves row-by-row through the users table (left) because no index can pre-filter.",
            "Each row is read first, then predicate-tested. Green rows match; other rows were still read and then discarded.",
            "The result box (right) only gets matching rows, but storage work happened for every row.",
            "Readout and chart compare this O(n) path to indexed range access O(log n + k).",
            "Next step reads one row at a time; the simplified code beside the drawing highlights the lines each step runs.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="scan-svg" viewBox="0 0 800 460" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (full table scan)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Rows read {ht("Rows the engine must touch when no index can filter first. For a full scan, this is every row in the table.")}</p><p class="value" id="out-read">—</p></div>
    <div class="item"><p class="label">Rows returned {ht("Rows that pass the WHERE predicate and reach the result set.")}</p><p class="value" id="out-match">—</p></div>
    <div class="item"><p class="label">Bytes read {ht("Approximate table bytes read: rows read × average row size.")}</p><p class="value" id="out-bytes">—</p></div>
    <div class="item"><p class="label">Estimated pages touched {ht("Approximate 16 KiB InnoDB pages touched while scanning. More pages = more I/O.")}</p><p class="value" id="out-pages">—</p></div>
    <div class="item"><p class="label">Indexed path rows touched {ht("Rough comparison path if a useful index existed: B+tree levels + matching rows (O(log n + k)).")}</p><p class="value" id="out-index">—</p></div>
    <div class="item"><p class="label">Work amplification {ht("How many times more rows a full scan touches versus an indexed path for the same selectivity.")}</p><p class="value" id="out-amp">—</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Rows touched vs table size (log–log, selectivity fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — when full scans are acceptable vs dangerous</summary>
  <div class="body">
    <p><strong>Full scan is not always bad.</strong> If the table is tiny, or if the query returns a large fraction of rows, scanning can be cheaper than random index lookups.</p>
    <p><strong>It becomes painful when selectivity is low.</strong> Example: reading 10 million rows to return 0.5% means you are doing near-table-sized I/O for a tiny result set.</p>
    <p><strong>Usual fixes:</strong> create an index on the predicate columns, avoid wrapping indexed columns in functions, and keep table statistics fresh so the optimizer can estimate selectivity correctly.</p>
    <p>In EXPLAIN output, look for operations like <code>Table scan on ...</code> or <code>access_type=ALL</code> as full-scan signals.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="full_scan",
        title="Full table scan: why MySQL reads every row",
        subtitle=(
            "Understand what a full table scan really means: O(n) row reads, "
            "predicate filtering after the read, and why selective predicates "
            "usually need an index."
        ),
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
        guide=GUIDE,
    )

