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
