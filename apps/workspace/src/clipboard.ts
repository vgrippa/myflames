// Shared clipboard copy path and control-character sanitizer. Kept in a leaf
// module with no top-level side effects so the entry chunk stays within its
// bundle-split budget while every caller shares one sanitizer.

const CLIPBOARD_CONTROLS = /[\0-\x08\v\f\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g;

export type CopyResult = { ok: boolean; text: string; removed: number };

/** Strip terminal and bidi control characters before text reaches the clipboard.
 * Keeps TAB and LF; turns CRLF, lone CR and U+2028/U+2029 into LF; removes
 * other C0 controls, DEL, C1 (U+0080-U+009F) and bidi embedding, override and
 * isolate controls (U+202A-U+202E, U+2066-U+2069). */
export function sanitizeClipboardText(text: string): { text: string; removed: number } {
  const normalized = String(text ?? "").replace(/\r\n|[\r\u2028\u2029]/g, "\n");
  const clean = normalized.replace(CLIPBOARD_CONTROLS, "");
  return { text: clean, removed: normalized.length - clean.length };
}

/** Append a plain-language count when clipboard sanitization removed controls. */
export function copyFeedback(base: string, removed: number): string {
  if (removed <= 0) return base;
  const s = base.trim();
  return `${s ? (/[.!?]$/.test(s) ? s : `${s}.`) : "Copied."} Removed ${removed} control character${removed === 1 ? "" : "s"}.`;
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
