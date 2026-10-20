# myflames desktop development

This launcher opens the existing React/TypeScript workspace in Electron and
starts its own Python backend. The workspace ships only as this macOS app; there
is no browser version and `myflames ui` is deprecated (the launcher still uses it
as the backend entry point). The Python CLI remains available for command-line use.

The implementation and verification target is macOS on Apple Silicon. An
unsigned `.app` bundles Electron, Python and the workspace, and opens directly
from Finder. Windows and Linux distribution are deferred, and the Windows notes
below describe unfinished groundwork, not a supported platform.
The window opens at 1440 by 960 pixels and cannot be smaller than 720 by 520.

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

Closing the final window quits this development app. On macOS/Linux, quitting
sends SIGINT to the backend's own process group. The backend first stops its
dump, restore and clone tools, which run in process groups of their own:
SIGTERM, then SIGKILL after one second, with `KILL QUERY` for their server
sessions started at the same time. Each interrupted operation is audited as
cancelled with outcome unknown, as for Cancel; slower cleanup follows. The
backend lists those tool groups in a fresh owner-only file in the data
directory's `runtime` folder (`MYFLAMES_TOOL_GROUPS_FILE`, `tool-groups-1.0`);
a tool whose group it cannot record is stopped at once. If the backend has not
exited after 5 seconds, the launcher kills the listed groups, kills the
backend's process group, then kills any group in the backend's last list that
it has not already seen gone. Quitting finishes only when the backend and every
listed tool group are gone, within 7 seconds. Otherwise, or when the list is
missing or unreadable, a dialog says that owned processes may remain. Lists
from an earlier launch are deleted, never used. On
Windows it uses `taskkill` for the owned process tree, checks the child exited,
and reports cleanup failures. **Windows crash cleanup is incomplete:** once the
backend parent has already exited, descendants may survive. A Windows Job Object
supervisor and native Windows validation are release prerequisites. Force-killing
the Electron main process bypasses normal cleanup; on macOS the packaged backend
notices within about a second that its parent is gone, stops like Ctrl+C and then
removes its own process group, including database clients still running
(`scripts/backend-entry.py`; only when the desktop started it and named its pid in
`MYFLAMES_DESKTOP_PARENT`). Tools started in their own session, such as a dump,
are not in that group. The development
`python3 -m myflames ui` child has no such watch. Do not use this foundation
for unattended production operations.

## Verify

```sh
npm test
npm run smoke
```

The Node tests cover launch parsing, origin checks, link restrictions, isolated
environments, symlink rejection, startup failure/timeout, owned shutdown
(including the tool process groups of a backend that has to be killed, also with
real processes) and the backend entry stopping after its parent is killed.
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
npm run build:backend
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

`npm run build:backend` runs the same `scripts/build-backend.py` with `python3`;
use the virtual environment's Python as shown so PyInstaller is available.
`package:mac` produces the Apple Silicon `.app` only. To publish it, zip the
bundle as `myflames-<version>-macos-arm64.zip` (for example with
`ditto -c -k --keepParent myflames.app myflames-3.1.0-macos-arm64.zip`) and attach
it to the GitHub release.

Copy `apps/desktop/dist/mac-arm64/myflames.app` to your Applications folder and
double-click it (Control-click and Open the first time, because the app is
unsigned; or `xattr -dr com.apple.quarantine` on the copy). No terminal, Node.js
or separately installed Python is required to run the bundle. Its data lives under `~/Library/Application Support/myflames-desktop`,
separate from development data and the CLI workspace. Connections must be added
there independently.

The macOS configuration explicitly disables Developer ID signing and
notarization. Apple Silicon executables retain local ad-hoc signatures required
to run; these do not certify a publisher. The app includes the project's LICENSE
and NOTICE under `Contents/Resources/licenses`, with the Electron, Chromium and
bundled Python license notices beside them, and the original inspiration credit
retained in About. Electron fuses turn off `ELECTRON_RUN_AS_NODE`, `NODE_OPTIONS`
and `--inspect`, load the app only from its asar archive and validate that
archive's integrity. `build-backend.py` collects the myflames modules and package
data but not distribution metadata, so a leftover development `myflames.egg-info`
cannot ship. Run with the system `python3`, it hands over to
`local/desktop-build-venv` when that venv exists.

The packaged smoke uses a temporary app-data directory, a minimal Finder-style
PATH and an invalid system Python override. Run it after every build and before
attaching the zip to a release. It verifies the bundled backend,
authentication, reload, renderer isolation, command search, keyword completion
and owned-process shutdown. It does not connect to a database. Packaged test mode
requires both `--smoke` and `MYFLAMES_DESKTOP_SMOKE=1`; normal Finder launches do
not enter it.
