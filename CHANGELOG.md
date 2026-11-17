# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- A connection group opens as one **Group connection** from the connection picker: the picker lists groups beside the saved connections (name, server count and the strictest safety of the members), and opening one starts on the member the backend picks (the primary or source). The top bar and the status bar name the Group connection, and **Edit group…** (in the picker, in the status bar and on Home's Connections place) opens the one group editor: rename the group, add or remove saved connections, or delete the group (its saved connections stay). Edits apply at once; a save that removes the active member moves the Group connection to the primary.

### Improvements

- The Cluster, Replication, ProxySQL and Readyset topologies have a new three-dimensional look: the diagram sits on a lit stage, servers float above it, and the group's line lights up only when every member is healthy. The Depth and Pause motion buttons are gone; motion still stops with your system's reduced motion setting, and each place's ⓘ explains what it means.

### Bug fixes

- Cluster actions in a Group connection can use the selected member's own saved login through `/api/live/cluster/action`. Reviews name that saved server and bind its endpoint, group and node identity; changing the target after review sends nothing.

- Fixed an issue where myflames could fail to open, with **Could not open myflames: The Python backend did not become ready in time.**, on a computer where DNS lookups are slow. The workspace no longer looks up a host name for its local address when it starts.

- Fixed Cluster reviews whose titles or confirmation labels changed when a member's topology label matched part of the action text. The active member can use a typed password, and recording an unrelated server identity no longer invalidates a Galera review. Restarted nodes with an unconfirmed identity suggest which saved connection to open.

### Security fixes

- Cluster actions refuse ambiguous member identities and name the saved connections that need refreshing. Group consent is checked again before MySQL Shell opens a login. Refusals after review, including group removal or a forgotten saved password, leave an Audit log record with nothing sent.
