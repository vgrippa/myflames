// Sandboxed previews have opaque origins, so appearance travels by message.
// Only the owning parent can change it; no host DOM or API access is granted.
const MESSAGE = "myflames-appearance";
const ACCENTS = ["--accent", "--accent-text", "--accent-fill", "--accent-hover", "--accent-soft", "--accent-line", "--accent-ink", "--focus"];

export function themeFrame(html: string): string {
  const style = `<style>
    button.primary { background:var(--accent-fill,var(--accent)); border-color:var(--accent-fill,var(--accent)); }
    button.primary:hover:not(:disabled) { background:var(--accent-hover); border-color:var(--accent-hover); }
    button[aria-pressed="true"], a { color:var(--accent-text,var(--accent)); }
  </style>`;
  // Fixed script text only. Lesson/plan content never enters executable code.
  const script = `<script>(function(){
    window.addEventListener("message",function(event){
      var data=event.data;
      if(event.source!==parent || !data || data.type!=="${MESSAGE}" || (data.theme!=="light" && data.theme!=="dark")) return;
      var root=document.documentElement;
      root.setAttribute("data-theme",data.theme);
      var names=${JSON.stringify(ACCENTS)};
      names.forEach(function(name){
        var value=data.accents && data.accents[name];
        if(typeof value==="string") root.style.setProperty(name,value);
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
    const explicit = doc.documentElement.dataset.theme;
    const query = new URLSearchParams(win.location.search).get("theme");
    const theme = explicit === "light" || explicit === "dark" ? explicit :
      query === "light" || query === "dark" ? query :
        win.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    const accents: Record<string, string> = {};
    ACCENTS.forEach(name => { accents[name] = styles.getPropertyValue(name).trim(); });
    frame.contentWindow?.postMessage({ type: MESSAGE, theme, accents }, "*");
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
