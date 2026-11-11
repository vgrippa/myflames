import { currentUiPreferences, setUiPreference, subscribeUiPreferences } from "./uiPreferences";
/** Stable ids, labels and bases are enough for the shell and boot script.
 * Picker metadata (credit and swatches) lives in theme-picker.ts, which only
 * the lazy Settings chunk imports, so it stays out of the entry chunk. */
// Dark is the default base; light palettes declare their override.
const THEME_DEFINITIONS = [
  [
    "system",
    "System",
    "light"
  ],
  [
    "light",
    "Light",
    "light"
  ],
  [
    "dark",
    "Dark"
  ],
  [
    "fire",
    "Fire"
  ],
  [
    "digital-rain",
    "Digital Rain"
  ],
  [
    "neon-grid",
    "Neon Grid"
  ],
  [
    "dracula",
    "Dracula"
  ],
  [
    "nord",
    "Nord"
  ],
  [
    "solarized-light",
    "Solarized Light",
    "light"
  ],
  [
    "solarized-dark",
    "Solarized Dark"
  ],
  [
    "tokyo-night",
    "Tokyo Night"
  ],
  [
    "gruvbox-dark",
    "Gruvbox Dark"
  ],
  [
    "catppuccin-mocha",
    "Catppuccin Mocha"
  ],
  [
    "catppuccin-latte",
    "Catppuccin Latte",
    "light"
  ],
  [
    "github-light",
    "GitHub Light",
    "light"
  ],
  [
    "github-dark",
    "GitHub Dark"
  ],
  [
    "one-dark",
    "One Dark"
  ]
] as const;
export const THEME_CHOICES = /* @__PURE__ */ THEME_DEFINITIONS.map(([id, name, base]) => ({ id, name, base: base ?? ("dark" as const) }));
export type ThemeId = typeof THEME_CHOICES[number]["id"];
export type ThemeBase = "light" | "dark";
export const THEME_IDS: readonly ThemeId[] = THEME_CHOICES.map(theme => theme.id);
export function isThemeId(value: unknown): value is ThemeId {
  return THEME_IDS.includes(value as ThemeId);
}
/** The root is authoritative even when persistence is unavailable. */
export function appliedThemeId(root: HTMLElement = document.documentElement): ThemeId {
  return isThemeId(root.dataset.theme) ? root.dataset.theme : "system";
}
export function appliedThemeBase(root: HTMLElement = document.documentElement): ThemeBase {
  const theme = THEME_CHOICES.find(theme => theme.id === root.dataset.theme && theme.id !== "system");
  return theme?.base ?? (root.ownerDocument.defaultView?.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
export function savedTheme(): ThemeId {
  const value = currentUiPreferences().theme;
  return isThemeId(value) ? value : "system";
}
function paintTheme(id: ThemeId): void {
  if (!isThemeId(id)) return;
  if (id === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = id;
}
// Settings and palette actions share persistence and failed-write recovery.
subscribeUiPreferences(value => paintTheme(value.theme));
export function applyTheme(id: ThemeId): void {
  if (!isThemeId(id)) return;
  void setUiPreference("theme", id).catch(() => {});
}
