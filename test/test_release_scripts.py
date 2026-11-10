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
        problems = assets.check(gh(FULL[4:] + FULL[:4]), "3.4.0")
        self.assertEqual(len(problems), 2)
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


if __name__ == "__main__":
    unittest.main()
