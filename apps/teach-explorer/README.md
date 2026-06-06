# Teach Explorer

Standalone navigation app for the `myflames/teach` algorithm lessons.
Built independently of the myflames Python CLI — its own Vite build,
its own deploy target.

## What it does

- Browses all 21 teach lessons grouped by family (scan, index, join, cache).
- Surfaces title, summary, Big-O complexity (best/avg/worst), and a sparkline curve for each.
- **Compare view** overlays selected algorithms on one interactive log–log chart with a draggable `n` marker.
- Each algorithm's "Open full lesson →" link points at the existing pre-rendered HTML under `../../docs/teach/<family>/<name>.html`.

The existing `docs/teach/index.html` is **not** replaced — this app sits alongside it.

## Develop

```bash
pnpm install
pnpm dev          # http://localhost:5174
```

## Regenerate lesson metadata

The catalog data is hand-curated complexity metadata plus titles/summaries pulled from the Python `LESSONS` dict.

```bash
pnpm regen-data
# equivalent to: cd ../.. && python3 scripts/export-teach-metadata.py
```

This rewrites `src/data/algorithms.json`. Re-run whenever you add a lesson to `myflames/teach/*_family/__init__.py` or edit the complexity table in `scripts/export-teach-metadata.py`.

## Build

```bash
pnpm build
```

Output goes to `../../docs/teach-explorer/` (deployable to GitHub Pages or any static host).

## Stack

- Vite 5, React 18, TypeScript strict
- Tailwind CSS 3
- Framer Motion for transitions
- React Router (hash mode so it works on plain static hosting)
- Zero CDN dependencies at runtime (Inter font preconnect is optional — system stack is the fallback)
