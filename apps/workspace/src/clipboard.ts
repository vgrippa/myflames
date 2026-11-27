// Shared clipboard copy path and control-character sanitizer. Kept in a leaf
// module with no top-level side effects so the entry chunk stays within its
// bundle-split budget while every caller shares one sanitizer.

const CLIPBOARD_CONTROLS = /[\0-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069]/g;

export type CopyResult = { ok: boolean; text: string; removed: number };

/** Replace terminal, separator and bidi control characters with spaces before
 * text reaches the clipboard. Keeps TAB and LF; turns only CRLF into LF;
 * replaces lone CR, other C0 controls, DEL, C1 (U+0080-U+009F), Unicode
 * line/paragraph separators (U+2028-U+2029) and bidi controls/marks
 * (U+061C, U+200E, U+200F, U+202A-U+202E, U+2066-U+2069) with one ASCII space
 * each so adjacent tokens cannot merge and line comments, TSV rows and JSON
 * strings keep their boundaries. Counts every replaced character in removed. */
export function sanitizeClipboardText(text: string): { text: string; removed: number } {
  const normalized = String(text ?? "").replace(/\r\n/g, "\n");
  let removed = 0;
  const clean = normalized.replace(CLIPBOARD_CONTROLS, () => {
    removed += 1;
    return " ";
  });
  return { text: clean, removed };
}

/** Append a plain-language count when clipboard sanitization replaced controls. */
export function copyFeedback(base: string, removed: number): string {
  if (removed <= 0) return base;
  const s = base.trim();
  return `${s ? (/[.!?]$/.test(s) ? s : `${s}.`) : "Copied."} Removed ${removed} control character${removed === 1 ? "" : "s"}.`;
}

let copySanitizerInstalled = false;

function isInsideCodeMirror(node: unknown): boolean {
  if (!node || typeof Element === "undefined") return false;
  const el = node instanceof Element ? node : (node as Node).parentElement;
  return !!el?.closest?.(".cm-editor, .cm-content");
}

function isPasswordInput(node: unknown): boolean {
  return typeof HTMLInputElement !== "undefined" && node instanceof HTMLInputElement && node.type === "password";
}

function isTextField(node: unknown): node is HTMLInputElement | HTMLTextAreaElement {
  return (
    (typeof HTMLTextAreaElement !== "undefined" && node instanceof HTMLTextAreaElement) ||
    (typeof HTMLInputElement !== "undefined" && node instanceof HTMLInputElement)
  );
}

function readFieldSelection(field: HTMLInputElement | HTMLTextAreaElement): string | null {
  if (isPasswordInput(field)) return null;
  let start: number | null = null;
  let end: number | null = null;
  try {
    start = field.selectionStart;
    end = field.selectionEnd;
  } catch {
    return null;
  }
  return typeof start === "number" && typeof end === "number" && end > start
    ? field.value.slice(start, end)
    : null;
}

function selectedCopyText(event: ClipboardEvent): string | null {
  const target = event.target ?? null;
  if (isInsideCodeMirror(target) || isPasswordInput(target)) return null;
  if (isTextField(target)) {
    const fieldText = readFieldSelection(target);
    if (fieldText !== null) return fieldText;
  }
  const sel = typeof window !== "undefined" ? window.getSelection?.() : null;
  if (sel && !sel.isCollapsed) {
    if (isInsideCodeMirror(sel.anchorNode) || isInsideCodeMirror(sel.focusNode)) return null;
    return sel.toString();
  }
  const active = typeof document !== "undefined" ? document.activeElement : null;
  if (isInsideCodeMirror(active) || isPasswordInput(active)) return null;
  if (isTextField(active)) return readFieldSelection(active);
  return null;
}

/** Sanitize manual selection copies (Cmd+C / Ctrl+C / Edit > Copy) outside CodeMirror. */
export function handleSelectionCopy(event: ClipboardEvent): boolean {
  if (event.defaultPrevented || !event.clipboardData) return false;
  const raw = selectedCopyText(event);
  if (!raw) return false;
  const { text: clean } = sanitizeClipboardText(raw);
  if (clean === raw) return false;
  event.clipboardData.setData("text/plain", clean);
  event.preventDefault();
  return true;
}

/** Register one document-level copy listener for selection copies outside CodeMirror. */
export function installCopySanitizer(): () => void {
  if (typeof document === "undefined") return () => {};
  if (!copySanitizerInstalled) {
    copySanitizerInstalled = true;
    document.addEventListener("copy", handleSelectionCopy);
  }
  return () => {
    if (!copySanitizerInstalled) return;
    copySanitizerInstalled = false;
    document.removeEventListener("copy", handleSelectionCopy);
  };
}

export async function copyToClipboardResult(text: string): Promise<CopyResult> {
  const { text: clean, removed } = sanitizeClipboardText(text);
  try {
    await navigator.clipboard.writeText(clean);
    return { ok: true, text: clean, removed };
  } catch {
    let area: HTMLTextAreaElement | undefined;
    const prev = typeof document !== "undefined" && document.activeElement instanceof HTMLElement ? document.activeElement : null;
    try {
      area = document.createElement("textarea");
      area.value = clean;
      area.readOnly = true;
      area.style.cssText = "position:fixed;opacity:0";
      document.body.appendChild(area);
      area.select();
      return { ok: document.execCommand("copy"), text: clean, removed };
    } catch {
      return { ok: false, text: clean, removed };
    } finally {
      area?.remove();
      if (prev?.isConnected) prev.focus({ preventScroll: true });
    }
  }
}

export async function copyToClipboard(text: string): Promise<boolean> {
  return (await copyToClipboardResult(text)).ok;
}
