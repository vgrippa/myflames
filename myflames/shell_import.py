"""Import MySQL Shell slow-query diagnostics bundles as capture-1.0 evidence.

MySQL Shell's ``util.debug.collectSlowQueryDiagnostics(path, query, options)``
writes a ZIP archive with the query's EXPLAIN output, the optimizer trace,
per-table metadata, server configuration and a Performance Schema sampling of
one execution. :func:`load_shell_diagnostics` turns that archive into the same
``capture-1.0`` dict that :class:`myflames.live.LiveJob` produces, so the UI
and CLI can reuse their existing plan / capture paths.

Bundle layout (MySQL Shell ``python/plugins/debug``)
----------------------------------------------------

Every member lives under one directory, ``<zip basename without .zip>/``
(``collect_diagnostics.py:369-383`` ``process_path``). The default archive name
is ``mysql-diagnostics-YYYYmmdd-HHMMSS.zip`` (``collect_diagnostics.py:294``).
Archives are written with ``zipfile`` defaults, so members are ZIP_STORED
(``sql_collector.py:36`` ``make_zipinfo``).

Members this module reads (``P`` = the directory prefix):

``P/shell_info.yaml``
    YAML; ``command`` names the Shell utility (``collectSlowQueryDiagnostics``,
    ``collectHighLoadDiagnostics`` or ``collectDiagnostics``) plus Shell
    version/options (``collect_diagnostics.py:181-197``). Present in every
    Shell diagnostics bundle, so it is the sniffing anchor. Read with a regex.
``P/instance``
    Plain text ``Key: value`` lines, including ``Version: <version_comment>
    <version>`` and ``UTC Time: YYYY-MM-DD HH:MM:SS`` (``collect_diagnostics.py:
    94-145``, written via ``collect_member_info`` from
    ``collect_common_high_load_data``, ``collect_diagnostics.py:503-527``).
``P/explain.tsv``
    Traditional ``EXPLAIN <query>`` as TSV, followed by ``# Warnings`` rows
    (``sql_collector.py:1523-1537``). Only used as a fallback source of the
    query text.
``P/explain_json.tsv``
    ``EXPLAIN format=json <query>`` (``sql_collector.py:1547``, written by
    ``collect_queries`` -> ``dump_query`` with ``as_yaml=False``,
    ``sql_collector.py:295-327``). The JSON document is the single value of
    the ``EXPLAIN`` column; it is pretty-printed, so it spans several lines.
    Its format follows the session's ``explain_json_format_version`` (8.3+):
    1 = ``query_block`` tree (default through 9.4, and the only format before
    8.3), 2 = iterator tree (default since 9.5, WL#17039; mysql-server
    ``sql/sys_vars.cc:7611-7617``).
``P/explain_analyze.tsv``
    ``EXPLAIN ANALYZE <query>`` (no FORMAT clause), only when the server
    version is >= 8.0.18 (``sql_collector.py:1548-1549``). Without an
    explicit FORMAT, EXPLAIN ANALYZE uses the session ``explain_format``
    and turns TRADITIONAL into TREE (mysql-server
    ``sql/parse_tree_nodes.cc`` ``PT_explain::make_cmd``), so this is TREE
    text unless the session had ``explain_format=JSON`` (JSON ANALYZE needs
    8.3+ and ``explain_json_format_version=2``, ``sql/opt_explain.cc``
    ``explain_query``). We sniff the content.
``P/explain-optimizer_trace.tsv`` (and ``.yaml``)
    ``SELECT * FROM information_schema.optimizer_trace`` captured right after
    the traditional EXPLAIN with ``optimizer_trace_limit=1``
    (``sql_collector.py:1526-1542``). Columns: QUERY, TRACE,
    MISSING_BYTES_BEYOND_MAX_MEM_SIZE, INSUFFICIENT_PRIVILEGES. The traced
    statement is ``EXPLAIN <query>``.
``P/referenced_table-<schema>.<table>.yaml``
    YAML mapping per table found by Shell's SQL parser
    (``collect_diagnostics.py:610-641``, ``sql_collector.py:1450-1520``):
    ``Table Name``, ``Table Schema``, ``Table Status`` (the
    ``information_schema.tables`` row), ``DDL`` (SHOW CREATE TABLE/VIEW),
    ``Indexes`` (SHOW INDEX rows), ``Triggers``, ``InnoDB Table Stats`` and
    ``InnoDB Index Stats``. Names are passed through ``sanitize``
    (``sql_collector.py:42``). Note: Shell appends the innodb_index_stats rows
    to ``InnoDB Table Stats`` and leaves ``InnoDB Index Stats`` empty
    (``sql_collector.py:1504-1508``).
``P/global_variables.tsv`` (and ``.yaml``)
    ``performance_schema.global_variables`` joined with ``variables_info``:
    columns ``name``, ``value`` [, ``source``] (``sql_collector.py:991-996``,
    written by ``DiagnosticsSession.collect_configs_and_state``).
``P/query-info.yaml``
    YAML mapping written after the profiled run (``collect_diagnostics.py:
    644-665, 767-769``): ``Execution Time`` (Shell ``format_seconds`` text such
    as ``0.0123 sec`` or ``2 min 3.0000 sec``), ``Result Fetch Time`` (float
    seconds), ``Query``, ``Warnings``, ``Affected Rows``, ``Result Rows``.

TSV members come from ``dump_query`` (``sql_collector.py:165-256``): a
comment header (``# Query:``, ``#\\t<statement line>`` per line, ``#``,
``# Started: <local ISO time>``, ``# Execution Time: <t>``, ``#``), then a
``# <col>\\t<col>`` header line, then rows whose fields are ``str(value)``
joined by tabs **without escaping** (multi-line values span lines), then an
optional ``# Warnings`` block. A failed statement produces ``<name>.error``
instead (``sql_collector.py:178-183``).

YAML members are written with PyYAML ``yaml.dump`` (block style, sorted keys)
and a representer override that uses literal ``|`` blocks for multi-line
strings (``collect_diagnostics.py:43-56``). The stdlib has no YAML parser, so
:func:`_yaml_load` implements only the subset PyYAML emits for plain data:
block mappings and sequences (including indentless sequences), plain, single-
and double-quoted scalars with line folding, literal/folded block scalars,
and simple flow collections. Anchors, aliases, tags, complex ``?`` keys and
multi-document streams are not supported; such values degrade to ``None`` or
a warning, never an exception.

Bundle kinds: only ``collectSlowQueryDiagnostics`` runs EXPLAIN.
``collectHighLoadDiagnostics`` and ``collectDiagnostics`` bundles contain
digest summaries but no plans, so loading them raises a clear ValueError.
A Shell bundle holds one query; if an archive contains several bundle
directories (for example several bundles zipped together), each directory
with EXPLAIN output is one selectable query, ordered by directory name.

Not imported: the traditional tabular EXPLAIN (except its query text), the
EXPLAIN warnings, HeatWave ``explain-rapid.txt``, the Performance Schema /
sys sampling (``diagnostics*`` members), global status deltas, error logs,
SHOW INDEX cardinalities, InnoDB persistent stats and host information.
"""
import datetime
import io
import json
import os
import re
import zipfile
import zlib

from .collectors import ADVISOR_VARIABLES, parse_show_create_table
from .parser import load_explain_json, parse_explain, _is_mariadb_format
from .server_info import detect_flavor

#: Per-member uncompressed size cap (bytes).
MAX_MEMBER_BYTES = 64 * 1024 * 1024
#: Total uncompressed bytes read from one archive.
MAX_TOTAL_BYTES = 256 * 1024 * 1024
#: Maximum number of members an archive may list.
MAX_MEMBERS = 100000

SOURCE = "mysqlsh-diagnostics"

_EXPLAIN_JSON = "explain_json"
_EXPLAIN_ANALYZE = "explain_analyze"
_EXPLAIN_TRADITIONAL = "explain"
_PLAN_BASENAMES = (_EXPLAIN_JSON + ".tsv", _EXPLAIN_ANALYZE + ".tsv",
                   _EXPLAIN_JSON + ".error", _EXPLAIN_ANALYZE + ".error")
_COMMANDS = {
    "collectHighLoadDiagnostics": "util.debug.collectHighLoadDiagnostics()",
    "collectDiagnostics": "util.debug.collectDiagnostics()",
}
_HOWTO = ("Collect one with util.debug.collectSlowQueryDiagnostics(path, query) "
          "in MySQL Shell.")


# ---------------------------------------------------------------------------
# Archive access (in memory, bounded)
# ---------------------------------------------------------------------------

def _open_zip(path_or_bytes):
    """Return an open ZipFile or raise ValueError."""
    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        source = io.BytesIO(bytes(path_or_bytes))
    elif isinstance(path_or_bytes, str) or hasattr(path_or_bytes, "__fspath__"):
        path = os.fspath(path_or_bytes)
        if not os.path.isfile(path):
            raise ValueError("File not found: {}".format(path))
        source = path
    elif hasattr(path_or_bytes, "read") and hasattr(path_or_bytes, "seek"):
        source = path_or_bytes
    else:
        raise ValueError("Expected a file path or the bytes of a ZIP archive.")
    try:
        return zipfile.ZipFile(source)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, ValueError):
        raise ValueError("This file is not a ZIP archive. Expected a MySQL Shell "
                         "diagnostics bundle. " + _HOWTO)


def _safe_name(name):
    """True for member names that are relative and stay inside the archive."""
    if not name or name.endswith("/"):
        return False
    if name.startswith(("/", "\\")) or "\\" in name or "\x00" in name:
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    return ".." not in name.split("/")


class _Bundle(object):
    """Bounded, in-memory reader over the safe members of one archive."""

    def __init__(self, zf):
        self.zf = zf
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ValueError("This ZIP lists {} members; the import limit is {}.".format(
                len(infos), MAX_MEMBERS))
        self.infos = {}
        for info in infos:
            if _safe_name(info.filename):
                self.infos[info.filename] = info
        self.total = 0
        self.cache = {}

    def names(self):
        return list(self.infos)

    def has(self, name):
        return name in self.infos

    def read(self, name):
        if name in self.cache:
            return self.cache[name]
        info = self.infos[name]
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError("Bundle member {} is {} bytes, over the {} MiB per-file "
                             "import limit.".format(name, info.file_size,
                                                    MAX_MEMBER_BYTES // (1024 * 1024)))
        if info.flag_bits & 0x1:
            raise ValueError("Bundle member {} is encrypted; export an unencrypted "
                             "bundle.".format(name))
        budget = min(MAX_MEMBER_BYTES, MAX_TOTAL_BYTES - self.total)
        try:
            with self.zf.open(info) as handle:
                data = handle.read(budget + 1)
        except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError,
                EOFError, OSError) as exc:
            raise ValueError("Bundle member {} could not be read: {}".format(name, exc))
        if len(data) > budget:
            if budget < MAX_MEMBER_BYTES:
                raise ValueError("The bundle exceeds the {} MiB total import limit.".format(
                    MAX_TOTAL_BYTES // (1024 * 1024)))
            raise ValueError("Bundle member {} is larger than the {} MiB per-file import "
                             "limit.".format(name, MAX_MEMBER_BYTES // (1024 * 1024)))
        self.total += len(data)
        text = data.decode("utf-8", "replace")
        self.cache[name] = text
        return text

    def text(self, name):
        return self.read(name) if self.has(name) else None


def _split(name):
    """Split a member name into (directory prefix incl. slash, basename)."""
    cut = name.rfind("/") + 1
    return name[:cut], name[cut:]


def _query_prefixes(bundle):
    found = set()
    for name in bundle.names():
        prefix, base = _split(name)
        if base in _PLAN_BASENAMES:
            found.add(prefix)
    return sorted(found)


def _command(bundle):
    for name in sorted(bundle.names()):
        if _split(name)[1] == "shell_info.yaml":
            match = re.search(r"(?m)^command:\s*['\"]?(\w+)", bundle.read(name))
            if match:
                return match.group(1)
    return None


# ---------------------------------------------------------------------------
# dump_query TSV members
# ---------------------------------------------------------------------------

def _tsv_sections(text):
    """Split a dump_query TSV into (statement, started, column_header, body)."""
    lines = text.split("\n")
    statement, started, i = [], "", 0
    if lines and lines[0].startswith("# Query:"):
        i = 1
        while i < len(lines) and lines[i].startswith("#\t"):
            statement.append(lines[i][2:])
            i += 1
    while i < len(lines) and lines[i].startswith("#"):
        line = lines[i]
        if line.startswith("# Started:"):
            started = line[len("# Started:"):].strip()
        i += 1
        if line.startswith("# Execution Time:"):
            # The header ends with a bare "#" line, then the column line.
            if i < len(lines) and lines[i] == "#":
                i += 1
            break
    header = ""
    if i < len(lines) and lines[i].startswith("# "):
        header = lines[i][2:]
        i += 1
    body = lines[i:]
    for j, line in enumerate(body):
        if line == "# Warnings":
            body = body[:j]
            break
    return "\n".join(statement), started, header, "\n".join(body)


_EXPLAIN_PREFIX = re.compile(r"^\s*EXPLAIN\s+(?:ANALYZE\s+)?(?:FORMAT\s*=\s*\w+\s+)?", re.I)


def _strip_explain(statement):
    return _EXPLAIN_PREFIX.sub("", statement, count=1).strip()


def _first_json(body):
    """Return the exact JSON object text at the start of *body*, or None."""
    start = body.find("{")
    if start < 0 or body[:start].strip():
        return None
    try:
        _, end = json.JSONDecoder().raw_decode(body, start)
    except ValueError:
        return None
    return body[start:end]


def _plan_root(text):
    """Parse plan text with the single parser; return the root or None."""
    try:
        data = load_explain_json(text)
        plan = data.get("query_plan", data) if isinstance(data, dict) else None
        if not isinstance(plan, dict) or not ("operation" in plan or _is_mariadb_format(plan)):
            return None
        return parse_explain(text)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
        return None


def _error_text(bundle, prefix, base):
    text = bundle.text(prefix + base + ".error")
    if text is None:
        return None
    lines = [l[2:] if l.startswith("# ") else l for l in text.strip().split("\n")]
    return lines[-1].strip() if lines else "unknown error"


def _parse_trace(text):
    _, _, header, body = _tsv_sections(text)
    body = body.rstrip("\n")
    if not body.strip():
        return None, "The bundle's optimizer trace is empty."
    try:
        head, missing, denied = body.rsplit("\t", 2)
        missing, denied = int(missing.strip()), int(denied.strip())
    except ValueError:
        return None, "The bundle's optimizer trace could not be read."
    data = None
    pos = head.find("\t")
    while pos >= 0:
        candidate = head[pos + 1:]
        if candidate.lstrip().startswith("{"):
            try:
                data = json.loads(candidate)
                break
            except ValueError:
                pass
        pos = head.find("\t", pos + 1)
    if not isinstance(data, dict):
        if denied:
            return None, "Optimizer trace was not recorded: insufficient privileges."
        if missing:
            return None, ("Optimizer trace was truncated by optimizer_trace_max_mem_size "
                          "({} bytes missing) and could not be read.".format(missing))
        return None, "The bundle's optimizer trace could not be read."
    return {"data": data, "missing_bytes": max(missing, 0),
            "insufficient_privileges": 1 if denied else 0}, None


def _parse_variables(text):
    out = {}
    wanted = {name.lower() for name in ADVISOR_VARIABLES}
    _, _, _, body = _tsv_sections(text)
    for line in body.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].lower() in wanted:
            out[parts[0]] = parts[1]
    return out


def _parse_instance(text):
    info = {}
    for line in text.split("\n"):
        key, sep, value = line.partition(": ")
        if sep:
            info[key.strip()] = value.strip()
    return info


_DURATION = re.compile(r"(?:(\d+) days?)?\s*(?:(\d+) hours?)?\s*(?:(\d+) min)?\s*"
                       r"(?:([\d.]+) sec)?\s*$")


def _shell_seconds(value):
    """Parse Shell ``format_seconds`` text (``1 day 2 hours 3 min 4.5 sec``)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    match = _DURATION.match(value.strip())
    if not match or not any(match.groups()):
        return None
    d, h, m, s = match.groups()
    return (int(d or 0) * 86400 + int(h or 0) * 3600 + int(m or 0) * 60
            + float(s or 0))


def _utc_iso(value):
    try:
        moment = datetime.datetime.strptime(value.strip()[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return ""
    return moment.replace(tzinfo=datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Minimal YAML reader for PyYAML ``yaml.dump`` output
# ---------------------------------------------------------------------------

_YAML_NULL = ("", "~", "null", "Null", "NULL")
_YAML_TRUE = ("yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON")
_YAML_FALSE = ("no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF")
_YAML_INT = re.compile(r"^[-+]?(?:0|[1-9][0-9_]*)$")
_YAML_FLOAT = re.compile(r"^[-+]?(?:[0-9][0-9_]*)?\.[0-9_]*(?:[eE][-+]?[0-9]+)?$")
_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
            "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
            "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ",
            "P": " "}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


def _resolve_plain(text):
    if text in _YAML_NULL:
        return None
    if text in _YAML_TRUE:
        return True
    if text in _YAML_FALSE:
        return False
    if _YAML_INT.match(text):
        return int(text.replace("_", ""))
    if _YAML_FLOAT.match(text) and any(c.isdigit() for c in text):
        return float(text.replace("_", ""))
    if text.lower() in (".inf", "+.inf", "-.inf", ".nan"):
        return float(text.lower().replace(".", ""))
    return text


def _fold(text, escapes):
    """Fold a multi-line flow scalar body per YAML line-folding rules."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if escapes and ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "\n":
                i += 2
                while i < n and text[i] in " \t":
                    i += 1
                continue
            if nxt in _HEX_ESCAPES:
                width = _HEX_ESCAPES[nxt]
                try:
                    out.append(chr(int(text[i + 2:i + 2 + width], 16)))
                except ValueError:
                    out.append(text[i:i + 2 + width])
                i += 2 + width
                continue
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        if ch in " \t\n":
            j = i
            while j < n and text[j] in " \t\n":
                j += 1
            run = text[i:j]
            breaks = run.count("\n")
            if not breaks:
                out.append(run)
            elif breaks == 1:
                out.append(" ")
            else:
                out.append("\n" * (breaks - 1))
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _quoted_end(text, start):
    """Index just past the closing quote of the scalar opening at *start*."""
    quote, i = text[start], start + 1
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and text[i + 1:i + 2] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return -1


def _unquote(token):
    if token[0] == "'":
        return _fold(token[1:-1], False).replace("''", "'")
    return _fold(token[1:-1], True)


class _FlowParser(object):
    def __init__(self, text):
        self.text, self.i = text, 0

    def ws(self):
        while self.i < len(self.text) and self.text[self.i] in " \t\n\r":
            self.i += 1

    def value(self):
        self.ws()
        ch = self.text[self.i:self.i + 1]
        if ch == "[":
            self.i += 1
            items = []
            while True:
                self.ws()
                if self.text[self.i:self.i + 1] == "]":
                    self.i += 1
                    return items
                items.append(self.value())
                self.ws()
                if self.text[self.i:self.i + 1] == ",":
                    self.i += 1
                elif self.text[self.i:self.i + 1] != "]":
                    raise ValueError("bad flow sequence")
        if ch == "{":
            self.i += 1
            out = {}
            while True:
                self.ws()
                if self.text[self.i:self.i + 1] == "}":
                    self.i += 1
                    return out
                key = self.value()
                self.ws()
                if self.text[self.i:self.i + 1] == ":":
                    self.i += 1
                    val = self.value()
                else:
                    val = None
                out[key if not isinstance(key, (list, dict)) else str(key)] = val
                self.ws()
                if self.text[self.i:self.i + 1] == ",":
                    self.i += 1
                elif self.text[self.i:self.i + 1] != "}":
                    raise ValueError("bad flow mapping")
        if ch in ("'", '"'):
            end = _quoted_end(self.text, self.i)
            if end < 0:
                raise ValueError("unterminated quote")
            token = self.text[self.i:end]
            self.i = end
            return _unquote(token)
        start = self.i
        while self.i < len(self.text):
            c = self.text[self.i]
            if c in ",[]{}":
                break
            if c == ":" and self.text[self.i + 1:self.i + 2] in (" ", "\n", ",", "]", "}", ""):
                break
            self.i += 1
        return _resolve_plain(_fold(self.text[start:self.i], False).strip())


class _BlockYaml(object):
    """Line-oriented reader for PyYAML's block-style ``yaml.dump`` output."""

    def __init__(self, text):
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.i = 0

    @staticmethod
    def indent(line):
        return len(line) - len(line.lstrip(" "))

    def peek(self):
        while self.i < len(self.lines):
            line = self.lines[self.i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or (
                    self.indent(line) == 0 and stripped in ("---", "...")):
                self.i += 1
                continue
            return self.indent(line), stripped
        return None

    @staticmethod
    def is_seq(content):
        return content == "-" or content.startswith("- ")

    @staticmethod
    def split_key(content):
        if content[0] in ("'", '"'):
            end = _quoted_end(content, 0)
            if end > 0 and content[end:end + 1] == ":" and content[end + 1:end + 2] in ("", " "):
                return _unquote(content[:end]), content[end + 1:].strip()
            return None
        if content[0] in "[{|>!&*%@`?":
            return None
        match = re.match(r"^(.*?):(?: (.*)|$)", content)
        if not match:
            return None
        return _resolve_plain(match.group(1).strip()), (match.group(2) or "").strip()

    def node(self, parent):
        top = self.peek()
        if top is None or top[0] <= parent:
            return None
        ind, content = top
        if self.is_seq(content):
            return self.sequence(ind)
        if self.split_key(content) is not None:
            return self.mapping(ind)
        self.i += 1
        return self.value(content, parent)

    def sequence(self, ind):
        items = []
        while True:
            top = self.peek()
            if top is None or top[0] != ind or not self.is_seq(top[1]):
                return items
            line = self.lines[self.i]
            # Re-read the item body as if the dash were indentation.
            self.lines[self.i] = line[:ind] + " " + line[ind + 1:]
            items.append(self.node(ind))

    def mapping(self, ind):
        out = {}
        while True:
            top = self.peek()
            if top is None or top[0] != ind or self.is_seq(top[1]):
                return out
            parsed = self.split_key(top[1])
            if parsed is None:
                return out
            key, rest = parsed
            self.i += 1
            if rest:
                val = self.value(rest, ind)
            else:
                val = self.node(ind)
                if val is None:
                    nxt = self.peek()
                    if nxt is not None and nxt[0] == ind and self.is_seq(nxt[1]):
                        val = self.sequence(ind)
            out[key if not isinstance(key, (list, dict)) else str(key)] = val

    def value(self, rest, parent):
        """Parse a scalar or flow value whose first line (*rest*) was consumed."""
        while rest[:1] in ("!", "&"):
            parts = rest.split(None, 1)
            rest = parts[1] if len(parts) > 1 else ""
            if not rest:
                return self.node(parent)
        if rest[:1] == "*":
            return None
        if rest[:1] in ("|", ">"):
            return self.block_scalar(rest, parent)
        if rest[:1] in ("'", '"', "[", "{"):
            text = rest
            while True:
                try:
                    if rest[0] in "'\"":
                        end = _quoted_end(text, 0)
                        if end > 0:
                            return _unquote(text[:end])
                    else:
                        return _FlowParser(text).value()
                except (ValueError, IndexError):
                    pass
                if self.i >= len(self.lines):
                    return None
                text += "\n" + self.lines[self.i]
                self.i += 1
        parts = [rest]
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() and self.indent(line) <= parent:
                break
            if not line.strip():
                ahead = self.i + 1
                while ahead < len(self.lines) and not self.lines[ahead].strip():
                    ahead += 1
                if ahead >= len(self.lines) or self.indent(self.lines[ahead]) <= parent:
                    break
            parts.append(line.strip())
            self.i += 1
        return _resolve_plain(_fold("\n".join(parts), False).strip()) if len(parts) > 1 \
            else _resolve_plain(rest.strip())

    def block_scalar(self, header, parent):
        style = header[0]
        indicator = re.match(r"^[|>]([1-9])?([-+])?([1-9])?\s*$", header)
        digits = indicator and (indicator.group(1) or indicator.group(3))
        chomp = (indicator and indicator.group(2)) or ""
        content_indent = parent + int(digits) if digits else None
        body = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip():
                if content_indent is None:
                    if self.indent(line) <= parent:
                        break
                    content_indent = self.indent(line)
                elif self.indent(line) < content_indent:
                    break
                body.append(line[content_indent:])
            else:
                body.append(line[content_indent:] if content_indent is not None else "")
            self.i += 1
        trailing = 0
        while body and not body[-1].strip():
            body.pop()
            trailing += 1
        if style == ">":
            text = ""
            for k, line in enumerate(body):
                if k and line and body[k - 1] and not line.startswith(" ") \
                        and not body[k - 1].startswith(" "):
                    text += " " + line
                else:
                    text += ("\n" if k else "") + line
        else:
            text = "\n".join(body)
        if not body:
            return "\n" * trailing if chomp == "+" else ""
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (1 + trailing)
        return text + "\n"


def _yaml_load(text):
    """Load the PyYAML-emitted subset described in the module docstring."""
    reader = _BlockYaml(text)
    return reader.node(-1)


# ---------------------------------------------------------------------------
# Referenced-table metadata -> collectors.py shapes
# ---------------------------------------------------------------------------

_STATS_NUMERIC = ("table_rows", "data_length", "index_length", "data_free", "auto_increment")
_STATS_TEXT = ("table_schema", "table_name", "engine", "row_format")


def _ci_get(mapping, key):
    if not isinstance(mapping, dict):
        return None
    for k, v in mapping.items():
        if isinstance(k, str) and k.lower() == key:
            return v
    return None


def _table_metadata(info, fallback_schema, fallback_table):
    schema = info.get("Table Schema") or fallback_schema
    table = info.get("Table Name") or fallback_table
    key = "{}.{}".format(schema, table) if schema else str(table)
    ddl = info.get("DDL")
    parsed = parse_show_create_table(ddl if isinstance(ddl, str) else "")
    status = info.get("Table Status")
    stats = None
    if isinstance(status, dict):
        stats = {}
        for col in _STATS_TEXT:
            value = _ci_get(status, col)
            stats[col] = "NULL" if value is None else str(value)
        for col in _STATS_NUMERIC:
            value = _ci_get(status, col)
            try:
                stats[col] = int(value) if value not in (None, "", "NULL") else 0
            except (TypeError, ValueError):
                stats[col] = 0
    return key, parsed, stats


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_shell_diagnostics_zip(path_or_bytes):
    """Cheap sniff: True when the input is a ZIP that looks like a Shell bundle.

    Never raises. Matches any ``util.debug.collect*Diagnostics`` archive
    (``shell_info.yaml``) and archives that carry Shell's EXPLAIN members.
    """
    try:
        zf = _open_zip(path_or_bytes)
    except ValueError:
        return False
    try:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            return False
        for info in infos:
            if not _safe_name(info.filename):
                continue
            base = _split(info.filename)[1]
            if base == "shell_info.yaml" or base in _PLAN_BASENAMES:
                return True
        return False
    finally:
        zf.close()


def _queries(bundle):
    out = []
    for index, prefix in enumerate(_query_prefixes(bundle)):
        query = ""
        for base in (_EXPLAIN_JSON, _EXPLAIN_ANALYZE, _EXPLAIN_TRADITIONAL):
            name = prefix + base + ".tsv"
            if bundle.has(name):
                query = _strip_explain(_tsv_sections(bundle.read(name))[0])
                if query:
                    break
        out.append({"index": index, "prefix": prefix, "query": query})
    return out


def _check_bundle(bundle):
    if not any(_split(n)[1] == "shell_info.yaml" for n in bundle.names()) \
            and not _query_prefixes(bundle):
        raise ValueError("This ZIP is not a MySQL Shell diagnostics bundle "
                         "(no shell_info.yaml or EXPLAIN output found). " + _HOWTO)


def list_shell_diagnostics_queries(path_or_bytes):
    """Return ``[{"index", "prefix", "query"}]`` for each query with EXPLAIN output."""
    zf = _open_zip(path_or_bytes)
    try:
        bundle = _Bundle(zf)
        _check_bundle(bundle)
        return _queries(bundle)
    finally:
        zf.close()


def _describe(queries):
    return "; ".join("{}: {}{}".format(q["index"], q["prefix"] or "(root) ",
                                       (" " + q["query"][:80]) if q["query"] else "")
                     for q in queries)


def load_shell_diagnostics(path_or_bytes, query_index=None):
    """Load a ``collectSlowQueryDiagnostics`` bundle as a capture-1.0 dict.

    *query_index* selects one query when the archive holds several bundle
    directories (see :func:`list_shell_diagnostics_queries`); ``None`` picks
    the first. Raises ``ValueError`` with a user-facing message on any
    unusable input.
    """
    from .live import measurement_summary

    if query_index is not None and (isinstance(query_index, bool)
                                    or not isinstance(query_index, int)):
        raise ValueError("Query index must be an integer.")
    zf = _open_zip(path_or_bytes)
    try:
        bundle = _Bundle(zf)
        _check_bundle(bundle)
        queries = _queries(bundle)
        if not queries:
            command = _command(bundle)
            if command in _COMMANDS:
                raise ValueError("This bundle was written by {}, which does not run "
                                 "EXPLAIN, so it has no query plan. {}".format(
                                     _COMMANDS[command], _HOWTO))
            raise ValueError("No EXPLAIN output was found in this MySQL Shell "
                             "diagnostics bundle. " + _HOWTO)
        warnings = []
        if query_index is None:
            query_index = 0
            if len(queries) > 1:
                warnings.append("The archive contains {} queries; imported query 0. "
                                "Available: {}".format(len(queries), _describe(queries)))
        if not 0 <= query_index < len(queries):
            raise ValueError("Query index {} is out of range. This archive has {} "
                             "quer{}: {}".format(query_index, len(queries),
                                                 "y" if len(queries) == 1 else "ies",
                                                 _describe(queries)))
        return _load_one(bundle, queries[query_index], warnings, measurement_summary)
    finally:
        zf.close()


def _load_one(bundle, selected, warnings, measurement_summary):
    prefix = selected["prefix"]
    attachments = {}
    started = ""

    analyze_raw, analyze_root = "", None
    text = bundle.text(prefix + _EXPLAIN_ANALYZE + ".tsv")
    if text is not None:
        _, started, _, body = _tsv_sections(text)
        candidate = _first_json(body)
        root = _plan_root(candidate) if candidate else None
        if root is not None:
            analyze_raw, analyze_root = candidate, root
        elif candidate:
            warnings.append("EXPLAIN ANALYZE was stored as JSON that myflames could not "
                            "read; using the estimated EXPLAIN FORMAT=JSON plan.")
        elif body.strip():
            attachments["explain_analyze_tree"] = body.strip("\n")
            warnings.append("EXPLAIN ANALYZE was stored as TREE text; using the estimated "
                            "EXPLAIN FORMAT=JSON plan. The TREE output is kept under "
                            "attachments.explain_analyze_tree.")
    else:
        error = _error_text(bundle, prefix, _EXPLAIN_ANALYZE)
        if error:
            warnings.append("EXPLAIN ANALYZE failed when the bundle was collected: " + error)
        else:
            warnings.append("The bundle has no EXPLAIN ANALYZE output (it requires MySQL "
                            "8.0.18 or later); using the estimated EXPLAIN FORMAT=JSON plan.")

    estimated_raw = ""
    text = bundle.text(prefix + _EXPLAIN_JSON + ".tsv")
    if text is not None:
        _, json_started, _, body = _tsv_sections(text)
        started = started or json_started
        candidate = _first_json(body)
        if candidate and _plan_root(candidate) is not None:
            estimated_raw = candidate
        elif not analyze_raw:
            notice = "The bundle's EXPLAIN FORMAT=JSON output could not be read."
            if candidate and '"query_block"' in candidate:
                notice += (" It uses the version 1 (query_block) JSON format, which "
                           "myflames reads only partially. To collect a readable plan, "
                           "run SET SESSION explain_format=JSON, "
                           "explain_json_format_version=2 (MySQL 8.3 or later) in MySQL "
                           "Shell before collectSlowQueryDiagnostics(), so EXPLAIN "
                           "ANALYZE is stored as JSON.")
            warnings.append(notice)
    else:
        error = _error_text(bundle, prefix, _EXPLAIN_JSON)
        if error:
            warnings.append("EXPLAIN FORMAT=JSON failed when the bundle was collected: " + error)

    if analyze_raw:
        mode, raw = "analyze", analyze_raw
    elif estimated_raw:
        mode, raw = "explain", estimated_raw
    else:
        raise ValueError("No usable JSON plan was found in this MySQL Shell diagnostics "
                         "bundle ({}). myflames needs EXPLAIN FORMAT=JSON or EXPLAIN "
                         "ANALYZE JSON output; TREE text cannot be imported.".format(
                             "; ".join(warnings) or "no plan members"))

    instance = _parse_instance(bundle.text(prefix + "instance") or "")
    version_text = instance.get("Version", "")
    server_version = version_text.rsplit(" ", 1)[-1] if version_text else ""
    engine = detect_flavor(version_text)
    captured_at = _utc_iso(instance.get("UTC Time", "")) or started

    query_info = {}
    text = bundle.text(prefix + "query-info.yaml")
    if text is not None:
        try:
            loaded = _yaml_load(text)
            if isinstance(loaded, dict):
                query_info = loaded
        except (ValueError, IndexError, TypeError, RecursionError):
            warnings.append("query-info.yaml could not be read; run timing is unavailable.")

    query = selected["query"]
    if not query and isinstance(query_info.get("Query"), str):
        query = query_info["Query"].strip()

    runs = []
    statistics = {}
    seconds = _shell_seconds(query_info.get("Execution Time"))
    if seconds is not None:
        statistics["client_execution_ms"] = round(seconds * 1000.0, 4)
    fetch = query_info.get("Result Fetch Time")
    if isinstance(fetch, (int, float)) and not isinstance(fetch, bool):
        statistics["result_fetch_ms"] = round(float(fetch) * 1000.0, 4)
    for source, target in (("Result Rows", "rows_sent"), ("Affected Rows", "affected_rows")):
        value = query_info.get(source)
        if isinstance(value, int) and not isinstance(value, bool):
            statistics[target] = value
    statistics = {k: v for k, v in statistics.items() if v == v and abs(v) != float("inf")}
    total = None
    if mode == "analyze" and (analyze_root.get("details") or {}).get("actual_last_row_ms") is not None:
        total = analyze_root["total_time"]
    if total is not None or statistics:
        runs.append({"total_time_ms": total, "statistics": statistics or None,
                     "session_status_delta": {}, "plan": raw})

    trace = None
    text = bundle.text(prefix + "explain-optimizer_trace.tsv")
    if text is not None:
        trace, notice = _parse_trace(text)
        if notice:
            warnings.append(notice)

    schema, table_stats = {}, {}
    for name in sorted(bundle.names()):
        member_prefix, base = _split(name)
        if member_prefix != prefix or not base.startswith("referenced_table-") \
                or not base.endswith(".yaml"):
            continue
        stem = base[len("referenced_table-"):-len(".yaml")]
        fallback_schema, _, fallback_table = stem.partition(".")
        try:
            info = _yaml_load(bundle.read(name))
        except (ValueError, IndexError, TypeError, RecursionError):
            info = None
        if not isinstance(info, dict):
            warnings.append("Table metadata in {} could not be read.".format(base))
            continue
        key, parsed, stats = _table_metadata(info, fallback_schema, fallback_table)
        schema[key] = parsed
        if stats is not None:
            table_stats[key] = stats

    variables = {}
    text = bundle.text(prefix + "global_variables.tsv")
    if text is not None:
        variables = _parse_variables(text)
        if variables:
            warnings.append("Server variables are GLOBAL values from the bundle; session "
                            "overrides at collection time are not recorded.")

    return {
        "schema_version": "capture-1.0",
        "source": SOURCE,
        "raw": raw,
        "query": query,
        "mode": mode,
        "engine": engine,
        "server_version": server_version,
        "captured_at": captured_at,
        "conditions": "",
        "runs": runs,
        "measurements": measurement_summary(runs),
        "trace": trace,
        "schema": schema,
        "table_stats": table_stats,
        "variables": variables,
        "warnings": list(dict.fromkeys(warnings)),
        "attachments": attachments,
    }
