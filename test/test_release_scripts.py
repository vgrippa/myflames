"""Release sync scripts: release_notes.py, check_release_sync.py, check_release_assets.py.

Fixture repositories are written to a temporary directory; nothing reads the
network or the real ~/.myflames.

Run:
  python3 -m unittest discover -s test -p "test_release_scripts.py" -v
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

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

    Release notes: docs/release-notes/index.md.

    ## [Unreleased]
    """)

PAGE = textwrap.dedent("""\
    # myflames 3.4.0 (2026-12-01)

    myflames 3.4.0 is available for macOS on Apple Silicon.

    ## New features

    - Adds one thing. It works.

    ```text
    ## [9.9.9] inside a code fence is not a section
    ```

    ## Bug fixes

    - Fixed an issue where the other thing failed.

    ## Additional resources

    - Download the latest myflames from [myflames.io](https://myflames.io/download.html)
    """)

INDEX = textwrap.dedent("""\
    # myflames release notes

    - [myflames 3.4.0 (2026-12-01)](3.4.0.md)
    - [myflames 3.3.9 (2026-11-20)](3.3.9.md)
    """)

OLDER = PAGE.replace("3.4.0 (2026-12-01)", "3.3.9 (2026-11-20)").replace("3.4.0 is", "3.3.9 is")


def run(script, *args, **kwargs):
    return subprocess.run([sys.executable, str(SCRIPTS / script)] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8", **kwargs)


class ReleaseNotesTest(unittest.TestCase):
    def test_notes_are_the_page_without_its_title(self):
        date, body = notes.page(PAGE, "3.4.0")
        self.assertEqual(date, "2026-12-01")
        self.assertTrue(body.startswith("myflames 3.4.0 is available"), body)
        self.assertNotIn("# myflames 3.4.0", body)
        self.assertTrue(body.endswith("(https://myflames.io/download.html)\n"))
        self.assertEqual(notes.page("\n\n" + PAGE, "v3.4.0")[0], "2026-12-01")

    def test_heading_inside_code_fence_is_not_a_section(self):
        _, body = notes.page(PAGE, "3.4.0")
        self.assertIn("## [9.9.9] inside a code fence", body)

    def test_longer_fences_and_tildes_close_only_on_their_own_kind(self):
        text = PAGE.replace("- Adds one thing. It works.\n", textwrap.dedent("""\
            - Adds an example:

              ````markdown
              ```sql
              ### Not a heading
              ```
              ````

            ~~~
            ```
            ~~~
            """))
        _, body = notes.page(text, "3.4.0")
        self.assertIn("### Not a heading", body)
        with self.assertRaisesRegex(notes.ReleaseNotesError, "'### After' is not a release notes section"):
            notes.page(text.replace("~~~\n```\n~~~\n", "~~~\n```\n~~~\n\n### After\n"), "3.4.0")

    def test_unclosed_fence_is_refused(self):
        with self.assertRaisesRegex(notes.ReleaseNotesError, "code fence \\(```\\) that is never closed"):
            notes.page(PAGE.replace("## [9.9.9] inside a code fence is not a section\n```\n", "no end\n"), "3.4.0")

    def test_version_with_a_newline_is_refused(self):
        for value in ("3.3.4\nX=1", "3.3.4/../x", "../3.3.4", "Unreleased", "\u0663.3.4"):
            with self.subTest(value=value), self.assertRaises(notes.ReleaseNotesError):
                notes.normalize_version(value)

    def test_format_errors_say_what_to_fix(self):
        cases = {
            "wrong title": (PAGE.replace("# myflames 3.4.0 (2026-12-01)", "# myflames 3.4.0"), "must start with '# myflames 3.4.0 (YYYY-MM-DD)'"),
            "other version": (OLDER, "must start with '# myflames 3.4.0"),
            "unknown section": (PAGE.replace("## Bug fixes", "## Fixed"), "unknown section '## Fixed'"),
            "out of order": (PAGE.replace("## New features", "## Packaging notes"), "'## Bug fixes' after '## Packaging notes'"),
            "twice": (PAGE.replace("## Bug fixes", "## New features"), "'## New features' twice"),
            "level three": (PAGE.replace("## Bug fixes", "### Fixed"), "'### Fixed' is not a release notes section"),
            "indented heading": (PAGE.replace("## Bug fixes", "   ### Fixed"), "'### Fixed' is not a release notes section"),
            "second title": (PAGE.replace("## Bug fixes", "# Bug fixes"), "'# Bug fixes' is not a release notes section"),
            "no resources": (PAGE.split("## Additional resources")[0], "needs '## Additional resources'"),
            "no entries": ("# myflames 3.4.0 (2026-12-01)\n\nIntro.\n\n## Additional resources\n\n- Link\n", "lists no changes"),
            "em dash": (PAGE.replace("It works.", "It works — fast."), "line 7 has an em dash"),
            "api path": (PAGE.replace("It works.", "It reads `/api/live/x`."), "internal /api/ path"),
        }
        for label, (text, message) in cases.items():
            with self.subTest(label):
                with self.assertRaises(notes.ReleaseNotesError) as caught:
                    notes.page(text, "3.4.0")
                self.assertIn(message, str(caught.exception))
        # An en dash in a range and an em dash inside a code block are fine.
        notes.page(PAGE.replace("It works.", "It takes 0.5–1 s.").replace("is not a section", "— here"), "3.4.0")

    def test_index_entries_in_order(self):
        self.assertEqual(notes.index(INDEX), [("3.4.0", "2026-12-01", "3.4.0.md"), ("3.3.9", "2026-11-20", "3.3.9.md")])
        self.assertEqual(notes.index("```\n- [myflames 1.0.0 (2025-01-01)](1.0.0.md)\n```\n"), [])

    def test_version_key_orders_releases(self):
        ordered = ["3.10.0", "3.4.0", "3.4.0-rc.10", "3.4.0-rc.2", "3.4.0-rc.1", "3.4.0-beta", "3.3.9", "2.0.0"]
        self.assertEqual(sorted(ordered, key=notes.version_key, reverse=True), ordered)

    def test_cli_prints_utf8_notes_and_fails_with_status_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "3.4.0.md").write_text(PAGE.replace("It works.", "It works in 0.5–1 s → µs."), encoding="utf-8")
            done = run("release_notes.py", "3.4.0", "--notes-dir", str(folder))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("It works in 0.5–1 s → µs.", done.stdout)
            self.assertFalse(done.stdout.startswith("#"))
            missing = run("release_notes.py", "3.5.0", "--notes-dir", str(folder))
            self.assertEqual(missing.returncode, 1)
            self.assertEqual(missing.stdout, "")
            self.assertIn("release_notes: cannot read", missing.stderr)
            self.assertIn("Write the release notes page for 3.5.0", missing.stderr)
            (folder / "3.4.1.md").write_text(PAGE, encoding="utf-8")
            wrong = run("release_notes.py", "v3.4.1", "--notes-dir", str(folder))
            self.assertEqual(wrong.returncode, 1)
            self.assertIn("must start with '# myflames 3.4.1", wrong.stderr)
            bad = run("release_notes.py", "latest", "--notes-dir", str(folder))
            self.assertEqual(bad.returncode, 1)
            self.assertIn("Not a release version", bad.stderr)

    def test_cli_notes_end_with_the_inspiration_credit_once(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(notes.CREDIT, readme, "the credit matches the README's line")
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "3.4.0.md"
            page.write_text(PAGE, encoding="utf-8")
            done = run("release_notes.py", "3.4.0", "--notes-dir", tmp)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(done.stdout.endswith("\n\n" + notes.CREDIT + "\n"), done.stdout[-200:])
            # A page that already carries the credit is not given a second one.
            page.write_text(PAGE + "\n" + notes.CREDIT + "\n", encoding="utf-8")
            again = run("release_notes.py", "3.4.0", "--notes-dir", tmp)
            self.assertEqual(again.stdout.count(notes.CREDIT), 1)


class RealReleaseNotesTest(unittest.TestCase):
    """docs/release-notes as shipped: every page valid, the index complete and in order."""

    FOLDER = REPO / "docs/release-notes"

    def test_every_page_is_in_the_format_and_ends_with_the_credit(self):
        pages = sorted(p for p in self.FOLDER.glob("*.md") if p.name != "index.md")
        self.assertGreater(len(pages), 20)
        for path in pages:
            with self.subTest(path.name):
                text = path.read_text(encoding="utf-8")
                date, body = notes.page(text, path.stem)
                self.assertTrue(text.rstrip("\n").endswith(notes.CREDIT), "%s ends with the credit" % path.name)
                self.assertIn("## Additional resources", body)

    def test_index_lists_every_page_newest_first_with_its_date(self):
        entries = notes.index((self.FOLDER / "index.md").read_text(encoding="utf-8"))
        pages = {p.stem for p in self.FOLDER.glob("*.md") if p.name != "index.md"}
        self.assertEqual({v for v, _, _ in entries}, pages)
        versions = [v for v, _, _ in entries]
        self.assertEqual(versions, sorted(versions, key=notes.version_key, reverse=True))
        for version, date, target in entries:
            with self.subTest(version):
                self.assertEqual(target, version + ".md")
                self.assertEqual(notes.page((self.FOLDER / target).read_text(encoding="utf-8"), version)[0], date)

    def test_the_latest_release_has_notes(self):
        version = json.loads((REPO / "apps/desktop/package.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(notes.index((self.FOLDER / "index.md").read_text(encoding="utf-8"))[0][0], version)


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
        "README.md": "# myflames\n\nDownload myflames-%s-macos-arm64.dmg.\n" % version,
        "docs/DESKTOP.md": "# Desktop\n\nDownload myflames-%s-windows-x64-setup.exe.\n" % version,
        "docs/release-notes/index.md": INDEX.replace("3.4.0", version),
        "docs/release-notes/%s.md" % version: PAGE.replace("3.4.0", version),
        "docs/release-notes/3.3.9.md": OLDER,
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

    def test_stale_download_names_include_document_line_and_wrong_version(self):
        for document in ("README.md", "docs/DESKTOP.md"):
            for suffix in ("macos-arm64.dmg", "windows-x64-setup.exe", "macos-arm64.zip"):
                with self.subTest(document=document, suffix=suffix):
                    name = "myflames-3.3.9-" + suffix
                    write_repo(self.tmp, **{document: "# Downloads\n\nDownload `%s`.\n" % name})
                    problems = sync.check(self.tmp, "3.4.0")
                    self.assertEqual(len(problems), 1, problems)
                    self.assertIn(document + ":3:", problems[0])
                    self.assertIn(name, problems[0])
                    self.assertIn("'3.3.9', not '3.4.0'", problems[0])

    def test_matching_download_names_and_templates_pass(self):
        text = ("# Downloads\n\n[macOS](https://example.invalid/myflames-3.4.0-macos-arm64.dmg)\n"
                "`myflames-3.4.0-windows-x64-setup.exe`\n"
                "`myflames-3.4.0-macos-arm64.zip.sha256`\n"
                "Template: `myflames-<version>-macos-arm64.dmg`\n")
        write_repo(self.tmp, **{"README.md": text, "docs/DESKTOP.md": text})
        self.assertEqual(sync.check(self.tmp, "v3.4.0"), [])

    def test_prerelease_download_versions_are_compared_in_full(self):
        write_repo(self.tmp, version="3.4.0-rc.1")
        self.assertEqual(sync.check(self.tmp, "3.4.0-rc.1"), [])
        (self.tmp / "README.md").write_text("myflames-3.4.0-rc.2-macos-arm64.dmg\n", encoding="utf-8")
        problems = sync.check(self.tmp, "3.4.0-rc.1")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("README.md:1:", problems[0])
        self.assertIn("'3.4.0-rc.2', not '3.4.0-rc.1'", problems[0])

    def test_all_stale_downloads_are_reported_and_cli_fails(self):
        write_repo(self.tmp, **{
            "README.md": "# Downloads\nmyflames-3.3.9-macos-arm64.dmg myflames-3.3.8-macos-arm64.zip\n",
            "docs/DESKTOP.md": "# Desktop\n\nmyflames-3.3.7-windows-x64-setup.exe\n",
        })
        problems = sync.check(self.tmp, "3.4.0")
        self.assertEqual(len(problems), 3, problems)
        done = run("check_release_sync.py", "3.4.0", "--root", str(self.tmp))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("README.md:2:", done.stderr)
        self.assertIn("docs/DESKTOP.md:3:", done.stderr)
        for version in ("3.3.9", "3.3.8", "3.3.7"):
            self.assertIn("'%s', not '3.4.0'" % version, done.stderr)

    def test_missing_download_document_cannot_skip_the_check(self):
        write_repo(self.tmp, **{"docs/DESKTOP.md": None})
        problems = sync.check(self.tmp, "3.4.0")
        self.assertTrue(any("docs/DESKTOP.md: cannot read" in p for p in problems), problems)

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

    def test_release_notes_page_index_and_unreleased(self):
        cases = (
            ({"docs/release-notes/3.4.0.md": None}, "docs/release-notes/3.4.0.md is missing"),
            ({"docs/release-notes/3.4.0.md": PAGE.replace("## Bug fixes", "## Fixed")}, "unknown section '## Fixed'"),
            ({"docs/release-notes/index.md": INDEX.replace("- [myflames 3.4.0 (2026-12-01)](3.4.0.md)\n", "")},
             "must list 3.4.0 first: add '- [myflames 3.4.0 (2026-12-01)](3.4.0.md)'"),
            ({"docs/release-notes/index.md": INDEX.replace("3.4.0 (2026-12-01)", "3.4.0 (2026-12-02)")},
             "dates 3.4.0 2026-12-02, but its page says 2026-12-01"),
            ({"docs/release-notes/3.3.9.md": None}, "lists myflames 3.3.9, but 3.3.9.md is missing"),
            ({"docs/release-notes/3.3.8.md": OLDER}, "does not list 3.3.8.md"),
            ({"docs/release-notes/index.md": INDEX + "- [myflames 3.3.10 (2026-11-21)](3.3.10.md)\n",
              "docs/release-notes/3.3.10.md": OLDER.replace("3.3.9", "3.3.10")}, "newest first"),
            ({"docs/release-notes/index.md": INDEX.replace("(3.3.9.md)", "(../../x.md)")}, "links myflames 3.3.9 to '../../x.md'"),
            ({"CHANGELOG.md": CHANGELOG + "\n### Bug fixes\n\n- Not moved yet.\n"}, "[Unreleased] still has entries"),
            ({"CHANGELOG.md": "# Changelog\n"}, "needs its '## [Unreleased]' heading"),
        )
        for override, message in cases:
            with self.subTest(message):
                shutil.rmtree(str(self.tmp))
                write_repo(self.tmp, **override)
                problems = sync.check(self.tmp, "3.4.0")
                self.assertTrue(any(message in problem for problem in problems), problems)

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


INSTALLERS = ("myflames-3.4.0-macos-arm64.dmg", "myflames-3.4.0-macos-arm64.dmg.sha256",
              "myflames-3.4.0-windows-x64-setup.exe", "myflames-3.4.0-windows-x64-setup.exe.sha256",
              "myflames-3.4.0-macos-arm64.zip", "myflames-3.4.0-macos-arm64.zip.sha256")
SOURCE = ("mysql-8.4.13.tar.gz", "mysql-8.4.13.tar.gz.sha256")
FULL = INSTALLERS + SOURCE


class CheckReleaseAssetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp))
        self.lock = self.tmp / "apps/desktop/mysql-client.lock.json"
        self.lock.parent.mkdir(parents=True)
        self.write_lock()
        patch = mock.patch.object(assets, "ROOT", self.tmp, create=True)
        patch.start()
        self.addCleanup(patch.stop)

    def write_lock(self, version="8.4.13"):
        self.lock.write_text(json.dumps({
            "version": version,
            "source": {"archive": "https://example.invalid/source/mysql-%s.tar.gz" % version},
        }), encoding="utf-8")

    def test_complete_release_with_dmg_first(self):
        self.assertEqual(assets.check(release(*FULL), "3.4.0"), [])

    def test_missing_files_are_listed(self):
        problems = assets.check(release(*(INSTALLERS[:4] + SOURCE)), "v3.4.0")
        self.assertEqual(problems, ["Missing release file myflames-3.4.0-macos-arm64.zip.",
                                    "Missing release file myflames-3.4.0-macos-arm64.zip.sha256."])

    def test_zip_uploaded_before_dmg_fails(self):
        order = INSTALLERS[4:] + INSTALLERS[:4] + SOURCE
        problems = assets.check(release(*order), "3.4.0")
        self.assertEqual(len(problems), 2)
        self.assertIn("myflames-3.4.0-macos-arm64.zip was uploaded before myflames-3.4.0-macos-arm64.dmg", problems[0])
        self.assertIn("upload them again", problems[0])
        self.assertIn("GitHub lists myflames-3.4.0-macos-arm64.zip before the DMG", problems[1])

    def test_listing_order_is_what_a_333_client_reads(self):
        # Lower DMG id, but listed after the zip: a 3.3.3 client takes the first match.
        listed = release(*FULL)
        listed["assets"].sort(key=lambda a: a["name"].endswith(".dmg"))
        problems = assets.check(listed, "3.4.0")
        self.assertEqual(problems, ["GitHub lists myflames-3.4.0-macos-arm64.zip before the DMG, so installed myflames "
                                    "3.3.3 and earlier would download it instead of myflames-3.4.0-macos-arm64.dmg."])
        # The rule matches whole labels only, like the shipped one: "darwin" is not "win".
        chosen = assets.legacy_macos_choice([{"name": "myflames-3.4.0-windows-x64-setup.exe"},
                                             {"name": "myflames-3.4.0-macos-arm64.dmg.sha256"},
                                             {"name": "myflames-3.4.0-macos-arm64.dmg"}])
        self.assertEqual(chosen["name"], "myflames-3.4.0-macos-arm64.dmg")

    def test_gh_release_view_json_of_a_draft(self):
        # `gh release view --json assets` gives node ids; the number is in apiUrl.
        def gh(order):
            return {"assets": [{"id": "RA_kw%d" % i, "name": name, "state": "uploaded",
                                "apiUrl": "https://api.github.com/repos/vgrippa/myflames/releases/assets/%d" % (700 + i),
                                "url": "https://github.com/vgrippa/myflames/releases/download/v3.4.0/" + name}
                               for i, name in enumerate(order)]}
        self.assertEqual(assets.check(gh(FULL), "3.4.0"), [])
        problems = assets.check(gh(INSTALLERS[4:] + INSTALLERS[:4] + SOURCE), "3.4.0")
        self.assertEqual(len(problems), 2)
        self.assertIn("(asset id 700 < 702)", problems[0])
        unknown = {"assets": [{"id": "RA_x", "name": n, "state": "uploaded"} for n in FULL]}
        self.assertEqual(assets.check(unknown, "3.4.0"), ["The DMG and zip asset ids cannot be read, so their upload order is unknown."])

    def test_unfinished_upload_fails(self):
        problems = assets.check(release(*FULL, state="open"), "3.4.0")
        self.assertEqual(len(problems), 8)
        self.assertIn("not fully uploaded", problems[0])

    def test_bundled_client_source_archive_missing(self):
        problems = assets.check(release(*INSTALLERS), "3.4.0")
        self.assertEqual(len(problems), 2, problems)
        for problem, name in zip(problems, SOURCE):
            self.assertIn("Missing release file " + name, problem)
            self.assertIn("NOTICE", problem)
            self.assertIn("unmodified", problem)

    def test_bundled_client_source_checksum_missing(self):
        problems = assets.check(release(*(INSTALLERS + SOURCE[:1])), "3.4.0")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("Missing release file mysql-8.4.13.tar.gz.sha256", problems[0])
        self.assertIn("NOTICE", problems[0])

    def test_bundled_client_source_upload_must_finish(self):
        for name in SOURCE:
            with self.subTest(name=name):
                payload = release(*FULL)
                next(a for a in payload["assets"] if a["name"] == name)["state"] = "starter"
                problems = assets.check(payload, "3.4.0")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(name + " is not fully uploaded", problems[0])
                self.assertIn("NOTICE", problems[0])

    def test_source_archive_version_comes_from_the_lock(self):
        self.write_lock("8.4.14")
        problems = assets.check(release(*FULL), "3.4.0")
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("Missing release file mysql-8.4.14.tar.gz.", problems[0])
        self.assertIn("Missing release file mysql-8.4.14.tar.gz.sha256.", problems[1])
        names = INSTALLERS + ("mysql-8.4.14.tar.gz", "mysql-8.4.14.tar.gz.sha256")
        self.assertEqual(assets.check(release(*names), "3.4.0"), [])

    def test_no_bundled_client_lock_needs_only_installers(self):
        self.lock.unlink()
        self.assertEqual(assets.check(release(*INSTALLERS), "3.4.0"), [])

    def test_invalid_client_lock_cannot_silently_skip_source_assets(self):
        cases = ["not json", "[]", "{}", '{"version": 8.4}',
                 '{"version": "8.4.13", "source": {}}',
                 '{"version": "8.4.13", "source": {"archive": ""}}',
                 '{"version": "8.4.13", "source": {"archive": "https://example.invalid/mysql-8.4.12.tar.gz"}}']
        for text in cases:
            with self.subTest(text=text):
                self.lock.write_text(text, encoding="utf-8")
                problems = assets.check(release(*FULL), "3.4.0")
                self.assertTrue(any("apps/desktop/mysql-client.lock.json" in p and "NOTICE" in p for p in problems), problems)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "release.json"
            path.write_text(json.dumps(release(*FULL)))
            done = run("check_release_assets.py", "3.4.0", str(path), "--root", str(self.tmp))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("the DMG came first", done.stdout)
            piped = run("check_release_assets.py", "3.4.0", "-", "--root", str(self.tmp), input=json.dumps(release(*FULL[:2])))
            self.assertEqual(piped.returncode, 1)
            self.assertIn("Missing release file myflames-3.4.0-windows-x64-setup.exe.", piped.stderr)
            self.assertIn("Missing release file mysql-8.4.13.tar.gz.", piped.stderr)
            self.assertIn("NOTICE", piped.stderr)
            broken = run("check_release_assets.py", "3.4.0", "-", input="not json")
            self.assertEqual(broken.returncode, 1)
            self.assertIn("cannot read the release JSON", broken.stderr)


def workflow_step(path, name):
    """The run: block of the step called *name* in a workflow file (no YAML parser in the stdlib)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "- name: " + name)
    run = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip())
    block = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return textwrap.dedent("\n".join(block)) + "\n"


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class DocsSyncWorkflowTest(unittest.TestCase):
    """Abuse cases for .github/workflows/docs-sync.yml's shell steps."""

    def run_tag_step(self, tag):
        script = workflow_step(REPO / ".github/workflows/docs-sync.yml", "Release tag")
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "github_env"
            env_file.write_text("")
            done = subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", "TAG": tag, "GITHUB_ENV": str(env_file)},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            return done, env_file.read_text()

    def test_release_tag_sets_only_version(self):
        for tag, version in (("v3.3.4", "3.3.4"), ("v3.4.0-rc.1", "3.4.0-rc.1")):
            with self.subTest(tag=tag):
                done, written = self.run_tag_step(tag)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(written, "VERSION=%s\n" % version)

    def run_changelog_step(self, files, label=False, base_files=()):
        """The PR job's changelog step on a merge commit shaped like refs/pull/N/merge."""
        script = workflow_step(REPO / ".github/workflows/docs-sync.yml", "CHANGELOG entry for user-facing changes")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.com", "NO_CHANGELOG": "true" if label else "false"}
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git", "-c", "init.defaultBranch=master", "-c", "commit.gpgsign=false"] + list(args),
                               cwd=tmp, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            git("init", "-q")
            for name in ("CHANGELOG.md",) + tuple(base_files):
                path = Path(tmp) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("base\n")
            git("add", "-A")
            git("commit", "-qm", "base")
            git("checkout", "-qb", "pr")
            for name, action in files:
                path = Path(tmp) / name
                if action == "delete":
                    git("rm", "-q", name)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("change\n")
            git("add", "-A")
            git("commit", "-qm", "pr")
            git("checkout", "-q", "master")
            git("merge", "-q", "--no-ff", "pr", "-m", "merge")
            return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=tmp, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    @unittest.skipUnless(shutil.which("git") and os.name != "nt", "needs git and newline file names")
    def test_changelog_gate(self):
        cases = [
            ("documentation only", [("docs/handbook/x.md", "add"), ("README.md", "add")], False, 0),
            ("tests and built assets only", [("apps/workspace/test/a.mjs", "add"), ("apps/desktop/test/a.test.cjs", "add"),
                                             ("myflames/ui_assets/index.html", "add"), ("myflames/x/tests/t.py", "add"),
                                             ("myflames/test_x.py", "add")], False, 0),
            ("workspace code without an entry", [("apps/workspace/src/Health.tsx", "add")], False, 1),
            ("Python code with an entry", [("myflames/ui.py", "add"), ("CHANGELOG.md", "add")], False, 0),
            ("workspace code with a release notes page", [("apps/workspace/src/a.tsx", "add"),
                                                          ("docs/release-notes/3.4.0.md", "add")], False, 0),
            ("workspace code with an unrelated docs page", [("apps/workspace/src/a.tsx", "add"),
                                                            ("docs/release-notes/x/notes.txt", "add")], False, 1),
            ("desktop code with the no-changelog label", [("apps/desktop/main.cjs", "add")], True, 0),
            ("a module moved out of myflames", [("myflames/old.py", "delete"), ("scripts/old.py", "add")], False, 1),
            # Abuse case: one file whose name holds a newline before CHANGELOG.md.
            ("a name that ends in a CHANGELOG.md line", [("myflames/x\nCHANGELOG.md", "add")], False, 1),
        ]
        for label, files, has_label, status in cases:
            with self.subTest(label):
                base = [name for name, action in files if action == "delete"]
                done = self.run_changelog_step(files, has_label, base)
                self.assertEqual(done.returncode, status, done.stdout + done.stderr)
                if status:
                    self.assertTrue(done.stdout.startswith("::error file=CHANGELOG.md::"), done.stdout)
                    self.assertFalse(any(line.startswith("::") for line in done.stdout.splitlines()[1:]), done.stdout)

    def test_a_second_line_in_the_tag_never_reaches_github_env(self):
        for tag in ("v1.2.3\nBASH_ENV=$(echo INJECTED >&2)", "v1.2.3\n", "x\nv1.2.3", "3.3.4", "v3.3.4; rm -rf /"):
            with self.subTest(tag=tag):
                done, written = self.run_tag_step(tag)
                self.assertEqual(done.returncode, 1)
                self.assertEqual(written, "")
                # The rejected value is not echoed, so it cannot print a workflow command.
                self.assertEqual(done.stdout, "::error::Not a release tag (expected vX.Y.Z).\n")


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class MacosRetryWorkflowTest(unittest.TestCase):
    """Run the real retry step against canned API pages, never the real gh."""

    WORKFLOW = REPO / ".github/workflows/macos-retry.yml"
    STEP = "Retry only when no runner was assigned"
    RUN_ID = "43912550465"
    GH_REPO = "vgrippa/myflames"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.log = self.tmp / "gh.jsonl"
        self.script = workflow_step(self.WORKFLOW, self.STEP)
        # A queued job can have started_at even though no step ever started.
        self.job = {"id": 133458036379, "conclusion": "cancelled", "steps": [],
                    "started_at": "2026-11-16T16:54:02Z"}
        self.annotations = {str(self.job["id"]): [{
            "annotation_level": "failure", "title": "", "raw_details": "",
            "message": "The job was not acquired by Runner of type hosted even after multiple attempts",
        }]}
        shim = self.tmp / "gh"
        shim.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""\
            import json
            import os
            import sys

            args = sys.argv[1:]
            with open(os.environ["GH_SHIM_LOG"], "a", encoding="utf-8") as log:
                log.write(json.dumps(args) + "\\n")
            if args[:1] == ["api"] and args[2:] == ["--paginate", "--slurp"]:
                status = int(os.environ.get("GH_SHIM_API_EXIT", "0"))
                if status:
                    sys.exit(status)
                endpoint = args[1]
                if endpoint.endswith("/jobs"):
                    sys.stdout.write(os.environ["GH_SHIM_JOBS"])
                elif "/check-runs/" in endpoint and endpoint.endswith("/annotations"):
                    annotations = json.loads(os.environ["GH_SHIM_ANNOTATIONS"])
                    job_id = endpoint.split("/")[-2]
                    sys.stdout.write(json.dumps([annotations.get(job_id, [])]))
                else:
                    sys.exit("Unexpected gh api endpoint: " + endpoint)
            elif args[:2] == ["run", "rerun"]:
                sys.exit(int(os.environ.get("GH_SHIM_RERUN_EXIT", "0")))
            else:
                sys.exit("Unexpected gh arguments: " + repr(args))
            """), encoding="utf-8")
        shim.chmod(0o755)

    def run_retry(self, jobs=None, annotations=None, **overrides):
        if jobs is None:
            jobs = [self.job]
        if annotations is None:
            annotations = self.annotations
        if self.log.exists():
            self.log.unlink()
        # Use a fresh environment: no real token, shell startup files or gh config.
        env = {
            "PATH": os.pathsep.join((str(self.tmp), str(Path(sys.executable).parent), os.defpath)),
            "HOME": str(self.tmp), "USERPROFILE": str(self.tmp),
            "GH_TOKEN": "fake", "GH_REPO": self.GH_REPO,
            "RUN_ID": self.RUN_ID, "RUN_ATTEMPT": "1", "RUN_CONCLUSION": "failure",
            "GH_SHIM_LOG": str(self.log),
            "GH_SHIM_JOBS": json.dumps([{"total_count": len(jobs), "jobs": jobs}]),
            "GH_SHIM_ANNOTATIONS": json.dumps(annotations),
        }
        env.update(overrides)
        done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", self.script],
                              cwd=str(self.tmp), env=env, timeout=20,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []
        return done, calls

    def assert_result(self, done, calls, status, rerun=False):
        self.assertEqual(done.returncode, status, done.stdout + done.stderr)
        expected = [["run", "rerun", self.RUN_ID, "--repo", self.GH_REPO]] if rerun else []
        self.assertEqual([args for args in calls if args[:2] == ["run", "rerun"]], expected, calls)

    def test_workflow_has_only_the_required_privileges_and_trusted_inputs(self):
        workflow = self.WORKFLOW.read_text(encoding="utf-8")
        trigger = workflow.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
        self.assertEqual([line for line in trigger.splitlines() if line.strip()],
                         ["  workflow_run:", '    workflows: ["macOS desktop"]', "    types: [completed]"])
        permissions = workflow.split("\npermissions:\n", 1)[1].split("\njobs:\n", 1)[0]
        self.assertEqual(sorted(line.strip() for line in permissions.splitlines() if line.strip()),
                         ["actions: write", "contents: read"])
        job = workflow.split("\n  retry:\n", 1)[1]
        self.assertNotIn("permissions:", job)
        self.assertNotIn("uses:", job)
        self.assertIn("    if: github.event.workflow_run.head_repository.full_name == github.repository",
                      job.split("\n    steps:\n", 1)[0].splitlines())
        step = job.split("\n      - name: " + self.STEP + "\n", 1)[1]
        env = step.split("        env:\n", 1)[1].split("\n        run: |\n", 1)[0]
        self.assertEqual(sorted(line.strip().split(":", 1)[0] for line in env.splitlines() if line.strip()),
                         ["GH_REPO", "GH_TOKEN", "RUN_ATTEMPT", "RUN_CONCLUSION", "RUN_ID"])
        self.assertNotIn("${{", self.script)

    def test_not_acquired_attempt_requests_exactly_one_rerun(self):
        done, calls = self.run_retry()
        self.assert_result(done, calls, 0, rerun=True)
        self.assertEqual(calls, [
            ["api", "repos/%s/actions/runs/%s/attempts/1/jobs" % (self.GH_REPO, self.RUN_ID),
             "--paginate", "--slurp"],
            ["api", "repos/%s/check-runs/%s/annotations" % (self.GH_REPO, self.job["id"]),
             "--paginate", "--slurp"],
            ["run", "rerun", self.RUN_ID, "--repo", self.GH_REPO],
        ])

    def test_concurrency_cancelled_attempt_is_not_retried(self):
        annotations = {str(self.job["id"]): [{
            "annotation_level": "failure", "title": "", "raw_details": "",
            "message": "Canceling since a higher priority waiting request for macOS desktop-refs/pull/34/merge exists",
        }]}
        done, calls = self.run_retry(annotations=annotations, RUN_ATTEMPT="3", RUN_CONCLUSION="cancelled")
        self.assert_result(done, calls, 0)

    def test_attempt_limit_and_success_do_not_call_gh(self):
        for overrides in ({"RUN_ATTEMPT": "4"}, {"RUN_ATTEMPT": "5"}, {"RUN_CONCLUSION": "success"}):
            with self.subTest(overrides=overrides):
                done, calls = self.run_retry(**overrides)
                self.assert_result(done, calls, 0)
                self.assertEqual(calls, [])

    def test_invalid_event_identifiers_cannot_run_commands(self):
        pwned, pwned2 = self.tmp / "pwned", self.tmp / "pwned2"
        cases = (
            {"RUN_ID": "1; touch " + str(pwned)},
            {"RUN_ID": "$(touch " + str(pwned2) + ")"},
            {"RUN_ID": ""},
            {"RUN_ATTEMPT": "01/../2"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                done, calls = self.run_retry(**overrides)
                self.assert_result(done, calls, 1)
                self.assertEqual(calls, [])
                self.assertFalse(pwned.exists())
                self.assertFalse(pwned2.exists())

    def test_started_step_rejects_a_forged_not_acquired_annotation(self):
        annotations = {str(self.job["id"]): [{
            "annotation_level": "failure", "title": "", "raw_details": "",
            "message": "::error:: fake The job was NOT ACQUIRED",
        }]}
        # Each independent sign of execution must veto even a matching annotation.
        for evidence in ({"started_at": "2026-11-16T16:54:02Z"},
                         {"status": "in_progress"}, {"conclusion": "failure"}):
            with self.subTest(evidence=evidence):
                step = {"name": "Set up job", "number": 1, "started_at": None,
                        "status": "completed", "conclusion": "skipped"}
                step.update(evidence)
                done, calls = self.run_retry(jobs=[dict(self.job, steps=[step])], annotations=annotations)
                self.assert_result(done, calls, 0)
                self.assertEqual(len(calls), 1, calls)  # No annotations needed once a step started.

    def test_skipped_job_prevents_retry_in_either_order(self):
        skipped = dict(self.job, id=133458036380, conclusion="skipped")
        for jobs in ([self.job, skipped], [skipped, self.job]):
            with self.subTest(order=[job["id"] for job in jobs]):
                done, calls = self.run_retry(jobs=jobs)
                self.assert_result(done, calls, 0)

    def test_malformed_data_and_api_failure_fail_without_rerun(self):
        cases = (
            ("null steps", {"steps": None}, {}),
            ("non-numeric job id", {"id": "1/../2"}, {}),
            ("non-JSON reply", {}, {"GH_SHIM_JOBS": "not json"}),
            ("gh api failure", {}, {"GH_SHIM_API_EXIT": "1"}),
        )
        for label, changes, overrides in cases:
            with self.subTest(label=label):
                done, calls = self.run_retry(jobs=[dict(self.job, **changes)], **overrides)
                self.assert_result(done, calls, 1)

    def test_failed_rerun_is_reported(self):
        done, calls = self.run_retry(GH_SHIM_RERUN_EXIT="1")
        self.assert_result(done, calls, 1, rerun=True)


if __name__ == "__main__":
    unittest.main()
