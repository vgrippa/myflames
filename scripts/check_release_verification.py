#!/usr/bin/env python3
"""Fail unless a release's notes entries carry evidence from the code that ships.

    python3 scripts/check_release_verification.py 3.4.3 [--head REV]

The record is docs/release-verification/VERSION.json (DEVELOPER_REFERENCE
"Release verification record"). Checks, all reported at once:

- the record exists, parses and its "version" is VERSION;
- every bullet under New features, Improvements and Bug fixes in
  docs/release-notes/VERSION.md matches exactly one item: the item's "entry"
  is a prefix of the bullet's text as plain words (Markdown emphasis, links
  and code marks removed, link text kept, whitespace collapsed), its "section"
  is the bullet's section, no item matches a bullet another item matches, and
  no item matches nothing;
- "needs" is a non-empty subset of tests, live, layout, reviews, always with
  tests, and each need is met by at least one evidence entry of that kind or
  by an "unverified" entry with that need and a non-empty accepted_by_owner;
- a layout entry was looked at and has four shots (1440 and 720 px, light and
  dark), each with a 64-hex sha256; a shot file that exists locally must hash
  to it (CI has no local files: the format is checked, the hash is not);
- every evidence "commit" exists, is an ancestor of the head (HEAD unless
  --head says otherwise) and only release-only paths differ between it and
  the head: the version files, CHANGELOG.md, ROADMAP.md, README.md, any *.md
  and anything under docs/. Anything else means the evidence ran on code that
  is not shipping, and the check must run again on the final code.

Exit status 0 when the record holds, 1 otherwise. Stdlib only, Python 3.7+.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import release_notes  # noqa: E402  (a sibling script, not a package)

ROOT = HERE.parent

EVIDENCE_KINDS = ("tests", "live", "layout", "reviews")
NOTE_SECTIONS = ("New features", "Improvements", "Bug fixes")

# What may change between an evidence commit and the release without
# invalidating the evidence: the release's own bookkeeping.
RELEASE_ONLY_FILES = frozenset((
    "pyproject.toml",
    "myflames/__init__.py",
    "apps/desktop/package.json",
    "apps/desktop/package-lock.json",
    "CHANGELOG.md",
    "ROADMAP.md",
    "README.md",
))

REQUIRED_SHOTS = frozenset(((1440, "light"), (1440, "dark"), (720, "light"), (720, "dark")))
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
EMPHASIS_RE = re.compile(r"\*\*|__|\*|_")


def _read(path):
    return path.read_text(encoding="utf-8")


def _plain(text):
    """*text* as plain words: links keep their text, emphasis and code marks go."""
    text = LINK_RE.sub(r"\1", text)
    text = EMPHASIS_RE.sub("", text.replace("`", ""))
    return " ".join(text.split())


def _bullets(text):
    """[(section, text)] of the bullets under the evidence sections of a notes page."""
    found = []
    section = None
    current = None
    for _, line in release_notes._prose(text.splitlines()):
        heading = release_notes.HEADING_RE.match(line)
        if heading and len(heading.group("marks")) == 2:
            if current:
                found.append((current[0], current[1]))
                current = None
            section = heading.group("text")
            continue
        if section not in NOTE_SECTIONS:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current:
                found.append((current[0], current[1]))
            current = [section, stripped[2:]]
        elif current and stripped:
            current[1] += " " + stripped
    if current:
        found.append((current[0], current[1]))
    return found


def _release_only(path):
    return path in RELEASE_ONLY_FILES or path.endswith(".md") or path.startswith("docs/")


def _git(root, *args):
    proc = subprocess.run(["git", "-C", str(root)] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    return proc.returncode, proc.stdout


def _commit_problems(root, head, commit, cache):
    """Problems with one evidence commit, cached by commit. Never prints file contents."""
    if commit in cache:
        return cache[commit]
    problems = []
    if not isinstance(commit, str) or not commit.strip():
        problems.append("an evidence entry has no commit.")
    elif _git(root, "rev-parse", "--verify", "--quiet", commit + "^{commit}")[0] != 0:
        problems.append("evidence commit %s does not exist." % commit)
    elif _git(root, "merge-base", "--is-ancestor", commit, head)[0] != 0:
        problems.append("evidence commit %s is not an ancestor of %s." % (commit, head))
    else:
        code, out = _git(root, "diff", "--name-only", commit, head)
        if code != 0:
            problems.append("cannot diff evidence commit %s against %s." % (commit, head))
        else:
            changed = [path for path in out.splitlines() if not _release_only(path)]
            if changed:
                problems.append("evidence commit %s is not on the final code: %s..%s changes %s; "
                                "the check must run again on the final code."
                                % (commit, commit, head, ", ".join(changed[:3])))
    cache[commit] = problems
    return problems


def _layout_problems(root, label, entry):
    problems = []
    if entry.get("looked_at") is not True:
        problems.append("%s: a layout entry was not looked at (looked_at is not true)." % label)
    shots = entry.get("shots")
    if not isinstance(shots, list):
        return problems + ["%s: a layout entry has no shots list." % label]
    seen = set()
    for shot in shots:
        if not isinstance(shot, dict):
            problems.append("%s: a layout shot is not an object: %r." % (label, shot))
            continue
        pair = (shot.get("width"), shot.get("theme"))
        if pair in REQUIRED_SHOTS:
            seen.add(pair)
        digest = shot.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            problems.append("%s: a layout shot has no 64-hex sha256: %r." % (label, digest))
        elif isinstance(shot.get("file"), str):
            path = root / shot["file"]
            if path.is_file():
                found = hashlib.sha256(path.read_bytes()).hexdigest()
                if found != digest:
                    problems.append("%s: %s hashes to %s, not the record's %s: capture the shot again."
                                    % (label, shot["file"], found, digest))
        else:
            problems.append("%s: a layout shot has no file: %r." % (label, shot))
    missing = REQUIRED_SHOTS - seen
    unexpected = seen - REQUIRED_SHOTS
    if missing or unexpected or len(shots) != 4:
        what = []
        if missing:
            what.append("missing %s" % ", ".join("%d px %s" % pair for pair in sorted(missing)))
        if unexpected:
            what.append("unexpected %s" % ", ".join("%r px %r" % pair for pair in sorted(unexpected)))
        if len(shots) != 4 and not what:
            what.append("%d shots" % len(shots))
        problems.append("%s: a layout entry needs four shots, 1440 and 720 px in light and dark (%s)."
                        % (label, "; ".join(what)))
    return problems


def _item_problems(root, head, item, cache):
    """Problems inside one record item (matching is done by the caller)."""
    label = "item %r" % (item.get("entry"),)
    problems = []
    needs = item.get("needs")
    if not isinstance(needs, list) or not needs or any(need not in EVIDENCE_KINDS for need in needs):
        problems.append("%s: needs must be a non-empty subset of %s." % (label, ", ".join(EVIDENCE_KINDS)))
        needs = []
    elif "tests" not in needs:
        problems.append("%s: needs must include tests." % label)
    unverified = item.get("unverified")
    accepted = set()
    for entry in unverified if isinstance(unverified, list) else []:
        if isinstance(entry, dict) and entry.get("need") in EVIDENCE_KINDS \
                and isinstance(entry.get("accepted_by_owner"), str) and entry["accepted_by_owner"].strip():
            accepted.add(entry["need"])
    for need in needs:
        evidence = item.get(need)
        if (not isinstance(evidence, list) or not evidence) and need not in accepted:
            problems.append("%s: need %r has no %s evidence and no unverified entry the owner accepted."
                            % (label, need, need))
    for kind in EVIDENCE_KINDS:
        entries = item.get(kind)
        if entries is None:
            continue
        if not isinstance(entries, list):
            problems.append("%s: %s must be a list of evidence entries." % (label, kind))
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append("%s: a %s entry is not an object: %r." % (label, kind, entry))
                continue
            for problem in _commit_problems(root, head, entry.get("commit"), cache):
                problems.append("%s: %s" % (label, problem))
            if kind == "layout":
                problems.extend(_layout_problems(root, label, entry))
    return problems


def check(root, version, head="HEAD"):
    """A list of problems (empty when the record proves the notes entries)."""
    problems = []
    try:
        version = release_notes.normalize_version(version)
    except release_notes.ReleaseNotesError as exc:
        return [str(exc)]

    bullets = None
    name = "docs/release-notes/%s.md" % version
    try:
        bullets = _bullets(_read(root / name))
    except OSError as exc:
        problems.append("%s: cannot read (%s)." % (name, exc))

    record_name = "docs/release-verification/%s.json" % version
    try:
        record = json.loads(_read(root / record_name))
    except OSError:
        return problems + ["%s is missing: record the evidence for every notes entry of %s "
                           "(DEVELOPER_REFERENCE \"Release verification record\")." % (record_name, version)]
    except ValueError as exc:
        return problems + ["%s does not parse (%s)." % (record_name, exc)]
    if not isinstance(record, dict):
        return problems + ["%s must be a JSON object." % record_name]
    if record.get("version") != version:
        problems.append("%s says version %r, not %r." % (record_name, record.get("version"), version))
    items = record.get("items")
    if not isinstance(items, list):
        items = []
        problems.append("%s: items must be a list, one per notes entry." % record_name)

    if _git(root, "rev-parse", "--verify", "--quiet", head + "^{commit}")[0] != 0:
        return problems + ["cannot resolve the head %r in %s: run inside the checkout." % (head, root)]

    valid = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("entry"), str) or not item["entry"].strip() \
                or not isinstance(item.get("section"), str):
            problems.append("%s: an item needs its entry and section strings: %r." % (record_name, item))
        else:
            valid.append(item)

    cache = {}
    for item in valid:
        problems.extend(_item_problems(root, head, item, cache))

    if bullets is not None:
        matched = set()
        for section, text in bullets:
            plain = _plain(text)
            candidates = [item for item in valid if plain.startswith(_plain(item["entry"]))]
            if not candidates:
                problems.append("%s: no item covers the %s entry %r." % (record_name, section, text[:80]))
                continue
            if len(candidates) > 1:
                problems.append("%s: the %s entry %r is matched by %d items; make each entry prefix name "
                                "one bullet." % (record_name, section, text[:80], len(candidates)))
            item = candidates[0]
            matched.add(id(item))
            if item["section"] != section:
                problems.append("%s: item %r says section %r, but its bullet is under %s."
                                % (record_name, item["entry"], item["section"], section))
        for item in valid:
            if id(item) not in matched:
                problems.append("%s: item %r matches no notes entry." % (record_name, item["entry"]))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check that a release's notes entries carry evidence from the code that ships.")
    parser.add_argument("version", help="release version, for example 3.4.3 or v3.4.3")
    parser.add_argument("--head", default="HEAD", help="the code that ships (default: HEAD)")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    args = parser.parse_args(argv)
    problems = check(args.root.resolve(), args.version, args.head)
    if problems:
        print("Release %s is not verified:" % args.version, file=sys.stderr)
        for problem in problems:
            print("- " + problem, file=sys.stderr)
        print("Release verification %s: %d problem(s)." % (args.version, len(problems)), file=sys.stderr)
        return 1
    print("Release %s is verified: every notes entry has evidence from the code that ships."
          % release_notes.normalize_version(args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
