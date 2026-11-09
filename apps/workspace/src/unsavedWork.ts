// Whether closing the window now would lose work: main.tsx's beforeunload
// rule, shared with actions that close the window on purpose (Restart to
// update), which must ask first because the desktop app closes it anyway.
let check: (() => boolean) | null = null;

export function setUnsavedWorkCheck(next: (() => boolean) | null) {
  check = next;
}

/** True when the workspace has changes it could not save yet (or the check failed). */
export function hasUnsavedWork(): boolean {
  try {
    return !!check?.();
  } catch {
    return true;
  }
}
