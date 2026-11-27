# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### Improvements

- Changes disabled action buttons in **Cluster**, **Readyset** and everywhere a missing privilege or policy blocks an action: the reason no longer sits under the button; clicking the button (or pressing Enter or Space) opens a message anchored to it with the reason, how to fix it and, when a privilege is missing, the exact `GRANT` statement to copy. Escape, a click outside or **Close** dismisses the message and returns focus to the button, and screen readers hear the reason as the button's description.

### Bug fixes

### Security fixes

### Known issues
