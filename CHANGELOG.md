# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Settings > Appearance now has a Text group: choose the interface font and text size (12 to 16 px) and the code font and text size (11 to 20 px) for the Query editor and all SQL and code text, with a live preview and a Restore default text settings button. The Inter and JetBrains Mono fonts are included with the app, and fonts already installed on the computer are offered too.

### Improvements

- The workspace loads Plans and Learn on demand, reducing the code needed to open the first screen (the entry chunk goes from 508,046 to 479,634 bytes).
- The installed version moved from the window status bar to the top of the sidebar beside the myflames name; choosing it opens **Settings > Updates and news**, which now holds the update status, **Check for updates** and **View releases**. When an update is available, downloading or ready, the badge beside the logo alerts you and names the new version. On the compact icon rail the version text hides and the logo's tooltip names the version.

- The Cluster, Replication, ProxySQL and Readyset topologies have a new three-dimensional look: the diagram sits on a lit stage, servers float above it, and the group's line lights up only when every member is healthy. The Depth and Pause motion buttons are gone; motion still stops with your system's reduced motion setting, and each place's ⓘ explains what it means.
- The Query editor toolbar has **Find** and **Find and replace** buttons, and an **Editing actions** menu lists every editing command with its shortcut, including **Toggle Tab moving focus**.

### Bug fixes

- Fixed Save investigation becoming unavailable when Plans could not load, a delayed notebook taking focus from another dialog, and files dropped into Plans being ignored while it loaded. A chart that cannot load now leaves the rest of Plans available.
- Fixed an issue where the table data editor's review button read **Review 1 changes** for a single change.
- Fixed an issue where myflames could fail to open, with **Could not open myflames: The Python backend did not become ready in time.**, on a computer where DNS lookups are slow. The workspace no longer looks up a host name for its local address when it starts.
