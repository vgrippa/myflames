"""scripts/ga-matrix.sh profiles: the MYFLAMES_HOME guard.

The script runs from a copy in a temporary directory (its LAB, state and
credentials live next to the copy), with a temporary HOME and stub docker and
python3 commands, so no test touches the repository, ~/.myflames or Docker.
"""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ga-matrix.sh"


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "needs bash and POSIX symlinks")
class TestMyflamesHomeGuard(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.home.chmod(0o755)
        repo = self.root / "repo"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(str(SCRIPT), str(repo / "scripts" / "ga-matrix.sh"))
        self.lab = repo / "local" / "ga-matrix"
        bindir = self.root / "bin"
        bindir.mkdir()
        for name in ("docker", "python3"):
            stub = bindir / name
            stub.write_text("#!/bin/sh\nexit 1\n")
            stub.chmod(0o755)
        self.creds = self.root / "credentials.env"
        self.creds.write_text("ROOT_PASSWORD=x\nMF_ADMIN_PASSWORD=x\nMF_READ_PASSWORD=x\nREPL_PASSWORD=x\n")
        self.script = repo / "scripts" / "ga-matrix.sh"
        self.path = str(bindir) + os.pathsep + "/usr/bin:/bin"

    def run_profiles(self, myflames_home, creds=None, command="profiles", cwd=None):
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "PATH": self.path,
               "MYFLAMES_SECRET_BACKEND": "vault", "MYFLAMES_HOME": str(myflames_home),
               "CREDS_FILE": str(creds or self.creds)}
        return subprocess.run(["bash", str(self.script), command], env=env, cwd=str(cwd or self.root),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def snapshot(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))

    def assert_refused(self, myflames_home, reason):
        for command in ("profiles", "env"):
            before = self.snapshot()
            result = self.run_profiles(myflames_home, command=command)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(reason, result.stderr.decode())
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o755)
            self.assertFalse(self.lab.exists())

    def test_regression_real_home_by_physical_path_is_refused_before_creating(self):
        link = self.root / "home-link"
        link.symlink_to(self.home)
        (self.home / "sub").mkdir()
        for value in (str(self.home) + "/.", str(self.home) + "/", str(self.home) + "//", link,
                      str(link) + "/.", str(self.home / "sub") + "/..", self.root, Path("home")):
            with self.subTest(myflames_home=str(value)):
                self.assert_refused(value, "must not be your real home")

    def test_regression_dot_dot_after_a_symlink_is_resolved_physically(self):
        """bash's logical cd reads x/lnk/.. as x; the kernel (mkdir, chmod,
        Python) follows lnk first, here to the home."""
        (self.home / "sub").mkdir()
        (self.root / "x").mkdir()
        (self.root / "x" / "lnk").symlink_to(self.home / "sub")
        self.assert_refused(str(self.root / "x" / "lnk") + "/..", "must not be your real home")

    def test_regression_store_myflames_would_use_must_not_be_the_real_store(self):
        """myflames writes $MYFLAMES_HOME/.myflames: refused when that is a
        symlink to the real store, or when the real ~/.myflames is a symlink
        into MYFLAMES_HOME."""
        lab = self.root / "lab"
        lab.mkdir()
        (self.home / ".myflames").mkdir()
        (lab / ".myflames").symlink_to(self.home / ".myflames")
        self.assert_refused(lab, "must not be your real ~/.myflames")
        (lab / ".myflames").unlink()
        (self.home / ".myflames").rmdir()
        elsewhere = self.root / "dotfiles"
        (elsewhere / ".myflames").mkdir(parents=True)
        (self.home / ".myflames").symlink_to(elsewhere / ".myflames")
        self.assert_refused(elsewhere, "must not be your real ~/.myflames")

    def test_regression_relative_home_is_used_as_its_physical_path(self):
        """The profile steps run after cd "$REPO": a relative MYFLAMES_HOME
        must name the directory the guard checked."""
        stop = self.root / "stop.env"
        stop.write_text('echo "MYFLAMES_HOME=$MYFLAMES_HOME" >&2; exit 7\n')
        work = self.home / "a" / "b"
        work.mkdir(parents=True)
        result = self.run_profiles("..", creds=stop, cwd=work)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("MYFLAMES_HOME=%s\n" % (self.home / "a"), result.stderr.decode())

    def test_regression_inside_real_myflames_store_is_refused(self):
        self.assert_refused(self.home / ".myflames" / "lab", "inside your real ~/.myflames")
        self.assertFalse((self.home / ".myflames").exists())
        (self.home / ".myflames").mkdir()
        link = self.root / "store-link"
        link.symlink_to(self.home / ".myflames")
        for value in (self.home / ".myflames", link, link / "lab"):
            with self.subTest(myflames_home=str(value)):
                self.assert_refused(value, "inside your real ~/.myflames")

    def test_regression_missing_dot_dot_names_are_refused(self):
        """mkdir -p would create x, then the path would resolve to the home."""
        self.assert_refused(str(self.home) + "/x/..", "must be a directory path")
        self.assertFalse((self.home / "x").exists())

    def test_isolated_home_passes_the_guard_before_anything_is_created(self):
        # The credentials file stops the script right after the guard, before mkdir.
        stop = self.root / "stop.env"
        stop.write_text("exit 7\n")
        for value in (self.root / "lab" / "home", self.home / "lab", Path("relative-lab")):
            with self.subTest(myflames_home=str(value)):
                result = self.run_profiles(value, creds=stop)
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertFalse((self.root / "lab").exists())
                self.assertFalse((self.home / "lab").exists())
                self.assertFalse((self.root / "relative-lab").exists())


if __name__ == "__main__":
    unittest.main()
