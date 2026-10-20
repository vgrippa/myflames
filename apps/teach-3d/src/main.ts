// Entry point of the 3D B+tree view: controls, chapters, toolbar, readout.
import { CHAPTERS, type Context, type Ui } from "./chapters";
import { DEFAULTS, OPTIONS, type Inputs, type Shape, indexShapes } from "./model";
import { COLORS, PAGE_BUDGET, GROUP_BUDGET, type TreeSpec, World, webglAvailable } from "./scene";
import { Player, type Timeline } from "./timeline";
import { bytes, fmt, kib, plural, short } from "./format";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
// ?nowebgl forces the text-only fallback (used by the smoke test).
let gpu = !/[?&]nowebgl\b/.test(location.search) && webglAvailable();

const state = { input: { ...DEFAULTS } as Inputs, chapter: 0, covering: false };
const viewport = $<HTMLDivElement>("viewport");
let world: World;
try {
  world = new World(viewport, gpu);
} catch {
  // A context can still fail after the probe (lost GPU, blocked driver).
  gpu = false;
  world = new World(viewport, false);
}
$("fallback").hidden = gpu;
$("hint-drag").hidden = !gpu;

// ---- narration and counters, written by the timeline on every frame ----

const frame = { say: "", pages: 0, rows: undefined as number | undefined };
const ui: Ui = {
  say(text) {
    frame.say = text;
  },
  read(pages, rows) {
    frame.pages = pages;
    frame.rows = rows;
  },
};

let ctx: Context;
let timeline: Timeline;

const player = new Player(
  {
    render(time) {
      world.resetState();
      frame.say = "";
      frame.pages = 0;
      frame.rows = undefined;
      timeline.apply(time);
      world.sync();
      flush();
    },
    changed: updateToolbar,
  },
  reducedMotion,
);

world.onUserCamera = () => {
  $("btn-follow").hidden = false;
};

// ---- controls ------------------------------------------------------------

function fillSelect(id: string, values: number[], label: (v: number) => string, current: number): void {
  const select = $<HTMLSelectElement>(id);
  select.replaceChildren(
    ...values.map((v) => {
      const option = document.createElement("option");
      option.value = String(v);
      option.textContent = label(v);
      option.selected = v === current;
      return option;
    }),
  );
}

fillSelect("in-rows", OPTIONS.rows, (v) => short(v) + " rows", state.input.rows);
fillSelect("in-row", OPTIONS.rowBytes, (v) => v + " bytes", state.input.rowBytes);
fillSelect("in-email", OPTIONS.emailBytes, (v) => v + " bytes", state.input.emailBytes);
fillSelect("in-page", OPTIONS.pageKib, (v) => kib(v) + (v === 16 ? " (default)" : ""), state.input.pageKib);

const inputs: [string, keyof Inputs][] = [
  ["in-rows", "rows"],
  ["in-row", "rowBytes"],
  ["in-email", "emailBytes"],
  ["in-page", "pageKib"],
];
for (const [id, key] of inputs) {
  $<HTMLSelectElement>(id).addEventListener("change", (event) => {
    state.input[key] = Number((event.target as HTMLSelectElement).value);
    rebuild();
  });
}
$<HTMLInputElement>("covering").addEventListener("change", (event) => {
  state.covering = (event.target as HTMLInputElement).checked;
  rebuild();
});

const chapterNav = $("chapters");
CHAPTERS.forEach((chapter, i) => {
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.chapter = chapter.id;
  const n = document.createElement("span");
  n.className = "n";
  n.textContent = String(i + 1);
  button.append(n, chapter.title);
  button.addEventListener("click", () => openChapter(i));
  chapterNav.append(button);
});

function openChapter(i: number): void {
  state.chapter = i;
  world.follow = true;
  $("btn-follow").hidden = true;
  rebuild();
}

// ---- toolbar ---------------------------------------------------------------

$("btn-play").addEventListener("click", () => {
  if (!player.playing || player.ended) player.play();
  else if (player.paused) player.resume();
  else player.pause();
});
$("btn-reset").addEventListener("click", () => player.reset());
$<HTMLSelectElement>("speed").addEventListener("change", (event) => {
  player.speed = Number((event.target as HTMLSelectElement).value);
});
$<HTMLInputElement>("scrubber").addEventListener("input", (event) => {
  player.seek((Number((event.target as HTMLInputElement).value) / 1000) * player.total);
});
$("btn-follow").addEventListener("click", () => {
  world.follow = true;
  $("btn-follow").hidden = true;
  player.seek(player.time);
});
$("btn-next").addEventListener("click", () => openChapter(Math.min(state.chapter + 1, CHAPTERS.length - 1)));

function updateToolbar(): void {
  const play = $("btn-play");
  play.textContent = !player.playing ? (player.ended ? "Replay" : "Play") : player.paused ? "Resume" : "Pause";
  const scrubber = $<HTMLInputElement>("scrubber");
  if (document.activeElement !== scrubber) {
    scrubber.value = String(player.total ? Math.round((player.time / player.total) * 1000) : 0);
  }
  $("btn-next").hidden = !(player.ended && state.chapter < CHAPTERS.length - 1);
  const marks = player.marks;
  let current = -1;
  marks.forEach((mark, i) => {
    if (player.time >= mark.at) current = i;
  });
  $("marks")
    .querySelectorAll("button")
    .forEach((button, i) => {
      if (i === current) button.setAttribute("aria-current", "step");
      else button.removeAttribute("aria-current");
    });
}

// ---- readout -------------------------------------------------------------

const live = { pages: -1, rows: -2, say: "" };

function flush(): void {
  if (frame.say !== live.say) {
    live.say = frame.say;
    $("phase-label").textContent = frame.say;
  }
  if (frame.pages !== live.pages || (frame.rows ?? -1) !== live.rows) {
    live.pages = frame.pages;
    live.rows = frame.rows ?? -1;
    const total = CHAPTERS[state.chapter].trees.reduce((sum, key) => sum + shapeOf(key).pages, 0);
    setValue("read", fmt(frame.pages), "of " + plural(total, "page") + " in " + (CHAPTERS[state.chapter].trees.length > 1 ? "both trees" : "the index"));
    if (frame.rows !== undefined) setValue("rows", fmt(frame.rows), "rows returned so far");
  }
}

function setValue(key: string, value: string, sub: string): void {
  const item = document.querySelector<HTMLElement>(`[data-key="${key}"]`);
  if (!item) return;
  item.querySelector(".value")!.textContent = value;
  item.querySelector(".sub")!.textContent = sub;
}

function tip(text: string): HTMLElement {
  const span = document.createElement("span");
  span.className = "help-tip";
  span.tabIndex = 0;
  span.setAttribute("role", "note");
  span.setAttribute("aria-label", text);
  span.textContent = "?";
  const inner = document.createElement("span");
  inner.className = "tip-text";
  inner.setAttribute("aria-hidden", "true");
  inner.textContent = text;
  span.append(inner);
  return span;
}

function readoutItem(key: string, label: string, help: string): HTMLElement {
  const item = document.createElement("div");
  item.className = "item";
  item.dataset.key = key;
  const l = document.createElement("p");
  l.className = "label";
  l.append(label, tip(help));
  const v = document.createElement("div");
  v.className = "value";
  const s = document.createElement("div");
  s.className = "sub";
  item.append(l, v, s);
  return item;
}

let shapes: { clustered: Shape; secondary: Shape };
const shapeOf = (key: "clustered" | "secondary") => shapes[key];

function renderReadout(): void {
  const chapter = CHAPTERS[state.chapter];
  const grid = $("readout-query");
  const items = [
    readoutItem(
      "read",
      "Pages read by this query",
      "Each page InnoDB has to look at is one page read: from memory if the page is in the buffer pool, from disk if not. Fewer pages means a faster query.",
    ),
  ];
  if (chapter.id === "range") {
    items.push(readoutItem("rows", "Rows returned", "Rows that match the BETWEEN range. Many rows share one leaf page, so rows grow much faster than pages read."));
  }
  grid.replaceChildren(...items);
  live.pages = -1;

  const table = $("shape-table");
  const keys = chapter.trees.length > 1 ? (["secondary", "clustered"] as const) : (["clustered"] as const);
  const names: Record<string, string> = { clustered: "users (clustered)", secondary: "idx_users_email" };
  const head = document.createElement("thead");
  const hr = document.createElement("tr");
  const corner = document.createElement("th");
  corner.textContent = "Index shape";
  hr.append(corner, ...keys.map((k) => Object.assign(document.createElement("th"), { textContent: names[k] })));
  head.append(hr);
  const rows: [string, string, (s: Shape) => string][] = [
    ["Levels", "How many pages a lookup reads from the top of the tree to the bottom: one per level.", (s) => String(s.levels.length)],
    ["Pages per level", "Root first. Each level holds the pages the level above points to.", (s) => s.levels.map(fmt).join(" · ")],
    ["Pointers per page (fan-out)", "How many child pages one non-leaf page can point to. Smaller keys fit more pointers, which makes the tree flatter.", (s) => fmt(s.fanOut)],
    ["Records per leaf page", "Clustered leaves hold whole rows and keep 1/16 of the page free for updates; index leaves hold (email, id). Smaller records fit more per page.", (s) => fmt(s.leafCapacity) + " × " + s.leafEntryBytes + " B"],
    ["Size", "Pages times the page size.", (s) => bytes(s.pages * state.input.pageKib * 1024)],
  ];
  const body = document.createElement("tbody");
  for (const [label, help, value] of rows) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.append(label, tip(help));
    tr.append(th, ...keys.map((k) => Object.assign(document.createElement("td"), { textContent: value(shapes[k]) })));
    body.append(tr);
  }
  table.replaceChildren(head, body);
}

function drawnNote(specs: TreeSpec[]): string {
  const notes: string[] = [];
  for (const spec of specs) {
    const levels = spec.shape.levels;
    const leaf = levels[levels.length - 1];
    if (leaf <= PAGE_BUDGET) continue;
    const parents = levels[levels.length - 2];
    notes.push(
      parents <= GROUP_BUDGET
        ? `${spec.title}: ${fmt(leaf)} leaf pages drawn as ${fmt(parents)} blocks; the ones the query reads open up.`
        : `${spec.title}: ${fmt(leaf)} leaf pages drawn as one plate; the ones the query reads open up.`,
    );
  }
  return notes.join(" ");
}

// ---- building a chapter --------------------------------------------------

function rebuild(): void {
  shapes = indexShapes(state.input);
  const chapter = CHAPTERS[state.chapter];
  const specs: TreeSpec[] = chapter.trees.map((key) =>
    key === "clustered"
      ? { key, title: "users · clustered index (id)", shape: shapes.clustered, leafColor: COLORS.clusteredLeaf }
      : { key, title: "idx_users_email · secondary index", shape: shapes.secondary, leafColor: COLORS.secondaryLeaf },
  );
  world.layout(specs);
  for (const tree of world.trees) {
    const levels = tree.spec.shape.levels;
    levels.forEach((count, l) => {
      const leaf = l === levels.length - 1;
      const name = l === 0 ? (leaf ? "root and leaf" : "root") : leaf ? "leaf level" : "level " + (l + 1);
      tree.setCaption(l, `${name} · ${plural(count, "page")}`);
    });
  }
  $("drawn").textContent = gpu ? drawnNote(specs) : "";

  ctx = { world, input: state.input, clustered: shapes.clustered, secondary: shapes.secondary, covering: state.covering, ui };
  CHAPTERS.forEach((c, i) => {
    chapterNav.querySelector(`[data-chapter="${c.id}"]`)!.setAttribute("aria-pressed", String(i === state.chapter));
  });
  $("query").textContent = chapter.query(ctx);
  $("covering-wrap").hidden = chapter.id !== "secondary";
  $("explainer").replaceChildren(
    ...chapter.explainer(ctx).map((text) => Object.assign(document.createElement("li"), { textContent: text })),
  );
  renderReadout();

  timeline = chapter.build(ctx);
  const marks = $("marks");
  marks.replaceChildren(
    ...timeline.marks.map((mark) => {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = mark.label;
      button.addEventListener("click", () => player.seek(mark.at));
      li.append(button);
      return li;
    }),
  );
  player.load(timeline);
  // Reduced motion: show the finished state with its narration.
  if (reducedMotion()) player.seek(player.total);
}

rebuild();

// A small hook for the smoke test and for embedding pages.
(window as unknown as { teach3d: object }).teach3d = {
  gpu,
  chapters: CHAPTERS.map((c) => c.id),
  open: (i: number) => openChapter(i),
  finish: () => player.seek(player.total),
  get state() {
    return { ...state.input, chapter: CHAPTERS[state.chapter].id, time: player.time, total: player.total };
  },
};
