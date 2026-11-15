# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Adds connection groups (**Group** in **Connections**) so you choose which saved connections **Cluster**, **Replication**, **ProxySQL** and **Readyset** may read together. A group bar above the topology shows which group members were read and offers **Add to <group>**, **Review…** or **Create a group…** when saved connections share a topology, matched without logging in from connections you have opened.

### Bug fixes

- Fixed an issue where **Cluster**, **Replication**, **ProxySQL** and **Readyset** logged in with saved passwords to up to six other saved connections, including production ones you never chose, to find linked ProxySQL or Readyset servers and replication source versions. Reads now open only saved connections in the same **Group** as the connection in use, and notes about saved connections no longer include SQL or raw error text.
- Fixed an issue where **Cluster** and **Replication** never detected ProxySQL or Readyset clients connected to a MySQL or MariaDB server because the `performance_schema` query joined on a column that does not exist. ProxySQL backend connections (`proxysql`) and the Readyset helper connections that send a program name (names starting `READYSET`, such as the shallow refresher, ACL pooler and query sampler) are now detected, while ordinary Readyset upstream and replication connections send no program name so seeing none does not mean Readyset is not connected. When `performance_schema` is off (the MariaDB default), the report says the client footprint is unknown.
