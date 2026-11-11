import { appliedThemeBase } from "../../themes";
import { CHART_THEME_DEFAULTS } from "./themeDefaults";
// Sandboxed previews have opaque origins, so appearance travels by message.
// Only the owning parent can change it; no host DOM or API access is granted.
const MESSAGE = "myflames-appearance";
const ACCENTS = ["--accent", ..."text fill hover soft line ink".split(" ").map(role => "--accent-" + role), "--focus"];

export const CHART_SURFACES = "canvas card pane ink muted line boundary hover selected focus link danger edge venn venn-on venn-ring estimate".split(" ").map(role => "--mfc-" + role);

export function themeFrame(html: string): string {
  const style = `<style>
    button.primary { background:var(--accent-fill,var(--accent)); border-color:var(--accent-fill,var(--accent)); }
    button.primary:hover:not(:disabled) { background:var(--accent-hover); border-color:var(--accent-hover); }
    button[aria-pressed="true"], a { color:var(--accent-text,var(--accent)); }
  </style>`;
  // Fixed script text only. Lesson/plan content never enters executable code.
  const script = `<script>(function(){
    function validColor(value){
      if(typeof value!=="string" || value.length>128 || value.includes("\\\\")) return false;
      var hex=/^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
      var fn=/^(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color|color-mix)\\([0-9a-z.,%/ ()+-]+\\)$/i;
      return (hex.test(value) || fn.test(value)) && !/\\b(?:var|attr|env|if|url|from)\\b/i.test(value) && CSS.supports("color",value);
    }
    window.addEventListener("message",function(event){
      var data=event.data;
      if(event.source!==parent || !data || data.type!=="${MESSAGE}" || (data.theme!=="light" && data.theme!=="dark")) return;
      var root=document.documentElement;
      root.setAttribute("data-theme",data.theme);
      root.style.colorScheme=data.theme;
      var names=${JSON.stringify(ACCENTS)};
      names.forEach(function(name){
        var value=data.accents?.[name];
        if(validColor(value)) root.style.setProperty(name,value);
      });
      var surfaces=${JSON.stringify(CHART_SURFACES)};
      surfaces.forEach(function(name){
        var value=data.surfaces?.[name];
        if(validColor(value)) root.style.setProperty(name,value);
      });
    });
  })();</script>`;
  return html.replace(/<\/head\s*>/i, style + script + "</head>");
}

/** Sync in place, preserving a lesson's animation position and chart focus. */
export function bindFrameAppearance(frame: HTMLIFrameElement): () => void {
  const doc = frame.ownerDocument;
  const win = doc.defaultView;
  if (!win) return () => {};
  const send = () => {
    const styles = win.getComputedStyle(doc.documentElement);
    const theme = appliedThemeBase(doc.documentElement);
    const accents: Record<string, string> = {};
    ACCENTS.forEach(name => { accents[name] = styles.getPropertyValue(name).trim(); });
    // Chart mappings live on .workspace, while accent roles live at the root.
    const chartStyles = win.getComputedStyle(doc.querySelector(".workspace") || doc.documentElement);
    const surfaces: Record<string, string> = {};
    CHART_SURFACES.forEach(name => { surfaces[name] = chartStyles.getPropertyValue(name).trim() || CHART_THEME_DEFAULTS[theme][name]; });
    frame.contentWindow?.postMessage({ type: MESSAGE, theme, accents, surfaces }, "*");
  };
  const observer = new MutationObserver(send);
  observer.observe(doc.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const media = win.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener("change", send);
  frame.addEventListener("load", send);
  send();
  return () => {
    observer.disconnect();
    media.removeEventListener("change", send);
    frame.removeEventListener("load", send);
  };
}
