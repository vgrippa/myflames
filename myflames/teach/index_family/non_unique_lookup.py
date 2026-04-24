"""Lesson: Non-Unique Key Lookup.

Teaches the full operator flow, not only the label:
1) descend the secondary B+tree (root -> internal -> leaf range),
2) scan matching leaf entries,
3) for non-covering plans, follow row-id pointers to clustered rows.
"""
from .. import _html


_LESSON_JS = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (non-unique lookup)</h2>
  <div class="control-grid">
    <div class="control">
      <label for="rows">Rows in table</label>
      <input type="range" id="rows" name="rows" min="1000" max="10000000" step="1000" value="1000000">
      <div class="hint">Table rows: <span data-pill-for="rows">1000000</span></div>
    </div>
    <div class="control">
      <label for="selectivity">Leaf-range selectivity (%)</label>
      <input type="range" id="selectivity" name="selectivity" min="0.1" max="40" step="0.1" value="2.0">
      <div class="hint">Percent of table rows whose leaf entries match <code>country='US'</code>: <span data-pill-for="selectivity">2.0</span>%</div>
    </div>
    <div class="control">
      <label for="covering">Covering index?</label>
      <select id="covering" name="covering">
        <option value="false" selected>No — need clustered row fetches</option>
        <option value="true">Yes — index contains needed columns</option>
      </select>
      <div class="hint">Covering avoids the second table lookup per match.</div>
    </div>
  </div>
</section>
"""
    query_card_html = _html.query_card(
        "SELECT id, name, country\nFROM users\nWHERE country = 'US';",
        "With index idx_users_country(country), this appears in EXPLAIN as Index lookup / Index range scan: descend the secondary B+tree, scan matching leaf entries, then follow row-id pointers.",
    )
    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The orange token is your predicate value (country='US') moving through root, internal, and leaf B+tree pages.",
            "At the leaf level, one key value maps to many entries: each entry stores a row-id pointer.",
            "Non-covering path: every matched entry triggers a clustered-row fetch by row-id (extra I/O).",
            "Covering path: if selected columns are in the index payload, row-id fetches are skipped.",
        ],
    )
    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="lookup-svg" viewBox="0 0 860 470" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""
    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (B+tree traversal + leaf scan + row-id fetch)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">B+tree height {ht("Approximate number of index pages traversed to reach matching leaf range.")}</p><p class="value" id="out-height">—</p></div>
    <div class="item"><p class="label">Descent page reads {ht("Root + internal + first matching leaf page reads before scanning the range.")}</p><p class="value" id="out-descent">—</p></div>
    <div class="item"><p class="label">Matched leaf entries {ht("Index entries in the key/range condition. Non-unique means this can be many.")}</p><p class="value" id="out-match">—</p></div>
    <div class="item"><p class="label">Index reads {ht("Tree traversal + matched index entries touched.")}</p><p class="value" id="out-index">—</p></div>
    <div class="item"><p class="label">Clustered row-id fetches {ht("Extra table-row reads for non-covering lookups (one per matched entry).")}</p><p class="value" id="out-fetch">—</p></div>
    <div class="item"><p class="label">Total reads (rough) {ht("Index reads + clustered row fetches. Covering index removes row-fetch part.")}</p><p class="value" id="out-total">—</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Work vs table size (log–log, selectivity fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""
    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — what actually happens in the background</summary>
  <div class="body">
    <p>For <code>country='US'</code>, MySQL first descends the secondary B+tree (root -> internal -> first matching leaf).
    Then it walks adjacent leaf entries while the key still matches US.</p>
    <p>Each matching leaf entry contains the primary key (row-id) pointer. On non-covering plans, that pointer triggers
    another lookup in the clustered PRIMARY tree to fetch full row columns.</p>
    <p>That second hop is the expensive part. If the index is covering, the operator can return directly from leaf payload
    and skip clustered fetches.</p>
  </div>
</details>
"""
    return _html.render_page(
        lesson_id="non_unique_lookup",
        title="Non-Unique Key Lookup — index hits that return many rows",
        subtitle="Understand how B+tree traversal, leaf-range scanning, and row-id fetches compose the real work of this operator.",
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
    )

