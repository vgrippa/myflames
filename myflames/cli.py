"""
Unified CLI: flame graph, bar chart, tree, or Visual Explain from MySQL EXPLAIN ANALYZE JSON.
"""
import argparse
import os
import sys
from . import __version__
from .parser import (
    parse_explain,
    load_explain_json,
    format_sql,
    build_flame_entries,
    flatten_nodes,
    enhance_flame_titles,
    analyze_plan,
    render_info_panel,
)
from .flamegraph import folded_to_svg, plan_folded
from .output_bargraph import render_bargraph
from .output_diagram import render_diagram
from .output_tree import render_tree
from .teach_hooks import build_teach_hooks, build_teach_index_maps
from .server_info import detect_flavor


# ── View recommendation guide ────────────────────────────────────────────

_GUIDE_TEXT = """\
myflames — Which view should I use?

  flamegraph   Start here for total time distribution.
               Width = time.  See where the query spends most of its time
               across the full execution hierarchy.

  bargraph     Start here to find the slowest operators.
               Sorted by self-time.  Quickly spots the single most
               expensive step (scan, sort, join) in the plan.

  workbench    Visual Explain: join semantics, operator flow, and access paths.
               Large Venn join symbols; pan, smooth zoom, and search.
               Colors = operator types; arrow width = estimated rows.

  diagram      Compatibility alias for workbench (same Visual Explain view).

  tree         Start here for big, complex plans.
               Collapsible rows with self/total time.  Navigate deep
               plans without losing context.

Quick start:
  myflames sample.json                          # flame graph (default)
  myflames --type bargraph explain.json         # bar chart
  myflames --output report.html explain.json    # self-contained HTML report
  myflames compare before.json after.json       # before vs after diff
  myflames explore explain.json -o explore.html # interactive branch exploration
"""


def _resolve_query_text(args, json_text):
    """Resolve query text from CLI flags or embedded JSON."""
    query_text = ""
    if args.query:
        query_text = args.query.strip()
    elif args.query_file:
        try:
            with open(args.query_file, "r", encoding="utf-8") as qf:
                query_text = qf.read().strip()
        except (OSError, UnicodeError) as e:
            sys.stderr.write("Cannot read --query-file: %s\n" % e)
            sys.exit(2)
    if not query_text:
        try:
            raw_data = load_explain_json(json_text)
            query_text = (raw_data.get("query") or "").strip()
        except Exception:
            query_text = ""
    return query_text


def _make_svg_responsive(svg_text):
    """Add responsive styling + a ``viewBox`` to the root ``<svg>`` element.

    The flame graph / bar / diagram / tree renderers all emit
    SVGs with a fixed ``width="1800"`` (or similar). When those files are
    opened directly in a browser, they overflow the viewport horizontally
    and don't rescale on resize.

    The fix adds two things to the first ``<svg>`` element:

    * An inline ``style="max-width: 100%; height: auto;"`` so browsers
      clamp the width to the container AND recompute height from the
      scaled width. HTML-embedded SVGs already scale thanks to the rich
      template's CSS — this makes standalone ``.svg`` viewing match.

    * A ``viewBox="0 0 W H"`` attribute derived from the intrinsic
      width/height if one isn't already present. Without a viewBox, CSS
      ``max-width: 100%`` + ``height: auto`` can distort the aspect ratio
      because the browser has no intrinsic ratio to honour. The bargraph
      and some other renderers emit SVGs without a viewBox by default —
      this backfills it.

    Any pre-existing ``style=`` or ``viewBox=`` attribute is left alone.
    """
    import re as _re_local
    if not svg_text or "<svg" not in svg_text:
        return svg_text

    def _inject(match):
        tag = match.group(0)
        inner = tag[4:-1]  # strip leading "<svg" and trailing ">"
        # viewBox
        has_viewbox = "viewBox=" in inner
        if not has_viewbox:
            w_m = _re_local.search(r'\bwidth="([\d.]+)"', inner)
            h_m = _re_local.search(r'\bheight="([\d.]+)"', inner)
            if w_m and h_m:
                inner += ' viewBox="0 0 {} {}"'.format(w_m.group(1), h_m.group(1))
        # style — only inject when not already set so custom themes survive
        if 'style="' not in inner and "style='" not in inner:
            inner += ' style="max-width: 100%; height: auto;"'
        return "<svg" + inner + ">"

    return _re_local.sub(r'<svg\b[^>]*>', _inject, svg_text, count=1)


def _fail(message, password=None):
    """Report an expected failure on stderr and exit 2, even under --json.

    Failures do not produce a result payload or open the selected output file.
    Keep this shared by plan commands and workspace server commands so quiet,
    credential redaction and terminal escaping have the same error contract.
    """
    from .connector import terminal_text
    text = str(message)
    if password:
        text = text.replace(password, '[redacted]')
    sys.stderr.write('myflames: %s\n' % terminal_text(' '.join(text.splitlines())))
    raise SystemExit(2)


def _write_output(content, output_path, quiet=False):
    """Write content to file or stdout.

    SVG payloads are passed through :func:`_make_svg_responsive` first so
    standalone ``.svg`` files scale to the browser viewport instead of
    overflowing at the fixed renderer width.

    ``quiet`` suppresses the incidental ``Written to <path>`` diagnostic on
    stderr — the ``--quiet/-q`` contract shared across subcommands. It never
    affects the stdout data path (there is no chatter to suppress there).
    """
    if content and content.lstrip().startswith(("<?xml", "<svg")):
        content = _make_svg_responsive(content)
    if output_path:
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            _fail("Cannot write %s: %s" % (output_path, e))
        if not quiet:
            sys.stderr.write("Written to %s\n" % output_path)
    else:
        try:
            sys.stdout.write(content)
            sys.stdout.flush()
        except OSError as e:
            # A closed pipe (BrokenPipeError) or a full disk: an output
            # failure under the 0/1/2 contract, exit 2 with a diagnostic.
            _discard_stdout()
            _fail("Cannot write to standard output: %s" % (e.strerror or e))


def _discard_stdout():
    """Point stdout at the null device after a failed write, so the
    interpreter's final flush cannot raise a second, unhandled error."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def _add_quiet_flag(parser):
    """Register the ``--quiet/-q`` flag with identical wording on every
    subcommand that emits stderr diagnostics. Keeping one definition here is
    what makes the flag mean the same thing across ``check``, ``digest``,
    ``advise``, and ``compare``: suppress the stderr chatter, never the stdout
    data (the result is the stdout payload and/or the exit code).
    """
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress incidental stderr diagnostics; stdout data and the exit "
             "code are unaffected.",
    )


def _maybe_write_teach_bundle(args, output_path):
    """Auto-emit the myteach catalog + every lesson into
    ``<output_dir>/teach/`` so the report's myteach section + the
    glossary "Learn → " chips have files to link to.

    Idempotent: if ``<output_dir>/teach/index.html`` already exists,
    skip everything (use ``--refresh-teach-bundle`` to force regen
    after a myflames upgrade). Disabled entirely with
    ``--no-teach-bundle`` for tests / CI / disk-constrained shells.

    Cost: ~21 lesson HTML files (~70 KB each) + 1 catalog index
    (~5 KB) = ~1.5 MB on first emission per directory. Subsequent
    runs into the same directory are no-ops.

    Errors during bundle emission never abort the main report —
    they're logged to stderr and the report still ships.
    """
    if getattr(args, "no_teach_bundle", False):
        return
    if not output_path or output_path == "-":
        return
    if not output_path.lower().endswith(".html"):
        return  # SVG / tree-text outputs don't reference teach links

    out_dir = os.path.dirname(os.path.abspath(output_path))
    teach_dir = os.path.join(out_dir, "teach")
    index_path = os.path.join(teach_dir, "index.html")

    refresh = getattr(args, "refresh_teach_bundle", False)
    if not refresh and os.path.exists(index_path):
        return  # Already populated.

    try:
        from .teach import LESSONS, render_lesson, render_catalog_html
    except ImportError as e:
        sys.stderr.write(
            "WARN: skipping teach bundle (import failed): {}\n".format(e)
        )
        return

    try:
        os.makedirs(teach_dir, exist_ok=True)
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(render_catalog_html())
        wrote = 1
        for slug in LESSONS:
            lesson_path = os.path.join(teach_dir, slug + ".html")
            if not refresh and os.path.exists(lesson_path):
                continue
            with open(lesson_path, "w", encoding="utf-8") as f:
                f.write(render_lesson(slug))
            wrote += 1
        # Status is incidental; output failures remain visible under -q.
        if not getattr(args, "quiet", False):
            sys.stderr.write(
                "Teach bundle: {} files in {}{}\n".format(
                    wrote, teach_dir,
                    " (refreshed)" if refresh else "",
                )
            )
    except OSError as e:
        sys.stderr.write(
            "WARN: teach bundle emission failed: {} (report itself was "
            "written successfully)\n".format(e)
        )


def _maybe_write_sidecar(args, root, analysis, json_text, live_artifacts, query_text):
    """Write a JSON sidecar alongside the current output, if applicable.

    Keeps the CLI entry point readable by isolating all of the precedence
    logic (explicit --sidecar path vs derived from --output vs suppressed)
    in one place. Explicit output failures exit 2; automatically derived
    auxiliary exports remain best-effort.
    """
    if args.no_sidecar:
        return
    sidecar_path = args.sidecar
    if sidecar_path == "-":
        return  # explicit suppression
    if not sidecar_path:
        # Auto-derive from --output
        if not args.output:
            return
        from .output_sidecar import sidecar_path_for
        sidecar_path = sidecar_path_for(args.output)
        if not sidecar_path:
            return

    # Figure out engine metadata from whichever path fed us the plan.
    engine = None
    engine_version = None
    source_type = "file"
    fixture_path = None
    if live_artifacts:
        source_type = "live"
        variables = live_artifacts.get("variables") or {}
        version_str = variables.get("version") or ""
        if detect_flavor(version_str) == "mariadb":
            engine = "mariadb"
        elif version_str:
            engine = "mysql"
        else:
            engine = _plan_engine(root, live_artifacts.get("capture"))
        if version_str:
            engine_version = version_str
    elif args.input and args.input != "-":
        fixture_path = args.input
        engine = _plan_engine(root)
    else:
        source_type = "stdin"

    from .output_sidecar import build_sidecar, write_sidecar
    from .parser import format_sql
    query_beautified = None
    if query_text:
        lines = format_sql(query_text)
        if lines:
            query_beautified = "\n".join(lines)

    try:
        payload = build_sidecar(
            root,
            analysis,
            source_type=source_type,
            engine=engine,
            engine_version=engine_version,
            fixture_path=fixture_path,
            query_raw=query_text or None,
            query_beautified=query_beautified,
        )
        write_sidecar(sidecar_path, payload)
        if not getattr(args, "quiet", False):
            sys.stderr.write("Sidecar: {}\n".format(sidecar_path))
    except Exception as e:
        sys.stderr.write("WARN: sidecar emission failed: {}\n".format(e))
        if args.sidecar:
            sys.exit(2)


def _run_live_explain(args):
    """Connect to a live server and run the full collection pipeline.

    Returns ``(explain_json_text, live_artifacts_dict)``. On any failure
    (connection refused, auth error, bad SQL) the error is written to
    stderr and the process exits 2 — we do not try to degrade to a
    file-mode invocation.

    Flow:
      1. Open :class:`MySQLConnection` from the parsed args.
      2. ``EXPLAIN ANALYZE FORMAT=JSON`` on ``args.execute``.
      3. If enabled, collect schema / stats for the referenced tables.
      4. If enabled, collect the session variables the advisor inspects.
      5. Return everything so main() can hand it to analyze_plan + advise.
    """
    from .connector import MySQLConnection, ConnectorError, terminal_text
    from .live import bind_query
    from .dbops import redact, WRITE_CLIENT_FLAGS
    from .collectors import (
        extract_table_names,
        collect_schema,
        collect_stats,
        collect_session_variables,
    )

    if not args.execute:
        sys.stderr.write(
            "Live-connection mode needs a query: pass -e 'SELECT ...' "
            "or --execute.\n"
        )
        sys.exit(2)

    # Validate before opening a connection or asking for credentials. This is
    # the same SELECT-only, two-backslash-readings guard used by capture.
    try:
        query = bind_query(args.execute)
    except ValueError as exc:
        sys.stderr.write("EXPLAIN ANALYZE refused: {}\n".format(terminal_text(redact(exc, args.password))))
        sys.exit(2)

    from .workspace_cli import _resolve_connection, _connection_options
    password = None if args.password == "__PROMPT__" else args.password
    try:
        settings = _resolve_connection(args)
        password = settings.get("password")
        options = _connection_options(settings)
        # Match the legacy renderer's client defaults rather than capture's
        # shorter per-query deadline.
        options.pop("query_timeout", None)
        options["connect_timeout"] = 10
        conn = MySQLConnection(**options)
        args.database = settings.get("database")
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\nAborted.\n")
        sys.exit(2)
    except (ConnectorError, ValueError, OSError) as exc:
        sys.stderr.write("Connection failed: {}\n".format(terminal_text(redact(exc, password))))
        sys.exit(2)

    artifacts = {"schema": {}, "stats": {}, "variables": {}, "freshness": {}}
    try:
        with conn:
            maria = conn.is_mariadb()
            prefix = "" if maria else "SET SESSION explain_json_format_version=2;\n"
            # One client/session, stopping at the first error, without reconnect:
            # a failed guard must never fall through to an unguarded ANALYZE.
            script = (prefix + "SET SESSION TRANSACTION READ ONLY;\nSTART TRANSACTION READ ONLY;\n"
                      + ("ANALYZE FORMAT=JSON " if maria else "EXPLAIN ANALYZE FORMAT=JSON ")
                      + query + "\n;\nROLLBACK;\n")
            json_text = conn.run_stdin(script, extra_flags=["--raw"] + list(WRITE_CLIENT_FLAGS))

            tables = extract_table_names(args.execute, default_schema=args.database)

            if not args.no_collect_schema and tables:
                try:
                    artifacts["schema"] = collect_schema(conn, tables)
                except ConnectorError as e:
                    sys.stderr.write("Schema collection failed: {}\n".format(terminal_text(redact(e, password))))
            if not args.no_collect_stats and tables:
                try:
                    artifacts["stats"] = collect_stats(conn, tables)
                except ConnectorError as e:
                    sys.stderr.write("Stats collection failed: {}\n".format(terminal_text(redact(e, password))))
                try:
                    from .collectors import collect_stats_freshness
                    artifacts["freshness"] = collect_stats_freshness(
                        conn, tables, mariadb=conn.is_mariadb())
                except ConnectorError as e:
                    sys.stderr.write("Statistics freshness collection failed: {}\n".format(terminal_text(redact(e, password))))
            if not args.no_collect_variables:
                try:
                    artifacts["variables"] = collect_session_variables(conn)
                except ConnectorError as e:
                    sys.stderr.write("Variables collection failed: {}\n".format(terminal_text(redact(e, password))))
    except (ConnectorError, ValueError, OSError) as exc:
        sys.stderr.write("EXPLAIN ANALYZE failed: {}\n".format(terminal_text(redact(exc, password))))
        sys.exit(2)

    return json_text, artifacts


def _cmd_compare(argv, alias_warning=None):
    """Run before-vs-after comparison."""
    parser = argparse.ArgumentParser(
        prog="myflames compare",
        description="Generate a before-vs-after comparison HTML report.",
    )
    parser.add_argument("before", help="EXPLAIN JSON file (before)")
    parser.add_argument("after", help="EXPLAIN JSON file (after)")
    parser.add_argument(
        "--title", default="Query Plan Comparison",
        help="Report title (default: Query Plan Comparison)",
    )
    form = parser.add_mutually_exclusive_group()
    form.add_argument(
        "--digest", action="store_true",
        help="Emit a compact text diff (token-cheap; paste into an LLM) instead of the HTML report.",
    )
    form.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the structured compare-1.0 diff as JSON instead of the HTML report.",
    )
    parser.add_argument(
        "--output", "-o", default=None, metavar="PATH",
        help="Write output to file instead of stdout",
    )
    _add_quiet_flag(parser)
    args = _parse_alias_args(parser, argv, alias_warning)

    json_before = _read_plan_source(args.before, quiet=args.quiet)[0]
    json_after = _read_plan_source(args.after, quiet=args.quiet)[0]
    _parse_explain_input(json_before)
    _parse_explain_input(json_after)

    if args.digest or args.as_json:
        from .output_compare_sidecar import build_compare_sidecar
        sidecar = build_compare_sidecar(json_before, json_after)
        if args.as_json:
            import json as _json
            _write_output(_json.dumps(sidecar, indent=2), args.output, quiet=args.quiet)
        else:
            from .digest import build_compare_digest
            _write_output(build_compare_digest(sidecar), args.output, quiet=args.quiet)
        return

    from .output_compare import render_compare
    html = render_compare(json_before, json_after, title=args.title)
    _write_output(html, args.output, quiet=args.quiet)


def _plan_engine(root, capture=None):
    """The plan's engine: the one a capture recorded from the server, else
    the canonical parser's (``parse_explain`` tells a MySQL format-1
    query_block plan from a MariaDB one). Never detected again from the
    JSON envelope."""
    recorded = (capture or {}).get("engine")
    if recorded in ("mysql", "mariadb"):
        return recorded
    return (root or {}).get("engine") or "unknown"


def _read_plan_source(input_path, quiet=False):
    """Read a plan file, stdin, a ``capture-1.0`` file, or a MySQL Shell
    diagnostics bundle.

    Returns ``(plan_json_text, capture)``. *capture* is the ``capture-1.0``
    dict (from ``myflames capture``, the workspace, or a
    ``util.debug.collectSlowQueryDiagnostics()`` zip, first query), or
    ``None`` for plain plan input. Import notices go to stderr unless
    *quiet*; an unusable bundle or capture exits 2.
    """
    if input_path != "-":
        from .shell_import import is_shell_diagnostics_zip, load_shell_diagnostics
        if is_shell_diagnostics_zip(input_path):
            try:
                capture = load_shell_diagnostics(input_path)
            except ValueError as e:
                _fail("Cannot import %s: %s" % (input_path, e))
            if not quiet:
                for notice in capture.get("warnings") or []:
                    sys.stderr.write("Note: %s\n" % notice)
            return capture["raw"], capture
    text = _read_explain_input(input_path)
    capture = _capture_from_text(text, input_path)
    if capture is not None:
        return capture["raw"], capture
    return text, None


def _capture_from_text(text, input_path):
    """Return the validated capture when *text* is a ``capture-1.0`` document."""
    try:
        data = load_explain_json(text)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema_version") != "capture-1.0":
        return None
    from .live import normalize_capture
    try:
        capture = normalize_capture(data)
    except (ValueError, TypeError) as e:
        _fail("Cannot read capture %s: %s" % (input_path, e))
    if not capture.get("raw"):
        _fail("Capture %s contains no plan." % input_path)
    return capture


def _capture_artifacts(capture):
    """Advisor inputs (``live_artifacts`` shape) carried by an imported capture."""
    return {
        "schema": capture.get("schema") or {},
        "stats": capture.get("table_stats") or {},
        "variables": capture.get("variables") or {},
        "freshness": capture.get("stats_freshness") or {},
        "capture": capture,
    }


def _read_explain_input(input_path):
    """Read a plan, reporting expected input failures without a traceback.

    A MySQL Shell diagnostics bundle is accepted too; its plan is returned.
    """
    if input_path == "-":
        return sys.stdin.read()
    from .shell_import import is_shell_diagnostics_zip
    if is_shell_diagnostics_zip(input_path):
        return _read_plan_source(input_path)[0]
    try:
        with open(input_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        _fail("Cannot read %s: %s" % (input_path, e))


def _parse_explain_input(raw_text):
    try:
        data = load_explain_json(raw_text)
        plan = data.get("query_plan", data) if isinstance(data, dict) else data
        from .parser import _is_mariadb_format
        if not isinstance(plan, dict) or not ("operation" in plan or _is_mariadb_format(plan)):
            # Any JSON object would otherwise parse as one "unknown" node,
            # turning a wrong file into a clean CI gate.
            raise ValueError("this is not an EXPLAIN FORMAT=JSON or ANALYZE FORMAT=JSON plan "
                             "(or a myflames capture file)")
        return parse_explain(raw_text)
    except Exception as e:
        _fail("Failed to parse EXPLAIN JSON: %s" % e)


def _sidecar_from_input(input_path, quiet=False):
    """Read, parse, analyze and serialize input for digest / check / advise.

    A MySQL Shell bundle's DDL, statistics, and variables feed the advisor,
    so these commands agree with the report rendered from the same zip.
    """
    raw_text, capture = _read_plan_source(input_path, quiet=quiet)
    root = _parse_explain_input(raw_text)
    analysis = analyze_plan(root)
    if capture:
        from .advisor import advise
        artifacts = _capture_artifacts(capture)
        advise(analysis, schema=artifacts["schema"], stats=artifacts["stats"],
               variables=artifacts["variables"], freshness=artifacts["freshness"],
               capture=artifacts["capture"])
    engine = _plan_engine(root, capture)
    version = (capture or {}).get("server_version")
    from .output_sidecar import build_sidecar
    payload = build_sidecar(
        root, analysis,
        source_type="stdin" if input_path == "-" else "file",
        engine=engine,
        engine_version=version if isinstance(version, str) and version else None,
        fixture_path=input_path if input_path != "-" else None,
    )
    return raw_text, payload


def _add_rule_flags(parser):
    """``--ignore`` and ``--list-rules``, shared by ``check`` and ``advise``."""
    parser.add_argument(
        "--ignore", default="", metavar="LIST",
        help="Comma-separated categories to drop before evaluating "
             "(see --list-rules), e.g. 'hash_join,env'.",
    )
    parser.add_argument(
        "--list-rules", action="store_true",
        help="Print every rule category with its documentation link and exit.",
    )


def _maybe_list_rules(args, parser):
    """Handle ``--list-rules`` (exit 0) and require PLAN otherwise (exit 2)."""
    if args.list_rules:
        from .findings import format_rule_catalog, rule_catalog
        if getattr(args, "as_json", False):
            import json as _json
            content = _json.dumps(rule_catalog(), indent=2) + "\n"
        else:
            content = format_rule_catalog()
        _write_output(content, getattr(args, "output", None), quiet=args.quiet)
        sys.exit(0)
    if args.input is None:
        parser.error("the following arguments are required: input")


def _apply_ignore_or_exit(payload, ignore):
    """Drop ``--ignore`` categories; exit 2 on a misspelled category so a typo
    never silently disables a gate."""
    from .findings import apply_ignore, unknown_ignores, known_ignores
    tokens = ignore.split(",") if ignore else []
    bad = unknown_ignores(tokens)
    if bad:
        _fail("error: unknown --ignore categor{}: {}; valid categories: {}".format(
            "y" if len(bad) == 1 else "ies", ", ".join(bad),
            ", ".join(sorted(known_ignores()))))
    return apply_ignore(payload, tokens)


def _cmd_import_shell(argv):
    """Convert a MySQL Shell diagnostics bundle into ``capture-1.0`` JSON."""
    parser = argparse.ArgumentParser(
        prog="myflames import-shell",
        description="Convert a MySQL Shell util.debug.collectSlowQueryDiagnostics() "
                    "zip into capture JSON for the workspace and the other commands.",
        epilog="Every plan-reading command also accepts the zip directly and uses "
               "its first query.",
    )
    parser.add_argument("bundle", metavar="BUNDLE", help="Diagnostics .zip written by MySQL Shell.")
    parser.add_argument("--query", type=int, default=None, metavar="N",
                        help="Import query N when the zip holds several bundles (default: 0).")
    parser.add_argument("--list", action="store_true",
                        help="List the queries in the zip and exit.")
    parser.add_argument("--output", "-o", default=None, metavar="PATH",
                        help="Write capture JSON to a file instead of stdout.")
    _add_quiet_flag(parser)
    args = parser.parse_args(argv)
    from .shell_import import (is_shell_diagnostics_zip, list_shell_diagnostics_queries,
                               load_shell_diagnostics)
    from .live import normalize_capture
    import json as _json
    try:
        if not is_shell_diagnostics_zip(args.bundle):
            raise ValueError("this is not a MySQL Shell diagnostics zip. Create one with "
                             "util.debug.collectSlowQueryDiagnostics().")
        if args.list:
            content = "".join("{}\t{}\t{}\n".format(
                q["index"], q["prefix"], " ".join((q.get("query") or "").split())[:160])
                for q in list_shell_diagnostics_queries(args.bundle))
            _write_output(content, args.output, quiet=args.quiet)
            return
        capture = load_shell_diagnostics(args.bundle, query_index=args.query)
        result = normalize_capture(capture)
        for key in ("source", "attachments", "stats_freshness"):
            if capture.get(key):
                result[key] = capture[key]
        from . import __version__
        result["myflames_version"] = __version__
    except (ValueError, OSError) as e:
        sys.stderr.write("Cannot import %s: %s\n" % (args.bundle, e))
        sys.exit(2)
    if not args.quiet:
        for notice in result.get("warnings") or []:
            sys.stderr.write("Note: %s\n" % notice)
    _write_output(_json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                  args.output, quiet=args.quiet)


def _cmd_check(argv):
    """CI gate: exit nonzero if the plan trips any --fail-on trigger.

    Exit codes: 0 = clean, 1 = a trigger matched, 2 = bad input/usage.
    """
    parser = argparse.ArgumentParser(
        prog="myflames check",
        description="Exit nonzero if a plan trips --fail-on categories. For CI and agent loops.",
    )
    parser.add_argument("input", nargs="?", help="EXPLAIN ANALYZE FORMAT=JSON file (or '-' for stdin)")
    parser.add_argument(
        "--fail-on", default="any", metavar="LIST",
        help="Comma-separated triggers: warning categories (see --list-rules), "
             "severities (error, warn, info), or 'any' (default).",
    )
    _add_rule_flags(parser)
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Suppress the per-match output; rely on the exit code.",
    )
    args = parser.parse_args(argv)
    _maybe_list_rules(args, parser)

    _raw, payload = _sidecar_from_input(args.input, quiet=args.quiet)
    from .findings import evaluate_check, unknown_triggers, known_triggers
    payload = _apply_ignore_or_exit(payload, args.ignore)
    triggers = args.fail_on.split(",")
    # Reject a misspelled trigger up front: an unrecognized token would match
    # nothing and silently exit 0 (clean), turning a typo into a green CI gate.
    bad = unknown_triggers(triggers)
    if bad:
        sys.stderr.write(
            "error: unknown --fail-on trigger(s): {}\n"
            "valid triggers: {}\n".format(
                ", ".join(bad), ", ".join(sorted(known_triggers()))))
        sys.exit(2)
    matched = evaluate_check(payload, triggers)

    if not matched:
        # 'check' has no data payload — its result is the exit code, so status
        # goes to stderr and stdout stays clean for piping.
        if not args.quiet:
            sys.stderr.write("OK: no findings matched --fail-on {}\n".format(args.fail_on))
        return
    if not args.quiet:
        sys.stderr.write("FAIL: {} finding(s) matched --fail-on {}:\n".format(len(matched), args.fail_on))
        for w in matched:
            sys.stderr.write("  [{}/{}] {}\n".format(
                w.get("severity"), w.get("category"), w.get("text", "").strip()))
    sys.exit(1)


def _cmd_advise(argv, alias_warning=None):
    """Emit a ranked list of advisor findings (warnings + suggestions) with confidence."""
    parser = argparse.ArgumentParser(
        prog="myflames advise",
        description="Ranked warnings + suggestions with confidence, for agents and triage.",
    )
    parser.add_argument("input", nargs="?", help="EXPLAIN ANALYZE FORMAT=JSON file (or '-' for stdin)")
    _add_rule_flags(parser)
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit findings as machine-readable JSON.",
    )
    parser.add_argument(
        "--output", "-o", default=None, metavar="PATH",
        help="Write output to a file instead of stdout.",
    )
    _add_quiet_flag(parser)
    args = _parse_alias_args(parser, argv, alias_warning)
    _maybe_list_rules(args, parser)

    _raw, payload = _sidecar_from_input(args.input, quiet=args.quiet)
    from .findings import build_findings, summarize
    findings = build_findings(_apply_ignore_or_exit(payload, args.ignore))

    if args.as_json:
        import json as _json
        _write_output(_json.dumps(findings, indent=2) + "\n", args.output, quiet=args.quiet)
        return

    if not findings:
        _write_output("No findings: the plan looks clean.\n", args.output, quiet=args.quiet)
        return
    lines = []
    for f in findings:
        line = "[{}/{}/{}] {}".format(
            f["severity"], f["category"], f["confidence"], f["text"].strip())
        if f.get("why"):
            line += "\n    Why: " + f["why"].strip()
        if f.get("node_labels"):
            line += "\n    @ " + ", ".join(f["node_labels"])
        if f.get("docs"):
            line += "\n    Docs: " + f["docs"]
        lines.append(line)
    parts = []
    for kind, counts in sorted(summarize(findings).items(), reverse=True):
        total = sum(counts.values())
        parts.append("{} {} ({})".format(
            total, kind if total != 1 else kind[:-1],
            ", ".join("{} {}".format(n, sev) for sev, n in sorted(counts.items()))))
    lines.append("Summary: " + "; ".join(parts))
    _write_output("\n".join(lines) + "\n", args.output, quiet=args.quiet)


def _cmd_digest(argv, alias_warning=None):
    """Emit the compact, LLM-ready digest of a query plan.

    The digest distils a verbose ``EXPLAIN ANALYZE FORMAT=JSON`` plan into the
    few facts a person or agent needs (summary, warnings, fixes, plan skeleton),
    at a fraction of the tokens. ``--cost`` shows that token/$ saving vs the raw
    plan instead of the digest itself.
    """
    parser = argparse.ArgumentParser(
        prog="myflames digest",
        description="Emit the compact, LLM-ready digest of a query plan (--cost shows the token/$ saving vs the raw plan).",
    )
    parser.add_argument("input", help="EXPLAIN ANALYZE FORMAT=JSON file (or '-' for stdin)")
    parser.add_argument(
        "--cost", action="store_true",
        help="Show the token + $ saving of the digest vs pasting the raw plan, instead of the digest text.",
    )
    form = parser.add_mutually_exclusive_group()
    form.add_argument(
        "--show-prompts", action="store_true", dest="show_prompts",
        help="Print both prompts (raw plan vs digest) side by side, so you can see exactly what's compared.",
    )
    form.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the savings comparison as machine-readable JSON (implies --cost).",
    )
    parser.add_argument(
        "--tokenizer", choices=["heuristic", "claude", "gpt"], default=None,
        help="How to count tokens for --cost/--json: 'heuristic' (offline default, no key/network), 'claude' (Anthropic count_tokens; needs ANTHROPIC_API_KEY), or 'gpt' (tiktoken; exact for GPT, keyless; needs `pip install 'myflames[gpt]'`). Falls back to the heuristic if unavailable.",
    )
    parser.add_argument(
        "--model", default=None, metavar="MODEL_ID",
        help="Pricing/counting model id (default: claude-sonnet-4-6). One of: claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5.",
    )
    parser.add_argument(
        "--output", "-o", default=None, metavar="PATH",
        help="Write output to a file instead of stdout.",
    )
    _add_quiet_flag(parser)
    args = _parse_alias_args(parser, argv, alias_warning)

    from . import digest as dg
    raw_text, payload = _sidecar_from_input(args.input, quiet=args.quiet)
    digest_text = dg.build_digest(payload)

    # Default mode: emit the digest text. --cost / --json / --show-prompts switch
    # to the savings comparison or the prompt dump.
    if not (args.cost or args.as_json or args.show_prompts):
        _write_output(digest_text, args.output, quiet=args.quiet)
        return

    pricing_model = args.model or dg.DEFAULT_PRICING_MODEL
    raw_prompt = dg.build_raw_prompt(raw_text)
    digest_prompt = dg.build_digest_prompt(digest_text)

    if args.show_prompts:
        both = (
            "===== BEFORE (raw plan + question) =====\n" + raw_prompt
            + "\n===== AFTER (myflames digest + question) =====\n" + digest_prompt + "\n"
        )
        _write_output(both, args.output, quiet=args.quiet)
        return

    choice = args.tokenizer or "heuristic"
    count_fn, method = dg.make_counter(model=pricing_model, tokenizer=choice)
    if choice != "heuristic" and method.startswith("heuristic") and not args.quiet:
        # An exact tokenizer was requested but couldn't be satisfied; say so on
        # stderr (stdout stays clean data) with the specific reason, then
        # continue with the estimate. --quiet suppresses this diagnostic.
        reason = method[len("heuristic estimate "):].strip("() ") or "unavailable"
        sys.stderr.write(
            "Note: exact counts unavailable ({}); showing the offline estimate.\n".format(reason)
        )
    comparison = dg.compare(raw_prompt, digest_prompt, count=count_fn, method=method)
    if args.as_json:
        import json as _json
        _write_output(_json.dumps(comparison, indent=2) + "\n", args.output, quiet=args.quiet)
    else:
        _write_output(dg.format_report(comparison, pricing_model=pricing_model), args.output, quiet=args.quiet)


def _warn_alias(args, message):
    """Use the command's parsed quiet flag, including argparse abbreviations."""
    if not args.quiet:
        sys.stderr.write(message)


def _parse_alias_args(parser, argv, message=None):
    # Pass the namespace explicitly so help still warns when argparse exits
    # before returning. Successful runs use the complete, validated namespace.
    args = argparse.Namespace(quiet=False)
    try:
        return parser.parse_args(argv, namespace=args)
    finally:
        if message:
            _warn_alias(args, message)


def _cmd_guide(argv):
    parser = argparse.ArgumentParser(prog="myflames guide", description="Show which visualization to choose.")
    parser.add_argument("-o", "--output", metavar="PATH", help="Write the guide to a file instead of stdout.")
    _add_quiet_flag(parser)
    args = parser.parse_args(argv)
    _write_output(_GUIDE_TEXT, args.output, quiet=args.quiet)


def _cmd_tokens_deprecated(argv):
    """Deprecated alias for `digest`. Translates the old flag set and preserves
    the old default (the savings comparison) so existing invocations still work."""
    warning = (
        "warning: 'tokens' is deprecated; use 'digest' (note: 'digest' emits the "
        "digest by default — use --cost for the savings report). 'tokens' will be "
        "removed in a future release.\n"
    )
    had_mode = any(a in ("--digest", "--json", "--show") for a in argv)
    translated = []
    for a in argv:
        if a == "--show":
            translated.append("--show-prompts")
        elif a == "--exact":
            translated += ["--tokenizer", "claude"]
        elif a == "--digest":
            continue  # digest is now the default mode
        else:
            translated.append(a)
    if not had_mode:
        translated.append("--cost")  # old bare `tokens` defaulted to the comparison
    _cmd_digest(translated, alias_warning=warning)


def _cmd_findings_deprecated(argv):
    """Deprecated alias for `advise`."""
    _cmd_advise(argv, alias_warning=(
        "warning: 'findings' is deprecated; use 'advise'. 'findings' will be "
        "removed in a future release.\n"
    ))


def main():
    # Handle subcommands before argparse to avoid conflicts with positional args
    if len(sys.argv) > 1:
        if sys.argv[1] == "sql":
            from .sql_cli import cmd_sql
            cmd_sql(sys.argv[2:])
            return
        if sys.argv[1] in ("capture", "explore", "top", "sessions", "running", "locks", "innodb", "error-log", "health",
                           "waits", "indexes", "tables", "io", "config", "instruments", "report", "profiles",
                           "audit", "replication", "cluster"):
            from . import workspace_cli
            command = getattr(workspace_cli, "cmd_" + sys.argv[1].replace("-", "_"))
            command(sys.argv[2:])
            return
        if sys.argv[1] in ("privileges", "accounts"):
            from . import accounts_cli
            getattr(accounts_cli, "cmd_" + sys.argv[1])(sys.argv[2:])
            return
        if sys.argv[1] in ("upgrade-check", "dump", "restore", "clone"):
            from . import lifecycle_cli
            getattr(lifecycle_cli, "cmd_" + sys.argv[1].replace("-", "_"))(sys.argv[2:])
            return
        if sys.argv[1] == "ui":
            from .ui import cmd_ui
            cmd_ui(sys.argv[2:])
            return
        if sys.argv[1] == "guide":
            _cmd_guide(sys.argv[2:])
            return
        if sys.argv[1] in ("compare", "diff"):
            warning = "warning: 'diff' is deprecated; use 'compare'.\n" if sys.argv[1] == "diff" else None
            _cmd_compare(sys.argv[2:], alias_warning=warning)
            return
        if sys.argv[1] == "teach":
            from .teach import cmd_teach
            cmd_teach(sys.argv[2:])
            return
        if sys.argv[1] == "digest":
            _cmd_digest(sys.argv[2:])
            return
        if sys.argv[1] == "tokens":  # deprecated alias for digest
            _cmd_tokens_deprecated(sys.argv[2:])
            return
        if sys.argv[1] == "check":
            _cmd_check(sys.argv[2:])
            return
        if sys.argv[1] == "advise":
            _cmd_advise(sys.argv[2:])
            return
        if sys.argv[1] == "findings":  # deprecated alias for advise
            _cmd_findings_deprecated(sys.argv[2:])
            return
        if sys.argv[1] == "import-shell":
            _cmd_import_shell(sys.argv[2:])
            return

    parser = argparse.ArgumentParser(
        prog="myflames",
        description="MySQL EXPLAIN ANALYZE visualizer: flame graphs, bar charts, diagrams, execution trees, and Workbench-style plans.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # add_help=False so the short flag ``-h`` is free for ``--host`` — we
        # add our own ``--help`` below. ``mysql -h host`` users expect this.
        add_help=False,
        epilog="""\
Examples:
  myflames ui                                           # local browser workspace (deprecated; use desktop app)
  myflames explore explain.json -o explore.html         # interactive branch exploration
  myflames capture -h localhost -u admin -p -e 'SELECT 1' # capture estimates as JSON
  myflames top -h localhost -u admin -p --order avg     # heaviest statements (P_S)
  myflames sessions -h localhost -u admin -p            # connections, idle-in-trx, lock waits
  myflames sessions -h localhost -u admin -p --explain 42 | myflames - > plan.svg
  myflames innodb -h localhost -u admin -p --fail-on problem # SHOW ENGINE INNODB STATUS, parsed
  myflames error-log -h localhost -u admin -p --fail-on watch # Recent error log entries
  myflames locks -h localhost -u admin -p --waits-only   # who blocks whom (read-only)
  myflames locks -h localhost -u admin -p --kill 42      # end one blocker after you confirm
  myflames health -h localhost -u admin -p --interval 10 # server rates + statuses (no P_S)
  myflames waits -h localhost -u admin -p               # top wait events over 10 s (P_S)
  myflames indexes -h localhost -u admin -p --schema shop # unused/redundant indexes
  myflames tables -h localhost -u admin -p --fail-on problem # no primary key, free space
  myflames io -h localhost -u admin -p --schema shop    # table and file I/O hot spots
  myflames config -h localhost -u admin -p --fail-on problem # settings review (CI gate)
  myflames instruments -h localhost -u admin -p --preset minimal # P_S setup SQL to run
  myflames report -h localhost -u admin -p --html -o report.html # full analysis of the server
  myflames replication --profile replica                # channels, lag caveats, skip plans
  myflames cluster --profile node1 --fail-on problem    # Galera, PXC, InnoDB Cluster, Group Replication
  myflames upgrade-check -h localhost -u admin -p --target-version 8.4.0 # before an upgrade
  myflames dump /backups/shop -h localhost -u admin -p --schema shop # dump with a verified manifest
  myflames restore /backups/shop -h staging -u admin -p # restore after typing the target
  myflames clone -h replica -u admin -p --donor-host primary --donor-user cloner --donor-password
  myflames top -h localhost -u admin -p --order full_join # statements behind joins without an index
  myflames capture --profile prod -e 'SELECT 1'       # reuse a saved profile + password
  myflames profiles list                                # saved connection profiles
  myflames profiles set prod --type production --read-only # guard writes on a connection
  myflames audit --since 7                              # writes sent, refused or declined
  myflames --type workbench explain.json -o plan.html   # Workbench-style Visual Explain
  myflames explain.json                                 # flame graph from file
  myflames --type bargraph explain.json > bar.svg       # bar chart
  myflames --output report.html explain.json            # HTML report
  myflames -h db.example.com -u admin -p \\\
           -e 'SELECT * FROM t WHERE id=1'              # connect + explain
  myflames compare before.json after.json               # before vs after (HTML)
  myflames compare before.json after.json --digest      # token-cheap text diff
  myflames digest explain.json                          # compact LLM-ready digest
  myflames digest explain.json --cost                   # tokens + $ saved vs raw plan
  myflames check explain.json --fail-on full_scan       # CI gate (exit code)
  myflames advise explain.json --json                   # ranked findings for agents
  myflames check --list-rules                           # rule categories + docs links
  myflames import-shell diagnostics.zip -o capture.json # MySQL Shell bundle -> capture
  myflames guide                                        # which view?
  myflames teach btree -o btree.html                    # interactive lesson

Subcommands:
  ui        Open the local Performance workspace in a browser (deprecated; use desktop app)
  sql       Run SQL from -e, -f or stdin in one Query editor session (--format, --yes)
  capture   Capture a live plan and server evidence as JSON (--mode analyze executes)
  explore   Export interactive HTML with branch focus and operator metrics
  top       List the heaviest statements from Performance Schema digests
  sessions  Every connection: summary, idle in transaction, lock waits; --explain ID prints a plan
  running   Deprecated alias of sessions (statements running now; --explain ID)
  locks     Blocking tree, waiting DDL, transactions, row/metadata locks, deadlock; --kill ID
  innodb    SHOW ENGINE INNODB STATUS parsed: purge, redo, buffer pool, deadlock (--fail-on)
  error-log Recent error log entries and current-run counts (--level, --limit, --fail-on)
  health    Sample global status twice: diagnostics and rates, ok/watch/problem (--fail-on)
  waits     Top wait events over a window, by class, since start; uninstrumented classes
  indexes   Unused and redundant indexes, tables read mostly by full scans (--schema)
  tables    Tables without a primary key or usable unique key; InnoDB free space (--fresh)
  io        Tables and files with the most I/O over a window, and since server start
  config    Review key settings: engine default, source, ok/watch/problem (--fail-on);
            config set / reset-persist change one variable after you confirm
  instruments  Performance Schema instruments/consumers: gain, cost, SQL to run (--preset)
  report    Full analysis: every Server view once, ranked findings, sections (--html, --fail-on)
  replication  Replication channels and lag; skip one failed transaction; skip-loop until stopped
  cluster   Galera, Percona XtraDB Cluster, InnoDB Cluster or Group Replication members (--fail-on)
  upgrade-check  Check a MySQL server before an upgrade (MySQL Shell, else a partial native subset)
  dump      Dump databases into a new directory with a verified manifest (mysqlsh, mysqldump, mariadb-dump)
  restore   Restore a myflames dump after typing the target; verify objects and row counts
  clone     CLONE INSTANCE from a MySQL donor into the recipient (in place or --data-directory)
  profiles  List saved connection profiles; set type/read-only; forget or delete one
  privileges  The connection's effective privileges and, per action, allowed, denied or not verifiable
  accounts  Users and roles: list, show, create, drop, rename, lock, unlock, expire, password,
            grant, revoke, role grant/revoke/default (writes confirm, re-read and are audited)
  audit     Local audit trail of server writes: list, export (JSON/CSV), purge
  compare   Compare before/after EXPLAIN JSON files (--digest/--json for agents)
  diff      Deprecated alias of compare
  digest    Emit the compact LLM-ready digest of a plan (--cost shows the token/$ saving)
  tokens    Deprecated alias of digest (defaults to --cost)
  findings  Deprecated alias of advise
  check     Exit nonzero if the plan trips --fail-on categories (CI gate)
  advise    Ranked warnings + suggestions with confidence (text or --json)
  import-shell  Convert a MySQL Shell collectSlowQueryDiagnostics() zip to capture JSON
  guide     Show which view to pick for your use case
  teach     Interactive algorithm lessons (btree, bnl, hash, join, lru)
""",
    )
    parser.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )
    parser.add_argument(
        "--version", action="version",
        version="myflames %s" % __version__,
    )
    # ---- Connection flags (match the mysql CLI) ----
    conn = parser.add_argument_group("Connection (live MySQL / MariaDB)")
    from .workspace_cli import _add_connection_args
    _add_connection_args(conn, client_paths=True)
    conn.add_argument(
        "-e", "--execute", default=None, metavar="SQL", dest="execute",
        help="SQL to EXPLAIN ANALYZE against the live connection.",
    )
    conn.add_argument(
        "--no-collect-schema", action="store_true", dest="no_collect_schema",
        help="Disable SHOW CREATE TABLE collection (enabled by default).",
    )
    conn.add_argument(
        "--no-collect-stats", action="store_true", dest="no_collect_stats",
        help="Disable information_schema.tables stats collection (enabled by default).",
    )
    conn.add_argument(
        "--no-collect-variables", action="store_true", dest="no_collect_variables",
        help="Disable SHOW SESSION VARIABLES collection (enabled by default).",
    )
    # ---- Sidecar flags ----
    sidecar_grp = parser.add_argument_group("Sidecar (machine-readable output)")
    sidecar_grp.add_argument(
        "--sidecar", default=None, metavar="PATH",
        help="Write a JSON sidecar to PATH. When omitted but --output is given, "
             "a sidecar is written automatically at <output>.json. Pass '-' to "
             "suppress sidecar emission.",
    )
    sidecar_grp.add_argument(
        "--no-sidecar", action="store_true", dest="no_sidecar",
        help="Do not emit a sidecar file even when --output is set.",
    )
    parser.add_argument(
        "--no-teach-bundle", action="store_true", dest="no_teach_bundle",
        help="When writing an HTML report, the report's myteach section "
             "links to ./teach/index.html (the algorithm catalog) and "
             "./teach/<lesson>.html (per-operator deep-dives) relative "
             "to the output directory. By default myflames auto-emits "
             "those files into <output_dir>/teach/ once per directory "
             "so the links resolve. Pass this flag to opt out — useful "
             "for tests and CI where the teach folder is not needed.",
    )
    parser.add_argument(
        "--refresh-teach-bundle", action="store_true",
        dest="refresh_teach_bundle",
        help="Force the teach bundle to be regenerated even if it "
             "already exists in <output_dir>/teach/. Use after upgrading "
             "myflames to pick up new lesson content without manually "
             "deleting the old folder.",
    )
    parser.add_argument(
        "--type",
        choices=["flamegraph", "bargraph", "diagram", "tree", "workbench", "treemap"],
        default="flamegraph",
        help="Output type (default: flamegraph). 'treemap' is deprecated "
             "and renders the bar chart.",
    )
    parser.add_argument(
        "--output", "-o", default=None, metavar="PATH",
        help="Write output to file instead of stdout. Use .html extension for a self-contained HTML report",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="SVG width (default: 1800 flamegraph, 1200 others)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=32,
        help="Frame height for flame graph (default: 32)",
    )
    parser.add_argument(
        "--colors",
        default="hot",
        help="Color scheme for flame graph (default: hot)",
    )
    parser.add_argument(
        "--title",
        default="MySQL Query Plan",
        help="Chart title",
    )
    parser.add_argument(
        "--inverted",
        action="store_true",
        help="Flame graph: icicle (inverted)",
    )
    parser.add_argument(
        "--no-enhance",
        action="store_true",
        dest="no_enhance",
        help="Disable detailed tooltips on flame graph",
    )
    parser.add_argument(
        "--query",
        default=None,
        metavar="SQL",
        help="SQL query text to embed in the output",
    )
    parser.add_argument(
        "--query-file",
        default=None,
        metavar="PATH",
        dest="query_file",
        help="Path to a file containing the SQL query to embed",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Input JSON file or - for stdin (default: -)",
    )
    _add_quiet_flag(parser)
    args = parser.parse_args()
    if args.type == "treemap":
        _warn_alias(args,
            "warning: '--type treemap' is deprecated; the treemap view was "
            "removed. Rendering the bar chart instead (--type bargraph).\n"
        )
        args.type = "bargraph"

    # ── Live-connection mode ────────────────────────────────────────────
    # --execute or connection flags select live mode; never ignore them.
    # myflames connects to the server, runs EXPLAIN
    # ANALYZE on --execute, collects schema/stats/session variables, and
    # feeds everything into the advisor. The fetched JSON is then treated
    # exactly like a fixture from disk so the rest of the pipeline is
    # unchanged.
    json_text = None
    live_artifacts = None
    live_flags = (args.host, args.port, args.user, args.password, args.database,
                  args.profile, args.ssl_mode, args.ssl_ca, args.ssl_cert, args.ssl_key,
                  args.mysql_binary)
    if args.execute is not None or any(value is not None for value in live_flags):
        json_text, live_artifacts = _run_live_explain(args)
        # Live mode synthesises the query text from --execute so the
        # renderer's query panel shows what was actually analyzed.
        if not args.query and not args.query_file and args.execute:
            args.query = args.execute
        # The rest of main() expects args.input to be either "-" or a file;
        # flag it as "-" since we have json_text in-hand.
        args.input = "-"

    # Show help if no input and stdin is a terminal
    if args.input == "-" and sys.stdin.isatty() and json_text is None:
        parser.print_help()
        return

    # Type-specific width default
    if args.width is None:
        args.width = 1200 if args.type in ("bargraph", "diagram", "tree", "workbench") else 1800

    # Read input (skipped in live mode — json_text is already set)
    if json_text is None:
        json_text, imported = _read_plan_source(args.input, quiet=args.quiet)
        if imported:
            # A MySQL Shell bundle carries DDL, table stats, and variables:
            # feed them to the advisor exactly like live-mode collections.
            live_artifacts = _capture_artifacts(imported)
            if not args.query and not args.query_file and imported.get("query"):
                args.query = imported["query"]
    root = _parse_explain_input(json_text)

    # Detect output format from --output extension
    output_path = args.output
    is_html = output_path and output_path.lower().endswith(".html")

    if is_html:
        from .output_html_report import render_html_report
        from .output_sidecar import sidecar_path_for
        query_text = _resolve_query_text(args, json_text)
        # Slice 6 / S3: point the HTML head at its sibling .json so an
        # AI agent that found the HTML can enumerate the sidecar.
        sc_path = sidecar_path_for(output_path)
        alternate_href = (
            "./" + os.path.basename(sc_path)
            if sc_path and not getattr(args, "no_sidecar", False)
            else None
        )
        html = render_html_report(
            json_text,
            view_type=args.type,
            width=args.width,
            title=args.title,
            query_text=query_text,
            live_artifacts=live_artifacts,
            frame_height=args.height,
            colors=args.colors,
            inverted=args.inverted,
            alternate_json_href=alternate_href,
        )
        _write_output(html, output_path, quiet=args.quiet)
        # Auto-emit the myteach bundle alongside so the in-report
        # links (./teach/index.html + ./teach/<lesson>.html) resolve.
        # Idempotent — skip if teach/index.html already exists.
        _maybe_write_teach_bundle(args, output_path)
        # Sidecar emission for HTML path — same precedence as the SVG path.
        # We re-parse here because the analyze_plan/advise pipeline below is
        # bypassed in the short-circuit branch; this stays cheap because
        # parse_explain is idempotent and the input is already in memory.
        try:
            root_for_sc = parse_explain(json_text)
            analysis_for_sc = analyze_plan(root_for_sc)
            if query_text:
                analysis_for_sc["query_text_lines"] = format_sql(query_text)
            if live_artifacts:
                from .advisor import advise
                advise(
                    analysis_for_sc,
                    schema=live_artifacts.get("schema"),
                    stats=live_artifacts.get("stats"),
                    variables=live_artifacts.get("variables"),
                    freshness=live_artifacts.get("freshness"),
                    capture=live_artifacts.get("capture"),
                )
            _maybe_write_sidecar(
                args, root_for_sc, analysis_for_sc, json_text,
                live_artifacts, query_text,
            )
        except Exception as e:
            sys.stderr.write(
                "WARN: sidecar emission failed for HTML output: {}\n".format(e)
            )
            if args.sidecar:
                sys.exit(2)
        return

    max_time = root["total_time"]
    use_microseconds = max_time > 0 and max_time < 1
    unit = "\u00b5s" if use_microseconds else "ms"

    analysis = analyze_plan(root)

    query_text = _resolve_query_text(args, json_text)
    if query_text:
        analysis["query_text_lines"] = format_sql(query_text)

    # Feed collected environment data (if any) into the advisor.
    if live_artifacts:
        from .advisor import advise
        advise(
            analysis,
            schema=live_artifacts.get("schema"),
            stats=live_artifacts.get("stats"),
            variables=live_artifacts.get("variables"),
            freshness=live_artifacts.get("freshness"),
            capture=live_artifacts.get("capture"),
        )
    teach_maps = build_teach_index_maps(
        build_teach_hooks(
            root,
            query_sql=query_text,
            variables=(live_artifacts or {}).get("variables") or analysis.get("collected_variables"),
            stats=(live_artifacts or {}).get("stats") or analysis.get("collected_stats") or {},
        )
    )

    # ---- Sidecar emission ----------------------------------------------
    # The sidecar path is derived from --output by default. Precedence:
    #   1. --no-sidecar        → never emit
    #   2. --sidecar -         → suppress (matches shell "to stdout" convention
    #                            without forcing us to stream JSON to stdout)
    #   3. --sidecar PATH      → write at PATH exactly
    #   4. --output PATH       → write at <output>.json (auto)
    #   5. no --output         → skip (no place to land)
    _maybe_write_sidecar(args, root, analysis, json_text, live_artifacts, query_text)

    if args.type == "flamegraph":
        # Whole microseconds per measured operator (flamegraph.flame_scale);
        # tooltips print readable units (flamegraph.flame_value_text).
        folded_text, flame_unit = plan_folded(build_flame_entries(root), root["total_time"],
                                              keep_root=True)
        if not folded_text.strip():
            # Empty/degenerate input, not a tripped finding — exit 2 (bad input)
            # per the 0/1/2 contract; 1 is reserved for a matched CI gate.
            sys.stderr.write("No flame graph data (all zero self-time).\n")
            sys.exit(2)
        from .parser import operator_family
        _complexity_by_folded = {}
        _family_by_folded = {}
        for _n in flatten_nodes(root):
            _det = _n.get("details") or {}
            _c = _det.get("complexity")
            if isinstance(_c, dict) and _c.get("big_o"):
                _complexity_by_folded.setdefault(_n.get("folded_label") or "", _c)
            _folded = _n.get("folded_label") or ""
            if _folded:
                _family_by_folded.setdefault(_folded, operator_family(_det))
        svg = folded_to_svg(
            folded_text,
            title=args.title,
            width=args.width,
            height=args.height,
            countname=flame_unit,
            inverted=args.inverted,
            colors=args.colors,
            teach_index_by_folded=teach_maps["by_folded_label"],
            complexity_by_folded=_complexity_by_folded or None,
            family_by_folded=_family_by_folded or None,
        )
        if not args.no_enhance:
            # Each frame's tooltip gets its own operator's details
            # (parser.enhance_flame_titles: exact lookup, linear in frames).
            svg = enhance_flame_titles(svg, root)
        # Inject consolidated info panel (How to read + Query Analysis) into flamegraph SVG
        import re as _re
        m_height = _re.search(r'<svg[^>]+height="(\d+)"', svg)
        m_width = _re.search(r'<svg[^>]+width="(\d+)"', svg)
        if m_height and m_width:
            old_h = int(m_height.group(1))
            svg_w = int(m_width.group(1))
            panel_lines, panel_h = render_info_panel(analysis, 4, old_h + 8, svg_w - 8, view_type="flamegraph")
            new_h = old_h + 8 + panel_h
            panel_svg = "\n".join(panel_lines)
            svg = _re.sub(r'(height=")(\d+)(")', lambda mo: '%s%s%s' % (mo.group(1), new_h, mo.group(3)), svg, count=1)
            svg = _re.sub(
                r'(viewBox="0 0 \d+ )(\d+)(")',
                lambda mo: '%s%s%s' % (mo.group(1), new_h, mo.group(3)),
                svg, count=1
            )
            svg = svg.replace("</svg>", panel_svg + "\n</svg>", 1)
        _write_output(svg, output_path, quiet=args.quiet)
        return

    if args.type == "bargraph":
        total_time = root["total_time"]
        svg = render_bargraph(
            root,
            width=args.width,
            title=args.title,
            unit_display=unit,
            total_time=total_time,
            analysis=analysis,
            teach_index_by_folded=teach_maps["by_folded_label"],
        )
        _write_output(svg, output_path, quiet=args.quiet)
        return

    if args.type == "diagram":
        svg = render_diagram(
            root,
            width=args.width,
            title=args.title,
            unit_display=unit,
            analysis=analysis,
            teach_index_by_folded=teach_maps["by_folded_label"],
        )
        _write_output(svg, output_path, quiet=args.quiet)
        return

    if args.type == "workbench":
        from .output_workbench import render_workbench
        svg = render_workbench(root, width=args.width, title=args.title,
                               analysis=analysis, teach_index_by_folded=teach_maps["by_folded_label"])
        _write_output(svg, output_path, quiet=args.quiet)
        return

    if args.type == "tree":
        svg = render_tree(root, width=args.width, title=args.title, unit_display=unit, analysis=analysis, teach_index_by_folded=teach_maps["by_folded_label"])
        _write_output(svg, output_path, quiet=args.quiet)
        return

    sys.exit(1)


if __name__ == "__main__":
    main()
