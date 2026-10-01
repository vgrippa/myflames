# Workspace UI testing

Use a separate `WorkspaceServer(store_path=...)` database for audits. Use a
disposable database account and sample dataset for live captures. Keep credentials,
screenshots containing query data, downloaded bundles, and audit databases under
the ignored `local/` directory.

## Interaction matrix

| Screen | Checks |
| --- | --- |
| Welcome | Logo/favicon, sample, import, query laboratory, Teach, keyboard focus |
| Import | Empty form, malformed JSON, wrong plan shape, file upload, same-file retry, size rejection, Escape, narrow/short windows, successful recovery |
| Plan explorer | All five views, operator search/no results, linked selection, all metrics, focus/collapse/expand, breadcrumbs, bottleneck shortcuts, raw fields, SQL copy |
| Visual Explain | Zoom percentage, plus/minus, reset, fit, limits, press feedback, keyboard operation, wheel and pan |
| Query laboratory | Connection failure/recovery, profiles without passwords, query tabs, parameter errors, estimated/measured capture, repeated runs, trace, SQL errors, timeout, cancellation |
| Execution context | Missing context, schema/index expansion, run statistics, trace search/no results/clear, server settings |
| Investigations | Empty/loading states, names/notes/tags search, no results/clear, save, reload/reopen, delete confirmation/Keep |
| Compare | Empty state, same-plan guard, measured versus estimated, two measured plans, row selection, HTML/JSON exports |
| Teach | Search/no results/clear, family filters, all lesson screens, Play/Pause/Reset, speed/loop/sliders, previous/next, first/last boundaries, contextual return/reopen, lesson download |
| Portability | Analysis JSON, HTML report, comparison files, investigation bundle, imported bundle, unsaved-work backup, password exclusion |

For each primary screen check desktop, tablet, narrow, and short windows. The
September 2026 audit used widths 1440, 768, 390, and 320 pixels. Check both page
overflow and actual screenshots: an element can be clipped without increasing
the document width. Use viewport screenshots for sticky layouts; stitched
full-page captures can repeat sticky or embedded content.

## Regression commands

```bash
./run-tests.sh
cd apps/workspace
npm run build
npm run test:browser
npm run test:investigation
npm run test:exploration
npm run test:zoom
```

The Python suite checks server, parser, renderer, persistence, and capture
behavior. Browser suites exercise the packaged frontend; the zoom suite uses
real SVG geometry. The September 2026 audit also used direct browser interaction
against MySQL 8.4 with Sakila, checked all 23 lesson players, and rendered all
38 Sakila estimated/measured plans across all five chart types.

## Platform checks

Repeat OS file drag/drop and native file-picker cancellation on the target
platform. Verify physical Magic Mouse/trackpad gestures and the native Chrome
favicon cache on macOS. These are distinct from in-app browser checks and
synthetic wheel tests. Inspect every permanent-delete confirmation, but use
backend tests for disposable deletion when interactive approval is unavailable.

This matrix covers named screens and representative states. It does not imply
every possible SQL input, animation parameter combination, or browser version
has been tested.
