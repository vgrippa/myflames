#!/usr/bin/env python3
"""Check a GitHub release's files: every installer with its checksum, DMG first.

    gh api repos/vgrippa/myflames/releases/tags/v3.3.4 > release.json
    python3 scripts/check_release_assets.py 3.3.4 release.json

A draft has no tag yet: `gh release view v3.3.4 --json assets > release.json`
gives the same check before the draft is published.

A release of VERSION must carry, each with its ".sha256" beside it:

- myflames-VERSION-macos-arm64.dmg          (the download for new installs)
- myflames-VERSION-macos-arm64.zip          (the in-app update file)
- myflames-VERSION-windows-x64-setup.exe    (the Windows preview installer)

The DMG must be uploaded before the zip (a lower asset id): an installed
myflames up to 3.3.3 takes the first macOS asset GitHub lists, which must stay
the DMG it knows how to open. Exit status 0 when the release is complete, 1
otherwise, with every problem listed. Stdlib only, Python 3.7+.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import release_notes  # noqa: E402  (a sibling script, not a package)


def required_assets(version):
    """The installer names a release of *version* carries, DMG before zip."""
    return ["myflames-%s-macos-arm64.dmg" % version,
            "myflames-%s-macos-arm64.zip" % version,
            "myflames-%s-windows-x64-setup.exe" % version]


def asset_id(asset):
    """The numeric asset id, from the REST answer ("id": 728250830) or from
    `gh release view --json assets` (a node id, with the number in apiUrl)."""
    value = asset.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    for key in ("apiUrl", "url"):
        match = re.search(r"/releases/assets/(\d+)$", str(asset.get(key) or ""))
        if match:
            return int(match.group(1))
    raise ValueError("no numeric id")


def legacy_macos_choice(assets):
    """The asset an installed myflames 3.3.3 or earlier downloads on an Apple
    Silicon Mac: the first one, in the order GitHub lists them, whose name has
    a macOS label and an arm64 label and ends in .zip, .tar.gz, .dmg or .exe
    (app_update.match_asset_for_platform as released in 3.3.3; kept here
    because that rule ships in apps already installed)."""
    def named(name, labels):
        return re.search(r"(?<![a-z0-9])(?:" + "|".join(labels) + r")(?![a-z0-9])", name)
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            continue
        name = asset["name"].lower()
        if (named(name, ("mac", "macos", "darwin", "apple")) and named(name, ("arm64", "aarch64", "universal"))
                and name.endswith((".zip", ".tar.gz", ".dmg", ".exe"))):
            return asset
    return None


def check(release, version):
    """A list of problems with *release* (the GitHub API release object)."""
    version = release_notes.normalize_version(version)
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list):
        return ["The release JSON has no assets list."]
    by_name = {}
    for asset in assets:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str):
            by_name[asset["name"]] = asset
    problems = []
    for installer in required_assets(version):
        for name in (installer, installer + ".sha256"):
            asset = by_name.get(name)
            if asset is None:
                problems.append("Missing release file %s." % name)
            elif asset.get("state", "uploaded") != "uploaded":
                problems.append("%s is not fully uploaded (state %r)." % (name, asset.get("state")))
    dmg, update = (by_name.get(name) for name in required_assets(version)[:2])
    if dmg is not None and update is not None:
        try:
            dmg_id, update_id = asset_id(dmg), asset_id(update)
            if dmg_id > update_id:
                problems.append(
                    "%s was uploaded before %s (asset id %s < %s): installed myflames 3.3.3 and earlier would "
                    "download the zip. Delete the zip and its .sha256 from the release and upload them again "
                    "(`gh release upload`) so they come after the DMG." % (update["name"], dmg["name"], update_id, dmg_id))
        except ValueError:
            problems.append("The DMG and zip asset ids cannot be read, so their upload order is unknown.")
    if dmg is not None:
        chosen = legacy_macos_choice(assets)
        if chosen is not dmg:
            problems.append("GitHub lists %s before the DMG, so installed myflames 3.3.3 and earlier would download "
                            "it instead of %s." % (chosen["name"] if chosen else "no macOS file", dmg["name"]))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check that a GitHub release has every installer, checksum and the DMG before the zip.")
    parser.add_argument("version", help="release version, for example 3.3.4 or v3.3.4")
    parser.add_argument("release_json", help="the release from `gh api repos/OWNER/REPO/releases/tags/TAG` or "
                                             "`gh release view TAG --json assets`, or - for stdin")
    args = parser.parse_args(argv)
    try:
        version = release_notes.normalize_version(args.version)
        text = sys.stdin.read() if args.release_json == "-" else Path(args.release_json).read_text(encoding="utf-8")
        release = json.loads(text)
    except release_notes.ReleaseNotesError as exc:
        print("check_release_assets: %s" % exc, file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print("check_release_assets: cannot read the release JSON: %s" % exc, file=sys.stderr)
        return 1
    problems = check(release, version)
    if problems:
        print("Release %s files are incomplete or out of order:" % version, file=sys.stderr)
        for problem in problems:
            print("- " + problem, file=sys.stderr)
        return 1
    print("Release %s has the DMG, the update zip and the Windows installer, each with its .sha256; the DMG came first." % version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
