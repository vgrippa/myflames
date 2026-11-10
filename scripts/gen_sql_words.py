#!/usr/bin/env python3
"""Generate the SQL keyword catalog for every cataloged MySQL and MariaDB release.

Reads `sql/lex.h` and `sql/sql_yacc.yy` from the MySQL and MariaDB git checkouts
at every tag in `myflames/sysvar_history_mysql.py` and
`myflames/sysvar_history_mariadb.py` without checking out files in the working
tree (`git ls-tree` and `git cat-file --batch`).

Usage:
    python3 scripts/gen_sql_words.py \\
        --mysql-source /path/to/mysql-server \\
        --mariadb-source /path/to/mariadb-server \\
        --out apps/workspace/src/editor/engineWords.generated.ts
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from myflames.sysvar_history_mariadb import VERSIONS as MARIADB_VERSIONS  # noqa: E402
from myflames.sysvar_history_mysql import VERSIONS as MYSQL_VERSIONS  # noqa: E402

DEFAULT_OUT = ROOT / "apps" / "workspace" / "src" / "editor" / "engineWords.generated.ts"

# Parser-internal lookahead / control tokens that are never user-facing SQL words.
# PARSE_GCOL_EXPR (PARSE_GCOL_EXPR_SYM): mysql-5.7.44:sql/lex.h:448 declares it as SYM,
# but sql/sql_yacc.yy:6471-6481 states that only the server may use it while reading
# a .frm file and rejects user statements (!Lex->parse_gcol_expr -> ER_SYNTAX_ERROR);
# it is not in MySQL 8.0's sql/lex.h.
INTERNAL_TOKENS = frozenset([
    "ABORT_SYM",
    "EMPTY_FROM_CLAUSE",
    "END_OF_INPUT",
    "FORCE_LOOKAHEAD",
    "Lookup",
    "NOT2_SYM",
    "NEG",
    "PARAM_MARKER",
    "PARSE_GCOL_EXPR",
    "PARSE_GCOL_EXPR_SYM",
    "SHIFT_PRECEDENCE",
    "UMINUS",
    "WITH_CUBE_SYM",
    "WITH_ROLLUP_SYM",
    "YYEOF",
    "YYerror",
    "YYUNDEF",
])

INTERNAL_TOKEN_PREFIXES = (
    "GRAMMAR_SELECTOR_",
    "OBSOLETE_TOKEN_",
)

# MySQL 5.7 predates sql/gen_keyword_list.cc (added in 8.0.11 by WL#8989).
# In MySQL 5.7's sql/sql_yacc.yy, `ident` expands to `IDENT_sys | keyword` and
# `label_ident` expands to `IDENT_sys | keyword_sp`, with `%type <symbol> keyword keyword_sp`.
MYSQL_57_KEYWORD_RULES = (
    "keyword",
    "keyword_sp",
    "ident_keyword",
    "ident_keywords_unambiguous",
    "ident_keywords_ambiguous_1_roles_and_labels",
    "ident_keywords_ambiguous_2_labels",
    "ident_keywords_ambiguous_3_roles",
    "ident_keywords_ambiguous_4_system_variables",
    "role_keyword",
    "label_keyword",
)

# MariaDB sql/sql_yacc.yy defines `ident: IDENT_sys | keyword_ident` and
# `ident_cli: IDENT | IDENT_QUOTED | keyword_ident`. Any token reachable from
# `keyword_ident` is accepted as an unquoted identifier and is non-reserved.
# For small unit-test fixtures that omit the top-level `keyword_ident` wrapper
# and define one of its constituent rules directly, we also list the constituent
# non-reserved keyword rule names below (excluding `keyword_sysvar_name`, which
# contains `OFFSET_SYM` for `@@offset` while `OFFSET` remains reserved for
# general identifiers in `reserved_keyword_udt_not_param_type`).
MARIADB_KEYWORD_RULES = (
    "keyword_ident",
    "keyword_directly_assignable",
    "keyword_sp_var_and_label",
    "keyword_func_sp_var_and_label",
    "keyword_sp_var_not_label",
    "keyword_func_sp_var_not_label",
    "keyword_sp_head",
    "keyword_sp_block_section",
    "keyword_verb_clause",
    "keyword_data_type",
    "keyword_cast_type",
    "keyword_sysvar_type",
    "keyword_set_special_case",
    "keyword_sp",
    "keyword",
)


def _run(repo: Path, args: Sequence[str], input_bytes: bytes = b"") -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("git {} in {} failed: {}".format(" ".join(args), repo, err))
    return proc.stdout


class Git(object):
    """Read-only git helper using `git ls-tree` and `git cat-file --batch`."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._proc = None  # type: Optional[subprocess.Popen]

    def tree(self, tag: str, paths: Sequence[str] = ("sql",)) -> Dict[str, str]:
        raw = _run(self.repo, ["ls-tree", "-r", tag, "--"] + list(paths)).decode(
            "utf-8", errors="replace"
        )
        out = {}  # type: Dict[str, str]
        for line in raw.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            meta, path = parts
            meta_parts = meta.split()
            if len(meta_parts) >= 3 and meta_parts[1] == "blob":
                out[path] = meta_parts[2]
        return out

    def _batch(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                ["git", "-C", str(self.repo), "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
        return self._proc

    def texts(self, oids: Iterable[str]) -> Dict[str, str]:
        unique = [oid for oid in dict.fromkeys(oids) if oid]
        if not unique:
            return {}
        proc = self._batch()
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(("".join(oid + "\n" for oid in unique)).encode("ascii"))
        proc.stdin.flush()
        out = {}  # type: Dict[str, str]
        for oid in unique:
            header = proc.stdout.readline().decode("ascii", errors="replace").strip()
            parts = header.split()
            if len(parts) < 3 or parts[1] == "missing":
                raise RuntimeError("Missing git blob {} in {}".format(oid, self.repo))
            size = int(parts[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing LF
            out[oid] = data.decode("utf-8", errors="replace")
        return out

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            self._proc.wait()
        self._proc = None


def _is_sql_word(word: str) -> bool:
    """Return True when `word` starts with an ASCII letter (matching isalpha in gen_keyword_list.cc)."""
    return bool(word) and ("A" <= word[0] <= "Z" or "a" <= word[0] <= "z")


def _is_internal_token(token: str) -> bool:
    if not token or token in INTERNAL_TOKENS:
        return True
    for prefix in INTERNAL_TOKEN_PREFIXES:
        if token.startswith(prefix):
            return True
    return False


def _strip_c_comments_and_strings(text: str) -> str:
    """Replace C comments and string/char literals with spaces so braces/colons inside them are ignored."""
    out = []  # type: List[str]
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            out.append("  ")
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(" ")
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_bison_actions(text: str) -> str:
    """Remove `{ ... }` action blocks from comment/string-stripped Bison grammar text."""
    out = []  # type: List[str]
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
            out.append(" ")
        elif ch == "}":
            if depth > 0:
                depth -= 1
            out.append(" ")
        elif depth > 0:
            out.append("\n" if ch == "\n" else " ")
        else:
            out.append(ch)
    return "".join(out)


def _extract_grammar_section(yacc_text: str) -> str:
    """Extract the Bison rules section between the first and second standalone `%%` markers."""
    cleaned = _strip_c_comments_and_strings(yacc_text)
    parts = re.split(r"(?m)^%%\s*$", cleaned)
    if len(parts) >= 2:
        return parts[1]
    return cleaned


def _parse_bison_rules(yacc_text: str) -> Dict[str, List[str]]:
    """Parse Bison rule bodies into `{rule_name: [symbol, ...]}`."""
    grammar = _strip_bison_actions(_extract_grammar_section(yacc_text))
    rules = {}  # type: Dict[str, List[str]]
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*:(.*?);", grammar, re.S):
        lhs = match.group(1)
        body = match.group(2)
        refs = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", body)
        rules.setdefault(lhs, []).extend(refs)
    return rules


def _expand_bison_rules(rules: Dict[str, List[str]], roots: Iterable[str]) -> Set[str]:
    """Transitively expand `roots` through `rules` and return all visited symbols."""
    visited = set()  # type: Set[str]
    stack = [r for r in roots if r in rules]
    while stack:
        curr = stack.pop()
        if curr in visited:
            continue
        visited.add(curr)
        for sym in rules.get(curr, ()):
            if sym not in visited:
                stack.append(sym)
    return visited


def _extract_c_array_section(lex_text: str, array_name: str) -> str:
    """Extract the initializer body of a named `SYMBOL ... array_name[] = { ... };` array."""
    pattern = re.compile(
        r"\b" + re.escape(array_name) + r"\s*\[\s*\]\s*=\s*\{",
        re.M,
    )
    match = pattern.search(lex_text)
    if not match:
        return ""
    start = match.end()
    end_match = re.search(r"^\s*\}\s*;", lex_text[start:], re.M)
    if end_match:
        return lex_text[start : start + end_match.start()]
    return lex_text[start:]


def parse_mysql_lex(lex_text: str) -> Tuple[Dict[str, str], List[str]]:
    """Parse MySQL `sql/lex.h` and return `(keyword_name_to_token, grammar_functions)`.

    Follows MySQL's `sql/gen_keyword_list.cc`:
    - `SYM(word, tok)` (`SG_KEYWORDS`) and `SYM_HK(word, tok)` (`SG_HINTABLE_KEYWORDS`)
      are SQL keywords when `word[0].isalpha()` and `tok` is not parser-internal.
    - `SYM_H(word, tok)` (`SG_HINTS`) are optimizer-hint-only tokens and are dropped.
    - `SYM_FN(word, tok)` (`SG_FUNCTIONS`) are kept apart as `grammarFunctions`.
    """
    body = _extract_c_array_section(lex_text, "symbols") or lex_text
    cleaned = _strip_c_comments_and_strings(body)
    # Match macro invocations in the original body that also exist at the same position in cleaned
    entry_re = re.compile(
        r'\b(SYM(?:_HK|_H|_FN)?)\s*\(\s*"([^"]+)"\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)'
    )
    keywords = {}  # type: Dict[str, str]
    functions = set()  # type: Set[str]
    for match in entry_re.finditer(body):
        # Ignore matches inside comments by checking that cleaned still has the macro name
        pos = match.start(1)
        macro = match.group(1)
        if cleaned[pos : pos + len(macro)] != macro:
            continue
        raw_word = match.group(2)
        token = match.group(3)
        word = raw_word.upper()
        if not _is_sql_word(word) or _is_internal_token(token) or _is_internal_token(word):
            continue
        if macro in ("SYM", "SYM_HK"):
            keywords[word] = token
        elif macro == "SYM_FN":
            functions.add(word)
        # SYM_H (optimizer hint only) is intentionally skipped.
    return keywords, sorted(functions)


def parse_mysql_nonreserved_tokens(yacc_text: str) -> Set[str]:
    """Return the set of non-reserved token symbols from MySQL `sql/sql_yacc.yy`.

    - MySQL 8.0.11+ (`sql/gen_keyword_list.cc`): `%token<keyword> TOK` (8.0.11-8.0.15)
      or `%token<lexer.keyword> TOK` (8.0.16-26.10.0), plus `%left`/`%right`/`%nonassoc`
      with the same `<keyword>` / `<lexer.keyword>` semantic type tag.
    - MySQL 5.7 (predating `sql/gen_keyword_list.cc`): tokens reachable from `keyword`
      and `keyword_sp` (the rules marked `%type <symbol> keyword keyword_sp`).
    """
    tag_re = re.compile(
        r"^%(?:token|left|right|nonassoc)\s*<(?:lexer\.)?keyword>\s*([A-Za-z_][A-Za-z0-9_]*)",
        re.M,
    )
    tagged = set(tag_re.findall(yacc_text))
    if tagged:
        return tagged

    rules = _parse_bison_rules(yacc_text)
    return _expand_bison_rules(rules, MYSQL_57_KEYWORD_RULES)


def parse_mysql_words(
    lex_text: str, yacc_text: str
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse MySQL `sql/lex.h` and `sql/sql_yacc.yy` into `(keywords, grammar_functions)`."""
    kw_tokens, grammar_functions = parse_mysql_lex(lex_text)
    non_reserved_tokens = parse_mysql_nonreserved_tokens(yacc_text)
    keywords = [
        {"name": name, "reserved": token not in non_reserved_tokens}
        for name, token in sorted(kw_tokens.items())
    ]
    return keywords, grammar_functions


def preprocess_mariadb_yacc(yacc_text: str, mode: str = "MARIADB") -> str:
    """Preprocess MariaDB `%ifdef` / `%else` / `%endif` directives (matching `sql/gen_yy_files.cmake`)."""
    out = []  # type: List[str]
    stack = [True]  # type: List[bool]
    for line in yacc_text.splitlines(True):
        m_ifdef = re.match(r"^%ifdef\s+(\w+)", line)
        if m_ifdef:
            cond = m_ifdef.group(1) == mode
            stack.append(stack[-1] and cond)
            out.append("\n")
            continue
        if re.match(r"^%else\b", line):
            if len(stack) > 1:
                parent = stack[-2]
                stack[-1] = parent and (not stack[-1])
            out.append("\n")
            continue
        if re.match(r"^%endif\b", line):
            if len(stack) > 1:
                stack.pop()
            out.append("\n")
            continue
        if stack[-1]:
            out.append(line)
        else:
            out.append("\n")
    return "".join(out)


def parse_mariadb_lex(lex_text: str) -> Tuple[Dict[str, str], List[str]]:
    """Parse MariaDB `sql/lex.h` and return `(keyword_name_to_token, grammar_functions)`.

    - `symbols[]` entries `{ "WORD", SYM(TOK) }` with `WORD[0].isalpha()` are SQL keywords.
    - `sql_functions[]` entries `{ "WORD", SYM(TOK) }` with `WORD[0].isalpha()` are
      `grammarFunctions` (names only).
    """
    symbols_body = _extract_c_array_section(lex_text, "symbols")
    funcs_body = _extract_c_array_section(lex_text, "sql_functions")
    entry_re = re.compile(
        r'\{\s*"([^"]+)"\s*,\s*SYM(?:_FN)?\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\}'
    )

    keywords = {}  # type: Dict[str, str]
    clean_symbols = _strip_c_comments_and_strings(symbols_body)
    for match in entry_re.finditer(symbols_body):
        # Verify the opening brace is not inside a comment
        if clean_symbols[match.start()] != "{":
            continue
        word = match.group(1).upper()
        token = match.group(2)
        if not _is_sql_word(word) or _is_internal_token(token) or _is_internal_token(word):
            continue
        keywords[word] = token

    functions = set()  # type: Set[str]
    clean_funcs = _strip_c_comments_and_strings(funcs_body)
    for match in entry_re.finditer(funcs_body):
        if clean_funcs[match.start()] != "{":
            continue
        word = match.group(1).upper()
        token = match.group(2)
        if not _is_sql_word(word) or _is_internal_token(token) or _is_internal_token(word):
            continue
        functions.add(word)

    return keywords, sorted(functions)


def parse_mariadb_nonreserved_tokens(yacc_text: str) -> Set[str]:
    """Return the set of non-reserved token symbols from MariaDB `sql/sql_yacc.yy`.

    In MariaDB's grammar, `ident` (`IDENT_sys | keyword_ident`) and `ident_cli`
    (`IDENT | IDENT_QUOTED | keyword_ident`) accept any token reachable from
    `keyword_ident` as an unquoted identifier. When a small fixture snippet
    defines sub-rules such as `keyword_sp`, `keyword_data_type`, or
    `keyword_func_sp_var_and_label` without wrapping them in `keyword_ident`,
    all present rules in `MARIADB_KEYWORD_RULES` are expanded.
    """
    preprocessed = preprocess_mariadb_yacc(yacc_text, mode="MARIADB")
    rules = _parse_bison_rules(preprocessed)
    if "keyword_ident" in rules:
        roots = ["keyword_ident"]
    else:
        roots = [r for r in MARIADB_KEYWORD_RULES if r in rules]
    return _expand_bison_rules(rules, roots)


def parse_mariadb_words(
    lex_text: str, yacc_text: str
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse MariaDB `sql/lex.h` and `sql/sql_yacc.yy` into `(keywords, grammar_functions)`."""
    kw_tokens, grammar_functions = parse_mariadb_lex(lex_text)
    non_reserved_tokens = parse_mariadb_nonreserved_tokens(yacc_text)
    keywords = [
        {"name": name, "reserved": token not in non_reserved_tokens}
        for name, token in sorted(kw_tokens.items())
    ]
    return keywords, grammar_functions


def series_of_version(version: str) -> str:
    parts = version.split(".")
    if len(parts) < 2:
        raise ValueError("Invalid release version: {!r}".format(version))
    return "{}.{}".format(parts[0], parts[1])


def release_tags(versions: Sequence[Tuple[Any, ...]]) -> List[Tuple[str, str, str]]:
    """Extract `(version, tag, series)` tuples from a `sysvar_history_*.VERSIONS` sequence."""
    out = []  # type: List[Tuple[str, str, str]]
    for entry in versions:
        version = str(entry[0])
        tag = str(entry[1])
        out.append((version, tag, series_of_version(version)))
    return out


def encode_series(releases: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Encode a chronological list of release word dictionaries for a single `major.minor` series.

    Each input element has:
        {
            "release": str,
            "tag": str,
            "keywords": [{"name": str, "reserved": bool}, ...],
            "grammarFunctions": [str, ...],
            "functions": {},
        }
    """
    if not releases:
        raise ValueError("encode_series requires at least one release")

    first = releases[0]
    first_kw = {
        item["name"]: bool(item["reserved"]) for item in first["keywords"]
    }
    base_reserved = sorted(name for name, res in first_kw.items() if res)
    base_non_reserved = sorted(name for name, res in first_kw.items() if not res)
    base_grammar_fns = sorted(set(first.get("grammarFunctions", ())))

    encoded_releases = [
        {
            "release": first["release"],
            "tag": first["tag"],
            "functions": dict(first.get("functions", {})),
        }
    ]  # type: List[Dict[str, Any]]

    prev_kw = dict(first_kw)
    prev_fns = set(base_grammar_fns)

    for rel in releases[1:]:
        curr_kw = {
            item["name"]: bool(item["reserved"]) for item in rel["keywords"]
        }
        curr_fns = set(rel.get("grammarFunctions", ()))

        added_reserved = sorted(
            name for name, res in curr_kw.items() if name not in prev_kw and res
        )
        added_non_reserved = sorted(
            name for name, res in curr_kw.items() if name not in prev_kw and not res
        )
        removed = sorted(name for name in prev_kw if name not in curr_kw)
        to_reserved = sorted(
            name
            for name, res in curr_kw.items()
            if name in prev_kw and not prev_kw[name] and res
        )
        to_non_reserved = sorted(
            name
            for name, res in curr_kw.items()
            if name in prev_kw and prev_kw[name] and not res
        )
        fn_added = sorted(curr_fns - prev_fns)
        fn_removed = sorted(prev_fns - curr_fns)

        delta = {
            "release": rel["release"],
            "tag": rel["tag"],
        }  # type: Dict[str, Any]
        if added_reserved or added_non_reserved:
            added_obj = {}  # type: Dict[str, List[str]]
            if added_reserved:
                added_obj["reserved"] = added_reserved
            if added_non_reserved:
                added_obj["nonReserved"] = added_non_reserved
            delta["added"] = added_obj
        if removed:
            delta["removed"] = removed
        if to_reserved or to_non_reserved:
            changed_obj = {}  # type: Dict[str, List[str]]
            if to_reserved:
                changed_obj["toReserved"] = to_reserved
            if to_non_reserved:
                changed_obj["toNonReserved"] = to_non_reserved
            delta["reservedChanged"] = changed_obj
        if fn_added:
            delta["grammarFunctionsAdded"] = fn_added
        if fn_removed:
            delta["grammarFunctionsRemoved"] = fn_removed
        delta["functions"] = dict(rel.get("functions", {}))
        encoded_releases.append(delta)

        prev_kw = curr_kw
        prev_fns = curr_fns

    return {
        "baseRelease": first["release"],
        "baseTag": first["tag"],
        "baseReserved": base_reserved,
        "baseNonReserved": base_non_reserved,
        "baseGrammarFunctions": base_grammar_fns,
        "releases": encoded_releases,
    }


def decode_series(series_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Reconstruct the per-release word lists from an `encode_series` structure."""
    kw_map = {}  # type: Dict[str, bool]
    for name in series_data["baseReserved"]:
        kw_map[name] = True
    for name in series_data["baseNonReserved"]:
        kw_map[name] = False
    fn_set = set(series_data["baseGrammarFunctions"])

    out = []  # type: List[Dict[str, Any]]
    for idx, rel in enumerate(series_data["releases"]):
        if idx > 0:
            for name in rel.get("removed", ()):
                kw_map.pop(name, None)
            added = rel.get("added", {})
            for name in added.get("reserved", ()):
                kw_map[name] = True
            for name in added.get("nonReserved", ()):
                kw_map[name] = False
            changed = rel.get("reservedChanged", {})
            for name in changed.get("toReserved", ()):
                kw_map[name] = True
            for name in changed.get("toNonReserved", ()):
                kw_map[name] = False
            for name in rel.get("grammarFunctionsRemoved", ()):
                fn_set.discard(name)
            for name in rel.get("grammarFunctionsAdded", ()):
                fn_set.add(name)

        keywords = [
            {"name": name, "reserved": kw_map[name]} for name in sorted(kw_map)
        ]
        out.append(
            {
                "release": rel["release"],
                "tag": rel["tag"],
                "keywords": keywords,
                "grammarFunctions": sorted(fn_set),
                "functions": dict(rel.get("functions", {})),
            }
        )
    return out


def collect_engine_catalog(
    repo: Path,
    versions: Sequence[Tuple[Any, ...]],
    engine: str,
) -> Dict[str, Dict[str, Any]]:
    """Read `sql/lex.h` and `sql/sql_yacc.yy` for every cataloged tag in `versions`."""
    git = Git(repo)
    try:
        tags = release_tags(versions)
        tag_blobs = {}  # type: Dict[str, Tuple[str, str]]
        needed_oids = []  # type: List[str]
        for version, tag, _series in tags:
            tree = git.tree(tag, ("sql/lex.h", "sql/sql_yacc.yy"))
            lex_oid = tree.get("sql/lex.h", "")
            yacc_oid = tree.get("sql/sql_yacc.yy", "")
            if not lex_oid or not yacc_oid:
                raise RuntimeError(
                    "Tag {} ({}) in {} is missing sql/lex.h or sql/sql_yacc.yy".format(
                        tag, version, repo
                    )
                )
            tag_blobs[tag] = (lex_oid, yacc_oid)
            needed_oids.append(lex_oid)
            needed_oids.append(yacc_oid)

        blob_texts = git.texts(needed_oids)
        parse_fn = parse_mysql_words if engine == "mysql" else parse_mariadb_words

        parsed_cache = {}  # type: Dict[Tuple[str, str], Tuple[List[Dict[str, Any]], List[str]]]
        by_series = {}  # type: Dict[str, List[Dict[str, Any]]]
        for version, tag, series in tags:
            pair = tag_blobs[tag]
            if pair not in parsed_cache:
                parsed_cache[pair] = parse_fn(
                    blob_texts[pair[0]], blob_texts[pair[1]]
                )
            keywords, grammar_functions = parsed_cache[pair]
            if not keywords:
                raise RuntimeError(
                    "Parsed zero keywords from {} ({}) in {}".format(tag, version, repo)
                )
            by_series.setdefault(series, []).append(
                {
                    "release": version,
                    "tag": tag,
                    "keywords": keywords,
                    "grammarFunctions": grammar_functions,
                    "functions": {},
                }
            )

        encoded = {}  # type: Dict[str, Dict[str, Any]]
        for series, rel_list in by_series.items():
            encoded[series] = encode_series(rel_list)
        return encoded
    finally:
        git.close()


def _version_sort_key(series_or_version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in series_or_version.split("."))


def _format_str_list(items: Sequence[str]) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join('"{}"'.format(item) for item in items) + "]"


def _format_release_delta(delta: Dict[str, Any]) -> str:
    fields = [
        'release: "{}"'.format(delta["release"]),
        'tag: "{}"'.format(delta["tag"]),
    ]
    added = delta.get("added")
    if added:
        added_parts = []
        if added.get("reserved"):
            added_parts.append(
                "reserved: {}".format(_format_str_list(sorted(added["reserved"])))
            )
        if added.get("nonReserved"):
            added_parts.append(
                "nonReserved: {}".format(
                    _format_str_list(sorted(added["nonReserved"]))
                )
            )
        if added_parts:
            fields.append("added: {{ {} }}".format(", ".join(added_parts)))
    removed = delta.get("removed")
    if removed:
        fields.append("removed: {}".format(_format_str_list(sorted(removed))))
    changed = delta.get("reservedChanged")
    if changed:
        changed_parts = []
        if changed.get("toReserved"):
            changed_parts.append(
                "toReserved: {}".format(
                    _format_str_list(sorted(changed["toReserved"]))
                )
            )
        if changed.get("toNonReserved"):
            changed_parts.append(
                "toNonReserved: {}".format(
                    _format_str_list(sorted(changed["toNonReserved"]))
                )
            )
        if changed_parts:
            fields.append(
                "reservedChanged: {{ {} }}".format(", ".join(changed_parts))
            )
    fn_added = delta.get("grammarFunctionsAdded")
    if fn_added:
        fields.append(
            "grammarFunctionsAdded: {}".format(_format_str_list(sorted(fn_added)))
        )
    fn_removed = delta.get("grammarFunctionsRemoved")
    if fn_removed:
        fields.append(
            "grammarFunctionsRemoved: {}".format(
                _format_str_list(sorted(fn_removed))
            )
        )
    fields.append("functions: {}")
    return "{ " + ", ".join(fields) + " }"


def render_generated_ts(
    mysql_series: Dict[str, Dict[str, Any]],
    mariadb_series: Dict[str, Dict[str, Any]],
) -> str:
    """Render deterministic TypeScript source for `engineWords.generated.ts`."""
    mysql_tags = []  # type: List[str]
    for s in sorted(mysql_series, key=_version_sort_key):
        for rel in mysql_series[s]["releases"]:
            mysql_tags.append(rel["tag"])

    mariadb_tags = []  # type: List[str]
    for s in sorted(mariadb_series, key=_version_sort_key):
        for rel in mariadb_series[s]["releases"]:
            mariadb_tags.append(rel["tag"])

    mysql_tag_summary = (
        "{}..{} ({} tags)".format(mysql_tags[0], mysql_tags[-1], len(mysql_tags))
        if mysql_tags
        else "none"
    )
    mariadb_tag_summary = (
        "{}..{} ({} tags)".format(
            mariadb_tags[0], mariadb_tags[-1], len(mariadb_tags)
        )
        if mariadb_tags
        else "none"
    )

    lines = [
        "// GENERATED FILE - DO NOT EDIT.",
        "// Generated by: python3 scripts/gen_sql_words.py --mysql-source <mysql-server> --mariadb-source <mariadb-server> --out apps/workspace/src/editor/engineWords.generated.ts",
        "// MySQL tags: {}".format(mysql_tag_summary),
        "//   " + ", ".join(mysql_tags),
        "// MariaDB tags: {}".format(mariadb_tag_summary),
        "//   " + ", ".join(mariadb_tags),
        "",
        "export type GeneratedFunctionEntry = Record<string, never>;",
        "",
        "export type GeneratedReleaseWordsDelta = {",
        "  readonly release: string;",
        "  readonly tag: string;",
        "  readonly added?: {",
        "    readonly reserved?: readonly string[];",
        "    readonly nonReserved?: readonly string[];",
        "  };",
        "  readonly removed?: readonly string[];",
        "  readonly reservedChanged?: {",
        "    readonly toReserved?: readonly string[];",
        "    readonly toNonReserved?: readonly string[];",
        "  };",
        "  readonly grammarFunctionsAdded?: readonly string[];",
        "  readonly grammarFunctionsRemoved?: readonly string[];",
        "  readonly functions: GeneratedFunctionEntry;",
        "};",
        "",
        "export type GeneratedSeriesWords = {",
        "  readonly baseRelease: string;",
        "  readonly baseTag: string;",
        "  readonly baseReserved: string;",
        "  readonly baseNonReserved: string;",
        "  readonly baseGrammarFunctions: string;",
        "  readonly releases: readonly GeneratedReleaseWordsDelta[];",
        "};",
        "",
        "export type GeneratedEngineWordsCatalog = {",
        "  readonly mysql: Record<string, GeneratedSeriesWords>;",
        "  readonly mariadb: Record<string, GeneratedSeriesWords>;",
        "};",
        "",
        "export const ENGINE_WORDS_CATALOG: GeneratedEngineWordsCatalog = {",
    ]

    for engine_name, catalog in (("mysql", mysql_series), ("mariadb", mariadb_series)):
        lines.append("  {}: {{".format(engine_name))
        for series in sorted(catalog, key=_version_sort_key):
            sdata = catalog[series]
            base_res = " ".join(sorted(sdata["baseReserved"]))
            base_non_res = " ".join(sorted(sdata["baseNonReserved"]))
            base_fns = " ".join(sorted(sdata["baseGrammarFunctions"]))
            lines.append('    "{}": {{'.format(series))
            lines.append('      baseRelease: "{}",'.format(sdata["baseRelease"]))
            lines.append('      baseTag: "{}",'.format(sdata["baseTag"]))
            lines.append('      baseReserved: "{}",'.format(base_res))
            lines.append('      baseNonReserved: "{}",'.format(base_non_res))
            lines.append('      baseGrammarFunctions: "{}",'.format(base_fns))
            lines.append("      releases: [")
            for rel in sdata["releases"]:
                lines.append("        {},".format(_format_release_delta(rel)))
            lines.append("      ],")
            lines.append("    },")
        lines.append("  },")

    lines.append("};")
    lines.append("")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate SQL keyword catalogs from MySQL and MariaDB git checkouts."
    )
    parser.add_argument(
        "--mysql-source",
        default=os.environ.get("MYSQL_SOURCE", "/Users/viniciusgrippa/Downloads/git/mysql-server"),
        help="Path to the MySQL git checkout.",
    )
    parser.add_argument(
        "--mariadb-source",
        default=os.environ.get("MARIADB_SOURCE", "/Users/viniciusgrippa/Downloads/git/mariadb-server"),
        help="Path to the MariaDB git checkout.",
    )
    parser.add_argument(
        "--out",
        "--output",
        dest="out",
        default=str(DEFAULT_OUT),
        help="Output TypeScript path (default: %(default)s).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Regenerate in memory and exit 1 if the output file differs.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    mysql_repo = Path(args.mysql_source)
    mariadb_repo = Path(args.mariadb_source)
    out_path = Path(args.out)

    if not mysql_repo.is_dir():
        sys.stderr.write("MySQL checkout not found: {}\n".format(mysql_repo))
        return 1
    if not mariadb_repo.is_dir():
        sys.stderr.write("MariaDB checkout not found: {}\n".format(mariadb_repo))
        return 1

    mysql_series = collect_engine_catalog(mysql_repo, MYSQL_VERSIONS, "mysql")
    mariadb_series = collect_engine_catalog(mariadb_repo, MARIADB_VERSIONS, "mariadb")
    rendered = render_generated_ts(mysql_series, mariadb_series)

    if args.check:
        if not out_path.is_file():
            sys.stderr.write("Missing generated file: {}\n".format(out_path))
            return 1
        current = out_path.read_text(encoding="utf-8")
        if current != rendered:
            sys.stderr.write(
                "Generated file {} is out of date. Run scripts/gen_sql_words.py to update.\n".format(
                    out_path
                )
            )
            return 1
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
