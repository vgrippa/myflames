# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Adds **Group connections** in the connection picker, opening a connection group under the strictest safety of its members with a **Server:** picker in single-server places and the Query editor that starts on the primary or source and names the server in every view header and query tab. **Edit group…** in the picker, the status bar and Home's **Connections** place renames the group, adds or removes saved connections, or deletes the group while keeping its saved connections; removing a member moves open views to the primary. In **Cluster**, member actions in a Group connection run through that member's saved connection and name that saved server in the review, or, when no saved connection in the group is confirmed as that member, say why with **Edit group…** beside the reason. **Rejoin to the cluster…** runs through the active ONLINE member with quorum and names the missing instance.

### Improvements

- The Cluster, Replication, ProxySQL and Readyset topologies have a new three-dimensional look: the diagram sits on a lit stage, servers float above it, and the group's line lights up only when every member is healthy. The Depth and Pause motion buttons are gone; motion still stops with your system's reduced motion setting, and each place's ⓘ explains what it means.

- **Cluster** now keeps every action for the cluster kind visible on the Cluster overview (**Switch to single-primary…**, **Switch to multi-primary…**, and ClusterSet switchover, failover and rejoin) and in each member's or node's inspector (**Make primary…**, **Stop Group Replication…**, **Start Group Replication…** and **Rejoin to the cluster…**, or on MariaDB Galera **Desync this node…**, **Resync this node…** and **Bootstrap a Primary component…**). Actions that cannot run now stay disabled with the reason and fix in plain words under them, and when the connection's account lacks a privilege they show the exact `GRANT` statement for that account to copy ("Run it as an administrator, then Refresh.").

- **Cluster** now shows InnoDB ClusterSet topology when connected to an InnoDB Cluster read replica, reading the replica's replicated metadata copy and noting that it may be behind the primary cluster. After a Cluster action runs, the before-and-after table states why any value was not reported in plain words instead of saying "Not readable".

### Security fixes

- In a Group connection, **Users and roles** changes re-check group membership and the group's strictest write policy at every login and right before sending, so a member removed from the group or made Read-only after the review receives nothing; a typed password no longer skips the membership check. Opening a Group connection installs the same per-login check on its member probes. A group deleted and recreated with the same name and members no longer matches an earlier Cluster review. A stored server identity can no longer be overwritten by an older cached probe. A query editor tab whose member left the group can still roll back, stop and close its existing session, so an open transaction does not keep its locks; new statements stay refused.

### Bug fixes

- Rejoining a missing InnoDB Cluster member in a Group connection now uses the active ONLINE member with quorum. Desync and Resync for another Galera node both stay available for review, which checks that node's own state.

- Cluster actions in a Group connection can review a member through a unique matching saved address. Review confirms that connection's server identity before allowing the change, without extra background logins. Group topology reads also remember identities confirmed by existing probes. Query editor tabs refuse runs after their member leaves the group, and refused Cluster actions keep the group's safety type in the audit.

- Fixed an issue where myflames could fail to open, with **Could not open myflames: The Python backend did not become ready in time.**, on a computer where DNS lookups are slow. The workspace no longer looks up a host name for its local address when it starts.

### Security fixes

- Cluster changes through MySQL Shell now verify the server UUID inside the sending session before any cluster operation. Galera identity guards also compare the node UUID, so matching server names, ids and ports cannot authorize a different node.

- Suggested Cluster GRANTs read the account, grants and schema wildcard setting in one login and refuse changed identity snapshots. Schema names containing underscores or percent signs are escaped according to that setting, preventing a suggestion from granting access to other matching schemas.
