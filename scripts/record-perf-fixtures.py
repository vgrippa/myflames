#!/usr/bin/env python3
"""Record one live Server view read as a replay fixture.

Writes test/fixtures/perf/<view>-<server>.json in the replay shape the view
tests read (``{server, source, args, calls, report}``: every statement the
view sent with the ``mysql --batch`` text it got back, and the report it
made), or for ``health`` test/fixtures/health/<server>.json (``{server,
captured, variables, samples, missing_names}``). Start the GA matrix first
(scripts/ga-matrix.sh up NAME), then for example:

    HOME=$(mktemp -d) python3 scripts/record-perf-fixtures.py --port 33184 \\
        --server percona-8.4 --view indexes --schema review

The password for ``--user`` (``mf_admin`` by default, ``root`` or
``mf_read``) comes from the matrix's credentials file (``CREDS_FILE``, else
local/ga-matrix/credentials.env), goes to the client in a defaults file and is
never printed; a recording whose answers contain it is not written. Your real
home and ~/.myflames are refused as ``HOME``. Every view here only reads.
"""
import argparse
import datetime
import json
import os
import sys

try:
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD_KEYS = {"root": "ROOT_PASSWORD", "mf_admin": "MF_ADMIN_PASSWORD", "mf_read": "MF_READ_PASSWORD"}
VIEWS = ("indexes", "tables", "config", "io", "waits", "health")


def die(message):
    sys.stderr.write("record-perf-fixtures: %s\n" % message)
    sys.exit(2)


def read_password(user):
    key = PASSWORD_KEYS.get(user)
    if key is None:
        die("unknown user %r: the matrix has %s" % (user, ", ".join(sorted(PASSWORD_KEYS))))
    path = os.environ.get("CREDS_FILE") or os.path.join(REPO, "local", "ga-matrix", "credentials.env")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        die("cannot read the credentials file %s (%s); run scripts/ga-matrix.sh up first"
            % (path, exc.strerror or exc.__class__.__name__))
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == key and value.strip():
            return value.strip().strip("'\"")
    die("%s has no %s" % (path, key))


def refuse_real_home():
    if pwd is None:
        die("needs a Unix system (the GA matrix runs on macOS and Linux)")
    real = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    home = os.path.realpath(os.path.expanduser("~"))
    store = os.path.join(real, ".myflames")
    if home == real or home == store or home.startswith(store + os.sep):
        die("run with an isolated HOME (for example HOME=$(mktemp -d)), not your real home or ~/.myflames")


def main():
    ap = argparse.ArgumentParser(description="Record one live Server view read as a replay fixture.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--user", default="mf_admin", help="a matrix account: mf_admin (default), root or mf_read")
    ap.add_argument("--server", required=True, help="fixture name, for example percona-8.4")
    ap.add_argument("--view", required=True, choices=VIEWS)
    ap.add_argument("--schema", action="append", help="limit indexes, tables or io to a schema (repeatable)")
    ap.add_argument("--interval", type=int, default=2, help="window in seconds for health, io and waits")
    ap.add_argument("--parameters", action="store_true", help="config: include the parameters-1.0 section")
    ap.add_argument("--out", help="output directory (default test/fixtures/perf, or test/fixtures/health)")
    args = ap.parse_args()
    refuse_real_home()
    password = read_password(args.user)

    sys.path.insert(0, REPO)
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

    schemas = args.schema or None
    if args.view == "indexes":
        from myflames.indexes import index_report as read
        view_args = {"schema": schemas[0]} if schemas and len(schemas) == 1 else {"schemas": schemas}
    elif args.view == "tables":
        from myflames.schema_health import table_report as read
        view_args = {"schemas": schemas, "fresh": False}
    elif args.view == "config":
        from myflames.config_review import config_report as read
        view_args = {"parameters": True} if args.parameters else {}
    elif args.view == "io":
        from myflames.io_stats import io_report as read
        view_args = {"interval": args.interval, "limit": 20, "schema": schemas[0] if schemas else None}
    elif args.view == "waits":
        from myflames.waits import waits_report as read
        view_args = {"interval": args.interval, "limit": 20}
    else:
        read, view_args = None, {}

    try:
        with Recording(host=args.host, port=args.port, user=args.user, password=password, query_timeout=60) as conn:
            if args.view == "health":
                from myflames import health
                import time
                variables = health.read_variables(conn)
                first = health.take_sample(conn)
                time.sleep(args.interval)
                second = health.take_sample(conn)
                window = health._Window(first, second, variables, args.interval)
                health.compute_health(first, second, variables, args.interval)
                # The optional counters this server does not report (from the
                # metrics that read them).
                for metric in (health._throughput, health._connections, health._buffer_pool, health._row_locks,
                               health._temp_sorts, health._table_cache, health._redo_io):
                    metric(window)
                out = {"server": args.server, "captured": "live %s (docker, 127.0.0.1), %s, recorded %s by "
                       "scripts/record-perf-fixtures.py" % (args.server, variables.get("version"),
                                                          datetime.datetime.now(datetime.timezone.utc)
                                                          .strftime("%Y-%m-%d")),
                       "variables": variables, "samples": [first, second], "missing_names": sorted(window.missing)}
            else:
                report = read(conn, **view_args)
                parameters = report.get("parameters") if isinstance(report, dict) else None
                if parameters and parameters.get("items"):
                    # The replay recomputes every item; keep the stored report small.
                    items = parameters.pop("items")
                    parameters["items_recorded"] = len(items)
                    parameters["vendor_items"] = sorted(i["name"] for i in items if i.get("vendor"))
                out = {"server": args.server, "source": "live", "args": view_args, "calls": calls,
                       "report": report}
    except (ConnectorError, ValueError) as exc:
        die("could not read %s: %s" % (args.view, str(exc).replace(password, "[redacted]")))
    if password in json.dumps(out):
        die("a recorded answer contains the password; nothing was written")
    folder = args.out or os.path.join(REPO, "test", "fixtures", "health" if args.view == "health" else "perf")
    os.makedirs(folder, exist_ok=True)
    name = "%s.json" % args.server if args.view == "health" else "%s-%s.json" % (args.view, args.server)
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=1, ensure_ascii=False, sort_keys=args.view == "health")
        handle.write("\n")
    print("%s %d statements" % (path, len(calls)))


if __name__ == "__main__":
    main()
