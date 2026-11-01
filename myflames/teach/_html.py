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
import json
import re
from typing import Iterable

from ._anim import ANIM_JS


def _load_tier1_runtime() -> str:
    """Read the committed Tier-1 bundle produced by assets/build.mjs.

    The bundle wraps Motion One (FLIP/spring animations) and d3
    (squarified treemap, Catmull-Rom paths) into additive helpers on
    window.anim. Install order matters: ANIM_JS runs first to set up
    the hand-rolled primitives (tween, timeline, pulse, arrival,
    svgEl, complexityChart…); this bundle then merges its new keys
    (flip, spring, squarify, smoothPath) without overwriting the
    existing ones. If the bundle file is missing (not yet built on
    the contributor's machine) we return an empty string so lessons
    still work — they just lose the Tier-1 helpers.
    """
    import os as _os
    bundle = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "assets", "anim-runtime.js",
    )
    try:
        with open(bundle, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return "/* tier-1 anim-runtime.js not built — run 'cd assets && npm install && npm run build' */"


TIER1_RUNTIME_JS = _load_tier1_runtime()


# Workspace tokens are shared with the standalone catalog. SVG colors have
# separate roles because they encode algorithm state, not UI selection.
_LIGHT_TOKENS = """
  --ink-secondary: #3c3c46;
  --line-subtle: #efeff4;
  --control-line-hover: #a8a8b3;
  --shadow-popover: 0 8px 24px rgba(15, 15, 20, 0.12), 0 0 0 1px rgba(15, 15, 20, 0.08);
  --bg: #ffffff;
  --surface: #ffffff;
  --surface-raised: #ffffff;
  --surface-muted: #f4f5f6;
  --surface-hover: rgba(15, 17, 23, 0.045);
  --surface-subtle: #fafbfc;
  --ink: #202429;
  --ink-muted: #59616b;
  --ink-faint: #626a74;
  --line: #d1d5da;
  --line-strong: #a7afb9;
  --control-line: #8c939b;
  --accent-line: #8ebcab;
  --accent: #16654e;
  --accent-hover: #104d3b;
  --accent-soft: rgba(22, 101, 78, 0.10);
  --accent-ink: #ffffff;
  --focus: var(--accent);
  --ok: #157347;
  --ok-soft: rgba(21, 115, 71, 0.10);
  --warn: #945300;
  --warn-soft: rgba(148, 83, 0, 0.12);
  --warn-line: #efdfc1;
  --danger: #c42b1c;
  --danger-soft: rgba(196, 43, 28, 0.10);
  --danger-line: rgba(196, 43, 28, 0.25);
  --info: #005fb8;
  --info-soft: rgba(0, 95, 184, 0.10);
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto,
    sans-serif;
  --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  --text-xs: 11px;
  --text-sm: 12px;
  --leading-sm: 18px;
  --text-md: 13px;
  --leading-md: 20px;
  --text-lg: 15px;
  --text-xl: 20px;
  --radius-sm: 5px;
  --radius-md: 7px;
  --radius-lg: 10px;
  --control-h: 28px;
  --svg-node-bg: #ffffff;
  --svg-node-muted: #f4f5f6;
  --svg-node-stroke: #b8bfc7;
  --svg-edge: #b8bfc7;
  --svg-line: #e4e7eb;
  --svg-ink: #17191c;
  --svg-ink-muted: #636b75;
  --svg-counter-bg: #17191c;
  --svg-counter-ink: #ffffff;
  --svg-on-solid: #ffffff;
  --svg-build-ink: #85400b;
  --svg-token-ink: #17598a;
  --svg-token: #17598a;
  --svg-token-soft: #eaf2f8;
  --svg-highlight: #fef08a;
  --svg-highlight-stroke: #d97706;
  --svg-hop: #b45309;
  --svg-hop-soft: #fdf3dc;
  --svg-build: #b45309;
  --svg-build-soft: #fdf3dc;
  --svg-probe: #0f766e;
  --svg-probe-soft: #e6f7f5;
  --svg-ok: #146c43;
  --svg-ok-soft: #e8f5ee;
  --svg-danger: #b42318;
  --svg-danger-soft: #fdeceb;
  --svg-group: #6d28d9;
  --svg-group-soft: #f3e8ff;
  --svg-group-alt: #be185d;
  --svg-group-alt-soft: #fce7f3;
  --panel: var(--surface);
  --panel-soft: var(--surface-muted);
  --panel-muted: var(--surface-muted);
  --text: var(--ink);
  --muted: var(--ink-muted);
  --border: var(--line);
  --border-strong: var(--line-strong);
  --code-bg: var(--surface-subtle);
  --accent-strong: var(--accent-hover);
  --hot: var(--danger);
  --cool: var(--svg-probe);
  --warm: var(--svg-highlight);
  --problem: var(--danger);
  --radius: var(--radius-md);
"""
_DARK_TOKENS = """
  --bg: #151719;
  --surface: #151719;
  --surface-raised: #25282c;
  --surface-muted: #1c1e21;
  --surface-hover: rgba(255, 255, 255, 0.06);
  --surface-subtle: #191b1e;
  --ink: #ededee;
  --ink-secondary: #d0d0d6;
  --ink-muted: #a5a9af;
  --ink-faint: #989ea7;
  --line: #34383d;
  --line-subtle: #292c30;
  --line-strong: #646c77;
  --control-line: #656e79;
  --control-line-hover: #6c6c74;
  --accent: #a9e7d3;
  --accent-hover: #c0eedf;
  --accent-soft: rgba(169, 231, 211, 0.12);
  --accent-line: #507b6e;
  --accent-ink: #143b31;
  --ok: #46c487;
  --ok-soft: rgba(70, 196, 135, 0.16);
  --warn: #e5a843;
  --warn-soft: rgba(229, 168, 67, 0.16);
  --warn-line: #5b4622;
  --danger: #ff6b5e;
  --danger-soft: rgba(255, 107, 94, 0.16);
  --danger-line: rgba(255, 107, 94, 0.4);
  --info: #63b3ff;
  --info-soft: rgba(99, 179, 255, 0.16);
  --shadow-popover: 0 8px 24px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.08);
  --svg-node-bg: #1c1e21;
  --svg-node-muted: #23272b;
  --svg-node-stroke: #454b52;
  --svg-edge: #454b52;
  --svg-line: #34383d;
  --svg-ink: #f0f2f4;
  --svg-ink-muted: #9ca3af;
  --svg-counter-bg: #25292e;
  --svg-counter-ink: #f0f2f4;
  --svg-on-solid: #151719;
  --svg-build-ink: #fcd38d;
  --svg-token-ink: #93c5fd;
  --svg-token: #60a5fa;
  --svg-token-soft: #1e3a5f;
  --svg-highlight: #713f12;
  --svg-highlight-stroke: #facc15;
  --svg-hop: #f59e0b;
  --svg-hop-soft: #3b2a14;
  --svg-build: #f59e0b;
  --svg-build-soft: #382814;
  --svg-probe: #2dd4bf;
  --svg-probe-soft: #133633;
  --svg-ok: #4ade80;
  --svg-ok-soft: #163324;
  --svg-danger: #f87171;
  --svg-danger-soft: #3b1d1d;
  --svg-group: #c4b5fd;
  --svg-group-soft: #302044;
  --svg-group-alt: #f9a8d4;
  --svg-group-alt-soft: #431e34;
"""
_THEME_CSS = (
    ":root { color-scheme: light;\n" + _LIGHT_TOKENS + "}\n"
    "@media (prefers-color-scheme: dark) {\n"
    ":root:not([data-theme=\"light\"]) { color-scheme: dark;\n"
    + _DARK_TOKENS + "}\n}\n"
    ':root[data-theme="dark"] { color-scheme: dark;\n' + _DARK_TOKENS + "}\n"
    ':root[data-theme="light"] { color-scheme: light; }\n'
)

# Base CSS shared by every lesson; names are also used by browser tests.
_BASE_CSS = _THEME_CSS + """
* {

  box-sizing: border-box;

}
html, body {
  margin: 0; padding: 0;
  background: var(--bg); color: var(--ink);
  font-family: var(--font-sans); font-size: var(--text-md);
  -webkit-font-smoothing: antialiased; line-height: 1.5;
}
main {
  max-width: 1120px;
  margin: 0 auto;
  padding: 16px 20px 32px;
}
header {
  padding: 12px 20px;
  border-bottom: 1px solid var(--line);
}
header .inner {
  max-width: 1080px;
  margin: 0 auto;
}
header h1 {
  margin: 0 0 4px;
  font-size: var(--text-lg);
  font-weight: 600;
  letter-spacing: -0.01em;
}
header .subtitle {
  margin: 0;
  color: var(--ink-secondary);
}
header .lesson-meta {
  margin-top: 8px;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
header .meta-label {
  font-size: var(--text-xs);
  color: var(--ink-muted);
}
header .family-chip, header .version-chip {
  display: inline-flex; align-items: center; gap: 6px;
  min-height: 19px; padding: 1px 6px;
  border: 1px solid var(--line-subtle); border-radius: var(--radius-sm);
  background: var(--surface-muted); color: var(--ink-secondary);
  font-size: var(--text-xs); font-weight: 500; letter-spacing: 0;
}
header .version-chip {
  margin-left: 8px;
  vertical-align: 1px;
}
header .chip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}
html[data-embedded="true"] body > header,
html[data-embedded="true"] body > footer {
  display: none;
}
html[data-embedded="true"] main {
  max-width: none;
  padding: 12px 16px 20px;
}
.banner {
  margin: 0 0 10px; padding: 8px 12px;
  border: 1px solid var(--warn-line); border-radius: var(--radius-md);
  background: var(--warn-soft); color: var(--ink); font-size: var(--text-sm);
}
.banner strong {
  color: var(--warn);
}
/* Parameters: one compact row next to the drawing (render_page moves each
   control's hint into the (?) beside its label), so a change shows on the
   drawing without scrolling. */
section.controls {
  padding: 6px 12px 8px; margin-bottom: 8px;
  border: 1px solid var(--line); border-radius: var(--radius-md);
  background: var(--surface-muted);
}
section.readout h2, section.about-example h2 {
  margin: 0 0 8px;
  font-size: var(--text-lg);
  font-weight: 600;
}
section.controls h2 {
  margin: 0 0 4px;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--ink-secondary);
}
.controls-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 0 12px;
  margin-bottom: 4px;
}
.controls-head h2 {
  margin: 0;
  min-width: 0;
}
.about-link {
  font-size: var(--text-sm);
  white-space: nowrap;
}
.control-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 6px 16px;
}
.control {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
/* The (?) sits right after the label, on its first line when a narrow
   column wraps the label. It stays outside the label, so a click on it
   never toggles or focuses the control. */
.control-head {
  display: flex;
  align-items: baseline;
  gap: 4px;
  min-width: 0;
}
.control-head label {
  min-width: 0;
}
.control label {
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--ink-secondary);
}
.control .hint, section.controls > .hint {
  font-size: var(--text-sm);
  color: var(--ink-muted);
}
section.controls > .hint {
  margin: 0;
}
/* A control's hint opens under the control, as wide as it, on hover of its
   (?) or while the control has keyboard focus. Screen readers get it as the
   control's description (aria-describedby). */
.control .help-tip {
  position: static;
  flex: none;
  margin-left: 0;
}
.control .help-tip .tip-text {
  top: calc(100% + 4px);
  bottom: auto;
  left: 0;
  right: 0;
  width: auto;
}
.control:has(:focus-visible) .tip-text {
  display: block;
}
.control input[type="number"], select {
  min-width: 0; max-width: 100%; min-height: var(--control-h);
  padding: 3px 8px; border: 1px solid var(--control-line); border-radius: var(--radius-sm);
  font: inherit; font-size: var(--text-sm); font-weight: 500;
  background-color: var(--surface); color: var(--ink);
}
select {
  appearance: none; padding-right: 24px;
  background-image: linear-gradient(45deg, transparent 50%, var(--ink-muted) 50%),
    linear-gradient(135deg, var(--ink-muted) 50%, transparent 50%);
  background-position: calc(100% - 12px) 50%, calc(100% - 8px) 50%;
  background-size: 4px 4px; background-repeat: no-repeat;
}
:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.value-pill {
  display: inline-block; padding: 1px 6px; border: 1px solid var(--line-subtle);
  border-radius: var(--radius-sm); background: var(--surface); color: var(--ink);
  font: 600 var(--text-sm) var(--font-mono); font-variant-numeric: tabular-nums;
}
input[type="range"] {
  appearance: none;
  height: 4px;
  border-radius: 999px;
  background: var(--line);
  accent-color: var(--accent);
  cursor: pointer;
}
.control input[type="range"] {
  width: 100%;
  margin: 7px 0;
}
input[type="range"]::-webkit-slider-runnable-track {
  height: 4px;
  border-radius: 999px;
  background: var(--line);
}
input[type="range"]::-moz-range-track {
  height: 4px;
  border-radius: 999px;
  background: var(--line);
}
input[type="range"]::-webkit-slider-thumb {
  appearance: none; margin-top: -5px; width: 14px; height: 14px;
  border-radius: 50%; border: 2px solid var(--surface); background: var(--accent); box-shadow: none;
}
input[type="range"]::-moz-range-thumb {
  width: 14px; height: 14px; border-radius: 50%; border: 2px solid var(--surface);
  background: var(--accent); box-shadow: none;
}
section.stage {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  padding: 12px;
  margin-bottom: 10px;
}
.query-card, .explainer-card, .explanation {
  margin: 0 0 10px; padding: 8px 12px; background: var(--surface-subtle);
  border: 1px solid var(--line); border-radius: var(--radius-md);
  color: var(--ink-secondary); font-size: var(--text-sm);
}
.query-card .query-label, .explainer-card .explainer-title, .complexity-chart .chart-title {
  margin: 0 0 3px; font-size: var(--text-xs); font-weight: 600; color: var(--ink-muted);
}
.query-card .query-sql {
  margin: 0;
  font: var(--text-sm)/1.45 var(--font-mono);
  color: var(--ink);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.query-card .query-note {
  margin: 4px 0 0;
  color: var(--ink-muted);
}
.explainer-card .explainer-title {
  color: var(--ink);
}
.explainer-card .explainer-title::before {
  content: "";
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  margin-right: 6px;
  vertical-align: 1px;
}
.explainer-card .explainer-list {
  margin: 0;
  padding-left: 18px;
}
.explainer-card li {
  margin-bottom: 4px;
}
.explainer-card li:last-child {
  margin-bottom: 0;
}
/* About this example: the example query and what the drawing shows, right
   under the stage (the stage comes first). Side by side when there is room. */
section.about-example {
  margin-bottom: 10px;
}
.about-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 380px), 1fr));
  gap: 10px;
}
.about-grid > .query-card, .about-grid > .explainer-card {
  margin: 0;
  min-width: 0;
}
.stage-toolbar {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  min-height: 38px; padding: 5px 10px; background: var(--surface-muted);
  border: 1px solid var(--line); border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  font-size: var(--text-sm);
}
.stage-toolbar .speed-label {
  color: var(--ink-secondary);
  font-size: var(--text-sm);
}
.phase-label {
  font: var(--text-sm)/1.5 var(--font-mono); color: var(--ink-secondary);
  background: var(--surface-subtle); border: 1px solid var(--line-subtle);
  padding: 3px 8px; border-radius: var(--radius-sm); overflow-wrap: anywhere;
}
.stage-toolbar .phase-label {
  margin-left: auto;
}
.stage-scrubber {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding: 5px 10px;
  background: var(--surface-subtle);
  border: 1px solid var(--line);
  border-top: 0;
}
.stage-scrubber input[type="range"] {
  flex: 1;
  min-width: 0;
}
.stage-scrubber .scrubber-time {
  font: var(--text-xs) var(--font-mono);
  color: var(--ink-muted);
  min-width: 42px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
button {
  display: inline-flex; align-items: center; gap: 6px; min-height: var(--control-h);
  padding: 4px 10px; border: 1px solid var(--control-line); border-radius: var(--radius-sm);
  background: var(--surface); color: var(--ink); font: inherit; font-size: var(--text-sm); font-weight: 500; cursor: pointer;
}
button:hover:not(:disabled) {
  background: var(--surface-subtle);
  border-color: var(--ink-muted);
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
button.primary {
  background: var(--accent);
  color: var(--accent-ink);
  border-color: var(--accent);
  font-weight: 600;
}
button.primary:hover:not(:disabled) {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}
button[aria-pressed="true"] {
  background: var(--accent-soft);
  color: var(--accent);
  border-color: var(--accent-line);
  font-weight: 600;
}
button .btn-icon, button .btn-icon svg {
  width: 16px;
  height: 16px;
  display: inline-flex;
  fill: currentColor;
}
button .btn-label {
  line-height: 1;
  white-space: nowrap;
}
section.stage svg {
  width: 100%;
  height: auto;
  display: block;
}
/* Fit: a stage drawing is never taller than the window or frame showing it,
   so scrolling to the stage shows the whole drawing under its toolbar. The
   drawing keeps its proportions (preserveAspectRatio meet). teachFocus.fit()
   measures --stage-chrome: the toolbar, scrubber and captions around it. */
.stage-with-phases svg, section.stage > svg, .stage-wrap > svg {
  max-height: max(160px, calc(100vh - var(--stage-chrome, 96px)));
  max-height: max(160px, calc(100dvh - var(--stage-chrome, 96px)));
}
section.stage button svg {
  width: 16px;
  height: 16px;
}
/* Full screen (focus layout): only the animation, its controls, the caption
   and the live numbers. Parameters, the query and the explainer open in the
   Details panel beside the stage. */
:root {
  --focus-details-w: clamp(280px, 32vw, 420px);
}
html:not([data-focus="true"]) .focus-only,
html[data-embedded="true"] #btn-fullscreen,
#btn-fullscreen .icon-exit,
html[data-focus="true"] #btn-fullscreen .icon-enter {
  display: none;
}
html[data-focus="true"] #btn-fullscreen .icon-exit {
  display: inline;
}
html[data-focus="true"],
html[data-focus="true"] body {
  height: 100%;
  overflow: hidden;
}
html[data-focus="true"] body > header,
html[data-focus="true"] body > footer,
html[data-focus="true"] main {
  display: none;
}
html[data-focus="true"][data-focus-details="true"] main {
  display: block;
  position: fixed;
  z-index: 2;
  top: 0;
  right: 0;
  bottom: 0;
  width: var(--focus-details-w);
  max-width: none;
  margin: 0;
  padding: 12px;
  overflow: auto;
  overscroll-behavior: contain;
  background: var(--surface);
  border-left: 1px solid var(--line);
}
html[data-focus="true"] main section.stage {
  padding: 0;
  border: 0;
  background: none;
}
.focus-shell {
  position: fixed;
  z-index: 1;
  inset: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 12px;
  background: var(--bg);
}
html[data-focus-details="true"] .focus-shell {
  right: var(--focus-details-w);
}
.focus-top {
  flex: none;
}
.focus-top .stage-scrubber {
  margin-bottom: 0;
}
html[data-focus="true"] #btn-details {
  margin-left: auto;
}
.focus-stage {
  flex: 1 1 0;
  display: flex;
  min-height: 0;
}
html[data-focus="true"] .focus-stage > * {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}
html[data-focus="true"] .focus-stage .stage-with-phases {
  display: flex;
  align-items: stretch;
  gap: 12px;
  overflow: hidden;
}
html[data-focus="true"] .focus-stage .phase-nav {
  align-self: flex-start;
  width: 168px;
  max-height: 100%;
  margin: 0;
  overflow: auto;
}
html[data-focus="true"] .focus-stage .stage-with-phases > :not(.phase-nav):not(.stage-side) {
  flex: 1 1 0;
  align-self: stretch;
  min-width: 0;
  min-height: 0;
}
/* Wrappers around several drawings: a lesson's own grid keeps its columns
   and gets one row that fills the height; other wrappers stack. */
html[data-focus="true"] .focus-stage .stage-with-phases > div:not(.phase-nav) {
  display: flex;
  flex-direction: column;
  grid-template-rows: minmax(0, 1fr);
}
html[data-focus="true"] .focus-stage .stage-with-phases > div:not(.phase-nav) > div {
  display: flex;
  flex-direction: column;
  align-self: stretch;
  min-width: 0;
  min-height: 0;
}
html[data-focus="true"] .focus-stage svg {
  flex: 1 1 0;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  max-height: none;
}
/* A drawing inside a panel keeps its own height (shrinking only when the
   window is short), so it stays next to the panel's title and caption. */
html[data-focus="true"] .focus-stage .stage-with-phases > div:not(.phase-nav),
html[data-focus="true"] .focus-stage .stage-with-phases > div:not(.phase-nav) > div {
  justify-content: center;
}
html[data-focus="true"] .focus-stage .stage-with-phases > div:not(.phase-nav) svg {
  flex: 0 1 auto;
  height: auto;
  max-height: 100%;
}
html[data-focus="true"] .focus-stage .stage-with-phases > div:not(.phase-nav) > svg {
  align-self: center;
}
.focus-strip {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.focus-caption .phase-label {
  display: block;
  margin: 0;
  padding: 5px 10px;
  font-size: var(--text-md);
  line-height: 1.45;
  color: var(--ink);
}
.focus-strip .readout-grid {
  grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
}
.focus-strip .readout-grid .item {
  padding: 4px 10px;
}
.focus-strip .readout-grid .value {
  font-size: var(--text-md);
  line-height: 1.4;
}
@media (max-width: 900px) {
  html[data-focus="true"] .focus-stage .phase-nav {
    display: none;
  }
}
@media (max-height: 600px) {
  .focus-shell {
    gap: 4px;
    padding: 4px 8px;
  }
  .focus-strip {
    gap: 4px;
  }
  .focus-caption .phase-label {
    padding: 3px 8px;
    font-size: var(--text-sm);
  }
  /* Short windows: each number becomes one "label value" chip. */
  .focus-strip .readout-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    background: none;
    border: 0;
  }
  .focus-strip .readout-grid .item {
    display: flex;
    align-items: baseline;
    gap: 6px;
    padding: 2px 8px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
  }
  .focus-strip .readout-grid .label {
    margin: 0;
    white-space: nowrap;
  }
  .focus-strip .readout-grid .value {
    font-size: var(--text-sm);
    line-height: 1.4;
  }
}
.stage-with-phases {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  overflow-x: auto;
}
.stage-with-phases > svg {
  flex: 1;
  min-width: 0;
}
.phase-nav {
  width: 180px;
  flex-shrink: 0;
  background: var(--surface-muted);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  padding: 8px 0;
  font-size: var(--text-sm);
}
.phase-nav:empty,
.phase-nav[hidden],
.phase-nav:not(:has(.phase-item)) {
  display: none;
}
.phase-nav-title {
  padding: 0 10px 6px;
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--ink-muted);
  border-bottom: 1px solid var(--line);
  margin-bottom: 4px;
}
.phase-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  cursor: pointer;
  color: var(--ink-secondary);
  border-left: 3px solid transparent;
}
.phase-item:hover {
  background: var(--surface-hover);
}
.phase-item.active {
  background: var(--accent-soft);
  color: var(--accent);
  border-left-color: var(--accent);
  font-weight: 600;
}
.phase-item.done {
  color: var(--ok);
}
.phase-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--line-strong);
  flex-shrink: 0;
}
.phase-item.active .phase-dot {
  background: var(--accent);
}
.phase-item.done .phase-dot {
  background: var(--ok);
}
section.readout {
  margin-bottom: 10px;
}
.readout-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
}
.readout-grid .item {
  background: var(--surface);
  padding: 9px 12px;
  min-width: 0;
}
.readout-grid .label {
  margin: 0 0 2px;
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--ink-muted);
}
.readout-grid .value {
  margin: 0;
  font: 600 16px/1.5 var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--ink);
  overflow-wrap: anywhere;
}
.readout-grid .value.warn {
  color: var(--warn);
}
.readout-grid .value.ok {
  color: var(--ok);
}
.readout-grid .value.hot {
  color: var(--danger);
}
.help-tip {
  display: inline-block;
  position: relative;
  cursor: help;
  color: var(--ink-muted);
  border: 1px solid var(--control-line);
  border-radius: 50%;
  width: 15px;
  height: 15px;
  font-size: var(--text-xs);
  line-height: 14px;
  text-align: center;
  margin-left: 4px;
}
.help-tip .tip-text {
  display: none;
  position: absolute;
  bottom: 22px;
  left: -100px;
  width: 220px;
  padding: 8px 10px;
  background: var(--surface-raised);
  border: 1px solid var(--line);
  color: var(--ink);
  box-shadow: var(--shadow-popover);
  font-size: var(--text-sm);
  line-height: 1.45;
  border-radius: var(--radius-md);
  text-align: left;
  z-index: 10;
  pointer-events: none;
}
.help-tip:hover .tip-text, .help-tip:focus .tip-text {
  display: block;
}
.explanation {
  margin-top: 10px;
}
.complexity-chart {
  margin-top: 10px;
  padding: 10px 12px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  overflow-x: auto;
}
.complexity-chart svg {
  width: 100%;
  max-width: 600px;
  height: auto;
}
details.learn-more {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  overflow: hidden;
}
details.learn-more summary {
  min-height: 34px;
  padding: 6px 12px;
  cursor: pointer;
  background: var(--surface-muted);
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--ink);
}
details.learn-more .body {
  padding: 12px;
  font-size: var(--text-md);
  color: var(--ink-secondary);
  overflow-wrap: anywhere;
}
details.learn-more .body p {
  margin: 0 0 10px;
}
code, pre {
  font-family: var(--font-mono);
}
details.learn-more code {
  background: var(--surface-muted);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
}
abbr.glossary-chip {
  text-decoration: underline dotted;
  text-decoration-color: var(--ink-muted);
  cursor: help;
}
footer {
  max-width: 1120px;
  margin: 16px auto 0;
  padding: 12px 20px 24px;
  font-size: var(--text-sm);
  color: var(--ink-muted);
}
a {
  color: var(--accent);
}
@media (max-width: 700px) {
  .stage-with-phases {
    display: block;
  }
  .phase-nav {
    width: 100%;
    margin-top: 8px;
  }
  .complexity-chart svg {
    min-width: 560px;
  }
  .stage-toolbar .phase-label {
    width: 100%;
    margin-left: 0;
  }
}
/* Narrow and tall (a phone): scroll drawings sideways at a readable scale and
   keep the surrounding page narrow. A short window fits the drawing instead. */
@media (max-width: 700px) and (min-height: 640px) {
  .stage-with-phases > svg {
    min-width: 760px;
  }
  .stage-with-phases > div:not(.phase-nav) {
    min-width: 800px;
  }
}
@media (max-width: 650px) {
  :root {
    --control-h: 32px;
  }
  main, html[data-embedded="true"] main {
    padding: 12px;
  }
  header {
    padding: 12px;
  }
  header .version-chip {
    margin: 4px 0 0;
  }
  .control-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
/* Guided lessons (a lesson with guide content): a step bar under the
   scrubber, simplified code beside the drawing (under it in a narrow window),
   Check yourself and Explore. The side column never grows taller than the
   drawing's own fit limit, so it cannot push the drawing off screen. */
html[data-guided="true"] .stage-scrubber {
  margin-bottom: 0;
}
.guide-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  padding: 5px 10px;
  background: var(--surface-subtle);
  border: 1px solid var(--line);
  border-top: 0;
  font-size: var(--text-sm);
}
.focus-top .guide-bar {
  margin-bottom: 0;
}
.guide-bar [hidden], .stage-side [hidden], .quiz-dialog [hidden] {
  display: none !important;
}
.guide-count {
  color: var(--ink-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.guide-end {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
.guide-live {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
.guide-explore-only {
  display: none;
}
html[data-explore="true"] button.guide-explore-only {
  display: inline-flex;
}
html[data-explore="true"] input.guide-explore-only {
  display: inline-block;
}
html[data-explore="true"] .stage-scrubber {
  display: none;
}
input.guide-beat {
  flex: 1 1 160px;
  min-width: 120px;
}
.guide-inputs {
  flex: 1 0 100%;
  min-width: 0;
}
.guide-inputs section.controls {
  margin: 4px 0 0;
  max-height: 40vh;
  overflow: auto;
  background: var(--surface);
}
.stage-side {
  position: relative;
  flex: 0 0 260px;
  width: 260px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: max(160px, calc(100vh - var(--stage-chrome, 96px)));
  max-height: max(160px, calc(100dvh - var(--stage-chrome, 96px)));
  overflow: auto;
}
.stage-side.is-collapsed {
  flex-basis: 180px;
  width: 180px;
}
.stage-side .phase-nav {
  width: auto;
  flex-shrink: 0;
}
.code-panel {
  flex-shrink: 0;
  background: var(--surface-subtle);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
}
.code-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px 3px 3px;
}
.code-panel:not(.is-collapsed) .code-head {
  border-bottom: 1px solid var(--line);
}
button.code-toggle {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 24px;
  padding: 2px 6px;
  border-color: transparent;
  background: none;
  font-weight: 600;
  text-align: left;
}
button.code-toggle:hover:not(:disabled) {
  border-color: transparent;
  background: var(--surface-hover);
}
.code-chevron {
  flex: none;
  width: 0;
  height: 0;
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
  border-top: 5px solid var(--ink-muted);
  transition: transform 150ms ease-out;
}
.code-panel.is-collapsed .code-chevron {
  transform: rotate(-90deg);
}
.code-tag {
  flex: none;
  padding: 0 5px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  color: var(--ink-muted);
  font-size: var(--text-xs);
}
.code-lines {
  list-style: none;
  margin: 0;
  padding: 6px 0;
  font: var(--text-sm)/1.45 var(--font-mono);
  counter-reset: code-line;
}
.code-line {
  display: grid;
  grid-template-columns: 26px minmax(0, 1fr);
  padding: 1px 8px 1px 0;
  border-left: 3px solid transparent;
  counter-increment: code-line;
}
.code-line::before {
  content: counter(code-line);
  padding-right: 8px;
  text-align: right;
  color: var(--ink-faint);
}
.code-text, .code-fn {
  grid-column: 2;
  padding-left: calc(var(--indent, 0) * 2ch);
  overflow-wrap: anywhere;
}
.code-text {
  color: var(--ink-secondary);
}
.code-fn {
  font-size: var(--text-xs);
  color: var(--ink-muted);
}
.code-fn-part {
  display: inline-block;
  max-width: 100%;
}
.code-line.is-active {
  background: var(--accent-soft);
  border-left-color: var(--accent);
}
.code-line.is-active .code-text {
  color: var(--ink);
  font-weight: 600;
}
.code-note {
  margin: 0;
  padding: 5px 10px 7px;
  border-top: 1px solid var(--line-subtle);
  color: var(--ink-muted);
  font-size: var(--text-xs);
}
.quiz-dialog {
  width: min(560px, calc(100vw - 32px));
  max-height: calc(100vh - 32px);
  max-height: calc(100dvh - 32px);
  padding: 0;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--surface-raised);
  color: var(--ink);
  box-shadow: var(--shadow-popover);
}
.quiz-dialog[open] {
  display: flex;
  flex-direction: column;
}
.quiz-dialog::backdrop {
  background: rgba(15, 17, 23, 0.45);
}
.quiz-head, .quiz-foot {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 14px;
}
.quiz-head {
  border-bottom: 1px solid var(--line);
}
.quiz-head h2 {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: 600;
}
.quiz-foot {
  flex-wrap: wrap;
  border-top: 1px solid var(--line);
}
.quiz-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 10px 14px;
}
.quiz-intro {
  margin: 0 0 10px;
  color: var(--ink-muted);
  font-size: var(--text-sm);
}
.quiz-q {
  min-width: 0;
  margin: 0 0 14px;
  padding: 0;
  border: 0;
}
.quiz-q legend {
  margin-bottom: 6px;
  padding: 0;
  font-weight: 600;
}
.quiz-choice {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 6px;
  padding: 6px 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  cursor: pointer;
}
.quiz-choice input {
  flex: none;
  margin: 3px 0 0;
  accent-color: var(--accent);
}
.quiz-choice.is-right {
  border-color: var(--ok);
  background: var(--ok-soft);
}
.quiz-choice.is-wrong {
  border-color: var(--danger-line);
  background: var(--danger-soft);
}
.quiz-mark {
  flex: none;
  margin-left: auto;
  font-weight: 700;
}
.quiz-choice.is-right .quiz-mark {
  color: var(--ok);
}
.quiz-choice.is-wrong .quiz-mark {
  color: var(--danger);
}
.quiz-feedback {
  margin: 2px 0 0;
  color: var(--ink-secondary);
  font-size: var(--text-sm);
}
.quiz-feedback:empty {
  display: none;
}
.quiz-feedback .verdict-right {
  color: var(--ok);
  font-weight: 600;
}
.quiz-feedback .verdict-wrong {
  color: var(--danger);
  font-weight: 600;
}
.quiz-tally {
  margin: 0;
  color: var(--ink-secondary);
  font-size: var(--text-sm);
}
html[data-focus="true"] .focus-stage .stage-side {
  flex: 0 0 auto;
  align-self: stretch;
  width: clamp(200px, 24vw, 280px);
  max-height: 100%;
}
html[data-focus="true"] .focus-stage .stage-side.is-collapsed {
  width: 168px;
}
html[data-focus="true"] .focus-stage .stage-side .phase-nav {
  align-self: stretch;
  width: auto;
  max-height: none;
}
/* Narrow windows: the simplified code goes under the drawing, which keeps
   the full width. */
@media (max-width: 900px) {
  html[data-guided="true"] .stage-with-phases {
    flex-wrap: wrap;
  }
  .stage-side, .stage-side.is-collapsed {
    flex: 1 1 100%;
    width: auto;
    max-height: none;
  }
}
@media (max-width: 700px) {
  .stage-side, .stage-side.is-collapsed {
    margin-top: 8px;
  }
  html[data-focus="true"] .focus-stage .stage-with-phases {
    flex-direction: column;
  }
  html[data-focus="true"] .focus-stage .stage-side,
  html[data-focus="true"] .focus-stage .stage-side.is-collapsed {
    flex: none;
    width: auto;
    max-height: 38%;
    margin-top: 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .anim-tween {
    transition: none !important;
    animation: none !important;
  }
  .code-chevron {
    transition: none;
  }
}
"""


# Shared JS runtime used by every lesson. Provides:
#  - teachRuntime.readControls(): reads every input/select in the controls
#    section and updates the numeric pills.
#  - teachRuntime.wire(recompute): binds recompute to every input/select
#    and runs it once.
#  - teachRuntime.wireToolbar(playFn, resetFn): binds the shared play/pause
#    toggle, speed dropdown, and reset button.
#  - teachRuntime.formatInt/Bytes/Ms: number formatters.
_THEME_JS = """
(function() {
  var params = new URLSearchParams(window.location.search);
  var theme = params.get("theme");
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  }
  if (params.get("embedded") === "1") {
    document.documentElement.setAttribute("data-embedded", "true");
  }
})();
"""
# Fit and full screen, shared by every lesson. The focus layout moves the
# toolbar, scrubber, stage, caption and readout into a fixed shell and puts
# each node back exactly where it was on exit, so lesson code keeps its
# element references. The workspace (the parent of an embedded lesson) owns
# full screen there and switches the layout by message; a standalone page
# uses its own Full screen button. Messages are accepted only from the parent
# window and only with this fixed type, like the appearance message.
_FOCUS_JS = """
var teachFocus = (function() {
  var MESSAGE = "myflames-lesson-focus";
  var root = document.documentElement;
  var state = { on: false, native: false, moved: [], shell: null, hold: null };

  function embedded() {
    return root.getAttribute("data-embedded") === "true" && window.parent !== window;
  }
  function stageSvgs() {
    var list = document.querySelectorAll(".stage-with-phases svg, section.stage > svg, .stage-wrap > svg");
    var out = [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].getClientRects().length) out.push(list[i]);
    }
    return out;
  }

  // Room a drawing needs around it: everything from the top of the stage
  // toolbar down to the drawing (scrubber, step bar, panel titles) plus
  // captions under it inside the same panel. None of it depends on the
  // drawing's height. What sits above the toolbar (the page's top margin, a
  // banner, the one-row parameters) counts too while it takes at most a
  // quarter of the window and costs the drawings at most 15% of their
  // scale: the lesson then opens with its parameters, toolbar and whole
  // drawing on screen, so a change shows on the drawing without scrolling.
  // Otherwise (a narrow window wraps the parameters, a short one) the
  // drawing keeps its scale and fits under its toolbar.
  function natural(svg) {
    var vb = svg.viewBox && svg.viewBox.baseVal;
    if (!vb || !vb.width || !vb.height) return 0;
    return svg.getBoundingClientRect().width * vb.height / vb.width;
  }
  function fit() {
    if (state.on) return;
    var toolbar = document.querySelector(".stage-toolbar");
    var svgs = stageSvgs();
    if (!toolbar || !svgs.length) return;
    var top = toolbar.getBoundingClientRect().top;
    var room = 0;
    for (var i = 0; i < svgs.length; i++) {
      var box = svgs[i].getBoundingClientRect();
      var extra = Math.max(0, box.top - top);
      var cell = svgs[i].parentElement;
      if (cell && !cell.classList.contains("stage-with-phases") &&
          window.getComputedStyle(cell).display === "block") {
        extra += Math.max(0, cell.getBoundingClientRect().bottom - box.bottom);
      }
      room = Math.max(room, extra);
    }
    // A little slack for a phase label that wraps onto one more line.
    room += 24;
    var above = top + (window.pageYOffset || 0);
    if (above > 0 && above <= window.innerHeight / 4) {
      var cap = window.innerHeight - room;
      var cheap = true;
      for (var j = 0; j < svgs.length && cheap; j++) {
        var want = natural(svgs[j]);
        if (!want) continue;
        var kept = Math.min(want, Math.max(160, cap));
        cheap = Math.min(want, Math.max(160, cap - above)) >= 0.85 * kept;
      }
      if (cheap) room += above;
    }
    root.style.setProperty("--stage-chrome", Math.ceil(room) + "px");
  }
  var fitFrame = 0;
  function refit() {
    if (state.hold) state.hold.reset();
    if (fitFrame) return;
    fitFrame = window.requestAnimationFrame(function() { fitFrame = 0; fit(); });
  }
  // The caption and the numbers change while the animation runs; a longer
  // caption may wrap. The strip keeps its tallest height so the stage never
  // grows and shrinks with the narration (at most it shrinks once).
  function holdHeight(el) {
    if (typeof ResizeObserver !== "function") return null;
    var tallest = 0;
    var observer = new ResizeObserver(function() {
      var height = el.getBoundingClientRect().height;
      if (height > tallest + 0.5) {
        tallest = height;
        el.style.minHeight = Math.ceil(height) + "px";
      }
    });
    observer.observe(el);
    return {
      reset: function() { tallest = 0; el.style.minHeight = ""; },
      stop: function() { observer.disconnect(); }
    };
  }

  function move(node, into) {
    if (!node || !node.parentNode) return;
    var mark = document.createComment("lesson focus");
    node.parentNode.insertBefore(mark, node);
    state.moved.push([node, mark]);
    into.appendChild(node);
  }
  function part(name) {
    var el = document.createElement("div");
    el.className = name;
    return el;
  }
  function syncButton() {
    var button = document.getElementById("btn-fullscreen");
    if (!button) return;
    var label = button.querySelector(".btn-label");
    if (label) label.textContent = state.on ? "Exit full screen" : "Full screen";
    button.setAttribute("aria-label", state.on ? "Leave full screen" : "Show the lesson animation full screen");
  }
  function setDetails(open) {
    if (open) root.setAttribute("data-focus-details", "true");
    else root.removeAttribute("data-focus-details");
    // The stage changes width with Details: measure the strip again.
    if (state.hold) state.hold.reset();
    var button = document.getElementById("btn-details");
    if (button) button.setAttribute("aria-expanded", open ? "true" : "false");
  }
  // Moving a node blurs it; give focus back to the control that had it, but
  // only while the reader is in this page: when the workspace has taken focus
  // back (its Full screen button), focusing here would pull it into the frame.
  // A control that the layout change hides (Details) hands focus to Full
  // screen, the control that toggles the layout.
  function keepFocus(run) {
    var active = document.hasFocus() ? document.activeElement : null;
    run();
    if (!active || active === document.body || document.activeElement === active) return;
    var target = active.isConnected && active.getClientRects().length ? active : document.getElementById("btn-fullscreen");
    if (target && target.getClientRects().length && typeof target.focus === "function") {
      target.focus({ preventScroll: true });
    }
  }

  function enter() {
    if (state.on) return;
    var toolbar = document.querySelector(".stage-toolbar");
    var svgs = stageSvgs();
    var stage = document.querySelector(".stage-with-phases") || (svgs[0] && svgs[0].parentElement);
    if (!toolbar || !stage) return;
    keepFocus(function() {
      state.on = true;
      var shell = part("focus-shell");
      var top = part("focus-top");
      var scene = part("focus-stage");
      var strip = part("focus-strip");
      var caption = part("focus-caption");
      shell.setAttribute("role", "region");
      shell.setAttribute("aria-label", "Lesson animation");
      move(toolbar, top);
      move(document.querySelector(".stage-scrubber"), top);
      // Guided lessons: Back, Next step, Explore and Check yourself.
      move(document.querySelector(".guide-bar"), top);
      move(stage, scene);
      var label = toolbar.querySelector(".phase-label");
      if (label) {
        move(label, caption);
        strip.appendChild(caption);
      }
      move(document.querySelector("section.readout .readout-grid") || document.querySelector(".readout-grid"), strip);
      shell.appendChild(top);
      shell.appendChild(scene);
      if (strip.firstChild) shell.appendChild(strip);
      document.body.insertBefore(shell, document.body.firstChild);
      state.shell = shell;
      if (strip.parentNode) state.hold = holdHeight(strip);
      root.setAttribute("data-focus", "true");
      syncButton();
    });
  }
  function leave() {
    if (!state.on) return;
    keepFocus(function() {
      state.on = false;
      setDetails(false);
      if (state.hold) state.hold.stop();
      state.hold = null;
      for (var i = state.moved.length - 1; i >= 0; i--) {
        var node = state.moved[i][0];
        var mark = state.moved[i][1];
        if (mark.parentNode) {
          mark.parentNode.insertBefore(node, mark);
          mark.parentNode.removeChild(mark);
        }
      }
      state.moved = [];
      if (state.shell && state.shell.parentNode) state.shell.parentNode.removeChild(state.shell);
      state.shell = null;
      root.removeAttribute("data-focus");
      syncButton();
    });
    fit();
  }
  // Leaving at the reader's request: the workspace owns full screen for an
  // embedded lesson; a standalone page leaves the browser's full screen.
  function exit() {
    if (embedded()) {
      leave();
      window.parent.postMessage({ type: MESSAGE, focus: false }, "*");
      return;
    }
    var wasNative = state.native;
    state.native = false;
    leave();
    if (wasNative && document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(function() {});
    }
  }
  // Standalone: the browser's full screen when allowed, else the same layout
  // filling the window.
  function request() {
    if (state.on) {
      exit();
      return;
    }
    if (!embedded() && document.fullscreenEnabled && root.requestFullscreen) {
      root.requestFullscreen().then(function() {
        state.native = true;
        enter();
      }, function() {
        enter();
      });
    } else {
      enter();
    }
  }

  var fullButton = document.getElementById("btn-fullscreen");
  if (fullButton) fullButton.addEventListener("click", request);
  var detailsButton = document.getElementById("btn-details");
  if (detailsButton) detailsButton.addEventListener("click", function() {
    setDetails(root.getAttribute("data-focus-details") !== "true");
  });
  document.addEventListener("fullscreenchange", function() {
    if (!document.fullscreenElement && state.native) {
      state.native = false;
      leave();
    }
  });
  // Escape closes Details first, then leaves full screen. Keys reach this
  // document only while focus is inside the lesson.
  document.addEventListener("keydown", function(event) {
    if (event.key !== "Escape" || !state.on || event.defaultPrevented) return;
    event.preventDefault();
    if (root.getAttribute("data-focus-details") === "true") {
      setDetails(false);
      if (detailsButton) detailsButton.focus({ preventScroll: true });
      return;
    }
    exit();
  });
  window.addEventListener("message", function(event) {
    var data = event.data;
    if (window.parent === window || event.source !== window.parent) return;
    if (!data || data.type !== MESSAGE || typeof data.focus !== "boolean") return;
    if (data.focus) enter();
    else leave();
  });
  window.addEventListener("resize", refit);
  window.addEventListener("load", refit);
  // In-page links (About this example) scroll this page and move focus to
  // the section's heading. In the workspace a lesson is an srcdoc frame whose
  // base address is the workspace's own, so a plain "#id" link would load
  // that address in the frame instead.
  document.addEventListener("click", function(event) {
    var link = event.target && event.target.closest ? event.target.closest('a[href^="#"]') : null;
    if (!link || event.defaultPrevented) return;
    var target = document.getElementById(link.getAttribute("href").slice(1));
    if (!target) return;
    event.preventDefault();
    var heading = target.querySelector("h2") || target;
    if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
    heading.focus({ preventScroll: true });
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
  });
  fit();
  syncButton();

  return { enter: enter, leave: leave, fit: fit, refit: refit, isOn: function() { return state.on; } };
})();
"""
_BASE_JS = _THEME_JS + """
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
  // Guided steps (lessons whose timeline marks beats with tl.beat):
  //   *       → Next   → PLAYING one beat → HELD at the beat's end
  //   *       → Back   → HELD at the end of the previous beat
  //   HELD    → Play   → PLAYING  (continues from the held beat)
  // =====================================================================
  var _ctrl = {
    build: null,         // fn → returns fresh anim.timeline()
    reset: null,         // fn → clears stage to ready state
    legacyPlay: null,    // fn → old-style play callback
    tl: null,            // current anim.timeline()
    phase: "idle",       // "idle" | "playing" | "paused" | "seeked" | "held"
    seekMs: 0,           // ms position we seeked to (valid when phase=seeked)
    loopEnabled: false,
    autoplay: false,     // true when Play runs on; false while stepping
    rafId: 0,            // current RAF id (0 = none)
    dragging: false,     // true while scrubber thumb is held
    // Phase nav
    phaseContainer: null,
    marks: []
  };
  // User-calibrated baseline: the dropdown's "1×" maps to this internal
  // speed. Started at 0.52 (original); user asked for 40% slower →
  // 0.312 felt too slow for state-swap lessons (icp etc.) where flat
  // tl.delay times then stretched to 3× wallclock and looked frozen.
  // 0.42 is a compromise: ~20% slower than the original 0.52 baseline
  // — tweened animations still breathe, stepped ones stay responsive.
  var BASELINE_SPEED_SCALE = 0.42;

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
    var label = _ctrl.phase === "idle" || _ctrl.phase === "held" ? "Play"
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
    _notifyStep();
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
    _notifyStep();
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
    _ctrl.autoplay = true;
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
    if (_ctrl.loopEnabled && _ctrl.autoplay) { _playFrom(0); return; }
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
    if (!_ctrl.marks.length) {
      c.innerHTML = "";
      c.hidden = true;
      return;
    }
    c.hidden = false;
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
    var tl = _ctrl.tl;
    if (tl && tl.getCursor && typeof _ctrl.marks[0].step === "number") {
      // By steps begun: held at a boundary, the next phase has not started.
      var cursor = tl.getCursor();
      for (var j = _ctrl.marks.length - 1; j > 0; j--) {
        if (cursor > _ctrl.marks[j].step) { active = j; break; }
      }
    } else {
      for (var i = _ctrl.marks.length - 1; i >= 0; i--) {
        if (currentMs >= _ctrl.marks[i].ms) { active = i; break; }
      }
    }
    _highlightPhaseIdx(active);
  }

  // --- Guided steps (beats) ---
  // A lesson marks each step of its timeline with tl.beat(kind). Next plays
  // one step and holds at its end; Back shows the end of the previous step;
  // stepTo(k) shows the end of step k. Step -1 is the ready state.
  var _steps = { listeners: [], cache: null, sig: "" };

  // Beats of the current timeline, or of a fresh one when none is built yet
  // (build() only resets the stage, so the ready state is unchanged).
  function _beatList() {
    if (_ctrl.tl && _ctrl.tl.getBeats) return _ctrl.tl.getBeats();
    if (_steps.cache) return _steps.cache;
    if (!_ctrl.build) return [];
    var tl = _ctrl.build();
    _steps.cache = tl.getBeats ? tl.getBeats() : [];
    return _steps.cache;
  }
  // The last beat whose first step has begun; -1 before the first.
  function _beatIndex() {
    var tl = _ctrl.tl;
    if (!tl || !tl.getCursor) return -1;
    var beats = tl.getBeats();
    var cursor = tl.getCursor();
    var k = -1;
    for (var i = 0; i < beats.length; i++) {
      if (cursor > (i === 0 ? 0 : beats[i].step)) k = i;
      else break;
    }
    return k;
  }
  function _finished() {
    var tl = _ctrl.tl;
    return !!tl && !!tl.getCursor && _ctrl.phase !== "playing" && _ctrl.phase !== "paused" &&
      tl.getCursor() >= tl.getStepCount();
  }
  function stepState() {
    var beats = _beatList();
    var k = _beatIndex();
    return {
      beat: k,
      count: beats.length,
      kind: k >= 0 && beats[k] ? beats[k].kind : "",
      phase: _ctrl.phase,
      finished: _finished()
    };
  }
  function _notifyStep() {
    if (!_steps.listeners.length) return;
    var state = stepState();
    var sig = [state.beat, state.count, state.kind, state.phase, state.finished].join("|");
    if (sig === _steps.sig) return;
    _steps.sig = sig;
    for (var i = 0; i < _steps.listeners.length; i++) {
      try { _steps.listeners[i](state); } catch (e) {}
    }
  }
  function onStep(fn) {
    _steps.listeners.push(fn);
    _steps.sig = "";
    _notifyStep();
  }

  function _resetAll() {
    _stopAll();
    if (_ctrl.reset) _ctrl.reset();
    if (_ctrl.phaseContainer) {
      _ctrl.phaseContainer.innerHTML = "";
      _ctrl.phaseContainer.hidden = true;
    }
    _syncUI();
  }

  // Rebuild and show the stage with steps [0, n) applied, held before step n.
  // `n` may be a function of the new timeline: the inputs may have changed
  // its steps since the last build.
  function _holdAtStep(n) {
    _stopAll();
    if (_ctrl.reset) _ctrl.reset();
    if (!_ctrl.build) return;
    _ctrl.tl = _ctrl.build();
    _steps.cache = null;
    _ctrl.tl.fastForwardToStep(typeof n === "function" ? n(_ctrl.tl) : n);
    _ctrl.phase = _ctrl.tl.getCursor() >= _ctrl.tl.getStepCount() ? "idle" : "held";
    anim.setPaused(false);
    _renderPhaseNav();
    _syncUI();
    _updatePhaseHighlight(_ctrl.tl.getCurrentTime());
    if (_ctrl.phase === "idle" && _dom.scrubber) _dom.scrubber.value = "1000";
  }
  function stepTo(k) {
    if (!_ctrl.build) return;
    if (k < 0) { _resetAll(); return; }
    _holdAtStep(function(tl) {
      var beats = tl.getBeats();
      if (!beats.length) return 0;
      var last = Math.min(k, beats.length - 1);
      return last + 1 < beats.length ? beats[last + 1].step : tl.getStepCount();
    });
  }
  function _onHold() {
    _ctrl.phase = "held";
    _tickGen++;
    if (_ctrl.rafId) { cancelAnimationFrame(_ctrl.rafId); _ctrl.rafId = 0; }
    _syncUI();
    if (_ctrl.tl) _updatePhaseHighlight(_ctrl.tl.getCurrentTime());
  }
  // Animate beat k live from the held end of beat k - 1, then hold.
  function _playBeat(k) {
    var tl = _ctrl.tl;
    var beats = tl.getBeats();
    var end = k + 1 < beats.length ? beats[k + 1].step : -1;
    _ctrl.phase = "playing";
    _ctrl.autoplay = false;
    anim.setPaused(false);
    if (!tl.continueTo(end, _onHold, _onDone)) {
      stepTo(k);
      return;
    }
    if (_ctrl.phase === "playing") {
      _syncUI();
      _startTick();
    }
  }
  function stepNext() {
    if (!_ctrl.build) return;
    var beats = _beatList();
    if (!beats.length) return;
    var k = _beatIndex();
    if (_ctrl.phase === "held" && _ctrl.tl && _ctrl.tl.isHeld()) {
      if (k + 1 < beats.length) _playBeat(k + 1);
      return;
    }
    if (_ctrl.phase === "idle" && !_ctrl.tl) {
      _stopAll();
      if (_ctrl.reset) _ctrl.reset();
      _ctrl.tl = _ctrl.build();
      _steps.cache = null;
      _ctrl.tl.fastForwardToStep(0);
      _renderPhaseNav();
      _playBeat(0);
      return;
    }
    if (_finished()) return;
    // Playing, paused or scrubbed inside a step: finish that step at once.
    stepTo(Math.max(0, k));
  }
  function stepBack() {
    if (!_ctrl.build) return;
    var k = _beatIndex();
    if (k < 0) return;
    stepTo(k - 1);
  }
  // Play step k again from its start.
  function replayStep() {
    if (!_ctrl.build) return;
    var k = Math.max(0, _beatIndex());
    if (k === 0) {
      _resetAll();
      stepNext();
      return;
    }
    stepTo(k - 1);
    if (_ctrl.phase === "held") _playBeat(k);
  }
  // The lesson's inputs changed (it already redrew its stage): rebuild the
  // timeline with the new values and show the same step again.
  function inputsChanged() {
    _steps.cache = null;
    if (!_ctrl.tl) { _syncUI(); return; }
    var k = _beatIndex();
    if (k < 0) { _resetAll(); return; }
    stepTo(k);
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
        case "held":
          // Run on from the end of the step shown (guided lessons).
          if (_ctrl.tl && _ctrl.tl.isHeld && _ctrl.tl.isHeld()) {
            _ctrl.phase = "playing";
            _ctrl.autoplay = true;
            anim.setPaused(false);
            _ctrl.tl.continueTo(-1, null, _onDone);
            if (_ctrl.phase === "playing") {
              _syncUI();
              _startTick();
            }
          } else {
            _playFrom(_ctrl.tl ? _ctrl.tl.getCurrentTime() : 0);
          }
          break;
      }
    });

    // --- Reset button ---
    if (_dom.btnReset) _dom.btnReset.addEventListener("click", _resetAll);

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

    // Static var() attributes follow CSS automatically. Replaying to the same
    // playhead also refreshes colors materialized by an interpolation.
    function redrawAppearance() {
      var phase = _ctrl.phase;
      if (phase === "held" && _ctrl.tl) {
        _holdAtStep(_ctrl.tl.getCursor());
        return;
      }
      var position = _ctrl.tl ? _ctrl.tl.getCurrentTime() : 0;
      if (phase === "seeked") position = _ctrl.seekMs;
      var completed = phase === "idle" && _ctrl.tl;
      if (completed) position = _ctrl.tl.getTotalDuration();
      if (phase === "playing") {
        _playFrom(position);
      } else if (phase === "paused" || phase === "seeked" || completed) {
        _seek(position);
        if (completed) {
          _ctrl.phase = "idle";
          anim.setPaused(false);
          _syncUI();
        }
      } else {
        _stopAll();
        if (_ctrl.reset) _ctrl.reset();
        _syncUI();
      }
    }
    var appearance = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    if (appearance) {
      if (appearance.addEventListener) appearance.addEventListener("change", redrawAppearance);
      else if (appearance.addListener) appearance.addListener(redrawAppearance);
    }
    if (typeof MutationObserver === "function") {
      new MutationObserver(redrawAppearance).observe(document.documentElement, {
        attributes: true, attributeFilter: ["data-theme"]
      });
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
    container.innerHTML = "";
    container.hidden = true;
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
    onStep: onStep,
    stepState: stepState,
    stepNext: stepNext,
    stepBack: stepBack,
    stepTo: stepTo,
    replayStep: replayStep,
    inputsChanged: inputsChanged,
    _updatePhaseNav: _updatePhaseNav,
    _tickPhaseNav: _tickPhaseNav,
    animationDone: animationDone
  };
})();
""" + _FOCUS_JS


# Guided lessons, run after the lesson's own script (it has wired the toolbar
# by then). Reads the lesson's guide content from #lesson-guide (JSON written
# by render_page) and builds the step bar, the simplified code beside the
# drawing, Explore and the Check yourself quiz with textContent only. A
# lesson without guide content has no #lesson-guide and is left as it was.
_GUIDE_JS = """
var teachGuide = (function() {
  var source = document.getElementById("lesson-guide");
  if (!source || typeof teachRuntime === "undefined" || !teachRuntime.onStep) return null;
  var guide;
  try { guide = JSON.parse(source.textContent); } catch (e) { return null; }
  var toolbar = document.querySelector(".stage-toolbar");
  var stageRow = document.querySelector(".stage-with-phases");
  if (!guide || !toolbar || !stageRow) return null;
  var root = document.documentElement;
  var beatLines = guide.beats || {};
  var questions = guide.quiz || [];
  var state = { beat: -1, count: 0, kind: "", phase: "idle", finished: false };
  var complete = false;
  var exploring = false;
  root.setAttribute("data-guided", "true");

  function node(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  function button(text, cls, label) {
    var el = node("button", cls || "");
    el.type = "button";
    el.appendChild(node("span", "btn-label", text));
    if (label) el.setAttribute("aria-label", label);
    return el;
  }
  function refit() {
    if (typeof teachFocus !== "undefined" && teachFocus) teachFocus.fit();
  }

  // --- Step bar: Back, Next step, the step count, Check yourself, Explore.
  // Explore adds a slider over every step, Replay step and the inputs.
  var bar = node("div", "guide-bar");
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", "Lesson steps");
  var back = button("Back", "guide-back", "Back one step");
  var next = button("Next step", "guide-next primary");
  var range = node("input", "guide-beat guide-explore-only");
  range.type = "range";
  range.min = "0";
  range.max = "0";
  range.step = "1";
  range.value = "0";
  range.setAttribute("aria-label", "Step");
  var count = node("span", "guide-count");
  var replay = button("Replay step", "guide-replay guide-explore-only");
  var ends = node("span", "guide-end");
  var check = questions.length ? button("Check yourself", "guide-check") : null;
  if (check) check.hidden = true;
  var explore = button("Explore", "guide-explore");
  explore.setAttribute("aria-pressed", "false");
  explore.title = "Step freely through the lesson and change its inputs";
  var live = node("span", "guide-live");
  live.setAttribute("aria-live", "polite");
  bar.appendChild(back);
  bar.appendChild(next);
  bar.appendChild(range);
  bar.appendChild(count);
  bar.appendChild(replay);

  var controls = document.querySelector("section.controls");
  var inputsButton = null;
  var inputsBox = null;
  var inputsMark = null;
  if (controls && controls.querySelector("input, select")) {
    inputsButton = button("Inputs", "guide-inputs-toggle guide-explore-only");
    inputsButton.setAttribute("aria-expanded", "false");
    inputsButton.setAttribute("aria-controls", "guide-inputs");
    inputsBox = node("div", "guide-inputs");
    inputsBox.id = "guide-inputs";
    inputsBox.hidden = true;
    bar.appendChild(inputsButton);
    // The lesson's own listeners on each input redraw its stage first; this
    // one (bubbling to the section) then shows the same step again with the
    // new values.
    var changed = function() { teachRuntime.inputsChanged(); };
    controls.addEventListener("input", changed);
    controls.addEventListener("change", changed);
  }
  if (check) ends.appendChild(check);
  ends.appendChild(explore);
  bar.appendChild(ends);
  bar.appendChild(live);
  if (inputsBox) bar.appendChild(inputsBox);
  var scrubber = document.querySelector(".stage-scrubber");
  var anchor = scrubber && scrubber.parentNode ? scrubber : toolbar;
  anchor.parentNode.insertBefore(bar, anchor.nextSibling);
  // Guided first: Next step is the main action, Play runs the rest.
  var play = document.getElementById("btn-play");
  if (play) play.classList.remove("primary");

  // --- Side column: the simplified code, then the lesson's phases.
  var side = node("aside", "stage-side");
  side.setAttribute("aria-label", "Lesson guide");
  var lineEls = [];
  var code = guide.code;
  if (code && code.lines && code.lines.length) {
    var panel = node("section", "code-panel");
    var head = node("div", "code-head");
    var toggle = node("button", "code-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "true");
    toggle.setAttribute("aria-controls", "code-body");
    var chevron = node("span", "code-chevron");
    chevron.setAttribute("aria-hidden", "true");
    toggle.appendChild(chevron);
    var title = node("span", "", code.title);
    title.id = "code-title";
    toggle.appendChild(title);
    head.appendChild(toggle);
    head.appendChild(node("span", "code-tag", "Simplified"));
    panel.setAttribute("aria-labelledby", "code-title");
    var body = node("div", "code-body");
    body.id = "code-body";
    var list = node("ol", "code-lines");
    var named = false;
    for (var i = 0; i < code.lines.length; i++) {
      var line = code.lines[i];
      var li = node("li", "code-line");
      li.setAttribute("data-line", line.id);
      li.style.setProperty("--indent", String(line.indent || 0));
      li.appendChild(node("span", "code-text", line.text));
      if (line.fn) {
        // A long Class::method name wraps after "::" first; a part still
        // wider than the column then wraps after an underscore, never
        // inside a word.
        var fn = node("span", "code-fn");
        var parts = String(line.fn).split("::");
        for (var p = 0; p < parts.length; p++) {
          var part = node("span", "code-fn-part");
          var words = (parts[p] + (p < parts.length - 1 ? "::" : "")).split("_");
          for (var w = 0; w < words.length; w++) {
            if (w) {
              part.appendChild(document.createTextNode("_"));
              part.appendChild(document.createElement("wbr"));
            }
            part.appendChild(document.createTextNode(words[w]));
          }
          fn.appendChild(part);
        }
        li.appendChild(fn);
        named = true;
      }
      list.appendChild(li);
      lineEls.push(li);
    }
    body.appendChild(list);
    if (named) {
      body.appendChild(node("p", "code-note",
        "Grey names are the MySQL source functions that do the step."));
    }
    panel.appendChild(head);
    panel.appendChild(body);
    toggle.addEventListener("click", function() {
      var open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      body.hidden = !open;
      panel.classList.toggle("is-collapsed", !open);
      side.classList.toggle("is-collapsed", !open);
      refit();
    });
    side.appendChild(panel);
  }
  var phaseNav = stageRow.querySelector(".phase-nav");
  if (phaseNav) side.appendChild(phaseNav);
  stageRow.appendChild(side);

  function highlight(kind) {
    var ids = (kind && beatLines[kind]) || [];
    var first = null;
    for (var i = 0; i < lineEls.length; i++) {
      var on = ids.indexOf(lineEls[i].getAttribute("data-line")) >= 0;
      lineEls[i].classList.toggle("is-active", on);
      if (on) {
        lineEls[i].setAttribute("aria-current", "step");
        if (!first) first = lineEls[i];
      } else {
        lineEls[i].removeAttribute("aria-current");
      }
    }
    // Keep the line in view inside the side column, never scrolling the page.
    if (first && side.scrollHeight > side.clientHeight + 1) {
      var top = first.offsetTop;
      var bottom = top + first.offsetHeight;
      if (top < side.scrollTop) side.scrollTop = Math.max(0, top - 8);
      else if (bottom > side.scrollTop + side.clientHeight) side.scrollTop = bottom - side.clientHeight + 8;
    }
  }
  function describe(s) {
    if (!s.count) return "";
    if (s.beat < 0) return s.count + " steps";
    if (s.finished && s.beat >= s.count - 1) return "Step " + s.count + " of " + s.count + ", done";
    return "Step " + (s.beat + 1) + " of " + s.count;
  }
  var phaseLabel = document.getElementById("phase-label");
  var said = "";
  function announce(text) {
    if (!text || text === said) return;
    said = text;
    live.textContent = text;
  }
  // Disabling the focused button would drop focus to the page: hand it on.
  function disable(el, off, fallback) {
    if (off && document.activeElement === el && fallback && !fallback.hidden && !fallback.disabled) {
      fallback.focus();
    }
    el.disabled = off;
  }
  function update(s) {
    state = s;
    highlight(s.beat >= 0 ? s.kind : "");
    var text = describe(s);
    count.textContent = text;
    var done = s.finished && s.beat >= s.count - 1;
    if (done && !complete) {
      complete = true;
      if (check) check.hidden = false;
      announce("End of the lesson. " + (check ? "Check yourself, or explore freely." : "Explore freely."));
    } else if (s.phase === "held" || (s.finished && s.beat >= 0)) {
      announce(text + ". " + (phaseLabel ? phaseLabel.textContent : ""));
    }
    disable(back, s.beat < 0, next);
    disable(next, !s.count || done, check || explore);
    range.max = String(s.count);
    range.value = String(Math.max(0, s.beat + 1));
    range.setAttribute("aria-valuetext", text || "Ready");
    replay.disabled = s.beat < 0;
  }

  function setInputs(open) {
    if (!inputsButton || !controls) return;
    if (open && !inputsMark && controls.parentNode) {
      inputsMark = document.createComment("lesson inputs");
      controls.parentNode.insertBefore(inputsMark, controls);
      inputsBox.appendChild(controls);
    } else if (!open && inputsMark) {
      if (inputsMark.parentNode) {
        inputsMark.parentNode.insertBefore(controls, inputsMark);
        inputsMark.parentNode.removeChild(inputsMark);
      }
      inputsMark = null;
    }
    inputsBox.hidden = !open;
    inputsButton.setAttribute("aria-expanded", open ? "true" : "false");
    refit();
  }
  // Explore: the slider over every step replaces the time scrubber, with
  // Replay step and the inputs. Leaving keeps the step that is shown, so the
  // guided steps continue from there.
  function setExplore(on) {
    exploring = !!on;
    if (exploring) root.setAttribute("data-explore", "true");
    else root.removeAttribute("data-explore");
    explore.setAttribute("aria-pressed", exploring ? "true" : "false");
    if (!exploring) setInputs(false);
    refit();
  }

  back.addEventListener("click", function() { teachRuntime.stepBack(); });
  next.addEventListener("click", function() { teachRuntime.stepNext(); });
  replay.addEventListener("click", function() { teachRuntime.replayStep(); });
  range.addEventListener("input", function() { teachRuntime.stepTo(Number(range.value) - 1); });
  explore.addEventListener("click", function() { setExplore(!exploring); });
  if (inputsButton) inputsButton.addEventListener("click", function() {
    setInputs(inputsButton.getAttribute("aria-expanded") !== "true");
  });

  // --- Check yourself: multiple choice with an answer and its reason at
  // once. Answers live only in this page; nothing is saved or sent.
  var dialog = null;
  function closeQuiz() {
    if (!dialog || !dialog.open) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }
  function openQuiz() {
    if (!dialog) return;
    if (!dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }
    var first = dialog.querySelector("input:checked") || dialog.querySelector("input");
    if (first) first.focus();
  }
  if (questions.length) {
    dialog = node("dialog", "quiz-dialog");
    dialog.setAttribute("aria-labelledby", "quiz-title");
    var qhead = node("div", "quiz-head");
    var qtitle = node("h2", "", "Check yourself");
    qtitle.id = "quiz-title";
    var close = button("Close", "quiz-close");
    qhead.appendChild(qtitle);
    qhead.appendChild(close);
    var qbody = node("div", "quiz-body");
    qbody.appendChild(node("p", "quiz-intro", questions.length +
      " questions about this lesson. Pick an answer to see whether it is right and why. Nothing is saved."));
    var results = [];
    var tally = node("p", "quiz-tally");
    tally.setAttribute("aria-live", "polite");
    var updateTally = function() {
      var answered = 0, right = 0;
      for (var r = 0; r < questions.length; r++) {
        if (results[r] === undefined) continue;
        answered++;
        if (results[r]) right++;
      }
      tally.textContent = answered ? right + " of " + questions.length + " right" : "";
    };
    questions.forEach(function(q, qi) {
      var set = node("fieldset", "quiz-q");
      set.appendChild(node("legend", "", (qi + 1) + ". " + q.question));
      var feedback = node("p", "quiz-feedback");
      feedback.setAttribute("aria-live", "polite");
      feedback.id = "quiz-feedback-" + qi;
      var choices = [];
      q.choices.forEach(function(choice, ci) {
        var label = node("label", "quiz-choice");
        var input = node("input");
        input.type = "radio";
        input.name = "quiz-" + qi;
        input.value = String(ci);
        input.setAttribute("aria-describedby", feedback.id);
        var mark = node("span", "quiz-mark");
        mark.setAttribute("aria-hidden", "true");
        label.appendChild(input);
        label.appendChild(node("span", "quiz-text", choice.text));
        label.appendChild(mark);
        input.addEventListener("change", function() {
          for (var c = 0; c < choices.length; c++) {
            choices[c].label.classList.remove("is-right", "is-wrong");
            choices[c].mark.textContent = "";
          }
          label.classList.add(choice.correct ? "is-right" : "is-wrong");
          mark.textContent = choice.correct ? "\\u2713" : "\\u2717";
          feedback.textContent = "";
          feedback.appendChild(node("span", choice.correct ? "verdict-right" : "verdict-wrong",
            choice.correct ? "Right. " : "Not quite. "));
          feedback.appendChild(document.createTextNode(choice.why));
          results[qi] = !!choice.correct;
          updateTally();
        });
        choices.push({ label: label, mark: mark });
        set.appendChild(label);
      });
      set.appendChild(feedback);
      qbody.appendChild(set);
    });
    var qfoot = node("div", "quiz-foot");
    var freely = button("Explore freely", "quiz-explore");
    qfoot.appendChild(tally);
    qfoot.appendChild(freely);
    dialog.appendChild(qhead);
    dialog.appendChild(qbody);
    dialog.appendChild(qfoot);
    document.body.appendChild(dialog);
    close.addEventListener("click", closeQuiz);
    freely.addEventListener("click", function() {
      closeQuiz();
      setExplore(true);
      explore.focus();
    });
    // Escape closes the quiz only, not the lesson's full screen.
    dialog.addEventListener("keydown", function(event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      closeQuiz();
    });
    dialog.addEventListener("cancel", function(event) {
      event.preventDefault();
      closeQuiz();
    });
    if (check) check.addEventListener("click", openQuiz);
  }

  if (typeof ResizeObserver === "function") new ResizeObserver(refit).observe(bar);
  teachRuntime.onStep(update);
  refit();
  return {
    setExplore: setExplore,
    isExploring: function() { return exploring; },
    openQuiz: openQuiz,
    closeQuiz: closeQuiz,
    state: function() { return state; }
  };
})();
"""


def load_lesson_js(module_file: str, js_name: str = None) -> str:
    """Load a lesson's JavaScript from a sibling ``.js`` file on disk.

    Tier-0 authoring ergonomics: each lesson's JS lives in a real
    ``.js`` file next to its ``.py``, not inside a Python raw-string
    literal. Editors highlight the syntax, eslint/prettier can touch
    it, and the whole class of ``%% vs %`` escape bugs goes away.

    Usage from a lesson module::

        from .. import _html
        _LESSON_JS = _html.load_lesson_js(__file__)

    The helper looks up ``<same-dir>/<same-basename>.js`` by default.
    Pass ``js_name`` to override (e.g. when a lesson bundles multiple
    JS files).
    """
    import os as _os
    if js_name is None:
        base = _os.path.splitext(_os.path.basename(module_file))[0]
        js_name = base + ".js"
    js_path = _os.path.join(_os.path.dirname(_os.path.abspath(module_file)),
                             js_name)
    with open(js_path, "r", encoding="utf-8") as f:
        return f.read()


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

    Orients first-time viewers: it names every shape, color and phase.
    ``render_page`` shows it in the "About this example" section right
    under the stage, beside the query card. Every lesson should have one;
    the animation-expert skill flags 'silent phase transitions' as a top
    ugliness signal.
    """
    # Lesson authors use code spans for SQL terms. Escape everything first,
    # then allow only these two exact tags; other markup stays inert text.
    items = "".join(
        "<li>" + esc(b).replace("&lt;code&gt;", "<code>")
        .replace("&lt;/code&gt;", "</code>") + "</li>"
        for b in bullets
    )
    return f"""
<div class="explainer-card">
  <p class="explainer-title">{esc(title)}</p>
  <ul class="explainer-list">{items}</ul>
</div>
"""


def query_card(sql: str, note: str = "") -> str:
    """Render a small SQL query card for a lesson.

    Uses real table names so the animation tracks something the user
    actually recognises from their own schemas. The SQL is the query
    that the lesson's algorithm would be asked to execute. Wherever a
    lesson places it, ``render_page`` moves it into the "About this
    example" section right under the stage.
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
    return f'<div id="{esc(nav_id)}" class="phase-nav" hidden></div>'


def stage_toolbar(status_text: str = "Ready — press Play") -> str:
    """Render the shared stage toolbar: play/pause toggle, speed dropdown,
    reset button, a scrubber (YouTube-style timeline seek), and a
    status-label span. Every lesson uses this.

    It also carries Full screen (shown on a standalone page; the workspace
    has its own button for an embedded lesson) and Details, which appears
    only in the full-screen focus layout and opens the parameters, query and
    explainer beside the stage."""
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
  <button id="btn-details" type="button" class="focus-only" aria-expanded="false" aria-controls="lesson-main">
    <span class="btn-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z"/></svg>
    </span>
    <span class="btn-label">Details</span>
  </button>
  <button id="btn-fullscreen" type="button" aria-label="Show the lesson animation full screen">
    <span class="btn-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path class="icon-enter" d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/><path class="icon-exit" d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg>
    </span>
    <span class="btn-label">Full screen</span>
  </button>
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
    "bnl": "Join operator",
    "hash": "Join operator",
    "join": "Join operator",
    "nested_loop": "Join operator",
    "full_scan": "Scan operator",
    "filter": "Filter operator",
    "filesort": "Sort operator",
    "tmp": "Temp operator",
    "btree": "Index access",
    "non_unique_lookup": "Index access",
    "unique_lookup": "Index access",
    "icp": "Index access",
    "index_merge": "Index access",
    "skip_scan": "Index access",
    "rowid_filter": "Index access",
    "bka_join": "Join operator",
    "semijoin_firstmatch": "Join operator",
    "semijoin_weedout": "Join operator",
    "lru": "Buffer cache",
    "derived_table": "Temp operator",
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
          "controls_html": …,  # query card + explainer + toolbar (render_page
                               # moves the two cards under the stage)
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


# ---- Guided lessons: simplified code, steps and Check yourself ----------
#
# A lesson opts in by passing ``guide=GUIDE`` to ``render_page`` and marking
# each step of its timeline with ``tl.beat("<kind>")`` in its .js file. The
# content is declarative (see docs/_lane_notes/LEARN.md for the format):
#
#   GUIDE = {
#       "code": {
#           "title": "How InnoDB finds the row",
#           # (line id, indent, text, MySQL function or ""), at most 14 lines
#           "lines": [("root", 0, "page = root page of the index",
#                      "btr_cur_search_to_nth_level"), ...],
#       },
#       # beat kind (tl.beat) -> the line ids that step runs
#       "beats": {"root": ["root"], ...},
#       # two to four questions, each with two to four choices, one right,
#       # and a one-sentence reason for every choice
#       "quiz": [{"question": "...",
#                 "choices": [("text", True, "why"), ("text", False, "why")]}],
#   }

#: MySQL functions a guide may print beside a code line. Each one was found
#: in the mysql-server source, both the mysql-8.4.9 tag and 9.7.0, before it
#: was listed (file given for the next reader). Add a name here only after
#: checking it the same way; normalize_guide refuses any other name.
VERIFIED_SERVER_FUNCTIONS = {
    "btr_cur_search_to_nth_level": "storage/innobase/btr/btr0cur.cc",
    "page_cur_search_with_match": "storage/innobase/page/page0cur.cc",
    "row_build_row_ref_in_tuple": "storage/innobase/row/row0row.cc",
    "Row_sel_get_clust_rec_for_mysql": "storage/innobase/row/row0sel.cc",
    "ha_innobase::rnd_init": "storage/innobase/handler/ha_innodb.cc",
    "ha_innobase::rnd_next": "storage/innobase/handler/ha_innodb.cc",
    # 8.4 overrides Read(); 9.x keeps Read() on RowIterator and the body in DoRead().
    "FilterIterator::Read": "sql/iterators/composite_iterators.cc",
    "NestedLoopIterator::Read": "sql/iterators/composite_iterators.cc",
    # Index access. Iterator Read()/Init() follow the same 8.4 / 9.x rule
    # (9.x bodies are DoRead()/DoInit()).
    "IndexSkipScanIterator::Read": "sql/range_optimizer/index_skip_scan.cc",
    "index_next_different": "sql/range_optimizer/range_optimizer.cc",
    "RowIDUnionIterator::Read": "sql/range_optimizer/rowid_ordered_retrieval.cc",
    "RowIDIntersectionIterator::Read": "sql/range_optimizer/rowid_ordered_retrieval.cc",
    "IndexMergeIterator::Init": "sql/range_optimizer/index_merge.cc",
    "innobase_index_cond": "storage/innobase/handler/ha_innodb.cc",
    "row_search_idx_cond_check": "storage/innobase/row/row0sel.cc",
    "EQRefIterator::Read": "sql/iterators/ref_row_iterators.cc",
    "read_const": "sql/sql_executor.cc",
    "ha_innobase::index_next_same": "storage/innobase/handler/ha_innodb.cc",
    "row_search_mvcc": "storage/innobase/row/row0sel.cc",
    # Joins and semijoins.
    "HashJoinIterator::BuildHashTable": "sql/iterators/hash_join_iterator.cc",
    "HashJoinIterator::Read": "sql/iterators/hash_join_iterator.cc",
    "BKAIterator::ReadOuterRows": "sql/iterators/bka_iterator.cc",
    "DsMrr_impl::dsmrr_fill_buffer": "sql/handler.cc",
    "DsMrr_impl::dsmrr_next": "sql/handler.cc",
    "WeedoutIterator::Read": "sql/iterators/composite_iterators.cc",
    "do_sj_dups_weedout": "sql/sql_executor.cc",
    # Temporary tables, derived tables and filesort.
    "create_ondisk_from_heap": "sql/sql_tmp_table.cc",
    "TemptableAggregateIterator::Init": "sql/iterators/composite_iterators.cc",
    "MaterializeIterator::Init": "sql/iterators/composite_iterators.cc",
    "Table_ref::generate_keys": "sql/table.cc",
    "Filesort_buffer::sort_buffer": "sql/filesort_utils.cc",
    "check_if_pq_applicable": "sql/filesort.cc",
    "merge_many_buff": "sql/merge_many_buff.h",
    "merge_buffers": "sql/filesort.cc",
    # Buffer pool.
    "buf_page_get_gen": "storage/innobase/buf/buf0buf.cc",
    "buf_page_peek_if_too_old": "storage/innobase/include/buf0buf.ic",
    "buf_page_make_young_if_needed": "storage/innobase/buf/buf0buf.cc",
    "buf_LRU_make_block_young": "storage/innobase/buf/buf0lru.cc",
    "buf_LRU_free_from_common_LRU_list": "storage/innobase/buf/buf0lru.cc",
    "buf_LRU_add_block": "storage/innobase/buf/buf0lru.cc",
    "buf_read_page": "storage/innobase/buf/buf0rea.cc",
    "buf_dump": "storage/innobase/buf/buf0dump.cc",
    "buf_load": "storage/innobase/buf/buf0dump.cc",
    # Join order search.
    "Optimize_table_order::greedy_search": "sql/sql_planner.cc",
    "Optimize_table_order::best_extension_by_limited_search": "sql/sql_planner.cc",
    "Optimize_table_order::best_access_path": "sql/sql_planner.cc",
}

_GUIDE_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_GUIDE_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def normalize_guide(guide: dict) -> dict:
    """Check a lesson's guide content and return the JSON form the page uses.

    Raises ``ValueError`` naming the first problem: unknown keys, duplicate
    or malformed line ids, a beat naming a line that does not exist, a quiz
    outside two to four questions, a question without exactly one right
    choice, a reason that is not one sentence, or a function name that is
    not in ``VERIFIED_SERVER_FUNCTIONS``.
    """
    def fail(message):
        raise ValueError("lesson guide: " + message)

    def text(value, where, limit):
        if not isinstance(value, str) or not value.strip():
            fail(where + " must be non-empty text")
        if len(value) > limit:
            fail("%s is longer than %d characters: %r" % (where, limit, value))
        return value.strip()

    if not isinstance(guide, dict):
        fail("must be a dict")
    unknown = set(guide) - {"code", "beats", "quiz"}
    if unknown:
        fail("unknown keys %s" % sorted(unknown))
    code = guide.get("code")
    if not isinstance(code, dict) or set(code) - {"title", "lines"}:
        fail("code must be a dict with title and lines")
    raw_lines = code.get("lines")
    if not isinstance(raw_lines, (list, tuple)) or not 3 <= len(raw_lines) <= 14:
        fail("code lines must be a list of 3 to 14 lines")
    lines = []
    seen = set()
    for row in raw_lines:
        if isinstance(row, dict):
            row = (row.get("id"), row.get("indent", 0), row.get("text"), row.get("fn", ""))
        if not isinstance(row, (list, tuple)) or len(row) not in (3, 4):
            fail("a code line is (id, indent, text[, function]): %r" % (row,))
        line_id, indent, line_text = row[0], row[1], row[2]
        fn = row[3] if len(row) == 4 else ""
        if not isinstance(line_id, str) or not _GUIDE_ID.match(line_id):
            fail("line id %r must be lowercase letters, digits and hyphens" % (line_id,))
        if line_id in seen:
            fail("duplicate line id %r" % line_id)
        seen.add(line_id)
        if not isinstance(indent, int) or isinstance(indent, bool) or not 0 <= indent <= 4:
            fail("line %r indent must be 0 to 4" % line_id)
        fn = fn or ""
        if fn and fn not in VERIFIED_SERVER_FUNCTIONS:
            fail("line %r names %r, which is not in VERIFIED_SERVER_FUNCTIONS; "
                 "check it in mysql-server and add it there first" % (line_id, fn))
        lines.append({"id": line_id, "indent": indent,
                      "text": text(line_text, "line %r text" % line_id, 72), "fn": fn})
    beats = guide.get("beats")
    if not isinstance(beats, dict) or not beats:
        fail("beats must map each tl.beat kind to its line ids")
    out_beats = {}
    for kind, ids in beats.items():
        if not isinstance(kind, str) or not _GUIDE_ID.match(kind):
            fail("beat kind %r must be lowercase letters, digits and hyphens" % (kind,))
        if not isinstance(ids, (list, tuple)) or not ids:
            fail("beat %r must name at least one line" % kind)
        for line_id in ids:
            if line_id not in seen:
                fail("beat %r names unknown line %r" % (kind, line_id))
        out_beats[kind] = list(ids)
    quiz = guide.get("quiz")
    if not isinstance(quiz, (list, tuple)) or not 2 <= len(quiz) <= 4:
        fail("quiz must have two to four questions")
    out_quiz = []
    for number, item in enumerate(quiz, 1):
        if not isinstance(item, dict) or set(item) != {"question", "choices"}:
            fail("question %d must be a dict with question and choices" % number)
        question = text(item["question"], "question %d" % number, 240)
        choices = item["choices"]
        if not isinstance(choices, (list, tuple)) or not 2 <= len(choices) <= 4:
            fail("question %d must have two to four choices" % number)
        out_choices = []
        for choice in choices:
            if isinstance(choice, dict):
                choice = (choice.get("text"), choice.get("correct"), choice.get("why"))
            if not isinstance(choice, (list, tuple)) or len(choice) != 3 or \
                    not isinstance(choice[1], bool):
                fail("question %d: a choice is (text, correct, why)" % number)
            why = text(choice[2], "question %d reason" % number, 260)
            if len(_GUIDE_SENTENCE_END.findall(why)) != 1 or why[-1] not in ".!?":
                fail("question %d: the reason %r must be one sentence" % (number, why))
            out_choices.append({"text": text(choice[0], "question %d choice" % number, 160),
                                "correct": choice[1], "why": why})
        if sum(1 for c in out_choices if c["correct"]) != 1:
            fail("question %d must have exactly one right choice" % number)
        out_quiz.append({"question": question, "choices": out_choices})
    return {
        "code": {"title": text(code.get("title"), "code title", 60), "lines": lines},
        "beats": out_beats,
        "quiz": out_quiz,
    }


def guide_json_html(guide: dict) -> str:
    """The lesson's guide content as an inert JSON script element.

    ``<``, ``>``, ``&`` and the line separators are written as \\u escapes,
    so no text in the content can end the element or start markup.
    """
    data = json.dumps(normalize_guide(guide), ensure_ascii=False, separators=(",", ":"))
    for raw, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                         (" ", "\\u2028"), (" ", "\\u2029")):
        data = data.replace(raw, escaped)
    return '<script type="application/json" id="lesson-guide">' + data + "</script>"


# ---- Page arrangement: the stage comes first ----------------------------
#
# Every lesson page opens with its parameters (one compact row), then the
# stage (toolbar, scrubber, step bar, drawing and simplified code), then
# "About this example" (the example query and what the drawing shows), then
# the numbers and Learn more. Lessons keep writing their query card and
# explainer where they always did; render_page moves them, so the order is
# decided once for all lessons and the DOM order is the visual order.

#: The two cards ``query_card`` and ``explainer`` write. Their content is
#: escaped (only <code> survives in an explainer), so the first </div> after
#: the opening tag closes the card.
_ABOUT_CARD_RE = re.compile(
    r'[ \t]*<div class="(?:query-card|explainer-card)">.*?</div>[ \t]*\n?', re.S)
_ABOUT_ID = "about-example"
_CONTROL_START_RE = re.compile(r'<div class="control"(?=[\s>])')
_CONTROL_HINT_RE = re.compile(
    r'\s*<div class="hint"(?P<attrs>[^>]*)>(?P<body>.*?)</div>', re.S)
_CONTROL_LABEL_RE = re.compile(r'<label\b(?P<attrs>[^>]*)>.*?</label>', re.S)
_CONTROLS_H2_RE = re.compile(r'(<section class="controls"[^>]*>\s*)(<h2\b.*?</h2>)', re.S)
_ATTR_RE = r'\b{0}="([^"]*)"'


def _attr(attrs: str, name: str) -> str:
    found = re.search(_ATTR_RE.format(name), attrs)
    return found.group(1) if found else ""


def _hint_to_help_tip(segment: str, used_ids: set) -> str:
    """One control: move its hint into a (?) help tip beside its label.

    The hint keeps its id when it has one (a lesson may rewrite the text),
    else gets ``hint-<control id>``; the control it describes points at it
    with aria-describedby, so a screen reader reads it with the control.
    """
    hint = _CONTROL_HINT_RE.search(segment)
    label = _CONTROL_LABEL_RE.search(segment)
    if not hint or not label or label.start() > hint.start():
        return segment
    target = _attr(label.group("attrs"), "for")
    tip_id = _attr(hint.group("attrs"), "id")
    if not tip_id:
        base = tip_id = "hint-" + (target or "control")
        n = 2
        while tip_id in used_ids:
            tip_id = "%s-%d" % (base, n)
            n += 1
        used_ids.add(tip_id)
    tip = ('<span class="help-tip" aria-hidden="true">?'
           '<span class="tip-text" id="%s">%s</span></span>' % (esc(tip_id), hint.group("body")))
    segment = (segment[:label.start()] + '<div class="control-head">' + label.group(0) + tip
               + "</div>" + segment[label.end():hint.start()] + segment[hint.end():])
    if target:
        control = re.compile(r'<(?:input|select|textarea)\b[^>]*\bid="%s"[^>]*?(?=/?>)'
                             % re.escape(target))
        found = control.search(segment)
        if found:
            tag = found.group(0)
            described = re.search(r'\baria-describedby="([^"]*)"', tag)
            if described:
                tag = tag.replace(described.group(0), 'aria-describedby="%s %s"'
                                  % (described.group(1), esc(tip_id)))
            else:
                tag = tag.rstrip() + ' aria-describedby="%s"' % esc(tip_id)
            segment = segment[:found.start()] + tag + segment[found.end():]
    return segment


def _compact_controls(controls_html: str, about: bool) -> str:
    """The parameters as one compact row: each control's hint becomes the
    (?) beside its label, and the heading gets a link to About this example."""
    starts = [m.start() for m in _CONTROL_START_RE.finditer(controls_html)]
    if starts:
        used = set(re.findall(r'\bid="([^"]*)"', controls_html))
        parts = [controls_html[:starts[0]]]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(controls_html)
            parts.append(_hint_to_help_tip(controls_html[start:end], used))
        controls_html = "".join(parts)
    if about:
        controls_html = _CONTROLS_H2_RE.sub(
            lambda m: (m.group(1) + '<div class="controls-head">' + m.group(2)
                       + '<a class="about-link" href="#%s">About this example'
                       ' <span aria-hidden="true">&#8595;</span></a></div>' % _ABOUT_ID),
            controls_html, count=1)
    return controls_html


def arrange_lesson(controls_html: str, stage_html: str):
    """Return ``(controls_html, stage_html, about_html)`` in page order.

    The query card and the explainer leave the stage (or the controls) for
    one "About this example" section that ``render_page`` puts right under
    the stage; the parameters become one compact row.
    """
    cards = []

    def take(match):
        cards.append(match.group(0).strip())
        return ""

    controls_html = _ABOUT_CARD_RE.sub(take, controls_html)
    stage_html = _ABOUT_CARD_RE.sub(take, stage_html)
    about_html = ""
    if cards:
        about_html = (
            '<section class="about-example" id="%s" aria-labelledby="%s-h">\n'
            '  <h2 id="%s-h">About this example</h2>\n'
            '  <div class="about-grid">\n    %s\n  </div>\n'
            '</section>' % (_ABOUT_ID, _ABOUT_ID, _ABOUT_ID, "\n    ".join(cards)))
    return _compact_controls(controls_html, bool(cards)), stage_html, about_html


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
    guide: dict = None,
) -> str:
    """Assemble a complete self-contained HTML document for a lesson.

    ``guide`` (optional) is the lesson's declarative guide content: the
    simplified code its steps highlight, the beat-to-line map and the
    Check yourself quiz. See ``normalize_guide``.

    The page order is decided here for every lesson (``arrange_lesson``):
    parameters, stage, About this example, then the readout and Learn more.
    """
    css = _BASE_CSS + "\n" + extra_css
    family_chip_html = _family_chip(lesson_id)
    guide_html = guide_json_html(guide) if guide else ""
    controls_html, stage_html, about_html = arrange_lesson(controls_html, stage_html)
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
  <main id="lesson-main">
    {banner_html}
    {controls_html}
    {stage_html}
    {about_html}
    {readout_html}
    {learn_more_html}
  </main>
  <footer>
    <p>Generated by <a href="https://github.com/vgrippa/myflames">myflames teach</a> —
    interactive database algorithm lessons. Offline-first, stdlib-only.</p>
  </footer>
  {guide_html}
  <script>{ANIM_JS}
{TIER1_RUNTIME_JS}
{_BASE_JS}
{lesson_js}
{_GUIDE_JS}
  </script>
</body>
</html>
"""
