"""Lesson: Covering index — why "Using index" in EXPLAIN is the goal.

A covering index is a secondary index that contains every column the
query needs, so MySQL can satisfy the SELECT entirely from the index
without doing a second lookup into the clustered (primary) index.

Three acts that build intuition:

Act 1 — **Without a covering index**. Plan uses a secondary index on
        ``(status)`` to find rows, then reads ``email`` from the
        clustered index. That's two reads per matching row.

Act 2 — **With a covering index**. CREATE INDEX idx ON users
        (status, email). EXPLAIN's Extra column now says "Using index".
        One read per row.

Act 3 — **The InnoDB PK-append trick**. Secondary indexes on InnoDB
        automatically include the primary-key columns — so
        ``idx_status(status)`` on a table with PK ``id`` is
        effectively ``(status, id)``. A query like
        ``SELECT id FROM users WHERE status = 'active'`` is
        *already* covered, even though the user never added id to
        the index definition. This is the #1 covering-index myth we
        want to correct.

Plus a sidebar: "Using index" (covering) vs "Using index condition"
(ICP, not covering).

MySQL internals verified against server source:
  * "Using index" string comes from ``ET_USING_INDEX`` in
    sql/opt_explain.cc:1615, pushed when
    ``table->key_read || tab->keyread_optim()`` is true — i.e. the
    read can be satisfied entirely from the index.
  * "Using index condition" is ``ET_USING_INDEX_CONDITION`` — a
    different mechanism (predicate pushed to the storage engine
    layer; rows may still need a table lookup for the SELECT list).
  * InnoDB secondary indexes always include the clustered-index
    unique columns (``clust_index->n_uniq``, see
    storage/innobase/dict/dict0dict.cc:3149
    ``dict_index_build_internal_non_clust``): that's how the
    PK-append behavior is implemented.
"""
from .. import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render():
    query_card_html = _html.query_card(
        "SELECT email FROM users WHERE status = 'active'",
        note=(
            "Same query three times — watch the plan change as we "
            "define (or don't) a covering index. Act 3 reveals an "
            "InnoDB property that silently makes many queries "
            "covering without the user knowing."
        ),
    )
    explainer_html = _html.explainer(
        "What you'll see",
        [
            "Blue rows = secondary-index entries. Yellow = clustered "
            "(primary) index rows, containing every column.",
            "Green flash = a row was read. Count of 'table reads' > 0 "
            "means the plan is NOT covering — it's doing a second lookup "
            "per matching row.",
            "The Extra column in EXPLAIN tells you which case you're in: "
            "<code>Using index</code> = covering; <code>Using index "
            "condition</code> = ICP (predicate pushdown, still does a "
            "table read for non-indexed columns in the SELECT list).",
        ],
    )
    controls_html = f"""
<section class="controls" aria-labelledby="controls-heading">
  <h2 id="controls-heading" class="visually-hidden">Controls</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play to walk the three cases")}
</section>
"""
    stage_html = f"""
<section class="stage">
  <div class="stage-with-phases">
    <div style="flex:1;min-width:0;display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <svg id="svg-index" viewBox="0 0 340 340" xmlns="http://www.w3.org/2000/svg"></svg>
      <svg id="svg-table" viewBox="0 0 520 340" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Live plan stats</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Phase</p><p class="value" id="out-phase">—</p></div>
    <div class="item"><p class="label">EXPLAIN Extra {ht("The Extra column text MySQL prints for this access. &#39;Using index&#39; = covering; anything else usually means a second lookup per row.")}</p><p class="value" id="out-extra">—</p></div>
    <div class="item"><p class="label">Index reads {ht("Rows read from the secondary index.")}</p><p class="value" id="out-index-reads">—</p></div>
    <div class="item"><p class="label">Clustered (table) reads {ht("Rows read from the primary/clustered index. Zero means the plan is covering.")}</p><p class="value hot" id="out-table-reads">—</p></div>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — the "Using index" vs "Using index condition" trap</summary>
  <div class="body">
    <p>Two strings in EXPLAIN's <code>Extra</code> column look similar
    and are routinely confused. They mean different things:</p>

    <h3><code>Using index</code> — covering</h3>
    <p>The read is satisfied entirely from the secondary index. No
    lookup into the clustered index happens. In MySQL source this is
    pushed when <code>table-&gt;key_read</code> is true (see
    <code>sql/opt_explain.cc</code> around line 1615). A covering
    query's cost is proportional to the matching index entries —
    never to the table size.</p>

    <h3><code>Using index condition</code> — ICP (not covering)</h3>
    <p>Index Condition Pushdown. The <em>filter</em> predicate is
    evaluated at the storage-engine layer (so rows that don't match
    never even come up the stack) — but the SELECT list may still
    require columns not in the index, and those still trigger a
    clustered-index lookup. Useful, but not the same as covering.</p>

    <h3>The InnoDB PK-append property</h3>
    <p>InnoDB's clustered-index primary key is silently appended to
    every secondary index. That's implemented in
    <code>storage/innobase/dict/dict0dict.cc</code>
    (<code>dict_index_build_internal_non_clust</code>): the internal
    index struct is created with <code>index-&gt;n_fields + 1 +
    clust_index-&gt;n_uniq</code> fields — the +<em>n_uniq</em> is
    the PK columns being appended. So:</p>

    <pre><code>CREATE TABLE users (id INT PRIMARY KEY, status VARCHAR(16), email VARCHAR(64));
CREATE INDEX idx_status ON users (status);
-- This index physically stores (status, id) — the id was appended for free.

SELECT id FROM users WHERE status = 'active';
-- EXPLAIN: Using index.  ← covering, even though `id` isn't in
--                           the index definition.
</code></pre>

    <p>This property is why defining extra indexes "just for the id"
    is almost always wasted effort on InnoDB. The flip side is that
    non-InnoDB engines (MyISAM, non-PK MariaDB Aria tables) don't do
    this — if you're on those engines, write the PK columns
    explicitly into the index.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="covering_index",
        title="Covering index — why \"Using index\" in EXPLAIN is the goal",
        subtitle=(
            "Three acts: non-covering plan, covering plan, and the "
            "InnoDB PK-append property that silently covers queries "
            "you didn't realize were covered."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS_TEMPLATE,
    )
