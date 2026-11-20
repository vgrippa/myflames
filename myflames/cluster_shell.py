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
import json
import os
import re
import subprocess

from .connector import ConnectorError

#: Errors MySQL Shell prints when it cannot open its first session: nothing ran.
#: (2006 and 2013, a lost connection, are left out: a session may have run.)
CONNECT_ERRORS = (1044, 1045, 1130, 2002, 2003, 2005, 2026, 2059, 2061)
_STDIN_LINE = re.compile(r"[ \t]*at STDIN:([0-9]+)(?::[0-9]+)?[ \t]*\r?")
_SHELL_CODE = re.compile(r"\(MYSQLSH (\d+)\)")
_MYSQL_CODE = re.compile(r"MySQL Error (\d+)")
_ANSI = re.compile(r"(?:\x1b|\\u001[bB]|\\x1[bB])\[[0-9;?]*[ -/]*[@-~]")
IDENTITY_MISMATCH_MARKER = "MYFLAMES_IDENTITY_MISMATCH"


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
        if (not isinstance(name, str) or not name or not isinstance(groups, dict) or not groups or
                any(not isinstance(key, str) or not key or not isinstance(value, str) or
                    not _TARGET_UUID.fullmatch(value) for key, value in groups.items())):
            raise ValueError("The reviewed ClusterSet identity is missing or invalid. Nothing was sent.")
        # Shell 8.0.32: cluster_set/status.cc:132-164 and cluster/status.cc:
        # 1555-1556,1798. extended:2 retains each cluster's groupName.
        guard += ['var myflamesClusterSet = dba.getClusterSet();',
                  'var myflamesStatus = myflamesClusterSet.status({extended: 2});',
                  # JSON.parse preserves a literal cluster named __proto__;
                  # a JavaScript object initializer would change its meaning.
                  'var myflamesGroups = JSON.parse(%s);' % json.dumps(json.dumps(groups, sort_keys=True)),
                  'if (myflamesStatus.domainName !== %s || !myflamesStatus.clusters || '
                  'JSON.stringify(Object.keys(myflamesStatus.clusters).sort()) !== '
                  'JSON.stringify(Object.keys(myflamesGroups).sort()) || '
                  'Object.keys(myflamesGroups).some(function(name) { return '
                  '!myflamesStatus.clusters[name] || myflamesStatus.clusters[name].groupName !== '
                  'myflamesGroups[name]; })) { %s }' % (json.dumps(name), mismatch)]
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
    message_codes = [match for pattern in (_SHELL_CODE, _MYSQL_CODE) for match in pattern.finditer(message)]
    code = int(max(message_codes, key=lambda match: match.start()).group(1)) if message_codes else 0
    message = ("" if _STDIN_LINE.fullmatch(message) else message).strip()
    message = re.sub(r"^ERROR:\s*", "", message)
    if (where is None and shell_code is None and code in CONNECT_ERRORS and not exception and
            re.match(r"^MySQL Error %d\b" % code, message)):
        # src/mysqlsh/main.cc:633-645 prints this bare database error when
        # opening the first session fails. An AdminAPI-wrapped connect error
        # can occur after earlier steps ran and proves no such thing.
        return "ERROR %d: %s" % (code, message)
    if where is None:
        # A number alone does not prove that the first statement failed. Do
        # not synthesize the ERROR/line framing used for definite outcomes,
        # or let embedded server text supply that framing to the parsers.
        message = re.sub(r"\bERROR(\s+\d+)", r"error\1", message)
        message = re.sub(r"\bat line(\s+\d+)\b", r"at input line\1", message)
        return "%s; the statement location and outcome are unknown." % message
    return "ERROR %d at line %d: %s" % (code, where, message)


class ShellWriter(object):
    """*conn* whose ``run_write`` runs MySQL Shell instead of the mysql client
    (every other attribute is the connection's)."""

    def __init__(self, conn, shell):
        if getattr(conn, "ssh_host", None):
            raise ConnectorError("MySQL Shell changes are not run over an SSH connection: MySQL Shell would "
                                 "connect to the other members directly, outside the tunnel, with this connection's "
                                 "user and password; nothing was sent.")
        mode = (getattr(conn, 'ssl_mode', None) or 'PREFERRED').upper()
        if mode == 'VERIFY_IDENTITY':
            raise ConnectorError('MySQL Shell changes are not offered when TLS mode is VERIFY_IDENTITY: '
                                 'MySQL Shell checks the certificate authority on its sessions to other members, '
                                 'not their host names. Use VERIFY_CA, or run the change in MySQL Shell.')
        if mode == 'REQUIRED':
            raise ConnectorError('MySQL Shell changes are not offered when TLS mode is REQUIRED without CA '
                                 'verification: MySQL Shell falls back to PREFERRED on its sessions to other '
                                 'members. Use VERIFY_CA, or run the change in MySQL Shell.')
        self._conn = conn
        self._shell = dict(shell or {})
        if not self._shell.get("path"):
            raise ConnectorError("MySQL Shell (mysqlsh) is not installed here, so nothing was sent.")

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def argv(self, defaults):
        """The MySQL Shell command line after the option file: JavaScript, no
        prompts (--no-wizard), nothing printed at start."""
        from .lifecycle import _shell_plugin_flags
        return defaults + _shell_plugin_flags(self._shell) + ["--js", "--no-wizard", "--quiet-start=2"]

    def run_write(self, script, extra_flags=None, timeout=None):
        from . import dbops, lifecycle
        from .connection_groups import ProfileReadError
        from .connector import _run_bounded, checked_shell_program
        from .reviewed_writes import Refusal
        # Consent precedes all credential access and is checked again at spawn
        # after option-file creation and executable validation may have waited.
        guard = getattr(self._conn, "_login_guard", None)
        if guard is not None:
            guard()
        password = getattr(self._conn, "password", None)
        tool = {"name": "mysqlsh", "path": self._shell["path"], "engine": "mysql"}
        try:
            with lifecycle._credentials(_connection(self._conn), tool) as (argv, env):
                argv[0] = checked_shell_program(self._shell["path"], self._shell.get("realpath"))
                if guard is not None:
                    guard()
                    self._conn._group_login_started = True
                proc = _run_bounded(self.argv(argv), timeout or 120, input=(script or "").encode("utf-8"),
                                    env=env, kill_tree=True)
        except subprocess.TimeoutExpired:
            raise ConnectorError("MySQL Shell timed out after %ss; the outcome is unknown." % (timeout or 120)) \
                from None
        except (Refusal, ProfileReadError):
            raise
        except ValueError as exc:
            raise ConnectorError(dbops.redact(str(exc), password)) from None
        except OSError as exc:
            raise ConnectorError("MySQL Shell could not be started (%s); nothing was sent." %
                                 dbops.redact(str(exc), password)) from None
        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        if proc.returncode < 0:
            raise ConnectorError("MySQL Shell was stopped (signal %d); the outcome is unknown." % -proc.returncode)
        if proc.returncode != 0:
            line = error_text(stderr, stdout)
            if line and line.startswith("ERROR 0 at line 1:") and IDENTITY_MISMATCH_MARKER in line:
                # Only the guard's first-line exception proves AdminAPI was
                # never called. A later exception must retain its outcome.
                raise Refusal(dbops.TARGET_IDENTITY_MISMATCH, "identity_mismatch")
            if line is None:
                raise ConnectorError("MySQL Shell exited %d without an error message; the outcome is unknown."
                                     % proc.returncode)
            raise ConnectorError(dbops.redact("mysqlsh exited %d: %s" % (proc.returncode, line), password))
        return dbops.redact(_ANSI.sub("", stdout), password)
