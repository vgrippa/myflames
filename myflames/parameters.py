"""Bulk global parameter metadata for the optional config-1.0 parameters section.

Reads only. The displayed inventory comes from the connected server, never
from the generated catalog: compiled-out plugins and removed variables must
not appear. Scope, type, bounds and mutability share the single-variable
reader's rules. A label describes application timing, not permission or a
promise that an arbitrary value will pass the separate change review.
"""
from . import mysql_sysvar_help, sysvar_docs, variables
from .server_info import percona_variable


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


def _location(entry, versions, wanted):
    """(manual version, page index, anchor or None) from a sysvar_docs entry.

    An entry is a location (a page index, or (page index, anchor)) valid in
    every manual, or one location per manual with None where that manual
    does not document the name. The manual for a server is the first at or
    after its major.minor (8.1 to 8.3 are in 8.4's, 9.0 to 9.6 in 9.7's), else
    the newest. A name that manual leaves out comes from the nearest newer
    manual, then the nearest older one.
    """
    if entry is None:
        return None
    keys = [tuple(int(p) for p in v.split(".")) for v in versions]
    start = next((i for i, key in enumerate(keys) if key >= tuple(wanted[:2])), len(keys) - 1)
    single = isinstance(entry, int) or (isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[1], str))
    order = [start] + list(range(start + 1, len(keys))) + list(range(start - 1, -1, -1))
    for i in order:
        loc = entry if single else entry[i]
        if loc is not None:
            page, anchor = (loc, None) if isinstance(loc, int) else loc
            return versions[i], page, anchor
    return None


def source_help(version_tuple, name):
    """The help text a MySQL server before 9.0 declares *name* with, from the
    tagged source (mysql_sysvar_help), or None. MySQL 9.0+ reports its own in
    performance_schema.variables_metadata, so this is never used there."""
    if tuple(version_tuple or (0,))[:1] >= (9,):
        return None
    if tuple(version_tuple or (0, 0))[:2] < (8, 1) and name in mysql_sysvar_help.HELP_80:
        return mysql_sysvar_help.HELP_80[name]
    return mysql_sysvar_help.HELP.get(name)


def documentation(engine, version_tuple, name, flavor=None):
    """``{"url", "source"}``: the official manual page for system variable
    *name* on this server, or None. Nothing is fetched; sysvar_docs is
    generated from saved reference pages. MySQL and Percona servers use the
    MySQL Reference Manual; a name it does not document links to Percona's
    manuals only on a Percona server (flavor "percona"). MariaDB uses its own
    documentation."""
    name = (name or "").lower()
    if not variables._NAME.match(name):
        return None
    if engine == "mariadb":
        found = _location(sysvar_docs.MARIADB.get(name), ("0.0",), (0, 0))
        if not found:
            return None
        _, page, anchor = found
        return {"url": sysvar_docs.MARIADB_URL % (sysvar_docs.MARIADB_PAGES[page], anchor or name),
                "source": "MariaDB documentation"}
    wanted = tuple(version_tuple or (0, 0))[:2]
    found = _location(sysvar_docs.MYSQL.get(name), sysvar_docs.MYSQL_VERSIONS, wanted)
    if found:
        version, page, anchor = found
        return {"url": sysvar_docs.MYSQL_URL % (version, sysvar_docs.MYSQL_PAGES[page], anchor or "sysvar_" + name),
                "source": "MySQL %s Reference Manual" % version}
    if flavor != "percona":
        return None
    for table, versions, pages, url, product in (
            (sysvar_docs.PERCONA, sysvar_docs.PERCONA_VERSIONS, sysvar_docs.PERCONA_PAGES, sysvar_docs.PERCONA_URL,
             "Percona Server"),
            (sysvar_docs.PXC, sysvar_docs.PXC_VERSIONS, sysvar_docs.PXC_PAGES, sysvar_docs.PXC_URL,
             "Percona XtraDB Cluster")):
        found = _location(table.get(name), versions, wanted)
        if found:
            version, page, anchor = found
            return {"url": url % (version, pages[page], anchor or name),
                    "source": "%s %s documentation" % (product, version)}
    return None


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


def _flavor(engine, flavor, globals_):
    """mysql, percona or mariadb: the server's own flavor when the caller
    knows it, else Percona's @@version_comment ("Percona Server (GPL), ...",
    "Percona XtraDB Cluster (GPL), ...")."""
    if engine == "mariadb":
        return "mariadb"
    if flavor in ("mysql", "percona"):
        return flavor
    return "percona" if "percona" in str(globals_.get("version_comment") or "").lower() else "mysql"


def compute_parameters(engine, version_tuple, rows=None, info_rows=None, persisted_rows=None,
                       metadata_rows=None, catalog_rows=None, checks=None, notes=None, flavor=None):
    """Build ``parameters-1.0`` from decoded positional rows, without I/O.

    Row order follows the SELECT constants above; ``rows`` is SHOW GLOBAL
    VARIABLES. None means a read failed; an empty list means it succeeded
    with no rows. Only MySQL 9.0+ requires metadata_rows. MariaDB uses only
    catalog_rows. Values are already unescaped by perf_common.Reader.
    modifiable="none" means the server sets the value; no option or SET changes it.
    *flavor* (``percona`` on Percona Server) marks the variables Percona adds
    with ``vendor: "percona"`` (server_info.PERCONA_VARIABLES); every other
    item has ``vendor: None``.
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
    flavor = _flavor(engine, flavor, globals_)
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
        description = cat.get("comment") if not mysql else meta.get("documentation") or source_help(version_tuple, name)
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
            "documentation": documentation(engine, version_tuple, name, flavor),
            "checks": sorted(associations.get(name, ())),
            "vendor": "percona" if percona_variable(name, flavor, version_tuple) else None,
        })
    return {"schema_version": SCHEMA_VERSION, "complete": all(rowset is not None for rowset in required),
            "notes": list(notes or []), "flavor": flavor or ("mariadb" if engine == "mariadb" else "mysql"),
            "items": items}


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
    report = compute_parameters(server.engine, server.version_tuple, checks=checks, notes=local_notes,
                                flavor=getattr(server, "flavor", None), **data)
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
