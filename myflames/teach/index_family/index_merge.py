"""Lesson: Index Merge — union / intersection / sort-union.

Animates how MySQL and MariaDB use two separate index scans on the same
table and combine their row-ID (primary key) lists instead of falling back
to a full table scan.

Real query: ``SELECT * FROM products WHERE category_id = 5 OR
supplier_id = 12`` with separate indexes on ``category_id`` and
``supplier_id``; the query card follows the Merge variant (AND for
intersection, ranges for sort-union).
"""
from .. import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). index_merge.js marks each index
# scan, the merge (one kind per variant) and the row reads with
# tl.beat(kind). Function names are on _html.VERIFIED_SERVER_FUNCTIONS
# (checked in mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Index merge",
        "lines": [
            ("for", 0, "for each index in (idx_category, idx_supplier):"),
            ("scan", 1, "read its entries for its condition and keep each entry's id"),
            ("merge", 0, "merge the two id lists:"),
            ("union", 1, "union (OR): both lists are in id order, keep each id once",
             "RowIDUnionIterator::Read"),
            ("inter", 1, "intersection (AND): keep the ids found in both lists",
             "RowIDIntersectionIterator::Read"),
            ("sort", 1, "sort-union (OR of ranges): sort all ids, keep each once",
             "IndexMergeIterator::Init"),
            ("fetch", 0, "for each id left, in id order: read the row by id"),
            ("send", 1, "send the row"),
        ],
    },
    "beats": {
        "scan-a": ["for", "scan"],
        "scan-b": ["for", "scan"],
        "merge-union": ["merge", "union"],
        "merge-intersection": ["merge", "inter"],
        "merge-sort-union": ["merge", "sort"],
        "fetch": ["fetch", "send"],
    },
    "quiz": [
        {
            "question": ("WHERE category_id = 5 OR supplier_id = 12: Webcam (id 12) is found by "
                         "both index scans. How many times is its row read from the clustered "
                         "index?"),
            "choices": [
                ("Once", True,
                 "The merge drops the repeated id before its row is read, so a product found "
                 "by both scans costs one lookup."),
                ("Twice, once per index", False,
                 "Two separate queries would read it twice; inside an index merge the repeated "
                 "id is dropped before the row is read."),
                ("Never, because the indexes hold every column", False,
                 "SELECT * needs columns neither single-column index holds, so each id still "
                 "costs a clustered index lookup."),
            ],
        },
        {
            "question": ("Why can a union of two equality scans merge the ids without sorting "
                         "them first?"),
            "choices": [
                ("Equal keys are stored in primary key order, so each scan returns sorted ids",
                 True,
                 "An InnoDB secondary entry is (key, id), so all category_id = 5 entries come "
                 "out in id order, and two sorted lists merge in one pass."),
                ("The optimizer sorts the table first", False,
                 "Nothing is sorted for this plan; the order comes from how the index stores "
                 "equal keys."),
                ("Index merge only works on primary keys", False,
                 "Both scans here use secondary indexes; only the row reads at the end use the "
                 "primary key."),
            ],
        },
        {
            "question": ("For WHERE category_id = 5 AND supplier_id = 12, which change usually "
                         "beats an index merge intersection?"),
            "choices": [
                ("One composite index on (category_id, supplier_id)", True,
                 "Its entries are sorted by both columns, so one range scan finds exactly the "
                 "matching entries, with no second scan and no merge."),
                ("A larger sort_buffer_size", False,
                 "The sort buffer is where sort-union sorts ids; an intersection of two "
                 "equality scans sorts nothing."),
                ("Another single-column index on supplier_id", False,
                 "That index already exists; it is one of the two scans being merged."),
            ],
        },
    ],
}


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
            "No single index serves both conditions, but MySQL and MariaDB can scan "
            "both single-column indexes and merge their id lists before reading rows "
            "from the clustered index. The query follows the Merge variant."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The two boxes at the top are index scans: idx_category on the left, idx_supplier on the right. Each line is one index entry with the product's id (#12 Webcam); a dot lights up when the entry is read.",
            "For an equality the ids come out in id order; for a range (Sort-union) they come in key order, unsorted.",
            "The purple middle box merges the two id lists: union and sort-union keep each id once (OR), intersection keeps the ids found in both (AND). Purple dots mark the products found by both scans.",
            "The green box at the bottom reads the rows that are left from the clustered index, one lookup per id, in id order.",
            "Next step shows one scan, the merge, then the row reads, and the simplified code beside the drawing highlights the lines each step runs.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
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
    <div class="item"><p class="label">idx_category rows {ht("Ids (primary keys) returned by the first index scan. For an equality they come out sorted by primary key, because InnoDB stores equal keys in id order.")}</p><p class="value" id="out-a-rows">\u2014</p></div>
    <div class="item"><p class="label">idx_supplier rows {ht("Ids (primary keys) returned by the second index scan.")}</p><p class="value" id="out-b-rows">\u2014</p></div>
    <div class="item"><p class="label">Overlapping rows {ht("Ids found by both index scans. A union reads each of them once; an intersection keeps only these.")}</p><p class="value" id="out-overlap">\u2014</p></div>
    <div class="item"><p class="label">Rows fetched {ht("Rows read from the clustered index after the merge, one lookup per id. This is the row-read cost of the plan.")}</p><p class="value" id="out-merged">\u2014</p></div>
    <div class="item"><p class="label">Variant {ht("Union = OR of equalities (both id lists are already sorted). Intersection = AND (keep only ids in both lists). Sort-union = OR of ranges (the ids are sorted first).")}</p><p class="value" id="out-variant">\u2014</p></div>
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

    <p><strong>Index merge union</strong> (OR): each index is scanned with an
    equality, so it returns its row IDs (primary keys) in order; the two
    sorted streams are merged and each ID is kept once. This avoids a full
    table scan when each index is selective enough.</p>

    <p><strong>Index merge intersection</strong> (AND): Both indexes are scanned,
    and only row-IDs present in <strong>both</strong> streams are kept. This is
    useful when no single index is selective enough, but together they are.</p>

    <p><strong>Index merge sort-union</strong> (OR with ranges): range scans
    return their row IDs in key order, not primary key order, so the row IDs
    from every scan are collected in a sort buffer (<code>sort_buffer_size</code>),
    sorted and de-duplicated, and then the rows are read in that order.</p>

    <p>You can control this behaviour with <code>optimizer_switch</code>:
    <code>index_merge=on</code>, <code>index_merge_union=on</code>,
    <code>index_merge_intersection=on</code>,
    <code>index_merge_sort_union=on</code>.</p>

    <p><strong>For AND conditions, a composite index usually beats an
    intersection</strong>: one index on <code>(category_id, supplier_id)</code>
    finds the matching entries in a single range. For OR conditions across two
    columns no single index can help, so a union of two index scans (or the
    query rewritten as a <code>UNION</code> of two SELECTs) is the usual answer.</p>

    <p>Sources: MySQL 8.4 reference manual §10.2.1.3 "Index Merge Optimization";
    MariaDB Knowledge Base "Index Merge Optimization".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="index_merge",
        title="Index merge: combining two index scans",
        subtitle=(
            "Watch MySQL and MariaDB scan two separate indexes, collect row IDs, "
            "and merge them with union, intersection, or sort-union."
        ),
        version_chip="MySQL 5.1+ \u2022 MariaDB 5.1+",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
        guide=GUIDE,
    )
