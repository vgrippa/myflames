// Styles live in strings so each SVG carries its own <style> (exported SVGs
// stay self-styled) and HTML panels need no external stylesheet. Classes
// are namespaced: wb-* (Visual Explain, shared with output_workbench.py)
// and mfc-* (runtime panels). Allowed by style-src 'unsafe-inline'.

export const FONT = '-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif';

// Theme defaults also travel inside exported SVGs. Workspace tokens are mapped
// in style.css; standalone reports use these defaults. Operator palettes are kept
// intact, with dark ink on their light bands in either appearance.
const LIGHT_THEME = "--mfc-canvas:#fafafa;--mfc-card:#fff;--mfc-pane:#f2f2f4;--mfc-ink:#243341;--mfc-muted:#596a79;--mfc-line:#d8e0e5;--mfc-boundary:#82858c;--mfc-focus:#007aff;--mfc-link:#0063cc;--mfc-hover:rgba(0,0,0,.04);--mfc-selected:#eaf3ff;--mfc-danger:#b42318;--mfc-edge:#8795a1;--mfc-mark-ink:#18222c;--mfc-venn:#f5f5f7;--mfc-venn-on:#b7d9ff;--mfc-venn-ring:#6e7d8c;";
const DARK_THEME = "--mfc-canvas:#151719;--mfc-card:#25282c;--mfc-pane:#1c1e21;--mfc-ink:#ededee;--mfc-muted:#a5a9af;--mfc-line:#34383d;--mfc-boundary:#7b8592;--mfc-focus:#0a84ff;--mfc-link:#409cff;--mfc-hover:rgba(255,255,255,.06);--mfc-selected:#20354e;--mfc-danger:#ff8b82;--mfc-edge:#a0aeba;--mfc-venn:#3a3f45;--mfc-venn-on:#2d5f96;--mfc-venn-ring:#8b97a3;";
export const THEME_CSS = ":root{" + LIGHT_THEME + "}" +
  "@media(prefers-color-scheme:dark){:root:not([data-theme=light]){" + DARK_THEME + "}}" +
  "[data-theme=light]{" + LIGHT_THEME + "}[data-theme=dark]{" + DARK_THEME + "}" +
  ":is(.wb-plan,.mfc-tree,.mfc-bar,.mfc-flame) .mfc-background{fill:var(--mfc-canvas)}" +
  ":is(.wb-plan,.mfc-tree,.mfc-bar) .mfc-pane{fill:var(--mfc-pane)}" +
  ":is(.mfc-tree,.mfc-bar) .mfc-rule{stroke:var(--mfc-line)}" +
  ".mfc-tree .mfc-stripe{fill:var(--mfc-hover)}" +
  ".wb-plan .wb-box{fill:var(--mfc-card);stroke:var(--mfc-boundary)}" +
  ".wb-plan .mfc-edge{stroke:var(--mfc-edge)}.wb-plan .mfc-arrow-head{fill:var(--mfc-edge)}";

export const WORKBENCH_CSS = [THEME_CSS,
  ".wb-plan{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;background:var(--mfc-canvas);color:var(--mfc-ink)}",
  ".wb-plan text{fill:var(--mfc-ink);font-size:12px}",
  ".wb-plan .wb-heading{font-size:19px;font-weight:650}",
  ".wb-plan .wb-caption{fill:var(--mfc-muted);font-size:11px}",
  ".wb-plan .wb-title{font-size:13px;font-weight:650}",
  ".wb-plan .wb-band-title{fill:var(--mfc-mark-ink)}",
  ".wb-plan .wb-node{cursor:pointer;outline:none}",
  ".wb-plan .wb-node:hover .wb-box{stroke:var(--mfc-focus)}",
  ".wb-plan .wb-node:focus .wb-box,.wb-plan .wb-node.selected .wb-box{stroke:var(--mfc-focus);stroke-width:3}",
  ".wb-plan .wb-node.dim{opacity:.25}",
  ".wb-plan .wb-controls{display:flex;align-items:center;height:100%}",
  ".wb-plan .wb-controls button,.wb-plan .wb-controls input{box-sizing:border-box;height:28px;margin:0;font:12px -apple-system,BlinkMacSystemFont,system-ui,sans-serif;color:var(--mfc-ink)}",
  ".wb-plan .wb-controls button{cursor:pointer;min-width:30px;padding:0 10px;border:1px solid var(--mfc-boundary);border-left-width:0;border-radius:0;background:var(--mfc-pane)}",
  '.wb-plan .wb-controls [data-action="out"]{border-left-width:1px;border-radius:6px 0 0 6px}',
  '.wb-plan .wb-controls [data-action="reset"]{min-width:56px;font-variant-numeric:tabular-nums}',
  '.wb-plan .wb-controls [data-action="fit"]{border-radius:0 6px 6px 0}',
  ".wb-plan .wb-controls button:hover:not(:disabled){background:var(--mfc-hover)}",
  ".wb-plan .wb-controls button:active:not(:disabled),.wb-plan .wb-controls button.wb-activated:not(:disabled){background:var(--mfc-card);color:var(--mfc-link)}",
  ".wb-plan .wb-controls button:disabled{opacity:.4;cursor:default}",
  ".wb-plan .wb-controls input{width:180px;margin-left:8px;padding:0 8px;border:1px solid var(--mfc-boundary);border-radius:4px;background:var(--mfc-card)}",
  ".wb-plan .wb-search-status{margin-left:8px;font:12px -apple-system,BlinkMacSystemFont,system-ui,sans-serif;color:var(--mfc-muted)}",
  ".wb-plan button:focus-visible,.wb-plan input:focus-visible{position:relative;z-index:1;outline:2px solid var(--mfc-focus)}",
  ".wb-plan .wb-details{user-select:text;font:12px/1.6 system-ui;color:var(--mfc-ink);white-space:pre-wrap;overflow:auto;height:100%;box-sizing:border-box;padding:12px;border:1px solid var(--mfc-line);border-radius:6px;background:var(--mfc-card)}",
  // Venn fills follow the theme so the L/R labels (ink) keep contrast; the
  // presentation attributes stay as the Python renderer writes them (parity).
  ".wb-venn circle:not([fill=none]):not(.wb-join-highlight),.wb-venn [data-region=excluded-overlap]{fill:var(--mfc-venn)}",
  ".wb-venn .wb-join-highlight{fill:var(--mfc-venn-on)}.wb-venn circle[fill=none]{stroke:var(--mfc-venn-ring)}",
  ".wb-plan .wb-edge-label{font-size:11px;paint-order:stroke;stroke:var(--mfc-canvas);stroke-width:6px;stroke-linejoin:round}",
  // The rows label sits on its edge: an opaque chip keeps the line out of the
  // spaces between words, where a glyph halo cannot reach (output_workbench).
  ".wb-plan .wb-edge-chip{fill:var(--mfc-canvas)}",
].join("");

// Tree, bar chart and flame graph: each SVG carries its own <style>, scoped
// by its root class so two charts (or the host page) never collide.
const SEARCH_RING = "{outline:2px solid var(--mfc-focus);outline-offset:1px}";

export const TREE_CSS = [THEME_CSS,
  ".mfc-tree{font-family:" + FONT + ";background:var(--mfc-canvas)}",
  ".mfc-tree text{font-size:11px;fill:var(--mfc-ink)}",
  ".mfc-tree .title{font-size:16px;font-weight:650}",
  ".mfc-tree .subtitle{font-size:11px;fill:var(--mfc-muted)}",
  ".mfc-tree .col-hdr{font-size:12px;fill:var(--mfc-muted);font-weight:600}",
  ".mfc-tree .row-label.warn{fill:var(--mfc-danger)}",
  ".mfc-tree .toggle{font-size:13px;fill:var(--mfc-ink);cursor:pointer}",
  ".mfc-tree .toggle:hover{fill:var(--mfc-ink)}",
  ".mfc-tree .leaf-indicator{font-size:13px;fill:var(--mfc-muted)}",
  ".mfc-tree .tree-row{cursor:pointer;outline:none}",
  ".mfc-tree .tree-row.mfc-dim{opacity:.25}",
  ".mfc-tree .row-bg:hover{fill:var(--mfc-hover)}",
  ".mfc-tree .tree-row.is-selected .row-bg{fill:var(--mfc-selected);stroke:var(--mfc-focus);stroke-width:1}",
  ".mfc-tree .tree-row:focus-visible .row-bg" + SEARCH_RING,
  ".mfc-tree .row-time{fill:var(--mfc-muted)}",
  ".mfc-tree .row-pct{fill:var(--mfc-ink);font-weight:650}",
  ".mfc-tree .details-line{font-size:12px;fill:var(--mfc-ink);user-select:text;-webkit-user-select:text;cursor:text}",
  ".mfc-tree .ctrl-link{font-size:12px;fill:var(--mfc-link);cursor:pointer}",
  ".mfc-tree .ctrl-link:hover,.mfc-tree .ctrl-link:focus-visible{fill:var(--mfc-link);text-decoration:underline;outline:none}",
].join("");

export const BAR_CSS = [THEME_CSS,
  ".mfc-bar{font-family:" + FONT + ";background:var(--mfc-canvas)}",
  ".mfc-bar text{font-size:12px;fill:var(--mfc-ink)}",
  ".mfc-bar .title{font-size:18px;font-weight:650}",
  ".mfc-bar .subtitle{font-size:11px;fill:var(--mfc-muted)}",
  ".mfc-bar .col-header{font-size:12px;fill:var(--mfc-muted);font-weight:600}",
  ".mfc-bar .label{font-size:11px;fill:var(--mfc-ink)}",
  ".mfc-bar .loops{font-size:10px;fill:var(--mfc-muted)}",
  ".mfc-bar .value{font-size:11px;font-weight:650}",
  ".mfc-bar .bar{transition:opacity .12s;cursor:pointer;outline:none}",
  ".mfc-bar .bar:hover{opacity:.82;stroke:var(--mfc-ink);stroke-width:2}",
  ".mfc-bar .bar.in-query-analysis:hover{stroke:var(--mfc-danger);stroke-width:2.5}",
  ".mfc-bar .bar.is-selected{stroke:var(--mfc-focus);stroke-width:2.5}",
  ".mfc-bar .bar:focus-visible{stroke:var(--mfc-focus);stroke-width:3}",
  ".mfc-bar .details-line{font-size:12px;user-select:text;-webkit-user-select:text}",
  ".mfc-bar .details-line.dim{fill:var(--mfc-muted)}",
].join("");

export const FLAME_CSS = [THEME_CSS,
  ".mfc-flame{font-family:" + FONT + ";background:var(--mfc-canvas)}",
  ".mfc-flame text{font-size:12px;fill:var(--mfc-ink)}",
  ".mfc-flame .fg-title{font-size:17px;font-weight:600}",
  ".mfc-flame .fg-reset{fill:var(--mfc-link);cursor:pointer;outline:none}",
  ".mfc-flame .fg-reset:hover,.mfc-flame .fg-reset:focus-visible{text-decoration:underline}",
  ".mfc-flame .fg-off,.mfc-flame .fg-hidden{display:none}",
  ".mfc-flame .frames>g{cursor:pointer;outline:none}",
  ".mfc-flame .frames>g:hover>rect{stroke:var(--mfc-ink);stroke-width:.6}",
  ".mfc-flame .frames>g:focus-visible>rect{stroke:var(--mfc-focus);stroke-width:2}",
  ".mfc-flame .frames>g.is-selected>rect{stroke:var(--mfc-focus);stroke-width:2.5}",
  ".mfc-flame .frames text{fill:#000;pointer-events:none}",
  ".mfc-flame .fg-ancestor{opacity:.55}",
  ".mfc-flame .fg-details{fill:var(--mfc-muted)}",
  ".mfc-flame .fg-matched{font-weight:600}",
].join("");

export const PANEL_CSS = [THEME_CSS,
  ".mfc-mount{display:block;background:var(--mfc-canvas);color:var(--mfc-ink)}",
  ".mfc-chart{overflow:auto}",
  ".mfc-chart>svg{max-width:100%;height:auto;display:block}",
  ".mfc-error{padding:20px;color:var(--mfc-danger)}",
  ".mfc-chart:focus{outline:none}",
  ".mfc-empty{padding:20px;color:var(--mfc-muted)}",
  ".mfc-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:6px 10px;font:12px " + FONT + ";color:var(--mfc-ink)}",
  ".mfc-toolbar input[type=search]{font:inherit;min-width:220px;padding:5px 9px;border:1px solid var(--mfc-boundary);border-radius:5px;background:var(--mfc-card);color:inherit}",
  ".mfc-toolbar input[type=search][aria-invalid=true]{border-color:var(--mfc-danger)}",
  ".mfc-toolbar input:focus-visible{outline:2px solid var(--mfc-focus)}",
  ".mfc-toolbar .mfc-toggle{display:inline-flex;align-items:center;gap:4px;cursor:pointer}",
  ".mfc-toolbar .mfc-search-status{color:var(--mfc-muted);font-variant-numeric:tabular-nums}",
  ".mfc-mount .metric-panel{padding:18px 20px;border-top:1px solid var(--mfc-line)}",
  ".mfc-mount .metric-panel h2{margin:0 0 4px;font-size:14px}",
  ".mfc-mount .metric-panel p{color:var(--mfc-muted);font-size:12px;line-height:1.5;margin:4px 0 14px}",
  ".mfc-mount .metric-row{--depth:0;display:grid;grid-template-columns:minmax(150px,1fr) 150px 100px;gap:16px;align-items:center;text-align:left;width:100%;border:0;border-radius:7px;background:transparent;padding:9px 10px;cursor:pointer;color:inherit;font:inherit}",
  ".mfc-mount .metric-row:hover{background:var(--mfc-hover)}",
  ".mfc-mount .metric-name{padding-left:calc(var(--depth)*14px);overflow-wrap:anywhere}",
  ".mfc-mount .metric-value{font-variant-numeric:tabular-nums;text-align:right;font-size:12px}",
  ".mfc-mount .metric-track{height:5px;background:var(--mfc-pane);border-radius:5px}",
  ".mfc-mount .metric-track>span{display:block;width:var(--amount);height:100%;border-radius:inherit;background:var(--mfc-focus)}",
  ".mfc-mount .metric-row.is-selected{background:var(--mfc-selected);box-shadow:inset 0 0 0 1px var(--mfc-focus)}",
  ".mfc-mount .metric-row:focus-visible{outline:2px solid var(--mfc-focus);outline-offset:2px}",
  "@media(max-width:640px){.mfc-mount .metric-row{grid-template-columns:minmax(100px,1fr) 110px}.mfc-mount .metric-track{display:none}}",
].join("");

const STYLE_ID = "mfc-panel-styles";

/** Insert the panel stylesheet once per document. */
export function ensurePanelStyles(doc: Document) {
  // An explicit host appearance takes precedence over the URL. This also
  // supports report/explore pages, which do not load workspace settings.
  const theme = new URLSearchParams(doc.defaultView?.location.search || "").get("theme");
  if ((theme === "light" || theme === "dark") && !doc.documentElement.hasAttribute("data-theme"))
    doc.documentElement.setAttribute("data-theme", theme);
  if (doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = PANEL_CSS;
  (doc.head || doc.documentElement).appendChild(style);
}
