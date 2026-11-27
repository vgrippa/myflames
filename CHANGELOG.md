# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### Improvements

- **Topology**: clicking a server card on the Map opens that server's place (Server, Replication, Cluster, ProxySQL or Readyset) on its own saved connection, with a note in the top bar naming the server in use and **Back to Topology** returning to the map; the card's **Details** button keeps the on-map inspector.

### Bug fixes

- **Topology map**: the Readyset upstream line no longer claims base-table replication from proxied identity alone, so it says `Upstream (queries)` until CDC evidence confirms the upstream, and an unmatched CDC-only upstream keeps its own line instead of being swallowed. A MySQL source's replica registration is now attributed to the Readyset member whose Binlog Dump session matches instead of drawing a stray `Replica without a reported address`, and a match inferred from a single host-less row with unavailable dump evidence is marked `(estimate)`. A replica that reports port 0 no longer appears with an invented port 3306.
- **Accessibility and layout**: map member cards name the place they open in their accessible name and tooltip, keyboard focus moves to the place heading when a card opens a place and back to the card on **Back to Topology**, and the top bar's member note keeps its own width so it no longer squeezes the connection picker at the 720 px minimum window width.

### Security fixes

### Known issues
