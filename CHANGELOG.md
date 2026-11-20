# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### Bug fixes

- Fixed an issue where the **Software update** dialog showed release notes as raw Markdown, with `##` and `**` marks, instead of headings, lists and bold text.

### Security fixes

- Fixed an issue where the **Software update** dialog's **View on GitHub** link used the address from the update check without checking it. It now opens only a myflames release page on GitHub.
- Fixed an issue on Windows where another program on the same computer could open the workspace's local port too. The workspace now keeps that port to itself.
