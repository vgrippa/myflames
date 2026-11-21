# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Added a **Topology** place in the sidebar (after **Workload**) and first-class **Group connections** on **Home** (above the **Saved connections** heading, with **New group…**, **Group (N)…** on selected connections, and **Open** or **Refresh** on each group card). On a Group connection, **Topology** reads the whole connection group, one bounded read per tab when first shown and on **Refresh** with no automatic polling: **Map** (interactive diagram and accessible lists with servers, verified, server ID match, address-only and alias links, clusters and outside peers), **Members** (role, state, engine and version, read-only status, lag and transactions or domain sequences behind), and **Parameters**, **Accounts** and **Plugins** (drift matrices with **Differences** and **All** filters, **Per server** tags and **Not compared** reasons, omitting unreadable accounts consistently across every member). Opening a group card from **Home** goes straight to **Topology**, and a saved connection in a group offers to open its Group connection there.
- Added a rolling parameter change to **Topology** > **Parameters**: a row's inspector offers **Change on every member…**, which lists every member of the group (proxies, caches and **Not compared** members stay unticked with their reason), lets you set one value and mode (**This server now (SET GLOBAL)**, **Now and after restart (SET PERSIST)** or **After restart only (SET PERSIST_ONLY)**; MariaDB members run `SET GLOBAL` with the `[mysqld]` option-file line to copy), and refuses `DEFAULT` values, secret variables, values at source length limits, proxies, duplicate router aliases and MariaDB servers without `server_uid` (MariaDB 10.11.9 or 11.4.3 or later required). The review orders members from live topology facts (Group Replication secondaries before primary, Galera and PXC ties by node name, asynchronous replication deepest replicas first with server ID matching named when used), refuses the whole plan if any selected member was refused, and requires typing the group name on a Production group plus acknowledging any excluded members. Apply runs one server at a time, re-checks consent, dependencies, server identity in the same packet and current variable value (including unchanged members), stops at the first failure or unaccepted value, and records one `Rolling parameter change` entry per member in **Settings** > **Audit log** and the parameter's audit history; **Stop** prevents the next member without interrupting a statement already sent and retries on failure, a run banner in **Topology** survives closing the sheet, switching connections or refreshing the tab (**Show** reopens it and a lost apply reply keeps polling progress), and **Parameters** prompts to **Refresh** afterwards without re-reading on its own.

### Bug fixes

- Fixed an issue where **Parameters** and **Compare servers** could compare or allow inline editing of a parameter value cut off at the server's output length limit (1,024 characters or UTF-8 bytes on MySQL; 2,048 or 4,096 on MariaDB). Values at or above the limit are now marked truncated instead of comparing as equal or editing a cut value.
- Fixed an issue where secret system variables (for example `wsrep_sst_auth`) could appear in **Settings** > **Audit log** when a change was declined or refused before sending. Secret variables are now redacted in every audit record, including declined and refused attempts.
- Fixed an issue where credential redaction could slow down on long or incomplete URL-like values. Redaction now handles long URL and `SET PASSWORD` values without slowing the app.
- Fixed an issue where replication source matching could read a source server ID as a source UUID on MySQL before 8.0.22 and MariaDB. Source UUIDs are now accepted only as valid UUIDs while keeping the numeric server ID separate.
- Fixed an issue where saving a connection group that moved a server out of the Group connection in use left the active Group connection with its previous member list. The active Group connection now refreshes automatically.
- Fixed an issue where the **Software update** dialog showed release notes as raw Markdown, with `##` and `**` marks, instead of headings, lists and bold text.
- Fixed an issue where the **Locks** blocking tree scrolled sideways at 1440 px when the interface font is Inter (the Linux fallback, and an option in **Settings** > **Appearance**).
- Fixed an issue where the **Connections** table on **Home** gave its **Name** column none of the spare width at 1440 px when the interface font is Inter (the Linux fallback, and an option in **Settings** > **Appearance**).
- Fixed an issue where **Workload** > **Top statements** scrolled sideways at 1440 px when the interface font is Inter (the Linux fallback, and an option in **Settings** > **Appearance**).
- Fixed an issue where choosing another source in **Import connections** right after opening it could leave the sheet loading, with the **Import from** picker disabled.

### Security fixes

- Fixed an issue where the **Software update** dialog's **View on GitHub** link used the address from the update check without checking it. It now opens only a myflames release page on GitHub.
- Fixed an issue on Windows where another program on the same computer could open the workspace's local port too. The workspace now keeps that port to itself.

### Packaging notes

- Running the workspace from a source checkout now needs `npm ci` and `npm run build` in apps/workspace first; PyPI packages and the desktop app build it during release.
