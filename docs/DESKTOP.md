# Desktop app

The desktop app uses Electron to host the existing React and TypeScript
workspace, with Python providing the same engine and CLI. Download size is
secondary to consistent rendering and reliable native integration.

The workspace ships as this macOS app (Apple Silicon, signed with a
Developer ID and notarized by Apple), with a Windows x64 installer as an
unsigned preview. There is
no browser version, and `myflames ui` is deprecated; the app uses it
internally to start its backend. The Python CLI (`pip install myflames`) remains
for the command-line tools. Linux distribution is deferred. The Windows
installer is built and smoke-tested in CI and attached to releases unsigned
([Windows build](../apps/desktop/README.md#build-the-windows-installer-preview)).

## Install and first launch

Download `myflames-3.3.1-macos-arm64.dmg` from the GitHub release, open it and
drag `myflames.app` to Applications. The app is signed with a Developer ID and
notarized by Apple, so it opens from Finder like any other downloaded app.
Releases before 3.3.0 were unsigned zips: Control-click those and choose Open
the first time. App data lives
under `~/Library/Application Support/myflames-desktop`, separate from the CLI's
`~/.myflames`. The minimum window size is 720 by 520 pixels.

`apps/desktop` is the development foundation. It opens the real workspace;
`docs/design/desktop-workspace.html` is a separate interactive design preview
with synthetic data. The preview does not connect to a database or execute SQL.
The working workspace now uses this design direction: compact neutral
navigation, links to open queries, a command palette and full-width Server
details. The preview remains a synthetic reference, not live application data.

Run the development app with `npm --prefix apps/desktop start` after installing
its dependencies. See [desktop setup](../apps/desktop/README.md) for prerequisites,
tests and platform limitations.

## Design direction

Home shows the installed engine version and a link to published releases.
Update status reads **Not checked yet** until you click **Check for updates**
in Home's version footer or About myflames; the result and its time are kept in
the app's own workspace store. The app never installs updates: **Download…**
saves the release file in your Downloads folder for you to install. SQL editing and plan analysis share
the **SQL workspace** navigation group. Home also opens Server and the diagram
canvas, including Mermaid ER imports. The app mark uses saturated flame colors
on white, while the interface retains system light and dark appearances.

The interface uses a compact navigator and document tabs, then gives the selected
task the rest of the window. Opening a statement replaces the list with its
details; Back restores the list and filters. Small windows show one work area
at a time. Consequential operations still open a focused SQL review and
confirmation dialog.

Use a quiet neutral palette, clear selection, compact rows and consistent
spacing. Put actions beside their object. Hide extended explanations until
requested, while keeping connection identity, write policy and failures
visible. The command palette should help experienced users move quickly
without becoming a requirement for finding features.

The preview demonstrates navigation, document tabs, full-width statement
details, filtering, sample query results, a schema view, appearance switching
and a keyboard command palette. Database diagram is a place of its own in the
SQL workspace group of the working product (after Query digest); its preview
shortcut represents an open document.

## Current implementation and next features

SQL completion and the first reviewed table editor are implemented in the
shared workspace. Completion suggests catalog names and simple aliases while
retaining the native editor input. Table data is a separate view of an explicit
table; arbitrary query results remain read-only. See
[the query guide](QUERY_WORKSPACE.md#sql-completion-and-table-data) for limits.
SSH connections and schema comparison have shipped too: a saved connection
can go through an SSH tunnel (see below), and Schema versions compares saved
or live schemas with a sync script myflames never runs. Parameter snapshots
compare configuration between servers or over time. The remaining rows below
are proposed extensions. Existing dump/restore, SQL/Mermaid diagram
import/export, result export, reports and Users and roles remain available.

| Order | Extension | First useful scope | Boundary |
| --- | --- | --- | --- |
| 1 | SQL completion | Suggest schemas, tables, columns and aliases from the selected connection. | Preserve editor undo and input methods; bound metadata reads. |
| 2 | Editable table data | Stage inserts, updates and deletes, then review the exact SQL together. | Begin with explicit tables and verified row keys; reject stale rows. Arbitrary result sets are not automatically editable. |
| 3 | Data import and full-result export | CSV/TSV column mapping, NULL handling, preview and rejected-row reporting. | Distinguish loaded rows from an export that runs another query. Specify batch and partial-failure behavior. |
| 4 | SSH connections (shipped) | Managed tunnels using keys or an agent, with host-key verification. | Keep ownership and cleanup explicit; never silently accept a changed host key. |
| 5 | Server history | Save snapshots and compare findings, configuration and measured workload (parameter snapshots and comparison have shipped). | Label sampling windows and server restarts. Start with manual snapshots before scheduled collection. |
| 6 | Schema comparison (shipped as Schema versions) | Compare saved metadata with a live schema, review changes and export a sync script. | Preserve unsupported clauses; do not infer renames or execute destructive synchronization automatically. |
| 7 | Object editing | Explore routines, triggers and events; design columns, indexes and relationships. | Show original definitions and review exact SQL before changes. |
| 8 | Incident collection | Collect a bounded diagnostics bundle and reopen it as an investigation. | Preview included data, distinguish query execution from collection, and redact exported secrets. |

Reference capabilities were checked in the local DBeaver, MySQL Shell,
Workbench and MySQLTuner source trees. Implementations must be original and
must not copy GPL code or text. Public references:
[DBeaver completion](https://dbeaver.com/docs/dbeaver/SQL-Editor/),
[data editing](https://dbeaver.com/docs/dbeaver/Data-Editor/),
[Workbench schema comparison](https://dev.mysql.com/doc/workbench/en/wb-design-schema.html),
[Shell diagnostics](https://dev.mysql.com/doc/mysql-shell/26.7/en/mysql-shell-utilities-debug-diagnostics.html),
and [MySQLTuner](https://github.com/major/MySQLTuner-perl).

## Distribution

All feature source remains public. The community build should enable all
features and have documented build instructions. Official free and paid builds
can differ in enabled features, with paid value in convenient installation,
signed releases, updates and support. Pricing and the free/paid feature matrix
are not defined by this development foundation. No payment or entitlement
checks have been added.

Bundle the Python engine with PyInstaller on the target operating system (macOS,
and Windows x64 for the preview installer).
The Apple Silicon `.app` bundles Python and opens directly from Finder.
Releases are signed with a Developer ID under the hardened runtime, notarized
and stapled, and ship in a DMG that is signed, notarized and stapled too (see
the [signed release build](../apps/desktop/README.md#build-the-signed-release));
local builds stay unsigned
([build instructions](../apps/desktop/README.md#build-an-unsigned-macos-app)).
Before distributing official builds more widely, complete native packaging
tests, dependency notices, a verified update channel, and a deliberate
MySQL/MariaDB client distribution policy. Installed clients are discovered in
standard macOS locations, and the MySQL and MariaDB installers' folders on Windows, as well as PATH. Keep the Python CLI
available independently.

Electron renderer code must stay sandboxed, with context isolation and no Node
integration. The main process owns its Python child and launch secret; it must
not expose a generic filesystem or process bridge. Its session refuses every
permission request except one: the Fullscreen API for the workspace's own
origin, which Learn's **Full screen** uses (`fullscreenOnly` in `main.cjs`). Quitting also stops the
backend's dump, restore and clone tools, which run in their own process groups.
The backend stops them first and audits each interrupted operation as
cancelled with outcome unknown. It lists their groups in a private file so the
main process can stop them when it has to kill the backend. Quitting waits at
most 7 seconds and reports processes it could not confirm stopped.

SSH tunnels (`myflames/ssh_tunnel.py`) run the system `ssh` in their own
process groups too, and the backend lists them in the same private file as
service groups: its own shutdown leaves them to their owner while the editor
and lifecycle cleanup that may need them runs (a job's KILL QUERY can still
go through its tunnel); 3 seconds after shutdown starts at the latest, a timer
starts closing them even when that cleanup is still running or fails (stopping
each ssh then waits up to 2 seconds for SIGTERM and 2 more after SIGKILL); the main process
kills them with the tools when it has to kill the backend. On macOS and other
POSIX systems each ssh starts under a small watchdog that leads its process
group and stops it when the process that opened the tunnel ends, even by
SIGKILL or a crash; the packaged backend runs it as `myflames-backend
__myflames_ssh_watchdog`. If the desktop app dies and the backend's own
shutdown hangs for 5 seconds, `backend-entry.py` SIGKILLs every tool and
tunnel group the backend owns before its own group. The file holds at most 16
groups (`MAX_TOOL_GROUPS` in `backend.cjs`, the same number as the backend's
`ToolGroups.MAX_GROUPS`, which refuses a 17th tool or tunnel before starting
it), and at most 8 tunnels are open at once. A group that holds only zombie
processes counts as stopped. ssh needs the user's own SSH
configuration, keys and agent, while the backend's HOME stays isolated:
`backendEnvironment` in `backend.cjs` passes `MYFLAMES_SSH_HOME` (the real
home, which `main.cjs` sets from `os.homedir()`) and, on macOS and other POSIX
systems, `MYFLAMES_SSH_AUTH_SOCK` (the inherited agent socket). The backend
gives them to ssh alone; database clients never see either. On Windows, ssh is
`%SystemRoot%\System32\OpenSSH\ssh.exe` (or `Sysnative`), else `ssh.exe` in an
absolute PATH folder (never the current folder or another PATHEXT name), and it
uses the OpenSSH agent's named pipe. Each ssh.exe starts suspended, joins the
backend's single job object (`myflames/win_job.py`, created with kill on job
close, its handle never inherited and never closed by myflames) and only then
runs, so Windows ends it, with anything it started, whenever the backend ends:
a normal exit, a crash, `taskkill /F`, or an Electron crash (libuv's
kill-on-close job ends the backend, whose job then ends ssh). The desktop's
normal Windows stop (`taskkill /PID backend /T /F`) is unchanged, and Windows
tunnels are not listed in the private groups file. If Windows cannot create
the job, assign ssh to it or resume it, ssh is stopped before it connects and
the tunnel is refused with a plain message. Dump, restore and clone tools are
not in that job yet, so on Windows they still outlive a backend that crashed
or was killed. Windows ssh.exe reads `~/.ssh/config`, keys and `known_hosts`
from the account's profile folder whatever HOME and USERPROFILE say. Tunnels
were checked on Windows 11 (OpenSSH_for_Windows 9.5p2) through the command
line, not yet through the packaged app. Existing API authentication,
write policy, privilege checks and audit remain enforced. Desktop development
uses a separate data directory and must not open the user's CLI workspace.
