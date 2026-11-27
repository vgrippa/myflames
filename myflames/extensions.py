"""Read-only VillageSQL extension inventory for the Server workspace.

All SQL is fixed and tagged. The public INFORMATION_SCHEMA views are the
interface: never query the protected villagesql schema. Extension registration
and error text are untrusted, so only bounded, typed projections leave here.
"""
import json
import re

from .connector import ConnectorError
from .dbops import REDACTED, redact, sensitive
from .perf_common import (PREFLIGHT_VARIABLES, SYSTEM_SCHEMAS, Reader, Server,
                          base_report, reason)

SCHEMA_VERSION = "extensions-1.0"
EXTENSIONS_TAG = "/*myflames:extensions*/ "
MAX_REGISTRATION_BYTES = 256 * 1024
OVERSIZED_REGISTRATION = "[myflames: registration exceeds 256 KB]"
MAX_REGISTRATION_ITEMS = 200
MAX_REPORT_REGISTRATION_ITEMS = 1000
MAX_EXTENSIONS = 1000
MAX_VARIABLES = 1000
MAX_NAME = 256
MAX_TEXT = 2000
COLUMN_LIMIT = 500
ROUTINE_LIMIT = 200
SAMPLE_LIMIT = 50

IDENTITY_SQL = "SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)" % ", ".join(
    "'%s'" % name for name in sorted(PREFLIGHT_VARIABLES))
UPTIME_SQL = "SHOW GLOBAL STATUS WHERE Variable_name = 'Uptime'"
EXTENSIONS_SQL = (
    "SELECT EXTENSION_NAME, EXTENSION_VERSION, PENDING_VERSION, PENDING_REQUESTED_AT, "
    "PENDING_LAST_ERROR, PENDING_LAST_ERROR_AT FROM INFORMATION_SCHEMA.EXTENSIONS "
    "ORDER BY EXTENSION_NAME LIMIT %d" % (MAX_EXTENSIONS + 1))
REGISTRATION_SQL = (
    "SELECT EXTENSION_NAME, NEGOTIATED_PROTOCOL, "
    "IF(LENGTH(REGISTRATION_JSON) > %d, '%s', REGISTRATION_JSON) "
    "FROM INFORMATION_SCHEMA.EXTENSION_REGISTRATION ORDER BY EXTENSION_NAME LIMIT %d"
    % (MAX_REGISTRATION_BYTES, OVERSIZED_REGISTRATION, MAX_EXTENSIONS + 1))
VARIABLES_SQL = (
    "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('veb_dir','vsql_allow_preview_extensions',"
    "'villagesql_vef_server_protocol','villagesql_schema_version','villagesql_server_version',"
    "'villagesql_build_info') OR Variable_name LIKE '%.%'")
STATUS_SQL = (
    "SHOW GLOBAL STATUS WHERE Variable_name IN ('Villagesql_veb_cache_repaired',"
    "'Com_install_extension','Com_uninstall_extension') OR Variable_name LIKE '%.%'")
_EXCLUDED_SCHEMAS = ", ".join("'%s'" % name for name in SYSTEM_SCHEMAS + ("villagesql",))
COLUMNS_SQL = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
    "WHERE DATA_TYPE LIKE '%%.%%' AND TABLE_SCHEMA NOT IN (%s) ORDER BY 1,2,3 LIMIT 501"
    % _EXCLUDED_SCHEMAS)
ROUTINES_SQL = (
    "SELECT SPECIFIC_SCHEMA, SPECIFIC_NAME, ROUTINE_TYPE, PARAMETER_NAME, DATA_TYPE "
    "FROM INFORMATION_SCHEMA.PARAMETERS WHERE DATA_TYPE LIKE '%%.%%' "
    "AND SPECIFIC_SCHEMA NOT IN (%s) ORDER BY 1,2,3,4 LIMIT 201" % _EXCLUDED_SCHEMAS)

_CONTROLS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(value, limit=MAX_TEXT):
    """Preserve text as text, with terminal controls visible and a strict cap."""
    if not isinstance(value, str):
        return None
    return _CONTROLS.sub(lambda match: "\\x%02x" % ord(match.group()), value[:limit])[:limit]


def _integer(value):
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not re.match(r"^[0-9]{1,20}$", text):
        return None
    number = int(text)
    return number if number <= 18446744073709551615 else None


def _boolean(value):
    if value is None:
        return None
    text = str(value).strip().upper()
    return True if text in ("ON", "1") else False if text in ("OFF", "0") else None


def _read(reader, sql, label, notes):
    # Unlike Reader.optional, connection failures are local to this part too.
    # Every subsequent read gets its own result or note; no missing data is zero.
    try:
        return reader.rows(sql)
    except ConnectorError as exc:
        message = redact(reason(exc), getattr(reader.conn, "password", None))
        notes.append("Could not read %s: %s" % (label, _text(message)))
        return None


def _pairs(rows):
    return {row[0].lower(): row[1] for row in rows or []
            if len(row) >= 2 and isinstance(row[0], str)}


def _variable_value(name, value, password):
    """Redact before bounding, so a truncated credential cannot leak a prefix."""
    if value is None:
        return None, False
    if sensitive(name):
        return REDACTED, True
    safe = redact(value, password)
    return _text(safe), safe != value


def _reject_constant(value):
    raise ValueError("Non-finite JSON value")


def _json_object(raw, label, notes):
    if raw is None:
        return None
    if raw == OVERSIZED_REGISTRATION or not isinstance(raw, str) or len(raw) > MAX_REGISTRATION_BYTES \
            or len(raw.encode("utf-8", errors="replace")) > MAX_REGISTRATION_BYTES:
        notes.append("%s exceeds the 256 KB JSON limit or is not text." % label)
        return None
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        notes.append("%s could not be parsed as a JSON object." % label)
        return None
    if not isinstance(value, dict):
        notes.append("%s is not a JSON object." % label)
        return None
    return value


def _registered_names(registration, key, label, notes, remaining=MAX_REGISTRATION_ITEMS):
    values = registration.get(key) if registration is not None else None
    if values is None:
        return None, None
    if not isinstance(values, list):
        notes.append("%s has an unreadable %s list." % (label, key))
        return None, None
    # Validate the bounded JSON document even when no projection budget is
    # left. A malformed tail must not become a valid empty or partial list.
    if any(not isinstance(value, dict) or not isinstance(value.get("name"), str)
           for value in values):
        notes.append("%s has malformed %s entries; its registration list is unavailable." % (label, key))
        return None, None
    limit = min(MAX_REGISTRATION_ITEMS, remaining)
    truncated = len(values) > limit
    if truncated:
        if remaining < MAX_REGISTRATION_ITEMS:
            note = ("Registered names are limited to %d across this report; "
                    "registration lists omitted by this limit are unknown."
                    % MAX_REPORT_REGISTRATION_ITEMS)
            if note not in notes:
                notes.append(note)
        else:
            notes.append("%s %s are limited to the first %d registrations."
                         % (label, key, limit))
    if values and limit == 0:
        return None, True
    return [_text(value["name"], MAX_NAME) for value in values[:limit]], truncated


def protocol_state(protocol):
    if protocol in (1, 3):
        return "deprecated"
    if protocol == 4:
        return "unsupported"
    if protocol is not None and protocol >= 5:
        return "current"
    return "unknown"


def _dependencies(rows, name, limit, keys):
    if rows is None:
        return {"count": None, "sample": [], "truncated": None}
    # A sentinel means the global query is incomplete. A missing extension in
    # that prefix cannot be reported as having zero dependencies.
    incomplete = len(rows) > limit
    matching = [row for row in rows[:limit] if len(row) == len(keys)
                and isinstance(row[-1], str) and row[-1].split(".", 1)[0].lower() == name.lower()]
    return {"count": None if incomplete else len(matching),
            "sample": [{key: _text(value, MAX_NAME) for key, value in zip(keys, row)}
                       for row in matching[:SAMPLE_LIMIT]],
            "truncated": incomplete or len(matching) > SAMPLE_LIMIT}


def _empty_report(server, notes):
    report = base_report(SCHEMA_VERSION, server)
    report.update({"available": False, "reason": None,
                   "settings": {"veb_dir": None, "preview_extensions": None,
                                "server_protocol": None, "schema_version": None,
                                "codebase": None, "build": None},
                   "status": {"veb_cache_repaired": None, "install_count": None, "uninstall_count": None},
                   "extensions": None, "other_variables": None, "other_variables_truncated": None,
                   "summary": {"installed": None, "pending": None, "failing": None,
                               "deprecated_protocol": None, "unsupported_protocol": None},
                   "notes": notes})
    return report


def extensions_report(conn):
    """Return extensions-1.0 on one connection; never install or change anything."""
    reader = Reader(conn, EXTENSIONS_TAG)
    notes = []
    identity = _pairs(_read(reader, IDENTITY_SQL, "server identity", notes))
    # The shared envelope copies VERSION verbatim, so bound this view's server
    # text before giving it to Server, just like extension-controlled fields.
    for name in ("version", "version_comment"):
        if name in identity:
            identity[name] = _text(identity[name])
    uptime = _pairs(_read(reader, UPTIME_SQL, "server uptime", notes))
    server = Server(identity, {"Uptime": uptime.get("uptime")})
    report = _empty_report(server, notes)
    if "performance_schema" not in identity:
        report["performance_schema"] = None
    if server.flavor != "villagesql":
        report["reason"] = ("Extensions are available only on VillageSQL connections."
                            if identity.get("version") else "Could not identify this server as VillageSQL.")
        return report

    raw_extensions = _read(reader, EXTENSIONS_SQL, "installed extensions", notes)
    raw_registration = _read(reader, REGISTRATION_SQL, "extension registration", notes)
    raw_variables = _read(reader, VARIABLES_SQL, "extension system variables", notes)
    raw_status = _read(reader, STATUS_SQL, "extension status variables", notes)
    raw_columns = _read(reader, COLUMNS_SQL, "columns using extension types", notes)
    raw_routines = _read(reader, ROUTINES_SQL, "routine parameters using extension types", notes)
    variables, status = _pairs(raw_variables), _pairs(raw_status)
    # The required settings read also supplies the codebase, avoiding a separate
    # VillageSQL identity probe. Rebuild just the shared envelope fields.
    if variables.get("villagesql_server_version") is not None:
        identity["villagesql_server_version"] = variables["villagesql_server_version"]
    report.update(base_report(SCHEMA_VERSION, Server(identity, {"Uptime": uptime.get("uptime")}),
                              taken_at=report["taken_at"]))
    if "performance_schema" not in identity:
        report["performance_schema"] = None
    report["available"] = True
    build = _json_object(variables.get("villagesql_build_info"), "VillageSQL build information", notes)
    report["settings"] = {
        "veb_dir": _text(variables.get("veb_dir")),
        "preview_extensions": _boolean(variables.get("vsql_allow_preview_extensions")),
        "server_protocol": _integer(variables.get("villagesql_vef_server_protocol")),
        "schema_version": _text(variables.get("villagesql_schema_version"), MAX_NAME),
        "codebase": _text(identity.get("villagesql_server_version"), MAX_NAME),
        "build": None if build is None else {
            "git_sha": _text(build.get("git_sha"), MAX_NAME),
            "is_dirty": build.get("is_dirty") if isinstance(build.get("is_dirty"), bool) else None},
    }
    # Com_install_extension counts install and update requests: INSTALL
    # EXTENSION and ALTER EXTENSION ... AT RESTART share the command code.
    report["status"] = {"veb_cache_repaired": _integer(status.get("villagesql_veb_cache_repaired")),
                        "install_count": _integer(status.get("com_install_extension")),
                        "uninstall_count": _integer(status.get("com_uninstall_extension"))}
    for rows, limit, label in ((raw_columns, COLUMN_LIMIT, "Columns"),
                               (raw_routines, ROUTINE_LIMIT, "Routine parameters")):
        if rows is not None and len(rows) > limit:
            notes.append("%s using extension types are limited to the first %d rows; total counts are unknown."
                         % (label, limit))
    if raw_extensions is None:
        # Ownership cannot be established without the authoritative inventory.
        return report

    # A dependency or registration must belong to at most one inventory row.
    # Refuse ambiguous ownership before projecting any repeated server text.
    names = [row[0].lower() for row in raw_extensions[:MAX_EXTENSIONS]
             if row and isinstance(row[0], str)]
    if len(names) != len(set(names)):
        notes.append("Installed extensions contain duplicate names; the inventory is unavailable.")
        return report

    if raw_registration is not None and len(raw_registration) > MAX_EXTENSIONS:
        notes.append("Extension registrations are limited to the first %d rows; "
                     "protocols and registration details outside that prefix are unknown." % MAX_EXTENSIONS)
    registrations = {row[0].lower(): row for row in (raw_registration or [])[:MAX_EXTENSIONS]
                     if len(row) >= 3 and isinstance(row[0], str)}
    items = []
    remaining_registration_items = MAX_REPORT_REGISTRATION_ITEMS
    for row in raw_extensions[:MAX_EXTENSIONS]:
        if len(row) < 6 or not isinstance(row[0], str):
            notes.append("An installed extension row is incomplete; the inventory is unavailable.")
            return report
        name = row[0]
        item = {"name": _text(name, MAX_NAME), "version": _text(row[1], MAX_NAME),
                "pending": None, "protocol": None, "protocol_state": "unknown", "protocol_note": None,
                "sdk_version": None, "functions": None, "types": None,
                "functions_truncated": None, "types_truncated": None,
                "columns": _dependencies(raw_columns, name, COLUMN_LIMIT, ("schema", "table", "column", "data_type")),
                "routines": _dependencies(raw_routines, name, ROUTINE_LIMIT,
                                           ("schema", "routine", "routine_type", "parameter", "data_type")),
                "variables": None, "variables_truncated": None}
        if any(value is not None for value in row[2:6]):
            safe_error = redact(row[4], getattr(conn, "password", None))
            error = _text(safe_error)
            item["pending"] = {"version": _text(row[2], MAX_NAME), "requested_at": _text(row[3], MAX_NAME),
                               "last_error": error,
                               "error_truncated": (len(safe_error) > MAX_TEXT or
                                                   len(_CONTROLS.sub(lambda m: "\\x%02x" % ord(m.group()), safe_error[:MAX_TEXT])) > MAX_TEXT)
                               if isinstance(safe_error, str) else False,
                               "last_error_at": _text(row[5], MAX_NAME)}
        reg = registrations.get(name.lower())
        if reg is not None:
            item["protocol"] = _integer(reg[1])
            item["protocol_state"] = protocol_state(item["protocol"])
            if item["protocol_state"] == "unsupported":
                item["protocol_note"] = "VillageSQL 0.0.8 refuses protocol 4 extensions and will not start with one installed."
            label = "Registration for %s" % item["name"]
            registration = _json_object(reg[2], label, notes)
            item["sdk_version"] = _text(registration.get("sdk_version"), MAX_NAME) if registration else None
            for key, source in (("functions", "funcs"), ("types", "types")):
                item[key], item[key + "_truncated"] = _registered_names(
                    registration, source, label, notes, remaining_registration_items)
                remaining_registration_items -= len(item[key] or [])
        items.append(item)
    report["extensions"] = items
    inventory_complete = len(raw_extensions) <= MAX_EXTENSIONS
    if not inventory_complete:
        notes.append("Installed extensions are limited to the first %d rows; summary counts are unknown." % MAX_EXTENSIONS)

    if raw_variables is not None and raw_status is not None and inventory_complete:
        owned = {row[0].lower(): item for row, item in zip(raw_extensions, items)}
        for item in items:
            item["variables"], item["variables_truncated"] = [], False
        report["other_variables"], report["other_variables_truncated"] = [], False
        used = 0
        redacted_variables = False
        for rows, kind in ((raw_variables, "system"), (raw_status, "status")):
            for row in rows:
                if len(row) < 2 or not isinstance(row[0], str) or "." not in row[0]:
                    continue
                owner = owned.get(row[0].split(".", 1)[0].lower())
                if used >= MAX_VARIABLES:
                    if owner is None:
                        report["other_variables_truncated"] = True
                    else:
                        owner["variables_truncated"] = True
                    continue
                used += 1
                value, was_redacted = _variable_value(row[0], row[1], getattr(conn, "password", None))
                redacted_variables = redacted_variables or was_redacted
                variable = {"name": _text(row[0], MAX_NAME), "value": value, "kind": kind}
                (report["other_variables"] if owner is None else owner["variables"]).append(variable)
        if redacted_variables:
            notes.append("Sensitive extension variable values are redacted.")
        if report["other_variables_truncated"] or any(item["variables_truncated"] for item in items):
            notes.append("Extension variables are limited to the first %d values across system and status reads." % MAX_VARIABLES)
    if inventory_complete:
        report["summary"].update({"installed": len(items),
                                  "pending": sum(item["pending"] is not None for item in items),
                                  "failing": sum(bool(item["pending"] and item["pending"]["last_error"]) for item in items)})
        if all(item["protocol"] is not None for item in items):
            report["summary"].update({"deprecated_protocol": sum(item["protocol_state"] == "deprecated" for item in items),
                                      "unsupported_protocol": sum(item["protocol_state"] == "unsupported" for item in items)})
    return report
