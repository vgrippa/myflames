#!/usr/bin/env python3
"""Record one live cluster-1.0 read as a replay fixture for test_cluster.py.

Run it from the repository root against a node of the throwaway clusters that
scripts/cluster-matrix.sh starts (ports 34101-34103 Galera, 34201-34203
Percona XtraDB Cluster, 34301-34303 InnoDB Cluster), after putting the cluster
in the state to record:

    python3 scripts/record-cluster-fixtures.py --port 34101 --server galera-11.8-healthy

It writes test/fixtures/cluster/cluster-<server>.json in the replay shape of
test/fixtures/perf: {server, source: "live", args, calls, report}. Every call
is [sql, text] or [sql, null, error] exactly as myflames/cluster.py sent it,
so test/_cluster_fixtures.py replays it with ReplayConn, and
test_recordings_replay_to_their_recorded_report compares the replay with the
stored report. Record again whenever cluster.py's report changes.

Credentials come from the matrix's credentials file, as cluster-matrix.sh
reads them: CREDS_FILE, else $CLUSTER_LAB/credentials.env, else
local/cluster-matrix/credentials.env (KEY=VALUE lines: ROOT_PASSWORD,
MF_ADMIN_PASSWORD, MF_READ_PASSWORD for the users root, mf_admin and
mf_read). The password never goes on a command line and is never printed;
the client gets it in a defaults file (myflames/connector.py). The script
refuses to run with HOME set to your real home or inside ~/.myflames, so
nothing it starts can read or write your own workspace.
"""
import argparse
import json
import os
import sys

try:
    import pwd
except ImportError:  # not Unix: the Docker matrix runs on macOS and Linux only
    pwd = None

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_OUT = os.path.join(REPO, "test", "fixtures", "cluster")
# The matrix's accounts and the credentials.env key that holds each password.
PASSWORD_KEYS = {"root": "ROOT_PASSWORD", "mf_admin": "MF_ADMIN_PASSWORD", "mf_read": "MF_READ_PASSWORD"}


def die(message):
    sys.stderr.write("record-cluster-fixtures: %s\n" % message)
    sys.exit(2)


def credentials_file():
    """The same lookup as cluster-matrix.sh (CREDS_FILE, CLUSTER_LAB, then the default lab)."""
    if os.environ.get("CREDS_FILE"):
        return os.environ["CREDS_FILE"]
    lab = os.environ.get("CLUSTER_LAB") or os.path.join(REPO, "local", "cluster-matrix")
    return os.path.join(lab, "credentials.env")


def read_password(user):
    key = PASSWORD_KEYS.get(user)
    if key is None:
        die("unknown user %r: the matrix has %s" % (user, ", ".join(sorted(PASSWORD_KEYS))))
    path = credentials_file()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        die("cannot read the credentials file %s (%s); run scripts/cluster-matrix.sh up first"
            % (path, exc.strerror or exc.__class__.__name__))
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                return value
    die("%s has no %s" % (path, key))


def refuse_real_home():
    """Same intent as cluster-matrix.sh's guard: never the real home or ~/.myflames."""
    if pwd is None:
        die("needs a Unix system (the cluster matrix runs on macOS and Linux)")
    real = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    home = os.path.realpath(os.path.expanduser("~"))
    store = os.path.join(real, ".myflames")
    if home == real or home == store or home.startswith(store + os.sep):
        die("run with an isolated HOME (for example HOME=$(mktemp -d)), not your real home or ~/.myflames")


def main():
    ap = argparse.ArgumentParser(description="Record one live cluster-1.0 read as a replay fixture.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--user", default="root", help="a matrix account: root (default), mf_admin or mf_read")
    ap.add_argument("--server", required=True, help="fixture name, for example galera-11.8-healthy")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output directory (default test/fixtures/cluster)")
    args = ap.parse_args()
    refuse_real_home()
    password = read_password(args.user)

    sys.path.insert(0, REPO)
    from myflames.cluster import read_status
    from myflames.connector import ConnectorError, MySQLConnection

    calls = []

    class Recording(MySQLConnection):
        def run(self, sql, extra_flags=None, timeout=None):
            try:
                text = MySQLConnection.run(self, sql, extra_flags=extra_flags, timeout=timeout)
            except ConnectorError as exc:
                calls.append([sql, None, str(exc)])
                raise
            calls.append([sql, text])
            return text

    try:
        with Recording(host=args.host, port=args.port, user=args.user, password=password, query_timeout=30) as conn:
            report = read_status(conn)
    except ConnectorError as exc:
        die("could not read the cluster: %s" % str(exc).replace(password, "[redacted]"))
    for call in calls:
        if any(isinstance(part, str) and password in part for part in call):
            die("a recorded answer contains the password; nothing was written")
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "cluster-%s.json" % args.server)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"server": args.server, "source": "live", "args": [], "calls": calls, "report": report},
                  handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    print("%s %s %s" % (path, report["kind"], report["status"]))


if __name__ == "__main__":
    main()
