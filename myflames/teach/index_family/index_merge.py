"""Lesson: Index Merge — union / intersection / sort-union.

Animates how MySQL uses two separate index scans on the same table
and combines their row-ID sets instead of falling back to a full
table scan.

Real query: ``SELECT * FROM products WHERE category_id = 5 OR
supplier_id = 12`` with separate indexes on ``category_id`` and
``supplier_id``.
"""
from .. import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (Index Merge)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="a_rows">idx_category rows: <span class="value-pill" data-pill-for="a_rows">5000</span></label>
      <input type="range" id="a_rows" name="a_rows" min="100" max="1000000" step="100" value="5000">
      <div class="hint">Rows returned by the idx_category index scan.</div>
    </div>

    <div class="control">
      <label for="b_rows">idx_supplier rows: <span class="value-pill" data-pill-for="b_rows">3000</span></label>
      <input type="range" id="b_rows" name="b_rows" min="100" max="1000000" step="100" value="3000">
      <div class="hint">Rows returned by the idx_supplier index scan.</div>
    </div>

    <div class="control">
      <label for="overlap">Overlap (%): <span class="value-pill" data-pill-for="overlap">10</span></label>
      <input type="range" id="overlap" name="overlap" min="0" max="100" step="1" value="10">
      <div class="hint">Percentage of rows present in both index scans (duplicates).</div>
    </div>

    <div class="control">
      <label for="variant">Merge variant:</label>
      <select id="variant" name="variant">
        <option value="union" selected>Union (OR condition)</option>
        <option value="intersection">Intersection (AND condition)</option>
        <option value="sort_union">Sort-Union (OR, unsorted ranges)</option>
      </select>
      <div class="hint">Union for OR, intersection for AND, sort-union when row-IDs aren't pre-sorted.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Separate indexes on category_id and supplier_id\n"
            "SELECT *\n"
            "FROM   products\n"
            "WHERE  category_id = 5\n"
            "   OR  supplier_id = 12;"
        ),
        note=(
            "No composite index covers both conditions, but MySQL can scan "
            "both single-column indexes and merge row-ID sets before clustered fetch."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Engine executes two independent index scans and collects row-ID lists.",
            "Merge stage combines lists as union/sort-union (OR) or intersection (AND).",
            "Duplicate row-IDs are removed before table fetch to avoid repeated reads.",
            "Only merged row-IDs are fetched from clustered index in the final step.",
            "Composite indexes can remove this merge stage entirely.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="merge-svg" viewBox="0 0 800 420" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (Index Merge)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">idx_category rows {ht("Row-IDs returned by the first index scan. These are sorted by primary key for InnoDB.")}</p><p class="value" id="out-a-rows">\u2014</p></div>
    <div class="item"><p class="label">idx_supplier rows {ht("Row-IDs returned by the second index scan.")}</p><p class="value" id="out-b-rows">\u2014</p></div>
    <div class="item"><p class="label">Overlapping rows {ht("Row-IDs present in both index scans. For union, these are de-duplicated. For intersection, these are the result.")}</p><p class="value" id="out-overlap">\u2014</p></div>
    <div class="item"><p class="label">Rows fetched {ht("Final number of rows fetched from the clustered index after merging. This is the actual I/O cost.")}</p><p class="value" id="out-merged">\u2014</p></div>
    <div class="item"><p class="label">Variant {ht("Union = OR conditions (combine both sets). Intersection = AND conditions (keep only common rows). Sort-union = OR with unsorted ranges.")}</p><p class="value" id="out-variant">\u2014</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Rows fetched vs index scan size (log\u2013log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — when does MySQL choose index merge?</summary>
  <div class="body">
    <p>The optimizer considers index merge when a query has OR or AND conditions
    on columns with <strong>separate single-column indexes</strong> and no
    composite index covers the full predicate.</p>

    <p><strong>Index merge union</strong> (OR): The optimizer scans each
    index separately, then merges the sorted row-ID streams with de-duplication.
    This avoids a full table scan when each index is selective enough.</p>

    <p><strong>Index merge intersection</strong> (AND): Both indexes are scanned,
    and only row-IDs present in <strong>both</strong> streams are kept. This is
    useful when no single index is selective enough, but together they are.</p>

    <p><strong>Index merge sort-union</strong> (OR with ranges): When the row-IDs
    from each index scan are not guaranteed to be in PK order (e.g. range scans),
    MySQL sorts each set first before merging. Slower than union but still faster
    than a full table scan.</p>

    <p>You can control this behaviour with <code>optimizer_switch</code>:
    <code>index_merge=on</code>, <code>index_merge_union=on</code>,
    <code>index_merge_intersection=on</code>,
    <code>index_merge_sort_union=on</code>.</p>

    <p><strong>A composite index is almost always better than index merge.</strong>
    If you see index merge in your EXPLAIN output, consider whether a composite
    index would serve the query more efficiently.</p>

    <p>Sources: MySQL 8.4 reference manual §10.2.1.3 "Index Merge Optimization";
    MariaDB Knowledge Base "Index Merge Optimization".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="index_merge",
        title="Index Merge — combining two index scans",
        subtitle=(
            "Watch MySQL scan two separate indexes, collect row-IDs, and "
            "merge them with union, intersection, or sort-union."
        ),
        version_chip="MySQL 5.1+ \u2022 MariaDB 5.1+",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
