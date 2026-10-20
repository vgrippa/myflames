// The four chapters. Each builds a Timeline whose steps set absolute state
// on the World (see timeline.ts), and narrates with the real numbers from
// model.ts. Facts checked against the server source (MySQL 8.4, MariaDB 11.4):
// - A secondary-index leaf stores the primary key; the row is fetched by
//   searching the clustered index from its root with that key (row0sel.cc:
//   MySQL clust_pcur->open_no_init, MariaDB btr_pcur_open_with_no_init). The
//   adaptive hash index can skip the descent; it is off by default in both.
// - A range scan moves to the next leaf through the page's next-page link
//   (btr0pcur.cc, move_to_next_page). Between batches of rows the cursor is
//   stored and restored; the restore searches from the root again only if
//   another session changed the page.
import { Vector3 } from "three";
import type { Shot, TreeKey, TreeView, World } from "./scene";
import { Timeline, ease } from "./timeline";
import {
  type Inputs,
  type Shape,
  RANGE_FIRST,
  TARGET_ID,
  aliceOrdinal,
  leafRangePages,
  pathTo,
  rangeLast,
  spans,
} from "./model";
import { fmt, kib, plural } from "./format";

export interface Ui {
  /** The status line; the last call at a given time wins. */
  say(text: string): void;
  /** Pages this query has read so far, and rows returned (range scan). */
  read(pages: number, rows?: number): void;
}

export interface Context {
  world: World;
  input: Inputs;
  clustered: Shape;
  secondary: Shape;
  covering: boolean;
  ui: Ui;
}

export interface Chapter {
  id: string;
  title: string;
  trees: TreeKey[];
  query(ctx: Context): string;
  explainer(ctx: Context): string[];
  build(ctx: Context): Timeline;
}

const EMAIL = "alice@example.com";

/** Camera and token bookkeeping while a chapter's steps are written. */
class Script {
  readonly tl = new Timeline();
  cam: Shot;
  token = new Vector3();

  constructor(readonly ctx: Context, start: Shot) {
    this.cam = start;
  }

  get world(): World {
    return this.ctx.world;
  }

  /** Move the camera to `shot` (alongside the previous step when `parallel`). */
  camTo(shot: Shot, dur: number, parallel = false, offset = 0): void {
    const from = this.cam;
    this.cam = shot;
    const run = (t: number) => this.world.view(from, shot, ease.inOutCubic(t));
    if (parallel) this.tl.with(dur, run, offset);
    else this.tl.add(dur, run);
  }

  /** Slide the token from where it is to `to`. */
  tokenTo(to: Vector3, text: string, dur: number): void {
    const from = this.token.clone();
    const end = to.clone();
    this.token = end;
    this.tl.add(dur, (t) => this.world.setToken(from.clone().lerp(end, ease.inOutCubic(t)), text));
  }
}

function above(v: Vector3, up: number): Vector3 {
  return v.clone().add(new Vector3(0, up, 0));
}

function idRange(shape: Shape, rows: number, level: number, page: number): [number, number] {
  const span = spans(shape)[level];
  return [page * span + 1, Math.min((page + 1) * span, rows)];
}

/** Shot that shows a page together with the block of its children. */
function pageShot(world: World, tree: TreeView, level: number, page: number): Shot {
  const top = tree.pos(level, page, true);
  if (level >= tree.height - 1) return world.shot(top, 7, -0.62, 0.72);
  return world.fit([above(top, 1.5), ...tree.corners(level, page)], -0.62, 0.42);
}

/** Opening shot: the token above the root, and the level below. */
function startShot(world: World, tree: TreeView, token: Vector3): Shot {
  const below = tree.height > 1 ? tree.corners(0, 0) : [tree.pos(0, 0)];
  return world.fit([above(token, 1), ...below], -0.62, 0.42);
}

interface DescentText {
  token: string;
  label(level: number, page: number): string;
  say(level: number, page: number, read: number): string;
}

/** Walk the token from the root to a leaf, one page read per level. */
function descend(s: Script, tree: TreeView, path: number[], base: number, text: DescentText, step = 700): void {
  const { world, ui } = { world: s.world, ui: s.ctx.ui };
  const h = tree.height;
  for (let l = 0; l < h; l++) {
    const page = path[l];
    const top = tree.pos(l, page, true);
    s.tokenTo(top, text.token, step);
    s.camTo(pageShot(world, tree, l, page), step + 150, true);
    const read = base + l + 1;
    s.tl.call(() => {
      tree.visit(l, page);
      tree.label(l, page, text.label(l, page), l === h - 1 ? "page leaf" : "page");
      ui.read(read);
      ui.say(text.say(l, page, read));
    });
    if (l < h - 1) {
      s.tl.add(step * 0.7, (t) => {
        tree.fan = { level: l, page, opacity: 0.5 * ease.outCubic(t) };
      });
      const child = path[l + 1];
      s.tl.call(() => {
        world.addPath(top, tree.pos(l + 1, child, true));
        tree.open(l + 1, page);
      });
      s.tl.delay(step * 0.5);
    } else if (l > 0) {
      s.tl.add(step * 0.6, (t) => {
        tree.fan = { level: l - 1, page: path[l - 1], opacity: 0.5 * (1 - t) };
      });
    }
  }
}

function clusteredText(ctx: Context, tree: TreeView, token: string, leafNote = `#${TARGET_ID} ${EMAIL}`): DescentText {
  const shape = ctx.clustered;
  const rows = ctx.input.rows;
  const h = tree.height;
  return {
    token,
    label(level, page) {
      const [a, b] = idRange(shape, rows, level, page);
      return level === h - 1 ? `ids ${fmt(a)}–${fmt(b)} · ${leafNote}` : `ids ${fmt(a)}–${fmt(b)}`;
    },
    say(level, page, read) {
      const [a, b] = idRange(shape, rows, level, page);
      if (h === 1) {
        return `The whole table fits in one page, so the root is also the leaf. Row ${TARGET_ID} is here: ${EMAIL}. ${plural(read, "page")} read.`;
      }
      if (level === h - 1) {
        return `Leaf page ${fmt(page + 1)} of ${fmt(tree.count(level))} holds ids ${fmt(a)}–${fmt(b)}. Row ${TARGET_ID} is here (${EMAIL}). ${plural(read, "page")} read.`;
      }
      const [first, last] = tree.childrenOf(level, page);
      const pointer = pathTo(shape, TARGET_ID)[level + 1] - first + 1;
      const where = level === 0 ? "Root page" : `Level ${level + 1} page`;
      return `${where}: ids ${fmt(a)}–${fmt(b)} in ${fmt(last - first)} child pages. id ${TARGET_ID} is under pointer ${fmt(pointer)} of ${fmt(last - first)}.`;
    },
  };
}

function secondaryText(ctx: Context, tree: TreeView): DescentText {
  const shape = ctx.secondary;
  const rows = ctx.input.rows;
  const h = tree.height;
  return {
    token: `looking for ${EMAIL}`,
    label(level, page) {
      if (level === h - 1) return `${EMAIL} → id ${TARGET_ID}`;
      if (level === 0) return `all ${fmt(rows)} emails, A to Z`;
      const [a, b] = idRange(shape, rows, level, page);
      return `emails #${fmt(a)}–#${fmt(b)} (A to Z)`;
    },
    say(level, page, read) {
      if (level === h - 1) {
        return `Secondary leaf: ${EMAIL} → id ${TARGET_ID}. The leaf stores the primary key, not a page address. ${plural(read, "page")} read.`;
      }
      const [first, last] = tree.childrenOf(level, page);
      const where = level === 0 ? "idx_users_email root" : `idx_users_email level ${level + 1}`;
      return `${where}: entries are sorted by email, so ${EMAIL} sits near the start. Follow pointer ${fmt(pathTo(shape, aliceOrdinal(rows))[level + 1] - first + 1)} of ${fmt(last - first)}.`;
    },
  };
}

/** Rows a tree of this height can hold before it needs another level. */
function rowsBeforeNextLevel(shape: Shape): number {
  return shape.leafCapacity * Math.pow(shape.fanOut, shape.levels.length - 1);
}

const shapeChapter: Chapter = {
  id: "shape",
  title: "Shape",
  trees: ["clustered"],
  query: () => "CREATE TABLE users (id BIGINT PRIMARY KEY, email VARCHAR(255), first_name …)",
  explainer: (ctx) => [
    "Each flat tile is one InnoDB page (" + kib(ctx.input.pageKib) + "). Every page is drawn the same size.",
    "The root page sits at the top. Each level below holds the pages the level above points to.",
    "Grey lines show the pointers out of one page: that is the fan-out.",
    "Teal tiles at the bottom are leaf pages. They hold the rows themselves, sorted by id.",
  ],
  build(ctx) {
    const world = ctx.world;
    const tree = world.tree("clustered");
    const shape = ctx.clustered;
    const h = tree.height;
    const rootTop = tree.pos(0, 0);
    const s = new Script(ctx, world.shot(rootTop, 1.4));
    const first = s.cam;
    s.tl.call(() => {
      for (let l = 1; l < h; l++) tree.reveal[l] = 0;
      world.view(first, first, 1);
      ctx.ui.read(0);
      ctx.ui.say(
        h === 1
          ? `${fmt(ctx.input.rows)} rows fit in one ${kib(ctx.input.pageKib)} page. That page is both the root and the leaf.`
          : `This is the root page of users. ${fmt(ctx.input.rows)} rows sit below it.`,
      );
    });
    s.tl.mark("Root page");
    s.tl.delay(1200);
    for (let l = 1; l < h; l++) {
      s.tl.mark(l === h - 1 ? "Leaf level" : `Level ${l + 1}`);
      s.camTo(world.fit([tree.pos(0, 0), ...tree.corners(l)], -0.62, 0.5), 1400);
      s.tl.with(1000, (t) => {
        tree.reveal[l] = ease.outCubic(t);
      }, 350);
      s.tl.with(800, (t) => {
        tree.fan = { level: l - 1, page: 0, opacity: 0.5 * ease.outCubic(t) };
      }, 900);
      const count = tree.count(l);
      const per = tree.perPage(l - 1);
      s.tl.call(() => {
        ctx.ui.say(
          l === h - 1
            ? `Leaf level: ${fmt(count)} pages, about ${fmt(shape.leafCapacity)} rows each. One page above points to up to ${fmt(per)} of them (grey lines).`
            : `Level ${l + 1} of ${h}: ${fmt(count)} ${count === 1 ? "page" : "pages"}. The grey lines are the ${fmt(Math.min(per, count))} pointers in the page above.`,
        );
      });
      s.tl.delay(1300);
    }
    if (h > 1) {
      s.tl.mark("Whole tree");
      s.camTo(world.fit([above(tree.pos(0, 0), 2), ...tree.corners(h - 1)], -0.62, 0.38), 1600);
      s.tl.call(() => {
        ctx.ui.say(
          `${fmt(ctx.input.rows)} rows need ${fmt(shape.pages)} pages in ${h} levels, so any one row is ${plural(h, "page read")} from the root. ` +
            `With pages this full, the tree keeps ${h} levels up to about ${fmt(rowsBeforeNextLevel(shape))} rows.`,
        );
      });
    }
    return s.tl;
  },
};

const lookupChapter: Chapter = {
  id: "lookup",
  title: "Primary key lookup",
  trees: ["clustered"],
  query: () => `SELECT * FROM users WHERE id = ${TARGET_ID};`,
  explainer: () => [
    `The red diamond is the query, looking for id = ${TARGET_ID}.`,
    "It drops one level at a time. Each page it lands on turns yellow and lifts: one page read.",
    "Grey lines show the pointers in that page; the orange line is the one InnoDB follows.",
    "The leaf page holds the whole row, so the lookup ends there.",
  ],
  build(ctx) {
    const world = ctx.world;
    const tree = world.tree("clustered");
    const path = pathTo(ctx.clustered, TARGET_ID);
    const token = `looking for id = ${TARGET_ID}`;
    const start = above(tree.pos(0, 0), 3);
    const s = new Script(ctx, startShot(world, tree, start));
    s.token = start;
    const first = s.cam;
    s.tl.call(() => {
      world.view(first, first, 1);
      world.setToken(start, token);
      ctx.ui.read(0);
      ctx.ui.say(`With default settings (adaptive hash index off), InnoDB starts each lookup at the root page of the clustered index, which is the table itself.`);
    });
    s.tl.mark("Root");
    s.tl.delay(900);
    descend(s, tree, path, 0, clusteredText(ctx, tree, token));
    s.tl.mark("Leaf");
    const total = ctx.clustered.pages;
    const h = tree.height;
    s.tl.delay(600).call(() => {
      ctx.ui.say(
        `${plural(h, "page")} read out of ${fmt(total)} in the index (${share(h, total)}). ` +
          `The same ${plural(h, "read")} find any row until the table passes about ${fmt(rowsBeforeNextLevel(ctx.clustered))} rows.`,
      );
    });
    return s.tl;
  },
};

const secondaryChapter: Chapter = {
  id: "secondary",
  title: "Secondary index",
  trees: ["secondary", "clustered"],
  query: (ctx) =>
    ctx.covering
      ? `SELECT id, email FROM users WHERE email = '${EMAIL}';`
      : `SELECT id, first_name, last_name FROM users WHERE email = '${EMAIL}';`,
  explainer: (ctx) => [
    "Left: idx_users_email, sorted by email. Right: the clustered index (the table), sorted by id.",
    "Indigo leaves hold (email, id) pairs. Teal leaves hold whole rows.",
    ctx.covering
      ? "This query asks only for email and id, which the index leaf already has: one tree is enough."
      : `The dashed orange line carries id = ${TARGET_ID} from the index leaf to the table's root page.`,
    "Every yellow page is one page read.",
  ],
  build(ctx) {
    const world = ctx.world;
    const sec = world.tree("secondary");
    const clu = world.tree("clustered");
    const secPath = pathTo(ctx.secondary, aliceOrdinal(ctx.input.rows));
    const cluPath = pathTo(ctx.clustered, TARGET_ID);
    const start = above(sec.pos(0, 0), 3);
    const span = Math.abs(clu.x - sec.x);
    const overview = world.fit(
      [above(sec.pos(0, 0), 4), above(clu.pos(0, 0), 4), ...sec.corners(sec.height - 1), ...clu.corners(clu.height - 1)],
      -0.3,
      0.32,
      1.02,
    );
    const s = new Script(ctx, overview);
    s.token = start;
    const first = s.cam;
    s.tl.call(() => {
      world.view(first, first, 1);
      world.setToken(start, `looking for ${EMAIL}`);
      ctx.ui.read(0);
      ctx.ui.say(`The WHERE clause is on email, so the optimizer picks idx_users_email and InnoDB starts at its root page.`);
    });
    s.tl.mark("Index root");
    s.tl.delay(1100);
    descend(s, sec, secPath, 0, secondaryText(ctx, sec));
    s.tl.mark("Index leaf");
    const hs = sec.height;
    const hc = clu.height;
    if (ctx.covering) {
      s.tl.delay(700).call(() => {
        ctx.ui.say(
          `Covering: the query wants only email and id, and this leaf has both. ${plural(hs, "page")} read, and the table is not visited (unless recent changes to this index page make InnoDB check the row's version in the table).`,
        );
      });
      return s.tl;
    }
    const from = sec.pos(hs - 1, secPath[hs - 1], true);
    const to = above(clu.pos(0, 0), 3);
    const lift = Math.max(span * 0.35, 6);
    const arc: Vector3[] = [];
    for (let i = 0; i <= 48; i++) {
      const t = i / 48;
      const mid = from.clone().lerp(to, 0.5).add(new Vector3(0, lift, 0));
      const a = from.clone().lerp(mid, t);
      const b = mid.clone().lerp(to, t);
      arc.push(a.lerp(b, t));
    }
    s.tl.delay(500);
    s.tl.mark("Carry id to the table");
    s.camTo(world.fit([from, to, ...arc.filter((_, i) => i % 8 === 0)], -0.3, 0.3, 1.15), 900);
    s.tl.add(1700, (t) => {
      const k = Math.max(1, Math.round(ease.inOutQuad(t) * 48));
      world.setCarry(arc.slice(0, k + 1));
      world.setToken(arc[k], `carrying id = ${TARGET_ID}`);
      ctx.ui.say(
        `To return first_name and last_name, InnoDB looks up id ${TARGET_ID} in the clustered index, starting again from its root page.`,
      );
    });
    s.token = to;
    s.tl.delay(400);
    s.tl.mark("Table root");
    descend(s, clu, cluPath, hs, clusteredText(ctx, clu, `looking for id = ${TARGET_ID}`));
    s.tl.mark("Table leaf");
    s.tl.delay(600).call(() => {
      ctx.ui.say(
        `${plural(hs + hc, "page")} read: ${hs} in idx_users_email, then ${hc} in the table. ` +
          `An index that also held first_name and last_name would answer from its own leaf, without the ${hc} table pages.`,
      );
    });
    return s.tl;
  },
};

const rangeChapter: Chapter = {
  id: "range",
  title: "Range scan",
  trees: ["clustered"],
  query: (ctx) => `SELECT * FROM users WHERE id BETWEEN ${RANGE_FIRST} AND ${rangeLast(ctx.input.rows)};`,
  explainer: () => [
    `The red diamond first finds id = ${RANGE_FIRST} the usual way: root to leaf.`,
    "Then it walks right along the leaf level. Each leaf page has a link to the next one.",
    "Every yellow page is one page read; the orange line is the path taken.",
    "Watch the counter: after the first leaf, every page read is a leaf.",
  ],
  build(ctx) {
    const world = ctx.world;
    const tree = world.tree("clustered");
    const shape = ctx.clustered;
    const h = tree.height;
    const last = rangeLast(ctx.input.rows);
    const path = pathTo(shape, RANGE_FIRST);
    const first = path[h - 1];
    const leaves = leafRangePages(RANGE_FIRST, last, shape.leafCapacity);
    const rowsTotal = last - RANGE_FIRST + 1;
    const token = `id BETWEEN ${RANGE_FIRST} AND ${fmt(last)}`;
    const start = above(tree.pos(0, 0), 3);
    const s = new Script(ctx, startShot(world, tree, start));
    s.token = start;
    const opening = s.cam;
    s.tl.call(() => {
      world.view(opening, opening, 1);
      world.setToken(start, token);
      ctx.ui.read(0, 0);
      ctx.ui.say(`A range scan starts like a lookup: find the first id (${RANGE_FIRST}) from the root.`);
    });
    s.tl.mark("Find id " + RANGE_FIRST);
    s.tl.delay(800);
    descend(s, tree, path, 0, clusteredText(ctx, tree, token, `range starts at id ${RANGE_FIRST}`), 520);
    const leafLevel = h - 1;
    const rowsUpTo = (leaf: number) => Math.min(last, (leaf + 1) * shape.leafCapacity) - RANGE_FIRST + 1;
    s.tl.call(() => ctx.ui.read(h, rowsUpTo(first)));
    if (leaves > 1) {
      s.tl.mark("Walk the leaves");
      const firstTop = tree.pos(leafLevel, first, true);
      // The camera rides along the leaf level with the token, then pulls back.
      const track = (at: Vector3): Shot => ({ target: at.clone(), dist: 16, az: -0.12, el: 0.82 });
      s.camTo(track(firstTop), 1000);
      const dur = Math.min(8000, Math.max(900, leaves * 300));
      s.tl.add(dur, (t) => {
        const x = t * (leaves - 1);
        const k = Math.min(leaves - 1, Math.floor(x + 1e-9));
        let prev = tree.pos(leafLevel, first, true);
        for (let i = 0; i <= k; i++) {
          const leaf = first + i;
          tree.visit(leafLevel, leaf);
          tree.open(leafLevel, tree.parentOf(leafLevel, leaf));
          if (i > 0) {
            const top = tree.pos(leafLevel, leaf, true);
            world.addPath(prev, top);
            prev = top;
          }
        }
        const here = tree.pos(leafLevel, first + k, true);
        const next = k < leaves - 1 ? tree.pos(leafLevel, first + k + 1, true) : here;
        const at = here.clone().lerp(next, ease.inOutQuad(x - k));
        world.setToken(at, token);
        const follow = track(at);
        world.view(follow, follow, 1);
        const [a, b] = idRange(shape, ctx.input.rows, leafLevel, first + k);
        tree.label(leafLevel, first + k, `ids ${fmt(a)}–${fmt(b)}`, "page leaf", true);
        ctx.ui.read(h + k, rowsUpTo(first + k));
        ctx.ui.say(
          k === 0
            ? `The rows after id ${RANGE_FIRST} continue on the next leaf page. InnoDB follows the leaf's next-page link instead of going back to the root.`
            : `Next-page link to leaf ${fmt(first + k + 1)}: ids ${fmt(a)}–${fmt(Math.min(b, last))}. ${plural(h + k, "page")} read so far.`,
        );
      });
    }
    s.tl.mark("Done");
    if (leaves > 1) {
      const firstTop = tree.pos(leafLevel, first, true);
      const lastTop = tree.pos(leafLevel, first + leaves - 1, true);
      s.cam = { target: lastTop.clone(), dist: 16, az: -0.12, el: 0.82 };
      s.camTo(world.fit([above(firstTop, 3), firstTop, lastTop, above(lastTop, 3)], -0.12, 0.9, 1.0), 1200);
    }
    s.tl.delay(300).call(() => {
      ctx.ui.read(h + leaves - 1, rowsTotal);
      ctx.ui.say(
        leaves > 1
          ? `${fmt(rowsTotal)} rows from ${fmt(leaves)} leaf pages: ${h} pages to reach id ${RANGE_FIRST}, then ${fmt(leaves - 1)} next-page reads, without going back to the root.`
          : `All ${fmt(rowsTotal)} rows are on one leaf page: ${plural(h, "page")} read in total.`,
      );
    });
    return s.tl;
  },
};

function share(part: number, total: number): string {
  const pct = (part / total) * 100;
  if (pct >= 1) return pct.toFixed(0) + "%";
  if (pct >= 0.01) return pct.toFixed(2) + "%";
  return "less than 0.01%";
}

export const CHAPTERS: Chapter[] = [shapeChapter, lookupChapter, secondaryChapter, rangeChapter];
