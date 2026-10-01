# myflames workspace

Local React + TypeScript interface for the existing myflames Python engine.

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

See [Local plan workspace](../../docs/LOCAL_UI.md) for behavior, limits, and API
contracts. The core package stays standard-library-only.

## Browser smoke test

```bash
npm run test:browser
```

This builds the frontend, launches a temporary local Python server and headless
Chrome, tests import/recovery, all chart views, Visual Explain zoom/search/selection
and exported HTML, comparison, downloads,
plan removal, Teach catalog filters, lesson playback/navigation/downloads, and mobile overflow, then stops its processes. Screenshots and
downloads are written to a temporary directory printed by the test. Set `PYTHON`
to choose an interpreter. Puppeteer is a development dependency only.

For a manual pass with the Python UI running:

- Load the sample and switch through all five charts.
- Select and search operators; inspect a finding's linked operator.
- Import a second plan through the file picker, drag-and-drop, and paste dialog.
- Submit invalid JSON and check that it can be corrected without losing plans.
- Compare two plans and download HTML/JSON from both exploration and comparison.
- Remove a plan currently selected for comparison.
- Check keyboard navigation, dialog focus, and narrow screens.
- Refresh and confirm that the session clears.

The API and command-line regressions run with the repository's Python suite.

## Investigation checks

`node test/investigation.mjs` checks focus/collapse, contextual Teach, local saves,
reopening, portable bundles, file import, side-by-side comparison, and responsive
layouts against a temporary workspace database. `node test/exploration.mjs`
checks selection in the five sandboxed chart renderers and the `diagram` alias.

For optional live browser verification, point `MYFLAMES_TEST_PORT` at a disposable
MySQL server on loopback with database `lab`, table `items(id, category, price)`,
user `root`, and password `myflames-validation-only`. The test issues SELECTs and
stores no profiles in the user's workspace. Do not point it at production.

`npm run test:zoom` checks proportional mouse/trackpad input, bounded momentum,
cursor anchoring, and toolbar/pan behavior in a real browser.
