// Shared clipboard copy path and control-character sanitizer. Kept in a leaf
// module with no top-level side effects so the entry chunk stays within its
// bundle-split budget while every caller shares one sanitizer.
import { clientCommandNote, withoutClientCommandNote } from "./sqlPasteSafety";

const CLIPBOARD_CONTROLS = /[\0-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069]/g;

export type CopyResult = { ok: boolean; text: string; removed: number; note: string };

/** Replace terminal, separator and bidi control characters with spaces before
 * text reaches the clipboard. Keeps TAB and LF; turns only CRLF into LF;
 * replaces lone CR, other C0 controls, DEL, C1 (U+0080-U+009F), Unicode
 * line/paragraph separators (U+2028-U+2029) and bidi controls/marks
 * (U+061C, U+200E, U+200F, U+202A-U+202E, U+2066-U+2069) with one ASCII space
 * each so adjacent tokens cannot merge and line comments, TSV rows and JSON
 * strings keep their boundaries. Immediately after --, non-ASCII controls
 * become _ instead: a space would create a SQL line comment where the UTF-8
 * client did not see one. Counts every replaced character in removed. */
export function sanitizeClipboardText(text: string): { text: string; removed: number } {
  const normalized = String(text ?? "").replace(/\r\n/g, "\n");
  let removed = 0;
  const clean = normalized.replace(CLIPBOARD_CONTROLS, (ch: string, offset: number) => {
    removed += 1;
    // Replacements cannot end in a hyphen, so the two preceding input
    // characters are also the output suffix whenever it ends in --.
    return ch.charCodeAt(0) >= 0x80 && normalized[offset - 1] === "-" && normalized[offset - 2] === "-" ? "_" : " ";
  });
  return { text: clean, removed };
}

/** Count replacements and optionally append the already-computed note once.
 * Legacy caller notes are replaced by the central clipboard status, ensuring
 * callers that have not opted into warnings also receive the same notice. */
export function copyFeedback(base: string, removed: number, note = ""): string {
  const plain = withoutClientCommandNote(base);
  const s = plain.trim();
  let feedback = removed > 0
    ? `${s ? (/[.!?]$/.test(s) ? s : `${s}.`) : "Copied."} Removed ${removed} control character${removed === 1 ? "" : "s"}.`
    : plain;
  if (note) {
    feedback = feedback.trim();
    feedback = `${feedback ? (/[.!?]$/.test(feedback) ? feedback : `${feedback}.`) : "Copied."}${note}`;
  }
  return feedback;
}

let copyNotice: HTMLDivElement | null = null;
let noticeTimer: ReturnType<typeof setTimeout> | undefined;

function clearCopyNotice(): void {
  if (noticeTimer !== undefined) clearTimeout(noticeTimer);
  noticeTimer = undefined;
  copyNotice?.remove();
  copyNotice = null;
}

/** One shared status reaches every copy caller, including the boolean helper.
 * Text content is used because copied text and command names are untrusted. */
function showCopyNotice(message: string): void {
  if (typeof document === "undefined" || !document.body) return;
  try {
    clearCopyNotice();
    const notice = document.createElement("div");
    notice.id = "myflames-copy-notice";
    notice.className = "table-copy-toast";
    notice.setAttribute("role", "status");
    notice.setAttribute("aria-live", "polite");
    notice.setAttribute("aria-atomic", "true");
    // Reuse the workspace toast's theme; allow time to read a longer note
    // and keep it above the caller's short confirmation at the window foot.
    notice.style.cssText = "bottom:64px;width:max-content;max-width:min(70ch,calc(100vw - 32px));white-space:normal;animation:none;z-index:2147483647";
    document.body.appendChild(notice);
    notice.textContent = message;
    copyNotice = notice;
    noticeTimer = setTimeout(() => {
      notice.remove();
      if (copyNotice === notice) copyNotice = null;
      noticeTimer = undefined;
    }, 12000);
  } catch {
    // A status failure must not turn an already successful copy into failure.
  }
}

let copySanitizerInstalled = false;
let fallbackCopyInProgress = false;

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
  if (isPasswordInput(target)) return null;
  if (isTextField(target)) {
    const fieldText = readFieldSelection(target);
    if (fieldText !== null) return fieldText;
  }
  const sel = typeof window !== "undefined" ? window.getSelection?.() : null;
  if (sel && !sel.isCollapsed) {
    // A page selection can end inside the editor without belonging to it.
    // Leave an editor-owned selection alone; sanitize cross-boundary ranges.
    if (isInsideCodeMirror(sel.anchorNode) && isInsideCodeMirror(sel.focusNode)) return null;
    return sel.toString();
  }
  if (isInsideCodeMirror(target)) return null;
  const active = typeof document !== "undefined" ? document.activeElement : null;
  if (isInsideCodeMirror(active) || isPasswordInput(active)) return null;
  if (isTextField(active)) return readFieldSelection(active);
  return null;
}

/** Sanitize manual selection copies (Cmd+C / Ctrl+C / Edit > Copy) outside CodeMirror. */
export function handleSelectionCopy(event: ClipboardEvent): boolean {
  // The fallback textarea already contains sanitized text, and this helper
  // reports its result only after execCommand confirms that copying succeeded.
  if (fallbackCopyInProgress || event.defaultPrevented || !event.clipboardData) return false;
  const raw = selectedCopyText(event);
  if (!raw) return false;
  const { text: clean, removed } = sanitizeClipboardText(raw);
  const note = clientCommandNote(clean);
  clearCopyNotice();
  if (clean === raw) {
    if (note) showCopyNotice(copyFeedback("Copied.", 0, note));
    return false;
  }
  event.clipboardData.setData("text/plain", clean);
  event.preventDefault();
  showCopyNotice(copyFeedback("Copied.", removed, note));
  return true;
}

/** Register one document-level copy listener for selection copies outside CodeMirror. */
export function installCopySanitizer(): () => void {
  if (typeof document === "undefined") return () => {};
  if (!copySanitizerInstalled) {
    document.addEventListener("copy", handleSelectionCopy);
    copySanitizerInstalled = true;
  }
  return () => {
    if (!copySanitizerInstalled) return;
    copySanitizerInstalled = false;
    document.removeEventListener("copy", handleSelectionCopy);
  };
}

export async function copyToClipboardResult(text: string): Promise<CopyResult> {
  const { text: clean, removed } = sanitizeClipboardText(text);
  // Compute against the actual clipboard bytes once, before either transport.
  const note = clientCommandNote(clean);
  const result = (ok: boolean): CopyResult => {
    clearCopyNotice();
    if (ok && note) showCopyNotice(copyFeedback("Copied.", removed, note));
    return { ok, text: clean, removed, note };
  };
  try {
    await navigator.clipboard.writeText(clean);
    return result(true);
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
      fallbackCopyInProgress = true;
      return result(document.execCommand("copy"));
    } catch {
      return result(false);
    } finally {
      fallbackCopyInProgress = false;
      area?.remove();
      if (prev?.isConnected) prev.focus({ preventScroll: true });
    }
  }
}

export async function copyToClipboard(text: string): Promise<boolean> {
  return (await copyToClipboardResult(text)).ok;
}
