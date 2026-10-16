# myflames Performance workspace

Local React + TypeScript interface (`myflames ui`) for the existing myflames
Python engine. The shell is `src/main.tsx` with `src/Sidebar.tsx`; the places
are Home (`Home.tsx`, with `Connections.tsx`), Plans, Query editor
(`QueryLab.tsx`), Server (`Server.tsx` and `SlowQueries.tsx`), Investigations
(`Library.tsx`), and Learn (`Teach.tsx`). Design tokens are on `:root` in
`src/style.css`.

```bash
npm ci
npm run build
cd ../..
python3 -m myflames ui
```

`npm run dev` rebuilds the packaged assets on source changes. Run the Python
server separately and refresh the browser after a rebuild. Vite's development
server is not used: Python serves the page, assets, and authenticated local API
from one origin.

The generated `myflames/ui_assets/` directory is committed and included in the
Python wheel. End users do not need Node.js or an npm install.

See [Performance workspace](../../docs/LOCAL_UI.md) for behavior, limits, and API
contracts. The core package stays standard-library-only.

## Authentication and UI contracts

Open the launch URL printed by the Python server. `api.ts` removes its one-use
fragment and exchanges it for a per-instance HttpOnly cookie and an origin-scoped
proof stored as `myflames.session-proof`. Both are required on API calls and
bootstrap refresh; the HTML shell contains no token. Use a fresh launch URL
after restart. Test helpers must perform this exchange rather than extract HTML
credentials. See the [server contract](../../docs/DEVELOPER_REFERENCE.md#local-authentication-and-request-limits).

On phones, flame graphs, execution trees and bar charts keep readable text in a horizontally scrollable canvas.

The UI follows system light or dark appearance. Verify charts, Compare and Audit
at 1440 and 390 pixels in both. Configuration Session opens the planned SQL in
the Query editor. Users and roles combines unticked catalog privileges, Add
scope, and the existing exact-SQL batch review. Database diagram is under
Server > Schema; imports never execute SQL, and JSON is the editable layout
format. Diagrams stay in memory unless exported.


## Browser smoke test

```bash
npm run test:browser
```

This builds the frontend, launches a temporary local Python server and headless
Chrome, tests import/recovery, all chart views, Visual Explain zoom/search/selection
and exported HTML, comparison, downloads,
plan removal, Learn catalog filters, lesson playback/navigation/downloads, and mobile overflow, then stops its processes. Screenshots and
downloads are written to a temporary directory printed by the test. Set `PYTHON`
to choose an interpreter. Puppeteer is a development dependency only.

For a manual pass with the Python UI running:

- Load the sample and switch through all four charts.
- Select and search operators; inspect a finding's linked operator.
- Import a second plan through the file picker, drag-and-drop, and paste dialog.
- Submit invalid JSON and check that it can be corrected without losing plans.
- Compare two plans and download HTML/JSON from both exploration and comparison.
- Remove a plan currently selected for comparison.
- Check keyboard navigation, dialog focus, and narrow screens.
- Refresh and confirm that the workspace is restored where you left it.

The API and command-line regressions run with the repository's Python suite.

## Navigation checks

`npm run test:navigation` checks the six places and their active state, the
Open plans list, the status indicator and Settings popover, Home's
Connections (add, edit, test, use, forget, delete), the connection picker in
the Query editor and Server, keyboard movement between places, and the
390-pixel layout with its Menu drawer. The profile API is mocked; nothing
touches a real keychain.

## Connection binding checks

`npm run test:connection-binding` switches between two saved connections and
checks that Server reports, selections, reads and actions stay bound to the
connection they came from: a report from the other connection is greyed with
its actions off, late answers are dropped, Kill, Explain top N and Run analysis
keep their confirmed target, Locks stops polling while hidden, and the Compare,
Cancel capture and Save connection races are closed. `npm run
test:session-rules` checks the restore rules and the connection identity in
Node, without a browser.

## Table column checks

`npm run test:datatable` checks the shared data table on the Indexes and
Sessions views: resizing any column by pointer and keyboard, the Columns menu
(hide, show, the last visible column stays, Reset columns), the layout kept
with the workspace across a reload and a corrupt stored layout ignored, sorting
independent of visible columns, compact counts with the exact value as the
tooltip, and a 2,000-row table drawn in a window that the keyboard still walks.

## Investigation checks

`node test/investigation.mjs` checks focus/collapse, contextual Learn links, local saves,
reopening, portable bundles, file import, side-by-side comparison, and responsive
layouts against a temporary workspace database. `node test/exploration.mjs`
checks selection in the five sandboxed chart renderers and the `diagram` alias.

For optional live browser verification, point `MYFLAMES_TEST_PORT` at a disposable
MySQL server on loopback with database `lab`, table `items(id, category, price)`,
user `root`, and password `myflames-validation-only`. The test issues SELECTs and
stores no profiles in the user's workspace. Do not point it at production.

`npm run test:zoom` checks proportional mouse/trackpad input, bounded momentum,
cursor anchoring, and toolbar/pan behavior in a real browser.
