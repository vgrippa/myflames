"""Release sync scripts: release_notes.py, check_release_sync.py, check_release_assets.py.

Fixture repositories are written to a temporary directory; nothing reads the
network or the real ~/.myflames.

Run:
  python3 -m unittest discover -s test -p "test_release_scripts.py" -v
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, str(SCRIPTS / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notes = _load("release_notes")
sync = _load("check_release_sync")
assets = _load("check_release_assets")

CHANGELOG = textwrap.dedent("""\
    # Changelog

    ## [Unreleased]

    ### Fixed

    - Something not released yet.

    ## [3.4.0] - 2026-12-01

    myflames 3.4.0 adds one thing.

    ### Added

    - **One thing.** It works.

    ```text
    ## [9.9.9] - 2026-01-01 inside a code fence is not a heading
    ```

    ## [3.3.9] - 2026-11-20

    ### Fixed

    - An older fix.

    ## [3.3.8]

    - Undated.

    ## [3.3.7] - 2026-11-10

    ### Added

    ## [2.3.0] (2026-09-30)

    - Older heading style.
    """)


def run(script, *args, **kwargs):
    return subprocess.run([sys.executable, str(SCRIPTS / script)] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8", **kwargs)


class ReleaseNotesTest(unittest.TestCase):
    def test_section_body_without_heading_or_outer_blank_lines(self):
        date, body = notes.section(CHANGELOG, "3.4.0")
        self.assertEqual(date, "2026-12-01")
        self.assertTrue(body.startswith("myflames 3.4.0 adds one thing.\n\n### Added\n"))
        self.assertIn("- **One thing.** It works.", body)
        self.assertNotIn("3.3.9", body)
        self.assertTrue(body.endswith("```\n"))

    def test_heading_inside_code_fence_does_not_end_the_section(self):
        _, body = notes.section(CHANGELOG, "3.4.0")
        self.assertIn("## [9.9.9] - 2026-01-01 inside a code fence", body)
        with self.assertRaises(notes.ReleaseNotesError):
            notes.section(CHANGELOG, "9.9.9")

    def test_v_prefix_and_older_heading_style(self):
        self.assertEqual(notes.section(CHANGELOG, "v3.3.9")[1], "### Fixed\n\n- An older fix.\n")
        self.assertEqual(notes.section(CHANGELOG, "2.3.0"), ("2026-09-30", "- Older heading style.\n"))

    def test_missing_undated_empty_and_duplicate_sections_fail_clearly(self):
        cases = {
            "3.5.0": "no section for 3.5.0",
            "3.3.8": "without a release date",
            "3.3.7": "is empty",
            "Unreleased": "Not a release version",
        }
        for version, message in cases.items():
            with self.subTest(version=version):
                with self.assertRaises(notes.ReleaseNotesError) as caught:
                    notes.section(CHANGELOG, version)
                self.assertIn(message, str(caught.exception))
        twice = CHANGELOG + "\n## [3.4.0] - 2026-12-02\n\n- Again.\n"
        with self.assertRaisesRegex(notes.ReleaseNotesError, "2 sections for 3.4.0"):
            notes.section(twice, "3.4.0")

    def test_cli_prints_utf8_notes_and_fails_with_status_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            changelog = Path(tmp) / "CHANGELOG.md"
            changelog.write_text(CHANGELOG.replace("It works.", "It works in 0.5–1 s → µs."), encoding="utf-8")
            done = run("release_notes.py", "3.4.0", "--changelog", str(changelog))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("It works in 0.5–1 s → µs.", done.stdout)
            self.assertFalse(done.stdout.startswith("## "))
            missing = run("release_notes.py", "3.5.0", "--changelog", str(changelog))
            self.assertEqual(missing.returncode, 1)
            self.assertEqual(missing.stdout, "")
            self.assertIn("release_notes: CHANGELOG.md has no section for 3.5.0", missing.stderr)
            unreadable = run("release_notes.py", "3.4.0", "--changelog", str(Path(tmp) / "absent.md"))
            self.assertEqual(unreadable.returncode, 1)
            self.assertIn("cannot read", unreadable.stderr)

    def test_real_changelog_has_the_333_notes(self):
        date, body = notes.section((REPO / "CHANGELOG.md").read_text(encoding="utf-8"), "3.3.3")
        self.assertEqual(date, "2026-11-09")
        self.assertTrue(body.startswith("### Added"))
        self.assertNotIn("## [3.3.2]", body)


def write_repo(root, version="3.4.0", **override):
    """A minimal checkout in sync for *version*; *override* maps a relative path to text (None removes it)."""
    files = {
        "apps/desktop/package.json": json.dumps({"name": "myflames-desktop", "version": version}, indent=2),
        "myflames/__init__.py": '# myflames\n__version__ = "%s"\n' % version,
        "pyproject.toml": textwrap.dedent("""\
            [build-system]
            requires = ["setuptools>=61"]
            version = "0.0.0"

            [project]
            name = "myflames"
            version = "%s"  # the release

            [tool.other]
            version = "9.9.9"
            """) % version,
        "CHANGELOG.md": CHANGELOG,
        "docs/screenshots/manifest.json": json.dumps({"version": version, "commit": "a" * 40, "captured_at": "2026-12-01T00:00:00Z"}),
        "docs/handbook/manifest.json": json.dumps([{"id": "quickstart", "file": "quickstart.md"}]),
        "docs/handbook/quickstart.md": "# Quickstart\n",
    }
    for name in sync.README_SCREENSHOTS:
        files["docs/screenshots/" + name] = "png"
    files.update(override)
    for relative, text in files.items():
        if text is None:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class CheckReleaseSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp))

    def test_in_sync(self):
        write_repo(self.tmp)
        self.assertEqual(sync.check(self.tmp, "3.4.0"), [])
        self.assertEqual(sync.check(self.tmp, "v3.4.0"), [])
        done = run("check_release_sync.py", "3.4.0", "--root", str(self.tmp))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Release 3.4.0 is in sync", done.stdout)

    def test_reads_only_the_project_table_version(self):
        write_repo(self.tmp)
        self.assertEqual(sync.pyproject_version(self.tmp), "3.4.0")

    def test_every_mismatch_is_reported_at_once(self):
        write_repo(self.tmp, version="3.4.0", **{
            "myflames/__init__.py": '__version__ = "3.3.9"\n',
            "docs/screenshots/manifest.json": json.dumps({"version": "3.3.9"}),
            "docs/handbook/manifest.json": "[{not json",
            "docs/screenshots/query-editor.png": None,
        })
        (self.tmp / "apps/desktop/package.json").write_text(json.dumps({"version": "3.4.1"}))
        problems = "\n".join(sync.check(self.tmp, "3.4.0"))
        self.assertIn("apps/desktop/package.json version is '3.4.1', not '3.4.0'", problems)
        self.assertIn("myflames/__init__.py __version__ is '3.3.9', not '3.4.0'", problems)
        self.assertNotIn("pyproject.toml", problems)
        self.assertIn("README screenshots were captured from '3.3.9', not '3.4.0'", problems)
        self.assertIn("docs/screenshots/query-editor.png is missing", problems)
        self.assertIn("docs/handbook/manifest.json does not parse", problems)
        done = run("check_release_sync.py", "3.4.0", "--root", str(self.tmp))
        self.assertEqual(done.returncode, 1)
        self.assertIn("Release 3.4.0 is not in sync:", done.stderr)
        self.assertEqual(done.stderr.count("\n- "), 5)

    def test_changelog_section_must_exist_be_dated_and_not_empty(self):
        for version, message in (("3.5.0", "no section"), ("3.3.8", "without a release date"), ("3.3.7", "is empty")):
            with self.subTest(version=version):
                write_repo(self.tmp, version=version)
                problems = sync.check(self.tmp, version)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(message, problems[0])

    def test_missing_screenshot_manifest_and_handbook_chapter(self):
        write_repo(self.tmp, **{"docs/screenshots/manifest.json": None, "docs/handbook/quickstart.md": None})
        problems = "\n".join(sync.check(self.tmp, "3.4.0"))
        self.assertIn("docs/screenshots/manifest.json: cannot read", problems)
        self.assertIn("npm run docs:screenshots", problems)
        self.assertIn("lists a chapter without its file", problems)

    def test_bad_version_argument(self):
        write_repo(self.tmp)
        self.assertEqual(sync.check(self.tmp, "latest"), ["Not a release version: 'latest' (expected X.Y.Z, for example 3.3.4)."])


def release(*names, state="uploaded"):
    return {"tag_name": "v3.4.0", "assets": [{"id": 100 + i, "name": name, "state": state} for i, name in enumerate(names)]}


FULL = ("myflames-3.4.0-macos-arm64.dmg", "myflames-3.4.0-macos-arm64.dmg.sha256",
        "myflames-3.4.0-windows-x64-setup.exe", "myflames-3.4.0-windows-x64-setup.exe.sha256",
        "myflames-3.4.0-macos-arm64.zip", "myflames-3.4.0-macos-arm64.zip.sha256")


class CheckReleaseAssetsTest(unittest.TestCase):
    def test_complete_release_with_dmg_first(self):
        self.assertEqual(assets.check(release(*FULL), "3.4.0"), [])

    def test_missing_files_are_listed(self):
        problems = assets.check(release(*FULL[:4]), "v3.4.0")
        self.assertEqual(problems, ["Missing release file myflames-3.4.0-macos-arm64.zip.",
                                    "Missing release file myflames-3.4.0-macos-arm64.zip.sha256."])

    def test_zip_uploaded_before_dmg_fails(self):
        order = FULL[4:] + FULL[:4]
        problems = assets.check(release(*order), "3.4.0")
        self.assertEqual(len(problems), 1)
        self.assertIn("myflames-3.4.0-macos-arm64.zip was uploaded before myflames-3.4.0-macos-arm64.dmg", problems[0])
        self.assertIn("upload them again", problems[0])

    def test_gh_release_view_json_of_a_draft(self):
        # `gh release view --json assets` gives node ids; the number is in apiUrl.
        def gh(order):
            return {"assets": [{"id": "RA_kw%d" % i, "name": name, "state": "uploaded",
                                "apiUrl": "https://api.github.com/repos/vgrippa/myflames/releases/assets/%d" % (700 + i),
                                "url": "https://github.com/vgrippa/myflames/releases/download/v3.4.0/" + name}
                               for i, name in enumerate(order)]}
        self.assertEqual(assets.check(gh(FULL), "3.4.0"), [])
        problems = assets.check(gh(FULL[4:] + FULL[:4]), "3.4.0")
        self.assertEqual(len(problems), 1)
        self.assertIn("(asset id 700 < 702)", problems[0])
        unknown = {"assets": [{"id": "RA_x", "name": n, "state": "uploaded"} for n in FULL]}
        self.assertEqual(assets.check(unknown, "3.4.0"), ["The DMG and zip asset ids cannot be read, so their upload order is unknown."])

    def test_unfinished_upload_fails(self):
        problems = assets.check(release(*FULL, state="open"), "3.4.0")
        self.assertEqual(len(problems), 6)
        self.assertIn("not fully uploaded", problems[0])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "release.json"
            path.write_text(json.dumps(release(*FULL)))
            done = run("check_release_assets.py", "3.4.0", str(path))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("the DMG came first", done.stdout)
            piped = run("check_release_assets.py", "3.4.0", "-", input=json.dumps(release(*FULL[:2])))
            self.assertEqual(piped.returncode, 1)
            self.assertIn("Missing release file myflames-3.4.0-windows-x64-setup.exe.", piped.stderr)
            broken = run("check_release_assets.py", "3.4.0", "-", input="not json")
            self.assertEqual(broken.returncode, 1)
            self.assertIn("cannot read the release JSON", broken.stderr)


if __name__ == "__main__":
    unittest.main()
