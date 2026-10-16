// Details strip: plain text only (details_text is built by Python).

export interface DetailsStrip {
  show(text: string): void;
  reset(): void;
}

export function createDetails(element: Element, placeholder: string): DetailsStrip {
  element.textContent = placeholder;
  return {
    show(text: string) {
      element.textContent = text;
    },
    reset() {
      element.textContent = placeholder;
    },
  };
}
