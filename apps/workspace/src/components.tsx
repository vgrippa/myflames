import React from "react";
import { icons, type IconShape } from "./icons.generated";
/**
 * Icons are named by meaning, and each meaning has its own glyph: a place in
 * the sidebar never shares a drawing with another place or with an action.
 * 16 px in rows and navigation; 12 px disclosures and inline status glyphs.
 */
export type IconName =
  | "home"
  | "plan"
  | "query"
  | "server"
  | "investigation"
  | "learn"
  | "connection"
  | "key"
  | "settings"
  | "menu"
  | "more"
  | "import"
  | "export"
  | "compare"
  | "plus"
  | "close"
  | "chevron"
  | "search"
  | "check"
  // Server views (one glyph each) and view controls.
  | "health"
  | "engine"
  | "analysis"
  | "tuning"
  | "errorlog"
  | "statements"
  | "sessions"
  | "memory"
  | "waits"
  | "io"
  | "lock"
  | "index"
  | "table"
  | "diagram"
  | "configuration"
  | "instruments"
  | "replication"
  | "cluster"
  | "topology"
  | "proxy"
  | "readyset"
  | "maintenance"
  | "info"
  | "refresh"
  | "back"
  | "disclosure"
  | "warning"
  | "columns"
  | "inspector"
  | "audit"
  // Query editor actions and panes.
  | "run"
  | "runAll"
  | "stop"
  | "history"
  | "copy"
  | "browser"
  | "view"
  | "baseline"
  | "database"
  | "teach"
  | "users"
  | "wrench"
  | "split-pane"
  | "chevron-down"
  | "branches"
  | "edit"
  | "trash"
  | "globe"
  | "optimize"
  | "shield"
  | "minus"
  | "block"
  | "clean"
  | "undo"
  // Query editor: Format SQL and Find and replace.
  | "format"
  | "replace"
  // Learn: show a lesson's animation full screen, and leave it.
  | "fullscreen"
  | "exit-fullscreen"
  // Files: the place, New folder and Up (row glyphs: fileGlyphs.generated.ts).
  | "files"
  | "folder-add"
  | "up";
// Glyphs are Hugeicons (free Stroke Rounded set, MIT) vendored into
// icons.generated.ts by scripts/vendor-hugeicons.mjs, drawn on a 24 px grid and
// scaled to 16 px (rows, navigation) or 12 px (disclosures, inline status).
// Memory's DIMM glyph is local; the vendored icon set stays generated.
const data: Record<IconName, IconShape> = { ...icons, memory: [
  ["rect", { x: "3", y: "6", width: "18", height: "12", rx: "2" }],
  ["path", { d: "M7 9v5h3V9H7ZM14 9v5h3V9h-3ZM6 18v3M10 18v3M14 18v3M18 18v3" }],
] };
export function isIconName(name: string): name is IconName {
  return Object.prototype.hasOwnProperty.call(data, name);
}
export function Icon({ name, size }: { name: IconName; size?: number }) {
  const inline = ["chevron", "disclosure", "back", "check", "warning"].includes(name);
  const pixels = size && size > 16 ? size : inline || size === 12 ? 12 : 16;
  return (
    <svg width={pixels} height={pixels} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth={pixels <= 12 ? 2 : 1.75}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {data[name].map(([tag, attrs], i) => React.createElement(tag, { key: i, ...attrs }))}
    </svg>
  );
}
