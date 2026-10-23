/** macOS (and iOS) name shortcuts with ⌘ and ⌥; Windows and Linux with Ctrl and Alt. */
export const MAC =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);

/** The visible label of a Command/Control shortcut: shortcut("K") is ⌘K on
 *  macOS and Ctrl+K elsewhere; with alt, ⌥⌘R and Ctrl+Alt+R. Handlers accept
 *  either modifier, so only the label differs. */
export function shortcut(key: string, alt = false): string {
  return MAC ? `${alt ? "⌥" : ""}⌘${key}` : `Ctrl+${alt ? "Alt+" : ""}${key}`;
}
