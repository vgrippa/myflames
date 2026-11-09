// Instance-scoped search: dims non-matching items and reports a count.
// Case-insensitive substring match, as in workbench.js.

export interface SearchItem {
  element: Element;
  text: string;
}

export function matches(text: string, query: string): boolean {
  const needle = query.toLowerCase().trim();
  return !needle || text.toLowerCase().indexOf(needle) !== -1;
}

export function bindSearch(input: HTMLInputElement, status: Element, items: SearchItem[]): () => void {
  const run = () => {
    const query = input.value.toLowerCase().trim();
    let count = 0;
    for (const item of items) {
      const match = matches(item.text, query);
      item.element.classList.toggle("dim", !match);
      if (match) count++;
    }
    status.textContent = query ? count + " / " + items.length : "";
  };
  input.addEventListener("input", run);
  return () => input.removeEventListener("input", run);
}
