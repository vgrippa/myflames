---
name: web-dev
description: Implements front-end features with React, TypeScript, Vite, and Tailwind. Use when building or modifying UI components, pages, API clients, or build configuration.
---

# Web Development (Front-End)

Implements and maintains front-end code for web apps in this workspace. Applies to React/Vite/TypeScript UIs (e.g. releem_flames) and to HTML/JS/CSS demos (e.g. myflames docs/demos).

## Tech Stack (when present)

- **React 18** + TypeScript (strict)
- **Vite** for build and dev server
- **Tailwind CSS** for styling
- **Zustand** for state; **React Router** for routing
- **Fetch** to backend APIs (no Supabase)

## Process

1. **Locate entry points**: `src/main.tsx`, `src/App.tsx`, routers, and API base URL (e.g. `VITE_API_URL`).
2. **Match existing patterns**: Same component structure, naming, and hooks as the rest of the app.
3. **Type strictly**: Use project types (e.g. `GraphType`, `FlamegraphOptions`); avoid `any`.
4. **API calls**: Use existing service modules (e.g. `myflamesService.ts`). Send bodies as specified (e.g. `explain_json` as string). Handle errors and loading state.

## Conventions

- **Components**: Functional components; props and state typed. Prefer small, focused components.
- **State**: Global state in Zustand; local UI state with `useState`/`useReducer` as appropriate.
- **Styling**: Tailwind utility classes; avoid inline styles unless necessary. Respect existing spacing/color tokens.
- **Env**: Use `import.meta.env.VITE_*` for config; document any new vars.

## Out of Scope

- Backend API implementation (FastAPI lives in `api/`).
- Data visualization algorithms (see viz-specialist for SVG/chart logic).
