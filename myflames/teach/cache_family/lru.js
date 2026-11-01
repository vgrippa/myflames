// ---- LRU lesson: 3-act story with concrete page names ----
var HOT_PAGES = 8;  // Pages in the OLTP hot set (act 1 + act 3)

// Concrete page labels for the hot set and scan pages
var HOT_PAGE_NAMES = [
  "users:42", "users:7", "orders:101", "products:55",
  "users:3", "orders:88", "products:12", "users:99"
];
function scanPageName(scanId) {
  return "events:" + (1001 + scanId);
}

// Color scheme
var HOT_COLOR = "var(--svg-token)";   // blue (OLTP hot)
var SCAN_COLOR = "var(--svg-build)";  // orange (scan)
var EVICT_COLOR = "var(--svg-danger)"; // light red (about to be evicted)
var MISS_COLOR = "var(--svg-danger)";  // red flash (miss in act 3)
var HIT_COLOR = "var(--svg-ok)";   // green flash (hit in act 3)
var EMPTY_COLOR = "var(--svg-node-muted)"; // grey (empty slot)

// Six cells per row keep the table:page labels readable beside the code.
var COLS = 6, CELL_W = 58, CELL_H = 18, GAP = 3, ROW = CELL_H + GAP;
var LABEL_FONT = 7.5;

// Guided steps: each kind names the InnoDB lines of the simplified code it
// runs (GUIDE in lru.py). Kinds come from this table so every one appears
// in the source as a literal.
var BEAT_KINDS = {
  hotSet: "hot-set",
  scanMiss: "scan-miss",
  scanReread: "scan-reread",
  scanEvict: "scan-evict",
  returnYoung: "return-young",
  returnOld: "return-old",
  returnMiss: "return-miss",
  returnMissRoom: "return-miss-room",
  summary: "summary"
};

// Shared geometry for both pools: the sublists stack, and the caption lines
// sit under the taller of the two drawings, so both SVGs get one height.
function lruGeometry(poolSize, oldPct) {
  var youngCap = Math.max(1, Math.floor(poolSize * (100 - oldPct) / 100));
  var oldCap = poolSize - youngCap;
  var youngBottom = 32 + Math.ceil(youngCap / COLS) * ROW - GAP;
  var midY = youngBottom + 15;
  var oldLabelY = midY + 16;
  var oldStartY = oldLabelY + 8;
  var innoBottom = oldStartY + Math.ceil(oldCap / COLS) * ROW - GAP;
  var classicBottom = 32 + Math.ceil(poolSize / COLS) * ROW - GAP;
  var noteY = Math.max(innoBottom, classicBottom) + 22;
  return {
    youngCap: youngCap, oldCap: oldCap, midY: midY, oldLabelY: oldLabelY, oldStartY: oldStartY,
    noteY: noteY, statsY: noteY + 22, verdictY: noteY + 47, actY: noteY + 72, height: noteY + 84
  };
}

function cell(svg, x, y) {
  var r = anim.svgEl("rect", {
    x: x, y: y, width: CELL_W, height: CELL_H,
    rx: 3, ry: 3, fill: EMPTY_COLOR, stroke: "var(--svg-node-stroke)", "stroke-width": 1
  });
  svg.appendChild(r);
  var t = anim.svgEl("text", {
    x: x + CELL_W / 2, y: y + CELL_H / 2 + 3,
    "text-anchor": "middle",
    "font-size": LABEL_FONT, "font-weight": 600, fill: "var(--svg-on-solid)"
  });
  svg.appendChild(t);
  return {rect: r, label: t};
}

function captionLines(svg, g, verdictFill, actFill) {
  function line(y, size, weight, fill) {
    var t = anim.svgEl("text", {x: 200, y: y, "text-anchor": "middle", "font-size": size, "font-weight": weight, fill: fill});
    svg.appendChild(t);
    return t;
  }
  return {
    // Caption in the scene: what this step did, with the page names.
    noteLbl: line(g.noteY, 11, 600, "var(--svg-ink)"),
    statsLbl: line(g.statsY, 12, 600, "var(--svg-ink)"),
    verdictLbl: line(g.verdictY, 13, 700, verdictFill),
    actLbl: line(g.actY, 14, 700, actFill)
  };
}

function buildInnoDB(poolSize, oldPct) {
  var svg = document.getElementById("svg-innodb");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var g = lruGeometry(poolSize, oldPct);
  svg.setAttribute("viewBox", "0 0 400 " + g.height);
  var youngCap = g.youngCap;
  var oldCap = g.oldCap;
  var ylbl = anim.svgEl("text", {x: 16, y: 22, "font-size": 11, "font-weight": 700, fill: "var(--svg-token-ink)"});
  ylbl.textContent = "Young sublist (" + youngCap + " slots) \u2014 your hot OLTP pages live here";
  svg.appendChild(ylbl);
  var youngSlots = [], youngLabels = [];
  for (var i = 0; i < youngCap; i++) {
    var yc = cell(svg, 16 + (i % COLS) * (CELL_W + GAP), 32 + Math.floor(i / COLS) * ROW);
    youngSlots.push(yc.rect);
    youngLabels.push(yc.label);
  }
  var mid = anim.svgEl("line", {
    x1: 16, y1: g.midY, x2: 384, y2: g.midY,
    stroke: "var(--svg-danger)", "stroke-width": 2, "stroke-dasharray": "5 3"
  });
  svg.appendChild(mid);
  var midLbl = anim.svgEl("text", {
    x: 384, y: g.midY - 4, "text-anchor": "end",
    "font-size": 9, fill: "var(--svg-danger)", "font-weight": 700
  });
  midLbl.textContent = "\u2190 midpoint: every page read from disk enters here";
  svg.appendChild(midLbl);
  var olbl = anim.svgEl("text", {x: 16, y: g.oldLabelY, "font-size": 11, "font-weight": 700, fill: "var(--svg-ink)"});
  olbl.textContent = "Old sublist (" + oldCap + " slots) \u2014 scan pages live and die here";
  svg.appendChild(olbl);
  var oldSlots = [], oldLabels = [];
  for (var j = 0; j < oldCap; j++) {
    var oc = cell(svg, 16 + (j % COLS) * (CELL_W + GAP), g.oldStartY + Math.floor(j / COLS) * ROW);
    oldSlots.push(oc.rect);
    oldLabels.push(oc.label);
  }
  var lines = captionLines(svg, g, "var(--svg-ok)", "var(--svg-token-ink)");
  return {
    svg: svg, youngSlots: youngSlots, oldSlots: oldSlots,
    youngLabels: youngLabels, oldLabels: oldLabels,
    youngCap: youngCap, oldCap: oldCap,
    young: [], old: [], evictions: 0,
    noteLbl: lines.noteLbl, statsLbl: lines.statsLbl, verdictLbl: lines.verdictLbl, actLbl: lines.actLbl
  };
}

function buildClassic(poolSize, oldPct) {
  var svg = document.getElementById("svg-classic");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var g = lruGeometry(poolSize, oldPct);
  svg.setAttribute("viewBox", "0 0 400 " + g.height);
  var lbl = anim.svgEl("text", {x: 16, y: 22, "font-size": 11, "font-weight": 700, fill: "var(--svg-ink)"});
  lbl.textContent = "Single LRU list (" + poolSize + " slots)";
  svg.appendChild(lbl);
  var slots = [], slotLabels = [];
  for (var i = 0; i < poolSize; i++) {
    var c = cell(svg, 16 + (i % COLS) * (CELL_W + GAP), 32 + Math.floor(i / COLS) * ROW);
    slots.push(c.rect);
    slotLabels.push(c.label);
  }
  var lines = captionLines(svg, g, "var(--svg-danger)", "var(--svg-ink-muted)");
  return {
    svg: svg, slots: slots, slotLabels: slotLabels, list: [], cap: poolSize,
    evictions: 0, hits: 0,
    noteLbl: lines.noteLbl, statsLbl: lines.statsLbl, verdictLbl: lines.verdictLbl, actLbl: lines.actLbl
  };
}

function renderInnoDB(panel) {
  for (var i = 0; i < panel.youngSlots.length; i++) {
    if (i < panel.young.length) {
      panel.youngSlots[i].setAttribute("fill", panel.young[i].color);
      panel.youngLabels[i].textContent = panel.young[i].name || "";
    } else {
      panel.youngSlots[i].setAttribute("fill", EMPTY_COLOR);
      panel.youngLabels[i].textContent = "";
    }
  }
  for (var j = 0; j < panel.oldSlots.length; j++) {
    if (j < panel.old.length) {
      panel.oldSlots[j].setAttribute("fill", panel.old[j].color);
      panel.oldLabels[j].textContent = panel.old[j].name || "";
    } else {
      panel.oldSlots[j].setAttribute("fill", EMPTY_COLOR);
      panel.oldLabels[j].textContent = "";
    }
  }
  panel.statsLbl.textContent = "Young: " + panel.young.length + "/" + panel.youngCap +
    "  ·  Old: " + panel.old.length + "/" + panel.oldCap +
    "  ·  Evictions: " + panel.evictions;
}

function renderClassic(panel) {
  for (var i = 0; i < panel.slots.length; i++) {
    if (i < panel.list.length) {
      panel.slots[i].setAttribute("fill", panel.list[i].color);
      panel.slotLabels[i].textContent = panel.list[i].name || "";
    } else {
      panel.slots[i].setAttribute("fill", EMPTY_COLOR);
      panel.slotLabels[i].textContent = "";
    }
  }
  panel.statsLbl.textContent = "Pages: " + panel.list.length + "/" + panel.cap +
    "  ·  Evictions: " + panel.evictions + "  ·  Hits: " + panel.hits;
}

// ---- InnoDB model -----------------------------------------------------
// The two sublists keep fixed shares of the pool, as they do in a full
// pool: innodb_old_blocks_pct of the pages are old. Index 0 of each array
// is the head (most recently used end) of that sublist.

// Put a page at the head of the young sublist. Past its share, the young
// tail slides to the head of the old sublist (the midpoint moves), and a
// full old sublist then evicts its own tail.
function innodbMakeYoung(panel, entry) {
  var out = {demoted: null, evicted: null};
  panel.young.unshift(entry);
  if (panel.young.length > panel.youngCap) {
    var down = panel.young.pop();
    out.demoted = down.name;
    panel.old.unshift(down);
    if (panel.old.length > panel.oldCap) {
      out.evicted = panel.old.pop().name;
      panel.evictions++;
    }
  }
  return out;
}

// One InnoDB page access. A miss reads the page into the old sublist at
// the midpoint (InnoDB links it just behind the first old page, LRU_old;
// the model puts it at the head of the old sublist), evicting the old tail
// once the old sublist has no empty slot: the drawing's "no free page left". An old-sublist hit moves the page to the young head only when
// `promote` says its first read was innodb_old_blocks_time or more ago.
function innodbAccess(panel, pageId, color, pageName, promote) {
  for (var i = 0; i < panel.young.length; i++) {
    if (panel.young[i].id === pageId) {
      return {result: "young_hit", where: "young", index: i};
    }
  }
  for (var j = 0; j < panel.old.length; j++) {
    if (panel.old[j].id === pageId) {
      if (!promote) return {result: "old_hit", where: "old", index: j};
      var e = panel.old.splice(j, 1)[0];
      e.color = color;
      var moved = innodbMakeYoung(panel, e);
      return {result: "old_hit", promoted: true, where: "young", index: 0,
              demoted: moved.demoted, evicted: moved.evicted};
    }
  }
  var evicted = null;
  if (panel.old.length >= panel.oldCap) {
    evicted = panel.old.pop().name;
    panel.evictions++;
  }
  panel.old.unshift({id: pageId, color: color, name: pageName || ""});
  return {result: "miss", where: "old", index: 0, evicted: evicted};
}

// Textbook LRU: a hit or a miss puts the page at the head; a full list
// evicts its tail.
function classicAccess(panel, pageId, color, pageName) {
  for (var i = 0; i < panel.list.length; i++) {
    if (panel.list[i].id === pageId) {
      var e = panel.list.splice(i, 1)[0];
      e.color = color;
      panel.list.unshift(e);
      panel.hits++;
      return {result: "hit", evicted: null};
    }
  }
  var evicted = null;
  if (panel.list.length >= panel.cap) {
    evicted = panel.list.pop().name;
    panel.evictions++;
  }
  panel.list.unshift({id: pageId, color: color, name: pageName || ""});
  return {result: "miss", evicted: evicted};
}

function hotName(pageId) {
  return HOT_PAGE_NAMES[pageId] || ("page:" + pageId);
}

function hotPagesIn(list) {
  var n = 0;
  for (var i = 0; i < list.length; i++) if (list[i].id < HOT_PAGES) n++;
  return n;
}

// The whole story without drawing, so the guided steps know before playing
// which page first evicts and what each act-3 read finds. The timeline then
// replays the same calls on the drawn panels.
function planStory(poolSize, oldPct, scanPages) {
  var youngCap = Math.max(1, Math.floor(poolSize * (100 - oldPct) / 100));
  var inno = {young: [], old: [], youngCap: youngCap, oldCap: poolSize - youngCap, evictions: 0};
  var classic = {list: [], cap: poolSize, evictions: 0, hits: 0};
  var plan = {act1: [], act2: [], act3: []};
  for (var h = 0; h < HOT_PAGES; h++) {
    plan.act1.push(innodbMakeYoung(inno, {id: h, color: HOT_COLOR, name: hotName(h)}));
    classic.list.unshift({id: h, color: HOT_COLOR, name: hotName(h)});
  }
  for (var s = 0; s < scanPages; s++) {
    var a = innodbAccess(inno, 1000 + s, SCAN_COLOR, scanPageName(s), false);
    var b = classicAccess(classic, 1000 + s, SCAN_COLOR, scanPageName(s));
    plan.act2.push({inno: a, classic: b});
  }
  for (var r = 0; r < HOT_PAGES; r++) {
    var inPool = innodbAccess(inno, r, HIT_COLOR, hotName(r), true);
    plan.act3.push(inPool);
  }
  return plan;
}

var innoPanel = null, classPanel = null;

// Slice 3 / A4 — staged fade for verdict labels at act boundaries.
// The old code relied on a flat tl.delay(1200) after setting .textContent
// directly; the label appeared instantly and the pause was dead air.
// This helper tweens opacity 0→1 over the pause so the act's conclusion
// visibly lands, then holds.
function fadeLabelIn(tl, el, durationMs) {
  if (!el) return;
  tl.call(function() { el.setAttribute("opacity", "0"); });
  tl.add({
    from: 0, to: 1, duration: durationMs || 400,
    ease: anim.easeOutCubic,
    onUpdate: function(t) { el.setAttribute("opacity", t.toFixed(3)); }
  });
}

// Hit/miss flash: a timeline step (so a jump to a later step lands on the
// final colour without replaying every flash) that crossfades with
// lerpColor. `target()` returns {el, from, to}, resolved at play time.
function flashStep(tl, target) {
  tl.add({
    from: 0, to: 1, duration: 240, ease: anim.easeOutCubic,
    onUpdate: function(t) {
      var f = target();
      if (f && f.el) f.el.setAttribute("fill", anim.lerpColor(f.from, f.to, t));
    },
    onComplete: function() {
      var f = target();
      if (f && f.el) anim.arrival(f.el);
    }
  });
}

function setNote(panel, text, color) {
  panel.noteLbl.textContent = text || "";
  panel.noteLbl.setAttribute("fill", color || "var(--svg-ink)");
}

// Build the timeline as a 3-act story
function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var poolSize = c.pool_size;
  var oldPct = c.old_pct;
  var scanPages = c.scan_pages;

  innoPanel = buildInnoDB(poolSize, oldPct);
  classPanel = buildClassic(poolSize, oldPct);
  renderInnoDB(innoPanel);
  renderClassic(classPanel);

  var plan = planStory(poolSize, oldPct, scanPages);
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var act3InnoHits = 0;
  var act3ClassicHits = 0;

  // ====== Act 1: Fill both pools with HOT_PAGES blue pages ======
  tl.mark("Act 1: Hot set fills pool");
  tl.beat(BEAT_KINDS.hotSet);
  tl.call(function() {
    phase.textContent = "Act 1 of 3: filling the pool with your 8 hot OLTP pages";
    innoPanel.actLbl.textContent = "Act 1: Hot set";
    classPanel.actLbl.textContent = "Act 1: Hot set";
  });
  for (var h = 0; h < HOT_PAGES; h++) {
    (function(pageId) {
      var pageName = hotName(pageId);
      tl.call(function() {
        var moved = innodbMakeYoung(innoPanel, {id: pageId, color: HOT_COLOR, name: pageName});
        classPanel.list.unshift({id: pageId, color: HOT_COLOR, name: pageName});
        renderInnoDB(innoPanel);
        renderClassic(classPanel);
        phase.textContent = "Act 1: " + pageName + " (" + (pageId + 1) + "/8) is read again over 1 s " +
          "after its first read, so InnoDB moves it to the head of the young sublist";
        setNote(innoPanel, moved.demoted
          ? "Young sublist full: " + moved.demoted + " slides down to the old sublist"
          : pageName + " → head of the young sublist");
        setNote(classPanel, pageName + " → head of the list");
      });
      tl.delay(300);
    })(h);
  }
  tl.call(function() {
    var youngHot = hotPagesIn(innoPanel.young);
    phase.textContent = youngHot === HOT_PAGES
      ? "Act 1 complete: 8 blue hot pages in both pools. Now a scan query arrives…"
      : "Act 1 complete: 8 hot pages in both pools, but InnoDB's young sublist holds only " +
        youngHot + " of them; the rest slid into the old sublist. Now a scan query arrives…";
    setNote(innoPanel, "");
    setNote(classPanel, "");
    innoPanel.verdictLbl.textContent = youngHot === HOT_PAGES
      ? "8 hot pages safe in young sublist"
      : youngHot + " of 8 hot pages fit in the young sublist";
    innoPanel.verdictLbl.setAttribute("fill", youngHot === HOT_PAGES ? "var(--svg-ok)" : "var(--svg-danger)");
    classPanel.verdictLbl.textContent = "8 hot pages at the top of the list";
  });
  // A4: stage the pause with a synchronized verdict fade-in.
  fadeLabelIn(tl, innoPanel.verdictLbl, 400);
  fadeLabelIn(tl, classPanel.verdictLbl, 400);
  tl.delay(800);

  // ====== Act 2: Scan pages stream in ======
  // Steps: the first scan page, its quick re-read, then a new step where
  // InnoDB first evicts and where the textbook list first loses a hot page.
  var firstInnoEvict = -1, firstClassicHotEvict = -1;
  for (var p = 0; p < plan.act2.length; p++) {
    if (firstInnoEvict < 0 && plan.act2[p].inno.evicted) firstInnoEvict = p;
    var ev = plan.act2[p].classic.evicted;
    if (firstClassicHotEvict < 0 && ev && HOT_PAGE_NAMES.indexOf(ev) >= 0) firstClassicHotEvict = p;
  }
  var stepStarts = [1, firstInnoEvict, firstClassicHotEvict];
  tl.mark("Act 2: Scan arrives");
  for (var s = 0; s < scanPages; s++) {
    (function(scanId) {
      var pageId = 1000 + scanId;
      var pageName = scanPageName(scanId);
      var innoEvicts = !!plan.act2[scanId].inno.evicted;
      if (scanId === 0 || (scanId > 0 && stepStarts.indexOf(scanId) >= 0)) {
        tl.beat(innoEvicts ? BEAT_KINDS.scanEvict : BEAT_KINDS.scanMiss);
      }
      tl.call(function() {
        if (scanId === 0) {
          innoPanel.actLbl.textContent = "Act 2: Full table scan";
          classPanel.actLbl.textContent = "Act 2: Full table scan";
          innoPanel.verdictLbl.textContent = "";
          classPanel.verdictLbl.textContent = "";
        }
        var a = innodbAccess(innoPanel, pageId, SCAN_COLOR, pageName, false);
        var b = classicAccess(classPanel, pageId, SCAN_COLOR, pageName);
        renderInnoDB(innoPanel);
        renderClassic(classPanel);
        var innoSays = a.evicted ? "evicts " + a.evicted + " from the old tail" : "enters at the midpoint";
        var classicSays = b.evicted
          ? "evicts " + b.evicted + (HOT_PAGE_NAMES.indexOf(b.evicted) >= 0 ? ", a hot page" : "")
          : "goes to the head, pushing the hot pages down";
        phase.textContent = "Act 2: SELECT SUM(amount) FROM events reads " + pageName +
          " (" + (scanId + 1) + "/" + scanPages + "): InnoDB " + innoSays + " · Classic " + classicSays;
        setNote(innoPanel, pageName + (a.evicted ? " evicts " + a.evicted : " → enters at the midpoint"));
        setNote(classPanel, pageName + (b.evicted ? " evicts " + b.evicted : " → head of the list"),
          b.evicted && HOT_PAGE_NAMES.indexOf(b.evicted) >= 0 ? "var(--svg-danger)" : "var(--svg-ink)");
      });
      tl.delay(200);
      if (scanId === 0) {
        // The scan reads the next rows of the same page milliseconds later:
        // an old-sublist hit under innodb_old_blocks_time leaves it old.
        tl.beat(BEAT_KINDS.scanReread);
        tl.call(function() {
          innodbAccess(innoPanel, pageId, SCAN_COLOR, pageName, false);
          renderInnoDB(innoPanel);
          phase.textContent = "Act 2: the scan reads the next rows of " + pageName +
            " a few ms later: it is in the old sublist and its first read was under " +
            "innodb_old_blocks_time (1,000 ms) ago, so it stays old";
          setNote(innoPanel, pageName + " read again after 2 ms: stays old");
          setNote(classPanel, pageName + " read again: already at the head");
        });
        tl.add({
          from: 0, to: 1, duration: 300, ease: anim.easeOutCubic,
          onUpdate: function(t) {
            var cell = innoPanel.oldSlots[0];
            if (cell) cell.setAttribute("stroke-width", (1 + 1.5 * Math.sin(Math.PI * t)).toFixed(2));
          },
          onComplete: function() {
            var cell = innoPanel.oldSlots[0];
            if (cell) cell.setAttribute("stroke-width", 1);
          }
        });
        tl.delay(500);
      }
    })(s);
  }
  tl.call(function() {
    phase.textContent = "Act 2 complete: the scan is done. Now the hot queries come back…";
    setNote(innoPanel, "");
    setNote(classPanel, "");
    var innoSurvived = hotPagesIn(innoPanel.young) + hotPagesIn(innoPanel.old);
    var classicSurvived = hotPagesIn(classPanel.list);
    innoPanel.verdictLbl.textContent = innoSurvived + "/8 hot pages survived the scan " +
      (innoSurvived === HOT_PAGES ? "✓" : "✗");
    innoPanel.verdictLbl.setAttribute("fill", innoSurvived === HOT_PAGES ? "var(--svg-ok)" : "var(--svg-danger)");
    classPanel.verdictLbl.textContent = classicSurvived + "/8 hot pages survived " +
      (classicSurvived === HOT_PAGES ? "✓" : "✗");
    classPanel.verdictLbl.setAttribute("fill", classicSurvived === HOT_PAGES ? "var(--svg-ok)" : "var(--svg-danger)");
  });
  // A4: staged fade-in during the act-2 → act-3 pause.
  fadeLabelIn(tl, innoPanel.verdictLbl, 500);
  fadeLabelIn(tl, classPanel.verdictLbl, 500);
  tl.delay(900);

  // ====== Act 3: Re-access the 8 hot pages ======
  tl.mark("Act 3: Hot queries return");
  for (var r = 0; r < HOT_PAGES; r++) {
    (function(pageId) {
      var pageName = hotName(pageId);
      var expect = plan.act3[pageId];
      var kind = expect.result === "young_hit" ? BEAT_KINDS.returnYoung
        : expect.result === "old_hit" ? BEAT_KINDS.returnOld
        : expect.evicted ? BEAT_KINDS.returnMiss : BEAT_KINDS.returnMissRoom;
      var innoFlash = null, classFlash = null;
      tl.beat(kind);
      tl.call(function() {
        if (pageId === 0) {
          innoPanel.actLbl.textContent = "Act 3: Hot queries return";
          classPanel.actLbl.textContent = "Act 3: Hot queries return";
          innoPanel.verdictLbl.textContent = "";
          classPanel.verdictLbl.textContent = "";
          act3InnoHits = 0;
          act3ClassicHits = 0;
        }
        // Resolve the read now; the flash step after this one plays it.
        var a = innodbAccess(innoPanel, pageId, HIT_COLOR, pageName, true);
        var innoText;
        if (a.result === "miss") {
          innoPanel.old[0].color = MISS_COLOR;
          innoFlash = {el: innoPanel.oldSlots[0], from: EMPTY_COLOR, to: MISS_COLOR};
          innoText = "MISS ✗, read from disk" + (a.evicted ? ", evicting " + a.evicted : "");
          setNote(innoPanel, pageName + " was evicted: read from disk again", "var(--svg-danger)");
        } else {
          act3InnoHits++;
          var list = a.where === "young" ? innoPanel.young : innoPanel.old;
          list[a.index].color = HIT_COLOR;
          innoFlash = {el: (a.where === "young" ? innoPanel.youngSlots : innoPanel.oldSlots)[a.index],
                       from: HOT_COLOR, to: HIT_COLOR};
          innoText = a.promoted ? "HIT ✓ in the old sublist, moved to the young head"
                                : "HIT ✓ in the young sublist";
          setNote(innoPanel, pageName + (a.promoted ? " found in the old sublist → young head" : " found in the young sublist"),
            "var(--svg-ok)");
        }
        renderInnoDB(innoPanel);

        var classResult = "miss";
        for (var ic = 0; ic < classPanel.list.length; ic++) {
          if (classPanel.list[ic].id === pageId) {
            classResult = "hit";
            classPanel.list[ic].color = HIT_COLOR;
            act3ClassicHits++;
            classFlash = {el: classPanel.slots[ic], from: HOT_COLOR, to: HIT_COLOR};
            break;
          }
        }
        if (classResult === "miss") {
          classicAccess(classPanel, pageId, MISS_COLOR, pageName);
          classFlash = {el: classPanel.slots[0], from: EMPTY_COLOR, to: MISS_COLOR};
          setNote(classPanel, pageName + " was evicted: read from disk again", "var(--svg-danger)");
        } else {
          setNote(classPanel, pageName + " still in the list", "var(--svg-ok)");
        }
        renderClassic(classPanel);

        phase.textContent = "Act 3: SELECT for " + pageName + " (" + (pageId + 1) + "/8): " +
          "InnoDB " + innoText + " · Classic " + (classResult === "hit" ? "HIT ✓" : "MISS ✗, read from disk");

        document.getElementById("out-innodb-hits").textContent = act3InnoHits + "/" + HOT_PAGES;
        document.getElementById("out-classic-hits").textContent = act3ClassicHits + "/" + HOT_PAGES;
      });
      flashStep(tl, function() { return innoFlash; });
      flashStep(tl, function() { return classFlash; });
      tl.delay(260);
    })(r);
  }
  tl.mark("Results");
  tl.beat(BEAT_KINDS.summary);
  tl.call(function() {
    document.getElementById("out-young").textContent = innoPanel.young.length + "/" + innoPanel.youngCap;
    document.getElementById("out-evictions").textContent = innoPanel.evictions;
    document.getElementById("out-classic-evictions").textContent = classPanel.evictions;
    document.getElementById("out-innodb-hits").textContent = act3InnoHits + "/" + HOT_PAGES;
    document.getElementById("out-classic-hits").textContent = act3ClassicHits + "/" + HOT_PAGES;
    setNote(innoPanel, "");
    setNote(classPanel, "");

    var survived = act3InnoHits === HOT_PAGES;
    innoPanel.verdictLbl.textContent = act3InnoHits + "/8 cache hits: " +
      (survived ? "hot set survived! ✓" : "young sublist too small ✗");
    innoPanel.verdictLbl.setAttribute("fill", survived ? "var(--svg-ok)" : "var(--svg-danger)");
    classPanel.verdictLbl.textContent = act3ClassicHits + "/8 cache hits: " +
      (act3ClassicHits === HOT_PAGES ? "hot set survived ✓" : "hot set was wiped ✗");
    classPanel.verdictLbl.setAttribute("fill", act3ClassicHits === HOT_PAGES ? "var(--svg-ok)" : "var(--svg-danger)");

    var exp = "The scan read " + scanPages + " unique pages. ";
    if (survived) {
      exp += "InnoDB kept all " + act3InnoHits + " hot pages: every scan page entered the old " +
        "sublist at the midpoint, its quick re-reads under innodb_old_blocks_time left it there, " +
        "and the scan's pages evicted each other from the old tail. ";
    } else {
      exp += "InnoDB kept " + act3InnoHits + " of 8 hot pages: with innodb_old_blocks_pct=" + oldPct +
        " the young sublist holds only " + innoPanel.youngCap + " pages, so the rest of the hot set " +
        "sat in the old sublist where the scan evicted it. ";
    }
    exp += "Classic LRU kept " + act3ClassicHits + " of 8 " +
      "because every scan page went to the head of its single list" +
      (act3ClassicHits < HOT_PAGES ? " and pushed hot pages off the end. " : ". ") +
      "That is why InnoDB's midpoint-insertion LRU exists.";
    document.getElementById("out-explanation").textContent = exp;

    phase.textContent = survived
      ? "✓ All 3 acts complete: InnoDB " + act3InnoHits + "/8 hits, classic " + act3ClassicHits +
        "/8: the scan's pages entered at the midpoint and stayed old"
      : "All 3 acts complete: InnoDB " + act3InnoHits + "/8 hits: a young sublist of " +
        innoPanel.youngCap + " pages could not hold the 8 hot pages";
  });
  tl.delay(300);

  return tl;
}

function resetAnim() {
  var c = teachRuntime.readControls();
  innoPanel = buildInnoDB(c.pool_size, c.old_pct);
  classPanel = buildClassic(c.pool_size, c.old_pct);
  renderInnoDB(innoPanel);
  renderClassic(classPanel);
  document.getElementById("out-young").textContent = "—";
  document.getElementById("out-evictions").textContent = "—";
  document.getElementById("out-innodb-hits").textContent = "—";
  document.getElementById("out-classic-evictions").textContent = "—";
  document.getElementById("out-classic-hits").textContent = "—";
  document.getElementById("out-explanation").textContent = "Press Next step, or Play to run the 3-act story.";
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function recompute() {
  resetAnim();
}

teachRuntime.wire(recompute);
teachRuntime.wireToolbar({
  build: buildCurrentTimeline,
  reset: resetAnim
});
teachRuntime.wirePhaseNav("phase-nav", {
  build: buildCurrentTimeline,
  reset: resetAnim
});
