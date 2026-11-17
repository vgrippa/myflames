// The font choices of Settings > Appearance and how they reach the page.
// "Interface font" and "Interface text size" set --font-sans and scale the
// --text-* tokens on the root element (13 px is the designed size: the scale
// is 1 and every token keeps its stylesheet value); "Code font" and "Code
// text size" set --font-mono and --text-code (the Query editor and pre/code
// text). The ids are a fixed allowlist, validated again by the workspace
// store (workspace_store.py UI_FONT_IDS and UI_CODE_FONT_IDS keep the same
// lists): no CSS can reach the page through a preference. No editor or lazy
// view imports.
import { currentUiPreferences, subscribeUiPreferences, type UiPreferences } from "./uiPreferences";

export type FontKind = "interface" | "code";
export type FontChoice = {
  id: string;
  label: string;
  /** The CSS font stack applied for this choice. */
  stack: string;
  kind: FontKind;
  /** Ships with the app (a @font-face in style.css), so it is always offered. */
  shipped: boolean;
};
export const UI_TEXT_SIZE_MIN = 12;
export const UI_TEXT_SIZE_MAX = 16;
export const UI_TEXT_SIZE_DEFAULT = 13;
export const CODE_TEXT_SIZE_MIN = 11;
export const CODE_TEXT_SIZE_MAX = 20;
export const CODE_TEXT_SIZE_DEFAULT = 13;

// The stylesheet's own stacks (style.css): the "System default" choices.
const SYSTEM_SANS = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro", "Helvetica Neue", "Segoe UI", Inter, Roboto, sans-serif';
const SYSTEM_MONO = '"SF Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace';
const sans = (family: string) => `"${family}", ${SYSTEM_SANS}`;
const mono = (family: string) => `"${family}", ${SYSTEM_MONO}`;

/** Every offerable font. Installed families are listed only when detected;
 * keep these ids equal to UI_FONT_IDS and UI_CODE_FONT_IDS in
 * myflames/workspace_store.py. */
export const FONT_CHOICES: readonly FontChoice[] = [
  { id: "system", label: "System default", stack: SYSTEM_SANS, kind: "interface", shipped: false },
  { id: "inter", label: "Inter", stack: sans("Inter"), kind: "interface", shipped: true },
  { id: "sf-pro", label: "SF Pro", stack: sans("SF Pro"), kind: "interface", shipped: false },
  { id: "helvetica-neue", label: "Helvetica Neue", stack: sans("Helvetica Neue"), kind: "interface", shipped: false },
  { id: "segoe-ui", label: "Segoe UI", stack: sans("Segoe UI"), kind: "interface", shipped: false },
  { id: "arial", label: "Arial", stack: sans("Arial"), kind: "interface", shipped: false },
  { id: "roboto", label: "Roboto", stack: sans("Roboto"), kind: "interface", shipped: false },
  { id: "open-sans", label: "Open Sans", stack: sans("Open Sans"), kind: "interface", shipped: false },
  { id: "source-sans-3", label: "Source Sans 3", stack: sans("Source Sans 3"), kind: "interface", shipped: false },
  { id: "ibm-plex-sans", label: "IBM Plex Sans", stack: sans("IBM Plex Sans"), kind: "interface", shipped: false },
  { id: "noto-sans", label: "Noto Sans", stack: sans("Noto Sans"), kind: "interface", shipped: false },
  { id: "verdana", label: "Verdana", stack: sans("Verdana"), kind: "interface", shipped: false },
  { id: "system", label: "System default", stack: SYSTEM_MONO, kind: "code", shipped: false },
  { id: "jetbrains-mono", label: "JetBrains Mono", stack: mono("JetBrains Mono"), kind: "code", shipped: true },
  { id: "sf-mono", label: "SF Mono", stack: mono("SF Mono"), kind: "code", shipped: false },
  { id: "menlo", label: "Menlo", stack: mono("Menlo"), kind: "code", shipped: false },
  { id: "monaco", label: "Monaco", stack: mono("Monaco"), kind: "code", shipped: false },
  { id: "consolas", label: "Consolas", stack: mono("Consolas"), kind: "code", shipped: false },
  { id: "cascadia-mono", label: "Cascadia Mono", stack: mono("Cascadia Mono"), kind: "code", shipped: false },
  { id: "cascadia-code", label: "Cascadia Code", stack: mono("Cascadia Code"), kind: "code", shipped: false },
  { id: "fira-code", label: "Fira Code", stack: mono("Fira Code"), kind: "code", shipped: false },
  { id: "source-code-pro", label: "Source Code Pro", stack: mono("Source Code Pro"), kind: "code", shipped: false },
  { id: "ibm-plex-mono", label: "IBM Plex Mono", stack: mono("IBM Plex Mono"), kind: "code", shipped: false },
  { id: "roboto-mono", label: "Roboto Mono", stack: mono("Roboto Mono"), kind: "code", shipped: false },
  { id: "ubuntu-mono", label: "Ubuntu Mono", stack: mono("Ubuntu Mono"), kind: "code", shipped: false },
  { id: "hack", label: "Hack", stack: mono("Hack"), kind: "code", shipped: false },
  { id: "courier-new", label: "Courier New", stack: mono("Courier New"), kind: "code", shipped: false },
];

declare global {
  interface Window {
    /** Test seam: overrides installed-font detection (settings-page.mjs). */
    __myflamesDetectFont?: (family: string) => boolean;
  }
}

/** Installed-font detection by text measurement. document.fonts.check is not
 * reliable for local fonts, so a probe string is measured against each
 * generic family: a different width means the named family rendered. */
function detectInstalled(family: string): boolean {
  if (window.__myflamesDetectFont) return window.__myflamesDetectFont(family);
  const canvas = document.createElement("canvas").getContext("2d");
  if (!canvas) return false;
  const probe = "mmmmmmmmmmlliWwQq@123";
  for (const generic of ["monospace", "sans-serif", "serif"]) {
    canvas.font = `72px ${generic}`;
    const fallback = canvas.measureText(probe).width;
    canvas.font = `72px "${family}", ${generic}`;
    if (Math.abs(canvas.measureText(probe).width - fallback) > 0.5) return true;
  }
  return false;
}

let detected: Record<FontKind, FontChoice[]> | null = null;
/** The choices Settings offers: System default, the shipped fonts, and the
 * installed families (detected once per launch). */
export function fontChoices(kind: FontKind): FontChoice[] {
  if (!detected) {
    detected = { interface: [], code: [] };
    for (const kind of ["interface", "code"] as const) {
      detected[kind] = FONT_CHOICES.filter(choice =>
        choice.kind === kind && (choice.id === "system" || choice.shipped || detectInstalled(choice.label)));
    }
  }
  return detected[kind];
}

function fontById(kind: FontKind, id: string): FontChoice {
  return FONT_CHOICES.find(choice => choice.kind === kind && choice.id === id)
    || FONT_CHOICES.find(choice => choice.kind === kind && choice.id === "system")!;
}

// The designed sizes at 13 px (style.css). The interface size scales them;
// at 13 the scale is 1 and every token keeps its stylesheet value exactly.
// Text tokens never go below the 11 px floor the type scale documents.
const TEXT_SCALE_TOKENS: readonly (readonly [string, number])[] = [
  ["--text-xs", 11], ["--text-sm", 12], ["--text-md", 13], ["--text-base", 13],
  ["--text-lg", 15], ["--text-xl", 20], ["--text-2xl", 28], ["--text-title", 15],
];
const LEADING_SCALE_TOKENS: readonly (readonly [string, number])[] = [
  ["--leading-xs", 16], ["--leading-sm", 18], ["--leading-md", 20],
  ["--leading-lg", 22], ["--leading-xl", 28], ["--leading-2xl", 34], ["--leading-title", 20],
];
const clampSize = (value: number, min: number, max: number, fallback: number) =>
  Number.isInteger(value) && value >= min && value <= max ? value : fallback;

/** Apply the preferences on the root element: inline custom properties beat
 * the stylesheet and any theme override. "system" removes the override so
 * the stylesheet's own stack (and themes) apply. */
export function applyFonts(preferences: UiPreferences): void {
  const root = document.documentElement;
  const ui = fontById("interface", preferences.ui_font);
  const code = fontById("code", preferences.code_font);
  if (ui.id === "system") root.style.removeProperty("--font-sans");
  else root.style.setProperty("--font-sans", ui.stack);
  if (code.id === "system") root.style.removeProperty("--font-mono");
  else root.style.setProperty("--font-mono", code.stack);
  const scale = clampSize(preferences.ui_text_size, UI_TEXT_SIZE_MIN, UI_TEXT_SIZE_MAX, UI_TEXT_SIZE_DEFAULT) / UI_TEXT_SIZE_DEFAULT;
  for (const [name, base] of TEXT_SCALE_TOKENS) root.style.setProperty(name, `${Math.max(11, Math.round(base * scale))}px`);
  for (const [name, base] of LEADING_SCALE_TOKENS) root.style.setProperty(name, `${Math.round(base * scale)}px`);
  // A scale above the designed size flips the narrow-width choices that
  // assume the designed metrics: the tile strip wraps instead of scrolling
  // sideways (kit.css) and the status bar's safety detail yields first
  // (style.css). At the designed size and below nothing changes.
  if (scale > 1) root.dataset.textScale = "large";
  else delete root.dataset.textScale;
  const codeSize = clampSize(preferences.code_text_size, CODE_TEXT_SIZE_MIN, CODE_TEXT_SIZE_MAX, CODE_TEXT_SIZE_DEFAULT);
  root.style.setProperty("--text-code", `${codeSize}px`);
  // The editor's line height follows its text size (20 px at 13 px text).
  root.style.setProperty("--editor-line-h", `${Math.round(codeSize * 20 / CODE_TEXT_SIZE_DEFAULT)}px`);
}

// Settings and palette actions share persistence: every preference publish
// repaints the fonts, and startup paints the defaults until the store's
// answer lands (main.tsx awaits it before the first workspace render).
applyFonts(currentUiPreferences());
subscribeUiPreferences(applyFonts);
