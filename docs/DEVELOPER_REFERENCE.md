# MyFlames Developer Reference

This is the detailed contract per feature; [AGENTS.md](../AGENTS.md) has the rules. Read the relevant section before changing behavior, output formats, UI integration, or feature-specific tests.

## Table of contents

- [Commands Reference](#commands-reference)
  - [Local UI](#local-ui)
  - [Shared view presentation](#shared-view-presentation)
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
  - [Waits, Indexes, Tables, I/O, Config](#waits-indexes-tables-io-config)
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
  - [MySQL Shell](#mysql-shell)
  - [--quiet / -q](#--quiet---q)
  - [MCP server](#mcp-server)
  - [Playground](#playground)
  - [MySQL Fixtures](#mysql-fixtures)
  - [MariaDB Fixtures](#mariadb-fixtures)
- [Visual Explain (merged Diagram / Workbench)](#visual-explain-merged-diagram--workbench)
- [Investigation engine](#investigation-engine)
- [Workspace navigation and configuration controls](#workspace-navigation-and-configuration-controls)
  - [Action feedback](#action-feedback)
  - [Tables and indexes: browse and export](#tables-and-indexes-browse-and-export)

## Commands Reference

### Local UI

The optional Electron development host is in `apps/desktop`. It starts its own
Python server and loads the one-use launch URL into a sandboxed window. It uses
separate app data and retains the same API authentication and write contracts.
The unsigned Apple Silicon package bundles its Python backend and opens through
Finder. Build with `package:mac`; validate the delivered `.app` using the packaged
smoke, which removes system Python from the startup path and checks owned cleanup.
Quitting sends SIGINT to the backend's process group and waits 5 s; `backend.cjs`
then kills the dump, restore and clone tool groups the backend listed in its
`runtime/tool-groups-<launch id>.json` file, kills the backend's group and stops
any group in its last list, never signalling an id it already saw gone. The
file is opened without following links or blocking, and a missing, oversized or
malformed list counts as unconfirmed. `stop()` resolves only when all are gone
(7 s in total) and otherwise rejects, which shows the shutdown-incomplete dialog.
Client discovery appends fixed macOS installation directories without shell
startup scripts. Packaged app data is separate from development and CLI data.
See [Desktop app](DESKTOP.md) and the desktop package README for launch and test
instructions. Current delivery targets macOS. The shared working UI now uses
compact neutral navigation, open-query links, a command palette and full-width
Server details. The original interactive preview remains synthetic design material.

Update checks run only when the workspace asks: **Check for updates** in
Home's version footer and in About myflames (`UpdateCheck.tsx`, shared state in
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
**Download…** appears for a current newer release (unavailable while a check
runs) and opens the update dialog with the result captured at the click;
after a reload it checks again first, because the download needs the asset the
check found.

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
enforce the 150 MiB limit before publishing. http.client returns an empty read
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
`HomeNews.tsx`, `home.css`) and the Settings popover (`NewsSetting`, busy
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
  The footer has one autosave status and a Settings popover (Restore
  workspace on startup, Show MySQL and MariaDB news on Home, Audit log, About
  myflames). Saved connections and the connection picker are in
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
  the picker shows `TypeDot` and the type tag. The Audit
  log is `Audit.tsx` (session mode `audit`, no place active). Stored session `mode` values keep
  the earlier names (`inspect`, `compare`, `query`, `library`, `teach`) plus
  `home`, `server` and `audit`; `placeOf` in `main.tsx` maps them to places. Naming follows
  `.claude/skills/ui-architecture/references/vocabulary.md`; styles use the
  tokens on `:root` in `style.css` (web-design skill). Browser test for the
  shell: `apps/workspace/test/navigation.mjs`; `apps/workspace/test/server-nav.mjs`
  covers the Server place. The app mark is
  `docs/brand/myflames-icon-v3.png` (the app icon; the workspace sidebar, Home, About and favicon use its 128 px copy `myflames-icon-v3-128.png`, while `docs/brand/myflames-icon-v2.png` is preserved for README references). Vite emits the stable
  `/assets/myflames-logo.png` URL; retain earlier logo URL aliases in `ui.py`. `npm ci && npm run build` there regenerates committed
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
  and `CURRICULUM` order. The Learn place uses a view toolbar (Search lessons, Family filter, Start learning), a one-line summary ("23 lessons, 16 in the learning path") and grouped rows by family, ordered by the learning path, each with its summary and "Step n of 16". An open lesson has one compact toolbar (All lessons, Previous lesson, Step n of 16, Next lesson, Download lesson) and fills the remaining height; the top bar shows the lesson title. The workspace adds `data-embedded="true"` to the lesson page, which hides its own header and footer. Lesson pages and the standalone catalog (`myflames teach --index`, which includes a lesson filter) use the workspace's light and dark tokens (`prefers-color-scheme`, `data-theme` overrides, and `?theme=` / `?embedded=1` URL parameters), restyled controls, and semantic `--svg-*` drawing colors resolved by `anim.cssVar` and `anim.palette`, so drawings redraw when the appearance changes and keep their meaning (descent path, arrival pulse, PK hop, build vs probe, hit, spill). Lesson titles use a colon ("B+tree lookup: how InnoDB finds a row"). See `docs/LOCAL_UI.md` and `docs/QUERY_WORKSPACE.md`. Test with `test/test_ui.py` and the CLI suite.
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

### Shared view presentation

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
its label on a separate line. `WindowStatusBar` keeps safety words and the
reported server version visible while flexible text truncates. Server,
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
1 check". Run full analysis belongs in the Scorecard toolbar; the Full
analysis tab uses Refresh for its next read. Database diagram uses its
view Refresh for rebuilds. I/O keeps its view controls in the toolbar and
its counters in the content, with no repeated action under a section heading.

Investigations hides the empty list when nothing is saved and offers
Save N open plans as an investigation… when plans are open, otherwise Import
plan. Learn's shared 2D lesson wrapper hides Phases when no phase items exist
(`myflames/teach/_html.py`); phase navigation remains when marks exist.

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
  about 90 KB). Mermaid keeps its own chunks (Database diagram).

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
  order (`style.css` last) and `main.tsx` imports it first, so the order never
  depends on which chunk loads first; the built stylesheet is identical to the
  unsplit build. Add a new stylesheet to `styles.ts` too.
- Code that looks up a lazy part's elements right after switching to it must
  wait for them: the Server toolbar measurement in `main.tsx` watches for the
  strip to appear, and `focusWhenMounted` in `Server.tsx` focuses a view's
  heading once it exists.
- `test/bundle-split.mjs` (`npm run test:bundle-split`) checks the stylesheet
  list, that no lazily loaded module is also imported statically, one built
  stylesheet, and an entry chunk under 450 KB. The chart runtime has its own
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
### Write safety and audit

`myflames/dbops.py` is the one safety layer for every statement that changes
server state (db-operations skill). Its parts:

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
  CSV export flattens it and quotes cells starting with `= + - @`.
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
policy read also covers edits during that review.

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
  identity, the operation and the statements); the run goes ahead only with
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
  for the persistent table; temporary tables may be absent or shadow it.
  The response's `alter_preflight` includes schema, table, engine/version,
  table_engine, approx_bytes, metadata_locks, transactions,
  unattributed_transactions, sessions, coverage flags, warnings and suggestion.
  Tokens also bind the reviewed schema/table, connection ID and server identity;
  changed session context requires a fresh confirmation. `start_run` also
  rechecks DATABASE() under `run_lock` before an unqualified ALTER, closing
  the gap for a concurrent USE between review and execution.
  `_alter_suggestion` offers INPLACE/LOCK=NONE only for recognized InnoDB
  ADD/DROP COLUMN or INDEX forms on MySQL 8.4+ and MariaDB 10.11+. Existing
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
  `status`, `stop`, `commit`, `rollback`, `close`, `history`, `export` under
  `/api/editor/`; `run` and `commit` are in `ui._WRITE_PATHS`, `open` and
  `stop` in `ui._CONNECTION_PATHS`. `/api/editor/status`, `stop` and `close`
  enforce session ownership by requiring the matching `connection`
  (`_connection_facts`, `session.identity == identity`); an unknown tab or
  mismatched connection returns `status: "none"`, stops nothing, or returns
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
  adopts a session still open. `SqlEditor.tsx` (textarea plus colored layer,
  gutter, statement tint, bracket match, error underline; `sqlHighlight.ts`
  tokenizer, off above `HIGHLIGHT_LIMIT` characters), `ResultsPane.tsx`
  (results tabs, `DataTable` grid, Messages, Export, copy menu),
  `SchemaBrowser.tsx` (on demand: `/api/live/schemas` through
  `useSchemaCatalog`, then `/api/live/catalog`, cached per connection key and
  kept while hidden), `HistoryPanel.tsx`, `editor.css`. Session restore: tab
  `limit`, `selection`, `result_tab`, and `query_lab.editor_layout` (height,
  browser and width, history, run time limit), reported only once changed
  from the defaults. Browser suite `test/editor.mjs` (`npm run test:editor`).
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
- The editor's room (`measureEditorRoom`) is set only when the measured value
  changes (`measuredRoom`): a no-op update from its every-commit layout effect
  kept re-rendering while a lower-priority caret update waited, and React
  stopped with "Maximum update depth exceeded" when Format SQL replaced the
  text.
- Schema browser endpoint: `POST /api/live/catalog` (`ui._CATALOG_PATHS`,
  read-only, saved password filled like the views) answers
  `schema_browser.catalog_report`: `catalog-1.0` tables of one schema
  (`information_schema.TABLES`, at most 200 per page) or one table's columns and
  indexes (`COLUMNS`, `STATISTICS` read by name), tagged
  `/*myflames:editor*/`, names as hex literals. Optional `prefix` (literal text, including `%` and `_`)
  and `after` (exclusive continuation) are forwarded to the module after `check_table` validation,
  before opening a connection: null/empty means absent; otherwise strings of at most 64 characters,
  with blank-only text, NUL, LF and CR rejected. Pages use binary table-name order and return
  `next_after` when truncated. `stats: true` (only a literal true) adds `engine`, `rows`
  (`TABLE_ROWS`, the server's estimate) and `bytes` (`DATA_LENGTH + INDEX_LENGTH`) to each table,
  None for views, and `stats` to the report; Maintenance > Dump reads it for the one chosen database
  (MariaDB opens each table of that schema to answer). Tests: `test/test_schema_browser.py`, `test/test_ui.py`.
### Run

The execution tree's **Self of total** column uses the operator's self time
divided by the plan's total time. `output_tree.py` and `charts/tree.ts` draw
the fill over a hairline baseline, with matching column text and scale; the
baseline still shows when the self-time fill is too small to draw.

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
### Find slow queries

`top`, `sessions` and `io` accept a single `--schema`; a repeated option exits 2 before connecting. Empty text results from `top`, `running`, `profiles list` and `audit list` emit table headers through the configured output destination, with explanatory notes on stderr unless quiet.

`python3 -m myflames top` (P_S digest summary, `top-1.0`) and `python3 -m myflames running [--explain ID]` (`running-1.0` / `explain-connection-1.0`; deprecated alias, use `sessions`), in `myflames/discover.py`; read-only, share `capture`'s connection flags. UI: the Workload place (`Server.tsx` with `place="workload"` wraps `SlowQueries.tsx`), `/api/live/top|running|explain-connection`; browser test `apps/workspace/test/discover.mjs`.
  Capture from a digest: `python3 -m myflames capture --statement-digest HASH` (full or 8+ hex prefix; exclusive with `-e`; `--digest` is a deprecated alias that warns on stderr) looks the row up with `discover.find_statement`, runs the sample text in the digest's schema when the connection has none, and records `source: {"kind": "digest", "digest", "schema"}` in capture-1.0 (`live.normalize_source`; `source` may also be a text label such as import-shell's `"mysqlsh-diagnostics"`). No runnable sample (MariaDB, or MySQL `sample_truncated`) exits 2 and prints the SQL with `:p1`, `:p2`. Every `top-1.0` row carries `capture_problem` from `live.capture_problem`, the same SELECT-only rule `bind_query` enforces; never duplicate it in the UI. Placeholder rules live twice, in `discover.parameterize` / `digest_query` / `join_function_calls` and `apps/workspace/src/placeholders.ts`; both run the shared cases in `apps/workspace/test/fixtures/digest-placeholders.json` (`test/test_digest_capture.py`, `test/placeholders.mjs`), so change them together. Digest text needs built-in function names rejoined to their `(` (`COUNT ( * )` is a syntax error; names from `sql/lex.h` SYM_FN). Every keyword token in digest text carries a trailing space (`gen_lex_token.cc` `m_append_space`), while a stored function is an identifier and prints quoted, so an unquoted `SUM (` is the built-in: check `digest_query(item)["sql"]` through `discover.digest_capture_problem`, never `["text"]`; raw SQL keeps the strict `dbops` check. Capture errors drop the wrapped script's "at line N" and map MySQL error 1235 for EXPLAIN ANALYZE below 8.4 to a version message (`live.client_error_text`); `server_info.unsupported_version_note` returns the one-line note for servers below MySQL 8.4 or MariaDB 10.11 (capture `warnings`, lifecycle plan `warnings`). Live CLI reports emit that note once on stderr unless `-q`: `_discover_run` uses the connection's cached version or the returned report's `engine` and `server_version` (`version` for Privileges), without an extra query. `upgrade-check` keeps its separate upgrade guidance. UI row actions Explain / Run analysis (confirmation first) and Explain top N (EXPLAIN only, sequential, max 20) live in `SlowQueries.tsx`; row actions open the Query editor through `QueryLabHandle.openDigest`; tabs keep `source`/`fill`; browser test `apps/workspace/test/digest-actions.mjs`.
  Workload attribution (`discover.py`, additive to `top-1.0`): `--order full_join` (`SUM_SELECT_FULL_JOIN`, then `SUM_SELECT_RANGE_CHECK`), `scan` (`SUM_SELECT_SCAN`) and `no_index` (`SUM_NO_INDEX_USED`) list only digests where that counter is above zero (`ORDER_FILTERS`), with ties broken by total time. `--table SCHEMA.TABLE` (`parse_table_name`; backquote a part with a dot) adds `TABLE_FILTER` (`SUM_NO_INDEX_USED > 0 OR SUM_SELECT_SCAN > 0`) and a `LOCATE` prefilter on the table name, reads up to `TABLE_CANDIDATES` (500) digests, and keeps those `table_reference` matches: `` `schema` . `table` `` or `schema.table` from any default schema (`qualified`), the bare name only when the digest's `SCHEMA_NAME` is the table's schema (`unqualified`), case-insensitive, never another schema's table or a longer name. Every row gains `select_full_join`, `select_range_check`, `select_scan` (read after `QUERY_SAMPLE_TEXT`, so older answers read as 0); with a table filter each row has `table_match {schema, table, kind, label: "text match, not proof"}` and the envelope `table` and `match {label, caveats, candidates, capped}`. Every envelope has `coverage {uptime_s, first_seen (oldest listed), digest_lost, overflow {executions, total_ms} | null}` (best effort; `Performance_schema_digest_lost` and Uptime from one `SHOW GLOBAL STATUS`). `/api/live/top` takes `order` (the seven values) and `table` (`"schema.table"` or `{schema, table}`). Never present a table match as proof: views, routines, aliases and digest text cut at `performance_schema_max_digest_length` break it, and the counters are per statement, not per table. Tests: `TestTableReference` and `TestWorkloadAttribution` in `test/test_discover.py`.
The `slow-queries-1.0` report (`POST /api/live/slow-queries`) accepts finite
`min_seconds` from 0 to 31,536,000 and an integer limit from 1 to 200. Slow-log
SQL applies `WHERE query_time >= SEC_TO_TIME(...) ORDER BY query_time DESC`
before LIMIT. Source failures, Performance Schema disabled state, grant hints
and consumer guidance are retained in additive `notes`, including when auto
mode falls back to digest data. Slow-log rows keep existing fields but return
null for unmeasured index-use, scan, join, temporary-table and sort counters;
rows-examined ratios do not imply an access method.

### Locks

`python3 -m myflames locks [--waits-only | --deadlock] [--limit N] [--json]` (`locks-1.1`), in `myflames/locks.py`: DDL statements waiting for a metadata lock (`ddl_waits`: from process-list wait states plus PENDING metadata locks; blockers `exact` from `performance_schema.metadata_locks`, `granted_locks` from MariaDB's optional `METADATA_LOCK_INFO`, else `likely` open transactions with an honest note; sessions queued behind the DDL), blocking tree over row and metadata lock waits with root blockers, INNODB_TRX transactions, row locks (MySQL `performance_schema.data_locks`, all locks; MariaDB `INNODB_LOCKS`, waits only), metadata locks with myflames' own GRANTED/PENDING compatibility check (not `sys.schema_table_lock_waits`), `kill_privilege` from plain `SHOW GRANTS` (active roles included; a hint, the server decides), and the parsed latest deadlock. The report only reads: never touch `setup_instruments`/`setup_consumers`; owner of a lock comes from `ENGINE_TRANSACTION_ID` → INNODB_TRX, not `THREAD_ID`. Keep the data_locks latch caution (MySQL < 8.0.40, 8.1–8.4.2, 9.0.x) and no default auto-refresh. The one write is `kill_session` (`kill-1.0`): `myflames locks --kill ID [--query] [--yes]` and the UI's **Kill blocker** (`POST /api/live/kill` with `connection_id`, `mode`, `expect`), one session per explicit, confirmed request; the workspace also offers a separately reviewed batch of at most 200 targets. It re-reads the session (same source as the report) and refuses when it is gone, a different session (user/host/thread/`trx_started` changed), its own or a myflames-tagged session, a server thread, already rolling back (INNODB_TRX `ROLLING BACK`; a "Killed" command alone is not proof, MariaDB shows it after KILL QUERY on an idle session), a prepared XA transaction on MySQL (idle session whose last statement in `events_statements_current` is `XA PREPARE`; the xid is copied only when well formed: ending the session releases nothing, so show `XA COMMIT`/`XA ROLLBACK` instead of Kill; MariaDB needs no check, it releases a detached XA transaction's metadata locks), or KILL QUERY on an idle session; server errors 1094/1095 become `gone`/`denied` results. A MySQL ALTER that adds a foreign key or renames also locks parent tables: `ddl_waits[].table` is the object it waits on, `statement_table` the one it names. CLI exit codes: 0 ended, 1 a sent operation failed, 2 an unsent refusal, decline, or error; refuses on a non-TTY without `--yes`. Both paths run the write policy first ([Write safety and audit](#write-safety-and-audit)): KILL is risky, so a read-only connection refuses it before any connection opens (CLI: before the password prompt; result `status: "refused"`, `engine: null`), and Production and Development both confirm (the CLI prompt or `--yes`; the UI sheet's `expect`). Every attempt writes an audit record (`kill_session(..., audit=)`, `kill_cli(..., audit=, policy_text=)`): refusals and declines as such, otherwise `pending` before the KILL is sent and `ok` or `error` (with the server error code) after; kill-1.0 results gain `audit_id`. Kill confirms and executions are bound to `connection_identity` and `server_identity` (MySQL `server_uuid`; MariaDB `server_id`, `hostname`, `version`); `locks.lock_report(identity=True)` includes the opt-in `server_identity` for the workspace Kill sheet (the CLI Kill re-reads it), and any mismatch or missing identity after confirmation refuses without sending. UI: `Locks.tsx` (Contention group in `Server.tsx`), `/api/live/locks` and `/api/live/kill` (saved-password helper, not in `_SERVER_VIEW_PATHS`); tests `test/test_locks.py`, `apps/workspace/test/locks.mjs` (fixtures captured from live servers in `apps/workspace/test/fixtures/`).
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

Sessions adds `kill_blocked_reason` (text or null) and marks Group Replication
streams as replication system threads. SHOW FULL PROCESSLIST has no SQL LIMIT;
its text is cut to `INFO_CAP` Unicode characters in Python after recording the
full UTF-8 byte length. The shared `READ_CAP` of 10,000 rows and partial-summary
note apply to this source too; the connector still bounds the full response.

### InnoDB

`python3 -m myflames innodb [--raw] [--fail-on problem|watch] [--json] [-o PATH] [-q]` (`innodb-1.0`), in `myflames/innodb_status.py`: `SHOW ENGINE INNODB STATUS` split into sections (`parse_status` is pure) and post-processed into `sections[]` of `{key, title, present, status, summary, items[], data, links[]}` (semaphores, transactions and purge, deadlock via `locks.parse_latest_deadlock`, foreign key error, file I/O, change buffer, adaptive hash index, redo log with health.py's pressure points against the capacity in use (`Innodb_redo_log_capacity_resized`, then "Log capacity used", never "Log capacity"), buffer pool with instances, row operations, background). InnoDB's `config` link reads "Open Parameters". Never return the raw text unless `raw: true` / `--raw`. Unknown or missing lines are tolerated (`parse`, `notes`); a sub-second averaging window drops the rates. Without PROCESS: `available: false`, `error.code: no_process`, CLI exit 2. UI endpoint `POST /api/live/innodb`; tests `test/test_innodb_status.py` with real captures in `test/fixtures/innodb/`.

### Error log

`python3 -m myflames error-log [--level LEVEL[,LEVEL...]] [--limit N] [--fail-on problem|watch] [--json] [-o PATH] [-q]` (`error-log-1.0`), in `myflames/error_log.py`. Read-only on both engines; the module docstring cites the server source for every fact below.

- MySQL before 8.0.22 returns `available: false`, `reason: unsupported_version`, the message "performance_schema.error_log needs MySQL 8.0.22 or later." and `how_to_view` before reading counters. MySQL (8.0.22+, WL#13681) reads `performance_schema.error_log` (`LOGGED`, `THREAD_ID`, `PRIO` System/Error/Warning/Note, `ERROR_CODE` such as `MY-010931`, `SUBSYSTEM`, `DATA`), which needs SELECT on that table and Performance Schema ON. The rows come from a 5 MB in-memory ring buffer described by the `Error_log_buffered_bytes`, `Error_log_buffered_events`, `Error_log_expired_events` and `Error_log_latest_write` status variables (`buffer`). When `@@log_error` names a file, the server re-reads its tail (at most 5 MiB) at startup through the leftmost parsing sink (`log_sink_internal` reads `@@log_error`, `log_sink_json` its own JSON file), so rows older than the current start are `previous_run`; with `stderr` or no parsing sink the table holds only events since this start. The start is UNIX_TIMESTAMP() minus Uptime: counts and `previous_run` compare epoch seconds (`UNIX_TIMESTAMP(LOGGED)`), and `server_started` is shown in the session time zone like LOGGED, so a daylight saving change does not move rows between runs. Status counters are read by an explicit name list (no LIKE pattern, so `NO_BACKSLASH_ESCAPES` cannot hide them). Only `log_sink_internal`, `log_sink_json` and `log_sink_syseventlog` feed the table; without one of them the view is not available (`no_pfs_sink`) instead of reporting zero events. With `log_sink_json` the `DATA` column holds the event's JSON document and the message is its `msg` key. `log_error_verbosity` drops Warning and Note events before they are logged (System and Error are never dropped by the built-in filter), so zero warnings at verbosity 1 means not recorded.
- MariaDB has no SQL access to its error log, and `LOAD_FILE` is never used (it needs FILE, `secure_file_priv`, a world-readable file and a size under `max_allowed_packet`). The module reads the `@@log_error` file (relative names resolve against `@@datadir`) only when all of these hold: the connection host is `localhost`, `127.0.0.1`, `::1` or empty; `@@hostname` equals this computer's host name (case-insensitive, the whole name); the path is a readable regular file; at least one line parses as an error log line; and the latest startup banner in the bytes read names `@@version` (or `'<built version>' as '<@@version>'` when version was set at startup) and `@@port`, and was logged no earlier than two seconds before and at most `BANNER_WINDOW_S` (an hour) after the server's start (`CONVERT_TZ(UTC_TIMESTAMP() - INTERVAL Uptime SECOND, '+00:00', 'SYSTEM')`, the log's local time). The banner fields are bounded (128 characters, no newlines). It reads at most the last 1 MiB (`TAIL_BYTES`; a cut that lands on a line start keeps that line) and returns parsed entries only: `[ERROR]`, `[Warning]`, `[Note]` lines and `YYMMDD H:MM:SS [ERROR] ... got signal` crash headers (no thread id), each with at most `MAX_CONTINUATION` (100) continuation lines; MariaDB has no System level. Otherwise `available: false` with `reason` (`log_to_stderr`, `remote`, `other_host`, `not_found`, `not_readable`, `unrecognized_format`, `identity_unverified` for no banner or one older than this start, `identity_mismatch` for another version, port or start), `reason_text` and a `how_to_view` hint (`tail -n 200 -- <path>` on the server host, words instead of a command when the path has control characters, or the service manager's log when `@@log_error` is empty). A denial during any MySQL read returns `denied` with no partial counts, codes or entries.
- Envelope: `base_report` fields plus `available`, `reason`, `reason_text`, `how_to_view`, `source` (`performance_schema.error_log` or `file`), `settings`, `buffer` (MySQL) or `file` (MariaDB), `server_started`, `counts` and `counts_current_run` (system, error, warning, note), `codes` (the 20 most frequent code, level and subsystem groups), `entries` (newest first, `limit` 1 to 1000, default 200, after the `levels` filter), `entries_total`, `summary` (`watch` when the current run logged an Error, never `problem`, `unknown` when not available) and `notes`.
- CLI: `--level` takes any of `system,error,warning,note`; `--fail-on watch` exits 1 on current-run errors. Not available exits 2 (also with either `--fail-on` threshold) with the reason and hint on stderr. With `--json`, the complete `error-log-1.0` envelope (`available: false`, `reason`, `reason_text`, `how_to_view`) is also written to stdout or `-o`; text mode emits only the stderr message. Connection/setup failures still have no payload.
- UI: Server > Overview > Error log (`ErrorLog.tsx`), after InnoDB. It reads when opened on screen, once per connection (Refresh and Refresh all read again; no Load button), and a server whose log cannot be read shows the reason once in its summary, with `how_to_view` prose separate from copyable commands and no duplicate gate; level filter, text filter, "This run only", Entries and By code segments, inspector with the full message. Endpoint `POST /api/live/error-log` (`limit`, `levels`; saved-password helper, `query_timeout` 30). Tests: `test/test_error_log.py` (replays in `test/fixtures/perf/error-log-*.json`, MariaDB file cases in a temporary directory) and `apps/workspace/test/errorlog.mjs` (`npm run test:errorlog`).

### Server identity: Percona Server

- Connection tests pass both `@@version` and `@@version_comment` to `product_identity`. With version `11.4.13` and comment `MariaDB Server`, they return `engine: mariadb`, `flavor: mariadb` and `product: MariaDB`.
- Query digest keeps `server_product` from both `top-1.0` and `slow-queries-1.0`, including slow-log reads and their Performance Schema fallback. The latter carries the same additive `flavor` and `server_product` fields via `conn_identity`. Its footer uses only the active connection's report; both the footer and text export use `engineName`.
- Two levels describe a server (`myflames/server_info.py`). The engine, `mysql` or `mariadb` (`detect_flavor`), decides rules, SQL and variable names. The flavor, `mysql`, `percona` or `mariadb`, and the product name, MySQL, Percona Server, Percona XtraDB Cluster or MariaDB (`product_identity(version, version_comment)`), decide only how the server is named and which Percona additions are read. Percona Server for MySQL and Percona XtraDB Cluster are engine `mysql`: every MySQL rule applies.
- Percona names itself only in `@@version_comment` ("Percona Server (GPL), Release 11, Revision 57878ff8", "Percona XtraDB Cluster (GPL), Release rel11, ..."; read live on 8.0.46-37, 8.4.11-11 and PXC 8.4.11-11.1). Its `version_suffix` variable can change the `@@version` suffix, so the version never decides when the comment is known. Where only the version is known (a relay log's format event for Replication's source version, the Privileges read and the Query editor's hello, which read `VERSION()` alone), `identity_from_version` names Percona Server by its numeric release suffix (`8.4.11-11`); MySQL builds carry none.
- Every view envelope carries `flavor` and `server_product` (`perf_common.base_report`, `Server.flavor` and `Server.product`; health, locks, top, running, replication, cluster, report and the single variable read add them too; `report-1.0` and `cluster-1.0` schemas list them; the Full analysis header's `server` has `flavor` and `product`; Replication's `source_version` has `flavor` and `product`; `/api/connection/test` returns `engine`, `flavor` and `product`). Text summaries use `server_info.product_name` / `server_label`, which fall back to MySQL or MariaDB for older reports; the workspace uses `engineLabel` (PerfShared) and `engineName(engine, version, product)` (OpsShared, which keeps Percona's release number: "Percona Server 8.4.11-11").
- Percona additions, read from the server only: with `userstat=ON` (dynamic on Percona, the same tables and columns as MariaDB's, `Ma/plugin/userstat/*_stats.cc`) Indexes reads `INDEX_STATISTICS` beside Performance Schema and keeps an index PS shows unused that userstat saw read (`verdict: keep`, `keep_reason: "userstat"`, `userstat_rows_read`; `report.userstat` `{enabled, read, role}`; with PS off userstat is the source, as on MariaDB), I/O uses `TABLE_STATISTICS` with PS off, and Tables adds `rows_read` and `rows_changed` to the inventory and to tables without a usable key (their `why` gives the rows changed, inserts, updates and deletes together, and says only updates and deletes need a replica search; `report.userstat` `{enabled, read}`). User statistics miss range reads of partitioned tables (Percona documents no support; a range read through `parted_pk.kw` was missing live on Percona Server 8.0.46-37 and 8.4.11-11 and MariaDB 11.4.13), so Tables gives partitioned tables `rows_read` and `rows_changed` null on every userstat server with a note, never the no-changes text; Indexes uses one bounded `information_schema.TABLES.CREATE_OPTIONS` read for candidate tables on every engine and usage source. Partitioned candidates go under No data (`reason: "partitioned"`), missing or unreadable metadata yields `reason: "partitioning_unknown"`, and neither gets DROP or visibility SQL. Percona userstat full-scan shares also require known plain tables; candidate-only metadata leaves other tables' shares unjudged; I/O's userstat note says partitioned tables can be missing or low. Health's `thread_cache` diagnostic, under `thread_handling=pool-of-threads` (Percona or MariaDB), shows the pool instead: `Threadpool_threads`, `Threadpool_idle_threads`, and on Percona 8.4 the queue counters, `thread_pool_size` and `thread_pool_max_threads` read in a second statement only then (state `not_applicable`, no threshold). Parameters (`parameters-1.0`) carries `flavor` and marks items with `vendor: "percona"` from `server_info.PERCONA_VARIABLES` (the names Percona Server 8.0.46 and 8.4.11 add to MySQL 8.0.46 and 8.4.11, compared live; `table_open_cache_triggers` only below MySQL 9.0, which upstreamed it); the single variable read has `vendor` too. The slow log check reads `log_slow_rate_limit`, `log_slow_rate_type`, `slow_query_log_always_write_time`, `log_queries_not_using_indexes` and `log_query_errors` in a second statement on Percona only and reports a sampled log as info with the share logged, also above a 10 s `long_query_time`. It follows Percona's `log_slow_applicable` (Percona Server 8.4.11-11 `sql/log.cc`): a statement admitted by `long_query_time`, `log_queries_not_using_indexes` or `log_query_errors` is skipped for the sample (query: a random 1 in N kept; session: only connections whose ID is a multiple of N) only when it ran under `slow_query_log_always_write_time` and, with `long_query_time` under 1 s, under 1 s. So nothing is sampled when that variable is 0, and at or below `long_query_time` only the statements the other two variables add are sampled; session sampling is never described as a share of statements (checked live on a throwaway 8.4.11-11). The Full analysis's version support check never applies MySQL's lifecycle dates to Percona: it is unknown, with MySQL's date for reference.
- Fixtures: `scripts/record-perf-fixtures.py` records one live view read (`indexes`, `tables`, `config [--parameters]`, `io`, `waits`, `health`) from the GA matrix as a replay fixture; the Percona ones (`test/fixtures/perf/*-percona-8.*.json`, `test/fixtures/health/percona-8.*.json`) feed `test/test_percona.py`.

### Server health

`python3 -m myflames health [--interval N] [--fail-on problem|watch] [--json] [-o PATH] [-q]` (`health-1.0`), in `myflames/health.py`: two `SHOW GLOBAL STATUS` samples N s apart (1–60, default 5) plus a few `SHOW GLOBAL VARIABLES`; no Performance Schema, no privilege. Sampling (`read_variables`, `take_sample`, `health_report`) is separate from the pure `compute_health`; each metric has value/delta/rate/unit/status/why/rule/link/learn. Statuses are rules of thumb (`RULES`, constants at the top), `info` where no universal limit exists, `unknown` for missing or reset counters (Uptime going down = restart). Keep the engine rules from the module docstring: MySQL disk reads = `Innodb_buffer_pool_reads` + `_read_ahead`, MariaDB reads already include read-ahead (same as `advisor._rule_cold_cache`); MariaDB counts a memory-to-disk temp table twice in `Created_tmp_tables`; MySQL redo rate uses the `Innodb_redo_log_current_lsn` delta; dirty pages are only ever watch. Diagnostics (`diagnostics` list, `_diagnostics`, `DIAGNOSTIC_RULES`): title, headline, verdict, why, rule, numbers, settings (value + engine default), a recommendation only at watch/problem and always with a caveat; `summary.diagnostics` counts them and `summary.status` is the worst of metrics and diagnostics. Buffer pool full = free pages <= `innodb_lru_scan_depth` x instances (MySQL; MariaDB one instance), bytes read = pages read x `innodb_page_size`; redo coverage and time-to-pressure come from the LSN growth (MySQL `Innodb_os_log_written` re-counts partial blocks, measured 2x under small commits), capacity from `config_review.redo_capacity` (`Innodb_redo_log_capacity_resized` first), thresholds from the checkpoint age (MySQL ~0.73/0.79 of capacity, MariaDB 7/8 and 1.0 of `Innodb_checkpoint_max_age`); the temp-table limits text is `config_review.tmp_table_limit`. Never change a setting. `--fail-on` covers diagnostics and metrics, exits 1 like `check`; 2 on bad input or connection failure. The tool's own second sample (3 statements, 1 temp table, 1–2 scans, 1 opened table on MySQL) must never trip a rule (`MIN_EVENTS`). UI: `Health.tsx` (first Server tab), `/api/live/health`; the sample history (20, sparklines) stays in memory, session restore keeps only the tab and `health_interval`. Tests `test/test_health.py` (live two-sample fixtures from MySQL 8.4/9.7/26.7 and MariaDB 11.4/13.0 in `test/fixtures/health/`, plus `scenario-*` captures for warming, full, redo, binlog, threads, and table cache), `apps/workspace/test/health.mjs` (`npm run test:health`; the Diagnostics cards, their trends, and 390 px).
Health and configuration advice share version-aware limits. MariaDB `innodb_log_file_size` is read-only before 10.9.1: recommend only `innodb_log_file_size = <bytes>` under `[mysqld]` (or `[mariadb]`) and a restart. MariaDB 10.6 to 10.8 has one redo file, so that line holds the total capacity. From 10.9.1, retain `SET GLOBAL innodb_log_file_size` online resize advice. MySQL TempTable has no per-table limit before 8.0.28; `temptable_max_ram` is global and decides when its tables overflow to disk (memory-mapped files may be used first). Only from 8.0.28 does `tmp_table_size` limit each TempTable table. MySQL MEMORY and MariaDB keep their smaller-of-two per-table limits. When only `TempTable_count_hit_max_ram` grows, Health recommends `SET PERSIST temptable_max_ram = <bytes>;`, with a caveat that all TempTable tables share it, and no `SET SESSION tmp_table_size`. Tests: `test_correctness_regression_mariadb_redo_resize_version` and `test_correctness_regression_temptable_per_table_version` in both `test_health.py` and `test_config_review.py`, plus `test_health.py::test_correctness_regression_temptable_limit_hit_action`.

Health text prints `Statements: myflames top --order full_join` once when a
`select_full_join` metric or diagnostic is watch/problem; other statuses omit it.

### Waits, Indexes, Tables, I/O, Config

- **Waits, Indexes, Tables, I/O, Parameters (`config`)** (Workload place Resources group: Waits, I/O; Server place groups Schema: Indexes, Tables; Setup: Parameters (internal view id `config`); read-only, share the connection flags and `--profile`, `--json`, `-o`, `-q`): shared plumbing in `myflames/perf_common.py` (`Reader` tags every statement `/*myflames:<view>*/`; `preflight` reads variables with `SHOW GLOBAL VARIABLES WHERE Variable_name IN (...)`, never `SELECT @@x`, plus Uptime and `Performance_schema_*_lost`; P_S off returns empty tables, not errors, so check `performance_schema` first). Never TRUNCATE (it also resets per-table and per-index stats), never write `setup_instruments`/`setup_consumers`, never `SET GLOBAL userstat`; print the statement instead. Raw P_S/I_S tables only, never sys views.
  - `python3 -m myflames waits [--interval N] [--limit N]` (`waits-1.0`, `myflames/waits.py`): two reads of `events_waits_summary_global_by_event_name` N s apart (2–60, default 10), `EVENT_NAME LIKE 'wait/%'` (excludes idle); window deltas (a counter going down drops the window), per-class sums, since-start list with max; never add classes up or show a share (table I/O nests file and synch waits). Coverage from `setup_instruments` with the enable UPDATE and my.cnf line (`ENABLE_CAVEAT`). MDL instrument off by default on MariaDB; `innodb_temp_file` means differ per engine (`ENGINE_MEANINGS`).
  - `python3 -m myflames indexes [--schema NAME ...] [--all-schemas] [--limit N]` (`indexes-1.0`, `myflames/indexes.py`): unused = COUNT_STAR 0 in `table_io_waits_summary_by_index_usage`, read before I_S (MariaDB's STATISTICS opens tables, registering them with zero counts); PRIMARY and UNIQUE never listed on both engines; FK-backing indexes (KEY_COLUMN_USAGE, leading columns in order, PK columns appended per engine) are `keep`; tables with no row operations or a disabled `setup_objects` row are `no_data`. Partitioning is checked after usage and definitions, reusing the `TABLES.CREATE_OPTIONS` read for every engine, only for distinct tables with zero-use index candidates (at most 100 per statement, one schema and exact table-name predicates). MariaDB opens these candidate tables; MySQL reads dictionary fields. Partitioned candidates and tables whose metadata is missing, NULL or unreadable are `no_data` (`reason: partitioned|partitioning_unknown`, additive `reason_text`); zero Performance Schema counts cannot establish non-use on partitioned tables. Failed metadata batches keep a note and cannot produce unused candidates; successful batches remain usable. Inventory and redundant rows of affected tables carry additive `action_blocked_reason`, suppressing DROP and visibility actions, including row and context menus and inspectors. Blocked redundant rows retain string `test_sql`, `drop_sql` and `caveat` fields as empty strings. The `indexes-1.0` version and existing field types stay unchanged. Redundant indexes reuse `advisor.find_redundant_indexes` over I_S.STATISTICS (parity test with the advisor rule). Full scans use `SUM_TIMER_FETCH` of the NULL-index row. MariaDB P_S off + `userstat=ON` falls back to INDEX_STATISTICS/TABLE_STATISTICS. `--schema` repeats (scope `schemas`; `schema` stays for one); the CLI warns without `--schema` or `--all-schemas` and reads every user schema up to 2,000 tables (`perf_common.require_choice` on the selected scope). `--all-schemas` lifts that limit like the API's `all_schemas`, for every user schema or, with `--schema`, for those schemas only. The UI database picker sends `schemas`; with none, `/api/live/indexes` reads all user schemas only up to `perf_common.ALL_SCHEMAS_TABLE_LIMIT` (2,000, MySQL's default table_definition_cache) base tables and raises above it (`all_schemas: true` skips the check; the UI never sends it). The shared Indexes/Tables gate and schema picker note add a MariaDB-only sentence warning that the read opens every table in scope and can evict cached table definitions (`all_schemas_note(engine=...)`); MySQL keeps the existing message. Indexes text adds `Statements: myflames top --order no_index --table SCHEMA.TABLE` under TABLES READ WITHOUT AN INDEX for the first listed table only; empty/unavailable sections omit it.
  - `python3 -m myflames tables [--schema NAME ...] [--all-schemas] [--fresh] [--limit N] [--fail-on problem|watch]` (`schema-2.0`, `myflames/schema_health.py`; named `tables` because `--schema` already means one database): tables without a PRIMARY KEY from I_S.TABLES (BASE TABLE + MariaDB SYSTEM VERSIONED; sequences and views skipped) and I_S.STATISTICS `NON_UNIQUE = 0` in key order. `no_usable_key` (problem: hidden 6-byte DB_ROW_ID) vs `not_null_unique` (watch: InnoDB clusters on the first unique key with all parts NOT NULL, whole columns, not functional, not virtual generated, not MariaDB long hash). GIPK (`my_row_id`, EXTRA `auto_increment INVISIBLE`) is listed apart; when `show_gipk_in_create_table_and_information_schema` is OFF globally, prefix `SET SESSION ... = ON;` in the same client call. Fix statements (never run): NULL/duplicate checks, `ADD PRIMARY KEY (...), DROP INDEX uk, ALGORITHM=INPLACE, LOCK=NONE`, invisible `AUTO_INCREMENT` surrogate with `LOCK=SHARED`; no ALGORITHM/LOCK for other engines. Free space: DATA_FREE is reusable allocation; `free_pct` is an approximate share of reported DATA_LENGTH + INDEX_LENGTH + DATA_FREE, not exact physical file size. Positive known file-per-table space is `info`; zero is `ok`; shared and unknown remain distinct. `summary.free_info` counts informational rows; `free_watch` and `free_problem` remain zero for compatibility, and `thresholds` is empty. Optional rebuild SQL remains available with caveats. Capacity and filesystem headroom are not measured. Tablespace map: MySQL `INNODB_TABLESPACES` (record-by-record DD scan with informational `AUTOEXTEND_SIZE`; `INNODB_TABLESPACES_BRIEF` fallback on "Unknown column"; never `INNODB_TABLES`, which loads every table into the dictionary cache) plus `SHOW CREATE TABLE` (max 50) only when a General tablespace exists; MariaDB `INNODB_SYS_TABLES` (space 0 = system; `INNODB_SYS_TABLESPACES` lists only opened spaces). Needs PROCESS, else `unknown` + note. `--fresh`: `SET SESSION information_schema_stats_expiry = 0;` before the TABLES read, same client call (MySQL only; MariaDB has no cache). Row-ID, replica-apply and OPTIMIZE texts are version-aware (`row_id_text`, `why_texts`, `optimize_caveat`). Schema picker list: `perf_common.schema_catalog` (`schemas-1.0`, `/api/live/schemas`; MariaDB counts name columns only so no table is opened); `check_schemas`/`scope_sql` are shared with Indexes.
  - MariaDB 11.2+ MyISAM primary-key rebuild advice states that the copy holds a table read lock: UPDATE and DELETE wait for the whole copy; only concurrent inserts continue (adding AUTO_INCREMENT blocks those too). InnoDB retains its in-place/LOCK guidance. Hidden InnoDB row IDs use one shared atomic counter on MariaDB 10.6 to 10.11, then a counter per table from 11.0. Test: `test_schema_health.py::test_correctness_regression_myisam_copy_lock`.
  - Rule passes stay linear in the rows read: Indexes builds the `setup_objects` lookup once per report and checks the three precedence keys per table; Tables computes whether any General tablespace exists, and the case-folded tablespace names used when `lower_case_table_names` is 1 or 2, once per report (`schema_health._folded`); Locks builds each metadata-lock group's owner list once and shares it across the pending requests that read it (`locks._mdl_blockers`). `test_indexes.py`, `test_schema_health.py` and `test_locks.py` count these passes.
  - `python3 -m myflames io [--interval N] [--schema NAME] [--limit N] [--all-instances]` (`io-1.0`, `myflames/io_stats.py`, never `io.py`): status deltas (no P_S needed), `table_io_waits_summary_by_table` (schema-scoped), `file_summary_by_instance` and `_by_event_name` (temp files only in the latter), since-start lists; `classify_file` maps FILE_NAME to kinds and schema.table (`#p#`/`#P#`, `@XXXX`). MariaDB has no redo log file instrument. Above `INSTANCE_LIMIT` (2,000) table or file instances a family is left out of both snapshots (`collection.omitted`) unless `--all-instances` / `all_instances`; collection has a 30 s budget plus the interval. No auto-refresh in the UI.
  - `python3 -m myflames config [--fail-on problem|watch]` (`config-1.0`, `myflames/config_review.py`): checks with engine `DEFAULTS`, `set_by` from `variables_info` (MySQL) or `SYSTEM_VARIABLES` (MariaDB), statuses with `RULES`, `why`, `caveat`, and `action` text; reuses `advisor.buffer_pool_coverage` and `advisor.flush_log_durability` (MariaDB's 3 is durable). Tablespace sizes need PROCESS (else `unknown`). `--fail-on` exits 1 like `health`. Each setting carries `dynamic` (`variables.static_dynamic`: true, false, or null when unknown, no extra read); changing a setting is [`config set`](#server-variables-config-set-reset-persist). The CLI `myflames config` is unchanged (no parameter list, no new flags). `/api/live/config` (still `config-1.0`, internal view id still `config`) calls `config_review.config_report(conn, parameters=True)` and includes `parameters` (`parameters-1.0`, built by `myflames/parameters.py`: `read_parameters` and pure `compute_parameters`) with `complete`, `notes`, and `items[]` (`name`, `value`, `truncated` for values over 4,096 characters, `scope`, `apply_type`, `modifiable` (`now`, `restart`, `option_file`, `none`, `null`), `type`, `values`, `min`, `max`, `block_size`, `default`, `source`, `source_label`, `source_path`, `set_time`, `set_user`, `set_host`, `persisted`, `modified`, `description` capped at 600 characters (MariaDB `VARIABLE_COMMENT`; MySQL 9.0+ `variables_metadata.DOCUMENTATION`; MySQL before 9.0 reports none, so `parameters.source_help` takes the help text the variable is declared with in the tagged source from the generated `myflames/mysql_sysvar_help.py`: `HELP` from the newest 8.4 tag, and `HELP_80` from the newest 8.0 tag where 8.0 declares another text or a name 8.4 removed; a text built with a macro, or one that only repeats the name, is left out), `documentation`, `checks`) and `flavor` (`mysql`, `percona` or `mariadb`: the server object's `flavor` when it has one, else `percona` when `@@version_comment` names Percona). `documentation` is `{url, source}` or null from `parameters.documentation(engine, version_tuple, name, flavor)`: the MySQL Reference Manual for MySQL and Percona servers (the first saved manual at or after the server's major.minor, else the newest; a name that manual leaves out comes from the nearest newer, then older manual, and `source` names it, "MySQL 9.7 Reference Manual"), MariaDB's documentation for MariaDB, and only for a name no MySQL manual documents on a Percona server, Percona Server's then Percona XtraDB Cluster's manual. The locations come from the generated `myflames/sysvar_docs.py`: `scripts/gen_sysvar_docs.py --saved-on DATE` reads saved official pages (never the network): each MySQL manual's "Server Option, System Variable, and Status Variable Reference" (rows whose System Var column says Yes; with `--mysql-pages`, the linked pages, a row that links an option anchor uses the `sysvar_<name>` anchor a saved page defines, and an anchor no saved page defines is dropped), MariaDB's "Full List of MariaDB Options, System and Status Variables" (lowercase system variable names whose link text is the anchor, status variable pages skipped; with `--mariadb-pages`, a stale anchor moves to the one saved `...system-variables` page that defines it, and a heading on such a page that the list leaves out is added) and saved copies of the Percona Server and Percona XtraDB Cluster manuals (a heading whose lowercase text is a variable name with the option table under it). Saved on 2026-10-21 from the MySQL 8.0, 8.4, 9.7 and 26.7 manuals, MariaDB's documentation, Percona Server 8.0, 8.4 and 9.7 and Percona XtraDB Cluster 8.0 and 8.4; a live MySQL 8.4.11 server links 621 of its 623 parameters, MySQL 9.7.2 625 of 631, MariaDB 11.4 657 of 674 and a Percona XtraDB Cluster 8.4 node 719 of 740. The desktop app opens `dev.mysql.com`, `mariadb.com` and `docs.percona.com` (`apps/desktop/backend.cjs` EXTERNAL_HOSTS); `Config.tsx`'s `documentationLink` accepts only HTTPS links to those hosts. Tests: `test/test_sysvar_docs.py`. `scripts/gen_mysql_sysvars.py --mysql-source PATH` writes `mysql_sysvar_help.py` beside `mysql_sysvars.py` (`parse_help`; tests in `test/test_mysql_sysvars.py` and `test/test_parameters.py`); a live MySQL 8.4.11 server describes 571 of its 623 parameters this way and MySQL 8.0.46 577 of 631. `read_parameters` reads MySQL `SHOW GLOBAL VARIABLES`, `performance_schema.variables_info`, `persisted_variables` and (9.0+) `variables_metadata`, or MariaDB `information_schema.SYSTEM_VARIABLES` without `SESSION ONLY` variables; a denied or missing table becomes a note and `complete: false`, never an error.
  - Generated MySQL system variable catalog: `scripts/gen_mysql_sysvars.py --mysql-source <mysql-server checkout>` writes `myflames/mysql_sysvars.py` (817 variables) from the `mysql-9.7.0` tag and the newest 8.4 tag (`sys_vars.cc` declarations with or without `static` and with `(` or `{` initializers, plus InnoDB, semisync, X plugin, group replication, `caching_sha2_password` and `validate_password` declarations), recording scope, type and the flags `ro` (`READ_ONLY`), `np` (`NON_PERSIST`), `pro` (`PERSIST_AS_READONLY`) and `nc` (no startup option: `NO_CMD_LINE`, the `sys_vars.h` classes that force it, or `PLUGIN_VAR_NOCMDOPT`); facts that differ between the two versions are recorded as unknown; a declaration-count test fails when a new declaration shape appears. `variables.py` keeps its hand-verified map authoritative and falls back to the catalog; `test/test_mysql_sysvars.py` checks that all 84 hand-verified entries agree. On a live MySQL 8.4 server, 597 of 623 parameters get an apply type; the SSL and admin TLS variables (declared in `sql/ssl_init_callback.cc`, and applied only by `ALTER INSTANCE RELOAD TLS` or a restart) and a few plugin variables stay unknown. `modifiable` is `none` for a static variable the server sets itself (MySQL catalog flag `nc`; MariaDB `COMMAND_LINE_ARGUMENT` is NULL), except variables set through separately named mysqld options (`core_file`, `log_bin`, `log_bin_index`, `relay_log_index`, `skip_external_locking`, `locked_in_memory`), which stay `option_file` or `restart`. `modified` is false for MySQL `COMPILED` and MariaDB `COMPILE-TIME` or `AUTO` (sized by the server). Batch output prints SQL NULL and the text NULL alike, so values read as NULL are read again exactly (`VALUE IS NULL, HEX(VALUE)` from `performance_schema.global_variables` or `SYSTEM_VARIABLES`); when that read fails the inventory is `complete: false`. `variable-1.0` carries the same fact as `startup_option` (`variables.startup_option`: false, true or null), so the single-parameter plan offers neither SET PERSIST_ONLY nor an option file line for a value the server sets, and the inspector's Change… is off for `modifiable: none`. In the batch review, a request that fails without a reply is reported as `unknown` (it may have run), never as unchanged.
  UI: `Waits.tsx`, `Indexes.tsx`, `Tables.tsx` (+ `tables.css`), `IoStats.tsx`, `Config.tsx` (Parameters), shared `PerfShared.tsx` (incl. `SchemaPicker` and `useSchemaCatalog`, one cached list per connection) + `perf.css`; endpoints `/api/live/waits|indexes|tables|schemas|io|config` (in `ui._SERVER_VIEW_PATHS`, saved-password helper). Session restore keeps the tab, the shared `schema` (I/O and Top statements' schema filter), per-tab `indexes_schemas`/`tables_schemas` (omitted when empty = all user databases) and `waits_interval`/`io_interval`, never reports. Links: I/O table rows open Index health or Top statements for that schema; Indexes and Tables rows open each other for the same table (picker set to its database, a "Showing only" focus chip); wait rows open Locks or I/O; Full analysis links that name a check open Parameters at that check's first parameter, InnoDB's link reads "Open Parameters", and the Full analysis section is named Parameters. Tests: `test/test_waits.py`, `test_indexes.py`, `test_schema_health.py`, `test_io_stats.py`, `test_config_review.py`, `test_mysql_sysvars.py` (replay fixtures recorded live in `test/fixtures/perf/`, helper `test/_perf_replay.py`), browser suites `apps/workspace/test/{waits,indexes,tables,io,config}.mjs` (`npm run test:waits|test:indexes|test:tables|test:io|test:config`, shared `perf-harness.mjs`, whose `open(endpoint, name, extra)` mocks extra paths such as `/api/live/schemas`).
### Server variables: config set, reset-persist

Backend `myflames/variables.py` (contract: the db-operations skill; facts and source citations: the module docstring), CLI `_config_change` in `myflames/workspace_cli.py` (dispatched from `cmd_config`), endpoints `ui._variable_response` / `_variable_apply`, UI `Config.tsx` + `config.css`. Tests: `test/test_variables.py` with live replays `test/fixtures/perf/variable-<server>-<scenario>.json` (MySQL 8.4.11 and MariaDB 11.4.13, recorded 2026-10-15 on throwaway containers: SET GLOBAL; SET PERSIST, then a container restart, `restart` shows the value survived; SET PERSIST_ONLY of the read-only innodb_log_file_size, `restart-persist-only` shows it applied; RESET PERSIST; SET SESSION; the 1238 and 1227 refusals; a value changed between plan and apply; buffer pool and MariaDB block-size rounding), browser suite `apps/workspace/test/config.mjs` (`variable-*.json` written by `server-fixtures.py`).

- `read_variable(conn, name)` (`variable-1.0`, tag `/*myflames:variable*/`, reads only): one client call with `SHOW GLOBAL VARIABLES` and `SHOW SESSION VARIABLES` separated by marker rows, then MySQL `performance_schema.variables_info`, `persisted_variables` and (9.0+) `variables_metadata`, or MariaDB `information_schema.SYSTEM_VARIABLES`. Fields: `scope` (global, both, session, or null), `dynamic` (MariaDB `READ_ONLY`; MySQL from the authoritative 84-entry `MYSQL_VARIABLES` map in `variables.py` or the generated 812-variable `mysql_sysvars.py` catalog, null outside both), `type` (bytes, integer, double, boolean, enum, set, flagset, string), `values`, `min`, `max` (MariaDB catalog; MySQL variables_info or variables_metadata for integers, the map for doubles), `block_size` (MariaDB), `global_value`, `session_value`, `default`, `set_by`, `persisted {supported, value, present, load}`, `modes {global, persist, persist_only, session, reset_persist: {available, reason, recommended}}`, `notes`. A MySQL variable outside the map is offered by the CLI only; the server decides (1238 when read-only).
- `plan_change(conn, name, value, mode)` and the pure `build_plan(v, value, mode, grants)` (`variable-plan-1.0`): the mode must be available (MariaDB has no PERSIST, PERSIST_ONLY or RESET PERSIST: refused with `alternative.config_line`, the `[mysqld]` line; a read-only MySQL variable offers only `persist_only`; wrong scope; NON_PERSIST; a session value that is read-only), and the value valid (`normalize_value`: integers within the range, K/M/G/T converted for byte sizes, doubles, ON/OFF, enum and set items matched without case, `DEFAULT`; string values preserve leading and trailing whitespace exactly, while numeric, boolean and enum input is trimmed; text as `'...'` when plain, else `CONVERT(X'..' USING utf8mb4)` so no escaping rule or sql_mode matters). Returns `statement` (untagged), `display`, `before {global, session, persisted}`, `undo`, `effect {now, after_restart, needs_restart, text}`, `caveats` (the server does not check a PERSIST_ONLY value, persisted_globals_load OFF, mysqld-auto.cnf applied after the option files, buffer pool rounding to chunk size x instances and the 1 GiB rule with several instances, MariaDB block size, memory per connection, durability, MariaDB's log_slow_query copied per connection, replication-sensitive session variables), `privileges` (`privileges(v, mode, grants)` over plain `SHOW GRANTS`: dbops' operations, MariaDB's per-variable SET GLOBAL privileges `MARIADB_GLOBAL_PRIVILEGE`, restricted SET SESSION per engine with MySQL's `_no_super` ones, both privileges to reset a PERSIST_AS_READONLY entry), `classification`, `config_line` and `expect {name, mode, statement, global_value, session_value, persisted_value}`. Status `refused` with `problems [{code, message}]` sends nothing.
- `apply_change(conn, plan, expect, decision, confirmed, audit)` (`variable-change-1.0`): `dbops.gate` first (SET GLOBAL, PERSIST, PERSIST_ONLY and RESET PERSIST are risky, so Production and Development confirm; SET SESSION follows dbops' variable lists; read-only refuses), then a fresh read and plan; nothing is sent when the plan is refused, its statement differs from `expect.statement`, or the global, session (session mode) or persisted value changed. The audit record is written before sending; one statement tagged `/*myflames:variable*/` with `dbops.WRITE_CLIENT_FLAGS`; SET SESSION and its `SHOW SESSION VARIABLES` go in one client call (one session). Server errors: `denied` (1227, 1095, 3630, 1142, 1044, 1045), `refused_by_server` (1228, 1229, 1238, 1231, 1232, 1193, 1621, 3615, 3976, 1210, 1290), `interrupted` (1317, 1205, 1213), else `failed`; client errors (2000 and up) raise, and an interrupt, time limit or 2013 leaves the record `unknown`. Then the variable is read again (`after {global, session, persisted, resize_status}`): `changed` (`adjusted` when the server rounded or clamped it) or `unchanged` (the server kept the old value, a warning-only refusal). Audit operations `set_global`, `set_persist`, `set_persist_only`, `set_session`, `reset_persist`.
- `python3 -m myflames config set NAME VALUE [--persist | --persist-only | --session] [--yes] [--json] [-o PATH] [-q]` and `config reset-persist NAME [--yes] [--json] [-o PATH] [-q]` (`change_cli`): the policy from `--profile` runs before any prompt or connection (read-only refuses, exit 2); the plan (statement, values, effect, privileges, caveats, undo) on stderr; `[y/N]` when the policy confirms (a harmless SET SESSION on Development runs without); non-TTY needs `--yes`. Exit 0 changed or already set to the requested value, 1 a sent change was denied, refused by the server or not applied, 2 an unsent refusal, decline, bad input or connection failure. `myflames config` alone stays the read-only review. `config set --session` prints a stderr note that it applies only to the one-off connection myflames opens, useful to test a value; `--yes -q` suppresses this note with the plan.
- Endpoints: `POST /api/live/variable` (`name`) and `POST /api/live/variable/plan` (`name`, `value`, `mode`; adds `policy {action, tone, title, text, connection_type, read_only}`) read with the saved password like the views (`_VARIABLE_READ_PATHS`, not `_SERVER_VIEW_PATHS`); `POST /api/live/variable/apply` (`name`, `value`, `mode`, `expect`, `confirmed: true`; `_WRITE_PATHS`: policy, then the saved password, then `apply_change`). A refused or unconfirmed request is audited.
- UI (**Parameters**, `Config.tsx` + `config.css`, modelled on an AWS RDS parameter group): reads on open (one attempt per connection; Refresh retries), while the sidebar badge still comes from the checks. One table lists every global parameter the server reports with columns Name, Value, Allowed values, Apply type (Dynamic, Static, Unknown), Source, Data type, and Description; search ("Filter parameters") matches name, value and description, and the Show filter offers All, Modified (source is not the compiled default), Dynamic, Static, Recommendations (parameters covered by a Watch or Problem check), and Pending while editing. The view toolbar stays pinned under the top bar. **Edit** turns the Value column into inline controls (switch, list or text box from the parameter's type) for changeable parameters: MySQL dynamic (applies now) and MySQL persistable static (applies after the next restart through `SET PERSIST_ONLY`), or MariaDB dynamic only; MariaDB static parameters are option-file only and parameters with an unknown apply type are not editable in the table. **Preview changes** lists Name, Current value, New value, Apply type and When it applies. **Review and apply** opens one batch review for up to 25 changes: the server plans each change (exact statement, problems, privileges, effect, undo); MySQL dynamic rows use "Keep after restart" (checked: `SET PERSIST`, unchecked: `SET GLOBAL`), MySQL static rows use `SET PERSIST_ONLY`, and MariaDB rows use `SET GLOBAL` and show the option file line to copy. Confirm applies the changes one at a time (each re-read and checked first), stops at the first change that is not confirmed, and shows a result per row; Cancel or Escape records a declined attempt for every planned row in the audit trail. The inspector has one compact section per parameter: one sentence (`oneLine`: the server's description, else the opening sentence of a check whose first setting it is), the **Documentation** link (`documentationLink`), and **Current value** (Value, Default, Source with path and who set it when, Changes in words from apply type, scope and `modifiable`, Allowed values, Persisted on MySQL, and Checked when covering checks do not flag it); a **Recommendation** follows only for a Watch or Problem check, the most severe one (reason, `action.text`, the statement and option file line as CopySql, the caveat), naming any other flagging check once. The actions sit in the inspector's action bar: **Edit value**, **Reset to default** (stages `DEFAULT` for MySQL numeric and boolean dynamic parameters), **Change…** (the single-parameter `VariableSheet` with every mode, including This session only), and **Reset persisted value…** (MySQL, when persisted). Permission details appear in the review dialog body; entry and confirmation buttons omit the Unverified badge.
- Session in Parameters (**Change…** > This session only) prepares SQL only. `ConfigView.onOpenSql(sql)` receives the exact server-planned `SET SESSION` statement plus its terminator; `VariableSheet` calls it for **Open in Query editor**, never `/api/live/variable/apply`. Cancel/Escape on a Session draft creates no declined audit. The editor owns execution and its existing policy checks. The planning session value is labelled as a snapshot; its undo is omitted because the editor may have a different value. Without the optional callback the button is disabled and the statement stays copyable. Shell integration: pass `onOpenSql` through `Server` to `ConfigView`; in `main.tsx` call `labRef.current?.openSql(sql)` and `setMode("query")`. Add `openSql` to `QueryLabHandle` and its imperative handle to update the current tab via `changeTab`, clear stale digest source/selection metadata, and focus it without calling `send` or capture. These shell files are owned separately from Config. `node test/config.mjs --unit` checks the handoff without a browser.


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
- A place of its own (`Place: "tuning"`, label "Tuning", palette keyword "mysql tuning"), never a Server view. `Server` renders it with `place="tuning"`: the TuningView full width (`.server-place.is-tuning-place`), no list of views or place toolbar (no Refresh all, ⌥⌘R off), the window footer (scope `tuning:<connectionKey>`), and the bound reports passed to TuningView (a report read on another connection is shown greyed with the other-connection note and its actions off, never as this one's; the Full analysis tab keeps its report that way). The Run full analysis buttons and the tab's Refresh are disabled while Refresh all runs on the Server place (`runBlocked`), so the Full analysis never starts a worker pool beside the global view queue. A Full analysis link that reveals a row in a view of another place keeps that row's inspector open (`keepInspectorOnNavigation`, as Instruments links do). `Server.choose("tuning")` or `choose("report")` navigates to the place; the Full analysis is its second tab (a `report` link or the palette's Full analysis selects it). A session that saved `tuning` or `report` as the Server view opens Health there.
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
- Tools: `detect_tools` (`lifecycle-tools-1.0`; PATH then `COMMON_BIN_DIRS`, `--version` in an isolated HOME, engine from the banner, `mysqlpump` listed as deprecated and never offered); `offered_tools(server, tools)`. Option files (`_credentials`): `[client]`, every value quoted, 0600 in a 0700 temporary directory, first on argv, removed in `finally`; environment allowlisted (`_clean_env`, no `MYSQL_PWD`). SQL and Shell scripts go on stdin. Endpoint `POST /api/lifecycle/tools` (with a `connection`, also `server` and `offered`).
- Tool release metadata: `tool_updates.enrich_tools`, invoked by `detect_tools(check_updates=True)` only after explicit UI detection/refresh. Each tool adds `download_url` and `update {status, latest_version, checked_at, source_url, message}`. Status is `not_checked`, `available`, `current`, `unknown`, `not_installed` or `deprecated`; current means no newer verified release in the checked series, not a compatibility or support guarantee. MySQL GA download headings and the MariaDB stable-series REST API supply versions. Fixed HTTPS endpoints only, no redirects or ambient proxies, 1 MiB response cap, 5 s socket timeout, 12 s caller budget, at most 8 in-flight workers and 64 cache keys; success TTL 1 hour, failure TTL 1 minute. Unsupported distributions/prereleases, mismatched series, malformed/future metadata and failed checks stay unknown. CLI discovery and lifecycle preflights do not request network checks.
- `POST /api/lifecycle/choose-directory` accepts an empty object through the normal authenticated API. It opens the server host's folder dialog, with a 120 s deadline and one dialog at a time; no client-supplied script or path is executed. Cancel preserves the form path. The selected directory still undergoes normal preflight and empty-directory validation.
- Logical dump options: `dump_options.capability_catalog` exposes tool/scope-specific descriptors, each with `group` (one of `groups`: performance, contents, output, compression, the sections of the Dump form) and, for a boolean, `row` (the label its checkbox shares: Chunking, Include, Write, Connection); `encryption` has a one-line `summary` and the full `message`. `normalize` rejects unknown names and invalid values. Shell supports compression default/none/gzip/zstd, levels from Shell 8.4 (gzip 0..9, zstd 0..22), chunking, decimal byte sizes (minimum 128k per chunk) and max rate per thread. SQL tools expose hex blobs, add-drop-table and network compression. Triggers/routines/events follow scope, with no routines/events toggle for Shell table dumps. Local file encryption is unsupported; TLS and connection compression affect transport only. Selected options bind the plan, execution and manifest; CLI flags are unchanged.
- Plans (`lifecycle-plan-1.0`: `operation, request, server, connection_key, tool, target, commands[], checks[], warnings[], ready, binding`) come from `prepare_dump`, `prepare_restore`, `prepare_clone`; a table dump adds the check `tables`, failed (with `missing`) when a named table or view is not in the schema's catalog read (names match exactly, or without case only when `SELECT @@lower_case_table_names` reads 1 or 2, so a missing `orders` is never taken for an existing `Orders` on a case-sensitive server); endpoint `POST /api/lifecycle/prepare` with `operation` dump|restore|clone (reads only; the Shell's `dryRun` runs here). Dump dry runs use a private temporary destination, and restore dry runs use a private temporary progress file, both removed afterwards; they cannot create the requested output directory or modify the saved load progress before confirmation. A plan is never authority: `_write_job` rebuilds it on a fresh connection and refuses (nothing sent) unless binding, target and commands match, every check passed, the server identity is unchanged and the typed target equals `plan.target`.
- Output directories (`_output_path`, `_empty_directory`): parent links resolved once and bound in the plan (macOS `/tmp`, `/var`), the directory itself must not be a link, a parent swapped for a link after the preflight refuses; absent or empty only. Dump writes `myflames-manifest.json` (`lifecycle-manifest-1.0`: files with SHA-256, exact `COUNT(*)` of up to 3 tables under 1 MiB, the object catalog, `complete`); `read_manifest` re-hashes every file before a restore, capped by `MAX_RESTORE_FILES` (10,000), `MAX_RESTORE_BYTES` (10 GiB), and `MAX_RESTORE_DIRECTORIES` (10,000). Exceeding a limit refuses preflight with the limit named. Bytes are counted while reading as well as checked against file size, so growth during hashing also refuses. Successful logical dumps hash files once and reuse those records for verification and the manifest. Interrupted dumps skip further hashing and retain an incomplete manifest. Confirmed dump manifest creation is not capped by these restore preflight limits; larger backups require another restore tool.
- Restore: API and direct restore callers reject a mismatched typed target synchronously, before creating or registering a job or opening its connection; the API checks before saved-password lookup. The CLI checks before job creation. A mismatch says `Nothing was restored: the typed target does not match <target>.`, is audited as unsent, and never carries a partial-restore warning. Policy checks retain precedence, and all server identity and preflight rechecks still run for a matching target. Shell dumps through `util.loadDump` (`local_infile=ON` is a failed check, never set; progress file beside the dump; `--resume`; `--existing ignore|drop`), SQL dumps through the client with `--binary-mode --comments` reading a staged copy whose hash is checked again. Verification: object presence and the recorded row counts (`passed`, `failed`, `not_checked`; a successful child process is never evidence).
- CLONE (MySQL only): `clone_preflight` (engine, version series, plugin on both, BACKUP_ADMIN donor, CLONE_ADMIN recipient or BACKUP_ADMIN with DATA DIRECTORY, platform and character set variables, collations, plugins, `max_allowed_packet`, `clone_valid_donor_list` containing the donor address, different servers; DATA DIRECTORY absolute and not under `@@datadir` as text, existence `not_checked` without a recipient-host probe because the server refuses an existing or in-datadir path before copying). `donor_address` (`--donor-address`) is the donor as the recipient reaches it, used in the statement and the donor-list check. The statement goes on the client's stdin (`clone_statement(..., redact=False)`), shown and audited redacted. Clone plans add `expect` with `donor` and `recipient` objects (`endpoint`, `connection_key`, `type`, `read_only`), `donor_address` and `require_ssl`, all included in the binding. The workspace evaluates both saved profiles with `dbops.connection_safety`; a read-only donor is allowed. Before sending a remembered donor password, it verifies negotiated TLS on the recipient connection. Start checks the reviewed pair before retrieving the saved donor password, and rejects TLS downgrades when the donor profile requires TLS. Both passwords are scrubbed from errors, audit records, logs and manifest free text. Progress: a second connection polls `performance_schema.clone_progress` (job `progress` percent and `detail`). In place: ERROR 3707 (no supervisor, the server shuts down) or a lost connection is expected; the job waits up to `restart_wait` (`--restart-wait`, default `RESTART_WAIT` 180 s) and then reads `clone_status`, tied to this clone by `BEGIN_TIME`, `SOURCE` and `DESTINATION` (the table keeps the previous clone's row); result `needs_restart` when it does not come back, `needs_verification` when the recipient account is refused (it now has the donor's accounts). GTID: donor `gtid_executed` at preflight against `clone_status.GTID_EXECUTED` (empty for DATA DIRECTORY clones, `not_checked`).
- Jobs (`LifecycleJob`, `lifecycle-job-1.0`: `id, operation, state queued|running|done|failed|refused|cancelled, progress, detail, events[] (last 200), result {status, verification[], partial_state, cancel_error, audit_id}, error`): tool output drained on threads, CR progress parsed, ANSI colors stripped, secrets redacted. Cancel: SIGTERM then SIGKILL to the process group (EPERM from a zombie group tolerated), and `KILL QUERY` on the session the job printed on its own stdout (marker line; mysqlsh's `print` needs an explicit line break). `partial_state` names what may remain. `LifecycleJobs` (the workspace's registry: two running at most, never two on one target, finished ones kept an hour, closed with the server). Shutdown: `LifecycleJobs.shutdown()` runs first in `WorkspaceServer.server_close`, before replication loops, table data, live jobs and editor sessions (whose cleanup can block on `KILL QUERY`). It refuses new jobs and preflight runners, cancels every running job and runner, starts `KILL QUERY` on threads with the session id read before the tool stops, sends SIGTERM to every tool process group and SIGKILL after 1 s, then gives jobs 1.5 s to write their audit record (cancelled, outcome unknown, as for Cancel); `close()` finishes the rest. `ToolGroups` lists a tool's process group from just after it starts until the group is gone. Tools start through `ToolGroups.start`, serialized with shutdown (`close_spawning`): a tool being started is listed before shutdown's snapshot, and none starts afterwards. With `MYFLAMES_TOOL_GROUPS_FILE` (set only by the desktop app, in a 0700 directory it owns; a missing or shared directory refuses startup with exit 2) the list is also kept in an owner-only `tool-groups-1.0` file (`{"schema_version", "groups"}`), written before the launch line, replaced atomically and kept (empty) after a clean close. When it cannot be rewritten it is removed, so it never names a group that may have ended, and the desktop treats a missing file after launch as unconfirmed; a tool whose group cannot be recorded is stopped at once and reported as cancelled. Shell dryRun and upgrade check tools run under `LifecycleJobs.runner()`, so shutdown stops them too.
- Writes: dump, restore and clone are `risky` for `dbops.decide` (a read-only connection refuses, Production and Development confirm). CLI: policy from `--profile` before any prompt or connection; plan and commands on stderr; dump asks `[y/N]`, restore and clone ask to type the target; non-TTY needs `--yes` (plus `--confirm-target TARGET` for restore and clone); `--preflight` prints the plan (exit 0 ready, 1 not); Ctrl+C cancels the job. Exit 0 completed, 1 a started job failed, was cancelled or was not verified, 2 an unsent refusal, decline, bad input or connection failure. Failed-job text names the reason once in the operation heading, omits duplicate or empty `not_checked operation` rows and repeated status words, and retains distinct verification and partial-state guidance; JSON evidence is unchanged. UI: `POST /api/lifecycle/start` (`operation`, `plan`, `typed_target`, `confirmed: true`, `connection`, and `donor` for clone) is in `ui._WRITE_PATHS`: `dbops.gate` first (refused envelope with `audit_id`), then saved passwords (`connection.profile_id`, `donor.profile_id`), then the job; `POST /api/lifecycle/status` and `/api/lifecycle/cancel` take `id` and `connection`. Every attempt has an `AuditTrail` record (`surface` cli or ui; refused, declined, `unknown` after a cancel, `ok` or `error`). `POST /api/lifecycle/jobs` (`connection`; `lifecycle-jobs-1.0`, `_lifecycle_jobs_response`) lists the jobs this workspace holds for the connection's endpoint (`dbops.identity_endpoint`, recorded by `_lifecycle_add` before the job starts and dropped when `LifecycleJobs` lets the job go); it opens no connection and starts nothing (`test/test_lifecycle_cli.py`). Status and cancel check this same owner before looking up or cancelling a job and return “No such job.” for missing, expired or mismatched jobs. They open no connection and read no saved password. A change to type or read-only preserves access to the original job.
- Workspace UI (`Maintenance.tsx`, Server > Setup): Tools, Upgrade check (MySQL Shell, or the labeled partial check), Dump, Restore, Clone; Run preflight only reads and opens the confirmation only when `ready` (target, exact commands, warnings, checks; restore and clone type the target; stale after `PREFLIGHT_TTL_MS`, 60 s); Cancel and Escape call `audit/declined`. Frontend callers must send the original `connection` on every status and cancel request, and show both `plan.expect.donor.endpoint` and `plan.expect.recipient.endpoint` with their policy when confirming clone. Jobs are polled while they run and bound to their connection (greyed after a switch). On open, and after a Start whose reply failed, the view reads `lifecycle/jobs` and adopts what it finds, so a reload or a lost reply never sends Start twice. The session keeps only `slow.maintenance_section`; the donor password stays in the page. Browser suite `apps/workspace/test/maintenance.mjs`.
- Not wired to the CLI or UI yet: `prepare_physical_backup` / `physical_backup_job` (MariaDB `mariadb-backup --backup` then `--prepare`, only with a matching local socket and an explicit same-host confirmation); `clone` on MariaDB prints the `mariadb-backup` steps instead.

### Replication: status, skip, skip-loop

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
- Saved passwords are bound to the exact `ENDPOINT_FIELDS` (`host`, `port`, `user`, `ssl_mode`, `ssl_ca`): a profile saved without `ssl_mode` means the client's own default (not PREFERRED for every client, for example MariaDB 11.4+ verifies by default), so a request that names PREFERRED is refused until the profile is saved with it (`test_saved_password_binds_to_the_exact_tls_mode`). `scripts/ga-matrix.sh profiles` saves `ssl_mode` PREFERRED for that reason; an endpoint mismatch also shows the connection as Production in the picker (`connectionSafety`).
- Endpoints: `POST /api/live/replication` (status; `_CONNECTION_PATHS`, saved-password helper), `POST /api/live/replication/skip` (`channel`, `expect`, `statements`, `confirmed: true`; `_WRITE_PATHS`: policy, then the saved password, then `skip_one`), `POST /api/live/replication/skip-loop/start` (`channel`, `codes`, `max_skips`, `max_minutes`, `poll_s`, `confirmed: true`; same order; the loop keeps its own connection and closes it when it stops), `.../skip-loop/status` (`job_id` or every loop of this connection) and `.../skip-loop/stop` (`job_id`). Loops live in `workspace.replication_loops` (`SkipLoopRegistry`) keyed by `dbops.connection_identity` and matched by its endpoint (`dbops.identity_endpoint`: saved connection id, host, port, user, TLS), so status and stop from another connection are refused while a change of the connection's type or read-only flag keeps them (one loop per channel and endpoint); status and stop open no connection. A refused or unconfirmed request is audited.
- Workspace UI (`ReplicationPlace.tsx` and `Replication.tsx`, the standalone Replication place): the channel table and inspector; Skip this transaction and Skip until stopped re-read the failure before their sheets open, show the tagged statements, plan again (nothing sent) when the identity or statements changed or the sheet is older than `PLAN_TTL_MS` (60 s), and call `audit/declined` on Cancel and Escape. Loop status is read once a replica report is shown, while a loop runs, and after Start or Stop; a running loop's line (with Stop) also sits above other Server views. The session keeps only `slow.replication_loop_active`, so a restored workspace mounts Replication, reads loop status, and says "Stopped when myflames restarted" when the registry is empty; it never starts a loop. Both views take `missingPrivileges` (`opsApi.MissingPrivileges`, "Requires CLONE_ADMIN") to grey an action with its reason once a privilege context passes it; absent means unknown and the server decides. Browser suite `apps/workspace/test/replication.mjs`.

### Cluster: Galera, Percona XtraDB Cluster, InnoDB Cluster

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
- Workspace (`ClusterPlace.tsx`, `ClusterTopology.tsx`, `clusterApi.ts`, `cluster.css`): a place of its own after Replication. It reads when opened on screen (`useOpenRead`, once per connection; Refresh reads again) and Server's Refresh all reads it too (`refreshTasks`, view id `cluster`, label Cluster). Toolbar with Refresh, the ⓘ (the severity scale, and that changes ask first and are audited) and the Segmented sections Topology, Members (Nodes on Galera and PXC) and, for InnoDB Cluster, Routers, which stay in the title row while they fit; the summary line (its OK has the same shape and word as Watch and Problem, and stays beside the text at 720 px); when the status is not OK the findings come first, one compact line each (the state's shape and word, the title in foreground text with only the member name linked, the reason on demand stacked under it; Details stay open by the finding's identity, `findingKeys`: its id and member, never its position, and `Findings` remounts per connection, so nothing opens on another finding after a refresh or on another cluster), then four tiles (Component or Quorum with failures tolerated, watch when it tolerates 0 and problem without quorum, "Not visible" with "Failures tolerated unknown" and the reason in its title on a read replica or a member out of the group; Nodes "3 in component · 1 inspected here" or Members "2 of 3 ONLINE", "Not visible" with "3 in metadata" when this member left the group or on a read replica, with the worst member's tone; This node or This member with the role as a hint; Flow control paused or Mode). Cards, the Members table, the inspector and findings share the status dot plus a word (`ClusterMark`); navigation uses a compact `NavMarker` with a neutral count or the state in its tooltip and accessible name. Topology is a static picture: member cards (role badge, name, the address when it differs from the name, engine and version (PXC as "PXC 8.4.11-11.1", the full name on hover; nothing for another Galera node), the state, queues or flow control as text, and last the "This connection" chip, so every card's lines line up) on stems to one group line labelled with the component or the group mode, quorum and failures tolerated; a configured Galera node not in the component as a dashed placeholder; read replicas below the group on dashed neutral links ("Read replicas · asynchronous, not group members"); routers under them. A member that is UNREACHABLE, MISSING, OFFLINE, in ERROR or judged a problem has a dashed edge in its tone, a cut stem and its shape; a watch member has a heavier left edge; nothing animates. At a view under 640 px the cards wrap in a grid inside a frame that stands for the group (dashed without quorum), and the tiles are shorter, so the first tab's content starts above the fold at 720×520. Members (`cluster.members` on Group Replication and InnoDB Cluster, `cluster.nodes` on Galera and PXC, whose columns differ; the name column starting at 150 px so nine columns fit at 1440 px) and Routers (`cluster.routers`) are DataTables, each its own instance (`key`) with its own column layout: the shell owns one record of layouts by table id (`main.tsx` `columnLayouts` through `useColumnStore`, given to Server as `columns`/`onColumns` and to the Cluster place through `ColumnStoreContext`) and saves it as `slow.columns_v1` (`TABLE_IDS` in `session.ts`), so a layout survives Topology, a restart and Server not being mounted; the inspector shows a member's state and where it was read (and the raw `pxc_cluster_view` STATUS), its counters (flow control as a percent and its fraction), and this node's Galera settings, or a router's ports and check-in (age and UTC, and a note when it is this member's frozen copy). Terms carry short glossary tooltips (Primary component, flow control, failures tolerated, queues, router ports). A report of another connection (`otherConnection`), or the last report after a failed read of this one, by the place's Refresh or by Refresh all (the shell keeps the failure with the report it failed over, `ClusterFailure`, until a read through either path succeeds; `is-stale`, with "Last read 14:40 (5 min ago)"), stays greyed and marks its member "Read here" (title: where and when it was read), never "This connection"; a failed read shows one short sentence (`failureText`: connection refused, lost, timed out, access denied), never the client's dump, and without a report a "Could not read the cluster" state with Retry instead of the first-run Load. The shell derives the sidebar item from the shared report of the connection in use (`clusterNav`): nothing before the first read; `NavMarker compact` with a status dot and neutral count for problem members, and the state word in the tooltip and accessible name; for `kind: "none"` the item gets `is-muted` (faint text and icon), `data-muted`, the tooltip and hidden text "This connection is not part of a cluster", stays focusable and clickable, and keeps the disabled look while open (a neutral gray fill, never the accent). The palette lists Cluster among the places. Browser suite `apps/workspace/test/cluster.mjs` (reports from `apps/workspace/test/cluster-fixtures.py`).
- Tests: `test/test_cluster.py` replays `test/fixtures/cluster/cluster-*.json` (live recordings: healthy, donor, a node down, an isolated node, a member with Group Replication stopped read from the primary and from itself, an UNREACHABLE member; the recorded `v2_routers` answers carry the `UNIX_TIMESTAMP` column derived from the recorded check-in, the lab servers running in UTC) plus states derived from them (another PXC node Joiner or Joining, the address list as IPs, IP:port, FQDNs, one seed or a DNS service name, two nodes, wsrep_on OFF, a joiner, Galera Cluster for MySQL) and synthetic servers (a read replica connected to, an IPv6 group) in `test/_cluster_fixtures.py`, and checks the schema, read-only SQL, the CLI exit codes, the API and the MCP tool; `test/test_report.py` checks that checks.wsrep and the Cluster place give the same status on the Galera and PXC recordings.

#### Cluster changes

Reviewed changes in a member's inspector (`cluster.available_actions(report, member_key, checks)`, `action_checks`, `plan_action`, `execute_action`, `with_actions`; the send half is `reviewed_writes.execute`; sources and line references in the "reviewed actions" comment in `cluster.py`):

| Action | Statement (tagged `/*myflames:cluster_op*/`) | Offered when | Typed name |
| --- | --- | --- | --- |
| `switchover` Make primary… | `SELECT group_replication_set_as_primary('<MEMBER_ID>')` | Group Replication or InnoDB Cluster on MySQL 8.0.13+, single-primary, the member SECONDARY and ONLINE with a UUID MEMBER_ID, the connected member ONLINE with quorum, no member RECOVERING or UNREACHABLE | no |
| `stop_member` Stop Group Replication… | `STOP GROUP_REPLICATION;` | the connected member, ONLINE, RECOVERING or ERROR | when it is the primary or the last ONLINE member |
| `start_member` Start Group Replication… | `START GROUP_REPLICATION;` | the connected member, OFFLINE, and `@@global.group_replication_bootstrap_group` OFF (read at plan and send time; ON or unreadable refuses) | no |
| `switch_to_multi_primary`, `switch_to_single_primary` | `SELECT group_replication_switch_to_multi_primary_mode();` / `..._single_primary_mode();` | plain Group Replication only (not InnoDB Cluster), on the connected member, group ready as for switchover | the connected member's name |
| `desync_node` Desync this node… | `SET GLOBAL wsrep_desync = ON;` | MariaDB Galera, the connected node, `wsrep_on` ON, `wsrep_desync` OFF and Synced | no |
| `resync_node` Resync this node… | `SET GLOBAL wsrep_desync = OFF;` | MariaDB Galera, the connected node, `wsrep_desync` ON | no |
| `bootstrap_pc` Bootstrap a Primary component… | `SET GLOBAL wsrep_provider_options = 'pc.bootstrap=YES';` | MariaDB Galera, the connected node, `wsrep_on` ON, component `non-Primary`, `wsrep_connected` ON, not joining; `expect` also binds the component view (cluster UUID, `wsrep_cluster_conf_id`, size, members), so a node joining or leaving needs another review | the node's name |

Never offered: `SET GLOBAL wsrep_on` (sessions opened later stop replicating their writes: silent divergence), anything on Percona XtraDB Cluster or Galera Cluster for MySQL (sources not checked), InnoDB Cluster mode switches (MySQL Shell's commands also set `auto_increment_*` and the metadata), and START on a member in ERROR (the plugin still runs; START fails). `expect` binds the server identity (engine, version, `server`), the kind, the member and the connected member (key, UUID, name, role, state), component, quorum and topology, the group's members and states (Group Replication), `wsrep_desync`, `wsrep_on` and the local state (Galera), `wsrep_last_committed` (bootstrap) and `bootstrap_group` (start); any change refuses with nothing sent. Endpoints: `POST /api/live/cluster/plan` (`action`, `member_key`; `_CONNECTION_PATHS`, saved password like the views; returns the plan with `policy` and `review_token`) and `POST /api/live/cluster/action` (`plan` as reviewed without its token, `review_token`, `typed`, `confirmed: true`; `_WRITE_PATHS`), audited as `cluster_action`, envelope `cluster-action-1.0` (below). The send waits up to 120 s (START and the UDFs wait for the group). The workspace's `POST /api/live/cluster` returns `with_actions(read_status(conn))`: each member's `actions` ({id, label, danger, description}; optional in `docs/schemas/cluster-1.0.json`); `myflames cluster` and `cluster_status` do not carry them. Tests: `test/test_cluster_actions.py` (what each recorded and derived state offers, exact statements, refusals, typed names, `bootstrap_group`, endpoints with the audit trail, drift between review and send, unknown and server refusal), and `apps/workspace/test/cluster.mjs` (the inspector's buttons, the review with the UUID, Escape recorded as declined, another connection's report offering nothing).

### ProxySQL

After a reviewed action completes (including an unknown outcome), the workspace
stops pending digest reads and clears cached routing before refreshing the main
report. A separate digest revision triggers a fresh read when Query digests is
visible even if its cache was already empty; hidden tabs defer that read. The
read hook rejects late replies from the cancelled request. This follow-up only
reads; the reviewed action's result and no-retry rule remain in effect.

Digest identity and completion ownership: keys are `dg` plus compact JSON of the primary-key tuple `[hostgroup, schemaname, username, client_address, digest]`, with names preserved and no delimiter splitting in the workspace. Digest hash filters normalize to `0x` plus uppercase hex before the hex-literal SQL comparison. A rule walk that needs an unread or invalid runtime variable returns `unknown` (Not judged), naming the variable; an unread restart budget must never become a known zero. `ActionReview` rechecks the captured connection scope when finishing every send, including refused or lost replies: a switch away, even followed by a switch back, drops the completed sheet and its follow-up actions. The server-side audit record is retained.

The ProxySQL place reads a ProxySQL Admin interface (`myflames/proxysql.py`, `docs/schemas/proxysql-1.0.json`; checked against the ProxySQL source at 6ef036a, v2.0.0 and v2.7.3 for the identity, and live on ProxySQL 3.0.11 in front of the MariaDB Galera and MySQL InnoDB Cluster lab, `scripts/cluster-matrix.sh up proxysql`).

- Identity (`identify`): `SELECT variable_value FROM global_variables WHERE variable_name = 'admin-version'` returns the version and `SELECT @@version` (Admin answers it itself, Admin_Handler.cpp) returns the same text. A server SQL error means not ProxySQL (`available: false`, `is_proxysql: false`, a message naming port 6032); a client error (2000 to 2999), a refused login or no answer raises, like the other views. Never from a `mysql_servers` table alone.
- Reads, all `SELECT`, each table with `SELECT *` read by column name from the client's XML output (`--xml`, `perf_common.named_rows`): SQL NULL is `xsi:nil` (Admin sends SQLite NULL as a protocol NULL, MySQL_Session.cpp `SQLite3_to_MySQL`) and the text 'NULL' is the field's content, which batch output prints alike; output that is not well-formed XML (a control character in a value) leaves that table unread. Tables: `stats_mysql_global` (uptime and a fixed list of counters), `main.`, `main.runtime_` and `disk.` copies of every table of the servers module (`mysql_servers`, `mysql_replication_hostgroups`, `mysql_group_replication_hostgroups`, `mysql_galera_hostgroups`, `mysql_aws_aurora_hostgroups`, `mysql_hostgroup_attributes`, `mysql_servers_ssl_params`) and the query rules module (`mysql_query_rules`, `mysql_query_rules_fast_routing`) (ProxySQL_Admin.cpp `mysql_servers_tablenames`, `mysql_query_rules_tablenames`), `stats_mysql_connection_pool` (`Bytes_data_sent`, `Bytes_data_recv`, `Latency_us`, doc/Stats_Tables.md), `stats_mysql_query_rules` and `stats_mysql_query_digest ORDER BY sum_time DESC LIMIT 100`. A table Admin refuses (absent in this version, or a stats-only account) is noted and its part is unknown (`null`); it counts as absent only when the main, runtime and disk reads all answer `no such table` (`ProxySQL Admin Error: no such table: main.x`, live on 3.0.11). A column absent in a version is absent from the row. `config_readable` is false without `main.mysql_servers` and `main.mysql_query_rules`, and then nothing is offered.
- Servers: one row per runtime row (`rt|hg|host|port`), with the pool's status word (SHUNNED_REPLICATION_LAG kept), its settings, pool numbers, its monitor and role (writer, backup writer, reader, offline), and its configuration row: the main row with the same hostgroup, host and port, else the one main row with that host and port among the same active monitor's hostgroups (several or none: `config_note`, no change). A configuration row with no runtime row is listed as `cfg|...` (OFFLINE_HARD: ProxySQL drops it from runtime). Tone: ONLINE ok; SHUNNED in a monitor's writer hostgroup while ONLINE in its backup writer hostgroup is a standby writer (ok; Galera keeps writers above `max_writers` there, seen live); other SHUNNED, OFFLINE_SOFT, OFFLINE_HARD and the offline hostgroup watch. Hostgroups: per runtime hostgroup the counts by status, standby writers, queries and connections, and the monitor role; a hostgroup with servers and none ONLINE (other than an offline hostgroup) is a problem. An active rule in `runtime_mysql_query_rules` (what ProxySQL runs, never the configuration in memory, which may not be loaded; a rule deleted or disabled in memory still routes until the LOAD) whose destination hostgroup has no runtime server is a problem. When the runtime rules, or both `runtime_mysql_servers` and the pool, cannot be read, routing is unknown: the status is at least `unknown`, with the reason. Connection errors are cumulative and never judged.
- Pending (`pending.servers|query_rules.runtime|disk`): lines naming each row that differs between memory and runtime or disk (`null`, with a note, when any table of the module cannot be read in one of the two copies; only a secondary table absent from every copy is passed over). Difference lines quote the text 'NULL' and the empty string, so neither reads like SQL NULL. Memory versus runtime for `mysql_servers` allows for what ProxySQL sets itself: a server in a monitor's hostgroups is compared by host and port over all its runtime rows in those hostgroups, settings only, with SHUNNED read as ONLINE, an OFFLINE_HARD row absent from runtime not pending, and OFFLINE_SOFT under the Galera monitor not pending (the monitor puts it back). Every other table is compared row by row by primary key.
- Routing, evaluated here (`myflames/proxysql_digests.py` `Routing`, checked against lib/Query_Processor.cpp `process_query` and `rule_matches_query` at 6ef036a): the report also reads `runtime_global_variables` (`mysql-query_processor_regex`, `mysql-query_processor_iterations`, `mysql-query_digests`, `mysql-query_digests_track_hostname`) and `runtime_mysql_users` by named columns (`username, active, default_hostgroup, transaction_persistent, frontend`; never the password), and evaluates each digest against the active runtime rules in rule_id order (ProxySQL_Admin.cpp loads `WHERE active=1 ORDER BY rule_id`): flagIN must equal the current flagIN (0 first), username and schemaname exact when set, client_addr against the digest's client address (unknown when `mysql-query_digests_track_hostname` is off or the rule has a `%`), proxy_addr and proxy_port unknown (a digest does not record them), digest as a number (`strtoull` base 0), match_digest on the digest text, match_pattern on the digest text marked approximate (it tests the query text), `negate_match_pattern` turning either regex result around (`rule_matches_regex`), CASELESS in `re_modifiers` ignoring case, a pattern Python's `re` cannot compile unknown. A match sets the destination when it is 0 or more and flagIN from flagOUT (or from `attributes` `flagOUTs`, a random pick by weight that a set flagOUT replaces; MySQL_Query_Processor.cpp `new_query_rule`); apply stops; `mysql-query_processor_iterations` restarts the walk after a flagOUT (followed here up to 10 restarts, `ITERATION_LIMIT`). Without apply, `mysql_query_rules_fast_routing` decides: (username, schemaname, flagIN), then the entry with an empty username for that schema and flagIN (`search_rules_fast_routing_dest_hg`); without a destination, the frontend user's `default_hostgroup`. The walk branches (`_Path`, at most 64, `BRANCH_LIMIT`): the first path takes each uncertain test at its best guess, the others flip one uncertain rule that would change something or take another flagOUT weight; a rule's `next_query_flagIN` adds walks that start from that flagIN (the session's following queries do). Patterns never run in the workspace process: `test_patterns` sends every pattern and digest text of a read to `regex_worker` in a child process (`python -I -c` from a checkout; the packaged backend with `__myflames_regex_worker`, handled in apps/desktop/scripts/backend-entry.py), stdin written by its own thread, a `ready` line within 10 s, then one line per pattern within 2 s each (`PATTERN_TIMEOUT`): a pattern that takes longer is unknown ("took too long") and a new worker goes on after it (at most three times); the child is always killed and reaped. A worker that cannot start leaves every pattern unknown with a note. `routing` per digest: `{observed, expected, source rule|fast_routing|default, rules, decisive, approximate, possible, unknown_rules, default_hostgroup, verdict, text, why}`; verdict `match`, `mismatch` (with the reasons: a transaction kept on one hostgroup when `transaction_persistent` is 1, a `/* hostgroup=N */` comment, a connection locked to a hostgroup, rules loaded later), `possible` (another path, from a flipped uncertain rule or a `next_query_flagIN`, ends on the observed hostgroup), `local` (statements MySQL_Session.cpp `handler_special_queries` answers itself, recorded on the default hostgroup: `select @@version_comment limit ?` and `select USER()` exactly, `SELECT $$` ignoring case; seen live on 3.0.11; and `SELECT @@version` or `SELECT VERSION()` only when `mysql-select_version_forwarding` is 0, 2 or 3, 3 answering when a backend's version is known; 1 forwards and 2.x has no such variable), `cache` (hostgroup -1, MySQL_Session.cpp sets it on a query cache hit), `unknown` (also when the walk left work out, a restart or a flagOUT choice past the limits, or fast routing could not be read and no apply stopped the walk: never a mismatch then). A flagOUT weight that reaches the observed hostgroup is a match. More than 500 active rules (`RULE_LIMIT`) or unread runtime rules: not evaluated (`routing.evaluated` false, with a note). The report's rules get `digests` and `digest_count` (the digests whose chain includes the rule, among the report's 100) and `possible_digests`; `report.routing` says how it was evaluated.
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

A row change refuses while the module has other changes in memory (LOAD would apply them too) and names them; the result offers Save to disk as the next, separately reviewed change (`next`). Host names must match `[A-Za-z0-9._:%/[]-]{1,255}` (no quote or backslash; quoted SQLite-style) or the row offers nothing. LOAD and SAVE are sent untagged because Admin compares the whole command text (Admin_Handler.cpp `is_admin_command_or_alias`; a leading comment gave "near LOAD: syntax error" live); the UPDATE goes to SQLite, which keeps the tag. `expect` binds the ProxySQL version, the configuration row or rule as read, the pending lists, and `fingerprints` (SHA-256 of every row) of the module copies the statements depend on: memory for any change, plus runtime query rules for rule changes and query rules loads and disk for saves (never runtime servers, which the pool and the monitors change); an added or removed row is described with its content. A table unread in a copy for any reason (one copy failed, a refusal other than `no such table`, a lost connection) makes that module's comparison unknown (`null`), so that module offers no change (`available_actions`; Save also needs memory versus disk) and a plan for one refuses; only a secondary table every copy reports as `no such table` is passed over. The fingerprints hash SQL NULL as JSON null, apart from the text 'NULL'. Endpoints: `POST /api/live/proxysql` (`_SERVER_VIEW_PATHS`, query timeout 30 s), `POST /api/live/proxysql/plan` (`action`, `target` `{config: [hg, host, port]}` or `{rule_id}` or null, `value`) and `POST /api/live/proxysql/action` (as for Cluster), audited as `proxysql_action`, send timeout 60 s. Workspace (`ProxySqlPlace.tsx`, `proxysqlApi.ts`, `proxysql.css`; the report is owned by the shell so the sidebar can mute the item for the connection that was read, `proxySqlNav`): tabs Servers, Hostgroups, Query rules, Query digests (tables `proxysql.servers`, `.hostgroups`, `.rules`, `.digests` keep their column layout), the Configuration line with Load and Save on Servers and Query rules, an inspector per row with its changes in the action bar. Query digests reads with its own `useConnectionReads` only while the tab is on screen and the report is the connection in use's (`proxysql-digests:<filters>`, so a newer choice drops an older read); its tools row has the order (a Segmented control, or a menu below 1000 px), Top, Hostgroup, Schema, User and a search that reads after a 450 ms pause; a digest or rule filter is a chip; tiles are hidden on that tab. The place holds the filters and the moves between tabs: a rule from a digest (Query rules, selected), a digest from a rule (Query digests filtered by its digest, selected; shown from the report while its read runs), a rule's digests (the `rule_id` filter). Rules show Hits with **No hits** and Digests (`digest_count`, "+N maybe"). Tests: `test/test_proxysql.py` (FakeAdmin: ProxySQL Admin over SQLite with LOAD and SAVE copying tables, Admin's whole-text match, the client's `--xml` output and its "at line N", the lab's rules with `re_modifiers` CASELESS, its ten digests, `runtime_global_variables` and `runtime_mysql_users`), `test/test_proxysql_digests.py` (routing cases, the report's cross-check, every order and limit bounded in Admin, hex filters and the reset table never named, `more`, the rule scan, a 2.0.6-shaped table without `sum_rows_sent`, input validation, the endpoints, progress), `apps/workspace/test/proxysql.mjs` over fixtures written by `apps/workspace/test/proxysql-fixtures.py`.

#### Reviewed changes planned from a fresh read (Cluster and ProxySQL)

`myflames/reviewed_writes.py` and `apps/workspace/src/ActionReview.tsx`. Plan: the module reads and plans (exact statements, `expect`, `typed_guard`, `facts`, `warnings`, `undo`, `changes`, `next`); `ui._action_response` adds `policy` from `dbops.decide(worst_kind(statements))` (every statement is risky, so Production and Development both confirm and Read-only answers `refuse`) and a review token over the plan without `review_token` and `policy` (`reviewed_writes.review_details`). Action: `check_reviewed` (a dict with an action and 1 to 10 single-line statements), the policy gate before any connection (refused and audited: `read_only`, `needs_confirmation`), the token consumer (missing, expired, used or changed: refused and audited as `review`), the saved password, then `execute`: plan again from a fresh read (not offered any more: refused `changed`), compare statements and `expect` (refused `changed`), the typed guard (refused `typed_guard`), read the saved policy again (`ui._action_response` `before_send`: a profile made Read-only or another type since the request refuses as `policy_changed`), consume the token, write the pending audit record (failure sends nothing), one `run_write` with the statements joined by newlines (`--comments --binary-mode`, stdin, no reconnect; batch mode stops at the first error), then: `done` with `after` (the state read again) and `next`. Only an error the client printed with its number decides a failure: `failed` when a server error (any number but the client's 2000 to 2999) names line 1, or when the client printed an error with no line, before it read any statement (it could not connect or log in; audit outcome `not_connected`); `partial` when a server error names line N > 1 (`sent`, `not_sent`). The mysql and MariaDB clients add "at line N" once they have read a statement (client/mysql.cc `put_info`). Everything else is `unknown` (audit `unknown`, outcome `timeout` or `no_reply`; never retried): a time limit, an interrupt, a client error at a line (2013, 2006), a line past the last statement, and a client that ended without printing an error number (killed or crashed after the server may have run every statement). Envelope `proxysql-action-1.0` / `cluster-action-1.0`: `{schema_version, action, status, message, statements, sent, not_sent, error, after, undo, next, audit_id, policy, states, effect, checks}`: `states` is each statement's final state (`done`, `failed`, `not_run`, `not_sent`, `unknown`; `done` only for statements the server answered), `effect` the rows `[what, before, now]` the module builds from the read before the send (captured in its fresh plan) and the read after (`proxysql._effect`: the changed field of the configuration row, Runtime, the rule in memory and runtime, memory versus runtime and disk; `cluster._effect`: Primary, the member's state, `wsrep_desync` and state, Component or Group mode), null when the read after failed or the outcome is not done; `checks` comes from the plan (`proxysql._checks`, `cluster._checks`: what to refresh and look at, and that the Audit log keeps it as Outcome unknown). Plans also carry `timeout_s` (60 for ProxySQL, 120 for Cluster). Progress (`reviewed_writes.Progress`, kept by `ProgressRegistry` on the workspace: at most 64, ten minutes after their last change): the action request may carry `progress_id` (16 to 64 of `[A-Za-z0-9_-]`; missing, invalid or reused means only that no progress is kept), and `POST /api/live/action/progress {progress_id}` (no connection, not in `_CONNECTION_PATHS`) returns `action-progress-1.0` `{phase received|checking|sending|reading|finished|unknown, states, status, elapsed_s, waiting_s, timeout_s}`: received (policy gate and token), checking (the fresh read and plan), sending (the pending audit record is written and every statement is `sent`: with the server, it may run), reading (the server answered; final states) and finished; a refusal finishes with `not_sent`, an exception after sending with `unknown`. No SQL, no credentials. The sheet: eyebrow (statement verb), question title, the description, the target, Connection (label with address), Type, the facts, Undo, the policy text, warnings, the statements exactly as sent, the differences (`changes`), a typed name field when needed; Cancel and Escape call `recordDeclined` (`proxysql_action` or `cluster_action`, the statements, the connection, the action, target and `expect`); a confirm the workspace stops itself (the saved connections cannot be read, or name another policy) posts the same to `/api/audit/declined` with `reason` `policy_unknown` or `policy_changed`, recorded as refused; a connection change closes and audits an open sheet; confirm re-reads the saved connections (`useWritePolicy().check()`) and refuses locally when the key changed; a reply below 500 means nothing was sent unless the last progress read was sending or reading; no reply or a 500 is Outcome unknown, never retried. After Confirm the same sheet stays (no second dialog): `confirm()` makes a 128-bit hex `progress_id`, polls the progress every 400 ms while the request runs, and the sheet shows the phase line (the seconds waited counted from the first sending snapshot, with `timeout_s`), each statement with its state (a spinner on Sent), the confirm button reading Sending… with `aria-busy` and duplicate clicks ignored. After ten seconds without a reply, Close and Escape can hide the sheet (`onLeave`) while the request continues; a pending line in the view retains its start time and connection label. A late reply replaces it with the result panel without reopening the sheet or resending the statements. On the answer: the outcome pill and message, the statements' final states, What changed (`effect`), What to check (`checks`, for unknown), the undo for partial or unknown, the Audit log line with Open the Audit log (the `myflames-open-audit` event with `audit_id`; `main.tsx` opens the Audit log with that record selected, `AuditLog focusId`), and ConfirmSheet's `doneLabel` mode: the next change (Save … to disk…) as secondary and Close focused, no Cancel; Close records nothing. The result panel in the view keeps the result (with an Audit log button). A planning error or a Read-only plan shows in the view and opens no sheet. Values (weight, hostgroup) are asked first in a small sheet with bounds and the current value; nothing is planned until Review.

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
empty user name and an explicit host. Server/component availability remains
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

- **MCP server** (agent tools): `myflames-mcp` (optional extra: `pip install myflames[mcp]`, constrained to `mcp>=1.2,<2` for the verified `mcp.server.fastmcp.FastMCP` API). MCP 2.x is not supported by this transport. Import failures report the installed SDK version (or not installed; unknown when version metadata is unavailable on Python 3.7), the original import error and the install command. Tool imports stay lazy and the core needs no SDK. Exposes `analyze_plan`, `digest_plan`, `compare_plans`, `explain_optimizer_switch`, `explain_query` (one SELECT only, checked by `live.bind_query` before any connection; `MySQLConnection.explain_analyze` sends `SET SESSION TRANSACTION READ ONLY; START TRANSACTION READ ONLY;`, the EXPLAIN ANALYZE (MariaDB `ANALYZE`) and `ROLLBACK` in one client session like capture, the client stopping at the first error so the query never runs outside the transaction; the transaction refuses writes to ordinary tables, not changes to existing temporary tables, GET_LOCK or variables set inside a stored function). Tool logic in `myflames/mcp_server.py` is stdlib + tested; only the transport needs the extra. Register: `claude mcp add myflames -- myflames-mcp`. `analyze_plan` defaults to `detail="core"` (token-cheap decision-grade sidecar); `detail="full"` restores the complete record (adds `collected`/`query`/`teach_hooks`). The `detail` gate lives on `build_sidecar` in `output_sidecar.py` (single source of truth, shared with the CLI `--sidecar` path, which stays `full`); the digest path (`digest_plan_tool`) also stays `full`.

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

Core projections reuse `report.format_digest` and the compact text formatters in Sessions, Locks, Health, InnoDB, Replication and Cluster. Statements retain normalized SQL, counters, coverage, and table-match caveats while omitting sample literals; Privileges retains credential-stripped grants, session scope, roles and grouped decision states. Reports can exceed 500 tokens: their size follows the observations and findings, without a fixed truncation cap. Full preserves the module's schema and evidence (including suggested SQL as data).

`_ReadConnection` checks every submitted statement, allowing only SELECT/SHOW. The Sessions and InnoDB readers normally prefix transaction-age queries with `SET SESSION time_zone = 'SYSTEM'`; this adapter removes those exact module-owned prefixes and converts `NOW()` with `CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', 'SYSTEM')` inside the SELECT instead. It rejects any other SET or non-read statement before sending it. The existing five MCP tools are unchanged, including `explain_query`, which executes a query through EXPLAIN ANALYZE.

Tests: `HOME=$(mktemp -d) MYFLAMES_SECRET_BACKEND=vault python3 -m unittest discover -s test -p 'test_mcp_server.py' -v`. Replays and module-test fakes cover MySQL and MariaDB, both detail projections, attribution orders, registered tool identities, all submitted SQL, the no-SET adapter, argument validation and password redaction. The transport extra is not required for those tests.

The workspace wraps non-Visual Explain SVGs in a focusable `.chart-scroll` region. Below 650 px, it preserves the 1200-unit canvas width and scrolls within the view instead of shrinking text. Desktop layout and renderer semantics are unchanged. `browser.mjs` verifies readable scale, scroll range and no page overflow at 390 and 320 pixels.

### Playground

`docs/playground/index.html`: client-side Pyodide page (`python3 -m http.server -d docs/playground`).
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

`sqlCompletion.ts` supplies bounded lexical context and suggestions;
`useSqlCompletion.ts` reads the existing catalog routes. The metadata cache is
in-memory and identity-scoped, including configured database and credential
changes. It is not a model of the persistent editor session. Keep native
textarea input, undo, IME and Run shortcuts intact.

`table_data.py` owns dedicated editor sessions and private, opaque snapshots.
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

## Workspace navigation and configuration controls

Automatic first reads use `useOpenRead` and the report's connection key. Opening a view without a report for the connection in use reads once; a failed attempt waits for Refresh. Switching connections while the view stays on screen keeps its old report greyed until Refresh or a later reopening. Parameters, Instruments and Users and roles follow this rule too. Health, InnoDB, Locks, Processlist, Waits, I/O, Indexes, Tables and Replication retain their single-flight and stale-response guards. Every `useConnectionReads` request forwards its AbortSignal to `api()`. Tables and Indexes retain schema-size consent and pass visibility to `useSchemaCatalog(connection, enabled)`, including when compact navigation hides their detail. Explicit Refresh and Refresh all remain independent of these automatic-read gates.

Query Digest stores its report as `Bound<DigestReport>` through `useConnectionReads`, keyed by `connectionKey` (profile, endpoint, user, TLS and write policy). Editor and Explain callbacks carry that report key to `QueryLab.openDigest`; stale actions are disabled and rechecked at dispatch. Late responses cannot replace a newer connection's report. Exports name the report's original connection and filter. Their statistics cover only the loaded digests: sums and execution-weighted means, plus the maximum of collected execution maxima. Execution minima, row/lock extrema and percentiles remain unknown; digest averages and per-digest totals are not individual execution samples.

Processlist **Kill sessions…** opens the Kill sessions dialog (`MassKillDialog.tsx`; `/api/live/kill/bulk/prepare` then `/api/live/kill/bulk`, `kill-bulk-1.0`, whose `message` reads "Kill sessions finished: N killed, …"). Its **Lock waiters** preset requires a recorded wait (`lock_wait` or `lock_status: waiting`). A session that only holds locks (`is_locked` or `lock_status: holding`) is excluded. Exact bulk review, policy checks, target verification and audit remain required.

Home reads the installed version from the server-injected version meta tag. Its release link does not perform an update check or install software; only **Check for updates** asks GitHub (see Application updates above). Its News section is described under [Local UI](#local-ui) (Home news). Stable internal place IDs remain unchanged; Query editor and Plans are grouped under SQL workspace.

Refresh all schedules unopened sections as well as loaded ones, with three concurrent reads and a separate Full analysis phase. It preserves scope-specific large-schema consent, joins identical reads, and drops stale results. Maintenance only detects tools. Parameters and Instruments read on open (one attempt per connection; Refresh retries). Parameters inline edit controls use parameter metadata to stage local changes, and **Review and apply** reviews up to 25 changes at once while **Change…** opens the single-parameter sheet (including **This session only** handoff to the Query editor). Guidance is collapsed by default.

### Action feedback

Every action reacts in the frame of the click and says what is happening and
what happened. The parts live in `apps/workspace/src/kit.tsx` and `kit.css`
(outside `DataTable`); `test/action-feedback-unit.mjs` and
`test/action-feedback.mjs` check them.

| Part | Contract |
| --- | --- |
| `useAction(scope?)` | One action's lifecycle: `idle`, `pending`, `done`, `failed`, `unknown`. `run(work, text)` sends nothing while the same action runs (single flight), shows `text.pending` at once, then `text.done` (a string, or a function of the reply; empty leaves no line), the error in plain words, or, for `write: true`, `writeFailure`'s answer. `cancel()`, a new `scope` (the connection or form it belongs to) or unmounting ends it quietly: its reply is never applied. Writes pass no scope; their outcome is always shown. A `done` or `failed` function that throws is logged and replaced by "Done." or the error's own words, so an action always ends. |
| `ActionButton` | The control that runs it. While `busy`: a spinner and `busyLabel` in the present tense ("Testing…", "Applying…"), `aria-busy`, clicks ignored, and focus kept (`aria-disabled`, never `disabled`). The other label is reserved, invisible, in the same grid cell, so the width never changes; beside an icon the label starts at the icon. |
| `ActionStatus` | The line beside or under the action: a polite live region that is always mounted (a region inserted with its text is often not read), with the seconds after 1 s (drawn, `aria-hidden`, never announced), "Still waiting for the server." after `ACTION_SLOW_AFTER_MS` (10 s) with `onStop` ("Stop" or "Stop waiting"), and `onRetry` ("Try again") after a failure. Idle it takes no room; `keepLine` keeps its empty line, for a status under a centered control. |
| `ConfirmSheet` | `busyLabel` on the confirm button (it keeps focus), and the busy label read aloud at once; after 1 s a one-line `progress` ("Waiting for the server" by default) with the seconds at the start of the action row, which never wraps the buttons. With `onLeave`, after 10 s without a reply the line reads "No reply yet", `leaveText` (what Close does) appears at the end of the body, and Cancel becomes Close: the sheet closes, the request goes on, and its owner shows the outcome in the view. Cancel and Escape never decline a write that was sent. Its buttons share one box (`.sheet-buttons`, `display: contents`), so the first is Cancel and the last confirms. |
| `ViewLayout` and `ViewStatus` | Refresh is an `ActionButton` ("Refreshing…"). The loading line counts seconds after 1 s; after `slowAfter` (20 s; sampling views add their window, Indexes and Tables 45 s) it says so (`slowText`, "Still waiting for the server." by default) and offers Stop (`reads.stop`: the view keeps its report and the request is aborted unless Refresh all waits for the same read); a read error offers Try again (`onRetry`) beside the alert, never inside it; `doneAt` (`reads.doneAt`) announces "Updated at …". Every new `loading` text is announced, so it stays the same while the read runs: never put the seconds in it (Tuning's Full analysis passes "Running every Server view once, about 10 to 20 s in all. It only reads." and `slowText` "Longer than the usual 10 to 20 s."). |
| `writeFailure(error)` | A write's exception: an HTTP 4xx reply is a failure in the server's words; anything else (no reply, a lost connection, a 5xx, a time limit) is "Outcome unknown", and the write is never sent again. |

Top statements keeps its first-load control mounted while reading, with progress
beneath it. Its prerequisite gates and capture errors never offer a read retry.
Error log reads on open and uses the shared Refresh, Stop and Try again controls.
Database diagram rebuilds through Refresh; its database-list reload reports
progress and errors separately. Closing a stalled import aborts the request and
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
- Refresh all becomes Stop in the same spot. Stop takes clicks only after
  `STOP_ARM_MS` (600 ms), so a double click on Refresh all does not stop the
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
  to their read status. Processlist shows a KILL outcome before its reread
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
- Maintenance: Detect tools, Check upgrade and each preflight keep focus and
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
commits on blur or Enter, once per changed value. A connect revision refreshes
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
