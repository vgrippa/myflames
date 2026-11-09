/* ---- cost model ---- */
function skipScanCost(tableRows, ndvLeading, selectivity) {
  var rowsPerGroup = tableRows / ndvLeading;
  var matchingPerGroup = Math.max(1, Math.floor(rowsPerGroup * selectivity / 100));
  var totalMatching = matchingPerGroup * ndvLeading;
  // B+tree height approximation (log base ~500 of tableRows)
  var height = Math.max(2, Math.ceil(Math.log(tableRows) / Math.log(500)));
  // Skip scan: for each distinct A, one seek + range read
  var skipScanReads = ndvLeading * (height + Math.max(1, Math.floor(rowsPerGroup * selectivity / 100)));
  // Full scan: read every row
  var fullScanReads = tableRows;
  // Index scan: read full index (same for covering)
  var indexScanReads = tableRows;
  return {
    rowsPerGroup: Math.floor(rowsPerGroup),
    matchingPerGroup: matchingPerGroup,
    totalMatching: totalMatching,
    skipScanReads: skipScanReads,
    fullScanReads: fullScanReads,
    indexScanReads: indexScanReads,
    savings: fullScanReads - skipScanReads
  };
}

/* ---- stage globals ---- */
var W = 800, H = 400;
var stage = null;

/* Distinct leading-column values, in index order ('F' sorts before 'M'). */
var GROUPS = [
  {value: "F", label: "gender = F", color: "var(--svg-group-alt)", light: "var(--svg-group-alt-soft)"},
  {value: "M", label: "gender = M", color: "var(--svg-token)", light: "var(--svg-token-soft)"}
];

/* A few sample ages inside each group, in index order. */
var SAMPLE_ROWS = [
  {age: 18}, {age: 22}, {age: 25}, {age: 27},
  {age: 30}, {age: 35}, {age: 42}, {age: 55}
];
var RANGE_LO = 25, RANGE_HI = 30;

function buildStage() {
  var svg = document.getElementById("skip-scan-svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  var groupCount = GROUPS.length;
  var rowCount = SAMPLE_ROWS.length;
  var groupW = (W - 40) / groupCount;
  var cellH = 28;
  var groupY = 70;
  var groupH = cellH * rowCount + 40;

  /* Title */
  var title = anim.svgEl("text", {
    x: W / 2, y: 24, "text-anchor": "middle",
    "font-size": 14, "font-weight": 700, fill: "var(--svg-ink)"
  });
  title.textContent = "Index idx_gender_age (gender, age): leaf entries in index order";
  svg.appendChild(title);

  var subtitle = anim.svgEl("text", {
    x: W / 2, y: 44, "text-anchor": "middle",
    "font-size": 11, fill: "var(--svg-ink-muted)"
  });
  subtitle.textContent = "Skip scan seeks to age 25 inside each gender and stops after age 30";
  svg.appendChild(subtitle);

  var groups = [];

  for (var g = 0; g < groupCount; g++) {
    var gx = 20 + g * groupW;
    var grp = GROUPS[g];

    /* Group box */
    var box = anim.svgEl("rect", {
      x: gx, y: groupY, width: groupW - 10, height: groupH,
      rx: 8, ry: 8, fill: grp.light, stroke: grp.color,
      "stroke-width": 2, opacity: 0.9
    });
    svg.appendChild(box);

    /* Group label */
    var lbl = anim.svgEl("text", {
      x: gx + (groupW - 10) / 2, y: groupY + 20, "text-anchor": "middle",
      "font-size": 12, "font-weight": 700, fill: grp.color
    });
    lbl.textContent = grp.label;
    svg.appendChild(lbl);

    /* Entry cells */
    var cells = [];
    var cellStartY = groupY + 30;
    for (var r = 0; r < rowCount; r++) {
      var cx = gx + 10;
      var cy = cellStartY + r * cellH;

      var cellBg = anim.svgEl("rect", {
        x: cx, y: cy, width: groupW - 30, height: cellH - 4,
        rx: 4, ry: 4, fill: "var(--svg-node-bg)", stroke: "var(--svg-edge)", "stroke-width": 1
      });
      svg.appendChild(cellBg);

      var cellText = anim.svgEl("text", {
        x: cx + 8, y: cy + 17, "font-size": 11, fill: "var(--svg-ink)"
      });
      cellText.textContent = "(" + grp.value + ", age " + SAMPLE_ROWS[r].age + ")";
      svg.appendChild(cellText);

      var cellIcon = anim.svgEl("text", {
        x: cx + groupW - 42, y: cy + 17, "text-anchor": "end",
        "font-size": 10, "font-weight": 700, fill: "var(--svg-ink-muted)"
      });
      cellIcon.textContent = "";
      svg.appendChild(cellIcon);

      cells.push({bg: cellBg, icon: cellIcon, age: SAMPLE_ROWS[r].age, y: cy});
    }

    groups.push({box: box, cells: cells, label: grp.label, value: grp.value, color: grp.color, x: gx});
  }

  /* Cursor: the scan's position in the index */
  var cursor = anim.svgEl("rect", {
    x: 0, y: 0, width: 10, height: cellH - 4,
    rx: 3, ry: 3, fill: "var(--svg-build)", opacity: 0
  });
  svg.appendChild(cursor);

  /* Summary area at bottom */
  var summaryY = groupY + groupH + 12;
  var summaryBox = anim.svgEl("rect", {
    x: 20, y: summaryY, width: W - 40, height: 34, rx: 6, ry: 6,
    fill: "var(--svg-ok-soft)", stroke: "var(--svg-ok-soft)", "stroke-width": 1
  });
  svg.appendChild(summaryBox);
  var summaryText = anim.svgEl("text", {
    x: W / 2, y: summaryY + 22, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "var(--svg-ok)"
  });
  summaryText.textContent = "";
  svg.appendChild(summaryText);

  stage = {
    svg: svg, groups: groups, cursor: cursor,
    summaryText: summaryText, groupW: groupW, cellH: cellH,
    groupY: groupY, cellStartY: groupY + 30
  };
}

function resetStage() {
  if (!stage) return;
  for (var g = 0; g < stage.groups.length; g++) {
    var cells = stage.groups[g].cells;
    for (var r = 0; r < cells.length; r++) {
      cells[r].bg.setAttribute("fill", "var(--svg-node-bg)");
      cells[r].bg.setAttribute("stroke", "var(--svg-edge)");
      cells[r].bg.setAttribute("stroke-dasharray", "");
      cells[r].icon.textContent = "";
    }
  }
  stage.cursor.setAttribute("opacity", "0");
  stage.summaryText.textContent = "";
}

/* Entry states: read to learn the gender, sent (in the range), read past
   the range (stops it), and never read (jumped over by a seek). */
function markCell(cell, state, gender) {
  if (state === "prefix") {
    cell.bg.setAttribute("fill", "var(--svg-build-soft)");
    cell.bg.setAttribute("stroke", "var(--svg-build)");
    cell.icon.textContent = "read: gender is " + gender;
    cell.icon.setAttribute("fill", "var(--svg-build)");
  } else if (state === "match") {
    cell.bg.setAttribute("fill", "var(--svg-ok-soft)");
    cell.bg.setAttribute("stroke", "var(--svg-ok)");
    cell.icon.textContent = "✔ in 25..30, sent";
    cell.icon.setAttribute("fill", "var(--svg-ok)");
  } else if (state === "past") {
    cell.bg.setAttribute("fill", "var(--svg-token-soft)");
    cell.bg.setAttribute("stroke", "var(--svg-token)");
    cell.icon.textContent = "read: past 30, range ends";
    cell.icon.setAttribute("fill", "var(--svg-token-ink)");
  } else {
    cell.bg.setAttribute("fill", "var(--svg-node-muted)");
    cell.bg.setAttribute("stroke", "var(--svg-node-stroke)");
    cell.bg.setAttribute("stroke-dasharray", "4 3");
    cell.icon.textContent = "never read";
    cell.icon.setAttribute("fill", "var(--svg-ink-muted)");
  }
}

function fmtFull(n) {
  return Math.round(n).toLocaleString("en-US");
}

function buildTimeline() {
  resetStage();
  var tl = anim.timeline();
  var phase = document.getElementById("phase-label");
  var drawn = stage.groups.length * SAMPLE_ROWS.length;
  // Per group: the first entry (to learn the gender), the matches, and the
  // one entry past the range. The sample's first age is below the range.
  var matchesPerGroup = 0;
  SAMPLE_ROWS.forEach(function(row) {
    if (row.age >= RANGE_LO && row.age <= RANGE_HI) matchesPerGroup++;
  });
  var matchCount = matchesPerGroup * stage.groups.length;
  var readCount = (matchesPerGroup + 2) * stage.groups.length;

  function moveCursor(gx, toY, duration) {
    var fromX, fromY, fromOpacity;
    tl.call(function() {
      fromX = parseFloat(stage.cursor.getAttribute("x")) || gx + 2;
      fromY = parseFloat(stage.cursor.getAttribute("y")) || toY;
      fromOpacity = parseFloat(stage.cursor.getAttribute("opacity")) || 0;
    });
    tl.add({
      from: 0, to: 1, duration: duration, ease: anim.easeInOutCubic,
      onUpdate: function(t) {
        stage.cursor.setAttribute("x", anim.lerp(fromX, gx + 2, t));
        stage.cursor.setAttribute("y", anim.lerp(fromY, toY, t));
        stage.cursor.setAttribute("opacity", anim.lerp(fromOpacity, 1, t));
      }
    });
  }

  tl.mark("Find the first gender");
  for (var g = 0; g < stage.groups.length; g++) {
    (function(gi) {
      var grp = stage.groups[gi];
      var cells = grp.cells;
      var first = cells[0];
      var loIdx = 0;
      while (cells[loIdx].age < RANGE_LO) loIdx++;
      var hiIdx = loIdx;
      while (hiIdx + 1 < cells.length && cells[hiIdx + 1].age <= RANGE_HI) hiIdx++;
      var pastIdx = hiIdx + 1;

      /* 1. Learn the next gender value: the first entry of the index, or
         the first entry after every entry of the previous gender. */
      if (gi === 0) {
        tl.beat("first-prefix");
        tl.call(function() {
          phase.textContent = "Read the first entry of the index, (" + grp.value + ", age " + first.age +
            "): the first gender value is " + grp.value;
        });
      } else {
        var prev = stage.groups[gi - 1].value;
        tl.beat("next-prefix");
        tl.mark("Next gender: " + grp.value);
        tl.call(function() {
          phase.textContent = "Seek past every " + prev + " entry: the next entry is (" + grp.value +
            ", age " + first.age + "), so the next gender value is " + grp.value;
        });
      }
      moveCursor(grp.x, first.y, gi === 0 ? 300 : 520);
      tl.call(function() { markCell(first, "prefix", grp.value); });
      tl.delay(240);

      /* 2. Seek straight to (g, 25): the ages below 25 are jumped over. */
      tl.beat("seek");
      tl.mark("Seek within " + grp.label);
      tl.call(function() {
        phase.textContent = "Seek to the first entry with gender = " + grp.value + " and age >= " + RANGE_LO +
          ": (" + grp.value + ", age " + cells[loIdx].age + "). The ages between are jumped over, never read";
        for (var k = 1; k < loIdx; k++) markCell(cells[k], "never");
      });
      moveCursor(grp.x, cells[loIdx].y, 360);
      tl.delay(200);

      /* 3. Read the range; the first entry past 30 ends it. */
      tl.beat("range");
      tl.call(function() {
        phase.textContent = "Read " + grp.label + " entries while age <= " + RANGE_HI + "…";
      });
      for (var r = loIdx; r <= hiIdx; r++) {
        (function(ri) {
          moveCursor(grp.x, cells[ri].y, 180);
          tl.call(function() { markCell(cells[ri], "match"); });
          tl.delay(160);
        })(r);
      }
      if (pastIdx < cells.length) {
        moveCursor(grp.x, cells[pastIdx].y, 180);
        tl.call(function() {
          markCell(cells[pastIdx], "past");
          for (var k = pastIdx + 1; k < cells.length; k++) markCell(cells[k], "never");
          var ages = [];
          for (var a = loIdx; a <= hiIdx; a++) ages.push(cells[a].age);
          phase.textContent = grp.label + ": ages " + ages.join(", ") + " are in 25 to 30 and sent. (" +
            grp.value + ", age " + cells[pastIdx].age + ") is past 30, so this range ends; the later " +
            grp.value + " entries are never read";
        });
      }
      tl.delay(240);
    })(g);
  }

  /* 4. No gender left. */
  tl.beat("end");
  tl.mark("Summary");
  tl.call(function() {
    stage.cursor.setAttribute("opacity", "0");
    var c = teachRuntime.readControls();
    var cost = skipScanCost(c.table_rows, c.ndv_leading, c.selectivity);
    phase.textContent = "✓ Seek past every " + stage.groups[stage.groups.length - 1].value +
      " entry: none left, so the skip scan is done. At " + fmtFull(c.table_rows) + " rows and " +
      fmtFull(c.ndv_leading) + " values of the first column, that is " + fmtFull(c.ndv_leading) +
      " groups, each with 2 seeks and one short range";
    stage.summaryText.textContent = "Read " + readCount + " of the " + drawn + " entries drawn: " + matchCount +
      " matches, " + stage.groups.length + " to learn the genders, " + stage.groups.length +
      " past a range; " + (drawn - readCount) + " never read";
  });

  return tl;
}

function buildCurrentTimeline() {
  return buildTimeline();
}

function resetAnim() {
  resetStage();
  document.getElementById("phase-label").textContent = "Ready: press Next step, or Play to run it all";
}

function renderChart(tableRows) {
  var c = teachRuntime.readControls();
  anim.complexityChart({
    svgId: "complexity-chart",
    width: 560, height: 200,
    xMin: 1000, xMax: 5e6,
    xLabel: "Table rows", yLabel: "Reads",
    curves: [
      { label: "Full table scan", color: "var(--svg-danger)",
        fn: function(n) { return n; } },
      { label: "Full index scan", color: "var(--svg-build)",
        fn: function(n) { return n; } },
      { label: "Skip Scan (NDV=" + c.ndv_leading + ", sel=" + c.selectivity + "%)", color: "var(--svg-ok)",
        fn: function(n) {
          var height = Math.max(2, Math.ceil(Math.log(n) / Math.log(500)));
          var rpg = n / c.ndv_leading;
          return c.ndv_leading * (height + Math.max(1, Math.floor(rpg * c.selectivity / 100)));
        }
      }
    ],
    current: { x: tableRows },
    xSlider: "table_rows",
    xSliderTransform: function(xVal) { return Math.round(Math.max(1000, Math.min(5000000, xVal))); }
  });
}

function recompute() {
  var c = teachRuntime.readControls();
  var cost = skipScanCost(c.table_rows, c.ndv_leading, c.selectivity);

  document.getElementById("out-ndv").textContent = teachRuntime.formatInt(c.ndv_leading);
  document.getElementById("out-rows-per-group").textContent = teachRuntime.formatInt(cost.rowsPerGroup);
  document.getElementById("out-matching-per-group").textContent = teachRuntime.formatInt(cost.matchingPerGroup);
  document.getElementById("out-total-matching").textContent = teachRuntime.formatInt(cost.totalMatching);
  document.getElementById("out-skip-scan-reads").textContent = teachRuntime.formatInt(cost.skipScanReads);
  document.getElementById("out-full-scan-reads").textContent = teachRuntime.formatInt(cost.fullScanReads);
  document.getElementById("out-savings").textContent = teachRuntime.formatInt(cost.savings);
  document.getElementById("out-savings").className = "value " + (cost.savings > 0 ? "ok" : "");

  document.getElementById("out-explanation").textContent =
    "Skip Scan does " + teachRuntime.formatInt(cost.skipScanReads) + " reads (" +
    c.ndv_leading + " groups \u00d7 seek+range) vs " +
    teachRuntime.formatInt(cost.fullScanReads) + " for a full table scan \u2014 " +
    (cost.savings > 0 ? "saving " + teachRuntime.formatInt(cost.savings) + " reads." :
     "no savings (NDV too high or selectivity too low).");

  renderChart(c.table_rows);
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
buildStage();
