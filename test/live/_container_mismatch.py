"""Live scenario, inside the lab network: a line-1 identity mismatch on a real
mysqlsh stops before line 2 and is classified as identity_mismatch.

Run by test/live/live_shell.py through _container_mismatch.sh, which writes
/tmp/my.cnf from the read-only lab credentials first.
"""
import subprocess
import sys
import uuid

sys.path.insert(0, "/src")
from myflames import cluster_shell  # noqa: E402


def sql(q):
    return subprocess.run(["mysql", "--defaults-file=/tmp/my.cnf", "-N", "-B", "-e", q],
                          capture_output=True, text=True, check=True).stdout.strip()


server_uuid, group = sql("SELECT @@global.server_uuid, @@global.group_replication_group_name").split("\t")
cases = {
    "right identities": {"server_uuid": server_uuid, "adminapi": {"kind": "cluster", "group_name": group}},
    "wrong group name": {"server_uuid": server_uuid, "adminapi": {"kind": "cluster", "group_name": str(uuid.uuid4())}},
    "wrong server uuid": {"server_uuid": str(uuid.uuid4()), "adminapi": {"kind": "cluster", "group_name": group}},
}
for name, identity in cases.items():
    line1 = cluster_shell.target_session_guard(identity)
    assert len(line1) == 1, line1
    script = line1[0] + '\nprint("LINE2-RAN\\n");\n'
    p = subprocess.run(["mysqlsh", "--defaults-file=/tmp/my.cnf", "--js", "--no-wizard", "--quiet-start=2"],
                       input=script, capture_output=True, text=True)
    ran2 = "LINE2-RAN" in p.stdout
    line = cluster_shell.error_text(p.stderr, p.stdout) if p.returncode else None
    # The product's classification (cluster_shell.run_script): the guard's
    # marker exception at line 1 proves the reviewed statements never ran.
    mismatch = bool(line and line.startswith("ERROR 0 at line 1:")
                    and cluster_shell.IDENTITY_MISMATCH_MARKER in line)
    verdict = "identity_mismatch" if mismatch else ("clean" if not p.returncode else "other")
    print("%-17s rc=%d line2_ran=%s verdict=%r error=%r" % (name, p.returncode, ran2, verdict,
                                                            (line or "")[:160]))
