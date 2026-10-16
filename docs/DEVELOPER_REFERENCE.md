# MyFlames Developer Reference

This is the detailed contract per feature; [AGENTS.md](../AGENTS.md) has the rules. Read the relevant section before changing behavior, output formats, UI integration, or feature-specific tests.

## Table of contents

- [Commands Reference](#commands-reference)
  - [Local UI](#local-ui)
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
  - [Server health](#server-health)
  - [Waits, Indexes, Tables, I/O, Config](#waits-indexes-tables-io-config)
  - [Server variables: config set, reset-persist](#server-variables-config-set-reset-persist)
  - [Instruments](#instruments)
  - [Full analysis report](#full-analysis-report)
  - [Server lifecycle: upgrade-check, dump, restore, clone](#server-lifecycle-upgrade-check-dump-restore-clone)
  - [Replication: status, skip, skip-loop](#replication-status-skip-skip-loop)
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

## Commands Reference

### Local UI

`python3 -m myflames ui [--port PORT] [--no-browser] [--no-restore]` launches the
  myflames Performance workspace on IPv4 loopback. React/TypeScript source is in
  `apps/workspace/`. Places (sidebar, `Sidebar.tsx`): Home (`Home.tsx`: getting
  started, Connections, recent work), Plans (explore, plus Compare as a mode;
  Import plan lives in the Plans bar), Query editor (`QueryLab.tsx`), Server
  (`Server.tsx`: a grouped sub-navigation, Overview: Health (first and default),
  InnoDB, Full analysis (`report`); Workload: Top statements (`SlowQueries.tsx`),
  Sessions (replaces Running queries; a saved `running` restores as `sessions`),
  Replication (`Replication.tsx`), Waits, I/O; Contention: Locks; Schema:
  Indexes, Tables, Database diagram; Setup: Configuration (`Config.tsx`), Instruments, Maintenance
  (`Maintenance.tsx`; both with `opsApi.ts`, `OpsShared.tsx`, `ops.css`,
  mounted on first visit and kept mounted so a skip loop or a job outlives a
  view switch); Security: Users and roles (`Accounts.tsx`)). Badges come only from loaded reports, which
  `Server.tsx` keeps. Every view uses `ViewLayout` from `kit.tsx`: toolbar,
  one summary line, a table or one-line rows, and an inspector (pane, or a
  bottom sheet below 1,040 px; the list of views is pushed below 720 px).
  Shared components live once in `kit.tsx`/`kit.css` (SourceList, DataTable,
  GroupedList, Inspector, Popover/NotesButton, StatusPill, CountBadge,
  Segmented, MetricTile, Sparkline, ConfirmSheet, PrerequisiteGate);
  explanations go behind ⓘ or into the inspector, never on the page. Investigations (`Library.tsx`),
  Learn (`Teach.tsx`; the CLI stays `myflames teach`). Below the places, Open
  plans; the footer has one autosave status and a Settings popover (Restore
  workspace on startup, Audit log, About myflames). Saved connections and the connection picker are in
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
  policy is unknown: the strip says "Production assumed" and Kill is off
  (`WritePolicy.blocked`). `SafetyStrip` marks a
  Production (red) or read-only (neutral) connection in use under the top bar
  of the Query editor and Server (`.workspace.has-safety-strip` adds
  `--strip-h` to `--topbar-height`), and the picker shows `TypeDot`. The Audit
  log is `Audit.tsx` (session mode `audit`, no place active). Stored session `mode` values keep
  the earlier names (`inspect`, `compare`, `query`, `library`, `teach`) plus
  `home`, `server` and `audit`; `placeOf` in `main.tsx` maps them to places. Naming follows
  `.claude/skills/ui-architecture/references/vocabulary.md`; styles use the
  tokens on `:root` in `style.css` (web-design skill). Browser test for the
  shell: `apps/workspace/test/navigation.mjs`; `apps/workspace/test/server-nav.mjs`
  covers the Server place. The app mark is
  `docs/brand/myflames-icon-v2.png`, shared by the sidebar, Home, and favicon. Vite emits the stable
  `/assets/myflames-logo.png` URL; retain earlier logo URL aliases in `ui.py`. `npm ci && npm run build` there regenerates committed
  `myflames/ui_assets/` files shipped in the wheel. Python users need no Node.js.
  The UI API reuses the parser, sidecars, and renderers. Investigations are
  saved only on request. Investigations and saved connections (profiles) use
  `~/.myflames/workspace.sqlite3`; remembered connection passwords are stored separately. **Save
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
  and `CURRICULUM` order. See `docs/LOCAL_UI.md` and `docs/QUERY_WORKSPACE.md`. Test with `test/test_ui.py` and the CLI suite.
  **3D lesson views** are an extra option beside a 2D lesson (today `btree`).
  `LESSON_3D` in `myflames/teach/__init__.py` maps a lesson to a prebuilt page in
  `myflames/assets/` (`teach3d-btree.html`), served by `render_lesson_3d` when
  `/api/teach/lesson` gets `"view": "3d"`; the catalog lists `views` per lesson
  and `Teach.tsx` shows a 2D | 3D switch only when `3d` is listed. The page is
  built from `apps/teach-3d` (plain TypeScript and three.js, no React): `npm ci
  && npm run build` there runs `tsc`, a Vite IIFE build, and
  `scripts/assemble.mjs`, which inlines the script and styles into one offline
  file (it works under the workspace iframe CSP), keeps three.js's MIT notice,
  and records a `myflames-sources-sha256` meta tag. Commit the rebuilt asset
  with the source change; `test/test_teach.py` fails on a stale hash or a page
  over its size budget. The numbers come from `innodb_index_shapes` and
  `leaf_range_pages` in `myflames/teach/_cost_model.py`; `apps/teach-3d/src/model.ts`
  mirrors them, `scripts/export-btree-shape.py` writes the parity fixture
  (`apps/teach-3d/test/fixtures/btree-shape.json`; rerun it after a model
  change), `npm test` checks the TypeScript against it, and `npm run test:smoke`
  checks the built page in a browser (every chapter's numbers, playback, reduced
  motion, the no-WebGL fallback, 390 px). The 2D lessons stay SVG only.
  Keep the import dialog's native file control visually clipped but keyboard
  accessible; verify open/error states at desktop and narrow, short viewports.
  Use `docs/UI_TESTING.md` for the interaction matrix. Check contextual Learn
  links after navigating to a different lesson and returning to the plan.
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
still use the explicit connection settings. Editor credentials are removed
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
  one item of the SET list; every item counts). Writes: INSERT/REPLACE,
  UPDATE/DELETE whose WHERE pins a key column (`key_columns`, given by the
  caller) to a value (`col = literal`, `literal = col`, `<=>`, `IN (literal,
  ...)`, not negated, no OR or XOR; the check is one linear token pass over at
  most `MAX_WHERE_CHECK` characters), changes of transaction_read_only or
  tx_read_only, `SET TRANSACTION READ WRITE`, `START TRANSACTION READ WRITE`,
  `SELECT ... FOR UPDATE|FOR SHARE|LOCK IN SHARE MODE` (`locking_read`), `INTO
  OUTFILE|DUMPFILE` (`file`), `INTO @var` and `:=`, GET_LOCK, NEXTVAL, other SET
  SESSION and user variables (`session`), COMMIT, XA START/END/PREPARE, temporary
  tables, ANALYZE/CHECK TABLE, FLUSH (not TABLES). Risky: DDL, TRUNCATE,
  RENAME, OPTIMIZE/REPAIR, UPDATE/DELETE without WHERE, with an always-true
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
  qualified/quoted calls, or nested calls that change state.
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
credentials or raw script are added. NULL is `null`; non-NULL values are strings.
Duplicate column names remain ordered columns plus row arrays. CSV, TSV and
Markdown use `export_result`; `insert` uses `export_result("sql", result, table=args.insert_table)`
to emit INSERT statements into the user-named table (default `result`). The name
is validated before execution and quoted as one identifier, including any dots.
It shares the UI exporter's identifier quoting and literals: typed binary columns
use `X'..'`, hex-looking text without binary metadata stays quoted text, NULL
stays NULL, and empty result sets emit no INSERT. Table mode JSON-quotes text to
distinguish SQL NULL from empty text and the literal string NULL. Non-JSON result
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
  confirms) and audited as `editor_safe_updates`. MariaDB `SET STATEMENT` of
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
  another connection; Stop, Commit and Roll back use the session's own
  connection, never the picker's. A restore probe is marked done only once
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
- Schema browser endpoint: `POST /api/live/catalog` (`ui._CATALOG_PATHS`,
  read-only, saved password filled like the views) answers
  `schema_browser.catalog_report`: `catalog-1.0` tables of one schema
  (`information_schema.TABLES`, at most 200 per page) or one table's columns and
  indexes (`COLUMNS`, `STATISTICS` read by name), tagged
  `/*myflames:editor*/`, names as hex literals. Optional `prefix` (literal text, including `%` and `_`)
  and `after` (exclusive continuation) are forwarded to the module after `check_table` validation,
  before opening a connection: null/empty means absent; otherwise strings of at most 64 characters,
  with blank-only text, NUL, LF and CR rejected. Pages use binary table-name order and return
  `next_after` when truncated. Tests: `test/test_schema_browser.py`, `test/test_ui.py`.
### Run

`python3 -m myflames --type [flamegraph|bargraph|diagram|tree|workbench] explain.json > output.svg`

`parser.parse_explain` rejects (ValueError, exit 2) a plan whose numeric fields
(`_PLAN_NUMBER_FIELDS`: `actual_*`, `estimated_*`, MariaDB `r_*`, `rows`, `loops`,
`cost`, format-1 `rows_examined_per_scan`) hold anything but null or a finite
number: measured counters up to 2^64, measured times up to 1e15 ms, estimates
any finite value. The check runs before any arithmetic. `load_explain_json`
uses text that parses as JSON unchanged; CLI decorations (a leading `EXPLAIN:`
label before the JSON, table borders, the column header) are stripped only
otherwise.
Root rendering uses the shared connection flags and `--profile` resolution. `-e/--execute` enables live mode with 127.0.0.1:3306 defaults; supplying connection flags without SQL exits 2. Explicit flags override profile fields and a saved password is resolved normally. The existing single-SELECT validation and read-only transaction guard apply before execution. Root, capture and the server runners in `workspace_cli.py` preserve `--ssl-cert`, `--ssl-key` and `--mysql-binary` through `_connection_options`; capture adds those CLI-only paths to `LiveJob.options` without changing its JSON schema.

An unreadable `--query-file` and a failed explicit `--sidecar PATH` exit 2. Automatically derived sidecars and lesson bundles remain best-effort. `guide [-o PATH] [-q]` parses its arguments and rejects unsupported flags. `teach LESSON` and `teach --index` / `--catalog` use `_write_output` for stdout/file routing, quiet status and exit 2 on write failures.

### Digest

- **Digest** (AI-era): `python3 -m myflames digest explain.json`: emits the compact, source-grounded digest of a plan (pipe to an LLM). `--cost` shows the token + $ saving vs pasting the raw plan; `--show-prompts` prints both prompts; `--tokenizer {heuristic,claude,gpt}` selects how tokens are counted (`claude` = Anthropic `count_tokens`, needs `myflames[tokens]` + `ANTHROPIC_API_KEY`; `gpt` = tiktoken, keyless, needs `myflames[gpt]`); `--json` for machine output (implies `--cost`, conflicts with `--show-prompts`; `--cost --json` is valid). Lives in `myflames/digest.py`. (Deprecated alias `tokens` still works: it warns on stderr and defaults to `--cost`.)
### Plan diff

`diff` remains a deprecated alias; it warns on stderr unless quiet. `--json` and `--digest` are mutually exclusive.

`python3 -m myflames compare before.json after.json` (alias of `compare`). `--digest` = token-cheap text diff for an LLM; `--json` = structured `compare-1.0` delta.
### CI gate

`python3 -m myflames check explain.json --fail-on full_scan,filesort`: exits 1 if a trigger matches (categories, severities, or `any`), 0 if clean, 2 on bad input. For pre-commit/CI/agent loops. Lives in `myflames/findings.py`.
### Advise

`python3 -m myflames advise explain.json [--json]`: ranked warnings + suggestions with `confidence`. Also `myflames/findings.py`. (Deprecated alias `findings` still works: it warns on stderr.) The single "Fix first" pick is `findings.primary_suggestion_index` / `build_findings` (one ranking policy, shared by the HTML card and the sidecar `primary_action`).
### Rule controls

`check` and `advise` take `--ignore LIST` (drop categories; unknown names exit 2) and `--list-rules` (prints `findings.RULES`: category, default severity, description, docs link). Every warning category in `output_sidecar._WARNING_CATEGORIES` must have exactly one `RULES` entry. `advise` text ends with a summary line; JSON findings carry `docs`.
### Advisor evidence rules

- **Advisor evidence rules** (`advisor.py` `_CONTEXT_RULES`, called with a context of `freshness` + `capture`): `STALE_STATS` (category `misestimate`), `REDUNDANT_INDEX` (`redundant_index`), `COLD_CACHE` (`cold_cache`); `RULE_CATEGORIES` maps them into the sidecar, other environment rules stay `env`. The plan-level row-estimate rule is `parser._collect_misestimates` (10x, 1,000 rows; skips overestimates below Limit/semijoin inner sides; MariaDB compares only ref/eq_ref). `parse_explain` tags `root["engine"]`.
### Find slow queries

`top`, `sessions` and `io` accept a single `--schema`; a repeated option exits 2 before connecting. Empty text results from `top`, `running`, `profiles list` and `audit list` emit table headers through the configured output destination, with explanatory notes on stderr unless quiet.

`python3 -m myflames top` (P_S digest summary, `top-1.0`) and `python3 -m myflames running [--explain ID]` (`running-1.0` / `explain-connection-1.0`; deprecated alias, use `sessions`), in `myflames/discover.py`; read-only, share `capture`'s connection flags. UI: the Server place (`Server.tsx` wraps `SlowQueries.tsx`), `/api/live/top|running|explain-connection`; browser test `apps/workspace/test/discover.mjs`.
  Capture from a digest: `python3 -m myflames capture --statement-digest HASH` (full or 8+ hex prefix; exclusive with `-e`; `--digest` is a deprecated alias that warns on stderr) looks the row up with `discover.find_statement`, runs the sample text in the digest's schema when the connection has none, and records `source: {"kind": "digest", "digest", "schema"}` in capture-1.0 (`live.normalize_source`; `source` may also be a text label such as import-shell's `"mysqlsh-diagnostics"`). No runnable sample (MariaDB, or MySQL `sample_truncated`) exits 2 and prints the SQL with `:p1`, `:p2`. Every `top-1.0` row carries `capture_problem` from `live.capture_problem`, the same SELECT-only rule `bind_query` enforces; never duplicate it in the UI. Placeholder rules live twice, in `discover.parameterize` / `digest_query` / `join_function_calls` and `apps/workspace/src/placeholders.ts`; both run the shared cases in `apps/workspace/test/fixtures/digest-placeholders.json` (`test/test_digest_capture.py`, `test/placeholders.mjs`), so change them together. Digest text needs built-in function names rejoined to their `(` (`COUNT ( * )` is a syntax error; names from `sql/lex.h` SYM_FN). UI row actions Explain / Run analysis (confirmation first) and Explain top N (EXPLAIN only, sequential, max 20) live in `SlowQueries.tsx`; row actions open the Query editor through `QueryLabHandle.openDigest`; tabs keep `source`/`fill`; browser test `apps/workspace/test/digest-actions.mjs`.
  Workload attribution (`discover.py`, additive to `top-1.0`): `--order full_join` (`SUM_SELECT_FULL_JOIN`, then `SUM_SELECT_RANGE_CHECK`), `scan` (`SUM_SELECT_SCAN`) and `no_index` (`SUM_NO_INDEX_USED`) list only digests where that counter is above zero (`ORDER_FILTERS`), with ties broken by total time. `--table SCHEMA.TABLE` (`parse_table_name`; backquote a part with a dot) adds `TABLE_FILTER` (`SUM_NO_INDEX_USED > 0 OR SUM_SELECT_SCAN > 0`) and a `LOCATE` prefilter on the table name, reads up to `TABLE_CANDIDATES` (500) digests, and keeps those `table_reference` matches: `` `schema` . `table` `` or `schema.table` from any default schema (`qualified`), the bare name only when the digest's `SCHEMA_NAME` is the table's schema (`unqualified`), case-insensitive, never another schema's table or a longer name. Every row gains `select_full_join`, `select_range_check`, `select_scan` (read after `QUERY_SAMPLE_TEXT`, so older answers read as 0); with a table filter each row has `table_match {schema, table, kind, label: "text match, not proof"}` and the envelope `table` and `match {label, caveats, candidates, capped}`. Every envelope has `coverage {uptime_s, first_seen (oldest listed), digest_lost, overflow {executions, total_ms} | null}` (best effort; `Performance_schema_digest_lost` and Uptime from one `SHOW GLOBAL STATUS`). `/api/live/top` takes `order` (the seven values) and `table` (`"schema.table"` or `{schema, table}`). Never present a table match as proof: views, routines, aliases and digest text cut at `performance_schema_max_digest_length` break it, and the counters are per statement, not per table. Tests: `TestTableReference` and `TestWorkloadAttribution` in `test/test_discover.py`.
### Locks

`python3 -m myflames locks [--waits-only | --deadlock] [--limit N] [--json]` (`locks-1.1`), in `myflames/locks.py`: DDL statements waiting for a metadata lock (`ddl_waits`: from process-list wait states plus PENDING metadata locks; blockers `exact` from `performance_schema.metadata_locks`, `granted_locks` from MariaDB's optional `METADATA_LOCK_INFO`, else `likely` open transactions with an honest note; sessions queued behind the DDL), blocking tree over row and metadata lock waits with root blockers, INNODB_TRX transactions, row locks (MySQL `performance_schema.data_locks`, all locks; MariaDB `INNODB_LOCKS`, waits only), metadata locks with myflames' own GRANTED/PENDING compatibility check (not `sys.schema_table_lock_waits`), `kill_privilege` from plain `SHOW GRANTS` (active roles included; a hint, the server decides), and the parsed latest deadlock. The report only reads: never touch `setup_instruments`/`setup_consumers`; owner of a lock comes from `ENGINE_TRANSACTION_ID` → INNODB_TRX, not `THREAD_ID`. Keep the data_locks latch caution (MySQL < 8.0.40, 8.1–8.4.2, 9.0.x) and no default auto-refresh. The one write is `kill_session` (`kill-1.0`): `myflames locks --kill ID [--query] [--yes]` and the UI's **Kill blocker** (`POST /api/live/kill` with `connection_id`, `mode`, `expect`), one session per explicit, confirmed request, never automatic or in bulk. It re-reads the session (same source as the report) and refuses when it is gone, a different session (user/host/thread/`trx_started` changed), its own or a myflames-tagged session, a server thread, already rolling back (INNODB_TRX `ROLLING BACK`; a "Killed" command alone is not proof, MariaDB shows it after KILL QUERY on an idle session), a prepared XA transaction on MySQL (idle session whose last statement in `events_statements_current` is `XA PREPARE`; the xid is copied only when well formed: ending the session releases nothing, so show `XA COMMIT`/`XA ROLLBACK` instead of Kill; MariaDB needs no check, it releases a detached XA transaction's metadata locks), or KILL QUERY on an idle session; server errors 1094/1095 become `gone`/`denied` results. A MySQL ALTER that adds a foreign key or renames also locks parent tables: `ddl_waits[].table` is the object it waits on, `statement_table` the one it names. CLI exit codes: 0 ended, 1 a sent operation failed, 2 an unsent refusal, decline, or error; refuses on a non-TTY without `--yes`. Both paths run the write policy first ([Write safety and audit](#write-safety-and-audit)): KILL is risky, so a read-only connection refuses it before any connection opens (CLI: before the password prompt; result `status: "refused"`, `engine: null`), and Production and Development both confirm (the CLI prompt or `--yes`; the UI sheet's `expect`). Every attempt writes an audit record (`kill_session(..., audit=)`, `kill_cli(..., audit=, policy_text=)`): refusals and declines as such, otherwise `pending` before the KILL is sent and `ok` or `error` (with the server error code) after; kill-1.0 results gain `audit_id`. Kill confirms and executions are bound to `connection_identity` and `server_identity` (MySQL `server_uuid`; MariaDB `server_id`, `hostname`, `version`); `locks.lock_report(identity=True)` includes the opt-in `server_identity` for the workspace Kill sheet (the CLI Kill re-reads it), and any mismatch or missing identity after confirmation refuses without sending. UI: `Locks.tsx` (Contention group in `Server.tsx`), `/api/live/locks` and `/api/live/kill` (saved-password helper, not in `_SERVER_VIEW_PATHS`); tests `test/test_locks.py`, `apps/workspace/test/locks.mjs` (fixtures captured from live servers in `apps/workspace/test/fixtures/`).
### Sessions

`python3 -m myflames sessions [--all] [--session-user U] [--session-host H] [--schema NAME] [--min-seconds N] [--limit N] [--explain ID] [--json] [-o PATH] [-q]` (`sessions-1.0`), in `myflames/sessions.py`: every connection (P_S `processlist` on MySQL 8.0.22+ with P_S on, else `information_schema.PROCESSLIST`; MariaDB adds TIME_MS, STAGE/MAX_STAGE/PROGRESS, MEMORY_USED, EXAMINED_ROWS) joined with `INNODB_TRX` on `trx_mysql_thread_id` (ages with `SET SESSION time_zone = 'SYSTEM'`, since `trx_started` is in the OS time zone; needs PROCESS; without it, own sessions only and `capabilities.process_privilege: false`). Flags `long_running` (10 s watch / 60 s problem), `idle_in_trx` (Sleep + trx, graded on trx age 10/60; guarded against the two-read skew), `lock_wait` (`innodb` from INNODB_TRX LOCK WAIT, a row or InnoDB table lock; `metadata` from the MDL wait states; `table` from thr_lock), `system_thread`, `own_connection` (`/*myflames:` tag; the reader itself is excluded by `ID <> CONNECTION_ID()`). The summary (connections vs `max_connections`, `by_command|state|user|host|db`, oldest transaction, history list length) always covers every session; `active_only`/`user`/`host`/`db`/`min_seconds` narrow only `sessions`. `--user`/`--host` are the connection flags, hence `--session-user`/`--session-host`. `sessions.sessions_report(identity=True)` includes opt-in `server_identity` (MySQL `server_uuid`; MariaDB `server_id`, `hostname`, `version`) so Kill session in the workspace is bound to connection and server identity. `myflames running` is a deprecated alias (warns unless `-q`, keeps `running-1.0`). UI endpoint `POST /api/live/sessions`; tests `test/test_sessions.py` (replays in `test/fixtures/perf/sessions-*.json`).
`myflames sessions --kill ID [--query] [--yes] [--json] [-o PATH] [-q]` is an
alias of `locks --kill`: `cmd_sessions` calls the same `_kill_command(args,
locks.kill_cli)`, returning the same `kill-1.0` result and exit codes (0 ended,
1 a sent operation failed, 2 an unsent refusal, decline, or error). It keeps the same
confirmation, connection/server identity binding, target re-read, write policy
and audit. `--kill` and `--explain` are mutually exclusive; `--query` and `--yes`
require `--kill ID`. Listing remains read-only.

### InnoDB

`python3 -m myflames innodb [--raw] [--fail-on problem|watch] [--json] [-o PATH] [-q]` (`innodb-1.0`), in `myflames/innodb_status.py`: `SHOW ENGINE INNODB STATUS` split into sections (`parse_status` is pure) and post-processed into `sections[]` of `{key, title, present, status, summary, items[], data, links[]}` (semaphores, transactions and purge, deadlock via `locks.parse_latest_deadlock`, foreign key error, file I/O, change buffer, adaptive hash index, redo log with health.py's pressure points against the capacity in use (`Innodb_redo_log_capacity_resized`, then "Log capacity used", never "Log capacity"), buffer pool with instances, row operations, background). Never return the raw text unless `raw: true` / `--raw`. Unknown or missing lines are tolerated (`parse`, `notes`); a sub-second averaging window drops the rates. Without PROCESS: `available: false`, `error.code: no_process`, CLI exit 2. UI endpoint `POST /api/live/innodb`; tests `test/test_innodb_status.py` with real captures in `test/fixtures/innodb/`.
### Server health

`python3 -m myflames health [--interval N] [--fail-on problem|watch] [--json] [-o PATH] [-q]` (`health-1.0`), in `myflames/health.py`: two `SHOW GLOBAL STATUS` samples N s apart (1–60, default 5) plus a few `SHOW GLOBAL VARIABLES`; no Performance Schema, no privilege. Sampling (`read_variables`, `take_sample`, `health_report`) is separate from the pure `compute_health`; each metric has value/delta/rate/unit/status/why/rule/link/learn. Statuses are rules of thumb (`RULES`, constants at the top), `info` where no universal limit exists, `unknown` for missing or reset counters (Uptime going down = restart). Keep the engine rules from the module docstring: MySQL disk reads = `Innodb_buffer_pool_reads` + `_read_ahead`, MariaDB reads already include read-ahead (same as `advisor._rule_cold_cache`); MariaDB counts a memory-to-disk temp table twice in `Created_tmp_tables`; MySQL redo rate uses the `Innodb_redo_log_current_lsn` delta; dirty pages are only ever watch. Diagnostics (`diagnostics` list, `_diagnostics`, `DIAGNOSTIC_RULES`): title, headline, verdict, why, rule, numbers, settings (value + engine default), a recommendation only at watch/problem and always with a caveat; `summary.diagnostics` counts them and `summary.status` is the worst of metrics and diagnostics. Buffer pool full = free pages <= `innodb_lru_scan_depth` x instances (MySQL; MariaDB one instance), bytes read = pages read x `innodb_page_size`; redo coverage and time-to-pressure come from the LSN growth (MySQL `Innodb_os_log_written` re-counts partial blocks, measured 2x under small commits), capacity from `config_review.redo_capacity` (`Innodb_redo_log_capacity_resized` first), thresholds from the checkpoint age (MySQL ~0.73/0.79 of capacity, MariaDB 7/8 and 1.0 of `Innodb_checkpoint_max_age`); the temp-table limits text is `config_review.tmp_table_limit`. Never change a setting. `--fail-on` covers diagnostics and metrics, exits 1 like `check`; 2 on bad input or connection failure. The tool's own second sample (3 statements, 1 temp table, 1–2 scans, 1 opened table on MySQL) must never trip a rule (`MIN_EVENTS`). UI: `Health.tsx` (first Server tab), `/api/live/health`; the sample history (20, sparklines) stays in memory, session restore keeps only the tab and `health_interval`. Tests `test/test_health.py` (live two-sample fixtures from MySQL 8.4/9.7/26.7 and MariaDB 11.4/13.0 in `test/fixtures/health/`, plus `scenario-*` captures for warming, full, redo, binlog, threads, and table cache), `apps/workspace/test/health.mjs` (`npm run test:health`; the Diagnostics cards, their trends, and 390 px).
Health text prints `Statements: myflames top --order full_join` once when a
`select_full_join` metric or diagnostic is watch/problem; other statuses omit it.

### Waits, Indexes, Tables, I/O, Config

- **Waits, Indexes, Tables, I/O, Configuration** (Server place groups Workload: Waits, I/O; Schema: Indexes, Tables; Setup: Configuration; read-only, share the connection flags and `--profile`, `--json`, `-o`, `-q`): shared plumbing in `myflames/perf_common.py` (`Reader` tags every statement `/*myflames:<view>*/`; `preflight` reads variables with `SHOW GLOBAL VARIABLES WHERE Variable_name IN (...)`, never `SELECT @@x`, plus Uptime and `Performance_schema_*_lost`; P_S off returns empty tables, not errors, so check `performance_schema` first). Never TRUNCATE (it also resets per-table and per-index stats), never write `setup_instruments`/`setup_consumers`, never `SET GLOBAL userstat`; print the statement instead. Raw P_S/I_S tables only, never sys views.
  - `python3 -m myflames waits [--interval N] [--limit N]` (`waits-1.0`, `myflames/waits.py`): two reads of `events_waits_summary_global_by_event_name` N s apart (2–60, default 10), `EVENT_NAME LIKE 'wait/%'` (excludes idle); window deltas (a counter going down drops the window), per-class sums, since-start list with max; never add classes up or show a share (table I/O nests file and synch waits). Coverage from `setup_instruments` with the enable UPDATE and my.cnf line (`ENABLE_CAVEAT`). MDL instrument off by default on MariaDB; `innodb_temp_file` means differ per engine (`ENGINE_MEANINGS`).
  - `python3 -m myflames indexes [--schema NAME ...] [--all-schemas] [--limit N]` (`indexes-1.0`, `myflames/indexes.py`): unused = COUNT_STAR 0 in `table_io_waits_summary_by_index_usage`, read before I_S (MariaDB's STATISTICS opens tables, registering them with zero counts); PRIMARY and UNIQUE never listed on both engines; FK-backing indexes (KEY_COLUMN_USAGE, leading columns in order, PK columns appended per engine) are `keep`; tables with no row operations or a disabled `setup_objects` row are `no_data`. Redundant indexes reuse `advisor.find_redundant_indexes` over I_S.STATISTICS (parity test with the advisor rule). Full scans use `SUM_TIMER_FETCH` of the NULL-index row. MariaDB P_S off + `userstat=ON` falls back to INDEX_STATISTICS/TABLE_STATISTICS. `--schema` repeats (scope `schemas`; `schema` stays for one); the CLI warns without `--schema` or `--all-schemas` and reads every user schema up to 2,000 tables (`perf_common.require_choice` on the selected scope). `--all-schemas` lifts that limit like the API's `all_schemas`, for every user schema or, with `--schema`, for those schemas only. The UI database picker sends `schemas`; with none, `/api/live/indexes` reads all user schemas only up to `perf_common.ALL_SCHEMAS_TABLE_LIMIT` (2,000, MySQL's default table_definition_cache) base tables and raises above it (`all_schemas: true` skips the check; the UI never sends it). The shared Indexes/Tables gate and schema picker note add a MariaDB-only sentence warning that the read opens every table in scope and can evict cached table definitions (`all_schemas_note(engine=...)`); MySQL keeps the existing message. Indexes text adds `Statements: myflames top --order no_index --table SCHEMA.TABLE` under TABLES READ WITHOUT AN INDEX for the first listed table only; empty/unavailable sections omit it.
  - `python3 -m myflames tables [--schema NAME ...] [--all-schemas] [--fresh] [--limit N] [--fail-on problem|watch]` (`schema-1.0`, `myflames/schema_health.py`; named `tables` because `--schema` already means one database): tables without a PRIMARY KEY from I_S.TABLES (BASE TABLE + MariaDB SYSTEM VERSIONED; sequences and views skipped) and I_S.STATISTICS `NON_UNIQUE = 0` in key order. `no_usable_key` (problem: hidden 6-byte DB_ROW_ID) vs `not_null_unique` (watch: InnoDB clusters on the first unique key with all parts NOT NULL, whole columns, not functional, not virtual generated, not MariaDB long hash). GIPK (`my_row_id`, EXTRA `auto_increment INVISIBLE`) is listed apart; when `show_gipk_in_create_table_and_information_schema` is OFF globally, prefix `SET SESSION ... = ON;` in the same client call. Fix statements (never run): NULL/duplicate checks, `ADD PRIMARY KEY (...), DROP INDEX uk, ALGORITHM=INPLACE, LOCK=NONE`, invisible `AUTO_INCREMENT` surrogate with `LOCK=SHARED`; no ALGORITHM/LOCK for other engines. Fragmentation: DATA_FREE vs DATA_LENGTH + INDEX_LENGTH (free % of the file), judged only for file-per-table; shared (system/general) tablespaces listed apart. Tablespace map: MySQL `INNODB_TABLESPACES` (record-by-record DD scan with `AUTOEXTEND_SIZE`, subtracted from DATA_FREE before judging; `INNODB_TABLESPACES_BRIEF` fallback on "Unknown column"; never `INNODB_TABLES`, which loads every table into the dictionary cache) plus `SHOW CREATE TABLE` (max 50) only when a General tablespace exists; MariaDB `INNODB_SYS_TABLES` (space 0 = system; `INNODB_SYS_TABLESPACES` lists only opened spaces). Needs PROCESS, else `unknown` + note. Thresholds `FREE_WATCH_*` (10%, 100 MiB) and `FREE_PROBLEM_*` (25%, 1 GiB). `--fresh`: `SET SESSION information_schema_stats_expiry = 0;` before the TABLES read, same client call (MySQL only; MariaDB has no cache). Row-ID, replica-apply and OPTIMIZE texts are version-aware (`row_id_text`, `why_texts`, `optimize_caveat`). Schema picker list: `perf_common.schema_catalog` (`schemas-1.0`, `/api/live/schemas`; MariaDB counts name columns only so no table is opened); `check_schemas`/`scope_sql` are shared with Indexes.
  - `python3 -m myflames io [--interval N] [--schema NAME] [--limit N] [--all-instances]` (`io-1.0`, `myflames/io_stats.py`, never `io.py`): status deltas (no P_S needed), `table_io_waits_summary_by_table` (schema-scoped), `file_summary_by_instance` and `_by_event_name` (temp files only in the latter), since-start lists; `classify_file` maps FILE_NAME to kinds and schema.table (`#p#`/`#P#`, `@XXXX`). MariaDB has no redo log file instrument. Above `INSTANCE_LIMIT` (2,000) table or file instances a family is left out of both snapshots (`collection.omitted`) unless `--all-instances` / `all_instances`; collection has a 30 s budget plus the interval. No auto-refresh in the UI.
  - `python3 -m myflames config [--fail-on problem|watch]` (`config-1.0`, `myflames/config_review.py`): checks with engine `DEFAULTS`, `set_by` from `variables_info` (MySQL) or `SYSTEM_VARIABLES` (MariaDB), statuses with `RULES`, `why`, `caveat`, and `action` text; reuses `advisor.buffer_pool_coverage` and `advisor.flush_log_durability` (MariaDB's 3 is durable). Tablespace sizes need PROCESS (else `unknown`). `--fail-on` exits 1 like `health`. Each setting carries `dynamic` (`variables.static_dynamic`: true, false, or null when unknown, no extra read) so the inspector can label Change; changing a setting is [`config set`](#server-variables-config-set-reset-persist).
  UI: `Waits.tsx`, `Indexes.tsx`, `Tables.tsx` (+ `tables.css`), `IoStats.tsx`, `Config.tsx`, shared `PerfShared.tsx` (incl. `SchemaPicker` and `useSchemaCatalog`, one cached list per connection) + `perf.css`; endpoints `/api/live/waits|indexes|tables|schemas|io|config` (in `ui._SERVER_VIEW_PATHS`, saved-password helper). Session restore keeps the tab, the shared `schema` (I/O and Top statements' schema filter), per-tab `indexes_schemas`/`tables_schemas` (omitted when empty = all user databases) and `waits_interval`/`io_interval`, never reports. Links: I/O table rows open Index health or Top statements for that schema; Indexes and Tables rows open each other for the same table (picker set to its database, a "Showing only" focus chip); wait rows open Locks or I/O. Tests: `test/test_waits.py`, `test_indexes.py`, `test_schema_health.py`, `test_io_stats.py`, `test_config_review.py` (replay fixtures recorded live in `test/fixtures/perf/`, helper `test/_perf_replay.py`), browser suites `apps/workspace/test/{waits,indexes,tables,io,config}.mjs` (`npm run test:waits|test:indexes|test:tables|test:io|test:config`, shared `perf-harness.mjs`, whose `open(endpoint, name, extra)` mocks extra paths such as `/api/live/schemas`).
### Server variables: config set, reset-persist

Backend `myflames/variables.py` (contract: the db-operations skill; facts and source citations: the module docstring), CLI `_config_change` in `myflames/workspace_cli.py` (dispatched from `cmd_config`), endpoints `ui._variable_response` / `_variable_apply`, UI `Config.tsx` + `config.css`. Tests: `test/test_variables.py` with live replays `test/fixtures/perf/variable-<server>-<scenario>.json` (MySQL 8.4.11 and MariaDB 11.4.13, recorded 2026-10-15 on throwaway containers: SET GLOBAL; SET PERSIST, then a container restart, `restart` shows the value survived; SET PERSIST_ONLY of the read-only innodb_log_file_size, `restart-persist-only` shows it applied; RESET PERSIST; SET SESSION; the 1238 and 1227 refusals; a value changed between plan and apply; buffer pool and MariaDB block-size rounding), browser suite `apps/workspace/test/config.mjs` (`variable-*.json` written by `server-fixtures.py`).

- `read_variable(conn, name)` (`variable-1.0`, tag `/*myflames:variable*/`, reads only): one client call with `SHOW GLOBAL VARIABLES` and `SHOW SESSION VARIABLES` separated by marker rows, then MySQL `performance_schema.variables_info`, `persisted_variables` and (9.0+) `variables_metadata`, or MariaDB `information_schema.SYSTEM_VARIABLES`. Fields: `scope` (global, both, session, or null), `dynamic` (MariaDB `READ_ONLY`; MySQL from `MYSQL_VARIABLES`, a map read from `sys_vars.cc` and `ha_innodb.cc` with scope, type and READ_ONLY / NON_PERSIST / PERSIST_AS_READONLY flags, null outside it), `type` (bytes, integer, double, boolean, enum, set, flagset, string), `values`, `min`, `max` (MariaDB catalog; MySQL variables_info or variables_metadata for integers, the map for doubles), `block_size` (MariaDB), `global_value`, `session_value`, `default`, `set_by`, `persisted {supported, value, present, load}`, `modes {global, persist, persist_only, session, reset_persist: {available, reason, recommended}}`, `notes`. A MySQL variable outside the map is offered by the CLI only; the server decides (1238 when read-only).
- `plan_change(conn, name, value, mode)` and the pure `build_plan(v, value, mode, grants)` (`variable-plan-1.0`): the mode must be available (MariaDB has no PERSIST, PERSIST_ONLY or RESET PERSIST: refused with `alternative.config_line`, the `[mysqld]` line; a read-only MySQL variable offers only `persist_only`; wrong scope; NON_PERSIST; a session value that is read-only), and the value valid (`normalize_value`: integers within the range, K/M/G/T converted for byte sizes, doubles, ON/OFF, enum and set items matched without case, `DEFAULT`; text as `'...'` when plain, else `CONVERT(X'..' USING utf8mb4)` so no escaping rule or sql_mode matters). Returns `statement` (untagged), `display`, `before {global, session, persisted}`, `undo`, `effect {now, after_restart, needs_restart, text}`, `caveats` (the server does not check a PERSIST_ONLY value, persisted_globals_load OFF, mysqld-auto.cnf applied after the option files, buffer pool rounding to chunk size x instances and the 1 GiB rule with several instances, MariaDB block size, memory per connection, durability, MariaDB's log_slow_query copied per connection, replication-sensitive session variables), `privileges` (`privileges(v, mode, grants)` over plain `SHOW GRANTS`: dbops' operations, MariaDB's per-variable SET GLOBAL privileges `MARIADB_GLOBAL_PRIVILEGE`, restricted SET SESSION per engine with MySQL's `_no_super` ones, both privileges to reset a PERSIST_AS_READONLY entry), `classification`, `config_line` and `expect {name, mode, statement, global_value, session_value, persisted_value}`. Status `refused` with `problems [{code, message}]` sends nothing.
- `apply_change(conn, plan, expect, decision, confirmed, audit)` (`variable-change-1.0`): `dbops.gate` first (SET GLOBAL, PERSIST, PERSIST_ONLY and RESET PERSIST are risky, so Production and Development confirm; SET SESSION follows dbops' variable lists; read-only refuses), then a fresh read and plan; nothing is sent when the plan is refused, its statement differs from `expect.statement`, or the global, session (session mode) or persisted value changed. The audit record is written before sending; one statement tagged `/*myflames:variable*/` with `dbops.WRITE_CLIENT_FLAGS`; SET SESSION and its `SHOW SESSION VARIABLES` go in one client call (one session). Server errors: `denied` (1227, 1095, 3630, 1142, 1044, 1045), `refused_by_server` (1228, 1229, 1238, 1231, 1232, 1193, 1621, 3615, 3976, 1210, 1290), `interrupted` (1317, 1205, 1213), else `failed`; client errors (2000 and up) raise, and an interrupt, time limit or 2013 leaves the record `unknown`. Then the variable is read again (`after {global, session, persisted, resize_status}`): `changed` (`adjusted` when the server rounded or clamped it) or `unchanged` (the server kept the old value, a warning-only refusal). Audit operations `set_global`, `set_persist`, `set_persist_only`, `set_session`, `reset_persist`.
- `python3 -m myflames config set NAME VALUE [--persist | --persist-only | --session] [--yes] [--json] [-o PATH] [-q]` and `config reset-persist NAME [--yes] [--json] [-o PATH] [-q]` (`change_cli`): the policy from `--profile` runs before any prompt or connection (read-only refuses, exit 2); the plan (statement, values, effect, privileges, caveats, undo) on stderr; `[y/N]` when the policy confirms (a harmless SET SESSION on Development runs without); non-TTY needs `--yes`. Exit 0 changed, 1 a sent change was denied, refused by the server or not applied, 2 an unsent refusal, decline, bad input or connection failure. `myflames config` alone stays the read-only review. `config set --session` prints a stderr note that it applies only to the one-off connection myflames opens, useful to test a value; `--yes -q` suppresses this note with the plan.
- Endpoints: `POST /api/live/variable` (`name`) and `POST /api/live/variable/plan` (`name`, `value`, `mode`; adds `policy {action, tone, title, text, connection_type, read_only}`) read with the saved password like the views (`_VARIABLE_READ_PATHS`, not `_SERVER_VIEW_PATHS`); `POST /api/live/variable/apply` (`name`, `value`, `mode`, `expect`, `confirmed: true`; `_WRITE_PATHS`: policy, then the saved password, then `apply_change`). A refused or unconfirmed request is audited.
- UI: in the Configuration inspector each setting under Current values has **Change…** (dynamic), **Change at next restart…** (read-only on MySQL: PERSIST_ONLY), **Option file line…** (read-only on MariaDB) and **Reset persisted value…** when `set_by` is SET PERSIST; they are off while the report is from another connection, a read runs, or the write policy is unknown. The sheet (`VariableSheet` on `ConfirmSheet` with `confirmDisabled`) reads the variable, offers only the modes it allows, plans as the value settles (350 ms, latest answer only), and shows the statement, effect, privileges, undo, caveats, MariaDB's option file line and the policy text; Confirm is on only for a ready plan of the inputs on screen. For server changes, Cancel and Escape record the declined intent with the tagged statement shown. Confirm re-checks the policy (`useWritePolicy().check()`) and the connection key, applies, shows the answer (before → after) focused above the settings, and reads the configuration again while the same connection is in use.
- Session in Configuration prepares SQL only. `ConfigView.onOpenSql(sql)` receives the exact server-planned `SET SESSION` statement plus its terminator; `VariableSheet` calls it for **Open in Query editor**, never `/api/live/variable/apply`. Cancel/Escape on a Session draft creates no declined audit. The editor owns execution and its existing policy checks. The planning session value is labelled as a snapshot; its undo is omitted because the editor may have a different value. Without the optional callback the button is disabled and the statement stays copyable. Shell integration: pass `onOpenSql` through `Server` to `ConfigView`; in `main.tsx` call `labRef.current?.openSql(sql)` and `setMode("query")`. Add `openSql` to `QueryLabHandle` and its imperative handle to update the current tab via `changeTab`, clear stale digest source/selection metadata, and focus it without calling `send` or capture. These shell files are owned separately from Config. `node test/config.mjs --unit` checks the handoff without a browser.


### Instruments

`python3 -m myflames instruments [--preset minimal|locks|waits|defaults] [--json] [-o PATH] [-q]` (`instruments-1.0`, `myflames/ps_instruments.py`; Setup group in Server.tsx, between Configuration and Maintenance): reads `setup_instruments` (+ PROPERTIES on MySQL), `setup_consumers`, `setup_actors`, `setup_objects` and P_S's own memory; never runs a write. Groups (statement, stage, file, table I/O, table lock, MDL, synch, socket, memory, transaction, idle, error) and consumers each carry gain/lose/cost (cost levels are rules of thumb), the engine default, changes from it, and on/off runtime SQL plus my.cnf lines. `default_state` / `default_consumer` are the compiled defaults per engine (MySQL 8.4/9.x identical in source, 26.7 matched live; MariaDB: MDL, transaction, memory and every events_* consumer off, no `error`, no PROPERTIES) and a test checks them against every live fixture instrument. `consumer_parent`: history needs `_current`; statement, transaction and wait `_current` need thread_instrumentation; stage events need only global (source and live). setup_actors does not gate metadata locks or background threads. Presets (`minimal`, `locks`, `waits` with `undo`, `defaults`) were run live on MySQL 8.4/9.7/26.7 and MariaDB 11.4/13.0; `defaults` restores an exact fresh-server state. Restore `setup_actors`/`setup_objects` with TRUNCATE + INSERT, never DELETE (MariaDB's setup_objects DELETE removes the TABLE row whatever the OBJECT_TYPE). `FEATURES` + `TABLES` map each view to the P_S tables, instruments and consumers it needs; optional guard features carry `advisory: True` (such as `replication_receiver`) for features shown in Instruments with guidance but never counted as unready views; `test/test_ps_instruments.py` fails when a view module reads an unmapped table or names an unmapped instrument/consumer, so update the map with the module. UI: `Instruments.tsx` + `instruments.css`, `/api/live/instruments` (in `_SERVER_VIEW_PATHS`); `InstrumentsLink` (context from `SlowQueries.tsx`) links Waits, Indexes, I/O, Locks and Top statements "off" notes to a row (`group:<key>`, `consumer:<name>`, `ps`, `actors`, `objects`). Tests: `test/test_ps_instruments.py` (replay fixtures `test/fixtures/perf/instruments-*.json`), `apps/workspace/test/instruments.mjs` (`npm run test:instruments`).
### Full analysis report

`python3 -m myflames report [--interval N] [--schema NAME ...] [--all-schemas] [--json | --markdown | --html | --digest] [--fail-on problem|watch] [-o PATH] [-q]` (`report-1.0`, `myflames/report.py`; endpoints `POST /api/live/report` and `POST /api/report/export`). One read-only run of every Server view on one connection: `report.preflight` (tag `/*myflames:report*/`; each subprocess owns its own temporary option file) and the all-schemas table count (`perf_common.require_choice`, `ALL_SCHEMAS_TABLE_LIMIT`; above it Indexes and Tables are `skipped` with a note unless `--all-schemas` / `all_schemas: true`), then `health`, `waits`, `io` (windowed, `--interval` 2 to 60, default 10, submitted first), `config`, `innodb`, `sessions`, `instruments`, `checks` (`server_checks.py`), `tables`, `indexes` (capped at `LIST_LIMIT` 50 each), at most `MAX_WORKERS` (3) at a time in a `ThreadPoolExecutor` (`_run_all`; wrapped in a `_Guarded` connection where Ctrl+C sets the cancel event that windowed sleeps wait on, cancels queued views, and does not wait for running ones), then the attribution (`discover.top_report` with `full_join`, and `no_index` + table for up to `ATTRIBUTION_TABLES` (3) tables read mostly without an index, `ATTRIBUTION_LIMIT` 5 rows each). A failing view is caught per view (`_timed`; its error is redacted of the password) and listed in `header.failures`; the sections it feeds say what is missing. Never add SQL or thresholds to `report.py`: each family's rule lives in its module (`health.RULES`, `config_review.RULES`, `innodb_status`, `sessions`, `schema_health`, `server_checks.RULES`), and the report only maps items to findings (`_health_findings`, `_check_findings`, `_innodb_findings`, ...).
  Envelope: `schema_version`, `myflames_version`, `engine`, `server_version`, `taken_at`, `uptime_s`, `performance_schema`, `header {server {engine, version, version_comment, uptime_s, performance_schema}, started_at, elapsed_s, interval, scope {schemas, all_user_schemas, text}, views [{key, label, status ok|limited|failed|skipped, schema_version, started_at, taken_at, elapsed_s, error, notes}], failures [{view, label, status, error}]}`, `summary {problem, watch, unknown, info, ok, findings, status, text}`, `look_first` (a finding id or null), `findings[]`, `sections[] {key, title, status, summary, counts, findings (ids), views, notes}` in `SECTIONS` order (memory, redo, connections, temp_sorts, caches, query_cache, indexes, schema, waits, io, innodb, replication, security, observability), `attribution {available, gate (one sentence when unavailable), fix, label, caveats, coverage, health_window, full_joins {order, statements, taken_at}, full_scans [{schema, table, label, rows_without_index, fetch_ms_without_index, share_time, statements, capped, error}], notes}`, `checklist {covered, not_checked}`, `notes`. A finding: `id` (`<view>.<key>`), `family` (a section key), `status` (problem, watch, unknown, info, ok), `title`, `headline`, `verdict` (what this server shows, without the rule), `why`, `rule`, `caveat`, `action {text, sql, config}` (never applied), `numbers [{label, text, value?, unit?}]`, `settings`, `links [{view, label, key? | order?, table?}]` (views are the Server tab keys; `top` links carry `order` and `table` for attribution), `source {view, kind, key}`, `learn`, `impact`, `attribution` (`full_joins` or `full_scans`). Ranking is one function, `rank_findings`: status (problem, watch, unknown, info, ok), then `FAMILY_RANK` (replication, redo, memory, connections, innodb, security, temp_sorts, caches, schema, indexes, query_cache, io, waits, observability), then `impact`; `look_first` is the first problem, else the first watch. Sections and `summary.status` aggregate with problem > watch > unknown > info > ok (`_worst`, `summarize`): an unknown finding, or a view that failed or was skipped, never reads as ok, and the summary text counts unknowns. A finding that could not be judged (a privilege, Performance Schema off, tablespaces not readable for fragmentation, NULL replica lag with both threads running) is `unknown`, and every human format lists unknown findings with their reason (NOT JUDGED / Not judged). `coverage_lines` is the one projection of the attribution's scope (cumulative digest counters vs Health's window, uptime and the oldest FIRST_SEEN, the overflow row, `Performance_schema_digest_lost`, list caps) used by text, Markdown, HTML and the digest, and a table whose lookup failed shows its error, never an empty match. `at_or_worse` drives `--fail-on` (exit 1; 0 otherwise; 2 on bad input or when the server cannot be read, including when every attempted view fails). Total view failure prints the reasons on stderr, emits no payload and preserves any output file. Partial view failure keeps the report and existing exit code, and prints one warning line naming the failed views on stderr, including with `--json` or `-q`. Outputs are generated from the JSON only (`export(report, fmt)`): `format_text` (default; notes on stderr), `format_digest` (one line per non-ok finding with its numbers and rule, ok counted), `format_markdown` (`md` escapes HTML as entities and Markdown punctuation, `md_code` fences longer than any backtick run), `format_html` (static, `output_html_report._CSS`, every string through `html.escape`, the JSON embedded as `application/json` with `<`, `>`, `&` escaped, no script runs); every text format passes through `connector.terminal_text`. `/api/live/report` takes `connection`, `interval`, `schemas` (or `schema`), `all_schemas`; it uses the saved-password helper (`_SERVER_VIEW_PATHS`), `query_timeout` 60, and `run_single_flight` keyed by `report.flight_key` (the profile id, every effective connection and TLS setting, and an HMAC-SHA256 of the password under a per-process random key, never stored): only requests that would authenticate the same way share a run; an identical one joins it and gets the same report, one with other settings for the same connection is refused. `/api/report/export` takes `{report, format: json|text|digest|markdown|html, source?}` and returns `{format, content, mime, filename}`; `source` (`report.normalize_source`: label, host, port, user, `type` production or development; any other field, a password included, is dropped) is the connection the report was read from, added as the optional top-level `source` and named on a "Connection:" line in every human format.
  `server_checks.py` (`checks-1.0`, internal to the report; tag `/*myflames:checks*/`): replication from `SHOW REPLICA STATUS` (MySQL 8.0.22+), `SHOW SLAVE STATUS` (older), `SHOW ALL SLAVES STATUS` (MariaDB), read by column name from the client's `--xml` output (the connector's `--batch --silent` prints no column names), statuses from `REPLICA_LAG_S` (60 / 600 s, rules of thumb), threads not running and error numbers, plus `replica_read_only` on a replica (Group Replication's `group_replication_applier` / `_recovery` channels are skipped: a member is not an asynchronous replica); privilege text by version (MariaDB 10.11 also accepts SUPER, 11.0+ needs SLAVE MONITOR; MariaDB's `read_only` modes beyond ON/OFF exist from 12.0.1); accounts from `mysql.user` (MySQL) or `mysql.global_priv` then the `mysql.user` view (MariaDB), comparing `authentication_string` with `''` on the server so no hash is read, skipping locked accounts and roles (`no_password` problem only for plugins that accept an empty password: MySQL native, caching_sha2 and sha256; MariaDB native, old and an empty plugin name, never ed25519 or parsec; `anonymous` and `root_any_host` watch); the query cache on MariaDB (watch when on; hit share is `Qcache_hits / (Qcache_hits + Com_select)`); binary log expiry (watch when the binary log is on and nothing purges it: no expiry period, automatic purge off on MySQL, and no `max_binlog_total_size` / `binlog_space_limit` size limit). Every check needs only what it reads; without the privilege it is `unknown` with the grant to add.
  Additional native checks (same `report-1.0` finding fields and existing section keys):

  - `checks.memory_budget` (Memory): configured `innodb_buffer_pool_size`, `innodb_log_buffer_size` (twice on MariaDB for its two redo buffers; memory-mapped redo can bypass them), `key_buffer_size`, plus MariaDB query/Aria cache sizes and P_S's own measured internal memory (`SUM(CURRENT_NUMBER_OF_BYTES_USED)` where `EVENT_NAME LIKE 'memory/performance_schema/%'`; SELECT on that P_S table required). Using only P_S's own instruments avoids counting the buffer pool and session buffers twice. The per-connection scenario adds `sort_buffer_size`, `join_buffer_size`, `read_buffer_size`, `read_rnd_buffer_size`, `thread_stack`, both `binlog_cache_size` and `binlog_stmt_cache_size` only when `log_bin` is ON, and `net_buffer_length` twice (network buffer and result buffer) on both engines. It shows global + session × `max_connections` and global + session × `Max_used_connections`. MySQL TempTable adds `temptable_max_ram` once globally; `tmp_table_size`, `max_heap_table_size` and `temptable_max_mmap` are shown separately. MEMORY internal tables (including MariaDB) add one `min(tmp_table_size, max_heap_table_size)` table per connection. This is a demand scenario, not measured usage or a hard upper bound: multiple buffers/tables, growing network buffers, TempTable thread-local blocks and other allocations can exceed it. The two network buffers are allocated at connect and `thread_stack` at thread creation; other buffers are allocated on demand, some per table or per join. Both binlog caches are created together on the first binlog write. The scenario can overstate actual use when sessions do not need all buffers together; unreadable `log_bin` keeps the estimate partial. SQL collection never guesses physical RAM or reads the client's RAM; without it the finding is `unknown` and still shows the computed budget. Pure `compute_checks` accepts independently supplied `raw.physical_memory_bytes`: complete inputs yield watch when either scenario exceeds RAM, problem when global capacities alone exceed RAM, else ok with the caveat that other memory is excluded. Missing P_S privileges, counters or variables keep it unknown; known subtotals are labeled partial.
  - `checks.version_support` (Coverage): explicit per-series `VERSION_SUPPORT`, `VERSION_CURRENT`, `VERSION_LAST_REVIEWED` (2026-10-15), using the evaluation date. MySQL 8.0 maintenance ends 2026-04-21; 8.4 and 9.7 are LTS, 9.0–9.6 Innovation end at their successors. Retained end dates for MySQL 8.0/8.4/9.0–9.7 and MariaDB Community 10.6/10.11/11.0–11.2/11.4/11.8 come from vendor lifecycle/release pages cited beside `VERSION_SUPPORT`, not source files or tag timestamps. MySQL 26.7 and MariaDB 13.0 are explicit unknown-support entries: the reviewed trees have no matching release tags (MySQL `MYSQL_VERSION` says 9.7 LTS; MariaDB `VERSION` says 13.0.1 gamma). MariaDB 12.3 is also unknown: `mariadb-12.3.1:VERSION` says gamma, so this source review assigns no LTS end date. Short-term 11.3, 11.5–11.7 and 12.0–12.2 are explicitly listed with unknown end dates: stable successor tags exist through 12.2.2, but tag timestamps do not prove maintenance dates; 12.3.1 is only gamma. Current identifies the reviewed MySQL 9.7 LTS line, not the latest release; no current MariaDB LTS is nominated from the incomplete tags. Supported identifies another line with a reviewed future maintenance end date. End of maintenance is problem; the 180 days before it are watch (a planning rule of thumb); unlisted series and entries without a proved end date are unknown. Dates exclude Oracle Sustaining Support and MariaDB Enterprise extensions; month-only dates use the first day after the listed final month. Verify vendor/fork policy and installed patches separately; static dates can become stale. Tests inject dates around every table boundary.
  - `checks.myisam_cache` / `checks.aria_cache` (Caches; Aria only on MariaDB): reuse Tables' existing `summary.engines` after the parallel views finish, with no extra inventory scan. Show configured cache size, block size, used/unused blocks, unused block bytes, physical reads, requests and miss percent (`reads / requests × 100`). Watch at 5% misses after 1,000 requests, both review heuristics, not server limits. `Key_blocks_used` and `Aria_pagecache_blocks_used` are high-water marks; neither is added to current unused blocks to infer occupancy. Missing settings/counters, no read activity, or skipped/failed inventory yield unknown with the reason. No visible engine tables skips efficiency assessment, scoped to selected schemas and account visibility; it makes no claim about the rest of the server. Aria can still serve internal disk temporary tables. Cache changes require workload measurements and the memory budget.
  - `checks.wsrep` (Replication): read-only when wsrep variables/status exist, otherwise omitted. Also omitted when `wsrep_on` is OFF or `wsrep_provider` is empty/`none`, including ordinary MariaDB without Galera; these do not count as ok findings. Show cluster size and Primary/connected/ready/local state, fraction paused by flow control, and provider-reported certification rollbacks since provider start or last FLUSH STATUS. Disconnected, not ready or non-Primary is problem; singleton membership, not Synced, any certification rollbacks, or at least 10% paused is watch (review heuristics). Missing status or denied reads are unknown unless the variables explicitly establish that wsrep is disabled. Expected membership is unknown; a singleton may be intentional. Paused time covers the provider's reset period, not Health's sample window, and historical certification rollbacks do not establish a current failure rate. Counter meanings and reset periods are provider-dependent: the server calls the provider's status-reset method on FLUSH STATUS, but the provider implementation is outside the reviewed tree. No recovery or setting change is applied.

  MySQLTuner (GPL-3.0) is a checklist only: never copy its code, text or thresholds. The module docstring lists the families covered and not checked; `checklist.not_checked` carries the same list to every output. Tests: `test/test_report.py` (live recordings `test/fixtures/perf/report-mysql-8.4-replica.json`, `report-mysql-8.0.json`, `report-mysql-8.0-limited.json` (SELECT on one schema only), `report-mariadb-11.4.json` (Performance Schema and the query cache on), `report-mariadb-11.4-default.json`; `summary` retains the original findings and adds expectations for the new checks; `extension_provenance` describes replay additions and `superseded_calls` preserves replaced I/O reads), engine inventory in `test/test_schema_health.py` (`summary.engines`, additive to `schema-1.0`). `report-checks-*.json` are explicitly synthetic SQL replays for complete memory formulas, MySQL 8.0/8.4, MariaDB 11.4 with and without engine tables, and mocked wsrep. Extended live replays select new variable/status reads from existing recordings; uncaptured P_S memory returns NULL, and the limited-account fixture models its SELECT denial. These are labeled replay extensions, not new live measurements.
### Server lifecycle: upgrade-check, dump, restore, clone

Backend `myflames/lifecycle.py` (contract and evidence: the server-lifecycle skill), CLI `myflames/lifecycle_cli.py`, endpoints in `ui.py` (`_lifecycle_*`). Tests: `test/test_lifecycle.py` (fake tool executables that record argv, env and option-file mode, strict replays of live recordings, a "regressions found on live servers" section) and `test/test_lifecycle_cli.py` (CLI exit codes, confirmations, audit, endpoints); fixtures and what was and was not verified live: `test/fixtures/lifecycle/PROVENANCE.txt`.

- `python3 -m myflames upgrade-check --target-version X.Y.Z [--no-shell] [--config-path PATH] [--fail-on error|warning] [--json] [-o PATH] [-q]` (`upgrade-check-1.0`): `lifecycle.upgrade_check`. MySQL Shell (`mysqlsh --defaults-file=<0600> --js -- util check-for-server-upgrade --target-version=... --output-format=JSON`) when installed and the pair is in range (server older than the Shell, target at most the Shell's major.minor; otherwise exit 2 with a `--no-shell` hint); `parse_upgrade_json` reads `errorCount`/`warningCount`/`noticeCount` and `checksPerformed` (the Shell exits 0 with errors). `--no-shell` or no Shell: `_native_upgrade` (`partial: true`, `limitations[]`), every check version gated as source < change <= target: `removed_variables` from `performance_schema.variables_info` excluding COMPILED, DYNAMIC and PERSISTED (error: an unknown option in an option file or on the command line stops mysqld; `my_getopt.cc`), `removed_persisted_variables` (warning: logged and ignored at startup; `persisted_variable.cc`), on 5.7 the running values (warning); `reserved_identifiers` over schema, table, view, column, routine, trigger and event names with `reserved_words(source, target, source_reserved)` from `RESERVED_SINCE` minus the source's own `information_schema.KEYWORDS` (parity test against KEYWORDS read live on 8.0.46, 8.4.11, 9.7.2); `partition_engines` (5.7 sources only, joins TABLES for ENGINE); `utf8mb3` and `zero_date_defaults` in user schemas (notice from 8.0 sources, warning from 5.7); `native_password_users` (`'user'@'host'`, warning for 8.4, error for 9.x). MariaDB: `method: mariadb_upgrade_guidance`, never invokes the Shell. Counts are affected objects per severity; `--fail-on` exits 1. Read-only: endpoint `POST /api/lifecycle/upgrade-check` (`connection`, `target_version`, `prefer_shell`). HTTP `config_path` is rejected before connecting, also on `/api/lifecycle/prepare`; only the CLI accepts `--config-path`.
- Tools: `detect_tools` (`lifecycle-tools-1.0`; PATH then `COMMON_BIN_DIRS`, `--version` in an isolated HOME, engine from the banner, `mysqlpump` listed as deprecated and never offered); `offered_tools(server, tools)`. Option files (`_credentials`): `[client]`, every value quoted, 0600 in a 0700 temporary directory, first on argv, removed in `finally`; environment allowlisted (`_clean_env`, no `MYSQL_PWD`). SQL and Shell scripts go on stdin. Endpoint `POST /api/lifecycle/tools` (with a `connection`, also `server` and `offered`).
- Plans (`lifecycle-plan-1.0`: `operation, request, server, connection_key, tool, target, commands[], checks[], warnings[], ready, binding`) come from `prepare_dump`, `prepare_restore`, `prepare_clone`; endpoint `POST /api/lifecycle/prepare` with `operation` dump|restore|clone (reads only; the Shell's `dryRun` runs here). Dump dry runs use a private temporary destination, and restore dry runs use a private temporary progress file, both removed afterwards; they cannot create the requested output directory or modify the saved load progress before confirmation. A plan is never authority: `_write_job` rebuilds it on a fresh connection and refuses (nothing sent) unless binding, target and commands match, every check passed, the server identity is unchanged and the typed target equals `plan.target`.
- Output directories (`_output_path`, `_empty_directory`): parent links resolved once and bound in the plan (macOS `/tmp`, `/var`), the directory itself must not be a link, a parent swapped for a link after the preflight refuses; absent or empty only. Dump writes `myflames-manifest.json` (`lifecycle-manifest-1.0`: files with SHA-256, exact `COUNT(*)` of up to 3 tables under 1 MiB, the object catalog, `complete`); `read_manifest` re-hashes every file before a restore, capped by `MAX_RESTORE_FILES` (10,000), `MAX_RESTORE_BYTES` (10 GiB), and `MAX_RESTORE_DIRECTORIES` (10,000). Exceeding a limit refuses preflight with the limit named. Bytes are counted while reading as well as checked against file size, so growth during hashing also refuses. Confirmed dump manifest creation is not capped by these restore preflight limits; larger backups require another restore tool.
- Restore: API and direct restore callers reject a mismatched typed target synchronously, before creating or registering a job or opening its connection; the API checks before saved-password lookup. The CLI checks before job creation. A mismatch says `Nothing was restored: the typed target does not match <target>.`, is audited as unsent, and never carries a partial-restore warning. Policy checks retain precedence, and all server identity and preflight rechecks still run for a matching target. Shell dumps through `util.loadDump` (`local_infile=ON` is a failed check, never set; progress file beside the dump; `--resume`; `--existing ignore|drop`), SQL dumps through the client with `--binary-mode --comments` reading a staged copy whose hash is checked again. Verification: object presence and the recorded row counts (`passed`, `failed`, `not_checked`; a successful child process is never evidence).
- CLONE (MySQL only): `clone_preflight` (engine, version series, plugin on both, BACKUP_ADMIN donor, CLONE_ADMIN recipient or BACKUP_ADMIN with DATA DIRECTORY, platform and character set variables, collations, plugins, `max_allowed_packet`, `clone_valid_donor_list` containing the donor address, different servers; DATA DIRECTORY absolute and not under `@@datadir` as text, existence `not_checked` without a recipient-host probe because the server refuses an existing or in-datadir path before copying). `donor_address` (`--donor-address`) is the donor as the recipient reaches it, used in the statement and the donor-list check. The statement goes on the client's stdin (`clone_statement(..., redact=False)`), shown and audited redacted. Clone plans add `expect` with `donor` and `recipient` objects (`endpoint`, `connection_key`, `type`, `read_only`), `donor_address` and `require_ssl`, all included in the binding. The workspace evaluates both saved profiles with `dbops.connection_safety`; a read-only donor is allowed. Before sending a remembered donor password, it verifies negotiated TLS on the recipient connection. Start checks the reviewed pair before retrieving the saved donor password, and rejects TLS downgrades when the donor profile requires TLS. Both passwords are scrubbed from errors, audit records, logs and manifest free text. Progress: a second connection polls `performance_schema.clone_progress` (job `progress` percent and `detail`). In place: ERROR 3707 (no supervisor, the server shuts down) or a lost connection is expected; the job waits up to `restart_wait` (`--restart-wait`, default `RESTART_WAIT` 180 s) and then reads `clone_status`, tied to this clone by `BEGIN_TIME`, `SOURCE` and `DESTINATION` (the table keeps the previous clone's row); result `needs_restart` when it does not come back, `needs_verification` when the recipient account is refused (it now has the donor's accounts). GTID: donor `gtid_executed` at preflight against `clone_status.GTID_EXECUTED` (empty for DATA DIRECTORY clones, `not_checked`).
- Jobs (`LifecycleJob`, `lifecycle-job-1.0`: `id, operation, state queued|running|done|failed|refused|cancelled, progress, detail, events[] (last 200), result {status, verification[], partial_state, cancel_error, audit_id}, error`): tool output drained on threads, CR progress parsed, ANSI colors stripped, secrets redacted. Cancel: SIGTERM then SIGKILL to the process group (EPERM from a zombie group tolerated), and `KILL QUERY` on the session the job printed on its own stdout (marker line; mysqlsh's `print` needs an explicit line break). `partial_state` names what may remain. `LifecycleJobs` (the workspace's registry: two running at most, never two on one target, finished ones kept an hour, closed with the server).
- Writes: dump, restore and clone are `risky` for `dbops.decide` (a read-only connection refuses, Production and Development confirm). CLI: policy from `--profile` before any prompt or connection; plan and commands on stderr; dump asks `[y/N]`, restore and clone ask to type the target; non-TTY needs `--yes` (plus `--confirm-target TARGET` for restore and clone); `--preflight` prints the plan (exit 0 ready, 1 not); Ctrl+C cancels the job. Exit 0 completed, 1 a started job failed, was cancelled or was not verified, 2 an unsent refusal, decline, bad input or connection failure. UI: `POST /api/lifecycle/start` (`operation`, `plan`, `typed_target`, `confirmed: true`, `connection`, and `donor` for clone) is in `ui._WRITE_PATHS`: `dbops.gate` first (refused envelope with `audit_id`), then saved passwords (`connection.profile_id`, `donor.profile_id`), then the job; `POST /api/lifecycle/status` and `/api/lifecycle/cancel` take `id` and `connection`. Every attempt has an `AuditTrail` record (`surface` cli or ui; refused, declined, `unknown` after a cancel, `ok` or `error`). `POST /api/lifecycle/jobs` (`connection`; `lifecycle-jobs-1.0`, `_lifecycle_jobs_response`) lists the jobs this workspace holds for the connection's endpoint (`dbops.identity_endpoint`, recorded by `_lifecycle_add` before the job starts and dropped when `LifecycleJobs` lets the job go); it opens no connection and starts nothing (`test/test_lifecycle_cli.py`). Status and cancel check this same owner before looking up or cancelling a job and return “No such job.” for missing, expired or mismatched jobs. They open no connection and read no saved password. A change to type or read-only preserves access to the original job.
- Workspace UI (`Maintenance.tsx`, Server > Setup): Tools, Upgrade check (MySQL Shell, or the labeled partial check), Dump, Restore, Clone; Run preflight only reads and opens the confirmation only when `ready` (target, exact commands, warnings, checks; restore and clone type the target; stale after `PREFLIGHT_TTL_MS`, 60 s); Cancel and Escape call `audit/declined`. Frontend callers must send the original `connection` on every status and cancel request, and show both `plan.expect.donor.endpoint` and `plan.expect.recipient.endpoint` with their policy when confirming clone. Jobs are polled while they run and bound to their connection (greyed after a switch). On open, and after a Start whose reply failed, the view reads `lifecycle/jobs` and adopts what it finds, so a reload or a lost reply never sends Start twice. The session keeps only `slow.maintenance_section`; the donor password stays in the page. Browser suite `apps/workspace/test/maintenance.mjs`.
- Not wired to the CLI or UI yet: `prepare_physical_backup` / `physical_backup_job` (MariaDB `mariadb-backup --backup` then `--prepare`, only with a matching local socket and an explicit same-host confirmation); `clone` on MariaDB prints the `mariadb-backup` steps instead.

### Replication: status, skip, skip-loop

Backend `myflames/replication.py` (contract and evidence: the replication skill and the module docstring), CLI `cmd_replication` in `myflames/workspace_cli.py`, endpoints in `ui.py` (`_replication_*`). Tests: `test/test_replication.py` with live replays `test/fixtures/perf/replication-*.json` (MySQL 8.4.11 with gtid_mode ON, four workers and a second channel `ch80`; MySQL 8.4.11 file and position; MySQL 8.0.46; MariaDB 11.4.13 with a named connection `src1` and the default one; recorded 2026-10-15). Each `*-error` fixture is the status after a duplicate key conflict and the matching `*-skip` is skip_one's re-read, script and read after; `mysql-8.0.21-slave-*` is the 8.0.46 replica answering `SHOW SLAVE STATUS` with the version answer rewritten to 8.0.21 (the naming before 8.0.22 cannot be produced by a current 8.0).

- `python3 -m myflames replication [--channel NAME] [--json] [-o PATH] [-q]` (`replication-1.0`, `read_status`): reads only, tag `/*myflames:replication*/`. `SHOW REPLICA STATUS` (MySQL 8.0.22+), `SHOW SLAVE STATUS` (older), `SHOW ALL SLAVES STATUS` (MariaDB), read by column name from the client's `--xml` output (`perf_common.Reader.named`, shared with `server_checks`). MySQL with Performance Schema on adds `replication_connection_configuration` (heartbeat interval), `replication_applier_configuration` (`ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS_TYPE`, 8.0.23+), `replication_connection_status` (heartbeats and the last queued transaction) and `replication_applier_status_by_worker` (worker errors, applying and last applied transactions; zero timestamps become null). For a channel whose applier stopped on an error, `SHOW RELAYLOG EVENTS ... FROM <Relay_Log_Pos>` (MySQL needs REPLICATION SLAVE, MariaDB SLAVE MONITOR) shows the transaction at the applier's position (`channel.relay`: gtid, end_log_pos, tables, complete) and the channel carries `skip_plan`. Envelope: `schema_version, engine, server_version, captured_at, uptime_s, available, message, replica, statement, gtid {mode, enforce_consistency | slave_pos, current_pos, *_domains, domain_id, strict_mode}, settings, channels[] {name, kind replication|group_replication, judged, source {host, port, uuid, server_id}, io, sql {running, state, error {errno, message, timestamp, table, end_log_pos}, delay_s}, lag_s, lag_notes, lag_signals, position, worker, agat, failing_gtid, relay, status, status_why, skip_plan}, capabilities, notes, summary`. Statuses: problem when a thread is stopped or reports an error, watch while connecting, when lag minus SQL_Delay passes `LAG_WATCH_S` (60 s), or when nothing (no event, no heartbeat) arrived for twice the heartbeat interval (at least 60 s); unknown when the lag is NULL with both threads running. Group Replication channels are listed, not judged. No privilege: `available: false` (CLI exit 2). `--channel` that does not exist exits 2.
- Skip plans (`plan_skip`): MySQL with a GTID for the failed transaction, gtid_mode not OFF and no AGAT: `SET GTID_NEXT = '<gtid>'; BEGIN; COMMIT; SET GTID_NEXT = 'AUTOMATIC'; START REPLICA SQL_THREAD FOR CHANNEL '<name>'` (`empty_gtid_transaction`; the GTID comes from the error text, the failing worker's text or APPLYING_TRANSACTION, or the relay log). Anonymous transactions or AGAT: `SET GLOBAL sql_replica_skip_counter = 1; START REPLICA SQL_THREAD FOR CHANNEL '<name>'` (`sql_slave_skip_counter` before 8.0.26, `START SLAVE` before 8.0.22), refused when the relay log shows the transaction at the applier's position is not the one that failed. MariaDB: `SET default_master_connection = '<name>'; SET GLOBAL sql_slave_skip_counter = 1; START SLAVE '<name>' SQL_THREAD`; with GTID, `slave_parallel_threads > 0`, a parallel mode and more than one domain in gtid_slave_pos the plan is `available: false` (the server answers 1966; setting gtid_slave_pos is explained, never automated). `identity` (channel, method, error number, message and time, GTID, relay and applied positions, source host, port and UUID) is what the person confirms. Error 3030 (`WORKER_STOPPED_BY_EARLIER`, ER_REPLICA_WORKER_STOPPED_PREVIOUS_THD_ERROR: with replica_preserve_commit_order a worker stopped and rolled back because an earlier transaction failed) is never the transaction skipped: `effective_error` takes the worker that failed first, the plan's `error` and GTID are its, the identity keeps the status as it reads, and the plan is `available: false` when only 3030 workers are visible; the skip loop compares the selected codes with the same error on every read, including both guards inside `skip_one` immediately before sending. If the underlying failure disappears and only 3030 remains, the loop refuses because it cannot tell which transaction to skip. All identity, policy, configuration, restart, receiver and progress guards remain active (`mysql-8.4-mta-3030-error`, recorded live: worker 1 1062 on :15, worker 2 3030 on :16, the status reporting the 3030).
- `skip_one(conn, channel, expect, audit, decision, confirmed, statements)` (`replication-skip-1.0`): policy first (`dbops.gate`; every skip script is risky, so Production and Development confirm and a read-only connection refuses before any SQL), then a fresh read and plan; refuses (nothing sent) when the channel is gone, has no error, has no safe method, the applier runs again, any identity field changed, or the statements differ from the confirmed ones. The script is sent as one client call, each statement tagged `/*myflames:replication-skip*/` on its own line, with `dbops.WRITE_CLIENT_FLAGS`. Server errors become statuses (`denied` 1044/1045/1095/1142/1227, `refused_by_server` 1198/1966/3086/1781/1782/1790 and other setting errors, `failed`), naming which statements ran before the failing line; a lost connection raises and the audit record is `unknown`. After `SETTLE_S` the channel is read again: `after.passed` checks gtid_executed (GTID method), the domain in the global gtid_slave_pos (MariaDB; the `Gtid_Slave_Pos` column is gtid_current_pos) or the applied position against the transaction's end; a leftover Skip_Counter is reported. Audit operation `replication_skip`.
- `python3 -m myflames replication skip --channel NAME [--yes] [--json] [-o PATH] [-q]`: policy from `--profile` before any prompt or connection, plan and drift warning on stderr, `[y/N]`, non-TTY needs `--yes`. Exit 0 skipped, 1 a sent skip was denied, failed or was not verified, 2 an unsent refusal, decline, bad input or connection failure. `--channel ''` is the default channel.
- `SkipLoop` (`replication-skip-loop-1.0`): selected error codes only (none preselected), `max_skips` 1 to 1000 (CLI default 10), optional duration, check interval 1 to 60 s with the wait doubling per consecutive skip up to 60 s. Stops at once (`STOP_REASONS`, `needs_attention`): user stop (wakes a waiting loop), cap, duration, an unselected error, the same transaction failing again, the applier stopped without an error, channel gone or now Group Replication, source host/port/UUID, gtid_mode, AGAT or skip method changed, Uptime went down or the boot-time interval estimated from Uptime and the monotonic clock changed, lost connection, a refused, denied or failed skip. Receiver reconnect monitoring is optional at startup: readable, enabled stage counters capture an epoch and a changed epoch stops with `receiver_reconnected`. If those counters become disabled, absent or unreadable after being enabled, stop with `receiver_unverified`; never silently drop an active guard. When instrumentation is off, absent or denied at startup, or Performance Schema is off, the loop runs with the other guards and keeps reconnect monitoring off for that run. Loop snapshots carry `receiver_guard` (`"on"` or `"off"`), `receiver_guard_reason` (string or null), `receiver_guard_instrument` (exact stage name, null before the engine is known), and `receiver_guard_note` (coverage and enable guidance). `Start` reads the initial receiver identity on the loop's own connection before returning, bounded by the existing connection/query timeouts, so its first snapshot already reports actual coverage and the instrument; pending initialization is never reported as disabled instrumentation. `receiver_guard_plan(conn, report)` reads this coverage for a confirmation without changing instrumentation; the workspace replication response includes these fields at the report level and on each `skip_plan`. The confirmation and running status should display the note and reason. Echo `receiver_guard` to `skip-loop/start`: `"on"` requires coverage from the first loop read, so losing it after confirmation also stops. `replication_receiver` in Instruments maps `events_stages_summary_by_thread_by_event_name`, `threads`, `global_instrumentation`, `thread_instrumentation` and the engine-specific `stage/sql/Checking source version` (MySQL) or `stage/sql/Checking master version` (MariaDB). Both stages are off by default; counts suffice without timing, and receiver background threads do not depend on `setup_actors`. myflames never enables instruments. Each skip goes through `skip_one` with the loop's decision and is one audit record (`replication_skip_loop`). Workspace loops reload the profile from the store before every skip and recheck at the send boundary. A changed policy, deleted profile or moved endpoint stops with `policy_changed`; editing a profile to read-only or deleting it also signals its registered loops immediately. `WorkspaceServer.server_close` stops and joins all loops. Status and Stop remain usable after a policy change. In memory only: nothing resumes after a restart.
- `python3 -m myflames replication skip-loop --channel NAME --errors 1062,1032 [--max N] [--max-minutes M] [--interval S] [--yes] [--json] [-o PATH] [-q]`: runs in the foreground and prints each live event on stderr (suppressed by `-q`); writes the final JSON snapshot of a started loop, or the final message without `--json` to stdout or `-o`, with identical payload bytes for either destination; Ctrl+C stops it. Exit 0 stopped by Ctrl+C, `--max` or `--max-minutes`, 1 a started loop stopped on a condition that needs attention, 2 an unsent refusal, decline, bad input or connection failure (also a lost connection).
- Endpoints: `POST /api/live/replication` (status; `_CONNECTION_PATHS`, saved-password helper), `POST /api/live/replication/skip` (`channel`, `expect`, `statements`, `confirmed: true`; `_WRITE_PATHS`: policy, then the saved password, then `skip_one`), `POST /api/live/replication/skip-loop/start` (`channel`, `codes`, `max_skips`, `max_minutes`, `poll_s`, `confirmed: true`; same order; the loop keeps its own connection and closes it when it stops), `.../skip-loop/status` (`job_id` or every loop of this connection) and `.../skip-loop/stop` (`job_id`). Loops live in `workspace.replication_loops` (`SkipLoopRegistry`) keyed by `dbops.connection_identity` and matched by its endpoint (`dbops.identity_endpoint`: saved connection id, host, port, user, TLS), so status and stop from another connection are refused while a change of the connection's type or read-only flag keeps them (one loop per channel and endpoint); status and stop open no connection. A refused or unconfirmed request is audited.
- Workspace UI (`Replication.tsx`, Server > Workload): the channel table and inspector; Skip this transaction and Skip until stopped re-read the failure before their sheets open, show the tagged statements, plan again (nothing sent) when the identity or statements changed or the sheet is older than `PLAN_TTL_MS` (60 s), and call `audit/declined` on Cancel and Escape. Loop status is read once a replica report is shown, while a loop runs, and after Start or Stop; a running loop's line (with Stop) also sits above other Server views. The session keeps only `slow.replication_loop_active`, so a restored workspace mounts Replication, reads loop status, and says "Stopped when myflames restarted" when the registry is empty; it never starts a loop. Both views take `missingPrivileges` (`opsApi.MissingPrivileges`, "Requires CLONE_ADMIN") to grey an action with its reason once a privilege context passes it; absent means unknown and the server decides. Browser suite `apps/workspace/test/replication.mjs`.

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
- `POST /api/accounts/plan {connection, operation, user, host, role?, ...}` (`account-plan-1.0`): reads only. `operation` create, drop, rename (`new_user` or `new_name`, `new_host`), lock, unlock, expire, password (`password` or `random_password`, MySQL `current_password` for REPLACE), grant and revoke (`privileges[]`, `level` global|database|table|column|routine|proxy, `database`, `table`, `columns[]`, `routine`, `kind`, `pattern`, `proxy {name, host}`, `grant_option`), roles (`mode` grant|revoke|default, `roles[] {name, host}`, `admin_option`, `special` ALL|NONE), options (`limits`, `expire`, `password_options`). `password` is accepted only for create and password and never echoed. Response: `schema_version, plan_id (= change_set_id), operation, request (the non-secret request), name, host, role, engine, version, account, action, before (account-1.0), expect, connection_identity, actor, actor_server, statements[] (passwords as '[redacted]'), warnings[], guards {blocked[], typed_name, self_lockout, protected, after_management}, privileges (the can() answer for the actor), effects {sessions[], definers[], warnings[]} (drop, rename), dependencies[] (granted roles), generates_password, classification, policy {action, tone, title, text, connection_type, read_only}, status ready|blocked`. Plans live on the workspace server as `_account_plans` (request and redacted statements only, no secret; 30 minutes, 200 at most per server, single use). Non-server callers of the plan helpers use a separate fallback store. Apply claims the plan atomically before opening a connection; a missing typed name restores it with its original expiry. Policy refusals leave it usable.
- `POST /api/accounts/apply {connection, plan_id, expect, confirmed, typed_name?, password?, current_password?}` (`account-change-1.0`, in `ui._WRITE_PATHS`): `dbops.gate` on the connection's policy first (account changes are risky: a read-only connection refuses and nothing is opened, Production and Development need `confirmed: true`; the refusal is audited and returned with `audit_id`), then the saved password, then a fresh account fingerprint is compared with both `expect` and the reviewed fingerprint before rebuilding the plan. A dropped or newly created account, or revoked privileges that invalidate planning, returns `status: stale` with audit `status: refused, outcome: stale`. Drift during a failed rebuild is checked again; unchanged invalid input remains an error. Rebuilt statements must equal the reviewed ones; `accounts.apply` then re-checks the connection identity, server version and identity, actor (`read_privileges`), every granted role's fingerprint, the account against `expect`, the rename destination, the guards and the privilege answer, and sends each statement with `connector.run_stdin` (stdin, `dbops.WRITE_CLIENT_FLAGS`, `--skip-reconnect`, empty temporary HOME, allowlisted environment), stopping at the first error. Response: `schema_version, change_set_id, operation, account, status, message, statements[] {index, statement, status ok|denied|refused_by_server|interrupted|unknown|not_sent, error, message}, before, after, diff {added[], removed[], levels, attributes_before/after, defaults_before/after, exists_before/after}, warnings[], audit_ids[], generated_password (once, MySQL RANDOM PASSWORD or the MariaDB generated one)`. Statuses: `applied`, `partial` (a later statement failed; a MariaDB create that stopped at SET PASSWORD leaves the account locked), `refused`, `needs_confirmation`, `needs_typed_name` (the plan stays usable), `stale`, `denied`, `refused_by_server`, `interrupted`, `unknown`. One `AuditTrail` record per statement (operation `accounts.<operation>`, `target.change_set_id`); refusals one finished record; a cancelled sheet calls `/api/audit/declined`. Server text is kept only for errors that never quote the statement (`accounts._SAFE_ERROR_TEXT`), redacted; others by number.
- Workspace batch review (`Accounts.tsx`, `accountsApi.ts`): compact compatible edits into groups and plan every group sequentially, retaining errors beside their group. One sheet lists every exact statement, warning and guard; an error or blocked plan disables Apply until fixed or removed. Typed-name fields are deduplicated by account. Apply re-plans each group immediately before its one-shot apply, using that fresh plan's `plan_id` and `expect`; it re-checks the connection before planning and sending. New statements, warnings or typed/self-lockout guards stop the batch for another review. Stop at the first refusal, partial result, failure or lost response; show every group's applied/failed/not-sent status, then refresh the list, selected account and privilege model. Earlier groups are not rolled back. Cancel, Escape and removal each post one declined audit per planned group; backend apply retains per-statement audit records. Browser suite `accounts.mjs` and its `--unit` adapter tests cover batch ordering, fresh fingerprints, per-group errors, cancel audits and stopping.

- Statements (one account, one level, one role per statement; tag `/*myflames:accounts*/`): names backquoted with doubled backquotes (MariaDB roles without host, `PUBLIC` bare); database grants escape `_`, `%` and `\` unless `pattern` (MySQL with `partial_revokes` ON matches literally and refuses patterns); privileges from the allowlist per engine, version and level (MySQL dynamic names from `SHOW PRIVILEGES`); passwords are doubled-quote literals inside the statement's own `NO_BACKSLASH_ESCAPES` session (`_with_password_mode`, restored after; verified live with a password ending in a backslash on all five servers with the mysql 8.0 client), NUL and line breaks refused. MySQL: `CREATE USER a IDENTIFIED BY 'x' | RANDOM PASSWORD [ACCOUNT LOCK]`, `ALTER USER a IDENTIFIED BY ... [REPLACE 'current']` (binary log: the hash, checked live). MariaDB New user: `CREATE USER a ACCOUNT LOCK`, `SET PASSWORD FOR a = PASSWORD('x')` (binary log: the hash), `ALTER USER a ACCOUNT UNLOCK`; with an active `PASSWORD VALIDATION` plugin (`information_schema.PLUGINS`) the first step is `CREATE USER a IDENTIFIED BY '<32-character random>' ACCOUNT LOCK` because the plugin refuses an empty password with 1819 (live on 10.11 and 11.4 with simple_password_check: the binary log shows only the temporary password, the typed one is validated by SET PASSWORD, a weak one leaves the account locked). Every MariaDB password plan warns that only the binary log is protected (the general and slow logs keep the typed text, checked live). Default roles: MySQL `ALTER USER a DEFAULT ROLE ...`, MariaDB `SET DEFAULT ROLE r FOR a` (one role or NONE).
- Guards (`accounts._guards`): system accounts are read-only; unparsed target grants block; MySQL blocks a target with a direct SYSTEM_USER grant when the actor lacks it (live: 1227; a SYSTEM_USER only through a role does not block, live: allowed). The typed name (`typed_name`, CLI `--confirm-name`) is required for drop, rename, lock and admin revokes of protected accounts (root, SYSTEM_USER, ALL, SUPER, CREATE USER or GRANT OPTION on `*.*`, roles included) and for any self-lockout (drop, rename, lock or expire your own account, or a revoke after which your own account no longer passes `create_user`, `account_options`, `drop_user` or `grant_role`). Drop and rename list sessions and DEFINER objects with the engine's consequence (MySQL 4006 unless ALLOW_NONEXISTENT_DEFINER; MariaDB 1449 through 11.8, 1446 from 12.0.1, 1045 for view callers without SET USER or SUPER, seen live on 10.11 and 11.4).
- `myflames accounts create|drop|rename|lock|unlock|expire|password|grant|revoke|role ...`: policy from `--profile` before any prompt or connection; the new password from a double prompt or `--password-stdin` (first line; `--current-password` reads the second); the plan on stderr, `[y/N]` or `--yes` (non-TTY without `--yes`: refused, exit 2, audited); `--confirm-name` or a prompt for the typed name. Exit 0 applied, 1 a sent change was denied, refused by the server or partial, 2 an unsent refusal or decline, bad input, connection failure or unknown outcome. `--json` prints account-change-1.0 for executed changes; unsent failures print only a stderr diagnostic and leave stdout and the output file untouched. All account changes accept `-o/--output PATH` for text or JSON, and `-q` suppresses file-write notes and, with `--yes`, the plan description. Ctrl+C or EOF at a credential or confirmation prompt is audited as declined and exits 2. `accounts role --help` lists grant, revoke and default; each action has its own `--help`.
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


### Database diagram

The view is **Server > Schema > Database diagram**. Frontend implementation is
`SchemaDiagram.tsx`, `schemaDiagramModel.ts`, and `schema-diagram.css`; the helper
name avoids a case-insensitive filesystem collision with the component.
`test/schema-diagram.mjs` covers browser behavior; `npm run test:diagram` is its
workspace script.

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
schema. `POST /api/schema-diagram/import {format, text}` parses SQL, Mermaid, or
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
aliases, attributes and symbolic cardinalities. Conceptual relationships never
fabricate executable SQL foreign keys. SQL export revalidates retained original
DDL; without reusable DDL it emits a structural subset with warnings. Mermaid
export reports details that its format cannot retain. Nullable attribute types use Mermaid 11.16 or newer (`?` suffix). JSON keeps the model and
layout without claiming SQL round-trip fidelity.

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
session restore. Refresh all reloads only loaded live diagrams for their
original database and preserves positions; imported diagrams are excluded.

### JSON and expected failures

`--json` selects the result format. Expected failures before a result exists
(bad input, missing files, connection setup failures) exit 2 with a diagnostic
on stderr and empty stdout, including under `--quiet`. They emit no JSON error
object and leave the `-o` destination untouched. Plan commands and workspace
server commands share `cli._fail` for these failures; argparse usage errors
also exit 2 on stderr. Scripts must check the exit code before parsing a result.

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

- **MCP server** (agent tools): `myflames-mcp` (optional extra: `pip install myflames[mcp]`, constrained to `mcp>=1.2,<2` for the verified `mcp.server.fastmcp.FastMCP` API). MCP 2.x is not supported by this transport. Import failures report the installed SDK version (or not installed; unknown when version metadata is unavailable on Python 3.7), the original import error and the install command. Tool imports stay lazy and the core needs no SDK. Exposes `analyze_plan`, `digest_plan`, `compare_plans`, `explain_optimizer_switch`, `explain_query`. Tool logic in `myflames/mcp_server.py` is stdlib + tested; only the transport needs the extra. Register: `claude mcp add myflames -- myflames-mcp`. `analyze_plan` defaults to `detail="core"` (token-cheap decision-grade sidecar); `detail="full"` restores the complete record (adds `collected`/`query`/`teach_hooks`). The `detail` gate lives on `build_sidecar` in `output_sidecar.py` (single source of truth, shared with the CLI `--sidecar` path, which stays `full`); the digest path (`digest_plan_tool`) also stays `full`.

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

Core projections reuse `report.format_digest` and the compact text formatters in Sessions, Locks, Health, InnoDB and Replication. Statements retain normalized SQL, counters, coverage, and table-match caveats while omitting sample literals; Privileges retains credential-stripped grants, session scope, roles and grouped decision states. Reports can exceed 500 tokens: their size follows the observations and findings, without a fixed truncation cap. Full preserves the module's schema and evidence (including suggested SQL as data).

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
  include run statistics and context, never connection credentials. `-e SQL` or
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
