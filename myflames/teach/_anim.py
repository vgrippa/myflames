"""Shared JS animation runtime for `teach` lessons.

Exports a single string constant ``ANIM_JS`` that every lesson embeds in its
``<script>`` block. Provides a vanilla-JS tween / easing / timeline library
that respects ``prefers-reduced-motion`` and works completely offline. See
``.claude/skills/animation-expert/SKILL.md`` for the craft rules this
runtime enforces.

The runtime lives here (not inline in every lesson) so that:

* Easing math is defined once. Lessons can't drift apart.
* A future upgrade (say, adding spring physics) is a one-file change.
* The regression tests assert every lesson contains the same shared
  library, which catches accidental copy-paste drift.
"""
from __future__ import annotations

# Raw JavaScript — kept as a triple-quoted string so the whole runtime can
# be grepped, node-parsed, and diffed as one unit.
ANIM_JS = r"""
// ---------------------------------------------------------------------------
// myflames teach animation runtime — vanilla JS, no deps, offline-first.
// See .claude/skills/animation-expert/SKILL.md for craft rules.
// ---------------------------------------------------------------------------
var anim = (function() {
  // --- Easing functions --------------------------------------------------
  function linear(t) { return t; }
  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }
  function easeInCubic(t) { return t * t * t; }
  function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }
  function easeOutQuart(t) { return 1 - Math.pow(1 - t, 4); }
  function easeInOutQuad(t) {
    return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
  }
  function easeOutBack(t) {
    var c1 = 1.70158, c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
  }

  // --- Interpolation -----------------------------------------------------
  function lerp(a, b, t) { return a + (b - a) * t; }

  function _parseHex(h) {
    return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  }
  function _toHex(c) {
    var h = Math.round(c).toString(16);
    return h.length < 2 ? "0" + h : h;
  }
  function lerpColor(a, b, t) {
    var ca = _parseHex(a), cb = _parseHex(b);
    return "#" + _toHex(lerp(ca[0], cb[0], t)) + _toHex(lerp(ca[1], cb[1], t)) + _toHex(lerp(ca[2], cb[2], t));
  }

  // --- prefers-reduced-motion -------------------------------------------
  function reducedMotion() {
    try {
      return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) { return false; }
  }

  // --- Global speed multiplier (1 = real time, 0.5 = half speed, 2 = 2×) -
  var _speed = 1;
  function setSpeed(s) { _speed = Math.max(0.1, s); }
  function getSpeed() { return _speed; }

  // --- Global pause flag --------------------------------------------------
  // When paused, every running tween suspends at its current progress and
  // resumes from the same progress when unpaused.
  var _paused = false;
  var _onPauseChange = [];
  function setPaused(p) {
    if (_paused === p) return;
    _paused = p;
    _onPauseChange.forEach(function(fn) { try { fn(p); } catch (e) {} });
  }
  function isPaused() { return _paused; }
  function onPauseChange(fn) { _onPauseChange.push(fn); }

  // --- tween: single-value interpolation with RAF, pause-aware ----------
  // opts: { from, to, duration, ease, onUpdate, onComplete, delay }
  // Returns a cancel function. Duration is scaled by the global _speed
  // at frame time, so a live speed change takes effect immediately.
  function tween(opts) {
    var from = opts.from;
    var to = opts.to;
    var baseDuration = opts.duration || 400;
    var ease = opts.ease || easeOutCubic;
    var onUpdate = opts.onUpdate || function() {};
    var onComplete = opts.onComplete || function() {};
    var delay = opts.delay || 0;
    var cancelled = false;

    function interpolate(f, t, eased) {
      if (typeof f === "number") return lerp(f, t, eased);
      var out = {};
      for (var k in f) out[k] = lerp(f[k], t[k], eased);
      return out;
    }

    // reduced-motion: skip to end state instantly
    if (reducedMotion()) {
      setTimeout(function() {
        if (cancelled) return;
        onUpdate(to);
        onComplete();
      }, 0);
      return function() { cancelled = true; };
    }

    // Time-based progress tracking. We accumulate "virtual elapsed time"
    // across frames, scaled by _speed and paused by _paused. This lets us
    // change speed or pause mid-flight without discontinuities.
    var lastFrame = null;
    var virtualElapsed = 0;  // in real ms (base duration units)

    function frame(now) {
      if (cancelled) return;
      if (lastFrame === null) lastFrame = now;
      var delta = now - lastFrame;
      lastFrame = now;
      if (!_paused) {
        virtualElapsed += delta * _speed;
      }
      var adjusted = virtualElapsed - delay;
      if (adjusted < 0) { requestAnimationFrame(frame); return; }
      var t = Math.min(1, adjusted / baseDuration);
      var eased = ease(t);
      onUpdate(interpolate(from, to, eased));
      if (t < 1) requestAnimationFrame(frame);
      else onComplete();
    }
    requestAnimationFrame(frame);
    return function() { cancelled = true; };
  }

  // --- stagger: run fn(item, i) at i*stepMs intervals -------------------
  function stagger(items, stepMs, fn) {
    if (reducedMotion()) {
      for (var i = 0; i < items.length; i++) fn(items[i], i);
      return function() {};
    }
    var timers = [];
    items.forEach(function(item, i) {
      timers.push(setTimeout(function() { fn(item, i); }, i * stepMs));
    });
    return function() { timers.forEach(clearTimeout); };
  }

  // --- timeline: sequential tween queue, pause-aware --------------------
  function timeline() {
    var steps = [];
    var current = null;
    var playing = false;
    var onDoneCb = null;
    var pendingDelayTimer = null;
    var pendingDelayRemaining = 0;
    var pendingDelayStart = 0;
    var tl = {};
    tl.add = function(step) { steps.push(step); return tl; };
    tl.delay = function(ms) { steps.push({ _delay: ms }); return tl; };
    tl.call = function(fn) { steps.push({ _call: fn }); return tl; };
    tl.play = function(onDone) {
      playing = true;
      onDoneCb = onDone || null;
      var i = 0;
      function scheduleDelay(remaining, after) {
        if (_paused) {
          // When paused, record that we were in a delay with this much left,
          // and wait until unpause to re-schedule.
          pendingDelayRemaining = remaining;
          return;
        }
        pendingDelayStart = performance.now();
        pendingDelayRemaining = remaining;
        pendingDelayTimer = setTimeout(function() {
          pendingDelayTimer = null;
          pendingDelayRemaining = 0;
          after();
        }, remaining / _speed);
      }
      function next() {
        if (!playing) return;
        if (i >= steps.length) {
          playing = false;
          if (onDoneCb) onDoneCb();
          return;
        }
        var step = steps[i++];
        if (step._delay !== undefined) {
          if (reducedMotion()) { next(); return; }
          scheduleDelay(step._delay, next);
          return;
        }
        if (step._call !== undefined) {
          step._call();
          next();
          return;
        }
        var origComplete = step.onComplete || function() {};
        current = tween(Object.assign({}, step, {
          onComplete: function() { origComplete(); next(); }
        }));
      }
      // When the global pause flag flips back to false while we're sitting
      // in a delay, re-schedule the delay with the remaining time.
      var offPause = function(p) {
        if (!p && pendingDelayRemaining > 0 && pendingDelayTimer === null && playing) {
          scheduleDelay(pendingDelayRemaining, next);
        }
      };
      onPauseChange(offPause);
      next();
      return tl;
    };
    tl.stop = function() {
      playing = false;
      if (pendingDelayTimer) { clearTimeout(pendingDelayTimer); pendingDelayTimer = null; }
      pendingDelayRemaining = 0;
      if (current) current();
    };
    tl.isPlaying = function() { return playing; };
    return tl;
  }

  // --- path: quadratic-bezier sampler (x1,y1) → (cx,cy) → (x2,y2) -------
  function path(x1, y1, cx, cy, x2, y2) {
    return function(t) {
      var omt = 1 - t;
      var x = omt * omt * x1 + 2 * omt * t * cx + t * t * x2;
      var y = omt * omt * y1 + 2 * omt * t * cy + t * t * y2;
      return { x: x, y: y };
    };
  }

  // --- pulse: briefly grow an SVG element's stroke width ---------------
  function pulse(el, peakWidth, baseWidth, durationMs) {
    if (!el) return;
    var dur = (durationMs || 400) / 2;
    tween({
      from: baseWidth || 1,
      to: peakWidth || 3,
      duration: dur,
      ease: easeOutCubic,
      onUpdate: function(v) { el.setAttribute("stroke-width", v.toFixed(2)); },
      onComplete: function() {
        tween({
          from: peakWidth || 3,
          to: baseWidth || 1,
          duration: dur,
          ease: easeInCubic,
          onUpdate: function(v) { el.setAttribute("stroke-width", v.toFixed(2)); }
        });
      }
    });
  }

  // --- svg element creators (tiny helpers to reduce boilerplate) -------
  function svgEl(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    if (attrs) for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  // --- complexityChart: small log-log plot of cost vs input size -------
  //
  // Draws a line-chart SVG showing how a cost function scales with input
  // size, with the current operating point highlighted. Useful for the
  // "feel the asymptotic gap" moment at the bottom of each lesson.
  //
  // opts: {
  //   svgId: "complexity-chart",
  //   width: 400, height: 180,
  //   xLabel: "Rows", yLabel: "Pages touched",
  //   curves: [
  //     {label: "BNL", color: "#ca8a04", fn: function(n) { return n * n; }},
  //     {label: "Hash", color: "#0d9488", fn: function(n) { return n + 1000; }}
  //   ],
  //   xMin: 10, xMax: 1000000,
  //   current: {x: 100000},  // highlight point
  // }
  function complexityChart(opts) {
    var svgEl = document.getElementById(opts.svgId);
    if (!svgEl) return;
    while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
    var W = opts.width || 400;
    var H = opts.height || 180;
    var padL = 44, padR = 20, padT = 20, padB = 32;
    var plotW = W - padL - padR;
    var plotH = H - padT - padB;
    var xMin = opts.xMin || 1;
    var xMax = opts.xMax || 1e9;
    var NSAMP = 80;

    // Sample every curve; use log X scale
    function logMap(v, lo, hi) {
      var lv = Math.log(v);
      var llo = Math.log(lo);
      var lhi = Math.log(hi);
      return (lv - llo) / (lhi - llo);
    }
    var samples = opts.curves.map(function(c) {
      var pts = [];
      for (var i = 0; i < NSAMP; i++) {
        var frac = i / (NSAMP - 1);
        var x = Math.exp(Math.log(xMin) + frac * (Math.log(xMax) - Math.log(xMin)));
        pts.push({x: x, y: c.fn(x)});
      }
      return pts;
    });
    // Compute global Y range (log scale)
    var yAll = [];
    samples.forEach(function(curve) { curve.forEach(function(p) { if (p.y > 0 && isFinite(p.y)) yAll.push(p.y); }); });
    if (yAll.length === 0) return;
    var yMin = Math.min.apply(null, yAll);
    var yMax = Math.max.apply(null, yAll);
    // Clamp yMin so it's not zero
    if (yMin <= 0) yMin = 1;

    function toSvg(x, y) {
      var fx = logMap(x, xMin, xMax);
      var fy = (y > 0) ? logMap(y, yMin, yMax) : 0;
      return {
        x: padL + fx * plotW,
        y: padT + (1 - fy) * plotH
      };
    }

    // Background grid (log decades)
    for (var gx = Math.pow(10, Math.floor(Math.log10(xMin))); gx <= xMax; gx *= 10) {
      if (gx < xMin) continue;
      var gridX = toSvg(gx, yMin).x;
      var gline = svgEl_create("line", {
        x1: gridX, y1: padT, x2: gridX, y2: padT + plotH,
        stroke: "#e5e7eb", "stroke-width": 1
      });
      svgEl.appendChild(gline);
      var glbl = svgEl_create("text", {
        x: gridX, y: padT + plotH + 14,
        "text-anchor": "middle", "font-size": 9, fill: "#9ca3af"
      });
      glbl.textContent = formatLog(gx);
      svgEl.appendChild(glbl);
    }
    for (var gy = Math.pow(10, Math.floor(Math.log10(yMin))); gy <= yMax * 10; gy *= 10) {
      if (gy < yMin) continue;
      var gridY = toSvg(xMin, gy).y;
      if (gridY < padT - 1 || gridY > padT + plotH + 1) continue;
      var gln = svgEl_create("line", {
        x1: padL, y1: gridY, x2: padL + plotW, y2: gridY,
        stroke: "#e5e7eb", "stroke-width": 1
      });
      svgEl.appendChild(gln);
      var glb = svgEl_create("text", {
        x: padL - 6, y: gridY + 3,
        "text-anchor": "end", "font-size": 9, fill: "#9ca3af"
      });
      glb.textContent = formatLog(gy);
      svgEl.appendChild(glb);
    }

    // Axis lines
    var axisX = svgEl_create("line", {
      x1: padL, y1: padT + plotH, x2: padL + plotW, y2: padT + plotH,
      stroke: "#6b7280", "stroke-width": 1.5
    });
    svgEl.appendChild(axisX);
    var axisY = svgEl_create("line", {
      x1: padL, y1: padT, x2: padL, y2: padT + plotH,
      stroke: "#6b7280", "stroke-width": 1.5
    });
    svgEl.appendChild(axisY);

    // Axis labels
    if (opts.xLabel) {
      var xl = svgEl_create("text", {
        x: padL + plotW/2, y: H - 6,
        "text-anchor": "middle", "font-size": 11, "font-weight": 600, fill: "#374151"
      });
      xl.textContent = opts.xLabel;
      svgEl.appendChild(xl);
    }
    if (opts.yLabel) {
      var yl = svgEl_create("text", {
        x: 12, y: padT + plotH/2,
        "text-anchor": "middle", "font-size": 11, "font-weight": 600, fill: "#374151",
        transform: "rotate(-90 12," + (padT + plotH/2) + ")"
      });
      yl.textContent = opts.yLabel;
      svgEl.appendChild(yl);
    }

    // Curves
    opts.curves.forEach(function(c, idx) {
      var pts = samples[idx];
      var d = "";
      pts.forEach(function(p, i) {
        if (!isFinite(p.y) || p.y <= 0) return;
        var svg = toSvg(p.x, p.y);
        d += (i === 0 ? "M " : " L ") + svg.x.toFixed(1) + "," + svg.y.toFixed(1);
      });
      var path = svgEl_create("path", {
        d: d, fill: "none", stroke: c.color || "#1f2937", "stroke-width": 2.5,
        "stroke-linecap": "round", "stroke-linejoin": "round"
      });
      svgEl.appendChild(path);
    });

    // Legend
    var legendY = padT - 6;
    var lx = padL;
    opts.curves.forEach(function(c, idx) {
      var sw = svgEl_create("rect", {
        x: lx, y: legendY - 8, width: 12, height: 3, rx: 1, fill: c.color || "#1f2937"
      });
      svgEl.appendChild(sw);
      var lt = svgEl_create("text", {
        x: lx + 16, y: legendY - 4, "font-size": 10, "font-weight": 600, fill: c.color || "#1f2937"
      });
      lt.textContent = c.label;
      svgEl.appendChild(lt);
      lx += (c.label.length * 6) + 34;
    });

    // Highlight current operating point
    if (opts.current && opts.current.x) {
      var cx = opts.current.x;
      if (cx >= xMin && cx <= xMax) {
        opts.curves.forEach(function(c, idx) {
          var cy = c.fn(cx);
          if (!isFinite(cy) || cy <= 0) return;
          var svg = toSvg(cx, cy);
          var dot = svgEl_create("circle", {
            cx: svg.x, cy: svg.y, r: 5,
            fill: c.color || "#1f2937", stroke: "#ffffff", "stroke-width": 2
          });
          svgEl.appendChild(dot);
        });
        // Vertical guide line at the current x
        var vx = toSvg(cx, yMin).x;
        var vline = svgEl_create("line", {
          x1: vx, y1: padT, x2: vx, y2: padT + plotH,
          stroke: "#ff3d3d", "stroke-width": 1, "stroke-dasharray": "3 3"
        });
        svgEl.appendChild(vline);
      }
    }
  }

  function svgEl_create(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    if (attrs) for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function formatLog(n) {
    if (n >= 1e9) return (n / 1e9) + "B";
    if (n >= 1e6) return (n / 1e6) + "M";
    if (n >= 1e3) return (n / 1e3) + "K";
    return String(n);
  }

  return {
    linear: linear,
    easeOutCubic: easeOutCubic,
    easeInCubic: easeInCubic,
    easeInOutCubic: easeInOutCubic,
    easeOutQuart: easeOutQuart,
    easeInOutQuad: easeInOutQuad,
    easeOutBack: easeOutBack,
    lerp: lerp,
    lerpColor: lerpColor,
    tween: tween,
    stagger: stagger,
    timeline: timeline,
    path: path,
    pulse: pulse,
    svgEl: svgEl,
    reducedMotion: reducedMotion,
    setSpeed: setSpeed,
    getSpeed: getSpeed,
    setPaused: setPaused,
    isPaused: isPaused,
    onPauseChange: onPauseChange,
    complexityChart: complexityChart
  };
})();
"""
