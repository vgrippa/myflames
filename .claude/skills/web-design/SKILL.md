---
name: web-design
description: Improves UI/UX and visual design for web interfaces. Use when designing layouts, typography, color, spacing, accessibility, or refining visual hierarchy.
---

# Web Design (UI/UX)

Improves the look, usability, and accessibility of web interfaces. Applies to React UIs, HTML demos, and SVG/HTML outputs (e.g. myflames docs/demos and releem_flames UI).

## Principles

1. **Visual hierarchy**: Important actions and information stand out; secondary content is clearly secondary.
2. **Consistency**: Same fonts, spacing scale, and color usage across related screens and components.
3. **Accessibility**: Sufficient contrast, focus states, and semantic structure (headings, landmarks, labels).
4. **Responsiveness**: Layouts work on small and large viewports; touch targets are adequate.

## Process

1. **Audit**: Identify layout, typography, color, and interaction patterns already in use.
2. **Align**: Propose changes that fit the existing design system (e.g. Tailwind palette, font stack).
3. **Implement**: Prefer CSS/Tailwind over one-off inline styles. Preserve existing behavior unless the task is to change it.
4. **Check**: High-contrast text, visible focus, and readable font sizes.

## Conventions

- **Fonts**: Prefer modern system or project fonts (e.g. Inter, Roboto). Use a single scale for headings and body.
- **Color**: Use semantic tokens (e.g. `blue-50` for info, `red-500` for errors). Avoid low-contrast text on backgrounds.
- **Spacing**: Use a consistent scale (e.g. 4/8/16/24px). Group related elements; separate sections clearly.
- **SVG/HTML demos**: Keep a unified palette (e.g. Join vs Scan colors). Update both `height` and `viewBox` when changing SVG size.

## Out of Scope

- Backend or API design.
- Deep data-viz logic (see viz-specialist for chart/SVG structure and tooltips).
