"""Lesson: Full table scan — what it means and why it hurts.

Shows that the storage engine must touch every row page to evaluate the
predicate when no useful index exists. Contrasts O(n) full scan work with an
indexed range path that is closer to O(log n + k), where k is matching rows.
"""
from .. import _html


_LESSON_JS = _html.load_lesson_js(__file__)


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
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
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
        title="Full table scan — why MySQL reads every row",
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
    )

