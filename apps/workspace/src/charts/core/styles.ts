// Styles live in strings so each SVG carries its own <style> (exported SVGs
// stay self-styled) and HTML panels need no external stylesheet. Classes
// are namespaced: wb-* (Visual Explain, shared with output_workbench.py)
// and mfc-* (runtime panels). Allowed by style-src 'unsafe-inline'.

export const FONT = '-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif';

export const WORKBENCH_CSS = [
  ".wb-plan{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;background:#f8fafb;color:#243341}",
  ".wb-plan text{fill:#243341;font-size:12px}",
  ".wb-plan .wb-heading{font-size:19px;font-weight:650}",
  ".wb-plan .wb-caption{fill:#596a79;font-size:11px}",
  ".wb-plan .wb-title{font-size:13px;font-weight:650}",
  ".wb-plan .wb-node{cursor:pointer;outline:none}",
  ".wb-plan .wb-node:hover .wb-box,.wb-plan .wb-node:focus .wb-box,.wb-plan .wb-node.selected .wb-box{stroke:#176aa8;stroke-width:3}",
  ".wb-plan .wb-node.dim{opacity:.25}",
  ".wb-plan .wb-controls{display:flex;align-items:center;height:100%}",
  ".wb-plan .wb-controls button,.wb-plan .wb-controls input{box-sizing:border-box;height:28px;margin:0;font:12px -apple-system,BlinkMacSystemFont,system-ui,sans-serif;color:#1d1d1f}",
  ".wb-plan .wb-controls button{cursor:pointer;min-width:30px;padding:0 10px;border:1px solid #8e8e98;border-left-width:0;border-radius:0;background:#f2f2f4}",
  '.wb-plan .wb-controls [data-action="fit"]{border-left-width:1px;border-radius:6px 0 0 6px}',
  '.wb-plan .wb-controls [data-action="reset"]{min-width:56px;border-radius:0 6px 6px 0;font-variant-numeric:tabular-nums}',
  ".wb-plan .wb-controls button:hover:not(:disabled){background:#e9e9ed}",
  ".wb-plan .wb-controls button:active:not(:disabled),.wb-plan .wb-controls button.wb-activated:not(:disabled){background:#fff;color:#176aa8}",
  ".wb-plan .wb-controls button:disabled{opacity:.4;cursor:default}",
  ".wb-plan .wb-controls input{width:180px;margin-left:8px;padding:0 8px;border:1px solid #8e8e98;border-radius:4px;background:#fff}",
  ".wb-plan .wb-search-status{margin-left:8px;font:12px -apple-system,BlinkMacSystemFont,system-ui,sans-serif;color:#5c5c66}",
  ".wb-plan button:focus-visible,.wb-plan input:focus-visible{position:relative;z-index:1;outline:2px solid #176aa8}",
  ".wb-plan .wb-details{user-select:text;font:12px/1.6 system-ui;color:#243341;white-space:pre-wrap;overflow:auto;height:100%;box-sizing:border-box;padding:12px;border:1px solid #d8e0e5;border-radius:6px;background:#fff}",
  ".wb-plan .wb-edge-label{font-size:11px;paint-order:stroke;stroke:var(--surface,#f8fafb);stroke-width:6px;stroke-linejoin:round}",
].join("");

// Tree, bar chart and flame graph: each SVG carries its own <style>, scoped
// by its root class so two charts (or the host page) never collide.
const SEARCH_RING = "{outline:2px solid #176aa8;outline-offset:1px}";

export const TREE_CSS = [
  ".mfc-tree{font-family:" + FONT + ";background:#fafafa}",
  ".mfc-tree text{font-size:11px;fill:#222}",
  ".mfc-tree .title{font-size:16px;font-weight:650}",
  ".mfc-tree .subtitle{font-size:11px;fill:#666}",
  ".mfc-tree .col-hdr{font-size:10px;fill:#777;font-weight:650;letter-spacing:.4px}",
  ".mfc-tree .row-label.warn{fill:#b71c1c}",
  ".mfc-tree .toggle{font-size:13px;fill:#333;cursor:pointer}",
  ".mfc-tree .toggle:hover{fill:#000}",
  ".mfc-tree .leaf-indicator{font-size:13px;fill:#bbb}",
  ".mfc-tree .tree-row{cursor:pointer;outline:none}",
  ".mfc-tree .tree-row.mfc-dim{opacity:.25}",
  ".mfc-tree .row-bg:hover{fill:rgba(0,0,0,.04)}",
  ".mfc-tree .tree-row.is-selected .row-bg{fill:rgba(0,90,200,.15);stroke:#1565c0;stroke-width:1}",
  ".mfc-tree .tree-row:focus-visible .row-bg" + SEARCH_RING,
  ".mfc-tree .row-time{fill:#444}",
  ".mfc-tree .row-pct{fill:#555;font-weight:650}",
  ".mfc-tree .details-line{font-size:12px;fill:#222;user-select:text;-webkit-user-select:text;cursor:text}",
  ".mfc-tree .ctrl-link{font-size:10px;fill:#1565c0;cursor:pointer}",
  ".mfc-tree .ctrl-link:hover,.mfc-tree .ctrl-link:focus-visible{fill:#0d47a1;text-decoration:underline;outline:none}",
].join("");

export const BAR_CSS = [
  ".mfc-bar{font-family:" + FONT + ";background:#fafafa}",
  ".mfc-bar text{font-size:12px;fill:#222}",
  ".mfc-bar .title{font-size:18px;font-weight:650}",
  ".mfc-bar .subtitle{font-size:11px;fill:#666}",
  ".mfc-bar .col-header{font-size:10px;fill:#777;font-weight:650;letter-spacing:.5px}",
  ".mfc-bar .label{font-size:11px;fill:#333}",
  ".mfc-bar .loops{font-size:10px;fill:#777}",
  ".mfc-bar .value{font-size:11px;font-weight:650}",
  ".mfc-bar .bar{transition:opacity .12s;cursor:pointer;outline:none}",
  ".mfc-bar .bar:hover{opacity:.82;stroke:#444;stroke-width:2}",
  ".mfc-bar .bar.in-query-analysis:hover{stroke:#c62828;stroke-width:2.5}",
  ".mfc-bar .bar.is-selected{stroke:#1565c0;stroke-width:2.5}",
  ".mfc-bar .bar:focus-visible{stroke:#176aa8;stroke-width:3}",
  ".mfc-bar .details-line{font-size:12px;user-select:text;-webkit-user-select:text}",
  ".mfc-bar .details-line.dim{fill:#999}",
].join("");

export const FLAME_CSS = [
  ".mfc-flame{font-family:" + FONT + "}",
  ".mfc-flame text{font-size:12px;fill:#1d1d1f}",
  ".mfc-flame .fg-title{font-size:17px;font-weight:600}",
  ".mfc-flame .fg-reset{fill:#176aa8;cursor:pointer;outline:none}",
  ".mfc-flame .fg-reset:hover,.mfc-flame .fg-reset:focus-visible{text-decoration:underline}",
  ".mfc-flame .fg-off,.mfc-flame .fg-hidden{display:none}",
  ".mfc-flame .frames>g{cursor:pointer;outline:none}",
  ".mfc-flame .frames>g:hover>rect{stroke:#1d1d1f;stroke-width:.6}",
  ".mfc-flame .frames>g:focus-visible>rect{stroke:#176aa8;stroke-width:2}",
  ".mfc-flame .frames>g.is-selected>rect{stroke:#007aff;stroke-width:2.5}",
  ".mfc-flame .frames text{fill:#000;pointer-events:none}",
  ".mfc-flame .fg-ancestor{opacity:.55}",
  ".mfc-flame .fg-details{fill:#3a3a3c}",
  ".mfc-flame .fg-matched{font-weight:600}",
].join("");

export const PANEL_CSS = [
  ".mfc-mount{display:block}",
  ".mfc-chart{overflow:auto}",
  ".mfc-chart>svg{max-width:100%;height:auto;display:block}",
  ".mfc-error{padding:20px;color:#b42318}",
  ".mfc-chart:focus{outline:none}",
  ".mfc-empty{padding:20px;color:#6e6e73}",
  ".mfc-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:6px 10px;font:12px " + FONT + ";color:#243341}",
  ".mfc-toolbar input[type=search]{font:inherit;min-width:220px;padding:5px 9px;border:1px solid #bbc8d3;border-radius:5px;background:#fff;color:inherit}",
  ".mfc-toolbar input[type=search][aria-invalid=true]{border-color:#b42318}",
  ".mfc-toolbar input:focus-visible{outline:2px solid #176aa8}",
  ".mfc-toolbar .mfc-toggle{display:inline-flex;align-items:center;gap:4px;cursor:pointer}",
  ".mfc-toolbar .mfc-search-status{color:#596a79;font-variant-numeric:tabular-nums}",
  ".mfc-mount .metric-panel{padding:18px 20px;border-top:1px solid #e5e5ea}",
  ".mfc-mount .metric-panel h2{margin:0 0 4px;font-size:14px}",
  ".mfc-mount .metric-panel p{color:#6e6e73;font-size:12px;line-height:1.5;margin:4px 0 14px}",
  ".mfc-mount .metric-row{--depth:0;display:grid;grid-template-columns:minmax(150px,1fr) 150px 100px;gap:16px;align-items:center;text-align:left;width:100%;border:0;border-radius:7px;background:transparent;padding:9px 10px;cursor:pointer;color:inherit;font:inherit}",
  ".mfc-mount .metric-row:hover{background:#f5f5f7}",
  ".mfc-mount .metric-name{padding-left:calc(var(--depth)*14px);overflow-wrap:anywhere}",
  ".mfc-mount .metric-value{font-variant-numeric:tabular-nums;text-align:right;font-size:12px}",
  ".mfc-mount .metric-track{height:5px;background:#f0f0f3;border-radius:5px}",
  ".mfc-mount .metric-track>span{display:block;width:var(--amount);height:100%;border-radius:inherit;background:#007aff}",
  ".mfc-mount .metric-row.is-selected{background:#eaf3ff;box-shadow:inset 0 0 0 1px #007aff}",
  ".mfc-mount .metric-row:focus-visible{outline:2px solid #007aff;outline-offset:2px}",
  "@media(max-width:640px){.mfc-mount .metric-row{grid-template-columns:minmax(100px,1fr) 110px}.mfc-mount .metric-track{display:none}}",
].join("");

const STYLE_ID = "mfc-panel-styles";

/** Insert the panel stylesheet once per document. */
export function ensurePanelStyles(doc: Document) {
  if (doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = PANEL_CSS;
  (doc.head || doc.documentElement).appendChild(style);
}
