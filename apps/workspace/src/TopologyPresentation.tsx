import "./topology-presentation.css";

/** A compact isometric server silhouette adds depth without rotating labels. */
export function ServerGlyph() {
  return <svg className="topology-server-glyph" viewBox="0 0 32 32" aria-hidden="true">
    <path className="server-top" d="M4 7 16 2 28 7 16 12Z" />
    <path className="server-side" d="M16 12 28 7v18L16 30Z" />
    <path className="server-front" d="M4 7 16 12v18L4 25Z" />
    <path className="server-slots" d="m7 13 6 2.5m-6 3 6 2.5m-6 3 6 2.5" />
  </svg>;
}
