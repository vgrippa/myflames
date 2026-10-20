#!/usr/bin/env python3
"""Extract system-variable facts from MySQL tags, without compiling the server.

This is a deliberately small C++ declaration reader, not a preprocessor. It
balances arguments, ignores comments and quoted punctuation, and merges all
conditional definitions conservatively. Conflicting facts become None; enum
lists are emitted only when every element is a literal and every definition
agrees. It never reads the working tree, so local source edits cannot leak in.
"""
import argparse
import ast
from pathlib import Path
import re
import subprocess


_STRING = r'"(?:\\.|[^"\\])*"'
_TOKEN = re.compile(_STRING + r"|'(?:\\.|[^'\\])*'|/\*.*?\*/|//[^\n]*", re.S)
_CORE = re.compile(r"\b(?P<static>static\s+)?Sys_var_(?P<kind>\w+)(?:\s*<[^;{}]*?>)?"
                   r"\s+(?P<symbol>\w+)\s*(?P<opener>[({])")
_PLUGIN = re.compile(r"\bMYSQL_(SYSVAR|THDVAR)_(BOOL|INT|UINT|LONG|ULONG|LONGLONG|ULONGLONG|DOUBLE|STR|ENUM|SET)\s*\(")
_FLAGS = (("READ_ONLY", "ro"), ("NON_PERSIST", "np"), ("PERSIST_AS_READONLY", "pro"),
          ("NO_CMD_LINE", "nc"))
_PLUGIN_FLAGS = (("PLUGIN_VAR_READONLY", "ro"), ("PLUGIN_VAR_NOPERSIST", "np"),
                 ("PLUGIN_VAR_PERSIST_AS_READ_ONLY", "pro"), ("PLUGIN_VAR_NOCMDOPT", "nc"))
_TYPES = {
    "bool": "boolean", "bit": "boolean", "test_flag": "boolean", "transaction_read_only": "boolean",
    "binlog_encryption": "boolean", "enum": "enum", "enum_binlog_checksum": "enum",
    "transaction_isolation": "enum", "gtid_mode": "enum", "enforce_gtid_consistency": "enum",
    "set": "set", "flagset": "flagset", "double": "double", "session_special_double": "double",
    "ulong": "integer", "ulonglong": "integer", "uint": "integer", "long": "integer",
    "int": "integer", "int32": "integer", "longlong": "integer", "harows": "integer",
    "max_user_conn": "integer", "session_special": "integer", "keycache": "integer",
    "charptr": "string", "lexstring": "string", "str": "string", "struct": "string",
    "plugin": "string", "have": "string", "dbug": "string", "debug_sync": "string",
    "proxy_user": "string", "external_user": "string", "system_time_zone": "string",
    "version": "string", "tz": "string", "gtid_set": "string", "gtid_next": "string",
    "gtid_executed": "string", "gtid_purged": "string", "gtid_owned": "string",
}
_ROOTS = ("sql/sys_vars.cc", "sql/sys_vars.h", "sql/set_var.h", "sql/handler.cc", "storage/innobase/",
          "plugin/semisync/", "plugin/x/", "plugin/group_replication/", "sql/auth/sha2_password.cc",
          "storage/perfschema/", "components/validate_password/", "storage/myisam/", "storage/temptable/")


def uncomment(text):
    """Keep offsets/lines and string literals while blanking comments."""
    return _TOKEN.sub(lambda m: re.sub(r"[^\n]", " ", m.group())
                      if m.group().startswith(("/*", "//")) else m.group(), text)


def arguments(text, start, closing=")"):
    """Split a balanced argument/initializer list, starting after its opener."""
    parts, begin, stack, pos = [], start, [closing], start
    quoted = re.compile(_STRING + r"|'(?:\\.|[^'\\])*'")
    while pos < len(text):
        char = text[pos]
        if char in "\"'":
            match = quoted.match(text, pos)
            if not match:
                raise ValueError("Unterminated C++ literal")
            pos = match.end()
            continue
        if char in "({[":
            stack.append({"(": ")", "{": "}", "[": "]"}[char])
        elif char == stack[-1]:
            stack.pop()
            if not stack:
                parts.append(text[begin:pos].strip())
                return parts, pos + 1
        elif char == "," and len(stack) == 1:
            parts.append(text[begin:pos].strip())
            begin = pos + 1
        pos += 1
    raise ValueError("Unterminated C++ declaration")


def string(text):
    if not re.fullmatch(r"\s*(?:" + _STRING + r"\s*)+", text):
        return None
    try:
        return "".join(ast.literal_eval(m.group()) for m in re.finditer(_STRING, text))
    except (SyntaxError, ValueError):
        return None


def agree(left, right):
    return tuple(a if a == b else None for a, b in zip(left, right))


def add(catalog, name, facts, values=None):
    """None is sticky across versions, build variants and duplicate names."""
    entry = tuple(facts) + (values,)
    catalog[name] = agree(catalog[name], entry) if name in catalog else entry


def literal_lists(text):
    arrays = {}
    pattern = r"\bchar\s*\*\s*(?:const\s+)?(\w+)\s*\[[^]]*\]\s*=\s*\{"
    for match in re.finditer(pattern, text):
        items, _ = arguments(text, match.end(), "}")
        values = []
        for item in items:
            if item in ("", "nullptr", "NULL", "NullS", "0"):
                continue
            value = string(item)
            if value is None:
                values = None
                break
            values.append(value)
        value = tuple(values) if values else None
        name = match[1]
        arrays[name] = value if name not in arrays or arrays[name] == value else None
    for match in re.finditer(r"\bTYPELIB\s+(\w+)\s*=\s*\{", text):
        items, _ = arguments(text, match.end(), "}")
        if len(items) >= 3:
            arrays[match[1]] = arrays.get(items[2])
    return arrays


def constructor_flags(header):
    """Extract constructor forwarding, including flags hidden by subclasses.

    E.g. Sys_var_binlog_encryption adds PERSIST_AS_READ_ONLY; charptr_func
    adds READ_ONLY NON_PERSIST; proxy_user fixes ONLY_SESSION and READONLY.
    NO_CMD_LINE or an assertion on getopt.id also excludes startup options.
    These come from the tagged header, not a second handwritten variable map.
    """
    no_cmd_line = set()
    for match in re.finditer(r"\bclass\s+(Sys_var_\w+)\b[^;{]*\{", header):
        _, end = arguments(header, match.end(), "}")
        body = _TOKEN.sub(" ", header[match.end():end])
        if re.search(r"\bNO_CMD_LINE\b|\bassert\s*\(\s*getopt\.id\s*==\s*-1\s*\)", body):
            no_cmd_line.add(match[1])
    result = {}
    pattern = re.compile(r"\b(Sys_var_\w+)\s*\(")
    for match in pattern.finditer(header):
        _, end = arguments(header, match.end())
        parent = re.match(r"\s*:\s*(sys_var|Sys_var_\w+)\s*\(", header[end:])
        if parent:
            args, _ = arguments(header, end + parent.end())
            index = 3 if parent[1] == "sys_var" else 2
            if len(args) > index:
                result[match[1]] = (parent[1], args[index], match[1] in no_cmd_line)
    return result


def expanded_flags(kind, expression, constructors):
    seen = set()
    no_cmd_line = False
    while kind in constructors and kind not in seen:
        seen.add(kind)
        kind, forwarded, forced_no_cmd_line = constructors[kind]
        no_cmd_line = no_cmd_line or forced_no_cmd_line
        expression = re.sub(r"\bflag_args?\b", lambda _: expression, forwarded)
    return expression + (" NO_CMD_LINE" if no_cmd_line else "")


def core_facts(kind, args, constructors):
    expression = args[2] if len(args) > 2 else ""
    expression = expanded_flags("Sys_var_" + kind, expression, constructors)
    options = _TOKEN.sub(" ", " ".join(args[2:]))
    no_cmd_line = re.search(r"\bNO_CMD_LINE\b", options)
    if no_cmd_line:
        expression += " NO_CMD_LINE"
    expression = expression.replace("sys_var::", "")
    scope = None
    for pattern, value in ((r"\b(GLOBAL_VAR|KEYCACHE_VAR|GLOBAL)\b", "global"),
                           (r"\b(SESSION_VAR|SESSION)\b", "both"),
                           (r"\b(SESSION_ONLY|ONLY_SESSION)\b", "session")):
        if re.search(pattern, expression):
            scope = value
    expression = re.sub(r"\bREADONLY\b", "READ_ONLY", expression)
    expression = re.sub(r"\bNOTPERSIST\b", "NON_PERSIST", expression)
    expression = re.sub(r"\bPERSIST_AS_READ_ONLY\b", "PERSIST_AS_READONLY", expression)
    flags = " ".join(flag for token, flag in _FLAGS if re.search(r"\b" + token + r"\b", expression))
    # E.g. large_pages uses IF_WIN(NO_CMD_LINE, CMD_LINE(OPT_ARG)). We do
    # not choose a build platform, so conflicting option facts stay unknown.
    if no_cmd_line and re.search(r"\bCMD_LINE\b", options):
        flags = None
    # Unrecognized scope expressions cannot establish absence of READ_ONLY.
    return scope, _TYPES.get(kind), flags if scope is not None else None


def plugin_prefix(path):
    if path.startswith("storage/innobase/"):
        return "innodb_"
    if path.startswith("plugin/semisync/"):
        match = re.search(r"semisync_(source|replica|master|slave)_plugin", path)
        return "rpl_semi_sync_" + match[1] + "_" if match else None
    for folder, prefix in (("plugin/x/", "mysqlx_"), ("plugin/group_replication/", "group_replication_"),
                           ("sql/auth/sha2_password.cc", "caching_sha2_password_"),
                           ("storage/perfschema/", "performance_schema_"), ("storage/myisam/", "myisam_"),
                           ("storage/temptable/", "temptable_")):
        if path.startswith(folder):
            return prefix
    return None


def plugin_flags(expression):
    return " ".join(flag for token, flag in _PLUGIN_FLAGS if re.search(r"\b" + token + r"\b", expression))


def semisync_variants(sources):
    """8.4 builds old and new plugin names from the same translation unit."""
    result = dict(sources)
    for path, text in sources.items():
        if not path.startswith("plugin/semisync/") or "#ifdef USE_OLD_SEMI_SYNC_TERMINOLOGY" not in text:
            continue
        pattern = r"#ifdef USE_OLD_SEMI_SYNC_TERMINOLOGY\s*\n(.*?)#else\s*\n(.*?)#endif"
        result[path] = re.sub(pattern, lambda m: m[2], text, flags=re.S)
        old_path = path.replace("_source_plugin", "_master_plugin").replace("_replica_plugin", "_slave_plugin")
        if old_path != path:
            result[old_path] = re.sub(pattern, lambda m: m[1], text, flags=re.S)
    return result


def expand_wrappers(text):
    """Expand simple one-argument declaration wrappers used by semisync."""
    text = re.sub(r"\\\n", " ", text)
    wrappers = {}
    pattern = r"^\s*#define\s+(\w+)\((\w+)\)[ \t]+([^\n]+)"

    def remove(match):
        if _PLUGIN.search(match[3]):
            wrappers[match[1]] = (match[2], match[3])
            return ""
        return match.group()

    text = re.sub(pattern, remove, text, flags=re.M)
    for name, (parameter, body) in wrappers.items():
        text = re.sub(r"\b" + name + r"\((\w+)\)",
                      lambda m: re.sub(r"\b" + parameter + r"\b", m[1], body), text)
    return text


def core_declarations(text):
    """Count typed declarations independently of the initializer syntax.

    An unfamiliar declaration shape must stop generation, not silently lose
    a variable. Nonliteral function declarations are filtered by parse_sources.
    """
    declarations = list(_CORE.finditer(text))
    symbols = {match.start("symbol") for match in declarations}
    pattern = r"\bSys_var_\w+(?:\s*<[^;{}]*?>)?\s+(\w+)\b"
    missing = [match[1] for match in re.finditer(pattern, text) if match.start(1) not in symbols]
    if missing:
        raise ValueError("Unparsed core declarations: " + ", ".join(missing))
    return declarations


def parse_sources(sources):
    """Parse a {repository path: C++ text} snapshot. Also used by unit tests."""
    sources = semisync_variants({path: uncomment(text) for path, text in sources.items()})
    sources = {path: expand_wrappers(text) for path, text in sources.items()}
    constructors = constructor_flags(sources.get("sql/sys_vars.h", ""))
    names = {m[1]: string(m[2]) for text in sources.values()
             for m in re.finditer(r"^\s*#define\s+(\w+)\s+(\"[^\n]+)", text, re.M)}
    shared_lists = literal_lists(sources.get("sql/handler.cc", ""))
    catalog, symbols, aliases = {}, {}, []
    for path, text in sorted(sources.items()):
        if path != "sql/sys_vars.cc" and not _PLUGIN.search(text) and not path.startswith("components/validate_password/"):
            continue
        arrays = dict(shared_lists)
        arrays.update(literal_lists(text))
        if path == "sql/sys_vars.cc":
            for match in core_declarations(text):
                args, _ = arguments(text, match.end(), "}" if match["opener"] == "{" else ")")
                name = string(args[0]) or names.get(args[0])
                if name is None:
                    if match["static"]:
                        raise ValueError("Nonliteral core variable name: " + match["symbol"])
                    # Without static, this may be a function declaration.
                    continue
                symbols[match["symbol"]] = name
                if match["kind"] == "deprecated_alias":
                    aliases.append((name, args[1]))
                    continue
                facts = core_facts(match["kind"], args, constructors)
                values = arrays.get(args[4]) if facts[1] in ("enum", "set", "flagset") and len(args) > 4 else None
                add(catalog, name, facts, values)
        prefix = plugin_prefix(path)
        if prefix is not None:
            for match in _PLUGIN.finditer(text):
                args, _ = arguments(text, match.end())
                name = args[0]
                if not re.fullmatch(r"\w+", name):
                    raise ValueError("Nonliteral plugin variable name: " + name)
                flags = args[1 if match[1] == "THDVAR" else 2]
                if "PLUGIN_VAR_NOSYSVAR" in flags:
                    continue
                kind = _TYPES[match[2].lower()]
                scope = "both" if match[1] == "THDVAR" else "global"
                values = arrays.get(args[-1].lstrip("& ")) if kind in ("enum", "set") else None
                add(catalog, prefix + name, (scope, kind, plugin_flags(flags)), values)
        if path.startswith("components/validate_password/"):
            pattern = r"\bmysql_service_component_sys_variable_register\s*->\s*register_variable\s*\("
            for match in re.finditer(pattern, text):
                args, _ = arguments(text, match.end())
                component, name = string(args[0]), string(args[1])
                if component is None or name is None:
                    continue
                flags = args[2]
                kinds = re.findall(r"\bPLUGIN_VAR_(BOOL|INT|UINT|LONG|ULONG|LONGLONG|ULONGLONG|DOUBLE|STR|ENUM|SET)\b", flags)
                scope = "both" if "PLUGIN_VAR_THDLOCAL" in flags else "global"
                add(catalog, component + "." + name,
                    (scope, _TYPES.get(kinds[0].lower()) if len(kinds) == 1 else None, plugin_flags(flags)))
    for name, target in aliases:
        entry = catalog.get(symbols.get(target), (None, None, None, None))
        add(catalog, name, entry[:3], entry[3])
    return catalog


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source)] + list(args)).decode("utf-8")


def source_tags(source):
    tags = git(source, "tag", "--list", "mysql-8.4.*").splitlines()
    stable = [tag for tag in tags if re.fullmatch(r"mysql-8\.4\.\d+", tag)]
    if not stable:
        raise ValueError("No stable mysql-8.4.* tag found")
    return "mysql-9.7.0", max(stable, key=lambda tag: int(tag.rsplit(".", 1)[1]))


def generate(source, tags):
    merged = {}
    for tag in tags:
        paths = git(source, "ls-tree", "-r", "--name-only", tag).splitlines()
        paths = [p for p in paths if p.endswith((".cc", ".h")) and p.startswith(_ROOTS)
                 and not any(part in ("test", "tests", "unittest") for part in p.split("/"))]
        # One git process per snapshot; batch mode reads objects without checkout.
        objects = subprocess.check_output(["git", "-C", str(source), "cat-file", "--batch"],
                                          input="".join("%s:%s\n" % (tag, p) for p in paths).encode("utf-8"))
        sources, offset = {}, 0
        for path in paths:
            end = objects.index(b"\n", offset)
            size = int(objects[offset:end].split()[-1])
            sources[path] = objects[end + 1:end + 1 + size].decode("utf-8")
            offset = end + size + 2
        for name, entry in parse_sources(sources).items():
            add(merged, name, entry[:3], entry[3])
    lines = ["# Generated by scripts/gen_mysql_sysvars.py; do not edit.",
             "# Source tags: " + ", ".join(tags) + ".",
             "# Regenerate from the repository root:",
             "#   python3 scripts/gen_mysql_sysvars.py --mysql-source /path/to/mysql-server",
             "# None marks a conflicting or unresolved fact. Names may be absent on a given server.",
             "# Conditional build definitions are merged; unresolved/conditional value lists are omitted.",
             "", "SOURCE_TAGS = %r" % (tuple(tags),), "", "# name: (scope, type, flags ro/np/pro/nc; nc = no startup option)", "SYSVARS = {"]
    lines += ["    %r: %r," % (name, merged[name][:3]) for name in sorted(merged)]
    lines += ["}", "", "VALUES = {"]
    lines += ["    %r: %r," % (name, merged[name][3]) for name in sorted(merged) if merged[name][3] is not None]
    lines += ["}", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mysql-source", required=True, type=Path, help="Read-only MySQL git checkout")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "myflames/mysql_sysvars.py")
    args = parser.parse_args()
    text = generate(args.mysql_source, source_tags(args.mysql_source))
    args.output.write_text(text, encoding="utf-8")
    print("Wrote %s" % args.output)


if __name__ == "__main__":
    main()
