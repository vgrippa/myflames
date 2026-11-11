// Theme defaults also travel inside exported SVGs. Workspace tokens are mapped
// in style.css; standalone reports use these defaults. Operator palettes are kept
// intact, with dark ink on their light bands in either appearance.
// Defaults never match a named palette: a chart can inject this style after
// themes.css, and must not overwrite the host palette through the OS query.
export const LIGHT_THEME = "--mfc-canvas:#fafafa;--mfc-card:#fff;--mfc-pane:#f2f2f4;--mfc-ink:#243341;--mfc-muted:#596a79;--mfc-line:#d8e0e5;--mfc-boundary:#82858c;--mfc-focus:#007aff;--mfc-link:#0063cc;--mfc-hover:rgba(0,0,0,.04);--mfc-selected:#eaf3ff;--mfc-danger:#b42318;--mfc-edge:#8795a1;--mfc-mark-ink:#18222c;--mfc-venn:#f5f5f7;--mfc-venn-on:#b7d9ff;--mfc-venn-ring:#6e7d8c;--mfc-estimate:#5b3cc4;";
export const DARK_THEME = "--mfc-canvas:#151719;--mfc-card:#25282c;--mfc-pane:#1c1e21;--mfc-ink:#ededee;--mfc-muted:#a5a9af;--mfc-line:#34383d;--mfc-boundary:#7b8592;--mfc-focus:#0a84ff;--mfc-link:#409cff;--mfc-hover:rgba(255,255,255,.06);--mfc-selected:#20354e;--mfc-danger:#ff8b82;--mfc-edge:#a0aeba;--mfc-venn:#3a3f45;--mfc-venn-on:#2d5f96;--mfc-venn-ring:#8b97a3;--mfc-estimate:#b3a4ff;";

// Frame messages need a literal color even when the host has no override.
export const CHART_THEME_DEFAULTS = {
  light: Object.fromEntries(LIGHT_THEME.split(";").filter(Boolean).map(value => value.split(":"))),
  dark: Object.fromEntries(DARK_THEME.split(";").filter(Boolean).map(value => value.split(":"))),
};
