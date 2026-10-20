// The 3D stage: B+trees as stacked levels of pages, the query token, and the
// camera. Layout and per-tree state are plain math so the page still runs
// (narration and readouts) when WebGL is unavailable; only `sync` and
// `render` need a GPU.
//
// Layout: every page is the same size (one 16 KiB page is one tile). A
// page's children fill a g x g block on the level below, centered under it,
// so the tree spreads out level by level: one root tile at the top, a field
// of leaf tiles on the ground.
import {
  BoxGeometry,
  BufferAttribute,
  BufferGeometry,
  Color,
  DirectionalLight,
  DynamicDrawUsage,
  Group,
  HemisphereLight,
  InstancedMesh,
  LineBasicMaterial,
  LineSegments,
  Matrix4,
  Mesh,
  MeshLambertMaterial,
  OctahedronGeometry,
  PerspectiveCamera,
  Quaternion,
  Scene,
  Vector3,
  WebGLRenderer,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import { LineSegments2 } from "three/addons/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/addons/lines/LineSegmentsGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import type { Shape } from "./model";

/** Same palette as the 2D lesson (myflames/teach/_html.py and btree.js). */
export const COLORS = {
  bg: 0xf6f8fc,
  root: 0x475569,
  internal: 0x64748b,
  clusteredLeaf: 0x21918c,
  secondaryLeaf: 0x6366f1,
  read: 0xfde725,
  path: 0xf97316,
  token: 0xdc2626,
  fan: 0x64748b,
};

const PAGE = 0.84;
const THICK = 0.16;
const LIFT = 0.35;
/** Levels with more pages than this draw one slab per parent instead. */
export const PAGE_BUDGET = 20000;
/** Levels whose parents exceed this draw one plate for the whole level. */
export const GROUP_BUDGET = 20000;
/** Parent groups opened into single pages at once, per level. */
const MAX_OPEN = 8;

export type TreeKey = "clustered" | "secondary";

export interface TreeSpec {
  key: TreeKey;
  title: string;
  shape: Shape;
  leafColor: number;
}

export interface Shot {
  target: Vector3;
  dist: number;
  az: number;
  el: number;
}

interface LabelState {
  level: number;
  page: number;
  text: string;
  kind: string;
}

interface LevelView {
  group: Group;
  pages: InstancedMesh | null;
  groups: InstancedMesh | null;
  detail: InstancedMesh | null;
  detailPages: number[];
  caption: CSS2DObject | null;
  painted: Set<number>;
  groupMarks: Map<number, "hidden" | "read">;
  detailSig: string;
}

const tmpM = new Matrix4();
const tmpQ = new Quaternion();
const tmpV = new Vector3();
const tmpS = new Vector3();
const tmpC = new Color();

function label(text: string, className: string): CSS2DObject {
  const el = document.createElement("div");
  el.className = className;
  el.textContent = text;
  return new CSS2DObject(el);
}

/**
 * A label kept a fixed number of pixels away from its anchor (CSS2DRenderer
 * owns `transform`, so the offset is padding on a wrapper).
 */
function offsetLabel(text: string, className: string, side: "above" | "right"): CSS2DObject {
  const wrap = document.createElement("div");
  wrap.className = "label3d-wrap " + side;
  const el = document.createElement("div");
  el.className = className;
  el.textContent = text;
  wrap.append(el);
  return new CSS2DObject(wrap);
}

function setText(obj: CSS2DObject, text: string): void {
  const el = (obj.element.firstElementChild as HTMLElement | null) || obj.element;
  if (el.textContent !== text) el.textContent = text;
}

/**
 * Grid layout of a tree: g[l] is the side of the block a level-l page's
 * children fill, grid[l] the side of level l in tiles, y[l] its height.
 */
export function treeGeometry(shape: Shape): { g: number[]; grid: number[]; y: number[] } {
  const levels = shape.levels;
  const g: number[] = [];
  const grid = [1];
  const y = [0];
  for (let l = 0; l < levels.length - 1; l++) {
    g[l] = Math.ceil(Math.sqrt(l === 0 ? levels[1] : shape.fanOut));
    grid[l + 1] = grid[l] * g[l];
    y[l + 1] = y[l] - Math.max(2.6, 0.42 * grid[l + 1]);
  }
  return { g, grid, y };
}

export class TreeView {
  readonly x: number;
  readonly g: number[];
  readonly grid: number[];
  readonly y: number[];
  readonly root = new Group();
  private views: LevelView[] = [];
  private title: CSS2DObject | null = null;
  private fanLines: LineSegments | null = null;
  private fanSig = "";
  private labelObjs = new Map<string, CSS2DObject>();
  /** Captions the state wants shown, leaf level first; draw() hides ones that collide. */
  readonly captionWanted: { obj: CSS2DObject; shown: boolean }[] = [];

  // State, rebuilt from scratch on every frame by the timeline.
  visited = new Map<number, Set<number>>();
  opened = new Map<number, number[]>();
  fan: { level: number; page: number; opacity: number } | null = null;
  labels = new Map<string, LabelState>();
  reveal: number[] = [];
  shown = true;

  constructor(readonly world: World, readonly spec: TreeSpec, x: number) {
    this.x = x;
    const geometry = treeGeometry(spec.shape);
    this.g = geometry.g;
    this.grid = geometry.grid;
    this.y = geometry.y;
    this.reset();
    if (world.gpu) this.build();
  }

  get height(): number {
    return this.spec.shape.levels.length;
  }

  count(level: number): number {
    return this.spec.shape.levels[level];
  }

  /** Children per page of `level` (the root has exactly levels[1]). */
  perPage(level: number): number {
    return level === 0 ? this.spec.shape.levels[1] : this.spec.shape.fanOut;
  }

  parentOf(level: number, page: number): number {
    return level === 0 ? 0 : Math.floor(page / this.perPage(level - 1));
  }

  childrenOf(level: number, page: number): [number, number] {
    const per = this.perPage(level);
    return [page * per, Math.min((page + 1) * per, this.count(level + 1))];
  }

  private gridOf(level: number, page: number): [number, number] {
    let cx = 0;
    let cy = 0;
    let mul = 1;
    let k = page;
    for (let l = level; l >= 1; l--) {
      const per = this.perPage(l - 1);
      const parent = Math.floor(k / per);
      const i = k - parent * per;
      const g = this.g[l - 1];
      cx += (i % g) * mul;
      cy += Math.floor(i / g) * mul;
      mul *= g;
      k = parent;
    }
    return [cx, cy];
  }

  private local(level: number, page: number, out: Vector3): Vector3 {
    const [cx, cy] = this.gridOf(level, page);
    const half = this.grid[level] / 2;
    return out.set(cx + 0.5 - half, 0, cy + 0.5 - half);
  }

  /** World position of the top of a page (lifted when it has been read). */
  pos(level: number, page: number, lifted?: boolean): Vector3 {
    const p = this.local(level, page, new Vector3());
    const up = (lifted === undefined ? this.visited.get(level)?.has(page) : lifted) ? LIFT : 0;
    return p.set(p.x + this.x, this.y[level] + THICK / 2 + up, p.z);
  }

  /** Center and size (in tiles) of the block a page's children occupy. */
  block(level: number, page: number): { center: Vector3; size: number } {
    const [cx, cy] = this.gridOf(level, page);
    const g = this.g[level];
    const [from, to] = this.childrenOf(level, page);
    const rows = Math.ceil((to - from) / g);
    const half = this.grid[level + 1] / 2;
    return {
      center: new Vector3(this.x + cx * g + g / 2 - half, this.y[level + 1], cy * g + rows / 2 - half),
      size: Math.max(g, rows),
    };
  }

  /** The four corners of a level (or of a page's child block), for camera fitting. */
  corners(level: number, page?: number): Vector3[] {
    if (page !== undefined) {
      const { center, size } = this.block(level, page);
      const h = size / 2;
      return [-1, 1].flatMap((sx) => [-1, 1].map((sz) => new Vector3(center.x + sx * h, center.y, center.z + sz * h)));
    }
    const h = this.grid[level] / 2;
    return [-1, 1].flatMap((sx) => [-1, 1].map((sz) => new Vector3(this.x + sx * h, this.y[level], sz * h)));
  }

  /** Camera framing for a whole level (or the whole tree when level is -1). */
  frame(level: number): { center: Vector3; size: number } {
    if (level < 0) {
      const bottom = this.y[this.height - 1];
      return { center: new Vector3(this.x, bottom * 0.5, 0), size: Math.max(this.grid[this.height - 1], -bottom, 3) };
    }
    return { center: new Vector3(this.x, this.y[level], 0), size: Math.max(this.grid[level], 2) };
  }

  reset(): void {
    this.visited.clear();
    this.opened.clear();
    this.fan = null;
    this.labels.clear();
    this.reveal = this.spec.shape.levels.map(() => 1);
  }

  visit(level: number, page: number): void {
    let set = this.visited.get(level);
    if (!set) this.visited.set(level, (set = new Set()));
    set.add(page);
  }

  /** Show a parent's children as single pages (levels drawn as slabs). */
  open(level: number, parent: number): void {
    const list = this.opened.get(level) || [];
    if (!list.includes(parent)) list.push(parent);
    if (list.length > MAX_OPEN) list.shift();
    this.opened.set(level, list);
  }

  /** Label a page. Earlier page labels go away unless `keep` is set. */
  label(level: number, page: number, text: string, kind = "page", keep = false): void {
    if (!keep) this.labels.clear();
    this.labels.set(kind + ":" + level + ":" + page, { level, page, text, kind });
  }

  // ---- drawing ---------------------------------------------------------

  private build(): void {
    const world = this.world;
    const levels = this.spec.shape.levels;
    this.root.position.set(this.x, 0, 0);
    world.scene.add(this.root);
    const leafLevel = levels.length - 1;
    this.title = label(this.spec.title, "label3d title");
    this.title.center.set(0.5, 0);
    this.title.position.set(0, this.y[leafLevel], this.grid[leafLevel] / 2 + 0.6);
    this.root.add(this.title);

    for (let l = 0; l < levels.length; l++) {
      const group = new Group();
      group.position.set(0, this.y[l], 0);
      this.root.add(group);
      const view: LevelView = {
        group, pages: null, groups: null, detail: null, detailPages: [], caption: null,
        painted: new Set(), groupMarks: new Map(), detailSig: "",
      };
      const count = levels[l];
      const base = this.baseColor(l);
      if (count <= PAGE_BUDGET) {
        const mesh = new InstancedMesh(world.pageGeo, world.pageMat, count);
        for (let j = 0; j < count; j++) {
          this.local(l, j, tmpV);
          mesh.setMatrixAt(j, tmpM.makeTranslation(tmpV.x, 0, tmpV.z));
          mesh.setColorAt(j, tmpC.setHex(base));
        }
        group.add((view.pages = mesh));
      } else {
        const parents = levels[l - 1];
        const per = this.perPage(l - 1);
        const g = this.g[l - 1];
        const slab = tmpC.setHex(base).clone().lerp(new Color(COLORS.bg), 0.45).getHex();
        if (parents <= GROUP_BUDGET) {
          const mesh = new InstancedMesh(world.slabGeo, world.pageMat, parents);
          for (let p = 0; p < parents; p++) {
            this.groupMatrix(l, p, per, g, tmpM);
            mesh.setMatrixAt(p, tmpM);
            mesh.setColorAt(p, tmpC.setHex(slab));
          }
          group.add((view.groups = mesh));
        } else {
          const size = this.grid[l];
          const plate = new Mesh(world.slabGeo, new MeshLambertMaterial({ color: slab }));
          plate.scale.set(size, 1, size);
          plate.position.set(0, -THICK * 0.3, 0);
          group.add(plate);
        }
        const detail = new InstancedMesh(world.pageGeo, world.pageMat, MAX_OPEN * per);
        detail.instanceMatrix.setUsage(DynamicDrawUsage);
        detail.count = 0;
        detail.setColorAt(0, tmpC.setHex(base));
        detail.frustumCulled = false;
        group.add((view.detail = detail));
      }
      const caption = offsetLabel("", "label3d caption", "right");
      caption.center.set(0, 0.5);
      caption.position.set(this.grid[l] / 2 + 0.5, 0, 0);
      group.add((view.caption = caption));
      this.captionWanted.unshift({ obj: caption, shown: true });
      this.views.push(view);
    }

    const fanGeo = new BufferGeometry();
    const cap = Math.max(...levels.map((_, l) => (l < levels.length - 1 ? this.perPage(l) : 0)), 1);
    fanGeo.setAttribute("position", new BufferAttribute(new Float32Array(cap * 6), 3).setUsage(DynamicDrawUsage));
    this.fanLines = new LineSegments(fanGeo, new LineBasicMaterial({ color: COLORS.fan, transparent: true, opacity: 0 }));
    this.fanLines.frustumCulled = false;
    world.scene.add(this.fanLines);
  }

  private groupMatrix(level: number, parent: number, per: number, g: number, out: Matrix4): Matrix4 {
    const first = parent * per;
    const n = Math.min(per, this.count(level) - first);
    const rows = Math.ceil(n / g);
    this.local(level, first, tmpV);
    const cx = tmpV.x - 0.5 + g / 2;
    const cz = tmpV.z - 0.5 + rows / 2;
    return out.compose(tmpS.set(cx, -THICK * 0.3, cz), tmpQ, new Vector3(g * 0.94, 1, rows * 0.94));
  }

  private baseColor(level: number): number {
    if (level === this.height - 1) return this.spec.leafColor;
    return level === 0 ? COLORS.root : COLORS.internal;
  }

  setCaption(level: number, text: string): void {
    const caption = this.views[level]?.caption;
    if (caption) setText(caption, text);
  }

  sync(): void {
    if (!this.world.gpu) return;
    this.root.visible = this.shown;
    if (this.title) this.title.visible = this.shown;
    for (let l = 0; l < this.views.length; l++) {
      const view = this.views[l];
      const k = this.shown ? this.reveal[l] : 0;
      view.group.visible = k > 0.002;
      view.group.scale.set(Math.max(k, 0.002), 1, Math.max(k, 0.002));
      const wanted = this.captionWanted.find((c) => c.obj === view.caption);
      if (wanted) wanted.shown = k > 0.6;
      const read = this.visited.get(l) || new Set<number>();
      if (view.pages) this.paintPages(l, view, read);
      if (view.groups) this.markGroups(l, view, read);
      if (view.detail) this.fillDetail(l, view, read);
    }
    this.syncFan();
    this.syncLabels();
  }

  private paintPages(level: number, view: LevelView, read: Set<number>): void {
    const mesh = view.pages!;
    let changed = false;
    for (const j of view.painted) {
      if (read.has(j)) continue;
      this.local(level, j, tmpV);
      mesh.setMatrixAt(j, tmpM.makeTranslation(tmpV.x, 0, tmpV.z));
      mesh.setColorAt(j, tmpC.setHex(this.baseColor(level)));
      view.painted.delete(j);
      changed = true;
    }
    for (const j of read) {
      if (view.painted.has(j) || j >= mesh.count) continue;
      this.local(level, j, tmpV);
      mesh.setMatrixAt(j, tmpM.makeTranslation(tmpV.x, LIFT, tmpV.z));
      mesh.setColorAt(j, tmpC.setHex(COLORS.read));
      view.painted.add(j);
      changed = true;
    }
    if (changed) {
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
  }

  private markGroups(level: number, view: LevelView, read: Set<number>): void {
    const mesh = view.groups!;
    const per = this.perPage(level - 1);
    const g = this.g[level - 1];
    const want = new Map<number, "hidden" | "read">();
    for (const j of read) want.set(Math.floor(j / per), "read");
    for (const p of this.opened.get(level) || []) want.set(p, "hidden");
    let changed = false;
    const slab = tmpC.setHex(this.baseColor(level)).clone().lerp(new Color(COLORS.bg), 0.45);
    for (const [p, mark] of view.groupMarks) {
      if (want.get(p) === mark) continue;
      mesh.setMatrixAt(p, this.groupMatrix(level, p, per, g, tmpM));
      mesh.setColorAt(p, slab);
      view.groupMarks.delete(p);
      changed = true;
    }
    for (const [p, mark] of want) {
      if (view.groupMarks.get(p) === mark || p >= mesh.count) continue;
      if (mark === "hidden") mesh.setMatrixAt(p, tmpM.makeScale(0, 0, 0));
      else mesh.setColorAt(p, tmpC.setHex(COLORS.read));
      view.groupMarks.set(p, mark);
      changed = true;
    }
    if (changed) {
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
  }

  private fillDetail(level: number, view: LevelView, read: Set<number>): void {
    const parents = this.opened.get(level) || [];
    let readSig = "";
    for (const j of read) readSig += j + ",";
    const sig = parents.join(",") + "|" + readSig;
    if (sig === view.detailSig) return;
    view.detailSig = sig;
    const mesh = view.detail!;
    const base = this.baseColor(level);
    let slot = 0;
    view.detailPages = [];
    for (const p of parents) {
      const [from, to] = this.childrenOf(level - 1, p);
      for (let j = from; j < to && slot < mesh.instanceMatrix.count; j++, slot++) {
        this.local(level, j, tmpV);
        const lifted = read.has(j);
        mesh.setMatrixAt(slot, tmpM.makeTranslation(tmpV.x, lifted ? LIFT : 0, tmpV.z));
        mesh.setColorAt(slot, tmpC.setHex(lifted ? COLORS.read : base));
        view.detailPages.push(j);
      }
    }
    mesh.count = slot;
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  }

  private syncFan(): void {
    const lines = this.fanLines;
    if (!lines) return;
    const fan = this.shown ? this.fan : null;
    const material = lines.material as LineBasicMaterial;
    // Hundreds of lines at full opacity turn into a solid curtain.
    const many = fan ? Math.min(1, 6 / Math.sqrt(Math.max(1, this.childrenOf(fan.level, fan.page)[1] - this.childrenOf(fan.level, fan.page)[0]))) : 0;
    material.opacity = fan ? fan.opacity * Math.max(many, 0.12) : 0;
    lines.visible = !!fan && fan.opacity > 0.01;
    if (!fan) return;
    const sig = fan.level + ":" + fan.page + ":" + (this.visited.get(fan.level)?.has(fan.page) ? 1 : 0);
    if (sig === this.fanSig) return;
    this.fanSig = sig;
    const attr = lines.geometry.getAttribute("position") as BufferAttribute;
    const from = this.pos(fan.level, fan.page);
    const [a, b] = this.childrenOf(fan.level, fan.page);
    let i = 0;
    for (let j = a; j < b && i < attr.count / 2; j++, i++) {
      const to = this.pos(fan.level + 1, j);
      attr.setXYZ(i * 2, from.x, from.y - THICK, from.z);
      attr.setXYZ(i * 2 + 1, to.x, to.y, to.z);
    }
    lines.geometry.setDrawRange(0, i * 2);
    attr.needsUpdate = true;
  }

  private syncLabels(): void {
    for (const [key, obj] of this.labelObjs) {
      if (!this.labels.has(key) || !this.shown) obj.visible = false;
    }
    if (!this.shown) return;
    for (const [key, state] of this.labels) {
      let obj = this.labelObjs.get(key);
      if (!obj) {
        obj = label(state.text, "label3d " + state.kind);
        obj.center.set(0.5, 1);
        this.world.scene.add(obj);
        this.labelObjs.set(key, obj);
      }
      if (obj.element.textContent !== state.text) obj.element.textContent = state.text;
      obj.position.copy(this.pos(state.level, state.page)).add(tmpV.set(0, 0.25, 0));
      obj.visible = true;
    }
  }

  dispose(): void {
    if (!this.world.gpu) return;
    this.root.traverse((obj) => {
      if (obj instanceof CSS2DObject) obj.element.remove();
      if (obj instanceof InstancedMesh) obj.dispose();
      if (obj instanceof Mesh && !(obj instanceof InstancedMesh) && obj.material !== this.world.pageMat) {
        (obj.material as MeshLambertMaterial).dispose();
      }
    });
    this.world.scene.remove(this.root);
    for (const obj of this.labelObjs.values()) {
      obj.element.remove();
      this.world.scene.remove(obj);
    }
    if (this.fanLines) {
      this.fanLines.geometry.dispose();
      (this.fanLines.material as LineBasicMaterial).dispose();
      this.world.scene.remove(this.fanLines);
    }
  }
}

export class World {
  readonly gpu: boolean;
  readonly scene = new Scene();
  readonly camera = new PerspectiveCamera(42, 1, 0.05, 1e6);
  readonly pageGeo = new BoxGeometry(PAGE, THICK, PAGE);
  readonly slabGeo = new BoxGeometry(1, THICK * 0.5, 1);
  readonly pageMat = new MeshLambertMaterial({ color: 0xffffff });
  trees: TreeView[] = [];
  /** The camera follows the timeline until the user moves it. */
  follow = true;
  onUserCamera: () => void = () => {};

  private renderer: WebGLRenderer | null = null;
  private labelRenderer: CSS2DRenderer | null = null;
  private controls: OrbitControls | null = null;
  private token: Mesh | null = null;
  private tokenLabel: CSS2DObject | null = null;
  private tokenAnchor: Vector3 | null = null;
  private tokenText = "";
  private path: LineSegments2 | null = null;
  private pathPoints: number[] = [];
  private pathSig = "";
  private carry: LineSegments2 | null = null;
  private carryPoints: number[] = [];
  private carrySig = "";
  private materials: LineMaterial[] = [];
  private queued = 0;
  private extent = 100;

  constructor(private stage: HTMLElement, gpu: boolean) {
    this.gpu = gpu;
    if (!gpu) return;
    const renderer = new WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setClearColor(COLORS.bg);
    // The canvas takes keyboard focus for the camera; the narration and
    // readout carry the content for screen readers.
    const canvas = renderer.domElement;
    canvas.tabIndex = 0;
    canvas.setAttribute("role", "application");
    canvas.setAttribute("aria-roledescription", "3D view");
    canvas.setAttribute(
      "aria-label",
      "B+tree in 3D. Arrow keys pan, Shift and arrow keys turn, plus and minus zoom. The status line describes each step.",
    );
    stage.appendChild(canvas);
    this.renderer = renderer;

    const labels = new CSS2DRenderer();
    labels.domElement.className = "label-layer";
    stage.appendChild(labels.domElement);
    this.labelRenderer = labels;

    this.scene.add(new HemisphereLight(0xffffff, 0xcbd5e1, 2.2));
    const sun = new DirectionalLight(0xffffff, 1.4);
    sun.position.set(0.6, 1, 0.35);
    this.scene.add(sun);

    const controls = new OrbitControls(this.camera, renderer.domElement);
    controls.enableDamping = false;
    controls.screenSpacePanning = true;
    controls.addEventListener("change", () => this.requestRender());
    const takeOver = () => {
      if (this.follow) {
        this.follow = false;
        this.onUserCamera();
      }
    };
    controls.addEventListener("start", takeOver);
    // Keyboard: OrbitControls pans with the arrows and turns with Shift+arrows;
    // + and - zoom. Any of them hands the camera to the user, like a drag.
    controls.listenToKeyEvents(canvas);
    canvas.addEventListener("keydown", (event) => {
      const zoom = event.key === "+" || event.key === "=" ? 1 : event.key === "-" || event.key === "_" ? -1 : 0;
      if (!zoom && !event.key.startsWith("Arrow")) return;
      event.preventDefault();
      takeOver();
      if (zoom > 0) controls.dollyIn(1.2);
      if (zoom < 0) controls.dollyOut(1.2);
      if (zoom) controls.update();
    });
    this.controls = controls;

    this.token = new Mesh(new OctahedronGeometry(1), new MeshLambertMaterial({ color: COLORS.token }));
    this.token.visible = false;
    this.scene.add(this.token);
    this.tokenLabel = label("", "label3d token");
    this.tokenLabel.center.set(0.5, 1);
    this.tokenLabel.position.set(0, 1.5, 0);
    this.token.add(this.tokenLabel);

    this.path = this.makeLine(false);
    this.carry = this.makeLine(true);

    new ResizeObserver(() => this.resize()).observe(stage);
    this.resize();
  }

  private makeLine(dashed: boolean): LineSegments2 {
    const material = new LineMaterial({
      color: COLORS.path,
      linewidth: dashed ? 2.5 : 3,
      dashed,
      dashSize: 1,
      gapSize: 0.6,
    });
    this.materials.push(material);
    const line = new LineSegments2(new LineSegmentsGeometry(), material);
    line.frustumCulled = false;
    line.visible = false;
    this.scene.add(line);
    return line;
  }

  /** Rebuild the trees (on load and when an input changes). */
  layout(specs: TreeSpec[]): void {
    for (const tree of this.trees) tree.dispose();
    this.trees = [];
    let x = 0;
    specs.forEach((spec, i) => {
      // Lay trees out left to right with a gap the width of the wider leaf level.
      const grid = treeGeometry(spec.shape).grid;
      const width = grid[grid.length - 1];
      if (i > 0) x += width / 2 + 6;
      this.trees.push(new TreeView(this, spec, x));
      x += width / 2;
    });
    this.extent = Math.max(...this.trees.map((t) => Math.max(t.grid[t.height - 1], -t.y[t.height - 1]))) + x;
    this.requestRender();
  }

  tree(key: TreeKey): TreeView {
    const tree = this.trees.find((t) => t.spec.key === key);
    if (!tree) throw new Error("no tree " + key);
    return tree;
  }

  /** Reset every piece of timeline-driven state. */
  resetState(): void {
    for (const tree of this.trees) {
      tree.reset();
      tree.shown = true;
    }
    this.tokenAnchor = null;
    this.tokenText = "";
    this.pathPoints = [];
    this.carryPoints = [];
  }

  setToken(anchor: Vector3 | null, text = ""): void {
    this.tokenAnchor = anchor;
    this.tokenText = text;
  }

  addPath(a: Vector3, b: Vector3): void {
    this.pathPoints.push(a.x, a.y, a.z, b.x, b.y, b.z);
  }

  setCarry(points: Vector3[]): void {
    this.carryPoints = [];
    for (let i = 0; i + 1 < points.length; i++) {
      const a = points[i];
      const b = points[i + 1];
      this.carryPoints.push(a.x, a.y, a.z, b.x, b.y, b.z);
    }
  }

  // ---- camera ----------------------------------------------------------

  shot(center: Vector3, size: number, az = -0.62, el = 0.62): Shot {
    return { target: center.clone(), dist: size * 1.25 + 3.5, az, el };
  }

  /** A shot from direction (az, el) that keeps every point in view. */
  fit(points: Vector3[], az = -0.62, el = 0.55, margin = 0.95): Shot {
    const min = new Vector3(Infinity, Infinity, Infinity);
    const max = new Vector3(-Infinity, -Infinity, -Infinity);
    for (const p of points) {
      min.min(p);
      max.max(p);
    }
    const target = min.clone().add(max).multiplyScalar(0.5);
    const radius = Math.max(max.clone().sub(min).length() / 2, 0.8);
    const vfov = (this.camera.fov * Math.PI) / 180;
    const aspect = this.gpu ? this.camera.aspect : 1.6;
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * aspect);
    const half = Math.min(vfov, hfov) / 2;
    return { target, dist: (radius / Math.sin(half)) * margin, az, el };
  }

  current(): Shot {
    const target = this.controls ? this.controls.target.clone() : new Vector3();
    const offset = this.camera.position.clone().sub(target);
    const dist = Math.max(offset.length(), 0.01);
    return { target, dist, az: Math.atan2(offset.x, offset.z), el: Math.asin(Math.max(-1, Math.min(1, offset.y / dist))) };
  }

  /** Move the camera along a shot path (log-distance, so big zooms feel even). */
  view(a: Shot, b: Shot, t: number): void {
    if (!this.follow || !this.controls) return;
    const target = a.target.clone().lerp(b.target, t);
    const dist = Math.exp(Math.log(a.dist) + (Math.log(b.dist) - Math.log(a.dist)) * t);
    const az = a.az + (b.az - a.az) * t;
    const el = a.el + (b.el - a.el) * t;
    this.camera.position.set(
      target.x + dist * Math.cos(el) * Math.sin(az),
      target.y + dist * Math.sin(el),
      target.z + dist * Math.cos(el) * Math.cos(az),
    );
    this.controls.target.copy(target);
    this.camera.lookAt(target);
  }

  // ---- drawing ---------------------------------------------------------

  sync(): void {
    if (!this.gpu) return;
    for (const tree of this.trees) tree.sync();
    if (this.token && this.tokenLabel) {
      this.token.visible = !!this.tokenAnchor;
      if (this.tokenLabel.element.textContent !== this.tokenText) this.tokenLabel.element.textContent = this.tokenText;
      this.tokenLabel.visible = !!this.tokenAnchor && !!this.tokenText;
    }
    this.pathSig = this.syncLine(this.path, this.pathPoints, this.pathSig);
    this.carrySig = this.syncLine(this.carry, this.carryPoints, this.carrySig);
    this.requestRender();
  }

  private syncLine(line: LineSegments2 | null, points: number[], sig: string): string {
    if (!line) return sig;
    const next = points.length + ":" + points.slice(-6).map((v) => v.toFixed(3)).join(",");
    line.visible = points.length > 0;
    if (next === sig || !points.length) return next;
    const geometry = new LineSegmentsGeometry();
    geometry.setPositions(points);
    line.geometry.dispose();
    line.geometry = geometry;
    if ((line.material as LineMaterial).dashed) {
      let length = 0;
      for (let i = 0; i < points.length; i += 6) {
        length += Math.hypot(points[i + 3] - points[i], points[i + 4] - points[i + 1], points[i + 5] - points[i + 2]);
      }
      const material = line.material as LineMaterial;
      material.dashSize = Math.max(length / 36, 0.05);
      material.gapSize = material.dashSize * 0.7;
      line.computeLineDistances();
    }
    return next;
  }

  requestRender(): void {
    if (!this.gpu || this.queued) return;
    this.queued = requestAnimationFrame(() => {
      this.queued = 0;
      this.draw();
    });
  }

  private draw(): void {
    const renderer = this.renderer;
    const controls = this.controls;
    if (!renderer || !controls) return;
    const dist = this.camera.position.distanceTo(controls.target);
    this.camera.near = Math.max(0.01, dist / 400);
    this.camera.far = Math.max(dist * 12, this.extent * 6, 100);
    this.camera.updateProjectionMatrix();
    if (this.token && this.tokenAnchor) {
      const toToken = this.camera.position.distanceTo(this.tokenAnchor);
      const size = Math.max(0.12, toToken * 0.02);
      this.token.scale.setScalar(size);
      this.token.position.copy(this.tokenAnchor).add(tmpV.set(0, size * 1.7, 0));
      this.token.rotation.y = performance.now() / 900;
    }
    this.cullCaptions();
    renderer.render(this.scene, this.camera);
    this.labelRenderer?.render(this.scene, this.camera);
  }

  /** Hide level captions that would land on top of a lower level's caption. */
  private cullCaptions(): void {
    const height = this.stage.clientHeight || 1;
    const width = this.stage.clientWidth || 1;
    const placed: [number, number][] = [];
    for (const tree of this.trees) {
      for (const caption of tree.captionWanted) {
        if (!caption.shown || !tree.shown) {
          caption.obj.visible = false;
          continue;
        }
        caption.obj.getWorldPosition(tmpV).project(this.camera);
        const x = ((tmpV.x + 1) / 2) * width;
        const y = ((1 - tmpV.y) / 2) * height;
        const clash = placed.some(([px, py]) => Math.abs(py - y) < 24 && Math.abs(px - x) < 150);
        caption.obj.visible = !clash && tmpV.z < 1;
        if (caption.obj.visible) placed.push([x, y]);
      }
    }
  }

  resize(): void {
    if (!this.renderer || !this.labelRenderer) return;
    const width = Math.max(this.stage.clientWidth, 1);
    const height = Math.max(this.stage.clientHeight, 1);
    this.renderer.setSize(width, height);
    this.labelRenderer.setSize(width, height);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    for (const material of this.materials) material.resolution.set(width, height);
    this.requestRender();
  }
}

/** True when this browser can create a WebGL2 context (three.js needs WebGL2). */
export function webglAvailable(): boolean {
  try {
    return !!document.createElement("canvas").getContext("webgl2");
  } catch {
    return false;
  }
}
