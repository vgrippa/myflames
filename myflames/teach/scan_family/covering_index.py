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


_LESSON_JS_TEMPLATE = r"""
// ---- covering_index lesson: non-covering → covering → PK-append ------
var TABLE_COLS = ["id", "email", "status", "country"];
var ROWS = [
  {id: 101, email: "alice@ex.com",   status: "active",   country: "US"},
  {id: 102, email: "bob@ex.com",     status: "active",   country: "UK"},
  {id: 103, email: "carol@ex.com",   status: "inactive", country: "US"},
  {id: 104, email: "dan@ex.com",     status: "active",   country: "CA"},
  {id: 105, email: "eve@ex.com",     status: "active",   country: "FR"},
  {id: 106, email: "frank@ex.com",   status: "inactive", country: "US"},
  {id: 107, email: "grace@ex.com",   status: "active",   country: "UK"},
  {id: 108, email: "henry@ex.com",   status: "inactive", country: "DE"},
  {id: 109, email: "ida@ex.com",     status: "active",   country: "US"}
];

// Color scheme (V1/D4 aware — chart fills stay separate from severity).
var IDX_COLOR     = "#93c5fd";   // light blue — index page
var TABLE_COLOR   = "#fde68a";   // yellow — clustered (primary) page
var MATCH_COLOR   = "#10b981";   // green — row matched the predicate
var SKIP_COLOR    = "#e5e7eb";   // grey — skipped / not read

var CELL_W = 120, CELL_H = 28, GAP = 6;

function clearSvg(id) {
  var s = document.getElementById(id);
  while (s.firstChild) s.removeChild(s.firstChild);
  return s;
}

function drawIndex(svgId, title, idxFields, rowSubset) {
  var svg = clearSvg(svgId);
  var lbl = anim.svgEl("text", {
    x: 14, y: 22, "font-size": 12, "font-weight": 700, fill: "#1e40af"
  });
  lbl.textContent = "Secondary index (" + idxFields.join(", ") + ")";
  svg.appendChild(lbl);
  var y = 36;
  var cells = [];
  rowSubset.forEach(function(row, i) {
    var x = 14;
    var g = anim.svgEl("g", {});
    var r = anim.svgEl("rect", {
      x: x, y: y, width: CELL_W * idxFields.length, height: CELL_H,
      rx: 4, ry: 4, fill: IDX_COLOR, stroke: "#60a5fa", "stroke-width": 1
    });
    g.appendChild(r);
    var xc = x;
    idxFields.forEach(function(f, fi) {
      var txt = anim.svgEl("text", {
        x: xc + 10, y: y + CELL_H / 2 + 4, "font-size": 11,
        "font-weight": 600, fill: "#1e3a8a"
      });
      txt.textContent = f + "=" + row[f];
      g.appendChild(txt);
      xc += CELL_W;
    });
    svg.appendChild(g);
    cells.push({rect: r, row: row});
    y += CELL_H + GAP;
  });
  return cells;
}

function drawTable(svgId, title) {
  var svg = clearSvg(svgId);
  var lbl = anim.svgEl("text", {
    x: 14, y: 22, "font-size": 12, "font-weight": 700, fill: "#92400e"
  });
  lbl.textContent = "Clustered (primary) index — full rows";
  svg.appendChild(lbl);
  var y = 36;
  var cells = [];
  ROWS.forEach(function(row, i) {
    var x = 14;
    var g = anim.svgEl("g", {});
    var r = anim.svgEl("rect", {
      x: x, y: y, width: CELL_W * TABLE_COLS.length, height: CELL_H,
      rx: 4, ry: 4, fill: SKIP_COLOR, stroke: "#d1d5db", "stroke-width": 1
    });
    g.appendChild(r);
    var xc = x;
    TABLE_COLS.forEach(function(f) {
      var txt = anim.svgEl("text", {
        x: xc + 10, y: y + CELL_H / 2 + 4, "font-size": 11,
        "font-weight": 500, fill: "#374151"
      });
      txt.textContent = f + "=" + row[f];
      g.appendChild(txt);
      xc += CELL_W;
    });
    svg.appendChild(g);
    cells.push({rect: r, row: row});
    y += CELL_H + GAP;
  });
  return cells;
}

function setStat(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text;
}

function runAct1(tl, idxCells, tblCells) {
  // Non-covering: index gives us (status); server goes back to table
  // for (email).
  tl.call(function() {
    setStat("out-phase",
      "Act 1 — Non-covering: index on (status) finds rows; "
      + "server then reads (email) from the clustered index");
    setStat("out-extra", "Extra: (none — second lookup happens)");
    setStat("out-index-reads", "0");
    setStat("out-table-reads", "0");
  });
  var idxReads = 0, tblReads = 0;
  idxCells.forEach(function(entry, i) {
    if (entry.row.status !== "active") return;
    tl.add({
      from: 0, to: 1, duration: 160, ease: anim.easeOutCubic,
      onUpdate: function() {},
      onComplete: function() {
        entry.rect.setAttribute("fill", MATCH_COLOR);
        anim.arrival(entry.rect);
        idxReads += 1;
        setStat("out-index-reads", String(idxReads));
      }
    });
    tl.delay(80);
    // Second lookup on clustered.
    tl.add({
      from: 0, to: 1, duration: 160, ease: anim.easeOutCubic,
      onUpdate: function() {},
      onComplete: function() {
        var match = tblCells.filter(function(c) {
          return c.row.id === entry.row.id;
        })[0];
        if (match) {
          match.rect.setAttribute("fill", TABLE_COLOR);
          anim.arrival(match.rect);
          tblReads += 1;
          setStat("out-table-reads", String(tblReads));
        }
      }
    });
    tl.delay(60);
  });
  tl.delay(600);
}

function runAct2(tl, idxCells) {
  tl.call(function() {
    setStat("out-phase",
      "Act 2 — Covering: CREATE INDEX idx ON users (status, email)");
    setStat("out-extra", "Extra: Using index  ← covering, no table lookup");
    setStat("out-index-reads", "0");
    setStat("out-table-reads", "0");
    // Reset colors.
    idxCells.forEach(function(c) { c.rect.setAttribute("fill", IDX_COLOR); });
  });
  var idxReads = 0;
  idxCells.forEach(function(entry) {
    if (entry.row.status !== "active") return;
    tl.add({
      from: 0, to: 1, duration: 140, ease: anim.easeOutCubic,
      onUpdate: function() {},
      onComplete: function() {
        entry.rect.setAttribute("fill", MATCH_COLOR);
        anim.arrival(entry.rect);
        idxReads += 1;
        setStat("out-index-reads", String(idxReads));
      }
    });
    tl.delay(50);
  });
  tl.delay(600);
}

function runAct3(tl, idxCells) {
  tl.call(function() {
    setStat("out-phase",
      "Act 3 — InnoDB trick: idx_status(status) actually stores "
      + "(status, id) — so SELECT id WHERE status=… is already covered");
    setStat("out-extra",
      "Extra: Using index  ← without ever adding `id` to the index");
    idxCells.forEach(function(c) { c.rect.setAttribute("fill", IDX_COLOR); });
  });
  // Redraw the index to show the PK suffix visually.
  tl.add({
    from: 0, to: 1, duration: 360, ease: anim.easeOutCubic,
    onUpdate: function() {},
    onComplete: function() {
      var active = ROWS.filter(function(r) { return r.status === "active"; });
      drawIndex("svg-index", "", ["status", "id"], active);
      setStat("out-index-reads", String(active.length));
      setStat("out-table-reads", "0");
    }
  });
}

function _buildStage() {
  var idxCells = drawIndex(
    "svg-index", "status",
    ["status", "email"],
    ROWS.filter(function(r) { return r.status === "active"; }).concat(
      ROWS.filter(function(r) { return r.status !== "active"; })
    )
  );
  var tblCells = drawTable("svg-table", "users");
  return {idxCells: idxCells, tblCells: tblCells};
}

function buildCurrentTimeline() {
  var s = _buildStage();
  var tl = anim.timeline();
  runAct1(tl, s.idxCells, s.tblCells);
  runAct2(tl, s.idxCells);
  runAct3(tl, s.idxCells);
  return tl;
}

function resetAnim() {
  _buildStage();
  setStat("out-phase", "Ready — press Play");
  setStat("out-extra", "—");
  setStat("out-index-reads", "—");
  setStat("out-table-reads", "—");
  document.getElementById("phase-label").textContent = "Ready — press Play";
}

document.addEventListener("DOMContentLoaded", function() {
  _buildStage();
});
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildCurrentTimeline,
  reset: resetAnim
});
"""


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
