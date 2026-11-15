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
exception). The account comes from a 0600 option file on argv, as for every
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
import os
import re
import subprocess

from .connector import ConnectorError

#: Errors MySQL Shell prints when it cannot open its first session: nothing ran.
#: (2006 and 2013, a lost connection, are left out: a session may have run.)
CONNECT_ERRORS = (1044, 1045, 1130, 2002, 2003, 2005, 2026, 2059, 2061)
_STDIN_LINE = re.compile(r"\bat STDIN:(\d+)\b")
_SHELL_CODE = re.compile(r"\(MYSQLSH (\d+)\)")
_MYSQL_CODE = re.compile(r"MySQL Error (\d+)")
_ANSI = re.compile(r"(?:\x1b|\\u001[bB]|\\x1[bB])\[[0-9;?]*[ -/]*[@-~]")


def find_shell(refresh=False):
    """The newest usable ``mysqlsh`` found on this machine as a dict (path,
    realpath, version, version_tuple), or None. Uses the client inventory
    (client_tools.discover): the same ownership checks and cached version
    probes as every other client myflames runs."""
    from . import client_tools
    try:
        shells = client_tools.discover(refresh=refresh).get("shells") or []
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
    """The error line reviewed_writes reads, from MySQL Shell's output."""
    text = _ANSI.sub("", "%s\n%s" % (stderr or "", stdout or ""))
    lines = [" ".join(raw.split()) for raw in text.splitlines()]
    lines = [line for line in lines if line and not re.match(r"(?:NOTE|WARNING|INFO):", line, re.I)]
    if not lines:
        return None
    where = next((int(m.group(1)) for m in map(_STDIN_LINE.search, lines) if m), None)
    shell_code = next((int(m.group(1)) for m in map(_SHELL_CODE.search, lines) if m), None)
    mysql_code = next((int(m.group(1)) for m in map(_MYSQL_CODE.search, lines) if m), None)
    if where is None and shell_code is None and mysql_code is None:
        return None
    # The numbered exception is more specific than preceding progress or ERROR lines.
    message = next((line for line in lines if _SHELL_CODE.search(line) or _MYSQL_CODE.search(line)), None)
    if message is None:
        message = next((line for line in lines if re.search(r"^ERROR:|Error|error|\(\w+Error\)", line)), None)
    message = message if message is not None else lines[0]
    # A wrapped exception can carry several numbers; its final cause wins.
    message_codes = [match for pattern in (_SHELL_CODE, _MYSQL_CODE) for match in pattern.finditer(message)]
    code = (int(max(message_codes, key=lambda match: match.start()).group(1)) if message_codes else
            shell_code if shell_code is not None else mysql_code if mysql_code is not None else 0)
    message = _STDIN_LINE.sub("", message).strip()
    message = re.sub(r"^ERROR:\s*", "", message)
    if where is None and shell_code is None and code in CONNECT_ERRORS:
        # The first session could not be opened: no statement was read.
        return "ERROR %d: %s" % (code, message)
    return "ERROR %d at line %d: %s" % (code, where or 1, message)


class ShellWriter(object):
    """*conn* whose ``run_write`` runs MySQL Shell instead of the mysql client
    (every other attribute is the connection's)."""

    def __init__(self, conn, shell):
        self._conn = conn
        self._shell = dict(shell or {})
        if not self._shell.get("path"):
            raise ConnectorError("MySQL Shell (mysqlsh) is not installed here, so nothing was sent.")

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def argv(self, defaults):
        """The MySQL Shell command line after the option file: JavaScript, no
        prompts (--no-wizard), nothing printed at start."""
        return defaults + ["--js", "--no-wizard", "--quiet-start=2"]

    def run_write(self, script, extra_flags=None, timeout=None):
        from . import dbops, lifecycle
        from .connector import _run_bounded, _same_dir, bundled_client_dir, checked_program
        password = getattr(self._conn, "password", None)
        tool = {"name": "mysqlsh", "path": self._shell["path"], "engine": "mysql"}
        try:
            with lifecycle._credentials(_connection(self._conn), tool) as (argv, env):
                bundle = bundled_client_dir()
                argv[0] = checked_program(self._shell["path"], self._shell.get("realpath"), bundled=bool(
                    bundle and _same_dir(os.path.dirname(os.path.realpath(self._shell["path"])), bundle)))
                proc = _run_bounded(self.argv(argv), timeout or 120, input=(script or "").encode("utf-8"),
                                    env=env, kill_tree=True)
        except subprocess.TimeoutExpired:
            raise ConnectorError("MySQL Shell timed out after %ss; the outcome is unknown." % (timeout or 120)) \
                from None
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
            if line is None:
                raise ConnectorError("MySQL Shell exited %d without an error message; the outcome is unknown."
                                     % proc.returncode)
            raise ConnectorError(dbops.redact("mysqlsh exited %d: %s" % (proc.returncode, line), password))
        return dbops.redact(_ANSI.sub("", stdout), password)
