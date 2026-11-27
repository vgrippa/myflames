# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Added support for VillageSQL 0.0.8+ on its MySQL 8.4, Percona Server 8.4 and MySQL 9.7 codebases. Connections and reports name VillageSQL and its base server, Parameters links VillageSQL settings to its documentation, and Users and roles includes the `EXTENSION_ADMIN` privilege. Percona diagnostics remain available on the Percona codebase. Custom indexes appear under **No data** in Indexes because Performance Schema does not count their reads.
- Added **Server > Setup > Extensions** for VillageSQL connections: a read-only view of installed versions, registered types and functions, protocol status, variables, objects using extension types and pending updates, with CSV and text exports. Missing or truncated reads show their limits instead of reporting zero.
- Added VillageSQL custom-index KNN plans to Visual Explain, flame graphs, execution trees, bar charts and plan findings. The operator recognizes released 0.0.8 and main (0.0.9-dev) formats, shows requested K separately from measured rows, excludes OFFSET from inherited K, bounds query-point previews at 128 characters and leaves search complexity unspecified because the extension chooses the algorithm.

### Improvements

- Changes disabled action buttons in **Cluster**, **Readyset**, **Topology** > **Parameters** (**Change on every member…**) and everywhere a missing privilege or policy blocks an action: the reason no longer sits under the button; clicking the button (or pressing Enter or Space) opens a message anchored to it with the reason, how to fix it and, when a privilege is missing, the exact `GRANT` statement to copy. Escape, a click outside or **Close** dismisses the message and returns focus to the button, and screen readers hear the reason as the button's description.
- Secret redaction now skips unneeded variable-inventory, follow-up and REPLACE scans on large SQL scripts and uses faster linear scanning for SET statements and inventory literals, reducing redaction time while ensuring redaction never redacts less.
- Clicking a **Topology** > **Map** server card opens its place (**Server**, **Replication**, **Cluster**, **ProxySQL** or **Readyset**) on its saved connection, with **Back to Topology** returning to the map and **Details** keeping the inspector. Non-active members show `Using <server> from group <group>.` (in **Server** view headers, or the top bar elsewhere), and loaded **Server** views keep the earlier member's report greyed until **Refresh**.

### Bug fixes

- Clarified why sensitive parameter editors start empty: the current value is hidden, and a replacement must be typed in full.
- Fixed VillageSQL dump selection to use mysqldump, including for whole-instance dumps. When only MySQL Shell is available, myflames explains that mysqldump is needed for custom types. Explicit selection of the internal villagesql schema is refused; restore checks use the target server's schema classification.
- Fixed Maintenance offering upstream MySQL releases and Oracle package guidance as VillageSQL upgrades. VillageSQL now has no suggested target; follow its own 0.0.x releases.
- Fixed database pickers hiding a user-created database named villagesql on MySQL, Percona Server and MariaDB.
- Protected VillageSQL custom indexes from unused-index advice even when they report a built-in type or column metadata is missing. Full-scan rankings keep incomplete user statistics out of their totals.
- Fixed Extensions exports using another connection's report, kept known server identity after partial reads, and clarified that truncated dependency counts remain unknown.
- Fixed an issue where **Topology** > **Map** could claim base-table replication for a Readyset upstream from proxied identity alone or swallow an unmatched CDC-only upstream or differing CDC server. Query-only links now say `Upstream (queries)` until CDC evidence confirms them.
- Fixed an issue where **Topology** > **Map** drew a stray `Replica without a reported address` when a MySQL source's replica registration matched Readyset, or invented port `3306` for port `0`. Unmatched host-less registrations still show `Replica without a reported address`, and single-row estimates without dump evidence show `(estimate)`.
- Fixed an issue where **Topology** > **Map** member cards did not name the destination place or restore keyboard focus on **Back to Topology**, and where a long member note in the top bar could squeeze the connection picker at the 720 px window width. Map cards now name the place in their accessible label and tooltip, move focus to the place heading and back to the card on return, and keep their own width in the top bar.
- Fixed an issue where a **Skip until stopped** loop or a running dump, restore or clone on another server of a connection group lost its **Stop** or **Cancel** when you switched servers, and a reload could wrongly report the loop as stopped on the connection in use. The banner now lists running work from every server of the group, **Stop** and **Cancel** act on the server the work runs on, and after a reload myflames checks that server again (the loop itself still never resumes).
- Fixed an issue where a running **Skip until stopped** loop could disappear after a failed status check on reload, or a running dump, restore or clone could disappear when its server left a connection group. Reload now keeps the server's loop tracked and retries its status until it responds, a running maintenance job whose server left the group is named in a status note until the next reload, and **Stop**, **Cancel** and status notes now report accurately when saved connections could not be read or when a server was never in a group.

### Security fixes

- Fixed a pre-existing exposure of known secret variables in Parameters and the variable inspector, including server passwords, authentication credentials and telemetry headers. Current, default and persisted secret values are now redacted wherever shown.
- Fixed secret variable changes returning values in results, messages and Audit log details. Reviews now detect changes to secret values before applying them; restarting the backend requires a fresh review.
- Fixed shared KNN plans and charts retaining query-point literals or shortened previews when **Hide literal values** was selected.
- Redacted `vsql_rest.jwt_secret`, the vsql-rest signing key, in Parameters, snapshots, comparisons, exports, Extensions, editor history and the Audit log. The TLS key file path and public-key settings remain visible.

### Known issues
