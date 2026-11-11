# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### Improvements

- Settings opens as a page with sections for Appearance, Query editor, Workspace, Updates and news, and Client tools, from the gear at the bottom of the sidebar, ⌘, (Ctrl+, on Windows and Linux) or the command palette.
- The sidebar can be resized by dragging its right edge or using the keyboard, and remembers its width across app restarts.
- The version and update bar appears on every screen, with Check for updates and View releases beside the current view's status.
- Added themes: Fire, Digital Rain, Neon Grid, Dracula, Nord, Solarized Light, Solarized Dark, Tokyo Night, Gruvbox Dark, Catppuccin Mocha, Catppuccin Latte, GitHub Light, GitHub Dark and One Dark. Choose a theme in Settings > Appearance, where a card per theme previews its colors, or from Search; the choice applies across the workspace, including the Query editor and charts.
- Query editor suggestions show keywords, functions and cached names as you type, while database names load in the background. Cached names stay available while they refresh.
- Text selections, matching words and the current Find result are easier to see in light and dark themes, including when the Query editor loses focus.

### Bug fixes

- Fixed an issue where the theme and some view choices reset when the desktop app restarted.
