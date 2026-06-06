import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0b1020",
        panel: "#121830",
        card: "#1a2240",
        border: "#2a335a",
        ink: "#e8ecf6",
        muted: "#9aa3c0",
        accent: "#5b8def",
        scan: "#ef4444",
        index: "#2563eb",
        join: "#a855f7",
        cache: "#f59e0b",
        planner: "#ec4899",
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'Menlo', 'monospace'],
      },
      boxShadow: {
        glow: "0 0 0 1px rgba(91,141,239,0.4), 0 8px 32px rgba(91,141,239,0.15)",
      },
    },
  },
  plugins: [],
} satisfies Config;
