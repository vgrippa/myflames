#!/usr/bin/env python3
"""Print one release's notes page, for the GitHub release.

    python3 scripts/release_notes.py 3.3.4 > notes.md
    gh release create v3.3.4 --notes-file notes.md ...

The page is docs/release-notes/X.Y.Z.md. It starts with the title line
"# myflames X.Y.Z (YYYY-MM-DD)", then a sentence about the version, then
level-two sections named in SECTIONS, in that order, ending with "Additional
resources". The notes are the page without its title line (GitHub shows the
release name above them) and without leading or trailing blank lines. A
missing page, a wrong title, an unknown, repeated or out-of-order section, a
deeper heading, a page with no entries, an unclosed code fence, an em dash or
an internal /api/ path is an error (exit status 1) with a message that says
what to fix. docs/release-notes/index.md lists every page, newest first; see
DEVELOPER_REFERENCE "Release notes". Stdlib only, Python 3.7+.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "docs" / "release-notes"
# ASCII digits only, the same rule as docs-sync.yml's tag check.
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")
TITLE_RE = re.compile(r"^# myflames (?P<version>\S+) \((?P<date>\d{4}-\d{2}-\d{2})\)$")
# "- [myflames 3.3.4 (2026-11-10)](3.3.4.md)" in index.md.
INDEX_RE = re.compile(r"^- \[myflames (?P<version>\S+) \((?P<date>\d{4}-\d{2}-\d{2})\)\]\((?P<file>[^)\s]+)\)$")
# Markdown allows up to three spaces before a heading.
HEADING_RE = re.compile(r"^ {0,3}(?P<marks>#{1,6})\s+(?P<text>.*?)\s*$")
# A code fence: three or more backticks or tildes (Markdown); it closes on the
# same character, at least as many, and nothing else on the line.
FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# The sections a page may have, in the order it must use them. Leave out the
# empty ones; "Additional resources" is always there.
SECTIONS = (
    "Release highlights",
    "New features",
    "Improvements",
    "Bug fixes",
    "Security fixes",
    "Deprecations and removals",
    "Known issues",
    "Packaging notes",
    "Additional resources",
)
# Writing rules a script can see (the rest are in DEVELOPER_REFERENCE).
STYLE = (
    ("—", "an em dash: use a period, comma, colon or parentheses"),
    ("/api/", "an internal /api/ path: say what the user can do instead"),
)

# Every release's notes end with the project's origin credit (README, NOTICE):
# Brendan Gregg and Tanel Poder are where this work began. Never remove it.
CREDIT = ("Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and "
          "[Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/).")


class ReleaseNotesError(Exception):
    """The release notes page is missing or not in the release notes format."""


def normalize_version(value):
    """'v3.3.4' or '3.3.4' -> '3.3.4'; anything else raises ReleaseNotesError."""
    version = value.strip()
    if version.startswith("v"):
        version = version[1:]
    if not VERSION_RE.match(version):
        raise ReleaseNotesError("Not a release version: %r (expected X.Y.Z, for example 3.3.4)." % value)
    return version


def version_key(version):
    """Sort key for X.Y.Z: a pre-release ("-rc.2") sorts before its release and
    after "-rc.1" (numeric identifiers compare as numbers); "+build" is ignored."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$", version)
    if not match:
        return (-1,)
    pre = match.group(4)
    parts = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split(".")) if pre else ()
    return tuple(int(part) for part in match.groups()[:3]) + (pre is None, parts)


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


def page(text, version, name=None):
    """Return (date, notes) of *version*'s release notes page *text*.

    *notes* is the page without its title line, as GitHub shows it."""
    version = normalize_version(version)
    name = name or "docs/release-notes/%s.md" % version
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    title = TITLE_RE.match(lines[0]) if lines else None
    if not title or title.group("version") != version:
        raise ReleaseNotesError("%s must start with '# myflames %s (YYYY-MM-DD)', the release date in brackets."
                                % (name, version))
    body = lines[1:]
    fences = {}
    seen, entries = [], False
    for index, line in _prose(body, state=fences):
        for needle, what in STYLE:
            if needle in line:
                raise ReleaseNotesError("%s line %d has %s." % (name, index + 2, what))
        heading = HEADING_RE.match(line)
        if heading:
            level, label = len(heading.group("marks")), heading.group("text")
            if level != 2:
                raise ReleaseNotesError("%s line %d: '%s' is not a release notes section. Use only "
                                        "'## <section>' headings: %s." % (name, index + 2, line.strip(), ", ".join(SECTIONS)))
            if label not in SECTIONS:
                raise ReleaseNotesError("%s has an unknown section '## %s'. Sections: %s."
                                        % (name, label, ", ".join(SECTIONS)))
            if label in seen:
                raise ReleaseNotesError("%s has '## %s' twice; keep one." % (name, label))
            if seen and SECTIONS.index(label) < SECTIONS.index(seen[-1]):
                raise ReleaseNotesError("%s has '## %s' after '## %s'. Order: %s."
                                        % (name, label, seen[-1], ", ".join(SECTIONS)))
            seen.append(label)
        elif line.lstrip().startswith("- ") and seen and seen[-1] != "Additional resources":
            entries = True
    if fences["open"]:
        raise ReleaseNotesError("%s has a code fence (%s) that is never closed." % (name, fences["open"]))
    if "Additional resources" not in seen:
        raise ReleaseNotesError("%s needs '## Additional resources' at the end (download, Quickstart and "
                                "issue links)." % name)
    if not entries:
        raise ReleaseNotesError("%s lists no changes: add entries under its sections." % name)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return title.group("date"), "\n".join(body) + "\n"


def index(text):
    """[(version, date, file)] of index.md's entries, in the order listed."""
    entries = []
    for _, line in _prose(text.splitlines()):
        match = INDEX_RE.match(line.strip())
        if match:
            entries.append((match.group("version"), match.group("date"), match.group("file")))
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print one release's notes page (the GitHub release notes).")
    parser.add_argument("version", help="release version, for example 3.3.4 or v3.3.4")
    parser.add_argument("--notes-dir", type=Path, default=NOTES_DIR,
                        help="folder of release notes pages (default: the repository's docs/release-notes)")
    args = parser.parse_args(argv)
    try:
        version = normalize_version(args.version)
    except ReleaseNotesError as exc:
        print("release_notes: %s" % exc, file=sys.stderr)
        return 1
    path = args.notes_dir / (version + ".md")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print("release_notes: cannot read %s: %s. Write the release notes page for %s there "
              "(DEVELOPER_REFERENCE \"Release notes\")." % (path, exc.strerror or exc, version), file=sys.stderr)
        return 1
    try:
        _, body = page(text, version, "%s/%s" % (args.notes_dir.name, path.name))
    except ReleaseNotesError as exc:
        print("release_notes: %s" % exc, file=sys.stderr)
        return 1
    if CREDIT not in body:
        body = body.rstrip("\n") + "\n\n" + CREDIT + "\n"
    # UTF-8 whatever the locale: the notes hold en dashes, arrows and µ.
    sys.stdout.flush()
    sys.stdout.buffer.write(body.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
