// DOM builders. Labels are untrusted plan text: only textContent and
// setAttribute are used, never innerHTML.
export const SVG_NS = "http://www.w3.org/2000/svg";
export const XHTML_NS = "http://www.w3.org/1999/xhtml";

type Attrs = Record<string, string | number | null | undefined>;

function apply(el: Element, attrs?: Attrs, parent?: Element | null, text?: string) {
  if (attrs) {
    for (const key of Object.keys(attrs)) {
      const value = attrs[key];
      if (value !== null && value !== undefined) el.setAttribute(key, String(value));
    }
  }
  if (text !== undefined) el.textContent = text;
  if (parent) parent.appendChild(el);
}

export function svg<K extends keyof SVGElementTagNameMap>(
  doc: Document, tag: K, attrs?: Attrs, parent?: Element | null, text?: string,
): SVGElementTagNameMap[K] {
  const el = doc.createElementNS(SVG_NS, tag) as SVGElementTagNameMap[K];
  apply(el, attrs, parent, text);
  return el;
}

export function html<K extends keyof HTMLElementTagNameMap>(
  doc: Document, tag: K, attrs?: Attrs, parent?: Element | null, text?: string,
): HTMLElementTagNameMap[K] {
  const el = doc.createElementNS(XHTML_NS, tag) as HTMLElementTagNameMap[K];
  apply(el, attrs, parent, text);
  return el;
}

/** Python's "%.1f"-style coordinate text, without "-0.0". */
export function f1(value: number): string {
  const text = value.toFixed(1);
  return text === "-0.0" ? "0.0" : text;
}
