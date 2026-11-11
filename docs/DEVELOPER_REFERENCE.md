# MyFlames Developer Reference

This is the detailed contract per feature; [AGENTS.md](../AGENTS.md) has the rules. Read the relevant section before changing behavior, output formats, UI integration, or feature-specific tests.

## Table of contents

- [Commands Reference](#commands-reference)
  - [Local UI](#local-ui)
  - [Release sync](#release-sync)
  - [Release notes](#release-notes)
  - [Shared view presentation](#shared-view-presentation)
  - [Chart runtime](#chart-runtime)
  - [Bundle and lazy loading](#bundle-and-lazy-loading)
  - [Local authentication and request limits](#local-authentication-and-request-limits)
  - [Client and local-storage boundaries](#client-and-local-storage-boundaries)
  - [Reviewed actions and session ownership](#reviewed-actions-and-session-ownership)
  - [Shared CLI confirmation](#shared-cli-confirmation)
  - [Profiles](#profiles)
  - [Write safety and audit](#write-safety-and-audit)
  - [SQL CLI](#sql-cli)
  - [Query editor sessions](#query-editor-sessions)
  - [Run](#run)
  - [Plan Stats and shared-plan literal hiding](#plan-stats-and-shared-plan-literal-hiding)
  - [Digest](#digest)
  - [Plan diff](#plan-diff)
  - [CI gate](#ci-gate)
  - [Advise](#advise)
  - [Rule controls](#rule-controls)
  - [Advisor evidence rules](#advisor-evidence-rules)
  - [Find slow queries](#find-slow-queries)
  - [Locks](#locks)
  - [Sessions](#sessions)
  - [InnoDB](#innodb)
  - [Error log](#error-log)
  - [Server identity: Percona Server](#server-identity-percona-server)
  - [Server health](#server-health)
  - [Baselines: Health and Query digest](#baselines-health-and-query-digest)
  - [Waits, Indexes, Tables, I/O, Config](#waits-indexes-tables-io-config)
  - [Memory](#memory)
  - [Server variables: config set, reset-persist](#server-variables-config-set-reset-persist)
  - [Instruments](#instruments)
  - [Full analysis report](#full-analysis-report)
  - [Database Tuning Advisor](#database-tuning-advisor)
  - [Server lifecycle: upgrade-check, dump, restore, clone](#server-lifecycle-upgrade-check-dump-restore-clone)
  - [Replication: status, skip, skip-loop](#replication-status-skip-skip-loop)
  - [Cluster: Galera, Percona XtraDB Cluster, InnoDB Cluster](#cluster-galera-percona-xtradb-cluster-innodb-cluster)
  - [Users and roles: privileges, accounts](#users-and-roles-privileges-accounts)
  - [Privilege catalogs and adding scopes](#privilege-catalogs-and-adding-scopes)
  - [Database diagram](#database-diagram)
  - [Schema versions](#schema-versions)
  - [Compare servers: parameters and accounts](#compare-servers-parameters-and-accounts)
  - [Parameter version catalogs](#parameter-version-catalogs)
  - [Engine words for the Query editor](#engine-words-for-the-query-editor)
  - [MySQL 5.7 support](#mysql-57-support)
  - [Export as SQL and Table structure](#export-as-sql-and-table-structure)
  - [Query digest sources and Time range](#query-digest-sources-and-time-range)
  - [Structure-only dumps](#structure-only-dumps)
  - [Saved connections and Database diagram Load](#saved-connections-and-database-diagram-load)
  - [Connection readiness and import](#connection-readiness-and-import)
  - [Learn guided lessons](#learn-guided-lessons)
  - [Command line deprecation](#command-line-deprecation)
  - [MySQL Shell](#mysql-shell)
  - [File transfer: SSH connections and SFTP](#file-transfer-ssh-connections-and-sftp)
  - [--quiet / -q](#--quiet---q)
  - [MCP server](#mcp-server)
  - [MySQL Fixtures](#mysql-fixtures)
  - [MariaDB Fixtures](#mariadb-fixtures)
- [Visual Explain (merged Diagram / Workbench)](#visual-explain-merged-diagram--workbench)
- [Investigation engine](#investigation-engine)
- [Import CSV](#import-csv)
- [Workspace navigation and configuration controls](#workspace-navigation-and-configuration-controls)
  - [Action feedback](#action-feedback)
  - [Tables and indexes: browse and export](#tables-and-indexes-browse-and-export)

## Commands Reference

### Local UI

The workspace and favicon use `docs/brand/myflames-app-logo.png`, derived from the primary flame SVG (`myflames-logo.svg`). The stable built URL remains `/assets/myflames-logo.png`. Desktop packaging uses the charcoal-slate icon in `docs/brand/`: `myflames-app-icon.icns` on macOS and the 1024 px PNG master on Windows.

The optional Electron development host is in `apps/desktop`. It starts its own
Python server and loads the one-use launch URL into a sandboxed window. It uses
separate app data and retains the same API authentication and write contracts.
The Apple Silicon package bundles its Python backend and opens through Finder.
Build with `package:mac` (unsigned, for local use) or `package:mac:release`
(`scripts/package-mac-release.sh`: Developer ID signing under the hardened runtime
with only `com.apple.security.cs.allow-jit` from `entitlements.mac.plist`,
notarization and stapling of the app and the DMG, Gatekeeper checks, and the
200 MiB update download limit (3.3.3 and older stop their Download at 150 MiB and open the release page instead); it also builds the update zip
(`electron-builder --mac dmg zip --arm64`; `mac.artifactName`
`myflames-${version}-macos-${arch}.${ext}`), extracts it and checks the app
inside (`codesign --verify --deep --strict`, Gatekeeper source Notarized
Developer ID, stapler validate), applies the 200 MiB limit to the DMG and the
zip, and writes `<name>.sha256` for both; the certificate comes from `MYFLAMES_MAC_IDENTITY`
and the notary sign-in from `APPLE_KEYCHAIN_PROFILE`, or from an App Store Connect
API key in `APPLE_API_KEY`, `APPLE_API_KEY_ID` and `APPLE_API_ISSUER`). Validate the delivered
`.app` using the packaged smoke, which removes system Python from the startup path
and checks owned cleanup.
Releases ship `myflames-<version>-macos-arm64.dmg` and
`myflames-<version>-macos-arm64.zip` from `package:mac:release`
(`apps/desktop/scripts/package-mac-release.sh`), with a `.sha256` for each;
attach the DMG, the zip and both `.sha256` files, uploading the DMG before the
zip because 3.3.3 and older pick the first matching `.zip` or `.dmg` in the
release's asset order for Download. The first release with in-place updates
must be installed by hand once. Packaging tests keep the desktop
`package.json` version equal to `myflames.__version__` and `pyproject.toml`.
Local builds (`package:mac`)
keep Developer ID signing and notarization explicitly disabled; the certificate
and the notary sign-in come from the environment, never the repository. Verify
the delivered app with a minimal PATH and an unavailable system Python; normal
Finder launches must not require a terminal. Keep the standard client-discovery
paths fixed; discovery order breaks ties after the per-server ranking below. Keep the renderer sandboxed
and isolated, with no Node integration or broad IPC bridge; the one bridge is
the updater's (`preload.cjs` exposes `window.myflamesDesktop.update` with
`support()`, `prepare()` and `restart()`, over the IPC channels
`myflames:update-support`, `myflames:update-prepare` and
`myflames:update-restart`, with no arguments; the main process answers only the
workspace origin's top frame and refuses anything else). Its session refuses
every permission request except the Fullscreen API for the workspace origin
(Learn's Full screen).
Quitting sends SIGINT to the backend's process group and waits 5 s; `backend.cjs`
then kills the dump, restore and clone tool groups the backend listed in its
`runtime/tool-groups-<launch id>.json` file, kills the backend's group and stops
any group in its last list, never signalling an id it already saw gone. The
file is opened without following links or blocking, and a missing, oversized or
malformed list counts as unconfirmed. `stop()` resolves only when all are gone
(7 s in total) and otherwise rejects, which shows the shutdown-incomplete dialog.
SSH tunnels (`myflames/ssh_tunnel.py`) are listed in the same file as service
groups (`ToolGroups.add/start(..., service=True)`): `ToolGroups.terminate(services=False)`
stops only the lifecycle tools, so a job's KILL QUERY can still go through its
tunnel, and `backend.cjs` kills the service groups with the tools when it must
kill the backend (it accepts at most `MAX_TOOL_GROUPS`, 16, listed groups,
the same number as `lifecycle.ToolGroups.MAX_GROUPS`, which refuses a 17th tool
or tunnel before spawning it; tunnels alone are capped at 8). A group that
holds only zombies counts as gone (`lifecycle._group_alive`: macOS answers
EPERM for it, and on Linux the group runs only while `/proc/PID/stat` shows a
member in a state other than Z or X, or a Z member whose num_threads is above
1: the state is the main thread's, which shows Z while other threads still
run and the process cannot be reaped), so dump, restore, clone and tunnel
cleanup never waits on one. When the desktop dies and the backend's own
shutdown hangs for 5 s, `backend-entry.py` SIGKILLs the groups the backend
owns before its own group (see [SSH tunnels](#ssh-tunnels)).
`backendEnvironment` sets `MYFLAMES_SSH_HOME` (the inherited real home) and, on
POSIX, `MYFLAMES_SSH_AUTH_SOCK` (the inherited agent socket); `main.cjs` sets
`MYFLAMES_SSH_HOME` from `os.homedir()`. The backend's own HOME stays isolated,
and database clients never see either variable.
Development and tests use separate app data, never the real CLI workspace.
Client discovery (`backendEnvironment` in `backend.cjs`) builds the backend PATH
without shell startup scripts, in this order. macOS: the inherited PATH (Finder's
default `/usr/bin:/bin:/usr/sbin:/sbin` when none), then `/opt/homebrew/bin`,
`/usr/local/bin`, `/usr/local/mysql/bin`, the Homebrew `opt` folders of
`mysql-client`, `mysql-shell` and `mariadb`, `/opt/local/bin`, keg-only versioned
formulae (`mysql-client@*`, `mysql@*`, `mariadb@*` under `/opt/homebrew/opt` then
`/usr/local/opt`, newest first), Laravel Herd's `~/Library/Application Support/Herd/bin`
and `/Applications/MySQLWorkbench.app/Contents/MacOS`. Windows: the inherited PATH,
then the installers' versioned folders under each Program Files folder, which the
installers do not add to PATH (`MySQL\MySQL Server *\bin`, `MariaDB *\bin`,
`MySQL\MySQL Shell *\bin`, then `MySQL\MySQL Workbench *` when it holds `mysql.exe`;
newest version first, `windowsClientDirectories`), compared without case.
**Bundled client.** The packaged app ships the official MySQL client in
`<resources>/mysql-client` (`Contents/Resources/mysql-client` in the `.app`,
`resources\mysql-client` beside `myflames.exe` on Windows): `bin/mysql` and
`bin/mysqldump` from the MySQL 8.4 LTS release pinned in
`apps/desktop/mysql-client.lock.json`, with exactly the libraries they load
(macOS: OpenSSL in `lib/`, found through `@loader_path/../lib`; Windows: the
OpenSSL DLLs and the Visual C++ runtime DLLs `msvcp140.dll`, `vcruntime140.dll`
and `vcruntime140_1.dll` beside the programs), the archive's `LICENSE` and
`README`, and a `manifest.json` that lists every file with its size, SHA-256 and
origin. `main.cjs` passes `process.resourcesPath` to `backendEnvironment` only when
`app.isPackaged` (development builds add nothing); `backendEnvironment` appends
`<resources>/mysql-client/bin` as the very last PATH entry on macOS and Windows,
after everything above (moving it there if the inherited PATH already holds it,
and leaving it off PATH when the folder name holds the PATH separator), and sets
`MYFLAMES_BUNDLED_CLIENT_DIR` to it. Discovery includes that folder alongside
installed tools; selection ranks each role against the server's engine and
major.minor series, so the bundled copy can beat an older or newer installed
series. Installed tools win only after series, authentication plugins and patch
version tie. The bundled `mysqldump` is offered for MySQL servers only. The
bundled client cannot sign in to MariaDB accounts that use `ed25519` or `parsec`
(`ERROR 2059: Authentication plugin 'client_ed25519' cannot be loaded`); those need
an installed MariaDB client. It carries no client plugins (`lib/plugin`), so
Kerberos, LDAP SASL, OCI and WebAuthn authentication also need an installed client.
`apps/desktop/scripts/fetch-mysql-client.py` (stdlib) downloads the archive into
`apps/desktop/build/mysql-client-cache` (or `MYFLAMES_MYSQL_CLIENT_CACHE`), refuses
any size or SHA-256 mismatch with the lock (HTTPS only, the download capped at
the pinned size; the cached file is opened once, without following links or
blocking, owned by the user and not writable by others, and the handle that was
hashed is the one extracted), reads the
dependencies from the Mach-O load commands or the PE import and delay-import
tables and refuses anything outside the archive, the operating system and, on
Windows, the Visual C++ runtime folder (DLL names must be plain file names;
output paths never come from the archive and must stay inside the output
folder; the runtime is found with
`vswhere` in `VC\Redist\MSVC\<version>\x64\Microsoft.VC*.CRT`, or `--vc-runtime`),
and writes `apps/desktop/build/mysql-client`. `package:mac`,
`package-mac-release.sh` and the Windows workflow (archive cached by its SHA-256)
run it before electron-builder; electron-builder copies the folder to
`mysql-client` and its `LICENSE` to `licenses/MySQL-Client-LICENSE`. The release
build re-signs the client and its libraries with the app's Developer ID under
the hardened runtime and checks that `mysql --version` and `mysqldump --version`
still run; the packaged smoke runs the bundled `mysql --version` with only the
system folders on PATH and checks that the backend found a usable `mysql`
(`/api/lifecycle/tools`). Moving to another MySQL release repeats the checks
recorded in the lock's `comment` (Oracle's MD5 file and `.asc` signature, then
the SHA-256) and updates NOTICE. Tests: `apps/desktop/test` (PATH order,
extraResources, lock) and `test/test_fetch_mysql_client.py`,
`test_connector.TestBundledClientResolution` and
`test_connector.TestVersionAwareClients`.
Packaged app data is separate from development and CLI data.
**Client tool discovery and selection.** `myflames/client_tools.py` is shared
by query connections and Maintenance. `snapshot` only stats executable regular
files: every copy of `mysql`, `mariadb`, `mysqldump`, `mariadb-dump`,
`mariadb-backup`, `mariadb-upgrade`, `mysqlpump` and `mysqlsh` in nonempty PATH
entries, then `lifecycle.COMMON_BIN_DIRS` on POSIX, then the bundled folder.
Aliases are deduplicated by realpath, with MariaDB names preferred for
compatibility links; there is no implicit current-directory or PATHEXT lookup.
Installations are keyed by `<engine>:<realpath folder>`; `folder` is resolved,
`found_in` retains the discovery folder. Each program keeps its path, realpath
and distribution version. Origin codes and labels are `bundled` (Bundled with
myflames), `homebrew` (Homebrew <formula>), `macports` (MacPorts),
`mysql-installer` (MySQL installer), `workbench` (MySQL Workbench),
`mariadb-installer` (MariaDB), `system` (System package, Linux /usr/bin and
/usr/sbin), `path` (On PATH), and `folder` (Chosen folder for otherwise
unclassified Settings folders outside the search). `has_auth_plugins` means a
recognized plugin file exists in `../lib/plugin`, `../lib/mysql/plugin` or
`../lib64/mysql/plugin`: LDAP SASL, Kerberos, OCI, WebAuthn, ed25519, GSSAPI or
parsec. It is a selection hint, not proof that an account can authenticate.

`discover` requests at most 32 version probes per call, including cache hits;
`discover_choices` and lifecycle discovery can make extra discovery calls for
chosen folders. `probe` runs `--version` with an isolated HOME and allowlisted
environment, no stdin and an 8 s timeout; banners retain at most 4,096 characters.
Successful recognized versions use a 64-entry LRU keyed by program name,
realpath, device, inode, size, mtime_ns and ctime_ns, without a time limit.
Failures and timeouts are not retained, and a file changed during a probe is
not cached. Concurrent probes of one identity share a flight, including its
failure; child processes run outside the cache lock. `refresh=True` bypasses
cached results. Workspace startup warms discovery on a daemon thread without
blocking the window. `mysqlpump` is presence-only, with null version and
version tuple; it is deprecated, never probed, selected or marked used. Shell
copies are listed separately, newest first.

`select` is pure. For each supported role it considers the server's own engine,
using that program's version even in a mixed-version folder. Automatic ranks
its own major.minor series first, then the nearest newer series, then the newest
older series. Within the selected series, installations with authentication
plugins win, then the newest patch, then installed before bundled, then discovery
order and stable keys/paths. An older automatic dump or load program carries
`<program> <version> is older than this server (<engine> <series>).`; dumps add
`Install <engine> <series> client tools for a complete dump.` A Settings choice
wins for that engine's roles, and missing programs never come from another
installation to repair that choice. Chosen tools use reason `chosen`, without
an automatic older-version warning.

Roles are `client` (queries), `dump`, `load` (SQL restores and CLONE), `backup`
(MariaDB physical backup) and `shell`. MariaDB prefers `mariadb` and
`mariadb-dump`, accepting its own `mysql` and `mysqldump` compatibility names.
Load uses the client in the selected dump installation; if it has no client,
load is missing. Without a selected dump, an own-engine query client can supply
load. MySQL Shell uses the newest discovered Shell independently of the Settings
choice and is selected only for a known MySQL server. Auxiliary programs such
as `mariadb-upgrade` use the same series ranking through `select_program`.
An unknown or unrecognized server gets only a query client: installed before
bundled, then mysql before mariadb, then discovery order. If a known server has
no tools of its own engine, only queries may use the other engine; dump, load
and backup stay missing. That `other_engine` query client prefers authentication
plugins, then the newest client version, then installed before bundled, then
discovery order and installation key. The `unknown_server` first-contact order
stays unchanged. For MariaDB this adds a note that dumps and restores need
MariaDB tools. Reason codes are `chosen`, `series`, `newer`, `older`,
`unknown_server`, `other_engine` and `newest` (Shell).

**Choices and trust.** **Settings > Client tools…** stores independent
`client_choice_mysql` and `client_choice_mariadb` settings. The value is the
absolute client path or folder as chosen (preserving stable symlink spelling),
`bundled`, or no key for **Automatic (best match for each server)**. Validation
requires an absolute path of at most 4,096 characters without NUL or line
breaks, a client named mysql or mariadb (with .exe on Windows), a regular
executable file, and a recognized mysql/mariadb banner of the requested engine.
The chooser runs `--no-defaults --version` in `client_environment()` with a
5 s deadline and 16 KiB per stream, in a process group killed with its children
at the deadline. A rejected choice leaves the stored and active choices intact.
A stored choice that moved, stopped working or became untrusted remains stored
with its error; resolution raises `ClientMissingError` rather than silently
falling back. At startup `migrate_client_choices` moves `client_binary` once:
a successful trusted probe decides the engine, otherwise a mariadb filename
means MariaDB and any other name means MySQL; broken paths are preserved.
Bundled paths become `bundled`, existing per-engine choices win, and the legacy
key is deleted. Choices are published with `connector.set_client_choices` at
startup and after changes; existing connection objects follow the generation
on their next resolution.

Every discovered or chosen non-bundled copy must pass
`connector.checked_program` before probing or receiving credentials. On POSIX,
the file and every ancestor of both the chosen spelling and its realpath must
be owned by the user or root, with no group/other write; symlink modes do not
control replacement. Exceptions are root-owned sticky directories and, only on
macOS, group write by the admin group (gid 80) without other write. Windows
accepts only local mysql.exe, mariadb.exe, mysqldump.exe, mariadb-dump.exe,
mariadb-backup.exe or mysqlsh.exe under ProgramFiles, ProgramFiles(x86),
ProgramW6432, LOCALAPPDATA or the user profile (MYFLAMES_USER_PROFILE or
USERPROFILE); UNC and device paths are refused before resolution. This is a
location restriction, not a Windows ACL check. Only the actual bundled folder
is exempt, never an arbitrary extra search folder. Refused copies remain in
`skipped` with their reason. Each use rechecks the path and pinned resolved
target, refuses a changed target, and executes the resolved file. Lifecycle
rechecks immediately before launching; editor helpers recheck the tab's pinned
client. The single-client shortcut also checks trust. An explicit connector
`binary=` override bypasses automatic selection and its trust checks; it is
not a workspace setting or accepted connection field.

**Connector resolution and first contact.** Resolution is a per-connection
`binary=` override, then the Settings choice for the known engine, otherwise
automatic selection for that server. With no choices and at most one stat
candidate, resolution uses the checked candidate without an inventory probe
or preliminary server read; option-file construction still probes its engine.
With multiple distinct clients and no cached facts, `_first_contact` runs
`SELECT @@version, @@version_comment` once per endpoint flight using that
connection's credentials, socket/TCP, TLS and SSH tunnel. A MySQL choice is used
first, then a MariaDB choice; without choices it uses the unknown-server query
fallback. Unrelated automatic clients are not probed while a chosen
installation supplies the connection. If the read discovers an engine with no
choice, full discovery then supplies that engine's automatic pick.

Concurrent callers share first-contact success or failure. Only a missing
authentication plugin permits one retry with the other engine's best client;
login and transport failures propagate without a second connection. A server
SQL error with `at line N` and a non-2xxx error number leaves the version
unknown; so do unparseable or unsupported versions such as ProxySQL 2.x.
Unknown and failed reads are not cached and a later request may retry.
`client_tools` keeps valid `{engine, version}` facts in a thread-safe 256-entry
LRU without a time limit. The key is host, port, socket, ssh_host, ssh_user and
ssh_port (defaults localhost/3306/22); localhost and 127.0.0.1 are distinct,
and database user, password and TLS are not part of the key. Connector version
reads, editor handshakes and the client-tools API also remember valid facts.

`ClientMissingError` (`ConnectorError`, code `client-missing`) reports no usable
client or a broken choice. `ClientAuthPluginError` subclasses it with code
`client-auth-plugin`. Only the first ERROR line of a client connection failure
matching `ERROR 2059 (HY000): Authentication plugin '<name>' cannot be loaded`
(end of line, colon or period after `loaded`, plugin name letters/digits/underscores)
gets that type. Statement errors (`at line N`) and subsequent ERROR lines do
not. Its text names the program, version, origin and plugin, and directs the
user to Settings. `client_missing_error` follows causes and only myflames'
own ConnectorError/ValueError wrapper contexts; unexpected exception contexts
still produce generic 500 errors. Typed client failures answer HTTP 400 with
`error` and the error's `code`. `ClientMissingNotice` offers **Choose client
tools…** and **Try again** in Server views, Test connection and the Query editor;
plugin failures show their specific text. Maintenance can retain its inventory
and download links when the server cannot be read.

Option files follow the probed client engine, including a mysql compatibility
link to MariaDB, and remain paired with that invocation's binary.
`client_tls_options` serves reads, writes, editor/CSV sessions and lifecycle
programs. MySQL gets `ssl-mode` and, for connector sessions unless TLS is
DISABLED, `get-server-public-key=ON`. MariaDB maps DISABLED to `skip-ssl`,
REQUIRED to `ssl`, VERIFY_CA and VERIFY_IDENTITY to `ssl` plus
`ssl-verify-server-cert`, and PREFERRED to no mode option. It receives neither
`ssl-mode` nor `get-server-public-key`. Both keep CA, certificate, key and
cipher options. This mapping uses the client engine, not the server engine.

**Client tools API.** Authenticated `POST /api/client/tools` accepts `{}` for
inventory only, optional `refresh: true` to re-probe, and optional `connection`
to read that server and select roles. `POST /api/client/tools/choose` requires
`engine` (mysql or mariadb), `path` (absolute program/folder, `bundled`, or null
for Automatic), and optionally `connection`; unexpected fields are refused.
Only choose mutates settings. Missing API authentication answers 403; invalid
fields, paths or engine choices answer 400 with a plain `{error}` and no code,
leaving the active choice intact. Both return `client-tools-1.0`:

- `installations[]`: `key`, `engine`, `version`, `series`, `folder`, `found_in`,
  `origin`, `origin_label`, `bundled`, `has_auth_plugins`, `programs` keyed by
  name with `{path, realpath, version}` (no mysqlpump), and `chosen_for[]`.
  Order is engine, descending version, then discovery order/key.
- `shells[]`: `{path, realpath, version, origin, origin_label}`, newest first;
  `skipped[]`: `{path, reason}`; `choice`: mysql and mariadb objects with
  `{path, installation, error}` (null where absent).
- With a connection only: `server` `{engine, version, product}` or null,
  `server_error` `{code, error}` or null, and `used` with nullable `client`,
  `dump`, `load`, `backup`, `shell`, plus `missing[]` `{role, program, text}`
  and `notes[]`. Each use has `role`, `program`, `path`, `version`, `engine`,
  `installation` (null for Shell), `origin`, `origin_label`, `bundled`,
  `reason`, `warning`; internal realpath/version_tuple/order are omitted.
  A server-read error uses `client-missing`, `client-auth-plugin` or
  `server-read`; typed client failures clear `used.client`.

`/api/client/settings`, `/api/client/detect` and `client-settings-1.0` are
removed; the old endpoints return 404. The dialog shows the programs for the
connection in use, separate engine choices and **Found on this computer** with
**Detect again**. Its install hint appears only when no installation was found.
`toolUseLine` renders program, version, origin and selection reason in
Maintenance and the review sheet; Maintenance heads this block **Used for this
server**. The Dump tool choice names the program, version and origin (or the
Shell version for Automatic).

The deprecated CLI and `myflames-mcp` use automatic selection only, without the
workspace's stored choices; `--mysql-binary` still overrides query connections.
Each new command process with several clients pays its own first-contact read
because endpoint facts are in memory. No new CLI flag or MCP tool is added.

Contract tests: `test/test_client_tools.py` (`DiscoveryTests`: inventory,
origins, plugins, trust, probe identity/LRU/single flight/limits;
`SelectionTests`: ranking, roles, reasons, missing choices; `EndpointTests`:
endpoint normalization and facts LRU). `test/test_connector.py`
(`TestVersionAwareClients`: resolution and first contact; `TestClientSelection`
and `TestWindowsClientTrust`: validation/trust; `TestClientSecurityFixes`:
trust, exact 2059 matching and MariaDB TLS; `TestBundledClientResolution`:
bundle paths). `test/test_workspace_store.py` (`TestWorkspaceStore`: independent
choices and legacy migration); `test/test_ui.py` (`TestClientToolsAPI`:
authentication, validation, wire fields, removed routes and typed errors).
`test/test_editor_session.py` (`ProcessTests`, `PinnedClientTrustTests`): pinned
helpers and permission changes. UI: `apps/workspace/test/client-tools.mjs`
(`npm run test:client-tools`), replacing `database-client.mjs`.

**UI preferences.** `POST /api/settings/ui` is in `_SETTINGS_PATHS`, requires the usual local session and CSRF token, and never opens a database server connection or writes an audit record. `{}` returns `ui-preferences-1.0`, with `preferences` and `defaults`. `{"set": {...}}` validates every change before writing `ui.<key>` JSON rows in one transaction; unknown keys, invalid values, unknown outer fields and a non-dict `set` (including null) return 400. A read never creates the workspace database. `workspace_store.UI_PREFERENCE_DEFAULTS` defines `theme` (`system`, or exactly one of the single literal `UI_THEME_IDS` list matching TypeScript `THEME_IDS`), `sidebar_width` (integer 208–320, default 208), `server_nav_collapsed` (boolean, default true), and `health_auto_production` / `health_auto_development` (each an integer -1, 0, 10, 30 or 60, default -1; -1 follows the sampling window and 0 disables refresh). Booleans and floats are refused for integer preferences. Unknown or malformed stored rows fall back to defaults. The index page omits its theme attribute if preference storage cannot be read and still returns 200. `src/uiPreferences.ts` shares a module value and listeners, displays changes immediately, serializes writes, ignores stale answers and re-reads after the latest write fails. Startup loads preferences before mounting the sidebar. Tests: `test/test_workspace_store.py`, `test/test_ui.py`, `test/ui-preferences-unit.mjs` and `test/shell-layout.mjs`.

**Query editor settings and variable names.** `POST /api/settings/editor`
(ui.py `_SETTINGS_PATHS`; no connection, no server read, no audit) reads and
writes the Query editor's settings in the workspace store's `settings` table
(rows `editor.<key>` = `on`/`off`). `{}` returns `editor-settings-1.0` with
`settings` (the defaults merged with the stored rows; unknown keys and
malformed values fall back to the default) and `defaults`;
`{"set": {key: bool}}` writes the given keys in one transaction and returns
the same shape. The keys are `multi_cursor`, `completion_auto`,
`close_brackets`, `soft_wrap`, `problem_warnings` and `hover_info`
(`EDITOR_SETTING_DEFAULTS`; `soft_wrap` defaults off, the rest on). A read
never creates the database. An omitted or null `set` reads without writing.
A payload with any field besides `set`, a non-null `set` that is not a dict,
an unknown key or a non-boolean value is a 400 through
the shared `request_error_text` ("Unexpected editor settings field.",
"Expected a dict of editor settings.", "Unknown editor setting ...",
"The editor setting ... must be true or false."), as is a call without a
running workspace. `POST /api/config/versions/names` (`_PARAM_VERSION_PATHS`)
answers offline from the generated parameter catalogs: `{engine, version}`
returns `param-names-1.0` with `engine`, `requested`, `release` (null when
the series is not cataloged or the version predates its first release),
`exact` (true when the numeric version extracted from the request is
cataloged, including a suffixed build; false when an uncataloged patch
resolves to the nearest earlier cataloged release of the same series) and
`names` (`name`, `scope` `global`/`session`/`both`, `type`,
`dynamic`, `deprecated`; `type` and `dynamic` are null where the catalogs do
not record them, never a guessed false). The Query editor's completion reads
it for `@@` system-variable suggestions and caches the answer per engine and
version in the page. A bad engine or version is a 400 ("Choose engine mysql
or mariadb.", "Choose a cataloged version of at most 32 characters.").
Tests: `test/test_ui.py` (editor-settings round trip, bad payloads, local
routing) and `apps/workspace/test/editor-settings.mjs`.
`QueryEditorSettings.tsx` tracks saves and errors per setting, with a request
sequence per key that ignores stale completions. Each row keeps an empty
`role="alert"` node mounted until an error fills it.

`package:win` builds the unsigned Windows x64 NSIS installer
(`myflames-<version>-windows-x64-setup.exe`, per user). PyInstaller does not
cross-compile, so `.github/workflows/windows-desktop.yml` builds the backend on a
Windows runner, packages the installer, runs the packaged smoke on
`dist/win-unpacked` and uploads the installer with its SHA-256; releases attach
that artifact (`myflames-<version>-windows-x64-setup.exe` and its `.sha256`,
from the workflow run of the release commit) as an unsigned preview until
Windows crash supervision and signing land. Windows quits with
`taskkill /PID backend /T /F` and is unsigned. Crash supervision is partial
there: an SSH tunnel's ssh.exe ends with the backend however the backend ends
(its kill-on-close job object; Windows tunnels are not listed in
`tool-groups-1.0`), while dump, restore and clone tools still outlive a backend
that crashed or was killed.
See [Desktop app](DESKTOP.md) (`docs/DESKTOP.md`) and the desktop package README
for launch and test instructions and for the scope and distribution work still
required. Current delivery targets macOS. The shared working UI now uses
compact neutral navigation, open-query links, a command palette and full-width
Server details.

Update checks run only when the workspace asks: **Check for updates** in
the window status bar on every screen and in About myflames (`UpdateCheck.tsx`, shared state in
`updateStatus.ts`) posts `/api/app/update/check` with `force: true`. The
`app-update-1.0` answer carries `checked_at` (UTC, when the release metadata was
read; a cached answer keeps its time), and a failed check carries `error` plus a
short `reason` (GitHub could not be reached, did not answer in 5 seconds, took
longer than 10 seconds to answer, limited requests, answered with a status,
sent the check to an unexpected address, unreadable release information, or "a
secure connection to GitHub could not be verified" when certificate
verification failed, which is not reported as offline);
a failed check is never cached and never reported as up to date. From the
workspace, the check also stores `last_check` (`checked_at`, `outcome`
`up_to_date` | `available` | `failed`, `current_version`, `latest_version`,
`reason`; never release notes or URLs) in the workspace store's `settings`
table (`app_update_last_check`). `/api/app/update/status` returns
`app-update-status-1.0` with that record and never reads the network; a record
made by another myflames version is dropped. The footer shows "Checked today at
10:42", "Checked yesterday" or "Checked Oct 18" in local time with the full
timestamp in the tooltip ("Tried …" for a failure), "Not checked yet" before
the first check, "Last check unknown" when the stored record could not be read
(retried when Home or About mounts again), and a result older than 24 hours, or
one whose time cannot be read, as "Last result: …".
**Check for updates automatically** in Settings (on by default, stored in the
workspace store's `settings` table under `app_update_auto` as `'on'`/`'off'`;
`POST /api/app/update/settings` with an optional `{"auto_check": bool}` returns
`app-update-settings-1.0` `{"auto_check"}`) lets the workspace check 10 seconds
after it opens when there is no stored check, the last one is older than 24
hours, or the last one failed more than an hour ago, re-testing that hourly; an
automatic check uses the same `/api/app/update/check` as the button, and a
failed automatic check shows no badge (the window status bar shows the reason as
before). When a current check found a newer version, a round badge appears
beside the flame logo and the myflames name at the top of the sidebar
(`UpdateBadge.tsx`): available (a download arrow), the download's progress (a
ring with the percent in its tooltip), preparing (a spinner) and ready (a green
restart arrow); on the 48 px icon rail (651–900 px windows) it is a dot on the
logo's corner, and a result older than 24 hours shows no badge. While an update
is pending, the Settings page's Updates and news section also lists **Update
available…**, **Update progress…** or **Restart to update…**; clicking the
badge or the menu item
opens the Software update dialog.
**Download…** appears for a current newer release (unavailable while a check
runs) and opens the update dialog with the result captured at the click;
after a reload it checks again first, because the download needs the asset the
check found. Download update saves the DMG or EXE in Downloads and never
installs it; it prefers the `.dmg` on macOS (then `.zip`, `.tar.gz`) and the
`.exe` on Windows regardless of the order GitHub lists assets, and checks the
download against the release's `<file>.sha256` when the release has one, a
mismatch falling back to the release page like any failed download.

Application updates (`myflames/app_update.py`, `/api/app/update/check` and
`/api/app/update/download`) build both openers as `tool_updates._fetch` does
(`app_update._opener`): `ProxyHandler({})`, so no proxy from the environment
or the macOS settings is used, one HTTPS handler with the `tls.https_context()`
context, and a redirect handler that checks every target before following it
and closes the redirect response without reading its body (urllib's own
handler reads it whole, with no size limit; urllib's limits of four visits to
one address and ten redirects still apply).
The check follows redirects only to HTTPS `api.github.com` at
`/repos/<owner>/<name>/releases/latest` or `/repositories/<id>/releases/latest`
(how GitHub moves a renamed or transferred repository) and checks the final
URL too; it reads at most 1 MiB (a longer answer is unreadable), and each
socket operation times out after 5 s. The check answers within
`CHECK_DEADLINE` (10 s): `app_update._read_release` runs the read in a worker
thread and waits for it at most that long, and `tls.Watchdog` shuts the
worker's TCP and TLS sockets at the same deadline. What comes before a socket
exists is not cut (building the TLS context, which may read the macOS roots
with `/usr/bin/security`, the DNS lookup, and the TCP connect, 5 s per
address), so a read still running at the deadline ends in the background. A
check made while a read runs waits for that read and shares its answer; once
the deadline has passed it reports the deadline at once, and no second read
starts until the first ends. The check selects an asset only when its
operating system and architecture match; macOS also accepts universal builds. An unmatched release
has no selected asset. The first download URL must be an HTTPS
`github.com/vgrippa/myflames/releases/download/<tag>/<name>` URL; when an
update check has found an asset (the cached `check_for_updates` result), only
that `asset_url` is accepted and any other client URL is refused, never
substituted. `objects.githubusercontent.com` and
`release-assets.githubusercontent.com` serve every repository's release files,
so they are accepted only as a redirect target or as the final response URL.
Every redirect is checked before following it, and the final response URL is
checked before reading the body. Other URLs raise `ValueError`; release-page
fallbacks also require the project's official HTTPS release page. Downloads use
an exclusively created private temporary file in the destination directory and
enforce the 200 MiB limit (`MAX_UPDATE_BYTES`) before publishing. http.client returns an empty read
at an early EOF even when Content-Length promised more (TLS ragged EOFs are
suppressed too), so after the read loop a remaining declared length, a byte
count different from Content-Length, or one different from the checked
`asset_size` raises "The download ended early." (or a size mismatch) and the
temporary file is removed unpublished. Publication uses an atomic hard link
to an unused name, adding ` (1)`, ` (2)`, etc. before the extension on collisions;
files and symlinks already present are preserved. The temporary file is removed
after success or failure. If the filesystem cannot publish a hard link, the
download opens the validated release page (or the official latest-release page)
in the browser, never the binary asset URL, and returns that page as `html_url`.
The TLS, headers and body read have a 300-second `tls.Watchdog` deadline, checked even after
an EOF so an interrupted response cannot be published. DNS, TCP connection
establishment and TLS-context construction precede socket registration and
cannot be interrupted by this watchdog. The destination is `MYFLAMES_DOWNLOADS_DIR` when it names an
absolute existing directory (the desktop app sets it to the user's Downloads
folder, because its backend runs with an isolated HOME), else `~/Downloads`,
else the temporary directory.

The Software update dialog's **Update** button stages the release's update file
(`POST /api/app/update/stage`, `app-update-stage-1.0`): the backend first reads
the release's `<file>.sha256` (at most 1 KiB, one `<64 hex>  <name>` or
`<64 hex> *<name>` line naming exactly that file), then downloads
`myflames-<version>-macos-arm64.zip` on macOS or
`myflames-<version>-windows-x64-setup.exe` on Windows with the same HTTPS,
redirect, host and 200 MiB rules as Download, within 900 seconds
(`STAGE_DEADLINE`), checks size and SHA-256, publishes the file atomically into
the desktop app's private updates folder (`MYFLAMES_UPDATES_DIR`, which
`main.cjs` sets to the updates folder inside the desktop data folder and makes
mode 0700), removes the updater's own older files there (`staged-update.json`,
release file names and `.myflames-stage-*.part`; anything else stays) and writes `staged-update.json`
(`staged-update-1.0`: `version`, `platform`, `file`, `size`, `sha256`,
`staged_at`) atomically. Staging answers at once and runs in a background
thread, one at a time; its state is `running` | `done` | `failed`, with
`version`, `received`, `total` and `reason`. The update file must be named
`myflames-<tag version>-(macos|windows)-(arm64|x64|universal)[-setup].(zip|exe)`
and match the platform (zip on macOS, `-setup.exe` on Windows), and its URL must
be this release's own `/releases/download/<tag>/<name>`. The updates folder must
be a plain folder, not a link, and on macOS and Linux owned by the user with no
access for others (`app_update._updates_directory` and `updater.checkFolder`
check the same rule); otherwise staging, the status record and startup cleanup
refuse it and touch nothing.
`/api/app/update/status` (still never the network) adds `auto_check`,
`staging` (that progress), `staged` (the verified record, or null) and
`active_operations` (dump, restore and clone jobs queued or running). At
startup, after the single-instance lock, the desktop app removes a staged
update that is not newer than itself (installed) or whose record cannot be
read. In development only (never a frozen backend, and the desktop's environment
allowlist drops both variables), when `MYFLAMES_TEST_UPDATE` and
`MYFLAMES_TEST_UPDATE_FILE` (an absolute path to a regular file) are set,
staging copies that file instead of downloading.
The page then calls `window.myflamesDesktop.update.prepare()`.
`updater.cjs` reads `staged-update.json` from the fixed folder only (no links
followed, at most 4096 bytes, the file name pattern above, this platform, a
version newer than the app's) and hashes the file again. On macOS it serves the
zip once on 127.0.0.1 (a random port, an unguessable 64-hex path, the JSON feed
and the zip, closed after the update is staged or after 2 minutes) to
Electron's `autoUpdater` (Squirrel.Mac, `serverType` `json`), which installs it
only when its code signature satisfies the installed app's designated
requirement (the same Developer ID team and bundle identifier), and
`ElectronSquirrelPreventDowngrades` in the app's Info.plist refuses a lower
version even from that team; a zip signed by anyone else is refused with "The update is not signed by the developer of this
copy of myflames, so it was not installed." macOS updates in place only when
the app is in an Applications folder (`app.isInApplicationsFolder()`) and its
bundle and that folder are writable; otherwise the dialog names the reason (for
example "Move myflames to the Applications folder to update it in place.") and
offers **Download update** as before. The loopback feed answers only exact
request lines with `Host: 127.0.0.1:<port>` (anything else gets 400, and no
request can throw in the main process). **Restart to update** on macOS stops the
backend and its dump, restore, clone and tunnel processes first (a second Quit
meanwhile is held, as during any quit), then `autoUpdater.quitAndInstall()`
replaces the app and opens the new version; **Later** installs the update when
you next quit myflames, without reopening. Squirrel.Mac also applies a staged
update at that quit when the backend could not be confirmed stopped.
On Windows, when the app quits after the backend stopped (never when the stop
failed, and never when `MYFLAMES_DESKTOP_DATA` chose the data folder, whose
owner cannot be checked there), it runs the installer from the updates folder,
in that folder, detached, without a shell: `<setup>.exe --updated /S`
(silent, keeps the app's data), plus `--force-run` after **Restart to update**
so the new version opens. The Windows installer is unsigned: the update is
trusted through HTTPS from GitHub's release hosts and the SHA-256 published in
the same release, and an Authenticode publisher check is to be added when
Windows signing lands. When a dump, restore or clone is running, **Restart to
update** first says "A dump is running. Restarting stops it before it
finishes." and offers **Restart anyway**; other work in progress (an open
editor transaction, an import, a file transfer, a skip loop) stops as it does
on any quit. Verified on 2026-11-09 with Developer ID signed (not notarized)
test builds 3.3.90 and 3.3.91 under a separate bundle identifier: **Restart to
update** installed 3.3.91 and reopened it within about 5 seconds with only the
new backend running, **Later** followed by a normal quit installed it without
reopening, and an ad hoc signed 3.3.91 zip with a correct checksum was refused
by Squirrel.Mac and the app stayed 3.3.90. On the Windows lab PC the CI
installer, staged as a newer version, was installed silently after
**Restart to update**, which replaced the install folder and reopened the app
13 seconds later. After the security fixes, signed builds 3.3.95 and 3.3.96 repeated the
macOS update through the Host-checked feed, and an older signed build staged
as 3.3.99 was refused by `ElectronSquirrelPreventDowngrades` ("The update is
older than this copy of myflames, so it was not installed.").

Every HTTPS read (the release check and download, `tool_updates._fetch`, and
Home news) takes its context from `myflames/tls.py` `https_context()`: the
standard library's default context, with certificate and host name checks on.
The packaged macOS app's bundled OpenSSL looks for certificate authorities only
at paths on the build machine (Homebrew's `/opt/homebrew/etc/openssl@3/cert.pem`),
so on a Mac without them the default context trusts nothing. When it holds no
CA certificate on macOS, `https_context()` also loads the roots macOS trusts,
read with `/usr/bin/security` (10 s limit per call, `LC_ALL=C`): the
certificates `find-certificate -a -p` exports from
`/System/Library/Keychains/SystemRootCertificates.keychain` and
`/Library/Keychains/System.keychain`, kept only when their SHA-1 is listed by
`trust-settings-export` (system `-s`, admin `-d` or user domain) as a trusted
root with no policy, application or host restriction, and no domain denies it
for any policy. The exports also hold intermediates (Developer ID, WWDR) and
System Identity certificates, which have no trust settings and are left out.
When any trust domain cannot be read (the export fails, times out, writes no
file or an unreadable plist), nothing is added, so a "Never Trust" choice is
never dropped. An admin or user domain without settings counts as empty only
in the two forms macOS reports it: an export that succeeds with an empty
`trustList` (macOS 27, checked on 27.0.1) or "No Trust Settings were found" on
stderr (older macOS). A complete read is kept for the process; a failed or
partial one (a keychain export that failed) is read again after
`tls.RETRY_AFTER` (60 s), never cached for the process. When `SSL_CERT_FILE`
or `SSL_CERT_DIR` is set, even empty, the user chose the trust store: myflames
adds no macOS roots (OpenSSL reads an `SSL_CERT_DIR` only on lookup, so its
context counts no CA up front). `tls.Watchdog`, `tls.watched_connection_class`
and `tls.WatchedHTTPSHandler` give Home news and the release check one deadline
per read for everything after the TCP connect (the TLS handshake, headers and
body). The DNS lookup, the connect and building the context come before the
watchdog holds a socket, so each caller also bounds its own wait (the release
check's worker, the news refresh's join). Tests: `test/test_tls.py` (with
`test/fixtures/tls/test-root-ca.pem`, a throwaway CA whose key was discarded).

Home news (`myflames/news.py`, `/api/news` and `/api/news/settings`, `news-1.0`)
reads the two feeds in `news.SOURCES` and nothing else: Planet MySQL
(`https://planet.mysql.com/rss20.xml`, keeping only links under
`https://blogs.oracle.com/mysql/`, the MySQL team's posts) and the MariaDB
Foundation (`https://mariadb.org/feed/`, links on `mariadb.org`). The Oracle
MySQL blog feed itself (`blogs.oracle.com/mysql/rss`) answered 403 to every
client tried on 2026-10-20, and `mariadb.com/feed/` refuses a User-Agent that
starts with Python-urllib, so neither is read. `fetch_feed` sends
`User-Agent: Python-urllib/<python> myflames/<version>` (Planet MySQL's CDN
refuses a first product it does not know), `Accept-Encoding: identity`, no
cookies and no identifiers, through an opener with only an HTTPS handler and
its own redirect handler: at most three redirects, each to an HTTPS feed host
without credentials or a port, within the feed's deadline, closing every
redirect response without reading its body. It reads at most 1 MiB, each
socket operation times out after 5 s, and the feed stops at its 8 s
deadline: a watchdog (`tls.Watchdog`) shuts the feed's sockets down at that
deadline, the TCP socket and the TLS socket wrapped around it (registered
before its handshake, since wrapping detaches the TCP socket, and shut on its
descriptor without touching the TLS state the reading thread uses), so the TLS
handshake, headers or chunk framing cannot outlast it either. The DNS lookup
and the TCP connect (5 s per address) come before the watchdog holds a
socket and can; the wait of `/api/news` (below) bounds them. Its tests and the
release check's use the local servers in `test/_tls_servers.py`: one that
never answers a TLS handshake, and one that sends an optional head at once,
then the rest of its answer a byte at a time (so the release check's test
stalls inside one body read as well as in the headers). `parse_feed` refuses NUL bytes and declared encodings other than
UTF-8, ASCII, ISO-8859-1 and windows-1252 (UTF-16 or EBCDIC would hide a
declaration from a byte check), refuses `<!DOCTYPE` or `<!ENTITY` anywhere
before `xml.etree.ElementTree.XMLPullParser` sees the document, and keeps the
RSS 2.0, RSS 1.0 or Atom entries (at most 40) that end before a cut at 1 MiB or
a malformed tail. Titles (200 characters) and summaries (240) are stripped of
HTML with `html.parser` and of control and bidirectional characters; RFC 822
and ISO 8601 dates become UTC (an invalid offset or a date more than a day
ahead is dropped); an item stays only when its link is HTTPS on the source's
official site. `news-cache.json` (mode 0600, written atomically, read without
following links and checked again item by item) sits next to the workspace
store; when the write fails the process keeps the read in memory. A source is
read again six hours after a successful read, ten minutes after a failed one,
or on Refresh when its last read is a minute old. One shared background read
per cache (re-checked under the lock, so a read that just finished is not
repeated) starts at most one worker per source, and never a second one while
an earlier worker is still stuck (a DNS lookup or a TCP connect is not cut
by the deadline);
`/api/news` waits for it at most 10 s and then answers from the cache ("News
is taking longer than usual."). A failed source keeps its cached headlines:
`stale` is true, `error` names it ("Planet MySQL could not be read.") or, when
every source failed without an answer, says "News is unavailable offline.",
and `fetched_at` is the oldest read among the sources shown. Home takes up to
six headlines, from each source in turn, newest first, without repeated links
or titles. The Settings switch (`settings` key `home_news`,
`WorkspaceStore.news_enabled`, default on) is read through
`/api/news/settings`; while it is off `/api/news` returns `enabled: false`
without reading, and the page sends no `/api/news` request at all. In the
page, `newsApi.ts` shares the switch between `HomeNews` (under Recent work;
`HomeNews.tsx`, `home.css`) and the Settings page's Updates and news section
(`NewsSetting`, busy
while a change is saved: `aria-disabled` through `Checkbox busy`, never
`disabled`, so it keeps keyboard focus and ignores changes until the answer;
**Refresh news** stays focusable the same way while it reads, as does the
account sheet's **Refresh privileges**; their dimmed look comes from the
`is-busy` class in `kit.css`, so an `aria-disabled` privilege gate keeps its
own look); writes go out one at a time in order, only the
latest answer is shown, and a failed latest write reads the stored value
again. `WorkspaceServer`
reads the network only with `news_reader`, which `cmd_ui` sets to
`news.fetch_feed`; servers built without it (every test harness) answer from
the cache or as offline. The desktop app opens headline links with the system
browser: `blogs.oracle.com` joins the HTTPS hosts `externalLink` permits, and
the window-open handler always denies a new app window. Tests:
`test/test_news.py`, `apps/workspace/test/news.mjs`,
`apps/workspace/test/news-setting.mjs`, and the desktop `backend.test.cjs`.

`python3 -m myflames ui [--port PORT] [--no-browser] [--no-restore]` launches the
  myflames Performance workspace on IPv4 loopback (deprecated in favor of the standalone desktop app binary). React/TypeScript source is in
  `apps/workspace/`. Places (sidebar, `Sidebar.tsx`): Home (`Home.tsx`: getting
  started, Connections, recent work), Plans (explore, plus Compare as a mode;
  Import plan lives in the Plans bar), Query editor (`QueryLab.tsx`), Query
  digest (`QueryDigest.tsx`, a place of its own right below the Query editor,
  mounted on its first visit, reading only on screen; its rows open the editor
  through `QueryLabHandle.openDigest`), Database diagram (`place="diagram"`,
  rendered by `Server` like Tuning: one view, full width, no list of views or
  Refresh all; `SchemaDiagram.tsx`), Server
  (`Server.tsx`: a grouped sub-navigation, Overview: Health (first and default),
  InnoDB, Error log (`ErrorLog.tsx`); Contention: Locks; Schema:
  Indexes, Tables; Setup: Parameters (`Config.tsx`; internal view id `config`), Instruments, Maintenance
  (`Maintenance.tsx`; both with `opsApi.ts`, `OpsShared.tsx`, `ops.css`,
  mounted on first visit and kept mounted so a skip loop or a job outlives a
  view switch); Security: Users and roles (`Accounts.tsx`)). The Workload place
  (`place="workload"`) lists Top statements (`SlowQueries.tsx`), Processlist
  (`sessions`; a saved `running` restores as `sessions`), Waits and I/O;
  Replication (`ReplicationPlace.tsx`), Cluster (`ClusterPlace.tsx`, right
  after Replication) and Tuning (`place="tuning"`, with the
  Full analysis, `report`, as its second tab) are places of their own. A view
  reads when it is opened on screen (once per connection; Refresh retries;
  `useViewOnScreen()` from `viewOnScreen.ts`, `useOpenRead`), Top statements,
  Error log and Query digest included; Tables and Indexes keep their
  schema-size consent. A route to the Database diagram (`chooseServerView`,
  Server's `choose`, Home's card, a session saved on the old Server view)
  opens its place. The sidebar's Replication item is drawn by the shell from
  the shared `replication-1.0` report (`replicationNav` in `opsApi.ts`):
  muted with "This connection is not a replica" when `replica` is false,
  Unknown only when `available` is false. Startup, a reload with a
  restored connection and choosing a connection send only the connected
  account's `/api/live/privileges` read (`PrivilegesProvider`, `privileges.ts`:
  on connect, on focus when older than 60 s, after an account change; it
  enables or greys actions and is not a view read); nothing else reads until a
  view is on screen. The same report is shown by `AccountPrivilegesSheet`
  (`Connections.tsx`), opened from the connection picker's **Account and
  privileges…** item or the palette's **Account and privileges** action; its
  Refresh privileges calls `refresh(true)`. The top bar has no Privileges
  button and no Local badge (the privacy line is in About myflames). The Replication report is owned by the shell
  (`main.tsx`, passed to `Server` and `ReplicationPlace`), so Server's Refresh
  all and the place share one `Bound<ReplicationReport>`; `ReplicationView`
  reads once when it opens on screen with no report or one from another
  connection, never while a hidden place stays hidden. The Cluster report is
  shared the same way (`clusterShared` in `main.tsx`, `cluster`/`onCluster`
  on `Server`), and the shell derives the sidebar item's state from it
  (`clusterNav` in `clusterApi.ts`). Badges come only from loaded reports, which
  `Server.tsx` keeps. Every view uses `ViewLayout` from `kit.tsx`: toolbar,
  one summary line, a table or one-line rows, and an inspector (pane, or a
  bottom sheet below 1,040 px; the list of views is pushed below 720 px).
  Shared components live once in `kit.tsx`/`kit.css` (SourceList, DataTable,
  GroupedList, Inspector, Popover/NotesButton, StatusPill, CountBadge,
  NavMarker, Segmented, MetricTile, TileGrid, FactStrip, Fields, Sparkline,
  ConfirmSheet, PrerequisiteGate);
  explanations go behind ⓘ or into the inspector, never on the page. Investigations (`Library.tsx`),
  Learn (`Teach.tsx`; the CLI stays `myflames teach`). Below the places, Open
  plans; from 651 to 900 px the sidebar becomes a 48 px icon rail, with
  tooltips, accessible names, compact status markers and an Open plans (N)
  popover. Its toggle opens the full sidebar as an overlay and Escape returns
  focus to the toggle. At 650 px and below it keeps the Menu drawer.
  The footer has one autosave status and the Settings gear, which opens the
  Settings page (`SettingsPage.tsx`): a page in the main area, not a place in
  the sidebar's navigation, with a section list on the left (the Server
  place's `SourceList`): Appearance, Query editor, Workspace (Restore
  workspace on startup and Start fresh), Updates and news (Check for updates
  automatically, Show MySQL and MariaDB news on Home, and the update item
  while one is pending) and Client tools, with Audit log and About myflames
  as actions at the bottom of the list. It opens from the gear, ⌘, (Ctrl+,
  on Windows and Linux) or the command palette, and closes with Escape, Back
  or choosing a place, returning focus to the gear.
  The two switches carry their explanation in a visible hint tied by
  `aria-describedby` ("Off clears the saved workspace. Passwords are never
  saved in it.", or "Off for this launch (--no-restore); the saved workspace is
  kept."; "Planet MySQL and the MariaDB Foundation, read over HTTPS while Home
  is open."), with no separate information button; Home's news section keeps
  its own. Saved connections and the connection picker are in
  `Connections.tsx`; the UI says "connection", the API/CLI keep `profile`. The
  connection in use is `query_lab.profile_id` + `query_lab.connection` in the
  session (its password stays in page memory; `connection.read_only` is the
  one safety choice of an unsaved connection). `connectionSafety` there is the
  client twin of `dbops.connection_safety` (a saved connection's type only
  while host, port, user and TLS settings match, else Production) and
  `requestConnection` sends its `type` and `read_only`. Profiles are read again
  on window focus and by `WritePolicyContext.check` right before a Kill is
  sent (a changed type or read-only flag changes the connection key, so the
  sheet sends nothing); while they are not loaded or the read failed the
  policy is unknown: the footer says "Production assumed" and Kill is off
  (`WritePolicy.blocked`). `SafetyStatus` (`Connections.tsx`) is the first
  item of the window footer (`WindowStatusBar lead`, `kit.tsx`) on the
  places that send to the connection in use (Query editor, Server, Workload,
  Replication, Cluster, Tuning): `data-status="safety"`, `data-safety` production,
  read-only, development or unknown, a dot plus words, `role="note"` with the
  full sentence as `aria-label` and tooltip, and Retry after a failed read.
  Nothing sits under the top bar (`--topbar-height` is the top bar row), and
  the picker shows `TypeDot` and `SafetyTags` (a short type word in place of
  the pills where they yield to width; see Home connections below). The Audit
  log is `Audit.tsx` (session mode `audit`, no place active). Stored session `mode` values keep
  the earlier names (`inspect`, `compare`, `query`, `library`, `teach`) plus
  `home`, `server` and `audit`; `placeOf` in `main.tsx` maps them to places. Naming follows
  `.claude/skills/ui-architecture/references/vocabulary.md`; styles use the
  tokens on `:root` in `style.css` (web-design skill). Browser test for the
  shell: `apps/workspace/test/navigation.mjs`; `apps/workspace/test/server-nav.mjs`
  covers the Server place. The workspace sidebar, Home, About and favicon use the 128 px `myflames-app-logo.png`, derived from `myflames-logo.svg`. Vite emits the stable
  `/assets/myflames-logo.png` URL. `npm ci && npm run build` there regenerates committed
  `myflames/ui_assets/` files shipped in the wheel. Python users need no Node.js.
  The UI API reuses the parser, sidecars, and renderers. Investigations are
  saved only on request. Investigations and saved connections (profiles) use
  `~/.myflames/workspace.sqlite3`; remembered connection passwords are stored separately.
  `/api/investigations/list` returns summaries only (`investigation-list-1.0`:
  `items`, newest first, and `next_cursor`, null on the last page; request
  `limit` 1 to 50, default 25, an optional `cursor` and `search`). Each item is
  an `investigation-summary-1.0` (`id`, `updated_at`, `name`, `notes`, `tags`,
  `baseline_id`, `plans [{id, name, capture: {mode}}]`), never a plan;
  `search` keeps the rows whose name, tags or notes contain the text, ignoring
  case, as Library's own filter does. `/api/investigations/get {id}` returns
  the complete saved `investigation-1.0` bundle (the shape `list` returned
  before), which Open, Compare to baseline and Export read on demand. Home asks
  for 6 rows; Library for pages of 25 with Load more and searches on the
  server after a 250 ms pause. `WorkspaceStore.list_investigations` pages on
  `(updated, id)`, so a delete never skips a row. Summaries live in the
  `investigation_summaries` table, written in the same transaction as each save
  and delete and checked against `records.updated` before every list: an older
  myflames using the same file writes records only, so missing or outdated
  summaries are rebuilt one bundle at a time and a record that is not a readable
  bundle is left out (no `STORE_VERSION` change or migration).
  `WorkspaceStore.list("investigation")` keeps its full-bundle result for
  Python callers. Tests: `test/test_investigation_summaries.py`,
  `test/test_workspace_api.py`, `navigation.mjs`.
  `/api/analyze` sends the plan's SQL once (`query`, and the sidecar's
  `query.raw`): its teach hooks, top level and in `analysis`, carry no
  `query_sql` (one copy per hook made a 801-operator plan with 70 KB of SQL a
  123 MiB response). Export JSON adds `query_sql` back to each hook
  (`exportedAnalysis` in `api.ts`), so the file equals the CLI sidecar
  (`browser.mjs` compares them); the CLI sidecar, digest and HTML report keep
  `query_sql` on every hook. Its `chart_data` is `chart-data-2.0`
  (`build_chart_data(..., compact=True)`): each analysis message is stored once
  in `highlight_messages` and nodes carry `highlight_id`, because a grouped
  warning names every affected table and applies to each of them. HTML reports
  and `explore` pages still embed `chart-data-1.0`; the bundle reads both
  (`charts/core/highlights.ts`), and `charts.mjs` checks that both draw the same
  chart. Tests: `test/test_workspace_payloads.py`, `test/test_chart_data.py`. **Save
  connection** then **Save with password** stores the secret through `secret_store.py` (macOS Keychain via
  `security -i` stdin, `secret-tool`, Windows Credential Manager, else the
  HMAC-SHA256 encrypted vault `~/.myflames/credentials.vault` + `vault.key`;
  `MYFLAMES_SECRET_BACKEND=keychain|vault|none`). Connection endpoints accept
  `connection.profile_id`; `WorkspaceStore.connection_with_saved_password`
  fills the password per request, only for the profile's own host/port/user.
  The secret's key is `<profile id>.<endpoint fingerprint>.<random>` (the record's
  `secret_ref`, never returned by the API): a SHA-256 of host, port, user, TLS mode
  and CA path, and a new random part on every save. The record and its
  `secret_ref` are written in one `BEGIN IMMEDIATE` transaction, the replaced
  secret is deleted after the commit, and the password is released only when the
  fingerprint in its key matches the profile's endpoint. Passwords saved before
  `secret_ref` existed stay under the bare profile ID until the next save.
  Tests must use temporary HOME and USERPROFILE with `MYFLAMES_SECRET_BACKEND=vault`. Live capture jobs reuse `live.py`; `/api/live/status` and `/api/live/cancel` require the owning `connection` (`dbops.identity_endpoint`), refusing other connections.
  Workspace restore is silent on successful startup. Settings > Start fresh opens a confirmation sheet and preserves the existing backup behavior. Actual restore warnings and save conflicts remain visible.
  Session restore autosaves the workspace snapshot (plans, query tabs, non-secret
  connection fields, screen/view/selection) about 1 s after each change to the
  single `session` row in the same database (`/api/session/load|save|clear|settings`,
  revision-checked, 64 MiB cap, `password` keys stripped at any depth, file 0600
  and directory 0700). Saves, clears and settings changes from one page share its
  `seq`; a request at or below the stored `writer_seq` of the same page (late or
  retried) changes nothing and returns `stale: true`, so it never deletes newer
  state or lowers the sequence. Client logic is in `apps/workspace/src/session.ts`;
  `--no-restore` skips restore and autosave for one launch without deleting the
  session. Test with `test/test_session.py` and `npm run test:session`.
  Its Learn catalog and lesson player use the existing `LESSONS` registry
  and `CURRICULUM` order. The Learn place uses a view toolbar (Search lessons, Family filter, Start learning), a one-line summary ("23 lessons, 16 in the learning path") and grouped rows by family, ordered by the learning path, each with its summary and "Lesson n of 16" ("Additional topic" off the path; never "Step", which is a beat of a lesson's own step bar). An open lesson has one thin toolbar, one control high (`.teach-toolbar.is-lesson`: All lessons, Previous lesson, Lesson n of 16, Next lesson, Download lesson, Full screen; it stays out of the top bar, which has no room for them at the 720 px minimum) and fills exactly the remaining height; the top bar shows the lesson title. `render_page` orders every lesson through `arrange_lesson(controls_html, stage_html)`: the banner, `section.controls` (parameters), `section.stage` (toolbar, scrubber, step bar, drawing and simplified code), `section.about-example#about-example` ("About this example": the `query_card`, then the `explainer`, side by side when there is room; lessons keep writing both cards in their stage HTML and `arrange_lesson` moves them by the exact markup the two helpers write), then the readout and Learn more; DOM order is the visual order. It also turns each control's `div.hint` into a `span.help-tip` (?) in a `.control-head` beside, never inside, its label, and appends its id to the control's `aria-describedby` (a hint keeps its own id, else `hint-<control id>`, never colliding with an id on the page); the tip opens on hover of the (?) or while the control has keyboard focus, and the controls heading gets `a.about-link` to `#about-example`. In-page `#` links are handled by the runtime (`teachFocus`: scrollIntoView, smooth unless reduced motion, focus to the section's h2) because an srcdoc frame resolves them against the workspace's address. Stage drawings fit the frame through the shared fit rule in `_BASE_CSS` (`max-height: max(160px, calc(100vh - var(--stage-chrome, 96px)))`); `teachFocus.fit()` measures `--stage-chrome` from the stage toolbar to the drawing plus captions and 24 px, and also counts what sits above the toolbar (page margin, banner, the one-row parameters) while that is at most a quarter of the window height and costs every stage drawing at most 15% of its scale; otherwise (wrapped parameters, a short window) the drawing fits under its toolbar as before. `test_teach.TestLessonPageOrder` checks the order for all 23 lessons and that the committed `docs/teach` pages equal the renderer. Full screen: `Teach.tsx` requests the browser's full screen on the lesson host (the desktop app grants only this permission, `fullscreenOnly` in `apps/desktop/main.cjs`; a refused request falls back to an overlay over the window, and one that settles after the lesson was left changes nothing) and posts `{type: "myflames-lesson-focus", focus}` to the frame; `teachFocus` in `myflames/teach/_html.py` accepts it only from its parent and switches to `html[data-focus="true"]`, moving the stage toolbar, scrubber, stage, phase label and readout grid into a fixed shell and back to their exact places on exit (lesson code keeps its element references); `main#lesson-main` becomes the Details panel. Escape in the lesson posts `focus: false`, which the workspace accepts only from that frame. A standalone page uses its own `#btn-fullscreen`. Covered by `test/learn-fullscreen.mjs` and `test_teach.TestLessonFitAndFullScreen`. The workspace adds `data-embedded="true"` to the lesson page, which hides its own header and footer. Lesson pages and the standalone catalog (`myflames teach --index`, which includes a lesson filter) use the workspace's light and dark tokens (`prefers-color-scheme`, `data-theme` overrides, and `?theme=` / `?embedded=1` URL parameters), restyled controls, and semantic `--svg-*` drawing colors resolved by `anim.cssVar` and `anim.palette`, so drawings redraw when the appearance changes and keep their meaning (descent path, arrival pulse, PK hop, build vs probe, hit, spill). Lesson titles use a colon ("B+tree lookup: how InnoDB finds a row"). See `docs/LOCAL_UI.md` and `docs/QUERY_WORKSPACE.md`. Test with `test/test_ui.py` and the CLI suite.
  Lessons use SVG only. The catalog exposes each lesson's key, title, summary
  and family; the workspace requests a lesson by key. The lesson API accepts
  an omitted `view` or `"2d"` for older clients and rejects any other value with
  HTTP 400. Session restore reads the lesson key and ignores obsolete view
  preferences. The shape and range helpers in `_cost_model.py` remain covered
  by the independent Python unit tests in `test/test_teach.py`.
  Keep the import dialog's native file control visually clipped but keyboard
  accessible; verify open/error states at desktop and narrow, short viewports.
  Use `docs/UI_TESTING.md` for the interaction matrix. Check contextual Learn
  links after navigating to a different lesson and returning to the plan.

### Release sync

A release is one commit in which the versions, the release notes, the README
screenshots and the handbook agree, and one GitHub release whose notes and
files come from that commit. AGENTS.md "Release sync" lists the steps; these
are the contracts of the tools that check them.

- **Versions.** `apps/desktop/package.json` `version`, `myflames/__init__.py`
  `__version__` and `pyproject.toml` `[project] version` are equal.
- **Release notes.** `scripts/release_notes.py X.Y.Z` (or `vX.Y.Z`) prints
  `docs/release-notes/X.Y.Z.md` (`--notes-dir` reads another folder) without
  its title line and without leading or trailing blank lines, as UTF-8: the
  GitHub release shows its name above the notes. It refuses a page that breaks
  the format in [Release notes](#release-notes) (title, sections, order, no
  heading deeper than level two, at least one entry, closed code fences, no em
  dash and no `/api/` path outside code fences) with a message on stderr that
  says what to fix, and exits 1. The script then ends the notes with the
  inspiration credit line (`CREDIT`, the README's "Inspired by Brendan Gregg's
  FlameGraph and Tanel Poder's SQL Plan FlameGraphs"), unless the page already
  holds it, so no release's notes lose it.
- **README screenshots.** `npm run docs:screenshots` in `apps/workspace`
  (`test/readme-screenshots.mjs`) builds nothing: it opens the built
  workspace in `myflames/ui_assets` through the perf harness (`open()` with
  `seed`, `beforeGoto`, `viewport`, `refuse`, `offline` and `launchArgs`), with the real Python server on
  a throwaway HOME, store and vault, two saved connections (Dev MySQL,
  Prod MySQL) and every database read answered from fixtures
  (`health-mysql-8.4-full.json`, `schemas-mysql-8.4.json`, a 24-row editor
  result, readiness, the editor's privileges preview, offline news). The page
  clock is frozen at 2026-10-14T16:26:00Z, the time zone is UTC, the locale
  en-US, the theme light, motion reduced, transitions and the caret hidden, and
  the window 1440x900 at scale 1. Chromium runs with `--disable-partial-raster`
  and `--force-color-profile=srgb` (without them a rounded corner's
  anti-aliasing changed between runs), and each image is written once two
  captures in a row agree. It writes `docs/screenshots/home.png`,
  `plan-visual-explain.png` (the sample plan), `query-editor.png` and
  `server-health.png`, then `manifest.json` (`version` from
  `apps/desktop/package.json`, `commit` from the checkout's HEAD,
  `captured_at` in UTC). Every request that is not for the local server is
  aborted from the first navigation on, and the script fails when one was made
  or on a page error; `--out DIR` writes elsewhere. Two runs of one build on one machine
  write identical PNGs (only `manifest.json` changes); fonts differ between
  systems, so capture on macOS, where the README's app runs. Run it after
  `npm run build` at the release commit.
- **`scripts/check_release_sync.py X.Y.Z`** exits 0 only when the three
  versions equal X.Y.Z, `docs/release-notes/X.Y.Z.md` passes
  `release_notes.py`'s format check, `docs/release-notes/index.md` lists it
  first with the page's date, lists every page in the folder newest first and
  names only pages that exist, `CHANGELOG.md` `[Unreleased]` holds no entries
  (they moved into the page),
  `docs/screenshots/manifest.json` `version` is X.Y.Z with the four README
  images present, and `docs/handbook/manifest.json` parses with every listed
  chapter file present; otherwise it lists every problem and exits 1.
- **Release files.** A release carries `myflames-X.Y.Z-macos-arm64.dmg` (new
  installs), `myflames-X.Y.Z-macos-arm64.zip` (the in-app update file) and
  `myflames-X.Y.Z-windows-x64-setup.exe`, each with its `.sha256`. The DMG is
  uploaded before the zip (a lower asset id): an installed myflames up to
  3.3.3 takes the first macOS asset GitHub lists
  (`app_update.match_asset_for_platform`), which must stay the DMG.
  `gh release create` uploads its files in parallel, so the zip and its
  `.sha256` go up with a separate `gh release upload` after the create ends.
  `scripts/check_release_assets.py X.Y.Z RELEASE_JSON` (the
  `gh api repos/vgrippa/myflames/releases/tags/vX.Y.Z` answer, or for a draft,
  which has no tag yet, `gh release view vX.Y.Z --json assets`, whose node ids
  it reads the number from `apiUrl` for; `-` reads stdin) checks the names,
  that every file finished uploading, the id order, and that the first macOS
  file in GitHub's list, by the 3.3.3 rule (`legacy_macos_choice`), is the DMG.
- **`.github/workflows/docs-sync.yml`** (`contents: read`; `contents: write`
  only in the release job). On a pull request to master (also when its labels
  change): a change under `apps/workspace/src/`, `apps/desktop/` (not
  `test/`) or `myflames/` (not `ui_assets/`, `tests/` or `test_*.py`; both
  sides of a rename count, and names are compared whole, so a name holding a
  newline cannot pass for `CHANGELOG.md`) without a change to `CHANGELOG.md`
  or to a `docs/release-notes/*.md` page fails unless the PR has the
  `no-changelog` label, and a
  change of the `apps/desktop/package.json` version runs
  `check_release_sync.py` for the new version. When a release is published, or
  by hand for a tag (Actions > Docs and release sync > Run workflow), it checks
  out the tag (the whole value must match `vX.Y.Z`; it is never echoed),
  replaces the release notes with `release_notes.py`'s text when they differ
  (ignoring line endings and outer blank lines), fails when the tag has no
  valid page for the version, and runs `check_release_assets.py`. Release
  notes of releases published before the tag carried `docs/release-notes`
  are edited by hand from master: `python3 scripts/release_notes.py X.Y.Z >
  local/notes.md && gh release edit vX.Y.Z --notes-file local/notes.md`.
- **Website.** myflames.io is built from the separate `vgrippa/myflames-website`
  repository. Its `sync.yml` workflow (every 6 hours and on demand) checks this
  repository out at its latest release tag (the peeled commit of an annotated
  tag; never master, so the site describes the released app) beside it, runs `sync_releases.py` (the latest
  release's DMG and Windows `-setup.exe` with their `.sha256`, never the update
  zip), `build_docs.py --source` (the handbook, the reference documents and the
  raster images in `docs/screenshots`; links and SVG files are not copied) and
  its release notes. Release notes are written after a release, so they come
  from master's `docs/release-notes` alone (a sparse checkout, read as
  Markdown, `--release-notes-source`): 3.0.0 and later under a Release notes
  group, older versions linked on GitHub, and nothing newer than the latest
  published release. The sync also reruns when that folder changes. Then it
  runs its QA without the CLI examples (no code from this repository runs in
  that job, which can push) and commits to its default branch when something
  changed. The site goes live only when someone clicks Publish
  in Lovable.

### Release notes

The release notes are written for people who use myflames (DBAs, SREs and
developers), in the form of Percona Server for MySQL's release notes: an index
and one page per version. Engineering detail (endpoints, schemas, functions,
tests, the reasons behind a design) belongs in this reference and in commit
messages, never in the notes.

- **Files.** `docs/release-notes/index.md` lists every version newest first,
  one line each: `- [myflames X.Y.Z (YYYY-MM-DD)](X.Y.Z.md)`.
  `docs/release-notes/X.Y.Z.md` is one version's page. `CHANGELOG.md` holds
  only `## [Unreleased]`: every pull request that changes what users see adds
  its entry there, already written in the style below and under the heading
  of the section it will go to (`### New features`, `### Bug fixes`, and so
  on). The release commit moves those entries into the new page and leaves
  `[Unreleased]` empty.
- **Page layout.** The first line is `# myflames X.Y.Z (YYYY-MM-DD)`. Under it
  comes one sentence on where the version runs (from 3.3.0: "myflames X.Y.Z is
  available for macOS on Apple Silicon and, as an unsigned preview, for
  Windows x64."). Then these `##` sections, in this order, each left out when
  empty: Release highlights, New features, Improvements, Bug fixes, Security
  fixes, Deprecations and removals, Known issues, Packaging notes, Additional
  resources (always present: the download links, the Quickstart and GitHub
  Issues). No other headings. The page ends with the inspiration credit line.
- **Release highlights.** Only for a version with three or more notable
  changes: two to four bullets of one sentence each, naming what is worth
  upgrading for. The detail stays in the sections below.
- **Entries.** Each is a bullet of one to three sentences, about 15 to 60
  words, with no bold title in front. New features start with "Adds",
  improvements with "Improves" or with the thing and what it does now, bug
  fixes with "Fixed an issue where" and the symptom the user saw (a second
  sentence may say what happens now), deprecations with what to use instead.
  UI labels are bold as they appear (**Settings > Client tools…**); SQL,
  variables, programs, options and file names are in backticks.
- **What stays out.** `/api/` paths, schema tags such as `memory-1.0`, module,
  function and test names, CI, release tooling, README screenshots, test
  infrastructure and other contributor-only work, mechanics such as timers,
  worker threads and time budgets, lists of edge cases, and review notes.
  Command line and `myflames-mcp` changes appear only as deprecations or when
  they are the whole point of the entry.
- **Writing.** Plain sentences a colleague would say aloud. No em dashes (a
  period, comma, colon or parentheses instead; en dashes only in number
  ranges), no chains of semicolons or stacked parentheses, no "not X, but Y"
  frames unless the contrast is the news, no lists padded to three, no filler
  such as seamless, robust, comprehensive, leverage, ensure or notably, no
  emojis, no notes about who wrote or reviewed the change. Every fact comes
  from the change itself.

### Shared view presentation


Appearance is registered in `apps/workspace/src/themes.ts`. `THEME_CHOICES`
contains stable ids, display names and a light/dark base; `THEMES` pairs each
choice with credit and picker swatches. The shell imports only choices so
picker metadata stays out of the entry chunk. `THEME_IDS` is exported for the
backend preferences allowlist comparison. System resolves through the OS
media query; `appliedThemeBase()` resolves explicit ids through the registry.
The command palette offers `Theme: <name>` for every choice. Settings > Appearance offers System, Light, Dark and the named themes.

`applyTheme()` and `savedTheme()` use the shared UI preferences store, including Settings > Appearance and palette actions. `ui.py` injects a validated stored `data-theme` on `<html>` for every non-System theme, so desktop launches paint the saved theme before JavaScript runs. System omits the attribute. Vite's blocking boot script still accepts only ids generated from `THEME_IDS`; it never removes an injected attribute. Legacy localStorage is a one-time browser development fallback, and the backend answer wins. `UI_THEME_IDS` is the backend's one literal list, checked against the registry by `test/themes.mjs`.

`themes.css` loads after `style.css` through `styles.ts`. Every named theme
sets `color-scheme` and every color property set by the explicit Dark block,
plus all chart surface roles. Light and Dark retain their existing values.
Adapted palettes retain official syntax colors when they pass; changes tune
HSL lightness only and show `official #xxxxxx` beside each adjusted syntax
color. Credits say `Based on <name> (MIT)`; exact upstream copyright notices
and license permissions are in NOTICE. Fire, Digital Rain and Neon Grid are
myflames originals under Apache-2.0.

The contrast contract is ink and muted ink at least 4.5:1 on background,
content and muted surfaces, lines and control lines at least 3:1, accent at
least 3:1 on background, each syntax role at least 4.5:1 on the editor surface,
and status text at least 4.5:1. Selection and current Find fill contrast at
least 1.8:1 against a dark editor or 1.5:1 against a light editor; selected
ink contrasts at least 4.5:1 against each highlight. Matching words use a
subtler fill, at least 0.3 lower in contrast than selection, yet at least
1.3:1 against a dark editor or 1.2:1 against a light one. Opaque hex literals
for measured tokens make source-based and computed-style tests comparable.
Status hues retain OK green, warning amber, danger red and information blue;
Production stays purple and Development teal. The estimate role stays violet.
Operator families, including Join and Scan, retain their bands and dark ink.
Only chart canvas, cards, panes, ink, lines, focus and selection follow the
workspace palette, through `--mfc-*` roles. Injected chart defaults match
System, Light or Dark only; a late chart style cannot overwrite a named palette.

Sandboxed previews receive the resolved base, accents and allowlisted chart
surface colors from `bindFrameAppearance()`. `themeFrame()` uses fixed script
text, accepts messages only from its parent, validates colors with
`CSS.supports`, rejects variable and URL references, and assigns them with
`style.setProperty`. Empty host overrides remove the inline property so a
return to Light, Dark or System restores the frame defaults. Values never
enter markup or executable code. The
sandbox remains opaque. SchemaDiagram watches both the root `data-theme`
attribute and OS appearance through `appliedThemeBase()`; its default Mermaid
color template follows appearance while explicit templates stay selectable.

To add a theme: add one choice and matching picker details, a complete CSS
block, upstream copyright/license attribution and user documentation. Extend
the backend allowlist only in its single existing location once preferences
are present. Run `theme-contrast-unit.mjs` for every token and selection pair,
then build and run `theme-contrast.mjs` for computed styles and `themes.mjs`
for registry completeness, palette actions, restore, base resolution and frame
isolation. Recheck 1440 and 720 px layouts and light/dark OS preferences.

Measured durations and ages use `durationText` (`duration.ts`). Duration table
cells and the Processlist inspector, Locks summary facts and Replication lag
facts use `Duration` (`PerfShared.tsx`) for exact seconds, including decimal
precision, on hover and in the accessible name; optional lag caveats join that
same tooltip. Keep sorting numeric and preserve unknown states. Replication's
summary, lag tile and topology use the same readable words; the tile and map
labels retain exact seconds on hover. The ALTER review transaction age uses
words too. Narrative text and other Locks badges need not offer exact seconds;
see `docs/LOCAL_UI.md` for the hover locations. Sampling windows, budgets,
thresholds, input controls and progress counters keep their existing units.

`NavMarker` (`kit.tsx`) keeps a state dot separate from a neutral count.
Compact navigation rows put the state in their tooltip and accessible name;
a caller that already provides hidden text uses `announce={false}` to avoid
reading it twice. Navigation may hide the OK dot. `ViewSwitcher` places the
other views' attention count on its chevron and names it there; the current
view's title never carries a bare dot for a different view's state.
`StatusPill` and `StatusDot` draw both a dot and a word, including quieter OK
rows in Health and InnoDB. Summary words are OK, Watch, Problem, Not judged
and Unknown.

`TileGrid` uses equal-width columns in wide windows. At 900 px and below,
`kit.css` turns it into one horizontally scrollable strip of 44 px tiles;
`FactStrip` uses the same breakpoint to collapse into Details. `Fields`
uses a label column sized to its longest label up to 40% of the available
width, with a flexible value column; at 900 px and below each value follows
its label on a separate line. `WindowStatusBar` keeps safety words visible while other facts truncate before the version and update actions. Server,
Workload, Tuning and Database diagram share the latest version read for the
connection in use; other connection places publish their own read version.
Unknown fields stay hidden. Visible Esc hints use a keycap.

Replication passes `detailBackLabel=""` to `ViewLayout` for a side inspector
with the list still visible in wide windows, and a sheet at narrow widths.
Parameters details follow the same interaction contract. Parameter Value and
Default use the same `valueSpan` formatter. Replication GTID sets use
`formatGtidSet` and `code.ops-gtid`, with wrap opportunities after commas.
Other views may keep the full-width details pattern.

Tuning category cards use an auto-fit grid. A category with at least one
measurement and fewer than half its checks measured has `is-low-coverage`:
a muted, hatched bar and a score labelled with coverage, such as "100% of
1 check". Run full analysis is the toolbar action on both Tuning tabs;
Tuning has no generic Refresh. Database diagram uses its
view Refresh for rebuilds. I/O keeps its view controls in the toolbar and
its counters in the content, with no repeated action under a section heading.

Investigations hides the empty list when nothing is saved and offers
Save N open plans as an investigation… when plans are open, otherwise Import
plan. Learn's shared 2D lesson wrapper hides Phases when no phase items exist
(`myflames/teach/_html.py`); phase navigation remains when marks exist.

### Chart runtime

Default flame colors use a cream-to-orange sequential scale of the square root of each frame’s own units divided by the whole graph’s units. Width still includes descendants. Color remains stable while zooming; unmeasured plans use a neutral fill. Python and TypeScript mirror this rule, and explicit alternative palettes remain supported.

HTML reports, `explore` pages, and the workspace draw every chart with the
TypeScript runtime in `apps/workspace/src/charts/` (built by `npm run build`
into the committed `myflames/assets/myflames-charts.js`, inlined by
`chart_assets.py`). Python stays the source of every semantic decision:
`chart_data.py` emits `chart-data-1.0` for reports and `explore` pages and
`chart-data-2.0` for the workspace (`docs/schemas/chart-data-*.json`; 2.0
stores each analysis message once, and the bundle reads both) using the same
helpers as the Python renderers, and the TS only lays out and handles
interaction. Keep both renderers in step: `apps/workspace/test/parity.mjs`
compares them on every fixture, `test_chart_data.py` fails if the committed
bundle is older than its sources or over 80 KB. Charts are instance-scoped
(no page-wide IDs), set plan text with `textContent` only, and capture Ctrl+F
only while focus is inside the chart.

Visual Explain's fit never renders text below 11 px: `MIN_TEXT_PX` in
`charts/core/zoom.ts` and `SMALLEST_TEXT_PX` (the smallest font in
`WORKBENCH_CSS`) in `charts/workbench.ts`, with the same two constants in
`myflames/workbench.js`, clamp the fit ratio by the screen scale (1 in the
workspace, `getScreenCTM()` in reports and the standalone SVG); a plan larger
than that view starts at its first inputs and pans, Fit, a double click and a
resize while fitted return to that opening view, and only an explicit zoom
out goes smaller. The Execution tree prints Total on every row, leaves
included (a leaf's total equals its self time), and `—` in Self and Total for
an unmeasured operator, in `output_tree.py` and `charts/tree.ts` alike.

### Bundle and lazy loading

The workspace build (`apps/workspace`, `npm run build`) keeps the first screen
in the entry chunk: React, the shell (`main.tsx`), Home, the sidebar, saved
connections, the command palette, Learn and the shared kit (about 365 KB,
1.1 MB before the split). Other places and views load their own chunk the
first time they mount, through `lazyComponent` in `src/lazy.tsx` (React.lazy
with its own Suspense boundary; the fallback shows nothing for 300 ms, then one
"Loading…" line, and a chunk that cannot be read shows "This part of the
workspace did not load." with Reload):

- `main.tsx`: the Query editor (mounted hidden at startup; its chunk starts
  loading with sign-in and the session read, and calls to its handle made
  before it mounts wait in `withLab`), Query digest, Server (with Workload,
  Tuning and Database diagram),
  Replication, Cluster, Investigations, the Audit log, and the Plans chart and
  execution context. Once the first screen is up, `preloadWhenIdle` loads
  Server, Replication, Cluster and the Plans chart in idle time, one chunk at a time.
- `Server.tsx`: the views it uses only as components (InnoDB, Tuning,
  Processlist, Waits, I/O, Indexes, Tables, Parameters, Database diagram (its
  own place, still drawn by Server), Users and roles), preloaded in idle time once the place has mounted. Health,
  Error log, Top statements, Locks, Instruments, Maintenance and Replication
  share helpers with `Server.tsx` and come with its chunk.
- `QueryLab.tsx`: Table data, and Format SQL (`import("./sqlFormat")` on
  first use: sql-formatter 15.9.0, pinned in `package.json` and
  `package-lock.json`, with only its MySQL and MariaDB dialects and nearley,
  about 90 KB). Mermaid keeps its own chunks (Database diagram). The Query
  editor chunk carries the CodeMirror 6 editor (`src/editor/`: the pinned
  `@codemirror/state`, `view`, `commands`, `language`, `autocomplete`,
  `search` and `lint` packages with `@lezer/common`, `highlight` and `lr`;
  only `QueryLab.tsx` imports them, so the entry chunk stays free of them).
  The Settings page is its own chunk (`SettingsPage.tsx`, loaded lazily from
  `main.tsx`), which keeps `editorSettings.ts` and the settings UI out of
  the entry chunk.

Rules for code that splits:

- A part keeps the form it mounted with: before its chunk arrived it renders
  through Suspense, after that directly. A mounted place is never remounted
  because its chunk arrived, so mounting rules hold (Server, Replication and
  Cluster mount on first visit and stay mounted, views read on open only while on
  screen, Back keeps the mounted list) and `QueryLab`'s ref handle is
  forwarded.
- Import a lazily loaded module only through `lazyComponent` (type imports are
  fine); one static value import puts it back in the entry chunk. The shell
  reads the lists of Server and Workload views from `serverViews.ts`.
- Styles are not split. `src/styles.ts` imports every stylesheet in cascade
  order (`style.css`, then `themes.css` last) and `main.tsx` imports it first, so the order never
  depends on which chunk loads first; the built stylesheet is identical to the
  unsplit build. Add a new stylesheet to `styles.ts` too.
- Code that looks up a lazy part's elements right after switching to it must
  wait for them: the Server toolbar measurement in `main.tsx` watches for the
  strip to appear, and `focusWhenMounted` in `Server.tsx` focuses a view's
  heading once it exists.
- `test/bundle-split.mjs` (`npm run test:bundle-split`) checks that
  `styles.ts` lists every stylesheet in `src/` exactly once with `style.css`
  followed by `themes.css` last and `main.tsx` importing it first, that no lazily loaded module is
  also imported statically by `main.tsx`, `Server.tsx` or `QueryLab.tsx`,
  that the committed build has exactly one stylesheet, and that the entry
  chunk stays under 500,000 bytes (about 475 KB in 3.3.3; Home, the sidebar
  and the shell only, a place or view belongs in a lazy chunk). The chart
  runtime has its own
  80 KB budget (`test_chart_data.py`).

Large tables: `DataTable` and `PaginatedReportTable` sort and measure only when
their rows change. `useStableRows` (`kit.tsx`) keeps the previous array while
it holds the same objects, so a view that filters its report on every render
(Processlist, Indexes) neither sorts again nor measures its columns again on a
selection or a keystroke elsewhere. A hidden table retains its last nonzero
frame width; its ResizeObserver tracks visibility separately and retries
deferred row and height measurements when it becomes visible, including at
the same width. Already measured rows keep their cache. `sortRows` reads each row's value once.
`formatNumber` (`api.ts`) reuses one `Intl.NumberFormat` per options for
`time`, `number`, `count` and `CompactNumber`: `toLocaleString` with options
builds a new formatter on every call.

Columns gutter: `DataTable`'s **Columns** control sits in a fixed 36 px
gutter inside the table frame, outside the horizontally scrolling data
viewport. A sticky header cell (or a trailing colgroup slot alone) is not
enough: it overlays earlier data whenever the table overflows sideways. Width
calculations use the data viewport, which excludes the gutter, so at every
scroll offset the viewport ends at or before the gutter's left edge, and at the
right end the last header and its numeric value end before it too; a table
whose column minimums exceed the viewport still scrolls inside its frame. A
trailing 36 px column exists only when the table has row actions, which scroll
with their row (36 px more width in those tables, and no fixed control over a
scrolling value); tables without row actions keep no empty trailing column.
Resize handles, sorting, virtualization, persisted widths and hidden keys and
the numeric minimum widths are unchanged. Suites find Columns by its
accessible name, since it is outside the scrolling table; `kit-static.mjs`
checks the markup without a browser and `kit-primitives.mjs` measures fitted
and overflowing tables at 1440x900 and 720x600 in both appearances, with and
without row actions, at the start, middle and end scroll offsets.
Tables and Indexes use `PaginatedReportTable`'s filter, count, Rows, Export and
paging locations (Tables' **Filter tables** joined that group; schema scope and
Fresh numbers stay in `ViewLayout`'s toolbar), and every Tables list marks
Table as its `primary` flexible column with a 220 px minimum. Indexes advice
(unused and redundant) holds explanations and copyable SQL only: Set
invisible/visible (Set ignored/not ignored on MariaDB) and Drop index have one
write-action home in the inspector's action bar, with row and context menus as
contextual entry points. `indexes.css` is registered in `styles.ts` to keep the
fixed stylesheet order.
### Local authentication and request limits

`WorkspaceServer.launch_url` carries a random, one-use `#launch=` secret, valid
for 30 minutes. `api.ts` removes the fragment before any API call and exchanges
it through `POST /api/auth/session` with `X-Myflames-Bootstrap: 1`. The server
returns the request token and sets a per-instance cookie with `HttpOnly`,
`SameSite=Strict`, and `Path=/api/`. The launch secret is never written to browser
storage, HTML, or the workspace database. Fetching the page alone grants no API
authority.

`myflames.session-proof` in origin-scoped localStorage holds the request token.
Bootstrap refresh and every subsequent API POST require both that proof in
`X-Myflames-Token` and the cookie, plus the exact allowed Host and Origin.
Cookies are host-scoped, so a copied cookie from another local port is
insufficient without that origin's proof. A new server instance invalidates the
old pair and requires its new launch URL. If browser storage is unavailable,
the authenticated current tab still works; a reload requires a fresh launch.
There is no CORS access. Responses use no-store and no-referrer policies.

The server admits at most 32 request workers. Socket reads have a 15-second timeout. Bodies also have a total 15-second
read deadline, so trickling body bytes cannot extend that deadline. The usual body limit is
12 MiB; session save permits 65 MiB for its bounded 64 MiB stored payload.
Account requests have an additional 256 KiB encoded-data budget.

### Client and local-storage boundaries

The desktop renderer uses an in-memory session partition, so UI preferences belong in the workspace SQLite store, not localStorage. Theme, sidebar width, Server view navigation collapse and Health auto-refresh are persisted through `/api/settings/ui`. Browser development reads legacy localStorage once as a fallback and never promotes it over the backend answer. The renderer's session proof remains origin-scoped localStorage authentication data. Query editor split height remains part of the autosaved `editor_layout` session state.

Read clients, capture jobs, writes, and editor sessions use the connector's
clean environment, an empty temporary HOME, and an exclusive `--defaults-file`.
Personal `.my.cnf` and `.mylogin.cnf` and system option files cannot redirect a
saved credential. The login-file override is also isolated. Socket connections
still use the explicit connection settings. Without an explicit socket, classic
client option files set `protocol=TCP`, including for localhost with a port.
An explicit socket selects `SOCKET`. MySQL Shell selects TCP through an
explicit port; its socket options omit port so it cannot override the socket.
Lifecycle control queries use the same 64 MiB stdout and 1 MiB stderr caps as
the connector. Editor hello reads both `@@version` and `@@version_comment`
and calls the shared flavor detector. Editor credentials are removed
once the client handshake completes. Every short-lived client invocation owns a
fresh mode-0600 option file, removed in a finally block when its subprocess
exits (including failures and cancellation). Concurrent calls on one connection
never share option files; close() remains idempotent and cannot remove another
invocation's credentials.

`connection_with_saved_password` copies and validates connection fields and
profile policy metadata before looking up a secret. Arbitrary executable,
socket, client-certificate/key, option-file, and initialization overrides do not
pass through the browser's saved-credential path. CLI connection flags remain
an explicit local choice. Clone checks the recipient's negotiated TLS before
sending a remembered donor password, in addition to the reviewed pair and donor
TLS policy checks.

Workspace database files and SQLite sidecars, vault directories, keys, and
locks reject symbolic links and foreign owners before opening or changing
permissions. Creation uses exclusive creation; existing file descriptors are
checked without following links where supported. POSIX directories are 0700
and files 0600; Windows retains its platform helper. SQLite connections close
if setup fails. Session persistence redacts recognized SQL credential literals
in editor text as well as password fields; ordinary SELECT drafts are retained.
History redacts the actual reused editor session's password even when a later
request supplies no password.

### Reviewed actions and session ownership

Kill preparation, single replication skips, and lifecycle plans
carry a server-issued review token. Its registry holds at most 256 entries for
300 seconds. Tokens bind the operation, connection identity and policy, and
reviewed details. Checks happen before saved-password lookup; consumption is
atomic at the send boundary after fresh state and policy checks. Missing,
expired, altered, or replayed tokens require a fresh review. Tokens are never
persisted. Lifecycle jobs re-read saved profile policy before connecting,
after preflight, and before tool invocation. A deleted profile or changed
endpoint/policy requires a fresh review; a rename alone does not. Clone donors
are bound too, with read-only permitted for the donor read.
Accounts and the Query editor retain their own bounded review
registries and confirmation contracts.

Every editor owner path checks the connection, including ended sessions,
status, export, stop, close, commit, and rollback. Ended sessions compare
endpoint and saved-profile identity; policy changes on the same endpoint do
not transfer ownership. Explicit reconnect evaluates fresh policy. Live
sessions retain full identity matching. A different connection cannot remove
an ended session by opening its tab, or learn the other connection's name from
an error. Lifecycle and capture status/cancel also enforce their endpoint
owner before accessing the job.

### Shared CLI confirmation

`dbops.cli_confirm` renders descriptions and prompts on stderr, implements
`--yes`, terminal detection, default No, and exact typed guards, and returns the
outcome to the command's existing audit owner. The write policy runs before
credential prompts. Every non-terminal server write requires `--yes`, including
ordinary SQL on Development. On a terminal, writes permitted by policy can
still proceed without a prompt. Required names, targets, and acknowledgements
match exactly; only the input line terminator is removed. EOF and Ctrl+C before
sending are declined and audited.

Unsent refusals and declines exit 2 with a stderr diagnostic, empty stdout even
under `--json`, and an untouched `-o` destination. This applies to
`locks --kill`, `sessions --kill`, `config set`, `config reset-persist`, account
changes, `sql`, `dump`, `restore`, `clone`, and replication `skip`/`skip-loop`.
Executed and partial results keep their existing schemas and exit behavior;
read-only `--preflight`/`--fail-on` findings retain their finding contracts.
Local `audit purge` has its separate confirmation contract.


### Profiles

`python3 -m myflames profiles list [--json]`, `profiles set NAME [--type production|development] [--read-only | --read-write]`, `profiles forget NAME`,
  `profiles delete NAME` manage workspace connection profiles and their saved
  passwords. `--profile NAME_OR_ID` on `capture`, `top`, `sessions`, and the other server commands loads a
  profile and its remembered password; explicit flags override profile fields; a
  missing profile, or a saved password with a different host/port/user, exits 2.
  Lives in `myflames/workspace_cli.py` (`_resolve_connection`, `cmd_profiles`).
  Every profile carries `type` (`production` | `development`) and `read_only`
  (bool) in its payload (`workspace_store.PROFILE_SAFETY_FIELDS`), returned by
  `/api/profiles/list|save`, `profiles list --json` (`profiles-1.0`, additive)
  and accepted by `/api/profiles/save` (`value.type`, `value.read_only`). A save
  that omits them keeps the stored values (older UI forms), forget-password
  keeps them, `WorkspaceStore.set_profile_safety` changes them without touching
  the saved password. New profiles and every existing one are `production`,
  read-write: the database carries `PRAGMA user_version` (`STORE_VERSION` 1),
  and `_migrate` writes the two fields into older profile rows once; `_present`
  also defaults them on read. The type, never the password, is part of the
  connection identity reports and confirmations are bound to.

  Pasted connection fields: `live.connection_options` (every API request,
  profile save and test, the CLI connection flags) and `lifecycle._options`
  (dump, restore, clone) trim white space (`str.isspace`, so also the no-break
  space) and `live.INVISIBLE` (zero-width spaces and joiners, word joiner, byte
  order mark) from both ends of `host`, `ssl_ca` (and the lifecycle
  `ssl_cert`, `ssl_key`, `socket`), and from the end of `database` (MySQL and
  MariaDB refuse a database name ending in a space: `check_table_name` in
  `sql/table.cc`; a leading space is legal and kept). `user` and `password` are
  never changed. `live.clean_host` refuses a space inside the host, a `/` (a
  URL or path) and `host:port`, and removes the brackets of `[IPv6]`; a host
  that is only spaces counts as empty (127.0.0.1). Endpoint comparisons trim
  the same way (`workspace_store.endpoint_value`, used by `_same_endpoint`,
  `dbops._same_endpoint` and `dbops.connection_identity`, and `trimPasted` in
  `binding.ts` `connectionKey`), so a profile saved with a spaced host keeps
  its saved password and its Development type. The Home form trims Host when
  it loses focus and splits a pasted `host:port`, `[IPv6]:port` or
  `mysql://user:password@host:port/db` into its fields (`splitHost` in
  `Connections.tsx`); Port must be 1 to 65535 before Use. Tests:
  `test_live.py` (`test_pasted_spaces_around_host_and_paths_are_dropped`,
  `test_connection_identity_ignores_pasted_spaces`), `test_secret_store.py`
  (`test_pasted_host_spaces_keep_the_saved_password`) and `test_lifecycle.py`.

  New connection… (the picker's menu and the palette's **New connection**)
  goes Home and opens the Add connection form once: `main.tsx` raises
  `addConnection`, `Connections` (`addRequest`, `onAddHandled`) opens the form
  and clears the request.
  Connection form (`Connections.tsx`): a new connection's name is
  `defaultConnectionName(user, host, port)`, `user@host` plus `:port` when the
  port is a valid port other than 3306, kept in step with User, Host and Port
  until the name is edited or a preset or saved connection is loaded. Names
  compare after `trim().toLowerCase()`: when another saved connection has the
  same name, `#connection-name-problem` says "A saved connection named NAME
  already exists.", the field is `aria-invalid` and Save is disabled; editing a
  connection may keep its own name. `/api/connection/test`
  (`ui._test_connection_response`) opens one `MySQLConnection`, reads
  `server_version()` and `server_version_comment()` (identity through
  `server_info.product_identity`), then `query_rows("SHOW DATABASES")` in the
  same request, and returns `version`, `engine`, `flavor`, `product`,
  `schemas` and `databases` (the same list, at most `_TEST_DATABASES_LIMIT`,
  1,000) and `databases_truncated`, plus `databases_error` only when SHOW
  DATABASES failed after the connection succeeded (password redacted; the test
  still succeeds) and `through_ssh` only for a connection through SSH. With
  `databases`, Default database is a pick list (`mysql`, `sys`,
  `performance_schema` and `information_schema` behind **Show system
  databases**, and **Type a name…** back to the text field with its datalist);
  with `databases_error` it stays a text field with `#connection-schema-hint`
  saying why. Tests: `test_ui.py`
  (`test_connection_test_databases_bounded_and_error_path`),
  `test_workspace_api.py` (`test_connection_and_preset_routes`),
  `apps/workspace/test/connections.mjs`.

  Home connections, the picker pill and the form (`Connections.tsx`,
  `style.css`; no endpoint or JSON change): `SafetyTags` is the type pill
  (`.type-tag.is-production` purple, `.is-development` teal) plus, when set, a
  Read-only outline pill; neither truncates (`.safety-tags` does not wrap in
  the table), and the Home table, the picker and the Audit log's Type column
  share it. The table's columns are select (48 px), Name, Type (184 px), the
  user@host:port address (`flex: true`, the leftover width), **Default
  database**, Password and Last used; an empty database and a connection not
  used in this window show "—" with the reason as `title` and in visually
  hidden text ("Not used in this window. Earlier use is not recorded."). The
  compact preset keeps a short type word beside each name
  (`.connection-type-short`: Prod or Dev; a read-only connection keeps its
  word with an outline, as in the picker). The form panel caps at 560 px, has Cancel beside
  Save connection, and Delete connection is a `text-button danger`. The
  picker pill hides `.picker-target` at 1,100 px and below, and
  `.safety-tags` there in the top bar (`.window-toolbar`) and everywhere at
  650 px and below, showing `.connection-pill-type` in their place: `aria-hidden` (the Popover label keeps the
  full type), Prod or Dev in `--env-prod-text` or `--env-dev-text`, never a
  status color, and for a read-only connection the same word with a
  `currentColor` outline. The Session password field commits on **Set**
  (`.picker-password-set`, disabled until the value differs from the committed
  one), Enter or blur, all through the same `commitPassword`, with the hint
  "Set, Enter or leaving the field applies it for this window. It is never
  saved." Tests: `connections.mjs`, `profiles.mjs` (720 px pill, Set and the
  hint), `kit-static.mjs` (the two pills, the header, the "—" cell).

  Connection type on screen: `ConnectionTypeStrip` is a 3 px decorative strip
  (`aria-hidden`, no text, `pointer-events: none`) fixed to the window's top
  edge inside `.topbar-stack` on the places that show `SafetyStatus` (Query
  editor, Query digest, Database diagram, Schema versions, Server, Workload,
  Replication, Cluster, ProxySQL, Tuning), colored by `--conn-prod-strip`,
  `--conn-dev-strip` or `--conn-readonly-strip` in both appearances; the type
  word stays in the window footer. `ConnectionTypeBand` (`ConfirmSheet.band`)
  heads every write review and confirmation sheet (`ActionReview.tsx`,
  `ReviewedSqlAction.tsx` and Instruments through it, `MassKillDialog.tsx`,
  `Locks.tsx`, `Config.tsx`, `Accounts.tsx`, `Maintenance.tsx`,
  `TableData.tsx`, `QueryLab.tsx`, `Replication.tsx`, `TableAlterDialog.tsx`)
  with the type word and the connection label ("Production · prod-eu
  (10.0.0.5:3306)"), styled by `--conn-*-band-bg`, `-text` and `-line`. It
  names the connection the plan is bound to (`plan.connection`,
  `via.connection`, `session.connection`), never the picker's later choice.
  Tests: `config.mjs`, `locks.mjs`, `navigation.mjs`.

#### SSH tunnels

A connection (its settings, a profile through `PROFILE_FIELDS`, the session's
connection in use) may carry `ssh_host`, `ssh_user` and `ssh_port`;
`connection_options` outputs them only when set. Code: `myflames/ssh_tunnel.py`,
wired through `connector.py`, `live.py`, `lifecycle.py`, `workspace_store.py`,
`dbops.py`, `ui.py`, `workspace_cli.py`, `replication.py`, `error_log.py`,
`editor_session.py`, `Connections.tsx` (`SshSettings`), `binding.ts` and the
desktop launcher.

- Validation: `ssh_tunnel.ssh_fields` (host `^[A-Za-z0-9_][A-Za-z0-9._-]{0,252}$`,
  user `^[A-Za-z0-9_][A-Za-z0-9._@-]{0,127}$`, so neither starts with `-` nor
  holds spaces, quotes, `%`, `:`, `/` or `;`; port 1 to 65535; pasted spaces
  trimmed; a user or port without a host refused) and
  `ssh_tunnel.check_connection` (the database host must be a host name or an
  IP address for `-L`, IPv6 bracketed; no socket; no VERIFY_IDENTITY, since the
  client would check 127.0.0.1). `sshProblem` in `Connections.tsx` is the
  client-side twin; Use refuses while it reports a problem.
- Process: `ssh -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o
  LogLevel=DEBUG1 -o UpdateHostKeys=no -o PermitLocalCommand=no -o
  ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3`, plus
  `-o ControlMaster=no -o ControlPath=none` on POSIX (no shared master outlives
  myflames), `-p PORT` and `-l USER` as separate arguments when set, then
  `-L 127.0.0.1:LOCAL:DBHOST:DBPORT -- TARGET`. `LogLevel` on the command line
  wins over any ssh_config setting; ClearAllForwardings is never passed (it
  would clear the tunnel's own `-L`), and there is no Unix-socket forward (the
  MySQL client treats a socket as secure transport and would send the password
  in clear). It is an argument list (no shell) with stdin from `/dev/null`, in
  its own process group (a POSIX session; on Windows a new process group
  without a window), so a terminal Ctrl+C cannot cut the tunnel under the KILL
  QUERY that cancels a statement. ssh gets an allowlisted environment (PATH,
  locale, temp and system roots) plus the real home (`MYFLAMES_SSH_HOME`) and
  agent socket (`MYFLAMES_SSH_AUTH_SOCK`, else `SSH_AUTH_SOCK`), no `DISPLAY`
  or `SSH_ASKPASS`, and never a database password (that stays in the 0600
  client option file). Keys and the agent only: SSH passwords are never
  accepted or stored, and myflames never accepts a host key (the user's own
  `StrictHostKeyChecking` still applies). `find_ssh` never uses
  `shutil.which`: on Windows it takes `%SystemRoot%\System32\OpenSSH\ssh.exe`,
  then `Sysnative`, then only `ssh.exe` in absolute PATH folders with a drive
  or UNC share (quotes stripped); on POSIX PATH first, then `/usr/bin`,
  `/usr/local/bin`, `/opt/homebrew/bin` and `/opt/local/bin`. Empty, `.` and
  relative PATH entries are skipped everywhere; otherwise "ssh was not found".
  The host, user, database name and IPv6 patterns end with `\Z`, so a trailing
  newline never reaches `-L`.
- Watchdog (POSIX, `myflames/ssh_watchdog.py`, stdlib only and importing
  nothing from myflames): ssh starts as `python -I ssh_watchdog.py -- ssh ...`
  (the packaged backend: `myflames-backend __myflames_ssh_watchdog -- ssh
  ...`, dispatched at the top of `backend-entry.py` before the CLI loads). The
  watchdog leads the tunnel's process group with a stdin pipe only the opening
  process holds, starts ssh in that group with stderr passed straight through,
  exits with ssh's status, forwards SIGTERM, SIGHUP and SIGINT (SIGKILL after
  `GRACE`, 2 s), and when the pipe closes (the owner ended, SIGKILL included)
  stops ssh and kills what is left of the group, such as a ProxyCommand.
- Job object (Windows, `myflames/win_job.py`, stdlib only: ctypes and the
  documented kernel32 calls `CreateJobObjectW`, `SetInformationJobObject` with
  `JobObjectExtendedLimitInformation`, `SetHandleInformation`,
  `AssignProcessToJobObject`, `CreateToolhelp32Snapshot`/`Thread32First`/
  `Thread32Next`, `OpenThread`, `ResumeThread`, `CloseHandle`; nothing runs at
  import). `TunnelManager._spawn` starts ssh.exe with `CREATE_NEW_PROCESS_GROUP
  | CREATE_NO_WINDOW | CREATE_SUSPENDED` and no watchdog, then `_join_job`
  calls `win_job.JOB.adopt(popen)`: `KillOnCloseJob.handle()` creates the
  process's one job once with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and a
  non-inheritable handle that myflames never closes (a failed setup closes the
  empty job and is tried again next time), `adopt` assigns `popen._handle` and
  then resumes the threads of `popen.pid`, raising `OSError`. When the process
  ends in any way (a normal exit, a crash, `taskkill /F`), Windows closes the
  handle and ends every ssh in the job with what it started. When creating the
  job, assigning or resuming fails, ssh is killed and waited for (2 s) before
  it ran, its pipes closed, and the tunnel refused: "Windows could not tie the
  SSH tunnel to myflames (job object: REASON), so ssh was stopped before it
  connected. Nothing was sent." An interrupt (`KeyboardInterrupt`) while
  ssh.exe waits suspended also kills it first, then is raised again. Struct
  layouts are plain ctypes types, so tests check them on any OS (64-bit: basic
  limit 64 bytes, extended 144, THREADENTRY32 28). A process started from an
  SSH session is already in sshd's session job (no kill on close), and the
  assignment nests ssh.exe under it (Windows 8 and later); in the desktop app
  libuv's job for the backend allows silent breakaway, so ssh.exe is in no job
  before the assignment. The desktop's Windows stop (`taskkill /PID backend /T
  /F`) is unchanged; the job covers a backend crash, a backend killed by itself
  or the user, and an Electron crash (libuv's kill-on-close job ends the
  backend, whose job then ends ssh). Windows tunnels are not recorded in
  `ToolGroups` (process groups are POSIX). A normal close terminates only
  ssh.exe, so a ProxyCommand it started stays in the job until the backend
  exits; a backend that dies between starting ssh.exe and assigning it leaves a
  suspended ssh.exe that never ran (no port, no connection). Dump, restore and
  clone tools are not in the job and still outlive a crashed backend on
  Windows. Windows ssh.exe (OpenSSH_for_Windows 9.5p2 on Windows 11) reads
  `~/.ssh/config`, keys and `known_hosts` from the account's profile folder and
  ignores `HOME` and `USERPROFILE`; the Host alias list (`config_hosts`,
  `/api/ssh/hosts`) reads `MYFLAMES_SSH_HOME`, the same folder in practice. The
  environment allowlist never passes Win32-OpenSSH sshd's
  `C28FC6F98A2C44ABBBD89D6A3037D0D9_POSIX_FD_STATE`, which makes an inheriting
  ssh.exe hang before it signs in. Readiness is unchanged there (DEBUG1 lines
  end in CRLF, stripped). Tests: `test_ssh_tunnel.TestWindowsJob` (mocks, any
  OS) and `TestWindowsJobLive` (Windows only, real kernel32).
- Readiness and errors: the local port comes from binding 127.0.0.1:0. The
  tunnel is ready only when ssh's own DEBUG1 lines on stderr say, in order and
  within `READY_TIMEOUT` (15 s) while ssh still runs, `Local forwarding
  listening on 127.0.0.1 port LOCAL.`, then `channel N: new port-listener
  [port listener]` (`channel N: new [port listener]` before OpenSSH 9), then
  `Entering interactive session.`. No TCP probe is made, so another local
  program listening on the port first never gets a lease (and nothing reaches
  the database while the tunnel opens); a session entered without this
  forward (ClearAllForwardings in ssh_config) fails at once naming that
  setting, and an ssh that never confirms times out and is stopped. Debug
  lines are matched and dropped: `stderr_text()` keeps only other lines and
  `failure_message` ignores them. A lease whose ssh ended is never reused
  (`Lease.alive()`), and the connector's legacy option file never names the
  port of a dead tunnel. A forward that failed to bind is retried once on a
  new port. ssh's messages become plain
  sentences (unknown host key: "Run `ssh X` once in Terminal to check and
  accept it"; changed host key; Permission denied for keys or agent; could not
  resolve; refused; timed out; no route; local port in use; the hint for an
  unknown host key says `ssh -p PORT HOST` for a non-default SSH port),
  otherwise the last non-debug line of its output with terminal controls
  escaped. When the forward cannot
  reach the database (ssh logs `open failed`), `MySQLConnection.run` adds "SSH
  host X could not reach the database at H:P (reason)", "does not allow port
  forwarding (AllowTcpForwarding)" or "could not resolve the database host" to
  the client's error.
- Sharing and lifetime: `TUNNELS` (`TunnelManager`) keys tunnels by SSH host,
  SSH user, SSH port, database host and database port, and leases one per
  client invocation, editor session (held for the session) or lifecycle tool
  run. Every client path (`MySQLConnection._invocation_defaults`, the editor
  session, `lifecycle._credentials`) writes `host=127.0.0.1`, the tunnel's port
  and `protocol=TCP`; a connector with `ssh_host` and no tunnel refuses to build
  an option file (fail closed). A tunnel closes after `IDLE_SECONDS` (300)
  without users and at shutdown. `WorkspaceServer.server_close` starts a timer
  that runs `TUNNELS.close_all()` after `WorkspaceServer.TUNNEL_CLOSE_SECONDS` (3 s) even while
  the editor and lifecycle cleanup (which may still send KILL QUERY through a
  tunnel) runs on, and calls it again in a `finally`, so a failed cleanup never
  skips it; `close_all` signals every tunnel first, then waits, and a second
  caller waits for the first. The CLI closes them at exit: `cli.main()` first
  calls `_close_on_termination_signals()`, which turns SIGTERM and SIGHUP into
  `SystemExit(128 + signal)` (main thread only, only for a signal still at
  `SIG_DFL`), so atexit runs and the status is 143 or 129. The MCP server
  installs no handlers; the watchdog (POSIX) or the job object (Windows)
  covers it. After the desktop
  died and the backend's shutdown hung for 5 s, `backend-entry.py`
  (`_kill_owned_groups()`) SIGKILLs every group the backend owns
  (`TUNNELS.owned_groups()`: each tunnel's group and every group of the
  attached ToolGroups, read under a 0.5 s lock bound with
  `ToolGroups.snapshot(timeout=)`), never its own, before it SIGKILLs its own
  group. At most `MAX_TUNNELS` (8) are open, idle ones evicted first, and
  tunnels count toward `lifecycle.ToolGroups.MAX_GROUPS` (16, the same number
  as `MAX_TOOL_GROUPS` in `apps/desktop/backend.cjs`) with dump, restore and
  clone tools: `start()` refuses another one with `ToolLimit` and spawns
  nothing. A tunnel whose ssh died starts again on a new port on the
  next use, never under a running statement. It listens only on 127.0.0.1:
  another local user can reach that port while it is open, as with any
  `ssh -L`, and still needs database credentials; VERIFY_CA through the tunnel
  protects against a port taken over after ssh dies.
- Identity: `dbops.connection_identity` appends `["ssh", host, user, port]`
  after the six endpoint fields only when SSH is set (`identity_endpoint`
  keeps it); `workspace_store.ENDPOINT_FIELDS` and the saved-password
  fingerprint include the SSH target only when set; `connectionKey` and
  `endpointKey` in `binding.ts` do the same. Identities without SSH are
  byte-for-byte unchanged. A saved password is released only when the SSH
  target matches the profile, and a Development type falls back to Production
  the same way.
- CLI: `profiles set NAME --ssh HOST [--ssh-user USER] [--ssh-port PORT]` or
  `--no-ssh` (`WorkspaceStore.set_profile_ssh`); a changed tunnel points the
  profile at another endpoint, so its saved password is forgotten and it becomes
  production unless `--type` is given. `--ssh-user` and `--ssh-port` need
  `--ssh` ("--ssh-user and --ssh-port need --ssh HOST", exit 2), and `set` with
  nothing to change says "choose --type, --read-only, --read-write, --ssh or
  --no-ssh". `profiles list` shows `host:port via user@sshhost:port` in HOST (user and SSH port only when set);
  its JSON carries the fields.
- Workspace: `POST /api/ssh/hosts` returns `ssh-hosts-1.0` `{hosts, ssh_found,
  message}`: the Host aliases of `~/.ssh/config` and its Includes (at most 32
  files of 256 KiB, depth 4, 500 names; no patterns, negations or names the
  field refuses). It runs no ssh and reads no network. `/api/connection/test`
  adds `through_ssh: "Through SSH host X"`. `SshSettings` is a plain `<details
  data-section="ssh">` (Connect through SSH), open for a connection through SSH;
  its SSH host field offers the aliases, read once when the section first
  opens. `connectionTarget` and `connectionLabel` add "via HOST", and
  `ConnectionTypeBand` uses that label, so every write review names the SSH
  host ("Prod (app@127.0.0.1:3306 via bastion)").
- Locality and CLONE: `lifecycle.refuse_clone_through_ssh` refuses CLONE when
  the donor or the recipient uses SSH (the recipient connects to the donor
  itself), and Clone's saved-donor list leaves such connections out; the
  MariaDB error-log file read, replication loopback source matching
  (`replication.reached_host`) and the physical backup locality check treat
  SSH as remote. A restore's typed target names the SSH target
  ("127.0.0.1:3306/shop via deploy@bastion").
- Tests: `test/test_ssh_tunnel.py` (`TestReadiness`, `TestOwnerExit`,
  `TestWorkspaceShutdown`, `TestDesktopEntry` and the Windows lookup tests; the
  fake ssh `test/_fake_ssh.py` logs what OpenSSH logs at DEBUG1; the live sshd
  test runs only with `MYFLAMES_SSH_LIVE=1`),
  `apps/workspace/test/connections-ssh.mjs` (run after `npm run build` with
  `node test/connections-ssh.mjs`; it also renders `ConnectionTypeBand` with
  `react-dom/server`), `apps/desktop/test/backend.test.cjs` (a full list of
  tool groups is read, one more is not).

### Write safety and audit

`myflames/dbops.py` is the one safety layer for every statement that changes
server state (db-operations skill). Editor run reviews also bind the **On error** mode through `editor_session._review_digest`: a Stop review cannot authorize Continue, or the reverse; changing the mode requires a fresh review. Its parts:

- `classify(sql, backslash_escapes=None, key_columns=None)` returns
  `{kind: read|write|risky, category, reasons[], statements: [{text, start, end,
  verb, kind, category, reasons}], count, ambiguous}`. `split_statements` splits
  like the mysql client on top of `live.TOKEN` / `TOKEN_NO_BACKSLASH` (no second
  tokenizer; `TOKEN` reads `-- ` as a comment only before the client's ASCII
  white space, never Python's `\s`): comments, quoted strings and backtick
  identifiers, executable comments `/*! */` and `/*M! */` whose bodies are code
  (a delimiter inside splits, as `client/mysql.cc` does), `DELIMITER` lines at a
  line start with nothing pending (any first word starting with `delimiter`, as
  in `--binary-mode`; the argument parsed like the client's `get_arg`: quotes
  stripped, an unquoted one ends at a space, not a tab; a missing argument or
  one with a backslash keeps the old delimiter and is risky), and the client's
  backslash commands outside quotes (`\g` and `\G` end a statement, `\N` is
  NULL, any other is risky: `REASON_CLIENT_COMMAND`). `--` before another
  control character is risky (`REASON_DASH_CONTROL`; the client reads code,
  the server a comment). Each client statement is also cut at top-level `;`
  and the worst part counts (`REASON_MULTI`): under a custom delimiter the
  server runs every `;`-separated statement of one request. Statements carry
  `issues`. Without `backslash_escapes` both string readings are classified
  and the worse wins (`ambiguous`, reason `AMBIGUOUS_REASON`). An unclosed
  quote or comment is risky. Decisions:
  reads are SELECT, WITH ... SELECT, TABLE, VALUES, SHOW, HELP, DESCRIBE/DESC,
  EXPLAIN without ANALYZE, USE, CHECKSUM, transaction starts and ROLLBACK,
  UNLOCK, and `SET NAMES`, `SET [SESSION] TRANSACTION` (not READ WRITE), SET
  SESSION of `SESSION_READ_VARIABLES` (NAMES, CHARACTER SET and CHARSET are
  one item of the SET list; every item counts; a value that calls a function,
  uses `:=`, contains a subquery (SELECT or TABLE) or advances a MariaDB
  sequence makes that item a write: `_SET_VALUE_WRITES` on the scanner code,
  where a name of identifier characters (`_IDENT_CHAR`: `\w`, `$` and any
  non-ASCII character, non-ASCII spaces included, as both lexers accept; the
  scanner collapses only ASCII white space), a backtick name or a masked
  string (an ANSI_QUOTES `"f"`) before `(` is a call, plus `NEXT VALUE FOR` and a qualified `NEXTVAL` or
  quoted part (sql_mode=ORACLE `seq.NEXTVAL`); MariaDB `SET STATEMENT ... FOR`
  values get the same test. Only myflames' own text value, exactly
  `CONVERT(X'...' USING utf8mb4)` as the whole value, is exempt). Writes: INSERT/REPLACE,
  UPDATE/DELETE whose WHERE pins a key column (`key_columns`, given by the
  caller) to a value (`col = literal`, `literal = col`, `<=>`, `IN (literal,
  ...)`, not negated, no OR or XOR; the check is one linear token pass over at
  most `MAX_WHERE_CHECK` characters), changes of transaction_read_only or
  tx_read_only, `SET TRANSACTION READ WRITE`, `START TRANSACTION READ WRITE`,
  `SELECT ... FOR UPDATE|FOR SHARE|LOCK IN SHARE MODE` (`locking_read`), `INTO
  OUTFILE|DUMPFILE` (`file`), `INTO @var` and `:=`, GET_LOCK, NEXTVAL, other SET
  SESSION and user variables (`session`), COMMIT, XA START/END/PREPARE, temporary
  tables, ANALYZE/CHECK TABLE, FLUSH (not TABLES). Risky: DDL, TRUNCATE,
  RENAME, OPTIMIZE/REPAIR, HANDLER OPEN (its metadata lock lasts until HANDLER
  CLOSE or the session ends; HANDLER READ and CLOSE stay reads), UPDATE/DELETE without WHERE, with an always-true
  one, or without key information (`key_columns` None), KILL, SET
  GLOBAL/PERSIST/PERSIST_ONLY, RESET, `SESSION_RISKY_VARIABLES` (sql_log_bin,
  binlog_format, gtid_next, pseudo_thread_id, skip_replication, wsrep_on,
  ...; also inside MariaDB `SET STATEMENT ... FOR`), DO, replication
  (START/STOP REPLICA|SLAVE|GROUP_REPLICATION|ALL SLAVES, CHANGE, PURGE), CLONE,
  accounts (GRANT, REVOKE, CREATE/ALTER/DROP/RENAME USER|ROLE, SET PASSWORD, SET
  DEFAULT ROLE), LOAD DATA/XML, LOCK TABLES/INSTANCE, FLUSH TABLES, XA
  COMMIT/ROLLBACK, CALL, EXECUTE, `BEGIN NOT ATOMIC`, INSTALL, SHUTDOWN, and any
  unknown verb (`unknown`). EXPLAIN ANALYZE and MariaDB `ANALYZE stmt` take the
  inner statement's verdict (they run it); MariaDB `SET STATEMENT ... FOR` too.
  The cautious native-function allowlist treats unqualified, unquoted `SLEEP`,
  `UUID` and `UUID_SHORT` as reads, including on read-only connections. This
  does not exempt `GET_LOCK`, `RELEASE_LOCK`, `BENCHMARK`, unknown functions,
  qualified/quoted calls, or nested calls that change state. Function names
  are read with `_IDENT_CHAR`, so `SELECT €()` and `CONCAT\u00a0(` (a
  no-break space is part of the name) are calls of unverified functions.
  A classification is a hint: a SELECT can still call a stored function that
  writes, so on a read-only connection the server enforces it too: run
  `READ_ONLY_SESSION_GUARD` (`SET SESSION TRANSACTION READ ONLY`, returned as
  `session_guard` by `decide`) in the session first; table writes then fail
  with 1792, and the statements that would lift it are writes.
- `decide(verdict, connection_type, read_only, name)` returns `{action:
  allow|confirm|refuse, kind, category, connection_type, read_only, tone:
  danger|primary, title, text, reasons, statements}`. Matrix: read allows
  everywhere; write confirms on production, allows on development; risky
  confirms on both; read-only refuses every write. `tone` is primary only when
  the kind is not risky and every non-read statement is in `session`,
  `transaction` or `locking_read`; within one kind `_worst` prefers a
  danger-tone category, so order never matters.
  `gate(decision, confirmed)` returns None or `("refused" | "needs_confirmation",
  text)`; write endpoints call it before opening any connection and require
  `confirmed: true` when the action is confirm (`/api/live/kill`'s confirmation
  is its `expect` identity). `connection_safety(profile, connection)` gives
  `{profile_id, profile_name, type, read_only, endpoint_matches}`: a request
  never lowers protection. An unsaved connection is production whatever it
  claims and may only ask for `read_only: true`; a profile's type applies only
  while the request keeps its `ENDPOINT_FIELDS` (host, port, user, TLS), else
  production; its read-only flag applies either way. A profile save that
  changes the endpoint without choosing a type makes it production.
  `connection_identity(connection, safety)` is the key reports and
  confirmations bind to, the Python twin of `connectionKey()` in
  `apps/workspace/src/binding.ts`: profile id, host, port, user, TLS mode and
  CA, then type and read-only only when not production and read-write (keys of
  default connections are unchanged). `report.flight_key` takes the same
  `safety`. Writes send with `WRITE_CLIENT_FLAGS` (`--comments`,
  `--binary-mode`, which turns off every client command except `\C` and
  DELIMITER in non-interactive use, on MySQL and MariaDB clients). All
  connector executions pass `connector.CLIENT_CHARSET_FLAGS`
  (`--default-character-set=utf8mb4` and `--init-command=SET NAMES utf8mb4`) to
  keep client byte encoding locale-independent and avoid MariaDB 10.11
  server-default collation fallback when negotiating an unknown utf8mb4 collation.
- Privileges (a hint, the server decides): `parse_grants` (global `ON *.*` and
  `ON db.*` grants; one SHOW GRANTS parser, also behind
  `locks.parse_kill_privilege`), `required_privileges(operation, engine,
  version, variable)` and `privilege_hint(...)` (`allowed` True, False or None)
  for `kill`, `set_global`, `set_persist`, `set_persist_only`, `reset_persist`,
  `set_session_restricted`, `replication`, `clone_donor`, `clone_recipient`,
  `write_when_read_only` and object privileges, per the db-operations table
  (MariaDB SUPER counts before 11.0, READ_ONLY ADMIN before 10.11;
  `ALLOWED_BY_SUPER_BEFORE_110000`). `read_grants(conn)` reads USER(),
  CURRENT_USER() and SHOW GRANTS, tagged, with `--comments`.
- Audit: table `audit` in `~/.myflames/workspace.sqlite3` (owner-only, columns
  `workspace_store.AUDIT_COLUMNS`: id, attempted_at, finished_at (ISO-8601 UTC),
  status `pending|ok|error|declined|refused|unknown`, outcome (operation status such as
  `killed`, `denied`, `gone`, `read_only`, `no_terminal`), surface `ui|cli`,
  os_user, operation (`kill`, `kill_query`, ...), profile_id, profile_name,
  connection_type, read_only, host, port, db_user, engine, server_version,
  statements (JSON list, as sent, redacted), target (the identity confirmed),
  verified, policy, error_code, message, rows_affected, after, duration_ms).
  `record_attempt` writes `pending` before sending (a failure raises and nothing
  is sent), `complete` finishes it; `record_outcome` writes declined, refused,
  or failed-before-sending attempts as one finished row in one transaction;
  `AuditTrail` wraps one write for callers (`attempt`, `complete`, `outcome`,
  `fail`, `pending`). `fail` after `attempt` finishes the record as `unknown`
  for an interrupt, a client time limit or client error 2013 (`may_have_run`:
  the statement may have reached the server), else `error`. Records left
  `pending` or finished `unknown` read as `outcome_unknown: true`. `redact` removes literals after IDENTIFIED BY / AS /
  USING, PASSWORD(...), SET PASSWORD, `*PASSWORD = '...'` (SOURCE_PASSWORD,
  MASTER_PASSWORD, START REPLICA PASSWORD, CREATE SERVER OPTIONS), REPLACE
  '...' after a redacted password, and the connection's own password anywhere
  (also with doubled or backslash-escaped quotes). Retention: 90 days by default
  (`settings` table, `audit_retention_days`, 1 to 3650), purged on every insert
  and when the setting changes. `audit-1.0` envelope: `{schema_version,
  retention_days, records[]}`; each record has a nested `connection` object.
  A write through SSH also records `connection.ssh` (only when set) in the
  `ssh` column, which `WorkspaceStore._add_audit_ssh` adds on demand without a
  `user_version` bump; every audit site passes `ssh_host`, `ssh_user` and
  `ssh_port` with host, port and user, and the Audit log inspector says
  Through SSH.
  CSV export flattens it, puts the `ssh` column last and quotes cells
  starting with `= + - @`.
- CLI: `python3 -m myflames audit [list|export] [--since DAYS] [--server NAME]
  [--json|--csv] [-o PATH] [-q]` (`list` is the default, `--limit N` default 50,
  text columns WHEN (UTC), STATUS, FROM, OPERATION, SERVER, STATEMENT, ID;
  `export` writes every matching record, JSON by default) and `audit purge
  --older-than DAYS [--yes]` (0 deletes all; asks on a terminal, refuses with
  exit 1 without one unless `--yes`; declined exits 1). Lives in
  `workspace_cli.cmd_audit`.
- Endpoints (token, no server connection): `POST /api/audit/list` (`since_days`,
  `server`, `status`, `limit` default 500, `before` a record id for the next
  page; every filter applies before the limit, keyset order attempted_at then
  rowid) returns `audit-1.0` with `has_more`; `/api/audit/export`
  (`format` json|csv, filters including `status`) returns `{format, content, mime, filename}`;
  `/api/audit/settings` (`retention_days` to change it) returns
  `{retention_days}`; `/api/audit/declined` (`operation`, `statements` as the
  confirmation sheet showed them, optional `target`, `connection`) records a
  cancelled confirmation as `declined` and returns `{audit_id, status}` (the
  workspace's Kill sheet calls it on Cancel and Escape through `declineKill` /
  `recordDeclined`, with the tagged KILL and the identity it would have sent). Write endpoints are listed in `ui._WRITE_PATHS`: they fill
  the saved password themselves, after the policy, and are never in
  `_SERVER_VIEW_PATHS`.
- Object change history (`POST /api/audit/object`, `ObjectChanges.tsx`; store
  queries in `workspace_store.py`, request checks in `dbops.py`): takes
  `{server, kind, key}` (plus optional `ssh`). `server` is the Audit log's
  `host:port` endpoint (at most 300 characters), matched case-insensitively
  against the recorded host and port, never a saved-connection label. `kind`
  is `parameter`, `account` or `index`: a parameter key is its exact name
  string (the `{name}` form is also accepted), an account key is `{name,
  host, role}` with `role` a strict boolean, an index key is `{schema, table,
  index}`. Names are at most 256 characters and cannot contain NUL; empty
  account names and hosts are retained where the account serializer permits
  them. Bad requests return the usual authenticated API 400. The response is
  `{schema_version: "audit-1.0", records}` with zero to five of the newest
  matching records, ordered by `attempted_at` descending then insertion order
  descending; only `ok` and `unknown` statuses qualify, and every filter
  applies before `LIMIT 5`. Parameters match `target.name` on the variable
  operation names (`set_global`, `set_persist`, `set_persist_only`,
  `set_session`, `reset_persist`); accounts match `target.account` through
  `accounts._name` for the recorded engine; indexes match `editor_run`
  records whose `target.alter_table` names that exact index (a statement
  recorded against the whole table never matches one index, so it is not
  attributed to one). No statement-text matching is used. The lookup is one
  local SQLite SELECT on the existing store, with no server connection and no
  SELECT when the store file does not exist; execution is interrupted at
  progress checkpoints after 250 ms or about 200,000 SQLite VM instructions,
  which produces a short local-history error pointing to the Audit log. The
  route shares the Host/Origin, session-cookie, token, POST allowlist,
  JSON-body and request-size checks of the other audit routes; no SQLite JSON
  extension is required. `/api/audit/list` and `/api/audit/export` accept an
  optional `id` (a 32-character lowercase hexadecimal audit id), applied
  before paging and export; audit navigation uses the existing
  `myflames-open-audit` event, selects and filters to that record, and offers
  **Show all records**. The inspector (`ObjectChanges.tsx`) shows only the
  newest record, with "Kept on this computer." and readable operation names (the raw operation key on hover): the stored redacted SQL in a box with Copy beside it, the relative
  attempted time with the exact timestamp on hover, the stored outcome, and
  **Open in Audit log**. For a known single-step parameter change, **Undo**
  pre-fills the existing variable sheet with the earlier value (`mode` and
  `value` only; the SQL is always built afresh by the normal variable
  planner, never replayed from a record) and is omitted for non-ok outcomes,
  account and index inspectors, compound SET PERSIST inverses, session
  changes and missing or redacted previous values (a recorded NULL value
  still gets Undo). Each inspector open reads once, and a completed parameter
  action refreshes its open inspector's local history. Tests:
  `test/test_audit_object.py`, `apps/workspace/test/object-changes-unit.mjs`
  and `object-changes-fixture.mjs`.
- Workspace (`Audit.tsx`, `audit.css`): only labels and words change on
  screen; stored values stay as recorded. Columns are Time, Status,
  Operation, Connection, Type (`SafetyTags`), Statements, Error, Duration;
  Type and Duration are `detailColumns`, dropped by the compact preset when the
  view is narrower than 960 px. Times are local (`when`) with
  `"<stored value> (UTC)"` as `title`, in the table and the inspector's
  Record section. The inspector maps stored outcome keys (`OUTCOME_WORDS`:
  `timeout` Timed out, `gone` Already ended, `unverified` Could not verify,
  and so on) and policy values (`POLICY_WORDS`: `confirm` Requires confirmation,
  `risky` Risky, `production` Production) to words, with `Stored as "KEY"`
  on hover; sections share one label column; each statement has its own
  Copy (copied as stored). The policy's `connection_type` is labelled
  **Judged as** (`POLICY_LABELS`). It can differ from the record's
  `connection.type` (the saved connection's type) because
  `connection_safety` judges a connection used with other settings than the
  saved ones as Production; the inspector then says "The connection is
  Development; this write was judged as Production." with that reason. The
  About ⓘ sits in the summary line once records are listed and keeps its
  toolbar place while the log is empty. Tests: `apps/workspace/test/audit.mjs`
  (column order, Type pills, inspector words and hovers, Judged as, local
  times, statement Copy, the ⓘ).
- Tests: `test/test_dbops.py` (classification table, splitting, both
  backslash readings, policy matrix, privileges on both engines, redaction,
  audit lifecycle with a crash left pending, retention and purge, export,
  migration, CLI and endpoints) and `TestKillAudit` in `test/test_locks.py`.
  Every test that reaches the store uses a temporary HOME (`LockCase.run`
  wraps each lock test in `private_home()`).

### SQL CLI

`myflames/sql_cli.py:cmd_sql` is dispatched by `cli.main` as `myflames sql`.
Input is mutually exclusive `-e/--execute SQL`, `-f/--file FILE` (UTF-8; `-` means
stdin), or stdin by default. Terminal stdin without a source is bad input.
Every command reads and writes UTF-8 on redirected standard streams: `cli.main`
calls `_utf8_standard_streams`, because Windows gives a redirected stream the ANSI
code page (output with other characters crashed, and piped UTF-8 SQL or
`--password-stdin` was misread). A console keeps Python's own Unicode path.
The editor's 1,000,000-character script cap applies before connection.

Connection flags come from `workspace_cli._add_connection_args`, with deferred
`_resolve_connection` for profiles, saved passwords and `-p`. The adapter reads
profile safety and calls `editor_session._plan_run`, `_decision` and `_gate`
before password resolution or connection. Confirmation uses the editor's exact
statement digest, expiring single-use token, and decline path. Every non-terminal write refuses without `--yes`; terminal stdin prompts on
stderr with default No when the policy requires confirmation. `--yes` bypasses the prompt; read-only still refuses.
A changed endpoint, default database or profile safety during password resolution
refuses before opening a client. Once connected, the script is re-planned against
the session's actual sql_mode; changed statements or policy need authorization
again. `EditorSession.start_run(Run(...))` executes the script in one client,
without helpers between user statements (the reviewed ALTER guard is an explicit
statement, included in results and audit). Writes use `AuditTrail(operation="sql",
surface="cli")`, including refused and declined attempts; no editor history entry
is created. Production safe updates, read-only session guards, truncation,
KILL QUERY cancellation and unknown write outcomes come from the editor engine.
The session is closed in `finally`; no implicit COMMIT, no reconnect.

Flags: `--format table|csv|json|tsv|markdown|insert` (default table), `--json` (overrides
`--format`), `--limit N` (1 to 100,000; default 1,000), `--timeout S` (finite 0 to
86,400 seconds; default 0 disables it), `--yes`, `--confirm-target NAME`,
`--insert-table NAME` (default `result`), `-o/--output PATH`, `-q/--quiet`. Results are stdout unless `-o` is set;
messages, truncation notices and server errors are stderr. Errors retain the
statement number, SQLSTATE, server code and editor line/column position. Quiet
suppresses summaries and, with `--yes`, the confirmation description; errors
and truncation notices remain visible.

Both JSON options emit one envelope for runs with results or statement outcomes,
including statement errors and partial runs:
`{"schema_version":"sql-1.0","status":...,"results":[],"counts":null,
"error":null,"stopped":null,"message":null,"audit_id":null}`. `status` is
`complete`, `error`, `session_ended`, `stopped`, `refused` or `declined`.
Completed runs reuse the editor's ordered result records (`rows`, `ok`, `error`)
and counts; `statement` is zero-based, errors include `position`. No connection
credentials or raw script are added. NULL is `null`; values with unknown column types remain strings. When numeric column metadata is supplied, small integer types within JavaScript's safe integer range and finite FLOAT/DOUBLE/REAL values become JSON numbers; BIGINT, DECIMAL, binary values, and values outside that range remain strings to preserve precision. Live editor XML currently provides no type metadata, so those values remain strings.
Duplicate column names remain ordered columns plus row arrays. CSV, TSV and
Markdown use `export_result`; `insert` uses `export_result("sql", result, table=args.insert_table)`
to emit INSERT statements into the user-named table (default `result`). The name
is validated before execution and quoted as one identifier, including any dots.
It shares the UI exporter's identifier quoting and literals: typed binary columns
use `X'..'`, hex-looking text without binary metadata stays quoted text, NULL
stays NULL, and empty result sets emit no INSERT. Table mode JSON-quotes text to
distinguish SQL NULL from empty text and the literal string NULL; values in known numeric columns are unquoted. INSERT also leaves known numeric values unquoted; untyped numeric-looking text remains quoted. Non-JSON result
sets are concatenated in execution order, each with its own headers or INSERT target. JSON retains boundaries
and metadata, including truncation. Credentials and SQL password literals are
redacted on both output streams and in audit records.

Exit codes: 0 complete, 1 statement failure or stopped run, 2 invalid/unreadable
input, connection failure (including an ended connection during a run), or any
failure/refusal/decline before sending a statement, including failure to create
a pre-send audit record. Pre-send failures write a diagnostic to stderr only:
stdout stays empty, even with either JSON option or `--quiet`, and no output
file is created. Errors after statement outcomes retain the JSON envelope and
any preceding results on stdout, including connection loss with exit 2. Tests: `HOME=$(mktemp -d) MYFLAMES_SECRET_BACKEND=vault
python3 -m unittest discover -s test -p 'test_sql_cli.py' -v` uses the editor's
fake MySQL/MariaDB clients, not a live server.

CLI SQL confirmations record the actual refusal, decline, or execution outcome. Preparing the confirmation is not itself an audited refusal, including with `--yes`.

DROP DATABASE/SCHEMA uses `dbops.cli_confirm(expected=target, supplied=args.confirm_target)`;
`--yes` alone never satisfies a typed guard. The comparison preserves case and
spaces. Pre-send refusal/decline remains exit 2 with no stdout or output-file
changes. `_emit` builds the payload separately from diagnostics and passes it to
`cli._write_output`; this implements cli-ux's output vocabulary, while
`--insert-table` implements query-editor section 10's user-named INSERT target.
ALTER opens a session for its read-only preflight before prompting; read-only,
script-shape and non-TTY/`--yes` checks still precede credentials. The final
policy read also covers edits during that review. The confirmation on stderr
prints `myflames: ALTER predicted: SUMMARY. TEXT REASON` (or the reason it was
not predicted) before `myflames: ALTER preflight: {json}`, whose JSON carries
the same `prediction` as the workspace review; no flag or help text changed.

### Query editor sessions

`myflames/editor_session.py` runs any SQL from a Query editor tab in that
tab's own server session (query-editor skill). `myflames sql` uses the same
planner, write gate and session engine from the CLI.

- Process: one `mysql`/`mariadb` client per tab, stdin kept open, started by
  `EditorSession.start` with the connector's 0600 `--defaults-file`
  (deleted once the client has connected; the password never reaches argv) and
  `--xml --binary-as-hex --binary-mode --comments --force --quick --unbuffered
  -vv --skip-reconnect --default-character-set=utf8mb4` (plus `--skip-commands`
  when the client's `--help` lists `--commands`). `--force` is required: the
  batch client exits at its first error otherwise, which would end the session;
  myflames itself stops a script at the first error by sending the next
  statement only after the previous one succeeded. Ended on `/close`,
  connection switch (a request for another identity is refused, so the UI
  closes first), idle timeout (`EditorSessions.reap`, default 30 minutes,
  `idle_minutes` 1 to 1440), and server exit (`WorkspaceServer.server_close`
  calls `EditorSessions.close`; an `atexit` hook kills leftover clients). Never
  reconnected: an ended session answers runs with `status: "session_ended"`
  and `SESSION_ENDED_TEXT` until `/close` or `/open`.
- Client: opening a session uses the connector's per-server choice and keeps the binary paired with its engine-specific option file. The handshake remembers valid endpoint engine/version facts. The tab pins its resolved client for plan and KILL QUERY helpers even after Settings changes; each helper rechecks trust before starting. New sessions use the current choice. Authentication-plugin connection failures preserve `client-auth-plugin`. Tests: `test/test_editor_session.py` (`ProcessTests`, `PinnedClientTrustTests`) and `test/test_connector.py` (`TestVersionAwareClients`, `TestClientSecurityFixes`).
- Framing: a random client delimiter is set once (`DELIMITER __mf<hex>_`);
  each statement is written as `<text>\n<delimiter>\ndelimiter\n`. The bare
  `delimiter` line makes the client print `ERROR at line N: DELIMITER must be
  followed by ...` on stderr without contacting the server; N is the line
  myflames wrote it on, so it ends the statement exactly and adds no server
  statement between user statements (ROW_COUNT(), FOUND_ROWS(), SHOW WARNINGS
  and GET DIAGNOSTICS keep working within a run). Server errors carry the line
  their statement started on, which attributes them. stdout and stderr are
  separate pipes, each read by its own thread: the MariaDB client writes an
  error before flushing stdout, so a merged stream could misorder them. On
  stdout, `-vv` echoes each statement between `--------------` lines; the echo
  is skipped by its line count (`Step.echo_lines`), then come XML result sets
  (parsed by structure: values are XML-escaped, so `</field>`,
  `  </row>` and `</resultset>` cannot occur inside them; `xsi:nil` is NULL),
  `N rows in set[, W warnings]`, `Query OK, N rows affected[, W warnings]` and
  the server's info line. A statement's stdout block ends at the next echo; the
  run's closing helper (a tagged status SELECT) closes the last one. Leading
  `--`/`#` comment lines are dropped before sending (the client would send them
  as a statement of their own) and trailing white space is trimmed (the client
  does that too).
- Planning (`plan_statements`): `dbops.split_statements` with the session's
  backslash reading (`@@sql_mode`, read at start and after every run); a
  statement with `unclosed` or `issues` (client backslash command, also inside
  a `/*+ */` optimizer hint, which the client does not treat as a comment; `--`
  before a control character; unusable DELIMITER), a NUL, a leading `delimiter` word,
  or dollar-quoted text (`$tag$`, read as a string by MySQL 8.1+ clients) is
  refused with `status: "invalid"`; so is a statement whose quotes read
  differently under NO_BACKSLASH_ESCAPES after a statement that may change
  `sql_mode` in the same run. `selection: {start, end}` runs only that part.
  Offsets in requests and responses are UTF-16 code units (JavaScript string
  indexes; `Offsets`).
- Values: NULL is `null`; everything else is the client's text (big integers,
  DECIMAL, dates as printed); binary columns print as `0x` plus upper-case hex
  and the column gets `hex: true` when every non-null value has that form. It
  is a guess (a text column holding such strings looks the same): SQL export
  writes `X'..'` only for columns the caller marks `binary: true`. Column types are `null`:
  the XML carries none and `--column-type-info` does nothing with `--xml`. An
  empty result set has no column names in the XML (`columns_known: false`).
- Limits: `limit` (default 1,000, up to 100,000) rows per result set; the next
  row past it marks the result `truncated`. Only a statement that is one plain
  read (classified `read`, one server statement) is stopped with
  `/*myflames:editor*/ KILL QUERY <id>` from a second connection
  (`stopped: "row_limit"`, its 1317 shown as `interrupted`, not a failure);
  anything else (CALL, several statements under a custom DELIMITER, a write)
  runs to its end and the rows past the limit are read and dropped
  (`"row_limit_read"`), so a KILL never undoes a write. Also cut: more than 6 Mi
  characters of values per run (under the 12 MiB request limit, so a shown
  result can be sent back for export) or one row over 4 Mi characters
  (`size_limit`, `size_limit_read`). While a run is running, status responses
  carry no rows. Load more re-runs with a larger limit. Stop
  (`EditorSession.stop`) sends the same KILL for the running user statement;
  `stopped` is true only when the KILL was sent. A statement it interrupts
  (1317 or 3169) is `stopped` and undone; one that ended normally is `ok` and
  took effect, and later statements do not run. A statement still running
  `STOP_GRACE` (15) seconds after a KILL ends the session (`unresponsive`).
  `force: true` ends the session instead. KILLs and new statements are
  serialized (`send_lock`) and a KILL is never sent while a helper runs, so it
  cannot hit another statement; both servers also clear a KILL QUERY that
  lands between statements at the next command. `timeout` (seconds) stops a
  run the same way. The `CONNECTION_ID()` read at start is checked after every
  run; a different one ends the session. A statement's error text never ends
  the session by itself (server messages quote table data): a lost connection
  (1053, 1927, 2006, 2013, 2055, 4031) ends it when the run's status helper
  fails with one, or when the client exits. stderr: one server error per
  statement; every following line belongs to its message until the exact
  `ERROR at line <n>: DELIMITER` line of that statement.
- Transactions: after each run `autocommit`, `sql_mode`, `sql_safe_updates`
  and the transaction state are read with `/*myflames:editor*/` helpers:
  MariaDB `@@in_transaction`; MySQL the `STATE` of
  `performance_schema.events_transactions_current` for
  `PS_CURRENT_THREAD_ID()` (`in_transaction: null` with a `transaction_note`
  when the consumer, the `transaction` instrument or the thread's
  instrumentation is off, or the table cannot be read). These helpers make
  ROW_COUNT() and SHOW WARNINGS in the next run describe them. `/commit` and
  `/rollback` send tagged COMMIT and ROLLBACK; `/close` with an open or
  unknown transaction answers `transaction_open` or `transaction_unknown`
  unless `discard: true`. A statement that commits implicitly (DDL except
  CREATE/DROP TEMPORARY TABLE, GRANT/REVOKE, LOCK/UNLOCK TABLES, START
  TRANSACTION, BEGIN, SET autocommit = 1, ANALYZE/OPTIMIZE/REPAIR/CHECK TABLE,
  FLUSH, RESET, replication statements; `implicit_commit`) while a transaction
  is open or unknown always asks, also on Development, with `warnings`.
- Safety: every run is classified per statement with `dbops.classify` (the
  session's backslash reading; before the session exists both readings) and
  judged by `dbops.decide` with `dbops.connection_safety` (a profile's type
  only while the endpoint matches; unsaved connections are production).
  `confirm` answers `status: "needs_confirmation"` with the exact statements
  and a one-time random `token` (`EditorSessions.issue_token`, valid
  `TOKEN_TTL` = 300 seconds, bound to a hash of the tab, the connection
  identity, the operation and the statements, including the **On error** mode for editor runs); changing Stop to Continue or back requires a fresh review. The run goes ahead only with
  `confirmed: true` and that token (`take_token` uses it up), so a
  confirmation never covers other statements or a later replay. `decline: true` records a
  declined attempt. Refused, declined and run writes get one audit record per
  run (`operation: "editor_run"`, the non-read statements, redacted; `unknown`
  when the session ended while a statement was out); reads are not audited.
  Read-only connections also start the session with
  `dbops.READ_ONLY_SESSION_GUARD` and refuse statements that would lift it.
  Production sessions start with `SET SESSION sql_safe_updates = 1`,
  Development leaves it off. A tab may turn it on (`safe_updates: true` on
  `/open` or the first `/run`); turning it off is `/open` with
  `safe_updates: false`, a session write judged by the policy (Production
  confirms) and audited as `editor_safe_updates`; the saved policy is re-read
  after the password lookup and again after the client connects, right before
  `SET SESSION sql_safe_updates = 0` (a change refuses, sends nothing and ends a
  session that request started). MariaDB `SET STATEMENT` of
  `transaction_read_only`, `tx_read_only` or `sql_safe_updates` is a write
  (`dbops.SESSION_GUARD_VARIABLES`); read-only connections also refuse `READ
  WRITE` switches matched with quoted identifiers restored. The audit record is
  written before anything is sent; when it cannot be, nothing is sent
  (`audit_failed`) and the tab stays usable. The saved password is filled only
  after the policy allowed the operation (ALTER metadata reads may precede its
  confirmation). `_policy_changed` re-reads profile type/read-only after password
  lookup and immediately before `start_run`; drift returns an audited refusal
  even if the old decision allowed Development DML. This implements db-operations
  section 1 step 6 and section 5's backend enforcement rule.
  Sessions are keyed by tab id plus `dbops.connection_identity` (saved
  connection id, host, port, user, TLS mode and CA, type and read-only).
  Concurrent runs on one tab answer 409 `code: "busy"`; at most 8 live
  sessions (`too_many_sessions`). `EditorSessions.open` never returns a live
  session of another identity, so requests racing on one tab cannot share it.
- DDL confirmation (db-operations sections 6, 7 and 9): `_decision` refuses
  multiple DDL statements, including those grouped by a custom DELIMITER, before
  credentials. Run them one at a time. A CREATE routine's body is one DDL.
  DDL embedded in anonymous blocks or SET STATEMENT wrappers refuses: submit
  it as standalone SQL so its target and safeguards can be reviewed.
  Temporary-table DDL also confirms. Multiple DML statements still share one
  review. DROP DATABASE/SCHEMA carries `confirmation.typed_target`; `_gate`
  requires `payload.typed_target` to match the decoded database identifier
  exactly, alongside the single-use token. Read-only refusal takes precedence.
- ALTER review (db-operations section 9): `_plan_run` adds the exact
  `/*myflames:editor*/ SET SESSION lock_wait_timeout = 5` before ALTER, leaving
  the user's SQL intact. It is shown, token-bound, audited and executed as its
  own statement in the same session; any error stops before ALTER. The setting
  persists in the tab afterwards. The skill gives `N`, not a numeric value;
  five seconds is the chosen application policy, per metadata-lock wait, not a
  total ALTER deadline. Longer MariaDB WAIT clauses and MySQL SET_VAR overrides
  of this timeout refuse before credentials.
  ALTER must be the only user statement in its run so preceding SQL cannot
  change the reviewed schema or table. `_alter_review` holds `run_lock` while
  reading DATABASE(), table metadata and Locks' existing transaction/MDL/session
  readers. `locks.alter_table_preflight` filters MDL SQL to the target table,
  caps collections at 500, attributes transactions through granted MDL, and
  labels unavailable/truncated coverage and unattributed open transactions.
  Approximate size is `information_schema.TABLES.DATA_LENGTH + INDEX_LENGTH`
  for the persistent table; temporary tables may be absent or shadow it. The
  review writes `approx_bytes` (unchanged) with `PerfShared.bytes` in binary
  units ("120.6 KiB (data and indexes)"), the grouped exact bytes on hover, and
  Unknown for null.
  The response's `alter_preflight` includes schema, table, engine/version,
  table_engine, approx_bytes, metadata_locks, transactions,
  unattributed_transactions, sessions, coverage flags, warnings and suggestion.
  Tokens also bind the reviewed schema/table, connection ID and server identity;
  changed session context requires a fresh confirmation. `start_run` also
  rechecks DATABASE() under `run_lock` before an unqualified ALTER, closing
  the gap for a concurrent USE between review and execution.
  `_alter_suggestion` offers INPLACE/LOCK=NONE only for recognized InnoDB
  ADD/DROP COLUMN or INDEX forms on MySQL 8.4+ and MariaDB 10.11+, and never
  when the prediction below is INSTANT (the review then says "No rewrite
  offered: the server is predicted to run this ALTER instantly."; on MySQL
  ALGORITHM=INPLACE turns an instant ADD or DROP COLUMN into a rebuild,
  `handler0alter.cc:1053-1056`). Existing
  algorithm/lock clauses, partitioning, comments and unknown cases get no
  rewrite, with an explanation. Suggested SQL is displayed separately; using
  it requires editing and a new review. INPLACE can rebuild a table, and
  LOCK=NONE does not remove metadata-lock acquisition. The UI points to Server
  > Locks; no automatic SQL rewrite or lock killing occurs.
  Source checks (mysql-expert's source-verification rule): local
  `mysql-server/sql/sql_table.cc:17360-17367` rejects INSTANT with a nondefault
  LOCK; `18470-18525` enforces requested INPLACE/NONE. Tag `mysql-8.4.0` has the
  same checks at `16499-16506` and `17604-17666`. Local
  `mariadb-server/sql/sql_alter.cc:139-223`, tag `mariadb-10.11.1:136-215`,
  validates requested algorithms and locks, including rebuild-capable INPLACE.
  MySQL rebuild evidence: `storage/innobase/handler/handler0alter.cc:927-949`
  and `3042-3043` (`innobase_need_rebuild` in the in-place path).
  SESSION timeout scope/ranges: MySQL `sql/sys_vars.cc:2378-2382`, MariaDB
  `sql/sys_vars.cc:1592-1596`. See also the official
  [MySQL operations table](https://dev.mysql.com/doc/refman/8.4/en/innodb-online-ddl-operations.html)
  and [MariaDB online DDL overview](https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-online-ddl/innodb-online-ddl-overview).
  Callers send the ALTER alone: the Tables view's Add column… and Remove
  column… (`TableAlterDialog.tsx`) once prefixed their own `SET SESSION
  lock_wait_timeout` line, which `_decision` refuses ("Review ALTER
  separately"), so they could never run.
- ALTER prediction (`myflames/alter_algorithm.py`): `predict(alter_sql,
  create_table_sql, engine, version, facts=None)` never raises for SQL input
  and mirrors the server's three steps (`fill_alter_inplace_info` flags,
  `mysql_alter_table`'s forced COPY cases, InnoDB's
  `check_if_supported_inplace_alter`), each rule citing mysql-server 9.7 or
  mariadb-server 13.0 and version-gated where mysql-8.4.0, mariadb-10.11.16 or
  11.4.10 differ. It returns `predicted`, `algorithm` (`INSTANT`, `INPLACE`,
  `COPY` or null), `rebuild`, `concurrent_dml`, `summary` ("INPLACE, rebuilds
  the table"), `text` (the outcome sentence), `reason`, `source`, `clauses`
  (`{text, algorithm, rebuild, concurrent_dml, summary, outcome, reason,
  source}` per clause), `notes` and `needs`. The statement's outcome is all
  clauses together, as the server computes it; when that is slower than every
  clause alone, `notes` says "Together these clauses…". `facts` may carry
  `foreign_key_checks`, `sql_mode`, `innodb_page_size`, `innodb_strict_mode`,
  `old_alter_table` (MySQL), `alter_algorithm` (MariaDB before 11.5),
  `old_mode`, `innodb_instant_alter_column_allowed` and
  `innodb_file_per_table` (MariaDB): a missing setting falls back to the
  server default and `notes` says "Assumes the server defaults: …". Table
  facts have no default: `total_row_versions` (MySQL INNODB_TABLES; the
  budget is 64 through 9.0.1 and 255 from 9.1.0), `fts_doc_id_index` (a
  hidden FTS_DOC_ID left by an old FULLTEXT index), `system_tablespace`
  (MariaDB), `referenced_columns` (other tables' foreign keys) and
  `default_collations` (MariaDB `character_set_collations`); when the outcome
  depends on a missing one the result is `predicted: false` and it is listed
  in `needs`. Not predicted: MySQL before 8.4 or from 10, MariaDB before 10.11
  or from 13.1; partitioned, temporary, system-versioned and non-InnoDB
  tables, functional index parts and MariaDB compressed columns; partition and
  tablespace clauses, ENABLE/DISABLE KEYS, WITH/WITHOUT VALIDATION, ADD PERIOD
  or SYSTEM VERSIONING; table options other than COMMENT, AUTO_INCREMENT,
  character set and collation, ROW_FORMAT, KEY_BLOCK_SIZE, ENGINE and the
  STATS_*, MIN_ROWS, MAX_ROWS and CHECKSUM family; generated column
  expression, type or storage changes, dropping or renaming a column a
  generated column reads, inline REFERENCES, VECTOR columns or indexes,
  MariaDB column-level CHECK in ADD or MODIFY; a row near InnoDB's record
  limit; ALTER IGNORE on MySQL; MariaDB `alter_algorithm` values other than
  DEFAULT or COPY and a non-default `innodb_instant_alter_column_allowed`.
  `editor_session._alter_review` adds `prediction` to `alter_preflight` from
  reads through the same locked helper: `SHOW CREATE TABLE`, one `SELECT @@…`
  of the engine's `_PREDICTION_SETTINGS`, and only when `needs` asks one
  INNODB_TABLES (MySQL) or INNODB_SYS_TABLES (MariaDB) row with
  FTS_DOC_ID_INDEX, KEY_COLUMN_USAGE (MySQL) or INNODB_SYS_FOREIGN (MariaDB)
  for referenced columns, and CHARACTER_SETS (MariaDB). A failed read leaves
  its fact unknown; an exception gives `predicted: false` with its class name
  and never blocks the review. QueryLab's ALTER sheet shows a Predicted fact
  row first and a `<details>` "Why: …" (`AlterPrediction.tsx`
  `AlterPredictionWhy`, shared with Schema versions) with each clause's
  reason and source and the notes; `ReviewedSqlAction` (Indexes' Set
  invisible… and Drop, Tables' Add column… and Remove column…) shows one
  "Predicted: …" line. Live evidence: `test/fixtures/alter_algorithm/
  live-mysql-8.4.json` (MySQL 8.4.11) and `live-mariadb-11.4.json` (MariaDB
  11.4.13), 107 cases each, every case run with no ALGORITHM clause and with
  INSTANT, INPLACE with LOCK=NONE, INPLACE, COPY with LOCK=NONE and COPY on a
  fresh 100-row table (rebuild seen from the InnoDB table id, COPY from rows
  affected, concurrency from whether LOCK=NONE was accepted).
  `test_alter_algorithm.LiveFixtureTests` asserts every outcome and every
  accepted or refused explicit attempt. Not live-checked (source only):
  row-version exhaustion, MySQL's empty-table INPLACE preference (8.4.4+ and
  9.2+, a note), `old_alter_table`, MariaDB `old_mode=LOCK_ALTER_TABLE_COPY`,
  MariaDB 10.11 and MySQL 9.x behaviour. `scripts/record-alter-algorithm-fixtures.py`
  rebuilds the fixtures on a lab server only: loopback `--host`, its own
  database `myflames_alter_probe` (dropped at the end), `sql_log_bin = 0`, and
  the password from the GA matrix credentials file in a 0600 defaults file,
  never printed. Tests: `test/test_alter_algorithm.py`, `apps/workspace/test/
  editor.mjs` (the Predicted row and the Why disclosure).
- History: one `records` row of kind `editor_history` per run
  (`WorkspaceStore.history_add|history_list|history_clear`): SQL redacted with
  `dbops.redact` (and the connection's password), connection name, host and
  port, time, duration, statement and row counts, status and error code; the
  oldest are dropped beyond 1,000 runs or 4 MiB.
- Export (`export_result`, pure): CSV (RFC 4180, CRLF, NULL empty and
  unquoted, `""` for an empty string, optional BOM and formula guard), TSV for
  the clipboard, JSON (`{columns, rows}` arrays by default, objects with
  repeated names suffixed), SQL INSERT (backtick identifiers with doubled
  backticks; NULL; numbers unquoted only for columns the caller types numeric;
  `X'..'` for columns marked `binary: true`; `'..'` with doubled quotes, or `_utf8mb4 X'..'` when
  the text holds a backslash or a control character, so the output does not
  depend on NO_BACKSLASH_ESCAPES; same for MySQL and MariaDB), Markdown
  (escaped pipes and Markdown characters, `<br>` for newlines, `*NULL*`).
  For SQL INSERT only, when a result lacks types, the editor makes at most
  one `live/catalog` read on that result's source connection and borrows
  declared types only for plain column references in a single-table SELECT,
  matching select items by position and source names case-insensitively
  (aliases do not change the source name; a lone `*` or `t.*` maps by result name).
  Expressions, ambiguous result shapes or mixed wildcards, UNIONs, joins,
  WITH, missing source schema/table metadata and failed reads leave untyped
  values quoted; existing server result types always take precedence.
- Privilege preview: `POST /api/editor/preview {connection, tab_id, sql,
  selection? | cursor?}` uses `_plan_run` and `dbops.classify`, exactly as Run
  does before connecting. It returns `status`, `scope`, `statements[]`
  (`index`, `kind`, `action_keys`, `state: allowed|denied|unknown`, `missing`,
  `reason`), `aggregate` (`state`, `missing`, `reason`, `warning`) and
  `policy` (`action`, `text`). Invalid SQL returns `status: invalid`, `code`,
  `message`, no statements and an unknown aggregate. No SQL or credentials
  are echoed. Preview opens no connection, retrieves no saved password and
  sends no SQL. `ui._accounts_response` caches successful `privileges-1.0`
  reads in workspace memory by the same connection identity as the editor;
  refresh invalidates the old entry first, overlapping reads keep the latest,
  and account applies invalidate the cache. Without a matching report the
  answer is unknown. `privileges.statement_privileges` maps classified units
  to existing action keys and `can()` answers; unsupported grammar and
  incomplete variable-specific checks remain unknown. A tab that sent SET
  ROLE, account SQL, opaque CALL/EXECUTE or temporary-table DDL remains unverified until
  reconnected. A role/account change in a proposed batch also makes subsequent
  statements unknown. Preview never changes the session itself.
  QueryLab debounces the selected/cursor and whole-tab previews by 300 ms,
  discards replies after text, scope, connection, session or privilege-report
  changes, and checks the matching answer again in keyboard Run handlers.
  Only a definitive denial for every selected statement greys a Run button;
  mixed batches remain enabled with a warning, and unknown shows Unverified.
  Denied buttons stay focusable with the reason in their tooltip and accessible
  description. The read-only policy refuses independently. Stop, Roll back
  and session closing retain their existing behavior; Run still performs its
  normal policy checks, confirmation and audit.
- Endpoints (`editor_session.PATHS`, token-checked, loopback): `open`, `run`, `preview`,
  `status`, `stop`, `commit`, `rollback`, `use`, `close`, `history`, `export`,
  `file/open`, `file/save` under `/api/editor/`; `run` and `commit` are in
  `ui._WRITE_PATHS`, `open`, `stop` and `use` in `ui._CONNECTION_PATHS`.
  `/api/editor/status`, `stop`, `use` and `close` enforce session ownership by
  requiring the matching `connection` (`_connection_facts`,
  `session.identity == identity`); an unknown tab or mismatched connection
  returns `status: "none"`, stops nothing, refuses `use`, or returns
  `closed: true` with `session: None`. Shapes: docs/QUERY_WORKSPACE.md.
- Tests: `test/test_editor_session.py`: replays of real MySQL 8.4 and MariaDB
  11.4 client output (`test/fixtures/editor/*.json`, stdin built by the
  module's own framing, recorded from throwaway containers, including a KILL
  QUERY while rows stream), the fake client `test/_fake_mysql_client.py`
  (copied onto PATH as `mysql`; MySQL and MariaDB modes) for the session,
  Stop, limits, policy and audit, identity, idle timeout and history, and the
  export formats.
- What runs is decided on the server: `/run` takes the whole text with a
  `selection` (split with `delimiter_at`, the delimiter in effect at its start)
  or a `cursor` (`statement_at` over `dbops.split_statements` with the
  session's backslash reading; `split_statements` takes the initial
  `delimiter`). `statement_at` and `sqlSelection.ts` `statementAt` are twins;
  both run `apps/workspace/test/fixtures/sql-split.json`
  (`test/test_sql_split_twins.py`, `test/sql-selection.mjs`), which also pins
  backslash modes, `\g` and executable comments. `decline: true` is answered
  first on `/run`, `/commit`, `/rollback` and `/open`: it never executes,
  whatever the policy says by then (a write is recorded as declined).
- Explain (`_plan_run`): `/run` with `explain_format` (`table`, `tree`,
  `json`) explains exactly one statement in the tab's session, like MySQL
  Workbench. It prefixes `EXPLAIN FORMAT=TRADITIONAL|TREE|JSON ` (MariaDB's
  Table is plain `EXPLAIN `, which is always tabular there; MySQL 9.7
  defaults `explain_format` to TREE in `sql/sys_vars.cc`, so Table names
  TRADITIONAL) and records
  `explain_prefix` so error offsets still point into the editor text. Tree
  needs MySQL 8.0.16 or later (refused otherwise). More than one statement
  (also through a DELIMITER) is `invalid_statement`, and so is a statement
  that starts with `ANALYZE` after optional `FORMAT=`, `EXTENDED` or
  `PARTITIONS`, because `EXPLAIN ANALYZE` executes. The prefixed text is
  classified like any statement, so EXPLAIN of a DELETE is a read, runs on a
  read-only connection without confirmation, and changes no row. Planning
  still evaluates constant parts (a derived table without tables is
  materialized while the plan is built, `sql/sql_derived.cc`; verified on
  MySQL 9.7: `EXPLAIN SELECT * FROM (SELECT GET_LOCK('x', 0)) a` leaves the
  lock held; stored functions ran this way before 8.0.42, 8.4.5 and 9.3.0,
  Bug#37560280), so `dbops` applies the SELECT expression checks to the
  explained statement (named locks, `:=`, sequences, locking clauses, and
  functions that are not verified native readers) without the INTO check; DDL
  after EXPLAIN stays a read because the server refuses it. An optional
  `parameters` object fills `:name` placeholders with `live.fill_parameters`
  (the literals of `bind_query`: hex strings, finite numbers, NULL, lists for
  IN; refused when backslash and NO_BACKSLASH_ESCAPES readings disagree); a
  statement without placeholders is sent as written, and Run never binds. No
  SELECT projection, transaction wrapper or session setting change. Messages
  and history show the exact `EXPLAIN …` sent (`statement.sent`). The UI's
  Explain format select (`myflames.editor.explainFormat` in localStorage)
  shows the rows in an "Explain" result tab with Export and Explore; Explore
  captures the original statement (`live/start`, mode `explain`, SELECT
  guards apply) and opens Plans. Run analysis keeps the capture path, and its
  result is the "Analysis" tab with its own Explore
  (`test/test_editor_session.py`, `test/editor-explain-live.mjs` against a
  real server).
- Workspace UI (`apps/workspace/src`): `QueryLab.tsx` keeps a `Runtime` per
  tab in page memory (session status, the connection key and connection it was
  opened with, the connection of the request on its way, where the results
  came from (connection key and session id, for Load more), a request
  generation, notes); the runtime ref is updated in the same task as each
  change. `runSql` sends the text with the selection or the caret
  (`sqlSelection.sqlRun`; its statement range only draws the tint), `wait: 2`,
  then long-polls `/status` (`wait: 5`) until the run ends; a failed poll
  keeps the run active (Stop stays) and retries. Every execution path,
  Explain and Run analysis included, refuses while the tab's session is on
  another connection. The mismatch banner names the session's server and the
  connection in use and offers Close session; Run, Run all, Explain and Run
  analysis are disabled. Results keep From &lt;connection&gt;, even after closing
  that session, and Load more requires the original session and connection.
  Stop, Commit and Roll back use the session's own connection, never the picker's.
  Export lives in the results tab row and applies to the selected result. A restore probe is marked done only once
  answered and is dropped when a request started meanwhile. `needs_confirmation` opens a ConfirmSheet; Confirm re-reads the
  write policy (`useWritePolicy().check()`) and sends nothing when the
  connection key changed, else resends with `confirmed` and the token; a reply
  that asks again says whether the token expired; Cancel and Escape resend with
  `decline: true` (the server audits it). Commit and Roll back keep the tab's
  results; closing a tab calls `/close` and only on `transaction_open` or
  `transaction_unknown` asks Commit and close or Roll back and close
  (`discard: true`). After a reload the active tab asks `/status` once and
  adopts a session still open. `editor/SqlEditor.tsx` (the CodeMirror 6
  component: `editor/sqlLanguage.ts` builds one Lezer tree per document from
  the `sqlHighlight.ts` tokenizer, driving the syntax colors and the bracket
  or quote pair next to the caret, both off above `HIGHLIGHT_LIMIT` (60,000)
  characters; `editor/marks.ts` draws the statement tint, the run flash, the
  error underline and the gutter line marks, and `editor/visual.ts` the
  active-line and selection-match highlights), `ResultsPane.tsx`
  (results tabs, `DataTable` grid, Messages, Export, copy menu),
  `SchemaBrowser.tsx` (`/api/live/schemas` through `useSchemaCatalog` once
  per connection identity, the first time the pane is visible with a
  connection, then `/api/live/catalog` on expand; cached per connection key
  and kept while hidden; the header has Reload schemas with the busy, retry
  and connection-scoped behavior; with no connection it asks for one),
  `HistoryPanel.tsx`, `editor.css`. Session restore: tab
  `limit`, `selection`, `result_tab`, and `query_lab.editor_layout` (height,
  browser and width, history, run time limit), reported only once changed
  from the defaults. Browser suite `test/editor.mjs` (`npm run test:editor`).
- Editor component (`apps/workspace/src/editor/`, CodeMirror 6): one
  `EditorState` per tab in a per-tab document store (`createDocumentStore`;
  the state carries the tab's own undo history, so undo is per tab, and
  external value replacements are one undo step through `isolateHistory`).
  Settings reconfigure Compartments, never remount the view.
  Every document includes the phrase overrides **Problems**, **No problem
  warnings**, **Close** and **SQL suggestions**. The host captures the lint
  panel's Close click, closes it and focuses the editor; CodeMirror content
  event handlers do not receive panel clicks. The shortcuts popover renders
  arrow glyphs and uses Esc, Space, Tab and platform-specific Return/Enter. Several
  selections: Run, Explain and Run analysis read only the main cursor or
  selection (`getSelection()`; `getSelections()` exists for checks). Run all
  runs the whole tab (`sqlSelection.sqlRun`, `all: true`), and
  Format SQL refuses several ranges ("Format SQL works with one cursor or
  selection; several are set. Keep one and try again."). Escape closes
  suggestions, else keeps only the main cursor, else closes Find, Replace or
  Go to line through `findHooks.close`, else closes the lint panel when open.
  Each handled step consumes Escape; otherwise it falls through to Stop and
  CodeMirror's Escape-then-Tab focus handling. Closing the Find controller
  clears marks and returns focus without changing the current selection or
  scrolling to the last match. Select all occurrences (`Shift-Mod-l`, only
  with Multiple cursors on) expands an empty caret using `state.wordAt`, or
  accepts several ranges with identical selected text, then delegates to
  CodeMirror's selection-match search. Unequal ranges are left unchanged.
  The standard macOS `Ctrl-k` binding prevents the default event and stops
  propagation so deleting to line end does not also open the command palette.
  The Next match label mentions Ctrl+G opening Go to line only off macOS.
  The desktop Edit menu's
  Undo and Redo reach the focused editor's CodeMirror history through the
  `window.__myflamesEditCommand` bridge, which accepts only the two literal
  commands `"undo"` and `"redo"` and reports an empty history as handled so
  Chromium's own undo never runs (`apps/desktop/main.cjs` falls back to the
  native editing command when no editor has focus). Feature suites:
  `test/editor-core.mjs` (`npm run test:editor-core`), `test/edit-menu.mjs`
  (`npm run test:edit-menu`), and the renderless unit suites
  `test/editor-core-unit.mjs`, `test/editor-commands-unit.mjs`,
  `test/editor-find-unit.mjs`, `test/sql-tree-parity.mjs` (Node, no
  browser).
- Safe updates explanation: `SafeUpdatesInfo.tsx` is the standard `InfoPopover`
  (ⓘ, "About safe updates") beside the Safe updates checkbox in Run options
  (`.safe-updates-row`). Its wording follows the server source, not the
  manuals: MySQL refuses (1175, before any row changes) a single-table UPDATE
  or DELETE with no index usable for its WHERE (`quick_keys` empty) and no
  LIMIT, and a multiple-table one that scans a table it changes
  (`CheckSqlSafeUpdate`); MariaDB refuses the same and also a DELETE whose
  WHERE is missing or constant even with a LIMIT (`const_cond` in
  sql_delete.cc), a multiple-table one with a full scan of any joined table
  (`error_if_full_join`), and a multiple-table DELETE without a WHERE even
  under EXPLAIN (`multi_delete_precheck`); MySQL never refuses EXPLAIN; the
  server variable does
  not limit SELECT (the client's `--safe-updates` adds `sql_select_limit` and
  `max_join_size`, which myflames never sets). Its two documentation links
  (`SAFE_UPDATES_DOCS`) open with `target="_blank"` and must pass the desktop
  `externalLink` allowlist (`dev.mysql.com`, `mariadb.com`; checked by
  `apps/desktop/test/backend.test.cjs` against the source and by the browser
  suite against the rendered page). `EditorSplitButton` keeps its panel open
  when a popover inside it opens (the `myflames-popover` event names an
  element inside the panel) and leaves Escape to that popover first;
  `.editor-options-popover:has(.popover)` drops the panel's entrance
  transform, which would otherwise be the nested popover's containing block.
  Browser suite `test/safe-updates-info.mjs` (`npm run test:safe-updates-info`;
  `SAFE_UPDATES_SHOTS=<dir>` writes screenshots at 1440x900 and 720x520, light
  and dark).
- Query tab menu (`QueryTabMenu.tsx`, the shared `.context-menu` with
  `useFocusScope({ menu: true })`): right-click, Shift+F10 or the ContextMenu
  key on a tab (`aria-haspopup="menu"`, the tab's `data-tab-id`) opens
  Rename… (F2), Duplicate, Close and Close others for that tab; Escape or a
  click elsewhere closes it and focus returns to the tab. Rename keeps the
  inline field (double-click, F2, the menu, and the palette's **Rename query
  tab** through `QueryLabHandle.renameTab`): trimmed, at most `MAX_TAB_NAME`
  (64) characters (`maxLength`; the session normalizer allows 200; a longer
  name is cut by `clipName`, which never splits a surrogate pair), Enter on
  an empty name keeps the field open with `aria-invalid` and a message in
  `.editor-feedback`, blur keeps the old name, Escape cancels. Duplicate copies
  the tab (SQL, parameters, limit, source and target binding, not the results
  tab) right after it as "NAME copy" (`copyName`). Close others calls
  `requestClose` (now returning `closed`, `asks`, `running` or `error`) tab by
  tab, skips running tabs and stops at the first one that asks Commit or Roll
  back. Labels end in an ellipsis past 240 px (`.query-tab-label`); the tab's
  tooltip has the whole name.
- Format SQL: `formatSql` in `QueryLab.tsx` (toolbar `IconButton`
  `data-action="format"`, icon `format`, Shift+Alt+F on the tab panel by
  `e.code === "KeyF"`, and the palette through `QueryLabHandle.formatSql`,
  which switches the Query editor to its Editor view and waits for the text
  area to be on screen, never formatting a hidden editor). It reads the
  textarea, loads `sqlFormat.ts` with `import()`, calls `formatEditorText`
  (a selection that starts or ends inside a token of the whole text, under
  every reading, is refused; the whole text after the change goes through
  `compareSql`, since a fragment alone cannot show it sat inside a string),
  and replaces the selection (or all the text) with one
  `execCommand("insertText")` (text changed, a selection made, changed or
  cleared, or the editor hidden while the chunk loaded, or an edit the
  browser refuses, changes nothing and says so), so the browser's undo
  restores it in one step; a selection stays on the formatted text, otherwise
  `mapOffset` keeps the caret, read after the chunk loaded, next to the same
  character. `sqlFormat.formatSql` splits
  with `splitStatements` and formats statement by statement with
  `formatDialect` (`mysql`, or `mariadb` when the tab's session is MariaDB;
  `tabWidth: 2`, `keywordCase: "preserve"`, `paramTypes` `?` and `:name`),
  keeps DELIMITER blocks, stored programs and compound statements, client
  commands, unclosed quotes or comments, names in square brackets, statements
  the formatter cannot read and statements whose executable comments or hints
  it would change exactly as written, and joins statements with one blank line (other gaps stay as
  written). `sqlFormatCheck.ts` holds the guarantee: `tokenize` (the server's
  lexical rules: `-- ` needs white space, `X'..'` `B'..'` `N'..'` are one
  token, `@var`, `:name`), `caseSafe` (words reserved in both servers, each
  statement's first word and the keywords of a statement opening, never next
  to a `.`, including a dot at the edge of an executable comment beside the
  word; an executable comment is code and ends a statement's opening, while
  plain comments and `/*+` hints are skipped), `upperKeywords` and
  `compareSql`, which runs under every reading in `READINGS` (with and
  without backslash escapes, and with `[name]` as one quoted name, as MariaDB
  10.4.5+ reads it under sql_mode=MSSQL, MDEV-19142) and refuses any change beyond white space, plain comment white space,
  line comment trailing space and safe keyword case, or a change of the space
  between an IGNORE_SPACE function and its parenthesis. `RESERVED` and
  `IGNORE_SPACE_FUNCTIONS` were computed from the server sources (MySQL
  `sql/lex.h` symbols not declared `%token<lexer.keyword>` in `sql_yacc.yy`,
  as `gen_keyword_list.cc` does, intersected with MariaDB `lex.h` symbols in no
  `keyword_*` rule; MySQL `SYM_FN` and MariaDB `sql_functions[]`). A refusal,
  a kept statement or "Already formatted" is said in `.editor-feedback`
  (`is-error` for a refusal); nothing is sent to the server. Unit tests
  `test/sql-format.mjs` (`npm run test:sql-format`, no build: the modules are
  compiled by `test/sql-format-load.mjs`); browser suite `test/editor-tools.mjs`
  (`npm run test:editor-tools`; `EDITOR_TOOLS_SHOTS=<dir>` writes screenshots at
  1440x900 and 720x520, light and dark).
- Toolbar layout (`editor.css`): the editor toolbar wraps onto another row
  whenever its items do not fit, at any pane width; no fixed breakpoint can
  promise one row, since the Database picker's longest name, the scope, the
  transaction state and the privilege note all change its width. An item
  shrinks below its content only when it clips that content: the plain
  transaction state (Auto-commit, Manual commit, Session ended) keeps its full
  width, and the transaction meter shortens with an ellipsis only when a row
  is too narrow for it. The privilege note's basis is its whole sentence
  (`flex: 1 1 auto; min-width: min(100%, 24em)`): it stays on a row only
  where it fits whole, otherwise starts the next row, and ends with an
  ellipsis only when even a full row is too narrow (the 720 px window, or a
  1040 px window with the schema browser open); its tooltip keeps the full
  text. In a pane of 1,100 px or less the groups may also wrap and the scope
  shortens. Browser suite `test/editor-toolbar-layout.mjs`
  (`npm run test:editor-toolbar-layout`; `EDITOR_TOOLBAR_SHOTS=<dir>` keeps
  screenshots) asserts that no two toolbar labels paint over each other and
  none leaves the toolbar, and that the note shows its whole sentence wherever
  one toolbar row can hold it (always at 1440 px, and at 1040 px with the
  schema browser closed), at 1440, 1040 and 720 px, light and dark, with the
  schema browser open and closed, one or two statements, Auto-commit and the
  transaction meter.
- The editor's room (`measureEditorRoom`) is set only when the measured value
  changes (`measuredRoom`): a no-op update from its every-commit layout effect
  kept re-rendering while a lower-priority caret update waited, and React
  stopped with "Maximum update depth exceeded" when Format SQL replaced the
  text.
- Schema browser endpoint: `POST /api/live/catalog` (`ui._CATALOG_PATHS`,
  read-only, saved password filled like the views) answers
  `schema_browser.catalog_report`: `catalog-1.0` tables of one schema
  (`information_schema.TABLES`, at most 200 per page), one table's columns and
  indexes (`COLUMNS`, `STATISTICS` read by name), or one schema's object group
  when `group` is `"routines"`, `"triggers"` or `"events"` (`level: "objects"`,
  `limit: 200` (`MAX_OBJECTS`), `truncated: bool`, and `objects`: `routines`
  `{name, kind: "procedure" | "function"}` from `information_schema.ROUTINES`
  with `ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION')`, MariaDB packages excluded;
  `triggers` `{name, table, event, timing}` from `information_schema.TRIGGERS`;
  `events` `{name, status}` from `information_schema.EVENTS`), tagged
  `/*myflames:editor*/`, names as hex literals. Optional `prefix` (literal text, including `%` and `_`)
  and `after` (exclusive continuation) are forwarded to the module after `check_table` validation,
  before opening a connection: null/empty means absent; otherwise strings of at most 64 characters,
  with blank-only text, NUL, LF and CR rejected. Pages use binary table-name order and return
  `next_after` when truncated. `stats: true` (only a literal true) adds `engine`, `rows`
  (`TABLE_ROWS`, the server's estimate) and `bytes` (`DATA_LENGTH + INDEX_LENGTH`) to each table,
  None for views, and `stats` to the report; Maintenance > Dump reads it for the one chosen database
  (MariaDB opens each table of that schema to answer). In `SchemaBrowser.tsx`, expanding a schema lists **Routines**, **Triggers** and **Events** folders after its tables and views; opening a folder loads its group on demand, double-click or Enter/Space on an object inserts `<quoted_schema>.<quoted_name>` at the editor cursor, and its context menu offers **Insert name** and **Show CREATE PROCEDURE / FUNCTION / TRIGGER / EVENT** (appended and run via `onQuery`). Tests: `test/test_schema_browser.py`, `test/test_ui.py`.
- Find and replace (`apps/workspace/src/editor/FindBar.tsx`, `editor/find.ts`, `editor/findMarks.ts`, `editor-find.css`): `⌘F` (`Ctrl+F`) opens Find and `⌥⌘F` (`Ctrl+H` on Windows and Linux; `Ctrl+Alt+F` also works there) opens Find and replace above the editor. A single-line selection seeds the Find field; there is no 200-character selection limit. `Enter` / `⇧Enter` in Find and `⌘G` / `⇧⌘G` (`Ctrl+G` / `Ctrl+Shift+G`) step between matches (`Ctrl+G` opens Go to line when Find is closed on Windows and Linux); Escape closes the bar and returns to the editor. Toggles are **Match case** (`Aa`), **Whole word** (`|ab|`) and **Regular expression** (`.*`). `computeFind` uses a Unicode literal search and, for literal whole words, letters, numbers, `_` and `$` as word characters. Regex mode uses JavaScript `g` / `gi` expressions (no `u`), with JavaScript `\b` boundaries for Whole word, and ignores zero-length matches. `createRegexFinder` runs regex searches in a terminable worker, with `FIND_REGEX_TIMEOUT_MS` = 250 ms and "Pattern too slow to search" on timeout; invalid patterns set `aria-invalid` and report an error. Searches retain at most `FIND_MATCH_LIMIT` (100,000) matches, append `+` to a capped count, and draw at most `FIND_MARK_LIMIT` (1,000) marks, including above `HIGHLIGHT_LIMIT` (60,000 characters). **Replace** and **Replace all** insert replacement text literally, including `$&` and `$1`; capture-group expansion is not implemented. They plan CodeMirror changes (`planReplace`) dispatched as one transaction with `isolateHistory`, so one undo restores the text; a single Replace remembers its undo anchor, and an immediate undo returns the find anchor to the undone match. Tests: `apps/workspace/test/editor-find.mjs` and `test/editor-find-unit.mjs`.
- Open and save `.sql` files, Continue on error, and the tab's default database picker (`QueryLab.tsx`, `QueryTabMenu.tsx`, `ResultsPane.tsx`, `session.ts`, `myflames/editor_session.py`, `myflames/file_picker.py`):
  - **Open and save `.sql` files**: **Open SQL file…** in the query tab menu or `⌘O` / `Ctrl+O` inside the Query editor calls `POST /api/editor/file/open` (`{}`). The native file dialog (`file_picker.choose_sql_file`, `.sql` and `.txt`, serialized by `folder_picker._DIALOG_LOCK`, 120 s timeout) resolves and checks its chosen path. The reader then opens through the checked parent without following a file or folder swapped for a symbolic link; it refuses anything but a regular file and checks the 5 MiB limit (`SQL_MAX_BYTES`, 5,242,880 bytes) both on the open descriptor and by reading at most one byte beyond the limit, so a file that grows during the read is refused too. It strips a UTF-8 BOM, replaces invalid UTF-8 bytes with `U+FFFD` with a `warning`, and opens a new tab named after the file (without `.sql` / `.txt`, clipped to 64 characters).
    The process-local `_FILE_TOKENS` map keeps at most 200 opaque tokens bound to the resolved path and its parent directory's device/inode; the renderer never supplies a write path. `⌘S` / `Ctrl+S` saves via `POST /api/editor/file/save` (`{sql, token}`). `⇧⌘S` / `Ctrl+Shift+S`, or Save without a binding, uses `{sql, save_as: true, name}` and `choose_save_file`. An `unknown_token` after a backend restart opens Save as within the same Save operation; success, cancellation or failure is reported, and a cancelled or failed save or close keeps the tab's unsaved edits. Save as suggests the bound file's name, or `<tab.name>.sql` without a binding. `_clean_suggested_name` removes path components and controls, replaces colons with hyphens, appends `.sql` when needed and clips the suggested name to 255 characters. On macOS and Windows the sanitized name reaches the native dialog through `MYFLAMES_SAVE_NAME` unchanged, including quotes and Unicode, never as script source.
    Save accepts at most 8,000,000 characters and `.sql` or `.txt` destinations. It refuses a symbolic-link target, directories, special files, and a parent whose identity changed since selection ("Use Save as to choose the file again"). When it adds `.sql` to a name without an extension, publication never replaces an existing file, including one created after validation: choose the full filename in Save as to approve replacement. `write_text_atomic` writes UTF-8 without BOM to a temporary file, flushes and `fsync`s it, then uses `os.replace` for an approved full name or an atomic no-clobber link when `.sql` was added. With directory-descriptor support, the parent stays pinned during creation, publication and cleanup; other platforms recheck its identity. An existing regular file owned by this user with one hard link keeps its `0777` permission bits; if the temporary file lands in a different group, group permissions are set to the old permissions for others. Other files use `0666` less the umask with directory-descriptor support; the temporary-file fallback uses owner-only permissions. A file-backed tab whose `sql !== file.savedSql` shows an edited dot (`title="Edited since the last save"`), adds `Edited` to the window status bar, and asks **Unsaved changes** with **Save**, **Don't save** and **Cancel** before closing the tab. Tests: `test/test_editor_files.py`, `test/test_file_picker.py`, `test/test_editor_session.py`, `apps/workspace/test/editor-files-unit.mjs` and `editor-file-safety.mjs`.
  - **Continue on error**: **Run options** offers **On error: Stop / Continue**, default Stop (stored per tab as `on_error?: "stop" | "continue"` in `session-1.0` and sent to `POST /api/editor/run`). `_review_digest` includes `on_error`, so approving a write in one mode never approves running it in the other. Continue applies only to `scope == "all"` (**Run all**); Stop, loss of the session, and safety or review refusals still prevent further statements. After a Continue run finishes, Messages counts actual statement statuses: succeeded (`ok`), failed (`error`), not run (`not_run` or `pending`), stopped (`stopped`) and outcome unknown (`session_ended` or `running`), showing the last three counts when nonzero, and lists each failure with its statement number and jump link. Error 1213 adds "Error 1213: an InnoDB deadlock rolls back the victim's whole transaction" and error 1205 adds "Error 1205: an InnoDB lock wait timeout rolls back the whole transaction only with innodb_rollback_on_timeout ON": engine behavior, never a claimed outcome, since a script can SIGNAL either code and a metadata lock wait also ends with 1205. The On error ⓘ explains that by default an InnoDB error rolls back only the failed statement; a deadlock, or a lock wait timeout with `innodb_rollback_on_timeout` ON, rolls back the whole transaction.
  - **Default database picker**: **Database** (`<select aria-label="Default database">`) offers **None** and schema names from `useSchemaCatalog(connection, onScreen)`, read only while the Query editor is on screen. A finished run whose statements may change the list (`runMayChangeDatabases`: a CREATE or DROP DATABASE or SCHEMA, or a statement without a verb) calls `invalidateSchemaCatalog` with the connection the run went to (`sessionConnectionOf`), never the one on screen when the late callback runs: that connection's cached list is dropped, its views on screen read it again and hidden ones (the editor included) read it when shown. Any `useSchemaCatalog` view that is shown again attaches to the current shared read when the list was invalidated or replaced while it was hidden (an invalidated list is read once); an unchanged list stays, because each read also counts tables. The picker re-reads it on focus when the read is older than `CATALOG_STALE_MS` (10 s), so a database created elsewhere appears without a restart. Every read uses the connection its cache key was rendered with (`catalogConnections`). A reload replaces the shared per-connection promise and notifies every other `useSchemaCatalog` instance of that connection (one listener set per key), so the Schema browser and server pickers see the new list. `database?: string` in `session-1.0` permits an explicit empty string for None, or up to 64 characters without controls; restore preserves the empty value. `connectionFor` uses the tab value with nullish fallback, so None starts the tab's next session without a default database even if the connection has one. `chooseDatabase` refuses a change while the tab's live session belongs to another connection. With no live session, the chosen database is used on the next session; with one, `POST /api/editor/use` (`{tab_id, connection, database}`) takes `run_lock`, sends tagged `USE` with quoted identifiers, and verifies through `refresh_state()` and `SELECT DATABASE()` (a session read, no confirmation or audit). Choosing None cannot clear a live session's database: it keeps that database until the session closes, and the next session starts with none.

#### Saved queries, the fill form and result selection

- Saved queries: `/api/editor/saved/list` (optional `limit` 1 to 500),
  `/api/editor/saved/save` (`name`, `sql`, optional `folder`, `note`,
  `database`; answers `{saved, redacted}`), `/api/editor/saved/rename` (`id`,
  `name`), `/api/editor/saved/move` (`id`, `folder`; empty or null is no
  folder) and `/api/editor/saved/delete` (`id`), dispatched by
  `editor_session.api_response`, allowlisted in `ui.py` and typed in `api.ts`
  (`listSavedQueries`, `saveQuery`, `renameSavedQuery`, `moveSavedQuery`,
  `deleteSavedQuery`). They are local only: none opens a connection, none is a
  write or connection path, and nothing is audited. Records are
  `saved-query-1.0` (`docs/schemas/saved-query-1.0.json`: `schema_version`,
  `name`, `folder`, `sql`, `note`, `database`, `created`, `updated`, plus the
  32-hex `id` on reads), stored as `workspace_store` records of kind
  `saved_query` (`SAVED_QUERY_KIND`), apart from `editor_history`, so Clear
  history leaves them. SQL, name and note pass `dbops.redact` (the run
  history's redaction) before storage, and the store strips `password` keys
  again; `redacted: true` when the SQL changed, and the editor says
  "Credentials in it were replaced by [redacted]." Bounds: SQL at most
  `SAVED_QUERY_SQL_CAP` = `HISTORY_SQL_CAP` (20,000 characters, after
  redaction), name and folder 200 characters on one line (white space
  collapsed), note 2,000, database 255; at most 500 records and 4 MiB of
  stored JSON. A full store refuses ("Delete some, then save again") and never
  drops a saved query. UI: `SavedQueries.tsx` in the **Saved** tab beside
  **History** (`HistoryPanel.tsx`, `Segmented` with ids `editor-side-tab-*` /
  `editor-side-panel-*`; each tab reads only while shown), grouped by folder
  (unfiled first, folders by name, each collapsible; Left and Right collapse
  and expand, Up and Down move) with a search over name, folder, SQL, note and
  database. A click opens the SQL in a new tab named after it and runs nothing
  (the note names the database it was saved for when that differs). The row
  menu (kit `ContextMenu`) has Open in a new tab, Insert at the cursor,
  Rename…, Move to folder… and Delete… (a ConfirmSheet). **Save query…** is in
  the tab menu after Duplicate and in the palette (`query-save`,
  `QueryLabHandle.saveQuery`); its sheet starts with the tab's name, No folder,
  the existing folders (read when it opens) or New folder…, and an optional
  note. Saved queries keep SQL only, not the Parameters JSON or row limit; the
  database is information and never switches the connection. Tests:
  `test/test_saved_queries.py`, `apps/workspace/test/editor.mjs`,
  `editor-tools.mjs`.
- Fill form (`FillParameters.tsx`, pure helpers `fillValues.ts`): opens when
  `openDigest` (Top statements, Query digest) produced parameters for a
  statement capture accepts, also with an analysis warning, and never for a
  refused one. Each `:pN` gets its SQL context (markers inside quotes and
  comments ignored), a type (Number where the SQL is numeric: LIMIT or OFFSET,
  arithmetic, a compared column named like an id or a count; Text otherwise;
  NULL) and a value. A collapsed list (`QueryTab.fill_lists`, normalized in
  `session.ts`) takes comma-separated values and becomes a JSON array. **Use
  values** writes into the Parameters JSON (the source of truth; other keys
  kept; an emptied field is null again) and removes the names given a value,
  NULL included, from `fill`; it never runs anything. Numbers with more than
  15 significant digits are refused, because a double would round them. Enter
  moves to the next value, then to Use values; Escape returns focus to the
  editor; **Later** opens the Parameters popover; **Fill in values…** on the
  notice opens the form again, also after a restore. `live.bind_query` binds a
  JSON list (1 to 1,000 finite numbers, strings, booleans or nulls) as a
  comma-separated list for `IN (:p1)`, refusing an empty list, a nested list
  or an object with the parameter's name; `myflames capture --parameters`
  shares it, and the `--statement-digest` note reads "give it a JSON list, such
  as [1, 2], or write the list into the SQL". Tests: `fill-values.mjs`,
  `digest-actions.mjs`, `placeholders.mjs`, `session-rules.mjs`,
  `test/test_digest_capture.py`, `test/test_saved_queries.py` (list binding).
- Result selection (`ResultsPane.tsx` `ResultGrid`, `resultTotals.ts`): the
  grid sorts its rows itself (controlled `DataTable` `sort`), so a selection is
  a rectangle in screen order, kept by the rows' original indexes across
  re-sorting. A click selects a cell; Shift+click and Shift with the arrow keys
  (Up, Down, Home, End and the Page keys through the table, Left and Right in
  the grid) extend it; Left and Right move the focused cell; a row number
  selects its row; Option+click (Alt+click) on a header, or **Select column** in
  the cell menu, selects a column (Shift adds columns; a plain click still
  sorts); ⌘A/Ctrl+A selects every cell. Selected cells carry `tr[data-sel]`
  and the column classes `is-sel-col` / `is-focus-col` (no imperative DOM), so
  long windowed results work. The footer reads "Count N · Sum S · Avg A · Min
  m · Max M" over the numeric cells, prefixed with "C cells" when others are
  selected too, "N cells" when none is numeric, and "· K NULL" for skipped
  NULLs. Sums, Min and Max are exact BigInt fixed-point decimals (2^63 and long
  DECIMAL values never round); Avg is rounded half away from zero at six digits
  past the longest fraction; a value with an exponent switches the line to
  doubles, labeled "approximate (floating point)". ⌘C/Ctrl+C copies the
  selection as TSV (NULL empty, the existing `tsv`), one cell as its raw value,
  and the row when nothing is selected. Cell menu order: Show row, Select
  column (name), Copy selection (N cells) when several cells are selected,
  Copy value, Copy row, Copy column, Copy all rows with header. A range across
  a column hidden in `DataTable` still includes its values in totals and
  copies.
- Row panel: **Show row** (double-click, Return or Enter through `DataTable
  onActivate`, or the menu) shows the focused row beside the grid (300 px; a
  fixed bottom sheet at 720 px and below): column name, type when known
  (`EditorColumn.type`) and value. NULL is the muted `sql-null` token, never
  empty; an empty string reads "empty string"; a hex-guessed binary column
  stays hex with its byte size; JSON (a type containing `json`, or text that
  parses as an object or array) is laid out by `prettyJson`, which copies number
  and string tokens verbatim (a BIGINT inside JSON keeps every digit), with
  **Copy JSON**. Up and Down move rows in the grid or the panel (buttons too);
  Escape or Close closes it and returns focus to the row it shows (`closePanel`
  reveals and focuses `sel.focus.i`, which Previous row and Next row may have
  moved), not the row it was opened from; in a narrow window this runs after
  the sheet's focus scope returned to its opener. A new result closes it and
  clears the selection. Tests: `result-totals.mjs`, `editor.mjs`.
- Narrow sheets (720 px and below, `NARROW` in `QueryLab.tsx`): the editor
  shows one bottom sheet at a time. `ResultGrid` gets `rowPanel={{narrow,
  blocked, onOpen}}`: opening a row calls `onOpen`, which clears the editor's
  narrow side-sheet choice (Schemas or History), and while a side sheet is open
  (`blocked`) the grid closes its row panel during render, not in an effect (an
  effect's update would lose to the Escape that closes Schemas and could
  briefly reopen the row sheet), and forgets its local open state, so closing
  Schemas or History never brings an old row sheet back. Narrow panels use the
  shared `useFocusScope` (initial focus, Tab containment, Escape, return to the
  opener); the scopes are inactive while the retained editor is off screen or
  Table data replaces it, and opening another sheet never restores focus into
  the hidden one. Wide Schemas, History and row panels stay independent and
  docked. Tests: `editor-sheets-unit.mjs` (the real handlers, no browser),
  `editor.mjs` (720x600 exclusion both ways, focus, no stale reopening, and
  nonoverlapping docked panels at 1440x900).
- Statement insights and the transaction meter (`myflames/editor_insights.py`,
  `apps/workspace/src/EditorInsights.tsx`): `editor-run-1.0` gains an optional
  per-statement `server_stats` (only on `kind == "run"` runs, only when set):
  `available` with `reason` (null when available), `event_id`, and the
  counters `rows_examined`, `rows_sent`, `rows_affected`, `no_index_used`,
  `no_good_index_used`, `created_tmp_tables`, `created_tmp_disk_tables`,
  `sort_merge_passes`, `select_full_join`, `select_scan`, `lock_time`,
  `timer_wait`, `errors` and `warnings`. Each `server_stats` also carries
  `analyzable` (boolean) and `analyze_reason` (the converter's refusal sentence
  when false, empty string when true), including when history is unavailable.
  Eligibility calls `analysis_query.prepare` on the statement's sent text
  without connecting or executing SQL. It accepts exactly that converter's
  SELECT/WITH and supported simple UPDATE, DELETE and INSERT/REPLACE SELECT
  projections; SET, SHOW and unsupported DML remain ineligible. Execution errors
  do not remove eligibility from convertible SQL. The editor uses these fields
  to gate Explain analyze; the capture checks remain authoritative.
  Unsigned counters and `event_id` are
  decimal strings to preserve BIGINT precision; `timer_wait` alone may be
  null; timers keep the server's picosecond units. The verdict (frontend):
  Watch when `rows_sent > 0` and `rows_examined > 10000` and `rows_examined >
  100 * rows_sent`, or `created_tmp_disk_tables > 0`, or `select_full_join >
  0`; Problem when `errors > 0`. The data comes from
  `performance_schema.events_statements_history` of the tab's own thread,
  `EVENT_ID` above a pre-run `MAX(EVENT_ID)` boundary, statement nesting
  level zero with stored-program child events excluded, myflames' own tagged
  helpers excluded, `LEFT(SQL_TEXT, 4096)`, the newest 1,000 events read
  (`ORDER BY EVENT_ID DESC LIMIT 1000`, re-sorted ascending). Attribution is
  conservative: when `len(units) == len(rows)` and every pair matches in order
  (`all(exact(u, r) for u, r in zip(units, rows))`), units are attributed
  positionally (including repeated identical statements); otherwise a history
  row must match a unit's exact sent text uniquely in both directions, else the
  unit reports the statement was not retained unambiguously; duplicate event IDs
  abort attribution. Collection runs after
  a script and inserts no helper between user statements, so `ROW_COUNT()`,
  `SHOW WARNINGS` and other intra-script diagnostics are preserved. Consumers
  and instrumentation are never enabled automatically; disabled collection
  returns `available: false` with the reason and a link to Server >
  Instruments. Helper reads use the 30 s helper timeout.
- `POST /api/editor/status` accepts an optional `refresh_insights: true`
  beside `tab_id` and the connection ownership fields; it returns the
  ordinary status/run envelope plus an optional
  `session.transaction_meter`. The refresh tries the existing run lock
  without waiting, skips while a run is active, throttles to one refresh per
  10 seconds per tab, never opens a new connection and never fills a
  password; other status requests remain cached. `transaction_meter` is null
  when no transaction is open; otherwise `{age_seconds, age_source ("innodb"
  or "observed"), rows_modified, rows_locked, lock_structs, blocking,
  blocking_truncated, note}` with the counts as exact decimal strings or
  null. It reads one `INNODB_TRX` row by `CONNECTION_ID()` (a PROCESS denial
  or a missing row leaves an observed lower-bound age with a note), then one
  bounded read of at most 501 distinct requesting transaction IDs:
  `blocking` caps at 500 with `blocking_truncated` marking a lower bound.
  MySQL 8.0.1+ reads `performance_schema.data_lock_waits WHERE ENGINE = 'INNODB'` filtered to the
  tab's blocking thread; MySQL 5.7 and MariaDB read
  `information_schema.INNODB_LOCK_WAITS` filtered to its transaction ID. It
  counts waiting InnoDB transactions (including InnoDB table-lock waits; metadata-lock waits are outside its coverage, and Server > Locks has the wider view) and
  is a sampled observation, not a promise that locks cannot change. The
  frontend polls only the active, visible editor tab with a known open
  transaction, every 10 s, pausing while SQL or a transaction action runs,
  while the document is hidden, or after a navigation; a failed poll clears
  the counters and points to Server > Locks rather than showing stale values,
  and the meter's age is extrapolated between samples. The transaction
  popover keeps Commit, Roll back and **Show who waits**; **Explain analyze**
  on a statement opens the existing Run analysis review. It is shown only when
  `server_stats.analyzable` is not false (absent on an older backend means
  shown); otherwise the stats line's title is `server_stats.analyze_reason`.
  A statement with status `error` starts its line with **Statement failed**. Commit, Roll back
  and Run analysis keep their review, confirmation, privilege and audit
  paths. Tests: `test/test_editor_session.py`,
  `apps/workspace/test/editor-insights.mjs`.
- Result filtering, pinning and compare (`apps/workspace/src/resultFilter.ts`,
  `ResultsPane.tsx`): the **Filter loaded rows** field filters the loaded
  rows in memory (case-insensitive substring over every column,
  `FILTER_DEBOUNCE_MS` 120 ms), the summary reads "12 of 1,000 loaded rows"
  (never implying the server's table), and Clear or Escape clears the filter
  and the chips. The cell menu (right click, Shift+F10 or the row's …
  button) adds column-scoped chips, **Show only this value** and **Hide this
  value**, combined with AND against the text filter; NULL and the empty
  string stay strictly distinct (the substring filter never matches NULL),
  and untrusted values render in `bdi`. While a filter or chip is active,
  selection totals, ⌘C/Ctrl+C and the copy menu actions, and Export work on
  the filtered rows (copy in display order; export in original loaded order,
  sent to `/api/editor/export` as the filtered slice), and each row keeps its
  original 1-based number. **Pin result** keeps the result's columns and up
  to `MAX_PIN_ROWS` (5,000) loaded rows in memory, scoped to the tab and
  pruned when it closes (badge "Pinned · N rows", or "Pinned · 5,000 of N
  rows" when capped). With matching column names and order (`sameColumns`),
  **Compare with pinned** opens the compare view (`ResultCompareView` inside
  `ResultsPane.tsx`): a Key column selector (an exact or case-insensitive
  `id` column first, else the first column), a summary of added, removed,
  changed and unchanged counts (plus ambiguous when a key repeats), and rows
  marked `is-added` / `is-removed` / `is-changed` with changed cells showing
  before → after (NULL and empty string distinct). Nothing runs on the server
  again; both sides are the loaded rows. Tests:
  `apps/workspace/test/result-filter.mjs`.

### Run

The execution tree's **Self of total** column uses the operator's self time
divided by the plan's total time. `output_tree.py` and `charts/tree.ts` draw
the fill over a hairline baseline, with matching column text and scale; the
baseline still shows when the self-time fill is too small to draw. The
**Total** column prints every row's inclusive time (`_format_time(total_time)`
in Python, `treeTime(total_ms)` in TypeScript), leaves included: the old rule
left it blank when `|total - self| <= 0.001`, which was de-duplication, not a
measured-or-not rule, and made the hottest leaf look like a gap. Unmeasured
operators print `—` in Self and Total (never blank, never `0 ms`). The details
strip and search text (`_row_info` / `rowInfo`) still add `Total:` only when it
differs from self. Tests: `test_measurement_semantics.TestTreeTotalColumn`,
`charts.mjs`, `parity.mjs`.

`python3 -m myflames --type [flamegraph|bargraph|diagram|tree|workbench] explain.json > output.svg`

`parser.parse_explain` rejects (ValueError, exit 2) a plan whose numeric fields
(`_PLAN_NUMBER_FIELDS`: `actual_*`, `estimated_*`, MariaDB `r_*`, `rows`, `loops`,
`cost`, format-1 `rows_examined_per_scan`) hold anything but null or a finite
number: measured counters up to 2^64, measured times up to 1e15 ms, estimates
any finite value. The check runs before any arithmetic. `load_explain_json`
uses text that parses as JSON unchanged; CLI decorations (a leading `EXPLAIN:`
label before the JSON, table borders, the column header) are stripped only
otherwise. MariaDB 10.7 and earlier (10.6 in practice) write the members of a
join as repeated `"table"` (or `"block-nl-join"`, ...) keys in one object
(10.8.1 and later write a `nested_loop` array, MDEV-27036); `parser._join_members_hook` turns an object with more than one member
key into a `"nested_loop"` array in written order, so no table is dropped.

#### Plan measurements

Unavailable is not zero. The parsed tree keeps `details.actual_rows` as None
(and `rows_measured` false) whenever the plan has no counter for an operator;
`node["rows"]` stays 0 for arithmetic only. chart-data emits null, the metric
reads "Not measured", and the bar chart and tree details print
`Rows: not measured`, or `Rows: not executed` for a MySQL operator that never
ran (null rows and loops) (Python and `charts/bargraph.ts`, `charts/tree.ts`).

- MySQL: `actual_rows` is rows per loop, `actual_loops` the init calls; both
  null for an iterator that never ran (`explain_access_path.cc`).
- MariaDB table: `r_rows` is rows read per scan (before the attached
  condition), `r_loops` the scans (0 kept: never scanned); `r_rows: null`
  stays None. `details.rows_returned` is `r_rows x r_filtered / 100`, the
  rows that passed the condition; a one-table root's `rows_sent` uses it.
  The estimate `loops` (11.0.1+) is `details.estimated_loops`, never
  `actual_loops`; `details.estimated_rows_out` is `rows x filtered / 100`.
- MariaDB synthetic joins (the folded `nested_loop` array): MariaDB has no
  counter on the join itself. The rows a join returns are the next table's
  `r_loops` (sub_select counts one per incoming row), or the `block-nl-join`
  `r_loops` when that table uses a join buffer (one per buffered row,
  MDEV-30806, 10.6.13+; the table's own `r_loops` counts refills). The last
  join of a select, and weedout and FirstMatch/LooseScan wrappers, are not
  measured. Sorts use `r_output_rows` (null stays None).
- MariaDB set operations: with a result table, rows and loops are
  `union_result.r_rows` / `r_loops` (read by the fake select after
  INTERSECT/EXCEPT removed rows, `sql_union.cc`); null stays None. An OFFSET
  on the whole unit is read and counted there but not sent, and the plan
  does not show it. UNION ALL without a result table is not measured: a
  LIMIT/OFFSET on the unit is applied when sending (`select_union_direct`)
  and not shown, and table members count rows before their WHERE.
- Engine: `parse_explain` sets `root["engine"]` (a query_block plan with
  MySQL format-1 keys is MySQL); the CLI uses a capture's recorded engine
  and server version first (`cli._plan_engine`), never the envelope.
- Sidecar `plan_summary` (1.4): `rows_examined_estimate` sums rows times loops
  over leaf operators; `total_time_ms`, `rows_sent` and
  `rows_examined_estimate` are null when the plan does not carry them (the
  digest prints "rows not measured", the HTML "Not measured", the executive
  summary "rows not measured" or "rows returned not measured").
- `full_scan`: a measured scan is exempt below 100 rows times loops; an
  operator without a counter in an executed plan never ran. A plan with no
  row counters is judged by `estimated_rows` times estimated loops
  (`parser._estimated_loops`: a nested-loop inner side runs once per
  estimated outer output row, for MariaDB `rows x filtered`, or MariaDB's
  printed `loops` from 11.0.1; a table under a join buffer counts one read
  because EXPLAIN prints no refill count), labeled "about N rows, estimated"
  (`estimated: true`).
- Hash joins: the build-phase count is the measured rows of the MySQL build
  input (the child with heading `"Hash"`, kept as `details.heading`); for
  MariaDB BNLH it is the rows put in the join buffer over all refills (the
  `block-nl-join` `r_loops`, 10.6.13+, not shown before). The plan's join
  buffer advice is per engine: MariaDB reads the inner table once per
  refill and does not spill. A hash join on an equality key is not a
  non-sargable finding (it hashes each row once); a hash join without an
  equijoin condition and a Block Nested-Loop join still are.
- Sorts: a spill is stated only from merge passes (every merge of on-disk
  sorted runs counts one, filesort.cc; a sort that fits makes none): the
  capture's `statistics.sort_merge_passes` (else its `Sort_merge_passes`
  status delta), or MariaDB ANALYZE `r_sort_passes` (a rounded per-run
  average printed only when nonzero) kept as `details.sort_merge_passes`
  = about `r_sort_passes x r_loops` (0 for an executed sort without it,
  None without ANALYZE). The plan advice is `parser.SORT_SUGGESTION` (index
  first; raise sort_buffer_size only if merge passes show a merge), or
  `SORT_SUGGESTION_IN_MEMORY` with 0 passes. `parser.restate_sorts` rewrites
  the warning and advice from a capture's counter ("sorted in memory (0 sort
  merge passes measured)"). `advisor._rule_sort_buffer_vs_filesort` reports
  measured passes (its advice replaces the plan's), stays silent at 0, and
  otherwise only notes the buffer size; the executive summary then says the
  plan does not show whether it spilled.
- Temporary tables: no plan format reports a spill, so the warning and the
  summary name neither a spill nor an on-disk engine. The warning's size is
  the most rows the input wrote per materialization (its measured output, an
  upper bound), omitted when unknown. The advisor names the limit per
  engine: "the smaller of tmp_table_size and max_heap_table_size" for MEMORY
  and MariaDB, "tmp_table_size" for TempTable. When the advisor knows the
  server variables, its temporary-table advice replaces the plan-level one
  (`parser.TEMP_TABLE_SUGGESTION_MYSQL` / `_MARIADB`).
- Docs links (`findings._docs_for`): the catalog keeps 8.4 links;
  findings for a MySQL source with a known version use that manual (8.0,
  8.4, 9.7, 26.7, checked 2026-10-19; 8.0 names the semijoin page
  `semijoins.html`) or the version-neutral `refman/en/` path. A MariaDB
  source links `findings._MARIADB_DOCS` (mariadb.com/docs pages checked
  2026-10-19) and gives no link where no MariaDB page fits (non-sargable
  joins, temporary tables, other).
- Escaping: `parser.xml_escape` is the one escape helper; it replaces
  XML-invalid characters with U+FFFD. `flamegraph._esc` and the renderer
  attribute helpers build on it. Flame tooltips are enhanced by
  `parser.enhance_flame_titles`, one exact operator lookup per frame
  (`data-node-id`, else `data-label`).
Root rendering uses the shared connection flags and `--profile` resolution. `-e/--execute` enables live mode with 127.0.0.1:3306 defaults; supplying connection flags without SQL exits 2. Explicit flags override profile fields and a saved password is resolved normally. The existing single-SELECT validation and read-only transaction guard apply before execution. Root, capture and the server runners in `workspace_cli.py` preserve `--ssl-cert`, `--ssl-key` and `--mysql-binary` through `_connection_options`; capture adds those CLI-only paths to `LiveJob.options` without changing its JSON schema.

An unreadable `--query-file` and a failed explicit `--sidecar PATH` exit 2. Automatically derived sidecars and lesson bundles remain best-effort. `guide [-o PATH] [-q]` parses its arguments and rejects unsupported flags. `teach LESSON` and `teach --index` / `--catalog` use `_write_output` for stdout/file routing, quiet status and exit 2 on write failures.

### Plan Stats and shared-plan literal hiding

`chart_data.build_chart_data` adds optional `stats` and `groups` to
`chart-data-1.0` and `chart-data-2.0`, and a `group` to each node; both JSON
Schemas describe them, and existing fields, versions, node identities and input
order keep their meaning (reports and explore pages still embed 1.0, the
workspace receives 2.0). Python computes every aggregate
(`chart_data._aggregate_stats`):

- `stats.tables` and `stats.operators` rows have `label`, `count`, `node_ids`,
  `self_ms`, `share`, `actual_rows`, `estimated_rows` and `loops`; table rows
  add `schema`, `table` and `operators`. `stats.note` states the units.
- `self_ms` sums the canonical self times the other charts use, over all loops,
  and never adds inclusive times. `share` is a fraction of the root's elapsed
  time, not a percentage of the largest operator, null without a positive
  measured root, and can exceed 1 because separate branches can overlap the
  root's timing scope.
- `actual_rows` counts rows produced across accesses and loops, not distinct
  table rows: MySQL actual rows per loop times loops; MariaDB `rows_returned`
  when present (its reported filter already applied) times loops.
  `estimated_rows` sums the estimates per loop and never multiplies them by
  actual loops. One missing counter makes that aggregate null rather than a
  partial sum. Without ANALYZE, counts and estimates remain; times, shares,
  actual rows and loops are unknown.
- A group key is the JSON tuple of the normalized operator type (case-folded,
  without conditions, table or index suffixes and occurrence values), schema,
  table and index: predicates never split a group, while another table,
  schema or index does. `groups` maps each key to an aggregate over the whole
  original plan. Focus and collapse never change aggregates, and operator
  order is unchanged.

The workspace's **Stats** (`PlanStats.tsx`) is a view of Plans, not a renderer
in `MyflamesCharts.availableViews()`; it uses kit DataTables outside the chart
runtime ("Time per table", "Time per operator type"). Selecting a Stats row
selects its first operator and highlights all of its `node_ids` on return to a
chart, clearing focus and collapse so the matches show. **All occurrences** in
the workspace operator inspector (`ContextPanel.tsx`; every chart view and both
Compare panes; `AllOccurrences` in `PlanStats.tsx`) keeps the group's count,
Self time in total, Share of plan time and Back to measurements, and lists the
members as a `listbox` of selectable two-line rows that fit the inspector,
sorted by numeric `self_ms` descending with unknown last (it sorts a new
presentation array, never plan inputs). A row shows the last two ancestor
labels of its path, drawn from existing parent links (the full path in its
`title` and accessible name; the current operator is the inspector heading),
then self time, the share of the unprojected root total (never a focused
branch's), and per-loop rows × loops, with the existing time and count
formatting and tabular numbers; missing measurements and a zero plan total stay
Unknown or Not measured, never NaN or Infinity. Arrows, Home and End move focus
(roving tab index); Enter, Space or a click select through the existing
operator callback, which reveals the node in the chart. Tests:
`plan-occurrences-unit.mjs` (the real component and handlers, no browser) and
`plan-occurrences.mjs` (rows, accessible paths, keyboard, overflow). HTML
reports have collapsed Stats tables with the same aggregates; exporting HTML
while viewing Stats uses Visual Explain plus that section. Standalone `explore`
pages keep their chart and metric list.

Narrow Plans and one-plan Compare: at 900 px and below the operator inspector
(`ContextPanel.tsx`) with nothing selected keeps its section tabs (Findings,
Lessons and Operators need no selection); only while Measurements is chosen
(`data-empty` is `!selected && tab === "measurements"`) the empty title and
Measurements body give way to the one line "Select an operator to see its
details", with the metric mount kept in the DOM, and the empty Explore chart
fills the window above that hint and the footer. That fixed-height layout
(`.plan-screen:has(.plan-inspector[data-empty="true"])` in `style.css`)
excludes Stats (`:not(:has(.plan-statistics))`): a page of tables keeps
scrolling with the page. Choosing an operator restores
the inspector. The inspector sits in flow below the chart at widths of 1,000 px
and below. In Compare, the first imported plan selects Before and leaves After
empty; later imports select After; opening a one-plan investigation or
restoring an older one-plan session also leaves After empty, and the empty
state's **Import plan…** calls the existing `openImport`. Tests:
`plan-editor-unit.mjs` (no browser) and `plan-editor-layout.mjs` (four chart
modes, empty and selected inspectors, one-plan Compare and keyboard import at
1440x900 and 720x600 in both appearances).

Literal hiding: `plan_privacy.hide_literals(text, backslash_escapes=None,
ansi_quotes=False)` replaces SQL strings, decimal, exponent, hex and bit
literals and quoted date and time values with `?`; signs stay operators, and
NULL, TRUE and FALSE stay keywords. Placeholders and identifiers (backticks
with doubled or escaped quotes) stay. Comments, executable comments included,
become `?` entirely, because they can hold any private text. An unknown
backslash mode (the sharing default) hides the union of both readings; double
quotes are strings unless ANSI_QUOTES is known. `parser.hide_plan_json_literals`
uses the canonical JSON loader (MariaDB's duplicate join members included) and
never parses EXPLAIN elsewhere; it redacts, element by element in lists, the
fields `operation`, `condition`, `attached_condition`, `index_condition`,
`pushed_index_condition`, `having_condition`, `const_condition`,
`hash_condition`, `ranges`, `range`, `sort_key`, `sort_fields`,
`grouping_operation`, `expression`, `cache`, `query`, `query_text`,
`query_sql` and `sql`, and `message` with the prose rule below.
`parser.hide_parsed_plan_literals` keeps node IDs and timings for explore's
selection and focus. Sidecars also hide literals in `raw`, `beautified`,
`ddl`, the labels (`short_label`, `full_label`, `folded_label`, `node_labels`,
`label`; canonical label rebuilding keeps identifiers and measurement
annotations), `query_text_lines` and collected `schema` DDL with the SQL rule,
and in generated prose (`executive_summary`, `why`, `action`, `rationale`,
`note`, `reason`, `text`, `message`, `explanation`, `description`, `hint`,
`summary`, `recommendation`) with the prose rule: quoted values and the
numeric operands of `=`, `<>`, `!=`, `<`, `>`, `<=`, `>=`, `<=>`, LIKE, NOT
LIKE, IN lists and BETWEEN are hidden, also inside copied parenthesised
conditions, while counts, comma-formatted row totals, timings, percentages,
loops, versions, contractions, possessives and number references such as
`Query #12` stay. In prose too, double-quoted text is a string unless
ANSI_QUOTES is known, so a quoted left operand is hidden; backtick names stay.
Collected schema redaction scans the DDL and expression fields (a column's
`rest`, DEFAULT values, unquoted structured defaults included, generated,
CHECK and partition expressions, view and routine definitions and bodies, and
functional index expressions) and keeps structured table, column and index
names, type text and key-part lengths. Custom report and comparison titles are
hidden before the markup, metadata and embedded chart data are built. HTML
reports and CLI sidecars are redacted after collected evidence and generated
prose have been added. A comparison's matching, confidence, statuses, node
IDs, deltas and verdict counts always come from the original plans; only its
presentation is redacted, the same in the API, CLI JSON, `--digest` and
HTML. This is display redaction with lexical context, not a natural-language
parser or identifier anonymization, and its result is never executable SQL.
Numeric metric fields, node IDs, table, schema and index names, versions and
collected variables stay.

`POST /api/report` accepts `hide_literals` (boolean, default false for older
callers) and an optional `query`; `format: "json"` returns `{analysis}` (the
existing sidecar), redacting an optional validated `analysis` sidecar on a copy
(evidence, timestamps and node references kept) or building one from the plan.
`POST /api/compare` accepts `hide_literals` and returns the existing
`{analysis, html}`. Neither endpoint is new; the original plans, editor SQL,
captures and investigations are never changed, and export retries keep the
literal-hiding choice. The workspace's Export menu choice **Hide literal
values** (a `role="menuitemcheckbox"` item from the shared `ExportMenu`
`toggles`, above the formats and part of the menu's arrow-key and Tab
navigation; Space or Return toggles it and the menu stays open, which opens on
the first format) starts checked and applies to HTML and JSON; HTML, explore and comparison pages then say **Literal values
hidden**. CLI: `--hide-literals` ("Replace literal values with ? in shared plan
HTML and JSON.") on the root command (HTML and its sidecar, applied after any
live execution or capture, never to the SQL sent), `explore` and `compare`
(HTML, `--json` and `--digest`; the deprecated `diff` alias inherits it). The
live `report` command is a server report and has no such flag. Tests:
`test/test_plan_analysis.py`, `test/test_cli_contract.py`,
`test/test_chart_data.py`, `apps/workspace/test/charts.mjs`, `parity.mjs`,
`browser.mjs` (an export with the option off keeps the original sidecar, and a
plan with private predicates exported with it on hides them while every
summary measurement stays).

### Digest

- **Digest** (AI-era): `python3 -m myflames digest explain.json`: emits the compact, source-grounded digest of a plan (pipe to an LLM). `--cost` shows the token + $ saving vs pasting the raw plan; `--show-prompts` prints both prompts; `--tokenizer {heuristic,claude,gpt}` selects how tokens are counted (`claude` = Anthropic `count_tokens`, needs `myflames[tokens]` + `ANTHROPIC_API_KEY`; `gpt` = tiktoken, keyless, needs `myflames[gpt]`); `--json` for machine output (implies `--cost`, conflicts with `--show-prompts`; `--cost --json` is valid). Lives in `myflames/digest.py`. (Deprecated alias `tokens` still works: it warns on stderr and defaults to `--cost`.)
### Plan diff

`diff` remains a deprecated alias; it warns on stderr unless quiet. `--json` and `--digest` are mutually exclusive.

`python3 -m myflames compare before.json after.json` (alias of `compare`). `--digest` = token-cheap text diff for an LLM; `--json` = structured `compare-1.0` delta.
### CI gate

`python3 -m myflames check explain.json --fail-on full_scan,filesort`: exits 1 if a trigger matches (categories, severities, or `any`), 0 if clean, 2 on bad input. For pre-commit/CI/agent loops. Lives in `myflames/findings.py`. An estimated plan (EXPLAIN without ANALYZE) is gated on its estimated rows (see Plan measurements), so a large estimated scan matches `full_scan`.
### Advise

`python3 -m myflames advise explain.json [--json]`: ranked warnings + suggestions with `confidence`. Also `myflames/findings.py`. (Deprecated alias `findings` still works: it warns on stderr.) The single "Fix first" pick is `findings.primary_suggestion_index` / `build_findings` (one ranking policy, shared by the HTML card and the sidecar `primary_action`).
### Rule controls

`check` and `advise` take `--ignore LIST` (drop categories; unknown names exit 2) and `--list-rules` (prints `findings.RULES`: category, default severity, description, docs link). Every warning category in `output_sidecar._WARNING_CATEGORIES` must have exactly one `RULES` entry. `advise` text ends with a summary line; JSON findings carry `docs`.
### Advisor evidence rules

- **Advisor evidence rules** (`advisor.py` `_CONTEXT_RULES`, called with a context of `freshness` + `capture`): `STALE_STATS` (category `misestimate`), `REDUNDANT_INDEX` (`redundant_index`), `COLD_CACHE` (`cold_cache`); `RULE_CATEGORIES` maps them into the sidecar, other environment rules stay `env`. The plan-level row-estimate rule is `parser._collect_misestimates` (10x, 1,000 rows; skips overestimates below Limit/semijoin inner sides; MariaDB compares only ref/eq_ref). `parse_explain` tags `root["engine"]`.
- **Misestimate marks** (`parser.py`): `_row_misestimate` flags an eligible measured base-table access (or a filter directly above an unflagged leaf access) when its actual rows per loop are at least `_MISESTIMATE_RATIO` (10) times off the estimate in either direction and the larger side times loops reaches `_MISESTIMATE_MIN_ROWS` (1,000); under an early-stop ancestor (Limit, semijoin/antijoin inner side) only underestimates are reported. MariaDB compares only `ref`/`eq_ref` access types (after normalization, which folds EQ_REF/REF_OR_NULL/CONST/SYSTEM into `ref`) and only underestimates, because its plans hide LIMIT. Estimated plans never report misestimates (the rule needs measured rows), so an estimated capture gets no marks. `parse_explain` stores the record in `details["misestimate"]` (`_annotate_misestimates`, same `_misestimated_nodes` walk): `direction` (under/over), `factor`, `estimated`, `actual`, `loops`, `table`, `kind` (access/filter), `label` ("rows ×N under", compact N), `text` and `reason`. Projections and literal-hiding copies keep the record; `parser.build_flame_entries(root, node_identity=True)` uses `operator:<node_id>` for each path component of a standalone flame entry, `misestimate_marks(root)` returns the `operator:<node_id>` flame tokens, and `parser.enhance_flame_titles` matches `data-node-id` (fallback `data-label`) so distinct operators with identical labels never merge or share marks or tooltips. `chart-data-1.0` and `2.0` add an optional node `misestimate` (the record plus evidence or null) only on flagged nodes; `details_text` adds "Row estimate" (plus "Statistics" and "Suggested SQL" when evidence exists) and `flame_details` adds "Row estimate". `advise()` adds `analysis["statistics_evidence"]`: `{plan table: {table, last_update, n_rows, histograms, text, statement}}`, matched by `advisor._stale_entries` (a qualified MySQL `schema.table` looks up `freshness[table]` directly; an unqualified identity looks up `_captured_base_tables(capture)`, which resolves only unaliased `schema.table` references from a single flat MariaDB `SELECT` with no CTEs, subqueries, set operations, table functions, hints or executable comments that appear once; unidentified or aliased tables get `evidence: null` and omit `ANALYZE TABLE`), with `statement` ``ANALYZE TABLE `s`.`t`;`` (backquotes doubled), data only and never executed. Tests: `test/test_misestimate_marks.py`, `test_advisor_extensions.py`, `apps/workspace/test/misestimates.mjs`.
### Find slow queries

`top`, `sessions` and `io` accept a single `--schema`; a repeated option exits 2 before connecting. Empty text results from `top`, `running`, `profiles list` and `audit list` emit table headers through the configured output destination, with explanatory notes on stderr unless quiet.

`python3 -m myflames top` (P_S digest summary, `top-1.0`) and `python3 -m myflames running [--explain ID]` (`running-1.0` / `explain-connection-1.0`; deprecated alias, use `sessions`), in `myflames/discover.py`; read-only, share `capture`'s connection flags. UI: the Workload place (`Server.tsx` with `place="workload"` wraps `SlowQueries.tsx`), `/api/live/top|running|explain-connection`; browser test `apps/workspace/test/discover.mjs`.
  Capture from a digest: `python3 -m myflames capture --statement-digest HASH` (full or 8+ hex prefix; exclusive with `-e`; `--digest` is a deprecated alias that warns on stderr) looks the row up with `discover.find_statement`, runs the sample text in the digest's schema when the connection has none, and records `source: {"kind": "digest", "digest", "schema"}` in capture-1.0 (`live.normalize_source`; `source` may also be a text label such as import-shell's `"mysqlsh-diagnostics"`). No runnable sample (MariaDB, or MySQL `sample_truncated`) exits 2 and prints the SQL with `:p1`, `:p2`. Every `top-1.0` row carries `capture_problem` from `live.capture_problem`, the same SELECT-only rule `bind_query` enforces; never duplicate it in the UI. Placeholder rules live twice, in `discover.parameterize` / `digest_query` / `join_function_calls` and `apps/workspace/src/placeholders.ts`; both run the shared cases in `apps/workspace/test/fixtures/digest-placeholders.json` (`test/test_digest_capture.py`, `test/placeholders.mjs`), so change them together. Digest text needs built-in function names rejoined to their `(` (`COUNT ( * )` is a syntax error; names from `sql/lex.h` SYM_FN). Every keyword token in digest text carries a trailing space (`gen_lex_token.cc` `m_append_space`), while a stored function is an identifier and prints quoted, so an unquoted `SUM (` is the built-in: check `digest_query(item)["sql"]` through `discover.digest_capture_problem`, never `["text"]`; raw SQL keeps the strict `dbops` check. Capture errors drop the wrapped script's "at line N" and map MySQL error 1235 for EXPLAIN ANALYZE below 8.4 to a version message (`live.client_error_text`); `server_info.unsupported_version_note` returns the one-line note for servers below MySQL 8.4 or MariaDB 10.11 (capture `warnings`, lifecycle plan `warnings`). Live CLI reports emit that note once on stderr unless `-q`: `_discover_run` uses the connection's cached version or the returned report's `engine` and `server_version` (`version` for Privileges), without an extra query. `upgrade-check` keeps its separate upgrade guidance. UI row actions Explain / Run analysis (confirmation first) and Explain top N (EXPLAIN only, sequential, max 20) live in `SlowQueries.tsx`; row actions open the Query editor through `QueryLabHandle.openDigest`; tabs keep `source`/`fill`; browser test `apps/workspace/test/digest-actions.mjs`.
  Workload attribution (`discover.py`, additive to `top-1.0`): `--order full_join` (`SUM_SELECT_FULL_JOIN`, then `SUM_SELECT_RANGE_CHECK`), `scan` (`SUM_SELECT_SCAN`) and `no_index` (`SUM_NO_INDEX_USED`) list only digests where that counter is above zero (`ORDER_FILTERS`), with ties broken by total time. `--table SCHEMA.TABLE` (`parse_table_name`; backquote a part with a dot) adds `TABLE_FILTER` (`SUM_NO_INDEX_USED > 0 OR SUM_SELECT_SCAN > 0`) and a `LOCATE` prefilter on the table name, reads up to `TABLE_CANDIDATES` (500) digests, and keeps those `table_reference` matches: `` `schema` . `table` `` or `schema.table` from any default schema (`qualified`), the bare name only when the digest's `SCHEMA_NAME` is the table's schema (`unqualified`), case-insensitive, never another schema's table or a longer name. Every row gains `select_full_join`, `select_range_check`, `select_scan` (read after `QUERY_SAMPLE_TEXT`, so older answers read as 0); with a table filter each row has `table_match {schema, table, kind, label: "text match, not proof"}` and the envelope `table` and `match {label, caveats, candidates, capped}`. Every envelope has `coverage {uptime_s, first_seen (oldest listed), digest_lost, overflow {executions, total_ms} | null}` (best effort; `Performance_schema_digest_lost` and Uptime from one `SHOW GLOBAL STATUS`). `/api/live/top` takes `order` (the seven values) and `table` (`"schema.table"` or `{schema, table}`). Never present a table match as proof: views, routines, aliases and digest text cut at `performance_schema_max_digest_length` break it, and the counters are per statement, not per table. Tests: `TestTableReference` and `TestWorkloadAttribution` in `test/test_discover.py`.
  Statement text limits (`discover._text_limits`, additive `text_limits` in `top-1.0` when `server_context`): when a listed sample was cut (`sample_truncated`) or a digest text ends with "..." (the parser appends it when it stops at `max_digest_length`, `sql/sql_digest.cc:333`), the envelope names the startup parameter, its value, a suggested value (the smallest of 4096, 8192, 16384, 32768 and 65536 above it) and an estimate of the extra Performance Schema memory: `{sql_text: {name, value, cut_rows, suggested, estimate_bytes, estimate_basis}, digest: {names, values, cut_rows, suggested, estimate_bytes, estimate_basis}, persist_only}` or null. Three best-effort reads at most, never one per row: `SHOW GLOBAL VARIABLES` for the sizing variables, `SHOW GLOBAL STATUS` for `Max_used_connections` (MariaDB has no `performance_schema.global_variables`; ERROR 1146 on 11.8) and `memory_summary_global_by_event_name`. `performance_schema_max_sql_text_length`: the registered `memory/performance_schema/events_statements_{current,history,history_long}.sql_text` bytes scaled by new/old, plus `performance_schema_digests_size` x (new - old) for the digest samples, whose instrument is declared but never registered (`pfs_builtin_memory.cc`); without the memory table, a formula over `history_long_size` and threads x (`history_size` + `performance_schema_max_statement_stack`). Digest text is limited by the smaller of `max_digest_length` and `performance_schema_max_digest_length`: `suggested_values` raises each to the suggestion and keeps a value already larger (never lowered); the estimate scales the stored digest buffers by the new `performance_schema_max_digest_length` alone and adds one `max_digest_length` growth per connection (`sql_class.cc:894-896`). A refused read leaves `estimate_bytes` null with the reason, never an error. `persist_only` is MySQL 8.0+ (`SET PERSIST_ONLY`). The report note names the fix. UI (`SlowQueries.tsx` `TextLimitFix`, in the Statement section): what was cut and why, the suggested value with the estimate, and copyable option-file lines plus the `SET PERSIST_ONLY` statements on MySQL 8.0+; the Parameters view performs that change as a reviewed write. Live on MySQL 9.7.2 (2026-11-08): two cut samples, suggestion 4096, measured estimate. Tests: `TestTextLimits` in `test/test_discover.py`, `test/test_ps_instruments.py`, browser `apps/workspace/test/discover.mjs`.
  Index suggestion, the shared statement inspector and Measure again (`myflames/index_hint.py`, `POST /api/live/index-suggestion {connection, digest, schema}`, a read-only server view path; `StatementInspector.tsx`, `statementThread.ts`, `MeasureAgain.tsx`, `IndexSuggestion.tsx`): **Workload > Top statements** (`source="pfs"`) and **Query digest** (`source="pfs" | "table" | "file"`) share `StatementInspector`, which renders sections in one order: **Statement** (full or sample SQL with `CopySql`, a normalized-text `?` hint when `sample_text` is absent, and source-specific extras), **Latency** (Total, Average, Longest, and when present P95, P99 and P99.9 upper bounds, plus an on-demand **Latency histogram** via `StatementHistogram` calling `POST /api/live/digest-histogram` on `pfs` when quantiles exist, or scrolling to the inline histogram in Query digest), **Rows** (Examined per execution, Examined per row returned or a zero-rows-sent note, Rows examined, Rows sent), **Flags** (`no_index_used`, `no_good_index_used`, `tmp_disk_tables`, `sort_merge_passes`, `low_selectivity`), **Suggested index** (shown when `source === "pfs"`, `row.digest` is present, and `flags` includes `no_index_used` or `full_scan`, read only after **Read suggestion** is clicked, aborted on row change, cached in memory for 120 s for up to 200 connection, digest and schema keys, never caching an `unknown` result, reused on reopening while fresh, with **Read again** forcing a fresh read; gated by the `explain` privilege), **Actions** and **Source notes**. `discover._digest_rows` adds `index_hint` to a row only for the narrow text-proven case `SELECT * FROM t` with no WHERE, JOIN condition, GROUP BY or ORDER BY (`index_hint.no_predicate`: "No index can help if <table> is a base table" (the digest alone cannot tell a view), the empty-table remark when `rows_examined` is 0, and the call count), so that case needs no server read. Otherwise `read_suggestion` re-reads the statement by digest (`discover.find_statement`; a row recorded under another schema answers `unknown`), never accepts SQL from the client, applies `live.capture_problem` and `live.bind_query` (SELECT only; a cut sample or one with parameters to fill is `not_explainable`), refuses a sample calling `SLEEP` or `BENCHMARK` (planning can evaluate expressions, even inside a view; the explicit click authorizes the estimated-plan read), runs `EXPLAIN FORMAT=JSON` (estimated, never ANALYZE) in the digest's schema on the client's standard input (`run_write`: never argv, `--binary-mode`, no `--force`) inside `SET SESSION TRANSACTION READ ONLY; START TRANSACTION READ ONLY; ... ROLLBACK`, so it never runs when either read-only statement fails, takes `parser._suggest_indexes` from `parse_explain`, keeps a candidate only when its table resolves to exactly one schema-qualified source (CTEs and subqueries answer `unknown`), and reads `information_schema.STATISTICS` for those tables (exact-case unless `lower_case_table_names`, at most 4,096 key parts). Statuses: `no_predicate`, `suggestion` (`CREATE INDEX` with quoted identifiers, a unique `idx_<table>_<columns>` name within 64 characters, DESC kept from a sort candidate, `reason`, `caveats`), `covered` (an existing BTREE index without prefix parts leads with the columns; invisible or ignored ones are named so), `not_explainable` and `unknown` (errors redacted). No index is created: the DDL is copied or opened in the Query editor (`onOpenSql` / `openSuggestedIndex`, `data-action="open-index-suggestion"`), which opens the `CREATE INDEX` in a new tab, selected and not run, keeping `indexOrigin` (`statement`, `before: {avgMs, examinedPerExecution}`, `target: {key, label}`) in page memory only (never in saved sessions) and showing `DigestBefore` (`"Digest average before <avgMs>, rows examined <examinedPerExecution>"`) below the splitter. Once that exact single DDL statement succeeds in the tab (`indexWasApplied(rt.run, rt.indexDdl)`), the insight bar offers **Measure the statement again** (`data-action="measure-again"`, enabled while on the same connection), which opens the source statement in a new tab (or prompts to fill `:pN` parameters first if only normalized text was recorded) and opens the **Run analysis** confirmation sheet (`1` run in a read-only transaction). When **Run analysis** completes on the same connection and unmodified prepared SQL (`measurementMatches`, not an in-session temporary-table retry), the **Analysis** tab (internal key `plan`) renders `MeasuredStatement` (`data-measure-result`, `"Digest average before <avgMs>, rows examined <examined> → after <after.ms>, <after.rows> (this run; caches may be warm)"`) from the last captured run's `performance_schema.events_statements_history` `timer_wait_ps` and `rows_examined` (never substituting plan iterator timings or row estimates, and linking to **Server > Instruments** when statement history counters were not collected), plus **Compare plans** (`data-action="compare-measurement"`) when a previous non-session capture of the same statement on the same connection and database (`captureThreadKey`, up to 50 entries in page memory) exists. Tests: `test/test_index_hint.py`, `test/test_ui_http.py`, browser `apps/workspace/test/discover.mjs`.
The `slow-queries-1.0` report (`POST /api/live/slow-queries`,
`discover.slow_queries_report`) and Query digest's "Slow queries over 1 s"
ranking were retired in 3.3.1: Query digest's Slow log table and Slow log file
sources (`slow-log-1.0`, see "Query digest sources and Time range") read the slow
query log instead.

Query digest against the whole server: `discover.top_report(...,
server_context=True)`, the default for `/api/live/top`, `myflames top` and MCP
`top_statements`, adds one aggregate read, `_CONTEXT_SQL`: `COUNT(*)`,
`SUM(SUM_TIMER_WAIT)`, `SUM(COUNT_STAR)`, `MIN(FIRST_SEEN)` and
`TIMESTAMPDIFF(SECOND, MIN(FIRST_SEEN), NOW())` over
`events_statements_summary_by_digest` (the overflow row included, so it counts
as one digest), plus the `events_statements_cpu` consumer as a scalar subquery
(NULL on MariaDB). It is best effort: a refused or odd reply leaves the new
fields null and the list stands. `top-1.0` additions: `totals` (`digests`,
`total_ms`, `executions` of every digest row), `others` (the same three for the
digests the list does not hold, never negative, null when the list holds them
all), `cpu` (`{collected: true|false|null}` on MySQL when the rows carried
SUM_CPU_TIME: false while the consumer is off, null when unread; `cpu` is null
on MariaDB and on MySQL without the column), `coverage.counting_since` (the
oldest FIRST_SEEN of the whole table when counting began more than
`COUNTING_GAP_S`, an hour, after the server started) and, per statement,
`cpu_ms`, `p95_ms`, `p99_ms`, `p999_ms` (null where the server has no such
column) and `new`. The optional MySQL columns `SUM_CPU_TIME`, `QUANTILE_95`,
`QUANTILE_99` and `QUANTILE_999` are requested with server context on MySQL
only and dropped one at a time when the server answers Unknown column, never
chosen by version number. Each quantile is the upper bound of the histogram
bucket that holds it (`table_esms_by_digest.cc`); SUM_CPU_TIME grows only while
`events_statements_cpu` is ENABLED, which is off by default. `new` comes from
the server's own `FIRST_SEEN > NOW() - INTERVAL 24 HOUR` and is true only while
Uptime is above 86,400 s and, when known, the oldest FIRST_SEEN is older than
that too (after a digest table reset every statement would be new); the raw
flag is not sent. Both time comparisons use session-local time, so a daylight
saving change can shift them by an hour. Without server context the digest
statement and metadata reads are exactly as before: the Full analysis
attribution (`report._attribution`) passes `server_context=False`, so
`SHARED_METADATA_SQL` and the replayed recordings do not change.
`sample_truncated` also recognizes MySQL's cut marker (a sample ending in "..."
within 12 bytes of `performance_schema_max_sql_text_length`, or one at the
limit), with one note "Sample text was cut at N bytes
(performance_schema_max_sql_text_length)."; "Counting since <time>: the digest
table was reset or statements began after the server started." appears only
with `counting_since`.

`POST /api/live/digest-histogram` (read-only, in `_SERVER_VIEW_PATHS` and the
POST allowlist) takes `connection`, `digest` (32 to 64 hex characters,
validated before connecting) and `schema` (the statement's default schema;
empty or null matches `SCHEMA_NAME IS NULL`; sent as a hex literal), and
answers `digest-histogram-1.0` (`docs/schemas/digest-histogram-1.0.json`) from
`discover.digest_histogram`: the buckets of
`performance_schema.events_statements_histogram_by_digest` with
`COUNT_BUCKET > 0`, in bucket order (`bucket`, `low_ms`, `high_ms` null for the
open last bucket, `count`). MariaDB and a MySQL without the table answer
`available: false` with a reason; a denied read names the GRANT.
`ps_instruments.FEATURES["top"]` lists the histogram table and the optional
consumer `events_statements_cpu`, gated to MySQL by `consumer_engines` (like
`instrument_engines`): a default MySQL server keeps Top statements Ready and
names the consumer as optional, and MariaDB never names it.

Workspace (`QueryDigest.tsx`, `latencyHistogram.ts`, `query-digest.css`): Share
of total time divides by `totals.total_ms` when present (the header title and
the bar's accessible name say "of the server's total statement time"), else by
the loaded total (the slow log). The summary line keeps the loaded totals and
leads with the numbers: `{n} statements · {total} total · {executions}
executions · since {uptime} ago`, the "since" part only for Performance Schema
rankings with a known uptime, its `title` "Counted since the server started …
ago, or since the digest table was last reset."; the slow log sources show the
numbers and their range, and `· {n} shown` trails while a schema or text filter is
active. It stays within 120 characters. The ⓘ popover beside the title
(`sourceSentence`) names the table the report on screen read
(`performance_schema.events_statements_summary_by_digest` or `mysql.slow_log`,
or the digest table the slow-query read fell back to) and the counting window,
in at most six lines. `others` is one closing line under the table frame ("N
other statements · X% of total time · M calls"), never a selectable row. New
is an info `Pill` before the statement text and in the inspector, titled
"First seen <time>". Inspector fact lists (`Fields`) share one 40% label
column (the kit's 900 px stacking rule still applies below it); the digest row
and the CPU note span both columns (`dd:has(> .qd-digest)`, `dd:has(>
.qd-cpu-note)`, empty `dt` hidden). `DigestValue` middle-truncates the digest
in JS (the first 16 characters, "…", the last 8, when longer than 34) with the
full value in `title`, and its Copy button copies the full value. `SeenAt`
writes First seen and Last seen in the server's own clock: FIRST_SEEN and
LAST_SEEN carry no time zone, so their digits are parsed as UTC and formatted
with `timeZone: "UTC"` (`month: "short", day: "numeric", year: "numeric",
hour: "numeric", minute: "2-digit"`), never shifted to this computer's zone,
with "Server time: <exact value>" in `title` and the value in `dateTime`; never
call them local time. The toolbar puts Statements to load before the filter,
and a `.qd-toolbar-break` (a 100%-basis, zero-height flex item shown only by
the `@container view (max-width: 760px)` rule) keeps Rank by, Schema and Top on
the first control row and the growing filter and Export on the second; Refresh
stays in the shared `.toolbar-end` beside the title. CPU time is a column
hidden by default (`detailColumns={["cpu"]}`, shown from the Columns menu) and
an inspector fact; Not collected carries "events_statements_cpu consumer is
off" on hover, and one muted line under it reads "events_statements_cpu is
off." with an `InstrumentsLink` to
`consumer:events_statements_cpu` (Query digest provides
`InstrumentsLinkContext` from `main.tsx`'s `openInstrumentsRow`, like
Replication). The inspector's Latency section (MySQL rankings with quantiles)
shows P95, P99 and P99.9, then the histogram, read through
`readDigestHistogram` only when the section intersects the screen
(IntersectionObserver), the view is on screen and the report belongs to the
connection in use; once per statement per report, a newer read aborts the
previous one, a new report clears them, and an answer after a connection switch
is dropped. Loading, Stop (after 10 s) and Try again use `ViewStatus`; another
connection's report says "From <connection>; refresh to read this server." and
reads nothing. The chart groups five MySQL buckets per bar (a tenth of a decade,
by bucket number, since the bounds arrive rounded), snaps its axis to whole
decades with ticks at 10 µs, 100 µs, 1 ms and so on (every other label when
crowded), draws bars at most 16 px wide in `--accent`, axis and ticks in
`--line-strong` and labels in `--ink-muted`, gives each bar a `<title>` ("501
to 631 ms: 1 call (50.0%)"), is `role="img"` named by its summary, shows the
summary as a caption ("Most calls took X to Y; the slowest took Z.": the
narrowest run of bars holding more than half of the calls, and the digest's
MAX_TIMER_WAIT) and has a collapsed Buckets table. The text report adds the
server totals, the not-loaded line, New, P95, P99, P99.9 and CPU time; the CSV
adds P95, P99, P99.9, CPU Time (blank when not collected), First Seen and New
at the end. Tests: `test/test_discover.py` (`TestServerContext`,
`TestDigestHistogram`), `test/test_ps_instruments.py`,
`apps/workspace/test/latency-histogram.mjs` (`npm run test:latency-histogram`),
`percona.mjs`, `navigation.mjs`.

**Tables section.** `POST /api/live/digest-tables` (read-only, in the POST
allowlist and `_SERVER_VIEW_PATHS`, so saved-password lookup and redacted
connection errors follow the live-read path; 30 s client query limit) takes
`connection`, `schema` (the statement's default schema, string or null) and
`text` (normalized digest text or sample SQL). `discover.check_digest_tables`
validates both before a connection opens: the schema with
`perf_common.check_schema`, the text as a string of at most 1,000,000
characters without NUL. `discover.digest_tables(conn, schema, text)` answers
`digest-tables-1.0` (`docs/schemas/digest-tables-1.0.json`): `method:
"statement text"`, `tables` and `notes`. Each table has `schema`, `name`,
`found`, `type` (`table`, `view`, `system view`, `sequence`, or null without
metadata), `engine`, `rows_estimate`, `data_bytes`, `index_bytes` and
`indexes` (`name`, `unique`, ordered string `columns`, nullable `visible`).
Unknown measurements stay null and no row count is computed; missing
metadata can mean no privilege, not only a missing object.

Names come from `collectors.extract_table_references(sql, default_schema,
limit)`, which returns schema and table pairs and caveats (the older
`extract_table_names` delegates to it and keeps its string-list API): joins,
comma lists, nested queries, DML targets, CTE scope (recursive and multiple
CTEs), derived-table wrappers; CTE names and aliases are left out while their
bodies are read, a qualified name is never hidden by a same-named CTE, table
functions and DUAL are left out, backquoted names keep dots and doubled
backquotes, and exact spelling is deduplicated without folding case. At most
20 pairs, 100,000 tokens and 64 nesting levels, each limit with a note.
Unqualified names use the statement's default schema, never the picker's
later database; without one the name stays `schema: null`, `found: false`
with a note and is never read, and invalid names are not read either. A
statement with no resolvable name sends no metadata query. Otherwise there
are at most two bounded reads and no engine, version or catalog probe: one
`information_schema.TABLES` SELECT (at most 21 rows, the last a truncation
sentinel; it also returns `@@lower_case_table_names`, so binary predicates
keep exact case when it is 0) and one `information_schema.STATISTICS`
`SELECT *` read as named XML rows (MySQL's `IS_VISIBLE` and functional
`EXPRESSION` without a retry; key parts ordered by `SEQ_IN_INDEX`, PRIMARY
first, at most 4,096 with an incomplete-list note). Every OR branch fixes both
schema and table as hex literals. MariaDB's absent `IS_VISIBLE` stays null
(its ignored-index property is not shown as visibility). It never reads row
data, expands a view or routine, runs ANALYZE or SHOW TABLE STATUS, or reads
a whole schema's catalog; Tables and Indexes keep their schema-size consent.
It is a text matcher, not a SQL resolver: double quotes are strings (an
ANSI_QUOTES identifier can be missed), and a cut or unusual statement can
hide names.

Workspace: `api.ts` `readDigestTables` (`DigestTable`, `DigestTables`).
`QueryDigest.tsx` shows **Tables** below Latency (before Normalized SQL when
Latency does not apply), labelled "Found in the statement text" with an ⓘ
saying it is a text match, not proof of access; it prefers the normalized
text and falls back to the sample. Rows (estimate), Data, Indexes and each
index's columns (unique, invisible) are shown; **Open in Indexes**, only for
found base tables, uses the connection-bound `openServerTable` route. A read
starts only while the section intersects the viewport, Query digest is on
screen and its report belongs to the connection in use; leaving the section,
closing or changing the inspector, hiding the place or replacing the report
aborts it, and late replies after a cancel or connection switch are dropped.
Completed and failed results are kept per statement per report (theme or
width changes never read again; Refresh replaces them). `ViewStatus` gives
busy feedback, Stop after 10 s, Try again and one error beside the section.
Tests: `test/test_digest_tables.py`, `test/test_collectors.py`,
`apps/workspace/test/percona.mjs` (with `apps/workspace/test/fixtures/digest-tables.json`:
no read before selection, below the fold or while hidden; the request's
schema and text; rows, indexes, visibility and views; the base-table-only
Open in Indexes; cached reads on resize, theme and row return; one error
without an automatic retry; light and dark at 1440 and 720 px).

### Locks

`python3 -m myflames locks [--waits-only | --deadlock] [--limit N] [--json]` (`locks-1.1`), in `myflames/locks.py`: DDL statements waiting for a metadata lock (`ddl_waits`: from process-list wait states plus PENDING metadata locks; blockers `exact` from `performance_schema.metadata_locks`, `granted_locks` from MariaDB's optional `METADATA_LOCK_INFO`, else `likely` open transactions with an honest note; sessions queued behind the DDL), blocking tree over row and metadata lock waits with root blockers, INNODB_TRX transactions, row locks (MySQL `performance_schema.data_locks`, all locks; MariaDB `INNODB_LOCKS`, waits only), metadata locks with myflames' own GRANTED/PENDING compatibility check (not `sys.schema_table_lock_waits`), `kill_privilege` from plain `SHOW GRANTS` (active roles included; a hint, the server decides), and the parsed latest deadlock. The report only reads: never touch `setup_instruments`/`setup_consumers`; owner of a lock comes from `ENGINE_TRANSACTION_ID` → INNODB_TRX, not `THREAD_ID`. Keep the data_locks latch caution (MySQL < 8.0.40, 8.1–8.4.2, 9.0.x) and no default auto-refresh. The one write is `kill_session` (`kill-1.0`): `myflames locks --kill ID [--query] [--yes]` and the UI's **Kill blocker** (`POST /api/live/kill` with `connection_id`, `mode`, `expect`), one session per explicit, confirmed request; the workspace also offers a separately reviewed batch of at most 200 targets. It re-reads the session (same source as the report) and refuses when it is gone, a different session (user/host/thread/`trx_started` changed), its own or a myflames-tagged session, a server thread, already rolling back (INNODB_TRX `ROLLING BACK`; a "Killed" command alone is not proof, MariaDB shows it after KILL QUERY on an idle session), a prepared XA transaction on MySQL (idle session whose last statement in `events_statements_current` is `XA PREPARE`; the xid is copied only when well formed: ending the session releases nothing, so show `XA COMMIT`/`XA ROLLBACK` instead of Kill; MariaDB needs no check, it releases a detached XA transaction's metadata locks), or KILL QUERY on an idle session; server errors 1094/1095 become `gone`/`denied` results. A MySQL ALTER that adds a foreign key or renames also locks parent tables: `ddl_waits[].table` is the object it waits on, `statement_table` the one it names. CLI exit codes: 0 ended, 1 a sent operation failed, 2 an unsent refusal, decline, or error; refuses on a non-TTY without `--yes`. Both paths run the write policy first ([Write safety and audit](#write-safety-and-audit)): KILL is risky, so a read-only connection refuses it before any connection opens (CLI: before the password prompt; result `status: "refused"`, `engine: null`), and Production and Development both confirm (the CLI prompt or `--yes`; the UI sheet's `expect`). Every attempt writes an audit record (`kill_session(..., audit=)`, `kill_cli(..., audit=, policy_text=)`): refusals and declines as such, otherwise `pending` before the KILL is sent and `ok` or `error` (with the server error code) after; kill-1.0 results gain `audit_id`. Kill confirms and executions are bound to `connection_identity` and `server_identity` (MySQL `server_uuid`; MariaDB `server_id`, `hostname`, `version`); `locks.lock_report(identity=True)` includes the opt-in `server_identity` for the workspace Kill sheet (the CLI Kill re-reads it), and any mismatch or missing identity after confirmation refuses without sending. UI: `Locks.tsx` (Contention group in `Server.tsx`), `/api/live/locks` and `/api/live/kill` (saved-password helper, not in `_SERVER_VIEW_PATHS`); tests `test/test_locks.py`, `apps/workspace/test/locks.mjs` (fixtures captured from live servers in `apps/workspace/test/fixtures/`). The blocking tree's frame is a size container (`lock-tree` in `locks.css`): below 960 px of frame width (the 720 px window, or the inspector beside it) the Waits for cell wraps with no 38ch cap or ellipsis, the Session cell's pills wrap under the name (column 44%), Wait keeps a fixed column and Statement (shown in the inspector) is hidden, so the tree never scrolls sideways; at 1440 px nothing changes (Waits for capped at 38ch, Statement at least 200 px). The tree inspector's Transaction section labels the id ID and shows one age, Open for (`trx_age_s`, INNODB_TRX); the Idle in transaction pill carries the process list's idle time only when the transaction age is unknown. `locks.mjs` checks both widths and the single age.
Bulk Kill (`/api/live/kill/bulk`, `kill-bulk-1.0`) validates every target before
policy gating and records an audit row per target even for unconfirmed,
read-only, expired-review, credential and connection failures. It reads server
metadata and a dedicated identity query once per batch. Each existing target
SELECT also returns the server identifier, so a routing change still refuses
without another client invocation. Fresh user, host, thread, transaction and
saved-policy checks remain at each send boundary. No per-target settle sleep or after-read is
used; one bounded process-list snapshot fills additive `results[].after` and
the audits. A failed snapshot leaves `after` null and the result and audit say
"Sent; could not verify"; the deferred result uses this message too, so an
interrupted snapshot cannot record completion. Single Kill uses the same rule.
Transaction details are not re-read in this snapshot. Existing counts and
per-target statuses remain: `killed` means the server accepted KILL; the message
and `after` describe verification separately. No verification failure resends KILL.
Group Replication's `Group Replication Data Stream subscription` command is
always refused by the shared kill guard, for both connection and query mode,
with guidance to stop Group Replication through Cluster. The refusal is audited.
`locks.is_group_replication_command` recognizes exact prefixes of at least 16
characters through the full name; the process-list tables return `Group Replicatio`.
MySQL 8.4.6 source defines the widths in `storage/perfschema/table_processlist.cc:65`
(`COMMAND VARCHAR(16)`) and `sql/sql_show.cc:5051` (`processlist_fields_info`).
No other name in `sql/sql_parse.cc:224-260` starts with `Group Replicatio`.
Sessions uses the same helper for `thread_kind` and `kill_blocked_reason`.

### Sessions

`python3 -m myflames sessions [--all] [--session-user U] [--session-host H] [--schema NAME] [--min-seconds N] [--limit N] [--explain ID] [--json] [-o PATH] [-q]` (`sessions-1.0`), in `myflames/sessions.py`: every connection (P_S `processlist` on MySQL 8.0.22+ with P_S on, else `information_schema.PROCESSLIST`; MariaDB adds TIME_MS, STAGE/MAX_STAGE/PROGRESS, MEMORY_USED, EXAMINED_ROWS) joined with `INNODB_TRX` on `trx_mysql_thread_id` (ages with `SET SESSION time_zone = 'SYSTEM'`, since `trx_started` is in the OS time zone; needs PROCESS; without it, own sessions only and `capabilities.process_privilege: false`). Flags `long_running` (10 s watch / 60 s problem), `idle_in_trx` (Sleep + trx, graded on trx age 10/60; guarded against the two-read skew), `lock_wait` (`innodb` from INNODB_TRX LOCK WAIT, a row or InnoDB table lock; `metadata` from the MDL wait states; `table` from thr_lock), `system_thread`, `own_connection` (`/*myflames:` tag; the reader itself is excluded by `ID <> CONNECTION_ID()`). The summary (connections vs `max_connections`, `by_command|state|user|host|db`, oldest transaction, history list length) always covers every session; `active_only`/`user`/`host`/`db`/`min_seconds` narrow only `sessions`. `--user`/`--host` are the connection flags, hence `--session-user`/`--session-host`. `sessions.sessions_report(identity=True)` includes opt-in `server_identity` (MySQL `server_uuid`; MariaDB `server_id`, `hostname`, `version`) so Kill session in the workspace is bound to connection and server identity. `myflames running` is a deprecated alias (warns unless `-q`, keeps `running-1.0`). UI endpoint `POST /api/live/sessions`; tests `test/test_sessions.py` (replays in `test/fixtures/perf/sessions-*.json`).
`myflames sessions --kill ID [--query] [--yes] [--json] [-o PATH] [-q]` is an
alias of `locks --kill`: `cmd_sessions` calls the same `_kill_command(args,
locks.kill_cli)`, returning the same `kill-1.0` result and exit codes (0 ended,
1 a sent operation failed, 2 an unsent refusal, decline, or error). It keeps the same
confirmation, connection/server identity binding, target re-read, write policy
and audit. `--kill` and `--explain` are mutually exclusive; `--query` and `--yes`
require `--kill ID`. Listing remains read-only.

Processlist rows have a context menu (right click, Shift+F10, the context
menu key) with Copy query, Copy connection ID, then **Kill query…** and
**Kill connection…** for that row (client sessions only). Each opens the
single Kill sheet (exact statement, identity re-read, write policy,
privilege, audit) with that KILL chosen; an item that cannot run stays
focusable with its reason (read-only, policy not loaded, privilege denied, a
server thread, myflames' own, no statement to stop). A Kill shows its
progress in the sheet and its answer at once (Done, Already ended, The server
refused, Outcome unknown, Nothing was sent), while the view's loading line
says the list is read again. Follow progress sets Auto-refresh to 10 s.

Sessions adds `kill_blocked_reason` (text or null) and marks Group Replication
streams as replication system threads. SHOW FULL PROCESSLIST has no SQL LIMIT;
its text is cut to `INFO_CAP` Unicode characters in Python after recording the
full UTF-8 byte length. The shared `READ_CAP` of 10,000 rows and partial-summary
note apply to this source too; the connector still bounds the full response.

#### DDL progress

`myflames/ddl_progress.py`; the shape is in `docs/schemas/ddl-progress-1.0.json`
(no sessions JSON Schema exists). `sessions-1.0` adds `ddl: {available,
reason, missing, source, items}`, and each matching session row adds `ddl`
(else null); MariaDB's per-stage `progress` stays. Items are `{id, stage,
stage_label, percent, completed, estimated, elapsed_s, left_s,
statement_kind, source, units}`, plus `note` when the server has no usable
estimate. Counters beyond JavaScript's safe integers are decimal strings.
`units` is `rows` for copy to tmp table, `work_units` for InnoDB stages, and
null for MariaDB native percentages (whose completed and estimated counts are
null). `available` means tracking is ready or an item was read; `missing`
names disabled consumers and instruments even with partial coverage; an empty
result with tracking on says no reporting stage is active, without claiming
every DDL reports progress. Read errors keep their reason and never fail the
sessions report.

Each sessions report adds at most one bounded SELECT when Performance Schema is
on: the three stage consumer settings, the known progress instruments and the
active stages (`END_EVENT_ID IS NULL`) through UNION ALL, joined through
`threads.THREAD_ID` and `PROCESSLIST_ID`, at most 10,011 rows with bounded
statement text; MariaDB's native rows are part of that read. With Performance
Schema off it reuses MariaDB's processlist STAGE, MAX_STAGE and PROGRESS and
sends nothing more (SHOW PROCESSLIST has an overall percentage without a stage
count, so its label invents none, and a zero from SHOW alone is not progress).
Progress ids absent from the report's visible processlist snapshot are dropped.
Performance Schema work counters already describe the whole ALTER; MariaDB's
are combined as `100 * (STAGE - 1 + PROGRESS / 100) / MAX_STAGE`, the server's
SHOW formula. A running percentage stays at most 99. Time left uses the
unrounded fraction, only at 1% or more with positive work done and at least
one elapsed second (processlist time, not stage timers). When known work done
meets or passes a positive estimate (InnoDB sets the estimate to the larger of
its statistics-based formula and the work completed, so out-of-date statistics
make them equal; the same for COPY rows past `stats.records`), percentage and
time left are null and the item's `note` says "The server has no estimate yet:
it grows with the work done (table statistics may be out of date)." A zero or
unknown estimate also leaves percentage and time left null, with `note` null.
TIMED=NO alone is
not reported missing: work counters need no timer.

`POST /api/live/ddl-progress` takes `{connection, connection_ids}` (1 to 200
positive safe integers, deduplicated after validation) and answers
`ddl-progress-1.0` with the same `ddl` object and the server metadata (null
product metadata when preflight fails). It is in the connection-password and
HTTP allowlists, with a 5 s query limit: a metadata read (version, version
comment, Performance Schema), then one bounded progress read; on MariaDB only,
when that read fails (SELECT on Performance Schema denied), one more bounded
native read, with the original error kept. Every statement is a SELECT or
SHOW: nothing enables instrumentation, changes consumers, sends DDL, retries a
write or changes the ALTER review; consumer changes stay with Instruments'
reviewed SQL, and persistence with its option-file guidance.

Processlist draws a compact bar and number in the State cell (the raw stage
and the estimate on hover) and a DDL progress inspector section (exact elapsed
and estimated seconds on hover, work counts); **Follow progress** chooses the
shortest Auto-refresh interval, 10 s. Reads keep the on-screen guard. The
Query editor (`DdlProgress.tsx`, `ddlProgressPoll.ts`; `main.tsx` passes its
on-screen state and `openInstrumentsRow`) polls only for a running ALTER
TABLE, OPTIMIZE TABLE or CREATE INDEX of the tab, bound to the tab, the run
generation and the session's original connection id and credentials, with one
outstanding read and the next 2 s after it settles; leaving the screen, a
hidden document, a tab switch, Table data, completion and Stop cancel it, and
disposed or aborted replies never restore old progress. Run-state polling is
unchanged. Missing tracking appears once, with **Turn on stage tracking…** to
the exact Instruments row (`InstrumentsLinkContext`); an editor session on
another connection gets the diagnostic without the link. `myflames sessions`
text adds a DDL PROGRESS section with one line per item ("ALTER on 42: copying
rows (stage/sql/copy to tmp table) 42%, about 2 min 54 s left (estimate)") and
a line naming what to turn on; its help is unchanged. Tests:
`test/test_ddl_progress.py`, `test/test_sessions.py`, `test/test_report.py`
(the replay adds an exact synthetic answer for the new SELECT when Performance
Schema is on), `apps/workspace/test/ddl-progress-unit.mjs` and
`ddl-progress-poll.mjs` (no browser or socket), `sessions.mjs`, `editor.mjs`.

#### Blocking graph and recent statements

`sessions-1.0` adds a blocking graph (`myflames/sessions.py`
`_blocking_graph`, edges from `locks.session_wait_edges`, which reuses the
Locks wait reads on the Processlist snapshot). The report gains
`blocking_edges`: `{waiter, blocker}` pairs (connection IDs only), unique,
resolved and non-self, ordered by waiter then blocker, collected on the full
snapshot before the scope, user/host/database/time filters and the row limit,
so an endpoint can be absent from `sessions`; empty when no usable edges were
collected. The graph keeps at most `locks.MAX_TREE_EDGES` (5,000) pairs, with
a note when capped, and row counts and root summaries come from that same
topology. Each session row gains `blocked_by` (sorted direct blocker
connection IDs), `blocks` (direct waiter count), `blocks_ids` (sorted direct
waiter connection IDs), `blocks_total` (transitive waiter count, cycle-safe),
`root_blocker` (blocks at least one session, has no resolved upstream edge and
is not itself in a lock wait) and `longest_wait_s` (the maximum known wait age
among transitive waiters, else null). `summary.root_blockers` lists `{id,
blocks_total, longest_wait_s}` ordered by descending `blocks_total`. A session
with downstream waiters is marked active and qualifies for the Locked filter,
which sorts root blockers first, then descending `blocks_total`. Counts
describe the collected snapshot, not a guarantee that ending one session
releases every downstream wait; a cycle yields no root blocker, and a waiter
whose upstream edge was capped or unavailable never becomes a false root. A
snapshot with a known waiting session invokes `locks.session_wait_edges` once
per wait source: on MySQL 8 with Performance Schema (`ps_on`), one wait-source
query (`_read_waits` on `data_lock_waits`) plus `_read_sessions` (a
`performance_schema.threads` owner query) when any wait's `connection_id`
remains `None` with a `ps_thread_id`, plus `_read_mdl` when metadata waits are
present; more on the legacy sources (a waiting-ids probe plus two filtered
passes) or when metadata waits add `_read_mdl`; and none when no session waits.
Row edges keep at most 1,000 rows (one sentinel); computed MDL edges keep the
5,000-edge budget and exist only when metadata locks are recorded. MySQL 5.7
uses the legacy `INNODB_LOCKS`/`INNODB_LOCK_WAITS` tables (no `data_locks`);
MariaDB uses its legacy row-wait source on every Performance Schema state.
Missing privileges, unknown owners and truncation are reported as notes. The
standard Processlist per-query timeouts apply.
Tests: `test/test_sessions_blocking.py`, `apps/workspace/test/sessions-blocking.mjs`.

The workspace shows the graph inside the existing Lock / Trx column (140 px):
"Waits on <id>" with "+N" for other direct blockers, "Blocks N"
(`blocks_total`), with the **Root blocker** pill first in the cell; no columns are added. IDs are links
only in the inspector, which follows them through the loaded rows and marks a
target the filters hide as "Connection <id> (not in this list)". The Kill
sheet places **What to end** directly below **Statement to send**, before the remaining facts, so the statement and choice stay together in the 720 × 520 minimum window. Its blocking sentence is mode-aware (Kill query only can leave the
transaction and its locks open; Kill connection rolls the transaction back;
another blocker can keep sessions waiting either way) and remains a snapshot
hint: the review token, target re-read, execution and audit contracts are
unchanged.

`POST /api/live/sessions/history` takes `{connection, id}`; `id` must be an
integer from 1 through 4294967295 (booleans refused), validated before any
connection opens. It returns `{id, available, reason, limit: 10,
statements: [{event_id, sql, duration_ms, rows_examined, rows_sent, errors,
errno, message}]}`; `event_id` is a decimal string, and unknown timing or
counters stay null. Statements are the thread's ten most recent from
`performance_schema.events_statements_history`, SQL capped at
`locks.TEXT_CAP` (4,096 characters). The endpoint reads the server variables,
the four required consumers and whether statement instrumentation exists, and
the thread's INSTRUMENTED/HISTORY flags, then joins `threads` on
PROCESSLIST_ID and pins the observed THREAD_ID against connection-ID reuse:
at most four small reads, each with a five-second timeout. Disabled
prerequisites or privilege denials return `available: false` with one reason;
an enabled empty ring returns `available: true` with an empty-list reason. It
is in the live-read POST allowlist (Host/Origin, session token, JSON body
limits, saved-password resolution, redacted errors), performs no server write
and never enables instrumentation. Client inspectors load it on open,
cancel replaced reads, render SQL as text in `bdi`, and link missing
prerequisites to Server > Instruments. Tests: `test/test_sessions_blocking.py`.

### InnoDB

`python3 -m myflames innodb [--raw] [--fail-on problem|watch] [--json] [-o PATH] [-q]` (`innodb-1.0`), in `myflames/innodb_status.py`: `SHOW ENGINE INNODB STATUS` split into sections (`parse_status` is pure) and post-processed into `sections[]` of `{key, title, present, status, summary, items[], data, links[]}` (semaphores, transactions and purge, deadlock via `locks.parse_latest_deadlock`, foreign key error, file I/O, change buffer, adaptive hash index, redo log with health.py's pressure points against the capacity in use (`Innodb_redo_log_capacity_resized`, then "Log capacity used", never "Log capacity"), buffer pool with instances, row operations, background). InnoDB's `config` link reads "Open Parameters". Never return the raw text unless `raw: true` / `--raw`. Unknown or missing lines are tolerated (`parse`, `notes`); a sub-second averaging window drops the rates. Without PROCESS: `available: false`, `error.code: no_process`, CLI exit 2. UI endpoint `POST /api/live/innodb`; tests `test/test_innodb_status.py` with real captures in `test/fixtures/innodb/`. In the workspace every section summary keeps 8 px between its title, status and summary, a quiet OK and No threshold included (`perf.css`; `apps/workspace/test/innodb.mjs` measures both gaps).

### Error log

`python3 -m myflames error-log [--level LEVEL[,LEVEL...]] [--limit N] [--fail-on problem|watch] [--json] [-o PATH] [-q]` (`error-log-1.0`), in `myflames/error_log.py`. Read-only on both engines; the module docstring cites the server source for every fact below.

- MySQL before 8.0.22 returns `available: false`, `reason: unsupported_version`, the message "performance_schema.error_log needs MySQL 8.0.22 or later." and `how_to_view` before reading counters. MySQL (8.0.22+, WL#13681) reads `performance_schema.error_log` (`LOGGED`, `THREAD_ID`, `PRIO` System/Error/Warning/Note, `ERROR_CODE` such as `MY-010931`, `SUBSYSTEM`, `DATA`), which needs SELECT on that table and Performance Schema ON. The rows come from a 5 MB in-memory ring buffer described by the `Error_log_buffered_bytes`, `Error_log_buffered_events`, `Error_log_expired_events` and `Error_log_latest_write` status variables (`buffer`). When `@@log_error` names a file, the server re-reads its tail (at most 5 MiB) at startup through the leftmost parsing sink (`log_sink_internal` reads `@@log_error`, `log_sink_json` its own JSON file), so rows older than the current start are `previous_run`; with `stderr` or no parsing sink the table holds only events since this start. The start is UNIX_TIMESTAMP() minus Uptime: counts and `previous_run` compare epoch seconds (`UNIX_TIMESTAMP(LOGGED)`), and `server_started` is shown in the session time zone like LOGGED, so a daylight saving change does not move rows between runs. Status counters are read by an explicit name list (no LIKE pattern, so `NO_BACKSLASH_ESCAPES` cannot hide them). Only `log_sink_internal`, `log_sink_json` and `log_sink_syseventlog` feed the table; without one of them the view is not available (`no_pfs_sink`) instead of reporting zero events. With `log_sink_json` the `DATA` column holds the event's JSON document and the message is its `msg` key. `log_error_verbosity` drops Warning and Note events before they are logged (System and Error are never dropped by the built-in filter), so zero warnings at verbosity 1 means not recorded.
- MariaDB has no SQL access to its error log, and `LOAD_FILE` is never used (it needs FILE, `secure_file_priv`, a world-readable file and a size under `max_allowed_packet`). The module reads the `@@log_error` file (relative names resolve against `@@datadir`) only when all of these hold: the connection host is `localhost`, `127.0.0.1`, `::1` or empty; `@@hostname` equals this computer's host name (case-insensitive, the whole name); the path is a readable regular file; at least one line parses as an error log line; and the latest startup banner in the bytes read names `@@version` (or `'<built version>' as '<@@version>'` when version was set at startup) and `@@port`, and was logged no earlier than two seconds before and at most `BANNER_WINDOW_S` (an hour) after the server's start (`CONVERT_TZ(UTC_TIMESTAMP() - INTERVAL Uptime SECOND, '+00:00', 'SYSTEM')`, the log's local time). The banner fields are bounded (128 characters, no newlines). It reads at most the last 1 MiB (`TAIL_BYTES`; a cut that lands on a line start keeps that line) and returns parsed entries only: `[ERROR]`, `[Warning]`, `[Note]` lines and `YYMMDD H:MM:SS [ERROR] ... got signal` crash headers (no thread id), each with at most `MAX_CONTINUATION` (100) continuation lines; MariaDB has no System level. Otherwise `available: false` with `reason` (`log_to_stderr`, `remote`, `other_host`, `not_found`, `not_readable`, `unrecognized_format`, `identity_unverified` for no banner or one older than this start, `identity_mismatch` for another version, port or start), `reason_text` and a `how_to_view` hint (`tail -n 200 -- <path>` on the server host, a `/` path kept as POSIX when myflames runs on Windows, words instead of a command when the path has control characters, or the service manager's log when `@@log_error` is empty). A denial during any MySQL read returns `denied` with no partial counts, codes or entries.
- Envelope: `base_report` fields plus `available`, `reason`, `reason_text`, `how_to_view`, `source` (`performance_schema.error_log` or `file`), `settings`, `buffer` (MySQL) or `file` (MariaDB), `server_started`, `counts` and `counts_current_run` (system, error, warning, note), `codes` (the 20 most frequent code, level and subsystem groups), `entries` (newest first, `limit` 1 to 1000, default 200, after the `levels` filter), `entries_total`, `summary` (`watch` when the current run logged an Error, never `problem`, `unknown` when not available) and `notes`.
- CLI: `--level` takes any of `system,error,warning,note`; `--fail-on watch` exits 1 on current-run errors. Not available exits 2 (also with either `--fail-on` threshold) with the reason and hint on stderr. With `--json`, the complete `error-log-1.0` envelope (`available: false`, `reason`, `reason_text`, `how_to_view`) is also written to stdout or `-o`; text mode emits only the stderr message. Connection/setup failures still have no payload.
- UI: Server > Overview > Error log (`ErrorLog.tsx`), after InnoDB. It reads when opened on screen, once per connection (Refresh and Refresh all read again; no Load button), and a server whose log cannot be read shows the reason once in its summary, with `how_to_view` prose separate from copyable commands and no duplicate gate; level filter, text filter, "This run only", Entries and By code segments, inspector with the full message. Endpoint `POST /api/live/error-log` (`limit`, `levels`; saved-password helper, `query_timeout` 30). Tests: `test/test_error_log.py` (replays in `test/fixtures/perf/error-log-*.json`, MariaDB file cases in a temporary directory) and `apps/workspace/test/errorlog.mjs` (`npm run test:errorlog`).

### Server identity: Percona Server

- Connection tests pass both `@@version` and `@@version_comment` to `product_identity`. With version `11.4.13` and comment `MariaDB Server`, they return `engine: mariadb`, `flavor: mariadb` and `product: MariaDB`.
- Query digest (`QueryDigest.tsx`, `query-digest.css`) uses `ViewLayout`, `SummaryLine`, `Select`, `SearchField`, `DataTable`, the shared inspector and `ExportMenu`. It retains `coverage` and `notes` as well as `server_product` from `top-1.0` and `slow-log-1.0`. Rankings name `performance_schema.events_statements_summary_by_digest`; the slow log sources name `mysql.slow_log` or the file. The summary line uses the bound report's loaded-row totals and its uptime when provided, and the title's ⓘ names its mode and source table (see the Query digest workspace paragraph above); text filtering does not change the share denominator. Conditional notes preserve digest loss, overflow and source failures. Unknown slow-log counters remain unknown in facts and exports. Measured cells use the view's own `Ms` component: the text Top statements writes ("59.04 ms", "1.20 s"), the exact milliseconds on hover, and sorting on the raw millisecond value. Schema plus digest/text and row position identify rows; rank remains the server ranking through sorting/filtering. Inspector and row menu actions disable on another connection's report, Explain also disables for `capture_problem`, and dispatch checks `bound.key === reads.currentKey()`. The inspector is a pane or a sheet below 1040 px. `useOpenRead` reads on opening; mode, limit and default-database changes reread while on screen. Export offers Text report (shared sheet preview/Copy/download, headed `# myflames query digest`) and CSV for all loaded rows. The text keeps `# Server:` and `# Filter mode:` plus source/coverage; preview text and filename are frozen when opened. Its footer uses only the active connection's report; both the footer and text export use `engineName`. The slow envelope carries additive `flavor` and `server_product` via `conn_identity`.
- Two levels describe a server (`myflames/server_info.py`). The engine, `mysql` or `mariadb` (`detect_flavor`), decides rules, SQL and variable names. The flavor, `mysql`, `percona` or `mariadb`, and the product name, MySQL, Percona Server, Percona XtraDB Cluster or MariaDB (`product_identity(version, version_comment)`), decide only how the server is named and which Percona additions are read. Percona Server for MySQL and Percona XtraDB Cluster are engine `mysql`: every MySQL rule applies.
- Percona names itself only in `@@version_comment` ("Percona Server (GPL), Release 11, Revision 57878ff8", "Percona XtraDB Cluster (GPL), Release rel11, ..."; read live on 8.0.46-37, 8.4.11-11 and PXC 8.4.11-11.1). Its `version_suffix` variable can change the `@@version` suffix, so the version never decides when the comment is known. Where only the version is known (a relay log's format event for Replication's source version, the Privileges read and the Query editor's hello, which read `VERSION()` alone), `identity_from_version` names Percona Server by its numeric release suffix (`8.4.11-11`); MySQL builds carry none.
- Every view envelope carries `flavor` and `server_product` (`perf_common.base_report`, `Server.flavor` and `Server.product`; health, locks, top, running, replication, cluster, report and the single variable read add them too; `report-1.0` and `cluster-1.0` schemas list them; the Full analysis header's `server` has `flavor` and `product`; Replication's `source_version` has `flavor` and `product`; `/api/connection/test` returns `engine`, `flavor` and `product`). Every summary line, the window footer, Tuning, the Full analysis, Replication's source versions and Cluster name the product. Text summaries use `server_info.product_name` / `server_label`, which fall back to MySQL or MariaDB for older reports; the workspace uses `engineLabel` (PerfShared) and `engineName(engine, version, product)` (OpsShared, which keeps Percona's release number: "Percona Server 8.4.11-11").
- Percona additions, read from the server only: with `userstat=ON` (dynamic on Percona, the same tables and columns as MariaDB's, `Ma/plugin/userstat/*_stats.cc`) Indexes reads `INDEX_STATISTICS` beside Performance Schema and keeps an index PS shows unused that userstat saw read (`verdict: keep`, `keep_reason: "userstat"`, `userstat_rows_read`; `report.userstat` `{enabled, read, role}`; with PS off userstat is the source, as on MariaDB), I/O uses `TABLE_STATISTICS` with PS off, and Tables adds `rows_read` and `rows_changed` to the inventory and to tables without a usable key (their `why` gives the rows changed, inserts, updates and deletes together, and says only updates and deletes need a replica search; `report.userstat` `{enabled, read}`). User statistics miss range reads of partitioned tables (Percona documents no support; a range read through `parted_pk.kw` was missing live on Percona Server 8.0.46-37 and 8.4.11-11 and MariaDB 11.4.13), so Tables gives partitioned tables `rows_read` and `rows_changed` null on every userstat server with a note, never the no-changes text; Indexes uses one bounded `information_schema.TABLES.CREATE_OPTIONS` read for candidate tables on every engine and usage source. Partitioned candidates go under No data (`reason: "partitioned"`), missing or unreadable metadata yields `reason: "partitioning_unknown"`, and neither gets DROP or visibility SQL. Percona userstat full-scan shares also require known plain tables; candidate-only metadata leaves other tables' shares unjudged; I/O's userstat note says partitioned tables can be missing or low. Health's `thread_cache` diagnostic, under `thread_handling=pool-of-threads` (Percona or MariaDB), shows the pool instead: `Threadpool_threads`, `Threadpool_idle_threads`, and on Percona 8.4 the queue counters, `thread_pool_size` and `thread_pool_max_threads` read in a second statement only then (state `not_applicable`, no threshold). Parameters (`parameters-1.0`) carries `flavor` and marks items with `vendor: "percona"` from `server_info.PERCONA_VARIABLES` (the names Percona Server 8.0.46 and 8.4.11 add to MySQL 8.0.46 and 8.4.11, compared live; `table_open_cache_triggers` only below MySQL 9.0, which upstreamed it); the single variable read has `vendor` too. The slow log check reads `log_slow_rate_limit`, `log_slow_rate_type`, `slow_query_log_always_write_time`, `log_queries_not_using_indexes` and `log_query_errors` in a second statement on Percona only and reports a sampled log as info with the share logged, also above a 10 s `long_query_time`. It follows Percona's `log_slow_applicable` (Percona Server 8.4.11-11 `sql/log.cc`): a statement admitted by `long_query_time`, `log_queries_not_using_indexes` or `log_query_errors` is skipped for the sample (query: a random 1 in N kept; session: only connections whose ID is a multiple of N) only when it ran under `slow_query_log_always_write_time` and, with `long_query_time` under 1 s, under 1 s. So nothing is sampled when that variable is 0, and at or below `long_query_time` only the statements the other two variables add are sampled; session sampling is never described as a share of statements (checked live on a throwaway 8.4.11-11). The Full analysis's version support check never applies MySQL's lifecycle dates to Percona: it is unknown, with MySQL's date for reference.
- Fixtures: `scripts/record-perf-fixtures.py` records one live view read (`indexes`, `tables`, `config [--parameters]`, `io`, `waits`, `health`) from the GA matrix as a replay fixture; the Percona ones (`test/fixtures/perf/*-percona-8.*.json`, `test/fixtures/health/percona-8.*.json`) feed `test/test_percona.py`.

### Server health

`python3 -m myflames health [--interval N] [--fail-on problem|watch] [--json] [-o PATH] [-q]` (`health-1.0`), in `myflames/health.py`: two `SHOW GLOBAL STATUS` samples N s apart (1–60, default 5) plus a few `SHOW GLOBAL VARIABLES`; no Performance Schema, no privilege. Sampling (`read_variables`, `take_sample`, `health_report`) is separate from the pure `compute_health`; each metric has value/delta/rate/unit/status/why/rule/link/learn. Statuses are rules of thumb (`RULES`, constants at the top), `info` where no universal limit exists, `unknown` for missing or reset counters (Uptime going down = restart). Keep the engine rules from the module docstring: MySQL disk reads = `Innodb_buffer_pool_reads` + `_read_ahead`, MariaDB reads already include read-ahead (same as `advisor._rule_cold_cache`); MariaDB counts a memory-to-disk temp table twice in `Created_tmp_tables`; MySQL redo rate uses the `Innodb_redo_log_current_lsn` delta; dirty pages are only ever watch. Diagnostics (`diagnostics` list, `_diagnostics`, `DIAGNOSTIC_RULES`): title, headline, verdict, why, rule, numbers, settings (value + engine default), a recommendation only at watch/problem and always with a caveat; `summary.diagnostics` counts them and `summary.status` is the worst of metrics and diagnostics. Buffer pool full = free pages <= `innodb_lru_scan_depth` x instances (MySQL; MariaDB one instance), bytes read = pages read x `innodb_page_size`; redo coverage and time-to-pressure come from the LSN growth (MySQL `Innodb_os_log_written` re-counts partial blocks, measured 2x under small commits), capacity from `config_review.redo_capacity` (`Innodb_redo_log_capacity_resized` first), thresholds from the checkpoint age (MySQL ~0.73/0.79 of capacity, MariaDB 7/8 and 1.0 of `Innodb_checkpoint_max_age`); the temp-table limits text is `config_review.tmp_table_limit`. Never change a setting. `--fail-on` covers diagnostics and metrics, exits 1 like `check`; 2 on bad input or connection failure. The tool's own second sample (3 statements, 1 temp table, 1–2 scans, 1 opened table on MySQL) must never trip a rule (`MIN_EVENTS`). UI: `Health.tsx` (first Server tab), `/api/live/health`; the sample history (20, sparklines) stays in memory, session restore keeps only the tab and `health_interval`. Tests `test/test_health.py` (live two-sample fixtures from MySQL 8.4/9.7/26.7 and MariaDB 11.4/13.0 in `test/fixtures/health/`, plus `scenario-*` captures for warming, full, redo, binlog, threads, and table cache), `apps/workspace/test/health.mjs` (`npm run test:health`; the Diagnostics cards, their trends, and 390 px).
Health and configuration advice share version-aware limits. MariaDB `innodb_log_file_size` is read-only before 10.9.1: recommend only `innodb_log_file_size = <bytes>` under `[mysqld]` (or `[mariadb]`) and a restart. MariaDB 10.6 to 10.8 has one redo file, so that line holds the total capacity. From 10.9.1, retain `SET GLOBAL innodb_log_file_size` online resize advice. MySQL TempTable has no per-table limit before 8.0.28; `temptable_max_ram` is global and decides when its tables overflow to disk (memory-mapped files may be used first). Only from 8.0.28 does `tmp_table_size` limit each TempTable table. MySQL MEMORY and MariaDB keep their smaller-of-two per-table limits. When only `TempTable_count_hit_max_ram` grows, Health recommends `SET PERSIST temptable_max_ram = <bytes>;`, with a caveat that all TempTable tables share it, and no `SET SESSION tmp_table_size`. Tests: `test_correctness_regression_mariadb_redo_resize_version` and `test_correctness_regression_temptable_per_table_version` in both `test_health.py` and `test_config_review.py`, plus `test_health.py::test_correctness_regression_temptable_limit_hit_action`.

Health text prints `Statements: myflames top --order full_join` once when a
`select_full_join` metric or diagnostic is watch/problem; other statuses omit it.

Live charts (`Health.tsx`): the workspace keeps the last `HISTORY_LIMIT` (60)
readings per connection in memory, cleared on a connection switch and never
saved to session storage or SQLite, and shows the section **Live charts**
above Diagnostics once one reading exists, from the `health-1.0` keys
`queries` (Queries per second), `threads_running` (Threads running),
`bp_disk_reads` (Buffer pool reads from disk per second) and
`threads_connected` (Connections in use: the count of open connections, never
`connections_used`, which is the share of `max_connections`); no server metric
is computed in TypeScript. With one reading (`.health-live-charts.is-pending`)
each card holds only its title and value (two grid rows, no SVG, no Min or
Max) and the screen reader summary "`<title>`: `<value>`, one reading so
far."; the head says "Collecting…" until the second reading, then "Last N
readings". From two readings each chart has a title, the latest value,
the Min and Max of the readings shown, a zero baseline, each reading's local
time and value on hover, and an accessible summary
(`svg[role="img"][aria-label]` plus `.health-chart-summary`). When every
reading shows the same text at the precision displayed
(`kit.trendRange(...).steady`, which only formats the server's values and
compares the text) the range line reads "Steady over the last N readings"
(card class `is-steady`) and the summary "`<title>`: steady at `<value>` over
the last N readings.". The inspector's trend follows the same rule: before
the second reading the value and "Collecting…"
(`data-trend="pending"`, never a one-point chart), and for equal readings
"Steady at X over the last N readings." (`data-trend="steady"`); a
`Sparkline` whose samples all show the same text labels itself "`<label>`,
last N samples: steady at X". `rememberHealth` treats an answer with the same
`taken_at` as the report already kept for that connection as the same
reading: it replaces the report and adds nothing, so one reading never draws
as two (real reads always carry a new `taken_at`; mocks and replays need a
distinct one per reading). Charts update only with Health's own reads, so off
screen they stop.

Health's Auto-refresh (a `ToolbarSelect`: **While visible**, the default,
**Off**, **Every 10 s**, **Every 30 s**, **Every 60 s**; the choice is kept
per connection type in workspace.sqlite3, `health_auto_production` and
`health_auto_development`; legacy `myflames.health.auto.<type>` localStorage
values are separate startup fallbacks until the backend answers) needs no
user action: with any value but Off, the first read starts as soon as Health
is on screen. `createHealthRefresh` schedules `every` from `finished()`, so
**While visible** waits one full sampling interval (the Health interval, 1 to
60 s, default 5) after each completed read before starting the next automatic
read (a 5 s window reads for 5 s, then waits 5 s). The loop runs only while the
document is visible, the view is mounted and laid out, the Server place is on
screen and no baseline sheet or Capture when… form is open, and it is
single-flight (a pending read blocks the next tick, and a manual Refresh shares
the same path). Refresh is busy only for a read it started: during an automatic
read it stays live, and pressing it supersedes that read (the newest read wins);
the status line shows every read's progress. In `Health.tsx` `load()`, an automatic read never replaces
another connection's greyed report: after a connection switch the report stays
greyed until a manual Refresh, and the schedule resumes after it (`deferred()`
waits a full interval). Leaving Health and returning calls `setVisible(true)`,
which reads immediately and restarts the schedule unless a failure backoff deadline is still pending.
Failed reads back off exponentially (at most 60 s) and say so ("Automatic
refresh backed off: retry in N s while visible."). The shared kit
`AutoRefresh` of Sessions, Locks and Waits keeps its own Off default.

At 900 px and below, where tiles scroll in one strip, each tile group heading
carries `.tile-section-status` (`kit.css`, hidden above 900 px): a
`StatusPill` of the group's worst status and its share in words from
`groupStatusText` ("1 problem, 1 to watch", "2 problems"; problem and watch
only, nothing when all is well), so Diagnostics' own count plus the groups add
up to the summary line. The FactStrip's Details fold holds only server facts,
which carry no status, so it has no marker. Tests:
`apps/workspace/test/health-watch-unit.mjs` (`rememberHealth`, `trendRange`,
`pendingTrendText`, `groupStatusText`) and `health.mjs` (one reading, steady
readings, the group headings at 720 px and their absence at 1440 px).

**Capture when…** is a toolbar popover built from kit controls: three
conditions (Threads running at or above N, A row lock wait older than N s, A
statement running longer than N s; whole numbers, checked at Start with the
error beside the field), a Check interval (5, 10, 30 or 60 s) and Stop after
the first capture (on by default). The watch lives in Server memory
(`HealthMemory.watch`, never saved) and belongs to the connection it started
on. Checks run only while Health is on screen, the page is visible and the
Query editor is not capturing (the status says paused otherwise); the first
runs at Start, and a connection switch stops checking. The status reads
"Watching: checked N times, last at HH:MM:SS." in this computer's local time,
with Stop, and names the conditions a read could not judge ("Not judged: …",
"Partly judged: …"); an unread value is never taken as zero
(`judgeWatch`, inclusive thresholds).

Each check is `POST /api/live/watch` (`watch.watch_report`, `watch-1.0` in
`docs/schemas/watch-1.0.json`: `{schema_version, engine, server_version,
server_product, read_at, threads_running, longest_statement,
longest_lock_wait, longest_waiting_statement, errors, notes, reason}`), one
bounded read-only check: `SHOW GLOBAL STATUS LIKE 'Threads_running'`, the
process list (`sessions._processlist_sql`; sleeping and daemon threads are not
statements) and `information_schema.INNODB_TRX` (`locks._read_transactions`).
`longest_lock_wait.seconds` is only how long an InnoDB transaction has waited
for its row lock (`INNODB_TRX.trx_wait_started`, the one wait time the server
records), never a statement's or transaction's running time (process list
TIME counts from the statement's start), and a wait without a recorded start
is not judged. A watch never reads `performance_schema.data_lock_waits` (it
records no wait start and holds InnoDB's lock-system latch on older versions);
without INNODB_TRX (no PROCESS) row lock waits are not judged and the reason
is kept. Metadata and table lock waiters are `longest_waiting_statement`
`{id, seconds, state}`, where seconds is how long the waiting statement has
run: reported (and in the evidence text), never judged as a lock wait, and
still caught by "A statement running longer than N s". A source that could
not be read is named in `errors.<field>` and `notes`; without PROCESS,
`longest_statement` and `longest_waiting_statement` say the process list
shows only the account's own sessions.

A met condition captures once: with Stop after the first capture off, the
next capture waits until a check finds no condition met. The capture reads
`/api/live/sessions` (`active_only: false`), `/api/live/innodb` and
`/api/live/locks` on the connection the check used, with progress in the
status line (Stop after 10 s), then saves "Capture: <condition> on
<connection name>, YYYY-MM-DD HH:MM" (local time) with notes listing the
conditions and anything not captured, and shows "Saved capture." with **Open
investigation** and Dismiss. A capture goes on after Health is left or the
connection changes (its outcome names its own connection); Stop ends both
the watch and a capture. `POST /api/live/watch/capture`
(`watch.capture_evidence`, `watch-capture-1.0`, 30 s query limit) builds the
same evidence in one request; the workspace does not use it, since it reads
the three views itself for step progress. Both are in `_SERVER_VIEW_PATHS`.
Tests: `test/test_watch.py` (MySQL 8.4 and MariaDB 11.4 reports; a 60 s
statement waiting 2 s reads 2 s; no data_lock_waits without PROCESS; a 600 s
ALTER waiting for its metadata lock is never a lock wait; evidence stored
without credentials), `apps/workspace/test/health-watch-unit.mjs` (in `npm run
test:health`: `judgeWatch`, `switchHealthConnection`, the local clock),
`apps/workspace/test/health.mjs`, `investigation.mjs`.

### Baselines: Health and Query digest

`myflames/baselines.py`; storage in `myflames/workspace_store.py`
(`baseline_save/list/get/delete`); UI `apps/workspace/src/Baselines.tsx`
(`BaselineMenu`, the Refresh menu items **Save baseline** and **Compare with
baseline** in Health and Query digest) and `baselineCompare.ts`. A baseline
keeps the reading on screen (a `health-1.0` report, or a `top-1.0` Statement
digests report) on this computer, tied to the server's own identity, so a
later reading of the same server can be compared with it.

- Storage: table `baselines` (`id`, `created_at`, `server_key`, `kind`,
  `name`, `summary`, `payload`), created on first use. The payload is
  zlib-compressed JSON, at most 2 MiB (2,097,152 bytes) measured on the JSON
  before compression; reads bound decompression to the same size. `kind` is
  `health` or `digest`; `name` is one line of 1 to 80 characters. At most 20
  baselines per server key and kind: the 21st is refused (`BaselineLimit`)
  inside `BEGIN IMMEDIATE`, nothing is deleted to make room, and delete
  happens only on request.
- Server identity: `server_key` is `<engine>/uuid/<@@server_uuid lowercased>`
  when it is a UUID (MySQL and Percona), else
  `<engine>/host/<@@hostname lowercased, trailing dot removed>:<@@port>`
  (MariaDB). The version is stored and shown but is not part of the key. The
  identity read is one client run with `--comments --skip-reconnect` and three
  statements tagged `/*myflames:baselines*/` (`SHOW GLOBAL VARIABLES WHERE
  Variable_name IN ('hostname','port','server_uuid','version','version_comment')`,
  `SHOW GLOBAL STATUS WHERE Variable_name = 'Uptime'`, `SELECT 'now', NOW(6)`),
  query timeout 3 s (`QUERY_TIMEOUT = 3`), connect timeout 8 s; it needs no
  privilege. `/api/live/health` and `/api/live/top` accept `with_identity:
  true`, which runs the required measurements and `IDENTITY_SQL` in one client
  invocation (`read_with_identity`, `--comments --skip-reconnect`) with a
  separate 3 s `tail_timeout` starting only after the last required measurement
  marker row is complete; an identity-only failure or timeout keeps the
  completed report with `reading_server: {verified: false, error}`. In
  `health.py`, `on_data=mark_sample` records `t` and `taken_at` at the
  status-tail marker before waiting for the identity tail, configuration (`SHOW
  GLOBAL VARIABLES`) runs before `SHOW GLOBAL STATUS` in the first sample
  invocation, identity is read in both sample invocations, and a different
  `server_key` or `version` between samples raises `ValueError("The server
  changed between Health samples. Refresh to sample again.")`. In
  `discover.py`, `with_identity` batches `[digest_sql(), _COVERAGE_SQL,
  _OVERFLOW_SQL]` with identity in `read_with_identity` (refusing if engine or
  version changed) and runs `_server_context(conn)` as a separate best-effort
  invocation with `CONTEXT_TIMEOUT = 3` (3 s), so its failure leaves `totals`,
  `others`, `counting_since` and `coverage.oldest_first_seen` null while keeping
  the digest rows, coverage, overflow and identity usable.
- Endpoints (POST JSON, workspace authentication, allowlisted through
  `baselines.PATHS`, outside `_CONNECTION_PATHS`; `list` and `save` fill the
  saved password for the identity read only; errors go through
  `dbops.redact`; no server write): `/api/baselines/list` `{connection,
  kind}` returns `baseline-list-1.0` `{kind, server, server_key, max: 20,
  items}` newest first; `/api/baselines/save` `{connection, kind, name,
  report}` returns the item plus `max`; `/api/baselines/get` `{id}` (32
  lowercase hex) returns the item plus its payload; `/api/baselines/delete`
  `{id}` returns `{deleted: true, id}`, and a missing id is refused. Item
  fields: `id`, `kind`, `name`, `created_at` (ISO UTC), and `server`
  (`engine`, `flavor`, `product`, `version`, `server_uuid`, `hostname`,
  `port`), read with the reading it belongs to. Health items add `taken_at`,
  `uptime_s`, `interval`, `status` and the problem/watch/ok counts; digest
  items add `read_at`, `server_time`, `uptime_s`, `order`, `limit`,
  `statements`, `executions` and `total_ms`.
- Save checks, before any connection: kind, name, report shape
  (`health-1.0`, or `top-1.0` with `read_at` and at most 500 statements), and
  `reading_server(report)`, which requires `reading_server.verified is True` on
  the report and a matching engine and version. After the current identity read:
  refuse when `reading["key"] != server_key(identity)` or when the reading's
  engine or version differs from the server reached now, and refuse a reading
  taken before a restart (its uptime above the current Uptime plus 2 s).
  A `restarted: true` flag on the Health sampling window is retained; it is not itself a save refusal. The restart refusal above is the save-time uptime check. Refusals begin "Could not save the baseline:".
- Kept payload: Health keeps engine, flavor, product, version, interval,
  elapsed_s, started_at, taken_at, uptime_s, restarted, server facts, summary
  counts, `metrics` (`key`, `group`, `label`, `unit`, `value`, `status`) and
  `diagnostics` (`key`, `title`, `status`, `state`, `headline`, `value`,
  `unit`); explanations are dropped. Digest keeps engine, version, product,
  order, limit, read_at, `server_time` (the identity read's NOW(6) adjusted by the uptime elapsed since the reading), coverage (`uptime_s`, `counting_since`, `oldest_first_seen`,
  `digest_lost`), totals, and per statement `digest`, `schema`, `digest_text`
  through `plan_privacy.hide_literals` (cut at 4,096 characters), `count`,
  `total_ms`, `avg_ms`, `max_ms`, `rows_examined`, `rows_sent`, `first_seen`
  and `last_seen`; never `sample_text`, `analysis_sql` or index hints.
- Comparison (`baselineCompare.ts`): a Health verdict compares when both
  readings have one and they differ. A restart (uptime went down, or the
  computed start moved more than 120 s) leaves every tile except the gauges
  (`threads_connected`, `threads_running`, `connections_used`, `bp_dirty`,
  `bp_free`, `row_lock_current`, `table_cache_used`) Not compared, because a
  restart starts the counters over. Digest statements match by schema and
  digest: **New** means absent from the baseline with FIRST_SEEN after the
  baseline's `server_time`; **Grew** means after is more than 1.5 times
  before for average latency or calls per second, from at least 10 calls
  since the baseline (before uses the count over `min(uptime, server_time -
  first_seen)`, after the delta over the delta). `coverage.oldest_first_seen`
  is the whole digest table's `MIN(FIRST_SEEN)`, independent of the 1-hour
  `counting_since` threshold, so `compareDigests` detects a digest reset when
  `oldestNow > oldestBefore` or `oldestNow > serverClock(baseline.server_time)`
  even when top-N rows do not overlap or within the first hour of uptime.
  After a restart or a digest reset, only average latency is compared and
  nothing is judged new.
- Workspace: the chosen baseline lives in `HealthMemory.baseline` (Health)
  and in view state (Query digest); both clear on a connection switch and are
  never saved in the workspace session.
- Tests: `test/test_baselines.py`, `npm run test:baselines`
  (`baselines-unit.mjs`, `query-digest-baselines.mjs`), `test:health`.

### Waits, Indexes, Tables, I/O, Config

- **Waits, Indexes, Tables, I/O, Parameters (`config`)** (Workload place Resources group: Waits, I/O; Server place groups Schema: Indexes, Tables; Setup: Parameters (internal view id `config`); read-only, share the connection flags and `--profile`, `--json`, `-o`, `-q`): shared plumbing in `myflames/perf_common.py` (`Reader` tags every statement `/*myflames:<view>*/`; `preflight` reads variables with `SHOW GLOBAL VARIABLES WHERE Variable_name IN (...)`, never `SELECT @@x`, plus Uptime and `Performance_schema_*_lost`; P_S off returns empty tables, not errors, so check `performance_schema` first). Never TRUNCATE (it also resets per-table and per-index stats), never write `setup_instruments`/`setup_consumers`, never `SET GLOBAL userstat`; print the statement instead. Raw P_S/I_S tables only, never sys views.
  - `python3 -m myflames waits [--interval N] [--limit N]` (`waits-1.0`, `myflames/waits.py`): two reads of `events_waits_summary_global_by_event_name` N s apart (2–60, default 10), `EVENT_NAME LIKE 'wait/%'` (excludes idle); window deltas (a counter going down drops the window), per-class sums, since-start list with max; never add classes up or show a share (table I/O nests file and synch waits). Coverage from `setup_instruments` with the enable UPDATE and my.cnf line (`ENABLE_CAVEAT`). MDL instrument off by default on MariaDB; `innodb_temp_file` means differ per engine (`ENGINE_MEANINGS`).
  - `python3 -m myflames indexes [--schema NAME ...] [--all-schemas] [--limit N]` (`indexes-1.0`, `myflames/indexes.py`): unused = COUNT_STAR 0 in `table_io_waits_summary_by_index_usage`, read before I_S (MariaDB's STATISTICS opens tables, registering them with zero counts); PRIMARY and UNIQUE never listed on both engines; FK-backing indexes (KEY_COLUMN_USAGE, leading columns in order, PK columns appended per engine) are `keep`; tables with no row operations or a disabled `setup_objects` row are `no_data`. Partitioning is checked after usage and definitions, reusing the `TABLES.CREATE_OPTIONS` read for every engine, only for distinct tables with zero-use index candidates (at most 100 per statement, one schema and exact table-name predicates). MariaDB opens these candidate tables; MySQL reads dictionary fields. Partitioned candidates and tables whose metadata is missing, NULL or unreadable are `no_data` (`reason: partitioned|partitioning_unknown`, additive `reason_text`); zero Performance Schema counts cannot establish non-use on partitioned tables. Failed metadata batches keep a note and cannot produce unused candidates; successful batches remain usable. Inventory and redundant rows of affected tables carry additive `action_blocked_reason`, suppressing DROP and visibility actions, including row and context menus and inspectors. Blocked redundant rows retain string `test_sql`, `drop_sql` and `caveat` fields as empty strings. The `indexes-1.0` version and existing field types stay unchanged. Redundant indexes reuse `advisor.find_redundant_indexes` over I_S.STATISTICS (parity test with the advisor rule). Full scans use `SUM_TIMER_FETCH` of the NULL-index row. MariaDB P_S off + `userstat=ON` falls back to INDEX_STATISTICS/TABLE_STATISTICS. `--schema` repeats (scope `schemas`; `schema` stays for one); the CLI warns without `--schema` or `--all-schemas` and reads every user schema up to 2,000 tables (`perf_common.require_choice` on the selected scope). `--all-schemas` lifts that limit like the API's `all_schemas`, for every user schema or, with `--schema`, for those schemas only. The UI database picker sends `schemas`; with none, `/api/live/indexes` reads all user schemas only up to `perf_common.ALL_SCHEMAS_TABLE_LIMIT` (2,000, MySQL's default table_definition_cache) base tables and raises above it (`all_schemas: true` skips the check; the UI never sends it). The shared Indexes/Tables gate and schema picker note add a MariaDB-only sentence warning that the read opens every table in scope and can evict cached table definitions (`all_schemas_note(engine=...)`); MySQL keeps the existing message. Indexes text adds `Statements: myflames top --order no_index --table SCHEMA.TABLE` under TABLES READ WITHOUT AN INDEX for the first listed table only; empty/unavailable sections omit it.
  - `python3 -m myflames tables [--schema NAME ...] [--all-schemas] [--fresh] [--limit N] [--fail-on problem|watch]` (`schema-2.0`, `myflames/schema_health.py`; named `tables` because `--schema` already means one database): tables without a PRIMARY KEY from I_S.TABLES (BASE TABLE + MariaDB SYSTEM VERSIONED; sequences and views skipped) and I_S.STATISTICS `NON_UNIQUE = 0` in key order. `no_usable_key` (problem: hidden 6-byte DB_ROW_ID) vs `not_null_unique` (watch: InnoDB clusters on the first unique key with all parts NOT NULL, whole columns, not functional, not virtual generated, not MariaDB long hash). GIPK (`my_row_id`, EXTRA `auto_increment INVISIBLE`) is listed apart; when `show_gipk_in_create_table_and_information_schema` is OFF globally, prefix `SET SESSION ... = ON;` in the same client call. Fix statements (never run): NULL/duplicate checks, `ADD PRIMARY KEY (...), DROP INDEX uk, ALGORITHM=INPLACE, LOCK=NONE`, invisible `AUTO_INCREMENT` surrogate with `LOCK=SHARED`; no ALGORITHM/LOCK for other engines. Fragmentation: DATA_FREE is reusable allocation that a rebuild can return; `free_pct` is an approximate share of reported DATA_LENGTH + INDEX_LENGTH + DATA_FREE, not exact physical file size. Positive known file-per-table space is `info`; zero is `ok`; shared and unknown remain distinct. `summary.free_info` counts informational rows; `free_watch` and `free_problem` remain zero for compatibility, and `thresholds` is empty. Capacity and filesystem headroom are not measured, and DATA_FREE is not a capacity alarm: warnings require measured capacity evidence. The workspace calls this list **Fragmentation** (the JSON keys, statuses and the deprecated CLI text are unchanged): segment "Fragmentation N", status label Fragmented for `info`, columns Reclaimable (`free_bytes` for file-per-table rows; Unknown for `shared` and `unknown`, whose DATA_FREE belongs to the whole tablespace) and Fragmentation % (`free_pct`, DATA_FREE / (DATA_LENGTH + INDEX_LENGTH + DATA_FREE) x 100, whole free extents only), default sort Reclaimable descending, filter Minimum fragmentation %. A selected row's **Rebuild** section offers `OPTIMIZE TABLE` and `ALTER TABLE ... ENGINE=InnoDB` (quoted through `sqlIdent`), each to copy or open in the Query editor, and `online_sql` (`ALTER TABLE ... FORCE, ALGORITHM=INPLACE, LOCK=NONE`, refused instead of blocking writes) to copy, with short notes: recreate + analyze versus rebuild only, online with brief metadata locks, FULLTEXT or SPATIAL (MariaDB: also an index on a virtual column) copies the table and blocks writes, free disk about data plus indexes, binary logging and `NO_WRITE_TO_BINLOG` / `LOCAL` (OPTIMIZE_LOCAL_TABLE on MySQL 8.4+), shared tablespaces not shrinking, and cached statistics. The reviewed **Optimize table…** row action stays in the inspector header. Tests: `test/test_schema_health.py`, `apps/workspace/test/tables.mjs`, `apps/workspace/test/tables-fragmentation-unit.mjs`. Tablespace map: MySQL `INNODB_TABLESPACES` (record-by-record DD scan with informational `AUTOEXTEND_SIZE`; `INNODB_TABLESPACES_BRIEF` fallback on "Unknown column"; never `INNODB_TABLES`, which loads every table into the dictionary cache) plus `SHOW CREATE TABLE` (max 50) only when a General tablespace exists; MariaDB `INNODB_SYS_TABLES` (space 0 = system; `INNODB_SYS_TABLESPACES` lists only opened spaces). Needs PROCESS, else `unknown` + note. `--fresh`: `SET SESSION information_schema_stats_expiry = 0;` before the TABLES read, same client call (MySQL only; MariaDB has no cache). Row-ID, replica-apply and OPTIMIZE texts are version-aware (`row_id_text`, `why_texts`, `optimize_caveat`). Schema picker list: `perf_common.schema_catalog` (`schemas-1.0`, `/api/live/schemas`; MariaDB counts name columns only so no table is opened); `check_schemas`/`scope_sql` are shared with Indexes.
  - MariaDB 11.2+ MyISAM primary-key rebuild advice states that the copy holds a table read lock: UPDATE and DELETE wait for the whole copy; only concurrent inserts continue (adding AUTO_INCREMENT blocks those too). InnoDB retains its in-place/LOCK guidance. Hidden InnoDB row IDs use one shared atomic counter on MariaDB 10.6 to 10.11, then a counter per table from 11.0. Test: `test_schema_health.py::test_correctness_regression_myisam_copy_lock`.
  - Rule passes stay linear in the rows read: Indexes builds the `setup_objects` lookup once per report and checks the three precedence keys per table; Tables computes whether any General tablespace exists, and the case-folded tablespace names used when `lower_case_table_names` is 1 or 2, once per report (`schema_health._folded`); Locks builds each metadata-lock group's owner list once and shares it across the pending requests that read it (`locks._mdl_blockers`). `test_indexes.py`, `test_schema_health.py` and `test_locks.py` count these passes.
  - `python3 -m myflames io [--interval N] [--schema NAME] [--limit N] [--all-instances]` (`io-1.0`, `myflames/io_stats.py`, never `io.py`): status deltas (no P_S needed), `table_io_waits_summary_by_table` (schema-scoped), `file_summary_by_instance` and `_by_event_name` (temp files only in the latter), since-start lists; `classify_file` maps FILE_NAME to kinds and schema.table (`#p#`/`#P#`, `@XXXX`). MariaDB has no redo log file instrument. Above `INSTANCE_LIMIT` (2,000) table or file instances a family is left out of both snapshots (`collection.omitted`) unless `--all-instances` / `all_instances`; collection has a 30 s budget plus the interval. No auto-refresh in the UI.
  - `python3 -m myflames config [--fail-on problem|watch]` (`config-1.0`, `myflames/config_review.py`): checks with engine `DEFAULTS`, `set_by` from `variables_info` (MySQL) or `SYSTEM_VARIABLES` (MariaDB), statuses with `RULES`, `why`, `caveat`, and `action` text; reuses `advisor.buffer_pool_coverage` and `advisor.flush_log_durability` (MariaDB's 3 is durable). Tablespace sizes need PROCESS (else `unknown`). `--fail-on` exits 1 like `health`. Each setting carries `dynamic` (`variables.static_dynamic`: true, false, or null when unknown, no extra read); changing a setting is [`config set`](#server-variables-config-set-reset-persist). The CLI `myflames config` is unchanged (no parameter list, no new flags). `/api/live/config` (still `config-1.0`, internal view id still `config`) calls `config_review.config_report(conn, parameters=True)` and includes `parameters` (`parameters-1.0`, built by `myflames/parameters.py`: `read_parameters` and pure `compute_parameters`) with `complete`, `notes`, and `items[]` (`name`, `value`, `truncated` for values over 4,096 characters, `scope`, `apply_type`, `modifiable` (`now`, `restart`, `option_file`, `none`, `null`), `type`, `values`, `min`, `max`, `block_size`, `default`, `source`, `source_label`, `source_path`, `set_time`, `set_user`, `set_host`, `persisted`, `modified`, `description` capped at 600 characters (MariaDB `VARIABLE_COMMENT`; MySQL 9.0+ `variables_metadata.DOCUMENTATION`; MySQL before 9.0 reports none, so `parameters.source_help` takes the help text the variable is declared with in the tagged source from the generated `myflames/mysql_sysvar_help.py`: `HELP` from the newest 8.4 tag, and `HELP_80` from the newest 8.0 tag where 8.0 declares another text or a name 8.4 removed; a text built with a macro, or one that only repeats the name, is left out), `documentation`, `checks`) and `flavor` (`mysql`, `percona` or `mariadb`: the server object's `flavor` when it has one, else `percona` when `@@version_comment` names Percona). `documentation` is `{url, source}` or null from `parameters.documentation(engine, version_tuple, name, flavor)`: the MySQL Reference Manual for MySQL and Percona servers (the first saved manual at or after the server's major.minor, else the newest; a name that manual leaves out comes from the nearest newer, then older manual, and `source` names it, "MySQL 9.7 Reference Manual"), MariaDB's documentation for MariaDB, and only for a name no MySQL manual documents on a Percona server, Percona Server's then Percona XtraDB Cluster's manual. The locations come from the generated `myflames/sysvar_docs.py`: `scripts/gen_sysvar_docs.py --saved-on DATE` reads saved official pages (never the network): each MySQL manual's "Server Option, System Variable, and Status Variable Reference" (rows whose System Var column says Yes; with `--mysql-pages`, the linked pages, a row that links an option anchor uses the `sysvar_<name>` anchor a saved page defines, and an anchor no saved page defines is dropped), MariaDB's "Full List of MariaDB Options, System and Status Variables" (lowercase system variable names whose link text is the anchor, status variable pages skipped; with `--mariadb-pages`, a stale anchor moves to the one saved `...system-variables` page that defines it, and a heading on such a page that the list leaves out is added) and saved copies of the Percona Server and Percona XtraDB Cluster manuals (a heading whose lowercase text is a variable name with the option table under it). Saved on 2026-10-21 from the MySQL 8.0, 8.4, 9.7 and 26.7 manuals, MariaDB's documentation, Percona Server 8.0, 8.4 and 9.7 and Percona XtraDB Cluster 8.0 and 8.4; a live MySQL 8.4.11 server links 621 of its 623 parameters, MySQL 9.7.2 625 of 631, MariaDB 11.4 657 of 674 and a Percona XtraDB Cluster 8.4 node 719 of 740. The desktop app opens `dev.mysql.com`, `mariadb.com` and `docs.percona.com` (`apps/desktop/backend.cjs` EXTERNAL_HOSTS); `Config.tsx`'s `documentationLink` accepts only HTTPS links to those hosts. Tests: `test/test_sysvar_docs.py`. `scripts/gen_mysql_sysvars.py --mysql-source PATH` writes `mysql_sysvar_help.py` beside `mysql_sysvars.py` (`parse_help`; tests in `test/test_mysql_sysvars.py` and `test/test_parameters.py`); a live MySQL 8.4.11 server describes 571 of its 623 parameters this way and MySQL 8.0.46 577 of 631. `read_parameters` reads MySQL `SHOW GLOBAL VARIABLES`, `performance_schema.variables_info`, `persisted_variables` and (9.0+) `variables_metadata`, or MariaDB `information_schema.SYSTEM_VARIABLES` without `SESSION ONLY` variables; a denied or missing table becomes a note and `complete: false`, never an error.
  - Generated MySQL system variable catalog: `scripts/gen_mysql_sysvars.py --mysql-source <mysql-server checkout>` writes `myflames/mysql_sysvars.py` (817 variables) from the `mysql-9.7.0` tag and the newest 8.4 tag (`sys_vars.cc` declarations with or without `static` and with `(` or `{` initializers, plus InnoDB, semisync, X plugin, group replication, `caching_sha2_password` and `validate_password` declarations), recording scope, type and the flags `ro` (`READ_ONLY`), `np` (`NON_PERSIST`), `pro` (`PERSIST_AS_READONLY`) and `nc` (no startup option: `NO_CMD_LINE`, the `sys_vars.h` classes that force it, or `PLUGIN_VAR_NOCMDOPT`); facts that differ between the two versions are recorded as unknown; a declaration-count test fails when a new declaration shape appears. `variables.py` keeps its hand-verified map authoritative and falls back to the catalog; `test/test_mysql_sysvars.py` checks that all 84 hand-verified entries agree. On a live MySQL 8.4 server, 597 of 623 parameters get an apply type; the SSL and admin TLS variables (declared in `sql/ssl_init_callback.cc`, and applied only by `ALTER INSTANCE RELOAD TLS` or a restart) and a few plugin variables stay unknown. `modifiable` is `none` for a static variable the server sets itself (MySQL catalog flag `nc`; MariaDB `COMMAND_LINE_ARGUMENT` is NULL), except variables set through separately named mysqld options (`core_file`, `log_bin`, `log_bin_index`, `relay_log_index`, `skip_external_locking`, `locked_in_memory`), which stay `option_file` or `restart`. `modified` is false for MySQL `COMPILED` and MariaDB `COMPILE-TIME` or `AUTO` (sized by the server). Batch output prints SQL NULL and the text NULL alike, so values read as NULL are read again exactly (`VALUE IS NULL, HEX(VALUE)` from `performance_schema.global_variables` or `SYSTEM_VARIABLES`); when that read fails the inventory is `complete: false`. `variable-1.0` carries the same fact as `startup_option` (`variables.startup_option`: false, true or null), so the single-parameter plan offers neither SET PERSIST_ONLY nor an option file line for a value the server sets, and the inspector's Change… is off for `modifiable: none`. In the batch review, a request that fails without a reply is reported as `unknown` (it may have run), never as unchanged.
  UI: `Waits.tsx`, `Indexes.tsx`, `Tables.tsx` (+ `tables.css`), `IoStats.tsx`, `Config.tsx` (Parameters), shared `PerfShared.tsx` (incl. `SchemaPicker` and `useSchemaCatalog`, one cached list per connection) + `perf.css`; endpoints `/api/live/waits|indexes|tables|schemas|io|config` (in `ui._SERVER_VIEW_PATHS`, saved-password helper). Session restore keeps the tab, the shared `schema` (I/O and Top statements' schema filter), per-tab `indexes_schemas`/`tables_schemas` (omitted when empty = all user databases) and `waits_interval`/`io_interval`, never reports. Links: I/O table rows open Index health or Top statements for that schema; Indexes and Tables rows open each other for the same table (picker set to its database, a "Showing only" focus chip); wait rows open Locks or I/O; Full analysis links that name a check open Parameters at that check's first parameter, InnoDB's link reads "Open Parameters", and the Full analysis section is named Parameters. Tests: `test/test_waits.py`, `test_indexes.py`, `test_schema_health.py`, `test_io_stats.py`, `test_config_review.py`, `test_mysql_sysvars.py` (replay fixtures recorded live in `test/fixtures/perf/`, helper `test/_perf_replay.py`), browser suites `apps/workspace/test/{waits,indexes,tables,io,config}.mjs` (`npm run test:waits|test:indexes|test:tables|test:io|test:config`, shared `perf-harness.mjs`, whose `open(endpoint, name, extra)` mocks extra paths such as `/api/live/schemas`).

### Memory

`myflames/memory.py`, `POST /api/live/memory` (read-only server-view path,
saved-password helper), Workload > Resources > Memory (`Memory.tsx`). The
request takes `{connection, interval?}`; `interval` is an integer from 2 to
60 seconds (`perf_common.check_window`), default 5; the UI dropdown offers 2,
5, 10, 15, 30 and 60. `memory-1.0` carries the shared `engine`, `flavor`,
`server_product`, `server_version`, `taken_at`, `uptime_s` and
`performance_schema` fields plus `available`, `source` (`"performance_schema"`,
`"mariadb_status"` or `null` when neither is available),
`interval`, `elapsed_s`, `window_status` (`ok`, `restarted`,
`coverage_changed`, `unavailable`), `status` (`ok`, `watch`, `not_judged`),
`gate`, `notes`, nullable `current_bytes`, `high_bytes`,
`growth_bytes_per_s`, `innodb_buffer_pool_size` and `buffer_pool_share_pct`,
`coverage` (`{enabled, total}` or null), `consumers` (name to enabled, or
null), and `thread_coverage` (`{foreground, uninstrumented}` or null), `categories`, `events`, `sessions`, `accounts`. `collection`
(`{events, sessions, accounts}` booleans) distinguishes an empty result from
an unreadable family. `findings` are `{key, status, rule, reason}` and are
labeled rules of thumb.

Global bytes and event highs are SUMs from
`performance_schema.memory_summary_global_by_event_name`; category sums use
known name prefixes (`buffer_pool`, `temporary`, `sort`, `join`, `network`,
`performance_schema`, `other`; unknown instruments remain `other`). Session
and account reads use the raw memory summaries, with foreground threads
joined to `performance_schema.threads`. Coverage and consumer state are read
before and after the two samples: an internal SHA-256 fingerprint of the
sorted (NAME, ENABLED) memory instruments (at most 8,192 plus one sentinel;
overflow makes coverage unknown and is never returned) detects instrument
swaps even when the enabled counts stay equal, and any change drops growth
with `window_status: coverage_changed`, as does an observed restart.
Sequential reads are not an atomic snapshot; `high_bytes` sums independent
event highs (upper bounds rather than recorded peaks), never a simultaneous server peak; deallocation is valid negative
growth. Each category adds its own `elapsed_s`, and its `growth_bytes_per_s`
uses that interval, while the top-level rate uses the total-query
completions. Shares divide `max(0, category current_bytes)` by the sum of
the non-negative category current bytes, return null for a zero denominator
and never exceed 100%.

Rules of thumb: growth is Watch only on a strict increase above both 1% of
the first tracked total and 16 MiB across the window; a category Watch
applies only to the workload categories (`temporary`, `sort`, `join`,
`network`) above 25% of the current tracked bytes (`buffer_pool`,
`performance_schema` and `other` cannot trigger it); a visible session above
1 GiB current bytes is Watch. Fewer than half of the memory instruments
enabled, unknown collection, missing required breakdowns, lost
instrumentation, more than 10% uninstrumented foreground threads or an incomparable window produce overall Not judged while
keeping any observable Watch findings. Session and account rows add an
optional `collection_reason` when zero accounting cannot establish collection
(attribution instruments or consumers off or unknown), so a genuine zero
stays distinguishable from Not collected.

MariaDB with Performance Schema off, or unavailable global memory tables,
uses the `mariadb_status` source: the global `Memory_used` status variable
and `information_schema.PROCESSLIST` `MEMORY_USED`/`MAX_MEMORY_USED` (at most
10 rows; without PROCESS only the account's own sessions are visible), with
no global high-water, category or account data or buffer-pool share inferred,
and a one-sentence pointer to Server > Instruments. MySQL 5.7 reads the
available summaries and explains disabled workload instrumentation.

Limits and safety: at most 7 categories, 15 events, 10 sessions and 10
accounts returned; 5 s per-query timeout and 30 s of total read execution
excluding the sampling sleep. LIMIT bounds the output, not the scan work of
the summaries. No application table contents or sys views are read, no
instrument changes, no server or user text is interpolated into the
statements, and no secret enters the report; no automatic polling. The
workspace reads once on open (manual Refresh reads again), shares the
single-flight and stale-response guards of Refresh all, and stores
`memory_interval` and the `memory.*` column layouts, never reports. A report
read on another connection offers no Instruments action on the current one.
Presentation (`Memory.tsx`): byte values use binary units; the Numbers and Server context fields in the inspector show the binary value with the exact bytes beside it in muted text (`.memory-value`, `.cell-muted`), so keyboard and screen-reader users get them too. General rules of thumb appear once in the notes (ⓘ), while row-specific findings have a status pill on the row and their reason in its inspector. Table headers are **Sum of highs** (tooltip "Sum of event highs: upper bounds, not a recorded peak") and **Share (%)** (tooltip "Share of tracked memory (non-negative category bytes)"); the inspector keeps **Sum of event highs**, while the MariaDB PROCESSLIST fallback uses **Session high**. When categories were not collected but sessions were, the view selects **By session**. A missing total high reads "sum of highs not collected".
Tests: `test/test_memory.py`, `apps/workspace/test/memory-unit.mjs` and
`memory.mjs` (fixtures from `memory-fixtures.py`).
### Server variables: config set, reset-persist

Backend `myflames/variables.py` (contract: the db-operations skill; facts and source citations: the module docstring), CLI `_config_change` in `myflames/workspace_cli.py` (dispatched from `cmd_config`), endpoints `ui._variable_response` / `_variable_apply`, UI `Config.tsx` + `config.css`. Tests: `test/test_variables.py` with live replays `test/fixtures/perf/variable-<server>-<scenario>.json` (MySQL 8.4.11 and MariaDB 11.4.13, recorded 2026-10-15 on throwaway containers: SET GLOBAL; SET PERSIST, then a container restart, `restart` shows the value survived; SET PERSIST_ONLY of the read-only innodb_log_file_size, `restart-persist-only` shows it applied; RESET PERSIST; SET SESSION; the 1238 and 1227 refusals; a value changed between plan and apply; buffer pool and MariaDB block-size rounding), browser suite `apps/workspace/test/config.mjs` (`variable-*.json` written by `server-fixtures.py`).

- `read_variable(conn, name)` (`variable-1.0`, tag `/*myflames:variable*/`, reads only): one client call with `SHOW GLOBAL VARIABLES` and `SHOW SESSION VARIABLES` separated by marker rows, then MySQL `performance_schema.variables_info`, `persisted_variables` and (9.0+) `variables_metadata`, or MariaDB `information_schema.SYSTEM_VARIABLES`. Fields: `scope` (global, both, session, or null), `dynamic` (MariaDB `READ_ONLY`; MySQL from the authoritative 84-entry `MYSQL_VARIABLES` map in `variables.py` or the generated 812-variable `mysql_sysvars.py` catalog, null outside both), `type` (bytes, integer, double, boolean, enum, set, flagset, string), `values`, `min`, `max` (MariaDB catalog; MySQL variables_info or variables_metadata for integers, the map for doubles), `block_size` (MariaDB), `global_value`, `session_value`, `default`, `set_by`, `persisted {supported, value, present, load}`, `modes {global, persist, persist_only, session, reset_persist: {available, reason, recommended}}`, `notes`. A MySQL variable outside the map is offered by the CLI only; the server decides (1238 when read-only).
- `plan_change(conn, name, value, mode)` and the pure `build_plan(v, value, mode, grants)` (`variable-plan-1.0`): the mode must be available (MariaDB has no PERSIST, PERSIST_ONLY or RESET PERSIST: refused with `alternative.config_line`, the `[mysqld]` line; a read-only MySQL variable offers only `persist_only`; wrong scope; NON_PERSIST; a session value that is read-only), and the value valid (`normalize_value`: integers within the range, K/M/G/T converted for byte sizes, doubles, ON/OFF, enum and set items matched without case, `DEFAULT`; string values preserve leading and trailing whitespace exactly, while numeric, boolean and enum input is trimmed; text as `'...'` when plain, else `CONVERT(X'..' USING utf8mb4)` so no escaping rule or sql_mode matters). Returns `statement` (untagged), `display`, `before {global, session, persisted}`, `undo`, `effect {now, after_restart, needs_restart, text}`, `caveats` (the server does not check a PERSIST_ONLY value, persisted_globals_load OFF, mysqld-auto.cnf applied after the option files, buffer pool rounding to chunk size x instances and the 1 GiB rule with several instances, MariaDB block size, memory per connection, durability, MariaDB's log_slow_query copied per connection, replication-sensitive session variables), `privileges` (`privileges(v, mode, grants)` over plain `SHOW GRANTS`: dbops' operations, MariaDB's per-variable SET GLOBAL privileges `MARIADB_GLOBAL_PRIVILEGE`, restricted SET SESSION per engine with MySQL's `_no_super` ones, both privileges to reset a PERSIST_AS_READONLY entry), `classification`, `config_line` and `expect {name, mode, statement, global_value, session_value, persisted_value}`. Status `refused` with `problems [{code, message}]` sends nothing.
- `apply_change(conn, plan, expect, decision, confirmed, audit)` (`variable-change-1.0`): `dbops.gate` first (SET GLOBAL, PERSIST, PERSIST_ONLY and RESET PERSIST are risky, so Production and Development confirm; SET SESSION follows dbops' variable lists; read-only refuses), then a fresh read and plan; nothing is sent when the plan is refused, its statement differs from `expect.statement`, or the global, session (session mode) or persisted value changed. The audit record is written before sending; one statement tagged `/*myflames:variable*/` with `dbops.WRITE_CLIENT_FLAGS`; SET SESSION and its `SHOW SESSION VARIABLES` go in one client call (one session). Server errors: `denied` (1227, 1095, 3630, 1142, 1044, 1045), `refused_by_server` (1228, 1229, 1238, 1231, 1232, 1193, 1621, 3615, 3976, 1210, 1290), `interrupted` (1317, 1205, 1213), else `failed`; client errors (2000 and up) raise, and an interrupt, time limit or 2013 leaves the record `unknown`. Then the variable is read again (`after {global, session, persisted, resize_status}`): `changed` (`adjusted` when the server rounded or clamped it) or `unchanged` (the server kept the old value, a warning-only refusal). Audit operations `set_global`, `set_persist`, `set_persist_only`, `set_session`, `reset_persist`.
- `python3 -m myflames config set NAME VALUE [--persist | --persist-only | --session] [--yes] [--json] [-o PATH] [-q]` and `config reset-persist NAME [--yes] [--json] [-o PATH] [-q]` (`change_cli`): the policy from `--profile` runs before any prompt or connection (read-only refuses, exit 2); the plan (statement, values, effect, privileges, caveats, undo) on stderr; `[y/N]` when the policy confirms (a harmless SET SESSION on Development runs without); non-TTY needs `--yes`. Exit 0 changed or already set to the requested value, 1 a sent change was denied, refused by the server or not applied, 2 an unsent refusal, decline, bad input or connection failure. `myflames config` alone stays the read-only review. `config set --session` prints a stderr note that it applies only to the one-off connection myflames opens, useful to test a value; `--yes -q` suppresses this note with the plan.
- Endpoints: `POST /api/live/variable` (`name`) and `POST /api/live/variable/plan` (`name`, `value`, `mode`; adds `policy {action, tone, title, text, connection_type, read_only}`) read with the saved password like the views (`_VARIABLE_READ_PATHS`, not `_SERVER_VIEW_PATHS`); `POST /api/live/variable/apply` (`name`, `value`, `mode`, `expect`, `confirmed: true`; `_WRITE_PATHS`: policy, then the saved password, then `apply_change`). A refused or unconfirmed request is audited.
- UI (**Parameters**, `Config.tsx` + `config.css`, modelled on an AWS RDS parameter group): reads on open (one attempt per connection; Refresh retries), while the sidebar badge still comes from the checks. One table lists every global parameter the server reports with columns Name, Value, Allowed values, Apply type (Dynamic, Static, Unknown), Source, Data type, and Description; search ("Filter parameters") matches name, value and description, and the Show filter offers All, Modified (source is not the compiled default), Dynamic, Static, Recommendations (parameters covered by a Watch or Problem check), and Pending while editing. The view toolbar stays pinned under the top bar. **Edit** turns the Value column into inline controls (switch, list or text box from the parameter's type) for changeable parameters: MySQL dynamic (applies now) and MySQL persistable static (applies after the next restart through `SET PERSIST_ONLY`), or MariaDB dynamic only; MariaDB static parameters are option-file only and parameters with an unknown apply type are not editable in the table. **Preview changes** lists Name, Current value, New value, Apply type and When it applies. **Review and apply** opens one batch review for up to 25 changes: the server plans each change (exact statement, problems, privileges, effect, undo); MySQL dynamic rows use "Keep after restart" (checked: `SET PERSIST`, unchecked: `SET GLOBAL`), MySQL static rows use `SET PERSIST_ONLY`, and MariaDB rows use `SET GLOBAL` and show the option file line to copy. Confirm applies the changes one at a time (each re-read and checked first), stops at the first change that is not confirmed, and shows a result per row; Cancel or Escape records a declined attempt for every planned row in the audit trail. The inspector has one compact section per parameter: one sentence (`oneLine`: the server's description, else the opening sentence of a check whose first setting it is), the **Documentation** link (`documentationLink`), and **Current value** (Value, Default, Source with path and who set it when, Changes in words from apply type, scope and `modifiable`, Allowed values, Persisted on MySQL, and Checked when covering checks do not flag it); a **Recommendation** follows only for a Watch or Problem check, the most severe one (reason, `action.text`, the statement and option file line as CopySql, the caveat), naming any other flagging check once. Never repeat the manual there. The actions sit in the inspector's action bar: **Stage value for review** (`editParameter`: turns on inline editing, filters the table to that parameter and focuses its value control), **Reset to default** (stages `DEFAULT` for MySQL numeric and boolean dynamic parameters), **Change this parameter…** (the single-parameter `VariableSheet` with every mode, including This session only), and **Reset persisted value…** (MySQL, when persisted); exact review, policy checks, connection binding, cancel and apply are unchanged. While the shared inspector is a sheet (below 1,040 px) the table uses the existing compact preset (`compact={sharedInspector?.sheet}`, `detailColumns={["allowed", "type", "description"]}`): Name, Value, Apply type and Source stay, and Allowed values (still in the inspector), Data type and Description become detail columns that Columns can restore through the persisted-width override. Permission details appear in the review dialog body; entry and confirmation buttons omit the Unverified badge.
- Session in Parameters (**Change this parameter…** > This session only) prepares SQL only. `ConfigView.onOpenSql(sql)` receives the exact server-planned `SET SESSION` statement plus its terminator; `VariableSheet` calls it for **Open in Query editor**, never `/api/live/variable/apply`. Cancel/Escape on a Session draft creates no declined audit. The editor owns execution and its existing policy checks. The planning session value is labelled as a snapshot; its undo is omitted because the editor may have a different value. Without the optional callback the button is disabled and the statement stays copyable. Shell integration: pass `onOpenSql` through `Server` to `ConfigView`; in `main.tsx` call `labRef.current?.openSql(sql)` and `setMode("query")`. Add `openSql` to `QueryLabHandle` and its imperative handle to update the current tab via `changeTab`, clear stale digest source/selection metadata, and focus it without calling `send` or capture. These shell files are owned separately from Config. `node test/config.mjs --unit` checks the handoff without a browser.


Numeric configuration literals use exact fixed-point text (for example `10` and `0.00000001`), including SQL, audit statements and option-file lines. When verified before/after values already satisfy every requested scope, the result is `unchanged` with a successful audit outcome; it is not counted as a change. A new persisted value still counts as changed even when the running value already matches. CLI plans and alternatives name SQL operations and `config set` flags.

### Instruments

`python3 -m myflames instruments [--preset minimal|locks|waits|defaults] [--json] [-o PATH] [-q]` (`instruments-1.0`, `myflames/ps_instruments.py`; Setup group in Server.tsx, between Parameters and Maintenance): reads `setup_instruments` (+ PROPERTIES on MySQL), `setup_consumers`, `setup_actors`, `setup_objects` and P_S's own memory; never runs a write on the CLI. `instruments-1.0` includes `instruments[]` (`name`, `group`, `enabled`, `timed` (`null` when timing does not apply), `timed_applies`, `default_enabled`, `default_timed`, `builtin`, `properties`). Groups (statement, stage, file, table I/O, table lock, MDL, synch, socket, memory, transaction, idle, error) and consumers each carry gain/lose/cost (cost levels are rules of thumb), the engine default, changes from it, and on/off runtime SQL plus my.cnf lines. `default_state` / `default_consumer` are the compiled defaults per engine (MySQL 8.4/9.x identical in source, 26.7 matched live; MariaDB: MDL, transaction, memory and every events_* consumer off, no `error`, no PROPERTIES) and a test checks them against every live fixture instrument. `consumer_parent`: history needs `_current`; statement, transaction and wait `_current` need thread_instrumentation; stage events need only global (source and live). setup_actors does not gate metadata locks or background threads. Presets (`minimal`, `locks`, `waits` with `undo`, `defaults`) were run live on MySQL 8.4/9.7/26.7 and MariaDB 11.4/13.0; `defaults` restores an exact fresh-server state. Restore `setup_actors`/`setup_objects` with TRUNCATE + INSERT, never DELETE (MariaDB's setup_objects DELETE removes the TABLE row whatever the OBJECT_TYPE). `FEATURES` + `TABLES` map each view to the P_S tables, instruments and consumers it needs; optional guard features carry `advisory: True` (such as `replication_receiver`) for features shown in Instruments with guidance but never counted as unready views; `test/test_ps_instruments.py` fails when a view module reads an unmapped table or names an unmapped instrument/consumer, so update the map with the module. UI: `Instruments.tsx` + `instruments.css`, `/api/live/instruments` (in `_SERVER_VIEW_PATHS`; reads on open, one attempt per connection, Refresh retries). Keeps the Performance Schema row, preset picker and change banner above four tabs: **Features** (the "What myflames can show" checklist), **Instruments**, **Consumers**, and **Actors and objects**. The Instruments tab has search, group filter, state filter (All, Enabled, Disabled, Different from default), a checkbox per row, a header checkbox for every row shown, **Select all shown** (up to 2,000), and the actions **Enable**, **Enable without timing**, **Disable**, and **Clear selection**. The Consumers tab has checkboxes, **Enable**, and **Disable**; enabling a consumer also enables the parent consumers it requires and says so. Statements are built only from names in the loaded report: `UPDATE performance_schema.setup_instruments SET ENABLED = ..., TIMED = ... WHERE NAME IN (...)` for timed instruments and `ENABLED` only for untimed ones (`memory`, `error`), at most 500 names per statement; built-in `memory/performance_schema` instruments are skipped by Disable; names containing a backslash are refused; consumers use `UPDATE performance_schema.setup_consumers`. Statements run through the existing reviewed SQL runner (exact SQL, confirmation, write policy, audit). Runtime changes are lost at restart; **Keep after restart** shows copy-only option file lines (`performance-schema-instrument='name=ON|OFF|COUNTED'`, `performance-schema-consumer-name=ON|OFF`). `InstrumentsLink` (context from `SlowQueries.tsx`) links Waits, Indexes, I/O, Locks and Top statements "off" notes to a row (`group:<key>`, `consumer:<name>`, `ps`, `actors`, `objects`). Tests: `test/test_ps_instruments.py` (replay fixtures `test/fixtures/perf/instruments-*.json`), `apps/workspace/test/instruments.mjs` (`npm run test:instruments`).
### Full analysis report

Health diagnostic findings preserve the optional `state` from `health-1.0` (`string | null`, absent in older reports). Tuning reads this state through `findingItem`, keeping `room` measured and `not_applicable` excluded just as when consuming Health directly. This is an additive `report-1.0` field; other finding types retain their existing shape.

`python3 -m myflames report [--interval N] [--schema NAME ...] [--all-schemas] [--json | --markdown | --html | --digest] [--fail-on problem|watch] [-o PATH] [-q]` (`report-1.0`, `myflames/report.py`; endpoints `POST /api/live/report` and `POST /api/report/export`). One read-only run of every Server view on one connection: `report.preflight` (tag `/*myflames:report*/`; each subprocess owns its own temporary option file) and the all-schemas table count (`perf_common.require_choice`, `ALL_SCHEMAS_TABLE_LIMIT`; above it Indexes and Tables are `skipped` with a note unless `--all-schemas` / `all_schemas: true`), then `health`, `waits`, `io` (windowed, `--interval` 2 to 60, default 10, submitted first), `config`, `innodb`, `sessions`, `instruments`, `checks` (`server_checks.py`), `tables`, `indexes` (capped at `LIST_LIMIT` 50 each), at most `MAX_WORKERS` (3) at a time in a `ThreadPoolExecutor` (`_run_all`; wrapped in a `_Guarded` connection where Ctrl+C sets the cancel event that windowed sleeps wait on, cancels queued views, and does not wait for running ones), then the attribution (`discover.top_report` with `full_join`, and `no_index` + table for up to `ATTRIBUTION_TABLES` (3) tables read mostly without an index, `ATTRIBUTION_LIMIT` 5 rows each). The attribution jobs share one `_AttributionContext`: the metadata and coverage statements every `top_report` sends (version and Performance Schema state, the two digest consumers, `performance_schema_max_sql_text_length`, the digest overflow row, `Uptime` and `Performance_schema_digest_lost`, matched by their exact text) are read once per analysis, an error included, and each filter still reads its own digest rows: 9 client calls instead of 24 on the MySQL recordings, 8 instead of 20 on MariaDB. A failing view is caught per view (`_timed`; its error is redacted of the password) and listed in `header.failures`; the sections it feeds say what is missing. Never add SQL or thresholds to `report.py`: each family's rule lives in its module (`health.RULES`, `config_review.RULES`, `innodb_status`, `sessions`, `schema_health`, `server_checks.RULES`), and the report only maps items to findings (`_health_findings`, `_check_findings`, `_innodb_findings`, ...).
  Envelope: `schema_version`, `myflames_version`, `engine`, `server_version`, `taken_at`, `uptime_s`, `performance_schema`, `header {server {engine, version, version_comment, uptime_s, performance_schema}, started_at, elapsed_s, interval, scope {schemas, all_user_schemas, text}, views [{key, label, status ok|limited|failed|skipped, schema_version, started_at, taken_at, elapsed_s, error, notes}], failures [{view, label, status, error}]}`, `summary {problem, watch, unknown, info, ok, findings, status, text}`, `look_first` (a finding id or null), `findings[]`, `sections[] {key, title, status, summary, counts, findings (ids), views, notes}` in `SECTIONS` order (memory, redo, connections, temp_sorts, caches, query_cache, indexes, schema, waits, io, innodb, replication, security, observability), `attribution {available, gate (one sentence when unavailable), fix, label, caveats, coverage, health_window, full_joins {order, statements, taken_at}, full_scans [{schema, table, label, rows_without_index, fetch_ms_without_index, share_time, statements, capped, error}], notes}`, `checklist {covered, not_checked}`, `health_metrics` (every Health metric of the window with its status, the health-1.0 fields minus link, learn, sources and sparkline; `report.health_metrics`; empty when Health did not run; absent in older reports), `notes`. A finding: `id` (`<view>.<key>`), `family` (a section key), `status` (problem, watch, unknown, info, ok), `title`, `headline`, `verdict` (what this server shows, without the rule), `why`, `rule`, `caveat`, `action {text, sql, config}` (never applied), `numbers [{label, text, value?, unit?}]`, `settings`, `links [{view, label, key? | order?, table?}]` (views are the Server tab keys; `top` links carry `order` and `table` for attribution), `source {view, kind, key}`, `learn`, `impact`, `attribution` (`full_joins` or `full_scans`). Ranking is one function, `rank_findings`: status (problem, watch, unknown, info, ok), then `FAMILY_RANK` (replication, redo, memory, connections, innodb, security, temp_sorts, caches, schema, indexes, query_cache, io, waits, observability), then `impact`; `look_first` is the first problem, else the first watch. Sections and `summary.status` aggregate with problem > watch > unknown > info > ok (`_worst`, `summarize`): an unknown finding, or a view that failed or was skipped, never reads as ok, and the summary text counts unknowns. A finding that could not be judged (a privilege, Performance Schema off, tablespaces not readable for fragmentation, NULL replica lag with both threads running) is `unknown`, and every human format lists unknown findings with their reason (NOT JUDGED / Not judged). `coverage_lines` is the one projection of the attribution's scope (cumulative digest counters vs Health's window, uptime and the oldest FIRST_SEEN, the overflow row, `Performance_schema_digest_lost`, list caps) used by text, Markdown, HTML and the digest, and a table whose lookup failed shows its error, never an empty match. `at_or_worse` drives `--fail-on` (exit 1; 0 otherwise; 2 on bad input or when the server cannot be read, including when every attempted view fails). Total view failure prints the reasons on stderr, emits no payload and preserves any output file. Partial view failure keeps the report and existing exit code, and prints one warning line naming the failed views on stderr, including with `--json` or `-q`. Outputs are generated from the JSON only (`export(report, fmt)`): `format_text` (default; notes on stderr), `format_digest` (one line per non-ok finding with its numbers and rule, ok counted), `format_markdown` (`md` escapes HTML as entities and Markdown punctuation, `md_code` fences longer than any backtick run), `format_html` (static, `output_html_report._CSS`, every string through `html.escape`, the JSON embedded as `application/json` with `<`, `>`, `&` escaped, no script runs); every text format passes through `connector.terminal_text`. `/api/live/report` takes `connection`, `interval`, `schemas` (or `schema`), `all_schemas`; it uses the saved-password helper (`_SERVER_VIEW_PATHS`), `query_timeout` 60, and `run_single_flight` keyed by `report.flight_key` (the profile id, every effective connection and TLS setting, and an HMAC-SHA256 of the password under a per-process random key, never stored): only requests that would authenticate the same way share a run; an identical one joins it and gets the same report, one with other settings for the same connection is refused. `/api/report/export` takes `{report, format: json|text|digest|markdown|html, source?}` and returns `{format, content, mime, filename}`; `source` (`report.normalize_source`: label, host, port, user, `type` production or development; any other field, a password included, is dropped) is the connection the report was read from, added as the optional top-level `source` and named on a "Connection:" line in every human format.
  `server_checks.py` (`checks-1.0`, internal to the report; tag `/*myflames:checks*/`): replication from `SHOW REPLICA STATUS` (MySQL 8.0.22+), `SHOW SLAVE STATUS` (older), `SHOW ALL SLAVES STATUS` (MariaDB), read by column name from the client's `--xml` output (the connector's `--batch --silent` prints no column names), statuses from `REPLICA_LAG_S` (60 / 600 s, rules of thumb), threads not running and error numbers, plus `replica_read_only` on a replica (Group Replication's `group_replication_applier` / `_recovery` channels are skipped: a member is not an asynchronous replica); privilege text by version (MariaDB 10.11 also accepts SUPER, 11.0+ needs SLAVE MONITOR; MariaDB's `read_only` modes beyond ON/OFF exist from 12.0.1); accounts from `mysql.user` (MySQL) or `mysql.global_priv` then the `mysql.user` view (MariaDB), comparing `authentication_string` with `''` on the server so no hash is read, skipping locked accounts and roles (`no_password` problem only for plugins that accept an empty password: MySQL native, caching_sha2 and sha256; MariaDB native, old and an empty plugin name, never ed25519 or parsec; `anonymous` and `root_any_host` watch); the query cache on MariaDB (watch when on; hit share is `Qcache_hits / (Qcache_hits + Com_select)`); binary log expiry (watch when the binary log is on and nothing purges it: no expiry period, automatic purge off on MySQL, and no `max_binlog_total_size` / `binlog_space_limit` size limit). Every check needs only what it reads; without the privilege it is `unknown` with the grant to add.
  Additional native checks (same `report-1.0` finding fields and existing section keys):

  - `checks.memory_budget` (Memory): configured `innodb_buffer_pool_size`, `innodb_log_buffer_size` (twice on MariaDB for its two redo buffers; memory-mapped redo can bypass them), `key_buffer_size`, plus MariaDB query/Aria cache sizes and P_S's own measured internal memory (`SUM(CURRENT_NUMBER_OF_BYTES_USED)` where `EVENT_NAME LIKE 'memory/performance_schema/%'`; SELECT on that P_S table required). Using only P_S's own instruments avoids counting the buffer pool and session buffers twice. The per-connection scenario adds `sort_buffer_size`, `join_buffer_size`, `read_buffer_size`, `read_rnd_buffer_size`, `thread_stack`, both `binlog_cache_size` and `binlog_stmt_cache_size` only when `log_bin` is ON, and `net_buffer_length` twice (network buffer and result buffer) on both engines. It shows global + session × `max_connections` and global + session × `Max_used_connections`. MySQL TempTable adds `temptable_max_ram` once globally; `tmp_table_size`, `max_heap_table_size` and `temptable_max_mmap` are shown separately. MEMORY internal tables (including MariaDB) add one `min(tmp_table_size, max_heap_table_size)` table per connection. This is a demand scenario, not measured usage or a hard upper bound: multiple buffers/tables, growing network buffers, TempTable thread-local blocks and other allocations can exceed it. The two network buffers are allocated at connect and `thread_stack` at thread creation; other buffers are allocated on demand, some per table or per join. Both binlog caches are created together on the first binlog write. The scenario can overstate actual use when sessions do not need all buffers together; unreadable `log_bin` keeps the estimate partial. SQL collection never guesses physical RAM or reads the client's RAM; without it the finding is `unknown` and still shows the computed budget. Pure `compute_checks` accepts independently supplied `raw.physical_memory_bytes`: complete inputs yield watch when either scenario exceeds RAM, problem when global capacities alone exceed RAM, else ok with the caveat that other memory is excluded. Missing P_S privileges, counters or variables keep it unknown; known subtotals are labeled partial.
  - `checks.version_support` (Coverage): explicit per-series `VERSION_SUPPORT`, `VERSION_CURRENT`, `VERSION_LAST_REVIEWED` (2026-10-15), using the evaluation date. MySQL 8.0 maintenance ends 2026-04-21; 8.4 and 9.7 are LTS, 9.0–9.6 Innovation end at their successors. Retained end dates for MySQL 8.0/8.4/9.0–9.7 and MariaDB Community 10.6/10.11/11.0–11.2/11.4/11.8 come from vendor lifecycle/release pages cited beside `VERSION_SUPPORT`, not source files or tag timestamps. MySQL 26.7 and MariaDB 12.3/13.0 are explicit unknown-support entries. Their support periods have not been verified; displayed findings say so without assigning release maturity from another patch or development source. Short-term 11.3, 11.5–11.7 and 12.0–12.2 are explicitly listed with unknown end dates: stable successor tags exist through 12.2.2, but tag timestamps do not prove maintenance dates; 12.3.1 is only gamma. Current identifies the reviewed MySQL 9.7 LTS line, not the latest release; no current MariaDB LTS is nominated from the incomplete tags. Supported identifies another line with a reviewed future maintenance end date. End of maintenance is problem; the 180 days before it are watch (a planning rule of thumb); unlisted series and entries without a proved end date remain unknown through Full analysis aggregation; they are not WATCH findings. Dates exclude Oracle Sustaining Support and MariaDB Enterprise extensions; month-only dates use the first day after the listed final month. Verify vendor/fork policy and installed patches separately; static dates can become stale. Tests inject dates around every table boundary.
  - `checks.myisam_cache` / `checks.aria_cache` (Caches; Aria only on MariaDB): reuse Tables' existing `summary.engines` after the parallel views finish, with no extra inventory scan. Show configured cache size, block size, used/unused blocks, unused block bytes, physical reads, requests and miss percent (`reads / requests × 100`). Watch at 5% misses after 1,000 requests, both review heuristics, not server limits. `Key_blocks_used` and `Aria_pagecache_blocks_used` are high-water marks; neither is added to current unused blocks to infer occupancy. Missing settings/counters, no read activity, or skipped/failed inventory yield unknown with the reason. No visible engine tables skips efficiency assessment, scoped to selected schemas and account visibility; it makes no claim about the rest of the server. Aria can still serve internal disk temporary tables. Cache changes require workload measurements and the memory budget.
  - `checks.wsrep` (Replication): read-only when wsrep variables/status exist, otherwise omitted. Also omitted when `wsrep_provider` is empty/`none`, or `wsrep_on` is OFF while the provider is not connected, including ordinary MariaDB without Galera; these do not count as ok findings. Also unknown (never omitted) when `wsrep_on` is OFF with a provider loaded and the status is unreadable. When wsrep is active it also reads the membership variables (`cluster.GALERA_VARIABLES`), `wsrep_gcomm_uuid`, `wsrep_incoming_addresses`, `wsrep_local_index`, `wsrep_cluster_weight`, `mysql.wsrep_cluster_members` and, on Percona XtraDB Cluster, `pxc_cluster_view`, and judges with `cluster.galera_status`: the Cluster place's rules and texts, so both give the same status for the same server (state by `wsrep_local_state_comment`, not ready, non-Primary or joining, wsrep_on OFF while connected, fewer nodes than `wsrep_cluster_address` names when this node is identified there, no tolerance for one more failure (a singleton or two nodes, equal `pc.weight` assumed), flow control paused at least 10%, any certification failures, pxc_maint_mode, another PXC node not Synced). Its observation names the findings' titles (and "2 of 3 configured nodes in the Primary component"). Missing status (`cluster.galera_incomplete`, the same completeness rule as the Cluster place) or denied reads are unknown unless the variables explicitly establish that wsrep is disabled. Paused time covers the provider's reset period, not Health's sample window, and certification failures are cumulative (`cluster.CERT_FAILURES_CAVEAT`: the server asks the provider to reset its statistics on FLUSH GLOBAL STATUS, also FLUSH STATUS before MariaDB 11.5.1 and on MySQL; whether that counter resets is the provider's). No recovery or setting change is applied.

  MySQLTuner (GPL-3.0) is a checklist only: never copy its code, text or thresholds. The module docstring lists the families covered and not checked; `checklist.not_checked` carries the same list to every output. Tests: `test/test_report.py` (live recordings `test/fixtures/perf/report-mysql-8.4-replica.json`, `report-mysql-8.0.json`, `report-mysql-8.0-limited.json` (SELECT on one schema only), `report-mariadb-11.4.json` (Performance Schema and the query cache on), `report-mariadb-11.4-default.json`; `summary` retains the original findings and adds expectations for the new checks; `extension_provenance` describes replay additions and `superseded_calls` preserves replaced I/O reads), engine inventory in `test/test_schema_health.py` (`summary.engines`, additive to `schema-1.0`). `report-checks-*.json` are explicitly synthetic SQL replays for complete memory formulas, MySQL 8.0/8.4, MariaDB 11.4 with and without engine tables, and mocked wsrep. Extended live replays select new variable/status reads from existing recordings; uncaptured P_S memory returns NULL, and the limited-account fixture models its SELECT denial. These are labeled replay extensions, not new live measurements.

### Database Tuning Advisor

Workspace UI (`Tuning.tsx`, `tuning.css`, `tuningRules.ts`, `apps/workspace/test/tuning-browser.mjs`, `apps/workspace/test/tuning-rules.mjs`):
- A place of its own (`Place: "tuning"`, label "Tuning", palette keyword "mysql tuning"), never a Server view. `Server` renders it with `place="tuning"`: the TuningView full width (`.server-place.is-tuning-place`), no list of views or place toolbar (no Refresh all, ⌥⌘R off), the window footer (scope `tuning:<connectionKey>`), and the bound reports passed to TuningView (a report read on another connection is shown greyed with the other-connection note and its actions off, never as this one's; the Full analysis tab keeps its report that way). The Run full analysis button is disabled while Refresh all runs on the Server place (`runBlocked`), so the Full analysis never starts a worker pool beside the global view queue. A Full analysis link that reveals a row in a view of another place keeps that row's inspector open (`keepInspectorOnNavigation`, as Instruments links do). `Server.choose("tuning")` or `choose("report")` navigates to the place; the Full analysis is its second tab (a `report` link or the palette's Full analysis selects it). A session that saved `tuning` or `report` as the Server view opens Health there.
- Mounting and reads: `main.tsx` mounts `Server`, `ReplicationPlace` and `ClusterPlace` on their first visit (or at once for a restored skip loop) and keeps them mounted, hidden; `Server` keeps the place it showed last while another place is on screen. Each view gets `ViewOnScreenContext` (`viewOnScreen.ts`): true only for the chosen view of the place on screen (false for kept views that are hidden, and for the view under the list of views on phones). Automatic first reads go through `useOpenRead` (`viewOnScreen.ts`): a view reads when it comes on screen without a report of the connection in use, or while on screen with no report at all for a connection not tried yet; choosing another connection while it stays on screen keeps the old report greyed until Refresh; Refresh all and explicit Refresh or Load read regardless.
- Two tabs: **Scorecard** and **Full analysis** (the report, its inspector, Refresh and its HTML, Markdown, JSON and Text export through `/api/report/export`). Optional prop `tabRequest {tab: "scorecard" | "analysis", n}` opens a tab; each new `n` switches again.
- `tuningRules.evaluateTuning({report, health, config, tables})` is pure (`tuning-1.0` in memory and in the JSON export). It reads Python statuses and numbers by their keys and adds no thresholds of its own: Health metrics `bp_dirty`, `bp_wait_free`, `select_full_join`, `connections_used`, `aborted_clients` and diagnostics `buffer_pool`, `redo`, `tmp_tables`, `sorts`, `thread_cache`, `table_cache`, `table_definitions`; config-1.0 checks `redo`, `tmp_tables`, `slow_log`, `max_connections`, `table_open_cache`; the Tables report summary (or `tables.no_primary_key`, `tables.unique_stand_in`, `tables.engines` findings); `checks.no_password`, `checks.anonymous`, `checks.root_any_host` (count from the finding headline); `require_secure_transport` and `have_ssl` only from Parameters (`config.parameters.items`, every global variable). From a Full analysis it uses the report's `health_metrics` for the metrics findings leave out; without it (older reports) those checks are unknown. When a view's own report and the Full analysis both have an item, the newer `taken_at` wins.
- 19 checks in 5 categories (weights 30/25/20/15/10): Buffer pool and redo (buffer pool, dirty pages, waits for a free page, redo log size), Queries (joins without an index, temporary tables on disk, sort merge passes, slow query log), Connections and threads (connection limit now and peak, thread cache, connections dropped), Tables and caches (table open cache, table definition cache, tables without a primary key, MyISAM tables), Security (accounts without a password, anonymous accounts, root from any host, encrypted connections).
- States: problem, watch, ok, unknown. Python's `info` is unknown unless the check maps it (`slow_log` on with long_query_time >= 10 s and `tables.engines` without MyISAM are ok; a buffer pool with room is ok, warming is unknown). `applies: false` (thread cache under a thread pool) leaves the check out of the counts. Primary keys and MyISAM tables are unknown when no table was read in scope (the Tables summary, or the Full analysis engines finding "no tables"), never OK. Points: ok 100, watch 50, problem 0; a category is the mean of its measured checks (null when none), the overall score the weighted mean of measured categories. `provisional` when fewer than half the applicable checks or no check of some category are measured: no grade. Grades come from `GRADE_SCALE` (A 90, B 80, C 70, D 50, else F; `letterGrade`, `gradeRangeText`, `gradeScaleText`); any problem caps at `PROBLEM_GRADE_CAP` (C). `gradeReason(report)` is the line beside the grade ("Capped at C: 1 check has a problem (89 is in the B range)." or "Score 84 is in the B range, 80 to 89."; empty without a grade). `GradeLegend` in `Tuning.tsx` is the ⓘ beside the grade (a `Popover` named "How the grade works") that renders the scale from `GRADE_SCALE`, marks the current grade with `aria-current`, and states the cap and the provisional rule. The view ⓘ shows `SCORE_RULE` and `GRADE_RULE`; `SCORING_RULE` joins them for `report.scoring` and the exports. Labels: Problems found, Needs attention, Not enough data, Good.
- Remediation (`fix {text, sql?, optionFile?, caveat?}`, only on problem and watch): `globalChange` writes `SET PERSIST` on MySQL and `SET GLOBAL` plus an option-file line on MariaDB; per-session limits are `SET SESSION`. Version gates: MySQL before 8.0.30 has no innodb_redo_log_capacity (option file and restart); MariaDB before 10.9 has a read-only innodb_log_file_size; TempTable reads tmp_table_size as a per-table limit only from MySQL 8.0.28, so before it (and when only `TempTable_count_hit_max_ram` grew) the statement is `SET PERSIST temptable_max_ram`, shared by every TempTable table. The redo size is only Python's target (an hour at the measured rate, rounded up to 256 MiB) and only when it is larger than the capacity; a status raised by the checkpoint age or by waits for the log buffer (innodb_log_buffer_size) gets no size. "Clients were refused" appears only when Connection_errors_max_connections counted refusals; a problem at 90% use says so. No io_capacity advice from dirty pages, no wait_timeout or max_allowed_packet values, no OPTIMIZE from DATA_FREE (not scored), no invented buffer pool or connection targets (placeholders such as `<bytes>`). Statements are copied or opened in the Query editor; the page runs nothing.
- Binding: `tuningInputs` keeps only reports whose `key` is `connectionKey(connection)`; with none, the newest other connection's reports are shown greyed (`ViewLayout otherConnection`). Props `health`, `config`, `tables` take the bound reports (`HealthMemory`, `Bound<ConfigReport>`, `Bound<TablesReport>`); the older unbound `healthReport`, `configReport`, `tablesReport` are used only when the bound prop is absent. The statements behind Joins without an index come from the Full analysis attribution or, on request, `StatementsBehind` (`onOpenTop`); nothing is read on open.
- Exports: `tuningHtml` (every dynamic string through `escapeHtml`, the inspiration credit in the footer), `tuningMarkdown` (`escapeMarkdown`, statements fenced), JSON.
- Browser test: `npm run test:tuning-browser` (`apps/workspace/test/tuning-browser.mjs`: nothing measured, Health only provisional, the grade legend from the keyboard and the reason line (uncapped, then capped by a problem after a second Full analysis), inspector, Full analysis and Parameters measured, the Full analysis tab, 390 px; `PERF_SHOTS` saves light and dark screenshots at 1440, 720 and 390). Rule engine test: `npm run test:tuning-rules` (`apps/workspace/test/tuning-rules.mjs`, computes Health, config and report-1.0 inputs from the Python replays for MySQL 8.4 and 9.7 and MariaDB 11.4, with MariaDB 10.6 and 11.8 by version).

### Server lifecycle: upgrade-check, dump, restore, clone

Backend `myflames/lifecycle.py` (contract and evidence: the server-lifecycle skill), CLI `myflames/lifecycle_cli.py`, endpoints in `ui.py` (`_lifecycle_*`). Tests: `test/test_lifecycle.py` (fake tool executables that record argv, env and option-file mode, strict replays of live recordings, a "regressions found on live servers" section) and `test/test_lifecycle_cli.py` (CLI exit codes, confirmations, audit, endpoints); fixtures and what was and was not verified live: `test/fixtures/lifecycle/PROVENANCE.txt`.

- `python3 -m myflames upgrade-check --target-version X.Y.Z [--no-shell] [--config-path PATH] [--fail-on error|warning] [--json] [-o PATH] [-q]` (`upgrade-check-1.0`): `lifecycle.upgrade_check`. MySQL Shell (`mysqlsh --defaults-file=<0600> --js -- util check-for-server-upgrade --target-version=... --output-format=JSON`) when installed and the pair is in range (server older than the Shell, target at most the Shell's major.minor; otherwise exit 2 with a `--no-shell` hint); `parse_upgrade_json` reads `errorCount`/`warningCount`/`noticeCount` and `checksPerformed` (the Shell exits 0 with errors). `--no-shell` or no Shell: `_native_upgrade` (`partial: true`, `limitations[]`), every check version gated as source < change <= target: `removed_variables` from `performance_schema.variables_info` excluding COMPILED, DYNAMIC and PERSISTED (error: an unknown option in an option file or on the command line stops mysqld; `my_getopt.cc`), `removed_persisted_variables` (warning: logged and ignored at startup; `persisted_variable.cc`), on 5.7 the running values (warning); `reserved_identifiers` over schema, table, view, column, routine, trigger and event names with `reserved_words(source, target, source_reserved)` from `RESERVED_SINCE` minus the source's own `information_schema.KEYWORDS` (parity test against KEYWORDS read live on 8.0.46, 8.4.11, 9.7.2); `partition_engines` (5.7 sources only, joins TABLES for ENGINE); `utf8mb3` and `zero_date_defaults` in user schemas (notice from 8.0 sources, warning from 5.7); `native_password_users` (`'user'@'host'`, warning for 8.4, error for 9.x). MariaDB: `method: mariadb_upgrade_guidance`, never invokes the Shell; a target below 10 (a MySQL number) or older than the server is refused. Dump with MySQL Shell refuses when the Shell's major is older than the server's and warns for an older minor (`shell_version_gap`, mysql-shell `dumper.cc:5311-5334`); failed tool output is reduced to one line by `tool_error_line` with colour codes stripped. Counts are affected objects per severity; `--fail-on` exits 1. Read-only: endpoint `POST /api/lifecycle/upgrade-check` (`connection`, `target_version`, `prefer_shell`). HTTP `config_path` is rejected before connecting, also on `/api/lifecycle/prepare`; only the CLI accepts `--config-path`.
- Tools: `detect_tools(server=...)` returns `lifecycle-tools-2.0`, including `default_dump_parent` from `lifecycle.default_dump_parent()` (the real user home from `ssh_tunnel.ssh_home()`, `Documents/myflames dumps` when Documents exists, otherwise `myflames dumps` under home; Windows uses USERPROFILE). Computing the path creates nothing. This field is present with or without a connection, including missing-client replies. Detection uses the shared [client selection and trust rules](#local-ui) after reading the server. `tools[name]` holds the selected copy for each tool, `copies[]` lists every discovered copy with `name`, path, resolved path, version banner/tuple, engine, availability/error, installation, origin/label, reason, warning and `used`; `mysqlpump` has null version/tuple and is never used. A copy is marked `used` by its path whenever any selected client, dump, load, backup or shell role uses it, including an other-engine query client with no load role; its reason and warning come from that role (client takes precedence for a shared path). With server facts, `used` has the same public role/missing/notes shape as `client-tools-1.0`. Without facts, `tools` keeps the first copy per name in discovery order and omits `used`. A stale engine choice leaves its roles unavailable instead of selecting another installation. `offered_tools(server, tools)` determines operations. `POST /api/lifecycle/tools` with a connection reads the server before detecting for it, then adds `server` and `offered`; without a connection it returns inventory only. A typed client failure retains inventory and download links with `server: {}`, `offered: {}` and `server_error: {code: "client-missing", error}`; this route currently uses `client-missing` even for `ClientAuthPluginError`, unlike `/api/client/tools`. Option files (`_credentials`): `[client]`, quoted values or engine-specific TLS switches, 0600 in a 0700 temporary directory, first on argv, removed in `finally`; environment allowlisted (`_clean_env`, no `MYSQL_PWD`). TLS uses the actual tool engine's mapping described above. SQL and Shell scripts go on stdin. Tests: `test/test_ui.py` (`TestLifecycleToolsAPI`), `test/test_lifecycle.py` (`LifecycleTests`, `MissingClientToolsTests`, `ClientToolTrustTests`).
- Tool release metadata: `tool_updates.enrich_tools`, invoked by `detect_tools(check_updates=True)` only after explicit UI detection/refresh. Each tool adds `download_url` and `update {status, latest_version, checked_at, source_url, message}`. Status is `not_checked`, `available`, `current`, `unknown`, `not_installed` or `deprecated`; current means no newer verified release in the checked series, not a compatibility or support guarantee. MySQL GA download headings and the MariaDB stable-series REST API supply versions. Fixed HTTPS endpoints only, no redirects or ambient proxies, 1 MiB response cap, 5 s socket timeout, 12 s caller budget, at most 8 in-flight workers and 64 cache keys; success TTL 1 hour, failure TTL 1 minute. Unsupported distributions/prereleases, mismatched series, malformed/future metadata and failed checks stay unknown. CLI discovery and lifecycle preflights do not request network checks.
- `POST /api/lifecycle/choose-directory` accepts an empty object through the normal authenticated API. It opens the server host's folder dialog, with a 120 s deadline and one dialog at a time; no client-supplied script or path is executed. Cancel preserves the form path. The selected directory still undergoes normal preflight and empty-directory validation. Native folder selection accepts no command or path input and does not start a backup.
- Dump folder facts: authenticated `POST /api/lifecycle/dump-info {directory}` accepts only `directory` and needs no connection or saved password. It is registered beside `choose-directory`, with the same authentication, CSRF header and request limits. It returns `{schema_version: "lifecycle-dump-info-1.0", tool: {name, version}, engine, scope: {schemas, tables}, structure_only, remap_allowed: true | false | null, remap_reason}`; `tool.version` is capped at 64 characters. Tool names are `mysqldump`, `mariadb-dump` or `mysqlsh`; engines are `mysql` or `mariadb`. Old manifests without `options.structure_only` report false. `dump_info` and `prepare_restore` share `_restore_input` (absolute/expanded real directory path, completed manifest validation and safe relative file records) and `_restore_sql_scan` with `_restore_remap`. A folder pick stays cheap: `dump_info` reads the manifest without checksumming every file (`verify=False`). For single-schema picked-table SQL dumps, it checks the opened `dump.sql` descriptor is a regular file and its size matches the manifest before reading; scans at most `DUMP_INFO_SCAN_BYTES` (16 MiB) and checks SHA-256. When the manifest record exceeds that cap, it skips the scan and returns `remap_allowed: null` with `remap_reason: "This dump is large; whether it can load into another database is decided when you review the restore."` A size mismatch refuses before any SQL read. Manifests, scanned SQL and preflight files are opened without following links or blocking on FIFOs, and must be regular files before reading. `prepare_restore` verifies every file, including its descriptor size before reading, and the restore file, directory and byte budgets before any restore; it stays the authority and retains its own scan limits. Files named outside the folder are refused by both. Whole-database SQL dumps report that they create their own databases; single-schema picked-table SQL dumps within the folder-pick scan budget scan `dump.sql` for CREATE DATABASE, CREATE SCHEMA, USE and qualified references; single-schema Shell table dumps allow remapping. Missing or unreadable folders/manifests, missing or unreadable scanned SQL, special files and invalid metadata raise `lifecycle.Refused`, using HTTP 400 `{error}` with an actionable sentence. Extra payload keys raise `ValueError` through the same HTTP 400 envelope. No tool, server connection or restore is started. These facts let Restore decide whether to offer a new database when the folder is picked. When reported, remap eligibility is independent of the proposed target name: a qualified reference to another database still refuses even if it spells that proposed name, so scanned folder facts and preflight cannot disagree. Restore preflight still reloads the folder and refuses unsupported remapping; jobs retain fresh validation and reviewed-write checks. Tests: `test/test_lifecycle.py` (folder facts/preflight agreement, SQL and Shell scopes, invalid metadata, traversal, symlinks, FIFO and directory refusal, missing SQL, checksum tampering, scan cap and size checks before reads), `test/test_lifecycle_cli.py` (route registration), `test/test_ui_http.py` (HTTP refusal, payload keys and authentication).
- Upgrade targets (`myflames/upgrade_targets.py`, `POST /api/lifecycle/upgrade-targets {connection}`, `upgrade-targets-1.0`): read when Maintenance > Upgrade check opens, once per connection, beside the view's other reads (never blocking Check upgrade). It reads `basedir`, `plugin_dir`, `character_sets_dir`, `lc_messages_dir`, `socket`, `version`, `version_comment`, `version_compile_os`, `version_compile_machine`, `build_id` and `hostname` (one `SHOW GLOBAL VARIABLES`), then the vendor's releases. MySQL Community Server: the release notes series the documentation index links (`https://dev.mysql.com/doc/`, then `https://dev.mysql.com/doc/relnotes/mysql/<series>/en/`; on 2026-11-08 the 8.0, 8.4, 9.7 and 26.10 pages, the 9.7 page also listing 9.0 to 9.6 and the 26.10 page 26.7), every "Changes in MySQL X.Y.Z (YYYY-MM-DD)" entry without a pre-GA label (Release Candidate, Development Milestone and similar) or a future date; then each offered target's own release page (`news-X-Y-Z.html`, at most 32 per report, cached per page) is read for Oracle's markers, and a release "no longer available for download" (8.0.38, 8.4.1, 9.0.0) or published as "a Critical Security Patch Update for the MySQL Server docker image, only" (8.4.12, 9.7.3) moves to `unavailable` with its reason, except that a Docker-only release stays (`docker_only: true`) when `install.container` is set; a page that cannot be read, or that has no "Changes in MySQL" heading, leaves its release listed and adds a note; a series page that answers but lists no release makes the read partial (cached for a minute); offline (or a server built without `release_reader`) it falls back to `sysvar_history_mysql.VERSIONS` with `source.kind` `bundled`. Percona Server: `repo.percona.com` package listings of the lines the root lists (`ps-80`, `ps-84-lts`, `ps-97-lts`, `ps-8x-innovation`): the Yum `RPMS/<arch>/` folders of the newest four el releases for an RPM install (a folder missing for this architecture answers 404 and counts as no packages, not an error), else the APT pool (`apt/pool/main/p/percona-server/`); each target carries `repository` and `packages` (codenames or `el8`, `el9`, `el10`), and Percona has no bundled list. Percona XtraDB Cluster and MariaDB get no list (`source.kind` `none`). Network: fixed HTTPS URL patterns (`_ALLOWED`), no redirects or ambient proxy, 2 MiB per response, 5 s per socket read and 10 s per whole response (`READ_TIMEOUT`), 15 s per report for every fetch including the first (pending fetches cancelled at the deadline), 4 worker threads, cache keyed by reader (success 1 hour; a failure or a partly read result 1 minute; at most 256 entries); only `cmd_ui` passes `release_reader=upgrade_targets.fetch_page`, and `upgrade_targets()` without a reader uses `offline`, so test servers never read the network. Server variables lose control and format characters (bidirectional overrides) before they become evidence text. Paths follow the MySQL 9.7 manual, "Upgrade Paths" (Table 3.1): patches in the series (`patch`), the next LTS or bugfix series (`next_lts`; from an Innovation series `lts_after_innovation`), Innovation releases before the next LTS (`innovation`), never skipping an LTS or bugfix series; series kinds come from the bundled catalog's maturity (5.7 and 8.0 bugfix, 8.4 and 9.7 LTS, 8.1-8.3, 9.0-9.6 and 26.x Innovation), and once the next LTS has a release the Innovation series before it are `not_direct`: still a supported path in Table 3.1, but superseded (an Innovation release is supported only until the next release); the step to a bugfix series (5.7 to 8.0) is labeled "next bugfix series". Releases of the server's own series are always `patch` (or `innovation`), even when the catalog does not classify the series. A series the catalog does not know is `unclassified`, never placed on a path. `recommended` is the newest next-LTS release, else the newest patch, else none (an Innovation release is never chosen by default). Each target has `check_version` (Percona's upstream version), `path_label`, `date`, `url` (release notes page) and `shell {ok, needs}` against the detected MySQL Shell (source older than the Shell, target major.minor at most the Shell's). `install {method, label, confidence, evidence[], package, build_id, container}`: `rpm` (`/usr` with `/usr/lib64/mysql/plugin`, INSTALL_LAYOUT=RPM, also the official Docker images), `distro_rpm` (that layout with `version_comment` "Source distribution", what a distribution build such as Fedora's reports), `deb` (`/usr/lib/mysql/plugin`; medium on 32-bit, where RPM uses it too), `ubuntu_deb` (an Ubuntu suffix in `version` or `version_comment` "(Ubuntu)"), `tarball` (every folder under `basedir`; `package` from an archive-named basedir), `windows_msi`, `windows_zip`, `homebrew`, `macos`, `unknown`; `container` when the host name is 12 hex digits and the layout is RPM (the official MySQL and Percona images are RPM builds). `build_id` is evidence only (no public index maps build IDs to packages). `guidance {text, command, url}` names the MySQL Yum or APT repository guide, Ubuntu's package, the generic archive download, or for Percona packages `sudo percona-release enable-only {repository} release` (the client fills in the selected target's line after checking it matches `ps-NN`, `ps-NN-lts` or `ps-Nx-innovation`; `percona-release setup ps-97-lts` enables nothing, per Percona's 9.7 install pages), with "run sudo dnf -y module disable mysql first" on RHEL 8 for RPM installs. UI: the Target version list shows `version · path_label · date` newest first, chooses `recommended`, and `UpgradeTargetNotes` shows the install method, source and read time, the guidance with its command, the chosen target's path, packages, Shell requirement and release notes link (`upgradeLinkUrl`: HTTPS on dev.mysql.com or docs.percona.com only), and **How this was decided** (evidence, `not_direct`, `unclassified`). Tests: `test/test_upgrade_targets.py` over `test/fixtures/upgrade_targets/` (vendor pages captured 2026-11-08, trimmed to the parsed lines; `layout-*.json` read live on 2026-11-08 from the official mysql:8.0.44 and mysql:9.7.2 images and percona/percona-server:8.0 (8.0.46-37), Ubuntu 24.04's mysql-server-8.0 and the generic mysql-8.4.13-linux-glibc2.28-aarch64 archive; the endpoint answered 200 on MySQL 8.0.44, 9.7.2 and Percona 8.0.46-37), browser `apps/workspace/test/maintenance.mjs` with `lifecycle-upgrade-targets-mysql-8.4.json`.
- Logical dump options: `dump_options.capability_catalog` exposes tool/scope-specific descriptors, each with `group` (one of `groups`: performance, contents, output, compression, the sections of the Dump form) and, for a boolean, `row` (the label its checkbox shares: Chunking, Include, Write, Connection); `encryption` has a one-line `summary` and the full `message`. `normalize` rejects unknown names and invalid values. Shell supports compression default/none/gzip/zstd, levels from Shell 8.4 (gzip 0..9, zstd 0..22), chunking, decimal byte sizes (minimum 128k per chunk) and max rate per thread. SQL tools expose hex blobs, add-drop-table and network compression. Triggers/routines/events follow scope, with no routines/events toggle for Shell table dumps. Local file encryption is unsupported; TLS and connection compression affect transport only. Selected options bind the plan, execution and manifest; CLI flags are unchanged.
- Plans (`lifecycle-plan-1.0`: `operation, request, server, connection_key, tool, target, commands[], checks[], warnings[], ready, binding`) come from `prepare_dump`, `prepare_restore`, `prepare_clone`; a table dump adds the check `tables`, failed (with `missing`) when a named table or view is not in the schema's catalog read (names match exactly, or without case only when `SELECT @@lower_case_table_names` reads 1 or 2, so a missing `orders` is never taken for an existing `Orders` on a case-sensitive server); endpoint `POST /api/lifecycle/prepare` with `operation` dump|restore|clone (reads only; the Shell's `dryRun` runs here). Dump dry runs use a private temporary destination, and restore dry runs use a private temporary progress file, both removed afterwards; they cannot create the requested output directory or modify the saved load progress before confirmation. Each plan's `tool` retains `name`, `path`, `realpath`, `version`, `version_tuple`, `engine`, `installation`, `origin`, `origin_label`, `reason`, `warning`, `available` and `deprecated` when present, without release-update metadata. Dump, restore, clone, upgrade check and physical backup read the server before selecting tools; job rebuilds pass those fresh server facts through detection. SQL restore and CLONE use the load client from the selected dump installation. An automatic older-than-server dump/load warning is also in `plan.warnings`. A plan is never authority: `_write_job` rebuilds it on a fresh connection and first compares the entire tool record. A mismatch refuses with `The tool for this server changed since the review (Settings > Client tools or an installed program changed). Review it again; nothing was run.` Binding, target and commands must then match, every check must pass, the server identity must be unchanged and the typed target must equal `plan.target`. Lifecycle rechecks trust and the pinned target immediately before executing the resolved program. Tests: `test/test_lifecycle.py` (`LifecycleTests`: per-server copies/choice, tool-change refusal, load installation and older warnings; `ClientToolTrustTests`: launch rechecks).
- Output directories (`_output_path`, `_empty_directory`): parent links resolved once and bound in the plan (macOS `/tmp`, `/var`), the directory itself must not be a link, a parent swapped for a link after the preflight refuses; absent or empty only. A missing output parent still refuses with `Output parent directory must exist.`, except exactly the backend default dump parent (absolute, realpath-normalized comparison). For that exception, its own parent (Documents or home) must already exist and must not be a symbolic link. Preflight creates nothing, reads free space from that existing parent, and adds an `output_parent` check saying `Creates the folder <path>.`; the creation intent participates in the plan binding, so repeating preflight before Start is stable while a changed intent requires a fresh review. Only the reviewed dump Start creates that one folder with `os.mkdir(..., 0o700)`, then applies the usual output checks. It never creates arbitrary intermediate folders. Dump writes `myflames-manifest.json` (`lifecycle-manifest-1.0`: files with SHA-256, exact `COUNT(*)` of up to 3 tables under 1 MiB, the object catalog, `schema_defaults` keyed by schema with `character_set` and `collation` (including selected empty databases), `complete`); `read_manifest` re-hashes every file before a restore, capped by `MAX_RESTORE_FILES` (10,000), `MAX_RESTORE_BYTES` (10 GiB), and `MAX_RESTORE_DIRECTORIES` (10,000). Exceeding a limit refuses preflight with the limit named. Bytes are counted while reading as well as checked against file size, so growth during hashing also refuses. Successful logical dumps hash files once and reuse those records for verification and the manifest. Interrupted dumps skip further hashing and retain an incomplete manifest. Confirmed dump manifest creation is not capped by these restore preflight limits; larger backups require another restore tool.
- Restore: API and direct restore callers reject a mismatched typed target synchronously, before creating or registering a job or opening its connection; the API checks before saved-password lookup. The CLI checks before job creation. A mismatch says `Nothing was restored: the typed target does not match <target>.`, is audited as unsent, and never carries a partial-restore warning. Policy checks retain precedence, and all server identity and preflight rechecks still run for a matching target. Shell dumps through `util.loadDump` (`local_infile=ON` is a failed check, never set; progress file beside the dump; `--resume`; `--existing ignore|drop`), SQL dumps through the client with `--binary-mode --comments` reading a staged copy whose hash is checked again. Picked-tables SQL restores inspect the checksum-verified `dump.sql` using `_sql_schema_scan`: skip string data and ordinary comments, inspect executable MySQL/MariaDB version comments, and recognize CREATE DATABASE, CREATE SCHEMA, statement-start USE and source-qualified references. This recognizes generated dump constructs, not arbitrary SQL semantics, and never rewrites the dump. The plan adds `sql_scan`, `remap_allowed` and `remap_reason`. A single-schema picked-tables SQL dump can use another default database only when none of those constructs occurs. Refusals are `dump.sql contains <CREATE DATABASE, CREATE SCHEMA, USE>, so it cannot load into another database.` or `dump.sql names <source> in qualified references (such as view definitions), so it cannot load into another database.` A whole-database SQL dump refuses remapping with `This dump creates its own databases (CREATE DATABASE and USE), so it restores into them; choose As in the dump.` Shell remapping remains limited to single-schema table dumps; otherwise `Shell schema remapping is offered only for single-schema table dumps.`
  When a SQL table restore has a selected target database that does not exist and the dump supplies no CREATE DATABASE/CREATE SCHEMA, `create_database` contains the exact reviewed CREATE DATABASE command before the load command. It uses the source's manifest `schema_defaults`; older manifests without both defaults warn that server defaults will be used. Existing databases need no extra CREATE. The plan binding includes the manifest hash, creation SQL and scan result. Execution creates the database under the same consumed review, policy and audit before starting the client with `--database`; it never creates during preflight or Shell load preparation. Before the load's connection-id marker, failure/cancellation guidance is `Nothing was loaded: the client stopped before running any statement.` After that marker it changes to the SQL or Shell partial-load guidance; database creation is a separate step and is not itself proof of loaded data. Verification: object presence and the recorded row counts (`passed`, `failed`, `not_checked`; a successful child process is never evidence). Tests: `test/test_lifecycle.py` (`LifecycleTests`: missing/existing database, charset quoting/defaults, review failure, remap content and version comments, typed target, SQL/Shell marker guidance and remap endpoint fields).
- CLONE (MySQL only): `clone_preflight` (engine, version series, plugin on both, BACKUP_ADMIN donor, CLONE_ADMIN recipient or BACKUP_ADMIN with DATA DIRECTORY, platform and character set variables, collations, plugins, `max_allowed_packet`, `clone_valid_donor_list` containing the donor address, different servers; DATA DIRECTORY absolute and not under `@@datadir` as text, existence `not_checked` without a recipient-host probe because the server refuses an existing or in-datadir path before copying). `donor_address` (`--donor-address`) is the donor as the recipient reaches it, used in the statement and the donor-list check. The statement goes on the client's stdin (`clone_statement(..., redact=False)`), shown and audited redacted. Clone plans add `expect` with `donor` and `recipient` objects (`endpoint`, `connection_key`, `type`, `read_only`), `donor_address` and `require_ssl`, all included in the binding. The workspace evaluates both saved profiles with `dbops.connection_safety`; a read-only donor is allowed. Before sending a remembered donor password, it verifies negotiated TLS on the recipient connection. Start checks the reviewed pair before retrieving the saved donor password, and rejects TLS downgrades when the donor profile requires TLS. Both passwords are scrubbed from errors, audit records, logs and manifest free text. Progress: a second connection polls `performance_schema.clone_progress` (job `progress` percent and `detail`). In place: ERROR 3707 (no supervisor, the server shuts down) or a lost connection is expected; the job waits up to `restart_wait` (`--restart-wait`, default `RESTART_WAIT` 180 s) and then reads `clone_status`, tied to this clone by `BEGIN_TIME`, `SOURCE` and `DESTINATION` (the table keeps the previous clone's row); result `needs_restart` when it does not come back, `needs_verification` when the recipient account is refused (it now has the donor's accounts). GTID: donor `gtid_executed` at preflight against `clone_status.GTID_EXECUTED` (empty for DATA DIRECTORY clones, `not_checked`).
- Jobs (`LifecycleJob`, `lifecycle-job-1.0`: `id, operation, state queued|running|done|failed|refused|cancelled, progress, detail, events[] (last 200), phase, elapsed_s, output, result {status, verification[], partial_state, cancel_error, audit_id}, error`): `progress` is a percent only when the tool printed one (MySQL Shell's `showProgress`); `phase` names the step in words (`LifecycleJob.step`, also a `status` event, never with a percent sign): the repeated preflight, reading the table list, counting rows, dumping with the tool, checksumming, counting again, writing the manifest, and for restore copying to staging, loading and verifying. `elapsed_s` counts from the start of the job and stops when it ends. `output` is set for SQL dumps only (`mysqldump`, `mariadb-dump`, which print no progress): `written_bytes` is the size of `dump.sql`, sampled from its file descriptor every 0.5 s while the tool runs and once after it ends, and `estimated_bytes` the sum of `DATA_LENGTH` of the dumped tables (`BASE TABLE`, and MariaDB's `SYSTEM VERSIONED`) (None when unknown or structure only). On MySQL 8.0.3 and later the sizes are read from the storage engine, not the information_schema cache, in one bounded call (`SET SESSION information_schema_stats_expiry=0` first: information_schema caches table sizes for a day by default and the variable is not settable with `SET_VAR`); on MariaDB, MySQL 5.7 or when that read fails, the catalog's sizes stand. The estimate only feeds a progress bar; the written size can end above or below it. tool output drained on threads, CR progress parsed, ANSI colors stripped, secrets redacted. Cancel: SIGTERM then SIGKILL to the process group (EPERM from a zombie group tolerated), and `KILL QUERY` on the session the job printed on its own stdout (marker line; mysqlsh's `print` needs an explicit line break). `partial_state` names what may remain. `LifecycleJobs` (the workspace's registry: two running at most, never two on one target, finished ones kept an hour, closed with the server). Shutdown: `LifecycleJobs.shutdown()` runs first in `WorkspaceServer.server_close`, before replication loops, table data, live jobs and editor sessions (whose cleanup can block on `KILL QUERY`). It refuses new jobs and preflight runners, cancels every running job and runner, starts `KILL QUERY` on threads with the session id read before the tool stops, sends SIGTERM to every tool process group and SIGKILL after 1 s, then gives jobs 1.5 s to write their audit record (cancelled, outcome unknown, as for Cancel); `close()` finishes the rest. `ToolGroups` lists a tool's process group from just after it starts until the group is gone. Tools start through `ToolGroups.start`, serialized with shutdown (`close_spawning`): a tool being started is listed before shutdown's snapshot, and none starts afterwards. With `MYFLAMES_TOOL_GROUPS_FILE` (set only by the desktop app, in a 0700 directory it owns; a missing or shared directory refuses startup with exit 2) the list is also kept in an owner-only `tool-groups-1.0` file (`{"schema_version", "groups"}`), written before the launch line, replaced atomically and kept (empty) after a clean close. When it cannot be rewritten it is removed, so it never names a group that may have ended, and the desktop treats a missing file after launch as unconfirmed; a tool whose group cannot be recorded is stopped at once and reported as cancelled. Shell dryRun and upgrade check tools run under `LifecycleJobs.runner()`, so shutdown stops them too.
- Writes: dump, restore and clone are `risky` for `dbops.decide` (a read-only connection refuses, Production and Development confirm). CLI: policy from `--profile` before any prompt or connection; plan and commands on stderr; dump asks `[y/N]`, restore and clone ask to type the target; non-TTY needs `--yes` (plus `--confirm-target TARGET` for restore and clone); `--preflight` prints the plan (exit 0 ready, 1 not); Ctrl+C cancels the job. Exit 0 completed, 1 a started job failed, was cancelled or was not verified, 2 an unsent refusal, decline, bad input or connection failure. Failed-job text names the reason once in the operation heading, omits duplicate or empty `not_checked operation` rows and repeated status words, and retains distinct verification and partial-state guidance; JSON evidence is unchanged. UI: `POST /api/lifecycle/start` (`operation`, `plan`, `typed_target`, `confirmed: true`, `connection`, and `donor` for clone) is in `ui._WRITE_PATHS`: `dbops.gate` first (refused envelope with `audit_id`), then saved passwords (`connection.profile_id`, `donor.profile_id`), then the job; `POST /api/lifecycle/status` and `/api/lifecycle/cancel` take `id` and `connection`. Every attempt has an `AuditTrail` record (`surface` cli or ui; refused, declined, `unknown` after a cancel, `ok` or `error`). `POST /api/lifecycle/jobs` (`connection`; `lifecycle-jobs-1.0`, `_lifecycle_jobs_response`) lists the jobs this workspace holds for the connection's endpoint (`dbops.identity_endpoint`, recorded by `_lifecycle_add` before the job starts and dropped when `LifecycleJobs` lets the job go); it opens no connection and starts nothing (`test/test_lifecycle_cli.py`). Status and cancel check this same owner before looking up or cancelling a job and return “No such job.” for missing, expired or mismatched jobs. They open no connection and read no saved password. A change to type or read-only preserves access to the original job.
- Workspace UI (`Maintenance.tsx`, Server > Setup): Tools, Upgrade check (MySQL Shell, or the labeled partial check), Dump, Restore, Clone. Tools are detected on open through `useOpenRead` (`lifecycle/tools` without `check_updates`; successful version probes use the shared identity-keyed cache without a time limit); the view's Refresh is labeled **Detect again** and shows only on **Tools**; **Check for updates** sends `check_updates: true`. **Used for this server** shows the role selections with program, version, origin and reason; **Change…** opens the same dialog as **Settings > Client tools…**. The table lists every copy with **From**, **Path** and **Used**, and selects rows by path so copies of one program keep distinct inspectors. The review uses the same `toolUseLine` as the dialog; the Dump tool choice names the selected program, version and origin, or the Shell version for Automatic. These are separate from the dialog's **Detect again**, which sends `refresh: true` to `/api/client/tools`. Start dump…, Start restore… and Start clone… run the preflight (it only reads) and open the confirmation only when `ready` (target, exact commands, warnings, checks; restore and clone type the target; stale after `PREFLIGHT_TTL_MS`, 60 s). Failed checks show under the button with a Next step (Choose another folder…, Review fields, Remove <table> from the selection); passed ones fold into a details line. A preflight error clears the old plan; after a field changes, the old checks are labeled as the last preflight's and the planned commands are hidden. Dump's Output folder is suggested (`OpsShared.suggestDumpFolder`: `dumpFolderStem` of the database or connection plus the local date, in the backend reply's `default_dump_parent`, or the last dump's parent when this window already started a dump, `-2`, `-3` past names already listed or reserved this session; never over a typed path); Browse… picks the parent. The dump confirmation is primary-toned (policy, review token and audit unchanged), names its Contents and shows non-routine warnings first. A restore plan with `required_acknowledgement` needs its own checkbox; the workspace sends that exact string as `typed_acknowledgement` only when checked, and `lifecycle.restore_job` compares it with the fresh plan's; Cancel and Escape call `audit/declined`. Frontend callers must send the original `connection` on every status and cancel request, and show both `plan.expect.donor.endpoint` and `plan.expect.recipient.endpoint` with their policy when confirming clone. Jobs are polled while they run and bound to their connection (greyed after a switch). On open, and after a Start whose reply failed, the view reads `lifecycle/jobs` and adopts what it finds, so a reload or a lost reply never sends Start twice. A job card (`JobProgress`) shows a bar from `lifecycleProgress`: determinate on the tool's `progress`, else an estimated "≈N%" from `output.written_bytes` over `output.estimated_bytes` (capped at 99, with a title saying it is estimated from table sizes), else indeterminate (also once the written size passes the estimate: "X written, more than the Y estimate"). Under it one facts line: the `phase` while it runs, the bytes written (of about the estimate), a rate from two snapshots on the job's own clock (`elapsed_s`), and "Elapsed m:ss" ("Completed in m:ss" when done). The last log line shows only when it differs from the phase. While a job runs on the connection, the actions row shows `JobSummary` beside the disabled Start button ("Dump running · ≈46% · 120.0 MiB", Show progress); after Start (and after a lost reply is found in the job list) the view switches to the job's task and scrolls to and focuses its card (no smooth scroll under reduced motion). Every job stays mounted across task switches so completion is noticed. A completed dump (`state` done, `result.status` completed) has Open in Finder / Open in File Explorer / Open folder, which reveals `<result.directory>/myflames-manifest.json` through `files/local/reveal` (the dump folder opens with its manifest selected; nothing is run); a dump this mounted view started opens it once by itself (`autoRevealDump`: claimed before the request, never retried by rendering, never for adopted, failed or cancelled jobs, restore or clone, and never on Windows, where File Explorer's `/select,` argument splits a path at commas: security open lead 17). The session keeps only `slow.maintenance_section`; the donor password stays in the page. Browser suite `apps/workspace/test/maintenance.mjs`.
- Not wired to the CLI or UI yet: `prepare_physical_backup` / `physical_backup_job` (MariaDB `mariadb-backup --backup` then `--prepare`, only with a matching local socket and an explicit same-host confirmation); `clone` on MariaDB prints the `mariadb-backup` steps instead.

### Replication: status, skip, skip-loop

Topology presentation shares Depth and Pause motion controls with dimensional server glyphs and selectable nodes. Motion illustrates reported running links or healthy group membership, never measured traffic. Failed, stopped, unknown and stale links remain still, and reduced-motion preferences disable animation.

Overview cards derive health from judged channel statuses. Errors and stopped threads are problems; unjudged Group Replication channels never imply an OK or running state. The GTID mode comes from `gtid.mode`, with `gtid.gtid_mode` accepted for older payloads and missing values shown as unavailable. Maximum applier lag is the maximum of measured judged channels, with measured, unknown and unjudged channel counts; zero does not establish synchronization. Receiver, heartbeat and worker lag caveats remain visible in the lag view and inspector.

Backend `myflames/replication.py` (contract and evidence: the replication skill and the module docstring), CLI `cmd_replication` in `myflames/workspace_cli.py`, endpoints in `ui.py` (`_replication_*`). Tests: `test/test_replication.py` with live replays `test/fixtures/perf/replication-*.json` (MySQL 8.4.11 with gtid_mode ON, four workers and a second channel `ch80`; MySQL 8.4.11 file and position; MySQL 8.0.46; MariaDB 11.4.13 with a named connection `src1` and the default one; recorded 2026-10-15). Each `*-error` fixture is the status after a duplicate key conflict and the matching `*-skip` is skip_one's re-read, script and read after; `mysql-8.0.21-slave-*` is the 8.0.46 replica answering `SHOW SLAVE STATUS` with the version answer rewritten to 8.0.21 (the naming before 8.0.22 cannot be produced by a current 8.0).

- `python3 -m myflames replication [--channel NAME] [--json] [-o PATH] [-q]` (`replication-1.0`, `read_status`): reads only, tag `/*myflames:replication*/`. `SHOW REPLICA STATUS` (MySQL 8.0.22+), `SHOW SLAVE STATUS` (older), `SHOW ALL SLAVES STATUS` (MariaDB), read by column name from the client's `--xml` output (`perf_common.Reader.named`, shared with `server_checks`). MySQL with Performance Schema on adds `replication_connection_configuration` (heartbeat interval), `replication_applier_configuration` (`ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS_TYPE`, 8.0.23+), `replication_connection_status` (heartbeats and the last queued transaction) and `replication_applier_status_by_worker` (worker errors, applying and last applied transactions; zero timestamps become null). For a channel whose applier stopped on an error, `SHOW RELAYLOG EVENTS ... FROM <Relay_Log_Pos>` (MySQL needs REPLICATION SLAVE, MariaDB SLAVE MONITOR) shows the transaction at the applier's position (`channel.relay`: gtid, end_log_pos, tables, complete) and the channel carries `skip_plan`. Envelope: `schema_version, engine, server_version, captured_at, uptime_s, available, message, replica, statement, group_replication_channels[], cluster_hint, gtid {mode, enforce_consistency | slave_pos, current_pos, *_domains, domain_id, strict_mode}, settings, channels[] {name, kind replication|group_replication, judged, source {host, port, uuid, server_id}, io, sql {running, state, error {errno, message, timestamp, table, end_log_pos}, delay_s}, lag_s, lag_notes, lag_signals, position, worker, agat, failing_gtid, relay, status, status_why, skip_plan}, capabilities, notes, summary`. Statuses: problem when a thread is stopped or reports an error, watch while connecting, when lag minus SQL_Delay passes `LAG_WATCH_S` (60 s), or when nothing (no event, no heartbeat) arrived for twice the heartbeat interval (at least 60 s); unknown when the lag is NULL with both threads running. Group Replication's own channels (`group_replication_applier`, `group_replication_recovery`, reserved in `M/sql/rpl_msr.cc`) are not asynchronous replication: they are left out of `channels[]` and named in `group_replication_channels[]` (a note names them when other channels exist), so a group member with no other channel has `replica: false` and a message pointing to Cluster. `SHOW REPLICA STATUS` walks only the asynchronous channel map (`M/sql/rpl_replica.cc:3866`, `M/sql/rpl_msr.h:568`), so a member normally reports no channel at all. For a server that is not a replica, `cluster_hint` (`galera`, `group_replication` or null) comes from one more read, `SHOW GLOBAL VARIABLES WHERE Variable_name IN ('group_replication_group_name', 'wsrep_provider')` (a Galera provider other than `none`, or a configured group name; a refused read is no hint), and the message then names Cluster (`myflames cluster`); it is a pointer, never a verdict on the cluster. No privilege: `available: false` (CLI exit 2). `--channel` that does not exist exits 2. Text output includes each channel's `Retrieved_Gtid_Set` (MySQL) or `Gtid_IO_Pos` (MariaDB) when present, including when the applier is stopped.
- Source version (`read_status(conn, source_versions=True, profiles, open_profile, replica_host)`, `attach_source_versions`): only the status endpoint and `myflames replication` ask for it, never the skip or loop reads. Every replication channel gets `source_version` (`null` on Group Replication channels, and when not asked; `capabilities.source_version` says it was): `{version, version_comment, engine, provenance saved_connection|relay_log|null, label, profile_id, profile_name, relay_log_file, verified, caveat, reason, hint, attempts[]}`. Order: (a) the relay log, read first because it needs no other login: `SHOW RELAYLOG EVENTS IN '<Relay_Log_File>' LIMIT 4 FOR CHANNEL '<name>'` (MySQL, REPLICATION SLAVE) or `SHOW RELAYLOG '<name>' EVENTS IN '<Relay_Log_File>' LIMIT 3` (MariaDB, SLAVE MONITOR): the last `Format_desc` row after the file's own header whose `Server_id` is the source's, "Server ver: X" (`parse_source_fde`); only type, server ID and a format event's text are kept; label "as recorded in the relay log" with `caveat` (the version that wrote the source binary log being read, which can lag a source upgrade). Source lines are in the replication.py block comment. (b) Only when the relay log gives no version: saved connections (`saved_profiles(store)`) whose host and port are the ones the replica reports (`_host_key`: names ignoring case and a trailing dot, IP addresses in canonical form, so `0:0:0:0:0:0:0:1` is `::1` and `::ffff:127.0.0.1` is `127.0.0.1`), tried in order; 127.0.0.1, localhost and ::1 are one host only when the replica connection itself is on loopback and exactly one saved connection matches that way (another loopback address such as 127.0.0.2 needs an exact match). Before any saved-password lookup or login for a loopback source, the replica must have been reached on loopback AND its `@@hostname`, read on the already-open replica connection, must equal `socket.gethostname()` under `error_log.same_host` (ignoring case and a trailing dot). A tunnel to a remote replica fails this check: no saved connection is prepared or opened, and `attempts` says "The replica is not on this computer, so its loopback source cannot be matched to a saved connection" with both host names. An unknown replica address (`replica_host` None: no match) or an unreadable replica `@@hostname` also opens nothing. Loopback (`_is_loopback`) is localhost and names under `.localhost`, 127.0.0.0/8, ::1, their IPv4-mapped forms, and the unspecified 0.0.0.0 and ::. Only connections with a saved password are read (`profile_opener(store)`: `connection_with_saved_password`, `MySQLConnection` with `SOURCE_CONNECT_TIMEOUT_S` 2 and `SOURCE_QUERY_TIMEOUT_S` 3, the isolated client, never a prompt, never the replica's credentials); one tagged `SHOW GLOBAL VARIABLES WHERE Variable_name IN ('hostname', 'server_id', 'server_uuid', 'version', 'version_comment')`. Accepted only when every identity the replica reports (`Source_Server_Id`, `Source_UUID`) is present and equal in the answer, never when the replica has recorded neither, and, as a second guard for a loopback source address, only when the answer's `hostname` equals the replica's `@@hostname` (`SHOW GLOBAL VARIABLES WHERE Variable_name = 'hostname'` on the replica, read once per report before any such login within `SOURCE_QUERY_TIMEOUT_S` and compared with `error_log.same_host`). `verified` is true only when a source UUID was compared; with the server ID alone (MariaDB, or MySQL before `Source_UUID` is known) it is false and the text and UI say "only its server ID could be compared". Label "from the saved connection <name>"; errors are redacted with that connection's password. Opens are limited per process: results (variables or failures) use a bounded cache (`SOURCE_CACHE_SIZE` 256; the least recently used answers go first, then failures, never a pending read). A separate registry (`SOURCE_ATTEMPT_LIMIT` 1024) retains every attempted `(profile ID, fingerprint)` until the process ends, including successes and failures whose results were evicted. An evicted result returns "Not checked" without another login; at the registry limit, new automatic attempts also return "Not checked" with the reason, while cached results remain available. Pending claims reserve registry slots; only a failure before anything is sent releases its slot. The fingerprint covers the current stored settings and `updated_at` plus a digest of the saved password reference (`secret_ref`, new on every password save) under a per-process random key. `profile_opener(store).prepare` reads that record in one snapshot (`WorkspaceStore._profile(include_updated=True)`), under the claim lock and without reading the secret store; stale caller fields never supply a new claim's fingerprint, and a changed endpoint requires a refresh before matching again. Single-flight is by saved connection ID: a pending read for any fingerprint keeps later callers waiting within their budget. After it completes, they claim again from the current stored profile, so a save during the wait cannot make them reuse an old answer. The password is looked up only by the owner, and a changed stored fingerprint during that lookup aborts before login. So each fingerprint is opened at most once per backend process; a changed profile gets a new attempt within `SOURCE_ATTEMPT_LIMIT`. A later read that uses a kept answer gets `caveat` saying it was read earlier; a kept failure says "Not tried again until the saved connection changes." A failed saved-password lookup (`ValueError`, `sqlite3.Error`, `OSError`) sends no login and is not kept. The CLI is one process per run, so it makes one attempt per run. Time: at most `SOURCE_PROFILE_READS` (4) opens per report, and `SOURCE_READ_BUDGET_S` (5) for all saved connections of one report from its first one; a source is opened only when the time left covers a full `SOURCE_QUERY_TIMEOUT_S`, otherwise it says "Not checked: ..." and gets its turn on a later read. That is checked again after the saved password lookup, which can wait (the Keychain, the vault lock) and cannot be interrupted: when the lookup leaves less than `SOURCE_QUERY_TIMEOUT_S`, the client is closed with nothing sent, the source says "Not checked: looking up the saved password ..." and nothing is kept, so the next read tries again. Otherwise the login and query run under `SOURCE_QUERY_TIMEOUT_S` (connect timeout `SOURCE_CONNECT_TIMEOUT_S`), so nothing is sent to a saved connection after the budget; only a lookup still running at its end can delay the report. (c) `label` "Version unknown", `reason` (why the relay log gave none: no privilege, no relay log file, source server ID not recorded, no source format event at the start of the relay log, the relay log could not be read; followed by the last saved-connection outcome when one was tried) and `hint` "Add a saved connection for host:port to show its version." when no saved connection matched and matching found no problem. Text output: one line per channel, `Source version: 9.7.2 (relay log)`, `(saved connection NAME)`, `(saved connection NAME; only its server ID could be compared)`, or `unknown.` with the reason and hint. Tests: `test/test_replication_source_version.py` over `test/fixtures/perf/replication-*-srcver-*.json` (live, 2026-10-20: MySQL 9.7.2 and MariaDB 11.8.9 replicas sharing their source's network namespace, so the source is 127.0.0.1:<port>; the `@@hostname` answers and this computer's host name are synthetic) and fakes for the cache, the time budget and the loopback host rule; recordings made before that get a synthetic refusal for the relay log head in `test_replication.fixture`. Browser: `test/replication-source-version.mjs`.
- Skip plans (`plan_skip`): MySQL with a GTID for the failed transaction, gtid_mode not OFF and no AGAT: `SET GTID_NEXT = '<gtid>'; BEGIN; COMMIT; SET GTID_NEXT = 'AUTOMATIC'; START REPLICA SQL_THREAD FOR CHANNEL '<name>'` (`empty_gtid_transaction`; the GTID comes from the error text, the failing worker's text or APPLYING_TRANSACTION, or the relay log). Anonymous transactions or AGAT: `SET GLOBAL sql_replica_skip_counter = 1; START REPLICA SQL_THREAD FOR CHANNEL '<name>'` (`sql_slave_skip_counter` before 8.0.26, `START SLAVE` before 8.0.22), refused when the relay log shows the transaction at the applier's position is not the one that failed. MariaDB: `SET default_master_connection = '<name>'; SET GLOBAL sql_slave_skip_counter = 1; START SLAVE '<name>' SQL_THREAD`; with GTID, `slave_parallel_threads > 0`, a parallel mode and more than one domain in gtid_slave_pos the plan is `available: false` (the server answers 1966; setting gtid_slave_pos is explained, never automated). `identity` (channel, method, error number, message and time, GTID, relay and applied positions, source host, port and UUID) is what the person confirms. Error 3030 (`WORKER_STOPPED_BY_EARLIER`, ER_REPLICA_WORKER_STOPPED_PREVIOUS_THD_ERROR: with replica_preserve_commit_order a worker stopped and rolled back because an earlier transaction failed) is never the transaction skipped: `effective_error` takes the worker that failed first, the plan's `error` and GTID are its, the identity keeps the status as it reads, and the plan is `available: false` when only 3030 workers are visible; the skip loop compares the selected codes with the same error on every read, including both guards inside `skip_one` immediately before sending. If the underlying failure disappears and only 3030 remains, the loop refuses because it cannot tell which transaction to skip. All identity, policy, configuration, restart, receiver and progress guards remain active (`mysql-8.4-mta-3030-error`, recorded live: worker 1 1062 on :15, worker 2 3030 on :16, the status reporting the 3030).
- `skip_one(conn, channel, expect, audit, decision, confirmed, statements)` (`replication-skip-1.0`): policy first (`dbops.gate`; every skip script is risky, so Production and Development confirm and a read-only connection refuses before any SQL), then a fresh read and plan; refuses (nothing sent) when the channel is gone, has no error, has no safe method, the applier runs again, any identity field changed, or the statements differ from the confirmed ones. The script is sent as one client call, each statement tagged `/*myflames:replication-skip*/` on its own line, with `dbops.WRITE_CLIENT_FLAGS`. Server errors become statuses (`denied` 1044/1045/1095/1142/1227, `refused_by_server` 1198/1966/3086/1781/1782/1790 and other setting errors, `failed`), naming which statements ran before the failing line; a lost connection raises and the audit record is `unknown`. After `SETTLE_S` the channel is read again: `after.passed` checks gtid_executed (GTID method), the domain in the global gtid_slave_pos (MariaDB; the `Gtid_Slave_Pos` column is gtid_current_pos) or the applied position against the transaction's end; a leftover Skip_Counter is reported. Audit operation `replication_skip`.
- `python3 -m myflames replication skip --channel NAME [--yes] [--json] [-o PATH] [-q]`: policy from `--profile` before any prompt or connection, plan and drift warning on stderr, `[y/N]`, non-TTY needs `--yes`. Exit 0 skipped, 1 a sent skip was denied, failed or was not verified, 2 an unsent refusal, decline, bad input or connection failure. `--channel ''` is the default channel.
- `SkipLoop` (`replication-skip-loop-1.0`): selected error codes only (none preselected), `max_skips` 1 to 1000 (CLI default 10), optional duration, check interval 1 to 60 s with the wait doubling per consecutive skip up to 60 s. Stops at once (`STOP_REASONS`, `needs_attention`): user stop (wakes a waiting loop), cap, duration, an unselected error, the same transaction failing again, the applier stopped without an error, channel gone or now Group Replication, source host/port/UUID, gtid_mode, AGAT or skip method changed, Uptime went down or the boot-time interval estimated from Uptime and the monotonic clock changed, lost connection, a refused, denied or failed skip. Receiver reconnect monitoring is optional at startup: readable, enabled stage counters capture an epoch and a changed epoch stops with `receiver_reconnected`. If those counters become disabled, absent or unreadable after being enabled, stop with `receiver_unverified`; never silently drop an active guard. When instrumentation is off, absent or denied at startup, or Performance Schema is off, the loop runs with the other guards and keeps reconnect monitoring off for that run. Loop snapshots carry `receiver_guard` (`"on"` or `"off"`), `receiver_guard_reason` (string or null), `receiver_guard_instrument` (exact stage name, null before the engine is known), and `receiver_guard_note` (coverage and enable guidance). `Start` reads the initial receiver identity on the loop's own connection before returning, bounded by the existing connection/query timeouts, so its first snapshot already reports actual coverage and the instrument; pending initialization is never reported as disabled instrumentation. `receiver_guard_plan(conn, report)` reads this coverage for a confirmation without changing instrumentation; the workspace replication response includes these fields at the report level and on each `skip_plan`. The confirmation and running status should display the note and reason. Echo `receiver_guard` to `skip-loop/start`: `"on"` requires coverage from the first loop read, so losing it after confirmation also stops. `replication_receiver` in Instruments maps `events_stages_summary_by_thread_by_event_name`, `threads`, `global_instrumentation`, `thread_instrumentation` and the engine-specific `stage/sql/Checking source version` (MySQL) or `stage/sql/Checking master version` (MariaDB). Both stages are off by default; counts suffice without timing, and receiver background threads do not depend on `setup_actors`. myflames never enables instruments. Each skip goes through `skip_one` with the loop's decision and is one audit record (`replication_skip_loop`). Workspace loops reload the profile from the store before every skip and recheck at the send boundary. A changed policy, deleted profile or moved endpoint stops with `policy_changed`; editing a profile to read-only or deleting it also signals its registered loops immediately. `WorkspaceServer.server_close` stops and joins all loops. Status and Stop remain usable after a policy change. In memory only: nothing resumes after a restart.
- `python3 -m myflames replication skip-loop --channel NAME --errors 1062,1032 [--max N] [--max-minutes M] [--interval S] [--yes] [--json] [-o PATH] [-q]`: runs in the foreground and prints each live event on stderr (suppressed by `-q`); writes the final JSON snapshot of a started loop, or the final message without `--json` to stdout or `-o`, with identical payload bytes for either destination; Ctrl+C stops it. Exit 0 stopped by Ctrl+C, `--max` or `--max-minutes`, 1 a started loop stopped on a condition that needs attention, 2 an unsent refusal, decline, bad input or connection failure (also a lost connection).
- An unexpected exception in any endpoint answers 500 with a neutral sentence that never carries the cause (`ui._unexpected_error_message`: plan endpoints "Could not render this plan. Try another view or the CLI."; `/api/live/*` "Could not read this from the server. Try Refresh or the CLI."; others "Could not complete the request. Try again or use the CLI.") and writes the traceback to stderr with every password replaced by `[redacted]`: values under any request key containing `password`, and the saved password of a connection that sends `profile_id` without one, also in their repr and JSON-escaped forms (`ui._log_unexpected_error`). Tests `test_unexpected_error_is_neutral_for_live_reads_and_logged_to_stderr` and `test_unexpected_error_log_redacts_every_password_form` in `test/test_ui.py`.
- Every 400 answer of `do_POST` (ValueError and friends, and ConnectorError) goes through `ui.request_error_text`. A common connection error leads with plain words and what to check (`dbops.connection_error_text`), then the client's line from `ERROR n` on, with the `mysql client exited N:` wrapper dropped and anything after the line (an SSH tunnel note) kept. 2002/2003: "Could not connect to the server. Check that it is running and that the host and port are right." ("… the socket path is right." for a socket failure; MariaDB's client reports a refused TCP connection as 2002). 2005: "Could not find the server's host name. Check the host for a typing error, or use its IP address." 2006/2013: "Lost the connection to the server. Check that it is still running: it may have restarted, or a network or time limit ended the connection." followed by the whole original message, so a write's context ("Could not end connection 41: …") stays. 1045 only when the text says Access denied: "The server refused the user name or password. Check both, and that the account may connect from your address." ("The server refused the login, and no password was sent. Enter the password, or check the user name." for `(using password: NO)`); a 1045 without Access denied gets no password hint. For 2002, 2003, 2005 and 1045 no session was opened, so a reader's context before the error ("Could not read the server status: ") is dropped. Other messages keep their words; only one that does not start with "Could not ", "Cannot ", "Can't ", "Unable to " or "Failed to " gets "Could not complete the request: ", and a ConnectorError without a mapped code passes through unchanged. The neutral 500 text is unchanged. Codes come from `include/errmsg.h` (texts checked in MySQL's `libmysql/errmsg.cc` and MariaDB's `sql-common/errmsg.c`). The CLI still prints its own context ("Could not read the server status: ERROR 2003 …"), and Cluster's `failureText` keeps its one sentence. Tests: `TestConnectionErrorText` in `test/test_dbops.py`; `test_failed_reads_lead_with_plain_words_on_every_server_view` and `test_a_reader_context_and_the_generic_prefix_never_stack` in `test/test_ui_http.py`.
- Saved passwords are bound to the exact `ENDPOINT_FIELDS` (`host`, `port`, `user`, `ssl_mode`, `ssl_ca`): a profile saved without `ssl_mode` means the client's own default (not PREFERRED for every client, for example MariaDB 11.4+ verifies by default), so a request that names PREFERRED is refused until the profile is saved with it (`test_saved_password_binds_to_the_exact_tls_mode`). `scripts/ga-matrix.sh profiles` saves `ssl_mode` PREFERRED for that reason; an endpoint mismatch also shows the connection as Production in the picker (`connectionSafety`).
- Endpoints: `POST /api/live/replication` (status; `_CONNECTION_PATHS`, saved-password helper), `POST /api/live/replication/skip` (`channel`, `expect`, `statements`, `confirmed: true`; `_WRITE_PATHS`: policy, then the saved password, then `skip_one`), `POST /api/live/replication/skip-loop/start` (`channel`, `codes`, `max_skips`, `max_minutes`, `poll_s`, `confirmed: true`; same order; the loop keeps its own connection and closes it when it stops), `.../skip-loop/status` (`job_id` or every loop of this connection) and `.../skip-loop/stop` (`job_id`). Loops live in `workspace.replication_loops` (`SkipLoopRegistry`) keyed by `dbops.connection_identity` and matched by its endpoint (`dbops.identity_endpoint`: saved connection id, host, port, user, TLS), so status and stop from another connection are refused while a change of the connection's type or read-only flag keeps them (one loop per channel and endpoint); status and stop open no connection. A refused or unconfirmed request is audited.
- Workspace UI (`ReplicationPlace.tsx` and `Replication.tsx`, the standalone Replication place): the channel table and inspector; Skip this transaction and Skip until stopped re-read the failure before their sheets open, show the tagged statements, plan again (nothing sent) when the identity or statements changed or the sheet is older than `PLAN_TTL_MS` (60 s), and call `audit/declined` on Cancel and Escape. Loop status is read once a replica report is shown, while a loop runs, and after Start or Stop; a running loop's line (with Stop) also sits above other Server views. The session keeps only `slow.replication_loop_active`, so a restored workspace mounts Replication, reads loop status, and says "Stopped when myflames restarted" when the registry is empty; it never starts a loop. Both views take `missingPrivileges` (`opsApi.MissingPrivileges`, "Requires CLONE_ADMIN") to grey an action with its reason once a privilege context passes it; absent means unknown and the server decides. Browser suite `apps/workspace/test/replication.mjs`.

### Cluster: Galera, Percona XtraDB Cluster, InnoDB Cluster

Topology presentation shares Depth and Pause motion controls with dimensional server glyphs and selectable nodes. Motion illustrates reported running links or healthy group membership, never measured traffic. Failed, stopped, unknown and stale links remain still, and reduced-motion preferences disable animation.

On a ProxySQL Admin connection, show Not part of a cluster, "This connection
is a ProxySQL Admin interface." and Open ProxySQL. Do not turn this into a
password error or show it over another connection's report. `failureText`
only uses the user-name-or-password sentence for error 1045 with Access denied.
Galera nodes not read here have one compact "In the component · not read here"
line; the notes explain the limitation once. `ClusterTopology` offers
Use &lt;connection&gt; when a saved connection matches the node, only on a current
report. The action switches to that connection so the node can be inspected.

The Cluster place and `myflames cluster` show the cluster the connected server belongs to. Reading changes nothing; the place's reviewed changes are described under "Cluster changes" below (`myflames cluster` and the MCP tool stay read-only). Contract (`myflames/cluster.py`, `docs/schemas/cluster-1.0.json`; verified against mariadb-server, mysql-server and mysql-shell source, with the line references in the module docstring, and live on MariaDB 11.8.9 Galera 26.4.27, Percona XtraDB Cluster 8.4.11-11.1 and a MySQL 8.4.11 InnoDB Cluster with MySQL Router 8.4.10):

- `python3 -m myflames cluster [--fail-on {problem,watch}] [--json] [-o PATH] [-q]` (`cluster-1.0`, `read_status`), connection flags and `--profile` as the other live commands, query timeout 30 s. Tag `/*myflames:cluster*/`; every statement is a `SELECT` or `SHOW GLOBAL`. Exit 0 (also for a server that is not part of a cluster), 1 when a finding is at the `--fail-on` level or worse, 2 for bad input, an account that cannot read the cluster tables (`available: false`; `--json` still writes the report to stdout or `-o`) or a connection failure.
- Detection: Galera when the global `wsrep_on` is ON (MariaDB), or, when SHOW GLOBAL VARIABLES has no `wsrep_on` (Percona XtraDB Cluster 8.4, verified live on 8.4.11, where it is session-only), when `wsrep_provider` names a provider. MariaDB with the global `wsrep_on` OFF and a provider loaded reads the wsrep status anyway: while `wsrep_connected` is ON or `wsrep_cluster_size` above 0 the node is still in the component (the server-level names stay listed, the provider's are hidden) and is reported as Galera with the problem finding `wsrep_off`; when the account cannot read that status, `available: false` (unreadable is not disconnected); otherwise not a cluster ("the Galera provider is not connected"). `kind` is `pxc` when `version_comment` names Percona XtraDB Cluster, else `galera` (labelled MariaDB Galera Cluster on MariaDB and Galera Cluster for MySQL on MySQL, `cluster.kind_label`, `kindLabel`). Otherwise, on MySQL 8.0+, Group Replication from `performance_schema.replication_group_members` (with `MEMBER_COMMUNICATION_STACK` from 8.0.27); a single OFFLINE row without a group name is the idle plugin. `innodb_cluster` when `mysql_innodb_cluster_metadata.v2_gr_clusters` lists the group name, else `group_replication`. Without a group, a server whose `@@server_uuid` is a `read-replica` instance in its replicated copy of `v2_instances` is an InnoDB Cluster read replica (`kind: innodb_cluster`, the local member with role `READ_REPLICA` judged by its `read_replica_replication` channel in `replication_connection_status` and `replication_applier_status`: both ON ok, the receiver CONNECTING watch (also with its last error kept while it retries, as MySQL Shell tells it apart), a stopped receiver or applier, or an error while not connecting, problem; a table that cannot be read leaves it unknown unless what was read shows a failure; the group instances from the metadata with state null, never MISSING). MariaDB without wsrep and MySQL without a group return `kind: "none"` with `message`.
- Reads: preflight `SHOW GLOBAL VARIABLES WHERE Variable_name IN (...)` and `Uptime`; Galera `SHOW GLOBAL STATUS WHERE Variable_name LIKE 'wsrep\_%'`, `mysql.wsrep_cluster_members` (membership of the last Primary view; on a non-Primary node the members come from `wsrep_incoming_addresses`), and on PXC `performance_schema.pxc_cluster_view`; Group Replication `replication_group_member_stats`, then `information_schema.SCHEMATA`, `schema_version`, `v2_gr_clusters`, `v2_instances` (`attributes->>'$.instance_type'`: read replicas from 2.2.0 are listed apart, never MISSING) and `v2_routers` (the five port keys only, by `attributes->>'$.KEY'`, so the router configuration and its metadata account never leave the server; from metadata 2.4.0 `COALESCE(router_stats.last_check_in, routers.last_check_in)`, as MySQL Shell reads it; also `UNIX_TIMESTAMP` of the check-in for its UTC time).
- Envelope: `schema_version, engine, server_version, version_comment, captured_at, uptime_s, performance_schema, available, message, kind, server {hostname, port, report_host, server_uuid}, cluster {name, uuid, size, expected_size, expected_source, component, quorum, failures_tolerated, topology, provider}, status, status_why, local, members[] {key, name, address, host, port, uuid, state, state_source, role, version, local, status, status_why, metrics, label?, endpoint?, in_metadata?, instance_type?, communication_stack?, pxc_status?, segment?}, galera {..., wsrep_on, address_nodes, address_names_this_node, missing, incomplete}, group_replication, metadata {..., local_copy}, routers[] {id, name, product, address, version, last_check_in, last_check_in_age_s, last_check_in_utc, from_local_copy, ports}, findings[] {id, status, title, text, member?}, summary, statements, notes`. Members are sorted by name; `member.host` and `port` are separate (an IPv6 host is bracketed only in `address`).
- Severity, one scale for every kind (CLI, JSON, MCP, the place, the sidebar, `--fail-on`): `ok` with quorum and every expected member present and healthy; `watch` while quorum holds but a member is missing (a Galera node not in the component, an InnoDB Cluster member MISSING or UNREACHABLE), in a transitional state (Donor/Desynced, Joiner, Joined, Joining, RECOVERING), or one more failure would lose the majority (`no_tolerance`, "Cannot survive another failure"); `problem` without quorum or outside a Primary component, for a member in ERROR, when the connected member is OFFLINE or in ERROR, or a Galera node that does not replicate (`wsrep_off`).
- Members as this server sees them: on Galera only the connected node has a state (`wsrep_local_state_comment`); the others have `state: null`, status `unknown` and read In the component (state and queues are read on that node), never a guess. On PXC the other nodes' states come from `pxc_cluster_view`, shown in the node's own words (DONOR as Donor/Desynced, JOINER Joiner, JOINED Joined, PRIMARY Joining, NON-PRIMARY Non-Primary, SYNCED Synced; SYNCED is ok, the others watch, each with its own sentence) and the raw STATUS in `pxc_status`. Group Replication members carry MEMBER_STATE and MEMBER_ROLE; an UNREACHABLE member keeps the role of its status (`ps_information.cc` overrides only the state). A group instance in the metadata but not in the group is `MISSING` (as MySQL Shell calls it) unless the connected member is OFFLINE or in ERROR, which sees no group: then `cluster.size` is null (it lists only itself), the other instances read Not visible here with a reason that names the connected member, `metadata.local_copy` is `left_group` and each router's `from_local_copy` marks its check-in as this member's frozen copy. Galera port 0 in an incoming address (`wsrep_node_incoming_address` AUTO) is `port: null` and every surface shows the host alone, with one note (names in member order). When `mysql.wsrep_cluster_members` cannot be used, PXC's `pxc_cluster_view` rows are matched to the incoming addresses by `LOCAL_INDEX` (the same view order). This node keeps the key `node:<gcomm uuid>` and its UUID in Primary and non-Primary components.
- Expected Galera size (`cluster.galera_membership`, shared with checks.wsrep): the distinct entries of `wsrep_cluster_address` (host and port), used only when this node is identified: named by one entry (by `wsrep_node_address` host and port when both carry a port, else by a host or name that matches only one entry, a short name matching an FQDN's first label and two different domains never matching: `@@hostname` and its first DNS label against FQDN entries, `report_host`, `wsrep_node_name`, `wsrep_node_incoming_address` when not AUTO, and the host of this node's own `wsrep_cluster_members` row or of `wsrep_incoming_addresses[wsrep_local_index]`), or proved absent (every entry is another member, or on this host with another port), which counts it once more. Otherwise `expected_size` is null and a note says the list is a lower bound; it never raises a watch. A component larger than the list (a partial seed list, an arbitrator) keeps its size alone with a note, never "3 of 2".
- Completeness (`cluster.galera_incomplete`, shared with checks.wsrep): without `wsrep_cluster_status`, `wsrep_connected`, `wsrep_ready` or `wsrep_cluster_size`, with a size that is not a node count, or with `wsrep_flow_control_paused` outside 0 to 1, nothing is judged (no finding, no member verdict, nothing for `--fail-on`): `galera.incomplete` names them, a note says so and `status` is `unknown`.
- Findings (`watch` or `problem`): `non_primary` (problem; the text lists what still runs on both engines: SET, SHOW, SELECT without tables, reads of only information_schema or performance_schema tables, reads with wsrep_dirty_reads), `state_transfer` (watch: non-Primary while connected and Joining, which is how MariaDB reports a joiner), `wsrep_off` (problem), `not_connected`, `not_ready` (watch while joining, else problem), `local_state` (this node not Synced: Donor/Desynced and Joined watch, Initialized problem; with `wsrep_desync` ON the text says so and that it lasts until set OFF, otherwise it names a state transfer, FLUSH TABLES WITH READ LOCK, BACKUP STAGE BLOCK_DDL and RSU schema changes), `member_state` (another member not Synced or not ONLINE), `size_below_configured` (Galera: fewer nodes than the expected size above, naming the entries not in the component when they match the missing nodes one for one (`galera.missing`, drawn as dashed placeholders), else saying how many; InnoDB Cluster: fewer than the metadata), `no_tolerance` (Galera (size - 1) / 2 is 0 in a Primary component, equal `pc.weight` assumed and nothing computed when `wsrep_cluster_weight` differs from the size; Group Replication (members - UNREACHABLE) - (members / 2 + 1) is 0, null without quorum), `flow_control` (`wsrep_flow_control_paused` at or above 0.1, `FLOW_CONTROL_WATCH`; the fraction covers the time since the provider's statistics were reset, FLUSH GLOBAL STATUS from MariaDB 11.5.1, `cluster.reset_statement`), `cert_failures` (any `wsrep_local_cert_failures`, `CERT_FAILURES_TEXT`, the Full analysis's rule), `pxc_maint_mode`, `no_quorum` (members minus UNREACHABLE at most half, `member_info.cc`), `local_not_in_group`, `read_replica`, `version_mismatch`, `primary_count` (single-primary mode with no or several PRIMARY members, while the group has quorum). A finding's text never repeats the state word its title carries. `status` is the worst finding, `ok` when a member was judged and nothing was found, `unknown` otherwise. An `ok` report's `status_why` says what was measured (`_ok_text`): "the cluster has quorum" only when `cluster.quorum` is true; from a read replica, which is not a group member, quorum and `failures_tolerated` stay null and the sentence says they are not visible there (the CLI text adds "quorum and failures tolerated unknown (not a group member)"); otherwise that quorum could not be measured from this server. Router check-ins are shown with their age and UTC time (`check_in_text`, the same in the CLI, MCP and the workspace), never judged (MySQL Router writes them on the writable member every tenth metadata refresh by default, about every 5 s, never by default in a ClusterSet, or as `stats_updates_frequency` sets; the metadata schema's comment says hourly).
- Text (`format_text`, also the MCP core digest): one line per member with the name once (an address equal to it is not repeated), "(this connection)" in words, the address as on every surface, and a state this server cannot read said in words; `wsrep_cluster_address` and named missing entries; flow control as a percent (`flow_percent`: one decimal, cut, "<0.1%"); the group size as "not visible from this member (N in the metadata)" when this member left the group.
- API: `POST /api/live/cluster` (`_SERVER_VIEW_PATHS`, saved password filled on the server, query timeout 30 s) returns the same report. MCP: `cluster_status(profile, detail)` (saved profile only; `detail="core"` is `format_text` with the notes, `"full"` the report).
- Workspace (`ClusterPlace.tsx`, `ClusterTopology.tsx`, `clusterApi.ts`, `cluster.css`): a place of its own after Replication. It reads when opened on screen (`useOpenRead`, once per connection; Refresh reads again) and Server's Refresh all reads it too (`refreshTasks`, view id `cluster`, label Cluster). Toolbar with Refresh, the ⓘ (the severity scale, and that changes ask first and are audited) and the Segmented sections Topology, Members (Nodes on Galera and PXC) and, for InnoDB Cluster, Routers, which stay in the title row while they fit; the summary line (its OK has the same shape and word as Watch and Problem, and stays beside the text at 720 px); when the status is not OK the findings come first, one compact line each (the state's shape and word, the title in foreground text with only the member name linked, the reason on demand stacked under it; Details stay open by the finding's identity, `findingKeys`: its id and member, never its position, and `Findings` remounts per connection, so nothing opens on another finding after a refresh or on another cluster), then four tiles (Component or Quorum with failures tolerated, watch when it tolerates 0 and problem without quorum, "Not visible" with "Failures tolerated unknown" and the reason in its title on a read replica or a member out of the group; Nodes "3 in component · 1 inspected here" or Members "2 of 3 ONLINE", "Not visible" with "3 in metadata" when this member left the group or on a read replica, with the worst member's tone; This node or This member with the role as a hint; Flow control paused or Mode). Cards, the Members table, the inspector and findings share the status dot plus a word (`ClusterMark`); navigation uses a compact `NavMarker` with a neutral count or the state in its tooltip and accessible name. Topology uses a dimensional presentation: member cards (role badge, name, the address when it differs from the name, engine and version (PXC as "PXC 8.4.11-11.1", the full name on hover; nothing for another Galera node), the state, queues or flow control as text, and last the "This connection" chip, so every card's lines line up) on stems to one group line labelled with the component or the group mode, quorum and failures tolerated; a configured Galera node not in the component as a dashed placeholder; read replicas below the group on dashed neutral links ("Read replicas · asynchronous, not group members"); routers under them. A member that is UNREACHABLE, MISSING, OFFLINE, in ERROR or judged a problem has a dashed edge in its tone, a cut stem and its shape; a watch member has a heavier left edge. Only reported healthy membership may animate, with Pause motion and reduced-motion overrides. At a view under 640 px the cards wrap in a grid inside a frame that stands for the group (dashed without quorum), and the tiles are shorter, so the first tab's content starts above the fold at 720×520. Members (`cluster.members` on Group Replication and InnoDB Cluster, `cluster.nodes` on Galera and PXC, whose columns differ; the name column starting at 150 px so nine columns fit at 1440 px) and Routers (`cluster.routers`) are DataTables, each its own instance (`key`) with its own column layout: the shell owns one record of layouts by table id (`main.tsx` `columnLayouts` through `useColumnStore`, given to Server as `columns`/`onColumns` and to the Cluster place through `ColumnStoreContext`) and saves it as `slow.columns_v1` (`TABLE_IDS` in `session.ts`), so a layout survives Topology, a restart and Server not being mounted; the inspector shows a member's state and where it was read (and the raw `pxc_cluster_view` STATUS), its counters (flow control as a percent and its fraction), and this node's Galera settings, or a router's ports and check-in (age and UTC, and a note when it is this member's frozen copy). Terms carry short glossary tooltips (Primary component, flow control, failures tolerated, queues, router ports). A report of another connection (`otherConnection`), or the last report after a failed read of this one, by the place's Refresh or by Refresh all (the shell keeps the failure with the report it failed over, `ClusterFailure`, until a read through either path succeeds; `is-stale`, with "Last read 14:40 (5 min ago)"), stays greyed and marks its member "Read here" (title: where and when it was read), never "This connection"; a failed read shows one short sentence (`failureText`: connection refused, lost, timed out, access denied), never the client's dump, and without a report a "Could not read the cluster" state with Retry instead of the first-run Load. The shell derives the sidebar item from the shared report of the connection in use (`clusterNav`): nothing before the first read; `NavMarker compact` with a status dot and neutral count for problem members, and the state word in the tooltip and accessible name; for `kind: "none"` the item gets `is-muted` (faint text and icon), `data-muted`, the tooltip and hidden text "This connection is not part of a cluster", stays focusable and clickable, and keeps the disabled look while open (a neutral gray fill, never the accent). The palette lists Cluster among the places. Browser suite `apps/workspace/test/cluster.mjs` (reports from `apps/workspace/test/cluster-fixtures.py`).
- Tests: `test/test_cluster.py` replays `test/fixtures/cluster/cluster-*.json` (live recordings: healthy, donor, a node down, an isolated node, a member with Group Replication stopped read from the primary and from itself, an UNREACHABLE member; the recorded `v2_routers` answers carry the `UNIX_TIMESTAMP` column derived from the recorded check-in, the lab servers running in UTC) plus states derived from them (another PXC node Joiner or Joining, the address list as IPs, IP:port, FQDNs, one seed or a DNS service name, two nodes, wsrep_on OFF, a joiner, Galera Cluster for MySQL) and synthetic servers (a read replica connected to, an IPv6 group) in `test/_cluster_fixtures.py`, and checks the schema, read-only SQL, the CLI exit codes, the API and the MCP tool; `test/test_report.py` checks that checks.wsrep and the Cluster place give the same status on the Galera and PXC recordings.

#### Cluster changes

Reviewed changes live only in a member's inspector (one home, no Management
tab or context menu; `cluster.available_actions(report, member_key, checks)`, `action_checks`, `plan_action`, `execute_action`, `with_actions`; the send half is `reviewed_writes.execute`; sources and line references in the "reviewed actions" comment in `cluster.py`):

| Action | Statement (tagged `/*myflames:cluster_op*/`) | Offered when | Typed name |
| --- | --- | --- | --- |
| `switchover` Make primary… | `SELECT group_replication_set_as_primary('<MEMBER_ID>')` | Group Replication or InnoDB Cluster on MySQL 8.0.13+, single-primary, the member SECONDARY and ONLINE with a UUID MEMBER_ID, the connected member ONLINE with quorum, no member RECOVERING or UNREACHABLE | no |
| `stop_member` Stop Group Replication… | `STOP GROUP_REPLICATION;` | the connected member, ONLINE, RECOVERING or ERROR | when it is the primary or the last ONLINE member |
| `start_member` Start Group Replication… | `START GROUP_REPLICATION;` | the connected member, OFFLINE, and `@@global.group_replication_bootstrap_group` OFF (read at plan and send time; ON or unreadable refuses) | no |
| `switch_to_multi_primary`, `switch_to_single_primary` | `SELECT group_replication_switch_to_multi_primary_mode();` / `..._single_primary_mode();` | plain Group Replication only (not InnoDB Cluster), on the connected member, group ready as for switchover | the connected member's name |
| `desync_node` Desync this node… | `SET GLOBAL wsrep_desync = ON;` | MariaDB Galera, the connected node, `wsrep_on` ON, `wsrep_desync` OFF and Synced | no |
| `resync_node` Resync this node… | `SET GLOBAL wsrep_desync = OFF;` | MariaDB Galera, the connected node, `wsrep_desync` ON | no |
| `bootstrap_pc` Bootstrap a Primary component… | `SET GLOBAL wsrep_provider_options = 'pc.bootstrap=YES';` | MariaDB Galera, the connected node, `wsrep_on` ON, component `non-Primary`, `wsrep_connected` ON, not joining; `expect` also binds the component view (cluster UUID, `wsrep_cluster_conf_id`, size, members), so a node joining or leaving needs another review | the node's name |

Never use "GCache" wording. Never offered: `SET GLOBAL wsrep_on` (sessions opened later stop replicating their writes: silent divergence), anything on Percona XtraDB Cluster or Galera Cluster for MySQL (sources not checked), InnoDB Cluster mode switches (MySQL Shell's commands also set `auto_increment_*` and the metadata), and START on a member in ERROR (the plugin still runs; START fails). `expect` binds the server identity (engine, version, `server`), the kind, the member and the connected member (key, UUID, name, role, state), component, quorum and topology, the group's members and states (Group Replication), `wsrep_desync`, `wsrep_on` and the local state (Galera), `wsrep_last_committed` (bootstrap) and `bootstrap_group` (start); any change refuses with nothing sent. Endpoints: `POST /api/live/cluster/plan` (`action`, `member_key`; `_CONNECTION_PATHS`, saved password like the views; returns the plan with `policy` and `review_token`) and `POST /api/live/cluster/action` (`plan` as reviewed without its token, `review_token`, `typed`, `confirmed: true`; `_WRITE_PATHS`), audited as `cluster_action`, envelope `cluster-action-1.0` (below). The send waits up to 120 s (START and the UDFs wait for the group). The workspace's `POST /api/live/cluster` returns `with_actions(read_status(conn))`: each member's `actions` ({id, label, danger, description}; optional in `docs/schemas/cluster-1.0.json`); `myflames cluster` and `cluster_status` do not carry them. Tests: `test/test_cluster_actions.py` (what each recorded and derived state offers, exact statements, refusals, typed names, `bootstrap_group`, endpoints with the audit trail, drift between review and send, unknown and server refusal), and `apps/workspace/test/cluster.mjs` (the inspector's buttons, the review with the UUID, Escape recorded as declined, another connection's report offering nothing).

### ProxySQL

After a reviewed action completes (including an unknown outcome), the workspace
stops pending digest reads and clears cached routing before refreshing the main
report. A separate digest revision triggers a fresh read when Query digests is
visible even if its cache was already empty; hidden tabs defer that read. The
read hook rejects late replies from the cancelled request. This follow-up only
reads; the reviewed action's result and no-retry rule remain in effect.

Digest identity and completion ownership: keys are `dg` plus compact JSON of the primary-key tuple `[hostgroup, schemaname, username, client_address, digest]`, with names preserved and no delimiter splitting in the workspace. Digest hash filters normalize to `0x` plus uppercase hex before the hex-literal SQL comparison. A rule walk that needs an unread or invalid runtime variable returns `unknown` (Not judged), naming the variable; an unread restart budget must never become a known zero. `ActionReview` rechecks the captured connection scope when finishing every send, including refused or lost replies: a switch away, even followed by a switch back, drops the completed sheet and its follow-up actions. The server-side audit record is retained.

The ProxySQL place reads a ProxySQL Admin interface, 2.x and 3.x (`myflames/proxysql.py`, `docs/schemas/proxysql-1.0.json`; checked against the ProxySQL source at 6ef036a, v2.0.0 and v2.7.3 for the identity, and live on ProxySQL 3.0.11 in front of the MariaDB Galera and MySQL InnoDB Cluster lab, `scripts/cluster-matrix.sh up proxysql`).

- Identity (`identify`): `SELECT variable_value FROM global_variables WHERE variable_name = 'admin-version'` returns the version and `SELECT @@version` (Admin answers it itself, Admin_Handler.cpp) returns the same text. A server SQL error means not ProxySQL (`available: false`, `is_proxysql: false`, a message naming port 6032); a client error (2000 to 2999), a refused login or no answer raises, like the other views. Never from a `mysql_servers` table alone.
- Reads, all `SELECT`, each table with `SELECT *` read by column name from the client's XML output (`--xml`, `perf_common.named_rows`): SQL NULL is `xsi:nil` (Admin sends SQLite NULL as a protocol NULL, MySQL_Session.cpp `SQLite3_to_MySQL`) and the text 'NULL' is the field's content, which batch output prints alike; output that is not well-formed XML (a control character in a value) leaves that table unread. Tables: `stats_mysql_global` (uptime and a fixed list of counters), `main.`, `main.runtime_` and `disk.` copies of every table of the servers module (`mysql_servers`, `mysql_replication_hostgroups`, `mysql_group_replication_hostgroups`, `mysql_galera_hostgroups`, `mysql_aws_aurora_hostgroups`, `mysql_hostgroup_attributes`, `mysql_servers_ssl_params`) and the query rules module (`mysql_query_rules`, `mysql_query_rules_fast_routing`) (ProxySQL_Admin.cpp `mysql_servers_tablenames`, `mysql_query_rules_tablenames`), `stats_mysql_connection_pool` (`Bytes_data_sent`, `Bytes_data_recv`, `Latency_us`, doc/Stats_Tables.md), `stats_mysql_query_rules` and `stats_mysql_query_digest ORDER BY sum_time DESC LIMIT 100`. A table Admin refuses (absent in this version, or a stats-only account) is noted and its part is unknown (`null`); it counts as absent only when the main, runtime and disk reads all answer `no such table` (`ProxySQL Admin Error: no such table: main.x`, live on 3.0.11). A column absent in a version is absent from the row. `config_readable` is false without `main.mysql_servers` and `main.mysql_query_rules`, and then nothing is offered.
- Servers: one row per runtime row (`rt|hg|host|port`), with the pool's status word (SHUNNED_REPLICATION_LAG kept), its settings, pool numbers, its monitor and role (writer, backup writer, reader, offline), and its configuration row: the main row with the same hostgroup, host and port, else the one main row with that host and port among the same active monitor's hostgroups (several or none: `config_note`, no change). A configuration row with no runtime row is listed as `cfg|...` (OFFLINE_HARD: ProxySQL drops it from runtime). Tone: ONLINE ok; SHUNNED in a monitor's writer hostgroup while ONLINE in its backup writer hostgroup is a standby writer (ok; Galera keeps writers above `max_writers` there, seen live); other SHUNNED, OFFLINE_SOFT, OFFLINE_HARD and the offline hostgroup watch. Hostgroups: per runtime hostgroup the counts by status, standby writers, queries and connections, and the monitor role; a hostgroup with servers and none ONLINE (other than an offline hostgroup) is a problem. An active rule in `runtime_mysql_query_rules` (what ProxySQL runs, never the configuration in memory, which may not be loaded; a rule deleted or disabled in memory still routes until the LOAD) whose destination hostgroup has no runtime server is a problem. When the runtime rules, or both `runtime_mysql_servers` and the pool, cannot be read, routing is unknown: the status is at least `unknown`, with the reason. Connection errors are cumulative and never judged.
- Pending (`pending.servers|query_rules.runtime|disk`): lines naming each row that differs between memory and runtime or disk (`null`, with a note, when any table of the module cannot be read in one of the two copies; only a secondary table absent from every copy is passed over). Difference lines quote the text 'NULL' and the empty string, so neither reads like SQL NULL. Memory versus runtime for `mysql_servers` allows for what ProxySQL sets itself: a server in a monitor's hostgroups is compared by host and port over all its runtime rows in those hostgroups, settings only, with SHUNNED read as ONLINE, an OFFLINE_HARD row absent from runtime not pending, and OFFLINE_SOFT under the Galera monitor not pending (the monitor puts it back). Every other table is compared row by row by primary key.
- Routing, evaluated here (`myflames/proxysql_digests.py` `Routing`, checked against lib/Query_Processor.cpp `process_query` and `rule_matches_query` at 6ef036a): the report also reads `runtime_global_variables` (`mysql-query_processor_regex`, `mysql-query_processor_iterations`, `mysql-query_digests`, `mysql-query_digests_track_hostname`) and `runtime_mysql_users` by named columns (`username, active, default_hostgroup, transaction_persistent, frontend`; never the password), and evaluates each digest against the active runtime rules in rule_id order (ProxySQL_Admin.cpp loads `WHERE active=1 ORDER BY rule_id`): flagIN must equal the current flagIN (0 first), username and schemaname exact when set, client_addr against the digest's client address (unknown when `mysql-query_digests_track_hostname` is off or the rule has a `%`), proxy_addr and proxy_port unknown (a digest does not record them), digest as a number (`strtoull` base 0), match_digest on the digest text, match_pattern on the digest text marked approximate (it tests the query text), `negate_match_pattern` turning either regex result around (`rule_matches_regex`), CASELESS in `re_modifiers` ignoring case, a pattern Python's `re` cannot compile unknown. A match sets the destination when it is 0 or more and flagIN from flagOUT (or from `attributes` `flagOUTs`, a random pick by weight that a set flagOUT replaces; MySQL_Query_Processor.cpp `new_query_rule`); apply stops; `mysql-query_processor_iterations` restarts the walk after a flagOUT (followed here up to 10 restarts, `ITERATION_LIMIT`). Without apply, `mysql_query_rules_fast_routing` decides: (username, schemaname, flagIN), then the entry with an empty username for that schema and flagIN (`search_rules_fast_routing_dest_hg`); without a destination, the frontend user's `default_hostgroup`. The walk branches (`_Path`, at most 64, `BRANCH_LIMIT`): the first path takes each uncertain test at its best guess, the others flip one uncertain rule that would change something or take another flagOUT weight; a rule's `next_query_flagIN` adds walks that start from that flagIN (the session's following queries do). Patterns never run in the workspace process: `test_patterns` sends every pattern and digest text of a read to `regex_worker` in a child process (`python -I -c` from a checkout; the packaged backend with `__myflames_regex_worker`, handled in apps/desktop/scripts/backend-entry.py), stdin written by its own thread, a `ready` line within 10 s (lines end with `\r\n` on Windows), then one line per pattern within 2 s each (`PATTERN_TIMEOUT`): a pattern that takes longer is unknown ("took too long") and a new worker goes on after it (at most three times); the child is always killed and reaped. A worker that cannot start leaves every pattern unknown with a note. `routing` per digest: `{observed, expected, source rule|fast_routing|default, rules, decisive, approximate, possible, unknown_rules, default_hostgroup, verdict, text, why}`; verdict `match`, `mismatch` (with the reasons: a transaction kept on one hostgroup when `transaction_persistent` is 1, a `/* hostgroup=N */` comment, a connection locked to a hostgroup, rules loaded later), `possible` (another path, from a flipped uncertain rule or a `next_query_flagIN`, ends on the observed hostgroup), `local` (statements MySQL_Session.cpp `handler_special_queries` answers itself, recorded on the default hostgroup: `select @@version_comment limit ?` and `select USER()` exactly, `SELECT $$` ignoring case; seen live on 3.0.11; and `SELECT @@version` or `SELECT VERSION()` only when `mysql-select_version_forwarding` is 0, 2 or 3, 3 answering when a backend's version is known; 1 forwards and 2.x has no such variable), `cache` (hostgroup -1, MySQL_Session.cpp sets it on a query cache hit), `unknown` (also when the walk left work out, a restart or a flagOUT choice past the limits, or fast routing could not be read and no apply stopped the walk: never a mismatch then). A flagOUT weight that reaches the observed hostgroup is a match. More than 500 active rules (`RULE_LIMIT`) or unread runtime rules: not evaluated (`routing.evaluated` false, with a note). The report's rules get `digests` and `digest_count` (the digests whose chain includes the rule, among the report's 100) and `possible_digests`; `report.routing` says how it was evaluated.
- Query digests (`proxysql_digests.read_digests(conn, order, limit, hostgroup, schema, user, text, digest, rule_id)`, `POST /api/live/proxysql/digests`, `proxysql-digests-1.0`, `docs/schemas/proxysql-digests-1.0.json`, in `_SERVER_VIEW_PATHS`, query timeout 30 s): `order` `slowest` (`CAST(sum_time AS REAL) / MAX(count_star, 1)`), `time` (`sum_time`, the default), `frequent` (`count_star`), `rate` (`CAST(count_star AS REAL) / MAX(last_seen - first_seen, 1)`), `rows` (`sum_rows_sent`; ProxySQL 2.0.7 added it, so "no such column" refuses with that reason and marks the order unavailable); ties by `sum_time`, `count_star`, `digest`; `limit` 10 (default), 25, 50 or 100, read as LIMIT n + 1 so `more` says whether more match. Filters: `hostgroup` an integer -1 to 2147483647; `schema` and `user` exact (null is every one; the empty text, a client without a default schema, is a choice of its own, sent by the workspace as the JSON text of the name), `digest` (`0x` and 1 to 16 hex digits) exact; `text` a case-insensitive substring of the digest text (`instr(lower(digest_text), lower(...))`, ASCII folding). Every text is written as a hex blob literal (`CAST(X'..' AS TEXT)`, `text_literal`): Admin refreshes `stats_mysql_query_digest` from memory for every statement whose text names it and resets every digest when the text names `stats_mysql_query_digest_reset` (ProxySQL_Admin.cpp `GenericRefreshStatistics`, `strstr` on the query), so no typed text ever reaches a statement and none names the reset table. `rule_id`: an active runtime rule (refused otherwise), evaluated here, so the top 500 (`RULE_SCAN`) in the chosen order are read and the ones whose chain includes the rule kept (`scanned`). One grouped read (`GROUP BY hostgroup, schemaname, username`, at most 200 groups, `facets.complete`) gives the filters' choices, `totals` (digests, executions, total time, null when the groups were cut) and `window` (first and last execution in Unix seconds; Query_Processor.cpp converts its monotonic times when the table is filled, so they can move by a second between reads). Each digest has `key` (the table's primary key: hostgroup, schemaname, username, client_address, digest), `rate_qps`, `seen_span_s` and `routing`; the reply carries the evaluated `rules`, `rule_hits` and `routing`. Reads only (`SELECT`, nine client invocations). Live on 3.0.11 (2026-10-21): every order and limit, each filter, the rule filter and a typed `stats_mysql_query_digest_reset` (no match, never in a statement); a SELECT inside a transaction kept on the writer hostgroup was flagged as a mismatch.
- Changes (`available_actions(report, target)`, `plan_action(report, action, target, value)`, `execute_action(conn, reviewed, audit, typed, before_send, progress)`):

| Action | Statements | Notes |
| --- | --- | --- |
| `server_online`, `server_offline_soft`, `server_offline_hard` | `/*myflames:proxysql_op*/ UPDATE mysql_servers SET status = '<STATUS>' WHERE hostgroup_id = N AND hostname = '<host>' AND port = N;` then `LOAD MYSQL SERVERS TO RUNTIME;` | the configuration row's status differs; no OFFLINE_SOFT under the Galera monitor (on 3.0.11 it was back ONLINE in the backup writer and reader hostgroups within about 20 s; `update_galera_set_writer` has no OFFLINE_SOFT filter, unlike Group Replication's "PRESERVE-OFFLINE_SOFT"); typed `host:port` for OFFLINE_HARD and for the last ONLINE server of a runtime hostgroup |
| `server_weight` | `... SET weight = N ...` then the LOAD | a JSON integer 0 to 10,000,000 (ProxySQL's CHECK), not the current one; never a string, a boolean or a default |
| `server_hostgroup` | `... SET hostgroup_id = N ...` then the LOAD | an integer 0 to 2147483647, not the current one, not already configured for that host and port, and only for a row in no monitor's hostgroups (live on 3.0.11, mf-ic-3 moved from 30 to 50 stayed ONLINE in reader hostgroup 32 for over 80 s; the comparison then names that runtime row as not in memory); a move into a monitor's hostgroups warns that the monitor places it; typed name when it is the last ONLINE server of its hostgroup |
| `rule_enable`, `rule_disable` | `UPDATE mysql_query_rules SET active = 1|0 WHERE rule_id = N;` then `LOAD MYSQL QUERY RULES TO RUNTIME;` | the rule exists in the fresh read |
| `servers_load`, `query_rules_load` | `LOAD MYSQL SERVERS TO RUNTIME;` / `LOAD MYSQL QUERY RULES TO RUNTIME;` | the review lists every memory-versus-runtime difference (`changes`) |
| `servers_save`, `query_rules_save` | `SAVE MYSQL SERVERS TO DISK;` / `SAVE MYSQL QUERY RULES TO DISK;` | memory to disk (an alias of SAVE ... FROM MEMORY); lists every memory-versus-disk difference and warns when memory has changes not loaded to runtime; cannot be undone |

A row change refuses while the module has other changes in memory (LOAD would apply them too) and names them; the result offers Save to disk as the next, separately reviewed change (`next`). Host names must match `[A-Za-z0-9._:%/[]-]{1,255}` (no quote or backslash; quoted SQLite-style) or the row offers nothing. LOAD and SAVE are sent untagged because Admin compares the whole command text (Admin_Handler.cpp `is_admin_command_or_alias`; a leading comment gave "near LOAD: syntax error" live); the UPDATE goes to SQLite, which keeps the tag. `expect` binds the ProxySQL version, the configuration row or rule as read, the pending lists, and `fingerprints` (SHA-256 of every row) of the module copies the statements depend on: memory for any change, plus runtime query rules for rule changes and query rules loads and disk for saves (never runtime servers, which the pool and the monitors change); an added or removed row is described with its content. A table unread in a copy for any reason (one copy failed, a refusal other than `no such table`, a lost connection) makes that module's comparison unknown (`null`), so that module offers no change (`available_actions`; Save also needs memory versus disk) and a plan for one refuses; only a secondary table every copy reports as `no such table` is passed over. The fingerprints hash SQL NULL as JSON null, apart from the text 'NULL'. Endpoints: `POST /api/live/proxysql` (`_SERVER_VIEW_PATHS`, query timeout 30 s), `POST /api/live/proxysql/plan` (`action`, `target` `{config: [hg, host, port]}` or `{rule_id}` or null, `value`) and `POST /api/live/proxysql/action` (as for Cluster), audited as `proxysql_action`, send timeout 60 s. Workspace (`ProxySqlPlace.tsx`, `proxysqlApi.ts`, `proxysql.css`; the report is owned by the shell so the sidebar can mute the item for the connection that was read, `proxySqlNav`, with the tooltip "This connection is not a ProxySQL Admin interface" only when the report of the connection in use says so): tabs Servers, Hostgroups, Query rules, Query digests (tables `proxysql.servers`, `.hostgroups`, `.rules`, `.digests` keep their column layout), the Configuration line with Load and Save on Servers and Query rules, an inspector per row with its changes in the action bar. Query digests reads with its own `useConnectionReads` only while the tab is on screen and the report is the connection in use's (`proxysql-digests:<filters>`, so a newer choice drops an older read); its tools row has the order (a Segmented control, or a menu below 1000 px), Top, Hostgroup, Schema, User and a search that reads after a 450 ms pause; a digest or rule filter is a chip; tiles are hidden on that tab. The place holds the filters and the moves between tabs: a rule from a digest (Query rules, selected), a digest from a rule (Query digests filtered by its digest, selected; shown from the report while its read runs), a rule's digests (the `rule_id` filter). Rules show Hits with **No hits** and Digests (`digest_count`, "+N maybe"). Tests: `test/test_proxysql.py` (FakeAdmin: ProxySQL Admin over SQLite with LOAD and SAVE copying tables, Admin's whole-text match, the client's `--xml` output and its "at line N", the lab's rules with `re_modifiers` CASELESS, its ten digests, `runtime_global_variables` and `runtime_mysql_users`), `test/test_proxysql_digests.py` (routing cases, the report's cross-check, every order and limit bounded in Admin, hex filters and the reset table never named, `more`, the rule scan, a 2.0.6-shaped table without `sum_rows_sent`, input validation, the endpoints, progress), `apps/workspace/test/proxysql.mjs` over fixtures written by `apps/workspace/test/proxysql-fixtures.py`.

#### Reviewed changes planned from a fresh read (Cluster, ProxySQL and Readyset)

`myflames/reviewed_writes.py` and `apps/workspace/src/ActionReview.tsx`. Plan: the module reads and plans (exact statements, `expect`, `typed_guard`, `facts`, `warnings`, `undo`, `changes`, `next`); `ui._action_response` adds `policy` from `dbops.decide(worst_kind(statements))` (every statement is risky, so Production and Development both confirm and Read-only answers `refuse`) and a review token over the plan without `review_token` and `policy` (`reviewed_writes.review_details`). Action: `check_reviewed` (a dict with an action and 1 to 10 single-line statements), the policy gate before any connection (refused and audited: `read_only`, `needs_confirmation`), the token consumer (missing, expired, used or changed: refused and audited as `review`), the saved password, then `execute`: plan again from a fresh read (not offered any more: refused `changed`), compare statements and `expect` (refused `changed`), the typed guard (refused `typed_guard`), read the saved policy again (`ui._action_response` `before_send`: a profile made Read-only or another type since the request refuses as `policy_changed`), consume the token, write the pending audit record (failure sends nothing), one `run_write` with the statements joined by newlines (`--comments --binary-mode`, stdin, no reconnect; batch mode stops at the first error), then: `done` with `after` (the state read again) and `next`. Only an error the client printed with its number decides a failure: `failed` when a server error (any number but the client's 2000 to 2999) names line 1, or when the client printed an error with no line, before it read any statement (it could not connect or log in; audit outcome `not_connected`); `partial` when a server error names line N > 1 (`sent`, `not_sent`). The mysql and MariaDB clients add "at line N" once they have read a statement (client/mysql.cc `put_info`). Everything else is `unknown` (audit `unknown`, outcome `timeout` or `no_reply`; never retried): a time limit, an interrupt, a client error at a line (2013, 2006), a line past the last statement, and a client that ended without printing an error number (killed or crashed after the server may have run every statement). Envelope `proxysql-action-1.0` / `cluster-action-1.0`: `{schema_version, action, status, message, statements, sent, not_sent, error, after, undo, next, audit_id, policy, states, effect, checks}`: `states` is each statement's final state (`done`, `failed`, `not_run`, `not_sent`, `unknown`; `done` only for statements the server answered), `effect` the rows `[what, before, now]` the module builds from the read before the send (captured in its fresh plan) and the read after (`proxysql._effect`: the changed field of the configuration row, Runtime, the rule in memory and runtime, memory versus runtime and disk; `cluster._effect`: Primary, the member's state, `wsrep_desync` and state, Component or Group mode), null when the read after failed or the outcome is not done; `checks` comes from the plan (`proxysql._checks`, `cluster._checks`: what to refresh and look at, and that the Audit log keeps it as Outcome unknown). Plans also carry `timeout_s` (60 for ProxySQL, 120 for Cluster). Progress (`reviewed_writes.Progress`, kept by `ProgressRegistry` on the workspace: at most 64, ten minutes after their last change): the action request may carry `progress_id` (16 to 64 of `[A-Za-z0-9_-]`; missing, invalid or reused means only that no progress is kept), and `POST /api/live/action/progress {progress_id}` (no connection, not in `_CONNECTION_PATHS`) returns `action-progress-1.0` `{phase received|checking|sending|reading|finished|unknown, states, status, elapsed_s, waiting_s, timeout_s}`: received (policy gate and token), checking (the fresh read and plan), sending (the pending audit record is written and every statement is `sent`: with the server, it may run), reading (the server answered; final states) and finished; a refusal finishes with `not_sent`, an exception after sending with `unknown`. No SQL, no credentials. The sheet: eyebrow (statement verb), question title, the description, the target, Connection (label with address), Type, the facts, Undo, the policy text, warnings, the statements exactly as sent, the differences (`changes`), a typed name field when needed; Cancel and Escape call `recordDeclined` (`proxysql_action` or `cluster_action`, the statements, the connection, the action, target and `expect`); a confirm the workspace stops itself (the saved connections cannot be read, or name another policy) posts the same to `/api/audit/declined` with `reason` `policy_unknown` or `policy_changed`, recorded as refused; a connection change closes and audits an open sheet; confirm re-reads the saved connections (`useWritePolicy().check()`) and refuses locally when the key changed; a reply below 500 means nothing was sent unless the last progress read was sending or reading; no reply or a 500 is Outcome unknown, never retried. After Confirm the same sheet stays (no second dialog): `confirm()` makes a 128-bit hex `progress_id`, polls the progress every 400 ms while the request runs, and the sheet shows the phase line (the seconds waited counted from the first sending snapshot, with `timeout_s`), each statement with its state (a spinner on Sent), the confirm button reading Sending… with `aria-busy` and duplicate clicks ignored. After ten seconds without a reply, Close and Escape can hide the sheet (`onLeave`) while the request continues; a pending line in the view retains its start time and connection label. A late reply replaces it with the result panel without reopening the sheet or resending the statements. On the answer: the outcome pill and message, the statements' final states, What changed (`effect`), What to check (`checks`, for unknown), the undo for partial or unknown, the Audit log line with Open the Audit log (the `myflames-open-audit` event with `audit_id`; `main.tsx` opens the Audit log with that record selected, `AuditLog focusId`), and ConfirmSheet's `doneLabel` mode: the next change (Save … to disk…) as secondary and Close focused, no Cancel; Close records nothing. The result panel in the view keeps the result (with an Audit log button). A planning error or a Read-only plan shows in the view and opens no sheet. Values (weight, hostgroup) are asked first in a small sheet with bounds and the current value; nothing is planned until Review.

### Readyset

The Readyset place reads a Readyset SQL adapter (`3307`) or Admin SQL (`6036`) listener (`myflames/readyset.py`, `myflames/topology_links.py`, `docs/schemas/readyset-1.0.json`; checked against the Readyset source at `6d2adde9b0`, `public/readyset-adapter`, `public/readyset-sql-parsing`, `public/nom-sql` and `public/replicators`, and live on `public.ecr.aws/readyset/readyset:latest-stable` behind ProxySQL 3.0.11 and in front of MySQL 8.4 GTID primary/replica in `scripts/readyset-matrix.sh`).

- Identity and port roles: `SELECT @@version_comment` equals `"Readyset"` (with `SHOW READYSET VERSION` and `SHOW READYSET STATUS` fallback); a non-Readyset server returns `available: false`, `is_readyset: false` with a calm message naming ports `3307` and `6036`, while client connection/login errors raise as in the other views. `port_role` is `"admin"` when connected on `6036` or when `database == "readyset"` and `SHOW DATABASES` exposes only internal schemas (`information_schema`, `public`, `readyset`); otherwise `"adapter"`. Capabilities follow Readyset's `extensions.rs` gates: `can_create_cache`, `can_explain_cache` and `can_export_caches` are true only on `"adapter"`, while `can_shutdown` (`ALTER READYSET SHUTDOWN [RESET]`) is true only on `"admin"`.
- Reads, all `SELECT`/`SHOW` over `--xml`: `SHOW READYSET VERSION`, `SHOW READYSET STATUS`, `SHOW READYSET TABLES`, `SHOW CACHES`, `SHOW SHALLOW CACHES`, `SHOW PROXIED QUERIES` (normalized into `proxied_queries` up to 200 rows), `SHOW SHALLOW CACHE ALLOWED FUNCTIONS|VARIABLES|SCHEMAS`, and `SELECT *` on the virtual telemetry tables in `readyset.*` (`query_log_stats`, `shallow_cache_stats`, `replication_lag`, `query_telemetry` when present). Replication lag resolves `lag_unit` to `"transactions"` for `mysql_gtid` and `"bytes"` for `mysql_file` / `postgres`. Severity (`status`): `problem` when a table snapshot failed or replication errored; `watch` when in Maintenance Mode, replication stopped, lag exceeds threshold, or `Replication Status == "Disabled"` while deep caches exist (`Disabled` stays `ok` when no deep caches exist); `ok` otherwise.
- Dry-run precheck and cache DDL export: `explain_cache(conn, query_or_id, cache_type)` (`POST /api/live/readyset/explain-cache`, `readyset-explain-1.0`, `_SERVER_VIEW_PATHS`) validates a single `SELECT` (`live.bind_query`) or `q_<hex>` query ID without mutating state, preserving whitespace inside single-quoted literals and backticked identifiers while normalizing unquoted whitespace, runs `EXPLAIN CREATE SHALLOW CACHE` and/or `EXPLAIN CREATE DEEP CACHE`, and checks `SHOW CACHES` + `SHOW SHALLOW CACHES` for duplicate query IDs or cache names (`collisions[]`). `export_caches(conn)` (`POST /api/live/readyset/export-caches`, `readyset-export-1.0`) runs `EXPLAIN ALL CACHES` and falls back to `EXPLAIN CACHES` on `ERROR 1064` (older Readyset releases prior to `ALL`), returning `statements[]` and joined `sql` without duplicate `USE` prefixes.
- Multi-tier topology (`myflames/topology_links.py`): `discover_readyset_tiers`, `discover_proxysql_tiers`, and `discover_database_tiers` cross-reference saved profiles (never reading or exposing passwords) to assemble end-to-end `ProxySQL -> Readyset -> MySQL/MariaDB (Standalone, Replicas, or Galera/PXC/InnoDB Cluster)` topologies across `Readyset`, `ProxySQL`, `Replication` and `Cluster`, with `Use <connection>` switching and cross-place navigation (`Open Readyset`, `Open ProxySQL`, `Open Replication`, `Open Cluster`). ProxySQL `"readyset"` hostgroup comment matching in `discover_database_tiers` only activates when ProxySQL already links to the inspected database tier (`links_db`).
- Reviewed changes (`available_actions`, `plan_action`, `execute_action` through `reviewed_writes`, audited as `readyset_action`, envelope `readyset-action-1.0`, send timeout 90 s):
  - Cache lifecycle: `create_cache` (`CREATE [SHALLOW|DEEP] CACHE [<name>] [WITH (ttl = Ns, refresh = Ms, coalesce = Kms | always)] FROM <query_or_id>;`; `ttl_seconds` bounded `1..31536000`, `refresh_seconds < ttl_seconds`, `coalesce_ms` `1..60000`; duplicate query ID or cache name across shallow/deep adds a warning and typed guard), `flush_cache` (`FLUSH CACHE <name>;`), `flush_all_shallow_caches` (`FLUSH ALL SHALLOW CACHES;`, typed `"SHALLOW"`), `drop_cache` (`DROP CACHE <name>;`, typed cache name), `drop_all_caches` (`DROP ALL [DEEP|SHALLOW] CACHES;`, typed scope word), `drop_all_proxied_queries` (`DROP ALL PROXIED QUERIES;`, typed `"PROXIED"`).
  - Dynamic parameters and allowlists: `set_log_level` (`ALTER READYSET SET LOG LEVEL '<directives>';`), `set_eviction_memory_limit` (`ALTER READYSET SET EVICTION MEMORY LIMIT <bytes>;`), `set_eviction_period` (`ALTER READYSET SET EVICTION PERIOD <ms>;`), `enter_maintenance_mode` / `exit_maintenance_mode` (`ALTER READYSET ENTER|EXIT MAINTENANCE MODE;`), `add_shallow_allowed` / `drop_shallow_allowed` (`ALTER READYSET ADD|DROP SHALLOW CACHE ALLOWED FUNCTION|VARIABLE|SCHEMA <ident>;`), `flush_privileges` (`ALTER READYSET FLUSH PRIVILEGES;`).
  - Tables, replication and admin shutdown: `resnapshot_table` (`ALTER READYSET RESNAPSHOT TABLE `[`schema`.]`table`;`, typed table name; warns that Noria's base-table reset drops dependent deep caches so cache DDL should be exported beforehand), `add_tables` (`ALTER READYSET ADD TABLES ...;`), `stop_replication` / `start_replication` (`ALTER READYSET STOP|START REPLICATION;`), `set_replication_position` (`ALTER READYSET SET REPLICATION POSITION '<pos>';`, typed `"SET POSITION"`), and Admin-port-only `shutdown` / `shutdown_reset` (`ALTER READYSET SHUTDOWN [RESET];`, typed `"SHUTDOWN"` / `"SHUTDOWN RESET"`).
- Endpoints, CLI, MCP and Workspace: `POST /api/live/readyset`, `/api/live/readyset/explain-cache`, `/api/live/readyset/export-caches`, `/api/live/readyset/plan` and `/api/live/readyset/action`; CLI `python3 -m myflames readyset [--json] [--export-caches] [--explain-cache SQL_OR_ID] [--fail-on watch|problem]`; MCP `readyset_status(profile, detail)` (read-only); Workspace (`ReadysetPlace.tsx`, `readysetApi.ts`, `readyset.css`, lazy-loaded right after `ProxySQL` with sidebar mute `"This connection is not a Readyset instance"`): tabs Topology, Caches, Proxied queries, Tables, Dynamic parameters, Performance, plus the `CreateCacheSheet` live dry-run builder and `ExportCachesSheet`. Tests: `test/test_readyset.py`, `test/test_topology_links.py`, `apps/workspace/test/readyset.mjs` (fixtures from `apps/workspace/test/readyset-fixtures.py`), and live Docker matrix `scripts/readyset-matrix.sh`.

### Users and roles: privileges, accounts

Backend `myflames/privileges.py` (the one SHOW GRANTS parser, `has`, `requirements`, `can`, `read_privileges`) and `myflames/accounts.py` (`list_accounts`, `read_account`, `plan_change` and the `plan_*` wrappers, `apply`, `grant_diff`, `fingerprint`); endpoints `myflames/accounts_api.py` (dispatched by `ui._accounts_response`); CLI `myflames/accounts_cli.py` (dispatched from `cli.main`). Contract and evidence: the accounts skill and its references. Tests: `test/test_privileges.py`, `test/test_accounts.py`, `test/test_accounts_api.py`, `test/test_accounts_cli.py`, with live replays `test/fixtures/accounts/<server>.json` (MySQL 8.0.46, 8.4.11, 9.7.2, MariaDB 10.11.19, 11.4.13, recorded 2026-10-15; scenario in `PROVENANCE.txt`; helper `test/_accounts_replay.py`).

- `python3 -m myflames privileges [--json] [-o PATH] [-q]` and `POST /api/live/privileges {connection}` (`privileges-1.0`): one tagged client call (`/*myflames:privileges*/`) in a fresh session, `SELECT 'myflames_who', USER(), CURRENT_USER(), CURRENT_ROLE(), VERSION()`, plain `SHOW GRANTS` (MySQL merges the active roles and lists mandatory roles; MariaDB adds the active role's tree and PUBLIC) and `SHOW GLOBAL VARIABLES` for `partial_revokes`, `activate_all_roles_on_login`, `activate_mandatory_roles`, `mandatory_roles`, `read_only`, `super_read_only`, `server_uuid`, `server_id`, `hostname`, `port`; MySQL 8.0.19+ adds `information_schema.APPLICABLE_ROLES` for the default and mandatory flags. Never `mysql.*` or SHOW CREATE USER; credential clauses are stripped before parsing. An expired account raises error 1820/1862 with a hint. Shape: the `parse_grants` model (`engine, version, checked, fresh_session, partial_revokes, global, dynamic, global_grantable, static_grant_option, schemas[] {pattern, database, wildcard, privs, grantable, source}, restrictions[], tables[], columns[], routines[] {kind PROCEDURE|FUNCTION|LIBRARY|PACKAGE|PACKAGE BODY}, proxies[], roles[] {name, host, role, admin_option, default, active, mandatory, grantee}, unparsed[], grants[]` without credentials) plus `schema_version, account, user, current_roles, read_at, variables, session_scope, session_note, warnings, actions`.
- `actions`: `{key: {state allowed|denied|unknown, allowed true|false|null, available, missing[], need, reason, via, note, grey_out}}` for every key in `privileges.ACTION_KEYS`. `missing` lists the alternatives still missing (a name, or names joined with " and "; any one is enough), empty when allowed. Only an absent global or dynamic privilege gives `denied`; database, table, column and routine absence is `unknown` (rows of the same user name at another host or of the anonymous user are not in SHOW GRANTS); an unparsed line, an unreadable model or a role-graph question (`grant_role`) is `unknown`. MariaDB SUPER: covers the split privileges through 10.11 and before 11.0.1 (`dbops.MARIADB_SUPER_SPLIT_END`), READ_ONLY ADMIN before 10.11.1 (`MARIADB_SUPER_READ_ONLY_END`); after those, `unknown` (the grant's `version_id` is not printed). MySQL SYSTEM_USER: account actions take `target_system_user` (a direct grant of the target; roles do not count) and KILL the target session's flag (unknown when not given). Kind `write` actions are `denied` under `super_read_only=ON` or MariaDB `read_only=NO_LOCK_NO_ADMIN`, and need `read_only_bypass` under `read_only=ON`. `available: false` (engine or version lacks it) means do not offer the action; `grey_out` is true only for an exact denial of a non-read. `can(model, key, **target)` takes the target fields (`database`, `table`, `columns`, `routine`, `kind`, `proxy`, `privileges`, `level`, `variable`, `own_user` for KILL, `own_account` for passwords and default roles, `target_system_user`, `roles`, `definer`, `data_directory`); `privileges.ALIASES` maps the dbops operation names (`replication`, `write_when_read_only`, `create`, `alter`, `drop`, `index`, ...) to keys.
- Action keys (kind; MySQL 8.4 need; MariaDB 11.4 need; "on db.*" is a database-level check that can only be unknown when absent):

| Key | Kind | MySQL | MariaDB |
|---|---|---|---|
| `privileges`, `health`, `config`, `config_sources`, `sessions_own`, `table_lists`, `database_picker` | read | nothing | nothing (`config_sources`: the option file path needs FILE) |
| `top_statements`, `waits`, `io`, `instruments`, `indexes_usage` | read | SELECT on performance_schema.* | same |
| `sessions_all`, `innodb_status`, `tablespaces` | read | PROCESS | PROCESS |
| `error_log` | read | SELECT on performance_schema.error_log | nothing (a verified local file is read, never through SQL) |
| `explain_connection` (another user's connection; your own user name needs nothing) | read | every static global privilege but GRANT OPTION | PROCESS |
| `locks_transactions` | read | PROCESS and SELECT on performance_schema.* | PROCESS |
| `locks_metadata` | read | SELECT on performance_schema.* | nothing |
| `replication_status`, `binlog_status` | read | REPLICATION CLIENT or SUPER | SLAVE MONITOR; BINLOG MONITOR |
| `relay_events` | read | REPLICATION SLAVE | SLAVE MONITOR |
| `routine_body` | read | global SELECT or SHOW_ROUTINE (8.0.20) | SHOW CREATE ROUTINE (11.3) or SELECT on mysql.proc |
| `view_definition` | read | SELECT and SHOW VIEW | SHOW VIEW |
| `accounts_list`, `grants_other` | read | SELECT on mysql.user | SELECT on mysql.global_priv; SELECT on mysql.* |
| `explain`, `run_analysis` | action | the statement's privileges (SELECT) | same |
| `kill` (another user's session or query), `kill_own` | action | CONNECTION_ADMIN or SUPER (plus SYSTEM_USER for a SYSTEM_USER session); own user name nothing | CONNECTION ADMIN; own user name nothing |
| `set_global`, `set_persist`, `set_persist_only`, `reset_persist`, `set_session_restricted` | action | `dbops.required_privileges` | same; PERSIST forms not available |
| `replica_start_stop`, `skip_gtid`, `skip_counter` | action | REPLICATION_SLAVE_ADMIN or SUPER; GTID_NEXT privileges and START REPLICA; SYSTEM_VARIABLES_ADMIN or SUPER and REPLICATION_SLAVE_ADMIN or SUPER | REPLICATION SLAVE ADMIN; not available; SUPER and REPLICATION SLAVE ADMIN |
| `clone_donor`, `clone_recipient`, `backup_lock` | action | BACKUP_ADMIN; CLONE_ADMIN (BACKUP_ADMIN with DATA DIRECTORY); BACKUP_ADMIN | not available |
| `dump_shell`, `dump_mysqldump`, `dump_mariadb`, `restore` | action, write | see action-privileges.md (SELECT, LOCK TABLES, TRIGGER, ...) | `dump_mariadb` only |
| `insert`, `update`, `delete`, `create_table`, `alter_table`, `create_index`, `create_view`, `drop_table`, `truncate_table` | write | the object privilege (CREATE VIEW and SELECT; TRUNCATE needs DROP) | same |
| `create_temporary_table`, `analyze_table`, `optimize_table`, `flush`, `instruments_presets` | action | CREATE TEMPORARY TABLES; SELECT and INSERT; RELOAD; UPDATE, INSERT, DELETE on performance_schema.* | same |
| `read_only_bypass` | action | SUPER or CONNECTION_ADMIN | READ_ONLY ADMIN |
| `create_user` | write | CREATE USER or INSERT on mysql.* | (CREATE USER or INSERT on mysql.*) and UPDATE on mysql.* (the SET PASSWORD step) |
| `create_role`, `drop_role` | write | CREATE ROLE or CREATE USER; DROP ROLE or CREATE USER | CREATE USER or INSERT on mysql.*; CREATE USER or DELETE on mysql.* |
| `drop_user`, `rename_user`, `lock_account`, `unlock_account`, `expire_password`, `account_options` | write | CREATE USER or DELETE (drop) / UPDATE on mysql.* | same |
| `set_password` (another account), `set_own_password` | write | CREATE USER or UPDATE on mysql.*; nothing (same user and host) | UPDATE on mysql.* (CREATE USER does not suffice); nothing |
| `grant`, `revoke` | write | GRANT OPTION (with named privileges: each privilege with GRANT OPTION at the level; dynamic ones or UPDATE on mysql.*) | same |
| `grant_role`, `revoke_role`, `default_roles` | write | ROLE_ADMIN or SUPER or WITH ADMIN OPTION; CREATE USER or UPDATE on mysql.* (own account nothing) | WITH ADMIN OPTION (unknown otherwise); UPDATE on mysql.* (own account nothing) |

- `myflames accounts list` and `POST /api/accounts/list {connection}` (`accounts-1.0`: `engine, version, accounts[] {name, host, plugin, locked, expired, password_last_changed, password_lifetime, no_password, role, system, public (MariaDB)}, role_grants[] {role, grantee, admin_option}, default_roles[] {account, role}, warnings, complete`): MySQL `mysql.user` (`authentication_string = ''` compared on the server), `role_edges`, `default_roles`; MariaDB `global_priv` read key by key with `JSON_VALUE` (never `Priv`; PUBLIC is `User='PUBLIC'`, `Host=''`; expired is `password_last_changed = 0`), `roles_mapping`. Without SELECT on mysql: the connected account only, `complete: false`.
- `myflames accounts show ACCOUNT [--role]` and `POST /api/accounts/read {connection, user, host, role?}` (`account-1.0`: `name, host, role, engine, version, exists, grants[], partial_revokes, model, roles[], default_roles[], attributes {locked, expired, plugins, limits, password_options}, role_grants[], effective_model, dangerous[], protected, system, warnings, fingerprint`): SHOW GRANTS FOR (1141 means `exists: false`), SHOW CREATE USER (MySQL 8.0.17+ with `print_identified_with_as_hex = ON` in the same call, every row joined because MariaDB prints a second `ALTER USER ... PASSWORD EXPIRE INTERVAL` row; parsed into attributes and discarded), MySQL default roles from its `DEFAULT ROLE` clause, MariaDB from `SET DEFAULT ROLE`, and SHOW GRANTS FOR every role reached (at most 256). `fingerprint` is SHA-256 over the credential-free grants (order ignored), roles, default roles and attributes: the plan's `expect`.
  CLI account show/grant commands diagnose a missing MariaDB user that matches a role and suggest `--role`; `show --json` adds `role_hint` to its missing-account result. Denied role reads do not assert that a role exists. MySQL roles share the account namespace and need no extra probe. Regression coverage: `test/test_accounts_cli.py`.
- `POST /api/accounts/plan {connection, operation, user, host, role?, ...}` (`account-plan-1.0`): reads only. `operation` create, drop, rename (`new_user` or `new_name`, `new_host`), lock, unlock, expire, password (`password` or `random_password`, MySQL `current_password` for REPLACE), grant and revoke (`privileges[]`, `level` global|database|table|column|routine|proxy, `database`, `table`, `columns[]`, `routine`, `kind`, `pattern`, `proxy {name, host}`, `grant_option`), roles (`mode` grant|revoke|default, `roles[] {name, host}`, `admin_option`, `special` ALL|NONE), options (`limits`, `expire`, `password_options`). `password` is accepted only for create and password and never echoed. Response: `schema_version, plan_id (= change_set_id), operation, request (the non-secret request), name, host, role, engine, version, account, action, before (account-1.0), expect, connection_identity, actor, actor_server, statements[] (passwords as '[redacted]'), warnings[], guards {blocked[], typed_name, self_lockout, protected, after_management}, privileges (the can() answer for the actor), effects {sessions[], definers[], warnings[]} (drop, rename), dependencies[] (granted roles), generates_password, classification, policy {action, tone, title, text, connection_type, read_only}, status ready|blocked`. Plans live on the workspace server as `_account_plans` (request and redacted statements only, no secret; 30 minutes, 200 at most per server, single use). Non-server callers of the plan helpers use a separate fallback store. Apply claims the plan atomically before opening a connection; a missing typed name restores it with its original expiry. Policy refusals leave it usable.
- `POST /api/accounts/apply {connection, plan_id, expect, confirmed, typed_name?, password?, current_password?}` (`account-change-1.0`, in `ui._WRITE_PATHS`): `dbops.gate` on the connection's policy first (account changes are risky: a read-only connection refuses and nothing is opened, Production and Development need `confirmed: true`; the refusal is audited and returned with `audit_id`), then the saved password, then a fresh account fingerprint is compared with both `expect` and the reviewed fingerprint before rebuilding the plan. A dropped or newly created account, or revoked privileges that invalidate planning, returns `status: stale` with audit `status: refused, outcome: stale`. Drift during a failed rebuild is checked again; unchanged invalid input remains an error. Rebuilt statements must equal the reviewed ones; `accounts.apply` then re-checks the connection identity, server version and identity, actor (`read_privileges`), every granted role's fingerprint, the account against `expect`, the rename destination, the guards and the privilege answer, and sends each statement with `connector.run_stdin` (stdin, `dbops.WRITE_CLIENT_FLAGS`, `--skip-reconnect`, empty temporary HOME, allowlisted environment), stopping at the first error. Response: `schema_version, change_set_id, operation, account, status, message, statements[] {index, statement, status ok|denied|refused_by_server|interrupted|unknown|not_sent, error, message}, before, after, diff {added[], removed[], levels, attributes_before/after, defaults_before/after, exists_before/after}, warnings[], audit_ids[], generated_password (once, MySQL RANDOM PASSWORD or the MariaDB generated one)`. Statuses: `applied`, `partial` (a later statement failed; a MariaDB create that stopped at SET PASSWORD leaves the account locked), `refused`, `needs_confirmation`, `needs_typed_name` (the plan stays usable), `stale`, `denied`, `refused_by_server`, `interrupted`, `unknown`. One `AuditTrail` record per statement (operation `accounts.<operation>`, `target.change_set_id`); refusals one finished record; a cancelled sheet calls `/api/audit/declined`. Server text is kept only for errors that never quote the statement (`accounts._SAFE_ERROR_TEXT`), redacted; others by number.
- Workspace batch review (`Accounts.tsx`, `accountsApi.ts`): compact compatible edits into groups and plan every group sequentially, retaining errors beside their group. One sheet lists every exact statement, warning and guard; an error or blocked plan disables Apply until fixed or removed. Typed-name fields are deduplicated by account. Apply re-plans each group immediately before its one-shot apply, using that fresh plan's `plan_id` and `expect`; it re-checks the connection before planning and sending. New statements, warnings or typed/self-lockout guards stop the batch for another review. Stop at the first refusal, partial result, failure or lost response; show every group's applied/failed/not-sent status, then refresh the list, selected account and privilege model. Earlier groups are not rolled back. Cancel, Escape and removal each post one declined audit per planned group; backend apply retains per-statement audit records. Browser suite `accounts.mjs` and its `--unit` adapter tests cover batch ordering, fresh fingerprints, per-group errors, cancel audits and stopping.

- Statements (one account, one level, one role per statement; tag `/*myflames:accounts*/`): names backquoted with doubled backquotes (MariaDB roles without host, `PUBLIC` bare); database grants escape `_`, `%` and `\` unless `pattern` (MySQL with `partial_revokes` ON matches literally and refuses patterns); privileges from the allowlist per engine, version and level (MySQL dynamic names from `SHOW PRIVILEGES`); passwords are doubled-quote literals inside the statement's own `NO_BACKSLASH_ESCAPES` session (`_with_password_mode`, restored after; verified live with a password ending in a backslash on all five servers with the mysql 8.0 client), NUL and line breaks refused. MySQL: `CREATE USER a IDENTIFIED BY 'x' | RANDOM PASSWORD [ACCOUNT LOCK]`, `ALTER USER a IDENTIFIED BY ... [REPLACE 'current']` (binary log: the hash, checked live). MariaDB New user: `CREATE USER a ACCOUNT LOCK`, `SET PASSWORD FOR a = PASSWORD('x')` (binary log: the hash), `ALTER USER a ACCOUNT UNLOCK`; with an active `PASSWORD VALIDATION` plugin (`information_schema.PLUGINS`) the first step is `CREATE USER a IDENTIFIED BY '<32-character random>' ACCOUNT LOCK` because the plugin refuses an empty password with 1819 (live on 10.11 and 11.4 with simple_password_check: the binary log shows only the temporary password, the typed one is validated by SET PASSWORD, a weak one leaves the account locked). Every MariaDB password plan warns that only the binary log is protected (the general and slow logs keep the typed text, checked live). Default roles: MySQL `ALTER USER a DEFAULT ROLE ...`, MariaDB `SET DEFAULT ROLE r FOR a` (one role or NONE).
- Guards (`accounts._guards`): system accounts are read-only; unparsed target grants block; MySQL blocks a target with a direct SYSTEM_USER grant when the actor lacks it (live: 1227; a SYSTEM_USER only through a role does not block, live: allowed). The typed name (`typed_name`, CLI `--confirm-name`) is required for drop, rename, lock and admin revokes of protected accounts (root, SYSTEM_USER, ALL, SUPER, CREATE USER or GRANT OPTION on `*.*`, roles included) and for any self-lockout (drop, rename, lock or expire your own account, or a revoke after which your own account no longer passes `create_user`, `account_options`, `drop_user` or `grant_role`). Drop and rename list sessions and DEFINER objects with the engine's consequence (MySQL 4006 unless ALLOW_NONEXISTENT_DEFINER; MariaDB 1449 through 11.8, 1446 from 12.0.1, 1045 for view callers without SET USER or SUPER, seen live on 10.11 and 11.4).
- `myflames accounts create|drop|rename|lock|unlock|expire|password|grant|revoke|role ...`: policy from `--profile` before any prompt or connection; the new password from a double prompt or `--password-stdin` (first line; `--current-password` reads the second). With `--random-password --current-password`, prompt only for the current password, or add `--password-stdin` with an empty first line and the current password on the second; missing current-password input refuses before connecting. MySQL statements containing a typed new or current password use the temporary `NO_BACKSLASH_ESCAPES` wrapper and restore the original session mode; the plan on stderr, `[y/N]` or `--yes` (non-TTY without `--yes`: refused, exit 2, audited); `--confirm-name` or a prompt for the typed name. Exit 0 applied, 1 a sent change was denied, refused by the server or partial, 2 an unsent refusal or decline, bad input, connection failure or unknown outcome. `--json` prints account-change-1.0 for executed changes; unsent failures print only a stderr diagnostic and leave stdout and the output file untouched. All account changes accept `-o/--output PATH` for text or JSON, and `-q` suppresses file-write notes and, with `--yes`, the plan description. Ctrl+C or EOF at a credential or confirmation prompt is audited as declined and exits 2. `accounts role --help` lists grant, revoke and default; each action has its own `--help`.
- Shared helpers fixed alongside: `dbops.strip_credentials` (every IDENTIFIED clause, including `AS 0x...` and `AND IDENTIFIED` factors), `dbops.read_grants` returns credential-free lines, `dbops.parse_grants` is a view of `privileges.parse_grants` (literal names in `schemas`, wildcard patterns apart in `patterns`, PACKAGE/LIBRARY kept in `model`, `unparsed` listed), `dbops.redact` covers `AS 0x...` and no longer treats a quoted plugin name ending in `password` as a PASSWORD option, `connector.MySQLConnection.run_stdin` sends SQL on stdin.

### MySQL Shell

`import-shell --list` honors `-o/--output` and `-q`, as does `advise --list-rules` (text or JSON).

`python3 -m myflames import-shell BUNDLE.zip [--list] [--query N]` converts a `collectSlowQueryDiagnostics()` zip (`myflames/shell_import.py`) to capture-1.0; `cli._read_plan_source` makes every plan-reading command accept the zip. The Shell plugin lives in `contrib/mysqlsh-plugin/` (tests: `test/test_mysqlsh_plugin.py`).
### Privilege catalogs and adding scopes

Account read responses include `privilege_catalog[level]` entries with `name`,
`description`, `dynamic`, `grantable_here`, and `dangerous`, plus
`privilege_catalog_meta {engine, version, source}`. `SHOW PRIVILEGES` tells
myflames which privilege names exist. Its Context text does not determine legal
levels: those come from myflames' source-verified engine/version tables. A
failed catalog read falls back to the verified built-in names and warns that
plugin privileges may be missing. `grantable_here` means legal at this level;
it is not evidence that the connected account can grant it.

`grant_scopes` retains direct and inherited scopes, plus Global and visible
database scopes without grants. Each scope has a stable opaque `id`, `label`,
`level`, scope fields, `system`, and `cells`. A cell has `privilege`, `direct`,
`grantable`, and optional `via`/`restricted`. Inherited and broader-scope grants
remain display-only. Standalone GRANT OPTION remains separate from normal
catalog checkboxes. Existing grants remain visible even when metadata listing
cannot see their objects.

`POST /api/accounts/scope-objects {connection, database}` returns `database`,
`tables`, `procedures`, `functions`, `truncated`, and `warnings`. Name objects
have `{name}`. Tables include views. The combined list is capped at 500, with
bounded metadata queries; unavailable lists permit exact manual entry. Columns
remain manual. The UI's **Add scope** supports literal databases, tables,
columns, procedures and functions, with explicit pattern entry. MariaDB packages
and package bodies and MySQL 9.2+ MLE libraries accept exact names; object lists
still discover procedures/functions only. Package GRANT/REVOKE scripts append
ORACLE to the existing session SQL mode and restore its exact original value,
with the wrapper included in review and audit. Anonymous proxy accounts use an
empty user name and an explicit host. Unknown actor authority stays explicit.
Server/component availability remains
authoritative; unsupported routine kinds remain visible without editing controls. Scope identity
is structural, so adding an existing scope does not duplicate it. New scopes
and unticked privileges join the same pending-change batch review as revokes.

Account requests have a 256 KiB encoded-data budget and are validated before
copying nested data or looking up secrets. Reviewed plans are limited to
512 KiB estimated retained objects each, 8 MiB total, 200 plans, 30 minutes,
and 200 statements per plan. Input validation bounds nesting to 32 and nodes
to 10,000. Internal plan, dependency, fingerprint, and apply reads omit editor
catalog metadata. Plans remain single-use and bound to the actor, server,
connection, account and role dependencies.


### Reviewed SELECT projections and server actions

`POST /api/analysis/prepare-query {query}` calls `analysis_query.prepare` without connecting. The result is `{sql, converted, warning, problem}`. The bounded parser accepts one statement, strips supported EXPLAIN wrappers, and projects simple single-table UPDATE/DELETE or an INSERT/REPLACE SELECT source. It refuses ambiguous escaping, unsupported forms, state-changing/waiting functions and anything rejected by `capture_problem`. UPDATE assignments and their subqueries are omitted; this is not an equivalent write execution plan. The editor preserves original SQL, opens a separate SELECT tab and requires a second explicit capture action. Top statements reviews converted SQL individually; batch analysis skips converted rows.

Top statement details use the full available width, preserving the mounted list's sorting, filtering and scroll when returning. Optional Optimize table and Instruments runtime Apply actions use exact SQL review and editor confirmation, including on Development connections. The UI's `require_confirmation` request strengthens the existing write policy; it cannot authorize a refused operation. Running jobs retain progress and Stop, and successful completion refreshes the source view. CLI Tables and Instruments collection remains read-only.

### Database diagram

The view is the **Database diagram** place (SQL workspace group, after Query
digest; `place="diagram"` of `Server.tsx`, so Server's Refresh all and its
diagram state are shared; opening the place does not drop a Refresh all read
on its way). Frontend implementation is
`SchemaDiagram.tsx`, `schemaDiagramModel.ts`, and `schema-diagram.css`; the helper
name avoids a case-insensitive filesystem collision with the component.
The view toolbar's Refresh is the one rebuild action; database selection
stays beside it, with Import and Export. `relationshipPath` in
`schemaDiagramModel.ts` breaks a horizontal line where it crosses another
relationship's vertical line, so crossings do not read as junctions. Canvas
and SVG export use that same helper.
`test/schema-diagram.mjs` covers browser behavior (`npm run test:diagram`), and
`apps/workspace/test/mermaid-export.mjs` parses and renders the export cases with
the real Mermaid library.

The canonical model has `schema_version: "schema-diagram-1.0"`, `tables`,
`relationships`, `warnings`, optional `incomplete`, and optional
`layout.positions` keyed by table ID. Tables contain `id`, `schema`, `name`,
columns with `name`, `data_type`, nullable state and primary-key flag, optional
indexes, SQL definition, and an external-reference flag. Relationships contain
IDs, names, source/target table IDs and ordered column mappings, with optional
supplied cardinalities, identifying status, and update/delete actions. Empty
column mappings represent conceptual relationships, not executable foreign keys.
Do not infer cardinalities from table names or invent foreign-key columns.

`POST /api/schema-diagram/read {connection, database}` reads one selected
schema. The view obtains database names from the connected server, accepts a typed name when needed, and uses the view toolbar's Refresh to rebuild the diagram. Matching table IDs retain layout; removed tables disappear and new metadata replaces the previous model. Replacing imported or different-source content requires confirmation. Connection switches invalidate in-flight reads and stale replacement confirmations. `POST /api/schema-diagram/import {format, text}` parses SQL, Mermaid, or
JSON without a database connection. `POST /api/schema-diagram/export
{format, diagram}` generates SQL or Mermaid as `{text, warnings}`. All routes
use the normal authentication boundary. Imports and exports never execute SQL.
Text and model input are capped at 2 MiB; the model caps are 200 tables,
10,000 columns, 1,000 relationships, and 20,000 index key parts. SQL parsing is
bounded to 100,000 tokens, 64 nesting levels, and 2,000 statements. Live reads
use four schema-scoped metadata queries sharing a 30-second budget. The frontend validates the model before rendering and
rejects invalid duplicates, edges, and sizes. Layout coordinates must be finite
and are bounded to the canvas range.

SQL import supports CREATE TABLE and ALTER TABLE ADD keys/foreign keys.
Unsupported clauses and statements produce warnings; this is not a general SQL
parser. Mermaid import accepts a bounded erDiagram subset with entities,
aliases, attributes, symbolic cardinalities, and reads column names and
nullability back. Conceptual relationships never fabricate executable SQL
foreign keys. SQL export revalidates retained original DDL; without reusable
DDL it emits a structural subset with warnings. Mermaid export uses Mermaid
entity escaping (`#quot;`, `#39;`) rather than HTML entities, readable sanitized
types (`int_unsigned`, `decimal(15-5)`), `PK`, `FK` and single-column `UK`
markers, readable column aliases with the original name in a comment, and
comments only where information would otherwise be lost (enum or set values,
renamed columns, unknown nullability). Nullable attribute types use Mermaid
11.16 or newer (`?` suffix). JSON keeps the model and layout without claiming
SQL round-trip fidelity.

A **Diagram | Mermaid** switch toggles between the interactive canvas and the
Mermaid view. The Mermaid view shows the rendered diagram (`mermaid` 11.17.2,
pinned in `apps/workspace/package.json`, loaded on first use as separate chunks
so the main bundle does not grow; `securityLevel: "strict"`; follows light and
dark; zoom in, zoom out, fit; render errors shown inline) beside the code with
**Copy**, **Download .mmd** and the export notes.

The canvas supports search, selection, inspection, orthogonal links, drag/pan,
zoom, Fit and auto layout. Table keyboard arrows move 16 pixels, Shift moves 64,
and Enter opens details. Canvas arrows pan, plus/minus zoom, and zero fits.
Position inputs and Reset table position provide alternatives to dragging.
Conceptual links are dashed; supplied Mermaid cardinalities remain explicit.

JSON and SVG export locally. JSON preserves canonical metadata and positions;
SVG contains generated geometry and escaped text, without scripts, event
handlers, image URLs, or foreignObject. SQL/Mermaid export warnings open
**Review export notes**, including a collapsed preview, before Download file
is permitted. Cancel sends no download. The in-memory model is not part of
session restore. Refresh all reloads live diagrams for their original database
and preserves positions. Before the first build it can use the selected database;
imported diagrams are excluded.

### Schema versions

Save one database's schema on this computer and compare any two schemas: a
saved version, a `schema-snapshot-1.0` file, or a database read now through a
connection. A comparison has a source (the schema to match) and a target (the
schema that would change); its sync script changes the target to match the
source. Python writes every sentence (`change.text`, `summary`, the script);
the workspace lays them out. myflames never runs the sync script: Open in
Query editor hands it to the editor, which runs it with its own
confirmations. Modules: `myflames/schema_versions.py` (capture, validation,
checksum, comparison, text and Markdown), `myflames/schema_sync.py`
(the sync script), `myflames/schema_ddl.py` (splitting `SHOW CREATE` output),
`myflames/schema_versions_api.py` (workspace endpoints),
`myflames/schema_versions_cli.py` (`myflames schema`) and the
`schema_version_*` methods of `WorkspaceStore`. Contracts:
`docs/schemas/schema-snapshot-1.0.json`, `schema-compare-1.0.json`,
`schema-version-list-1.0.json`.

**Reading a database** (`capture(conn, database, source)`) is read-only and
bounded by `READ_SECONDS` (120) for the whole read. `information_schema` and
`performance_schema` are refused; above `MAX_TABLES` (2,000, the
`ALL_SCHEMAS_TABLE_LIMIT`), `MAX_OBJECTS` (5,000) or `MAX_COLUMNS` (200,000
text columns) it refuses. Statements, all tagged `/*myflames:schema-versions*/`:

1. `SHOW GLOBAL VARIABLES` for version, version_comment, hostname,
   lower_case_table_names and the `SESSION_SETTINGS` variables.
2. One `conn.run` with the inventory: `SCHEMATA`, `TABLES` (BASE TABLE,
   SYSTEM VERSIONED, VIEW, SEQUENCE), `ROUTINES` (PROCEDURE, FUNCTION,
   PACKAGE, PACKAGE BODY), `TRIGGERS` (with timing, event and
   `ACTION_ORDER`), `EVENTS`, and `COLUMNS` with a collation (the effective
   character set and collation of text columns), each row led by a one-letter
   marker. Other types are named in `notes`; MySQL 9.2+ JavaScript libraries
   are not read.
3. `privileges.read_privileges` for `coverage` (below).
4. One `conn.run_script` (stdin, `--force --skip-reconnect --comments
   --binary-mode`): `USE information_schema`, `SET SESSION` of each
   `SESSION_SETTINGS` entry the server has (`sql_mode = ''`,
   `sql_quote_show_create = 1`; MySQL `show_create_table_verbosity = OFF`,
   `show_gipk_in_create_table_and_information_schema = ON`, from 8.0.18
   `show_create_table_skip_secondary_engine = OFF`), then `SHOW CREATE
   DATABASE` and one `SHOW CREATE <type>` per object. The default database and
   sql_mode change the printed text (`SHOW CREATE VIEW` drops the view's own
   database name when it is the default; ANSI_QUOTES and MariaDB's
   NO_*_OPTIONS and ORACLE modes change quoting and options), so both are
   fixed. Rows map to the statements that did not fail, in order; failures
   come from `ERROR n (state) at line N` on stderr. A row count that does not
   match, a failed SET line, or a client error 2000-2999 fails the read.

Per object: tables strip `AUTO_INCREMENT=N` from the options line
(`schema_ddl.strip_auto_increment`, before any quoted option); views keep
`character_set_client` and `collation_connection`; routines, packages,
triggers and events keep `sql_mode` (and events `time_zone`) and the client
character set and collations. Errors 1146, 1360, 1539 (and 1049 for the
database) mean the object was dropped during the read: it is left out with a
note. 1305 from `SHOW CREATE PROCEDURE` is kept as unreadable, because the
server answers it for a routine the account cannot see too
(mysql-server sql/sp.cc:1705-1713, mariadb-server sql/sp.cc:2039-2048). 1044,
1142, 1143, 1227 and 1370 are "The account cannot read it" with the server's
words; a NULL routine definition says which privilege shows it (`ROUTINE_HINT`:
the definer, SHOW_ROUTINE or global SELECT on MySQL; the definer, SHOW CREATE
ROUTINE from 11.3 or SELECT on `mysql.proc` on MariaDB). An unreadable object
has `ddl` null and `reason`.

`coverage` (tables, routines, triggers, events): `complete` when SHOW GRANTS
proves every object of that kind is listed, else `unknown` with the privilege
that would. Tables: any table privilege on the database (MySQL
`TABLE_OP_ACLS`; MariaDB `TABLE_ACLS`, which adds GRANT OPTION and DELETE
HISTORY). Routines: EXECUTE, CREATE ROUTINE or ALTER ROUTINE on the
database, else global SELECT or SHOW_ROUTINE (MySQL), SELECT on `mysql.proc`
or SHOW CREATE ROUTINE (MariaDB). Triggers: TRIGGER on the database. Events:
EVENT on the database. `privileges.has` never proves an absence below the
global level, so `unknown` means "may be incomplete", never "missing".

`checksum` is sha256 over each object's type, name, ddl and compared fields
(definers included), so equal checksums mean no difference.

**Comparing** (`compare(source, target, source_meta, target_meta,
ignore_definers)`): objects pair by namespace (tables, views and sequences
share `relation`; routines, packages, triggers, events and the database each
their own) and exact name, then without regard to letter case when that pairs
one object on each side (a `name` change; `target_name` keeps the target's
spelling). Each is `different`, `only_in_source`, `only_in_target`,
`identical` or `not_compared` (a side's definition could not be read;
`reason` says which). The DDL fields depend on the status: `only_in_source`
has `source_ddl`, `only_in_target` has `target_ddl`, `different` has both,
`not_compared` has both with null for the unreadable side, and `identical` entries omit both (with `changes`
and `target_name`). Tables
(`table_changes`) compare elements from `schema_ddl.split_table`: columns in
the source's order (`moved` when outside the longest common order), keys, foreign keys, checks, other constraints, MariaDB
periods, table options and partitioning. A column compares without the
CHARACTER SET and COLLATE that follow its type (`schema_ddl.column_body`),
because MySQL prints them from the column's history as well as its value
(`is_explicit_collation`, mysql-server sql/sql_show.cc:2533-2560), and with
its effective character set and collation from `COLUMNS` instead
(`collation` on the change). Other objects compare the definition without the
definer and with each side's own database name made neutral in backquoted
`db`. qualifiers, the definer (`definer_only` when nothing else differs;
`ignore_definers` leaves it out), `sql_mode`, an event's `time_zone`, and for
triggers their order among the triggers both sides share on the same table,
timing and event. A change is `different`, `only_in_source`,
`only_in_target` or `moved` with the `source` and `target` text; every change
has `label` ("column email") and `text` (the sentence the CLI prints), every
different object `summary` and a unified `diff` (`[op, text]`, from the
target's definition to the source's: `-` only in the target, `+` only in the
source). `notes` name different engines,
versions, database names and `lower_case_table_names`, and every side whose
coverage is not complete.

**Sync script** (`schema_sync.build(target, source, report)`, `sync` in the
report): statements that would change the target to match the source,
ordered by phase (`session`: `USE`; `database` (MySQL `READ ONLY = 0` first
when the target is a read-only database); `drop_objects`;
`drop_foreign_keys`; `drop_checks`; `drop_tables`; `create_sequences`;
`alter_tables`; `create_tables`; `add_foreign_keys`; `routines`; `views`;
`triggers`; `events`; `database_end`: `READ ONLY = 1` when the source is
read-only). Its header says "Changes the target" and "To match the source".
Tables only in the target go
before the ALTERs (a removed child's foreign key blocks changes to its parent,
and foreign key and MySQL check names are unique per database); new sequences
before them (a column default can call `NEXT VALUE FOR`). Each has `risks`
(`drops_data`, `may_change_data`, `checks_rows`, `drops_privileges`; these
codes are unchanged), `risk_details` (the sentence shape of **Risks** below),
`warnings` and `compound` (the script wraps it in `DELIMITER`). Rules:

- Checks that are removed or change are dropped first, one ALTER TABLE per
  table (`DROP CHECK` on MySQL from 8.0.16, `DROP CONSTRAINT` on MariaDB).
  Then one ALTER TABLE per changed table: dropped keys and columns; added and
  changed columns in the source's order (`AFTER` or `FIRST` only when a
  column has to move); keys and checks; then only the table options that
  differ (never ENGINE again; options the source lacks are reset by
  `_RESET_OPTION` or named as
  not generated; a changed `KEY_BLOCK_SIZE` warns that kept indexes keep
  theirs; `DATA DIRECTORY` and `INDEX DIRECTORY` are left to the user); then
  the new partitioning or `REMOVE PARTITIONING` at its end, without a comma
  (MySQL drops a column the old partitioning uses only in the statement that
  repartitions). A text column names its character set and collation (after
  MariaDB's `COMPRESSED` comment). On MySQL a second new FULLTEXT key is its
  own ALTER TABLE (InnoDB adds one per statement). A generated column that
  switches between STORED and VIRTUAL, or becomes (not) generated, is not
  generated (MODIFY refuses it).
- Foreign keys that are removed or change, and unchanged ones on or to a
  column whose type changes or a key that is dropped or changes, are dropped
  first and added after the tables; new tables are created without them.
- Views are created after routines (a view may call a function), in
  dependency order, `CREATE OR REPLACE` when they exist. MySQL routines are
  dropped and created (no OR REPLACE; DROP revokes routine grants while
  `automatic_sp_privileges` is ON); MariaDB uses `CREATE OR REPLACE`, which
  keeps them, and a replaced package gets its body again. Every routine,
  trigger and event is created under its own `sql_mode` (and an event under
  its `time_zone`), restored to the saved session values right after.
  Triggers of one table, timing and event are recreated in the source's order
  when a new or changed one must run before one that stays.
- The source's database name is replaced by the target's in copied
  definitions.
- Not generated (in `skipped` and as script comments): objects not compared,
  definitions the source could not read, changed sequences (use ALTER SEQUENCE so
  the next value is kept), system-versioning changes, generated-column
  storage changes, letter-case renames, tables whose definition could not be
  split. Warnings name possible table and column renames, other engines, a
  newer source server version, definers, backslash escapes (run without
  NO_BACKSLASH_ESCAPES), events with a fixed AT or ENDS time (not created or
  created disabled once it has passed) and named event time zones (need the
  time zone tables).

**Choosing objects.** Every entry has `key`, `namespace:name` with the name's
case kept (tables, views and sequences share `relation`, so a table replaced by
a view keeps one key); `--json` shows the keys `--only` takes.
`schema_sync.build` takes `chosen_keys`: absent or None means every entry
except `only_in_target`, so no object only in the target is dropped by
default (changes within a chosen object, such as a DROP COLUMN or a
replacement by another type, still are, with a `-- WARNING:` line and
Drops data); an empty list
means no statements; unknown, malformed or duplicate keys are input errors.
`sync` adds `chosen_keys` and `omitted: [{key, reason}]`, and the header counts
the omitted differences and the objects only in the target that were not
dropped. Changes inside a chosen table, dropped columns included, stay in with
their risk text. A chosen identical entry permits the dependency maintenance
it needs but is not itself a difference.

A choice never expands to unchosen objects. `schema_sync._Selection` computes
the target the choice keeps before `_Plan` writes anything, and planning is
tentative until planner skips and dependency closure reach a fixed point: an
unreadable object or a skipped table (one ungenerated column change is enough)
counts as kept whole, and no SQL from an earlier tentative plan is returned.
A kept table's incoming foreign key blocks a parent DROP, a column removal, a
type, character set or collation change, or a change to the index it uses. A
source foreign key must match the kept parent's columns, both tables must be
InnoDB, and the parent needs a full leftmost index on those columns (prefix,
expression-led, FULLTEXT, SPATIAL, VECTOR and HASH indexes do not count;
integer display widths and string lengths alone do not make types
incompatible); on MySQL 8.4 and later, or an unknown MySQL version, the kept
parent needs a complete PRIMARY KEY or UNIQUE key on them, since snapshots do
not record `restrict_fk_on_non_standard_key` and its default is ON. Table
names in these checks follow each snapshot's `lower_case_table_names` (0 keeps
case, 1 and 2 fold it); without it names are case-sensitive and a warning says
so. Source foreign key names are checked against kept target tables on both
engines (database-wide, so on MariaDB 12.1 and later, where names are per
table, this can withhold a change that would have worked), and CHECK names
across tables on MySQL only. Every change on a blocked table is withheld
together (no half-created table, orphan foreign key or foreign key dropped
without its replacement); foreign keys and known local view, routine, default
and sequence references propagate the block, while literals, comments and
other schemas do not, and dynamic SQL is not resolved. Trigger reorder groups
with an unchosen or unreadable member, a package replacement or drop that
would lose an unchosen or unreadable body, an unchosen read-only database
(never opened for DDL) and an unchosen source read-only option are skipped
too. Every skip is in `sync.skipped` with Python's reason, repeated as a
one-line script comment; `objects[].blocked_by` lists the original keys to
choose (empty when nothing blocks; comparisons built with `sync=False` may
omit it). The workspace shows Not generated and **Needs** with those names, and
never adds a blocker to the choice.

**Risks.** Entries and their changes carry `risks: {destructive: [],
data_dependent: [], rebuild}`, sentences written by Python; statements carry
the same shape as `risk_details`. Destructive: dropped objects, a replacement by another type that
drops the target, MySQL routines dropped and created, events and triggers
dropped and created, dropped columns. Data-dependent: PRIMARY or UNIQUE added,
NULL to NOT NULL, narrowing and other lossy conversions, changed character
sets or collations, FOREIGN KEY or CHECK added. Known integer, string, decimal
and float widenings, integer display widths, quoted SQL words and comments
create none, and ENUM member case is kept. Every emitted sentence sits right
above its statement as `-- WARNING:` (destructive) or `-- CHECK:`
(data-dependent) on one line; `sync.summary.warnings` counts those lines,
repeats on different statements included. Informational notes stay separate.
The workspace labels them **Drops data** and **May fail**.

**ALTER predictions.** `_predict_algorithms` runs once selection and planner
skips reach their fixed point and calls `alter_algorithm.predict` (see [Query
editor sessions](#query-editor-sessions)) once per emitted `ALTER TABLE` with
the target's original captured table DDL, the target engine and
`server_version`, and no `facts`. It never reads a server, never invents
hidden table facts and never uses the source's DDL; a later ALTER on the same
table still uses the original target definition, and a table the script
creates has no target definition, so its ALTER is not predicted. Each such
statement gets `algorithm` (`predicted`, `algorithm` INSTANT, INPLACE, COPY or
null, `rebuild`, `concurrent_dml`, `summary`, `reason`, `source`, `needs`,
`clauses`, `notes`; the notes keep the predictor's default-setting
assumptions and add "Uses the captured target definition; no live table facts
were read."). A known COPY or rebuild sets the statement's
`risk_details.rebuild` to "Copies the table (COPY)" or "Rebuilds the table in
place (INPLACE)" followed by "; writes continue." or "; writes wait until it
finishes."; an entry joins the unique sentences of its statements, and a
change gets one only when it maps to exactly one ALTER (a replacement split
over DROP and ADD keeps null). Null means no known copy or rebuild, or not
predicted; `algorithm` tells them apart. Reselection clears and recomputes
every value from the final plan. Each sentence is one more `-- WARNING:` line
(counted in `sync.summary.warnings`), and every emitted ALTER gets one
`-- ALGORITHM:` line after its WARNING and CHECK lines: `INSTANT`, the INPLACE
summary (with ", writes wait" when known), `COPY, writes continue` or `COPY,
writes wait`, or `not predicted (reason)`. No ALGORITHM= or LOCK= clause is
ever added. The workspace shows **Rebuilds table** (its title the sentence) in
Differences, change details and the sync review, and each emitted ALTER's
**Predicted** line with the shared `AlterPredictionWhy` disclosure in the
full-width detail. Endpoints, requests and CLI flags are unchanged; `myflames
schema compare … --sql` prints the new comments. Ordinary stored ADD COLUMN
is not guaranteed INSTANT offline (MySQL needs the hidden FTS and row-version
facts, MariaDB the hidden FTS fact), and INT to BIGINT needs other tables'
foreign keys, so those stay not predicted with their `needs`. Tests:
`test/test_schema_sync.py`, `apps/workspace/test/schema-versions.mjs` (the
Risk label and title, the Predicted line, clause reasons and sources, script
comments).

**Checks** (`myflames/schema_lint.py`; `schema_lint.lint(snapshot,
server=None)` returns `schema-lint-1.0`, `docs/schemas/schema-lint-1.0.json`):
the input is validated as `schema-snapshot-1.0`; the result has the captured
`database` and `server`, `findings` `{rule, status: "watch", object, column,
text, detail}` and `not_judged` `{rule, object, column, reason}`. `object` is a
type and name such as `table orders`; `column` is null, a column name, or the
ordered child columns of a foreign key joined by commas. Rules: `money_float`
(a FLOAT, DOUBLE or REAL column whose name suggests money, such as price,
amount, cost, total, balance, salary, fee, tax, payment or revenue; the name
is a heuristic, never proof), `utf8mb3` (the effective column character set
wins over a printed `utf8` alias), `myisam`, `zero_date` (a zero default on a
DATE, DATETIME or TIMESTAMP column; only MySQL gets the default NO_ZERO_DATE
and strict mode explanation), `reserved_word` (current words from
`information_schema.KEYWORDS` on a live MySQL 8.0.11 or later, where that
table first appears (mysql-8.0.11 `sql/dd/impl/system_views/keywords.cc`), at
most 10,000 rows, a row whose WORD is NULL skipped; future words from
`lifecycle.RESERVED_SINCE` (shared with the upgrade check: QUALIFY from 8.3.0,
mysql-8.3.0 `sql/lex.h`, TABLESAMPLE from 8.4.0, LIBRARY from 9.2.0, EXTERNAL
from 9.4.0; MANUAL and PARALLEL are keywords MySQL 8.4.11 accepts as unquoted
column names, so they are not listed), offline too, kept even when a newer connected
server already reserves them; Not judged on MariaDB, with one database-level
reason when the list cannot be read), `fk_index`, `fk_parent_key` (both
engines; only MySQL 8.4 and later gets the `restrict_fk_on_non_standard_key`
explanation) and `definer_missing` (only for a named connection whose host,
port and engine match the snapshot, using the complete accounts list). The
optional live context is `{connection, host, port}` from validated connection
options; a failed open passes `{connection: null, host, port, error}` and the
offline findings stay. Live reads share a 30 s deadline (`READ_SECONDS`) and
check the actual version and version comment. Missing live metadata,
unreadable definitions or accounts, a parent table in another database and
incomplete inventories are Not judged, never clean, and passwords are redacted
from failed-read reasons. No SQL is generated or sent to change a server.

**Comparison cache.** `/api/schema-versions/compare` accepts optional
`chosen_keys` and returns `comparison_id`. `/api/schema-versions/sync` takes
`{comparison_id, chosen_keys}` and returns a full comparison with the rebuilt
SQL and Markdown. Sync never reads a server or trusts DDL from the client: it
rebuilds from validated, compressed snapshots cached per `WorkspaceStore` in
memory (at most four comparisons and 64 MiB, ten minutes from creation; an
expired or evicted ID asks for Compare again, and IDs never work in another
workspace instance). Deleting a saved version does not change a captured
comparison. `/api/schema-versions/lint` takes `{id, connection?, database?}`
where `id` is a saved version, `latest` or `live` (`live` needs the connection
and database and reads without saving; a saved or latest version without a
connection is checked offline). The `ui.py` allowlist comes from
`schema_versions_api.PATHS`, so both endpoints are registered with it, and
`LIVE_PATHS` (save, compare, lint) are the ones that open connections.

Round trip, verified live on MySQL 8.4.11 and MariaDB 11.4.13 (2026-10-26)
and kept by `test/test_schema_versions_live.py` (Docker): running the script
on a copy of the target, in both directions, and reading it again gives no
differences except what is listed as not generated, including a removed child
table whose parent changes type, a check that moves to another table,
repartitioning that drops the old partitioning column, two new FULLTEXT keys,
a MariaDB sequence used as a column default, and a read-only MySQL target.

**Storage**: the store's `schema_versions` table (`id, saved_at, source_key,
number, name, note, summary, payload`), created on demand like
`investigation_summaries`; `payload` is zlib-compressed snapshot JSON, decoded
only by `schema_version_get` (bounded inflate, then `validate_snapshot`).
`number` counts per `source_key` (`host:port/database`, with the SSH target
after the port for a read through SSH: `127.0.0.1:3306 via deploy@bastion/shop`).
A read through SSH records `ssh_host` and, when given, `ssh_user` and
`ssh_port` in the snapshot's `server` (`schema_versions.read_source`; absent
for a direct read, so its snapshot and key are unchanged), and that SSH target
is part of the server's identity everywhere Schema versions matches a server.
`schema_version_save`
returns `same_as` when the latest version of that database has the same
checksum. `schema_version_find` resolves an ID, an ID prefix of 6 or more
characters, an exact name, or `latest` (of a source).

**Workspace API** (`schema_versions_api.py`, not in `_CONNECTION_PATHS`: each
live side fills its own connection's saved password): `/api/schema-versions/`
`list`, `get`, `update`, `delete`, `export` (`{filename, text}`), `import`
(`{text}`; the request body may be as large as a session save, but
`schema_versions.parse_file` refuses text over 36 MiB of UTF-8, which is
`MAX_SNAPSHOT_BYTES` (32 MiB) plus 4 MiB, and snapshot validation and storage
add their own bounds), `save` (`{connection, database,
name, note}`), `compare` (`{source, target, ignore_definers, markdown,
chosen_keys}`; a side is `{version: ID}` or `{live: {connection, database}}`;
`markdown: true` adds `markdown`; the answer has `comparison_id`), `sync`
(`{comparison_id, chosen_keys}`) and `lint` (`{id, connection?, database?}`),
as described above. Live reads use `query_timeout = READ_SECONDS`; errors
redact the password.

**Place** (`SchemaVersions.tsx`, `schemaVersionsApi.ts`,
`schema-versions.css`; mounted on first visit and kept hidden like Query
digest): Saved versions (table `schema.versions`; a Database choice in the
toolbar from `useSchemaCatalog` of the connection in use plus the databases
with saved versions, never a typed filter; inspector with the objects and
definitions, Compare with the database now, Compare with…, Export, Rename…,
Delete…; Save version… and Import… in the toolbar, Save starting with the
chosen database) and Compare (Source and Target each a saved version or a
database now through the connection in use or a saved connection with a
saved password, filled in on open: the target a database of the connection in
use, the source the newest version of that database; Ignore definers;
Differences table `schema.differences` with **Include in sync script** (a
checkbox per object; the header chooses every object, identical ones hidden
from the list included), Status, Type, Name, Sync script (what the script does
to the target, Excluded, or Not generated), Risk (Drops data and May fail
pills, then **Needs** and the objects to choose) and Details; a new choice
calls `/api/schema-versions/sync`, guarded by comparison identity and request
revision, and the last SQL stays greyed while it is pending or stale, with
Copy, Download, Open in Query editor and the exports off; a selected difference opens
over the whole view (`detailBackLabel` "Back to differences") with What
differs as a Source | Target table from each change's `label`, `source` and
`target`, and both definitions side by side from the unified `diff`
(`splitRows`: removed lines are the target's, added lines the source's,
unchanged stretches outside the hunks fold into a gap); Sync script with Copy
script, Download .sql and Open in Query editor, enabled only when the target
is a database read now through the connection in use's server (`kind`
`live`; a saved version is a past state; the same server is the same host,
port and SSH target, `serverMatch` in `schemaVersionsApi.ts`: 127.0.0.1:3306
through another SSH host, or with SSH on one side only, is refused with the
reason); Export Markdown, JSON or the sync script). Compare with the database
now uses the version as the source and the database as the target, through
the connection in use or a saved connection that reaches the same server by
the same rule, and the automatic source prefers the newest version of the
target's database from that server. Lint's definer check (`schema_lint`) also
requires the same SSH target. Opening the place on screen reads the saved list and
the database list of the connection in use (`useSchemaCatalog`,
`/api/live/schemas`) for the Database choice; a schema is captured only by
Save version or Compare, and Checks read live metadata only while open.
Refresh reads the saved list again. The
Compare tab has no Refresh: Compare is its one way to read again. A version
whose account could not see every kind of object has a May be incomplete pill
in its Name cell (words, never color alone). A saved version's inspector has
**Checks**, a disclosure that calls `/api/schema-versions/lint` with the
connection in use (when one is chosen) only when it is open, the inspector is
on screen and the view is on screen; Stop and Try again use the shared action
feedback, Not judged entries sit under Details, and changing the version or the
connection invalidates the read. Selecting a version alone never reads.

**Comparison badges and setup bar** (`compareShared.tsx`, `compare-shared.css`
imported only by `styles.ts`; shared by Schema versions, Parameters > Compare
and Users and roles > Compare). A comparison describes a relationship, never a
server's health. `ComparisonBadge` maps each status to a tone: Different
accent text and tint; Only in source, Only in target and Moved a neutral
outline on an opaque surface (so selection never lowers its text contrast);
Not compared a muted surface; Identical a faint surface with readable text.
These badges never use `is-ok`, `is-warn` or `is-danger`, an Identical summary
has no health marker, and schema definition line highlights use accent and
neutral surfaces. Drops data, May fail and Rebuilds table keep their risk
colors and meanings. `ComparisonSetup` keeps the setup open before the first
result; only a successful Compare collapses it into one bar (Source → Target,
Swap, Change…, Compare) at every width, and when that first result replaces
the setup, focus moves to the bar's Compare. Sync selection regeneration never
collapses it; a failure or Stop keeps the previous result and the setup as the
user left it. Change… carries `aria-expanded` and `aria-controls` and toggles
the setup; Escape inside the expanded setup collapses it and returns focus to
Change…. The bar keeps naming the sides the result came from (`result.sides`
in Parameters and Users and roles, the compared labels in Schema versions)
until Compare runs again, with their identity and read or capture stamps in
its tooltip and accessible text. Swap and Change… only change the chosen
sides and run nothing; the view then says "The chosen sides changed (next:
Source X → Target Y). Press Compare to update the differences." (Schema
versions too, for a changed schema choice). A connection switch keeps the
result greyed and the bar usable. A side's read error appears once, beside the
bar or inside the expanded side, and Manage snapshots and Refresh choices stay
reachable through Change…, their outcomes visible when the setup closes. Before
the first result Schema versions shows Source and Target as side-by-side
compact cards (also at 720 px), each role's hint in its label's `title`. No
endpoint, JSON schema, server read or write, CLI subcommand or flag changed.
Tests: `server-compare.mjs --unit` (all six badge words and tones, no health
classes, initial, collapsed and expanded setups), `server-compare.mjs` and
`schema-versions.mjs` in a browser (collapse, Change… toggling, Escape and
focus, Swap without reads, a failed Compare keeping the setup, badge classes,
strip position, visible rows and contrast at 720x600 and 1440x900 in both
appearances).

**CLI**: `myflames schema read|save|list|show|compare|edit|delete|export|import`
(`schema_versions_cli.py`); see the README command table. A compare side is a
saved version, `latest` (newest version of the other side's
`host:port/database`), `live` (connection flags and `-D`),
`profile:NAME[/DATABASE]` (a saved connection with its saved password) or a
`.json` / `file:PATH` snapshot: `myflames schema compare SOURCE TARGET`, and
`--sql` prints the sync script that changes TARGET to match SOURCE. It leaves
out objects only in the target unless `--include-drops` includes every object;
repeatable `--only KEY` chooses exactly the objects named (objects only in the
target included when named); the two are mutually exclusive.
`--fail-on any` exits 1 when the sides differ, `--fail-on drops` when any
object is only in the target, chosen or not, or a chosen sync statement drops
data. `myflames schema lint SIDE [--fail-on watch] [--json] [-o PATH] [-q]`
takes the same sides: exit 0 for a report, 1 when `--fail-on watch` meets a
Watch finding (Not judged never trips it), 2 for bad input or a failed live
capture; a saved or file side does not connect just because connection
settings exist, and invalid input leaves an existing output file untouched.
Snapshot files for `import`, `compare` and `lint` are read in binary, at most
`MAX_SNAPSHOT_BYTES` (32 MiB) plus one byte, and refused before decoding when
larger (exit 2, no server contacted).

Tests: `test/test_schema_ddl.py`, `test/test_schema_versions.py` (replay of
the recorded reads in `test/fixtures/schema_versions`, store and API),
`test/test_schema_sync.py`, `test/test_schema_versions_cli.py`,
`test/test_schema_versions_review.py` (review regressions),
`test/test_schema_lint.py` (every rule with Watch, clean and Not judged
paths, engine and version gates, bounded live reads, CLI exits and the API),
`test/test_schema_versions_contracts.py` (the JSON Schemas, `blocked_by`
included) and
`test/test_schema_versions_live.py` (round trips in MySQL 8.4 and MariaDB 11.4
containers it starts, skipped without Docker); replays come from
`test/_schema_replay.py`. Browser: `apps/workspace/test/schema-versions.mjs`
(`npm run test:schema-versions`).

### Compare servers: parameters and accounts

Compare global parameters, or users and roles, between two sides without
changing either server. Modules: `myflames/param_compare.py`,
`myflames/account_compare.py`, `myflames/compare_api.py` (endpoints),
`myflames/compare_cli.py` (`config compare`, `config snapshot`, `accounts
compare`, `accounts export`) and the `parameter_snapshot_*` methods of
`WorkspaceStore`. Contracts: `docs/schemas/parameter-snapshot-1.0.json`,
`parameter-snapshot-list-1.0.json`, `parameter-compare-1.0.json`,
`account-compare-1.0.json`.

**Capture.** `param_compare.capture(conn, server=None, label='')` reads the
existing `config_report(..., parameters=True)` projection and copies
`parameters-1.0.notes` (not the configuration review's notes; validation also
removes that boilerplate from older snapshots). `account_compare.capture(conn,
server=None)` uses `accounts.list_accounts` and `accounts.read_account(...,
include_catalog=False)` plus one SELECT of User, Host, ssl_type and the HEX of
ssl_cipher, x509_issuer and x509_subject from `mysql.user`; no new SQL selects
a credential field, and SHOW CREATE USER text is never exported. Each side
opens a fresh `MySQLConnection` with the isolated client environment and the
saved-password endpoint checks, at most 30 s per query and 120 s per side
(`BoundedConnection`, role expansion included); the two sides are read one
after the other. Accounts: at most 1,000, with an incomplete note beyond.
Parameter files and snapshots: at most 10,000 items and 8 MiB
(`MAX_FILE_BYTES`; CLI files are read in binary, refused before decoding when
larger).

**Comparison.** Both reports have `schema_version`, `compared_at`, `source`,
`target`, `items`, `notes`, `counts`, `complete` and `identical`. Statuses are
those of Schema versions (`different`, `only_in_source`, `only_in_target`,
`identical`, `not_compared`), each row with `status_text` (Different, Only in
source, Only in target, Identical, Not compared); `not_compared` always has a
reason, and there is no Added/Removed or Before/After wording. `identical`
needs complete inventories and no unreadable rows or side errors. A connection
or profile failure becomes that side's `error` with `kind: unavailable`; the
other side is still read and the rows are Not compared with a reason. Missing
or malformed selectors are side errors too; local CRUD and export validation
failures are HTTP 400.

- Parameter rows: `name`, `status`, `status_text`, `identity`, `group`, the
  `source` and `target` items (or null), `source_at_default` and
  `target_at_default` (null when the default is unknown) and an optional
  `reason`. Raw values are never replaced. Only typed values are normalized to
  compare: booleans (ON, OFF, TRUE, FALSE, 1, 0), byte sizes with K to E
  suffixes and optional B or iB, exact numbers, and enum and set words (white
  space ignored, case-insensitive). Other strings and paths keep every space,
  tab, line break and letter case, in values and in default checks. Truncated
  or redacted values, and parameters missing on an incomplete side, are Not
  compared. `IDENTITY_NAMES` (`server_id`, `server_uuid`, `hostname`, `port`
  and the like) are still compared and flagged `identity`. `group` comes from
  the existing parameter-to-check associations; none is invented. A MySQL and
  MariaDB pair adds a note and still compares.
- Snapshot validation is an allowlist: it drops connection credentials and
  unknown fields, redacts SQL credentials with the dbops helpers, and replaces
  with `[redacted]` the values and defaults of `_SECRET_VARIABLES` (an
  explicit list of credential-bearing server and plugin settings;
  `wsrep_sst_auth` stays redacted even when the server already masks it) and
  any value with credential URL, `user:password` or SQL credential syntax;
  allowed-value lists keep no recognized credential. Password policy
  settings, authentication plugin tuning (such as the `caching_sha2_password`
  settings) and key file paths stay comparable; numeric host:port values and
  Windows paths are kept. Snapshots saved before this rule need a fresh
  capture to recover values that were redacted.
- Account rows pair exact (name, host) with a structured `name` and `host`, a
  display `account`, the `source` and `target` projections, `differences`
  (`grants`, `roles` and `default_roles`, each with `only_in_source` and
  `only_in_target`, and `attributes: [{name, source, target}]` for the plugin,
  locked, expired, resource limits, password policy and TLS requirements, the
  TLS cipher, issuer and subject as hex) and `system` (true for
  `accounts.SYSTEM_ACCOUNTS`, also on each side; optional in the schema so
  older reports stay valid). Grant lines compare after normalizing privilege
  and column order, spacing and quote style (identifier case, options and
  scope kept), line by line; MariaDB's inherited role lines stay with their
  roles (`grant_source`). Unreadable attributes or grants make the row Not
  compared while known differences stay; an account that disappeared during
  capture, or is missing from an incomplete list, is Not compared. Password
  hashes, credential clauses, internal privilege models and other attribute
  keys are never emitted.

**Export** (`account_compare.export_sql(side, chosen=None)`; text only, never
sent to a server and never in the execution audit): the chosen accounts and
the roles they need, transitively (cycles bounded by the chosen set), with
every CREATE before any grant: roles first, then users as `CREATE USER IF NOT
EXISTS` without IDENTIFIED and with a comment to set the password, then direct
privilege grants before restrictions, role grants with their admin option, and
`SET DEFAULT ROLE` (TO on MySQL; FOR and a hostless role on MariaDB). MariaDB
PUBLIC is not created; package grants use the Oracle-mode wrapper with exact
session-mode restoration; names go through the existing quoting builders. The
text says TLS requirements, resource limits and authentication are not
copied. Without a choice, built-in system accounts are left out with one
`-- Omitted system accounts: ...` line (an unreadable omitted account does not
block the export, and a side with only system accounts exports comments);
`chosen=[]` exports nothing; a chosen system account is exported, and a system
role a chosen account needs must be chosen too. Protected accounts such as
root are ordinary export candidates. A missing chosen account, an unreadable
detail or dependency, unparsed grant syntax, an inconsistent default-role
grant, or an incomplete inventory without a choice refuses before any output
file is written.

**Snapshots.** The store's `parameter_snapshots` table holds zlib-compressed
JSON with a summary, an exact UUID `id`, `label` (at most 200 characters),
`saved_at`, `source_key` (JSON `[host, port]`) and `number` (per host:port,
the highest kept number plus one, under BEGIN IMMEDIATE). IDs are exact: no
prefix or label lookup. `parameter_snapshot_list(source=None, limit=1000)`
takes 1 to 5,000; `latest` resolves with its own query. Reads bound
decompression to 8 MiB and validate again.

**Endpoints** (POST JSON, workspace authentication, in the `ui.py` allowlist
through `compare_api.PATHS`; outside `_CONNECTION_PATHS`, because each side
fills its own saved password; none runs generated SQL or writes a server):

| Endpoint | Request | Response |
| --- | --- | --- |
| `/api/config/snapshots/list` | `{source_key?, limit?}` | `parameter-snapshot-list-1.0` |
| `/api/config/snapshots/save` | `{connection, label?}` | The saved summary: `id`, `number`, `label`, `server`, `captured_at`, `saved_at`, `source_key`, `complete`, `count` |
| `/api/config/snapshots/get` | `{id}` (`latest` accepted) | The summary plus `snapshot` (`parameter-snapshot-1.0`) |
| `/api/config/snapshots/delete` | `{id}` | `{deleted: true, id}`; a missing ID is refused |
| `/api/config/compare` | `{source, target}`, each `{live: {connection}}`, `{profile: NAME_OR_ID}`, `{snapshot: ID_OR_LATEST}` or `{file: JSON_TEXT}` | `parameter-compare-1.0` |
| `/api/accounts/compare` | `{source, target}`, each `live` or `profile` | `account-compare-1.0` |
| `/api/accounts/export` | `{side, accounts?: ["user@host", ...]}` | `{filename: "account-grants.sql", text, side}` |

A file side is JSON text, never a path for the backend to open. `latest` while
comparing means the newest snapshot of the other side's host:port; both sides
cannot be `latest`, and an unidentified other side asks for an explicit ID.
Outside a comparison, `latest` is the newest snapshot of all.

**Workspace** (`ServerCompare.tsx`, `compareApi.ts`, `server-compare.css`,
hosted by `Config.tsx` and `Accounts.tsx`): a segmented control (`Parameters |
Compare`, `Users and roles | Compare`) keeps the list as the default tab; both
tabs stay mounted after their first visit, preserving drafts and account write
outcomes, and `ViewOnScreenContext` scopes each, so list reads (`useOpenRead`)
happen only while their list is on screen. Compare never reads a server on its
own (not on a connection change or a return to the tab). Its choices (This
connection, saved connections by name, numbered snapshots with label and time,
and **Latest snapshot of** each saved server address, from up to 5,000
summaries) come from local reads, with **Refresh choices**; a latest choice is
resolved again at Compare time with a source-key-scoped list read and sent as
the exact ID, and a saved side sends only `{profile: id}`. Being on screen
gates starting a read (`useViewOnScreen`); a comparison, catalog or export
request already started can finish while hidden. Comparisons and exports
are cancelled by a change of the connection in use (`useAction(key)`) or
Stop, and replies from a cancelled request are ignored. **Save snapshot…** (Parameters toolbar) captures the connection in use
when its sheet opens, takes an optional label and reports "Saved snapshot #N ·
server"; **Manage snapshots (N)** deletes after one confirmation, and local
choices update without being overwritten by an older catalog reply. The Source
→ Target bar, Swap, Differences with Filter differences, and full-width detail
with Back follow Schema versions; **Show identical** and **Show
server-specific settings** fold rows while the counts cover every row; an
unreadable missing side says Not read; side errors appear once beside their
side. A report keeps the connection and selectors it was made with: a
connection switch greys only the result (`ViewLayout.otherConnection`) without
reading, while the Source → Target bar and Compare stay live and the note
offers Compare; changed side choices keep the old labels and ask for Compare
again. Compare is disabled with a reason when both sides are the same; Target
starts with the latest snapshot of the server in use (Parameters) or another
saved connection. Both details are one Setting | Source | Target table, as
Schema versions' What differs, with Different in words; differences sort
Different first, empty raw values read Empty, and a note says what the
filters hide. Before the first result the setup copies Schema versions
(chevron and swap icon button, uppercase side labels, equal-height sides; at
the 1100 and 820 px breakpoints Compare moves to its own row and both sides
stay side by side, also at 720 px); after it the shared collapsed bar and the
neutral comparison badges of `compareShared.tsx` take over (see **Comparison
badges and setup bar** under Schema versions), and compared sides read like
Schema versions' (snapshot number and label, saved connection name, read or
captured time). Parameters
is kept mounted after its first visit (`.server-keep` in `Server.tsx`, like
Users and roles, Instruments and Tables), so a comparison, a save in flight
and staged edits survive a switch to another Server view; each tab passes
the floating list of views only to the tab on screen, and focus after
choosing a view goes to that tab's title. A slow Save snapshot can be left
with Close while it continues (`ConfirmSheet onLeave`), and hiding the view
aborts neither a save nor a comparison the user started. Account detail
groups grants Only in source, Only in target and In both (the source
spelling of lines Python matched; the browser parses no grants); TLS, limits
and password policy read as words. Rows whose `account-compare-1.0` entry has
`system: true` carry a **System** pill, and All accounts only in source
leaves them out and says what it chose; an explicit checkbox still exports
one. **Export
grants as SQL** (Export side, a checkbox per account by structured identity
quoted through `sqlIdent.bt`, and All accounts only in source across the whole
report) reads the chosen side again with the retained selectors, says
passwords are not included and nothing is executed, keeps the SQL in memory
with Copy and Download .sql, and is unavailable for a greyed report.

**CLI**: `myflames config snapshot save [--label TEXT] [--json] [-o PATH]
[-q]` with the shared connection flags, `snapshot list`, `snapshot show
SNAPSHOT` (an ID or `latest`) and `snapshot delete SNAPSHOT [--yes]` (asks
`[y/N]`, needs `--yes` without a terminal, deletes local data only); `myflames
config compare SOURCE TARGET [--json] [--all] [--fail-on different] [-o PATH]
[-q]` (sides `live`, `profile:NAME`, `snapshot:ID`, `latest`, `file:PATH` or a
`.json` path; text folds Identical and identity rows unless `--all`, and JSON
always has every row); `myflames accounts compare SOURCE TARGET [--json]
[--fail-on different] [-o PATH] [-q]` (`live` or `profile:NAME`); `myflames
accounts export SIDE [--account USER@HOST ...] [-o PATH] [-q]`. The shared
connection flags apply to the `live` side only; each profile uses its own
validated fields and saved password, and passwordless profiles work. Exit
codes: 0 success; 1 when `--fail-on different` finds Different, Only in source
or Only in target (identity rows included, even when the text folds them); 2
for bad input, an unreadable side or Not compared rows. A partial comparison
is still written with exit 2, while a failure before the payload leaves an
existing output file untouched. Diagnostics go to stderr through `_emit_text`;
`-q` keeps the payload.

Tests: `test/test_param_compare.py`, `test/test_account_compare.py`,
`test/test_compare_api_cli.py`; browser `apps/workspace/test/server-compare.mjs`
(`npm run test:server-compare`; `--unit` validates the fixtures against the
JSON Schemas and runs the typed API helpers, `--compile-only` builds the
fixture page, `COMPARE_SHOTS` keeps screenshots), with
`test/fixtures/server-compare.json` generated by the Python comparison and
export functions.

### Parameter version catalogs

Which parameters differ between two MySQL or MariaDB releases, and every
release where one parameter changed or was deprecated, answered offline from
catalogs shipped with the package. Files: `myflames/param_versions.py` (the
API), `myflames/param_versions_cli.py` (`myflames config versions`), the
generated `myflames/sysvar_history_mysql.py` and
`myflames/sysvar_history_mariadb.py` (written by
`scripts/gen_sysvar_history.py`, committed), and the contracts
`docs/schemas/parameter-version-catalog-1.0.json`,
`parameter-version-compare-1.0.json` and `parameter-history-1.0.json`. The
workspace view is `apps/workspace/src/ParameterVersions.tsx` +
`paramVersionsApi.ts` + `parameter-versions.css`, a third tab of
`ServerCompareHost` (kind `"parameters"` only). Nothing in the feature reads
a server, opens a connection or writes an audit record.

**Where the facts come from.** Every catalog fact about names, scope, type,
default, range, allowed values and dynamic comes from running servers: the
lab script `scripts/record-sysvar-catalog.py` (lab-only, like
`scripts/record-alter-algorithm-fixtures.py`) starts each release in scope in
a throwaway container with `--no-defaults` (no option file read) and reads
the server itself: MySQL from `performance_schema.global_variables`,
`variables_by_thread`, `variables_info` and `variables_metadata` where
present, with `mysqld --no-defaults --verbose --help` for defaults, and
MariaDB from `information_schema.SYSTEM_VARIABLES`. A guard refuses a record
with any value from an option file. It writes one `sysvar-runtime-1.0` record
per release under `local/param-versions-2026-10-28/runtime/<engine>/`; its
own tests are `test/test_record_sysvar_catalog.py` (also lab-only). MySQL
builds are Oracle's official Docker images; MariaDB builds are MariaDB's own
Ubuntu packages as in the official Docker image (or the same packages when no
image exists), and other builds can compile other defaults. Source tags and
the official MySQL manual's added/deprecated/removed pages (saved pages, read
offline) only add what a running server cannot report: deprecation evidence
(`server`, `source`, `manual`, in that sorted order), replacement names,
aliases (`alias_of`) and, only when a record could not say, the dynamic flag.
Scope: MySQL GA, LTS and Innovation releases from 8.0.11 (including 8.1 to
8.3, 9.x and 26.x); MariaDB stable releases from 10.6. A release with a tag
but no image or package that could be run goes to SKIPPED with the reason,
never filled from documentation. Percona Server uses the MySQL catalog;
variables only Percona adds are not cataloged. `default_note`: null (a plain
default), `"null"` (SQL NULL), `"host"` (computed at startup from the host or
derived at startup; the default holds what a lab server computed),
`"release"` (the value names the release or build, such as `version` and
`build_id`; never compared), `"build"` (the default follows the build's
installation layout, such as `socket` and `plugin_dir`; never compared),
`"state"` (session or server state such as `pseudo_thread_id`,
`statement_id`, `gtid_executed`, `server_uuid` and `timestamp`: the lab's own
values, never compared; `STATE_NAMES`) or `"unknown"`. The generator sets `"host"` from the lab's two-host comparison,
MariaDB's `origin` AUTO and a reviewed source list (`HOST_REVIEW` in
`scripts/gen_sysvar_history.py`, each entry with its source location, first
and last release and reason; `RELEASE_NAMES` and `BUILD_NAMES` hold the other
two lists). A default lowered only when the operating system grants too few
open files (`max_connections`, `table_open_cache`, `table_definition_cache`,
the Performance Schema autosized sizes) is reviewed and left unmarked, so a
real change of it is still compared. `BUILD_ONLY` maps names whose presence
follows build options (`innodb_numa_interleave`, NUMA builds only) to one
sentence: compare counts them unchanged and says why in `notes`, and history
drops their added and removed events and returns the sentence as
`build_only`.

**Lab and review details.** The MySQL catalog includes the 5.7 GA releases
(see [MySQL 5.7 support](#mysql-57-support)). The lab script's containers
carry a per-run token (`RUN_TOKEN`); it removes only containers and images it
created (images present at start are never removed, whatever
`--images-before` lists), and a stopped run starts no queued release and
writes no record for it. `HOST_REVIEW` covers, for example,
innodb_purge_threads, innodb_parallel_read_threads and innodb_read_io_threads
from 8.4.0, temptable_max_ram from 8.4.0 to 9.5.2, open_files_limit,
lower_case_file_system, and MariaDB thread_pool_size and log_tc_size.
`DEFAULT_RULES` maps names to reviewed (first, last or None, sentence)
periods saying how the server sets a default it computes at startup (marked
"host" or reviewed and unmarked, such as innodb_buffer_pool_instances from
8.4.0). A default that becomes or stops being computed at startup gets its own
sentence followed by the rule ("From 8.4.0 the server computes the default at
startup; before, it was 4. The server sets it at startup to 1 when it sees 16
CPUs or fewer, else 4 (a lab server got 1)."); a default that stops being
computed adds the earlier rule ("Before then: ..."). `BUILD_ONLY` also holds
the old semisync plugin's variables (rpl_semi_sync_master_*,
rpl_semi_sync_slave_*: the lab loaded the source/replica plugins from
8.0.26); compare groups build-only names by sentence in one note. Reviewed
source deprecations (innodb_log_file_size and innodb_log_files_in_group from
8.0.30, replacement innodb_redo_log_capacity; innodb_undo_tablespaces) add
"source" evidence, and reviewed rejections drop the manual's evidence where
the source deprecates only a value (group_replication_communication_stack
before 26.7.0). MariaDB "Alias for" help texts fill alias_of.

**Catalog module format.** Each generated module holds `ENGINE`, `GENERATED`
(`on`, `runtime`, `source`, `manual` per series), `VERSIONS` (one tuple per
cataloged release: version, tag, maturity, tag date, previous index or None,
method), `SKIPPED` ((version, short reason, tag date)), `FIELDS` (`scope`, `type`, `dynamic`,
`default`, `default_note`, `min`, `max`, `values`, `deprecated`,
`replacement`, `alias_of`, `plugin`) and `HISTORY`: per parameter a
run-length list `((first_index, facts_or_None), ...)` over VERSIONS in
numeric order, where facts is a tuple in FIELDS order and None means absent
from that version on; a run list starts at the first version where the name
exists (no leading `(0, None)`), so before its first entry the name is
absent. The previous index is the lineage: within a series the previous
release; the first release of a series follows the latest-dated release (by
tag date) of the nearest earlier series that has one. Generated modules are
byte-for-byte deterministic for the same inputs and `--generated-on`, and are
imported with plain `from . import sysvar_history_<engine>` inside functions
so PyInstaller sees them.

**Regenerating** (offline; the source checkouts are read with
`git for-each-ref`, `git ls-tree` and `git cat-file --batch`, never checked
out; the C++ declaration reader is `scripts/gen_mysql_sysvars.py`, imported).
First the lab script (needs Docker; only where new runtime records are
wanted), then the generator:

```
python3 scripts/record-sysvar-catalog.py --list
python3 scripts/record-sysvar-catalog.py --jobs 3 \
    --hub-tags hub-mysql.json hub-mariadb.json \
    --log local/param-versions-2026-10-28/runtime/run-full.log
python3 scripts/gen_sysvar_history.py \
    --runtime local/param-versions-2026-10-28/runtime \
    --manual local/param-versions-2026-10-28/manual \
    --mysql-source ../mysql-server --mariadb-source ../mariadb-server \
    --report local/param-versions-2026-10-28/VALIDATION.md \
    [--engine mysql|mariadb] [--output-dir myflames] [--cache DIR] [--generated-on YYYY-MM-DD]
```

`--cache DIR` keeps parsed source facts per tag, keyed by the blob ids of the
files read and a digest of the parsing code, so a rerun takes seconds and is
byte-identical; an engine with no usable record is reported and its module is
left as it was. The validation report compares every running server with its
source tag: unexplained names in either direction, scope and read-only
disagreements, deprecation warnings that are not evidence (a warning that
deprecates changing a variable at runtime or one of its values is not
deprecation of the variable), defaults that change at almost every release
(such as `build_id`), and every skipped release.

**Python API and contracts** (`myflames/param_versions.py`). `ENGINES =
("mysql", "mariadb")`; `load(engine, module=None)` (tests pass a fixture
module) returns a `Catalog`; a missing or malformed module raises
`CatalogError` (a ValueError), and `catalog_index()` instead reports that
engine with `available: false` and the reason in `notes`. `catalog_index()`
emits `parameter-version-catalog-1.0`; `resolve_version(engine, text)`
extracts the numeric release from texts like `8.4.6-6`,
`11.4.5-MariaDB-log` or a tag and returns it when cataloged, else None;
`compare(engine, from_version, to_version)` emits
`parameter-version-compare-1.0`; `history(engine, name)` emits
`parameter-history-1.0`. Change kinds in order: `added`, `removed`,
`deprecated`, `undeprecated`, `default`, `dynamic`, `scope`, `type`,
`values`, `range`. A value unknown on either side is never a change of that
kind; an unknown default adds a Not-compared sentence and a report note.
Comparisons never count an unknown default, a host-dependent default on both
sides, or a release, build or state value as a default change, and MySQL
before 9.0 reports no data types (null in the catalog, shown Unknown, never a
type change). `Catalog.where` places each difference by walking both lineages
back to where they meet: made on To's own line, `since[kind]` is the release
from which To's value holds (the walk never passes the meeting point) and the
sentence ends "in X"; made on From's line (most differences of a downgrade,
or a patch of an older series that the newer series does not have),
`since[kind]` is null and `_from_line_sentence` names the release from From's
side ("8.0.38 changed the range (...); 8.4.0 keeps the earlier range.");
changed on both lines, the line that passed through the other side's
value is named, else null with no release named. When From lacks a parameter
To has deprecated, the deprecation is To's own ("Deprecated in V", or "since
the first cataloged release or earlier"); `readded` adds "It was removed in R
and added again in V." for a gap on To's line. `from_line_twin` adds
the release of From's own series that made the same change after the lines
split ("Deprecated in 8.1.0 (8.0.34 on the 8.0 line)."); `when` names a
release with the unread releases its lineage step skips ("in 10.6.6 or
10.6.7 (10.6.6 could not be read)", from SKIPPED's tag dates). A parameter
that arrives deprecated is added and deprecated; `_replacement_note` says
when the suggested replacement is itself deprecated at To; `_successor` names
a removed parameter's replacement when To has it. A boolean declared again as
an enum whose values are only ON and OFF and a maximum that moved by less
than a block are not changes (a boolean compares as the values OFF and ON). `DEFAULT_RULES` (reviewed, per period) gives the sentence that
follows a computed default's change and history's `default_rule`; history's
`aliases` lists names whose alias_of is this one. History events
compare each version with its lineage predecessor; a name present in the
first cataloged release is "present since the first cataloged release",
never "added". Not found: `found: false` plus at most 8 close names of the
same engine. Every sentence is written in Python; summaries are at most 120
characters and never cut a word.

Sentence examples: a difference made on From's line, such as 8.0.38's
innodb_io_capacity range in 8.0.46 -> 8.4.0, reads "8.4.0 changed the default
from 200 to 10000; 8.0.36 keeps 200."; a deprecation in the first cataloged
release reads "Deprecated since <that release> (the first cataloged
release) or earlier" (5.7.9 for MySQL since the 5.7 records); an unread step reads "Removed in 10.6.6 or 10.6.7 (10.6.6 could not
be read).", or as a sentence subject "8.4.3 (or 8.4.1, which could not be
read)"; a removal names its successor when To has it ("Removed in 8.2.0; use
binlog_expire_logs_seconds instead."). utf8 to utf8mb3 is counted as a default
change and reads "the same character set under its full name" (from From's
line: "under two names"). A maximum that moved by less than a block is not a
change because 8.0.26 started rounding maxima to the block size. Parameter
names are case-insensitive (lowercased before lookup); an empty-string default
reads "an empty string"; SKIPPED reasons are one short sentence (the lab's
full text stays in the runtime records and VALIDATION.md).

**Endpoints** (ui.py `_PARAM_VERSION_PATHS`; no connection, no server read,
no audit, no store): `POST /api/config/versions` `{}` returns the catalog
index; `POST /api/config/versions/compare` `{engine, from, to}` the compare
report; `POST /api/config/versions/history` `{engine, name}` the history
report (found false is 200). A bad engine, version or name is a 400 through
the shared `request_error_text`; versions are at most 32 characters, names at
most 64.

**CLI.** `myflames config versions list [--engine mysql|mariadb] [--json]
[-o PATH] [-q]`; `config versions compare FROM TO [--engine E] [--kind
KIND]... [--name TEXT] [--json | --markdown] [-o PATH] [-q]`; `config
versions history NAME [--engine E] [--json | --markdown] [-o PATH] [-q]`.
Exit 0 on success, 2 on bad input or a version or name the catalog does not
have; results on stdout, diagnostics on stderr. Without `--engine`, compare
uses the engine whose catalog has both versions and exits 2 naming `--engine`
when none or both do; history prints every engine that has the name (with
`--json`, an array of reports). Table columns show the value of the kind that
changed; with `--kind` or `--name`, JSON keeps the whole comparison's `counts`
and `summary` and adds `filter` and `shown`. Dispatched in `cli.py` before the live
`config` command and registered in the root epilog.

**Workspace.** Server > Parameters shows Parameters | Compare | Versions;
Versions mounts on its first visit, stays mounted, and reads the catalog
index once when first on screen (Try again on failure; no Refresh button, and
Refresh all does not involve it). Compare versions reads whenever Engine,
From or To changes; Parameter history whenever the engine or the parameter
changes, only while Versions is on screen; a superseded request is cancelled
and late replies dropped, and a loading result keeps the previous one greyed.
The engine follows the Parameters report of the connection in use (Percona is
`mysql`) until the user picks one; From defaults to the server's release when
cataloged, else the nearest earlier cataloged release, and To to the newest
cataloged release. One-line notes cover a Percona connection ("Percona Server
uses the MySQL catalog. Variables Percona Server adds are not cataloged."), a
server newer than the catalog, and a skipped release's From fallback. Kind
filters with counts (All, Added, Removed, Deprecated, Default changed,
Other), a name filter, and **Only parameters set on <connection>** when the
Parameters report of the connection in use is loaded and its engine matches
(names whose `modified` is true). Badges are comparison tones only: Added and
Removed outline, every other kind accent, never health colors; at most three
per row. A superseded result is greyed while a new one loads (busy only while
a read is pending; Stop leaves "Stopped." with Try again), and Since shows
"—" for a difference From's line made. The row inspector shows both releases' facts and **Show full
history**, which switches to Parameter history for that name; the Parameters
list inspector's **Version history** does the same and sets the engine.
Integers of four or more digits get exact grouping (BigInt) with the server's
text on hover; `default_note` "null" reads NULL, "host" reads "Computed at
startup", "build" reads "Depends on the build", "release" shows the value muted
(never compared), "unknown" or a null default reads Unknown, and an absent
parameter reads Not present. Parameter history shows `build_only` as one line
under the summary.

**Tests.** `test/test_param_versions.py` (fixture catalogs in
`test/fixtures/param_versions/`: unit, compare, history, CLI through
`cli.main`, and the three endpoints over the in-memory HTTP handler with a
guarded connection factory proving none is opened),
`test/test_gen_sysvar_history.py` (scope, lineage, run-length encoding,
deprecation evidence, source parsing, deterministic output, an end-to-end run
against a tiny git repository), `test/test_record_sysvar_catalog.py`
(lab-only, with the lab script), and the browser suite
`apps/workspace/test/parameter-versions.mjs` (`npm run
test:parameter-versions`; mocked replies validated against the schemas).

### Engine words for the Query editor

The Query editor's keyword suggestions come from a generated per-release
catalog of the engines' own vocabularies, which also records every native
function's argument-count bounds; nothing is hand-kept. Files:
`scripts/gen_sql_words.py` (the generator, Python
3.7, stdlib only), the generated
`apps/workspace/src/editor/engineWords.generated.ts` (committed; its own lazy
chunk, loaded the first time completion needs it), the decoder and resolver
`apps/workspace/src/editor/engineWords.ts`, and the consumers
`apps/workspace/src/editor/completion.ts` (suggestions) and
`apps/workspace/src/editor/argumentHints.ts` (the caret's argument hint).
Currently the catalog covers the
MySQL tags 5.7.9 to 26.10.0 (102 tags) and the MariaDB tags 10.6.3 to
13.0.2 (113 tags), the same releases as the parameter version catalogs.

**How it is generated.** The generator reads every release tag listed in
`myflames/sysvar_history_mysql.py` and `myflames/sysvar_history_mariadb.py`
out of local MySQL and MariaDB git checkouts with `git ls-tree` and
`git cat-file --batch`, never the working tree, and parses `sql/lex.h`,
`sql/sql_yacc.yy` and `sql/item_create.cc` (plus MariaDB's
`sql/item_geofunc.cc`). Keyword rules follow MySQL's
`sql/gen_keyword_list.cc`: `SYM` and `SYM_HK` words whose first character is
a letter are keywords, `SYM_H` optimizer-hint tokens are dropped, `SYM_FN`
words are kept apart as grammar functions, and parser-internal tokens are
excluded (for example `PARSE_GCOL_EXPR`, which only the server may use while
reading a .frm file: mysql-5.7.44 `sql/lex.h:448`,
`sql/sql_yacc.yy:6471-6481`). A keyword is marked non-reserved when the
grammar accepts it as an unquoted identifier: MySQL 8.0.11+ from the
`%token<lexer.keyword>` declarations (5.7 predates `gen_keyword_list.cc`, so
the `keyword` and `keyword_sp` rules are expanded instead), MariaDB from the
`keyword_ident` rule family after `%ifdef` preprocessing. Function argument
counts come from `func_array` in `sql/item_create.cc` on MySQL 8.0+ (the
explicit `SQL_FN*` bounds and `SQL_FACTORY` instantiator constants) and from
the `Create_func_argN` classes and `create_native` bodies on MySQL 5.7 and
MariaDB (only universal rejection guards and exhaustive `switch (arg_count)`
arms count; mode- or parity-dependent validation stays unknown). Bounds are
counts only, never parameter names: `?` marks an unknown bound, `*` an
unbounded one (`MAX_ARGLIST_SIZE`). Each `major.minor` series is stored as a
base release plus per-release deltas (keywords added, removed or re-flagged,
grammar functions, function bounds). Regenerate with
`python3 scripts/gen_sql_words.py --mysql-source <checkout> --mariadb-source <checkout>`;
`--check` regenerates in memory and exits 1 when the committed file differs.

**Resolution and use.** `wordsFor(engine, version)` answers the keywords
with a reserved flag for the exact cataloged release, or, for an
uncataloged patch or a suffixed build (a Percona `8.0.36-28`), the nearest
earlier cataloged release of the same series with `exact: false`; a series
the catalog does not have resolves to nothing, and completion falls back to
the small common word list (`commonCompletionWords`, `sqlCompletion.ts`).
`isKeyword` drives engine-aware backtick quoting in completion: a name that
is a keyword on the connected server's own release is quoted. The suggestion
detail is `reserved` for reserved keywords and empty for other keywords. The
detail names the release when the match is not exact (", MySQL 8.0.36 list").
`functionsFor` resolves the native and grammar functions with their
argument-count bounds under the same policy. Completion lists each function
with a `function` kind and an argument-count detail such as `N arguments` (a `min` to
`max` range reads `N to M arguments`, an unbounded maximum `at least N
arguments`, singular counts use `argument`, unknown bounds omitted). Accepting
one inserts `NAME()` with the caret inside, or only the name when the text already holds an
opening `(` (`completion.ts`). `argumentHints.ts` shows the call the caret
sits in as `NAME(…) · N arguments · argument k` with the same count
strings, never parameter names, behind the **Details on hover** setting: it
needs one empty caret, the document within the highlight limit, a cataloged
or nearest-earlier release list, and the call's opening `(` within the
4,000-character window scanned before the caret; strings, quoted
identifiers and comments are skipped, and a name the list does not hold, or
one qualified by a dot, shows no hint. A WeakMap keyed by the shared
`functionsFor` function array holds an uppercase-name index, so each call
lookup is independent of the catalog's size. The tooltip anchors after the
call's opening parenthesis and reuses its object and create function while
the anchor and label stay unchanged. Tests:
`apps/workspace/test/engine-words.mjs` (`node test/engine-words.mjs`,
no browser: exact, uncataloged-patch, Percona- and MariaDB-suffixed,
earlier-than-base and unknown-series resolution, case-insensitive keyword
lookup, and a verified sample of keyword and function facts from both
engines) and `apps/workspace/test/argument-hints-unit.mjs` (hint labels, the
no-hint rules, lazy vocabulary loading, indexed lookups, tooltip identity
and text-only rendering; Node, no
browser).

### MySQL 5.7 support

MySQL 5.7 (tested on 5.7.44) is the support floor: `server_info.SUPPORTED_MINIMUM = {"mysql": (5, 7), "mariadb": (10, 11)}`. A MySQL 5.6 or older server gets "myflames supports MySQL 5.7 or later; ...", and 5.7, 8.0, 8.4 and 9.x no longer get the "supports MySQL 8.4 or later" warning. MySQL 5.7 is end of life (Oracle Sustaining Support since 2023-10-25, from the vendor's notice; `lifecycle` has `(5, 7): ("maintenance", "2023-10-25")`). What 5.7 does not record is reported as unknown, never as an error, and what it lacks (roles, SET PERSIST and RESET PERSIST, EXPLAIN ANALYZE so no Run analysis, CLONE, LOCK INSTANCE FOR BACKUP, `performance_schema.error_log`, invisible indexes and columns, ALTER algorithm prediction) is said in one plain sentence. No JSON contract gained or lost a field for the Server views; the 5.7 values are additive in meaning (`statistics.fresh`, `persisted: false`, `test_sql: ""`, notes). Tests: `test/test_mysql57_views.py` (the Server place replays of the 5.7.44 recordings), `test/test_mysql57.py` (privileges, Users and roles, Run analysis, editor probe, plans, ALTER, Import CSV), fixtures `test/fixtures/perf/*-mysql-5.7*.json`, `test/fixtures/health/mysql-5.7.json`, `test/fixtures/cluster/cluster-gr-5.7-*.json`, `test/fixtures/accounts/mysql-5.7.json` and ten `test/fixtures/mysql-5.7-*.json` plans; `scripts/ga-matrix.sh` has `mysql-5.7` and `mysql-5.7-replica`.

Server place:

- **Indexes** (`indexes.py`): `perf_common.Reader.rows_with_columns(build, optional)` reads `information_schema.STATISTICS` and, on Unknown column (`perf_common.unknown_column(exc)`), reads again without IS_VISIBLE (stand-in `'YES'`) or EXPRESSION (stand-in `NULL`); this also covers MySQL 8.0.0 to 8.0.12. A server without invisible indexes gets `test_sql: ""`, the `NO_INVISIBLE_CAVEAT` caveat and a `drop:` line in the CLI text; the workspace hides Set invisible below MySQL 8 (`supportsInvisibleIndexes`).
- **Tables** (`schema_health.py`): the unique-key read drops EXPRESSION the same way; tablespaces come from `INNODB_SYS_TABLESPACES` when `INNODB_TABLESPACES` is an Unknown table; MySQL without `information_schema_stats_expiry` has `statistics.fresh` true and `cached` false (Fresh numbers says it changes nothing); replica-apply text follows 5.7's default slave_rows_search_algorithms (INDEX_SCAN,TABLE_SCAN); the surrogate primary key is offered without INVISIBLE below 8.0.23 (`_invisible_columns`).
- **Parameters and Compare** (`parameters.py`, `config_review.py`, `variables.py`): MySQL before 8.0 reads no `variables_info` or `persisted_variables`, so the `parameters-1.0` inventory is `complete: true`; `config_review.MYSQL57_SOURCE_NOTE` appears once in the report and once in `parameters.notes`; every item is `persisted: false` and `source: null`, static items `modifiable: "option_file"`, never "restart"; `config_review.defaults_for(engine, version)` and `MYSQL57_DEFAULTS` supply 5.7 defaults where they differ from 8.4 (table_open_cache 2000, innodb_io_capacity 200, innodb_flush_method unset); single-variable modes persist, persist_only and reset_persist are unavailable ("MySQL 5.7 has no SET PERSIST.") with SET GLOBAL plus the option-file line instead and `persisted.supported` false; the batch sheet (`Config.tsx`) uses SET GLOBAL and hides Keep after restart; Compare between 5.7 and 8.x lists Only in source or Only in target instead of Not compared. Apply type (dynamic or static) on MySQL before 8.0 comes from the parameter version catalog's row for that exact release, read from a running server of it (`variables.static_dynamic(engine, name, version_tuple)`; innodb_log_buffer_size and innodb_undo_tablespaces are static on 5.7.44, dynamic on 8.x); a release or a name the catalog lacks is Unknown and not editable inline, never the 8.x answer. `config_review.before_mysql80` is the shared test.
- **Locks** (`locks.py`): `locks.innodb_lock_tables(engine, ver)` is true for MariaDB and MySQL below 8.0.1; row locks then come from `information_schema.INNODB_LOCKS` and `INNODB_LOCK_WAITS` (`row_locks: "waits_only"`), `data_locks_latch_caution` is false, the metadata lock note says "(the MySQL 5.7 default)", `kill_needs()` needs SUPER only (`system_user: null` below 8.0.16); `lock_product` names the product.
- **Health** (`health.py`): 5.7's MEMORY internal temporary tables (`config_review.tmp_table_limit`), redo checkpoint counters stated as 8.0.30+, 5.7 defaults from `MYSQL57_DIAGNOSTIC_DEFAULTS` (log_bin OFF; buffer pool instances 8 from 1 GiB, else 1), never SET PERSIST. **Instruments** (`ps_instruments.py`): `defaults_profile(server)` returns `mysql57` below 8.0 (metadata lock, transaction and memory instruments off, every statement instrument on, nine progress stages on, no error instrument, transaction consumers off; `MYSQL57_PROGRESS_STAGES`; no PROPERTIES column, no SET PERSIST_ONLY; `FEATURES` engine fields may be tuples). **Replication**: below 8.0.1 `replication_connection_status` and `_by_worker` with null queue and apply timestamps and one note, `capabilities.worker_timestamps` false, lag notes name Seconds_Behind_Master, skip plans say SUPER, the GTID-ON refusal says 5.7 rejects sql_slave_skip_counter (error 1858), `replication.receiver_stage()` names "Checking master version" below 8.0.26. **Cluster** (`cluster.py`): Group Replication is read from 5.7.17, MEMBER_ROLE and MEMBER_VERSION are NULL stand-ins, roles come from `group_replication_primary_member` (UNDEFINED or empty means no role), only the local member's version is known, reviewed changes stay off below 8.0.13, `cluster-1.0` is unchanged. **Full analysis** (`server_checks.py`, `report.py`): `expire_logs_days` with `SET GLOBAL expire_logs_days = <days>;` and an option-file line, the query cache judged with a 5.7 caveat, `SET GLOBAL super_read_only = ON;` for a writable replica, the memory budget with MEMORY temp tables, no "invisible first" for index actions. **Tuning** (`tuningRules.ts`): `globalChange("mysql57", ...)`, `Ctx.mysql57` and `Ctx.changeKind`.
- Already right on 5.7: Sessions, Watch, InnoDB status, Waits, I/O, Top statements (QUERY_SAMPLE_TEXT, quantiles and CPU null through the optional-column drop), the digest histogram ("no latency histogram"), digest tables, DDL progress and the Error log (`unsupported_version` with file guidance). A running statement's plan still needs MySQL 8.3. Parameter documentation links for 5.7 point to the MySQL 8.0 manual (`sysvar_docs.py` has no 5.7 pages).

Elsewhere:

- **Privileges** (`privileges-1.0`, `/api/live/privileges`, `myflames privileges`): `read_privileges` sends the usual read; on 1046, or on 1305 or 1370 naming CURRENT_ROLE, it reads again with NULL in its place (on 5.7 these mean no default database, a database without the function, a database without EXECUTE). On 5.7 the action map keeps only alternatives of privileges 5.7 has (KILL, SET GLOBAL, read_only bypass, replica start and stop, skip and restricted SET SESSION need SUPER), ALL ON *.* and EXPLAIN FOR CONNECTION use the 5.7 static set (no CREATE ROLE or DROP ROLE), SYSTEM_USER is not considered before 8.0.16 (and the Kill reason does not name it there), a MySQL Shell dump needs RELOAD and REPLICATION CLIENT. `run_analysis`, `set_persist`, `set_persist_only`, `reset_persist`, `create_role`, `drop_role`, `grant_role`, `revoke_role`, `default_roles`, `backup_lock`, `clone_donor`, `clone_recipient` ("CLONE needs MySQL 8.0.17 or later.") and `error_log` ("performance_schema.error_log needs MySQL 8.0.22 or later.") report `available: false` with a one-sentence `note`; the notes are `privileges.NO_ROLES_NOTE`, `NO_PERSIST_NOTE`, `NO_ANALYZE_NOTE` and `NO_BACKUP_LOCK_NOTE`; an unknown version keeps the 8.x answers. The Query editor's `/api/editor/preview` reads the same answers (Run is disabled for SET PERSIST and CREATE ROLE with the note; SET GLOBAL shows "Allowed through SUPER"). `dbops.required_privileges("set_global")` still names "SYSTEM_VARIABLES_ADMIN or SUPER" on 5.7.
- **Users and roles** (`accounts-1.0`, additive `roles_supported` and `roles_note`, the note also in `warnings`): the floor is "Users and roles needs MySQL 5.7.6 or later, or MariaDB 10.11 or later." (account_locked, SHOW CREATE USER and ACCOUNT LOCK arrived in 5.7.6); `list_accounts` reads no `mysql.role_edges` or `mysql.default_roles` on 5.7; a role change (`role: true` or operation `roles`) is refused with `NO_ROLES_NOTE` ("This MySQL version has no roles; MySQL added them in 8.0.") before any read; the workspace hides New role and the Roles tab shows `roles_note`; a generated password before MySQL 8.0.18 comes from `accounts.generate_password` (IDENTIFIED BY in its own NO_BACKSLASH_ESCAPES session; the plan warns that myflames generates it and shows it once, `generates_password` stays true and apply returns `generated_password` once); Drop and Rename before 8.0.22 warn that the server does not check definers (routines then fail with 1449, views with 1449 for SUPER callers and 1045 for others, 1698 for an auth_socket login); from 8.0.22 the 4006 warning names the privilege that passes it: SET_USER_ID through 8.1, SET_USER_ID or ALLOW_NONEXISTENT_DEFINER in 8.2 and 8.3, ALLOW_NONEXISTENT_DEFINER from 8.4. `accounts create` and `accounts password` help: "--random-password  Generate the password (MySQL 8.0.18 and later: the server; MariaDB and older MySQL: myflames) and print it once."
- **Run analysis**: `live.analyze_unsupported(version, maria)` returns the refusal sentence for MySQL before 8.3.0 and None otherwise, including an unreadable version or MariaDB: "Run analysis needs EXPLAIN ANALYZE FORMAT=JSON, which MySQL has from 8.3 (8.4 LTS and later), and this server is MySQL X; nothing was sent, so use Explain for the estimated plan." Run analysis sends EXPLAIN ANALYZE FORMAT=JSON, and before 8.3 the server refuses it after it is sent (5.7 has no EXPLAIN ANALYZE, 8.0.18 to 8.0.20 parse only a bare ANALYZE, 8.0.21 to 8.2 answer 1235); `privileges.NO_ANALYZE_NOTE` says the same for the `run_analysis` action. LiveJob, `MySQLConnection.explain_analyze`, `myflames capture --mode analyze` (exit 2), the MCP `explain_query` tool and the `run_analysis` action (`available: false`, `note`) use it, and nothing is sent after the version read. The root command's `_run_live_explain` (`myflames -e ...` with connection flags) exits 2 with "EXPLAIN ANALYZE refused: <reason>" on stderr before sending anything, and sets `explain_json_format_version` only from MySQL 8.3 (before, MySQL 5.7 and 8.0.18 to 8.2 failed with ERROR 1193 Unknown system variable); the product's stated floor for timed JSON analysis stays MySQL 8.4.
- **Query editor** (`EditorSession.refresh_state`): before MySQL 8.0.16 the probe uses `(SELECT THREAD_ID FROM performance_schema.threads WHERE PROCESSLIST_ID = CONNECTION_ID())` in place of `PS_CURRENT_THREAD_ID()` (an unknown version keeps it); 5.7 ships the `transaction` instrument and the `events_transactions_current` consumer off, so the state stays unknown with the "instrument ... is off" note until both are on. The ALTER review's `prediction.predicted` is false with "MySQL 5.7.44 is not predicted: it has no ALGORITHM=INSTANT (MySQL 8.0.12 added it), and the rules were checked for MySQL 8.4 and 9.x." Import CSV before 8.0 names SET GLOBAL plus the option file ("MySQL 5.7 has no SET PERSIST") for `local_infile` OFF and SUPER for `read_only`; the lab matrix passes 35 of 35 on 5.7.44.
- **Schema versions**: capture works on 5.7.44 (the inventory reads only columns 5.7 has; TRIGGERS.ACTION_ORDER orders triggers, which FOLLOWS and PRECEDES set from 5.7.2; session settings are sent only when the server has the variable); coverage is complete once privileges read, and Checks judge every rule except `reserved_word` (no `information_schema.KEYWORDS` before 8.0.11: Not judged with that reason).
- **Parameter version catalog**: `sysvar_history_mysql.py` includes the MySQL 5.7 GA releases 5.7.9 to 5.7.44 recorded from running 5.7 servers, 102 releases and 843 names in all. The 5.7 `validate_password` plugin and 8.0's `component_validate_password` component are two labels. MySQL 8.0.11 follows 5.7.22 (the latest-dated release of the nearest earlier series), so Compare versions 5.7.44 to 8.0.11 lists 78 added, 35 removed and 48 default changes. The 5.7 manual check used the 5.7 reference manual text.
- **Compare**: `myflames config compare` no longer exits 2 on every MySQL: a secret of the explicit list (`param_compare.sensitive`, such as `report_password`, which exists on every MySQL) read on both sides is Not compared by design and does not fail the run. Any other Not compared row still exits 2, values redacted for their shape included (two TLS cipher lists look like credentials and are not known to be equal), as do a truncated value, a missing inventory, a report with `complete: false` and an unreadable side (`compare_cli._incomplete`).

### Export as SQL and Table structure

`myflames/schema_export.py`: `schema_export.build(snapshot, database=None, create_database=False, keep_definers=False, meta=None, generated_at=None)` takes a validated `schema-snapshot-1.0` and returns `{database, source_database, options, label, taken_at, generated_at, statements, not_exported, warnings, notes, summary, filename, script}`. Python writes every sentence; myflames never runs the script; it creates and never drops.

- Script order: header comments (`-- N warnings.`, then a `-- WARNING:` line per unreadable object, coverage gap and snapshot note); `SET @myflames_<setting> = @@SESSION.<setting>` for every session setting the script changes; `SET NAMES utf8mb4` only when a statement holds text outside ASCII; optional `CREATE DATABASE` with the source's options (the `/* READ ONLY = 1 */` comment dropped); `USE`; sequences; tables in foreign key order (Tarjan components, parents first, ties by name; a self reference stays inline; foreign keys among tables of a cycle, to another database, or to a table not in the script are left out of CREATE TABLE and added with `ALTER TABLE ... ADD` after all tables, the last two with a `-- WARNING:`); routines and packages (package before body); views in dependency order; triggers grouped by table, timing and event in ACTION_ORDER with FOLLOWS and PRECEDES removed; events; `ALTER DATABASE ... READ ONLY = 1` when a MySQL source database is read only; the saved settings back; `-- Notes:`.
- Session settings: tables, sequences, foreign keys and views run under `sql_mode = 'NO_ENGINE_SUBSTITUTION'`. Views get their recorded character_set_client and collation_connection; routines, triggers and events also their recorded sql_mode, and events their time_zone. A setting an object does not record returns to the session's saved value (`@myflames_<setting>`), never carries over from the object before; the ASCII check reads the text as the script writes it (another database name can add characters outside ASCII); views are ordered by names compared the way the server does (lower case under `lower_case_table_names` 1 and 2). A recorded character_set_client is replaced by utf8mb4 only when the object's text is outside ASCII and the recorded one is not a UTF-8 set. Compound statements use a DELIMITER that does not occur in the body (`$$`, `;;`, `//`, `$$$$`, then `$$N`).
- Definers are left out by default (the account that runs the script becomes the definer, said in a note); `keep_definers` keeps them with a note on the privilege and on missing definer accounts (MySQL 8.2+ ALLOW_NONEXISTENT_DEFINER). `database` requalifies backquoted `` `source`. `` references outside literals and comments (never a name after a dot; when the database name is also an object name, as a table named like its database, or appears on its own as an alias (`` from `shop`.`orders` `shop` ``), only before an object name or a third part, so `` `orders`.`id` `` and `` `shop`.`id` `` stay columns; Schema versions comparisons and the sync script use the same rule, `schema_ddl.requalify(text, old, new, names)`); a bare `source.` reference in a routine, trigger or event body is not changed and is named in a note.
- `-- WARNING:` lines: unreadable objects with their reason, coverage gaps, snapshot notes, a sequence starting again at START WITH, system-versioned tables created without history, DATA/INDEX DIRECTORY, a general tablespace that must exist, encrypted tables, events with a past AT or ENDS time, events marked DISABLE ON SLAVE or REPLICA, a view on a view not in the script, a view that calls an unreadable function of the database (MySQL 5.7, 8.4 and MariaDB 11.4 refuse CREATE VIEW then, ERROR 1305), a trigger on a table not in the script, a trigger whose order group has unreadable members. An account that cannot see every object should not export: the script stops at the first view that calls an unseen function.
- `POST /api/schema-versions/export-sql` (`schema_versions_api.EXPORT_SQL_PATH`, in `PATHS` and `LIVE_PATHS`): `id` (a saved version), `live: {connection, database}` (read now; the saved password is filled for that read only) or `export_id`, plus `database`, `create_database` and `keep_definers` (booleans, default false). The response is `build()` without `statements`, plus `kind` (`version` or `live`), `server` and, for a live read, `export_id`. A live read is kept in a bounded per-workspace cache (4 reads, 64 MiB, 10 minutes) so a changed option rebuilds without reading the server; an expired key answers "This read is no longer kept; read the database again to export it." Not a versioned contract.
- Workspace (`SchemaExport.tsx`, `schemaVersionsApi.ts` `exportSql`): **Export as SQL…** in the saved version's inspector action bar and the Saved versions toolbar; the sheet has Name in the script, Create the database, Keep definers, the summary line, Review before running, the script, and Close, Copy script, Download .sql (`schema-<db>[-v<N>].sql`) and Open in Query editor. A rebuild greys the old script until the new one arrives; errors show once with Try again. Tests: `test/test_schema_export.py`, `apps/workspace/test/schema-export.mjs`, fixtures `test/fixtures/schema_export/`.

CLI help of `myflames schema export` (it gains the shared connection flags for a live side):

```
VERSION                Choose an ID, ID prefix (6+ characters), exact name or latest; with --sql also live, profile:NAME[/DATABASE] or file:PATH (.json also works).
--sql                  Emit a SQL script that recreates the schema on another server; never run it.
--create-database      Start the SQL script with CREATE DATABASE (the source's character set and collation); by default it only switches to the database.
--keep-definers        Keep the DEFINER of views, routines, triggers and events in the SQL script; by default they are left out.
--target-database NAME Write the SQL script for a database with this name instead of the source's name.
```

Description: "Export a saved version as a schema snapshot file, or a schema as a SQL script that recreates it (--sql)." Without `--sql` the export is unchanged. `--create-database`, `--keep-definers`, `--target-database`, a connection option or a live, profile or file side without `--sql` exit 2 with "<option> needs --sql: a schema version file is exported from a saved version only." The script goes to stdout (or `-o`); stderr gets one note ("N statements for DB: ...; N warnings. myflames never runs it."), suppressed by `-q`. The root help's schema line ends "sync script; export as SQL (export VERSION --sql)".

**Table structure** (`schema_browser.table_structure`, `table-structure-1.0`, `docs/schemas/table-structure-1.0.json`): `POST /api/live/table-structure` (`ui._CATALOG_PATHS`, saved password filled, 30 s, read-only) with `connection`, `schema`, `table`. Fields: `schema`, `table`, `type`, `engine`, `collation`, `comment`, `columns` (`name`, `position`, `type`, `nullable`, `default`, `extra`, `comment`, `character_set`, `collation`, `key`, `generated`), `indexes` (`name`, `primary`, `unique`, `type`, `comment`, `visible` from IS_VISIBLE on MySQL 8.0+ or not IGNORED on MariaDB 10.6+, null otherwise, `columns` with `name`, `expression` (MySQL 8.0.13+), `sub_part`, `order`), `foreign_keys` (`name`, `columns`, `referenced_schema`, `referenced_table`, `referenced_columns`, `on_update`, `on_delete` as the server reports them: MySQL 8.0+ NO ACTION, 5.7 and MariaDB RESTRICT for an unspecified rule), `create_table` (exact SHOW CREATE TABLE, AUTO_INCREMENT kept) or null with `create_error`, and `filename` (`<schema>.<table>.sql`). Reads: information_schema TABLES, COLUMNS, STATISTICS, KEY_COLUMN_USAGE and, only with foreign keys, REFERENTIAL_CONSTRAINTS, each `SELECT *` by column name with hex literals, tagged `/*myflames:editor*/`, then SHOW CREATE TABLE; a refused SHOW CREATE TABLE keeps the rest, a lost connection fails the read. Workspace (`TableStructure.tsx`, `table-structure.css`): **Table structure** is first in the Tables inspector action bar, the row's ⋯ menu and its context menu, disabled with the reason on a list from another connection; the sheet reads once when it opens; tabs Columns, Indexes, Foreign keys, CREATE TABLE; Close, Copy CREATE TABLE, Download .sql. `TextSheet` (Close first, focus returned, the body scrolls, actions stay in view at 720x520) is shared with Export as SQL. Tests: `test/test_table_structure.py`.

### Query digest sources and Time range

In `QueryDigest.tsx`, **Tables** reads when a statement opens with the **Slow log table** source (`TablesSection` with `eager={report?.kind === "table"}`). The Statement digests source reads that section when it comes into view; Slow log file has no Tables read. Existing connection, visibility, cancellation and cached-result guards still apply.

Query digest separates the title and actions from a grouped filter row, with wrapping controls at narrow widths. Busy action labels stay centered within their reserved width.

New modules: `myflames/slowlog.py` (parser, fingerprints, aggregation, `slow-log-1.0`), `myflames/slow_queries.py` (`mysql.slow_log` as a source), `myflames/slowlog_api.py` (workspace endpoints, native chooser), `myflames/slowlog_cli.py` (`myflames slow-log`). They replace the retired "Slow queries over 1 s" Rank by (`/api/live/slow-queries`, `discover.slow_queries_report`, removed). Contracts: `docs/schemas/slow-log-1.0.json` (file and table alike) and `docs/schemas/slow-log-progress-1.0.json` (bytes only, no SQL, no path).

- Endpoints (`ui.py`, `_SLOW_LOG_PATHS` in the `do_POST` allowlist; the file paths need a running workspace): `/api/slow-log/choose` (macOS osascript `choose file`, Windows OpenFileDialog with "Log files (*.log;*.txt)" and "All files", Linux zenity; fixed scripts, shares `folder_picker._DIALOG_LOCK` and `file_picker._run_dialog`; `{cancelled: true}` on cancel), `/api/slow-log/open-path` (typed path), both checked by `slowlog.check_path` (absolute, regular, readable, at most 4 GiB) and kept in this instance's memory under a random `file_id` (`SlowLogFiles`, at most 16 files for 6 h; never saved; replies carry the file name, never its folder); `/api/slow-log/read` `{file_id, progress_id?, order, limit, from, to, user}` (at most 2 reads at once); `/api/slow-log/progress` and `/api/slow-log/stop` by `progress_id` (8 to 64 of `[A-Za-z0-9_-]`). `/api/live/slow-log` `{connection, order, limit, from, to, user}` is in `_SERVER_VIEW_PATHS` (saved password filled), validated before connecting (`slow_queries.check_request`), 30 s query limit.
- Reading a file: streamed line by line in binary (Stop and progress checked every 4,096 lines and every 4 MiB, also while skipping the rest of a long line), lines over 1 MiB cut (rest skipped, `parse.long_lines`), statement text up to 1 MiB per entry (`truncated_statements`), the sample (the slowest call) up to 64 KiB; at most 50,000 different (schema, fingerprint) groups, later ones summed into `others` with a note (told apart by a hash, up to 200,000, then counted "at least"); malformed entries and runs of stray lines counted in `parse.malformed`, the first 100 listed with line and reason. A 100 MB log of repeated statements read in about 2.3 s on the lab computer.
- Formats: MySQL 5.7 to 9.x (`# Time:` ISO 8601 with `Z` or an offset, `# User@Host: ... Id:`, the Query_time line with every log_slow_extra pair, `use db;`, `SET [last_insert_id=,][insert_id=,]timestamp=;`, `# administrator command:`, server header blocks anywhere, short format), MariaDB (`# Time: YYMMDD H:MM:SS` only when the second changes, `# Thread_id: n  Schema: db  QC_hit:`, `# Rows_affected`, log_slow_verbosity lines, ``use `db`;``), Percona Server (every `# Key: value` pair; written from documented lines, not checked against Percona source). The database is the entry's own `Schema:` or the last `use` line.
- Times are never moved to another zone. Each report has `clock` (`utc`, `offset`, `local`, `mixed`, `none`, `server`) with a label and one sentence, and `time_meaning` (MySQL: the time the statement was logged, after it ended; MariaDB: its start). MariaDB entries before a file's first `# Time:` line take their time from SET timestamp at the UTC offset the log shows, counted in `parse.inferred_times`, with a note. For `mysql.slow_log` (a CSV table that stores text) each row's time is in the time zone of the session that ran the logged statement; the clock sentence names this session's zone (`time_zone`, `system_time_zone` and the offset) and says that a client setting its own time_zone logs in that zone.
- Range: From and To are wall-clock values in the report's clock, both inclusive to the precision written (a To without seconds keeps that whole minute; a date alone the whole day), `slowlog.normalize_bound` in Python and `digestTimes.ts` in the page. The file source compares as written; the table source uses `start_time >= ... AND start_time <= ...` in SQL; entries without a time are left out of a range and counted in a note. Performance Schema: the page keeps loaded statements with FIRST_SEEN <= To and LAST_SEEN >= From and says "Numbers are totals since counting began, not only this range: the range keeps the statements seen in it." plus an ⓘ; no read. Quick ranges end at `anchor` (the file's last entry, the server's NOW() for the table, the newest LAST_SEEN for Performance Schema).
- Fingerprints: comments dropped, string, number, hex and bit literals as `?` (a sign before a number joins it), a parenthesized list of only values that is not a call's arguments as `(...)`, every VALUES row after the first dropped, white space collapsed, keywords and built-in functions in upper case, names as written. `fingerprint_id` is the first 16 hex characters of SHA-256 of the text; `digest` is always null for slow logs. A per-read cache keyed by the statement's shape skips re-tokenizing statements that differ only in values (a text with a backslash is never cached: its meaning depends on sql_mode). A string a backslash leaves open is read again with the backslash as a plain character (NO_BACKSLASH_ESCAPES). Lists nested deeper than 64 levels are not collapsed, so a deeply nested statement is read in linear time.
- Aggregation: groups by (schema, fingerprint). Percentiles follow Performance Schema's histogram (450 buckets, 10 µs base, 10^(1/50) growth; the upper edge of the bucket that holds ceil(count·q)) and never exceed the slowest call; each statement carries its sparse `histogram` in the digest-histogram bucket shape. Counters the log does not record are null, never zero (tmp tables and sort merge passes need log_slow_extra or MariaDB or Percona lines), and a counter some executions of a statement lack is null for that statement (log_slow_extra can change while a log is written); MariaDB's Tmp_tables and Merge_passes lines, written only when nonzero under log_slow_verbosity=query_plan, count as zero once the log has them. Statements tagged `/*myflames:` are left out and counted (`parse.excluded_own`); the table read excludes them in SQL.
- `mysql.slow_log` read: one context SELECT (`NOW(6)`, `@@session.time_zone`, `@@system_time_zone`, the offset, `@@global.log_output`, `@@global.slow_query_log`, `@@global.long_query_time`), one span SELECT (`MIN` and `MAX(start_time)`, `COUNT(*)`), one bounded SELECT of the rows in the range, newest first, at most 20,000 (`source.capped` and a note), `CONVERT(LEFT(sql_text, 8192) USING utf8mb4)` with `CHAR_LENGTH`. The client stops at 64 MiB of output, so unless the span count shows that every row fits, a length read (`OCTET_LENGTH` of that text, same range and order) comes first and the main read takes the newest rows that fit 40 MiB (`READ_BUDGET`; the note then says the statements' text limited the read). The User and Database choices filter the rows read, and a capped note says so. Notes: the log goes to a file (`log_output` without TABLE: "choose Slow log file as the source"), the slow log is off, when the server logs a statement (MySQL 8.0.28 and later: the whole time, lock waits included, exceeds long_query_time; 5.7 and 8.0 before 8.0.28: the time after tables are locked exceeds it; MariaDB: the time after tables are locked is at least it), and a denied read says `GRANT SELECT ON mysql.slow_log TO <user>;`.
- Workspace (`QueryDigest.tsx`, `DigestRange.tsx`, `digestTimes.ts`, `slowLogApi.ts`, `query-digest.css`): see LOCAL_UI's Query digest. Source picks Statement digests, **Slow log table** or **Slow log file**; the slow log sources add a Rank by (Total time, Calls, Average time, Slowest call, P95, Lock time, Rows examined, Rows sent) and a User select. The file source's empty state is "Open a slow query log" (**Choose file…**, **Type a path instead**, **Open another file…**). The inspector shows Latency, **Who ran it**, **Fingerprint** and **Sample (the slowest call)**, with no Tables section for a file. **Time range** offers Any time, Last hour, Last 24 hours and Today. Changing Source starts the range and user over. The file source's progress bar is polled every 500 ms and Stop also tells the server; the summary line is `Slow log file: <name> · {n} statements · {total} total · {executions} executions · {range}` (or `mysql.slow_log · ...`). The Tables section reads only for the table source. Text export adds Source, Logged by, Clock, Time range, User and per-statement Fingerprint, Fastest call, Users and Hosts; CSV adds Last Seen, Min Time (ms), Users and Fingerprint ID.
- Tests: `test/test_slowlog.py` (real logs in `test/fixtures/slowlog/` from MySQL 5.7.44, 8.4.13 and MariaDB 11.4.13 against what `mysql.slow_log` recorded for the same statements), `apps/workspace/test/digest-times.mjs` (Node), `apps/workspace/test/query-digest-slowlog.mjs` (browser, `npm run test:query-digest-slowlog`).

CLI help of `myflames slow-log`:

```
usage: myflames slow-log [--help] [--profile NAME_OR_ID] [-h HOST] [-P PORT] [-u USER] [-p [PASSWORD]]
                         [-D DATABASE] [--ssl-mode ...] [--ssl-ca PATH] [--ssl-cert PATH] [--ssl-key PATH]
                         [--mysql-binary PATH] [--table] [--from TIME] [--to TIME] [--executed-by USER]
                         [--schema NAME] [--order {total,count,avg,max,p95,lock,examined,sent}] [--limit N]
                         [--json] [-o PATH] [-q] [FILE]

Sum a slow query log by statement fingerprint: count, total, average, P95 and maximum time, lock time and
rows, within an optional time range. Read a log file (MySQL 5.7 to 9.x, Percona Server or MariaDB), standard
input, or the mysql.slow_log table of a server with --table.

  FILE                  Read this slow log file ('-' reads standard input).
  --table               Read the mysql.slow_log table of the server the connection flags name instead of a file.
  --from TIME           Keep entries logged at or after TIME (YYYY-MM-DD HH:MM[:SS], in the log's clock).
  --to TIME             Keep entries logged at or before TIME (inclusive to the precision written).
  --executed-by USER    Keep only statements this user ran (the user of the User@Host line).
  --schema NAME         Keep only statements run with this default schema.
  --order {...}         Rank by total time, calls (count), average, maximum, P95, lock time, rows examined
                        or rows sent (default: total).
  --limit N             Show the top 1-1000 statements (default: 20).
  --json                Emit the slow-log-1.0 report as machine-readable JSON.
  -o, --output PATH     Write the result to this file instead of stdout.
  -q, --quiet           Suppress notes on stderr; rely on stdout and the exit code.

Times are compared as the log writes them, in its own clock (UTC, an offset, or the server's local time; the
report says which). --from and --to take YYYY-MM-DD HH:MM[:SS] and are inclusive to the precision written.
Read-only: a file is only read; --table sends only SELECTs (mysql.slow_log and the server's time zone).
Examples: myflames slow-log /var/log/mysql/slow.log --from '2026-10-31 09:00' --to '2026-10-31 10:00';
myflames slow-log --table --profile prod --order avg --limit 10. Exit codes: 0 success, 2 bad input,
unreadable file, or connection failure.
```

stdout is the ranked table (or JSON); stderr has `source:`, `clock:`, `counted:`, `range:` and `log spans:` lines and `note:` lines (all suppressed by `-q`). The user filter is `--executed-by` and the schema filter `--schema` because `-u/--user` and `-D/--database` are the connection flags. Standard input is read line by line and refused past 4 GiB. The root help lists "slow-log  Sum a slow query log file, or mysql.slow_log with --table, by fingerprint (--from, --to)".

### Structure-only dumps

`dump_options.capability_catalog` gains the boolean `structure_only` (group `contents`, row `Rows`, default false, label "Structure only (no rows)") for every logical dump tool (mysqlsh, mysqldump, mariadb-dump) and for instance, schema and table scope alike. It goes through `dump_options.normalize` like every other option (unknown keys still refuse; it must be a real boolean), so it is bound into the reviewed plan and its binding hash. mysqldump and mariadb-dump add `--no-data` only when chosen; triggers, routines and events still follow the Include options. MySQL Shell sends `ddlOnly: true` inside the options object of dumpInstance, dumpSchemas and dumpTables. `prepare_dump` adds the warning "Structure only: the dump has table and object definitions and no rows." `lifecycle-manifest-1.0` gains the additive boolean `options.structure_only` (written for every new dump; old manifests read as false). A structure-only dump records no `samples` (no COUNT(*) is taken) and its verification says `source_sample_stability` and `dump_rows` are not checked for that reason. `prepare_restore` warns that the dump is structure only; the restore compares objects (`object_presence`) as before but replaces the per-table row count checks with one `row_count` not_checked line, "Structure-only dump (no rows): row counts were not compared."; nothing a restore accepted before is refused, and a dump with rows is verified exactly as before. Workspace: the choice renders from the capability catalog in the Contents section (no bespoke control), `optionsSummary` in `DumpOptions.tsx` names it, and the review sheet lists "Contents: Structure only (no rows)" in its Fields (`Maintenance.tsx`). CLI flag of `myflames dump` (a modifier of the existing capability, named after the mysqldump option it maps to): `--no-data  Dump table and object definitions only, no rows.` It sets `options={'structure_only': True}` on `prepare_dump`, works with MySQL Shell and with `--no-shell`, and the preflight says "Structure only". Verified live on MySQL 5.7.44: mysqldump 8.0.45 with and without `--no-data` (the COLUMN_STATISTICS issue is only a warning), MySQL Shell 9.6.0 `dumpSchemas` with `ddlOnly` and `loadDump` back, and the CLI end to end including restore. `mariadb-dump` was not run live (unit tests cover its command line). Tests: `test/test_dump_options.py`, `test/test_lifecycle.py`, `test/test_lifecycle_cli.py`, `apps/workspace/test/maintenance-convenience.mjs`.

### Saved connections and Database diagram Load

`savedConnections.ts` (not `main.tsx`) owns the one list of saved connections per window. `useSavedConnections(load = true)` (React `useSyncExternalStore`) returns `{items, loaded, error, secretStore}`; `refreshSavedConnections()` reads `profiles/list` (the workspace store on this computer: no server is read, nothing is sent). The latest read wins: an older answer, success or failure, never replaces a newer one, and every caller gets the latest read's answer (write-policy checks keep that rule). An unchanged answer keeps the list's identity and publishes nothing. When it is read: once the workspace session is restored (the shell, `useSavedConnections(false)` plus its first read), after every create, edit, rename, delete, password save or forget in this window (Connections calls the shell's `refresh`, which is `refreshSavedConnections`), when the window comes back (`focus`, `visibilitychange`; at most once a second, only after a first read), and when a Compare tab opens or Refresh choices is pressed. A component shown on its own reads it once on mount. Every list reads it: the connection picker, Home's Connections, Compare's Source and Target (`ServerCompare.tsx`), Schema versions' Compare sides, Maintenance's clone donor, Cluster's member switch and the Audit log's connection names (the last four through the shell's props). A chosen side keeps its choice while it exists (choices are ids); a chosen saved connection that was deleted stays chosen and says so: "<name> (deleted)" (`deletedConnectionLabel`, from the names this window has seen, "A deleted saved connection" otherwise), the setup says "Target: the saved connection “<name>” was deleted. Choose another target." and Compare stays off; Schema versions' side reads "<name> (deleted)" or "<connection> · no saved password"; Maintenance's clone donor "Fill from saved connection" names "<name> (deleted)" and says the copied fields stay. The connection in use deleted in another window or with the CLI is kept on the next read as an unsaved connection and the picker shows "<name> (not saved)" with the note "The saved connection “<name>” was deleted. Its settings are kept as an unsaved connection." (a deletion on Home still clears it). Tests: `apps/workspace/test/saved-connections.mjs` (`npm run test:saved-connections`), `profiles.mjs --unit`.

Database diagram (`SchemaDiagram.tsx`, `schema-diagram.css`): choosing a database never reads it (a diagram read can be large, a keyboard walk through a closed select fires one change per arrow press, and a shown diagram is never replaced without asking); opening the place still reads nothing and Server's Refresh all still reads the chosen database once. Empty state: a live database chosen shows "See how the tables of <db> connect", one sentence naming what is read and through which connection ("Nothing on the server changes."), primary **Load <db>** (`.diagram-load`, an `ActionButton`) and secondary **Import a schema**; its progress (`Reading <db>…`, seconds after 1 s, Stop once slow) and error with **Try again** show once under the button (`.diagram-load-status`, `ActionStatus keepLine`), and the status line under the toolbar takes them over only once a diagram is shown; a load started there moves focus to the canvas. A connection but no database shows "Choose a database"; no connection leaves **Import a schema** as the only, primary action. The toolbar's one Refresh (`data-action="refresh"`) reads **Load** or **Loading…** until the diagram on screen is the chosen database read from the connection in use, then **Refresh** or **Refreshing…**. Another database chosen while a diagram is shown (and only after the user chose it) shows `<db> is chosen. The diagram shows <shown>.` with **Load <db>** (`.diagram-choice`), and the "Replace this diagram?" sheet confirms with **Load <db>**. A diagram from another connection says "This diagram was read from <connection>." with **Load <db>**. **Diagram | Mermaid** shows only with a diagram (Mermaid files import through Import a schema, format Mermaid). **Reload database list** is an icon button (`.diagram-reload-list`) and "Database list updated." shows only after it was pressed. Shared kit: `ViewLayout` takes `refreshLabel` (default "Refresh") and `refreshBusyLabel` (default "Refreshing…"), used as the button's `aria-label` and in the other-connection note, and `otherAction.text` replaces the note's generated sentence. Tests: `schema-diagram.mjs`, `action-feedback-batch-b.mjs` (`diagram-load`, `diagram-rebuild`).

### Connection readiness and import

Connection readiness (`myflames/readiness.py`, `POST /api/live/readiness`;
in `_CONNECTION_PATHS`, not a write path, so the saved password fills for an
optional `profile_id`) answers `{engine, flavor, product, version,
read_only, super_read_only, performance_schema, replica, group_member,
galera_member, privileges, actions, notes, checked_at}`; a failure returns
the usual `{error}` response with the password redacted. State flags are
booleans, or null when unavailable. `privileges` maps PROCESS,
CONNECTION_ADMIN, "CONNECTION ADMIN", SUPER, REPLICATION CLIENT and SLAVE
MONITOR to true, false or null through `privileges.has`; `actions` carries
the `privileges.can` decisions for `sessions_all`, `kill` and
`replication_status` (`state`, `allowed`, `available`, `missing`, `need`,
`reason`, `via`, `note`, `grey_out`). `checked_at` is an ISO UTC timestamp.
One initial client invocation (`conn.run_script(BASE_SQL)`, tagged
`/*myflames:readiness*/`, connect timeout 3 s) reads filtered global
variables, two `wsrep` status values and this account's SHOW GRANTS, followed
by up to two `probe()` (`conn.run`) topology invocations, all sharing one
monotonic 3 s deadline (`remaining()`). Replication probes on MySQL and
Percona use `SELECT 1 ... LIMIT 1` over the configured channel metadata; group
membership reads only the local MEMBER_ID with LIMIT 1; MariaDB uses `SHOW ALL
SLAVES STATUS`; MySQL with Performance Schema off can use `SHOW REPLICA
STATUS` (8.0.22+) or `SHOW SLAVE STATUS` when privileges permit (SHOW has no
LIMIT: these paths enumerate configured channels only, under each invocation's
64 MiB stdout and 1 MiB stderr caps and the shared 3 s deadline). No application tables or workload samples are read, no
server writes are performed and action authorization does not change. MySQL
5.7 lacks CONNECTION_ADMIN and uses SUPER for other-user Kill; a missing
`super_read_only` stays null. The frontend (`readiness.tsx`) caches only
allowlisted readiness facts and the non-secret connection fingerprint under
`myflames.connectionReadiness.v1` (inside try/catch), restores them as stale
with their "seen" time, keeps them after errors, shows an error for six
seconds and joins concurrent checks of the same connection. Explicit picker
choices, **Test connection** and **Check now** in a connection's menu request
readiness; mounting or restoring the app never does. Home's Connections table
shows the facts in its **Server** column, after **Database** (tooltip "Default database"; the connection form keeps **Default database**). The Server cell gives product and version priority over the "seen … stale" hint, which truncates first with the full text on hover. Default columns are sized for 1440 px. Tests:
`test/test_readiness.py`, `apps/workspace/test/connections-readiness-unit.mjs`
and `connections-readiness.mjs`.

Connection import (`myflames/connection_import.py`, `POST
/api/connections/import`, `ImportConnections.tsx`) accepts only `{tool:
"workbench"|"dbeaver", choose?: boolean}`; any other field (a path included)
is rejected. Without `choose` it reads only the fixed macOS and Windows
default locations (Workbench `connections.xml`, DBeaver `data-sources.json`;
other platforms return not found). `choose: true` opens a fixed-argv native
file dialog (a 120-second helper timeout through `folder_picker`). The reply
is `{items, found, cancelled}`, with `items` holding `name`, `host`, `port`,
`user`, `database`, `type: "production"` and optional `ssh_host`,
`ssh_user`, `ssh_port` (plus a per-entry `error`). Only the two exact
filenames, regular non-symbolic-link files up to 2 MiB, UTF-8 XML without
DTD or entity declarations or UTF-8 JSON, and at most 1,000 matching
connections are accepted. Non-MySQL/MariaDB drivers and providers are
ignored; Workbench TCP/IP and TCP/IP-over-SSH and DBeaver direct and basic
SSH handlers are supported; URL-only targets, sockets, missing hosts,
unsupported proxies or tunnels, SSH jump chains and custom forwarding are
refused. No password, SSH key or credential store is opened or returned. The
preview unchecks duplicate names and endpoints, including duplicates within
the same file; the import re-reads the local saved profiles, saves only the
explicit fields through `profiles/save` with `type: "production"`,
`remember_password: false` and TLS at PREFERRED, preserves partial successes
for retry, and never activates an imported connection. The import sheet uses a wider preview (`--connection-import-width: 104ch`, bounded by the window) for its Select, Name, Endpoint, Database and SSH columns; an alert is rendered only when there is an error. Neither route creates
a server-write audit record; both use the existing POST authentication and
body limits. Tests: `test/test_connection_import.py`.

### Learn guided lessons

A lesson opts in with two edits; `myflames/teach/_html.py` and `_anim.py` do
the rest. In the lesson's `.js` `build()` timeline, `tl.beat("<kind>")` marks
where each guided step starts (one idea per step: one page read, one row, one
probe), before that step's caption call. The first step always starts at the
timeline's first step. Every kind appears as a string literal in the `.js`
(build kinds from a table, as `btree.js` `BEAT_KINDS` does, never by
concatenation), and `build()` only resets the stage besides returning the
timeline, because the guide builds one to count steps while the lesson is
idle. In the lesson's `.py`, `GUIDE` goes to `_html.render_page(...,
guide=GUIDE)`:

- `code`: `title` (at most 60 characters) and 3 to 14 `lines`, each `(id,
  indent 0 to 4, text up to 72 characters[, MySQL function])`.
- `beats`: every `tl.beat` kind mapped to the line ids it runs.
- `quiz`: two to four questions (at most 240 characters), each with two to
  four choices, exactly one true, each with a one-sentence reason (no `e.g.`,
  no second period, at most 260 characters).

`_html.normalize_guide` refuses a bad guide with a `ValueError` naming the
problem (ids and kinds are lowercase letters, digits and hyphens; line ids are
unique; every beat names existing lines), and `test/test_teach.py`
(`TestLessonGuides`) also checks that every line is reached by some beat and
every kind is a literal in the `.js`. A function name is allowed only from
`_html.VERIFIED_SERVER_FUNCTIONS`: add one only after finding it in
`mysql-server` at the `mysql-8.4.9` tag and in the 9.x checkout, with its file.
MariaDB gets no function notes, and the panel's footnote says the names are
MySQL's. Every claim must hold for MySQL and MariaDB as the lesson presents
them, naming the engine where they differ; route new claims to
`mysql-correctness-reviewer`. B-tree lessons say root page, internal page and
leaf page, never level n. The panel is labelled Simplified.

On the page, **Back** and **Next step** (the main action) play one step and
hold, and Play runs the rest; jumping to a step or scrubbing never flashes the
pages visited, and the Phases list marks a phase only once its first step has
begun. The code panel sits beside the drawing (under it in a narrow window,
and in full screen) and highlights the lines of the current step. **Check
yourself** answers at once with every choice's reason; nothing is saved or
sent. **Explore** gives a slider over every step and, when the lesson's
`section.controls` has inputs, an **Inputs** button under the step bar;
changing an input shows the same step again (**Replay step** plays it), and
leaving Explore continues the guided steps at that step. A page without
`#lesson-guide` stops the shared guide script at once and changes no DOM. The
suggested ready caption is `Ready: press Next step, or Play to run it all`, and
the lesson's explainer gets one bullet saying Next step and the simplified
code exist. Every lesson has a guide (23: the index, join, scan, cache and
planner families), and `test_lessons_without_a_guide_still_render` renders
one with `guide=None` to keep that path working. A lesson's family chip comes
from `_html._LESSON_FAMILY_LABELS` (Skip scan and Rowid filter are Index
access; Batched key access and both semijoin lessons are Join operator).
Function notes in use cover the InnoDB B-tree, row and buffer pool calls,
the handler scan calls, the composite, hash join, BKA/DS-MRR, weedout,
temp-table, materialization and filesort iterators and functions, and
`Optimize_table_order::greedy_search`,
`::best_extension_by_limited_search` and `::best_access_path`; 9.x moved
iterator bodies from `Read()` to `DoRead()` while keeping `Read()` on
`RowIterator`, so names follow the 8.4.9 spelling. A long `Class::method`
name wraps after `::`, then after an underscore, never inside a word
(`.code-fn-part`). The Filesort lesson has a **Server** input: MariaDB's
radix sort applies when the sort key is at most 20 bytes and the buffer holds
1,000 to 99,999 rows (`radixsort_is_applicable`), decided by the key length
(3 bytes for order_date), not the row size, for the in-memory sort and for
each spilled run; choosing a server moves `sort_buffer_size` to that
server's default (256 KiB MySQL, 2 MiB MariaDB) while it still holds the
other's. Browser: `apps/workspace/test/learn-guide.mjs` (`npm run
test:learn-guide`) walks every lesson with the expected lines of its first
two steps (Nested loop join last, since the 720 px and full screen checks
continue from it), plus per-lesson checks such as the LRU hit counts at
`innodb_old_blocks_pct=90`, the warmup dump rule, Join order with
`optimizer_prune_level=0` and the hypergraph steps, and Filesort's MariaDB
radix path; add a new lesson to that loop.

### Command line deprecation

Since 2026-11-06 the Python command line (`myflames <command>`, `python3 -m myflames`) is deprecated: myflames focuses on the desktop app, and new features, subcommands and flags go in the workspace only. The CLI keeps every command, flag, output format and exit code, gets fixes only, and is removed in a future major release announced in the CHANGELOG first.

- `cli.main` writes one line to stderr before any command runs (`cli.CLI_DEPRECATION`): it names the desktop app, its download page and the opt-out. Stdout, `-o` files and exit codes are untouched, so `--json`, digests and CI gates parse as before.
- Not shown for `ui` (the desktop app runs `myflames-backend ui --no-browser` as its backend, and `ui` keeps its own browser-launch notice), with `-q`, `-qq`, `--quiet` or an abbreviation (`--qui`, `--quie`) before `--`, consistent with the other deprecation warnings, or with `MYFLAMES_NO_DEPRECATION_WARNING=1`.
- `myflames-mcp` is deprecated the same way: every tool keeps working and none are added. `mcp_server.main` writes `mcp_server.MCP_DEPRECATION` to stderr before it imports the SDK (stdout carries the MCP protocol), so a missing SDK still shows it first; `MYFLAMES_NO_DEPRECATION_WARNING=1` hides it. Its server tools read the CLI's saved profiles (`~/.myflames`), never the desktop app's. Tests: `test/test_mcp_server.py` (`test_main_prints_the_deprecation_on_stderr_only`, `test_deprecation_is_shown_before_a_missing_sdk_error`).
- User documentation carries no CLI or MCP material: the README keeps one sentence saying both are deprecated, and the handbook (`docs/handbook/`, rendered by the website) and the website describe the desktop app only. CLI and MCP contracts stay in this reference for fixes.
- Tests: `test/test_cli_contract.py` (`CliDeprecationTests`).

### JSON and expected failures

An unavailable error log or cluster report is an exception: the read completed with an `available: false` result, so `error-log --json` and `cluster --json` write that envelope to stdout or `-o` even with exit 2. Connection and setup failures still emit nothing. See [Error log](#error-log) and [Cluster](#cluster-galera-percona-xtradb-cluster-innodb-cluster).

`--json` selects the result format. Expected failures before a result exists
(bad input, missing files, connection setup failures) exit 2 with a diagnostic
on stderr and empty stdout, including under `--quiet`. They emit no JSON error
object and leave the `-o` destination untouched. Plan commands and workspace
server commands share `cli._fail` for these failures; argparse usage errors
also exit 2 on stderr. Scripts must check the exit code before parsing a result.
A failed write to standard output (a closed pipe, a full disk) also exits 2
with a stderr diagnostic and no traceback (`cli._write_output`).

An unsent server-write refusal or decline also exits 2 with empty stdout and
an untouched output file, including with `--json`. Once an operation sent
statements, it retains its result schema for failures and partial results. A
loop that loses its connection after starting retains its final snapshot
(exit 2). Live loop events go to stderr; completed loop text and JSON use the
same stdout/file writer.

### `--quiet` / `-q`

Uniform across root rendering, `capture`, `explore`, `teach`, `guide`, plan analysis and server commands: suppresses incidental stderr diagnostics (file/status announcements, tokenizer notes and deprecation warnings); stdout data and the exit code are untouched. Errors and required confirmations remain visible. Deprecated-alias warnings use the parsed quiet flag, including grouped `-qq` and accepted long-option abbreviations such as `--qui`. Registered via `_add_quiet_flag` and routed through `_write_output(..., quiet=...)` in `cli.py`.

Server text on a terminal: `capture` and the server commands write text and JSON
through `workspace_cli._emit_text`, which applies `connector.terminal_text`, as
do their stderr notes, `--fail-on` lines and errors (`_fail`), table cells
(`_table`, `_compact`), the `locks --kill` confirmation, and the live-connection
errors in `cli.py`. C0 controls except newline and tab, DEL and C1 controls print
as `\uXXXX`, so another session's SQL cannot move the cursor, rewrite a prompt
or set the clipboard (OSC 52). JSON keeps its values: `json.dumps` already
escapes C0, and the C1/DEL escapes are JSON escapes for the same characters.
Use `terminal_text` for any new plain-text output of server strings.
### MCP server

- **MCP server** (agent tools, deprecated with the CLI: see [Command line deprecation](#command-line-deprecation); no new tools): `myflames-mcp` (optional extra: `pip install myflames[mcp]`, constrained to `mcp>=1.2,<2` for the verified `mcp.server.fastmcp.FastMCP` API). MCP 2.x is not supported by this transport. Import failures report the installed SDK version (or not installed; unknown when version metadata is unavailable on Python 3.7), the original import error and the install command. Tool imports stay lazy and the core needs no SDK. Exposes `analyze_plan`, `digest_plan`, `compare_plans`, `explain_optimizer_switch`, `explain_query` (one SELECT only, checked by `live.bind_query` before any connection; `MySQLConnection.explain_analyze` sends `SET SESSION TRANSACTION READ ONLY; START TRANSACTION READ ONLY;`, the EXPLAIN ANALYZE (MariaDB `ANALYZE`) and `ROLLBACK` in one client session like capture, the client stopping at the first error so the query never runs outside the transaction; the transaction refuses writes to ordinary tables, not changes to existing temporary tables, GET_LOCK or variables set inside a stored function). Tool logic in `myflames/mcp_server.py` is stdlib + tested; only the transport needs the extra. Register: `claude mcp add myflames -- myflames-mcp`. `analyze_plan` defaults to `detail="core"` (token-cheap decision-grade sidecar); `detail="full"` restores the complete record (adds `collected`/`query`/`teach_hooks`). The `detail` gate lives on `build_sidecar` in `output_sidecar.py` (single source of truth, shared with the CLI `--sidecar` path, which stays `full`); the digest path (`digest_plan_tool`) also stays `full`.

`analyze_plan_tool` and `digest_plan_tool` accept raw EXPLAIN JSON and `capture-1.0` JSON text. Captures use the same `live.normalize_capture` validator as the CLI, then the bounded `plan_input._plan` reader; missing or invalid raw plans raise `ValueError`. Raw-plan engine identity comes from the parsed plan, including query-plan envelopes. Captures retain their recorded engine and server version and feed schema, table statistics, variables and freshness evidence into the same advisor path as CLI digest/check/advise; both MCP plan tools preserve those findings.

The additional server tools are read-only. Each requires `profile: str` (a saved workspace profile name or ID), with `detail="core"` for compact text or `detail="full"` for the complete module JSON dictionary. `_server_read` uses `workspace_cli._resolve_connection` and `live.connection_options`, so the profile supplies host, user, database, TLS settings and any remembered password. There are no password, arbitrary-SQL, endpoint-override, or write arguments. Missing profiles or unavailable saved credentials fail through the normal profile/secret-store diagnostics. Saved passwords are redacted from both returned projections and errors.

| MCP tool | Module reader / full schema | Additional arguments |
| --- | --- | --- |
| `server_report` | `report.analysis_report` / `report-1.0` | `interval=5` (integer 2–60), `schemas=None` (list of schema names); the existing large-schema gate remains enabled. |
| `top_statements` | `discover.top_report` / `top-1.0` | `limit=20` (1–200), `schema=None`, `order="total"` (`total`, `avg`, `examined`, `count`, `full_join`, `scan`, `no_index`), `table=None` (qualified `schema.table`). |
| `sessions` | `sessions.sessions_report` / `sessions-1.0` | `active_only=True`, `min_seconds=0` (finite, nonnegative), `limit=1000` (1–10000). |
| `locks_report` | `locks.lock_report` / `locks-1.1` | `limit=500` (1–5000), `waits_only=False`, `deadlock_only=False`; the scope flags are mutually exclusive. |
| `health_sample` | `health.health_report` / `health-1.0` | `interval=5` (integer 1–60). |
| `innodb_status` | `innodb_status.innodb_report` / `innodb-1.0` | None; raw status text stays disabled, matching the module default. |
| `my_privileges` | `privileges.read_privileges` / `privileges-1.0` | None; includes the existing `can()` decisions, never an account mutation. |
| `replication_status` | `replication.read_status` / `replication-1.0` | None; no skip/start/stop arguments. |
| `cluster_status` | `cluster.read_status` / `cluster-1.0` | None; never changes membership or settings. |
| `readyset_status` | `readyset.read_status` / `readyset-1.0` | None; read-only status, caches, tables, allowlists, telemetry and topology; never runs cache DDL or `ALTER READYSET`. |

Core projections reuse `report.format_digest` and the compact text formatters in Sessions, Locks, Health, InnoDB, Replication, Cluster and Readyset. Statements retain normalized SQL, counters, coverage, and table-match caveats while omitting sample literals; Privileges retains credential-stripped grants, session scope, roles and grouped decision states. Reports can exceed 500 tokens: their size follows the observations and findings, without a fixed truncation cap. Full preserves the module's schema and evidence (including suggested SQL as data).

`_ReadConnection` checks every submitted statement, allowing only SELECT/SHOW. The Sessions and InnoDB readers normally prefix transaction-age queries with `SET SESSION time_zone = 'SYSTEM'`; this adapter removes those exact module-owned prefixes and converts `NOW()` with `CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', 'SYSTEM')` inside the SELECT instead. It rejects any other SET or non-read statement before sending it. The existing five MCP tools are unchanged, including `explain_query`, which executes a query through EXPLAIN ANALYZE.

Tests: `HOME=$(mktemp -d) MYFLAMES_SECRET_BACKEND=vault python3 -m unittest discover -s test -p 'test_mcp_server.py' -v`. Replays and module-test fakes cover MySQL and MariaDB, both detail projections, attribution orders, registered tool identities, all submitted SQL, the no-SET adapter, argument validation and password redaction. The transport extra is not required for those tests.

The workspace wraps non-Visual Explain SVGs in a focusable `.chart-scroll` region. Below 650 px, it preserves the 1200-unit canvas width and scrolls within the view instead of shrinking text. Desktop layout and renderer semantics are unchanged. `browser.mjs` verifies readable scale, scroll range and no page overflow at 390 and 320 pixels.

### MySQL Fixtures

`./scripts/generate-fixtures.sh` (requires Docker) to regenerate MySQL `test/fixtures/`.
### MariaDB Fixtures

`./scripts/generate-mariadb-fixtures.sh` (requires Docker) to regenerate MariaDB `test/fixtures/`.

## Visual Explain (merged Diagram / Workbench)

`charts/workbench.ts` (reports, explore, workspace) and `output_workbench.py`
+ `workbench.js` (standalone `.svg`) render the canonical tree using Workbench
26.7 visual conventions with scoped pan/zoom/search/selection; change both and
keep `parity.mjs` green. Preserve
every parsed node and edge. Colors encode operation type; arrow widths use
estimated rows. Keep estimated cost separate from measured milliseconds.
The UI and CLI share this renderer. `output_diagram.render_diagram` is a
compatibility wrapper; the UI exposes one Visual Explain tab. Preserve reported
`join_type` for Venn shading, and keep MariaDB synthetic joins explicitly unknown.
Join cards have their own height; edges must attach to the actual card bounds.
Wheel input uses normalized deltas with bounded animation (`workbench.js`, `charts/core/zoom.ts`). See `docs/WORKBENCH_VIEW.md`.
Update the zoom percentage and directional button limits in the shared paint
path so button, wheel, fit, and reset actions stay synchronized.
Fit keeps text readable: `MIN_TEXT_PX = 11` in `charts/core/zoom.ts`;
`createZoom` takes `smallestTextPx`, which `workbench.ts` passes as
`SMALLEST_TEXT_PX = 11` (the smallest font size in `WORKBENCH_CSS`,
`.wb-caption` and `.wb-edge-label`), and `workbench.js` carries the same two
constants. The fit ratio is `min(wanted, screenScale * smallestTextPx /
MIN_TEXT_PX)`, where `wanted` is the plain fit (the workspace: fit width, never
past 100%; reports and the standalone SVG: the whole plan) and `screenScale`
is 1 in the workspace (its ResizeObserver keeps one CSS pixel per unit) and
`root.getScreenCTM()` in reports, so a report column that shrinks the SVG
zooms in to keep 11 px. When the plan is larger than that view, the
overflowing axis starts at -28 (the first inputs, top left) instead of a
centered slice; the workspace keeps y = -28 always. The fit runs on open, on
Fit, on double click and on resize while fitted, so Fit always returns to the
opening view; zoom out and the wheel can still go below 11 px on request, and
the percentage still resets to 100%. The old 75% workspace floor is gone (the
workspace opens at 100%). Chart data is unchanged. Tests:
`test_workbench.test_fit_keeps_the_smallest_text_at_eleven_pixels`,
`zoom.mjs`, `browser.mjs`, `charts.mjs`, `investigation.mjs` (Compare panes
open at zoom 1).

Misestimate marks (see Advisor evidence rules) render in Python and
TypeScript alike, parity-tested, on the flagged operators each chart renders:
Visual Explain draws a chip (`rect.mfc-mis-chip` + `text.mfc-mis`, 11 px
semibold) and a dashed outline (`rect.mfc-mis-outline`, 2 px dashed 6 4, 3 px
outside the card; the card's accessible name gains ", rows ×N under"), the
execution tree puts the chip 6 px before the number columns, the bar chart
starts the label column with it on rendered bars (the bar chart omits operators
with `self < 0.001`), and the flame graph underlines the frame (`line.fg-mark`,
2 px inside the bottom edge, following zoom, keyed by `operator:<node_id>`).
The colors are `--mfc-estimate` / `--estimate` (#5b3cc4 light, #b3a4ff dark)
and the flame underline #3b2a8f, never the status, search or focus colors. In
the workspace the plan's measurements (`dl.plan-stats`) gain a **Misestimates
N** chip only when N > 0; pressing it reveals each flagged operator worst
first, then in plan order, wrapping, with Shift going back, through the
runtime's `ChartHandle.reveal(id)` (`ViewInstance.reveal`,
`ZoomController.show(x, y, w, h)`), and a `role="status"` line
(`.misestimate-status`) announces the position; when a flagged operator is
omitted from the bar chart (`self < 0.001`), it remains selectable through
**Misestimates**, stays selected in the inspector, and `.misestimate-status`
adds "Not shown in the bar chart (no self time); open Visual Explain or the
execution tree to see it". No new reads and no writes; the `ANALYZE TABLE`
statement in the inspector is copy-only.

## Investigation engine

- `myflames capture`: shared `LiveJob` engine; estimated/analyzed JSON capture,
  typed parameters, timeout, repeat, trace, and cancellation. Capture1.0 artifacts
  include run statistics and context, never connection credentials. Table
  statistics (`collectors.collect_stats`, `table_stats`) are read in batches:
  one `UNION ALL` of the per-table `information_schema.TABLES` SELECTs (each
  keeps its constant `table_name` and `table_schema` equalities, `DATABASE()` for
  an unqualified name, so both servers look the table up directly), tagged with
  the name's position, at most 100 tables and 24,000 characters per client call
  because the SQL is passed on the client's command line. Each name keeps the
  first row the server returns for it, as before; when a batch fails, its
  tables are read one at a time, so only a table whose own read fails is left
  out. Verified identical to the per-table reads on MySQL 8.0, 8.4, 9.7 and 26.7
  and MariaDB 10.6 to 13.0; `test/test_collectors.py`. `-e SQL` or
  `--statement-digest HASH` (a Performance Schema digest; adds `source`). `--digest HASH` remains a deprecated alias and warns on stderr unless `-q`.
- `myflames explore`: four interactive charts (`diagram` remains a `workbench` alias; `treemap` is a deprecated alias for `bargraph`) with `--metric`, `--focus`, repeatable
  `--collapse`, and `--selected` canonical node IDs.
- `exploration.py`: copy projections only; never mutate canonical measurements.
  Selection is linked with `myflames-node` / `myflames-select` messages.
- `plan_matching.py`: shared structural comparison. Uncertain matches and absent
  timing must not count as measured regressions. Honor `timing_available`.
- `workspace_store.py`: explicit opt-in SQLite persistence and versioned bundles.
- Investigation evidence (`workspace_store.validate_bundle`,
  `docs/schemas/investigation-1.0.json`): an optional `evidence` list of at
  most 30 items `{id, kind, captured_at, title, text, data}` (plus optional
  `data_trimmed`, `text_trimmed` and `note`), `kind` one of `sessions`,
  `innodb`, `locks` or `watch` (any other is refused). A bundle needs at least
  one plan or one evidence item (each list holds at most 30). Before storage
  every string in `data`, the text, the title and the note pass
  `dbops.redact`/`redact_value` (`IDENTIFIED BY`/`AS` literals,
  `PASSWORD('...')`, `SET PASSWORD`, `*PASSWORD =` options). Empty `text` is
  written from the redacted `data` by `watch.format_evidence_text` (the
  module's own `format_text`, else the report JSON). An item whose `data` exceeds 2 MiB
  (`EVIDENCE_ITEM_MAX_BYTES`), or a bundle over 10 MiB (`BUNDLE_MAX_BYTES`),
  loses `data` before `text`, with `data_trimmed: true` and a note.
  `_investigation_summary` adds `{id, kind, captured_at, title}` per item only
  when there is evidence, so plans-only investigations are unchanged.
  Investigations (`Library.tsx`) lists an Evidence section with each item's
  time and title, loads the full bundle on demand and shows the text in a
  monospace block with Copy and Download.
- `live.py`: preserve client SQL comments for statement tags; disable optimizer
  trace immediately after the target statement, before any marker SELECT.
  Status/P_S scope is the capture statement, not bare SELECT result delivery.
  The SELECT-only guard (`_select_problem`, shared by `bind_query` and
  `capture_problem`) accepts only text whose strings, quoted identifiers and
  comments have the same boundaries with and without backslash escapes: the
  mysql client splits the script itself and honors NO_BACKSLASH_ESCAPES, so
  `'it\'s'` is refused (write `'it''s'`), while `'a\nb'` and `'C:\\x'` pass.
  Unclosed strings and comments are refused. The script runs without `--force`,
  so a failed `SET SESSION TRANSACTION READ ONLY` or `START TRANSACTION READ ONLY`
  stops it before the query; nothing before the query may fail on a supported
  server (`explain_json_format_version` is set only on MySQL 8.3+). Ctrl+C in
  `_run_script` sends KILL QUERY (while the connection ID and password are known)
  before the client is ended. The client defaults file (`connector.py`) is
  created by `mkstemp` (0600 on POSIX; no `os.fchmod` on Windows) and removed if
  writing it fails.


### Completion and table-data contracts

`sqlCompletion.ts` supplies the bounded lexical context (statement bounds,
place, qualifiers, aliases, CTE names and the `@@` variable context);
`editor/completion.ts` is the CodeMirror completion source, and
`editor/catalog.ts` (`CatalogService`) is the metadata cache that
completion, hover and the problem warnings share. The cache is in-memory
and identity-scoped: the connection key, its configured database and any
credential change bump an epoch that aborts pending reads and invalidates
every entry. It has `schemas`, `tables` and `columns` keys read through
`/api/live/schemas` and `/api/live/catalog`, a 30 s lifetime (10 s after a
failed read), at most 64 entries (stale entries evicted first),
`failed`/`complete`/`truncated` coverage flags, `stale` and a `readAt` time
on every result. Expired entries from the same identity epoch remain usable
for suggestions and hover while one refresh per key runs in the background.
A failed refresh retains good stale data and delays automatic retries for
10 s. `peek` never starts a read and marks expired entries stale even before
their expiry timer fires. Completion also reuses names from other cached
prefix inventories without granting them complete coverage; a new prefix still
gets its own read. Shared reads track each owner separately: cancelling
one owner does not abort another owner's wait; identity or credential changes
abort all reads and clear fresh and stale entries at once. Nothing is written
to disk. This cache is not a model of the persistent editor session.

Suggestions use two CodeMirror completion sources. Local words, aliases, CTEs,
known variables and cached catalog names appear from the first typed character
without a debounce or waiting for IO. The remote source reads only missing or
stale metadata, debouncing those reads by 220 ms; Ctrl+Space reads immediately
and also asks when automatic suggestions are off, up to `HIGHLIGHT_LIMIT`.
CodeMirror merges arrivals into the existing open list, retaining the local
option objects and the highlighted option, without restarting completion.
The editor's `aria-expanded` follows CodeMirror's open-list `aria-controls`
attribute, including a disabled list retained during a read; a pending read
with no list stays collapsed. The content-attribute callback runs on every
view update, including completion effects with no document change.
The editor preloads engine words and variable names in an idle callback on
mount and when the engine or version becomes known. Suggestions cover
the target tables' columns with `schema.table · type` details, aliases, CTE
names, tables, schemas, the engine's own keywords
and functions for the connected server's release (see [Engine words for the
Query editor](#engine-words-for-the-query-editor); functions carry a
`N arguments` detail; a small common word list when the release
is not cataloged) and `@@` system variables from
`/api/config/versions/names`, filtered to the typed scope (`global`,
`session`, or both when no scope was typed) with deprecated names marked.
Fuzzy table matching requires a complete table inventory: when the schema
list reports at most 200 tables, the table list is read without a prefix;
otherwise it is read with the typed prefix, and a truncated answer shows
"More tables match. Keep typing to narrow the list." Catalog reads time out after 8 s ("Catalog
suggestions timed out. Try Ctrl+Space again.", "System variable suggestions
timed out. Try Ctrl+Space again."); failures show "Some catalog suggestions
are unavailable." or "System variable suggestions are unavailable." in a
status panel that never accepts HTML. At most 40 options, prefix matches
first. Inserts are quoted engine-aware: a name that is not a plain
identifier, or that is a keyword on the connected server's own release,
gets backticks with embedded backticks doubled; completing inside an
opening backtick keeps it and adds the closing one; accepting a function
inserts `NAME()` with the caret inside the parentheses, or only the name
when an opening `(` already follows.

Hover (`editor/hover.ts`, 350 ms) shows a table's columns (name, type, key
and null flags; the first 12, then "and N more") or one column's type and
flags, plus the table's estimated row count when a cached table list
already holds it: it reads columns through the catalog service and peeks
cached lists, never sending `stats: true`. Row estimates prefer the exact
table name; a case-insensitive fallback must identify exactly one table. Problem warnings
(`editor/problems.ts`; lint delay 750 ms, a gutter marker per warning, and
the panel and F8 navigation from `editor/commands.ts`) are a bounded
reading aid, not an SQL validity checker: above `HIGHLIGHT_LIMIT`, with a
DELIMITER line anywhere in the text, or inside strings and comments,
nothing is diagnosed; a statement containing an executable comment or
`USE`, and every statement after it, are not diagnosed (the
default database can no longer be trusted), nor is an unbalanced or
routine/compound statement. UPDATE or DELETE without
WHERE is warned, and an unknown table or qualified column is warned only
against a fresh, complete, untruncated cached list of that schema or table (`peek`
never fetches one; the table warning names the list's read time). References
to CTE names, `DUAL` and tables created earlier in the current script are
exempt from missing-name warnings. `env.sessionTempTables` suppresses all
missing-table warnings, but not missing-column or missing-WHERE warnings.
Warnings re-run when a catalog entry arrives or expires, never inside
CodeMirror's update loop.

`table_data.py` owns dedicated editor sessions and private, opaque snapshots. Snapshots and baselines stay server-owned and bounded. Exact review is
required for Development connections too. Apply only to supported InnoDB
tables with a verified reliable key and no triggers.
The table-data API accepts table names and typed row intents, not SQL, WHERE
clauses or client baselines. Review tokens bind the exact server-generated plan
to its connection. Apply consumes the token once and runs under a single-flight
snapshot lock. Initial loading holds a bounded explicit table READ lock.
Reject views and generated columns before reading their values. Editing also
refuses CHECK constraints, expression defaults, foreign keys, triggers and
unsupported automatic columns. A length-only probe bounds the subsequent HEX
response before the client retains it; both reads use the same held table lock.
Apply uses SET autocommit=0 and LOCK TABLES WRITE, with no subsequent START
TRANSACTION that would release the explicit lock. Metadata, authority, policy
and original values must still match before writes; policy is checked again before COMMIT. All changes share one
transaction, and loss of a commit reply is unknown, never retryable implicitly.
Audit creation must succeed before writes. Closing or expiring a snapshot ends
its owned session. See [the query guide](QUERY_WORKSPACE.md#sql-completion-and-table-data)
for supported table types, privilege requirements and limits.

## Import CSV

Import CSV loads a .csv, .tsv or .txt file into an existing table or a new
one. Entry points: the Query editor's Schemas panel (a table's menu:
**Import CSV…**; a user database's menu: **Import CSV into a new table…**)
and Server > Tables (a table row's menu and its inspector). There is no CLI.

**Files.** `myflames/file_picker.py` (`choose_file()`: the native dialog,
macOS osascript, Windows PowerShell OpenFileDialog, Linux zenity, 120 s, one
dialog at a time shared with `folder_picker`, no caller input;
`check_file(path)`: an absolute, resolved, readable regular file of at most
4 GiB, never a folder, device, socket or FIFO; spaces around a typed path
are trimmed only when the path as typed does not exist).
`myflames/csv_import.py` is the pure core (reading, detection, type
inference, conversion, quoting, statement builders; no server).
`myflames/csv_import_api.py` holds the endpoints, the file and plan
registries and apply. `myflames/ui.py` adds `_CSV_IMPORT_PATHS` to the POST
allowlist, puts apply in `_WRITE_PATHS` (none in `_CONNECTION_PATHS`: the
handler fills the saved password itself, after the policy) and calls
`csv_imports.close()` from `server_close`. Workspace:
`apps/workspace/src/ImportCsv.tsx` (the wizard, a lazy chunk),
`importCsvApi.ts` (wire types and calls), `import-csv.css`, with the entry
points in `SchemaBrowser.tsx`, `QueryLab.tsx` and `Tables.tsx`. Contracts:
`docs/schemas/csv-inspect-1.0.json`, `csv-import-plan-1.0.json`,
`csv-import-result-1.0.json`, `csv-import-progress-1.0.json`.

**Endpoints.** All POST JSON. `/api/import/csv/choose {}` and
`/api/import/csv/open-path {path}` answer `{file_id, name, size, modified}`
or `{cancelled: true}`; file ids map to absolute paths in memory only (32
files, one hour), and no later request names a path.
`/api/import/csv/inspect {file_id, format?}` answers `csv-inspect-1.0`: the
detected and effective format, columns with a suggested name, inferred SQL
type and samples, a 50-row preview, an exact row count for files up to
64 MiB (else an estimate with `rows_counted: false`) and at most 100
problems with physical line numbers. `/api/import/csv/plan {connection,
file_id, format, target, method, transaction, on_error, progress_id?}`
answers `csv-import-plan-1.0`: the review token (null when not ready),
target facts, the mapping, the full CREATE TABLE and a data summary, row
counts, problems, blockers, privileges, policy and warnings.
`/api/import/csv/apply {connection, token, progress_id}` answers
`csv-import-result-1.0`: the outcome, rows sent, committed and skipped,
`failed_at`, `server_error`, warnings, `table_rows_after` (a COUNT(*) only
when the estimate plus the committed rows is at most 1,000,000), `drop_hint`
and the audit id. `/api/import/csv/progress {progress_id}` answers
`csv-import-progress-1.0` (an unknown id gets a synthetic `phase: "unknown"`);
`/api/import/csv/stop {progress_id}` answers `{stop_requested, message}`.
Plan and apply carry the connection so the write gate and the saved password
apply.

**Detection and reading.** Encodings: `utf-8`, `utf-8-sig`, `utf-16-le`,
`utf-16-be`, `cp1252` (Latin-1 / Windows-1252, decoded as MySQL `latin1`);
a BOM decides, NUL bytes reveal UTF-16 without one, then UTF-8 validity,
else cp1252; a leading U+FEFF is dropped in every encoding. The separator is
the most consistent field count among comma, semicolon, tab and pipe, or one
other chosen character. The first row is the header when its values are
distinct, filled, non-numeric and not date-like, with a confirmation from
the later rows; a request that names a separator, quote or encoding without
a header choice decides the header again. Reading uses the csv module with
strict quoting (text after a closing quote is a problem), any line ending,
quoted separators, quotes and newlines, empty lines skipped, a field limit
of 16 MiB and at most 1,024 columns; a ragged row is a problem with its
line, as is a wrong encoding. NULL markers are a subset of `\N` and `NULL`
(the bare word joins when the sample has it); **Empty fields are** decides
whether an empty field is NULL or an empty string. `skip_lines` is 0 to
1,000. Conversion (`csv_import.convert`, a sentence per problem, never
rounding) covers integer ranges and UNSIGNED, DECIMAL precision and scale
(extra non-zero decimals refused), FLOAT/DOUBLE (no inf or nan), DATE,
DATETIME, TIMESTAMP (1970 to 2038, 2106 on MariaDB 11.5+; the server decides
the exact edge), TIME and YEAR in ISO forms, CHAR/VARCHAR length in
characters, TEXT/BLOB sizes in bytes, utf8mb3, latin1 and ascii limits, ENUM
and SET members, BIT, JSON (valid, no NaN or Infinity; MariaDB's JSON is
LONGTEXT with a `json_valid` CHECK, detected from
information_schema.CHECK_CONSTRAINTS) and booleans as 1/0 for TINYINT(1) and
BIT(1). Spatial, VECTOR, INET, UUID and generated columns cannot be filled;
NULL for a NOT NULL column is a problem (AUTO_INCREMENT excepted). New-table
types come from a fixed grammar (`parse_type`, rendered by `render_type`;
typed type text is never used as SQL), with inference over TINYINT(1), INT,
BIGINT, BIGINT UNSIGNED, DECIMAL(p,s), DOUBLE, DATE, DATETIME(fsp),
TIME(fsp), JSON, VARCHAR or TEXT (the widest VARCHARs become TEXT until a
row fits 60,000 bytes) and an optional `id BIGINT UNSIGNED AUTO_INCREMENT`
primary key.

**Statement builders.** Every statement carries the
`/*myflames:import_csv*/` tag; identifiers are backquoted. Text literals use
plain quotes when the value has no backslash or control character, escapes
when the session's NO_BACKSLASH_ESCAPES state is known, and
`CONVERT(X'..' USING utf8mb4)` otherwise; binary values are `X'..'`. Apply
refuses when the session's NO_BACKSLASH_ESCAPES state differs from the
plan's. INSERT batches hold at most 1,000 rows and 1 MiB per statement and
never cross a commit boundary; a single larger row goes alone up to
`min(max_allowed_packet, 16 MiB) - 4 KiB`, and a larger row is a problem.
LOAD DATA reads a checked copy (UTF-8, tab separated, `\N` for NULL) written
to a private temporary folder (its chunk files are 0600) at plan time, in
chunks of 50,000 rows or 256 MiB (or N rows with Commit every N rows), each
unlinked after use and the folder removed at the end: `LOAD DATA LOCAL INFILE '<copy>' INTO TABLE
... CHARACTER SET utf8mb4 FIELDS TERMINATED BY X'09' ENCLOSED BY '' ESCAPED
BY X'5C' LINES TERMINATED BY X'0A'`, with BIT columns through user variables
and `SET col = CAST(@cN AS UNSIGNED)`. LOCAL is IGNORE on both engines (a
duplicate key is skipped with a warning), so after every chunk the
affected-rows count and SHOW WARNINGS (Notes aside) are compared with the
rows written and any difference rolls that statement's transaction back and
fails the import.

**Execution and Stop.** Apply sends `SET SESSION sql_mode = CONCAT_WS(',',
NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')`, `SET SESSION
lock_wait_timeout = 5` and `SET SESSION innodb_lock_wait_timeout = 5`, then
a new table's CREATE TABLE (which commits on its own), then START
TRANSACTION, the data statements and COMMIT, once (Keep nothing) or after
every N rows. Keep nothing is refused with the reason on a non-transactional
engine (only InnoDB, RocksDB, TokuDB and NDB count as transactional).
Stop ends an INSERT import between statements and sends KILL QUERY from a
second connection to a LOAD DATA in flight; Stop is checked again before
every COMMIT, so a Stop during the last statement still rolls back. Before
every COMMIT the file's size and mtime are re-checked, and before the last
statement's commit its SHA-256. With Skip and report, a row error the server
returns on a transactional table (the `ROW_ERRORS` codes) is resent row by
row in the same transaction and the refused rows are skipped and listed.
Outcomes are done, failed (all or nothing keeps nothing; Commit every N rows
keeps the committed rows and names `last_committed_line` and `next_line`),
stopped and unknown; unknown only when a COMMIT, a CREATE TABLE or a
statement on a non-transactional table got no reply, and it is never
retried (a lost session on InnoDB with no COMMIT in flight is failed: the
server rolls back an uncommitted transaction). A new table the rows could
not fill stays, empty, and the result carries its `drop_hint` as text only,
never sent.

**Safety.** Plan reads only (variables, information_schema, SHOW GRANTS) in
a private editor session and checks every row; the workspace can stop a slow
check with `/stop`. Blockers (no token) include bad rows with Bad rows:
Stop, a denied privilege, Read-only, Keep nothing on a non-transactional
engine, LOAD DATA while `local_infile` is OFF or unreadable, an unmapped NOT
NULL column without a default, an unsupported mapped column, a new table
whose name exists or whose database does not, a new table without the id key
while `sql_require_primary_key` or `innodb_force_primary_key` is ON, and a
decode error; unknown privileges never block. Policy is
`dbops.decide("risky", ...)`: Production and Development both review;
Read-only refuses before any connection opens. The review token
(`ReviewTokens(ttl=600, capacity=8)`) binds the connection identity, the
file's size, mtime and SHA-256, the format, the target and its metadata
fingerprint (table type, engine, every column, primary key, triggers,
foreign keys, CHECK constraints), the mapping, the new columns, the method,
the transaction mode, the NO_BACKSLASH_ESCAPES state, the resolved client path,
its `client_limits_files` policy and the statements; one
apply per plan, at most two imports at once. Apply re-checks, in order, the
policy, the token, the file (size and mtime, then a full SHA-256), the saved
password, the client path and file policy, then the private session. A changed
client path or file policy refuses the import and requires another review. Each
plan or apply resolves the client once through the per-server connector rule
(override, engine choice, otherwise automatic) and uses that same path for the
capability check and session start. A Settings or inventory change that resolves
to another path requires another review; there is no substitution on apply.
The session's option file uses that client's engine, including MariaDB-compatible
TLS switches for a mysql compatibility link. Tests: `test/test_csv_import_api.py`
(`ClientReviewTest`, `SessionFlagTest`) pin path/file-policy binding and refusal;
`test/test_connector.py` (`TestClientSecurityFixes`) pins TLS mapping. A client or capability check failure refuses the import;
it never falls back to `--local-infile=1`. Capability results are cached by file
identity (`st_dev`, `st_ino`, `st_size`, `st_mtime_ns`), so a replacement at the
same path is checked again. The local-file capability is only for LOAD DATA: a
MySQL client gets `--local-infile=0 --load-data-local-dir=<private folder>`,
so it sends no file outside that folder; the MariaDB client has only
`--local-infile`, reported in `method.client_limits_files` with a plan
warning. After the session opens, apply re-checks the
target's metadata, the NO_BACKSLASH_ESCAPES state, `@@GLOBAL.local_infile`
for LOAD DATA, the privileges and the saved policy again, consumes the
token and writes the pending audit record before anything is sent; every
refusal is a 400 that says nothing was sent, audited `refused` with its
outcome (`read_only`, `changed`, `busy`, `file_changed`, `not_connected`,
`unverified`, `denied`, `policy_changed`, `audit_failed`, `stopped`).
myflames never sends SET GLOBAL and never changes `local_infile`. One audit
record per import (operation `import_csv`) holds the session settings, the
full CREATE TABLE, the data summary (rows and statement counts, the file
name) and the COMMIT line, never row data.

**Further rules.** A malformed first record is a problem, never the header.
NULL markers are chosen per import (Treat \N as NULL, Treat the word NULL as
NULL). Number and date checks accept ASCII digits only; lines are read with a
bound and an over-long line is a problem. Names are looked up with a binary
comparison under `lower_case_table_names` 0, so `Orders` and `orders` stay
apart. When the server names no row (a duplicate key), a transactional
table's statement is resent one row at a time inside the same transaction
until the row is found, then rolled back (`failed_at` has `line`,
`first_line` and `last_line`); a non-transactional table reports the range.
The plan warns or blocks with the reason for: `super_read_only` (refuses
every account), `read_only` (unless the account has CONNECTION_ADMIN or SUPER
on MySQL, READ ONLY ADMIN on MariaDB), NDB with Skip and report (statement
rollback discards the whole transaction there),
`explicit_defaults_for_timestamp` OFF for a new table,
`sql_require_primary_key` (not when `sql_generate_invisible_primary_key` is
ON), triggers unknown without the TRIGGER privilege (`target.triggers` null),
and single-transaction size limits (MyRocks row locks, Group Replication and
Galera write-set sizes); MariaDB's `EMPTY_STRING_IS_NULL` is handled. A
privilege preflight checks INSERT on the table and CREATE on the database for
a new table from SHOW GRANTS (unknown leaves the action enabled). An outcome
with no reply, or a lost connection (`editor_session.LOST_CONNECTION_CODES`)
on any data statement, COMMIT or CREATE TABLE, is unknown and never retried
(a lost CREATE TABLE says the table may exist). The audit record lists the
CREATE TABLE and the data summary as its statements (the session settings sit
in `target.session_setup`) and holds error codes with sentences, never the
server's text with row values; a stopped import is audit status `stopped`
(`dbops.AUDIT_STATUSES`; the Audit log reads Stopped and offers it in its
filter) with the rows committed. A check that is superseded, stopped or
closed is stopped on the server too, and the server checks Stop by bytes
read; stale `myflames-csv-*` temporary folders of earlier processes are swept
at startup.

**Tests.** `test/test_csv_import.py` (49 tests: reading, detection, the type
grammar, every conversion edge, literals in the three NO_BACKSLASH_ESCAPES
states, batches, the LOAD DATA text, inference, CREATE TABLE),
`test/test_csv_import_api.py` (44 tests, every answer validated against its
JSON Schema: open-path refusals, the chooser, inspect, plan blockers, the
Read-only refusal before any connection, a changed file or metadata,
statement order, the audit without row data, Skip and report row by row,
Stop, a COMMIT without a reply unknown and never retried, LOAD DATA from a
checked copy, HTTP through `WorkspaceHandler.do_POST`) and
`test/test_file_picker.py` (8 tests). Browser:
`apps/workspace/test/import-csv.mjs` (`npm run test:import-csv`), one
sequential script against a real Python server with every import endpoint
mocked to the contracts, covering both entry points, every step, the
outcomes, the gates, and no page overflow at 1440x900 and 720x600 in light
and dark. Run the Python tests with
`HOME=$(mktemp -d) USERPROFILE=$HOME MYFLAMES_SECRET_BACKEND=vault python3 -m unittest discover -s test -p 'test_csv_import*.py'`
and `-p 'test_file_picker.py'`.

**Lab.** `scripts/lab-csv-import.py` runs the import against running
servers, the way the parameter catalogs are validated:
`python3 scripts/lab-csv-import.py [--servers mysql-8.4 mysql-9.7 mariadb-10.11 mariadb-11.4] [--out PATH] [--json PATH] [--keep-images]`.
It needs Docker, sets its own throwaway HOME with the vault backend, starts
`--rm` containers from `public.ecr.aws/docker/library` (MySQL 8.4.11 and
9.7.2, MariaDB 10.11.19 and 11.4.13; two per server, `local_infile` ON and
OFF) listening on 127.0.0.1 only, and runs 35 scenarios per server through
`ui.api_response` (open-path, inspect, plan, apply, progress and stop) with
an unsaved Production connection, validating every answer against the
schemas and reading rows back with SELECT and HEX byte for byte. Containers,
volumes and the images it pulled are removed unless `--keep-images`. The
2026-10-29 runs passed 116 of 116, then 140 of 140 after the review round
(exact failing row, a killed connection reported unknown, the client's
folder restriction refusing an outside file with error 2068, names that
differ only by case, MyISAM LOAD DATA, triggers without the TRIGGER
privilege, explicit_defaults_for_timestamp, the generated invisible key,
EMPTY_STRING_IS_NULL, the `stopped` audit status). The lab-only
`apps/workspace/test/import-csv-live.mjs` drives the real wizard end to end
against MySQL 8.4 and MariaDB 11.4 containers. The write-up is in
`local/csv-import-2026-10-29/LAB.md`. Percona Server, MySQL 8.0, MariaDB
11.5 and later (TIMESTAMP up to 2106), SSH tunnels and the Windows file
chooser were not in the lab matrix and are covered by unit tests only.

## File transfer: SSH connections and SFTP

**File transfer** is a place of its own (sidebar label "File transfer",
internal key `files`, after Tuning; command palette "File transfer", also
found by "files", "sftp" or "scp"; icon CircleArrowDataTransferHorizontal). It does not use the workspace's database connection: it
works with **SSH connections**, saved objects of their own, and moves files
between this computer and an SSH host over SFTP, in the spirit of FileZilla.
Frontend: `FilesPlace.tsx` (the frame, `files-place.css`) composes
`SshConnections.tsx` (`SshConnectionList`, `SshConnectionSheet`,
`HostKeySheet`; `ssh-connections.css`) and `FileBrowser.tsx` (`FilePane.tsx`,
`Transfers.tsx`; `files.css`), all through `filesApi.ts`. The place stays
mounted after its first visit, so transfers and the folders on screen survive
a trip to another place. The selected connection id is a per-viewer
convenience in localStorage (`myflames.files.selected`).

- Connection list: a click, Enter or Space opens a connection; arrow keys,
  Home and End only move focus (nothing connects). Each row has a menu
  (right-click, Shift+F10 or the context-menu key, or its ⋯ button): Open,
  Edit… (F2), Duplicate (the New sheet prefilled as "NAME copy", never the id
  or the saved secret), Test connection (inline result under the row; an
  unknown host key opens the trust prompt and tests again), Copy address
  (`targetLabel`, IPv6 in brackets) and Delete… (a ConfirmSheet; the secret
  is deleted with the record). The menu never selects or connects. Deleting
  the open connection leaves "Choose an SSH connection" instead of opening
  another one. With none saved, the main area's EmptyState holds the one
  New SSH connection action and the list says "None saved yet." In the New
  or Edit sheet an unknown host key turns the same dialog into the trust
  step (Back or Escape returns to the form; the typed secret waits in memory
  only for the retried test). The Key file list leads with the file name
  (`id_ed25519 (ed25519) · comment · folder`), with the full path in the
  hint.
- Panes: each pane has one Refresh (F5 while the pane has focus; it spins
  and ignores clicks while listing); the browser toolbar has none. The
  footer buttons are Upload and Download; only the pane last focused or
  clicked shows its button as primary. Rows of This computer, and its
  folder menu, offer Reveal in Finder (macOS), Show in File Explorer
  (Windows) or Show in folder (elsewhere): `/api/files/local/reveal`
  `{path}` resolves the folder with the listing's link rule and runs
  `open -R`, `explorer.exe /select,` or `xdg-open <folder>` with an argv
  list, never a shell. `/api/files/local/list` answers `transfers`
  (`sftp_jobs.local_transfers_available()`: POSIX with `O_NOFOLLOW` and
  `dir_fd` opens); when it is false (Windows) Upload, Download, Enter on a
  file and drops are off with "Uploads and downloads are not available on
  Windows yet." Browse… sits in This computer's title bar beside Filter, and
  the path bar keeps the current folder's name in view at any width. Below
  760 px the panes stack and each keeps header, list, footer order.

Backend: `myflames/ssh_connections.py` (records, secrets, host keys, key
discovery), `myflames/sftp.py` (a minimal SFTP v3 client over the system
`ssh`, never a library), `myflames/sftp_api.py` (listings, file operations,
browsing-session cache: at most 8 sessions, idle 120 s, keyed by the
connection id and its session settings, so an edit drops it) and
`myflames/sftp_jobs.py` (transfers). Routes: `_SSH_CONNECTION_PATHS` and
`_FILES_PATHS` in `ui.py`, every one a POST behind the workspace's
authenticated checks. An SSH failure (`SftpError` subclasses, which are
`ConnectorError`s) answers 400 `{"error", "code"}` with `code` one of
`host_key_unknown`, `host_key_changed`, `auth_failed`, `ssh_missing`; no other
exception becomes a code, even one with a `reason` attribute (UnicodeError,
SSLError, URLError).

### SSH connections

WorkspaceStore records of kind `ssh_connection` (the `records` table, no
schema change). API shape: `{id, name, host, port, user, auth:
agent|key|password, key_path, has_secret, remote_dir, local_dir, updated}`.
Validation (`ssh_connections.validate`): name 1 to 80 characters, unique
ignoring case; host, user and port with `ssh_tunnel.ssh_fields` (a host may
be a `~/.ssh/config` alias); `key_path` only for `key`, an absolute existing
file without `"` or control characters; `remote_dir` absolute POSIX or empty;
`local_dir` absolute or empty.

The secret (the SSH password, or a key's passphrase) is never in the record
and never answered. It lives in `secret_store` under
`sshconn.<id>.<fp>.<rand16>`, where `fp` is the first 24 hex characters of
SHA-256 over the NUL-joined host, user, port (0 when empty), auth and key
path; the record keeps `secret_ref` and `secret_backend`, checked with
`hmac.compare_digest` before any store read. A save that changes any of those
five fields deletes the old secret unless the same save sends a new one;
`forget_secret` deletes it; deleting the connection deletes it. Order: a new
secret is stored, then the record is written, and only then is the old secret
deleted, so a failed write keeps the old record and its secret; a delete
removes the record first, so a store failure leaves an orphaned entry (said
in the error), never a connection that claims a secret it lost. A test of a
draft (`{connection, secret}`) uses the sent secret and stores nothing.
Endpoints: `/api/ssh/connections/list|save|delete|test`,
`/api/ssh/hostkey/scan|trust`, `/api/ssh/keys` (private keys in
`<ssh_home>/.ssh` with a matching `.pub`, read for type and comment, and
`.pem` files without one, recognized by their first line only; a private key
is never read past that line), and the existing `/api/ssh/hosts`.

### ssh invocation

Every session: `ssh -T -s` with `LogLevel=ERROR`, `StrictHostKeyChecking=yes`,
`UpdateHostKeys=no`, `PermitLocalCommand=no`, `ControlMaster=no`,
`ControlPath=none`, `ClearAllForwardings=yes`, `ForwardAgent=no`,
`ForwardX11=no`, `ConnectTimeout=10`, `ServerAliveInterval=15`,
`ServerAliveCountMax=2`, and `GlobalKnownHostsFile=` the global files ssh
would use for the endpoint plus the myflames file, each double-quoted (the
user's own `UserKnownHostsFile` is left as configured, so it still counts).
`sftp.known_hosts_config` reads both lists from `ssh -G` (OpenSSH defaults
when it cannot), splitting only where the next absolute path starts, because
ssh prints paths unquoted and a home folder may contain a space. Auth `agent`: `BatchMode=yes` and the user's agent and
`~/.ssh/config`. Auth `key`: `IdentityFile="<path>"`, `IdentitiesOnly=yes`,
`PreferredAuthentications=publickey`; with a saved passphrase also
`AddKeysToAgent=no` and, on Apple's ssh (probed once per binary with `ssh -o
UseKeychain=no -G localhost`), `UseKeychain=no`, so the user's config never
copies a passphrase myflames holds into the agent or the Keychain where
Forget could not remove it (found live: `UseKeychain yes` stored it).
Auth `password`: `PreferredAuthentications=password`,
`PubkeyAuthentication=no`. A secret is handed over by a single-use askpass:
a `/bin/sh` script in a 0700 `mkdtemp` folder that opens the 0600, O_EXCL
secret file, unlinks it and only then prints it with `/bin/cat` (no Python,
so the packaged app needs no system `python3`); `SSH_ASKPASS_REQUIRE=force`,
`NumberOfPasswordPrompts=1`, never a secret on argv or in the environment.
The helper answers only the ssh myflames started: its pid is written beside
it right after `Popen`, and the helper refuses any other parent, so a
ProxyJump or ProxyCommand ssh (a grandchild that inherits `SSH_ASKPASS`)
never receives the target's secret for its bastion. Paths handed to ssh
options are double-quoted and refuse `"`, `%` and `$` (ssh expands tokens
and `${VAR}` in them).
Windows fails closed for passwords and passphrases (keys and agent work).

stderr is normalized (CRLF) before classification: a changed key
(`REMOTE HOST IDENTIFICATION HAS CHANGED`, `Host key for ... has changed`)
is `host_key_changed` and names the file and line of the trusted key when
ssh reports them; an unknown key is `host_key_unknown`; `Permission denied`
or `Authentication failed` is `auth_failed` worded for the method. ssh at
`LogLevel=ERROR` prints nothing when a server drops the connection before
sign-in (OpenSSH 9.8+ does that for a while after failed sign-ins,
`PerSourcePenalties`): that case says so and suggests waiting; a drop after
sign-in reads "The SSH connection to ... was lost."

Chunks: a server that advertises `limits@openssh.com` is asked for its read
and write limits (used up to 256 KiB); otherwise 32 KiB, the size every SFTP
server must accept. OpenSSH's sftp-server closes the session on any message
over 256 KiB with its headers, so a fixed 256 KiB WRITE broke every upload
over 256 KB (found live). Eight requests are in flight per transfer.

### Host keys

Keys the user trusts in myflames go to `<WorkspaceStore folder>/ssh_known_hosts`
(normally `~/.myflames/ssh_known_hosts`, 0600, created with
`secret_store._open_private_file`); myflames never writes `~/.ssh/known_hosts`.
`hostkey/scan` runs ssh once with `StrictHostKeyChecking=accept-new`,
`UserKnownHostsFile="<private temp file>"`, `GlobalKnownHostsFile=/dev/null`,
`HashKnownHosts=no`, `BatchMode=yes`, every authentication method off and the
same host, port, user and `~/.ssh/config` (so a `ProxyJump` or `HostKeyAlias`
applies), command `true`: ssh records the negotiated key before
authentication fails. Each captured line must be `host keytype base64`;
`ssh-keygen -l -E sha256` (next to the `ssh` in use) gives the fingerprints.
The answer names the host as ssh checks it (`[db.example]:2222`, or the
alias). Before a token is issued, exactly the files a session consults (the user
files and the global files of `known_hosts_config`, and the myflames file) are
searched with `ssh-keygen -F` (a file it cannot search refuses the scan): a host already trusted with other keys is
`host_key_changed` (file and line named), not a first contact, because ssh
calls a key of a new type for a known host "unknown" (an impersonator
offering RSA where ED25519 is trusted). When nothing was captured the error carries ssh's first line
(refused, unknown host) or the 15 s timeout. The lines wait under a random
token for 5 minutes (at most 256 pending); `hostkey/trust` appends exactly
those lines (identical lines once) and consumes the token.

### Listings and file operations

`/api/files/remote/list` `{ssh, path}` (`""` is the remote home, from
`REALPATH(".")`) and `/api/files/local/list` `{path}` (`""` is the user's real
home, `ssh_tunnel.ssh_home()`) answer `{path (resolved, absolute), home,
entries}`; local paths resolve under the transfers' rule (a link is followed
only when root or the user owns it), so a listing never answers a folder a
transfer from it would refuse; an entry is `{name, kind: file|dir|link|other, is_dir, size, mtime
(epoch s), mode (full st_mode)}`. `is_dir` is also true for a link to a
folder (one STAT per link, at most 200 per listing). `.` and `..` from
READDIR are skipped (every OpenSSH folder has them: refusing `..` broke every
real listing, found live); any other name with `/` or NUL is refused. Local
listings stop at 100000 entries and skip entries that cannot be read.

Remote: `mkdir` (no permission attribute, so the server's umask decides),
`rename`, and `remove` `{paths}`: folders with their contents, counted first
(at most 10000 entries, depth 64, nothing removed over a limit), every type
checked again before deletion, children before their folder, links removed as
links and never followed, `/` refused. Ancestors are checked with lstat once
per folder in the read-only phases and again, all of them, before every
deletion, so a folder swapped for a link between two deletions stops the
removal (SFTP v3 has no directory handles: this narrows the race, it cannot
close it). Local: `mkdir` (the user's umask) and `rename`, which never
replaces a target, even one created after the check: `renamex_np(RENAME_EXCL)`
on macOS, `renameat2(RENAME_NOREPLACE)` on Linux, Windows' own rename; elsewhere
`link()` then `unlink()` for a file, and a folder is refused. Local delete is
not offered.

### Transfers

`/api/files/upload` `{ssh, local_paths, remote_dir, on_exists}` and
`/api/files/download` `{ssh, remote_paths, local_dir, on_exists}` start a job
(`on_exists`: `fail`, `skip` or `overwrite`, per file); `jobs` `{ssh?}`
(newest first), `cancel`, `retry` (a new job with the failed and cancelled
files of a finished one), `clear` `{ssh?}` (removes **finished** jobs only:
failed and cancelled ones stay for their errors and Retry). Job shape:
`filesApi.ts` `Transfer` (state, phase `scanning`/`transferring`, counts,
bytes, rate over a rolling 5 s, ETA, `current`, the first 200 `items`,
`items_more`, up to 200 `failed_items`, `error`). At most 256 jobs are kept,
two run at once, finished ones expire after an hour.

A job walks folders in its own thread: local with directory fds and
`O_NOFOLLOW` at every level, remote by lstat-based READDIR; links and special
files become `skipped` items ("Symbolic link not transferred.", "Not a
regular file."); every created name passes `_target_name` (no separators,
Windows device names or trailing dots). It fails before writing anything over
10000 files or depth 64. Destination folders are reused when they exist (a
non-folder in the way fails that subtree only) and created with the umask.
One failed file does not stop the others; the job ends `failed` if any did. A
local problem (a file that shrank while uploading, a disk that refused a
write) is `SftpLocalError` (code 0) and fails that file only; a transport or
protocol failure (no SFTP status) fails what is left. A job that fails as a
whole names its SSH reason in `error_code` (`auth_failed`,
`host_key_unknown`...), and the job row offers Edit connection or the
host-key prompt. Retry after a job stopped while scanning starts the original
selection again (nothing was transferred and not every file was listed);
after that it retries the failed and cancelled files.
The local folder the person chose is resolved once when the job starts
(macOS `/tmp` and `/var` are links), following only links owned by root or
the user (a link another user planted is refused); every later open walks
the resolved path without following links, so a link swapped in afterwards
is refused.
Downloads are staged as `<name>.myflames-part` (0600) and published with
`link()` (no overwrite) or `replace()` (overwrite); uploads set the local
mode without setuid, setgid and sticky bits on the open handle (FSETSTAT)
before CLOSE, never by path afterwards (a link swapped in would be chmodded),
and a remote replacement removes the old regular file first (SFTP v3 cannot
restore it if the upload stops). A queued job re-reads
its connection before it starts and refuses to run after an edit of its
endpoint or authentication.

SFTP actions are host-level file operations, not SQL writes: no database write
policy applies and they are not in the Audit log; the Transfers panel lists
each job while it lives. The database's saved password is never used here.

### Desktop drag and drop

Electron 32 removed `File.path`. `apps/desktop/preload.cjs` (sandboxed,
context-isolated) exposes exactly `window.myflamesDesktop.pathForFile(file)`
(`webUtils.getPathForFile`; "" for a File built in JavaScript) and nothing
else; `filesApi.droppedFilePath` reads it. The packaged smoke checks that the
bridge holds only that function (`preload: true` in `smoke.json`).

### Tests

`test/test_ssh_connections.py`, `test/test_sftp.py` (pipe-based SFTP peer,
including `limits@openssh.com`), `test/test_sftp_jobs.py`,
`test/test_ui.py` (`TestFilesEndpoints`), browser suites
`apps/workspace/test/files-connections.mjs` (the real `FilesPlace` with
fixture routes: row menu, focus without connecting, empty state, in-sheet
trust step), `files-browser.mjs` (reveal, per-pane Refresh, one primary
transfer button, the `transfers: false` state, 720 px order) and
`files-live.mjs` (the built app against the SSH lab), and the
live suite `test/test_files_live.py` against a real OpenSSH server in Docker
(`scripts/ssh-lab.sh`: up, test, down; password, plain key and
passphrase-protected key sign-in, host-key trust, folders round trip with a
64 MiB file checked by SHA-256, removal that must not follow links, a changed
host key). The live suite runs only with `MYFLAMES_SSH_LAB` set and checks
that `~/.ssh/known_hosts` is unchanged.

## Workspace navigation and configuration controls

The sidebar's right-edge separator uses pointer capture to resize `--sidebar-w` on the document root; `--sidebar-width` aliases it for every fixed layer. Width is clamped to 208–320 px (at least 400 px remains from a 720 px window before compact navigation applies). Its focusable vertical separator exposes current/minimum/maximum ARIA values. Left/Right move 8 px, Shift moves 24 px, and Home, End or double-click restores 208 px. The existing rail/drawer breakpoint stays at 900 px and ignores the stored width. The window footer renders on every ready place, including Home, Settings, Learn and Investigations. Facts retain their existing scoped publishers on the left; the right end owns version, shared update state and update/release actions. At narrower widths the update timestamp hides and outcome text compacts; left facts truncate first. Home has no separate version footer. `.workspace` reserves `--statusbar-h` below every place, retaining the top-level wrappers and sticky headers.

Automatic first reads use `useOpenRead` and the report's connection key. Opening a view without a report for the connection in use reads once; a failed attempt waits for Refresh. Switching connections while the view stays on screen keeps its old report greyed until Refresh or a later reopening. Parameters, Instruments and Users and roles follow this rule too. Health, InnoDB, Locks, Processlist, Waits, I/O, Indexes, Tables and Replication retain their single-flight and stale-response guards. Every `useConnectionReads` request forwards its AbortSignal to `api()`. Tables and Indexes retain schema-size consent and pass visibility to `useSchemaCatalog(connection, enabled)`, including when compact navigation hides their detail. Explicit Refresh and Refresh all remain independent of these automatic-read gates.

Query Digest stores its report as `Bound<DigestReport>` through `useConnectionReads`, keyed by `connectionKey` (profile, endpoint, user, TLS and write policy). Editor and Explain callbacks carry that report key to `QueryLab.openDigest`; stale actions are disabled and rechecked at dispatch. Late responses cannot replace a newer connection's report. Exports name the report's original connection and filter. Their statistics cover only the loaded digests: sums and execution-weighted means, plus the maximum of collected execution maxima. Execution minima, row/lock extrema and percentiles remain unknown; digest averages and per-digest totals are not individual execution samples.

Processlist **Kill sessions…** opens the Kill sessions dialog (`MassKillDialog.tsx`; `/api/live/kill/bulk/prepare` then `/api/live/kill/bulk`, `kill-bulk-1.0`, whose `message` reads "Kill sessions finished: N killed, …"). Its **Lock waiters** preset requires a recorded wait (`lock_wait` or `lock_status: waiting`). A session that only holds locks (`is_locked` or `lock_status: holding`) is excluded. Exact bulk review, policy checks, target verification and audit remain required.

The window status bar reads the installed version from the server-injected version meta tag. Its release link does not perform an update check or install software; only **Check for updates** asks GitHub (see Application updates above). Its News section is described under [Local UI](#local-ui) (Home news). Stable internal place IDs remain unchanged; Query editor, Query digest, Database diagram, Schema versions and Plans share
the SQL workspace navigation group.

Refresh all schedules unopened sections as well as loaded ones, with three concurrent reads and a separate Full analysis phase. It preserves scope-specific large-schema consent, joins identical reads, and drops stale results. Maintenance only detects tools; refresh never starts jobs or upgrade checks,
and imported diagrams are preserved. Parameters and Instruments read on open (one attempt per connection; Refresh retries). Parameters inline edit controls use parameter metadata to stage local changes, and **Review and apply** reviews up to 25 changes at once while **Change…** opens the single-parameter sheet (including **This session only** handoff to the Query editor). Guidance is collapsed by default. Legal scope selection, exact SQL review and
Session editor ownership remain mandatory.

Back and Forward (`main.tsx` `navHistory`, `QueryLab.tsx`), cross-link chips and `⌘K` object search (`CommandPalette.tsx`, `myflames/schema_browser.py`, `POST /api/live/object-search`):
- **Back and Forward**: in-memory `navHistory` (capped at 100 entries) records settled places, `Server` and `Workload` views, `Plans` active plan (`selection.plan`) or `Compare` pair (`selection.compare`), and `Query editor` tab and schema-browser reveal (`selection.query = {tabId, reveal}`). `⌘[` and `⌘]` (`Ctrl+[` / `Ctrl+]`, plus `Alt+Left` / `Alt+Right` on Windows and Linux when no `<dialog open>` is active) and the top bar's Back / Forward controls step through the history. Traversal never switches the connection in use; when an entry was recorded on another connection, a notice says `"<label> was open on <entry.connectionName>; this window is on <now.name>. Nothing was switched: views read the connection in use."` Cross-links (`openServerTable`, `openDigest`, `openInstrumentsRow`, `openAudit`, `onNavigatePlace`, `SchemaVersionsPlace` `onOpenSql`, `Server` `onOpenSql` / `onOpenInvestigation`, `openObject` from `⌘K`) capture the origin via `linkOrigin()` and render a `.nav-back-chip` (**Back to `<label>`**) in the top bar that jumps back to the captured origin entry (re-inserting it if aged past 100 entries).
- **`⌘K` / `Ctrl+K` recent destinations and object search**: when the search input is empty, up to five deduplicated **Recent** destinations from `navHistory` lead the list (`group: "Recent"`). Typing 2 to 128 characters filters commands (ranking commands whose `label` contains every word before keyword or group matches) and searches schema objects on the connection in use: first matching cached schema names from `useSchemaCatalog` (`group: "Database"`), then after a 250 ms debounce calling `POST /api/live/object-search` (`ui._OBJECT_SEARCH_PATHS`, read-only, saved-password helper, 2 s `query_timeout`, tagged `/*myflames:editor*/ `). `schema_browser.object_search` answers `object-search-1.0` (`{schema_version: "object-search-1.0", text, objects, limit: 50, truncated}`) from one `UNION ALL` over `information_schema.{TABLES, ROUTINES, TRIGGERS, EVENTS}` with `LIKE <hex-literal> ESCAPE '!'` (filtering `ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION')`), ordered by `BINARY obj_name, BINARY obj_schema, obj_kind`, bounded to `OBJECT_SEARCH_LIMIT = 50`. `perf_common.named_rows` reversibly escapes XML-forbidden control characters and its own escape marker before parsing client XML, then restores them in field names and values, so object groups and search retain exact names rather than stripping controls or failing the whole read (lone surrogates become U+FFFD). Choosing a database, table or view opens **Query editor** and calls `revealInSchemaBrowser` (setting the table filter to the table name so targets past the first unfiltered page are reachable in one bounded catalog read); choosing a procedure, function, trigger or event opens `SHOW CREATE {PROCEDURE|FUNCTION|TRIGGER|EVENT} <schema>.<name>` in a new query tab, selected and not run.

The Settings page (`SettingsPage.tsx`, `settings.css`) is a page in the main area, not a place: while it is shown the gear carries `aria-current=page` and no place is current. The gear at the bottom of the sidebar, ⌘, (Ctrl+, on Windows and Linux) and the command palette's Settings action open it (a palette query about the editor opens it at the Query editor section); Escape and Back return to where the user was, skipping the consecutive Settings entries a reload that restored the page can leave, and focus returns to the gear. Its section list (the kit `SourceList`) holds Appearance, Query editor, Workspace, Updates and news and Client tools, with Audit log and About myflames as actions at the bottom; at 720 px and below the list sits above the controls. `main.tsx` loads the page lazily, so the settings UI and `editorSettings.ts` stay out of the entry chunk. Every control keeps its own storage and behavior; the page only lays them out. `test/settings-page.mjs` covers it.

### Action feedback

Every action reacts in the frame of the click and says what is happening and
what happened. The parts live in `apps/workspace/src/kit.tsx` and `kit.css`
(outside `DataTable`); `test/action-feedback-unit.mjs` and
`test/action-feedback.mjs` check them, and
`test/action-feedback-batch-c.mjs` checks the Plans, Maintenance and
Cluster/ProxySQL rules below.

| Part | Contract |
| --- | --- |
| `useAction(scope?)` | One action's lifecycle: `idle`, `pending`, `done`, `failed`, `unknown`. `run(work, text)` sends nothing while the same action runs (single flight), shows `text.pending` at once, then `text.done` (a string, or a function of the reply; empty leaves no line), the error in plain words, or, for `write: true`, `writeFailure`'s answer. `cancel()`, a new `scope` (the connection or form it belongs to) or unmounting ends it quietly: its reply is never applied. Writes pass no scope; their outcome is always shown. A `done` or `failed` function that throws is logged and replaced by "Done." or the error's own words, so an action always ends. |
| `ActionButton` | The control that runs it. While `busy`: a spinner and `busyLabel` in the present tense ("Testing…", "Applying…"), `aria-busy`, clicks ignored, and focus kept (`aria-disabled`, never `disabled`). The other label is reserved, invisible, in the same grid cell, so the width never changes; the visible label stays centered in the width reserved for its busy state. |
| `ActionStatus` | The line beside or under the action: a polite live region that is always mounted (a region inserted with its text is often not read), with the seconds after 1 s (drawn, `aria-hidden`, never announced), "Still waiting for the server." after `ACTION_SLOW_AFTER_MS` (10 s) with `onStop` ("Stop" or "Stop waiting"), and `onRetry` ("Try again") after a failure. Idle it takes no room; `keepLine` keeps its empty line, for a status under a centered control. |
| `ConfirmSheet` | `busyLabel` on the confirm button (it keeps focus), and the busy label read aloud at once; after 1 s a one-line `progress` ("Waiting for the server" by default) with the seconds at the start of the action row, which never wraps the buttons. With `onLeave`, after 10 s without a reply the line reads "No reply yet", `leaveText` (what Close does) appears at the end of the body, and Cancel becomes Close: the sheet closes, the request goes on, and its owner shows the outcome in the view. Cancel and Escape never decline a write that was sent. Its buttons share one box (`.sheet-buttons`, `display: contents`), so the first is Cancel and the last confirms. |
| `ViewLayout` and `ViewStatus` | Refresh is an `ActionButton` ("Refreshing…"; `refreshLabel` renames it, for example Maintenance's Detect again). The other-connection note has no button of its own: it points to the toolbar Refresh, or names `otherAction` when that differs. The loading line counts seconds after 1 s; after `slowAfter` (20 s; sampling views add their window, Indexes and Tables 45 s) it says so (`slowText`, "Still waiting for the server." by default) and offers Stop (`reads.stop`: the view keeps its report and the request is aborted unless Refresh all waits for the same read); a read error offers Try again (`onRetry`) beside the alert, never inside it; `doneAt` (`reads.doneAt`) announces "Updated at …". Every new `loading` text is announced, so it stays the same while the read runs: never put the seconds in it (Tuning's Full analysis passes "Running every Server view once, about 10 to 20 s in all. It only reads." and `slowText` "Longer than the usual 10 to 20 s."). |
| `writeFailure(error)` | A write's exception: an HTTP 4xx reply is a failure in the server's words; anything else (no reply, a lost connection, a 5xx, a time limit) is "Outcome unknown", and the write is never sent again. |

Top statements keeps its first-load control mounted while reading, with progress
beneath it. Its prerequisite gates and capture errors never offer a read retry.
Error log reads on open and uses the shared Refresh, Stop and Try again controls.
Database diagram's Load <database> (the empty state's button, whose progress and
error show once under it, with focus moving to the canvas) and its rebuild go
through the toolbar's one Refresh control (Load until the chosen database's
diagram is shown); its database-list reload reports progress and errors
separately. Closing a stalled import aborts the request and
invalidates its reply, preserving the current diagram.

The Query editor keeps busy Run, Run all, Explain, Run analysis and session
controls focusable. Session operations and Stop keep progress per tab. A run
finishing does not clear a Stop request still awaiting its reply; after 10 seconds
the Stop line says the statement may still be running. Commit and Roll back use
`writeFailure` for request errors and offer no retry after an unknown outcome.
Reconnect keeps its ended-session banner until the reply. Exact SQL review,
policy rechecks and the server's audit remain in the existing write paths.

Rules:

- A status line never moves the control that started it: put it under or
  beside the control. A first load started from an empty state shows its
  progress under its button (`keepLine`), and the line under the toolbar is
  for later reads (Users and roles).
- The result stays visible until the next action: what changed ("Headlines
  updated.", "Exported “Checkout slowdown” as Checkout-slowdown.json.", "1
  connection removed."), or the error in plain words with Try again. A failure
  never reads like success, and "Nothing was sent" appears only where the code
  knows nothing was sent (a review that was still being prepared, a refusal
  before sending).
- Stop, Try again and Dismiss go away when clicked; focus then moves to the
  view's Refresh or the action beside the line (`restoreFocus`), or to the
  line itself (`tabIndex=-1`), never to the page.
- A guard that must hold before React renders the busy state uses a ref set in
  the click handler (Refresh news reads start in an effect, so its click checks
  `reading.current` as well as `loading`).
- Refresh all is the item "Refresh every view on <connection>" in the menu
  of the view's Refresh (`RefreshAllContext` in `kit.tsx`, supplied by
  `Server` in Server and Workload only); the toolbar keeps one Refresh. While
  it runs, the top bar shows its progress and Stop. Stop takes clicks only
  after `STOP_ARM_MS` (600 ms), so a fast repeated click does not stop the
  run it started; its status names the views being read ("Refreshing 4 of 17 ·
  Waits, I/O…") with the seconds, and announces only the start and the end.
- Kill sessions…: the review is prepared with "Preparing review…" and Stop
  waiting (nothing is sent); the send reports Done, The server refused, Could
  not end it, or Nothing was sent from `kill-bulk-1.0`'s counts; closing the
  sheet before the reply leaves "Waiting for the reply." in Processlist until
  the outcome replaces it, and a Kill sessions dialog opened meanwhile says
  the earlier request has not answered and sends nothing (one bulk KILL at a
  time). That late outcome never closes a dialog opened since: it shows in it
  ("The earlier Kill sessions request on … answered: Done. …") and reaches
  Processlist when that dialog closes; the owner's `onSuccess` records the
  note, and the dialog closes itself (`onClose`). A dialog its owner closes
  while its send waits (the connection in use changed, or was made read-only
  elsewhere) is treated as if its sheet had been closed: Processlist says it
  waits, and the outcome, a refusal included, arrives the same way.
  Policy refusals and policy-read errors before `killBulkSessions` also use
  that delivery path after Close or unmount. They say nothing was sent and
  never call `writeFailure` or claim an execution audit record. Their notes
  carry `sent: false` so the connection label reads "Reviewed on".
- A successful Kill note (`KillResultNote`, shared by Locks and Processlist)
  starts with "Done." before the server's message, using `killStatusWord` as
  Kill sessions does. A Kill note names the connection the KILL was sent on
  ("Sent on Prod (app@db1:3306), not the connection in use.") when another
  connection is in use (`currentKey`, the view's `reads.key`), and then offers
  no Refresh, which would read the connection in use. Without `currentKey` only
  a late note (its sheet was closed) is named. Locks' waiting line names the
  connection too once another one is in use.
- Try again repeats the failed action on what it acted on (Investigations
  keeps the investigation an Open all or Export failed for, even after another
  one is selected).
- Reviewed SQL actions (Indexes, Tables, Instruments): preparing the review
  can be abandoned with Stop waiting (nothing was sent); a confirmed send that
  gets a 4xx reply is a failure, and only a send with no reply is an unknown
  outcome that is checked, never resent.
- Processlist, Parameters and Replication pass Stop, Try again and Updated at
  to their read status, and their reviewed sends show Applying, Skipping or
  Starting. Processlist shows a KILL outcome before its reread
  finishes, and only that note's identity may clear its rereading indicator.
  Kill sessions owns closing its dialog; pending notes trigger no reread and
  late replies never take focus or close a newer dialog.
- Parameter change/reset and batch reviews, and replication skip/start
  reviews, allow Close after ten seconds while keeping the request alive.
  Their owners retain progress and the eventual outcome, including the
  original connection after a switch, and block another send while pending.
  Apply exceptions use `writeFailure`; failed policy or preflight reads still
  say nothing was sent. Parameter batches stop after either a refusal or an
  unknown outcome. Replication's preflight has a synchronous single-flight
  guard and Checking buttons; Stop cancels that read only. Inspector preflight
  progress sits below its actions, and the temporary restriction preserves the
  existing privilege-note layout so repeated clicks land on the same control.
  Reviews focus Cancel and return to the initiating button on dismissal;
  `ConfirmSheet.returnFocus` identifies that button across asynchronous preflight. Loop Stop keeps
  focus with Stopping and elapsed time, and after ten seconds says the loop
  may still be running.
- Plans: sample analysis uses Opening… with elapsed progress and Stop waiting;
  stopping aborts the read and discards its answer. HTML export keeps the menu
  item and Export trigger busy while building; HTML and local JSON downloads
  report Saved with the filename. Try again retains the original export input.
  Investigation Save keeps focus, shows its error once in the sheet, and can
  be closed while saving; its late outcome appears in the window.
- Maintenance: Detect again, Check for updates, Check upgrade and each preflight keep focus and
  show progress beside their initiating control, with Stop waiting for reads.
  Read errors offer Try again, and completed reads show Updated at. Start
  keeps its exact review and policy checks; closing its slow sheet leaves a
  pending job card until the reply or recovery from the jobs list. Cancel
  shows Cancelling… and elapsed progress; a lost reply never proves it stopped.
- Cluster/ProxySQL confirmations use Sending… and keep the existing statement
  progress, audit and result panels. Close hides the sheet only; the send
  remains single flight. Mount the review output in `ViewLayout.status` so its
  pending line stays visible when the inspector hides the retained list.
  A late refusal or unknown result is shown without
  reopening it. ProxySQL Refresh offers Stop, Try again and Updated at.
- Motion is subtle: spinners stop under `prefers-reduced-motion` (the
  present-tense label and the seconds still say that it works); a busy icon
  button keeps full color and turns its glyph (`.is-busy[aria-busy="true"]`).

### Tables and indexes: browse and export

Tables includes an All tables inventory, and Indexes includes All indexes,
including used and primary indexes. Both paginate in groups of 25, 50, 100 or
200 rows. Sorting applies to the entire result before pagination. Search and
scope changes return to the first page.

Each list exports all filtered, sorted rows as CSV or standalone HTML. PDF
opens the system print dialog; choose Save as PDF. It repeats column headings
and wraps long names. Exports describe the report scope and include every
filtered row, not only the current page. CSV guards spreadsheet formulas;
HTML escapes server-provided text and contains no scripts.

The workspace requests complete scoped metadata rather than truncating at 200
rows. Existing schema-size consent still applies. Reports above 100,000 rows
in a category are refused with a request to narrow the database selection;
client-output limits also remain in force. CLI row limits are unchanged.

Reusable space in Tables is an InnoDB tablespace metric. Its minimum-percent
filter applies only where file-per-table ownership and all denominator values
are known. Shared or unknown tablespaces and missing sizes have no percentage.
This is not per-index fragmentation; Indexes does not invent such a metric.

The Tables report is `schema-2.0`: `fragmentation[].free_pct` is a number or null. Null means the percentage cannot be established from the available values. The workspace also reads historical `schema-1.0` fixtures. `tables` is the inventory array; Indexes adds an `indexes` inventory array to its existing report. Neither inventory adds a database query.



### Workspace interaction correctness

`PrivilegesProvider` refreshes after the connect revision or the session-password
commit revision changes. Typing never increments either revision. The picker
commits on Set, blur or Enter, once per changed value. A connect revision refreshes
status but cannot adopt draft credentials; only a password commit revision or
a new connection identity updates the committed credential. Incidental privilege
reads ignore an uncommitted password. `refreshProfiles` adopts only the newest request
and makes older callers await that request, preserving current write policy.
Explicit Use connection, Start fresh and workspace restore also advance the
password commit revision when they choose or clear the session credential.

`AccountsView` stores layout as `accounts.list`. Incomplete account metadata
gets Not judged with the backend reason and a single Details column; account
selection guards staged edits with Review changes, Discard and Cancel.
Server keeps Users and roles mounted after its first visit. Apply outcomes and
generated passwords remain in separate, connection-bound queues in that view's
memory until individually dismissed; subsequent writes cannot replace them.
Only the owning connection displays them. Closing a slow Apply sheet or changing
connections does not discard its late reply or resend the write. Follow-up
privilege, account-list and detail reads recheck the active identity after each
wait. Reload and Start fresh clear these queues; no password enters session
storage or the audit. Socket-free regressions: `npm run test:account-connection-races`;
browser cases: `accounts.mjs`, `editor.mjs`, `server-nav.mjs`.
`sqlIdent.ts` owns quoted identifiers, literals and shared index/table builders.
`MassKillDialog` compiles the unmodified query pattern once per edit; invalid
patterns match no rows.

`ContextMenu` in `kit.tsx` serves DataTable and TableData, measures its rendered
size, preserves keyboard focus, and suppresses empty menus. View actions precede
standard copy actions. Resizers clear drag state and listeners on pointercancel
and lostpointercapture. Full analysis start time belongs to its read owner in
Server; Tuning uses it across remounts and uses ExportMenu for both exports.
