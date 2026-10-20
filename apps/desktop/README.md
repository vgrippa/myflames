# myflames desktop development

This launcher opens the existing React/TypeScript workspace in Electron and
starts its own Python backend. The CLI and browser workspace remain available.

The current implementation and verification target is macOS on Apple Silicon.
An unsigned `.app` bundles Electron, Python and the workspace, and opens directly
from Finder. Windows and Linux distribution are deferred.

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph)
and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/).

## Run

Use Node.js 22.12 or newer and Python 3.7 or newer. Build the workspace first if
its bundled assets are missing or stale:

```sh
cd apps/workspace
npm ci
npm run build
cd ../desktop
npm ci
npm start
```

The launcher runs `python3 -m myflames ui --port 0 --no-browser` from the repository
root (`python` on Windows). Set `MYFLAMES_DESKTOP_PYTHON` to a Python executable
path if needed. It must be an executable path, without extra arguments.

Development data lives in the platform application data folder under
`myflames-desktop-development`. The Python HOME and USERPROFILE are set to its
`backend-home` subfolder. The desktop uses the vault backend and does not import
the CLI's connections, passwords or workspace. `MYFLAMES_DESKTOP_DATA` can select
another directory. Paths resolving into the real `~/.myflames` are rejected,
including symlink aliases. Do not share the desktop data directory between users.

The launch URL is captured privately from the owned Python process. Its one-use
secret becomes the existing HttpOnly session cookie and origin-bound proof.
Reload keeps that session; restarting launches a new backend and authenticates
again. The window starts with no renderer Node integration, a sandbox, context
isolation and no preload or renderer IPC bridge. Network requests stay on the
owned loopback origin. External HTTPS links to the supported documentation,
download and inspiration sites open in the normal browser. Other navigation,
popups and permission requests are denied. Native Edit menu copy/paste remains
available; clipboard permissions and additional desktop integrations need
separate review before release.

Closing the final window quits this development app. Normal shutdown sends a
signal to its owned process group on macOS/Linux and cleans up descendants. On
Windows it uses `taskkill` for the owned process tree, checks the child exited,
and reports cleanup failures. **Windows crash cleanup is incomplete:** once the
backend parent has already exited, descendants may survive. A Windows Job Object
supervisor and native Windows validation are release prerequisites. Force-killing
the Electron main process also bypasses normal cleanup. Do not use this foundation
for unattended production operations.

## Verify

```sh
npm test
npm run smoke
```

The Node tests cover launch parsing, origin checks, link restrictions, isolated
environments, symlink rejection, startup failure/timeout and owned shutdown.
The smoke opens a real Electron window using a temporary data directory, checks
an authenticated API request, renderer isolation, command navigation and SQL
keyword completion, captures the window, quits,
and verifies the owned Python process stopped. The temporary data is removed;
the screenshot remains at `smoke-results/desktop.png`. It requires a graphical
session. It performs no database operations.

## Packaging foundation

Electron 44.8.0 and electron-builder 26.15.3 are pinned in the lock file. Their
versions were checked against the npm registry. The security settings follow the
[Electron security guide](https://www.electronjs.org/docs/latest/tutorial/security/)
and [sandbox documentation](https://www.electronjs.org/docs/latest/tutorial/sandbox).

The included PyInstaller script builds a backend directory on the target OS.
Use a disposable virtual environment and install PyInstaller there, then run:

```sh
python scripts/build-backend.py
npm run package:dir
```

`build-backend.py` collects myflames Python modules and package assets. The builder
copies that directory into the app resources, and a packaged app uses that
backend rather than a system Python. Build on each target OS and architecture.
The package directory is scaffolding, not a signed or notarized installer.
Signing, notarization, updater trust, platform icons, dependency licensing,
Windows process supervision and packaged platform validation remain release work.
Database clients are not bundled. On macOS the launcher appends standard
Homebrew, Oracle MySQL and MacPorts client locations to the backend PATH so
Finder launches can discover installed tools without reading shell startup files.
No automatic application updates or install actions are enabled.

## Build an unsigned macOS app

Use a disposable build environment with a supported Python version (the current
Apple Silicon build was verified with Python 3.14). From the repository root:

```sh
python3 -m venv local/desktop-build-venv
local/desktop-build-venv/bin/python -m pip install -r apps/desktop/requirements-build.txt
npm --prefix apps/workspace run build
local/desktop-build-venv/bin/python apps/desktop/scripts/build-backend.py
CSC_IDENTITY_AUTO_DISCOVERY=false npm --prefix apps/desktop run package:mac
node apps/desktop/scripts/smoke.cjs apps/desktop/dist/mac-arm64/myflames.app
```

Copy `apps/desktop/dist/mac-arm64/myflames.app` to your Applications folder and
double-click it. No terminal, Node.js or separately installed Python is required
to run the bundle. Its data lives under `~/Library/Application Support/myflames-desktop`,
separate from development data and the CLI workspace. Connections must be added
there independently.

The macOS configuration explicitly disables Developer ID signing and
notarization. Apple Silicon executables retain local ad-hoc signatures required
to run; these do not certify a publisher. The app includes the project's LICENSE
and NOTICE under `Contents/Resources/licenses`, with the original inspiration
credit retained in About.

The packaged smoke uses a temporary app-data directory, a minimal Finder-style
PATH and an invalid system Python override. It verifies the bundled backend,
authentication, reload, renderer isolation, command search, keyword completion
and owned-process shutdown. It does not connect to a database. Packaged test mode
requires both `--smoke` and `MYFLAMES_DESKTOP_SMOKE=1`; normal Finder launches do
not enter it.
