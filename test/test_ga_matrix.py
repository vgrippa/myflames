"""scripts/ga-matrix.sh and scripts/cluster-matrix.sh profiles: the MYFLAMES_HOME guard.

Each script runs from a copy in a temporary directory (its LAB, state and
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

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "needs bash and POSIX symlinks")
class TestMyflamesHomeGuard(unittest.TestCase):
    script_name = "ga-matrix.sh"
    lab_name = "ga-matrix"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.home.chmod(0o755)
        repo = self.root / "repo"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(str(SCRIPTS / self.script_name), str(repo / "scripts" / self.script_name))
        self.lab = repo / "local" / self.lab_name
        bindir = self.root / "bin"
        bindir.mkdir()
        for name in ("docker", "python3"):
            stub = bindir / name
            stub.write_text("#!/bin/sh\nexit 1\n")
            stub.chmod(0o755)
        self.creds = self.root / "credentials.env"
        self.creds.write_text("ROOT_PASSWORD=x\nMF_ADMIN_PASSWORD=x\nMF_READ_PASSWORD=x\nREPL_PASSWORD=x\n")
        self.script = repo / "scripts" / self.script_name
        self.path = str(bindir) + os.pathsep + "/usr/bin:/bin"

    def run_profiles(self, myflames_home, creds=None, command="profiles", cwd=None, args=()):
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "PATH": self.path,
               "MYFLAMES_SECRET_BACKEND": "vault", "MYFLAMES_HOME": str(myflames_home),
               "CREDS_FILE": str(creds or self.creds)}
        return subprocess.run(["bash", str(self.script), command] + list(args), env=env, cwd=str(cwd or self.root),
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


class TestClusterMatrixHomeGuard(TestMyflamesHomeGuard):
    """scripts/cluster-matrix.sh runs the same guard for profiles and env,
    on MYFLAMES_HOME or on --home DIR, which wins."""
    script_name = "cluster-matrix.sh"
    lab_name = "cluster-matrix"

    def test_home_option_is_guarded_and_wins_over_the_environment(self):
        stop = self.root / "stop.env"
        stop.write_text('echo "MYFLAMES_HOME=$MYFLAMES_HOME" >&2; exit 7\n')
        safe = self.root / "lab" / "home"
        for command in ("profiles", "env"):
            with self.subTest(command=command):
                before = self.snapshot()
                result = self.run_profiles(safe, command=command, args=("--home", str(self.home)))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("must not be your real home", result.stderr.decode())
                self.assertEqual(self.snapshot(), before)
                result = self.run_profiles(self.home, creds=stop, command=command, args=("--home", str(safe)))
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertIn("MYFLAMES_HOME=%s\n" % safe, result.stderr.decode())
                self.assertFalse((self.root / "lab").exists())

    def test_unknown_profiles_argument_is_refused(self):
        result = self.run_profiles(self.root / "lab", args=("--hom", "x"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("unknown argument --hom", result.stderr.decode())



@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "needs bash")
class TestGaMatrixServers(unittest.TestCase):
    """The Percona Server entries: pinned official images, MySQL rules, a GTID replica."""

    def setUp(self):
        self.text = (SCRIPTS / "ga-matrix.sh").read_text(encoding="utf-8")

    def spec(self, name):
        import re
        match = re.search(r"^\s+%s\)\s+echo \"([^\"]+)\"" % re.escape(name), self.text, re.M)
        self.assertIsNotNone(match, name)
        return match.group(1).split()

    def test_percona_servers_use_pinned_official_images(self):
        import re
        for name, port in (("percona-8.0", "33180"), ("percona-8.4", "33184"), ("percona-8.4-replica", "33185")):
            with self.subTest(name=name):
                image, engine, got_port, role = self.spec(name)[:4]
                self.assertRegex(image, r"^percona/percona-server:\d+\.\d+\.\d+-\d+\.\d+$", "an exact tag, never latest")
                self.assertEqual(engine, "mysql", "Percona Server is a MySQL flavor: the mysql client and MySQL rules")
                self.assertEqual(got_port, port)
                self.assertEqual(role, "replica" if name.endswith("replica") else "primary")
        self.assertEqual(self.spec("percona-8.4-replica")[6], "percona-8.4")
        all_line = re.search(r'^ALL="([^"]+)"', self.text, re.M).group(1).split()
        for name in ("percona-8.0", "percona-8.4", "percona-8.4-replica"):
            self.assertIn(name, all_line)
        # The replica pair replicates with GTIDs; telemetry is off.
        self.assertIn("percona-8.4|percona-8.4-replica) args+=(--gtid-mode=ON", self.text.replace(
            "mysql-9.7|mysql-9.7-replica|", ""))
        self.assertIn("PERCONA_TELEMETRY_DISABLE=1", self.text)

    def test_help_lists_percona(self):
        result = subprocess.run(["bash", str(SCRIPTS / "ga-matrix.sh")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.decode()
        self.assertIn("percona-8.0 percona-8.4", out)
        self.assertIn("percona-8.4-replica", out)
        self.assertIn("CREDS_FILE", out)  # the whole header, through Settings


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "needs bash")
class TestKillLabScenarios(unittest.TestCase):
    """Execute scenario functions with server operations replaced by a shell lab.

    These validate orchestration and the 5.7-compatible doubling loop, not
    database behavior. The coordinator runs the actual live scenarios.
    """
    def run_lab(self, command, initial=0):
        text = (SCRIPTS / "ga-matrix.sh").read_text(encoding="utf-8")
        functions = text.rsplit('\nmain "$@"', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            count_file = Path(tmp) / "count"
            count_file.write_text(str(initial))
            harness = r'''
valid() { [ "$1" = mysql-5.7 ]; }
load_creds() { :; }
is_up() { return 0; }
f() { echo primary; }
bg_session() { printf 'BG|%s|%s|%s|%s|%s\n' "$@"; }
rq() {
  local sql="$2" count
  printf 'SQL|%s\n' "$sql" >&2
  count=$(cat "$LAB_COUNT")
  case "$sql" in
    'SELECT COUNT(*)'*) echo "$count" ;;
    'INSERT INTO review.rollback_probe (v) VALUES'*) echo 1 > "$LAB_COUNT" ;;
    'INSERT INTO review.rollback_probe (v) SELECT'*)
      local deficit="${sql##*LIMIT }"; deficit="${deficit%;}"
      [ "$deficit" -lt "$count" ] || deficit="$count"
      echo "$((count + deficit))" > "$LAB_COUNT" ;;
  esac
}
'''
            result = subprocess.run(["bash", "-c", functions + harness + "\n" + command],
                                    env=dict(os.environ, LAB_COUNT=str(count_file)),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            return result, int(count_file.read_text())

    def test_background_client_options_allowlist(self):
        text = (SCRIPTS / 'ga-matrix.sh').read_text(encoding='utf-8')
        functions = text.rsplit('\nmain "$@"', 1)[0]
        # Run the real bg_session, stubbing Docker so no service is touched.
        harness = r'''
cname() { echo lab; }
client() { echo mysql; }
docker() { printf 'DOCKER|%s\n' "$*"; }
ROOT_PASSWORD=test
bg_session mysql-5.7 0 'SELECT 1;' 0 ''
'''
        for value in ('', '--force', '--force; echo INJECTED', '$(echo INJECTED)', '--force\ntrue', '-v'):
            with self.subTest(value=value):
                result = subprocess.run(['bash', '-c', functions + harness],
                                        env=dict(os.environ, BG_CLIENT_OPTS=value),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                if value in ('', '--force'):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('DOCKER|exec', result.stdout.decode())
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('BG_CLIENT_OPTS must be empty or --force', result.stderr.decode())
                    self.assertNotIn('DOCKER|', result.stdout.decode())
                    self.assertNotIn('INJECTED', result.stdout.decode())

    def test_busy_statement_runs_inside_the_blocking_transaction(self):
        result, _ = self.run_lab('cmd_locks_busy mysql-5.7 30')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('INSERT IGNORE INTO review.lock_probe VALUES (3, 0);', result.stderr.decode())
        output = result.stdout.decode()
        # COMMIT comes after the session stays idle in its transaction: a SLEEP
        # stopped by KILL QUERY returns without an error and would otherwise commit.
        self.assertIn('BG|mysql-5.7|0|BEGIN; UPDATE review.lock_probe SET v=v+1 WHERE id=3; '
                      'SELECT SLEEP(30);|60|COMMIT;', output)
        self.assertIn('BG|mysql-5.7|1|SET SESSION innodb_lock_wait_timeout=100; '
                      'UPDATE review.lock_probe SET v=v+1 WHERE id=3;', output)

    def test_rollback_doubles_and_bounds_the_last_insert(self):
        for initial, expected_inserts in ((0, 4), (2, 2), (10, 0)):
            with self.subTest(initial=initial):
                result, count = self.run_lab('cmd_rollback mysql-5.7 5 30', initial)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(count, max(5, initial))
                sql = result.stderr.decode()
                self.assertEqual(sql.count('SQL|INSERT INTO review.rollback_probe'), expected_inserts)
                self.assertIn('ENGINE=InnoDB;', sql)
                self.assertNotIn('WITH RECURSIVE', sql)
                self.assertIn('BG|mysql-5.7|0|BEGIN; UPDATE review.rollback_probe SET v=v+1;|30|ROLLBACK;',
                              result.stdout.decode())
        for rows in ('0', '-1', 'x'):
            result, _ = self.run_lab('cmd_rollback mysql-5.7 %s 30' % rows)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('ROWS must be a positive whole number', result.stderr.decode())

    def test_help_and_dispatcher_include_scenarios(self):
        result = subprocess.run(["bash", str(SCRIPTS / "ga-matrix.sh")], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('locks-busy NAME SECONDS', result.stdout.decode())
        self.assertIn('rollback NAME ROWS SECONDS', result.stdout.decode())
        for command, usage in (("locks-busy", "locks-busy NAME SECONDS"),
                                ("rollback", "rollback NAME ROWS SECONDS")):
            result = subprocess.run(["bash", str(SCRIPTS / "ga-matrix.sh"), command],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('usage: ' + usage, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
