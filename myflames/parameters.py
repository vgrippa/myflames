"""Bulk global parameter metadata for the optional config-1.0 parameters section.

Reads only. The displayed inventory comes from the connected server, never
from the generated catalog: compiled-out plugins and removed variables must
not appear. Scope, type, bounds and mutability share the single-variable
reader's rules. A label describes application timing, not permission or a
promise that an arbitrary value will pass the separate change review.
"""
from . import variables


SCHEMA_VERSION = "parameters-1.0"
MYSQL_INFO_SQL = ("SELECT VARIABLE_NAME, VARIABLE_SOURCE, VARIABLE_PATH, MIN_VALUE, MAX_VALUE, "
                  "SET_TIME, SET_USER, SET_HOST FROM performance_schema.variables_info")
MYSQL_PERSISTED_SQL = "SELECT VARIABLE_NAME FROM performance_schema.persisted_variables"
MYSQL_METADATA_SQL = ("SELECT VARIABLE_NAME, VARIABLE_SCOPE, DATA_TYPE, MIN_VALUE, MAX_VALUE, DOCUMENTATION "
                      "FROM performance_schema.variables_metadata")
MARIADB_SQL = ("SELECT VARIABLE_NAME, GLOBAL_VALUE, GLOBAL_VALUE_ORIGIN, DEFAULT_VALUE, VARIABLE_SCOPE, "
               "VARIABLE_TYPE, NUMERIC_MIN_VALUE, NUMERIC_MAX_VALUE, NUMERIC_BLOCK_SIZE, ENUM_VALUE_LIST, "
               "READ_ONLY, GLOBAL_VALUE_PATH, VARIABLE_COMMENT, COMMAND_LINE_ARGUMENT "
               "FROM information_schema.SYSTEM_VARIABLES "
               "WHERE VARIABLE_SCOPE <> 'SESSION ONLY'")
_INFO_KEYS = ("name", "source", "path", "min", "max", "set_time", "set_user", "set_host")
_METADATA_KEYS = ("name", "scope", "type", "min", "max", "documentation")
_CATALOG_KEYS = ("name", "global_value", "origin", "default", "scope", "type", "min", "max", "block",
                 "values", "read_only", "path", "comment", "command_line_argument")


def _index(rows, keys):
    return {row[0].lower(): dict(zip(keys, row)) for row in rows or [] if row and row[0]}


def _check_names(checks):
    out = {}
    for check in checks or []:
        for setting in check.get("settings", []):
            for name in (setting.get("name"), setting.get("alias")):
                if name:
                    out.setdefault(name.lower(), set()).add(check["key"])
    return out


def compute_parameters(engine, version_tuple, rows=None, info_rows=None, persisted_rows=None,
                       metadata_rows=None, catalog_rows=None, checks=None, notes=None):
    """Build ``parameters-1.0`` from decoded positional rows, without I/O.

    Row order follows the SELECT constants above; ``rows`` is SHOW GLOBAL
    VARIABLES. None means a read failed; an empty list means it succeeded
    with no rows. Only MySQL 9.0+ requires metadata_rows. MariaDB uses only
    catalog_rows. Values are already unescaped by perf_common.Reader.
    modifiable="none" means the server sets the value; no option or SET changes it.
    """
    mysql = engine == "mysql"
    required = [rows, info_rows, persisted_rows] if mysql else [catalog_rows]
    if mysql and version_tuple >= (9, 0, 0):
        required.append(metadata_rows)
    catalog = _index(catalog_rows, _CATALOG_KEYS) if not mysql else {}
    details = _index(info_rows, _INFO_KEYS) if mysql else {}
    metadata = _index(metadata_rows, _METADATA_KEYS) if mysql else {}
    persisted = set(_index(persisted_rows, ("name",))) if mysql and persisted_rows is not None else None
    globals_ = ({row[0].lower(): row[1] for row in rows or [] if len(row) >= 2 and row[0]} if mysql else
                {name: row.get("global_value") for name, row in catalog.items()})
    associations = _check_names(checks)
    items = []
    for name, value in sorted(globals_.items()):
        info = {"name": name, "engine": engine, "version_tuple": version_tuple,
                "in_global": True, "in_session": False, "global_value": value, "session_value": None,
                "platform": globals_.get("version_compile_os"), "catalog": catalog.get(name),
                "info": details.get(name), "metadata": metadata.get(name)}
        scope, _ = variables._scope(info)
        if scope == "session":
            continue
        kind, values = variables._type(info)
        low, high, block = variables._range(info, kind)
        dynamic = variables._dynamic(info)
        source, source_label = variables._set_by(info)
        detail, cat, meta = details.get(name, {}), catalog.get(name, {}), metadata.get(name, {})
        modifiable = None
        if dynamic is True:
            modifiable = "now"
        elif dynamic is False:
            flags = variables._flags(name) if mysql else set()
            # Same rule as the single-variable planner (variables.startup_option).
            if variables.startup_option(info) is False:
                modifiable = "none"
            else:
                modifiable = "option_file" if not mysql or "np" in flags else "restart"
        description = cat.get("comment") if not mysql else meta.get("documentation")
        items.append({
            "name": name, "value": value[:variables.MAX_VALUE_LENGTH] if value is not None else None,
            "truncated": value is not None and len(value) > variables.MAX_VALUE_LENGTH,
            "scope": scope, "apply_type": None if dynamic is None else "dynamic" if dynamic else "static",
            "modifiable": modifiable, "type": kind, "values": values, "min": low, "max": high,
            "block_size": block, "default": variables._default(info), "source": source,
            "source_label": source_label, "source_path": detail.get("path") or cat.get("path") or None,
            "set_time": detail.get("set_time"), "set_user": detail.get("set_user"), "set_host": detail.get("set_host"),
            "persisted": name in persisted if persisted is not None else None,
            "modified": source.upper() not in (("COMPILED",) if mysql else ("COMPILE-TIME", "AUTO")) if source else None,
            "description": description[:600] if description else None,
            "checks": sorted(associations.get(name, ())),
        })
    return {"schema_version": SCHEMA_VERSION, "complete": all(rowset is not None for rowset in required),
            "notes": list(notes or []), "items": items}


def read_parameters(reader, server, notes, checks):
    """Read each catalog independently so denied details do not hide values.

    Reader.optional records denied/missing-table notes and Reader.rows handles
    batch escapes/NULL. Connection failures retain Reader's normal exception
    behavior. Parameter notes are also appended to the enclosing report.
    """
    local_notes = []
    data = {}
    if server.engine == "mysql":
        data["rows"] = reader.optional("SHOW GLOBAL VARIABLES", "global parameters", local_notes)
        data["info_rows"] = reader.optional(MYSQL_INFO_SQL, "performance_schema.variables_info", local_notes)
        data["persisted_rows"] = reader.optional(MYSQL_PERSISTED_SQL, "performance_schema.persisted_variables", local_notes)
        if server.version_tuple >= (9, 0, 0):
            data["metadata_rows"] = reader.optional(MYSQL_METADATA_SQL, "performance_schema.variables_metadata", local_notes)
    else:
        data["catalog_rows"] = reader.optional(MARIADB_SQL, "information_schema.SYSTEM_VARIABLES", local_notes)
    # Batch output prints SQL NULL and the text NULL alike: read those values again exactly.
    value_rows = data.get("rows") if server.engine == "mysql" else data.get("catalog_rows")
    resolved = _resolve_nulls(reader, server.engine, value_rows, local_notes)
    notes.extend(local_notes)
    report = compute_parameters(server.engine, server.version_tuple, checks=checks, notes=local_notes, **data)
    if not resolved:
        # SQL NULL and the text NULL are still indistinguishable for some values.
        report["complete"] = False
    return report


#: Names per exact re-read statement.
NULL_CHUNK = 100
MYSQL_EXACT_SQL = ("SELECT VARIABLE_NAME, VARIABLE_VALUE IS NULL, HEX(VARIABLE_VALUE) "
                   "FROM performance_schema.global_variables WHERE VARIABLE_NAME IN (%s)")
MARIADB_EXACT_SQL = ("SELECT VARIABLE_NAME, GLOBAL_VALUE IS NULL, HEX(GLOBAL_VALUE) "
                     "FROM information_schema.SYSTEM_VARIABLES WHERE VARIABLE_NAME IN (%s)")


def _resolve_nulls(reader, engine, rows, notes):
    """Replace a None value (column 1) with the exact value from the variable
    catalog: SQL NULL stays None, the text 'NULL' becomes 'NULL'. Names
    outside the variable name syntax, names the catalog does not return and a
    failed read (one note) keep None. False when a read failed."""
    pending = {row[0].lower(): row for row in rows or []
               if len(row) > 1 and row[1] is None and row[0] and variables._NAME.match(row[0].lower())}
    names = sorted(pending)
    if not names:
        return True
    for start in range(0, len(names), NULL_CHUNK):
        chunk = names[start:start + NULL_CHUNK]
        quoted = ", ".join("'%s'" % (n if engine == "mysql" else n.upper()) for n in chunk)
        found = reader.optional((MYSQL_EXACT_SQL if engine == "mysql" else MARIADB_EXACT_SQL) % quoted,
                                "values shown as NULL", notes)
        if found is None:
            return False
        for cells in found:
            row = pending.get((cells[0] or "").lower()) if len(cells) == 3 else None
            if row is None:
                continue
            try:
                row[1] = variables._decode_value(cells[1:])
            except ValueError:
                pass
    return True
