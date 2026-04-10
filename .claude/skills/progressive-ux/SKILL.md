---
name: progressive-ux
description: Designs outputs that serve both first-time viewers AND senior DBAs through progressive disclosure. Use when designing the HTML wrappers around myflames SVGs, writing onboarding copy, adding glossary entries for EXPLAIN jargon, or reviewing whether an output is readable by someone who has never seen an EXPLAIN plan before. Owns the *approachability* of outputs — not their correctness or visual polish.
---

# Progressive UX for Technical Outputs

Design outputs where a first-time reader can land, understand the one thing that matters, and leave confident — while a senior DBA can skim the same page in two seconds and find every metric they need. The rule is **progressive disclosure**: surface a clear primary action above the fold, keep advanced detail one click away, never dump everything on the reader at once.

## Principles

1. **Default to dense (for experts), open to guide (for newcomers).** Never force a DBA through an onboarding flow, and never strand a newcomer in front of a wall of raw metrics. A single mode toggle, or inline `<details>` elements that start collapsed, satisfies both.

2. **Primary action above the fold.** Every output must promote exactly ONE "fix this first" suggestion at the top — the one the advisor ranked highest. Everything else is secondary. If the tool has nothing urgent to say, say that explicitly.

3. **Translate, don't simplify.** Jargon stays (`filesort`, `Block Nested-Loop`, `derived table`) — it's precise and senior DBAs need it — but every jargon term is paired with a **one-sentence plain-English translation** on first hover or in an inline glossary chip. Never dumb down the technical term itself.

4. **Copy-paste as a first-class affordance.** Every SET statement, every DDL suggestion, every code snippet must live in a selectable `<pre>` or `<code>` block — never locked inside an SVG `<text>` node. If a DBA can't paste it into Slack or a ticket, the suggestion has failed.

5. **Crossref the visualization.** When a warning says "Table scan on users (3000 rows)", clicking the warning should visually highlight the matching node in the SVG (or at minimum scroll it into view with a callout). Words and pictures must point at each other.

6. **Accessible by default.** Contrast ratios ≥ 4.5:1 for body text, focus states on every interactive element, ARIA landmarks (`<nav>`, `<main>`, `<aside>`) so screen readers can navigate sections, `<abbr title="...">` for every acronym.

## The Three Audiences (decision matrix)

| Element | Newcomer sees… | DBA sees… | Never show |
|---|---|---|---|
| Top strip | Plain-English summary + primary action | Same strip, one-line | — |
| Warnings | Expanded with glossary chips on jargon | Dense list, collapsed | — |
| Suggestions | `Why:` clause visible + ELI5 tooltip | Just the SET / DDL | — |
| Glossary | Inline chips on first hover | Hidden by default | — |
| Raw metrics | Behind a "Show details" click | Always visible in a dense table | — |
| Query text | Always visible, syntax-highlighted | Same | Behind a tab — always visible |

## Process

1. **Audit the current output.** Read the existing HTML/SVG with "What would a dev who has never seen EXPLAIN output think?" in mind. List three things that would confuse them in the first 10 seconds.

2. **Identify the primary action.** From the advisor output, pick the single most impactful recommendation and promote it to the top strip. If the advisor has ranked multiple, pick the one with the highest `severity` (or the one pointing at the biggest cost-contributing node).

3. **Design the top strip.** One sentence of plain English describing what this query does ("Scans 3,000 users, sorts by name, returns top 10"), followed by the primary action in a visually distinct card ("Fix first: raise sort_buffer_size to 8M — sort is spilling to disk").

4. **Layer the rest.** Everything else goes below the primary strip, in dense but navigable sections. Use `<details>` for anything that's not immediately relevant (Raw JSON, Collected schema, Collected stats).

5. **Glossary pass.** Scan the output for jargon: `filesort`, `hash join`, `BNL`, `materialize`, `derived table`, `temp table spill`, `index merge`, `covering index`, `MRR`, `ICP`. Every occurrence gets a `<abbr>` wrapper with a one-sentence definition — or a project-wide glossary linked from the footer.

6. **Test with the newcomer lens.** Ask: "If I removed all prior context, would the top 200px tell me (a) what this query does, (b) whether it's broken, and (c) the single most useful thing I can do about it?" If any of the three is no, iterate.

## Conventions

- **Tone:** Direct but not terse. "Sort will likely spill to disk" is good. "OMG your sort is broken" is not. "The optimizer chose a suboptimal execution strategy" is not — say what it actually did.
- **Length budgets:** Top strip ≤ 2 sentences. Each warning body ≤ 3 lines. Each suggestion body ≤ 4 lines unless it's a `Why:` explanation (which has a higher cap because it carries real info).
- **Jargon threshold:** If a term appears 3+ times on the page, it needs a glossary entry. If it appears once, inline parenthetical is enough ("Block Nested-Loop (an outer-inner join that scans the inner table once per batch of outer rows)").
- **Glossary chips:** Use CSS + `<abbr>` for zero-JS tooltips on hover, OR use `<details><summary>` for mobile-friendly tap-to-expand. Never use JS-only tooltips — screen readers break.
- **Colors for severity:** `#b71c1c` for errors/warnings, `#ef6c00` for watch-out notices, `#1b5e20` for recommendations, `#0d47a1` for info. Consistent across all myflames outputs.

## Out of Scope

- MySQL correctness of advice (see `mysql-expert`).
- Graph/chart internals (see `viz-specialist`).
- Tailwind / React / build pipeline (see `web-dev`).
- Machine-readable output formats (see `structured-output`).

## Key Files in myflames

- [myflames/parser.py](myflames/parser.py) — `render_info_panel` currently stacks everything inside the SVG. Progressive-UX redesign pulls most of this OUT of the SVG into the HTML wrapper.
- [myflames/output_html_report.py](myflames/output_html_report.py) — the main entry point for HTML output. Most progressive-disclosure work lands here.
- [docs/demos/](docs/demos/) — existing demo HTML wrappers are minimal (title + SQL card + SVG). They need the top strip, the glossary chips, and the expandable sections.
