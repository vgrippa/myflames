# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""MySQL Shell (``mysqlsh``) as the send half of the InnoDB Cluster and
InnoDB ClusterSet changes the Cluster place offers (cluster.py, "reviewed
actions"; reviewed_writes.execute does the rest).

Some changes are AdminAPI operations, not one statement: MySQL Shell's
``Cluster.switchToMultiPrimaryMode`` runs the Group Replication UDF and then
sets auto_increment on every member and the metadata's primary mode
(Sh/modules/adminapi/cluster/switch_to_multi_primary_mode.cc:50-91);
``Cluster.rejoinInstance`` recreates the recovery account and may clone
(Sh/modules/adminapi/cluster/rejoin_instance.cc); the ClusterSet operations
move the ``clusterset_replication`` channels between clusters
(Sh/modules/adminapi/cluster_set/cluster_set_impl.cc). myflames does not
re-implement them: it runs MySQL Shell.

:class:`ShellWriter` takes the place of a connection's ``run_write`` for one
send. The reviewed statements are AdminAPI calls in JavaScript, one per line,
sent on standard input to ``mysqlsh --js`` (batch mode stops at the first
exception). The first JavaScript line checks the reviewed server UUID
through ``session.runSql``, then the identity of the returned AdminAPI
object through its extended status. The action reuses that object. A mismatch
throws MYFLAMES_IDENTITY_MISMATCH and is a refusal with no action sent.
The account comes from a 0600 option file on argv, as for every
other tool myflames runs (lifecycle._credentials): never a URI, an
environment variable or the command line. The Shell runs with an empty
temporary HOME and MYSQLSH_USER_CONFIG_HOME, so no user startup script or
saved credential takes part.

Errors are reported in the shape reviewed_writes reads (``ERROR <n> at line
<l>: <message>``):

* the Shell could not connect or log in (no statement ran): the MySQL error
  number without a line, so the send is "failed: nothing was sent";
* an exception from a statement: MySQL Shell's own number (``MYSQLSH n``) or
  the server's (``MySQL Error n``), else 0 (an exception without a number),
  at that statement's line. The Cluster place then reads the cluster again
  (verify_on_error) and reports "partial" when anything it can see changed,
  because an AdminAPI operation can fail after some of its steps ran;
* a time limit or a Shell that ended without an exception: no number, so the
  outcome is unknown.
"""
import contextlib
import json
import os
import re
import subprocess
import tempfile

from .connector import ConnectorError

#: Errors MySQL Shell prints when it cannot open its first session: nothing ran.
#: (2006 and 2013, a lost connection, are left out: a session may have run.)
CONNECT_ERRORS = (1044, 1045, 1130, 2002, 2003, 2005, 2026, 2059, 2061)
_STDIN_LINE = re.compile(r"[ \t]*at STDIN:([0-9]+)(?::[0-9]+)?[ \t]*\r?")
_SHELL_CODE = re.compile(r"\(MYSQLSH (\d+)\)")
_MYSQL_CODE = re.compile(r"MySQL Error (\d+)")
_ANSI = re.compile(r"(?:\x1b|\\u001[bB]|\\x1[bB])\[[0-9;?]*[ -/]*[@-~]")
# Consume an unterminated run once, including a dangling backslash. Requiring
# a closing quote retries the same escaped suffix from each quote, quadratically.
_JSON_STRING = re.compile(r'"(?:[^"\\\r\n]|\\[^\r\n])*(?:"|\\?(?=[\r\n]|\Z))')
IDENTITY_MISMATCH_MARKER = "MYFLAMES_IDENTITY_MISMATCH"
_INSTANCE_MARKER = "/*myflames:instance "
_CLUSTERSET_MARKER = "/*myflames:clusterset "

# Application limits, not Shell deadlines. recoveryProgress=1 prints completed
# stages, not a heartbeat (mysql-shell common/clone_progress.cc:109-169).
# Its 500 ms status polling (member_recovery_monitoring.h:40) does not bound
# silence during FILE COPY/FILE SYNC. Allow an hour without any output and a
# full day overall, and disclose that even a healthy silent stage can expire.
CLONE_IDLE_TIMEOUT = 60 * 60
CLONE_TIMEOUT = 24 * 60 * 60


class _ShellText(str):
    """Redacted text with private transport evidence for the result adapter.

    cluster._Sender retains stdout/stderr as strings. A string subclass lets
    that adapter carry validated evidence without serializing raw output or
    asking a downstream consumer to parse already redacted JSON/error text.
    No attribute contains unredacted server text. Configuration evidence is
    only a boolean, consumed before the display string can lose JSON keys.
    """
    def __new__(cls, value, diagnostic=None, error_info=None, configuration_ok=False):
        obj = super().__new__(cls, value)
        obj.diagnostic = diagnostic
        obj.error_info = error_info
        obj.configuration_ok = configuration_ok
        return obj


def _redact_shell_text(text, password):
    """Redact JSON-escaped secrets in keys/values as well as ordinary text.

    Work on complete JSON string tokens so quotes, backslashes, Unicode and
    escaped slashes have the same meaning as the password. This also works
    on a pretty-printed status line and on malformed JSON with valid string
    tokens, without interpreting the output as instructions.
    """
    from . import dbops

    def redact_string(match):
        try:
            value = json.loads(match.group(0))
        except ValueError:
            return match.group(0)
        redacted = dbops.redact(value, password)
        return json.dumps(redacted, ensure_ascii=True) if redacted != value else match.group(0)

    text = _JSON_STRING.sub(redact_string, _ANSI.sub("", text))
    return dbops.redact(text, password)


def _clusterset_identity(script, full=False):
    """Read the identity embedded in an exact, derived first-line guard."""
    from .reviewed_writes import Refusal
    first, _, _ = (script or "").partition("\n")
    if not first.startswith(_CLUSTERSET_MARKER):
        return None
    try:
        encoded, separator, _ = first[len(_CLUSTERSET_MARKER):].partition("*/ ")
        identity = json.loads(encoded)
        if not separator or target_session_guard(identity) != [first]:
            raise ValueError("Not the derived guard")
    except (TypeError, ValueError, RecursionError):
        raise Refusal("The reviewed ClusterSet identity guard is invalid. Nothing was sent. Review again.",
                      "identity_unavailable") from None
    return identity if full else identity["adminapi"]


def _check_clusterset_script(script, shell):
    """Validate the derived guard for both reviewed changes and diagnostics.

    reviewed_writes binds the action lines; this check binds the complete
    first-line guard before credentials or process creation. The object's
    status and the global session's view narrow the identity check, but
    cannot pin AdminAPI's internal sessions through a load-balanced address.
    """
    from .cluster import _shell_check_clusterset
    from .reviewed_writes import Refusal
    identity = _clusterset_identity(script, full=True)
    if identity is None:
        return None
    try:
        _shell_check_clusterset(shell.get("version_tuple"))
    except (TypeError, ValueError):
        raise Refusal("The MySQL Shell version for this ClusterSet action is missing or unsupported. "
                      "Nothing was sent. Refresh and review again.", "identity_unavailable") from None
    return identity


def _strict_json(text):
    """Parse private machine output without duplicate keys or non-JSON constants."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate output key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Not a JSON constant")

    return json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def _status_output(stdout, password, identity=None):
    """Validate the complete raw status privately, then retain redacted facts.

    A secret can be a domain name, a JSON key or even JSON punctuation. No
    identity/shape check may use the display string after secret redaction.
    Strict JSON also prevents duplicate keys or a truncated suffix becoming
    evidence. Validation does not require a healthy topology.
    """
    from . import dbops
    diagnostic = None
    if identity is not None:
        try:
            report = _strict_json(stdout)
        except (TypeError, ValueError, RecursionError):
            report = None
        valid = (isinstance(report, dict) and report.get("domainName") == identity.get("name") and
                 report.get("status") in ("HEALTHY", "AVAILABLE", "UNAVAILABLE") and
                 isinstance(report.get("clusters"), dict) and bool(report["clusters"]) and
                 set(report["clusters"]) == set(identity.get("groups") or {}) and
                 all(isinstance(item, dict) and isinstance(item.get("globalStatus"), str) and
                     bool(item["globalStatus"]) for item in report["clusters"].values()))
        facts = []
        if valid:
            # Only bounded-shape scalar observations are retained as evidence;
            # the redacted stdout carries the full diagnostic detail.
            def observed(server, fact, value):
                facts.append({"server": dbops.redact(server, password), "fact": fact,
                              "expected": "reported (health is not an action effect)",
                              "actual": dbops.redact(value, password), "status": "verified"})
            observed(report["domainName"], "MySQL Shell ClusterSet status", report["status"])
            for name, item in report["clusters"].items():
                for key in ("globalStatus", "status", "clusterRole", "clusterSetReplicationStatus",
                            "transactionSetConsistencyStatus"):
                    if isinstance(item.get(key), str):
                        observed(name, "MySQL Shell " + key, item[key])
        diagnostic = {"valid": valid, "facts": facts}
    # Never parse the redacted display copy: passwords can equal "status",
    # "ok", the marker, or JSON punctuation. Retain no raw configuration.
    config_lines = [line[len("MYFLAMES_CONFIG="):] for line in stdout.split("\n")
                    if line.startswith("MYFLAMES_CONFIG=")]
    config = None
    if len(config_lines) == 1:
        try:
            config = _strict_json(config_lines[0])
        except (TypeError, ValueError, RecursionError):
            pass
    return _ShellText(_redact_shell_text(stdout, password), diagnostic=diagnostic,
                      configuration_ok=isinstance(config, dict) and config.get("status") == "ok")


def _instance_target(value):
    """Validate a reviewed instance endpoint without accepting connection options."""
    from .cluster import _ADDRESS, _split_address
    from .dbops import _TARGET_UUID
    if not isinstance(value, dict) or set(value) != {"address", "server_uuid"}:
        raise ValueError("The reviewed instance target is missing or invalid. Nothing was sent.")
    address, uuid = value["address"], value["server_uuid"]
    if (not isinstance(address, str) or len(address) > 1024 or not _ADDRESS.fullmatch(address) or
            not isinstance(uuid, str) or not _TARGET_UUID.fullmatch(uuid)):
        raise ValueError("The reviewed instance address or server UUID is invalid. Nothing was sent.")
    host, port = _split_address(address)
    if not host or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("The reviewed instance address has an invalid port. Nothing was sent.")
    return {"address": address, "server_uuid": uuid}


def _probe_instance_target(conn, script):
    """Check the bound target address before Shell opens its own connection.

    The prefix is part of the derived guard, review token and audit script.
    This is an endpoint preflight, not a same-session guard: AdminAPI opens a
    new connection to the exact same address after this probe finishes.
    """
    from .connection_groups import ProfileReadError
    from .connector import MySQLConnection
    from .cluster import CLUSTER_TAG, _split_address
    from .perf_common import Reader
    from .reviewed_writes import Refusal
    if not script.startswith("/*myflames:instance"):
        return
    match = re.match(r"\A/\*myflames:instance ([^\r\n]*?)\*/ ", script)
    try:
        if match is None or len(match.group(1)) > 2048:
            raise ValueError("The reviewed rejoin identity marker is invalid. Nothing was sent.")
        target = _instance_target(json.loads(match.group(1)))
    except (TypeError, ValueError):
        raise Refusal("The reviewed rejoin identity marker is invalid. Nothing was sent. Review again.",
                      "identity_unavailable") from None
    guard = getattr(conn, "_login_guard", None)
    if guard is not None:
        guard()
    options = _connection(conn)
    options.pop("socket", None)
    options["host"], options["port"] = _split_address(target["address"])
    binary = getattr(conn, "_binary_override", None)
    if binary:
        options["binary"] = binary
    try:
        with MySQLConnection(**options) as probe:
            probe._login_guard = guard
            probe._group_login_started = False
            rows = Reader(probe, CLUSTER_TAG).rows("SELECT @@global.server_uuid", timeout=30)
    except (Refusal, ProfileReadError):
        raise
    except (ConnectorError, OSError, ValueError):
        raise Refusal("Could not verify the server UUID at the reviewed rejoin address. Nothing was sent. "
                      "Check that address and review again.", "identity_unavailable") from None
    if rows != [[target["server_uuid"]]]:
        raise Refusal("The rejoin address no longer reaches the server UUID you reviewed. Nothing was sent. "
                      "Refresh and review again.", "identity_mismatch")
    if guard is not None:
        guard()


def target_session_guard(identity):
    """One JavaScript line, checking identity before any action call.

    ClassicSession.runSql and ClassicResult.fetchOne are exposed in Shell's
    modules/mod_mysql_session.cc:89 and mod_mysql_resultset.cc:67. Never
    inspect AdminAPI until the global session's UUID has matched. AdminAPI
    opens separate sessions (modules/adminapi/mod_dba.cc:849-862).
    """
    from .dbops import _TARGET_UUID
    identity = identity if isinstance(identity, dict) else {}
    uuid = identity.get("server_uuid")
    if not isinstance(uuid, str) or not _TARGET_UUID.fullmatch(uuid):
        raise ValueError("The reviewed server UUID is missing or invalid. Nothing was sent. Refresh and review again.")
    guard = ['if (session.runSql("SELECT @@global.server_uuid").fetchOne()[0] !== %s) { throw new Error(%s); }'
             % (json.dumps(uuid), json.dumps(IDENTITY_MISMATCH_MARKER))]
    adminapi = identity.get("adminapi")
    target = None
    if identity.get("target_instance") is not None:
        target = _instance_target(identity["target_instance"])
        if not isinstance(adminapi, dict) or adminapi.get("kind") != "clusterset":
            guard[0] = _INSTANCE_MARKER + json.dumps(target, sort_keys=True) + "*/ " + guard[0]
    if adminapi is None:
        return guard
    if not isinstance(adminapi, dict):
        raise ValueError("The reviewed AdminAPI identity is invalid. Nothing was sent.")
    # Keep every read and check on line 1: only this prefix can prove that no
    # reviewed action ran. Use the very object checked here for the action,
    # never another getCluster/getClusterSet connection after the check.
    mismatch = 'throw new Error(%s);' % json.dumps(IDENTITY_MISMATCH_MARKER)
    if adminapi.get("kind") == "cluster":
        group = adminapi.get("group_name")
        if not isinstance(group, str) or not _TARGET_UUID.fullmatch(group):
            raise ValueError("The reviewed Group Replication group name is missing or invalid. Nothing was sent.")
        guard += ['var myflamesCluster = dba.getCluster();',
                  'var myflamesStatus = myflamesCluster.status({extended: 1});',
                  'if (!myflamesStatus.defaultReplicaSet || myflamesStatus.defaultReplicaSet.groupName !== %s) { %s }'
                  % (json.dumps(group), mismatch)]
    elif adminapi.get("kind") == "clusterset":
        name, groups = adminapi.get("name"), adminapi.get("groups")
        roles, invalidated = adminapi.get("roles"), adminapi.get("invalidated")
        primary, addresses = adminapi.get("primary_cluster"), adminapi.get("metadata_addresses")
        view = adminapi.get("view_id")
        if (not isinstance(name, str) or not name or not isinstance(groups, dict) or not groups or
                any(not isinstance(key, str) or not key or not isinstance(value, str) or
                    not _TARGET_UUID.fullmatch(value) for key, value in groups.items())):
            raise ValueError("The reviewed ClusterSet identity is missing or invalid. Nothing was sent.")
        if (not isinstance(roles, dict) or set(roles) != set(groups) or
                any(role not in ("PRIMARY", "REPLICA") for role in roles.values()) or
                not isinstance(invalidated, dict) or set(invalidated) != set(groups) or
                any(not isinstance(flag, bool) for flag in invalidated.values()) or
                not isinstance(primary, str) or primary not in groups or
                roles[primary] != "PRIMARY" or invalidated[primary]):
            raise ValueError("The reviewed ClusterSet roles or invalidation flags are missing or invalid. Nothing was sent.")
        if (not isinstance(addresses, list) or not addresses or
                any(not isinstance(address, str) or not address for address in addresses)):
            raise ValueError("The reviewed ClusterSet member addresses are missing or invalid. Nothing was sent.")
        if not isinstance(view, str) or not re.fullmatch(r"[0-9]+", view):
            raise ValueError("The reviewed ClusterSet view is missing or invalid. Nothing was sent.")
        reviewed = {"kind": "clusterset", "name": name, "groups": groups, "roles": roles,
                    "invalidated": invalidated, "primary_cluster": primary,
                    "metadata_addresses": sorted(set(addresses)), "view_id": view}
        # Shell 8.0.32: cluster_set/status.cc:132-164 and cluster/status.cc:
        # 1555-1556,1798. extended:2 retains each cluster's metadata groupName,
        # even when unavailable. Invalidation is globalStatus == INVALIDATED
        # (cluster_set_impl.cc:600-606), not an invalidated boolean in status.
        guard += ['var myflamesClusterSet = dba.getClusterSet();',
                  'var myflamesStatus = myflamesClusterSet.status({extended: 2});',
                  # JSON.parse preserves a literal cluster named __proto__;
                  # a JavaScript object initializer would change its meaning.
                  'var myflamesIdentity = JSON.parse(%s);' % json.dumps(json.dumps(reviewed, sort_keys=True)),
                  'if (!myflamesStatus || myflamesStatus.domainName !== myflamesIdentity.name || '
                  'myflamesStatus.primaryCluster !== myflamesIdentity.primary_cluster || '
                  '!myflamesStatus.clusters || '
                  'JSON.stringify(Object.keys(myflamesStatus.clusters).sort()) !== '
                  'JSON.stringify(Object.keys(myflamesIdentity.groups).sort()) || '
                  'Object.keys(myflamesIdentity.groups).some(function(name) { '
                  'var cluster = myflamesStatus.clusters[name]; return !cluster || '
                  'cluster.groupName !== myflamesIdentity.groups[name] || '
                  'cluster.clusterRole !== myflamesIdentity.roles[name] || '
                  'typeof cluster.globalStatus !== "string" || !cluster.globalStatus || '
                  '(cluster.globalStatus === "INVALIDATED") !== myflamesIdentity.invalidated[name]; }) || '
                  '(Object.prototype.hasOwnProperty.call(myflamesStatus, "metadataServer") && '
                  'myflamesIdentity.metadata_addresses.indexOf(myflamesStatus.metadataServer) === -1)) { %s }'
                  % mismatch,
                  # No SQL parameters: Shell's string escaping depends on
                  # sql_mode. Compare server text only in JavaScript, and keep
                  # BIGINT UNSIGNED view IDs as decimal strings, never doubles.
                  'var myflamesViews = session.runSql("SELECT domain_name, CAST(view_id AS CHAR) FROM '
                  'mysql_innodb_cluster_metadata.v2_cs_clustersets").fetchAll().filter(function(row) { '
                  'return row[0] === myflamesIdentity.name; });',
                  'if (myflamesViews.length !== 1 || myflamesViews[0][1] !== myflamesIdentity.view_id) { %s }'
                  % mismatch]
        # The global view is not proof of the object's internal metadata
        # session/view. Direct member addresses are required to avoid routing
        # those separate sessions to different servers behind a load balancer.
        # Bind all reviewed facts with the exact guard and private validator.
        bound = {"server_uuid": uuid, "adminapi": reviewed}
        if target is not None:
            from .cluster import _split_address
            host, port = _split_address(target["address"])
            bound["target_instance"] = target
            # Shell parseUri(session.uri) preserves this session's TLS options
            # and account, but uri omits its password (base_session.h:118-119).
            # Supply that only through a private file, never the bound script.
            # openSession does not replace the global session (mod_shell.cc:973).
            guard += [
                'var myflamesTarget = null;',
                'try { var myflamesTargetOptions = shell.parseUri(session.uri); '
                'delete myflamesTargetOptions.socket; delete myflamesTargetOptions.schema; '
                'myflamesTargetOptions.scheme = "mysql"; '
                'myflamesTargetOptions.host = %s; myflamesTargetOptions.port = %d; '
                'myflamesTarget = shell.openSession(myflamesTargetOptions, '
                'JSON.parse(os.loadTextFile(os.getenv("MYFLAMES_SHELL_PASSWORD_FILE")))); '
                'var myflamesTargetUuid = myflamesTarget.runSql("SELECT @@global.server_uuid").fetchOne(); '
                'if (!myflamesTargetUuid || myflamesTargetUuid[0] !== %s) { %s } '
                '} catch (error) { %s } finally { if (myflamesTarget) { '
                'try { myflamesTarget.close(); } catch (error) { %s } } }'
                % (json.dumps(host), port, json.dumps(target["server_uuid"]), mismatch, mismatch, mismatch)]
        encoded = json.dumps(bound, sort_keys=True, ensure_ascii=True).replace("/", "\\/")
        guard[0] = _CLUSTERSET_MARKER + encoded + "*/ " + guard[0]
    else:
        raise ValueError("The reviewed AdminAPI object kind is invalid. Nothing was sent.")
    return [" ".join(guard)]


def find_shell(refresh=False):
    """The newest installed ``mysqlsh`` found on this machine as a dict (path,
    realpath, version, version_tuple), or None. Uses the client inventory
    (client_tools.discover): the same ownership checks and cached version
    probes as every other client myflames runs. Keep older versions for the
    catalog's version-specific refusal; actions require at least 8.0.32."""
    from . import client_tools
    try:
        shells = client_tools.discover(refresh=refresh, cache_shell_trust=not refresh).get("shells") or []
    except Exception:  # noqa: BLE001 - no Shell found is the answer, never a failed read
        return None
    for shell in shells:
        if shell.get("path") and tuple(shell.get("version_tuple") or ()) >= (8, 0, 0):
            return {"path": shell["path"], "realpath": shell.get("realpath"), "version": shell.get("version"),
                    "version_tuple": list(shell.get("version_tuple") or ())}
    return None


def _connection(conn):
    """The connection options of *conn* for an option file (lifecycle._options)."""
    out = {"host": getattr(conn, "host", None) or "127.0.0.1", "port": getattr(conn, "port", None) or 3306}
    for key in ("user", "password", "socket", "ssl_mode", "ssl_ca", "ssl_cert", "ssl_key", "ssl_cipher", "ssh_host",
                "ssh_user", "ssh_port"):
        value = getattr(conn, key, None)
        if value is not None and value != "":
            out[key] = value
    return out


def error_text(stderr, stdout=""):
    """The error reviewed_writes reads, using stderr exception evidence only.

    stdout contains AdminAPI progress and may echo instance or cluster names.
    Neither a statement location nor an exception number can come from it.
    """
    text = _ANSI.sub("", stderr or "")
    # Split physical output lines only. Unicode separators in an echoed
    # server label must not change which exception is next to a call site.
    physical_lines = text.split("\n")
    raw_lines = [" ".join(raw.split()) for raw in physical_lines]
    lines = [line for line in raw_lines if line and not re.match(r"(?:NOTE|WARNING|INFO):", line, re.I)]
    if not lines:
        return None
    # mysql-shell/mysqlshdk/scripting/types.cc:717 appends the location;
    # scripting/polyglot/utils/polyglot_error.cc:438 appends backtrace sites.
    # Keep the last stderr site, never one from stdout progress.
    locations = [(index, match) for index, line in enumerate(physical_lines)
                 for match in [_STDIN_LINE.fullmatch(line)] if match]
    where = int(locations[-1][1].group(1)) if locations else None
    if locations:
        index = locations[-1][0]
        # Preserve blank/progress lines when deciding adjacency. Removing
        # them first could promote an echoed server name into an exception.
        candidates = [line for line in reversed(raw_lines[max(0, index - 1):index + 1])
                      if line and not re.match(r"(?:NOTE|WARNING|INFO):", line, re.I)]
    else:
        candidates = lines
    shell_code = next((int(m.group(1)) for m in map(_SHELL_CODE.search, candidates) if m), None)
    mysql_code = next((int(m.group(1)) for m in map(_MYSQL_CODE.search, candidates) if m), None)
    exception = any(re.search(r"\(\w+Error\)|^\w*Error:", line) for line in candidates)
    if where is None and shell_code is None and mysql_code is None and not exception:
        return None
    # Only a standalone call site proves a location. Exception text can echo
    # arbitrary server labels, including embedded "at STDIN:1" strings.
    message = candidates[-1] if locations else None
    if message is None:
        message = next((line for line in candidates if _SHELL_CODE.search(line) or _MYSQL_CODE.search(line)), None)
    if message is None:
        message = next((line for line in candidates if re.search(r"^ERROR:|Error|error|\(\w+Error\)", line)), None)
    message = message if message is not None else candidates[0]
    # A wrapped exception can carry several numbers; its final cause wins.
    message_codes = [(match, source) for pattern, source in ((_SHELL_CODE, "mysqlsh"), (_MYSQL_CODE, "mysql"))
                     for match in pattern.finditer(message)]
    last = max(message_codes, key=lambda item: item[0].start()) if message_codes else None
    code = int(last[0].group(1)) if last else 0
    # polyglot_error.cc:405-408 appends the actual exception type/code at
    # the end. A quoted '(MYSQLSH n)' inside a RuntimeError/server message
    # is not provenance, even though it remains a useful display number.
    typed = re.search(r"\((MYSQLSH|MySQL Error) (\d+)\)$", message)
    source = {"MYSQLSH": "mysqlsh", "MySQL Error": "mysql"}.get(typed.group(1), "unknown") if typed else "unknown"
    info = {"source": source, "code": int(typed.group(2)) if typed else None,
            "line": where, "stage": "unknown"}
    message = ("" if _STDIN_LINE.fullmatch(message) else message).strip()
    message = re.sub(r"^ERROR:\s*", "", message)
    if (where is None and shell_code is None and code in CONNECT_ERRORS and not exception and
            re.match(r"^MySQL Error %d\b" % code, message)):
        # src/mysqlsh/main.cc:633-645 prints this bare database error when
        # opening the first session fails. An AdminAPI-wrapped connect error
        # can occur after earlier steps ran and proves no such thing.
        return _ShellText("ERROR %d: %s" % (code, message),
                          error_info=dict(info, source="mysql", code=code, stage="connection"))
    if where is None:
        # A number alone does not prove that the first statement failed. Do
        # not synthesize the ERROR/line framing used for definite outcomes,
        # or let embedded server text supply that framing to the parsers.
        message = re.sub(r"\bERROR(\s+\d+)", r"error\1", message)
        message = re.sub(r"\bat line(\s+\d+)\b", r"at input line\1", message)
        return _ShellText("%s; the statement location and outcome are unknown." % message, error_info=info)
    return _ShellText("ERROR %d at line %d: %s" % (code, where, message), error_info=info)


class ShellWriter(object):
    """*conn* whose ``run_write`` runs MySQL Shell instead of the mysql client
    (every other attribute is the connection's)."""

    def __init__(self, conn, shell, progress=None):
        from .reviewed_writes import Refusal
        if getattr(conn, "ssh_host", None):
            raise Refusal("MySQL Shell changes are not run over an SSH connection: MySQL Shell would "
                          "connect to the other members directly, outside the tunnel, with this connection's "
                          "user and password; nothing was sent.", "shell_unavailable")
        mode = (getattr(conn, 'ssl_mode', None) or 'PREFERRED').upper()
        if mode == 'VERIFY_IDENTITY':
            raise Refusal('MySQL Shell changes are not offered when TLS mode is VERIFY_IDENTITY: '
                          'MySQL Shell checks the certificate authority on its sessions to other members, '
                          'not their host names. Use VERIFY_CA, or run the change in MySQL Shell. '
                          'Nothing was sent.', "shell_unavailable")
        if mode == 'REQUIRED':
            raise Refusal('MySQL Shell changes are not offered when TLS mode is REQUIRED without CA '
                          'verification: MySQL Shell falls back to PREFERRED on its sessions to other '
                          'members. Use VERIFY_CA, or run the change in MySQL Shell. Nothing was sent.',
                          "shell_unavailable")
        self._conn = conn
        self._progress = progress
        self.stdout = ""
        self.stderr = ""
        self._shell = dict(shell or {})
        if not self._shell.get("path"):
            raise Refusal("MySQL Shell (mysqlsh) is not installed here, so nothing was sent.", "shell_unavailable")

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def argv(self, defaults):
        """The MySQL Shell command line after the option file: JavaScript, no
        prompts (--no-wizard), nothing printed at start."""
        from .lifecycle import _shell_plugin_flags
        return defaults + _shell_plugin_flags(self._shell) + ["--js", "--no-wizard", "--quiet-start=2"]

    def run_write(self, script, extra_flags=None, timeout=None, idle_timeout=None):
        from . import dbops, lifecycle
        from .connection_groups import ProfileReadError
        from .connector import _run_bounded, checked_shell_program
        from .reviewed_writes import Refusal
        # Consent precedes all credential access and is checked again at spawn
        # after option-file creation and executable validation may have waited.
        guard = getattr(self._conn, "_login_guard", None)
        if guard is not None:
            guard()
        guarded_identity = _check_clusterset_script(script or "", self._shell)
        clusterset_identity = guarded_identity["adminapi"] if guarded_identity is not None else None
        _probe_instance_target(self._conn, script or "")
        password = getattr(self._conn, "password", None)
        pending = {"stdout": bytearray(), "stderr": bytearray()}
        dropping = {"stdout": False, "stderr": False}
        started = False
        groups = getattr(self._conn, "_tool_groups", None)

        def spawned():
            nonlocal started
            started = True

        def shutdown_error(stdout=b"", stderr=b""):
            from .clusterset_execution import SHELL_SHUTDOWN_MESSAGE
            self.stdout = _redact_shell_text(stdout.decode("utf-8", "replace"), password)
            self.stderr = _ShellText(_redact_shell_text(stderr.decode("utf-8", "replace"), password),
                                     error_info={"source": "workspace", "code": None,
                                                 "line": None, "stage": "shutdown"})
            record = getattr(self._conn, "_shell_shutdown_audit", None)
            if record is not None:
                record()
            error = ConnectorError(SHELL_SHUTDOWN_MESSAGE)
            error.guarded_unknown = True
            return error

        def output(stream, chunk):
            if self._progress is None or not hasattr(self._progress, "emit"):
                return
            pending[stream].extend(chunk)
            while b"\n" in pending[stream]:
                line, _, rest = pending[stream].partition(b"\n")
                pending[stream][:] = rest
                if dropping[stream] or len(line) > 8192:
                    dropping[stream] = False
                    self._progress.emit("[Long MySQL Shell progress line omitted]", stream)
                    continue
                clean = _redact_shell_text(line.decode("utf-8", "replace"), password)
                self._progress.emit(clean, stream)
            if len(pending[stream]) > 8192:
                # Do not publish fragments: a password can cross a chunk.
                pending[stream].clear()
                dropping[stream] = True
        tool = {"name": "mysqlsh", "path": self._shell["path"], "engine": "mysql"}
        try:
            with contextlib.ExitStack() as stack:
                argv, env = stack.enter_context(lifecycle._credentials(_connection(self._conn), tool))
                if guarded_identity is not None and guarded_identity.get("target_instance") is not None:
                    # No secret enters the reviewed script, argv, output or
                    # environment. Only this private file's path is exported.
                    credential_dir = stack.enter_context(tempfile.TemporaryDirectory(prefix="myflames-shell-target-"))
                    os.chmod(credential_dir, 0o700)
                    path = os.path.join(credential_dir, "password.json")
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(password or "", handle, ensure_ascii=True)
                    env = dict(env, MYFLAMES_SHELL_PASSWORD_FILE=path)
                argv[0] = checked_shell_program(self._shell["path"], self._shell.get("realpath"))
                if guard is not None:
                    guard()
                    self._conn._group_login_started = True
                stream_args = {"on_output": output} if self._progress is not None else {}
                if idle_timeout is not None:
                    stream_args["idle_timeout"] = idle_timeout
                if groups is not None:
                    stream_args["tool_groups"] = groups
                proc = _run_bounded(self.argv(argv), timeout or 120, input=(script or "").encode("utf-8"),
                                    env=env, kill_tree=True, on_spawn=spawned, **stream_args)
        except subprocess.TimeoutExpired as exc:
            if started and groups is not None and groups.closed:
                raise shutdown_error(exc.output or b"", exc.stderr or b"") from None
            # Only idle-enabled calls retain bounded timeout output. Redact
            # before exposing it; incomplete output is never status evidence.
            self.stdout = _redact_shell_text((exc.output or b"").decode("utf-8", "replace"), password)
            self.stderr = _redact_shell_text((exc.stderr or b"").decode("utf-8", "replace"), password)
            if getattr(exc, "idle_timeout", None) is not None:
                raise ConnectorError("MySQL Shell produced no output for %ss; the client was stopped and "
                                     "the outcome is unknown." % exc.idle_timeout) from None
            raise ConnectorError("MySQL Shell timed out after %ss; the outcome is unknown." % (timeout or 120)) \
                from None
        except (Refusal, ProfileReadError):
            raise
        except (ConnectorError, ValueError, OSError) as exc:
            if started and groups is not None and groups.closed:
                raise shutdown_error(getattr(exc, "output", None) or b"",
                                     getattr(exc, "stderr", None) or b"") from None
            message = dbops.redact(str(exc), password)
            if not started:
                # Includes option-file writes, executable validation and a
                # failed Popen. Message wording is not proof of a no-send.
                raise Refusal("MySQL Shell could not be started (%s). Nothing was sent." % message,
                              "shell_unavailable") from None
            # In particular, credential cleanup can fail after Shell ran.
            # Never let local exception text (including a file path) supply
            # the server ERROR/line framing used to prove an unsent refusal.
            message = re.sub(r"\bERROR(?=\s+\d)", "error code", message, flags=re.I)
            message = re.sub(r"\bat line(?=\s+\d)", "at input line", message, flags=re.I)
            error = ConnectorError("MySQL Shell failed after starting (%s); the outcome is unknown." % message)
            error.guarded_unknown = True
            raise error from None
        if started and groups is not None and groups.closed:
            # Shutdown can kill the group just as the last pipe closes. A
            # signal or even a zero exit is not a fully observed completion.
            raise shutdown_error(proc.stdout or b"", proc.stderr or b"")
        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        self.stdout = _status_output(stdout, password, clusterset_identity)
        line = error_text(stderr, stdout) if proc.returncode > 0 else None
        error_info = getattr(line, "error_info", None)
        if error_info is not None:
            error_info = dict(error_info)
            if clusterset_identity is not None and error_info["line"] == 1:
                error_info["stage"] = "identity_guard"
            elif error_info["line"] is not None and error_info["line"] > 1:
                # A call site says which AdminAPI call failed, never which
                # internal step ran. An account refresh may precede the
                # first metadata mutation (cluster_set_impl.cc:2556,2586).
                error_info["stage"] = "adminapi"
        self.stderr = _ShellText(_redact_shell_text(stderr, password), error_info=error_info)
        for stream in pending:
            if pending[stream]:
                output(stream, b"\n")
        if proc.returncode < 0:
            raise ConnectorError("MySQL Shell was stopped (signal %d); the outcome is unknown." % -proc.returncode)
        if proc.returncode != 0:
            if line and line.startswith("ERROR 0 at line 1:") and IDENTITY_MISMATCH_MARKER in line:
                # Only the guard's first-line exception proves AdminAPI was
                # never called. A later exception must retain its outcome.
                raise Refusal(dbops.TARGET_IDENTITY_MISMATCH, "identity_mismatch")
            if line is None:
                raise ConnectorError("MySQL Shell exited %d without an error message; the outcome is unknown."
                                     % proc.returncode)
            raise ConnectorError(_redact_shell_text("mysqlsh exited %d: %s" % (proc.returncode, line), password))
        return self.stdout
