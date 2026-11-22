# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Adds a **Topology** place to the sidebar for Group connections. **Map** draws the group's servers with their replication, cluster and proxy links and marks which links are verified, and **Members** shows each server's role, state, version, read-only status and lag.
- Adds **Parameters**, **Accounts** and **Plugins** tabs to **Topology**. Each lists what differs between the servers of a Group connection, with **Differences** and **All** filters and a reason for anything that could not be compared.
- Adds **New group…** and **Group (N)…** to **Home** to create a connection group from saved connections, and **Open** on each group card, which opens the Group connection in **Topology**.
- Adds **Change on every member…** to **Topology** > **Parameters**. One review covers a parameter change on every server in the group. myflames then applies it one server at a time (replicas and secondaries before the primary), checks each server's identity first, stops at the first failure and writes one audit record per server.

### Bug fixes

- Fixed an issue where **Parameters** and **Compare servers** could compare, or allow inline editing of, a parameter value that the server had cut off at its output length limit. Such values are now marked truncated.
- Fixed an issue where secret system variables (for example `wsrep_sst_auth`) could appear in **Settings** > **Audit log** when a change was declined or refused before sending. Secret variables are now redacted in every audit record, including declined and refused attempts.
- Fixed an issue where replication source matching could read a source server ID as a source UUID on MySQL before 8.0.22 and MariaDB. Source UUIDs are now accepted only as valid UUIDs while keeping the numeric server ID separate.
- Fixed an issue where saving a connection group that moved a server out of the Group connection in use left the active Group connection with its previous member list. The active Group connection now refreshes automatically when members remain, or keeps the active server open as a plain connection when every member moves out.
- Fixed an issue where the **Software update** dialog showed release notes as raw Markdown, with `##` and `**` marks, instead of headings, lists and bold text.
- Fixed an issue where the **Locks** blocking tree scrolled sideways at 1440 px when the interface font is Inter (the Linux fallback, and an option in **Settings** > **Appearance**).
- Fixed an issue where the **Connections** table on **Home** gave its **Name** column none of the spare width at 1440 px when the interface font is Inter (the Linux fallback, and an option in **Settings** > **Appearance**).
- Fixed an issue where **Workload** > **Top statements** scrolled sideways at 1440 px when the interface font is Inter (the Linux fallback, and an option in **Settings** > **Appearance**).
- Fixed an issue where choosing another source in **Import connections** right after opening it could leave the sheet loading, with the **Import from** picker disabled.

### Security fixes

- Fixed an issue where a password in a `SET PASSWORD` statement could stay visible in the **Audit log** and in messages when the account name or a comment contained `=` or `;`.
- Fixed an issue where a very long URL-like value read from a server could freeze the app for seconds while myflames hid the passwords in it.
- Fixed an issue where the **Software update** dialog's **View on GitHub** link used the address from the update check without checking it. It now opens only a myflames release page on GitHub.
- Fixed an issue on Windows where another program on the same computer could open the workspace's local port too. The workspace now keeps that port to itself.

### Packaging notes

- Running the workspace from a source checkout now needs `npm ci` and `npm run build` in apps/workspace first; PyPI packages and the desktop app build it during release.
