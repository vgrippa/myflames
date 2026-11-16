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
  is the bullet's section, and each item matches exactly one bullet in that
  section;
- "needs" is a non-empty subset of tests, live, layout, reviews, always with
  tests, and each need is met by at least one evidence entry of that kind or
  by an "unverified" entry with that need and a non-empty accepted_by_owner;
- tests and live entries have outcome "pass"; tests record executed > 0 and
  skipped >= 0 as integers, with command and result text; live entries name
  servers and checks with non-empty action/outcome text; reviews have verdict
  "ship", or "ship-with-follow-ups" with a non-empty follow_ups list;
- a layout entry was looked at and has four shots (1440 and 720 px, light and
  dark), each with a 64-hex sha256; a shot file that exists locally must hash
  to it (CI has no local files: the format is checked, the hash is not);
- every evidence "commit" exists, is an ancestor of the head (HEAD unless
  --head says otherwise) and only release-only paths differ between it and
  the head: version values in the four version files, root *.md files,
  apps/*/README.md and docs/ except docs/schemas/ and docs/brand/. Anything
  else means the evidence ran on code that is not shipping, and the check
  must run again on the final code.

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

# Only the release version values in these files may change after evidence.
VERSION_FILES = frozenset((
    "pyproject.toml",
    "myflames/__init__.py",
    "apps/desktop/package.json",
    "apps/desktop/package-lock.json",
))

REQUIRED_SHOTS = frozenset(((1440, "light"), (1440, "dark"), (720, "light"), (720, "dark")))
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
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
    """Documentation exemptions, excluding schemas and packaged brand assets."""
    return (("/" not in path and path.endswith(".md"))
            or (path.startswith("docs/") and path not in ("docs/schemas", "docs/brand")
                and not path.startswith(("docs/schemas/", "docs/brand/")))
            or re.fullmatch(r"apps/[^/]+/README\.md", path) is not None)


def _git(root, *args):
    proc = subprocess.run(["git", "-C", str(root)] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Do not translate CRLF in committed blobs: only version values are exempt.
    return proc.returncode, proc.stdout.decode("utf-8")


def _json_object(pairs):
    """Do not hide a changed field behind a duplicate JSON key."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _without_version(path, text):
    """Normalize only existing release version values; reject unknown formats."""
    if path.endswith(".json"):
        data = json.loads(text, object_pairs_hook=_json_object)
        release_notes.normalize_version(data["version"])
        del data["version"]
        if path.endswith("package-lock.json"):
            package = data["packages"][""]
            release_notes.normalize_version(package["version"])
            del package["version"]
        # Canonical JSON preserves scalar types (Python considers True == 1).
        return json.dumps(data, sort_keys=True, allow_nan=False)

    key = "version" if path == "pyproject.toml" else "__version__"
    pattern = re.compile(r"^([ \t]*" + key + r"[ \t]*=[ \t]*)([\"'])([^\"'\r\n]*)\2([ \t]*(?:#.*)?)$")
    table = None
    found = 0
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if path == "pyproject.toml":
            if line.lstrip().startswith("["):
                header = re.match(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$", line)
                table = header.group(1).strip() if header else None
            if table != "project":
                continue
        match = pattern.match(line.rstrip("\r\n"))
        if match:
            release_notes.normalize_version(match.group(3))
            # Keep whitespace, quotes, comments and the rest of the file intact.
            lines[i] = line[:match.start(3)] + "<release-version>" + line[match.end(3):]
            found += 1
    if found != 1:
        raise ValueError("expected one release version assignment")
    return "".join(lines)


def _version_only(root, path, commit, head):
    """Compare committed contents, never the working tree or dependency versions."""
    normalized = []
    modes = []
    for revision in (commit, head):
        code, tree = _git(root, "ls-tree", revision, "--", path)
        if code != 0 or not tree:
            return False
        modes.append(tree.split()[0])
        if modes[-1] not in ("100644", "100755"):
            return False
        try:
            code, content = _git(root, "show", revision + ":" + path)
            if code != 0:
                return False
            normalized.append(_without_version(path, content))
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
    return modes[0] == modes[1] and normalized[0] == normalized[1]


def _commit_problems(root, head, commit, cache):
    """Problems with one evidence commit, cached by commit. Never prints file contents."""
    if commit in cache:
        return cache[commit]
    problems = []
    if not isinstance(commit, str) or not commit.strip():
        problems.append("an evidence entry has no commit.")
    elif not COMMIT_SHA_RE.match(commit):
        problems.append("evidence commit %r must be the full commit SHA." % (commit,))
    else:
        code, resolved = _git(root, "rev-parse", "--verify", "--quiet", commit + "^{commit}")
        if code != 0:
            problems.append("evidence commit %s does not exist." % commit)
        elif resolved.strip() != commit:
            problems.append("evidence commit %r must be the full commit SHA." % (commit,))
        elif _git(root, "merge-base", "--is-ancestor", commit, head)[0] != 0:
            problems.append("evidence commit %s is not an ancestor of %s." % (commit, head))
        else:
            # Both sides of a rename count; newlines in a path are not separators.
            code, out = _git(root, "diff", "--name-only", "--no-renames", "-z", commit, head)
            if code != 0:
                problems.append("cannot diff evidence commit %s against %s." % (commit, head))
            else:
                changed = [path for path in out.split("\0") if path and not _release_only(path)
                           and not (path in VERSION_FILES and _version_only(root, path, commit, head))]
                if changed:
                    problems.append("evidence commit %s is not on the final code: %s..%s changes %s; "
                                    "the check must run again on the final code."
                                    % (commit, commit, head, ", ".join(changed[:3])))
    cache[commit] = problems
    return problems


def _outcome_problems(label, kind, entry):
    problems = []
    if kind in ("tests", "live") and entry.get("outcome") != "pass":
        problems.append("%s: a %s entry outcome must be 'pass', not %r."
                        % (label, kind, entry.get("outcome")))
    if kind == "tests":
        for field, minimum in (("executed", 1), ("skipped", 0)):
            value = entry.get(field)
            if type(value) is not int or value < minimum:
                problems.append("%s: a tests entry %s must be an integer >= %d, not %r."
                                % (label, field, minimum, value))
        for field in ("command", "result"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                problems.append("%s: a tests entry %s must be non-empty text." % (label, field))
    elif kind == "live":
        servers = entry.get("servers")
        if not isinstance(servers, list) or not servers \
                or any(not isinstance(value, str) or not value.strip() for value in servers):
            problems.append("%s: a live entry servers must be a non-empty list of non-empty strings." % label)
        checks = entry.get("checks")
        if not isinstance(checks, list) or not checks:
            problems.append("%s: a live entry checks must be a non-empty list of objects." % label)
        else:
            for index, check_entry in enumerate(checks):
                if not isinstance(check_entry, dict):
                    problems.append("%s: a live entry checks[%d] must be an object." % (label, index))
                    continue
                for field in ("action", "outcome"):
                    value = check_entry.get(field)
                    if not isinstance(value, str) or not value.strip():
                        problems.append("%s: a live entry checks[%d].%s must be non-empty text."
                                        % (label, index, field))
    elif kind == "reviews":
        verdict = entry.get("verdict")
        if verdict not in ("ship", "ship-with-follow-ups"):
            problems.append("%s: a reviews entry verdict must be 'ship' or 'ship-with-follow-ups', not %r."
                            % (label, verdict))
        elif verdict == "ship-with-follow-ups":
            follow_ups = entry.get("follow_ups")
            if not isinstance(follow_ups, list) or not follow_ups \
                    or any(not isinstance(value, str) or not value.strip() for value in follow_ups):
                problems.append("%s: a reviews entry follow_ups must be a non-empty list of non-empty strings "
                                "for verdict 'ship-with-follow-ups'." % label)
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
            problems.extend(_outcome_problems(label, kind, entry))
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
        if not isinstance(item, dict) or not isinstance(item.get("entry"), str) \
                or not isinstance(item.get("section"), str):
            problems.append("%s: an item needs its entry and section strings: %r." % (record_name, item))
        elif not re.search(r"\w", _plain(item["entry"])):
            problems.append("%s: item %r contains no word character." % (record_name, item["entry"]))
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
            matching_bullets = [text for section, text in bullets if section == item["section"]
                                and _plain(text).startswith(_plain(item["entry"]))]
            if len(matching_bullets) > 1:
                problems.append("%s: item %r matches %d bullets under %s: %s; "
                                "make its entry prefix name exactly one bullet."
                                % (record_name, item["entry"], len(matching_bullets), item["section"],
                                   ", ".join(repr(text) for text in matching_bullets)))
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
