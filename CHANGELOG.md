# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### Improvements

- Changes disabled action buttons in **Cluster**, **Readyset**, **Topology** > **Parameters** (**Change on every member…**) and everywhere a missing privilege or policy blocks an action: the reason no longer sits under the button; clicking the button (or pressing Enter or Space) opens a message anchored to it with the reason, how to fix it and, when a privilege is missing, the exact `GRANT` statement to copy. Escape, a click outside or **Close** dismisses the message and returns focus to the button, and screen readers hear the reason as the button's description.
- Secret redaction now skips unneeded variable-inventory, follow-up and REPLACE scans on large SQL scripts and uses faster linear scanning for SET statements and inventory literals, reducing redaction time while ensuring redaction never redacts less.
- Clicking a **Topology** > **Map** server card opens its place (**Server**, **Replication**, **Cluster**, **ProxySQL** or **Readyset**) on its saved connection, with **Back to Topology** returning to the map and **Details** keeping the inspector. Non-active members show `Using <server> from group <group>.` (in **Server** view headers, or the top bar elsewhere), and loaded **Server** views keep the earlier member's report greyed until **Refresh**.

### Bug fixes

- Fixed an issue where **Topology** > **Map** could claim base-table replication for a Readyset upstream from proxied identity alone or swallow an unmatched CDC-only upstream or differing CDC server. Query-only links now say `Upstream (queries)` until CDC evidence confirms them.
- Fixed an issue where **Topology** > **Map** drew a stray `Replica without a reported address` when a MySQL source's replica registration matched Readyset, or invented port `3306` for port `0`. Unmatched host-less registrations still show `Replica without a reported address`, and single-row estimates without dump evidence show `(estimate)`.
- Fixed an issue where **Topology** > **Map** member cards did not name the destination place or restore keyboard focus on **Back to Topology**, and where a long member note in the top bar could squeeze the connection picker at the 720 px window width. Map cards now name the place in their accessible label and tooltip, move focus to the place heading and back to the card on return, and keep their own width in the top bar.
- Fixed an issue where a **Skip until stopped** loop or a running dump, restore or clone on another server of a connection group lost its **Stop** or **Cancel** when you switched servers, and a reload could wrongly report the loop as stopped on the connection in use. The banner now lists running work from every server of the group, **Stop** and **Cancel** act on the server the work runs on, and after a reload myflames checks that server again (the loop itself still never resumes).
- Fixed an issue where a running **Skip until stopped** loop could disappear after a failed status check on reload, or a running dump, restore or clone could disappear when its server left a connection group. Reload now keeps the server's loop tracked and retries its status until it responds, a running maintenance job whose server left the group is named in a status note until the next reload, and **Stop**, **Cancel** and status notes now report accurately when saved connections could not be read or when a server was never in a group.

### Security fixes

### Known issues
