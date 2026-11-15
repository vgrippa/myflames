# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Added mouse-wheel and trackpad zoom to the Database diagram, with a Diagram wheel zoom speed setting in Settings > Workspace for both the diagram and Mermaid preview.
- After a Kill in Locks or Processlist, the note shows what the server confirms: the statement, transaction and rows locked before and after, whether the transaction and its locks are still open, whether each session it blocked still waits, and about how many changes are left to undo while a rollback runs. The note stays amber while anything is still held or cannot be confirmed, turns green only once the locks are released, checks again during a rollback, and keeps Refresh while the result is open. Processlist now shows whether the sessions a killed session blocked still wait.

### Improvements

- Changed Mermaid preview wheel zoom to follow scroll distance, giving small trackpad movements a smoother response.
- **Kill connection** shows an amber warning for an open transaction: its uncommitted changes are undone and lost, the rollback can take a long time, the connection keeps its locks (and the sessions it blocks keep waiting) until the rollback ends, and the application loses its connection and gets an error.
- The Kill sheet starts on **Kill connection** when the session is idle or its statement runs inside a longer transaction, in the blocking tree and for Waiting DDL blockers, and keeps an explicit row-menu choice. **Kill query only** then warns that the transaction and its locks stay open and the blocked sessions keep waiting.

### Bug fixes

- Kill connection is no longer refused while a transaction shows ROLLING BACK after Kill query stopped its statement; that state can last for a large statement, and the transaction stays open with its locks afterwards. Kill query is still refused then.
- **InnoDB Cluster changes through MySQL Shell.** When MySQL Shell (`mysqlsh` 8.0 or later) is installed, the Cluster place offers the InnoDB Cluster changes that only MySQL Shell does in full: switching the cluster to multi-primary or single-primary mode (`Cluster.switchToMultiPrimaryMode()` and `switchToSinglePrimaryMode()`, which also set `auto_increment` on every member and the mode in the metadata), and rejoining a member that is in the metadata but missing from the group (`Cluster.rejoinInstance()`). They are reviewed like the other cluster changes: the exact call is shown, checked again from a fresh read before it is sent, typed where it can lose data, and audited. MySQL Shell gets the account from a private option file, never the command line.
- **InnoDB ClusterSet.** `myflames cluster`, the MCP tool `cluster_status` and the Cluster place show the ClusterSet an InnoDB Cluster belongs to: its clusters, which one is primary, invalidated clusters and, on a replica cluster's primary, the `clusterset_replication` channel, with findings when a cluster is invalidated or the channel stops. The Cluster topology draws the ClusterSet as a band above the group: the primary cluster, an arrow to each replica cluster with the channel's state when this member runs it (a broken arrow when it stops or the cluster is invalidated), and the other clusters' instances from the shared metadata, with Use for a saved connection to one of them. With MySQL Shell installed the place offers the ClusterSet changes: make a replica cluster the primary cluster (`setPrimaryCluster`), fail over to this cluster when the primary cluster is lost (`forcePrimaryCluster`, typed), and rejoin an invalidated cluster (`rejoinCluster`). In a replica cluster, Make primary uses MySQL Shell's `setPrimaryInstance`, because the server refuses the switchover function while the ClusterSet channel runs.
