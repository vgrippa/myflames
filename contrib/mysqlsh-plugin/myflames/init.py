# myflames plugin for MySQL Shell.
#
# Adds a `myflames` global object to MySQL Shell (JavaScript and Python modes,
# plus the `mysqlsh -- myflames ...` command line) that captures the plan of a
# query on the current Shell session and renders it with the installed
# myflames package (flame graph / Visual Explain HTML report, LLM digest,
# ranked advisor findings).
#
# Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs.
#
# Install: copy or symlink this folder to ~/.mysqlsh/plugins/myflames and
# install the package into Shell's Python:
#     mysqlsh --pym pip install --user myflames
#
# Note: this folder deliberately has NO __init__.py. MySQL Shell puts the
# plugins directory first on sys.path while it loads init.py, so a regular
# package named `myflames` here would shadow the real myflames package.
# The package is imported lazily, at call time, for the same reason.

import importlib
import os
import pathlib
import re
import sys
import tempfile
import webbrowser

import mysqlsh
from mysqlsh.plugin_manager import plugin, plugin_function

INSTALL_COMMAND = "mysqlsh --pym pip install --user myflames"
MIN_MYSQL_VERSION = (8, 3, 0)
MIN_SHELL_VERSION_SQL_HANDLER = "9.0.0"
SQL_HANDLER_NAME = "myflames"
SQL_HANDLER_KEYWORD = "MYFLAMES"
VIEW_TYPES = ("flamegraph", "bargraph", "tree", "workbench")
_READ_VERBS = ("SELECT", "WITH", "TABLE", "VALUES")
_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE")
_OPTION_KEYS = {
    "explain": ("analyze", "output", "type", "open"),
    "digest": ("analyze",),
    "advise": ("analyze",),
    "sql_handler": ("type", "open"),
}

# Prefixes registered with shell.register_sql_handler(). The Shell matcher is
# case-insensitive and collapses runs of whitespace, but it treats "=" and
# " = " differently, so every spacing variant around "=" is listed.
SQL_HANDLER_PREFIXES = []
for _verb in ("EXPLAIN ANALYZE", "EXPLAIN"):
    for _eq in ("=", " =", "= ", " = "):
        SQL_HANDLER_PREFIXES.append(
            _verb + " FORMAT" + _eq + SQL_HANDLER_KEYWORD)
del _verb, _eq

_HANDLER_RE = re.compile(
    r"^\s*(?:/\*.*?\*/\s*)*EXPLAIN\s+(ANALYZE\s+)?FORMAT\s*=\s*"
    + SQL_HANDLER_KEYWORD + r"\b(.*)$",
    re.IGNORECASE | re.DOTALL)

_handler_state = {"registered": False, "enabled": False, "options": {}}


class PluginError(Exception):
    """Fallback error type when mysqlsh.Error is unavailable."""


def _fail(message):
    error_cls = getattr(mysqlsh, "Error", None) or PluginError
    return error_cls(message)


def _shell():
    return mysqlsh.globals.shell


# ---------------------------------------------------------------------------
# myflames package access (lazy, see the header note)
# ---------------------------------------------------------------------------

def _load_api():
    """Import the installed myflames package, or explain how to install it."""
    cached = sys.modules.get("myflames")
    if cached is not None and getattr(cached, "__file__", None) is None:
        # A namespace package (this plugin folder) was cached instead of the
        # real package; drop it so the real one can be found.
        for name in list(sys.modules):
            if name == "myflames" or name.startswith("myflames."):
                del sys.modules[name]
    try:
        api = {
            "parser": importlib.import_module("myflames.parser"),
            "render": importlib.import_module("myflames.render"),
            "report": importlib.import_module("myflames.output_html_report"),
            "sidecar": importlib.import_module("myflames.output_sidecar"),
            "digest": importlib.import_module("myflames.digest"),
            "findings": importlib.import_module("myflames.findings"),
        }
    except ImportError as exc:
        raise _fail(
            "The myflames Python package is not installed in MySQL Shell's "
            "Python. Install it with:\n    " + INSTALL_COMMAND + "\nthen "
            "restart mysqlsh. (import error: " + str(exc) + ")")
    return api


# ---------------------------------------------------------------------------
# Option and statement validation
# ---------------------------------------------------------------------------

def _as_bool(value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in (
            "true", "false", "1", "0", "yes", "no", "on", "off"):
        return value.strip().lower() in ("true", "1", "yes", "on")
    raise _fail("Option '%s' must be a boolean, got %r." % (name, value))


def _normalize_options(kind, options):
    options = dict(options or {})
    allowed = _OPTION_KEYS[kind]
    unknown = sorted(k for k in options if k not in allowed)
    if unknown:
        raise _fail("Unknown option(s): %s. Valid options: %s." % (
            ", ".join(unknown), ", ".join(allowed)))
    result = {}
    if "analyze" in allowed:
        result["analyze"] = _as_bool(options.get("analyze", True), "analyze")
    if "open" in allowed:
        result["open"] = _as_bool(options.get("open", True), "open")
    if "type" in allowed:
        view = options.get("type")
        if view is not None:
            view = str(view).strip().lower()
            if view not in VIEW_TYPES:
                raise _fail("Option 'type' must be one of %s, got %r." % (
                    ", ".join(VIEW_TYPES), options.get("type")))
        result["type"] = view
    if "output" in allowed:
        output = options.get("output")
        result["output"] = os.path.expanduser(str(output)) if output else None
    return result


def _strip_noise(sql):
    """Return *sql* with comments, string literals and quoted identifiers
    replaced by spaces, so keyword scanning cannot be fooled by them."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        elif ch == "#" or (ch == "-" and nxt == "-" and
                           (i + 2 >= n or sql[i + 2].isspace())):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
            out.append(" ")
        elif ch in ("'", '"', "`"):
            j = i + 1
            while j < n:
                if sql[j] == "\\" and ch != "`":
                    j += 2
                    continue
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            out.append(" x ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _clean_query(query):
    if not isinstance(query, str) or not query.strip():
        raise _fail("Pass the SQL statement to explain, e.g. "
                    "myflames.explain(\"SELECT ...\").")
    text = query.strip()
    if text.endswith("\\G") or text.endswith("\\g"):
        text = text[:-2].rstrip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        raise _fail("The statement is empty.")
    return text


def _depth0_words(sql):
    """Upper-cased words that appear outside parentheses, in order."""
    words, depth = [], 0
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*|[()]|;", sql):
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            words.append(token.upper())
    return words


def _main_verb(words):
    """First statement verb, skipping a leading WITH clause."""
    if not words:
        return None
    if words[0] != "WITH":
        return words[0]
    for word in words[1:]:
        if word in ("SELECT", "TABLE", "VALUES") + _WRITE_VERBS:
            return word
    return None


def check_statement(query, analyze=True):
    """Validate a statement before it is wrapped in EXPLAIN.

    EXPLAIN ANALYZE executes the statement, so analyze mode only accepts
    read-shaped statements (SELECT / WITH ... SELECT / TABLE / VALUES).
    Returns the cleaned statement text.
    """
    text = _clean_query(query)
    scrubbed = _strip_noise(text)
    if ";" in scrubbed:
        raise _fail("Pass a single statement (found ';' inside the text).")
    words = _depth0_words(scrubbed)
    stripped = scrubbed.lstrip(" (\t\r\n")
    first = re.match(r"[A-Za-z_]+", stripped)
    first = first.group(0).upper() if first else ""
    if first in ("EXPLAIN", "DESCRIBE", "DESC", "ANALYZE"):
        raise _fail("Pass the statement without EXPLAIN; myflames adds "
                    "EXPLAIN ANALYZE FORMAT=JSON itself.")
    verb = _main_verb(words) if first == "WITH" else first
    if analyze:
        if first not in _READ_VERBS or verb not in ("SELECT", "TABLE",
                                                    "VALUES"):
            raise _fail(
                "EXPLAIN ANALYZE executes the statement, so myflames only "
                "analyzes SELECT, WITH ... SELECT, TABLE and VALUES "
                "statements (got %s). Use {analyze: false} for an estimated "
                "plan of a data-changing statement." % (verb or first or "?"))
        for i, word in enumerate(words[:-1]):
            if word == "INTO" and words[i + 1] in ("OUTFILE", "DUMPFILE"):
                raise _fail("SELECT ... INTO OUTFILE/DUMPFILE writes files on "
                            "the server; myflames will not analyze it.")
    elif first not in _READ_VERBS + _WRITE_VERBS:
        raise _fail("myflames explains SELECT, WITH, TABLE, VALUES, INSERT, "
                    "UPDATE, DELETE and REPLACE statements (got %s)." %
                    (first or "?"))
    return text


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _scalar(session, sql):
    row = session.run_sql(sql).fetch_one()
    return None if row is None else row[0]


def _parse_version(text):
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _require_session(session):
    if session is None:
        session = _shell().get_session()
    if session is None or (hasattr(session, "is_open")
                           and not session.is_open()):
        raise _fail("Connect MySQL Shell to a MySQL server first, e.g. "
                    "\\connect root@localhost:3306")
    return session


def capture_plan(session, query, analyze=True):
    """Run EXPLAIN [ANALYZE] FORMAT=JSON on *session* and return a dict
    with the raw JSON plan and server metadata.

    Mirrors myflames.live: explain_json_format_version=2 and, for ANALYZE,
    a READ ONLY transaction that is always rolled back. The previous
    explain_json_format_version is restored afterwards.
    """
    text = check_statement(query, analyze)
    version_text = str(_scalar(session, "SELECT VERSION()") or "")
    if "mariadb" in version_text.lower():
        raise _fail(
            "This server is MariaDB (%s). The myflames MySQL Shell plugin "
            "supports MySQL only; use the myflames CLI for MariaDB "
            "(python3 -m myflames --help)." % version_text)
    version = _parse_version(version_text)
    if version is not None and version < MIN_MYSQL_VERSION:
        raise _fail(
            "MySQL %s is too old: myflames needs MySQL 8.3 or later "
            "(explain_json_format_version=2, EXPLAIN ANALYZE FORMAT=JSON)."
            % version_text)

    if analyze:
        # START TRANSACTION implicitly commits an open transaction, so refuse
        # instead of silently committing the user's work. Best effort: if
        # Performance Schema is unavailable the check is skipped.
        try:
            active = _scalar(
                session,
                "SELECT COUNT(*) FROM performance_schema."
                "events_transactions_current WHERE THREAD_ID = "
                "PS_CURRENT_THREAD_ID() AND STATE = 'ACTIVE' "
                "AND AUTOCOMMIT = 'NO'")
        except Exception:
            active = None
        if active:
            raise _fail(
                "This session has an open transaction. myflames runs EXPLAIN "
                "ANALYZE inside its own READ ONLY transaction, which would "
                "commit yours; COMMIT or ROLLBACK first.")

    previous = _scalar(session, "SELECT @@SESSION.explain_json_format_version")
    try:
        previous = int(previous)
    except (TypeError, ValueError):
        previous = None
    changed = previous is not None and previous != 2
    if changed:
        session.run_sql("SET SESSION explain_json_format_version = 2")
    raw = None
    try:
        if analyze:
            session.run_sql("START TRANSACTION READ ONLY")
            try:
                raw = _scalar(session, "EXPLAIN ANALYZE FORMAT=JSON " + text)
            finally:
                session.run_sql("ROLLBACK")
        else:
            raw = _scalar(session, "EXPLAIN FORMAT=JSON " + text)
    finally:
        if changed:
            session.run_sql(
                "SET SESSION explain_json_format_version = %d" % previous)
    if not raw:
        raise _fail("The server returned no JSON plan for this statement.")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return {"raw": str(raw), "query": text, "server_version": version_text,
            "analyze": analyze}


def _sidecar(api, captured):
    root = api["parser"].parse_explain(captured["raw"])
    analysis = api["parser"].analyze_plan(root)
    return api["sidecar"].build_sidecar(
        root, analysis, source_type="live", engine="mysql",
        engine_version=captured["server_version"] or None,
        query_raw=captured["query"])


# ---------------------------------------------------------------------------
# Actions (shared by the plugin functions and the SQL handler)
# ---------------------------------------------------------------------------

def _default_output_path():
    handle, path = tempfile.mkstemp(prefix="myflames-", suffix=".html")
    os.close(handle)
    return path


def _open_in_browser(path):
    try:
        webbrowser.open(pathlib.Path(os.path.abspath(path)).as_uri())
    except Exception:
        pass


def run_explain(session, query, options=None):
    """Capture, render and write the plan. Returns the output path."""
    opts = _normalize_options("explain", options)
    api = _load_api()
    session = _require_session(session)
    captured = capture_plan(session, query, opts["analyze"])
    output = opts["output"] or _default_output_path()
    view = opts["type"] or "flamegraph"
    title = "MySQL Query Plan" if opts["analyze"] else \
        "MySQL Query Plan (estimated)"
    if output.lower().endswith(".svg"):
        content = api["render"].render_explain(captured["raw"], view,
                                               title=title)
    else:
        content = api["report"].render_html_report(
            captured["raw"], view_type=view, title=title,
            query_text=captured["query"])
    folder = os.path.dirname(os.path.abspath(output))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(content)
    print("myflames: wrote %s" % output)
    if opts["open"]:
        _open_in_browser(output)
    return output


def run_digest(session, query, options=None):
    opts = _normalize_options("digest", options)
    api = _load_api()
    session = _require_session(session)
    captured = capture_plan(session, query, opts["analyze"])
    text = api["digest"].build_digest(_sidecar(api, captured))
    print(text.rstrip("\n"))
    return text


def format_findings(findings):
    """Same text layout as `myflames advise`."""
    if not findings:
        return "No findings: the plan looks clean."
    lines = []
    for f in findings:
        line = "[{}/{}/{}] {}".format(
            f["severity"], f["category"], f["confidence"], f["text"].strip())
        if f.get("why"):
            line += "\n    Why: " + f["why"].strip()
        if f.get("node_labels"):
            line += "\n    @ " + ", ".join(f["node_labels"])
        lines.append(line)
    return "\n".join(lines)


def run_advise(session, query, options=None):
    opts = _normalize_options("advise", options)
    api = _load_api()
    session = _require_session(session)
    captured = capture_plan(session, query, opts["analyze"])
    findings = api["findings"].build_findings(_sidecar(api, captured))
    text = format_findings(findings)
    print(text)
    return findings


# ---------------------------------------------------------------------------
# SQL handler: EXPLAIN [ANALYZE] FORMAT=MYFLAMES <statement>
# ---------------------------------------------------------------------------

def match_handler_sql(sql):
    """Return (analyze, statement) when *sql* is a FORMAT=MYFLAMES request,
    else None."""
    match = _HANDLER_RE.match(sql or "")
    if not match:
        return None
    return bool(match.group(1)), match.group(2).strip()


def handle_sql(session, sql):
    """Renders EXPLAIN [ANALYZE] FORMAT=MYFLAMES statements with myflames."""
    if not _handler_state["enabled"]:
        raise _fail("The myflames SQL handler is disabled. Run "
                    "myflames.enableSqlHandler() (JS) or "
                    "myflames.enable_sql_handler() (Python) to use "
                    "EXPLAIN ANALYZE FORMAT=MYFLAMES.")
    parsed = match_handler_sql(sql)
    if parsed is None:
        return None
    analyze, statement = parsed
    options = dict(_handler_state["options"])
    options["analyze"] = analyze
    path = run_explain(session, statement, options)
    return _shell().create_result({
        "data": [{"myflames_output": path}],
        "info": "myflames rendered the %s plan." % (
            "analyzed" if analyze else "estimated"),
    })


def enable_handler(options=None):
    shell = _shell()
    opts = _normalize_options("sql_handler", options)
    if not hasattr(shell, "register_sql_handler"):
        print("myflames: the SQL handler needs MySQL Shell %s or later "
              "(shell.register_sql_handler). Use myflames.explain() "
              "instead." % MIN_SHELL_VERSION_SQL_HANDLER)
        return False
    if not _handler_state["registered"]:
        shell.register_sql_handler(
            SQL_HANDLER_NAME,
            "Renders EXPLAIN [ANALYZE] FORMAT=MYFLAMES <statement> with "
            "myflames (flame graph HTML report).",
            list(SQL_HANDLER_PREFIXES), handle_sql)
        _handler_state["registered"] = True
    _handler_state["enabled"] = True
    _handler_state["options"] = {k: v for k, v in opts.items()
                                 if v is not None}
    print("myflames: SQL handler enabled. In SQL mode run "
          "EXPLAIN ANALYZE FORMAT=MYFLAMES SELECT ...")
    return True


def disable_handler():
    if not _handler_state["registered"]:
        print("myflames: the SQL handler is not enabled.")
        return False
    # MySQL Shell has no API to unregister an SQL handler, so the prefix
    # stays claimed and the handler refuses with a clear error.
    _handler_state["enabled"] = False
    print("myflames: SQL handler disabled for this Shell session.")
    return True


# ---------------------------------------------------------------------------
# Shell registration
# ---------------------------------------------------------------------------

@plugin
class myflames:
    """Visualize MySQL query plans with myflames.

    Captures EXPLAIN ANALYZE FORMAT=JSON for a statement on the current
    Shell session and renders it as a flame graph / Visual Explain HTML
    report, a token-cheap digest for LLMs, or ranked advisor findings.
    Requires the myflames package in Shell's Python, installed with
    mysqlsh --pym pip install --user myflames
    """


@plugin_function("myflames.explain", cli=True)
def explain(query, **options):
    """Renders the plan of a statement as an HTML report or SVG.

    Runs EXPLAIN ANALYZE FORMAT=JSON (or EXPLAIN FORMAT=JSON when analyze
    is false) on the current session, inside a READ ONLY transaction that
    is rolled back, and writes a myflames report. Output ending in .svg is
    written as a bare SVG chart; anything else is a self-contained HTML
    report.

    Args:
        query (str): The SELECT, WITH, TABLE or VALUES statement to explain.
        **options (dict): Optional arguments.

    Keyword Args:
        analyze (bool): Execute the statement with EXPLAIN ANALYZE.
            Default true. Set false for an estimated plan.
        output (str): Output file path. Default a new temporary .html file.
        type (str): View: flamegraph, bargraph, tree or workbench.
            Default flamegraph.
        open (bool): Open the output in the default browser. Default true.
    """
    run_explain(None, query, options)


@plugin_function("myflames.digest", cli=True)
def digest(query, **options):
    """Prints the token-cheap myflames digest of a statement's plan.

    The digest is compact text designed to be pasted into an LLM prompt.

    Args:
        query (str): The SELECT, WITH, TABLE or VALUES statement to explain.
        **options (dict): Optional arguments.

    Keyword Args:
        analyze (bool): Execute the statement with EXPLAIN ANALYZE.
            Default true. Set false for an estimated plan.
    """
    run_digest(None, query, options)


@plugin_function("myflames.advise", cli=True)
def advise(query, **options):
    """Prints ranked myflames advisor findings for a statement's plan.

    Args:
        query (str): The SELECT, WITH, TABLE or VALUES statement to explain.
        **options (dict): Optional arguments.

    Keyword Args:
        analyze (bool): Execute the statement with EXPLAIN ANALYZE.
            Default true. Set false for an estimated plan.
    """
    run_advise(None, query, options)


@plugin_function("myflames.enableSqlHandler")
def enable_sql_handler(**options):
    """Enables EXPLAIN [ANALYZE] FORMAT=MYFLAMES in SQL mode.

    Registers a MySQL Shell SQL handler (Shell 9.0.0 or later) so that
    EXPLAIN ANALYZE FORMAT=MYFLAMES <statement> and
    EXPLAIN FORMAT=MYFLAMES <statement> render the plan with myflames.
    Regular EXPLAIN statements are never intercepted.

    Args:
        **options (dict): Optional arguments.

    Keyword Args:
        type (str): View: flamegraph, bargraph, tree or workbench.
        open (bool): Open the report in the default browser. Default true.
    """
    enable_handler(options)


@plugin_function("myflames.disableSqlHandler")
def disable_sql_handler():
    """Disables the EXPLAIN ... FORMAT=MYFLAMES SQL handler."""
    disable_handler()
