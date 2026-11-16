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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def _tests(commit):
    return {"commit": commit, "command": "./run-tests.sh", "result": "Ran 7200 tests OK (skipped=17)",
            "outcome": "pass", "executed": 7183, "skipped": 17}


def _record(commit):
    """A fully valid record whose evidence all points at *commit*."""
    return {
        "version": VERSION,
        "items": [
            {"entry": "Adds Cluster mode switches", "section": "New features",
             "needs": ["tests", "live", "layout", "reviews"],
             "tests": [_tests(commit)],
             "live": [{"commit": commit, "outcome": "pass", "driver": "mysql", "servers": ["MySQL 8.4.13"],
                       "tools": ["MySQL Shell 8.4.10"],
                       "checks": [{"action": "switch to multi-primary", "outcome": "cluster switched"}]}],
             "layout": [{"commit": commit, "view": "Cluster", "looked_at": True,
                         "shots": [_shot(1440, "light"), _shot(1440, "dark"),
                                   _shot(720, "light"), _shot(720, "dark")]}],
             "reviews": [{"commit": commit, "reviewer": "security-reviewer", "verdict": "ship"}]},
            {"entry": "Faster loading", "section": "Improvements", "needs": ["tests"],
             "tests": [_tests(commit)]},
            {"entry": "Fixed an issue where Cluster refused", "section": "Bug fixes", "needs": ["tests"],
             "tests": [_tests(commit)]},
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
        env["USERPROFILE"] = env["HOME"]
        env["MYFLAMES_SECRET_BACKEND"] = "vault"
        env.pop("MYFLAMES_VAULT_PASSPHRASE", None)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = str(Path(self.tmp) / "no-global-gitconfig")
        self.env = env
        # The checker invokes git too, so isolate its environment as well.
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self._git("init", "-q")
        self._git("config", "user.name", "Fixture")
        self._git("config", "user.email", "fixture@example.com")
        self._write("apps/desktop/package.json", json.dumps({
            "version": "9.9.8", "private": True, "devDependencies": {"electron": "40.0.0"}}) + "\n")
        self._write("apps/desktop/package-lock.json", json.dumps({
            "version": "9.9.8", "lockfileVersion": 3,
            "packages": {"": {"version": "9.9.8", "devDependencies": {"electron": "40.0.0"}},
                         "node_modules/electron": {"version": "40.0.0"}}}) + "\n")
        self._write("myflames/__init__.py", '__version__ = "9.9.8"\n')
        self._write("myflames/parser.py", "def parse():\n    return None\n")
        self._write("pyproject.toml", '[project]\nversion = "9.9.8"\n\n[tool.other]\nversion = "1.0.0"\n')
        self._write("docs/release-notes/%s.md" % VERSION, PAGE)
        self._write("docs/handbook.md", "# Handbook\n")
        self._write("docs/handbook/quickstart.md", "# Quickstart\n")
        self._write("docs/schemas/plan.json", '{"type": "object"}\n')
        self._write("docs/brand/myflames-app-icon.png", "the packaged Windows icon\n")
        self._write("docs/brand/myflames-app-icon.icns", "the packaged macOS icon\n")
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

    def _read_json(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

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

    def test_complete_record_passes_command_line_entry_point(self):
        done = subprocess.run([sys.executable, str(REPO / "scripts/check_release_verification.py"),
                               VERSION, "--root", str(self.root)], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertIn("Release 9.9.9 is verified: every notes entry has evidence from the code that ships.", done.stdout)

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
                                     "tests": [_tests(self.evidence)]})
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("matches no notes entry" in p for p in problems), problems)

    def test_one_bullet_two_items(self):
        twin = dict(self.record["items"][2])
        self.record["items"].append(twin)
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("matched by 2 items" in p for p in problems), problems)

    def test_one_item_cannot_cover_two_bullets_in_its_section(self):
        first = "Fixed an issue where Cluster refused a mode switch."
        second = "Fixed an issue where the editor lost its settings."
        self._write("docs/release-notes/%s.md" % VERSION,
                    PAGE.replace("- " + first, "- " + first + "\n- " + second))
        self.record["items"][2]["entry"] = "Fixed"
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("item 'Fixed'" in p and "matches 2 bullets" in p
                            and "Bug fixes" in p and first in p and second in p
                            for p in problems), problems)

    def test_two_bullets_with_distinct_item_prefixes_pass(self):
        first = "Fixed an issue where Cluster refused a mode switch."
        second = "Fixed an issue where the editor lost its settings."
        self._write("docs/release-notes/%s.md" % VERSION,
                    PAGE.replace("- " + first, "- " + first + "\n- " + second))
        self.record["items"].append({"entry": "Fixed an issue where the editor",
                                     "section": "Bug fixes", "needs": ["tests"],
                                     "tests": [_tests(self.evidence)]})
        self._write_record(self.record)
        self.assertEqual(self.check(), [])

    def test_wrong_section(self):
        self.record["items"][2]["section"] = "Improvements"
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("section" in p and "Bug fixes" in p for p in problems), problems)

    def test_markdown_only_or_blank_entries_are_rejected(self):
        for bad_entry in ("**", "`", "_", "[](x)", "   "):
            with self.subTest(entry=bad_entry):
                self.record["items"][0]["entry"] = bad_entry
                self._write_record(self.record)
                problems = self.check()
                self.assertTrue(
                    any("item %r contains no word character" % (bad_entry,) in p for p in problems),
                    problems,
                )
                self.assertTrue(
                    any("no item covers" in p and "New features" in p for p in problems),
                    problems,
                )
        self.record["items"][0]["entry"] = "Adds Cluster mode switches"
        self._write_record(self.record)
        self.assertEqual(self.check(), [])


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


class TestOutcomes(RepoTest):

    def assert_field_problem(self, kind, field):
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("item 'Adds Cluster mode switches'" in p and kind in p and field in p
                            for p in problems), problems)

    def test_failed_tests_outcome(self):
        entry = self.record["items"][0]["tests"][0]
        entry.update(outcome="fail", result="FAILED (failures=1)")
        self.assert_field_problem("tests", "outcome")

    def test_zero_tests_executed(self):
        self.record["items"][0]["tests"][0]["executed"] = 0
        self.assert_field_problem("tests", "executed")

    def test_missing_tests_outcome(self):
        del self.record["items"][0]["tests"][0]["outcome"]
        self.assert_field_problem("tests", "outcome")

    def test_counts_require_integers_not_booleans_or_coercible_values(self):
        entry = self.record["items"][0]["tests"][0]
        for field in ("executed", "skipped"):
            original = entry[field]
            for value in (True, False, -1, 1.0, "1", None):
                with self.subTest(field=field, value=value):
                    entry[field] = value
                    self.assert_field_problem("tests", field)
            del entry[field]
            self.assert_field_problem("tests", field)
            entry[field] = original

    def test_one_executed_and_zero_skipped_pass(self):
        self.record["items"][0]["tests"][0].update(executed=1, skipped=0)
        self._write_record(self.record)
        self.assertEqual(self.check(), [])

    def test_test_command_and_result_remain_required_text(self):
        entry = self.record["items"][0]["tests"][0]
        for field in ("command", "result"):
            original = entry[field]
            for value in (None, "", " \n", 1, ["text"]):
                with self.subTest(field=field, value=value):
                    entry[field] = value
                    self.assert_field_problem("tests", field)
            entry[field] = original

    def test_live_requires_passing_outcome(self):
        entry = self.record["items"][0]["live"][0]
        for value in ("fail", "partial", True, None):
            with self.subTest(value=value):
                entry["outcome"] = value
                self.assert_field_problem("live", "outcome")
        del entry["outcome"]
        self.assert_field_problem("live", "outcome")

    def test_live_requires_nonempty_servers_with_nonempty_names(self):
        entry = self.record["items"][0]["live"][0]
        for value in ([], None, "MySQL 8.4.13", {}, [None], [1], [True], [""], [" \n"],
                      ["MySQL 8.4.13", ""]):
            with self.subTest(value=value):
                entry["servers"] = value
                self.assert_field_problem("live", "servers")
        del entry["servers"]
        self.assert_field_problem("live", "servers")

    def test_live_requires_nonempty_checks(self):
        entry = self.record["items"][0]["live"][0]
        for value in ([], None, "switched the cluster", {}):
            with self.subTest(value=value):
                entry["checks"] = value
                self.assert_field_problem("live", "checks")
        del entry["checks"]
        self.assert_field_problem("live", "checks")

    def test_live_empty_servers_and_checks_cannot_claim_pass(self):
        self.record["items"][0]["live"][0].update(servers=[], checks=[])
        self.assert_field_problem("live", "servers")
        self.assert_field_problem("live", "checks")

    def test_live_checks_require_objects(self):
        entry = self.record["items"][0]["live"][0]
        for value in (None, "switched the cluster", False, 1, []):
            with self.subTest(value=value):
                entry["checks"] = [value]
                self.assert_field_problem("live", "checks")

    def test_live_checks_require_nonempty_action_and_outcome(self):
        # A later incomplete check must fail even when an earlier check passed.
        entry = self.record["items"][0]["live"][0]
        check = dict(entry["checks"][0])
        entry["checks"].append(check)
        for field in ("action", "outcome"):
            original = check[field]
            for value in (None, "", " \n", True, 1, ["text"], {}):
                with self.subTest(field=field, value=value):
                    check[field] = value
                    self.assert_field_problem("live", "checks[1]." + field)
            del check[field]
            with self.subTest(field=field, missing=True):
                self.assert_field_problem("live", "checks[1]." + field)
            check[field] = original

    def test_live_with_servers_and_complete_checks_passes(self):
        entry = self.record["items"][0]["live"][0]
        entry["servers"].append("MariaDB 11.4.9")
        entry["checks"].append({"action": "read replication status", "outcome": "replication is running"})
        self._write_record(self.record)
        self.assertEqual(self.check(), [])

    def test_review_block_is_rejected(self):
        self.record["items"][0]["reviews"][0]["verdict"] = "block"
        self.assert_field_problem("reviews", "verdict")

    def test_review_free_text_is_rejected(self):
        self.record["items"][0]["reviews"][0]["verdict"] = "0 wrong claims; residual: follow up later"
        self.assert_field_problem("reviews", "verdict")

    def test_review_changes_requested_and_missing_verdict_are_rejected(self):
        entry = self.record["items"][0]["reviews"][0]
        entry["verdict"] = "changes-requested"
        self.assert_field_problem("reviews", "verdict")
        del entry["verdict"]
        self.assert_field_problem("reviews", "verdict")

    def test_ship_with_follow_ups_requires_follow_ups(self):
        self.record["items"][0]["reviews"][0]["verdict"] = "ship-with-follow-ups"
        self.assert_field_problem("reviews", "follow_ups")

    def test_follow_ups_must_be_a_nonempty_list_of_nonempty_strings(self):
        entry = self.record["items"][0]["reviews"][0]
        entry["verdict"] = "ship-with-follow-ups"
        for value in ([], "later", [None], [1], [""], [" "], ["valid", False]):
            with self.subTest(value=value):
                entry["follow_ups"] = value
                self.assert_field_problem("reviews", "follow_ups")

    def test_ship_with_documented_follow_ups_passes(self):
        self.record["items"][0]["reviews"][0].update(
            verdict="ship-with-follow-ups", follow_ups=["Document the optional keyboard shortcut."])
        self._write_record(self.record)
        self.assertEqual(self.check(), [])

    def test_accepted_unverified_does_not_hide_failed_evidence(self):
        item = self.record["items"][0]
        item["tests"][0]["outcome"] = "fail"
        item["unverified"] = [{"need": "tests", "accepted_by_owner": "ship without that check"}]
        self.assert_field_problem("tests", "outcome")

    def test_a_passing_entry_does_not_hide_a_failed_entry(self):
        item = self.record["items"][0]
        item["tests"].append(dict(item["tests"][0], outcome="fail"))
        self.assert_field_problem("tests", "outcome")


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

    def assert_code_changed(self, path):
        problems = self.check()
        self.assertTrue(any(path in p and "must run again" in p for p in problems), problems)

    def test_unknown_commit(self):
        self.record["items"][0]["tests"][0]["commit"] = "0" * 40
        self._write_record(self.record)
        problems = self.check()
        self.assertTrue(any("does not exist" in p for p in problems), problems)

    def test_symbolic_or_abbreviated_evidence_commits_are_rejected(self):
        self.assertEqual(self.check(), [])
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        self._write("myflames/parser.py", "def parse():\n    return 'changed after evidence'\n")
        head_sha = self._commit_all("code change before tagging")
        self._git("tag", "v9.9.9", "HEAD")
        self._git("tag", "-a", "-m", "release tag object", "v9.9.9-annotated", "HEAD")
        annotated_tag_sha = self._git("rev-parse", "v9.9.9-annotated")
        for rev in ("HEAD", "@", "HEAD~0", branch, "v9.9.9",
                    self.evidence[:12], self.evidence.upper(), annotated_tag_sha):
            with self.subTest(rev=rev):
                self._write_record(_record(rev))
                problems = self.check()
                self.assertTrue(
                    any("evidence commit %r must be the full commit SHA" % (rev,) in p for p in problems),
                    problems,
                )
        self._write_record(_record(head_sha))
        self._commit_all("record full sha for head code")
        self.assertEqual(self.check(), [])

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

    def test_package_dependency_added_after_evidence(self):
        path = "apps/desktop/package.json"
        package = self._read_json(path)
        package["devDependencies"]["electron-builder"] = "26.0.0"
        self._write(path, json.dumps(package))
        self._commit_all("add a dependency after evidence")
        self.assert_code_changed(path)

    def test_lockfile_dependency_version_changed_after_evidence(self):
        path = "apps/desktop/package-lock.json"
        lock = self._read_json(path)
        lock["packages"]["node_modules/electron"]["version"] = "41.0.0"
        self._write(path, json.dumps(lock))
        self._commit_all("change a dependency lock after evidence")
        self.assert_code_changed(path)

    def test_schema_changed_after_evidence(self):
        path = "docs/schemas/plan.json"
        self._write(path, '{"type": "array"}\n')
        self._commit_all("change a schema contract after evidence")
        self.assert_code_changed(path)

    def test_packaged_icons_changed_after_evidence_invalidate_it(self):
        for path in ("docs/brand/myflames-app-icon.png", "docs/brand/myflames-app-icon.icns"):
            with self.subTest(path=path):
                self._write(path, "a changed packaged icon\n")
                self._commit_all("change a packaged icon after evidence")
                self.assert_code_changed(path)

    def test_version_only_bump_in_all_four_files_passes(self):
        for path in ("apps/desktop/package.json", "apps/desktop/package-lock.json"):
            package = self._read_json(path)
            package["version"] = VERSION
            if path.endswith("package-lock.json"):
                package["packages"][""]["version"] = VERSION
            self._write(path, json.dumps(package, indent=2, sort_keys=True) + "\n")
        self._write("myflames/__init__.py", '__version__ = "%s"\n' % VERSION)
        self._write("pyproject.toml", '[project]\nversion = "%s"\n\n[tool.other]\nversion = "1.0.0"\n' % VERSION)
        self._commit_all("bump only release versions after evidence")
        self.assertEqual(self.check(), [])

    def test_handbook_change_after_evidence_passes(self):
        self._write("docs/handbook/quickstart.md", "# Quickstart\n\nA documentation correction.\n")
        self._commit_all("update the handbook after evidence")
        self.assertEqual(self.check(), [])

    def test_app_readme_change_after_evidence_passes(self):
        self._write("apps/desktop/README.md", "# Desktop\n")
        self._write("apps/workspace/README.md", "# Workspace\n")
        self._commit_all("update app documentation after evidence")
        self.assertEqual(self.check(), [])

    def test_markdown_outside_documentation_exemptions_invalidates(self):
        for path in ("myflames/runtime.md", "apps/desktop/src/README.md", "docs/schemas/README.md"):
            with self.subTest(path=path):
                self._write(path, "A shipped contract.\n")
                self._commit_all("add non-exempt markdown after evidence")
                self.assert_code_changed(path)

    def test_python_initializer_logic_changed_after_evidence(self):
        path = "myflames/__init__.py"
        self._write(path, '__version__ = "9.9.8"\nENABLED = True\n')
        self._commit_all("change initializer logic after evidence")
        self.assert_code_changed(path)

    def test_pyproject_dependency_changed_after_evidence(self):
        path = "pyproject.toml"
        self._write(path, '[project]\nversion = "9.9.8"\ndependencies = ["new-package"]\n')
        self._commit_all("change Python dependencies after evidence")
        self.assert_code_changed(path)

    def test_pyproject_other_table_version_is_not_exempt(self):
        path = "pyproject.toml"
        text = (self.root / path).read_text(encoding="utf-8").replace('version = "1.0.0"', 'version = "2.0.0"')
        self._write(path, text)
        self._commit_all("change a tool version after evidence")
        self.assert_code_changed(path)

    def test_version_line_cannot_hide_code_or_comment_changes(self):
        path = "myflames/__init__.py"
        for text in ('__version__ = "9.9.9"; ENABLED = True\n', '__version__ = "9.9.9" # changed comment\n'):
            with self.subTest(text=text):
                self._write(path, text)
                self._commit_all("change more than the version value")
                self.assert_code_changed(path)

    def test_malformed_missing_or_duplicate_package_versions_fail_closed(self):
        path = "apps/desktop/package.json"
        for text in ('{not json', '{}', '{"version": 42}',
                     '{"version": "9.9.8", "version": "9.9.9"}'):
            with self.subTest(text=text):
                self._write(path, text)
                self._commit_all("write an invalid package manifest after evidence")
                self.assert_code_changed(path)

    def test_json_boolean_to_integer_change_is_not_version_only(self):
        path = "apps/desktop/package.json"
        package = self._read_json(path)
        package["private"] = 1
        self._write(path, json.dumps(package))
        self._commit_all("change a JSON scalar type after evidence")
        self.assert_code_changed(path)

    def test_removing_version_file_invalidates_evidence(self):
        path = "apps/desktop/package-lock.json"
        (self.root / path).unlink()
        self._commit_all("remove the lockfile after evidence")
        self.assert_code_changed(path)

    def test_renaming_code_into_docs_invalidates_evidence(self):
        self._git("mv", "myflames/parser.py", "docs/parser.py")
        self._commit_all("move source into documentation after evidence")
        self.assert_code_changed("myflames/parser.py")

    def test_version_file_mode_change_invalidates_evidence(self):
        path = "myflames/__init__.py"
        self._git("update-index", "--chmod=+x", path)
        self._git("commit", "-q", "-m", "change initializer file mode")
        self.assert_code_changed(path)

    def test_version_file_line_endings_are_not_exempt(self):
        path = "pyproject.toml"
        self._git("config", "core.autocrlf", "false")
        data = (self.root / path).read_bytes().replace(b"\n", b"\r\n")
        (self.root / path).write_bytes(data)
        self._commit_all("change line endings after evidence")
        self.assert_code_changed(path)

    def test_version_only_bump_preserves_existing_crlf(self):
        self._git("config", "core.autocrlf", "false")
        paths = ("pyproject.toml", "myflames/__init__.py")
        for path in paths:
            data = (self.root / path).read_bytes().replace(b"\n", b"\r\n")
            (self.root / path).write_bytes(data)
        evidence = self._commit_all("evidence was run on CRLF files")
        self._write_record(_record(evidence))
        for path in paths:
            data = (self.root / path).read_bytes().replace(b'"9.9.8"', b'"9.9.9"')
            (self.root / path).write_bytes(data)
        self._commit_all("bump only version values with CRLF intact")
        self.assertEqual(self.check(), [])


class TestWorkflows(unittest.TestCase):

    WORKFLOWS = ("test.yml", "integration.yml", "fixtures-drift.yml", "publish.yml")

    def _read_workflow(self, name):
        return (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")

    def test_top_level_read_only_contents_permissions(self):
        for name in self.WORKFLOWS:
            with self.subTest(workflow=name):
                text = self._read_workflow(name)
                self.assertIn("\njobs:\n", text)
                header = text.split("\njobs:\n", 1)[0]
                self.assertRegex(header, r"(?m)^permissions:\n  contents: read$")

    def test_publish_workflow_checkout_credentials_and_pinned_action(self):
        text = self._read_workflow("publish.yml")
        checkouts = []
        current = []
        for line in text.splitlines():
            if line.startswith("      - "):
                if current:
                    checkouts.append("\n".join(current))
                current = [line]
            elif current and (line.startswith("        ") or not line.strip()):
                current.append(line)
            elif current:
                checkouts.append("\n".join(current))
                current = []
        if current:
            checkouts.append("\n".join(current))
        checkout_steps = [step for step in checkouts if "uses: actions/checkout@" in step]
        self.assertTrue(checkout_steps, "expected at least one actions/checkout step in publish.yml")
        for step in checkout_steps:
            self.assertRegex(step, r"(?m)^\s+persist-credentials:\s*false$")
        self.assertIn(
            "uses: pypa/gh-action-pypi-publish@7245906fb61fa33a13554f9d5fb856fe61758086 # v1.14.3",
            text,
        )
        self.assertNotIn("pypa/gh-action-pypi-publish@release/", text)
        self.assertRegex(text, r"(?m)^    permissions:\n      id-token: write\b")


if __name__ == "__main__":
    unittest.main()
