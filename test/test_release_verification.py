"""Release verification gate: scripts/check_release_verification.py.

Each test builds a small git repository in a temporary directory with a
release notes page and a verification record, then checks it. Git runs with a
clean environment: a throwaway HOME, no GIT_* variables, no global or system
config, and user.name/user.email set local to the fixture repository. Nothing
reads the network or the real ~/.myflames.

Run:
  python3 -m unittest discover -s test -p "test_release_verification.py" -v
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, str(REPO / "scripts" / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verification = _load("check_release_verification")

VERSION = "9.9.9"

PAGE = """# myflames 9.9.9 (2026-11-15)

myflames 9.9.9 is available for macOS on Apple Silicon.

## Release highlights

- Highlights are not evidence entries.

## New features

- Adds **Cluster** mode switches through [MySQL Shell](https://dev.mysql.com/doc/mysql-shell/en/) with `auto_increment` updates.

## Improvements

- Faster loading.

## Bug fixes

- Fixed an issue where Cluster refused a mode switch.

## Additional resources

- [myflames](https://myflames.io/download.html)
"""


def _shot(width, theme, digest="a" * 64):
    return {"width": width, "theme": theme,
            "file": "local/cluster-%d-%s.png" % (width, theme), "sha256": digest}


def _record(commit):
    """A fully valid record whose evidence all points at *commit*."""
    return {
        "version": VERSION,
        "items": [
            {"entry": "Adds Cluster mode switches", "section": "New features",
             "needs": ["tests", "live", "layout", "reviews"],
             "tests": [{"commit": commit, "command": "./run-tests.sh", "result": "Ran 7200 tests OK"}],
             "live": [{"commit": commit, "driver": "mysql", "servers": ["MySQL 8.4.13"],
                       "tools": ["MySQL Shell 8.4.10"],
                       "checks": [{"action": "switch to multi-primary", "outcome": "cluster switched"}]}],
             "layout": [{"commit": commit, "view": "Cluster", "looked_at": True,
                         "shots": [_shot(1440, "light"), _shot(1440, "dark"),
                                   _shot(720, "light"), _shot(720, "dark")]}],
             "reviews": [{"commit": commit, "reviewer": "security-reviewer", "verdict": "ship"}]},
            {"entry": "Faster loading", "section": "Improvements", "needs": ["tests"],
             "tests": [{"commit": commit, "command": "./run-tests.sh", "result": "Ran 7200 tests OK"}]},
            {"entry": "Fixed an issue where Cluster refused", "section": "Bug fixes", "needs": ["tests"],
             "tests": [{"commit": commit, "command": "./run-tests.sh", "result": "Ran 7200 tests OK"}]},
        ],
    }


class RepoTest(unittest.TestCase):
    """A fixture repository: base commit, then the record commit on top."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="myflames-release-verification-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = Path(self.tmp) / "repo"
        self.root.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["HOME"] = str(Path(self.tmp) / "home")
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = str(Path(self.tmp) / "no-global-gitconfig")
        self.env = env
        self._git("init", "-q")
        self._git("config", "user.name", "Fixture")
        self._git("config", "user.email", "fixture@example.com")
        self._write("apps/desktop/package.json", json.dumps({"version": VERSION}) + "\n")
        self._write("myflames/__init__.py", '__version__ = "%s"\n' % VERSION)
        self._write("myflames/parser.py", "def parse():\n    return None\n")
        self._write("pyproject.toml", '[project]\nversion = "%s"\n' % VERSION)
        self._write("docs/release-notes/%s.md" % VERSION, PAGE)
        self._write("docs/handbook.md", "# Handbook\n")
        self.evidence = self._commit_all("the code that ships")
        self.record = _record(self.evidence)
        self._write_record(self.record)
        self.head = self._commit_all("the release verification record")

    def _git(self, *args):
        proc = subprocess.run(["git", "-C", str(self.root)] + list(args), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), proc.stderr.decode("utf-8", "replace")))
        return proc.stdout.decode("utf-8", "replace").strip()

    def _write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _write_record(self, record):
        self._write("docs/release-verification/%s.json" % VERSION, json.dumps(record, indent=2) + "\n")

    def _commit_all(self, message):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def check(self):
        return verification.check(self.root, VERSION, "HEAD")


class TestRecord(RepoTest):

    def test_valid_record(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(verification.main([VERSION, "--root", str(self.root)]), 0)

    def test_missing_record(self):
        os.remove(str(self.root / ("docs/release-verification/%s.json" % VERSION)))
        problems = self.check()
        self.assertTrue(any("is missing" in p and "release-verification" in p for p in problems), problems)
        self.assertEqual(verification.main([VERSION, "--root", str(self.root)]), 1)

    def test_unparseable_record(self):
        self._write("docs/release-verification/%s.json" % VERSION, "{not json\n")
        problems = self.check()
        self.assertTrue(any("does not parse" in p for p in problems), problems)

    def test_version_mismatch(self):
        self.record["version"] = "9.9.8"
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("'9.9.8'" in p and VERSION in p for p in problems), problems)

    def test_prefix_matching_with_markdown(self):
        # The bullet's emphasis, link and code marks are removed before the
        # prefix test: the whole plain sentence still matches.
        self.record["items"][0]["entry"] = \
            "Adds Cluster mode switches through MySQL Shell with auto_increment updates."
        self._write_record(self.record)
        self.assertEqual(self.check(), [])

    def test_bullet_without_item(self):
        del self.record["items"][1]
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("no item covers" in p and "Improvements" in p for p in problems), problems)

    def test_extra_item(self):
        self.record["items"].append({"entry": "Something the notes do not say",
                                     "section": "Bug fixes", "needs": ["tests"],
                                     "tests": [{"commit": self.evidence, "command": "t", "result": "OK"}]})
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("matches no notes entry" in p for p in problems), problems)

    def test_one_bullet_two_items(self):
        twin = dict(self.record["items"][2])
        self.record["items"].append(twin)
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("matched by 2 items" in p for p in problems), problems)

    def test_wrong_section(self):
        self.record["items"][2]["section"] = "Improvements"
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("section" in p and "Bug fixes" in p for p in problems), problems)


class TestNeeds(RepoTest):

    def test_missing_need(self):
        item = self.record["items"][0]
        item["live"] = []
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("'live'" in p and "no live evidence" in p for p in problems), problems)

    def test_needs_without_tests(self):
        self.record["items"][0]["needs"] = ["live", "layout", "reviews"]
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("must include tests" in p for p in problems), problems)

    def test_needs_empty(self):
        self.record["items"][0]["needs"] = []
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("non-empty subset" in p for p in problems), problems)

    def test_unverified_accepted_by_owner(self):
        item = self.record["items"][0]
        item["live"] = []
        item["unverified"] = [{"what": "Windows clean install", "need": "live",
                               "accepted_by_owner": "ship it; the installer changed in packaging only"}]
        self._write_record(self.record)
        self.assertEqual(self.check(), [])

    def test_unverified_without_acceptance(self):
        item = self.record["items"][0]
        item["live"] = []
        item["unverified"] = [{"what": "Windows clean install", "need": "live", "accepted_by_owner": ""}]
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("'live'" in p and "accepted" in p for p in problems), problems)


class TestLayout(RepoTest):

    def test_layout_with_three_shots(self):
        del self.record["items"][0]["layout"][0]["shots"][3]
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("720 px dark" in p for p in problems), problems)

    def test_looked_at_false(self):
        self.record["items"][0]["layout"][0]["looked_at"] = False
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("looked_at" in p for p in problems), problems)

    def test_bad_sha256(self):
        self.record["items"][0]["layout"][0]["shots"][0]["sha256"] = "not-a-hash"
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("64-hex sha256" in p for p in problems), problems)

    def test_local_sha256_mismatch(self):
        shot = self.record["items"][0]["layout"][0]["shots"][0]
        shot["sha256"] = hashlib.sha256(b"the real pixels").hexdigest()
        self._write_record(self.record)
        self._write(shot["file"], "other pixels")
        problems = self.check()
        self.assertTrue(any("hashes to" in p and shot["file"] in p for p in problems), problems)
        self._write(shot["file"], "the real pixels")
        self.assertEqual(self.check(), [])


class TestCommits(RepoTest):

    def test_unknown_commit(self):
        self.record["items"][0]["tests"][0]["commit"] = "0" * 40
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("does not exist" in p for p in problems), problems)

    def test_non_ancestor_commit(self):
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        self._git("checkout", "-q", "-b", "side")
        self._write("myflames/parser.py", "def parse():\n    return 'side'\n")
        side = self._commit_all("a commit that never ships")
        self._git("checkout", "-q", branch)
        self.record["items"][0]["tests"][0]["commit"] = side
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("not an ancestor" in p and side[:12] in p for p in problems), problems)

    def test_code_change_after_evidence(self):
        self._write("myflames/parser.py", "def parse():\n    return 'changed after the evidence'\n")
        self._commit_all("a fix after the checks ran")
        problems = self.check()
        self.assertTrue(any("myflames/parser.py" in p and "must run again" in p for p in problems), problems)

    def test_docs_only_change_after_evidence(self):
        self._write("docs/handbook.md", "# Handbook\n\nA late documentation fix.\n")
        self._write("README.md", "# myflames\n")
        self._commit_all("docs and README after the checks ran")
        self.assertEqual(self.check(), [])


if __name__ == "__main__":
    unittest.main()
