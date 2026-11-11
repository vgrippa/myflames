/** Picker metadata: credit and preview swatches per theme, keyed by stable
 * id. Only the lazy Settings chunk imports this module, so it stays out of
 * the entry chunk. */
import { THEME_CHOICES, type ThemeId } from "./themes";
const PICKER_DETAILS = {
  "system": {
    "credit": "myflames",
    "swatches": [
      "#ffffff",
      "#151719",
      "#007aff"
    ],
    "syntax": [
      "#0b4f9c",
      "#a31515"
    ]
  },
  "light": {
    "credit": "myflames",
    "swatches": [
      "#ffffff",
      "#f5f5f7",
      "#007aff"
    ],
    "syntax": [
      "#0b4f9c",
      "#a31515"
    ]
  },
  "dark": {
    "credit": "myflames",
    "swatches": [
      "#151719",
      "#25282c",
      "#0a84ff"
    ],
    "syntax": [
      "#6cb6ff",
      "#ff9b8f"
    ]
  },
  "fire": {
    "credit": "myflames original (Apache-2.0)",
    "swatches": [
      "#181210",
      "#261c18",
      "#f7ece6",
      "#ff7a33"
    ],
    "syntax": [
      "#ff7a33",
      "#ffc857"
    ]
  },
  "digital-rain": {
    "credit": "myflames original (Apache-2.0)",
    "swatches": [
      "#050a06",
      "#0d1910",
      "#d4ffd8",
      "#00ff41"
    ],
    "syntax": [
      "#00ff41",
      "#8aff9e"
    ]
  },
  "neon-grid": {
    "credit": "myflames original (Apache-2.0)",
    "swatches": [
      "#070b19",
      "#101830",
      "#e6f7ff",
      "#00f0ff"
    ],
    "syntax": [
      "#ff851b",
      "#6df0ff"
    ]
  },
  "dracula": {
    "credit": "Based on Dracula (MIT)",
    "swatches": [
      "#282a36",
      "#44475a",
      "#f8f8f2",
      "#bd93f9"
    ],
    "syntax": [
      "#ff79c6",
      "#f1fa8c"
    ]
  },
  "nord": {
    "credit": "Based on Nord (MIT)",
    "swatches": [
      "#2e3440",
      "#3b4252",
      "#d8dee9",
      "#88c0d0"
    ],
    "syntax": [
      "#81a1c1",
      "#a3be8c"
    ]
  },
  "solarized-light": {
    "credit": "Based on Solarized (MIT)",
    "swatches": [
      "#fdf6e3",
      "#eee8d5",
      "#576a71",
      "#2689d0"
    ],
    "syntax": [
      "#687800",
      "#217d76"
    ]
  },
  "solarized-dark": {
    "credit": "Based on Solarized (MIT)",
    "swatches": [
      "#002b36",
      "#073642",
      "#8d9d9e",
      "#268bd2"
    ],
    "syntax": [
      "#859900",
      "#2aa198"
    ]
  },
  "tokyo-night": {
    "credit": "Based on Tokyo Night (MIT)",
    "swatches": [
      "#1a1b26",
      "#24283b",
      "#a9b1d6",
      "#7aa2f7"
    ],
    "syntax": [
      "#bb9af7",
      "#9ece6a"
    ]
  },
  "gruvbox-dark": {
    "credit": "Based on Gruvbox (MIT)",
    "swatches": [
      "#282828",
      "#3c3836",
      "#ebdbb2",
      "#fe8019"
    ],
    "syntax": [
      "#fb5642",
      "#b8bb26"
    ]
  },
  "catppuccin-mocha": {
    "credit": "Based on Catppuccin (MIT)",
    "swatches": [
      "#1e1e2e",
      "#313244",
      "#cdd6f4",
      "#cba6f7"
    ],
    "syntax": [
      "#cba6f7",
      "#a6e3a1"
    ]
  },
  "catppuccin-latte": {
    "credit": "Based on Catppuccin (MIT)",
    "swatches": [
      "#eff1f5",
      "#ccd0da",
      "#4c4f69",
      "#8839ef"
    ],
    "syntax": [
      "#8839ef",
      "#327c21"
    ]
  },
  "github-light": {
    "credit": "Based on GitHub themes (Primer) (MIT)",
    "swatches": [
      "#ffffff",
      "#f6f8fa",
      "#1f2328",
      "#0969da"
    ],
    "syntax": [
      "#cf222e",
      "#0a3069"
    ]
  },
  "github-dark": {
    "credit": "Based on GitHub themes (Primer) (MIT)",
    "swatches": [
      "#0d1117",
      "#161b22",
      "#e6edf3",
      "#2f81f7"
    ],
    "syntax": [
      "#ff7b72",
      "#a5d6ff"
    ]
  },
  "one-dark": {
    "credit": "Based on Atom One Dark (MIT)",
    "swatches": [
      "#282c34",
      "#21252b",
      "#abb2bf",
      "#61afef"
    ],
    "syntax": [
      "#c678dd",
      "#98c379"
    ]
  }
} satisfies Record<ThemeId, { credit: string; swatches: readonly string[]; syntax: readonly string[] }>;
/** Full registry: palette attribution and swatches are keyed by stable id. */
export const THEMES = /* @__PURE__ */ THEME_CHOICES.map(theme => ({ ...theme, ...PICKER_DETAILS[theme.id] }));
