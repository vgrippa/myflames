"""Lesson: InnoDB midpoint-insertion LRU vs textbook LRU.

Redesigned as a 3-act story so the scan-resistance property is visceral:

Act 1 -- "The hot set": 8 OLTP pages fill both pools. Blue pages = hot.
Act 2 -- "The scan arrives": 30+ unique scan pages stream through.
         Classic LRU: hot pages are evicted one by one.
         InnoDB: scan pages enter only the old sublist. Young stays put.
Act 3 -- "Hot queries return": the original 8 pages are re-accessed.
         Classic: all misses (they're gone). InnoDB: all hits (still there).

Each act is a separate phase with a pause and a clear label in between.

Concrete sample data: hot pages are labelled with actual table/row names
(e.g. "users:42", "orders:101") and scan pages show "events:1001" etc.,
following the same pattern as hash_join.py.
"""
from .. import _html
from .._cost_model import (
    INNODB_OLD_BLOCKS_PCT_DEFAULT,
    INNODB_OLD_BLOCKS_TIME_DEFAULT_MS,
)


_LESSON_JS_TEMPLATE = r"""
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
var HOT_COLOR = "#2563eb";   // blue (OLTP hot)
var SCAN_COLOR = "#f97316";  // orange (scan)
var EVICT_COLOR = "#fca5a5"; // light red (about to be evicted)
var MISS_COLOR = "#ef4444";  // red flash (miss in act 3)
var HIT_COLOR = "#10b981";   // green flash (hit in act 3)
var EMPTY_COLOR = "#e5e7eb"; // grey (empty slot)

var CELL_W = 38, CELL_H = 18, GAP = 3;

function buildInnoDB(poolSize, oldPct) {
  var svg = document.getElementById("svg-innodb");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var youngCap = Math.max(1, Math.floor(poolSize * (100 - oldPct) / 100));
  var oldCap = poolSize - youngCap;
  // Young label
  var ylbl = anim.svgEl("text", {x: 16, y: 22, "font-size": 11, "font-weight": 700, fill: "#1e40af"});
  ylbl.textContent = "Young sublist (" + youngCap + " slots) \u2014 your hot OLTP pages live here";
  svg.appendChild(ylbl);
  // Young slots (with text labels)
  var youngSlots = [];
  var youngLabels = [];
  var colsPerRow = Math.min(youngCap, 9);
  for (var i = 0; i < youngCap; i++) {
    var row = Math.floor(i / colsPerRow);
    var col = i %% colsPerRow;
    var r = anim.svgEl("rect", {
      x: 16 + col * (CELL_W + GAP), y: 32 + row * (CELL_H + GAP),
      width: CELL_W, height: CELL_H,
      rx: 3, ry: 3, fill: EMPTY_COLOR, stroke: "#d1d5db", "stroke-width": 1
    });
    svg.appendChild(r);
    var tLbl = anim.svgEl("text", {
      x: 16 + col * (CELL_W + GAP) + CELL_W / 2,
      y: 32 + row * (CELL_H + GAP) + CELL_H / 2 + 3,
      "text-anchor": "middle",
      "font-size": 6, "font-weight": 600, fill: "#ffffff"
    });
    svg.appendChild(tLbl);
    youngSlots.push(r);
    youngLabels.push(tLbl);
  }
  // Midpoint
  var youngRows = Math.ceil(youngCap / colsPerRow);
  var midY = 32 + youngRows * (CELL_H + GAP) + 6;
  var mid = anim.svgEl("line", {
    x1: 16, y1: midY, x2: 384, y2: midY,
    stroke: "#dc2626", "stroke-width": 2, "stroke-dasharray": "5 3"
  });
  svg.appendChild(mid);
  var midLbl = anim.svgEl("text", {
    x: 384, y: midY - 4, "text-anchor": "end",
    "font-size": 9, fill: "#dc2626", "font-weight": 700
  });
  midLbl.textContent = "\u2190 midpoint: scan pages enter here, never above";
  svg.appendChild(midLbl);
  // Old label
  var oldLabelY = midY + 16;
  var olbl = anim.svgEl("text", {x: 16, y: oldLabelY, "font-size": 11, "font-weight": 700, fill: "#374151"});
  olbl.textContent = "Old sublist (" + oldCap + " slots) \u2014 scan pages live and die here";
  svg.appendChild(olbl);
  // Old slots (with text labels)
  var oldSlots = [];
  var oldLabels = [];
  var oldStartY = oldLabelY + 10;
  var oldColsPerRow = Math.min(oldCap, 9);
  for (var j = 0; j < oldCap; j++) {
    var orow = Math.floor(j / oldColsPerRow);
    var ocol = j %% oldColsPerRow;
    var r2 = anim.svgEl("rect", {
      x: 16 + ocol * (CELL_W + GAP), y: oldStartY + orow * (CELL_H + GAP),
      width: CELL_W, height: CELL_H,
      rx: 3, ry: 3, fill: EMPTY_COLOR, stroke: "#d1d5db", "stroke-width": 1
    });
    svg.appendChild(r2);
    var oLbl = anim.svgEl("text", {
      x: 16 + ocol * (CELL_W + GAP) + CELL_W / 2,
      y: oldStartY + orow * (CELL_H + GAP) + CELL_H / 2 + 3,
      "text-anchor": "middle",
      "font-size": 6, "font-weight": 600, fill: "#ffffff"
    });
    svg.appendChild(oLbl);
    oldSlots.push(r2);
    oldLabels.push(oLbl);
  }
  // Stats
  var statsLbl = anim.svgEl("text", {x: 200, y: 220, "text-anchor": "middle", "font-size": 12, "font-weight": 600, fill: "#374151"});
  svg.appendChild(statsLbl);
  var verdictLbl = anim.svgEl("text", {x: 200, y: 245, "text-anchor": "middle", "font-size": 13, "font-weight": 700, fill: "#065f46"});
  svg.appendChild(verdictLbl);
  var actLbl = anim.svgEl("text", {x: 200, y: 270, "text-anchor": "middle", "font-size": 14, "font-weight": 700, fill: "#1e40af"});
  svg.appendChild(actLbl);
  return {
    svg: svg, youngSlots: youngSlots, oldSlots: oldSlots,
    youngLabels: youngLabels, oldLabels: oldLabels,
    youngCap: youngCap, oldCap: oldCap,
    young: [], old: [], evictions: 0,
    statsLbl: statsLbl, verdictLbl: verdictLbl, actLbl: actLbl
  };
}

function buildClassic(poolSize) {
  var svg = document.getElementById("svg-classic");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var lbl = anim.svgEl("text", {x: 16, y: 22, "font-size": 11, "font-weight": 700, fill: "#374151"});
  lbl.textContent = "Single LRU list (" + poolSize + " slots)";
  svg.appendChild(lbl);
  var slots = [];
  var slotLabels = [];
  var colsPerRow = Math.min(poolSize, 9);
  for (var i = 0; i < poolSize; i++) {
    var row = Math.floor(i / colsPerRow);
    var col = i %% colsPerRow;
    var r = anim.svgEl("rect", {
      x: 16 + col * (CELL_W + GAP), y: 32 + row * (CELL_H + GAP),
      width: CELL_W, height: CELL_H,
      rx: 3, ry: 3, fill: EMPTY_COLOR, stroke: "#d1d5db", "stroke-width": 1
    });
    svg.appendChild(r);
    var sLbl = anim.svgEl("text", {
      x: 16 + col * (CELL_W + GAP) + CELL_W / 2,
      y: 32 + row * (CELL_H + GAP) + CELL_H / 2 + 3,
      "text-anchor": "middle",
      "font-size": 6, "font-weight": 600, fill: "#ffffff"
    });
    svg.appendChild(sLbl);
    slots.push(r);
    slotLabels.push(sLbl);
  }
  var statsLbl = anim.svgEl("text", {x: 200, y: 220, "text-anchor": "middle", "font-size": 12, "font-weight": 600, fill: "#374151"});
  svg.appendChild(statsLbl);
  var verdictLbl = anim.svgEl("text", {x: 200, y: 245, "text-anchor": "middle", "font-size": 13, "font-weight": 700, fill: "#991b1b"});
  svg.appendChild(verdictLbl);
  var actLbl = anim.svgEl("text", {x: 200, y: 270, "text-anchor": "middle", "font-size": 14, "font-weight": 700, fill: "#6b7280"});
  svg.appendChild(actLbl);
  return {
    svg: svg, slots: slots, slotLabels: slotLabels, list: [], cap: poolSize,
    evictions: 0, hits: 0,
    statsLbl: statsLbl, verdictLbl: verdictLbl, actLbl: actLbl
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
    "  \u00b7  Old: " + panel.old.length + "/" + panel.oldCap +
    "  \u00b7  Evictions: " + panel.evictions;
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
    "  \u00b7  Evictions: " + panel.evictions + "  \u00b7  Hits: " + panel.hits;
}

// InnoDB step: simplified for the 3-act story
function innodbAccess(panel, pageId, color, nowMs, pageName) {
  // Check young
  for (var i = 0; i < panel.young.length; i++) {
    if (panel.young[i].id === pageId) {
      var e = panel.young.splice(i, 1)[0];
      e.color = color;
      panel.young.unshift(e);
      return "young_hit";
    }
  }
  // Check old
  for (var j = 0; j < panel.old.length; j++) {
    if (panel.old[j].id === pageId) {
      return "old_hit";
    }
  }
  // Miss: insert at head of old
  if (panel.old.length >= panel.oldCap) {
    panel.old.pop();
    panel.evictions++;
  }
  panel.old.unshift({id: pageId, color: color, firstSeen: nowMs, name: pageName || ""});
  return "miss";
}

function classicAccess(panel, pageId, color, pageName) {
  for (var i = 0; i < panel.list.length; i++) {
    if (panel.list[i].id === pageId) {
      var e = panel.list.splice(i, 1)[0];
      e.color = color;
      panel.list.unshift(e);
      panel.hits++;
      return "hit";
    }
  }
  if (panel.list.length >= panel.cap) {
    panel.list.pop();
    panel.evictions++;
  }
  panel.list.unshift({id: pageId, color: color, name: pageName || ""});
  return "miss";
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

// Slice 3 / A4 — hit/miss flash via lerpColor instead of a hard swap.
// The color crossfades through EMPTY_COLOR so we never dip into muddy
// intermediates (web-design's Round 2 warning about blue→orange
// mid-transitions hitting low-contrast browns).
function flashSlot(tl, slotEl, fromColor, toColor) {
  if (!slotEl) return;
  tl.add({
    from: 0, to: 1, duration: 220, ease: anim.easeOutCubic,
    onUpdate: function(t) {
      slotEl.setAttribute("fill", anim.lerpColor(fromColor, toColor, t));
    },
    onComplete: function() { anim.arrival(slotEl); }
  });
}

// Build the timeline as a 3-act story
function buildCurrentTimeline() {
  var c = teachRuntime.readControls();
  var poolSize = c.pool_size;
  var oldPct = c.old_pct;
  var scanPages = c.scan_pages;

  innoPanel = buildInnoDB(poolSize, oldPct);
  classPanel = buildClassic(poolSize);

  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var act3InnoHits = 0;
  var act3ClassicHits = 0;

  // ====== Act 1: Fill both pools with HOT_PAGES blue pages ======
  tl.mark("Act 1: Hot set fills pool");
  tl.call(function() {
    phase.textContent = "Act 1 of 3 \u2014 Filling the pool with your 8 hot OLTP pages";
    innoPanel.actLbl.textContent = "Act 1: Hot set";
    classPanel.actLbl.textContent = "Act 1: Hot set";
  });
  for (var h = 0; h < HOT_PAGES; h++) {
    (function(pageId) {
      var pageName = HOT_PAGE_NAMES[pageId] || ("page:" + pageId);
      tl.call(function() {
        innoPanel.young.push({id: pageId, color: HOT_COLOR, firstSeen: 0, name: pageName});
        classPanel.list.unshift({id: pageId, color: HOT_COLOR, name: pageName});
        renderInnoDB(innoPanel);
        renderClassic(classPanel);
        phase.textContent = "Act 1 \u2014 Hot page " + pageName + " (" + (pageId + 1) + "/8) added to both pools";
      });
      tl.delay(300);
    })(h);
  }
  tl.call(function() {
    phase.textContent = "Act 1 complete \u2014 8 blue hot pages in both pools. Now a scan query arrives\u2026";
    innoPanel.verdictLbl.textContent = "8 hot pages safe in young sublist";
    classPanel.verdictLbl.textContent = "8 hot pages at the top of the list";
  });
  // A4: stage the pause with a synchronized verdict fade-in.
  fadeLabelIn(tl, innoPanel.verdictLbl, 400);
  fadeLabelIn(tl, classPanel.verdictLbl, 400);
  tl.delay(800);

  // ====== Act 2: Scan pages stream in ======
  tl.mark("Act 2: Scan arrives");
  tl.call(function() {
    phase.textContent = "Act 2 of 3 \u2014 SELECT SUM(amount) FROM events WHERE \u2026 starts scanning";
    innoPanel.actLbl.textContent = "Act 2: Full table scan";
    classPanel.actLbl.textContent = "Act 2: Full table scan";
    innoPanel.verdictLbl.textContent = "";
    classPanel.verdictLbl.textContent = "";
  });
  for (var s = 0; s < scanPages; s++) {
    (function(scanId) {
      tl.call(function() {
        var pageId = 1000 + scanId;
        var pageName = scanPageName(scanId);
        innodbAccess(innoPanel, pageId, SCAN_COLOR, 100 + scanId * 5, pageName);
        classicAccess(classPanel, pageId, SCAN_COLOR, pageName);
        renderInnoDB(innoPanel);
        renderClassic(classPanel);
        phase.textContent = "Act 2 \u2014 Scan page " + pageName + " (" + (scanId + 1) + "/" + scanPages +
          ") (InnoDB: into old only \u00b7 Classic: pushes hot pages down)";
      });
      tl.delay(200);
    })(s);
  }
  tl.call(function() {
    phase.textContent = "Act 2 complete \u2014 scan is done. Now the hot queries come back\u2026";
    var innoSurvived = 0;
    for (var k = 0; k < innoPanel.young.length; k++) {
      if (innoPanel.young[k].id < HOT_PAGES) innoSurvived++;
    }
    var classicSurvived = 0;
    for (var m = 0; m < classPanel.list.length; m++) {
      if (classPanel.list[m].id < HOT_PAGES) classicSurvived++;
    }
    innoPanel.verdictLbl.textContent = innoSurvived + "/8 hot pages survived the scan \u2713";
    innoPanel.verdictLbl.setAttribute("fill", "#065f46");
    classPanel.verdictLbl.textContent = classicSurvived + "/8 hot pages survived \u2717";
    classPanel.verdictLbl.setAttribute("fill", "#991b1b");
  });
  // A4: staged fade-in during the act-2 → act-3 pause.
  fadeLabelIn(tl, innoPanel.verdictLbl, 500);
  fadeLabelIn(tl, classPanel.verdictLbl, 500);
  tl.delay(900);

  // ====== Act 3: Re-access the 8 hot pages ======
  tl.mark("Act 3: Hot queries return");
  tl.call(function() {
    phase.textContent = "Act 3 of 3 \u2014 OLTP queries return: SELECT * FROM users WHERE id = 42";
    innoPanel.actLbl.textContent = "Act 3: Hot queries return";
    classPanel.actLbl.textContent = "Act 3: Hot queries return";
    innoPanel.verdictLbl.textContent = "";
    classPanel.verdictLbl.textContent = "";
    act3InnoHits = 0;
    act3ClassicHits = 0;
  });
  for (var r = 0; r < HOT_PAGES; r++) {
    (function(pageId) {
      var pageName = HOT_PAGE_NAMES[pageId] || ("page:" + pageId);
      tl.call(function() {
        // A4: resolve state, then fire a lerpColor flash directly
        // (not via tl.add — the resolution has to happen at play
        // time but the flash has to be same-step so it reads the
        // freshly-resolved state).
        var innoFlashTarget = null, innoFlashFrom = HOT_COLOR, innoFlashTo = HIT_COLOR;
        var classFlashTarget = null, classFlashFrom = HOT_COLOR, classFlashTo = HIT_COLOR;
        var innoResult = "miss", classResult = "miss";

        for (var iy = 0; iy < innoPanel.young.length; iy++) {
          if (innoPanel.young[iy].id === pageId) {
            innoResult = "hit";
            innoPanel.young[iy].color = HIT_COLOR;
            act3InnoHits++;
            innoFlashTarget = innoPanel.youngSlots[iy];
            innoFlashFrom = HOT_COLOR;
            innoFlashTo = HIT_COLOR;
            break;
          }
        }
        if (innoResult === "miss") {
          innodbAccess(innoPanel, pageId, MISS_COLOR, 9999, pageName);
          innoFlashTarget = innoPanel.oldSlots[0];
          innoFlashFrom = innoPanel.oldSlots[0].getAttribute("fill");
          innoFlashTo = MISS_COLOR;
        }
        renderInnoDB(innoPanel);

        for (var ic = 0; ic < classPanel.list.length; ic++) {
          if (classPanel.list[ic].id === pageId) {
            classResult = "hit";
            classPanel.list[ic].color = HIT_COLOR;
            act3ClassicHits++;
            classFlashTarget = classPanel.slots[ic];
            classFlashFrom = HOT_COLOR;
            classFlashTo = HIT_COLOR;
            break;
          }
        }
        if (classResult === "miss") {
          classicAccess(classPanel, pageId, MISS_COLOR, pageName);
          classFlashTarget = classPanel.slots[0];
          classFlashFrom = classPanel.slots[0].getAttribute("fill");
          classFlashTo = MISS_COLOR;
        }
        renderClassic(classPanel);

        // Fire the lerpColor flashes directly. anim.tween is
        // standalone — not queued on the timeline — so it plays
        // concurrently with the 500 ms tl.delay() that follows.
        if (innoFlashTarget) {
          anim.tween({
            from: 0, to: 1, duration: 240, ease: anim.easeOutCubic,
            onUpdate: function(t) {
              innoFlashTarget.setAttribute(
                "fill", anim.lerpColor(innoFlashFrom, innoFlashTo, t));
            },
            onComplete: function() { anim.arrival(innoFlashTarget); }
          });
        }
        if (classFlashTarget) {
          anim.tween({
            from: 0, to: 1, duration: 240, ease: anim.easeOutCubic,
            onUpdate: function(t) {
              classFlashTarget.setAttribute(
                "fill", anim.lerpColor(classFlashFrom, classFlashTo, t));
            },
            onComplete: function() { anim.arrival(classFlashTarget); }
          });
        }

        phase.textContent = "Act 3 \u2014 Re-accessing " + pageName + " (" + (pageId + 1) + "/8): " +
          "InnoDB " + (innoResult === "hit" ? "HIT \u2713" : "MISS \u2717") +
          " \u00b7 Classic " + (classResult === "hit" ? "HIT \u2713" : "MISS \u2717");

        document.getElementById("out-innodb-hits").textContent = act3InnoHits + "/" + HOT_PAGES;
        document.getElementById("out-classic-hits").textContent = act3ClassicHits + "/" + HOT_PAGES;
      });
      tl.delay(500);
    })(r);
  }
  tl.call(function() {
    document.getElementById("out-young").textContent = innoPanel.young.length + "/" + innoPanel.youngCap;
    document.getElementById("out-evictions").textContent = innoPanel.evictions;
    document.getElementById("out-classic-evictions").textContent = classPanel.evictions;
    document.getElementById("out-innodb-hits").textContent = act3InnoHits + "/" + HOT_PAGES;
    document.getElementById("out-classic-hits").textContent = act3ClassicHits + "/" + HOT_PAGES;

    innoPanel.verdictLbl.textContent = act3InnoHits + "/8 cache hits \u2014 hot set survived! \u2713";
    innoPanel.verdictLbl.setAttribute("fill", "#065f46");
    classPanel.verdictLbl.textContent = act3ClassicHits + "/8 cache hits \u2014 hot set was wiped \u2717";
    classPanel.verdictLbl.setAttribute("fill", "#991b1b");

    var exp = "The scan read " + scanPages + " unique pages. " +
      "InnoDB kept all " + act3InnoHits + " hot pages in the young sublist \u2014 " +
      "the scan only cycled through the old sublist and got evicted there. " +
      "Classic LRU lost " + (HOT_PAGES - act3ClassicHits) + " of 8 hot pages " +
      "because the scan pushed them off the end of the single list. " +
      "That is why InnoDB's midpoint-insertion LRU exists.";
    document.getElementById("out-explanation").textContent = exp;

    phase.textContent = "\u2713 All 3 acts complete \u2014 InnoDB's hot set survived the scan";
  });
  tl.mark("Results");

  return tl;
}

function resetAnim() {
  var c = teachRuntime.readControls();
  innoPanel = buildInnoDB(c.pool_size, c.old_pct);
  classPanel = buildClassic(c.pool_size);
  renderInnoDB(innoPanel);
  renderClassic(classPanel);
  document.getElementById("out-young").textContent = "\u2014";
  document.getElementById("out-evictions").textContent = "\u2014";
  document.getElementById("out-innodb-hits").textContent = "\u2014";
  document.getElementById("out-classic-evictions").textContent = "\u2014";
  document.getElementById("out-classic-hits").textContent = "\u2014";
  document.getElementById("out-explanation").textContent = "Press Play to start the 3-act story.";
  document.getElementById("phase-label").textContent = "Ready \u2014 press Play";
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
"""


def render() -> str:
    controls_html = f"""
<section class="controls">
  <h2>Parameters (InnoDB buffer pool)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="pool_size">Buffer pool (pages): <span class="value-pill" data-pill-for="pool_size">20</span></label>
      <input type="range" id="pool_size" name="pool_size" min="12" max="40" step="2" value="20">
      <div class="hint">For illustration. Real pools are millions of pages.</div>
    </div>

    <div class="control">
      <label for="old_pct">innodb_old_blocks_pct: <span class="value-pill" data-pill-for="old_pct">{INNODB_OLD_BLOCKS_PCT_DEFAULT}</span></label>
      <input type="range" id="old_pct" name="old_pct" min="10" max="90" step="1" value="{INNODB_OLD_BLOCKS_PCT_DEFAULT}">
      <div class="hint">% of pool reserved for the old (cold) sublist. Default {INNODB_OLD_BLOCKS_PCT_DEFAULT}.</div>
    </div>

    <div class="control">
      <label for="scan_pages">Scan pages (act 2): <span class="value-pill" data-pill-for="scan_pages">30</span></label>
      <input type="range" id="scan_pages" name="scan_pages" min="10" max="80" step="5" value="30">
      <div class="hint">How many unique pages the reporting query scans.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Act 1: Your OLTP workload keeps 8 hot pages in the pool\\n"
            "SELECT * FROM users WHERE id = 42;   -- repeated point lookups\\n\\n"
            "-- Act 2: A reporting query runs a full table scan\\n"
            "SELECT SUM(amount) FROM events WHERE event_date >= '2026-01-01';\\n\\n"
            "-- Act 3: OLTP workload returns — same 8 pages\\n"
            "SELECT * FROM users WHERE id = 42;   -- hit or miss?"
        ),
        note="Watch what happens to the 8 blue pages during the scan."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation — a 3-act story",
        [
            "Act 1 — 'The hot set': 8 blue OLTP pages fill both pools. These are your frequently-accessed users, orders, products rows.",
            "Act 2 — 'The scan arrives': orange scan pages stream in. In the textbook LRU (right), they push the blue pages out. In InnoDB (left), scan pages only enter the OLD sublist — the blue young pages don't move.",
            "Act 3 — 'Hot queries return': the same 8 blue pages are re-accessed. Classic LRU: all 8 are cache MISSES (they were evicted during the scan). InnoDB: all 8 are cache HITS (they never left the young sublist).",
            "A counter at the bottom tracks misses vs hits. After act 3, the difference is the whole argument for InnoDB's LRU design.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play to start the 3-act story")}
  <div class="stage-with-phases">
    <div style="flex:1;min-width:0;display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#1e40af;letter-spacing:0.4px;text-transform:uppercase">InnoDB midpoint-insertion LRU</p>
        <svg id="svg-innodb" viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#6b7280;letter-spacing:0.4px;text-transform:uppercase">Textbook single-list LRU</p>
        <svg id="svg-classic" viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
    </div>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Simulation results</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">InnoDB: young pages {ht("Pages in the hot (MRU) half. These survive a full scan because new pages only enter the old sublist.")}</p><p class="value" id="out-young">—</p></div>
    <div class="item"><p class="label">InnoDB: evictions {ht("Pages kicked out of the old sublist tail. During a scan these are scan pages, not your hot pages.")}</p><p class="value" id="out-evictions">—</p></div>
    <div class="item"><p class="label">Act 3 — InnoDB hits {ht("When the hot queries return in Act 3, how many find their page still in the pool. Should be 8/8 after a scan.")}</p><p class="value ok" id="out-innodb-hits">—</p></div>
    <div class="item"><p class="label">Classic LRU: evictions {ht("Pages kicked out during the scan. In a textbook LRU, these are your hot OLTP pages — the scan pushed them all out.")}</p><p class="value" id="out-classic-evictions">—</p></div>
    <div class="item"><p class="label">Act 3 — Classic hits {ht("When the hot queries return, how many find their page. Should be 0/8 after a scan — they were all evicted.")}</p><p class="value hot" id="out-classic-hits">—</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
</section>
"""

    learn_more_html = f"""
<details class="learn-more">
  <summary>Learn more — why is a full table scan a problem for plain LRU?</summary>
  <div class="body">
    <p>Imagine a 1 GB buffer pool and a 10 GB table. Under <strong>textbook
    LRU</strong>, reading the whole table once visits every page exactly
    once — and each visit bumps that page to the head of the list. By the
    end of the scan, every single page that used to be hot has been
    evicted. Your OLTP working set just got destroyed by a reporting query.</p>

    <p>InnoDB's answer is <strong>midpoint-insertion LRU</strong>:</p>
    <ol>
      <li>The linked list is split into a <em>young</em> sublist (MRU end,
      ~5/8) and an <em>old</em> sublist (LRU end, ~3/8). The split is
      <code>innodb_old_blocks_pct</code> (default {INNODB_OLD_BLOCKS_PCT_DEFAULT}).</li>

      <li>On a <strong>cache miss</strong>, the new page enters at the
      <em>midpoint</em> (head of old), NOT the head of the list.</li>

      <li>On a <strong>hit in old</strong>, the page only promotes to young if
      <code>now - first_access ≥ innodb_old_blocks_time</code> (default
      {INNODB_OLD_BLOCKS_TIME_DEFAULT_MS} ms). A one-pass scan never triggers
      this — so scan pages cycle through old and never pollute young.</li>
    </ol>

    <p>Sources: MySQL 8.4 Reference Manual §17.5.1 "Buffer Pool" and
    §17.8.3.3 "Making the Buffer Pool Scan Resistant".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="lru",
        title="InnoDB buffer pool — midpoint-insertion LRU",
        subtitle=(
            "A 3-act story: your hot pages, a full-table scan, and the "
            "moment you discover whether the scan wiped your cache."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
