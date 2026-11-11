import { currentUiPreferences, setUiPreference, subscribeUiPreferences } from "./uiPreferences";
/** Stable ids, labels and bases are enough for the shell and boot script.
 * Picker metadata stays tree-shakeable until the Settings picker imports it. */
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
const PICKER_DETAILS = {
  "system": {
    "credit": "myflames",
    "swatches": [
      "#ffffff",
      "#151719",
      "#007aff"
    ]
  },
  "light": {
    "credit": "myflames",
    "swatches": [
      "#ffffff",
      "#f5f5f7",
      "#007aff"
    ]
  },
  "dark": {
    "credit": "myflames",
    "swatches": [
      "#151719",
      "#25282c",
      "#0a84ff"
    ]
  },
  "fire": {
    "credit": "myflames original (Apache-2.0)",
    "swatches": [
      "#181210",
      "#261c18",
      "#f7ece6",
      "#ff7a33"
    ]
  },
  "digital-rain": {
    "credit": "myflames original (Apache-2.0)",
    "swatches": [
      "#050a06",
      "#0d1910",
      "#d4ffd8",
      "#00ff41"
    ]
  },
  "neon-grid": {
    "credit": "myflames original (Apache-2.0)",
    "swatches": [
      "#070b19",
      "#101830",
      "#e6f7ff",
      "#00f0ff"
    ]
  },
  "dracula": {
    "credit": "Based on Dracula (MIT)",
    "swatches": [
      "#282a36",
      "#44475a",
      "#f8f8f2",
      "#bd93f9"
    ]
  },
  "nord": {
    "credit": "Based on Nord (MIT)",
    "swatches": [
      "#2e3440",
      "#3b4252",
      "#d8dee9",
      "#88c0d0"
    ]
  },
  "solarized-light": {
    "credit": "Based on Solarized (MIT)",
    "swatches": [
      "#fdf6e3",
      "#eee8d5",
      "#576a71",
      "#2689d0"
    ]
  },
  "solarized-dark": {
    "credit": "Based on Solarized (MIT)",
    "swatches": [
      "#002b36",
      "#073642",
      "#8d9d9e",
      "#268bd2"
    ]
  },
  "tokyo-night": {
    "credit": "Based on Tokyo Night (MIT)",
    "swatches": [
      "#1a1b26",
      "#24283b",
      "#a9b1d6",
      "#7aa2f7"
    ]
  },
  "gruvbox-dark": {
    "credit": "Based on Gruvbox (MIT)",
    "swatches": [
      "#282828",
      "#3c3836",
      "#ebdbb2",
      "#fe8019"
    ]
  },
  "catppuccin-mocha": {
    "credit": "Based on Catppuccin (MIT)",
    "swatches": [
      "#1e1e2e",
      "#313244",
      "#cdd6f4",
      "#cba6f7"
    ]
  },
  "catppuccin-latte": {
    "credit": "Based on Catppuccin (MIT)",
    "swatches": [
      "#eff1f5",
      "#ccd0da",
      "#4c4f69",
      "#8839ef"
    ]
  },
  "github-light": {
    "credit": "Based on GitHub themes (Primer) (MIT)",
    "swatches": [
      "#ffffff",
      "#f6f8fa",
      "#1f2328",
      "#0969da"
    ]
  },
  "github-dark": {
    "credit": "Based on GitHub themes (Primer) (MIT)",
    "swatches": [
      "#0d1117",
      "#161b22",
      "#e6edf3",
      "#2f81f7"
    ]
  },
  "one-dark": {
    "credit": "Based on Atom One Dark (MIT)",
    "swatches": [
      "#282c34",
      "#21252b",
      "#abb2bf",
      "#61afef"
    ]
  }
} satisfies Record<ThemeId, { credit: string; swatches: readonly string[] }>;
/** Full registry: palette attribution and swatches are keyed by stable id. */
export const THEMES = /* @__PURE__ */ THEME_CHOICES.map(theme => ({ ...theme, ...PICKER_DETAILS[theme.id] }));
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
