#!/usr/bin/env python3
"""Fail unless everything a release names agrees on VERSION.

    python3 scripts/check_release_sync.py 3.3.4

Checks, all reported at once:

- apps/desktop/package.json "version", myflames/__init__.py __version__ and
  pyproject.toml [project] version all equal VERSION;
- docs/release-notes/VERSION.md is a release notes page in the release notes
  format (scripts/release_notes.py), and docs/release-notes/index.md lists it
  first with the same date, lists every page in the folder, newest first, and
  names only pages that exist;
- CHANGELOG.md's [Unreleased] holds no entries (they moved into the page);
- docs/screenshots/manifest.json (written by `npm run docs:screenshots` in
  apps/workspace) says the README screenshots were captured from VERSION, and
  the four README images exist;
- docs/handbook/manifest.json parses and every chapter file it lists exists
  (the website renders it).

Exit status 0 when in sync, 1 otherwise. Stdlib only, Python 3.7+.
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import release_notes  # noqa: E402  (a sibling script, not a package)

ROOT = HERE.parent
README_SCREENSHOTS = ("home.png", "plan-visual-explain.png", "query-editor.png", "server-health.png")


def _read(path):
    return path.read_text(encoding="utf-8")


def desktop_version(root):
    return json.loads(_read(root / "apps/desktop/package.json")).get("version")


def package_version(root):
    tree = ast.parse(_read(root / "myflames/__init__.py"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            value = node.value
            # ast.Str before Python 3.8, ast.Constant after.
            return getattr(value, "value", getattr(value, "s", None))
    return None


def pyproject_version(root):
    """The [project] table's version (no tomllib before Python 3.11)."""
    table = None
    for line in _read(root / "pyproject.toml").splitlines():
        header = re.match(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$", line)
        if header:
            table = header.group(1).strip()
            continue
        if table == "project":
            match = re.match(r"""^\s*version\s*=\s*(["'])([^"']*)\1\s*(?:#.*)?$""", line)
            if match:
                return match.group(2)
    return None


def release_notes_problems(root, version):
    """Problems with VERSION's release notes page, the index and [Unreleased]."""
    problems = []
    folder = root / "docs/release-notes"
    name = "docs/release-notes/%s.md" % version
    date = None
    try:
        date, _ = release_notes.page(_read(folder / (version + ".md")), version, name)
    except OSError:
        problems.append("%s is missing: write the release notes page for %s (DEVELOPER_REFERENCE "
                        "\"Release notes\"), from the entries under CHANGELOG.md [Unreleased]." % (name, version))
    except release_notes.ReleaseNotesError as exc:
        problems.append(str(exc))

    try:
        entries = release_notes.index(_read(folder / "index.md"))
    except OSError as exc:
        problems.append("docs/release-notes/index.md: cannot read (%s)." % exc)
    else:
        line = "- [myflames %s (%s)](%s.md)" % (version, date or "YYYY-MM-DD", version)
        if not entries or entries[0][0] != version:
            problems.append("docs/release-notes/index.md must list %s first: add '%s' at the top of the list."
                            % (version, line))
        elif date and entries[0][1] != date:
            problems.append("docs/release-notes/index.md dates %s %s, but its page says %s."
                            % (version, entries[0][1], date))
        listed = set()
        for listed_version, _, target in entries:
            listed.add(target)
            if target != listed_version + ".md":
                problems.append("docs/release-notes/index.md links myflames %s to %r, not %r."
                                % (listed_version, target, listed_version + ".md"))
            elif not (folder / target).is_file():
                problems.append("docs/release-notes/index.md lists myflames %s, but %s is missing."
                                % (listed_version, target))
        keys = [release_notes.version_key(v) for v, _, _ in entries]
        if keys != sorted(keys, reverse=True):
            problems.append("docs/release-notes/index.md must list versions newest first.")
        for path in sorted(folder.glob("*.md")):
            if path.name != "index.md" and path.name not in listed:
                problems.append("docs/release-notes/index.md does not list %s." % path.name)

    try:
        changelog = _read(root / "CHANGELOG.md").splitlines()
    except OSError as exc:
        problems.append("CHANGELOG.md: cannot read (%s)." % exc)
    else:
        unreleased = next((i for i, text in enumerate(changelog) if text.strip() == "## [Unreleased]"), None)
        if unreleased is None:
            problems.append("CHANGELOG.md needs its '## [Unreleased]' heading (new entries go under it).")
        else:
            rest = changelog[unreleased + 1:]
            end = next((i for i, text in enumerate(rest) if text.startswith("## ")), len(rest))
            if any(text.lstrip().startswith("- ") for text in rest[:end]):
                problems.append("CHANGELOG.md [Unreleased] still has entries: move them into %s." % name)
    return problems


def check(root, version):
    """A list of problems (empty when everything agrees on *version*)."""
    problems = []
    try:
        version = release_notes.normalize_version(version)
    except release_notes.ReleaseNotesError as exc:
        return [str(exc)]

    for label, read in (("apps/desktop/package.json version", desktop_version),
                        ("myflames/__init__.py __version__", package_version),
                        ("pyproject.toml [project] version", pyproject_version)):
        try:
            found = read(root)
        except (OSError, ValueError, SyntaxError) as exc:
            problems.append("%s: cannot read (%s)." % (label, exc))
            continue
        if found != version:
            problems.append("%s is %r, not %r." % (label, found, version))

    problems.extend(release_notes_problems(root, version))

    shots = root / "docs/screenshots"
    try:
        manifest = json.loads(_read(shots / "manifest.json"))
    except (OSError, ValueError) as exc:
        problems.append("docs/screenshots/manifest.json: cannot read (%s). Run `npm run docs:screenshots` in "
                        "apps/workspace from the release build." % exc)
    else:
        captured = manifest.get("version") if isinstance(manifest, dict) else None
        if captured != version:
            problems.append("README screenshots were captured from %r, not %r: run `npm run build` and "
                            "`npm run docs:screenshots` in apps/workspace, then commit docs/screenshots." % (captured, version))
    for name in README_SCREENSHOTS:
        if not (shots / name).is_file():
            problems.append("docs/screenshots/%s is missing." % name)

    try:
        chapters = json.loads(_read(root / "docs/handbook/manifest.json"))
    except (OSError, ValueError) as exc:
        problems.append("docs/handbook/manifest.json does not parse (%s)." % exc)
    else:
        if not isinstance(chapters, list) or not chapters:
            problems.append("docs/handbook/manifest.json must be a non-empty list of chapters.")
        else:
            for chapter in chapters:
                name = chapter.get("file") if isinstance(chapter, dict) else None
                if not isinstance(name, str) or not (root / "docs/handbook" / name).is_file():
                    problems.append("docs/handbook/manifest.json lists a chapter without its file: %r." % (chapter,))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check that versions, release notes, README screenshots and handbook agree on a release.")
    parser.add_argument("version", help="release version, for example 3.3.4 or v3.3.4")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    args = parser.parse_args(argv)
    problems = check(args.root.resolve(), args.version)
    if problems:
        print("Release %s is not in sync:" % args.version, file=sys.stderr)
        for problem in problems:
            print("- " + problem, file=sys.stderr)
        return 1
    print("Release %s is in sync: versions, release notes, README screenshots and handbook manifest."
          % release_notes.normalize_version(args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
