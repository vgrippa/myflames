#!/usr/bin/env python3
"""Read the system variables of every cataloged MySQL and MariaDB release from a
running server, one ``sysvar-runtime-1.0`` JSON file per release.

Lab use only. Nothing here connects to a server this script did not start.
For each release in scope the script pulls the official image
(public.ecr.aws/docker/library first, then docker.io/library only when ECR has
no such tag; a Hub tag that only has a schema 1 manifest, which current Docker
refuses, is copied into a local image with skopeo run as a throwaway container)
and runs it as a throwaway container named ``myflames-catalog-*``
(``--rm``, removed with its anonymous volumes, no published ports) whose
entrypoint is ``sleep``. Through ``docker exec`` it initializes and starts the
server with ``--no-defaults`` (no option file of the image is read, so every
variable keeps its compiled default; this matters on MariaDB too, whose
DEFAULT_VALUE column follows character-set-server from option files), gives
root a random password (sent on stdin, never printed), reads the variables with
the client inside the container, removes the container, and removes the image
when it was not present before the run.

A release with no image runs from an official package inside a plain Ubuntu
container (``method: "package"``): MariaDB's own Ubuntu packages from
archive.mariadb.org (the build the images install, on the same platform, with
the Ubuntu release of the nearest image of the series), else the MariaDB
x86_64 bintar; MySQL the generic Linux tarball. A release nobody can run gets a
record with ``"skipped": "<reason>"`` and no variables.

Scope (from the read-only source checkouts, never checked out):
  MySQL   plain ``mysql-X.Y.Z`` tags (a ``-respin`` tag stands for its version):
          every 5.7 GA release (5.7.9 to 5.7.44; 5.7.1 to 5.7.6 are milestones
          -m11 to -m16 and 5.7.7 and 5.7.8 are -rc) and everything from 8.0.11
          whose MYSQL_VERSION (VERSION in old tags) says GA, LTS or INNOVATION.
          A tag with no MYSQL_VERSION_MATURITY and an empty MYSQL_VERSION_EXTRA
          is GA; ``-dmr``, ``-rc`` and ``-mNN`` are DMR, RC and milestones.
  MariaDB plain ``mariadb-X.Y.Z`` tags from 10.6 whose VERSION says
          SERVER_MATURITY=stable.

What is read, per engine:
  MySQL   names and values from performance_schema.global_variables; session
          scope from performance_schema.variables_by_thread for the reading
          thread (a name only there is session only); VARIABLE_SOURCE, MIN_VALUE
          and MAX_VALUE from performance_schema.variables_info; scope, type, min
          and max from performance_schema.variables_metadata where the release
          has it (9.0.0 and later). A COMPILED variable shows its default as its
          value. Any other source (the socket set on the command line, a value
          the server set itself) takes its default from ``mysqld --no-defaults
          --verbose --help`` run in the same container. Types: variables_metadata where present (Integer -> integer,
          Numeric -> double, String -> string, Enumeration -> enum, Boolean ->
          boolean, Set -> set, or flagset when the value is ``name=on|off,...``);
          before 9.0 only boolean is inferred (the value is ON or OFF and --help
          prints the option as TRUE or FALSE), otherwise null. MIN_VALUE and
          MAX_VALUE are kept only for numeric variables; the server prints a
          double's range as the raw IEEE 754 bits, which are decoded.
          MySQL 5.7 has neither variables_info nor variables_metadata: names
          and scopes come from performance_schema.global_variables and
          variables_by_thread (information_schema.GLOBAL_VARIABLES and
          SESSION_VARIABLES when performance_schema cannot be read), every
          value is the default because the server ran with --no-defaults
          (socket, set on the command line, takes --help's value), min, max
          and origin are null, and type is set only by the boolean rule.
          Dynamic: ``SET GLOBAL name = @@GLOBAL.name`` (``SET SESSION`` for
          session-only names), one statement at a time, inside the throwaway
          container only: error 1238 "read only" -> false, success -> true,
          anything else -> null with the error number in ``notes``.
  MariaDB information_schema.SYSTEM_VARIABLES: VARIABLE_SCOPE (GLOBAL ->
          global, SESSION -> both, SESSION ONLY -> session), VARIABLE_TYPE
          (BOOLEAN -> boolean; INT, INT UNSIGNED, BIGINT, BIGINT UNSIGNED ->
          integer; DOUBLE -> double; ENUM -> enum; SET -> set; FLAGSET ->
          flagset; VARCHAR -> string; anything else -> null), DEFAULT_VALUE,
          NUMERIC_MIN_VALUE, NUMERIC_MAX_VALUE, ENUM_VALUE_LIST (enum, set and
          flagset only) and READ_ONLY (YES -> dynamic false, NO -> true).
  Both    Deprecation the server reports: ``SET @v = @@scope.name`` for every
          variable and the SET probe (MariaDB: only for variables that are not
          read only), each followed by SHOW WARNINGS; the text of warnings that
          say "deprecated" is kept in ``deprecated_warning``.
          Optional plugins, loaded before reading and tagged ``plugin``: MySQL
          group_replication, semisync (rpl_semi_sync_source/replica, else the
          master/slave names), clone, the validate_password component
          (``component_validate_password``, 8.0 and later) and the
          validate_password plugin (5.7, and still shipped in later releases,
          where it is deprecated). On 5.7
          group_replication initializes only with server_id other than 0 and
          TABLE replica repositories: the lab sets those three for the INSTALL,
          puts them back, and takes their defaults from --help;
          MariaDB server_audit, simple_password_check, sql_errlog and
          query_response_time. A plugin that fails to load is a note.
          Host-dependent defaults: a second, smaller server (one CPU, 1 GiB,
          another host name) is read the same way; a default that differs is
          marked ``default_note: "host"``. Docker's cpuset does not change what
          MySQL before 9.3 counts (it counts online CPUs), so the small run also
          mounts a one-CPU /sys/devices/system/cpu/online.

SET statements not probed because they act even when the value is unchanged:
  MySQL   binlog_checksum (Sys_var_enum_binlog_checksum::global_update rotates
          the binary log on every SET); query_cache_size on 5.7
          (fix_query_cache_size resizes and empties the query cache on every
          SET).
  MariaDB query_response_time_flush (its update function flushes the
          statistics on every SET).

Examples:

    python3 scripts/record-sysvar-catalog.py --list
    python3 scripts/record-sysvar-catalog.py --engine mysql --versions 8.0.11 8.4.0
    python3 scripts/record-sysvar-catalog.py --jobs 3 --log local/param-versions-2026-10-28/runtime/run.log
    python3 scripts/record-sysvar-catalog.py --engine mariadb --versions 11.4.2 --force --platform linux/amd64

Output (``--out``, default local/param-versions-2026-10-28/runtime):
<engine>/<version>.json, versions-<engine>.json and REPORT.md. A release that
already has a record with variables is not read again without ``--force``;
releases that failed are tried again on every run, releases with no image or
package only with ``--force``.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import secrets
import shlex
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_ROOT = os.path.dirname(REPO)
DEFAULT_OUT = os.path.join(REPO, "local", "param-versions-2026-10-28", "runtime")
SCHEMA = "sysvar-runtime-1.0"
VERSIONS_SCHEMA = "sysvar-runtime-versions-1.0"
PREFIX = "myflames-catalog-"
ECR = "public.ecr.aws/docker/library/"
HUB = "docker.io/library/"
MAX_JOBS = 3
ENGINES = ("mysql", "mariadb")
MIN_VERSION = {"mysql": (8, 0, 11), "mariadb": (10, 6, 0)}
MYSQL_MATURITIES = ("GA", "LTS", "INNOVATION")
CLIENT_CNF = "/tmp/myflames-client.cnf"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
NAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")
FLAGSET_RE = re.compile(r"^[A-Za-z0-9_]+=(on|off)(,[A-Za-z0-9_]+=(on|off))*$")
DOUBLE_TEXT_RE = re.compile(r"^-?\d+\.\d{6}$")
INT_TEXT_RE = re.compile(r"^-?\d+$")
PROBE_MARK = "myflames-probe"
# SET statements that act even when the value is unchanged (see the docstring).
PROBE_SKIP = {
    "mysql": {"binlog_checksum": "SET rotates the binary log even when the value is unchanged",
              "query_cache_size": "SET resizes and empties the query cache even when the value is unchanged"},
    "mariadb": {"query_response_time_flush": "SET flushes the query response time statistics on every update"},
}
MYSQL_PLUGINS = (
    # (label, statements, fallback label, fallback statements)
    # MySQL 5.7's plugin refuses to initialize while server_id is 0 and the replica
    # repositories are FILE (all three are 5.7's defaults); see PLUGIN_SETS.
    ("group_replication", ("INSTALL PLUGIN group_replication SONAME 'group_replication.so'",),
     "group_replication", ("SET GLOBAL server_id = 1", "SET GLOBAL master_info_repository = 'TABLE'",
                           "SET GLOBAL relay_log_info_repository = 'TABLE'",
                           "INSTALL PLUGIN group_replication SONAME 'group_replication.so'")),
    ("rpl_semi_sync_source", ("INSTALL PLUGIN rpl_semi_sync_source SONAME 'semisync_source.so'",),
     "rpl_semi_sync_master", ("INSTALL PLUGIN rpl_semi_sync_master SONAME 'semisync_master.so'",)),
    ("rpl_semi_sync_replica", ("INSTALL PLUGIN rpl_semi_sync_replica SONAME 'semisync_replica.so'",),
     "rpl_semi_sync_slave", ("INSTALL PLUGIN rpl_semi_sync_slave SONAME 'semisync_slave.so'",)),
    ("clone", ("INSTALL PLUGIN clone SONAME 'mysql_clone.so'",), None, None),
    # Two forms: the component (8.0 and later, validate_password.* variables) and
    # the plugin (5.7, and still shipped, deprecated, in 8.0 and later:
    # validate_password_* variables). Both are loaded where they exist, so a
    # 5.7 -> 8.0 comparison does not show the plugin's variables as removed.
    ("component_validate_password", ("INSTALL COMPONENT 'file://component_validate_password'",), None, None),
    ("validate_password", ("INSTALL PLUGIN validate_password SONAME 'validate_password.so'",), None, None),
)
# Variables a fallback install sets for a while. Their values are put back
# right after the attempt, and their defaults come from --help, not from the
# running server.
PLUGIN_SETS = {"group_replication": ("server_id", "master_info_repository", "relay_log_info_repository")}
MARIADB_PLUGINS = (
    ("server_audit", ("INSTALL SONAME 'server_audit'",), None, None),
    ("simple_password_check", ("INSTALL SONAME 'simple_password_check'",), None, None),
    ("sql_error_log", ("INSTALL SONAME 'sql_errlog'",), None, None),
    ("query_response_time", ("INSTALL SONAME 'query_response_time'",), None, None),
)
MYSQL_TYPES = {"Integer": "integer", "Numeric": "double", "String": "string", "Enumeration": "enum",
               "Boolean": "boolean", "Set": "set"}
MARIADB_TYPES = {"BOOLEAN": "boolean", "INT": "integer", "INT UNSIGNED": "integer", "BIGINT": "integer",
                 "BIGINT UNSIGNED": "integer", "DOUBLE": "double", "ENUM": "enum", "SET": "set",
                 "FLAGSET": "flagset", "VARCHAR": "string"}
MARIADB_SCOPES = {"GLOBAL": "global", "SESSION": "both", "SESSION ONLY": "session"}
FIELDS = ("scope", "type", "default", "default_note", "min", "max", "values", "dynamic", "dynamic_how",
          "deprecated_warning", "plugin", "default_from", "origin")
# Where a value may come from in a --no-defaults lab server: any name (None)
# or only the listed names. Anything else (an option file: MySQL GLOBAL,
# SERVER, EXPLICIT, EXTRA, USER, LOGIN, PERSISTED; MariaDB CONFIG,
# ENVIRONMENT, SQL) means the reading did not see compiled defaults, and the
# release fails instead of writing a record.
ALLOWED_ORIGINS = {
    # socket is set on the command line; the server sets foreign_key_checks
    # itself right after a --no-defaults start (VARIABLE_SOURCE DYNAMIC).
    "mysql": {"COMPILED": None, "COMMAND_LINE": frozenset(["socket"]),
              "DYNAMIC": frozenset(["foreign_key_checks"])},
    # AUTO: the server sized the value itself at startup. basedir is always
    # ENVIRONMENT on Unix (mysqld.cc: MY_BASEDIR_VERSION, else the compiled
    # home); port and socket would be too if MYSQL_TCP_PORT or MYSQL_UNIX_PORT
    # were set, which the lab never does.
    "mariadb": {"COMPILE-TIME": None, "AUTO": None, "COMMAND-LINE": frozenset(["socket"]),
                "ENVIRONMENT": frozenset(["basedir"])},
}

_LOG_LOCK = threading.Lock()
_LOG_FILE = None


def log(message):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = "%s %s" % (stamp, message)
    with _LOG_LOCK:
        print(line, flush=True)
        if _LOG_FILE is not None:
            _LOG_FILE.write(line + "\n")
            _LOG_FILE.flush()


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Version scope (pure)
# --------------------------------------------------------------------------

def version_key(version):
    return tuple(int(part) for part in version.split("."))


def parse_version_file(text):
    """KEY=VALUE lines of MYSQL_VERSION / VERSION, quotes stripped."""
    fields = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


def mysql_maturity(fields):
    """GA, LTS, INNOVATION, DMR, RC or the raw extra (MYSQL_VERSION_EXTRA)."""
    maturity = fields.get("MYSQL_VERSION_MATURITY")
    if maturity:
        return maturity.upper()
    extra = fields.get("MYSQL_VERSION_EXTRA", "").lstrip("-").upper()
    if not extra:
        return "GA"
    return extra


def mariadb_maturity(fields):
    return fields.get("SERVER_MATURITY", "").lower() or None


def select_tags(engine, tag_names):
    """Map version -> tag from plain numeric tags; a -respin tag replaces its version."""
    if engine == "mysql":
        pattern = re.compile(r"^mysql-(\d+\.\d+\.\d+)(-respin)?$")
    else:
        pattern = re.compile(r"^mariadb-(\d+\.\d+\.\d+)$")
    chosen = {}
    for tag in tag_names:
        match = pattern.match(tag)
        if not match:
            continue
        version = match.group(1)
        respin = bool(match.lastindex and match.lastindex >= 2 and match.group(2))
        if version not in chosen or respin:
            chosen[version] = tag
    return chosen


def compute_scope(engine, tag_names, read_file, tag_dates=None):
    """In-scope releases and the tags left out, from the tag list and VERSION files.

    read_file(tag, path) returns the file's text or None. Returns
    (in_scope, excluded): lists of dicts sorted by version.
    """
    tag_dates = tag_dates or {}
    floor = MIN_VERSION[engine]
    chosen = select_tags(engine, tag_names)
    in_scope, excluded = [], []
    for version in sorted(chosen, key=version_key):
        tag = chosen[version]
        key = version_key(version)
        if key[:2] < floor[:2] and engine == "mariadb":
            continue
        if engine == "mysql" and key[:2] < (5, 7):
            continue
        if engine == "mysql":
            text = read_file(tag, "MYSQL_VERSION")
            if text is None:
                text = read_file(tag, "VERSION")
            fields = parse_version_file(text)
            maturity = mysql_maturity(fields)
            # 5.7 GA releases are in scope; from 8.0 only 8.0.11 and later.
            ok = maturity in MYSQL_MATURITIES and (key >= floor or key[:2] == (5, 7))
        else:
            fields = parse_version_file(read_file(tag, "VERSION"))
            maturity = mariadb_maturity(fields)
            ok = key >= floor and maturity == "stable"
        entry = {"version": version, "tag": tag, "maturity": maturity, "tag_date": tag_dates.get(tag)}
        if ok:
            in_scope.append(entry)
        else:
            if key < floor and not (engine == "mysql" and key[:2] == (5, 7)):
                entry["reason"] = "Before %s and %s" % (".".join(str(p) for p in floor), maturity)
            else:
                entry["reason"] = "Maturity %s" % maturity
            excluded.append(entry)
    return in_scope, excluded


class GitReader(object):
    """Read files at tags of a read-only checkout through one `git cat-file --batch`."""

    def __init__(self, path):
        self.path = path
        self.proc = subprocess.Popen(["git", "-C", path, "cat-file", "--batch"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE)

    def read(self, tag, name):
        self.proc.stdin.write(("%s:%s\n" % (tag, name)).encode())
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().decode().split()
        if len(header) < 2 or header[1] == "missing":
            return None
        size = int(header[2])
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)
        return data.decode("utf-8", "replace")

    def tags(self):
        out = subprocess.run(["git", "-C", self.path, "for-each-ref", "refs/tags",
                              "--format=%(refname:short)\t%(*committerdate:iso-strict)\t%(committerdate:iso-strict)"],
                             stdout=subprocess.PIPE, check=True).stdout.decode()
        names, dates = [], {}
        for line in out.splitlines():
            parts = line.split("\t")
            if not parts or not parts[0]:
                continue
            names.append(parts[0])
            date = (parts[1] if len(parts) > 1 and parts[1] else "") or (parts[2] if len(parts) > 2 else "")
            dates[parts[0]] = date or None
        return names, dates

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def scope_from_checkout(engine, path):
    reader = GitReader(path)
    try:
        names, dates = reader.tags()
        return compute_scope(engine, names, reader.read, dates)
    finally:
        reader.close()


# --------------------------------------------------------------------------
# Parsing (pure)
# --------------------------------------------------------------------------

def parse_xml_rows(text):
    """Rows of `mysql --xml` output as dicts; SQL NULL (xsi:nil) is None."""
    rows = []
    text = text or ""
    for chunk in re.split(r"<\?xml[^>]*\?>", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        root = ET.fromstring(chunk)
        for row in root.iter("row"):
            item = {}
            for field in row.iter("field"):
                name = field.get("name")
                if field.get(XSI_NIL) == "true":
                    item[name] = None
                else:
                    item[name] = field.text or ""
            rows.append(item)
    return rows


def parse_help(text):
    """Option defaults from `mysqld --verbose --help`: name (underscores) -> text or None.

    None stands for "(No default value)"; an empty value is "".
    """
    values = {}
    in_table = False
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if not in_table:
            if line.startswith("-----") and index > 0 and "Value (after reading options)" in lines[index - 1]:
                in_table = True
            continue
        if not line.strip():
            break
        match = re.match(r"^(\S+)(?:\s+(.*?))?\s*$", line)
        if not match:
            continue
        name = match.group(1).replace("-", "_")
        value = match.group(2) if match.group(2) is not None else ""
        values[name] = None if value == "(No default value)" else value
    return values


def missing_secure_file_priv(text):
    """The folder named when MySQL refuses to start because secure_file_priv's folder is missing."""
    if "secure-file-priv" not in (text or ""):
        return None
    match = re.search(r"Supplied value : (/[A-Za-z0-9_./-]+)", text)
    return match.group(1) if match else None


def help_is_boolean(value):
    return value in ("TRUE", "FALSE")


def server_text_from_help(help_value, runtime_value):
    """Write a --help default the way the server shows the variable's value."""
    if help_value is None:
        return None
    if help_is_boolean(help_value) and runtime_value in ("ON", "OFF"):
        return "ON" if help_value == "TRUE" else "OFF"
    if runtime_value is not None and DOUBLE_TEXT_RE.match(runtime_value):
        try:
            return "%.6f" % float(help_value)
        except ValueError:
            return help_value
    return help_value


def decode_double_bits(text):
    """The server prints a double's MIN/MAX as its IEEE 754 bits; return the number as %.6f."""
    if text is None or not INT_TEXT_RE.match(text):
        return None
    bits = int(text)
    if bits < 0:
        bits &= (1 << 64) - 1
    value = struct.unpack("<d", struct.pack("<Q", bits))[0]
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return "%.6f" % value


def mysql_type(meta_type, value, help_value):
    if meta_type:
        mapped = MYSQL_TYPES.get(meta_type)
        if mapped == "set" and value and FLAGSET_RE.match(value):
            return "flagset"
        return mapped
    if value in ("ON", "OFF") and help_is_boolean(help_value):
        return "boolean"
    return None


def mysql_range(vtype, value, min_text, max_text):
    """MIN/MAX for numeric variables only (variables_info prints 0/0 for the rest)."""
    if min_text is None and max_text is None:
        return None, None
    if min_text == "0" and max_text == "0":
        return None, None
    double = vtype == "double" or (vtype is None and value is not None and DOUBLE_TEXT_RE.match(value))
    if double:
        return decode_double_bits(min_text), decode_double_bits(max_text)
    if vtype == "integer":
        return min_text, max_text
    if vtype is None and value is not None and INT_TEXT_RE.match(value) and not (min_text == "0" and max_text == "0"):
        return min_text, max_text
    return None, None


def blank_facts():
    return dict((field, None) for field in FIELDS)


# Variables the lab sets on the server's command line; with no variables_info
# (MySQL 5.7) their default comes from --help instead of the running value.
COMMAND_LINE_NAMES = ("socket",)
NO_INFO_NOTE = ("This release has no performance_schema.variables_info or variables_metadata (both arrived in "
                "8.0), so VARIABLE_SOURCE, MIN_VALUE and MAX_VALUE cannot be read: min, max and origin are null, and "
                "type is set only for booleans (an ON or OFF value with a TRUE or FALSE option in --help). The "
                "server ran with --no-defaults, so every value read is the default; socket, which the lab sets on "
                "the command line, takes --help's value. The origin guard cannot check this release; the "
                "--no-defaults start is what keeps option files out.")


def mysql_variables(global_rows, thread_rows, info_rows, meta_rows, help_values, plugin_of=None, lab_set=()):
    """Assemble variables from performance_schema reads and --help (no probe yet).

    Rows are dicts as parse_xml_rows returns them; info_rows is None when the
    release has no performance_schema.variables_info (MySQL 5.7). Returns
    (variables, notes).
    """
    plugin_of = plugin_of or {}
    notes = []
    no_info = info_rows is None
    global_values = dict((r["VARIABLE_NAME"].lower(), r.get("VARIABLE_VALUE")) for r in global_rows)
    thread_values = dict((r["VARIABLE_NAME"].lower(), r.get("VARIABLE_VALUE")) for r in thread_rows)
    info = dict((r["VARIABLE_NAME"].lower(), r) for r in (info_rows or []))
    meta = dict((r["VARIABLE_NAME"].lower(), r) for r in (meta_rows or []))
    names = set(global_values) | set(thread_values)
    missing = sorted(set(info) - names)
    if missing:
        notes.append("variables_info lists names in neither global_variables nor variables_by_thread: "
                     + ", ".join(missing))
    variables = {}
    scope_disagree = []
    for name in sorted(names):
        facts = blank_facts()
        if name in global_values and name in thread_values:
            facts["scope"] = "both"
        elif name in global_values:
            facts["scope"] = "global"
        else:
            facts["scope"] = "session"
        value = global_values[name] if name in global_values else thread_values.get(name)
        row = meta.get(name)
        if row is not None:
            meta_scope = {"GLOBAL": "global", "SESSION": "both", "SESSION_ONLY": "session"}.get(row.get("VARIABLE_SCOPE"))
            if meta_scope and meta_scope != facts["scope"]:
                scope_disagree.append("%s (tables: %s, variables_metadata: %s)" % (name, facts["scope"], meta_scope))
        help_value = help_values.get(name) if help_values else None
        facts["type"] = mysql_type(row.get("DATA_TYPE") if row else None, value, help_value)
        irow = info.get(name, {})
        source = irow.get("VARIABLE_SOURCE")
        facts["origin"] = None if no_info else source
        if no_info and name in COMMAND_LINE_NAMES:
            source = "COMMAND_LINE"  # the lab set it; not stored, origin stays null
        elif name in lab_set:
            source = "LAB"  # changed by the lab for a plugin install: default from --help
        facts["min"], facts["max"] = mysql_range(facts["type"], value, irow.get("MIN_VALUE"), irow.get("MAX_VALUE"))
        if source == "COMPILED" or source is None:
            facts["default"] = value
            facts["default_from"] = "runtime"
            if value is None:
                facts["default_note"] = "null"
        else:
            facts["default_from"] = "help"
            if help_values is not None and name in help_values:
                facts["default"] = server_text_from_help(help_values[name], value)
                if facts["default"] is None:
                    facts["default_note"] = "null"
            else:
                facts["default_note"] = "unknown"
        facts["plugin"] = plugin_of.get(name)
        variables[name] = facts
    if scope_disagree:
        notes.append("Scope from the performance_schema tables differs from variables_metadata for: "
                     + "; ".join(scope_disagree))
    if no_info:
        notes.append(NO_INFO_NOTE)
        differ = help_differences(variables, global_values, thread_values, help_values)
        if differ:
            notes.append("The running value differs from --help's for %d variables (the server sizes or sets "
                         "them at startup; the running value is kept): %s" % (len(differ), ", ".join(differ)))
    return variables, notes


def unclear_scopes(variables, notes):
    """information_schema.SESSION_VARIABLES also lists global-only variables: global and both look alike."""
    unclear = sorted(name for name, facts in variables.items() if facts["scope"] == "both")
    for name in unclear:
        variables[name]["scope"] = None
    if unclear:
        notes.append("information_schema.SESSION_VARIABLES also lists global-only variables, so the scope of %d "
                     "variables found in both lists is unknown (null)." % len(unclear))


def help_differences(variables, global_values, thread_values, help_values):
    """Names whose running value is not --help's value written the server's way."""
    differ = []
    for name in sorted(variables):
        if not help_values or name not in help_values or name in COMMAND_LINE_NAMES or \
                variables[name].get("default_from") == "help":
            continue
        value = global_values[name] if name in global_values else thread_values.get(name)
        if server_text_from_help(help_values[name], value) != value:
            differ.append(name)
    return differ


def mariadb_variables(rows, plugin_of=None):
    """Assemble variables from information_schema.SYSTEM_VARIABLES rows."""
    plugin_of = plugin_of or {}
    notes = []
    variables = {}
    unknown_types = set()
    for row in rows:
        name = row["VARIABLE_NAME"].lower()
        facts = blank_facts()
        facts["scope"] = MARIADB_SCOPES.get((row.get("VARIABLE_SCOPE") or "").upper())
        raw_type = (row.get("VARIABLE_TYPE") or "").upper()
        facts["type"] = MARIADB_TYPES.get(raw_type)
        if facts["type"] is None:
            unknown_types.add(raw_type or "(empty)")
        facts["default"] = row.get("DEFAULT_VALUE")
        facts["default_from"] = "default_value_column"
        facts["origin"] = row.get("GLOBAL_VALUE_ORIGIN")
        if facts["default"] is None:
            facts["default_note"] = "null"
        if facts["type"] in ("integer", "double"):
            facts["min"] = row.get("NUMERIC_MIN_VALUE")
            facts["max"] = row.get("NUMERIC_MAX_VALUE")
        if facts["type"] in ("enum", "set", "flagset") and row.get("ENUM_VALUE_LIST"):
            facts["values"] = row["ENUM_VALUE_LIST"].split(",")
        read_only = (row.get("READ_ONLY") or "").upper()
        if read_only in ("YES", "NO"):
            facts["dynamic"] = read_only == "NO"
            facts["dynamic_how"] = "read_only_column"
        facts["plugin"] = plugin_of.get(name)
        variables[name] = facts
    if unknown_types:
        notes.append("VARIABLE_TYPE values with no type word (type null): " + ", ".join(sorted(unknown_types)))
    return variables, notes


# A statement that reads a table resets the warning list. MariaDB keeps the
# messages of the last statement that produced any, so without it one error
# would be reported again after every later statement.
CLEAR_WARNINGS = {"mysql": "SELECT 1 FROM mysql.user LIMIT 0;",
                  "mariadb": "SELECT 1 FROM mysql.global_priv LIMIT 0;"}


def probe_plan(engine, variables, set_probe=True):
    """Statements for the deprecation read and the SET probe.

    Returns (sql_text, steps) where steps[i] = (kind, name); kind is "read" or "set".
    """
    clear = CLEAR_WARNINGS[engine]
    lines = ["SET SESSION lock_wait_timeout = 5;"]
    steps = []
    skip = PROBE_SKIP.get(engine, {})
    for name in sorted(variables):
        if not NAME_RE.match(name):
            continue
        scope = variables[name]["scope"]
        sql_scope = "SESSION" if scope == "session" else "GLOBAL"
        steps.append(("read", name))
        lines.append(clear)
        lines.append("SELECT '%s %d';" % (PROBE_MARK, len(steps) - 1))
        lines.append("SET @myflames_probe = @@%s.%s;" % (sql_scope, name))
        lines.append("SHOW WARNINGS;")
        if not set_probe or name in skip:
            continue
        if engine == "mariadb" and variables[name].get("dynamic") is False:
            continue
        steps.append(("set", name))
        lines.append(clear)
        lines.append("SELECT '%s %d';" % (PROBE_MARK, len(steps) - 1))
        lines.append("SET %s %s = @@%s.%s;" % (sql_scope, name, sql_scope, name))
        lines.append("SHOW WARNINGS;")
    return "\n".join(lines) + "\n", steps


def unescape_batch(text):
    """Undo the mysql client's batch-mode escapes (\\t, \\n, \\0, \\\\)."""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"t": "\t", "n": "\n", "0": "\0", "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_probe_output(text):
    """Warnings per probe step from `mysql -B -N --force` output: index -> [(level, code, message)]."""
    results = {}
    current = None
    for line in (text or "").splitlines():
        if line.startswith(PROBE_MARK + " "):
            try:
                current = int(line.split()[1])
            except (IndexError, ValueError):
                current = None
                continue
            results[current] = []
            continue
        if current is None:
            continue
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[1].isdigit():
            results[current].append((parts[0], int(parts[1]), unescape_batch(parts[2])))
    return results


def is_deprecation(level, code, message):
    return level in ("Warning", "Note") and (code in (1287, 1681) or "deprecated" in message.lower())


def apply_probe(engine, variables, steps, results, notes):
    """Fill dynamic (MySQL) and deprecated_warning (both) from the probe results."""
    errors = []
    unfinished = 0
    deprecations = {}
    for index, (kind, name) in enumerate(steps):
        facts = variables[name]
        if index not in results:
            unfinished += 1
            continue
        rows = results[index]
        for level, code, message in rows:
            if is_deprecation(level, code, message):
                seen = deprecations.setdefault(name, [])
                if message not in seen:
                    seen.append(message)
        if kind != "set":
            continue
        error = next(((code, message) for level, code, message in rows if level == "Error"), None)
        if engine == "mysql":
            if error is None:
                facts["dynamic"], facts["dynamic_how"] = True, "set_probe"
            elif error[0] == 1238 and "read only" in error[1]:
                facts["dynamic"], facts["dynamic_how"] = False, "set_probe"
            else:
                facts["dynamic"], facts["dynamic_how"] = None, None
                errors.append("%s: error %d (%s)" % (name, error[0], error[1]))
        elif error is not None:
            errors.append("%s: error %d (%s)" % (name, error[0], error[1]))
    for name, messages in deprecations.items():
        variables[name]["deprecated_warning"] = "\n".join(messages)
    for name, reason in sorted(PROBE_SKIP.get(engine, {}).items()):
        if name in variables:
            notes.append("SET probe skipped for %s: %s." % (name, reason))
    if errors:
        if engine == "mysql":
            notes.append("SET probe errors (dynamic left null): " + "; ".join(errors))
        else:
            notes.append("SET probe errors (dynamic comes from READ_ONLY; listed for reference): "
                         + "; ".join(errors))
    if unfinished:
        notes.append("The probe did not reach %d of %d statements." % (unfinished, len(steps)))
    return variables


def origin_problems(engine, variables):
    """Variables whose value came from somewhere a --no-defaults lab server should not read."""
    allowed = ALLOWED_ORIGINS[engine]
    problems = []
    for name in sorted(variables):
        origin = variables[name].get("origin")
        if origin is None:
            continue
        if origin not in allowed:
            problems.append("%s (%s)" % (name, origin))
        elif allowed[origin] is not None and name not in allowed[origin]:
            problems.append("%s (%s)" % (name, origin))
    return problems


def mysql_host_values(variables):
    """What the host comparison looks at on MySQL: the default as read."""
    return dict((name, facts.get("default")) for name, facts in variables.items())


def mariadb_host_values(rows):
    """What the host comparison looks at on MariaDB.

    DEFAULT_VALUE, plus the global value when the server computed it itself
    (GLOBAL_VALUE_ORIGIN COMPILE-TIME or AUTO): a value sized at startup from the
    CPUs, the memory or the host name differs between the two runs even when
    DEFAULT_VALUE does not.
    """
    values = {}
    for row in rows:
        origin = (row.get("GLOBAL_VALUE_ORIGIN") or "").upper()
        computed = row.get("GLOBAL_VALUE") if origin in ("COMPILE-TIME", "AUTO") else None
        values[row["VARIABLE_NAME"].lower()] = (row.get("DEFAULT_VALUE"), computed)
    return values


def mark_host(variables, main_values, other_values):
    """Set default_note "host" where a second, smaller run read something else."""
    changed = []
    for name, facts in variables.items():
        if name not in other_values or name not in main_values:
            continue
        if facts.get("default_note") == "unknown":
            continue
        if other_values[name] != main_values[name]:
            facts["default_note"] = "host"
            changed.append(name)
    return sorted(changed)


def ordered_variables(variables):
    out = {}
    for name in sorted(variables):
        facts = variables[name]
        out[name] = dict((field, facts.get(field)) for field in FIELDS)
    return out


def build_record(engine, version, meta, variables, plugins_loaded, notes):
    record = {"schema": SCHEMA, "engine": engine, "version": version}
    for key in ("tag", "maturity", "method", "image", "image_digest", "package", "platform", "server_version",
                "version_comment", "read_at", "elapsed_seconds", "host_check"):
        if key in meta:
            record[key] = meta[key]
    record["plugins_loaded"] = list(plugins_loaded)
    record["counts"] = {"variables": len(variables),
                        "plugin_variables": sum(1 for f in variables.values() if f.get("plugin")),
                        "dynamic_known": sum(1 for f in variables.values() if f.get("dynamic") is not None),
                        "deprecated_warning": sum(1 for f in variables.values() if f.get("deprecated_warning")),
                        "host_defaults": sum(1 for f in variables.values() if f.get("default_note") == "host")}
    record["variables"] = ordered_variables(variables)
    record["notes"] = list(notes)
    return record


def skipped_record(engine, version, meta, reason, kind):
    record = {"schema": SCHEMA, "engine": engine, "version": version}
    for key in ("tag", "maturity", "method", "image", "platform", "read_at", "elapsed_seconds"):
        if key in meta:
            record[key] = meta[key]
    record["skipped"] = reason
    record["skipped_kind"] = kind
    record["notes"] = list(meta.get("notes", []))
    return record


def write_json(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle, indent=1, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, path)


def read_record(out_dir, engine, version):
    path = os.path.join(out_dir, engine, version + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as handle:
            return json.load(handle)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Docker orchestration
# --------------------------------------------------------------------------

class LabError(Exception):
    pass


class Unavailable(LabError):
    """No image or package exists for the release."""


_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()
_BASES = set()  # plain Linux images pulled for package runs; removed at the end of the run
_IMAGES_BEFORE = set()  # images present when the run started (never removed)
_BASES_LOCK = threading.Lock()
_STOP = threading.Event()
# Every container of this run carries the token, so a second run (another
# lane reading the same release) never removes this run's containers.
RUN_TOKEN = secrets.token_hex(3)


def run(cmd, timeout=600, input_text=None, env=None, check=True):
    try:
        proc = subprocess.run(cmd, input=input_text.encode() if input_text is not None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise LabError("timed out after %ds: %s" % (timeout, " ".join(shlex.quote(c) for c in cmd[:6])))
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise LabError("%s exited %d: %s" % (" ".join(shlex.quote(c) for c in cmd[:5]), proc.returncode,
                                             (err or out).strip()[-600:]))
    return proc.returncode, out, err


def docker_images():
    _, out, _ = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=60)
    refs = set()
    for line in out.splitlines():
        line = line.strip()
        if line and not line.endswith(":<none>"):
            refs.add(line)
            if line.startswith(HUB):
                refs.add(line[len(HUB):])
            elif "/" not in line.split(":")[0]:
                refs.add(HUB + line)
    return refs


def pull(ref, platform=None, timeout=1800):
    cmd = ["docker", "pull", "-q"]
    if platform:
        cmd += ["--platform", platform]
    cmd.append(ref)
    code, out, err = run(cmd, timeout=timeout, check=False)
    return code == 0, (err or out).strip()


def image_info(ref):
    _, out, _ = run(["docker", "image", "inspect", ref, "--format",
                     "{{.Os}}/{{.Architecture}}\t{{range .RepoDigests}}{{.}} {{end}}"], timeout=60)
    platform, _, digests = out.strip().partition("\t")
    digest = None
    for item in digests.split():
        if "@" in item:
            digest = item.split("@", 1)[1]
            break
    return platform, digest


def remove_container(name):
    # -v: the images declare VOLUME /var/lib/mysql; without it every run would leave its datadir behind.
    run(["docker", "rm", "-f", "-v", name], timeout=120, check=False)
    with _ACTIVE_LOCK:
        _ACTIVE.discard(name)


def cleanup_all():
    with _ACTIVE_LOCK:
        names = list(_ACTIVE)
    for name in names:
        log("cleanup: removing %s" % name)
        remove_container(name)


SOCKET = "/tmp/myflames.sock"


def manual_commands(engine, bin_dir="", tarball=False):
    """Shell commands that initialize and start a server with --no-defaults inside the container.

    With --no-defaults no option file is read (not the image's, not a
    package's), so every variable keeps its compiled default. That matters on
    MariaDB too: its DEFAULT_VALUE column follows option files for some
    variables (the character set and collation ones take the image's
    character-set-server). Only the socket is set, on the command line.
    """
    if engine == "mysql":
        return {
            "init": "%smysqld --no-defaults --initialize-insecure --user=mysql > /tmp/myflames-init.log 2>&1" % bin_dir,
            "start": "%smysqld --no-defaults --user=mysql --socket=%s > /tmp/myflames-server.log 2>&1" % (bin_dir, SOCKET),
            "admin": "%smysql --no-defaults -uroot --socket=%s" % (bin_dir, SOCKET),
            "help": "%smysqld --no-defaults --user=mysql --verbose --help" % bin_dir,
            "client": "%smysql" % bin_dir,
        }
    if tarball:  # MariaDB bintar unpacked in /usr/local/mysql
        init = ("cd /usr/local/mysql && scripts/mariadb-install-db --no-defaults --user=mysql "
                "> /tmp/myflames-init.log 2>&1")
    else:  # the image's or a package's own layout: binaries in PATH, compiled datadir /var/lib/mysql
        init = ("mariadb-install-db --no-defaults --user=mysql --datadir=/var/lib/mysql "
                "> /tmp/myflames-init.log 2>&1")
    return {
        "init": init,
        "start": "%smariadbd --no-defaults --user=mysql --socket=%s > /tmp/myflames-server.log 2>&1" % (bin_dir, SOCKET),
        "admin": "%smariadb --no-defaults -uroot --socket=%s" % (bin_dir, SOCKET),
        "help": None,
        "client": "%smariadb" % bin_dir,
    }


class Server(object):
    """One throwaway server container and its client.

    The container runs `sleep`; the server is initialized and started with
    --no-defaults through docker exec (images and packages alike), so neither
    the image's entrypoint nor its option files touch any variable.
    """

    def __init__(self, engine, version, role, ref, platform, password, lab_dir, small=False, package=None):
        self.engine = engine
        self.version = version
        self.role = role
        self.ref = ref
        self.platform = platform
        self.password = password
        self.lab_dir = lab_dir
        self.small = small
        self.package = package
        self.name = "%s%s-%s-%s-%s" % (PREFIX, engine, version, role, RUN_TOKEN)
        self.emulated = bool(platform and platform.endswith("amd64"))
        self.lab_set = set()  # variables the lab changed for a while (their defaults come from --help)
        self.commands = package["commands"] if package else manual_commands(engine)
        self.client = self.commands["client"]
        self.defaults_option = "--defaults-file=" + CLIENT_CNF

    # -- lifecycle

    def resource_args(self):
        args = ["--hostname", "myflames-lab-%s" % self.role]
        if self.small:
            online = os.path.join(self.lab_dir, "online-1")
            with open(online, "w") as handle:
                handle.write("0\n")
            args += ["--cpuset-cpus=0", "--memory=1g", "-v", "%s:/sys/devices/system/cpu/online:ro" % online]
        return args

    def start(self):
        remove_container(self.name)
        cmd = ["docker", "run", "-d", "--rm", "--name", self.name]
        if self.platform:
            cmd += ["--platform", self.platform]
        cmd += self.resource_args()
        if self.package and self.package.get("dir"):
            cmd += ["-v", "%s:/pkg:ro" % self.package["dir"]]
        cmd += ["--entrypoint", "sleep", self.ref, "infinity"]
        with _ACTIVE_LOCK:
            _ACTIVE.add(self.name)
        run(cmd, timeout=300)
        self.start_manual()
        self.write_client_config()

    def stop(self):
        remove_container(self.name)

    def logs_tail(self, lines=30):
        _, out, _ = self.exec(["sh", "-c", "tail -%d /tmp/myflames-server.log" % lines], timeout=60, check=False)
        return out.strip()

    def running(self):
        code, out, _ = run(["docker", "inspect", "-f", "{{.State.Running}}", self.name], timeout=60, check=False)
        return code == 0 and out.strip() == "true"

    def start_manual(self):
        slow = 1500 if self.emulated else 600
        if self.package:
            self.exec(["sh", "-c", self.package["setup"]], timeout=1800)
        code, out, err = self.exec(["sh", "-c", self.commands["init"]], timeout=slow, check=False)
        if code != 0:
            _, text, _ = self.exec(["sh", "-c", "tail -30 /tmp/myflames-init.log"], timeout=60, check=False)
            raise LabError("initialize failed (exit %d): %s" % (code, text.strip()[-800:]))
        try:
            self.launch(slow)
        except LabError as error:
            folder = missing_secure_file_priv(str(error)) if self.engine == "mysql" else None
            if not folder:
                raise
            # The image lacks the compiled secure_file_priv folder (MySQL 8.0.11's
            # does) and the server refuses to start without it. Creating the
            # folder changes no variable; start once more.
            self.exec(["sh", "-c", "mkdir -p %s && chown mysql %s && chmod 750 %s" % (folder, folder, folder)],
                      timeout=60)
            self.launch(slow)
        # The account starts with an empty password (--initialize-insecure, or
        # unix_socket on MariaDB); give it the random one before anything else.
        self.exec(["sh", "-c", self.commands["admin"]], timeout=120,
                  input_text="ALTER USER 'root'@'localhost' IDENTIFIED BY '%s';\n" % self.password)

    def launch(self, slow):
        run(["docker", "exec", "-d", self.name, "sh", "-c", self.commands["start"]], timeout=120)
        deadline = time.time() + slow
        ping = self.commands["admin"] + " -e 'SELECT 1' >/dev/null 2>&1"
        alive = ("for p in /proc/[0-9]*; do cat $p/comm 2>/dev/null; done | grep -qE '^(mysqld|mariadbd)$'")
        while time.time() < deadline:
            if _STOP.is_set():
                raise LabError("stopped")
            code, _, _ = self.exec(["sh", "-c", ping], timeout=60, check=False)
            if code == 0:
                break
            code, _, _ = self.exec(["sh", "-c", alive], timeout=60, check=False)
            if code != 0:
                time.sleep(2)
                code, _, _ = self.exec(["sh", "-c", ping], timeout=60, check=False)
                if code == 0:
                    break
                _, errors, _ = self.exec(["sh", "-c", "grep -E 'ERROR|Error' /tmp/myflames-server.log | tail -6"],
                                         timeout=60, check=False)
                raise LabError("server exited during startup: " + (errors.strip() or self.logs_tail(20))[-900:])
            time.sleep(3)
        else:
            raise LabError("server not ready in time: " + self.logs_tail(40)[-800:])

    def exec(self, args, timeout=300, input_text=None, user=None, check=True):
        cmd = ["docker", "exec", "-i"]
        if user:
            cmd += ["-u", user]
        cmd += [self.name] + list(args)
        return run(cmd, timeout=timeout, input_text=input_text, check=check)

    def write_client_config(self):
        lines = ["[client]", "user=root", "password=%s" % self.password, "socket=%s" % SOCKET]
        self.exec(["sh", "-c", "umask 077; cat > %s" % CLIENT_CNF], input_text="\n".join(lines) + "\n", timeout=60)
        deadline = time.time() + 120
        last = ""
        while time.time() < deadline:
            code, out, err = self.exec([self.client, self.defaults_option, "-B", "-N", "-e", "SELECT 1"],
                                       timeout=60, check=False)
            if code == 0 and out.strip() == "1":
                return
            last = err.strip()
            time.sleep(2)
        raise LabError("client could not connect: " + last[-300:])

    def help_text(self):
        if not self.commands.get("help"):
            return 1, "", "no --help for this engine"
        return self.exec(["sh", "-c", self.commands["help"]], timeout=600, check=False)

    # -- SQL
    def query_xml(self, sql, timeout=300):
        _, out, _ = self.exec([self.client, self.defaults_option, "--xml", "-e", sql], timeout=timeout)
        return parse_xml_rows(out)

    def sql(self, sql, timeout=300, check=True):
        return self.exec([self.client, self.defaults_option, "-B", "-N", "-e", sql], timeout=timeout, check=check)

    def script(self, text, timeout=1200):
        return self.exec([self.client, self.defaults_option, "-B", "-N", "--force"], timeout=timeout,
                         input_text=text, check=False)


def load_plugins(server, plugins, notes):
    """Install optional plugins; return (loaded labels, variable name -> plugin label)."""
    loaded = []
    plugin_of = {}
    before = variable_names(server)
    for label, statements, fallback_label, fallback_statements in plugins:
        attempts = [(label, statements, False)]
        if fallback_label:
            attempts.append((fallback_label, fallback_statements, True))
        errors = []
        for attempt_label, attempt_statements, is_fallback in attempts:
            failed = None
            touched = PLUGIN_SETS.get(attempt_label, ()) if is_fallback else ()
            saved = read_globals(server, touched)
            for statement in attempt_statements:
                code, out, err = server.sql(statement, timeout=180, check=False)
                if code != 0:
                    failed = err.strip().splitlines()[-1] if err.strip() else "exit %d" % code
                    break
            if touched:
                server.lab_set.update(touched)
                restore_globals(server, saved, notes)
                if failed is None:
                    notes.append("%s installs here only with %s changed for the install (server_id other than 0, "
                                 "TABLE repositories); the lab set them, put them back, and takes their defaults "
                                 "from --help." % (attempt_label, ", ".join(touched)))
            if failed is None:
                after = variable_names(server)
                for name in sorted(after - before):
                    plugin_of[name] = attempt_label
                before = after
                loaded.append(attempt_label)
                break
            errors.append("%s: %s" % (attempt_label, failed))
        else:
            notes.append("Plugin not loaded (" + "; ".join(errors) + ")")
    return loaded, plugin_of


def read_globals(server, names):
    """Current global values of these variables (names checked against NAME_RE)."""
    values = {}
    for name in names:
        if NAME_RE.match(name):
            rows = server.query_xml("SELECT @@GLOBAL.%s AS v" % name)
            values[name] = rows[0].get("v") if rows else None
    return values


def restore_globals(server, values, notes):
    for name, value in sorted(values.items()):
        if value is None:
            continue
        literal = value if INT_TEXT_RE.match(value) else "'%s'" % value.replace("\\", "\\\\").replace("'", "''")
        code, _, err = server.sql("SET GLOBAL %s = %s" % (name, literal), timeout=120, check=False)
        if code != 0:
            notes.append("Could not put %s back to %s: %s" % (name, value, err.strip()[-200:]))


def variable_names(server):
    if server.engine == "mysql":
        global_sql, session_sql, _ = mysql_variable_sql(server)
        rows = server.query_xml(global_sql) + server.query_xml(session_sql)
    else:
        rows = server.query_xml("SELECT VARIABLE_NAME FROM information_schema.SYSTEM_VARIABLES")
    return set(r["VARIABLE_NAME"].lower() for r in rows)


THREAD_SQL = ("SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.variables_by_thread WHERE THREAD_ID = "
              "(SELECT THREAD_ID FROM performance_schema.threads WHERE PROCESSLIST_ID = CONNECTION_ID())")
PS_GLOBAL_SQL = "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_variables"
IS_GLOBAL_SQL = "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.GLOBAL_VARIABLES"
IS_SESSION_SQL = "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.SESSION_VARIABLES"


def mysql_variable_sql(server):
    """(global SQL, session SQL, source name): performance_schema when it answers, else information_schema.

    MySQL 5.7 has performance_schema.global_variables and variables_by_thread
    (5.7.6 and later, with show_compatibility_56 OFF, the default from 5.7.8);
    information_schema.GLOBAL_VARIABLES and SESSION_VARIABLES are the fallback.
    """
    cached = getattr(server, "variable_sql", None)
    if cached:
        return cached
    code, _, _ = server.sql("SELECT COUNT(*) FROM performance_schema.global_variables", timeout=120, check=False)
    if code == 0:
        code, _, _ = server.sql(THREAD_SQL + " LIMIT 1", timeout=120, check=False)
    if code == 0:
        server.variable_sql = (PS_GLOBAL_SQL, THREAD_SQL, "performance_schema")
    else:
        server.variable_sql = (IS_GLOBAL_SQL, IS_SESSION_SQL, "information_schema")
    return server.variable_sql


def performance_schema_tables(server):
    rows = server.query_xml("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = "
                            "'performance_schema' AND TABLE_NAME IN ('variables_info', 'variables_metadata')")
    return set((r.get("TABLE_NAME") or "").lower() for r in rows)


def read_mysql(server, notes):
    """performance_schema (or information_schema) reads and --help for one MySQL server."""
    global_sql, session_sql, source = mysql_variable_sql(server)
    if source != "performance_schema":
        notes.append("Names, values and scopes were read from information_schema.GLOBAL_VARIABLES and "
                     "SESSION_VARIABLES (performance_schema.global_variables did not answer).")
    global_rows = server.query_xml(global_sql)
    thread_rows = server.query_xml(session_sql)
    tables = performance_schema_tables(server)
    info_rows = server.query_xml("SELECT * FROM performance_schema.variables_info") if "variables_info" in tables else None
    meta_rows = None
    if "variables_metadata" in tables:
        meta_rows = server.query_xml("SELECT VARIABLE_NAME, VARIABLE_SCOPE, DATA_TYPE, MIN_VALUE, MAX_VALUE "
                                     "FROM performance_schema.variables_metadata")
    # performance_schema keeps at most 1024 characters of a value; read long ones whole.
    for rows, scope in ((global_rows, "GLOBAL"), (thread_rows, "SESSION")):
        for row in rows:
            value = row.get("VARIABLE_VALUE")
            name = row["VARIABLE_NAME"]
            if value is not None and len(value) >= 1000 and NAME_RE.match(name):
                full = server.query_xml("SELECT @@%s.%s AS v" % (scope, name))
                if full and full[0].get("v") is not None and len(full[0]["v"]) > len(value):
                    row["VARIABLE_VALUE"] = full[0]["v"]
                    notes.append("%s read whole through @@%s (performance_schema truncates it)." % (name, scope))
    help_values = None
    code, out, err = server.help_text()
    if "Value (after reading options)" in out:
        help_values = parse_help(out)
    else:
        notes.append("mysqld --help gave no option table (exit %d): %s" % (code, err.strip()[-300:]))
    return global_rows, thread_rows, info_rows, meta_rows, help_values


def read_mariadb(server):
    return server.query_xml("SELECT VARIABLE_NAME, VARIABLE_SCOPE, VARIABLE_TYPE, DEFAULT_VALUE, NUMERIC_MIN_VALUE, "
                            "NUMERIC_MAX_VALUE, ENUM_VALUE_LIST, READ_ONLY, GLOBAL_VALUE, GLOBAL_VALUE_ORIGIN "
                            "FROM information_schema.SYSTEM_VARIABLES")


def session_retry_names(engine, variables, steps, results):
    """Both-scope MySQL variables whose SET GLOBAL said "is a SESSION variable" (sql_log_bin)."""
    names = []
    if engine != "mysql":
        return names
    for index, (kind, name) in enumerate(steps):
        if kind != "set" or variables[name]["scope"] != "both":
            continue
        for level, code, message in results.get(index, []):
            if level == "Error" and code in (1228, 1238) and "SESSION variable" in message:
                names.append(name)
                break
    return names


def session_probe_plan(names):
    lines = ["SET SESSION lock_wait_timeout = 5;"]
    for index, name in enumerate(names):
        lines.append(CLEAR_WARNINGS["mysql"])
        lines.append("SELECT '%s %d';" % (PROBE_MARK, index))
        lines.append("SET SESSION %s = @@SESSION.%s;" % (name, name))
        lines.append("SHOW WARNINGS;")
    return "\n".join(lines) + "\n"


def apply_session_probe(variables, names, results, notes):
    """SET GLOBAL is refused but SET SESSION works: the variable can be changed while the server runs."""
    for index, name in enumerate(names):
        if index not in results:
            continue
        error = next(((code, message) for level, code, message in results[index] if level == "Error"), None)
        if error is None:
            variables[name]["dynamic"], variables[name]["dynamic_how"] = True, "set_probe"
            notes.append("%s: SET GLOBAL is refused (a SESSION variable); SET SESSION works, so dynamic is true."
                         % name)
        elif error[0] == 1238 and "read only" in error[1]:
            variables[name]["dynamic"], variables[name]["dynamic_how"] = False, "set_probe"


def run_probe(server, variables, notes, set_probe=True):
    sql_text, steps = probe_plan(server.engine, variables, set_probe=set_probe)
    started = time.time()
    code, out, err = server.script(sql_text, timeout=1800 if server.emulated else 900)
    results = parse_probe_output(out)
    apply_probe(server.engine, variables, steps, results, notes)
    retry = session_retry_names(server.engine, variables, steps, results)
    if retry:
        code, out, err = server.script(session_probe_plan(retry), timeout=600)
        apply_session_probe(variables, retry, parse_probe_output(out), notes)
    if not server.running():
        notes.append("The server stopped during the probe: " + server.logs_tail(10))
    return time.time() - started


def read_server(server, plugins, probe, notes):
    """Start, load plugins, read; returns (variables, plugins_loaded, version, comment, host_values)."""
    server.start()
    version_rows = server.query_xml("SELECT @@version AS v, @@version_comment AS c")
    server_version = version_rows[0].get("v") if version_rows else None
    comment = version_rows[0].get("c") if version_rows else None
    loaded, plugin_of = load_plugins(server, plugins, notes)
    if server.engine == "mysql":
        parts = read_mysql(server, notes)
        variables, more = mysql_variables(*parts, plugin_of=plugin_of, lab_set=server.lab_set)
        if mysql_variable_sql(server)[2] != "performance_schema":
            unclear_scopes(variables, more)
        host_values = mysql_host_values(variables)
    else:
        rows = read_mariadb(server)
        variables, more = mariadb_variables(rows, plugin_of=plugin_of)
        host_values = mariadb_host_values(rows)
    notes.extend(more)
    problems = origin_problems(server.engine, variables)
    if problems:
        raise LabError("values not at their compiled defaults (an option file or a SET reached the server): "
                       + ", ".join(problems[:20]))
    if probe:
        seconds = run_probe(server, variables, notes)
        log("%s %s: probe took %.0fs" % (server.engine, server.version, seconds))
    return variables, loaded, server_version, comment, host_values


# --------------------------------------------------------------------------
# Images and packages
# --------------------------------------------------------------------------

def mysql_amd64_only(version):
    return version_key(version) < (8, 0, 29)


SKOPEO = "quay.io/skopeo/stable:v1.16.1"
LOCAL_PREFIX = "myflames-catalog-local/"


def skopeo_reason(message):
    """Why Docker cannot pull this old Hub image itself, or None.

    Docker (containerd 2.1 and later) refuses images that only have a schema 1
    manifest (MySQL 5.7.9 to 5.7.12), and some old schema 2 images (5.7.13 to
    5.7.15) fail to unpack ("failed to get reader from content store"). Both
    load fine from a docker-archive that skopeo writes.
    """
    message = message or ""
    if "manifest.v1+prettyjws" in message or "no longer supported" in message:
        return "only a schema 1 manifest, which this Docker no longer pulls"
    if "failed to unpack image" in message or "failed to extract layer" in message:
        return "a layer this Docker could not unpack (%s)" % message.strip().splitlines()[-1][:160]
    return None


def is_schema1_refusal(message):
    return skopeo_reason(message) is not None and "schema 1" in skopeo_reason(message)


def convert_schema1(ref, engine, version, platform, lab_dir):
    """Copy a schema 1 image with skopeo into a local image Docker can run.

    Old Docker Hub tags (MySQL 5.7.9 to 5.7.12) only have schema 1 manifests.
    skopeo, run as a throwaway container, writes a docker-archive that
    `docker load` accepts. Returns (local reference, source digest or None).
    """
    ok, message = pull(SKOPEO)
    if not ok:
        raise LabError("could not pull %s: %s" % (SKOPEO, message[-200:]))
    with _BASES_LOCK:
        _BASES.add(SKOPEO)
    local = "%s%s:%s" % (LOCAL_PREFIX, engine, version)
    archive = "%s-%s.tar" % (engine, version)
    path = os.path.join(lab_dir, archive)
    arch = (platform or "linux/amd64").split("/")[-1]
    name = "%sskopeo-%s-%s-%s" % (PREFIX, engine, version, RUN_TOKEN)
    with _ACTIVE_LOCK:
        _ACTIVE.add(name)
    try:
        run(["docker", "run", "--rm", "--name", name, "-v", "%s:/out" % lab_dir, SKOPEO, "copy",
             "--override-os", "linux", "--override-arch", arch, "docker://" + ref,
             "docker-archive:/out/%s:%s" % (archive, local)], timeout=1800)
        run(["docker", "load", "-i", path], timeout=900)
        code, out, _ = run(["docker", "run", "--rm", "--name", name, SKOPEO, "inspect", "--override-os", "linux",
                            "--override-arch", arch, "--format", "{{.Digest}}", "docker://" + ref],
                           timeout=300, check=False)
        digest = out.strip() if code == 0 and out.strip().startswith("sha256:") else None
    finally:
        remove_container(name)
        if os.path.exists(path):
            os.remove(path)
    return local, digest


def is_transient_pull_error(message):
    """A registry hiccup worth one more try (not a missing tag, not a rate limit)."""
    message = (message or "").lower()
    if "toomanyrequests" in message or "rate limit" in message:
        return False
    return any(word in message for word in ("unexpected status", "i/o timeout", "connection reset", "tls handshake",
                                            "eof", "502 bad gateway", "503 service"))


def remove_partial_image(ref):
    """A pull that failed while unpacking can leave the tag behind; this run made it, so remove it."""
    code, _, _ = run(["docker", "image", "inspect", ref], timeout=60, check=False)
    if code == 0 and ref not in _IMAGES_BEFORE:
        run(["docker", "rmi", ref], timeout=300, check=False)


def resolve_image(engine, version, hub_tags, notes, forced_platform=None, lab_dir=None):
    """Pull the release's image; returns (ref to run, platform, source ref, source digest or None).

    Raises Unavailable when no registry has the tag.
    """
    platform = "linux/amd64" if engine == "mysql" and mysql_amd64_only(version) else None
    if forced_platform:
        platform = forced_platform
    tried = []
    for registry in (ECR, HUB):
        if registry == HUB and hub_tags is not None and version not in hub_tags.get(engine, set()):
            tried.append("%s%s:%s (not in the cached Docker Hub tag list)" % (registry, engine, version))
            continue
        ref = "%s%s:%s" % (registry, engine, version)
        ok, message = pull(ref, platform)
        if not ok and is_transient_pull_error(message):
            time.sleep(10)
            ok, message = pull(ref, platform)
        if not ok and platform is None and "no matching manifest" in message:
            ok, message = pull(ref, "linux/amd64")
            if ok:
                platform = "linux/amd64"
        if ok:
            return ref, platform, ref, None
        reason = skopeo_reason(message) if registry == HUB and lab_dir else None
        if reason:
            remove_partial_image(ref)
            local, digest = convert_schema1(ref, engine, version, platform, lab_dir)
            notes.append("Docker Hub's %s has %s; skopeo (%s) copied it into the local image %s, which was run "
                         "and removed." % (ref, reason, SKOPEO, local))
            return local, platform, ref, digest
        tried.append("%s (%s)" % (ref, message.splitlines()[-1][:160] if message else "failed"))
        if registry == ECR and "not found" not in message.lower() and "manifest unknown" not in message.lower():
            notes.append("ECR pull failed for another reason: " + message[:200])
    raise Unavailable("No image: " + "; ".join(tried))


def url_exists(url):
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def download(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=120) as response, open(tmp, "wb") as handle:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
    os.replace(tmp, path)
    return path


def package_candidates(engine, version):
    """(url, file name, platform) for the official binary package."""
    if engine == "mysql":
        series = ".".join(version.split(".")[:2])
        out = []
        for arch, platform in (("aarch64", "linux/arm64"), ("x86_64", "linux/amd64")):
            name = "mysql-%s-linux-glibc2.28-%s.tar.xz" % (version, arch)
            for base in ("https://cdn.mysql.com/archives/mysql-%s/" % series,
                         "https://cdn.mysql.com/Downloads/MySQL-%s/" % series,
                         "https://downloads.mysql.com/archives/get/p/23/file/"):
                out.append((base + name, name, platform))
        return out
    name = "mariadb-%s-linux-systemd-x86_64.tar.gz" % version
    return [("https://archive.mariadb.org/mariadb-%s/bintar-linux-systemd-x86_64/%s" % (version, name), name,
             "linux/amd64")]


UBUNTU = {"2004": "focal", "2204": "jammy", "2404": "noble", "2504": "plucky", "2510": "questing"}
CODENAME_TAG = dict((name, number[:2] + "." + number[2:]) for number, name in UBUNTU.items())
MARIADB_ARCHIVE = "https://archive.mariadb.org/mariadb-%s/repo/ubuntu"
MARIADB_KEY = "https://mariadb.org/mariadb_release_signing_key.pgp"


def ubuntu_codename(server_version):
    """'11.4.5-MariaDB-ubu2404' -> 'noble' (the Ubuntu release the official image was built on)."""
    match = re.search(r"ubu(\d{4})", server_version or "")
    return UBUNTU.get(match.group(1)) if match else None


def neighbour_codename(out_dir, version):
    """The Ubuntu release of the nearest read image of the same MariaDB series (lower first)."""
    series = version_key(version)[:2]
    folder = os.path.join(out_dir, "mariadb")
    found = []
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if not name.endswith(".json"):
                continue
            other = name[:-5]
            try:
                key = version_key(other)
            except ValueError:
                continue
            if key[:2] != series or other == version:
                continue
            record = read_record(out_dir, "mariadb", other) or {}
            codename = ubuntu_codename(record.get("server_version")) if record.get("method") == "image" else None
            if codename:
                below = key < version_key(version)
                found.append((0 if below else 1, abs(key[2] - version_key(version)[2]), codename))
    return sorted(found)[0][2] if found else None


def mariadb_repo_spec(version, out_dir):
    """MariaDB's own Ubuntu packages of this release, from its archive, on the platform the images use."""
    codenames = []
    near = neighbour_codename(out_dir, version)
    if near:
        codenames.append(near)
    codenames += [c for c in ("noble", "jammy") if c not in codenames]
    for codename in codenames:
        repo = MARIADB_ARCHIVE % version
        if not url_exists("%s/dists/%s/Release" % (repo, codename)):
            continue
        base = "public.ecr.aws/docker/library/ubuntu:%s" % CODENAME_TAG[codename]
        setup = ("set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null; "
                 "apt-get install -y -qq --no-install-recommends ca-certificates curl >/dev/null; "
                 "mkdir -p /etc/apt/keyrings; curl -fsSL %s -o /etc/apt/keyrings/mariadb-keyring.pgp; "
                 "echo 'deb [signed-by=/etc/apt/keyrings/mariadb-keyring.pgp] %s %s main' "
                 "> /etc/apt/sources.list.d/mariadb.list; "
                 "printf 'Package: *\\nPin: origin archive.mariadb.org\\nPin-Priority: 1001\\n' "
                 "> /etc/apt/preferences.d/mariadb; "
                 "printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d; chmod +x /usr/sbin/policy-rc.d; "
                 "apt-get update -qq >/dev/null; apt-get install -y -qq mariadb-server >/dev/null 2>&1; "
                 "rm -rf /var/lib/mysql; mkdir -p /var/lib/mysql; chown mysql:mysql /var/lib/mysql"
                 % (MARIADB_KEY, repo, codename))
        commands = manual_commands("mariadb")
        return {"url": "%s %s main (mariadb-server)" % (repo, codename), "platform": None, "base": base,
                "setup": setup, "commands": commands, "dir": None}
    return None


def package_spec(engine, version, cache_dir, out_dir=None):
    """Describe how to run the release's official package in a plain container.

    MariaDB: its own Ubuntu packages from archive.mariadb.org (native platform,
    the build the official images install), else the x86_64 bintar under
    emulation. MySQL: the generic Linux tarball (aarch64 first).
    """
    if engine == "mariadb":
        spec = mariadb_repo_spec(version, out_dir or os.path.dirname(cache_dir))
        if spec:
            return spec
    found = None
    for url, name, platform in package_candidates(engine, version):
        if url_exists(url):
            found = (url, name, platform)
            break
    if not found:
        checked = sorted(set(u.rsplit("/", 1)[0] + "/" for u, _, _ in package_candidates(engine, version)))
        if engine == "mariadb":
            checked.insert(0, (MARIADB_ARCHIVE % version) + "/dists/")
        raise Unavailable("No package: none at " + ", ".join(checked) + " (checked %s)"
                          % datetime.date.today().isoformat())
    url, name, platform = found
    os.makedirs(cache_dir, exist_ok=True)
    download(url, os.path.join(cache_dir, name))
    folder = re.sub(r"\.tar\.(xz|gz)$", "", name)
    # Runtime libraries the packages link against (names differ across Ubuntu releases; missing ones are skipped).
    libs = "libaio1t64 libaio1 libnuma1 libncurses6 libtinfo6 liburing2 libssl3t64 libssl3 libsystemd0 xz-utils"
    setup = ("set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null; "
             "for p in %s; do apt-get install -y -qq --no-install-recommends $p >/dev/null 2>&1 || true; done; "
             "for d in /usr/lib/x86_64-linux-gnu /usr/lib/aarch64-linux-gnu; do "
             "if [ -e $d/libaio.so.1t64 ] && [ ! -e $d/libaio.so.1 ]; then ln -s libaio.so.1t64 $d/libaio.so.1; fi; "
             "done; id mysql >/dev/null 2>&1 || useradd -r -M -s /bin/false mysql; "
             "cd /usr/local; tar xf /pkg/%s; ln -sfn /usr/local/%s /usr/local/mysql; "
             "mkdir -p /usr/local/mysql/data; chown -R mysql /usr/local/%s"
             % (libs, name, folder, folder))
    return {"url": url, "platform": platform, "base": "public.ecr.aws/docker/library/ubuntu:24.04",
            "setup": setup, "commands": manual_commands(engine, "/usr/local/mysql/bin/", tarball=True), "dir": cache_dir}


# --------------------------------------------------------------------------
# One release
# --------------------------------------------------------------------------

def read_release(entry, engine, args, images_before, hub_tags):
    version = entry["version"]
    if _STOP.is_set():
        # Stopped (Ctrl+C or SIGTERM) before this queued release began: pull
        # nothing, start nothing, and keep whatever record it already has.
        return "stopped"
    started = time.time()
    notes = []
    meta = {"tag": entry["tag"], "maturity": entry["maturity"]}
    password = secrets.token_hex(16)
    pulled = None
    package = None
    lab_dir = os.path.join(args.out, ".lab")
    os.makedirs(lab_dir, exist_ok=True)
    try:
        try:
            ref, platform, source_ref, source_digest = resolve_image(engine, version, hub_tags, notes,
                                                                     args.platform, lab_dir)
            pulled = ref
            meta["method"] = "image"
            meta["image"] = source_ref
            meta["platform"], meta["image_digest"] = image_info(ref)
            if source_ref != ref:
                meta["image_digest"] = source_digest
        except Unavailable as missing:
            try:
                package = package_spec(engine, version, os.path.join(args.out, ".cache"), args.out)
            except Unavailable as no_package:
                raise Unavailable("%s. %s" % (missing, no_package))
            notes.append(str(missing))
            if args.platform:
                package["platform"] = args.platform
            ok, message = pull(package["base"], package["platform"])
            if not ok:
                raise LabError("could not pull %s: %s" % (package["base"], message))
            with _BASES_LOCK:
                _BASES.add(package["base"])
            ref, platform = package["base"], package["platform"]
            meta["method"] = "package"
            meta["package"] = package["url"]
            meta["image"] = package["base"]
            meta["platform"], _ = image_info(package["base"])
            if platform:
                meta["platform"] = platform
        plugins = MYSQL_PLUGINS if engine == "mysql" else MARIADB_PLUGINS
        log("%s %s: start %s (%s)" % (engine, version, ref, meta.get("platform")))
        main = Server(engine, version, "main", ref, platform, password, lab_dir, package=package)
        try:
            variables, loaded, server_version, comment, main_values = read_server(
                main, plugins, probe=not args.no_probe, notes=notes)
        finally:
            main.stop()
        meta["server_version"] = server_version
        meta["version_comment"] = comment
        meta["read_at"] = utc_now()
        if server_version and not server_version.startswith(version):
            if package:
                raise LabError("the package installed %s, not %s" % (server_version, version))
            notes.append("The server reports version %s for release %s." % (server_version, version))
        if not args.no_host_check:
            small_notes = []
            small = Server(engine, version, "small", ref, platform, secrets.token_hex(16), lab_dir, small=True,
                           package=package)
            try:
                small_vars, _, _, _, small_values = read_server(small, plugins, probe=False, notes=small_notes)
            finally:
                small.stop()
            changed = mark_host(variables, main_values, small_values)
            meta["host_check"] = {"main": "all CPUs and memory of the lab VM, host name myflames-lab-main",
                                  "small": "1 CPU (cpuset 0 and online CPU list 0), 1 GiB, host name myflames-lab-small",
                                  "compared": len(set(small_vars) & set(variables)),
                                  "differ": changed}
        meta["elapsed_seconds"] = int(time.time() - started)
        record = build_record(engine, version, meta, variables, loaded, notes)
        write_json(os.path.join(args.out, engine, version + ".json"), record)
        log("%s %s: %d variables (%d plugin), %d host defaults, %ds" % (
            engine, version, record["counts"]["variables"], record["counts"]["plugin_variables"],
            record["counts"]["host_defaults"], meta["elapsed_seconds"]))
        return "read"
    except Unavailable as missing:
        meta["elapsed_seconds"] = int(time.time() - started)
        meta["notes"] = notes
        write_json(os.path.join(args.out, engine, version + ".json"),
                   skipped_record(engine, version, meta, str(missing), "unavailable"))
        log("%s %s: skipped: %s" % (engine, version, missing))
        return "unavailable"
    except Exception as error:  # a lab failure must not stop the other releases
        if _STOP.is_set():
            log("%s %s: stopped" % (engine, version))
            return "stopped"
        meta["elapsed_seconds"] = int(time.time() - started)
        meta["notes"] = notes
        write_json(os.path.join(args.out, engine, version + ".json"),
                   skipped_record(engine, version, meta, "Could not be read: %s" % str(error)[:800], "failed"))
        log("%s %s: FAILED: %s" % (engine, version, str(error)[:300]))
        return "failed"
    finally:
        if pulled and not args.keep_images and pulled not in images_before:
            run(["docker", "rmi", pulled], timeout=300, check=False)


# --------------------------------------------------------------------------
# Summary files
# --------------------------------------------------------------------------

def write_versions(out_dir, engine, in_scope, excluded, source):
    rows = []
    for entry in in_scope:
        record = read_record(out_dir, engine, entry["version"]) or {}
        row = dict(entry)
        if record.get("variables"):
            row["status"] = "read"
        elif record.get("skipped"):
            row["status"] = "skipped"
            row["reason"] = record["skipped"]
        else:
            row["status"] = "not read yet"
        for key in ("method", "image", "image_digest", "package", "platform", "server_version"):
            if record.get(key):
                row[key] = record[key]
        rows.append(row)
    write_json(os.path.join(out_dir, "versions-%s.json" % engine),
               {"schema": VERSIONS_SCHEMA, "engine": engine, "computed_at": utc_now(), "source": source,
                "rule": ("plain mysql-X.Y.Z tags (a -respin tag stands for its version): 5.7 GA releases, and from "
                         "8.0.11 with maturity GA, LTS or INNOVATION" if engine == "mysql" else
                         "plain mariadb-X.Y.Z tags from 10.6 with SERVER_MATURITY=stable"),
                "versions": rows, "excluded": excluded})
    return rows


def write_report(out_dir, scopes, run_seconds=None):
    lines = ["# Parameter catalog: lab run report", "",
             "Written by scripts/record-sysvar-catalog.py on %s. Every release below was started in a "
             "throwaway container and read from the running server; nothing was taken from documentation."
             % utc_now(), ""]
    if run_seconds is not None:
        lines += ["Last run: %.0f minutes." % (run_seconds / 60.0), ""]
    for engine in ENGINES:
        if engine not in scopes:
            continue
        in_scope = scopes[engine]
        read, skipped, pending, total_seconds = [], [], [], 0
        for entry in in_scope:
            record = read_record(out_dir, engine, entry["version"]) or {}
            total_seconds += record.get("elapsed_seconds") or 0
            if record.get("variables"):
                read.append((entry, record))
            elif record.get("skipped"):
                skipped.append((entry, record))
            else:
                pending.append(entry)
        lines.append("## %s" % ("MySQL" if engine == "mysql" else "MariaDB"))
        lines.append("")
        lines.append("- In scope: %d releases. Read from a running server: %d (image %d, package %d). "
                     "Skipped: %d. Not read yet: %d." % (
                         len(in_scope), len(read), sum(1 for _, r in read if r.get("method") == "image"),
                         sum(1 for _, r in read if r.get("method") == "package"), len(skipped), len(pending)))
        lines.append("- Lab time spent on these records: %.0f minutes (sum over releases; releases ran up to %d "
                     "at a time)." % (total_seconds / 60.0, MAX_JOBS))
        if read:
            counts = [r["counts"]["variables"] for _, r in read]
            lines.append("- Variables per release: %d to %d. Releases on amd64 emulation: %d." % (
                min(counts), max(counts), sum(1 for _, r in read if (r.get("platform") or "").endswith("amd64"))))
            lines.append("")
            lines.append("| Release | Method | Platform | Variables | Plugin vars | Dynamic known | Deprecation warnings "
                         "| Host defaults | Plugins | Seconds |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for entry, record in read:
                counts_ = record["counts"]
                lines.append("| %s | %s | %s | %d | %d | %d | %d | %d | %s | %s |" % (
                    entry["version"], record.get("method"), record.get("platform"), counts_["variables"],
                    counts_["plugin_variables"], counts_["dynamic_known"], counts_["deprecated_warning"],
                    counts_["host_defaults"], ", ".join(record.get("plugins_loaded") or []) or "-",
                    record.get("elapsed_seconds")))
        if skipped:
            lines.append("")
            lines.append("Skipped:")
            lines.append("")
            for entry, record in skipped:
                lines.append("- %s (%s): %s" % (entry["version"], record.get("skipped_kind"), record["skipped"]))
        if pending:
            lines.append("")
            lines.append("Not read yet: " + ", ".join(e["version"] for e in pending))
        odd = aggregate_notes([(entry["version"], record.get("notes") or []) for entry, record in read])
        if odd:
            lines.append("")
            lines.append("Notes from the records (each with the releases it applies to):")
            lines.append("")
            lines.extend("- %s: %s" % (text, versions) for text, versions in odd)
        lines.append("")
    extra = os.path.join(out_dir, "NOTES.md")
    if os.path.exists(extra):
        with open(extra) as handle:
            lines += ["## Notes from the lab", "", handle.read().strip(), ""]
    with open(os.path.join(out_dir, "REPORT.md"), "w") as handle:
        handle.write("\n".join(lines))


def version_list(versions):
    """'8.0.11 to 8.0.28' for a run of consecutive releases, else a comma list."""
    if len(versions) > 3:
        return "%s to %s (%d releases)" % (versions[0], versions[-1], len(versions))
    return ", ".join(versions)


def aggregate_notes(per_release):
    """Group record notes by text; SET probe error lists are split into one line per variable."""
    groups = {}
    order = []
    for version, notes in per_release:
        for note in notes:
            if note.startswith("SET probe skipped"):
                continue
            items = [note]
            if note.startswith("SET probe errors"):
                head, _, body = note.partition("): ")
                items = ["%s): %s" % (head, item.strip()) for item in body.split("; ") if item.strip()]
            elif note.startswith("Plugin not loaded"):
                items = [re.sub(r"\(errno: .*", "", note)[:200]]
            for item in items:
                key = item[:400]
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                if version not in groups[key]:
                    groups[key].append(version)
    return [(key, version_list(sorted(groups[key], key=version_key))) for key in order]


def load_hub_tags(paths):
    if not paths:
        return None
    tags = {}
    for engine in ENGINES:
        tags[engine] = set()
    for path in paths:
        engine = "mariadb" if "mariadb" in os.path.basename(path) else "mysql"
        with open(path) as handle:
            for item in json.load(handle):
                tags[engine].add(item[0] if isinstance(item, list) else item)
    return tags


def main(argv=None):
    global _LOG_FILE
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", choices=ENGINES + ("all",), default="all")
    parser.add_argument("--versions", nargs="*", help="Only these versions (default: every release in scope).")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--mysql-source", default=os.path.join(GIT_ROOT, "mysql-server"))
    parser.add_argument("--mariadb-source", default=os.path.join(GIT_ROOT, "mariadb-server"))
    parser.add_argument("--hub-tags", nargs="*", help="Cached Docker Hub tag lists (hub-mysql.json, hub-mariadb.json); "
                        "a tag not listed is not pulled from Docker Hub.")
    parser.add_argument("--images-before", help="File with the image references present before the lab "
                        "(one per line, first word); these are never removed. Default: the images present now.")
    parser.add_argument("--jobs", type=int, default=MAX_JOBS)
    parser.add_argument("--list", action="store_true", help="Write versions-<engine>.json and print the scope.")
    parser.add_argument("--force", action="store_true", help="Read releases that already have a record.")
    parser.add_argument("--keep-images", action="store_true")
    parser.add_argument("--no-host-check", action="store_true")
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--platform", help="Run every image and package on this platform (for example linux/amd64, "
                        "so that every release of an engine is read from the same build platform).")
    parser.add_argument("--log")
    args = parser.parse_args(argv)
    args.jobs = max(1, min(MAX_JOBS, args.jobs))
    args.out = os.path.abspath(args.out)
    if not os.path.isdir(args.out):
        os.makedirs(args.out)
    if args.log:
        _LOG_FILE = open(args.log, "a")
    engines = ENGINES if args.engine == "all" else (args.engine,)
    scopes, excluded, sources = {}, {}, {}
    for engine in engines:
        source = args.mysql_source if engine == "mysql" else args.mariadb_source
        scopes[engine], excluded[engine] = scope_from_checkout(engine, source)
        sources[engine] = source
        write_versions(args.out, engine, scopes[engine], excluded[engine], source)
        log("%s: %d releases in scope (%s to %s), %d tags left out" % (
            engine, len(scopes[engine]), scopes[engine][0]["version"], scopes[engine][-1]["version"],
            len(excluded[engine])))
    if args.list:
        for engine in engines:
            for entry in scopes[engine]:
                print("%-8s %-8s %-11s %s" % (engine, entry["version"], entry["maturity"], entry["tag"]))
        write_report(args.out, scopes)
        return 0
    images_before = set()
    if args.images_before:
        with open(args.images_before) as handle:
            for line in handle:
                if line.split():
                    ref = line.split()[0]
                    images_before.add(ref)
                    images_before.add(HUB + ref if "/" not in ref.split(":")[0] else ref)
    # Images present now are never removed, whatever the file lists.
    images_before |= docker_images()
    _IMAGES_BEFORE.update(images_before)
    hub_tags = load_hub_tags(args.hub_tags)
    work = []
    wanted = set(args.versions or [])
    for engine in engines:
        for entry in scopes[engine]:
            if wanted and entry["version"] not in wanted:
                continue
            record = read_record(args.out, engine, entry["version"])
            if record and not args.force:
                if record.get("variables"):
                    continue
                if record.get("skipped_kind") == "unavailable":
                    continue
            work.append((engine, entry))
    unknown = wanted - set(e["version"] for eng in engines for e in scopes[eng])
    if unknown:
        log("not in scope, ignored: " + ", ".join(sorted(unknown)))
    log("reading %d releases, %d at a time" % (len(work), args.jobs))

    def handle_signal(signum, frame):
        _STOP.set()
        cleanup_all()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    started = time.time()
    outcome = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = dict((pool.submit(read_release, entry, engine, args, images_before, hub_tags),
                            (engine, entry["version"])) for engine, entry in work)
            for future in concurrent.futures.as_completed(futures):
                try:
                    outcome[futures[future]] = future.result()
                except Exception as error:  # keep the other releases going
                    outcome[futures[future]] = "failed"
                    log("%s %s: FAILED outside the release: %s" % (futures[future] + (error,)))
                for engine in engines:
                    write_versions(args.out, engine, scopes[engine], excluded[engine], sources[engine])
                write_report(args.out, scopes, time.time() - started)
    finally:
        cleanup_all()
        for base in sorted(_BASES):
            if base not in images_before and not args.keep_images:
                run(["docker", "rmi", base], timeout=300, check=False)
    write_report(args.out, scopes, time.time() - started)
    summary = {}
    for value in outcome.values():
        summary[value] = summary.get(value, 0) + 1
    log("done in %.0f min: %s" % ((time.time() - started) / 60.0,
                                  ", ".join("%s %d" % (k, v) for k, v in sorted(summary.items())) or "nothing to do"))
    return 1 if summary.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
