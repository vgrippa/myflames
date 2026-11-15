# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Added mouse-wheel and trackpad zoom to the Database diagram, with a Diagram wheel zoom speed setting in Settings > Workspace for both the diagram and Mermaid preview.
- After a Kill in Locks or Processlist, the note shows what the server confirms: the statement, transaction and rows locked before and after, whether the transaction and its locks are still open, whether each session it blocked still waits, and about how many changes are left to undo while a rollback runs. The note stays amber while anything is still held or cannot be confirmed, turns green only once the locks are released, checks again during a rollback, and keeps Refresh while the result is open. Processlist now shows whether the sessions a killed session blocked still wait.
- Adds reviewed InnoDB Cluster mode switches and incremental-only rejoin in **Cluster** when MySQL Shell is installed. Mode switches need MySQL Shell 8.0 or later and update every member's `auto_increment` settings and the cluster metadata; rejoin needs 8.1 or later.
- Adds an InnoDB ClusterSet view in **Cluster**, showing the primary cluster, replica clusters, invalidated clusters and replication channel health. The topology links clusters and offers **Use** for saved connections to their instances; a replica cluster's metadata copy may be behind.
- Adds reviewed InnoDB ClusterSet switchover, failover to this replica cluster and rejoin of invalidated clusters in **Cluster**, with MySQL Shell 8.0.27 or later installed. Failover requires typed confirmation and loses transactions the old primary committed that this cluster did not receive. Contributed by Acacio L.R ([@acaciolr](https://github.com/acaciolr)) in [#31](https://github.com/vgrippa/myflames/pull/31).

### Improvements

- Changed Mermaid preview wheel zoom to follow scroll distance, giving small trackpad movements a smoother response.
- **Kill connection** shows an amber warning for an open transaction: its uncommitted changes are undone and lost, the rollback can take a long time, the connection keeps its locks (and the sessions it blocks keep waiting) until the rollback ends, and the application loses its connection and gets an error.
- The Kill sheet starts on **Kill connection** when the session is idle or its statement runs inside a longer transaction, in the blocking tree and for Waiting DDL blockers, and keeps an explicit row-menu choice. **Kill query only** then warns that the transaction and its locks stay open and the blocked sessions keep waiting.

### Bug fixes

- Kill connection is no longer refused while a transaction shows ROLLING BACK after Kill query stopped its statement; that state can last for a large statement, and the transaction stays open with its locks afterwards. Kill query is still refused then.
