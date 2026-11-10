# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Adds multiple cursors, automatic closing of brackets and quotes, and comment and line editing shortcuts to the Query editor. Each tab keeps its own undo history, and **Query editor shortcuts** lists the available keys.
- Adds suggestions for the connected server's keywords and functions, schemas, tables, columns, aliases, CTE names and system variables. Function suggestions insert parentheses, and a hint inside a known call shows its argument count when known and the current argument number.
- Adds details on hover for tables and columns, plus warnings for `UPDATE` or `DELETE` without `WHERE` and names missing from complete metadata lists already read.

### Improvements

- **Settings > Query editor** offers **Multiple cursors**, **Suggestions while typing**, **Close brackets and quotes**, **Wrap long lines**, **Problem warnings** and **Details on hover**, kept on this computer and applied at once.

### Security fixes

- Fixed an issue where a statement with many leading comments could freeze an open myflames window when it appeared in Sessions or the Query editor.
- Fixed an issue where a slow regular expression could freeze Find in the Query editor. Searches that take too long stop with **Pattern too slow to search**.
- Completion suggestion labels and details isolate right-to-left text, so a server-controlled name containing right-to-left characters cannot reorder the suggestion list.
