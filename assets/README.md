# myflames assets — Tier-1 lesson runtime

This workspace builds `myflames/assets/anim-runtime.js`, a small bundle
that wraps **Motion One** (FLIP / spring animations) and **d3** (treemap
layouts, Catmull-Rom curves) into additive helpers on `window.anim`.

The bundle is **committed** so end users never touch `npm`. Only
contributors who edit the TypeScript source need to rebuild it.

## When to rebuild

Rebuild after editing anything in `src/`:

```
cd assets
npm install          # first time only, or when package.json changes
npm run build        # emits ../myflames/assets/anim-runtime.js
```

`npm run build:watch` keeps a watcher running during development.

## What the bundle adds

The existing hand-rolled primitives in `_html.py::ANIM_JS` still run
first and install the core API (`anim.tween`, `anim.timeline`,
`anim.pulse`, `anim.arrival`, `anim.svgEl`, `anim.complexityChart`).
Then this bundle runs and merges **new** helpers in without
overwriting:

| Helper | Backing library | Replaces |
|---|---|---|
| `anim.flip(el, {x,y,scale,rotate,opacity}, opts)` | Motion One `animate()` | ~40 lines of hand-rolled `tween` + `setAttribute("transform")` |
| `anim.spring(el, props, {stiffness,damping,mass})` | Motion One `spring()` | Mechanical-feeling cubic eases for "arrival" moments |
| `anim.squarify(tree, width, height)` | `d3-hierarchy.treemap` | The ~150-line hand-rolled `_layout_squarified` |
| `anim.smoothPath([{x,y}, …])` | `d3-shape.curveCatmullRom` | Hand-rolled 2-point quadratic Bezier path strings |

All helpers honour `prefers-reduced-motion` — under reduced motion
they apply the end-state immediately and resolve synchronously.

## Licenses

All runtime dependencies are permissive (MIT / ISC) — see
`package.json`. Dev-only: `esbuild` (MIT), `typescript` (Apache-2.0).

## File layout

```
assets/
  package.json          npm metadata — deps + scripts
  build.mjs             esbuild entrypoint (minified IIFE → ../myflames/assets/)
  tsconfig.json         TypeScript strict config
  src/
    runtime.ts          The Tier-1 module. Entry point.
```
