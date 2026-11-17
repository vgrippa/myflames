# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Adds **Group connections** in the connection picker, opening a connection group under the strictest safety of its members with a **Server:** picker in single-server places and the Query editor that starts on the primary or source and names the server in every view header and query tab. **Edit group…** in the picker, the status bar and Home's **Connections** place renames the group, adds or removes saved connections, or deletes the group while keeping its saved connections; removing a member moves open views to the primary. In **Cluster**, member actions in a Group connection run through that member's saved connection and name that saved server in the review, or explain which saved connection to add, open or refresh when a member's identity is not confirmed.

### Improvements

- The Cluster, Replication, ProxySQL and Readyset topologies have a new three-dimensional look: the diagram sits on a lit stage, servers float above it, and the group's line lights up only when every member is healthy. The Depth and Pause motion buttons are gone; motion still stops with your system's reduced motion setting, and each place's ⓘ explains what it means.

- **Cluster** now keeps every action for the cluster kind visible on the Cluster overview (**Switch to single-primary…**, **Switch to multi-primary…**, and ClusterSet switchover, failover and rejoin) and in each member's or node's inspector (**Make primary…**, **Stop Group Replication…**, **Start Group Replication…** and **Rejoin to the cluster…**, or on MariaDB Galera **Desync this node…**, **Resync this node…** and **Bootstrap a Primary component…**). Actions that cannot run now stay disabled with the reason and fix in plain words under them, and when the connection's account lacks a privilege they show the exact `GRANT` statement for that account to copy ("Run it as an administrator, then Refresh.").

- **Cluster** now shows InnoDB ClusterSet topology when connected to an InnoDB Cluster read replica, reading the replica's replicated metadata copy and noting that it may be behind the primary cluster. After a Cluster action runs, the before-and-after table states why any value was not reported in plain words instead of saying "Not readable".

### Bug fixes

- Fixed an issue where myflames could fail to open, with **Could not open myflames: The Python backend did not become ready in time.**, on a computer where DNS lookups are slow. The workspace no longer looks up a host name for its local address when it starts.
