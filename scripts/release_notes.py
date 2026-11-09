#!/usr/bin/env python3
"""Print the CHANGELOG.md section of one release, for GitHub release notes.

    python3 scripts/release_notes.py 3.3.4 > notes.md
    gh release create v3.3.4 --notes-file notes.md ...

The section is the body under "## [VERSION] - YYYY-MM-DD", up to the next
level-two heading, without the heading itself and without leading or trailing
blank lines. A missing, undated, duplicated or empty section is an error (exit
status 1) with a message that says what to write. Stdlib only, Python 3.7+.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")
# "## [3.3.3] - 2026-11-09" (Keep a Changelog); "## [2.3.0] (2026-09-30)" in older entries.
HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]+)\](?P<rest>.*)$")
DATE_RE = re.compile(r"^\s*(?:-\s*(?P<dash>\d{4}-\d{2}-\d{2})|\((?P<paren>\d{4}-\d{2}-\d{2})\))\s*$")
# A code fence: three or more backticks or tildes (Markdown); it closes on the
# same character, at least as many, and nothing else on the line.
FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


class ReleaseNotesError(Exception):
    """The CHANGELOG has no usable section for the version."""


def normalize_version(value):
    """'v3.3.4' or '3.3.4' -> '3.3.4'; anything else raises ReleaseNotesError."""
    version = value.strip()
    if version.startswith("v"):
        version = version[1:]
    if not VERSION_RE.match(version):
        raise ReleaseNotesError("Not a release version: %r (expected X.Y.Z, for example 3.3.4)." % value)
    return version


def _prose(lines, start=0, state=None):
    """(index, line) of every line from *start* that is outside a code fence.
    *state*, a dict, gets "open": the fence still open at the end, or None."""
    state = {} if state is None else state
    opened = state["open"] = None
    for index in range(start, len(lines)):
        line = lines[index]
        match = FENCE_RE.match(line)
        if opened is None:
            # A backtick fence's info string cannot hold a backtick.
            if match and not (match.group("fence")[0] == "`" and "`" in match.group("info")):
                opened = state["open"] = match.group("fence")
                continue
            yield index, line
        elif match and match.group("fence")[0] == opened[0] and len(match.group("fence")) >= len(opened) \
                and not match.group("info").strip():
            opened = state["open"] = None


def _headings(lines):
    """(index, version, rest) for every level-two release heading outside code fences."""
    for index, line in _prose(lines):
        match = HEADING_RE.match(line)
        if match:
            yield index, match.group("version").strip(), match.group("rest")


def _next_section(lines, start):
    """Index of the next level-two heading after *start* (fences respected), or len(lines)."""
    for index, line in _prose(lines, start + 1):
        if line.startswith("## "):
            return index
    return len(lines)


def section(text, version, changelog="CHANGELOG.md"):
    """Return (date, body) of *version*'s dated section in CHANGELOG *text*."""
    version = normalize_version(version)
    lines = text.splitlines()
    found = [(index, rest) for index, name, rest in _headings(lines) if name == version]
    if not found:
        raise ReleaseNotesError(
            "%s has no section for %s. Add '## [%s] - YYYY-MM-DD' above the previous release "
            "(move the entries from [Unreleased] into it)." % (changelog, version, version))
    if len(found) > 1:
        raise ReleaseNotesError("%s has %d sections for %s; keep one." % (changelog, len(found), version))
    index, rest = found[0]
    date = DATE_RE.match(rest)
    if not date:
        raise ReleaseNotesError(
            "%s has '## [%s]' without a release date. Write '## [%s] - YYYY-MM-DD'." % (changelog, version, version))
    body = lines[index + 1:_next_section(lines, index)]
    fences = {}
    for _ in _prose(body, state=fences):
        pass
    if fences["open"]:
        raise ReleaseNotesError("%s's section for %s has a code fence (%s) that is never closed." % (changelog, version, fences["open"]))
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    # Subsection headings alone ("### Added" with nothing under it) are no notes.
    if not any(line.strip() and not line.lstrip().startswith("#") for line in body):
        raise ReleaseNotesError("%s's section for %s is empty: list what changed under it." % (changelog, version))
    return date.group("dash") or date.group("paren"), "\n".join(body) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print one release's CHANGELOG section (the GitHub release notes).")
    parser.add_argument("version", help="release version, for example 3.3.4 or v3.3.4")
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md", help="default: the repository's CHANGELOG.md")
    args = parser.parse_args(argv)
    try:
        text = args.changelog.read_text(encoding="utf-8")
    except OSError as exc:
        print("release_notes: cannot read %s: %s" % (args.changelog, exc.strerror or exc), file=sys.stderr)
        return 1
    try:
        _, body = section(text, args.version, args.changelog.name)
    except ReleaseNotesError as exc:
        print("release_notes: %s" % exc, file=sys.stderr)
        return 1
    # UTF-8 whatever the locale: the notes hold en dashes, arrows and µ.
    sys.stdout.flush()
    sys.stdout.buffer.write(body.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
