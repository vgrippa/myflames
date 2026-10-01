"""Local browser workspace with explicit saved investigations. Runtime dependencies: Python stdlib only."""
import argparse
import json
import math
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .connector import ConnectorError
from .parser import load_explain_json, parse_explain, analyze_plan, _is_mariadb_format
from .output_sidecar import build_sidecar
from .output_compare_sidecar import build_compare_sidecar
from .output_compare import render_compare
from .output_html_report import render_html_report, _render_svg

ASSET_DIR = Path(__file__).resolve().parent / "ui_assets"
MAX_BODY = 12 * 1024 * 1024
MAX_PLAN = 5 * 1024 * 1024
VIEWS = ("flamegraph", "diagram", "tree", "bargraph", "treemap", "workbench")


def _plan(payload, key="plan"):
    text = payload.get(key)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Choose or paste an EXPLAIN JSON plan.")
    if len(text.encode("utf-8")) > MAX_PLAN:
        raise ValueError("Each plan must be at most 5 MiB.")
    data = load_explain_json(text)
    if not isinstance(data, dict):
        raise ValueError("Expected an EXPLAIN JSON object.")
    plan = data.get("query_plan", data)
    if not isinstance(plan, dict) or not ("operation" in plan or _is_mariadb_format(plan)):
        raise ValueError("Expected a MySQL or MariaDB EXPLAIN ANALYZE JSON plan.")
    # Bound work before the recursive parser and renderers run. Validate the
    # entire input, including metadata, without introducing a second parser.
    stack = [(data, 0)]
    containers = 0
    while stack:
        item, depth = stack.pop()
        if depth > 80:
            raise ValueError("This plan is too deeply nested (maximum 80 levels).")
        if isinstance(item, (dict, list)):
            containers += 1
            if containers > 10000:
                raise ValueError("This plan is too large to inspect in the UI.")
            values = item.values() if isinstance(item, dict) else item
            stack.extend((value, depth + 1) for value in values)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Plan numbers must be finite.")
    root = parse_explain(text)
    query = data.get("query") or plan.get("query") or ""
    if not isinstance(query, str):
        query = ""
    return text, root, query, "mariadb" if _is_mariadb_format(plan) else "mysql"


def _operators(root):
    """UI projection of canonical parsed nodes; sidecar schema stays unchanged."""
    result = []
    stack = [(root, 0, None)]
    while stack:
        node, depth, parent_id = stack.pop()
        result.append({
            "node_id": node["node_id"], "label": node["short_label"],
            "depth": depth, "self_time_ms": node["self_time"],
            "total_time_ms": node["total_time"], "rows": node["rows"],
            "loops": node["loops"], "details": node.get("details", {}),
            "parent_id": parent_id, "children": [c["node_id"] for c in node.get("children", [])],
        })
        stack.extend((child, depth + 1, node["node_id"]) for child in reversed(node.get("children", [])))
    return result


def api_response(path, payload, workspace=None):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON request object.")
    if path.startswith(("/api/investigations/", "/api/profiles/", "/api/live/", "/api/connection/")):
        if workspace is None:
            raise ValueError("This operation requires a running workspace.")
        if path.startswith(("/api/investigations/", "/api/profiles/")):
            kind = "investigation" if path.startswith("/api/investigations/") else "profile"
            operation = path.rsplit("/", 1)[-1]
            if operation == "list": return {"items": workspace.store.list(kind)}
            if operation == "save": return workspace.store.save(kind, payload.get("value"), payload.get("id"))
            if operation == "delete": return workspace.store.delete(kind, payload.get("id"))
        if path == "/api/live/start": return workspace.jobs.start(payload)
        if path == "/api/live/status": return workspace.jobs.get(payload.get("id")).snapshot()
        if path == "/api/live/cancel":
            job = workspace.jobs.get(payload.get("id"))
            job.cancel()
            return job.snapshot()
        if path == "/api/connection/test":
            from .live import connection_options
            from .connector import MySQLConnection
            with MySQLConnection(**connection_options(payload.get("connection"))) as conn:
                return {"version": conn.server_version(), "schemas": [row[0] for row in conn.query_rows("SHOW DATABASES") if row]}
        raise ValueError("Unknown workspace operation.")
    if path.startswith("/api/teach/"):
        from .teach import LESSONS, CURRICULUM, LESSON_FAMILIES, _FAMILY_LABELS, render_lesson
        if path == "/api/teach/catalog":
            track = [key for key in CURRICULUM if key in LESSONS]
            keys = track + [key for key in LESSONS if key not in track]
            return {
                "curriculum": track,
                "families": [{"key": key, "label": _FAMILY_LABELS[key]}
                             for key in LESSON_FAMILIES],
                "lessons": [{"key": key, "title": LESSONS[key]["title"],
                             "summary": LESSONS[key]["summary"],
                             "family": LESSONS[key]["family"]} for key in keys],
            }
        if path == "/api/teach/lesson":
            key = payload.get("lesson")
            if not isinstance(key, str) or key not in LESSONS:
                raise ValueError("Choose a lesson from the Teach catalog.")
            return {"html": render_lesson(key)}
        raise ValueError("Unknown Teach endpoint.")
    if path == "/api/compare":
        before, _, _, _ = _plan(payload, "before")
        after, _, _, _ = _plan(payload, "after")
        return {"analysis": build_compare_sidecar(before, after),
                "html": render_compare(before, after)}
    text, root, query, engine = _plan(payload)
    if path == "/api/analyze":
        from .teach_hooks import build_teach_hooks
        analysis = analyze_plan(root)
        from .live import normalize_capture
        capture = normalize_capture(payload.get("capture"))
        if isinstance(capture, dict):
            from .advisor import advise
            advise(analysis, schema=capture.get("schema"), stats=capture.get("table_stats"), variables=capture.get("variables"))
            query = capture.get("query") or query
        return {"analysis": build_sidecar(root, analysis,
                                          source_type="file", engine=engine,
                                          query_raw=query),
                "operators": _operators(root), "query": query, "teach_hooks": build_teach_hooks(root, query_sql=query), "capture": capture}
    view = payload.get("view", "flamegraph")
    if view not in VIEWS:
        raise ValueError("Choose a supported visualization.")
    if path == "/api/report":
        return {"html": render_html_report(text, view_type=view, query_text=query)}
    if path == "/api/render":
        unit = "\u00b5s" if 0 < root["total_time"] < 1 else "ms"
        from .exploration import render_exploration
        return {"html": render_exploration(root, view, width=1200,
                    selected_id=payload.get("selected_id", ""), focus_id=payload.get("focus_id", ""),
                    collapsed_ids=payload.get("collapsed_ids"), metric=payload.get("metric", "self_time"))}

    raise ValueError("Unknown API endpoint.")


class WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, store_path=None):
        from .workspace_store import WorkspaceStore
        from .live import LiveJobs
        self.store = WorkspaceStore(store_path)
        self.jobs = LiveJobs()
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), WorkspaceHandler)
        self.origin = "http://127.0.0.1:%s" % self.server_address[1]


    def server_close(self):
        self.jobs.close()
        super().server_close()


class WorkspaceHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        # Plan text, filenames, and tokens must not end up in access logs.
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, dict):
            body = json.dumps(body, allow_nan=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _local_request(self):
        # Host checking also prevents DNS rebinding. No wildcard bind or CORS.
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self._send(403, {"error": "Open the URL printed by myflames ui."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            self._send(403, {"error": "Cross-origin requests are not allowed."})
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        path = urlsplit(self.path).path
        if path == "/":
            index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
            self._send(200, index.replace("__MYFLAMES_TOKEN__", self.server.token),
                       "text/html; charset=utf-8")
            return
        # Only flat bundled assets are exposed, never arbitrary local files.
        name = path[len("/assets/"):] if path.startswith("/assets/") else ""
        # Earlier UI bundles referenced content-hashed logos. Keep those image
        # requests working for tabs open across the switch to a stable address.
        if name in ("myflames-BpuTIrWf.jpeg", "logo-BpuTIrWf.jpeg",
                    "myflames-icon-v2-CFba5YPm.png"):
            name = "myflames-logo.png"
        if name and "/" not in name and "\\" not in name and name not in (".", ".."):
            asset = ASSET_DIR / "assets" / name
            types = {".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml"}
            if asset.is_file() and asset.suffix in types:
                self._send(200, asset.read_bytes(), types[asset.suffix] + "; charset=utf-8")
                return
        self._send(404, {"error": "Not found."})

    def do_POST(self):
        if not self._local_request():
            return
        token = self.headers.get("X-Myflames-Token", "")
        if not secrets.compare_digest(token.encode("utf-8"), self.server.token.encode("ascii")):
            self._send(403, {"error": "Session expired. Reload the page."})
            return
        if self.path not in ("/api/analyze", "/api/render", "/api/report", "/api/compare",
                             "/api/teach/catalog", "/api/teach/lesson",
                             "/api/investigations/list", "/api/investigations/save", "/api/investigations/delete",
                             "/api/profiles/list", "/api/profiles/save", "/api/profiles/delete",
                             "/api/live/start", "/api/live/status", "/api/live/cancel", "/api/connection/test"):
            self._send(404, {"error": "Not found."})
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, {"error": "Send application/json."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                self._send(413, {"error": "Request must be between 1 byte and 12 MiB."})
                return
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            result = api_response(self.path, payload, self.server)
            self._send(200, result)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
            self._send(400, {"error": "Could not complete the request: %s" % exc})
        except ConnectorError as exc:
            self._send(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            self._send(500, {"error": "Could not render this plan. Try another view or the CLI."})


def cmd_ui(argv):
    parser = argparse.ArgumentParser(prog="myflames ui",
                                     description="Open the local query-plan workspace in your browser.")
    parser.add_argument("--port", type=int, default=0, metavar="PORT",
                        help="Listen on this local port (default: choose an available port).")
    parser.add_argument("--no-browser", action="store_true",
                        help="Print the local URL without opening a browser.")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if not (ASSET_DIR / "index.html").is_file():
        parser.exit(2, "UI assets are missing. Reinstall myflames or build apps/workspace.\n")
    try:
        server = WorkspaceServer(args.port)
    except OSError as exc:
        parser.exit(2, "Cannot start the UI: %s. Try --port 0.\n" % exc)
    with server:
        sys.stderr.write("myflames UI: %s\nPress Ctrl+C to stop.\n" % server.origin)
        if not args.no_browser:
            try:
                if not webbrowser.open(server.origin):
                    sys.stderr.write("Open the URL above in your browser.\n")
            except webbrowser.Error:
                sys.stderr.write("Open the URL above in your browser.\n")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("\nUI stopped.\n")
