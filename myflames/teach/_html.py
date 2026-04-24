"""Shared HTML chrome for `myflames teach` lessons.

Emits a complete offline HTML5 document with:

* system font stack (matches output_diagram.py)
* controls section (sliders / number inputs / dropdowns)
* stage section (SVG + optional tuple overlay)
* cost-readout section (live numeric block)
* learn-more collapsible `<details>` block

No external dependencies. No ``<script src=``, no ``<link href=`` except
``data:`` URIs. Offline-first and accessible (ARIA labels, keyboard
navigation, ``prefers-reduced-motion`` fallback).
"""
from __future__ import annotations

import html
from typing import Iterable

from ._anim import ANIM_JS


# System font stack — same one used by myflames/output_diagram.py.
_FONT_STACK = (
    '"Google Sans", Roboto, "Noto Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", '
    '"Helvetica Neue", Arial, sans-serif'
)

# Base CSS shared by every lesson.
_BASE_CSS = """
:root {
  --bg: #f6f8fc;
  --panel: #ffffff;
  --panel-soft: #f8faff;
  --text: #111827;
  --muted: #637083;
  --border: #dbe4f0;
  --accent: #1a73e8;
  --accent-strong: #1557b0;
  --focus: #8ab4f8;
  --elev-1: 0 1px 2px rgba(15, 23, 42, 0.06), 0 1px 1px rgba(15, 23, 42, 0.04);
  --elev-2: 0 6px 18px rgba(15, 23, 42, 0.08), 0 2px 6px rgba(15, 23, 42, 0.06);
  --hot: #ff3d3d;
  --cool: #21918c;
  --warm: #fde725;
  --warn: #f59e0b;
  --ok: #10b981;
}
/* Lesson-family color grading (shared semantics, different accent ramps). */
body[data-lesson="bka"],
body[data-lesson="bnl"],
body[data-lesson="hash"],
body[data-lesson="join"],
body[data-lesson="nested_loop"],
body[data-lesson="semijoin_weedout"] {
  --accent: #1a73e8;
  --accent-strong: #1557b0;
  --focus: #8ab4f8;
}
body[data-lesson="full_scan"],
body[data-lesson="filter"],
body[data-lesson="filesort"],
body[data-lesson="tmp"],
body[data-lesson="derived_table"] {
  --accent: #ea580c;
  --accent-strong: #c2410c;
  --focus: #fdba74;
}
body[data-lesson="btree"],
body[data-lesson="non_unique_lookup"],
body[data-lesson="unique_lookup"],
body[data-lesson="icp"],
body[data-lesson="index_merge"],
body[data-lesson="skip_scan"] {
  --accent: #0f766e;
  --accent-strong: #0f5f59;
  --focus: #5eead4;
}
body[data-lesson="lru"] {
  --accent: #7c3aed;
  --accent-strong: #6d28d9;
  --focus: #c4b5fd;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: __FONT_STACK__;
  -webkit-font-smoothing: antialiased;
  line-height: 1.5;
}
main { max-width: 1100px; margin: 0 auto; padding: 24px 20px 48px; }
header {
  background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
  border-bottom: 1px solid #e7eef8;
  padding: 20px;
  box-shadow: var(--elev-1);
}
header .inner { max-width: 1100px; margin: 0 auto; }
header h1 {
  margin: 0 0 6px;
  font-size: 23px;
  letter-spacing: -0.2px;
  font-weight: 700;
}
header .subtitle {
  color: var(--muted);
  font-size: 14px;
  margin: 0;
}
header .lesson-meta {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
header .meta-label {
  font-size: 11px;
  color: var(--muted);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.35px;
}
header .family-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 600;
  background: color-mix(in srgb, var(--accent) 13%, #ffffff);
  color: var(--accent-strong);
  border: 1px solid color-mix(in srgb, var(--accent) 35%, #ffffff);
}
header .family-chip .chip-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}
header .version-chip {
  display: inline-block;
  margin-left: 8px;
  padding: 3px 10px;
  background: #e8f0fe;
  color: #174ea6;
  border: 1px solid #c6dafc;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 600;
  vertical-align: 2px;
}
.banner {
  margin: 0 0 20px;
  padding: 12px 16px;
  border-left: 4px solid var(--warn);
  background: #fffbeb;
  color: #78350f;
  border-radius: 4px;
  font-size: 13.5px;
}
.banner strong { color: #92400e; }
section.controls {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px 20px;
  margin-bottom: 20px;
  box-shadow: var(--elev-1);
}
section.controls h2 { margin: 0 0 12px; font-size: 15px; font-weight: 600; }
.control-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px 20px;
}
.control {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.control label {
  font-size: 12px;
  font-weight: 600;
  color: #374151;
}
.control .hint {
  font-size: 11px;
  color: var(--muted);
}
.control input[type="range"] { width: 100%; }
.control input[type="number"],
.control select {
  padding: 8px 10px;
  border: 1px solid #cfd8e5;
  border-radius: 10px;
  font: inherit;
  font-size: 13px;
  background: #fff;
}
.control input[type="number"]:focus,
.control select:focus,
.control input[type="range"]:focus {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
.value-pill {
  display: inline-block;
  padding: 2px 9px;
  background: #eef4ff;
  border-radius: 999px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  font-size: 12px;
  color: #1e3a8a;
}
section.stage {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 20px;
  box-shadow: var(--elev-1);
}
.query-card {
  margin-bottom: 14px;
  padding: 12px 14px;
  background: #f8fbff;
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: 8px;
}
.query-card .query-label {
  margin: 0 0 6px;
  font-size: 10.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--muted);
}
.query-card .query-sql {
  margin: 0;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 12.5px;
  line-height: 1.5;
  color: #0f172a;
  white-space: pre-wrap;
}
.query-card .query-note {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--muted);
  font-style: italic;
}
.explainer-card {
  margin-bottom: 14px;
  padding: 12px 16px;
  background: #ecfdf5;
  border: 1px solid #a7f3d0;
  border-left: 4px solid #059669;
  border-radius: 8px;
}
.explainer-card .explainer-title {
  margin: 0 0 6px;
  font-size: 11.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: #065f46;
}
.explainer-card .explainer-list {
  margin: 0;
  padding-left: 20px;
  font-size: 12.5px;
  color: #064e3b;
  line-height: 1.55;
}
.explainer-card .explainer-list li {
  margin-bottom: 4px;
}
.explainer-card .explainer-list li:last-child {
  margin-bottom: 0;
}
section.stage .stage-toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 10px;
  padding: 10px 12px;
  background: var(--panel-soft);
  border: 1px solid #dbe6f3;
  border-radius: 12px;
  font-size: 13px;
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
}
section.stage .stage-toolbar:hover {
  box-shadow: var(--elev-1);
  border-color: #cddbef;
}
.stage-toolbar .speed-label {
  font-size: 11.5px;
  font-weight: 600;
  color: #374151;
  margin-left: 2px;
  letter-spacing: 0.2px;
}
.stage-toolbar select {
  padding: 6px 10px;
  border: 1px solid #cfd8e5;
  border-radius: 10px;
  font: inherit;
  font-size: 12.5px;
  background: #fff;
  cursor: pointer;
}
.stage-toolbar select:focus {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
.stage-toolbar .phase-label {
  margin-left: auto;
  font-size: 12px;
  color: var(--muted);
  font-weight: 600;
}
.stage-scrubber {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding: 8px 10px;
  background: var(--panel-soft);
  border: 1px solid #dbe6f3;
  border-radius: 12px;
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
}
.stage-scrubber:hover {
  box-shadow: var(--elev-1);
  border-color: #cddbef;
}
.stage-scrubber input[type="range"] {
  flex: 1;
  height: 6px;
  border-radius: 999px;
  background: linear-gradient(90deg, #c8ddff 0%, #dbeafe 100%);
  accent-color: var(--accent);
  cursor: pointer;
}
.stage-scrubber input[type="range"]:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.stage-scrubber input[type="range"]::-webkit-slider-runnable-track {
  height: 6px;
  border-radius: 999px;
  background: linear-gradient(90deg, #bed8ff 0%, #dbeafe 100%);
}
.stage-scrubber input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  margin-top: -5px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid #ffffff;
  background: var(--accent);
  box-shadow: 0 1px 5px color-mix(in srgb, var(--accent) 45%, transparent);
  transition: transform 0.14s ease, box-shadow 0.14s ease;
}
.stage-scrubber input[type="range"]::-webkit-slider-thumb:hover {
  transform: scale(1.06);
  box-shadow: 0 2px 8px color-mix(in srgb, var(--accent) 55%, transparent);
}
.stage-scrubber input[type="range"]::-moz-range-track {
  height: 6px;
  border-radius: 999px;
  background: linear-gradient(90deg, #bed8ff 0%, #dbeafe 100%);
}
.stage-scrubber input[type="range"]::-moz-range-thumb {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid #ffffff;
  background: var(--accent);
  box-shadow: 0 1px 5px color-mix(in srgb, var(--accent) 45%, transparent);
  transition: transform 0.14s ease, box-shadow 0.14s ease;
}
.stage-scrubber input[type="range"]::-moz-range-thumb:hover {
  transform: scale(1.06);
  box-shadow: 0 2px 8px color-mix(in srgb, var(--accent) 55%, transparent);
}
.stage-scrubber .scrubber-time {
  font-family: "SFMono-Regular", Consolas, Menlo, monospace;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  min-width: 42px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.complexity-chart {
  margin-top: 14px;
  padding: 14px;
  background: #f9fafb;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.complexity-chart .chart-title {
  margin: 0 0 6px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--muted);
}
.complexity-chart svg {
  width: 100%;
  max-width: 600px;
  height: auto;
}
button {
  position: relative;
  overflow: hidden;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border: 1px solid #cfd8e5;
  border-radius: 999px;
  background: #fff;
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;
}
button:hover { background: #edf3ff; border-color: #9eb6d8; }
button:active { transform: translateY(1px) scale(0.99); }
button:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
button.primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent-strong);
  box-shadow: 0 1px 0 rgba(255,255,255,0.25) inset;
}
button.primary:hover { background: var(--accent-strong); border-color: #144a96; }
button .btn-icon {
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
button .btn-icon svg {
  width: 16px;
  height: 16px;
  fill: currentColor;
  transition: transform 0.16s ease;
}
button .btn-label {
  line-height: 1;
  white-space: nowrap;
}
button[aria-pressed="true"] {
  background: color-mix(in srgb, var(--accent) 14%, #ffffff);
  color: var(--accent-strong);
  border-color: color-mix(in srgb, var(--accent) 35%, #ffffff);
}
button#btn-reset {
  background: #ffffff;
}
button#btn-reset:hover {
  background: #eef2ff;
}
button#btn-play:hover .btn-icon svg {
  transform: translateX(0.5px);
}
button#btn-loop[aria-pressed="true"] .btn-icon svg {
  transform: rotate(12deg);
}
section.stage svg { width: 100%; height: auto; display: block; }
section.readout {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px 20px;
  margin-bottom: 20px;
  box-shadow: var(--elev-1);
}
section.readout h2 { margin: 0 0 12px; font-size: 15px; font-weight: 600; }
.readout-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px 18px;
}
.readout-grid .item {
  padding: 12px 14px;
  background: #f8fbff;
  border-radius: 10px;
  border: 1px solid var(--border);
}
.readout-grid .label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: var(--muted);
  margin: 0 0 4px;
  font-weight: 600;
}
.readout-grid .label .help-tip {
  display: inline-block;
  cursor: help;
  font-size: 10px;
  font-weight: 700;
  color: #9ca3af;
  border: 1px solid #d1d5db;
  border-radius: 50%;
  width: 15px;
  height: 15px;
  line-height: 14px;
  text-align: center;
  margin-left: 4px;
  vertical-align: 1px;
  text-transform: none;
  letter-spacing: 0;
  position: relative;
}
.readout-grid .label .help-tip:hover {
  color: #374151;
  border-color: #9ca3af;
}
.readout-grid .label .help-tip .tip-text {
  display: none;
  position: absolute;
  bottom: 22px;
  left: 50%;
  transform: translateX(-50%);
  width: 240px;
  padding: 8px 10px;
  background: #1f2937;
  color: #f3f4f6;
  font-size: 12px;
  font-weight: 400;
  line-height: 1.45;
  border-radius: 6px;
  text-transform: none;
  letter-spacing: 0;
  white-space: normal;
  z-index: 10;
  pointer-events: none;
}
.readout-grid .label .help-tip:hover .tip-text {
  display: block;
}
.readout-grid .value {
  font-size: 19px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: #111827;
}
.readout-grid .value.warn { color: var(--warn); }
.readout-grid .value.ok { color: var(--ok); }
.readout-grid .value.hot { color: var(--hot); }
details.learn-more {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 4px 20px;
  box-shadow: var(--elev-1);
}
details.learn-more summary {
  padding: 12px 0;
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  color: #374151;
  outline: none;
}
details.learn-more[open] summary { color: #111827; }
details.learn-more .body {
  padding-bottom: 16px;
  font-size: 13.5px;
  color: #374151;
}
details.learn-more .body p { margin: 0 0 10px; }
details.learn-more .body code {
  background: #f3f4f6;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 12.5px;
}
abbr.glossary-chip {
  text-decoration: underline dotted;
  text-decoration-color: #9ca3af;
  cursor: help;
}
.explanation {
  padding: 10px 14px;
  background: #f0f9ff;
  border-left: 3px solid #0284c7;
  border-radius: 4px;
  font-size: 13px;
  color: #0c4a6e;
  margin-top: 10px;
}
footer {
  max-width: 1100px;
  margin: 24px auto 0;
  padding: 16px 20px 32px;
  font-size: 12px;
  color: var(--muted);
  text-align: center;
}
footer a { color: var(--muted); }
@media (max-width: 640px) {
  header h1 { font-size: 19px; }
  .readout-grid .value { font-size: 17px; }
}
.stage-with-phases {
  display: flex;
  gap: 14px;
  align-items: flex-start;
}
.stage-with-phases > svg {
  flex: 1;
  min-width: 0;
}
.phase-nav {
  width: 180px;
  flex-shrink: 0;
  background: #f8fbff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px 0;
  font-size: 11px;
  box-shadow: var(--elev-1);
}
.phase-nav .phase-nav-title {
  padding: 0 10px 6px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  margin-bottom: 4px;
}
.phase-nav .phase-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  cursor: pointer;
  color: #6b7280;
  font-weight: 500;
  transition: background 0.15s, color 0.15s, transform 0.15s, border-color 0.15s;
  border-left: 3px solid transparent;
}
.phase-nav .phase-item:hover {
  background: #eef4ff;
  color: #374151;
  transform: translateX(1px);
}
.phase-nav .phase-item.active {
  background: color-mix(in srgb, var(--accent) 14%, #ffffff);
  color: var(--accent-strong);
  border-left-color: var(--accent);
  font-weight: 700;
  transform: translateX(2px);
}
.phase-nav .phase-item.done {
  color: #059669;
}
.phase-nav .phase-item .phase-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #d1d5db;
  flex-shrink: 0;
}
.phase-nav .phase-item.active .phase-dot {
  background: var(--accent);
  animation: phaseDotPulse 1.15s ease-in-out infinite;
}
.phase-nav .phase-item.done .phase-dot {
  background: #059669;
}
@keyframes phaseDotPulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 38%, transparent); }
  70% { box-shadow: 0 0 0 6px color-mix(in srgb, var(--accent) 0%, transparent); }
  100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 0%, transparent); }
}
@media (max-width: 700px) {
  .stage-with-phases { flex-direction: column; }
  .phase-nav { width: 100%; }
  section.stage .stage-toolbar {
    flex-wrap: wrap;
  }
}
@media (prefers-reduced-motion: reduce) {
  .anim-tween { transition: none !important; animation: none !important; }
  .phase-nav .phase-item,
  .stage-scrubber input[type="range"]::-webkit-slider-thumb,
  .stage-scrubber input[type="range"]::-moz-range-thumb,
  button,
  button .btn-icon svg {
    transition: none !important;
  }
  .phase-nav .phase-item.active .phase-dot {
    animation: none !important;
  }
}
""".replace("__FONT_STACK__", _FONT_STACK)


# Shared JS runtime used by every lesson. Provides:
#  - teachRuntime.readControls(): reads every input/select in the controls
#    section and updates the numeric pills.
#  - teachRuntime.wire(recompute): binds recompute to every input/select
#    and runs it once.
#  - teachRuntime.wireToolbar(playFn, resetFn): binds the shared play/pause
#    toggle, speed dropdown, and reset button.
#  - teachRuntime.formatInt/Bytes/Ms: number formatters.
_BASE_JS = """
var teachRuntime = (function() {
  function formatInt(n) {
    if (!isFinite(n)) return "—";
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(Math.round(n));
  }
  function formatBytes(b) {
    if (!isFinite(b) || b < 0) return "—";
    if (b >= 1024*1024*1024) return (b / (1024*1024*1024)).toFixed(2) + " GiB";
    if (b >= 1024*1024) return (b / (1024*1024)).toFixed(2) + " MiB";
    if (b >= 1024) return (b / 1024).toFixed(1) + " KiB";
    return b + " B";
  }
  function formatMs(ms) {
    if (!isFinite(ms)) return "—";
    if (ms >= 1000) return (ms / 1000).toFixed(2) + " s";
    if (ms >= 1) return ms.toFixed(1) + " ms";
    return (ms * 1000).toFixed(1) + " µs";
  }
  function readControls(rootSel) {
    var root = document.querySelector(rootSel || "section.controls");
    var out = {};
    if (!root) return out;
    var els = root.querySelectorAll("input, select");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!el.name) continue;
      var v;
      if (el.type === "checkbox") v = el.checked;
      else if (el.type === "number" || el.type === "range") v = Number(el.value);
      else v = el.value;
      out[el.name] = v;
      var pill = document.querySelector('[data-pill-for="' + el.name + '"]');
      if (pill) pill.textContent = el.value;
    }
    return out;
  }
  function wire(recompute) {
    var els = document.querySelectorAll("section.controls input, section.controls select");
    for (var i = 0; i < els.length; i++) {
      els[i].addEventListener("input", recompute);
      els[i].addEventListener("change", recompute);
    }
    window.__teachRecompute = recompute;
    recompute();
  }
  function bootstrapFromObject(ctx) {
    ctx = ctx || {};
    var controls = ctx.controls || {};
    var root = document.querySelector("section.controls");
    if (root) {
      var els = root.querySelectorAll("input, select");
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (!el.name || !Object.prototype.hasOwnProperty.call(controls, el.name)) continue;
        var v = controls[el.name];
        if (el.type === "checkbox") {
          el.checked = !!v;
        } else {
          el.value = String(v);
        }
        var pill = document.querySelector('[data-pill-for="' + el.name + '"]');
        if (pill) pill.textContent = el.value;
      }
    }
    // Keep the lesson's own example query — it is a pedagogical classic
    // that matches the animated sample data (Alice, Bob, Carol, …).
    // Only inject the operator note as context for which operator the
    // user clicked on.
    if (ctx.note) {
      var note = document.querySelector(".query-card .query-note");
      if (note) note.textContent = String(ctx.note);
    }
    if (typeof window.__teachRecompute === "function") {
      window.__teachRecompute();
    } else {
      // Legacy fallback: try to trigger any control-driven recompute path.
      var evt;
      if (typeof Event === "function") evt = new Event("input", { bubbles: true });
      var root2 = document.querySelector("section.controls");
      if (evt && root2) {
        var first = root2.querySelector("input, select");
        if (first) first.dispatchEvent(evt);
      }
    }
  }

  // =====================================================================
  // Playback controller — unified toolbar, scrubber, and phase navigation.
  //
  // One state machine, one RAF loop, one seek() primitive that every entry
  // point (Play, Pause, Reset, scrubber drag, phase click) routes through.
  //
  // State diagram:
  //   IDLE  → Play    → PLAYING
  //   PLAYING → Pause  → PAUSED
  //   PAUSED  → Resume → PLAYING  (continues from currentMs)
  //   PAUSED  → Play   → PLAYING  (restart from 0)
  //   *       → seek() → SEEKED   (paused at targetMs, Resume → PLAYING)
  //   *       → Reset  → IDLE
  // =====================================================================
  var _ctrl = {
    build: null,         // fn → returns fresh anim.timeline()
    reset: null,         // fn → clears stage to ready state
    legacyPlay: null,    // fn → old-style play callback
    tl: null,            // current anim.timeline()
    phase: "idle",       // "idle" | "playing" | "paused" | "seeked"
    seekMs: 0,           // ms position we seeked to (valid when phase=seeked)
    loopEnabled: false,
    rafId: 0,            // current RAF id (0 = none)
    dragging: false,     // true while scrubber thumb is held
    // Phase nav
    phaseContainer: null,
    marks: []
  };
  // User-calibrated baseline: the dropdown's "1×" maps to this internal
  // speed. 0.312 = previous 0.52 × 0.6 (40% slower per user request
  // 2026-04-24; the animations felt rushed at the old baseline).
  var BASELINE_SPEED_SCALE = 0.312;

  // --- DOM refs (resolved once in wireToolbar) ---
  var _dom = {};

  // --- Helpers ---
  function _formatSec(ms) { return (Math.max(0, ms) / 1000).toFixed(1) + "s"; }

  function _setBtnLabel(btn, txt) {
    if (!btn) return;
    var lbl = btn.querySelector(".btn-label");
    if (lbl) lbl.textContent = txt; else btn.textContent = txt;
  }

  function _syncUI() {
    // Button label
    var label = _ctrl.phase === "idle" ? "Play"
              : _ctrl.phase === "playing" ? "Pause"
              : "Resume";
    _setBtnLabel(_dom.btnPlay, label);
    // Scrubber + time labels
    if (_ctrl.tl && _dom.scrubber) {
      var total = _ctrl.tl.getTotalDuration();
      if (_dom.timeTot) _dom.timeTot.textContent = _formatSec(total);
      if (total > 0) {
        var cur = _ctrl.tl.getCurrentTime();
        _dom.scrubber.value = String(Math.round(Math.min(1, cur / total) * 1000));
        if (_dom.timeCur) _dom.timeCur.textContent = _formatSec(cur);
      }
    } else {
      if (_dom.scrubber) _dom.scrubber.value = "0";
      if (_dom.timeCur) _dom.timeCur.textContent = "0.0s";
    }
    // Loop button
    if (_dom.btnLoop) {
      _setBtnLabel(_dom.btnLoop, _ctrl.loopEnabled ? "Loop: On" : "Loop: Off");
      _dom.btnLoop.setAttribute("aria-pressed", _ctrl.loopEnabled ? "true" : "false");
    }
  }

  // --- Single RAF loop: runs only while phase = "playing" ---
  // Uses a generation counter so stale loops from previous playbacks die.
  var _tickGen = 0;
  function _tick(gen) {
    if (gen !== _tickGen) return;                // stale loop — die
    if (_ctrl.phase !== "playing") { _ctrl.rafId = 0; return; }
    if (_ctrl.tl && _dom.scrubber && !_ctrl.dragging) {
      var total = _ctrl.tl.getTotalDuration();
      if (total > 0) {
        var cur = _ctrl.tl.getCurrentTime();
        var frac = Math.min(1, cur / total);
        _dom.scrubber.value = String(Math.round(frac * 1000));
        if (_dom.timeCur) _dom.timeCur.textContent = _formatSec(cur);
        _updatePhaseHighlight(cur);
      }
    }
    _ctrl.rafId = requestAnimationFrame(function() { _tick(gen); });
  }
  function _startTick() {
    _tickGen++;
    var gen = _tickGen;
    _ctrl.rafId = requestAnimationFrame(function() { _tick(gen); });
  }

  // --- Core operations ---

  // Fully stop everything and return to idle.  Does NOT call reset().
  function _stopAll() {
    if (_ctrl.tl) _ctrl.tl.stop();
    _ctrl.tl = null;
    _ctrl.phase = "idle";
    _ctrl.seekMs = 0;
    anim.setPaused(false);
    _tickGen++;                                    // kill any stale _tick loop
    if (_ctrl.rafId) { cancelAnimationFrame(_ctrl.rafId); _ctrl.rafId = 0; }
  }

  // Build a fresh timeline, apply all state up to `ms`, leave paused there.
  // This is the single seek primitive — scrubber, phase-nav, and toolbar
  // all route through here.
  function _seek(ms) {
    _stopAll();
    if (_ctrl.reset) _ctrl.reset();
    if (!_ctrl.build) return;
    _ctrl.tl = _ctrl.build();
    var total = _ctrl.tl.getTotalDuration();
    if (ms > total) ms = total;
    if (ms < 0) ms = 0;
    _ctrl.tl.fastForwardTo(ms);
    _ctrl.phase = "seeked";
    _ctrl.seekMs = ms;
    anim.setPaused(true);
    _renderPhaseNav();
    _syncUI();
    // Override scrubber to exact seek position
    if (_dom.scrubber && total > 0) {
      _dom.scrubber.value = String(Math.round((ms / total) * 1000));
    }
    if (_dom.timeCur) _dom.timeCur.textContent = _formatSec(ms);
  }

  // Start playing from `ms` (0 for fresh start).
  function _playFrom(ms) {
    _stopAll();
    if (_ctrl.reset) _ctrl.reset();
    if (!_ctrl.build) return;
    _ctrl.tl = _ctrl.build();
    _ctrl.phase = "playing";
    _ctrl.seekMs = 0;
    anim.setPaused(false);
    _renderPhaseNav();
    _syncUI();
    if (ms > 0) {
      _ctrl.tl.playFrom(ms, _onDone);
    } else {
      _ctrl.tl.play(_onDone);
    }
    _startTick();
  }

  function _onDone() {
    if (_ctrl.loopEnabled) { _playFrom(0); return; }
    _ctrl.phase = "idle";
    if (_ctrl.rafId) { cancelAnimationFrame(_ctrl.rafId); _ctrl.rafId = 0; }
    anim.setPaused(false);
    _syncUI();
    // Pin scrubber to end
    if (_ctrl.tl && _dom.scrubber) _dom.scrubber.value = "1000";
    if (_ctrl.tl && _dom.timeCur) _dom.timeCur.textContent = _formatSec(_ctrl.tl.getTotalDuration());
  }

  // --- Phase nav rendering & highlighting ---
  function _renderPhaseNav() {
    var c = _ctrl.phaseContainer;
    if (!c || !_ctrl.tl || !_ctrl.tl.getMarks) return;
    _ctrl.marks = _ctrl.tl.getMarks();
    c.innerHTML = '<div class="phase-nav-title">Phases</div>';
    for (var i = 0; i < _ctrl.marks.length; i++) {
      (function(idx) {
        var item = document.createElement("div");
        item.className = "phase-item";
        item.setAttribute("data-phase-idx", idx);
        item.innerHTML = '<span class="phase-dot"></span>' + _ctrl.marks[idx].name;
        item.addEventListener("click", function() {
          _seek(_ctrl.marks[idx].ms);
          _highlightPhaseIdx(idx);
        });
        c.appendChild(item);
      })(i);
    }
    _highlightPhaseIdx(0);
  }

  function _highlightPhaseIdx(activeIdx) {
    var c = _ctrl.phaseContainer;
    if (!c) return;
    var items = c.querySelectorAll(".phase-item");
    for (var i = 0; i < items.length; i++) {
      var cls = "phase-item";
      if (i === activeIdx) cls += " active";
      else if (i < activeIdx) cls += " done";
      items[i].className = cls;
    }
  }

  function _updatePhaseHighlight(currentMs) {
    if (!_ctrl.marks.length) return;
    var active = 0;
    for (var i = _ctrl.marks.length - 1; i >= 0; i--) {
      if (currentMs >= _ctrl.marks[i].ms) { active = i; break; }
    }
    _highlightPhaseIdx(active);
  }

  // --- Public API: wireToolbar ---
  function wireToolbar(optsOrPlay, maybeReset) {
    _dom.btnPlay  = document.getElementById("btn-play");
    _dom.btnReset = document.getElementById("btn-reset");
    _dom.btnLoop  = document.getElementById("btn-loop");
    _dom.selSpeed = document.getElementById("sel-speed");
    _dom.scrubber = document.getElementById("scrubber");
    _dom.timeCur  = document.getElementById("scrubber-time-current");
    _dom.timeTot  = document.getElementById("scrubber-time-total");

    if (typeof optsOrPlay === "function") {
      _ctrl.legacyPlay = optsOrPlay;
      _ctrl.reset = maybeReset;
    } else if (optsOrPlay && typeof optsOrPlay === "object") {
      _ctrl.build = optsOrPlay.build;
      _ctrl.reset = optsOrPlay.reset;
    }

    // --- Play / Pause / Resume button ---
    if (_dom.btnPlay) _dom.btnPlay.addEventListener("click", function() {
      switch (_ctrl.phase) {
        case "idle":
          if (_ctrl.legacyPlay) { _ctrl.legacyPlay(); return; }
          _playFrom(0);
          break;
        case "playing":
          // Pause
          _ctrl.phase = "paused";
          anim.setPaused(true);
          _syncUI();
          break;
        case "paused":
          // Resume from current position
          _ctrl.phase = "playing";
          anim.setPaused(false);
          _syncUI();
          _startTick();
          break;
        case "seeked":
          // Resume from seeked position
          _playFrom(_ctrl.seekMs);
          break;
      }
    });

    // --- Reset button ---
    if (_dom.btnReset) _dom.btnReset.addEventListener("click", function() {
      _stopAll();
      if (_ctrl.reset) _ctrl.reset();
      _syncUI();
    });

    // --- Loop button ---
    if (_dom.btnLoop) _dom.btnLoop.addEventListener("click", function() {
      _ctrl.loopEnabled = !_ctrl.loopEnabled;
      _syncUI();
    });

    // --- Speed dropdown ---
    function applyUiSpeed() {
      if (!_dom.selSpeed) return;
      anim.setSpeed(Number(_dom.selSpeed.value) * BASELINE_SPEED_SCALE);
    }
    if (_dom.selSpeed) _dom.selSpeed.addEventListener("change", applyUiSpeed);
    if (_dom.selSpeed) applyUiSpeed();

    // --- Scrubber (range input) ---
    if (_dom.scrubber && _ctrl.build) {
      _dom.scrubber.addEventListener("mousedown",  function() { _ctrl.dragging = true; });
      _dom.scrubber.addEventListener("touchstart", function() { _ctrl.dragging = true; });
      _dom.scrubber.addEventListener("input", function() {
        var frac = Number(_dom.scrubber.value) / 1000;
        if (!_ctrl.build) return;
        // _seek rebuilds the timeline internally; we need total from a
        // fresh build to convert frac → ms. Use _seek(0) first to get a
        // timeline, read its total, then re-seek to the real target.
        // Optimisation: if we already have a tl, reuse its totalDuration.
        var total;
        if (_ctrl.tl) {
          total = _ctrl.tl.getTotalDuration();
        } else {
          // No timeline yet — do a throwaway seek to 0 to get one
          _seek(0);
          if (!_ctrl.tl) return;
          total = _ctrl.tl.getTotalDuration();
        }
        _seek(frac * total);
      });
      function endDrag() { _ctrl.dragging = false; }
      _dom.scrubber.addEventListener("change",  endDrag);
      _dom.scrubber.addEventListener("mouseup",  endDrag);
      _dom.scrubber.addEventListener("touchend", endDrag);
    }

    // Legacy done callback
    window._teachOnDone = function() { _onDone(); };

    _syncUI();
  }

  // --- Public API: wirePhaseNav ---
  function wirePhaseNav(containerId, opts) {
    var container = document.getElementById(containerId);
    if (!container) return;
    _ctrl.phaseContainer = container;
    // build/reset already set by wireToolbar; opts here are redundant but
    // kept for backward compat.
    if (opts && opts.build && !_ctrl.build) _ctrl.build = opts.build;
    if (opts && opts.reset && !_ctrl.reset) _ctrl.reset = opts.reset;
    container.innerHTML = '<div class="phase-nav-title">Phases</div>';
  }

  // Backward-compat aliases used by older code paths
  function _updatePhaseNav(tl) { _renderPhaseNav(); }
  function _tickPhaseNav(currentMs) { _updatePhaseHighlight(currentMs); }

  function animationDone() {
    if (typeof window._teachOnDone === "function") window._teachOnDone();
  }

  return {
    formatInt: formatInt,
    formatBytes: formatBytes,
    formatMs: formatMs,
    readControls: readControls,
    wire: wire,
    bootstrapFromObject: bootstrapFromObject,
    wireToolbar: wireToolbar,
    wirePhaseNav: wirePhaseNav,
    _updatePhaseNav: _updatePhaseNav,
    _tickPhaseNav: _tickPhaseNav,
    animationDone: animationDone
  };
})();
"""


def esc(s: str) -> str:
    """HTML-escape *s*."""
    return html.escape(s, quote=True)


def help_tip(tip: str) -> str:
    """Render a small (?) help icon with a hover tooltip.

    Used inside readout labels so every metric box explains itself to
    first-time viewers. Example:

        <p class="label">Fan-out {help_tip("How many child...")}</p>
    """
    return (
        '<span class="help-tip">?'
        f'<span class="tip-text">{esc(tip)}</span>'
        "</span>"
    )


def explainer(title: str, bullets: list) -> str:
    """Render a small 'what you'll see in the animation' card.

    Prepended above the stage to orient first-time viewers before they
    press Play. Every lesson should have one — the animation-expert
    skill flags 'silent phase transitions' as a top ugliness signal.
    """
    items = "".join(f"<li>{esc(b)}</li>" for b in bullets)
    return f"""
<div class="explainer-card">
  <p class="explainer-title">{esc(title)}</p>
  <ul class="explainer-list">{items}</ul>
</div>
"""


def query_card(sql: str, note: str = "") -> str:
    """Render a small SQL query card that sits above a lesson's stage.

    Uses real table names so the animation tracks something the user
    actually recognises from their own schemas. The SQL is the query
    that the lesson's algorithm would be asked to execute.
    """
    note_html = f'<p class="query-note">{esc(note)}</p>' if note else ""
    return f"""
<div class="query-card">
  <p class="query-label">Example query this animation executes</p>
  <pre class="query-sql">{esc(sql)}</pre>
  {note_html}
</div>
"""


def phase_nav(nav_id: str = "phase-nav") -> str:
    """Render the phase navigation sidebar container.

    The actual phase items are populated dynamically by JS when the
    timeline is built (via ``tl.mark("Phase name")`` calls). Clicking
    a phase seeks the animation to that point.
    """
    return f'<div id="{esc(nav_id)}" class="phase-nav"></div>'


def stage_toolbar(status_text: str = "Ready — press Play") -> str:
    """Render the shared stage toolbar: play/pause toggle, speed dropdown,
    reset button, a scrubber (YouTube-style timeline seek), and a
    status-label span. Every lesson uses this."""
    return f"""
<div class="stage-toolbar">
  <button id="btn-play" class="primary">
    <span class="btn-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
    </span>
    <span class="btn-label">Play</span>
  </button>
  <button id="btn-reset">
    <span class="btn-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M12 6V3L8 7l4 4V8c2.8 0 5 2.2 5 5a5 5 0 1 1-9.9-1H5.1A7 7 0 1 0 12 6z"/></svg>
    </span>
    <span class="btn-label">Reset</span>
  </button>
  <button id="btn-loop" aria-pressed="false">
    <span class="btn-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M7 7h9v3l4-4-4-4v3H6a4 4 0 0 0-4 4v3h2V9a2 2 0 0 1 2-2zm10 10H8v-3l-4 4 4 4v-3h10a4 4 0 0 0 4-4v-3h-2v2a2 2 0 0 1-2 2z"/></svg>
    </span>
    <span class="btn-label">Loop: Off</span>
  </button>
  <label for="sel-speed" class="speed-label">Speed:</label>
  <select id="sel-speed" aria-label="Animation speed">
    <option value="0.25">0.25×</option>
    <option value="0.5">0.5×</option>
    <option value="1" selected>1×</option>
    <option value="2">2×</option>
    <option value="4">4×</option>
  </select>
  <span class="phase-label" id="phase-label">{esc(status_text)}</span>
</div>
<div class="stage-scrubber">
  <span class="scrubber-time" id="scrubber-time-current">0.0s</span>
  <input type="range" id="scrubber" min="0" max="1000" value="0" step="1"
    aria-label="Animation timeline scrubber" />
  <span class="scrubber-time" id="scrubber-time-total">0.0s</span>
</div>
"""


_LESSON_FAMILY_LABELS = {
    "bnl": "Join Operator",
    "hash": "Join Operator",
    "join": "Join Operator",
    "nested_loop": "Join Operator",
    "full_scan": "Scan Operator",
    "filter": "Filter Operator",
    "filesort": "Sort Operator",
    "tmp": "Temp Operator",
    "btree": "Index Access",
    "non_unique_lookup": "Index Access",
    "unique_lookup": "Index Access",
    "icp": "Index Access",
    "index_merge": "Index Access",
    "lru": "Buffer Cache",
    "derived_table": "Temp Operator",
}


def _family_chip(lesson_id: str) -> str:
    label = _LESSON_FAMILY_LABELS.get(lesson_id)
    if not label:
        return ""
    return (
        '<div class="lesson-meta"><span class="meta-label">Lesson family</span>'
        '<span class="family-chip"><span class="chip-dot" aria-hidden="true"></span>'
        + esc(label)
        + "</span></div>"
    )


def lesson_stage(
    *,
    sql: str,
    note: str = "",
    bullets: list = None,
    svg_id: str = "viz",
    viewbox: str = "0 0 960 540",
    phase_default: str = "",
    extra_stage_svg: str = "",
    readout_placeholders: list = None,
    explainer_title: str = "What you'll see",
    learn_more_html: str = "",
    toolbar_status: str = "Ready — press Play",
) -> dict:
    """Slice 3 / T4 — shared stage scaffold for every teach lesson.

    Returns the four HTML blobs ``render_page`` expects, each already
    composed using the smaller helpers above (``query_card``,
    ``explainer``, ``stage_toolbar``, ``phase_nav``):

        {
          "controls_html": …,  # query card + explainer + toolbar
          "stage_html":    …,  # <svg id=svg_id viewBox=viewbox>…</svg>
          "readout_html":  …,  # optional dl of live-updated stats
          "learn_more_html": … # as-passed; for curriculum Prev/Next etc.
        }

    Every output is pure HTML — the lesson is responsible only for the
    SVG elements and the JS that animates them. Authors call
    ``render_page(**lesson_stage(...), lesson_js=...)`` which
    eliminates ~80 lines of duplicated per-lesson chrome and makes
    cross-family drift impossible.

    ``readout_placeholders`` is a list of ``(id, label)`` tuples that
    render as ``<dt>label</dt><dd id="…">…</dd>`` for the lesson's JS
    to populate at runtime.

    ``phase_default`` seeds the visible phase label inside the
    phase-nav block. The JS engine will swap it as the timeline runs.
    """
    if bullets is None:
        bullets = []
    if readout_placeholders is None:
        readout_placeholders = []

    controls = []
    if sql:
        controls.append(query_card(sql, note))
    if bullets:
        controls.append(explainer(explainer_title, bullets))
    controls.append(stage_toolbar(toolbar_status))
    controls_html = "\n".join(controls)

    stage_html = (
        '<div class="stage-wrap">\n'
        f'  <svg id="{esc(svg_id)}" viewBox="{esc(viewbox)}"'
        f' preserveAspectRatio="xMidYMid meet"'
        f' role="img" aria-label="Lesson animation stage">\n'
        f'    {extra_stage_svg}\n'
        f'  </svg>\n'
        f'  {phase_nav()}\n'
        f'  <p class="phase-label" id="phase-label">{esc(phase_default)}</p>\n'
        '</div>'
    )

    if readout_placeholders:
        readout_items = "\n".join(
            f'    <dt>{esc(label)}</dt>\n    <dd id="{esc(rid)}">—</dd>'
            for rid, label in readout_placeholders
        )
        readout_html = (
            '<section class="readout" aria-label="Live stats">\n'
            '  <dl>\n'
            f'{readout_items}\n'
            '  </dl>\n'
            '</section>'
        )
    else:
        readout_html = ""

    return {
        "controls_html": controls_html,
        "stage_html": stage_html,
        "readout_html": readout_html,
        "learn_more_html": learn_more_html,
    }


def render_page(
    *,
    lesson_id: str,
    title: str,
    subtitle: str,
    version_chip: str = "MySQL 8.4 • MariaDB 11.4",
    banner_html: str = "",
    controls_html: str,
    stage_html: str,
    readout_html: str,
    learn_more_html: str,
    lesson_js: str,
    extra_css: str = "",
) -> str:
    """Assemble a complete self-contained HTML document for a lesson."""
    css = _BASE_CSS + "\n" + extra_css
    family_chip_html = _family_chip(lesson_id)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <meta name="description" content="{esc(subtitle)}">
  <meta name="generator" content="myflames teach">
  <style>{css}</style>
</head>
<body data-lesson="{esc(lesson_id)}">
  <header>
    <div class="inner">
      <h1>{esc(title)}<span class="version-chip">{esc(version_chip)}</span></h1>
      <p class="subtitle">{esc(subtitle)}</p>
      {family_chip_html}
    </div>
  </header>
  <main>
    {banner_html}
    {controls_html}
    {stage_html}
    {readout_html}
    {learn_more_html}
  </main>
  <footer>
    <p>Generated by <a href="https://github.com/vgrippa/myflames">myflames teach</a> —
    interactive database algorithm lessons. Offline-first, stdlib-only.</p>
  </footer>
  <script>{ANIM_JS}
{_BASE_JS}
{lesson_js}
  </script>
</body>
</html>
"""
