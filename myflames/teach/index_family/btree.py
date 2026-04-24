"""Lesson: InnoDB B+tree lookup.

Animates a query token descending the tree(s), with smooth easing,
pause/resume/speed controls, a real SQL example using the ``users``
table, and a log-log complexity chart showing how page count scales
with row count.

Concrete sample data: nodes on the lookup path are labelled with real
key ranges and row data (e.g. "root: keys 1..500K", "leaf: user #42,
alice@ex.com") following the same pattern as hash_join.py.
"""
from .. import _html
from .._cost_model import (
    INNODB_PAGE_OVERHEAD_BYTES,
    INNODB_CHILD_POINTER_BYTES,
    INNODB_PAGE_SIZE_DEFAULT,
)


# ---- Concrete sample data for node labels on the lookup path ----
# Secondary tree: looking for email='alice@example.com'
# Clustered tree: looking for id=42
# Node labels are applied during the animation when the token visits them.
_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = f"""
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters</h2>
  <div class="control-grid">

    <div class="control">
      <label for="rows">Rows in <code>users</code> table: <span class="value-pill" data-pill-for="rows">1000000</span></label>
      <input type="range" id="rows" name="rows" min="1" max="9" step="1" value="6">
      <div class="hint">Logarithmic: 10, 100, 1K, 10K, 100K, 1M, 10M, 100M, 1B</div>
    </div>

    <div class="control">
      <label for="key_size">Key size (bytes): <span class="value-pill" data-pill-for="key_size">8</span></label>
      <input type="range" id="key_size" name="key_size" min="4" max="64" step="2" value="8">
      <div class="hint">BIGINT PK = 8 B. INT = 4 B. 64-char VARCHAR prefix ≈ 64 B.</div>
    </div>

    <div class="control">
      <label for="page_size">InnoDB page size</label>
      <select id="page_size" name="page_size">
        <option value="4096">4 KiB</option>
        <option value="8192">8 KiB</option>
        <option value="16384" selected>16 KiB (default)</option>
        <option value="32768">32 KiB</option>
        <option value="65536">64 KiB</option>
      </select>
      <div class="hint">Set at <code>innodb_page_size</code>; default 16 KiB.</div>
    </div>

    <div class="control">
      <label for="key_type">Lookup type</label>
      <select id="key_type" name="key_type">
        <option value="pk">Clustered PK lookup (WHERE id = 42)</option>
        <option value="secondary_covering">Covering secondary index</option>
        <option value="secondary_noncovering" selected>Non-covering secondary (PK fetch)</option>
      </select>
      <div class="hint">Non-covering is MySQL's single biggest "secret" I/O tax.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Non-covering secondary index lookup\n"
            "SELECT id, first_name, last_name, country_code\n"
            "FROM   users\n"
            "WHERE  email = 'alice@example.com';   -- idx_users_email contains only (email, id)"
        ),
        note="Switch the lookup-type dropdown to see PK-clustered vs covering-index vs non-covering variants of this query."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The grey rectangles are B+tree pages. Top row is the root page, bottom row is the leaf.",
            "A red diamond 'query token' appears above the root and descends level by level — each step is one page read.",
            "When the token arrives at a page, the page pulses yellow. That's the 'page is in the buffer pool now'.",
            "For non-covering secondary lookups, the secondary-tree LEAF stores the primary key (labelled 'leaf: PK = 42'). A dashed orange PK-hop arrow then links that leaf directly to the clustered-tree leaf which holds the actual row (labelled 'leaf: full row'). The token rides that arrow across — no re-descent magic, the link is a real index pointer.",
            "That dashed arrow is the extra I/O a covering index would eliminate.",
        ],
    )

    stage_html = f"""
<section class="stage" aria-labelledby="stage-h">
  <h2 id="stage-h" class="sr-only" style="position:absolute;left:-9999px">Tree descent animation</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Adjust parameters, then press Play")}
  <div class="stage-with-phases">
    <svg id="btree-svg" viewBox="0 0 800 400" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (updates live)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Fan-out (non-leaf) {ht("How many child pointers fit in one 16 KiB page. Higher fan-out means a shallower tree — fewer page reads per lookup.")}</p><p class="value" id="out-fanout">—</p></div>
    <div class="item"><p class="label">Tree height {ht("Number of page levels from root to leaf. Each level is one disk page that InnoDB must read. A 1-billion-row table with BIGINT PK is only 4 levels deep.")}</p><p class="value" id="out-height">—</p></div>
    <div class="item"><p class="label">Tree traversals {ht("A clustered-PK or covering-index lookup walks one tree. A non-covering secondary lookup walks two: the secondary index tree, then the clustered tree using the PK it found.")}</p><p class="value" id="out-traversals">—</p></div>
    <div class="item"><p class="label">Pages touched {ht("Total page reads for this single-row lookup. Equals tree height times the number of traversals. Each page is 16 KiB by default.")}</p><p class="value" id="out-pages">—</p></div>
    <div class="item"><p class="label">Cold-cache I/O {ht("Worst-case: none of these pages are in the buffer pool yet, so every page is a disk read. In practice, the upper levels of the tree are almost always cached.")}</p><p class="value" id="out-cold-io">—</p></div>
    <div class="item"><p class="label">Complexity {ht("B+tree lookups are O(log n) — doubling the table size adds roughly one extra page read. Non-covering secondary adds a second tree walk: O(log users) + O(log users) = O(2 log n).")}</p><p class="value" id="out-complexity">O(log users) = O(log n)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Pages touched vs table size (log–log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = f"""
<details class="learn-more">
  <summary>Learn more — why is a non-covering secondary index slower?</summary>
  <div class="body">
    <p>InnoDB stores every table as a <strong>clustered B+tree on the primary key</strong>.
    The leaf pages of that tree hold the full row. A secondary index is a
    <em>separate</em> B+tree whose leaves hold <code>(secondary_cols, primary_key)</code>,
    not the row itself.</p>

    <p>So a lookup by a non-covering secondary index has to:</p>
    <ol>
      <li>Walk the secondary B+tree down to a leaf, yielding <code>PK</code>.</li>
      <li>Walk the <em>clustered</em> B+tree using that <code>PK</code> to fetch the row.</li>
    </ol>
    <p>Two traversals, each the full height of its tree. That is why
    <strong>covering indexes</strong> — where the leaf already has every
    column the query asked for — are such an important tuning tool.</p>

    <p>The fan-out formula here is
    <code>(page_size − {INNODB_PAGE_OVERHEAD_BYTES}) / (key_size + {INNODB_CHILD_POINTER_BYTES})</code>.
    It's an approximation: real InnoDB records have variable-length headers and
    leaf pages split at a 15/16 fill factor, so real fan-out is a bit lower.
    The <em>height</em> is stable under that noise because it's a logarithm —
    halving the real fan-out only adds one level of the tree for every
    factor of the original fan-out.</p>

    <p>Sources: MySQL 8.4 Reference Manual §17.6.2 (InnoDB Indexes),
    §17.11.2 (File Space Management). Default
    <code>innodb_page_size</code> = 16 KiB.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % (
        INNODB_PAGE_OVERHEAD_BYTES,
        INNODB_CHILD_POINTER_BYTES,
        INNODB_PAGE_SIZE_DEFAULT,
    )

    return _html.render_page(
        lesson_id="btree",
        title="B+tree lookup — how InnoDB finds a row",
        subtitle=(
            "Clustered primary key, covering vs non-covering secondary "
            "index, and why one extra tree walk doubles your I/O."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
