// ---- covering_index lesson: non-covering, covering, primary key in every entry ----
//
// One drawing (one coordinate system, so every label keeps the same size):
// the secondary index's leaf entries on the left, the clustered index's leaf
// rows (the table itself) on the right. Act 1 hands each matching entry's id
// along an arrow to the clustered index; Acts 2 and 3 answer from the entry.
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

// Index entries in index order: status first, then the primary key id (the
// same order for (status, email) with this data).
var ENTRIES = ROWS.filter(function(r) { return r.status === "active"; })
  .concat(ROWS.filter(function(r) { return r.status !== "active"; }));
var N_ACTIVE = ROWS.filter(function(r) { return r.status === "active"; }).length;

var W = 800, H = 460;
var IDX_X = 20, IDX_W = 330;
var TBL_X = 430, TBL_W = 350;
var ROW0_Y = 80, ROW_H = 26, ROW_STEP = 32;

var IDX_FILL = "var(--svg-token-soft)";     // index entry
var TABLE_FILL = "var(--svg-hop-soft)";     // clustered row fetched by a lookup
var SENT_FILL = "var(--svg-ok-soft)";       // entry answered the query by itself
var IDLE_FILL = "var(--svg-node-muted)";    // clustered row not read

var stage = null;

function rowY(i) { return ROW0_Y + i * ROW_STEP; }
function rowMid(i) { return rowY(i) + ROW_H / 2; }
function tableIndexOf(id) {
  for (var i = 0; i < ROWS.length; i++) if (ROWS[i].id === id) return i;
  return -1;
}

function text(svg, attrs, value) {
  var t = anim.svgEl("text", attrs);
  t.textContent = value;
  svg.appendChild(t);
  return t;
}

function setStat(id, value) {
  var el = document.getElementById(id);
  if (el) el.textContent = value;
}

function buildStage() {
  var svg = document.getElementById("ci-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var act = text(svg, {x: 20, y: 22, "font-size": 13, "font-weight": 700, fill: "var(--svg-ink)"}, "");
  var idxTitle = text(svg, {x: IDX_X, y: 50, "font-size": 12, "font-weight": 700, fill: "var(--svg-token-ink)"}, "");
  text(svg, {x: TBL_X, y: 50, "font-size": 12, "font-weight": 700, fill: "var(--svg-build-ink)"},
    "Clustered index leaf rows (by id): full rows");

  // Column headers.
  var head = {"font-size": 10, "font-weight": 700, fill: "var(--svg-ink-muted)"};
  text(svg, Object.assign({x: IDX_X + 12, y: 72}, head), "status");
  var emailHead = text(svg, Object.assign({x: IDX_X + 104, y: 72}, head), "email");
  var idHead = text(svg, Object.assign({x: IDX_X + 252, y: 72}, head), "id (PK)");
  text(svg, Object.assign({x: TBL_X + 12, y: 72}, head), "id");
  text(svg, Object.assign({x: TBL_X + 62, y: 72}, head), "email");
  text(svg, Object.assign({x: TBL_X + 186, y: 72}, head), "status");
  text(svg, Object.assign({x: TBL_X + 280, y: 72}, head), "country");

  // Arrows from an entry to its clustered row (Act 1), drawn under the rows.
  var hops = [];
  for (var h = 0; h < N_ACTIVE; h++) {
    var e = ENTRIES[h];
    var y1 = rowMid(h), y2 = rowMid(tableIndexOf(e.id));
    var x1 = IDX_X + IDX_W, x2 = TBL_X;
    var path = anim.svgEl("path", {
      d: "M" + x1 + "," + y1 + " C" + (x1 + 40) + "," + y1 + " " + (x2 - 40) + "," + y2 + " " + (x2 - 6) + "," + y2,
      fill: "none", stroke: "var(--svg-hop)", "stroke-width": 1.8, opacity: 0
    });
    svg.appendChild(path);
    var head2 = anim.svgEl("path", {
      d: "M" + (x2 - 10) + "," + (y2 - 4) + " L" + (x2 - 2) + "," + y2 + " L" + (x2 - 10) + "," + (y2 + 4),
      fill: "none", stroke: "var(--svg-hop)", "stroke-width": 1.8, opacity: 0
    });
    svg.appendChild(head2);
    hops.push({path: path, head: head2, x1: x1, y1: y1, x2: x2, y2: y2});
  }

  var entries = [];
  for (var i = 0; i < ENTRIES.length; i++) {
    var r = ENTRIES[i];
    var y = rowY(i);
    var bg = anim.svgEl("rect", {
      x: IDX_X, y: y, width: IDX_W, height: ROW_H, rx: 4, ry: 4,
      fill: IDX_FILL, stroke: "var(--svg-token)", "stroke-width": 1
    });
    svg.appendChild(bg);
    var cell = {"font-size": 11, "font-weight": 600, fill: "var(--svg-token-ink)"};
    var st = text(svg, Object.assign({x: IDX_X + 12, y: y + 17}, cell), r.status);
    var em = text(svg, Object.assign({x: IDX_X + 104, y: y + 17}, cell), "");
    var idCell = anim.svgEl("rect", {
      x: IDX_X + 244, y: y + 3, width: 78, height: ROW_H - 6, rx: 3, ry: 3,
      fill: "none", stroke: "none", "stroke-width": 1.5
    });
    svg.appendChild(idCell);
    var idt = text(svg, Object.assign({x: IDX_X + 252, y: y + 17}, cell), "id=" + r.id);
    entries.push({bg: bg, status: st, email: em, idCell: idCell, id: idt, row: r});
  }

  var rows = [];
  for (var j = 0; j < ROWS.length; j++) {
    var u = ROWS[j];
    var ty = rowY(j);
    var tbg = anim.svgEl("rect", {
      x: TBL_X, y: ty, width: TBL_W, height: ROW_H, rx: 4, ry: 4,
      fill: IDLE_FILL, stroke: "var(--svg-node-stroke)", "stroke-width": 1
    });
    svg.appendChild(tbg);
    var tcell = {"font-size": 11, "font-weight": 500, fill: "var(--svg-ink)"};
    text(svg, Object.assign({x: TBL_X + 12, y: ty + 17}, tcell), String(u.id));
    var temail = text(svg, Object.assign({x: TBL_X + 62, y: ty + 17}, tcell), u.email);
    text(svg, Object.assign({x: TBL_X + 186, y: ty + 17}, tcell), u.status);
    text(svg, Object.assign({x: TBL_X + 280, y: ty + 17}, tcell), u.country);
    rows.push({bg: tbg, email: temail, row: u});
  }

  // Stroke-only cursor over the entry being read (keeps the text legible).
  var cursor = anim.svgEl("rect", {
    x: IDX_X - 3, y: ROW0_Y - 3, width: IDX_W + 6, height: ROW_H + 6, rx: 6, ry: 6,
    fill: "none", stroke: "var(--svg-build)", "stroke-width": 3, opacity: 0, "pointer-events": "none"
  });
  svg.appendChild(cursor);

  // The primary key id travelling from the entry to the clustered index.
  var token = anim.svgEl("g", {opacity: 0});
  token.appendChild(anim.svgEl("rect", {
    x: -26, y: -9, width: 52, height: 18, rx: 9, ry: 9,
    fill: "var(--svg-hop)", stroke: "var(--svg-ink)", "stroke-width": 1
  }));
  var tokenTxt = text(token, {x: 0, y: 4, "text-anchor": "middle", "font-size": 10,
    "font-weight": 700, fill: "var(--svg-on-solid)"}, "");
  svg.appendChild(token);

  var tally = text(svg, {x: W / 2, y: 398, "text-anchor": "middle", "font-size": 12,
    "font-weight": 700, fill: "var(--svg-ink)"}, "");
  var status = text(svg, {x: W / 2, y: 436, "text-anchor": "middle", "font-size": 12.5,
    "font-weight": 600, fill: "var(--svg-ink)"}, "");

  stage = {svg: svg, act: act, idxTitle: idxTitle, emailHead: emailHead, idHead: idHead,
           entries: entries, rows: rows, hops: hops, cursor: cursor, token: token,
           tokenTxt: tokenTxt, tally: tally, status: status};
}

// --- stage state helpers (all instant; motion lives in timeline tweens) ---
function showEmails(on) {
  stage.emailHead.setAttribute("opacity", on ? 1 : 0);
  for (var i = 0; i < stage.entries.length; i++) {
    stage.entries[i].email.textContent = on ? stage.entries[i].row.email : "";
  }
}
function markIds(on) {
  stage.idHead.textContent = on ? "id (PK, kept by InnoDB)" : "id (PK)";
  stage.idHead.setAttribute("fill", on ? "var(--svg-ok)" : "var(--svg-ink-muted)");
  for (var i = 0; i < stage.entries.length; i++) {
    var e = stage.entries[i];
    e.idCell.setAttribute("stroke", on ? "var(--svg-ok)" : "none");
    e.id.setAttribute("fill", on ? "var(--svg-ok)" : "var(--svg-token-ink)");
  }
}
function clearEntries() {
  for (var i = 0; i < stage.entries.length; i++) stage.entries[i].bg.setAttribute("fill", IDX_FILL);
}
function clearRows(opacity) {
  for (var i = 0; i < stage.rows.length; i++) {
    stage.rows[i].bg.setAttribute("fill", IDLE_FILL);
    stage.rows[i].bg.setAttribute("opacity", String(opacity));
    stage.rows[i].email.setAttribute("font-weight", "500");
  }
}
function clearHops() {
  for (var i = 0; i < stage.hops.length; i++) {
    stage.hops[i].path.setAttribute("opacity", 0);
    stage.hops[i].head.setAttribute("opacity", 0);
  }
}
function placeCursor(i, on) {
  stage.cursor.setAttribute("y", rowY(i) - 3);
  stage.cursor.setAttribute("opacity", on ? 1 : 0);
}
function setTally(entries, rows, extra) {
  stage.tally.textContent = "index entries read: " + entries + "   ·   clustered rows read: " +
    rows + "   ·   EXPLAIN Extra: " + extra;
  setStat("out-index-reads", String(entries));
  setStat("out-table-reads", String(rows));
  setStat("out-extra", extra);
}

function resetStage() {
  if (!stage) return;
  stage.act.textContent = "Three plans for one table: watch where each value comes from";
  stage.idxTitle.textContent = "idx_status leaf entries: (status, id)";
  showEmails(false);
  markIds(false);
  clearEntries();
  clearRows(1);
  clearHops();
  placeCursor(0, false);
  stage.token.setAttribute("opacity", 0);
  stage.tally.textContent = "";
  stage.status.textContent = "";
}

// Move the cursor from entry a to entry b as one timeline-owned tween.
function moveCursor(tl, a, b, ms) {
  tl.add({from: 0, to: 1, duration: ms, ease: anim.easeInOutCubic, onUpdate: function(t) {
    stage.cursor.setAttribute("y", (rowY(a) + (rowY(b) - rowY(a)) * t - 3).toFixed(1));
    stage.cursor.setAttribute("opacity", 1);
  }});
}

function buildCurrentTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");

  // Guided steps: each act's plan, each entry read (one lookup per entry in
  // Act 1, the whole range in Acts 2 and 3) and the end of the range. Kinds
  // map to the simplified-code lines (GUIDE in covering_index.py).

  // ---- Act 1: idx_status (status), SELECT email: not covering ----
  tl.mark("Act 1: not covering");
  tl.beat("plan");
  tl.call(function() {
    stage.act.textContent = "Act 1 of 3: SELECT email ... WHERE status = 'active', using idx_status (status)";
    stage.idxTitle.textContent = "idx_status leaf entries: (status, id)";
    showEmails(false);
    markIds(false);
    clearEntries();
    clearRows(1);
    clearHops();
    setTally(0, 0, "(empty)");
    setStat("out-phase", "Act 1: not covering");
    phase.textContent = "Act 1: an idx_status entry holds status and the primary key id, but not email, " +
      "so this plan is not covering and must visit the clustered index";
    stage.status.textContent = "Plan: find status = 'active' in idx_status, then fetch email from each full row.";
  });
  moveCursor(tl, 0, 0, 260);

  var tableReads = 0;
  for (var i = 0; i < N_ACTIVE; i++) {
    (function(idx) {
      var e = ENTRIES[idx];
      var hop = stage.hops[idx];
      var trow = stage.rows[tableIndexOf(e.id)];
      tableReads += 1;
      var readsNow = tableReads;
      var fly = anim.path(hop.x1, hop.y1, (hop.x1 + hop.x2) / 2, Math.min(hop.y1, hop.y2) - 30, hop.x2 - 26, hop.y2);
      tl.beat("lookup");
      if (idx > 0) moveCursor(tl, idx - 1, idx, 200);
      tl.call(function() {
        stage.entries[idx].bg.setAttribute("fill", "var(--svg-highlight)");
        stage.tokenTxt.textContent = "id=" + e.id;
        setTally(idx + 1, readsNow - 1, "(empty)");
        phase.textContent = "Act 1, row " + (idx + 1) + " of " + N_ACTIVE + ": entry (active, id=" + e.id +
          ") has no email, so InnoDB looks up id=" + e.id + " in the clustered index";
        stage.status.textContent = "Read idx_status entry " + (idx + 1) + ": status=active, id=" + e.id;
      });
      tl.add({from: 0, to: 1, duration: 460, ease: anim.easeInOutCubic,
        onUpdate: function(t) {
          var p = fly(t);
          stage.token.setAttribute("transform", "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ")");
          stage.token.setAttribute("opacity", t < 0.92 ? 1 : (1 - t) / 0.08);
          hop.path.setAttribute("opacity", (0.9 * t).toFixed(2));
          hop.head.setAttribute("opacity", (0.9 * t).toFixed(2));
        },
        onComplete: function() {
          stage.token.setAttribute("opacity", 0);
          trow.bg.setAttribute("fill", TABLE_FILL);
          trow.email.setAttribute("font-weight", "700");
          anim.arrival(trow.bg);
          setTally(idx + 1, readsNow, "(empty)");
          phase.textContent = "Act 1, row " + (idx + 1) + " of " + N_ACTIVE + ": entry (active, id=" + e.id +
            ") has no email, so InnoDB read clustered row id=" + e.id + " for email=" + e.email;
          stage.status.textContent = "Clustered row id=" + e.id + ": email=" + e.email + " → sent to the client";
        }});
      tl.delay(220);
      tl.call(function() {
        stage.entries[idx].bg.setAttribute("fill", IDX_FILL);
        hop.path.setAttribute("opacity", 0.35);
        hop.head.setAttribute("opacity", 0.35);
      });
    })(i);
  }

  tl.beat("stop");
  moveCursor(tl, N_ACTIVE - 1, N_ACTIVE, 220);
  tl.call(function() {
    var e = ENTRIES[N_ACTIVE];
    stage.entries[N_ACTIVE].bg.setAttribute("fill", "var(--svg-danger-soft)");
    setTally(N_ACTIVE + 1, N_ACTIVE, "(empty)");
    phase.textContent = "Act 1 done: the next entry has status = 'inactive', so the range ends. " +
      N_ACTIVE + " rows cost " + N_ACTIVE + " clustered index lookups on top of the index reads";
    stage.status.textContent = "Entry (inactive, id=" + e.id + ") does not match: no lookup, the read stops here.";
  });
  tl.delay(900);

  // ---- Act 2: idx_status_email (status, email): covering ----
  tl.mark("Act 2: covering index");
  tl.beat("plan");
  tl.call(function() {
    stage.act.textContent = "Act 2 of 3: the same query after CREATE INDEX idx_status_email ON users (status, email)";
    stage.idxTitle.textContent = "idx_status_email leaf entries: (status, email, id)";
    showEmails(true);
    markIds(false);
    clearEntries();
    clearRows(0.45);
    clearHops();
    placeCursor(0, false);
    setTally(0, 0, "Using index");
    setStat("out-phase", "Act 2: covering");
    phase.textContent = "Act 2: an idx_status_email entry holds status, email and id, every column the query " +
      "needs, so the plan is covering and EXPLAIN says Using index";
    stage.status.textContent = "The clustered index (right) will not be read at all.";
  });
  moveCursor(tl, 0, 0, 260);

  tl.beat("covered");
  tl.call(function() {
    phase.textContent = "Act 2: InnoDB reads the " + N_ACTIVE + " 'active' entries and sends email straight " +
      "from each one; no row of the clustered index is touched";
  });
  for (var c = 0; c < N_ACTIVE; c++) {
    (function(idx) {
      var e = ENTRIES[idx];
      if (idx > 0) moveCursor(tl, idx - 1, idx, 150);
      tl.add({from: 0, to: 1, duration: 140, ease: anim.easeOutCubic, onUpdate: function() {},
        onComplete: function() {
          stage.entries[idx].bg.setAttribute("fill", SENT_FILL);
          anim.arrival(stage.entries[idx].bg);
          setTally(idx + 1, 0, "Using index");
          stage.status.textContent = "Entry " + (idx + 1) + ": sent email=" + e.email + " from the index entry (no lookup)";
        }});
      tl.delay(90);
    })(c);
  }

  tl.beat("stop");
  moveCursor(tl, N_ACTIVE - 1, N_ACTIVE, 200);
  tl.call(function() {
    stage.entries[N_ACTIVE].bg.setAttribute("fill", "var(--svg-danger-soft)");
    setTally(N_ACTIVE + 1, 0, "Using index");
    phase.textContent = "Act 2 done: the next entry is 'inactive', so the range ends. " + N_ACTIVE +
      " rows came from " + N_ACTIVE + " index entries and 0 clustered index rows";
    stage.status.textContent = "Same result as Act 1, with no second lookup per row.";
  });
  tl.delay(900);

  // ---- Act 3: idx_status (status), SELECT id: covered by the primary key ----
  tl.mark("Act 3: primary key in every entry");
  tl.beat("plan");
  tl.call(function() {
    stage.act.textContent = "Act 3 of 3: SELECT id ... WHERE status = 'active', using idx_status (status)";
    stage.idxTitle.textContent = "idx_status leaf entries: (status, id), id added by InnoDB";
    showEmails(false);
    markIds(true);
    clearEntries();
    clearRows(0.45);
    clearHops();
    placeCursor(0, false);
    setTally(0, 0, "Using index");
    setStat("out-phase", "Act 3: covered by the primary key");
    phase.textContent = "Act 3: the query asks only for id, and every idx_status entry already holds id " +
      "(InnoDB stores the primary key in each entry), so this plan is covering too";
    stage.status.textContent = "You saw this id in Act 1: it is how an entry finds its full row.";
  });
  moveCursor(tl, 0, 0, 260);

  tl.beat("covered");
  tl.call(function() {
    phase.textContent = "Act 3: InnoDB reads the " + N_ACTIVE + " 'active' entries and sends id from each one; " +
      "no clustered index lookup is needed";
  });
  for (var k = 0; k < N_ACTIVE; k++) {
    (function(idx) {
      var e = ENTRIES[idx];
      if (idx > 0) moveCursor(tl, idx - 1, idx, 150);
      tl.add({from: 0, to: 1, duration: 140, ease: anim.easeOutCubic, onUpdate: function() {},
        onComplete: function() {
          stage.entries[idx].bg.setAttribute("fill", SENT_FILL);
          anim.arrival(stage.entries[idx].bg);
          setTally(idx + 1, 0, "Using index");
          stage.status.textContent = "Entry " + (idx + 1) + ": sent id=" + e.id + " from the index entry (no lookup)";
        }});
      tl.delay(90);
    })(k);
  }

  tl.beat("stop");
  moveCursor(tl, N_ACTIVE - 1, N_ACTIVE, 200);
  tl.call(function() {
    stage.entries[N_ACTIVE].bg.setAttribute("fill", "var(--svg-danger-soft)");
    setTally(N_ACTIVE + 1, 0, "Using index");
    setStat("out-phase", "Done");
    phase.textContent = "Done: Act 1 needed " + N_ACTIVE + " clustered index lookups for " + N_ACTIVE +
      " rows; Acts 2 and 3 needed none, because every column they return is in the index entry";
    stage.status.textContent = "Using index = no clustered lookup. The primary key is in every InnoDB secondary entry.";
  });
  tl.delay(400);
  return tl;
}

function resetAnim() {
  resetStage();
  setStat("out-phase", "Ready");
  setStat("out-extra", "—");
  setStat("out-index-reads", "—");
  setStat("out-table-reads", "—");
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

buildStage();
resetAnim();
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildCurrentTimeline,
  reset: resetAnim
});
