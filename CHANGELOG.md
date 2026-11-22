# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Adds a **Topology** place to the sidebar for Group connections. **Map** draws the group's servers with their replication, cluster and proxy links and marks which links are verified, and **Members** shows each server's role, state, version, read-only status and lag.
- Adds **Parameters**, **Accounts** and **Plugins** tabs to **Topology**. Each lists what differs between the servers of a Group connection, with **Differences** and **All** filters and a reason for anything that could not be compared.
- Adds **New group…** and **Group (N)…** to **Home** to create a connection group from saved connections, and **Open** on each group card, which opens the Group connection in **Topology**.
- Adds **Change on every member…** to **Topology** > **Parameters**. One review covers a parameter change on every server in the group. myflames then applies it one server at a time (replicas and secondaries before the primary), checks each server's identity first, stops at the first failure and writes one audit record per server.

### Improvements

- Improves **Readyset**, **ProxySQL**, and the upstream tiers above **Cluster** and **Replication** to share one compact topology layout with Cluster-style cards, tier jump buttons, and detached cards for down active CDC upstreams and problem peers. Upstream server details move into each upstream card's inspector while **Active client connections** stays on **Topology**.
- Improves **Readyset** findings and action controls so findings appear worst first with **Details** toggles and **Show in `<tab>`** links. Every server, row, and allowlist action stays visible with its disabled reason and fix beneath it, and **Drop shallow caches…** and **Drop deep caches…** sit alongside **Drop all caches…**.

### Bug fixes

- Fixed an issue where **Settings**, **Maintenance** and query connections could choose different client tools with repeated PATH entries, a chosen tools folder or a MySQL Shell symlink. Equal-version Shell copies on PATH also keep precedence over chosen-folder copies in **Settings**, **Maintenance** and **Cluster**. Authentication plugin errors also avoid scanning MySQL Shell.
- Fixed an issue where every server read took longer, and a whole **Topology** tab could take minutes, when MySQL Shell was installed on the computer: myflames re-checked every file of the MySQL Shell installation before each query.
- Fixed an issue where loading a database in **Database diagram** could leave keyboard focus on the page instead of the diagram when the computer was busy.
- Fixed an issue where an action could fail with "Private storage changed while it was being opened" when the workspace was saving its data at the same moment, for example right after the app started.
- Fixed an issue where **Parameters** and **Compare servers** could compare, or allow inline editing of, a parameter value that the server had cut off at its output length limit. Such values are now marked truncated.
- Fixed an issue where secret system variables (for example `wsrep_sst_auth`) could appear in **Settings** > **Audit log** when a change was declined or refused before sending. Secret variables are now redacted in every audit record, including declined and refused attempts.
- Fixed an issue where replication source matching could read a source server ID as a source UUID on MySQL before 8.0.22 and MariaDB. Source UUIDs are now accepted only as valid UUIDs while keeping the numeric server ID separate.
- Fixed an issue where saving a connection group that moved a server out of the Group connection in use left the active Group connection with its previous member list. The active Group connection now refreshes automatically when members remain, or keeps the active server open as a plain connection when every member moves out.
- Fixed an issue where the **Software update** dialog showed release notes as raw Markdown, with `##` and `**` marks, instead of headings, lists and bold text.
- Fixed an issue where the **Locks** blocking tree scrolled sideways at 1440 px when the interface font is Inter (the Linux fallback, and an option in **Settings** > **Appearance**).
- Fixed an issue where the **Connections** table on **Home** gave its **Name** column none of the spare width at 1440 px when the interface font is Inter (the Linux fallback, and an option in **Settings** > **Appearance**).
- Fixed an issue where **Workload** > **Top statements** scrolled sideways at 1440 px when the interface font is Inter (the Linux fallback, and an option in **Settings** > **Appearance**).
- Fixed an issue where choosing another source in **Import connections** right after opening it could leave the sheet loading, with the **Import from** picker disabled.
- Fixed an issue where **Readyset** showed **OK**, **Online**, empty lists, zero telemetry counts, or inferred upstream health and inactive CDC roles when status, cache, table, or telemetry reads failed or omitted fields. Unreported states and telemetry now read **Not read**, failed status, cache, and table reads raise findings and disable dependent actions, and zero-traffic tiles show **No traffic yet**.
- Fixed an issue where the **Readyset** **Base replication** tile, CDC link, and upstream **Readyset** tiers could show **Running** or healthy despite a reported replicator error, **Disabled** replication was labelled as CDC replication, and a connecting async replica peer showed **Replication stopped**. Reported errors now govern those states, and **Disabled** and **Connecting** show their own labels.
- Fixed an issue where **Readyset** guessed the SQL adapter or admin listener role from the port number or an unprobed `readyset` database. Listener detection now uses query behavior across status reads and topology probes, showing **Port not read** and disabling **Create cache…**, `EXPLAIN CREATE CACHE`, **Shut down Readyset…**, and **Shut down & reset snapshot…** when unconfirmed.
- Fixed an issue where **ProxySQL** and **Readyset** topologies treated configuration-only rows as runtime health, showed unread **ProxySQL** tiers as healthy, and hid tier link labels from screen readers. In the **ProxySQL** place, offline-hostgroup nodes also showed as healthy. Unread rows now read **Not read**, and stopped nodes in the **ProxySQL** place stay stopped.
- Fixed an issue where **ProxySQL** and **Readyset** topologies labelled address matches as verified and matched servers by address even when the reported server UUID differed. Address matches now read **Matched by address**, and a reported server UUID that differs from a saved server's UUID no longer matches that server by address.
- Fixed an issue where **Readyset** maintenance mode was described as bypassing caches and **Enter maintenance mode…** stayed available while snapshots were running or unread. The finding, **Parameters** row, and review now explain it is an in-memory signal for tools such as **ProxySQL**, and **Enter maintenance mode…** enables only when **Status** is **Online**.
- Fixed an issue where **Readyset** trimmed reported names, allowed dotted cache names in `DROP CACHE` or `FLUSH CACHE`, let backticked names bypass **Create cache** collision or allowlist checks, or skipped default-database and exact-cache review checks. Reported names now stay intact, unusable or control-character names disable only that row, and mismatched targets are refused.
- Fixed an issue where a support check in **Create cache** could apply results to an edited query, and an aborted check could leave the dialog stuck checking. Each check now stays bound to the current draft and connection, discarding stale results and clearing the checking state on abort.
- Fixed an issue where the **Readyset** and **ProxySQL** tiers above **Replication** could be missing for the server in use, because its own port was compared as text.

### Security fixes

- Fixed an issue where a password in a `SET PASSWORD` statement could stay visible in the **Audit log** and in messages when the account name or a comment contained `=` or `;`.
- Fixed an issue where a very long URL-like value read from a server could freeze the app for seconds while myflames hid the passwords in it.
- Fixed an issue where the **Software update** dialog's **View on GitHub** link used the address from the update check without checking it. It now opens only a myflames release page on GitHub.
- Fixed an issue on Windows where another program on the same computer could open the workspace's local port too. The workspace now keeps that port to itself.
- Fixed an issue where a **Readyset** cache export could contain extra statements or `mysql` client commands supplied by the server, which would run when the file was loaded. Only a single `CREATE CACHE` statement now stays runnable; anything else is commented out.
- Fixed an issue where database URL passwords and the connection password could appear unredacted in **Readyset** cached or proxied queries, table errors, cacheability notes, `EXPLAIN CREATE CACHE` results, exported cache definitions, and connection error messages. Those fields are now redacted before display or export, and redacted exported definitions are commented out with a warning.
- Fixed an issue where a password containing `@` in a database URL could stay partly visible in status and error text, query text, and the **Audit log**. URL redaction now hides the whole password up to the final `@` before the host.

### Packaging notes

- Running the workspace from a source checkout now needs `npm ci` and `npm run build` in apps/workspace first; PyPI packages and the desktop app build it during release.
