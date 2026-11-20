"""Live suite: MySQL Shell cluster actions, run inside the lab network.

The scenarios need mysqlsh and the members by container name, so they run in
the lab-runner container (scripts/lab-runner/Dockerfile) on mf-ic-net with the
worktree mounted read-only; this module drives docker and asserts on the
scenario output. Covers: Rejoin of mf-ic-3 through the group after a Stop
(test/live/_container_rejoin.py), and a line-1 identity mismatch on a real
mysqlsh stopping before line 2 (test/live/_container_mismatch.py). The rejoin
scenario restores the member also on failure.

Run: scripts/live-tests.sh shell   (or MYFLAMES_LIVE_LABS=1 with the ic lab up)
"""
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import (REPO, check_lab, credentials_path, lab_password, redact,  # noqa: E402
                  require_live)

IMAGE = "myflames-lab-runner:8.4"
DOCKERFILE_DIR = os.path.join(REPO, "scripts", "lab-runner")
LIVE_DIR = os.path.dirname(os.path.abspath(__file__))
NETWORK = "mf-ic-net"


def setUpModule():
    require_live()


def docker(*args, timeout=600):
    proc = subprocess.run(["docker"] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True,
                          timeout=timeout)
    return proc.returncode, proc.stdout or ""


class ShellLabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_lab("ic")
        code, out = docker("image", "inspect", IMAGE)
        if code != 0:
            code, out = docker("build", "-t", IMAGE, DOCKERFILE_DIR, timeout=900)
            if code != 0:
                # Sandboxes that cannot write ~/.docker still run the legacy builder.
                env = dict(os.environ, DOCKER_BUILDKIT="0")
                proc = subprocess.run(["docker", "build", "-t", IMAGE, DOCKERFILE_DIR],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      universal_newlines=True, timeout=900, env=env)
                code, out = proc.returncode, proc.stdout or ""
            if code != 0:
                raise unittest.SkipTest("cannot build the lab-runner image: %s" % redact(out)[-500:])

    def run_container(self, *command, timeout=600):
        args = ["run", "--rm", "--network", NETWORK,
                "-v", "%s:/src:ro" % REPO,
                "-v", "%s:/run/creds.env:ro" % credentials_path(),
                "-v", "%s:/run/live:ro" % LIVE_DIR,
                "-e", "HOME=/tmp/home", "-e", "MYFLAMES_SECRET_BACKEND=vault",
                IMAGE] + list(command)
        code, out = docker(*args, timeout=timeout)
        self.assertNotIn(lab_password(), out, "the lab password leaked into container output")
        return code, redact(out)

    def test_01_rejoin_through_the_group(self):
        code, out = self.run_container("python3", "-I", "/run/live/_container_rejoin.py")
        self.assertEqual(code, 0, "rejoin scenario failed:\n%s" % out[-3000:])
        self.assertIn("LIVE SHELL REJOIN OK", out)
        self.assertNotIn("FAIL", out)

    def test_02_line1_identity_mismatch_stops_before_line2(self):
        code, out = self.run_container("sh", "/run/live/_container_mismatch.sh")
        self.assertEqual(code, 0, "mismatch scenario failed:\n%s" % out[-3000:])
        rows = {}
        for line in out.splitlines():
            match = re.match(r"^(\S(?:.*?\S)?)\s+rc=(\d+) line2_ran=(\w+) verdict='([^']*)'", line)
            if match:
                name, rc, ran2, verdict = match.groups()
                rows[name.strip()] = (int(rc), ran2 == "True", verdict)
        self.assertEqual(sorted(rows), ["right identities", "wrong group name", "wrong server uuid"],
                         "unexpected scenario output:\n%s" % out[-2000:])
        rc, ran2, _v = rows["right identities"]
        self.assertEqual((rc, ran2), (0, True),
                         "the matching identity must reach line 2:\n%s" % out[-2000:])
        for name in ("wrong group name", "wrong server uuid"):
            with self.subTest(case=name):
                rc, ran2, verdict = rows[name]
                self.assertNotEqual(rc, 0, "%s: mysqlsh exited cleanly" % name)
                self.assertFalse(ran2, "%s: line 2 ran after the identity mismatch" % name)
                self.assertEqual(verdict, "identity_mismatch",
                                 "%s: mismatch not classified: %s\n%s" % (name, verdict, out[-1500:]))


if __name__ == "__main__":
    unittest.main()
