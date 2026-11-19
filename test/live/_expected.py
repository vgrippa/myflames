"""Record and compare normalized read-only lab reports: mtr's .result for JSON.

`scripts/live-tests.sh --record` runs `python3 -m test.live._expected record`
after the suites: it reads the Cluster, Replication and ProxySQL reports from
the labs and writes them under test/live/expected/, one file per report and
server version, with volatile fields (UUIDs, timestamps, ports, durations,
connection ids, log positions and counters) masked and ProxySQL per-query
workload statistics and the cluster live-footprint client list omitted. A
normal run (`compare`, also run as the
live_expected suite) normalizes the same way and fails on a drift when the file
for the version under test exists (writing the drift file under
local/live-evidence/drift/); a missing file skips.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lab import Lab, REPO, check_lab, conn_for, make_home  # noqa: E402

EXPECTED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expected")
DRIFT_DIR = os.path.join(REPO, "local", "live-evidence", "drift")

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")
_HOSTPORT = re.compile(r":\d{2,5}\b")
#: Lab container addresses: Docker assigns them anew (and in another order)
#: whenever the lab restarts, so two healthy runs can differ.
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# Volatile keys: values that legitimately differ between two healthy runs.
# ProxySQL's hostgroup aggregates and each server's pool contain ConnFree,
# ConnOK, ConnUsed, ConnERR and MaxConnUsed. Those are observed counters,
# unlike configured max_connections, weight or the pool's status.
_VOLATILE_KEY = re.compile(
    r"(uuid|guid|^ports?$|_ports?$|timestamp|taken_at|recorded_at|first_seen|last_seen|"
    r"duration|elapsed|connection_ids?|thread_ids?|processlist|gtid|xid|view_id|"
    r"position|log_file|relay_log|_file$|count|counter|bytes|queries|questions|uptime|hits|"
    r"^(?:max_)?conn_(?:free|ok|used|err)$|"
    r"^rate_|^rows_|_time$|_s$|_ms$|_us$)", re.I)
#: Counter blocks: every value moves between two healthy runs (Group
#: Replication cumulative counters, ProxySQL stats); only the shape is stable.
_COUNTER_BLOCKS = ("metrics", "stats")
#: Per-query workload statistics in ProxySQL reports change on every run
#: because the live suites themselves send queries through ProxySQL. Omit them
#: entirely while keeping structural parts (hostgroups, servers, rules, users,
#: monitors and health settings) compared.
_PROXYSQL_OMIT = ("query_digests",)
_PROXYSQL_RULE_OMIT = ("digests", "digest_count", "possible_digests")
#: topology_tiers.live_footprint.clients lists whatever clients are connected
#: at that moment (the ProxySQL monitor and pooled connections, in any order),
#: so it changes on every run like the ProxySQL workload statistics.
_FOOTPRINT_OMIT = ("clients",)


def _is_proxysql_report(value):
    return isinstance(value, dict) and (
        value.get("schema_version") == "proxysql-1.0" or value.get("is_proxysql") is True
    )


def _normalize_proxysql_rules(rules):
    if not isinstance(rules, (list, tuple)):
        return normalize(rules, "query_rules")
    return [
        {k: normalize(v, k) for k, v in sorted(rule.items()) if k not in _PROXYSQL_RULE_OMIT}
        if isinstance(rule, dict) else normalize(rule, "query_rules")
        for rule in rules
    ]


def normalize(value, key=""):
    """Mask volatile fields so two healthy runs of one server version agree."""
    if isinstance(value, dict):
        if key in _COUNTER_BLOCKS:
            return {k: "<volatile>" for k in sorted(value)}
        if key == "live_footprint":
            return {k: normalize(v, k) for k, v in sorted(value.items())
                    if k not in _FOOTPRINT_OMIT}
        if not key and _is_proxysql_report(value):
            return {
                k: _normalize_proxysql_rules(v) if k == "query_rules" else normalize(v, k)
                for k, v in sorted(value.items())
                if k not in _PROXYSQL_OMIT
            }
        return {k: normalize(v, k) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [normalize(item, key) for item in value]
    if key and _VOLATILE_KEY.search(key):
        return "<volatile>"
    if isinstance(value, str):
        value = _UUID.sub("<uuid>", value)
        value = _DATETIME.sub("<timestamp>", value)
        value = _HOSTPORT.sub(":<port>", value)
        value = _IPV4.sub("<ip>", value)
        return value
    return value


def _version_of(report):
    for key in ("server_version", "version"):
        text = report.get(key)
        if isinstance(text, str) and text:
            return re.sub(r"[^0-9A-Za-z.\-]", "_", text)
    return "unknown"


REPORTS = (
    # (name, profile, api path, labs the report needs)
    ("cluster-ic", "cluster-ic-1", "/api/live/cluster", ("ic",)),
    ("cluster-galera", "cluster-galera-1", "/api/live/cluster", ("galera",)),
    ("replication-ic", "cluster-ic-1", "/api/live/replication", ("ic",)),
    ("proxysql", "proxysql-admin", "/api/live/proxysql", ("proxysql",)),
)


def expected_path(name, version):
    return os.path.join(EXPECTED_DIR, "%s-%s.json" % (name, version))


def drift_path(target):
    return os.path.join(DRIFT_DIR, os.path.basename(target) + ".drift")


def _write_json(target, data):
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = target + ".part"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, target)


def read_report(lab, profile_name, path):
    return lab.api(path, {"connection": conn_for(lab.profile(profile_name))})


def _reports_for(labs):
    wanted = set(labs)
    return [spec for spec in REPORTS if set(spec[3]) & wanted]


def record(labs):
    lab = Lab()
    written = []
    for name, profile, path, _needs in _reports_for(labs):
        report = read_report(lab, profile, path)
        target = expected_path(name, _version_of(report))
        _write_json(target, normalize(report))
        written.append(target)
    return written


def compare(labs):
    """A list of (name, None) skips, (name, True) matches, (name, drift) drifts."""
    lab = Lab()
    outcomes = []
    for name, profile, path, _needs in _reports_for(labs):
        raw = read_report(lab, profile, path)
        target = expected_path(name, _version_of(raw))
        if not os.path.exists(target):
            outcomes.append((name, None))
            continue
        with open(target, encoding="utf-8") as handle:
            expected = json.load(handle)
        normalized = normalize(raw)
        # Re-normalize the recorded file too, so a mask added after it was
        # recorded (normalize is idempotent) applies to both sides.
        if normalize(expected) == normalized:
            outcomes.append((name, True))
        else:
            drift = drift_path(target)
            _write_json(drift, normalized)
            outcomes.append((name, drift))
    return outcomes


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("record", "compare"):
        print(__doc__)
        return 2
    mode = argv[0]
    labs = argv[1:] or ["ic", "galera", "proxysql"]
    check_lab(*labs)
    make_home("expected")
    if mode == "record":
        for target in record(labs):
            print("recorded %s" % os.path.relpath(target, os.getcwd()))
        return 0
    failed = False
    for name, outcome in compare(labs):
        if outcome is None:
            print("SKIP %s (no expected file for this version)" % name)
        elif outcome is True:
            print("PASS %s" % name)
        else:
            failed = True
            print("FAIL %s (drift written to %s)" % (name, outcome))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
