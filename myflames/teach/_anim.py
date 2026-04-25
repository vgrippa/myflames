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
  // Returns a callable cancel function that also exposes ``.getElapsed()``
  // so the parent timeline can report precise scrubber positions. Duration
  // is scaled by the global _speed at frame time, so a live speed change
  // takes effect immediately.
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

    var virtualElapsed = 0;  // accumulated virtual time inside this tween
    var lastFrame = null;

    // reduced-motion: skip to end state instantly
    if (reducedMotion()) {
      setTimeout(function() {
        if (cancelled) return;
        virtualElapsed = baseDuration;
        onUpdate(to);
        onComplete();
      }, 0);
      var handle_rm = function() { cancelled = true; };
      handle_rm.getElapsed = function() { return virtualElapsed; };
      return handle_rm;
    }

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

    var handle = function() { cancelled = true; };
    handle.getElapsed = function() { return virtualElapsed; };
    return handle;
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

  // --- timeline: sequential tween queue, pause-aware, scrubbable -------
  //
  // Each `.add(step)`, `.delay(ms)`, `.call(fn)` is assigned a cumulative
  // position on the global timeline (``_start``, ``_duration``). This lets
  // the scrubber seek to any point by calling ``fastForwardTo(ms)``, which
  // synchronously applies every step's final state up to the target.
  //
  // During play a tiny RAF ticker advances ``virtualElapsed`` so external
  // code (the toolbar scrubber) can read ``getCurrentTime()`` every frame.
  // Pauses and speed changes update the ticker exactly like tween() does.
  function _interpolateValue(from, to, eased) {
    if (typeof from === "number") return lerp(from, to, eased);
    var out = {};
    for (var k in from) out[k] = lerp(from[k], to[k], eased);
    return out;
  }

  function timeline() {
    var steps = [];
    var totalDuration = 0;
    var current = null;
    var playing = false;
    var onDoneCb = null;
    var pendingDelayTimer = null;
    var pendingDelayRemaining = 0;
    var marks = [];  // phase markers: [{name, ms}]

    // Virtual-time ticker state. ``virtualElapsed`` is the total ms we have
    // "played" across all steps, pause-adjusted and speed-adjusted. The
    // currently-running tween reports its own elapsed; we add that to
    // ``stepVirtualStart`` to get the precise scrubber position.
    var virtualElapsed = 0;
    var stepVirtualStart = 0;
    var tickerLastFrame = null;

    var tl = {};
    tl.add = function(step) {
      step._type = "tween";
      step._start = totalDuration;
      step._duration = step.duration || 400;
      totalDuration += step._duration;
      steps.push(step);
      return tl;
    };
    tl.delay = function(ms) {
      var s = { _type: "delay", _start: totalDuration, _duration: ms };
      totalDuration += ms;
      steps.push(s);
      return tl;
    };
    tl.call = function(fn) {
      steps.push({ _type: "call", _fn: fn, _start: totalDuration, _duration: 0 });
      return tl;
    };
    tl.mark = function(name) {
      marks.push({ name: name, ms: totalDuration });
      return tl;
    };
    tl.getMarks = function() { return marks; };
    tl.getTotalDuration = function() { return totalDuration; };
    tl.getCurrentTime = function() {
      // If a tween is mid-flight, add its local elapsed to the step start.
      if (current && typeof current.getElapsed === "function") {
        return stepVirtualStart + current.getElapsed();
      }
      return virtualElapsed;
    };

    // Synchronously apply every step's final state up to ``targetMs``.
    // Used by the scrubber to seek to any point in the timeline.
    // Tweens that straddle targetMs get one partial update at their local
    // progress. Leaves ``virtualElapsed = targetMs`` and playing=false.
    tl.fastForwardTo = function(targetMs) {
      tl.stop();
      virtualElapsed = 0;
      stepVirtualStart = 0;
      for (var i = 0; i < steps.length; i++) {
        var s = steps[i];
        var stepEnd = s._start + s._duration;
        if (stepEnd <= targetMs) {
          // Fully elapsed — fire final state
          if (s._type === "tween") {
            if (s.onUpdate) s.onUpdate(s.to);
            if (s.onComplete) s.onComplete();
          } else if (s._type === "call") {
            s._fn();
          }
          virtualElapsed = stepEnd;
        } else if (s._start < targetMs) {
          // Step is currently running at targetMs
          if (s._type === "tween") {
            var progress = (targetMs - s._start) / s._duration;
            var eased = (s.ease || easeOutCubic)(progress);
            if (s.onUpdate) s.onUpdate(_interpolateValue(s.from, s.to, eased));
          } else if (s._type === "call") {
            s._fn();
          }
          virtualElapsed = targetMs;
          stepVirtualStart = s._start;
          return;
        } else {
          virtualElapsed = Math.min(targetMs, s._start);
          return;
        }
      }
      virtualElapsed = targetMs;
    };

    tl.play = function(onDone) {
      playing = true;
      onDoneCb = onDone || null;
      virtualElapsed = 0;
      stepVirtualStart = 0;
      tickerLastFrame = null;
      var i = 0;

      // Scrubber-tracking RAF loop. Runs while playing; updates
      // virtualElapsed so getCurrentTime() reflects the current position
      // even across delay gaps.
      function ticker(now) {
        if (!playing) return;
        if (tickerLastFrame === null) tickerLastFrame = now;
        var delta = now - tickerLastFrame;
        tickerLastFrame = now;
        if (!_paused && !current) {
          // Only advance during delay windows; while a tween is running,
          // getCurrentTime() reads the tween's own elapsed instead.
          virtualElapsed += delta * _speed;
        } else if (!_paused && current && typeof current.getElapsed === "function") {
          // Keep virtualElapsed in sync with the running tween so
          // getCurrentTime() is monotonic even if nothing queries it.
          virtualElapsed = stepVirtualStart + current.getElapsed();
        }
        if (playing) requestAnimationFrame(ticker);
      }
      requestAnimationFrame(ticker);

      function scheduleDelay(remaining, after) {
        if (_paused) {
          pendingDelayRemaining = remaining;
          return;
        }
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
          virtualElapsed = totalDuration;
          if (onDoneCb) onDoneCb();
          return;
        }
        var step = steps[i++];
        stepVirtualStart = step._start;
        if (step._type === "delay") {
          if (reducedMotion()) {
            virtualElapsed = step._start + step._duration;
            next();
            return;
          }
          scheduleDelay(step._duration, function() {
            virtualElapsed = step._start + step._duration;
            next();
          });
          return;
        }
        if (step._type === "call") {
          step._fn();
          virtualElapsed = step._start;
          next();
          return;
        }
        // tween
        var origComplete = step.onComplete || function() {};
        current = tween(Object.assign({}, step, {
          onComplete: function() {
            origComplete();
            current = null;
            virtualElapsed = step._start + step._duration;
            next();
          }
        }));
      }
      var offPause = function(p) {
        if (!p && pendingDelayRemaining > 0 && pendingDelayTimer === null && playing) {
          scheduleDelay(pendingDelayRemaining, next);
        }
      };
      onPauseChange(offPause);
      next();
      return tl;
    };
    // Resume from a specific time. Synchronously applies all steps up to
    // ``fromMs``, then starts live playback from that point. This is how
    // the scrubber implements "Resume from where I dragged to".
    tl.playFrom = function(fromMs, onDone) {
      tl.stop();
      // Apply all steps up to fromMs synchronously
      virtualElapsed = 0;
      stepVirtualStart = 0;
      for (var k = 0; k < steps.length; k++) {
        var s = steps[k];
        var stepEnd = s._start + s._duration;
        if (stepEnd <= fromMs) {
          if (s._type === "tween") {
            if (s.onUpdate) s.onUpdate(s.to);
            if (s.onComplete) s.onComplete();
          } else if (s._type === "call") {
            s._fn();
          }
          virtualElapsed = stepEnd;
        } else {
          break;
        }
      }
      // Now start live playback. The play() method will walk from step 0,
      // but each step whose _start + _duration <= virtualElapsed will
      // complete instantly because virtualElapsed is already past them.
      // We override virtualElapsed inside play() to resume cleanly.
      playing = true;
      onDoneCb = onDone || null;
      tickerLastFrame = null;
      var resumeIdx = 0;
      // Find the first step that hasn't been fully applied yet
      for (var j = 0; j < steps.length; j++) {
        if (steps[j]._start + steps[j]._duration > fromMs) {
          resumeIdx = j;
          break;
        }
        if (j === steps.length - 1) resumeIdx = steps.length;
      }
      virtualElapsed = fromMs;
      stepVirtualStart = (resumeIdx < steps.length) ? steps[resumeIdx]._start : totalDuration;

      function ticker2(now) {
        if (!playing) return;
        if (tickerLastFrame === null) tickerLastFrame = now;
        var delta = now - tickerLastFrame;
        tickerLastFrame = now;
        if (!_paused && !current) {
          virtualElapsed += delta * _speed;
        } else if (!_paused && current && typeof current.getElapsed === "function") {
          virtualElapsed = stepVirtualStart + current.getElapsed();
        }
        if (playing) requestAnimationFrame(ticker2);
      }
      requestAnimationFrame(ticker2);

      function scheduleDelay2(remaining, after) {
        if (_paused) { pendingDelayRemaining = remaining; return; }
        pendingDelayRemaining = remaining;
        pendingDelayTimer = setTimeout(function() {
          pendingDelayTimer = null;
          pendingDelayRemaining = 0;
          after();
        }, remaining / _speed);
      }
      var ri = resumeIdx;
      function next2() {
        if (!playing) return;
        if (ri >= steps.length) {
          playing = false;
          virtualElapsed = totalDuration;
          if (onDoneCb) onDoneCb();
          return;
        }
        var step = steps[ri++];
        stepVirtualStart = step._start;
        if (step._type === "delay") {
          if (reducedMotion()) { virtualElapsed = step._start + step._duration; next2(); return; }
          // If we're resuming mid-delay, shorten it
          var elapsed = fromMs - step._start;
          var remaining = Math.max(0, step._duration - elapsed);
          if (remaining <= 0) { virtualElapsed = step._start + step._duration; next2(); return; }
          scheduleDelay2(remaining, function() {
            virtualElapsed = step._start + step._duration;
            next2();
          });
          return;
        }
        if (step._type === "call") {
          step._fn();
          virtualElapsed = step._start;
          next2();
          return;
        }
        // tween — if we're resuming past this step, skip it
        if (step._start + step._duration <= fromMs) {
          if (step.onUpdate) step.onUpdate(step.to);
          if (step.onComplete) step.onComplete();
          virtualElapsed = step._start + step._duration;
          next2();
          return;
        }
        var origComplete = step.onComplete || function() {};
        current = tween(Object.assign({}, step, {
          onComplete: function() {
            origComplete();
            current = null;
            virtualElapsed = step._start + step._duration;
            next2();
          }
        }));
      }
      var offPause2 = function(p) {
        if (!p && pendingDelayRemaining > 0 && pendingDelayTimer === null && playing) {
          scheduleDelay2(pendingDelayRemaining, next2);
        }
      };
      onPauseChange(offPause2);
      next2();
      return tl;
    };

    tl.stop = function() {
      playing = false;
      if (pendingDelayTimer) { clearTimeout(pendingDelayTimer); pendingDelayTimer = null; }
      pendingDelayRemaining = 0;
      if (current) {
        if (typeof current === "function") current();
        current = null;
      }
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

  // --- arrival: universal "something just landed here" pulse -----------
  // Slice 3 / A5. Call after every tween/timeline step that places a
  // node or cell in its final position. Centralizes the visual
  // vocabulary so every lesson's arrival looks the same — users learn
  // "short ring pulse = new state arrived" once, it applies everywhere.
  //
  //   anim.arrival(rect)                 // default ring pulse
  //   anim.arrival(rect, {peakWidth: 3}) // tuning
  //
  // Respects reducedMotion() by short-circuiting to no-op.
  function arrival(el, opts) {
    if (!el || reducedMotion()) return;
    opts = opts || {};
    pulse(
      el,
      opts.peakWidth || 2.5,
      opts.baseWidth || 1,
      opts.durationMs || 320
    );
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
      // When lowerIsBetter is set, append a hint so readers don't
      // bring the default "higher = better" intuition. User-reported
      // confusion 2026-04-24 — the chart was correct but read wrong.
      var yLabelText = opts.yLabel;
      if (opts.lowerIsBetter) yLabelText += "  (lower = better ↓)";
      var yl = svgEl_create("text", {
        x: 12, y: padT + plotH/2,
        "text-anchor": "middle", "font-size": 11, "font-weight": 600, fill: "#374151",
        transform: "rotate(-90 12," + (padT + plotH/2) + ")"
      });
      yl.textContent = yLabelText;
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
        // Compute each curve's y at the cursor — used both for the
        // dots AND the lower-is-better winner annotation below.
        var perCurve = opts.curves.map(function(c) {
          var cy = c.fn(cx);
          return (isFinite(cy) && cy > 0) ? { c: c, cy: cy } : null;
        }).filter(function(e) { return e !== null; });

        perCurve.forEach(function(e) {
          var svg = toSvg(cx, e.cy);
          var dot = svgEl_create("circle", {
            cx: svg.x, cy: svg.y, r: 5,
            fill: e.c.color || "#1f2937", stroke: "#ffffff", "stroke-width": 2
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

        // Winner annotation: when lowerIsBetter is set, mark the
        // lowest curve at the cursor with a green ✓, and (when there
        // are exactly 2 curves) draw a "N× cheaper" callout between
        // their dots so the visual win is unambiguous. Without this
        // the reader has to mentally translate "low number = good".
        if (opts.lowerIsBetter && perCurve.length >= 2) {
          var sorted = perCurve.slice().sort(function(a, b) { return a.cy - b.cy; });
          var winner = sorted[0];
          var loser = sorted[sorted.length - 1];
          var winSvg = toSvg(cx, winner.cy);
          var loseSvg = toSvg(cx, loser.cy);

          // Green check next to the winning dot.
          var check = svgEl_create("text", {
            x: winSvg.x + 9, y: winSvg.y + 4,
            "font-size": 14, "font-weight": 800,
            fill: "#16a34a"
          });
          check.textContent = "✓";
          svgEl.appendChild(check);

          // Speedup label between the two dots, mid-y.
          var ratio = loser.cy / winner.cy;
          if (isFinite(ratio) && ratio >= 1.2) {
            var ratioStr;
            if (ratio >= 1000) ratioStr = (ratio / 1000).toFixed(1) + "k×";
            else if (ratio >= 100) ratioStr = ratio.toFixed(0) + "×";
            else ratioStr = ratio.toFixed(1) + "×";
            var midY = (winSvg.y + loseSvg.y) / 2;
            var labelText = (winner.c.label || "winner").split(":")[0]
                          + " is " + ratioStr + " cheaper";
            // Background pill so the callout reads cleanly across
            // the chart grid.
            var approxW = labelText.length * 6.2 + 16;
            var pillX = winSvg.x + 14;
            var pillY = midY - 9;
            var pillBg = svgEl_create("rect", {
              x: pillX, y: pillY, width: approxW, height: 18,
              rx: 9, ry: 9,
              fill: "rgba(22,163,74,0.12)",
              stroke: "#16a34a", "stroke-width": 1
            });
            svgEl.appendChild(pillBg);
            var pillTxt = svgEl_create("text", {
              x: pillX + 8, y: midY + 3,
              "font-size": 10.5, "font-weight": 700, fill: "#15803d"
            });
            pillTxt.textContent = labelText;
            svgEl.appendChild(pillTxt);
          }
        }
      }
    }

    // ------------- Interactivity: hover guide + tooltip + click ----------
    // A transparent rect over the plot area captures mouse moves and
    // clicks. On hover, a vertical guide tracks the pointer and each curve
    // gets a small dot + label showing its value at that x.
    var hitRect = svgEl_create("rect", {
      x: padL, y: padT, width: plotW, height: plotH,
      fill: "transparent", "pointer-events": "all",
      style: "cursor: crosshair"
    });
    svgEl.appendChild(hitRect);

    var hoverGroup = svgEl_create("g", { "pointer-events": "none", opacity: 0 });
    var hoverLine = svgEl_create("line", {
      y1: padT, y2: padT + plotH,
      stroke: "#1f2937", "stroke-width": 1, "stroke-dasharray": "2 2"
    });
    hoverGroup.appendChild(hoverLine);
    var hoverDots = [];
    var hoverLabelBg = svgEl_create("rect", {
      rx: 4, ry: 4, fill: "rgba(31,41,55,0.95)", stroke: "none"
    });
    hoverGroup.appendChild(hoverLabelBg);
    var hoverLabelLines = [];
    opts.curves.forEach(function(c, idx) {
      var dot = svgEl_create("circle", {
        r: 4, fill: c.color || "#1f2937", stroke: "#fff", "stroke-width": 1.5
      });
      hoverGroup.appendChild(dot);
      hoverDots.push(dot);
      var txt = svgEl_create("text", {
        "font-size": 10, "font-weight": 600, fill: "#ffffff"
      });
      hoverGroup.appendChild(txt);
      hoverLabelLines.push(txt);
    });
    svgEl.appendChild(hoverGroup);

    function svgPointFromEvent(evt) {
      var ctm = svgEl.getScreenCTM();
      if (!ctm) return { x: evt.clientX, y: evt.clientY };
      try {
        var pt = svgEl.createSVGPoint();
        pt.x = evt.clientX; pt.y = evt.clientY;
        var inv = ctm.inverse();
        return pt.matrixTransform(inv);
      } catch (e) {
        return { x: evt.clientX, y: evt.clientY };
      }
    }
    function formatBig(n) {
      if (!isFinite(n)) return "—";
      if (n >= 1e12) return (n / 1e12).toFixed(2) + "T";
      if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
      if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
      if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
      return Math.round(n).toString();
    }
    function updateHover(svgX) {
      var clamped = Math.max(padL, Math.min(padL + plotW, svgX));
      var xFrac = (clamped - padL) / plotW;
      var xVal = Math.exp(Math.log(xMin) + xFrac * (Math.log(xMax) - Math.log(xMin)));
      hoverLine.setAttribute("x1", clamped);
      hoverLine.setAttribute("x2", clamped);

      var labelLines = [];
      labelLines.push(opts.xLabel + ": " + formatBig(xVal));
      opts.curves.forEach(function(c, idx) {
        var y = c.fn(xVal);
        if (!isFinite(y) || y <= 0) {
          hoverDots[idx].setAttribute("opacity", 0);
          hoverLabelLines[idx + 1] && hoverLabelLines[idx + 1].setAttribute("opacity", 0);
          return;
        }
        var svgPt = toSvg(xVal, y);
        hoverDots[idx].setAttribute("cx", svgPt.x);
        hoverDots[idx].setAttribute("cy", svgPt.y);
        hoverDots[idx].setAttribute("opacity", 1);
        labelLines.push(c.label + ": " + formatBig(y));
      });

      // Compose tooltip on the left or right depending on cursor position
      var labelX = (clamped < padL + plotW / 2) ? clamped + 10 : clamped - 150;
      var labelY = padT + 6;
      var maxLineW = 0;
      for (var j = 0; j < labelLines.length; j++) {
        if (!hoverLabelLines[j]) {
          var t = svgEl_create("text", { "font-size": 10, "font-weight": 600, fill: "#ffffff" });
          hoverGroup.appendChild(t);
          hoverLabelLines[j] = t;
        }
        hoverLabelLines[j].textContent = labelLines[j];
        hoverLabelLines[j].setAttribute("x", labelX + 6);
        hoverLabelLines[j].setAttribute("y", labelY + 12 + j * 13);
        hoverLabelLines[j].setAttribute("opacity", 1);
        maxLineW = Math.max(maxLineW, labelLines[j].length * 5.8);
      }
      // Clear any extra stale labels
      for (var k = labelLines.length; k < hoverLabelLines.length; k++) {
        if (hoverLabelLines[k]) hoverLabelLines[k].setAttribute("opacity", 0);
      }
      hoverLabelBg.setAttribute("x", labelX);
      hoverLabelBg.setAttribute("y", labelY);
      hoverLabelBg.setAttribute("width", Math.max(140, maxLineW + 12));
      hoverLabelBg.setAttribute("height", 8 + labelLines.length * 13);
      hoverGroup.setAttribute("opacity", 1);
    }

    hitRect.addEventListener("mousemove", function(evt) {
      var pt = svgPointFromEvent(evt);
      updateHover(pt.x);
    });
    hitRect.addEventListener("mouseleave", function() {
      hoverGroup.setAttribute("opacity", 0);
    });

    // Click to update a slider — opts.xSlider = "rows" to bind.
    if (opts.xSlider) {
      hitRect.addEventListener("click", function(evt) {
        var pt = svgPointFromEvent(evt);
        var clamped = Math.max(padL, Math.min(padL + plotW, pt.x));
        var xFrac = (clamped - padL) / plotW;
        var xVal = Math.exp(Math.log(xMin) + xFrac * (Math.log(xMax) - Math.log(xMin)));
        var slider = document.getElementById(opts.xSlider);
        if (!slider) return;
        // Some sliders use linear values (outer_rows), some use a log index
        // mapped from ROW_SCALE. The caller can provide a transform.
        var newVal;
        if (opts.xSliderTransform) {
          newVal = opts.xSliderTransform(xVal);
        } else {
          newVal = Math.round(xVal);
        }
        slider.value = newVal;
        // Fire input event so the lesson's recompute runs
        var ev = new Event("input", { bubbles: true });
        slider.dispatchEvent(ev);
      });
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
    arrival: arrival,
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
