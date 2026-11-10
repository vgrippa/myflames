#!/usr/bin/env python3
"""Generate the SQL keyword and native-function catalog for every cataloged MySQL and MariaDB release.

Reads `sql/lex.h`, `sql/sql_yacc.yy`, `sql/item_create.cc` and MariaDB's
`sql/item_geofunc.cc` from the MySQL and MariaDB git checkouts
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
import json
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
        out = {}  # type: Dict[str, str]
        for oid in unique:
            # Drain each response before sending more: both pipes are bounded.
            proc.stdin.write((oid + "\n").encode("ascii"))
            proc.stdin.flush()
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
    cleaned = _strip_c_comments_and_strings(lex_text)
    end = _balanced_end(cleaned, start - 1)
    return lex_text[start:end]


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



# Bounds describe the registry's argument-count envelope, not argument types or
# parity (SQL_FN_ODD/EVEN). Unknown expressions must never become guessed counts.
FunctionBounds = Tuple[Optional[int], Any]
UNKNOWN_BOUNDS = (None, None)  # type: FunctionBounds


def _balanced_end(text: str, start: int, opening: str = "{", closing: str = "}") -> int:
    """Return the matching delimiter in comment/string-masked source."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opening:
            depth += 1
        elif text[i] == closing:
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("Unclosed source delimiter at {}".format(start))


def _count_constant(expr: str) -> Any:
    expr = expr.strip()
    if re.fullmatch(r"\d+[uUlL]*", expr):
        return int(re.sub(r"[uUlL]+$", "", expr))
    # mysql-8.0.36:sql/item_create.cc:129 uses the argument-list size type's
    # numeric maximum as the variadic sentinel. No arbitrary constants evaluated.
    if expr == "MAX_ARGLIST_SIZE":
        return "unbounded"
    return None


def _classes(clean: str) -> Dict[str, Tuple[str, str]]:
    classes = {}
    pattern = r"\bclass\s+(\w+)(?:\s*:\s*public\s+(\w+))?\s*\{"
    for match in re.finditer(pattern, clean):
        end = _balanced_end(clean, match.end() - 1)
        classes[match.group(1)] = (match.group(2) or "", clean[match.end():end])
    return classes


def parse_mysql_functions(source: str) -> Dict[str, FunctionBounds]:
    """MySQL 8+: explicit SQL_FN bounds and SQL_FACTORY instantiator constants."""
    clean = _strip_c_comments_and_strings(source)
    classes = _classes(clean)
    aliases = dict(re.findall(r"\busing\s+(\w+)\s*=\s*([^;]+);", clean))

    def factory_bounds(name: str, seen: Set[str]) -> FunctionBounds:
        name = name.strip()
        root = name.split("<", 1)[0].strip()
        if root in seen:
            return UNKNOWN_BOUNDS
        seen = seen | {root}
        if root in aliases:
            return factory_bounds(aliases[root], seen)
        # The spatial aliases select the explicit 1/2-argument specializations.
        # mysql-8.0.36:sql/item_create.cc:195-215,589-601.
        if root == "Instantiator_with_functype":
            match = re.search(r",\s*([12])\s*>$", name)
            if match:
                count = int(match.group(1))
                return count, count
        body = classes.get(root, ("", ""))[1]
        values = []
        for field in ("Min_argcount", "Max_argcount"):
            match = re.search(r"\b" + field + r"\s*=\s*([^;]+);", body)
            values.append(_count_constant(match.group(1)) if match else None)
        if not isinstance(values[0], int) or values[1] is None:
            return UNKNOWN_BOUNDS
        return values[0], values[1]

    body = _extract_c_array_section(source, "func_array")
    masked = _strip_c_comments_and_strings(body)
    functions = {}
    pattern = r'\{\s*"([A-Z0-9_]+)"\s*,\s*(SQL_\w+)\s*\('
    for match in re.finditer(pattern, body):
        if masked[match.start()] != "{":
            continue
        name, macro = match.groups()
        if "INTERNAL" in name or "INTERNAL" in macro:
            continue
        end = _balanced_end(masked, match.end() - 1, "(", ")")
        args = masked[match.end():end].strip()
        bounds = UNKNOWN_BOUNDS
        if macro == "SQL_FACTORY":
            bounds = factory_bounds(args, set())
        elif macro in ("SQL_FN", "SQL_FN_LIST"):
            count = _count_constant(args.rsplit(",", 1)[-1])
            if isinstance(count, int):
                bounds = count, count
        elif macro in ("SQL_FN_V", "SQL_FN_V_THD", "SQL_FN_V_LIST",
                       "SQL_FN_V_LIST_THD", "SQL_FN_ODD", "SQL_FN_EVEN"):
            parts = args.rsplit(",", 2)
            if len(parts) == 3:
                lo, hi = _count_constant(parts[1]), _count_constant(parts[2])
                if isinstance(lo, int) and hi is not None:
                    bounds = lo, hi
        functions[name] = bounds
    return dict(sorted(functions.items()))


def _guard_bounds(condition: str, accepts: bool) -> FunctionBounds:
    """Bounds for a deliberately small, fully matched constant-check language."""
    wrapper = re.fullmatch(r"(?:unlikely|likely)\s*\((.*)\)", condition, re.S)
    if wrapper:
        condition = wrapper.group(1).strip()
    negated = re.fullmatch(r"!\s*\((.*)\)", condition, re.S)
    if negated:
        return _guard_bounds(negated.group(1).strip(), not accepts)
    # Equality alternatives and their rejected complement.
    separator, operator = (r"\|\|", "==") if accepts else (r"&&", "!=")
    parts = re.split(separator, condition)
    equalities = [re.fullmatch(r"\s*arg_count\s*" + operator + r"\s*(\d+)\s*", x)
                  for x in parts]
    if all(equalities):
        values = sorted(set(int(x.group(1)) for x in equalities if x))
        if len(values) == values[-1] - values[0] + 1:
            return values[0], values[-1]
        return UNKNOWN_BOUNDS
    parts = re.split(r"&&" if accepts else r"\|\|", condition)
    lo, hi = 0, "unbounded"
    for part in parts:
        check = re.fullmatch(r"\s*arg_count\s*(<=|>=|<|>)\s*(\d+)\s*", part)
        if not check:
            return UNKNOWN_BOUNDS
        op, value = check.group(1), int(check.group(2))
        if accepts:
            op = {"<": ">=", "<=": ">", ">": "<=", ">=": "<"}[op]
        if op in ("<", "<="):
            lo = max(lo, value + int(op == "<="))
        else:
            upper = value - int(op == ">=")
            hi = upper if hi == "unbounded" else min(hi, upper)
    return (lo, hi) if hi == "unbounded" or lo <= hi else UNKNOWN_BOUNDS


def _unconditional_count_error(block: str) -> bool:
    return ("ER_WRONG_PARAMCOUNT_TO_NATIVE_FCT" in block
            and not re.search(r"\b(if|switch|for|while)\b|\?", block))


def _native_body_bounds(body: str) -> FunctionBounds:
    """Recognize universal rejection guards or exhaustive constant-count switches.

    This is not a C++ evaluator: mode-dependent, parity-dependent and delegated
    validation stay unknown. Lower-only bounds describe the registry builder,
    not runtime argument types or other server resource limits.
    """
    # Hide nested blocks: a check in only one path cannot establish global bounds.
    depth = 0
    top = []
    for char in body:
        top.append(char if depth == 0 else " ")
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    top = "".join(top)
    bounds = []
    covered_errors = 0
    error_name = "ER_WRONG_PARAMCOUNT_TO_NATIVE_FCT"
    for match in re.finditer(r"\bif\s*\(", top):
        end = _balanced_end(body, match.end() - 1, "(", ")")
        condition = body[match.end():end].strip()
        tail = body[end + 1:]
        brace = re.match(r"\s*\{", tail)
        if brace:
            start = end + 1 + brace.end() - 1
            block_end = _balanced_end(body, start)
            block = body[start + 1:block_end]
        else:
            # A single unbraced error call is also an unconditional rejection.
            statement = re.match(r"\s*my_error\s*\([^;]+;", tail)
            if not statement:
                continue
            block = statement.group(0)
            block_end = end + statement.end()
        accepts = False
        if error_name not in block:
            alternate = re.match(r"\s*else\s*\{", body[block_end + 1:])
            if not alternate:
                continue
            start = block_end + 1 + alternate.end() - 1
            block = body[start + 1:_balanced_end(body, start)]
            accepts = True
        if not _unconditional_count_error(block):
            continue
        candidate = _guard_bounds(condition, accepts)
        if candidate == UNKNOWN_BOUNDS:
            return UNKNOWN_BOUNDS
        bounds.append(candidate)
        covered_errors += block.count(error_name)

    for match in re.finditer(r"\bswitch\s*\(\s*arg_count\s*\)\s*\{", top):
        end = _balanced_end(body, match.end() - 1)
        switch = body[match.end():end]
        if re.search(r"\bswitch\b", switch):
            return UNKNOWN_BOUNDS
        cases = re.findall(r"\bcase\s+(\d+)\s*:", switch)
        if not cases or len(cases) != len(re.findall(r"\bcase\b", switch)):
            return UNKNOWN_BOUNDS
        default = re.search(r"\bdefault\s*:", switch)
        error_tail = switch[default.end():] if default else body[end + 1:]
        if not _unconditional_count_error(error_tail):
            # MariaDB MASTER_*_WAIT checks a finite interval first, then
            # dispatches every allowed count without an error/default arm.
            # A dispatch alone never proves which other counts are rejected.
            values = sorted(set(int(x) for x in cases))
            if bounds and not default and error_name not in switch:
                lower = max(b[0] for b in bounds)
                uppers = [b[1] for b in bounds if isinstance(b[1], int)]
                if (uppers and values[0] == lower and values[-1] == min(uppers)
                        and len(values) == values[-1] - values[0] + 1):
                    continue
            return UNKNOWN_BOUNDS
        if default and re.search(r"\bcase\b|\bnew\b", error_tail):
            return UNKNOWN_BOUNDS
        if not default:
            branches = re.split(r"\bcase\s+\d+\s*:", switch)[1:]
            if any(not re.search(r"\breturn\s+new\b", b) for b in branches):
                return UNKNOWN_BOUNDS
        values = sorted(set(int(x) for x in cases))
        if len(values) != values[-1] - values[0] + 1:
            return UNKNOWN_BOUNDS
        bounds.append((values[0], values[-1]))
        covered_errors += error_tail.count(error_name)

    # Do not silently ignore a second validation path (including nested checks).
    if not bounds or covered_errors != body.count(error_name):
        return UNKNOWN_BOUNDS
    lo = max(b[0] for b in bounds)
    upper = [b[1] for b in bounds if isinstance(b[1], int)]
    hi = min(upper) if upper else "unbounded"
    return (lo, hi) if hi == "unbounded" or lo <= hi else UNKNOWN_BOUNDS


def parse_legacy_functions(source: str) -> Dict[str, FunctionBounds]:
    """MySQL 5.7 / MariaDB registries, excluding Oracle-mode overrides."""
    clean = _strip_c_comments_and_strings(source)
    classes = _classes(clean)
    bodies = {}
    for match in re.finditer(r"\b(\w+)::create_native\s*\([^;{}]*\)\s*\{", clean):
        bodies[match.group(1)] = clean[match.end():_balanced_end(clean, match.end() - 1)]

    def class_bounds(name: str, seen: Set[str]) -> FunctionBounds:
        if name in seen:
            return UNKNOWN_BOUNDS
        parent, body = classes.get(name, ("", ""))
        fixed = re.fullmatch(r"Create_func_arg([0-3])", parent)
        if fixed:
            count = int(fixed.group(1))
            return count, count
        implementation = bodies.get(name)
        if implementation is None:
            inline = re.search(r"\bcreate_native\s*\([^;{}]*\)\s*(?:override\s*)?\{", body)
            if inline:
                implementation = body[inline.end():_balanced_end(body, inline.end() - 1)]
        if implementation is not None:
            return _native_body_bounds(implementation)
        if parent and parent != "Create_native_func":
            return class_bounds(parent, seen | {name})
        return UNKNOWN_BOUNDS

    functions = {}
    # Main and geometry arrays, plus any same-format additional native registry.
    # Restrict to registry initializers so Oracle overrides cannot replace defaults.
    pattern = r"\bNative_func_registry\s+(\w+)\s*\[\s*\]\s*=\s*\{"
    for array in re.finditer(pattern, clean):
        if "oracle" in array.group(1).lower():
            continue
        end = _balanced_end(clean, array.end() - 1)
        original = source[array.end():end]
        masked = clean[array.end():end]
        entry = r'(?:C_)?STRING_WITH_LEN\s*\(\s*"([A-Z0-9_]+)"\s*\)\s*\}\s*,\s*(?:GEOM_)?BUILDER\s*\(\s*(\w+)\s*\)'
        for match in re.finditer(entry, original):
            if not masked[match.start():].startswith(("STRING_WITH_LEN", "C_STRING_WITH_LEN")):
                continue
            name, builder = match.groups()
            if "INTERNAL" not in name:
                functions[name] = class_bounds(builder, set())
    return dict(sorted(functions.items()))


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
            "functions": {name: (minimum, maximum), ...},
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
    prev_functions = dict(first.get("functions", {}))

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
        curr_functions = dict(rel.get("functions", {}))
        delta["functions"] = {
            name: bounds for name, bounds in sorted(curr_functions.items())
            if name not in prev_functions or bounds != prev_functions[name]
        }
        removed_functions = sorted(set(prev_functions) - set(curr_functions))
        if removed_functions:
            delta["functionsRemoved"] = removed_functions
        prev_functions = curr_functions
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
    functions = {}

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

        for name in rel.get("functionsRemoved", ()):
            functions.pop(name, None)
        functions.update(rel.get("functions", {}))
        keywords = [
            {"name": name, "reserved": kw_map[name]} for name in sorted(kw_map)
        ]
        out.append(
            {
                "release": rel["release"],
                "tag": rel["tag"],
                "keywords": keywords,
                "grammarFunctions": sorted(fn_set),
                "functions": dict(sorted(functions.items())),
            }
        )
    return out


def collect_engine_catalog(
    repo: Path,
    versions: Sequence[Tuple[Any, ...]],
    engine: str,
) -> Dict[str, Dict[str, Any]]:
    """Read lexer, grammar and native registries for every cataloged release tag."""
    git = Git(repo)
    try:
        tags = release_tags(versions)
        tag_blobs = {}  # type: Dict[str, Tuple[str, ...]]
        needed_oids = []  # type: List[str]
        for version, tag, _series in tags:
            paths = ["sql/lex.h", "sql/sql_yacc.yy", "sql/item_create.cc"]
            if engine == "mariadb":
                paths.append("sql/item_geofunc.cc")
            tree = git.tree(tag, paths)
            lex_oid = tree.get("sql/lex.h", "")
            yacc_oid = tree.get("sql/sql_yacc.yy", "")
            if not lex_oid or not yacc_oid:
                raise RuntimeError(
                    "Tag {} ({}) in {} is missing sql/lex.h or sql/sql_yacc.yy".format(
                        tag, version, repo
                    )
                )
            if any(path not in tree for path in paths):
                raise RuntimeError("Missing function source in {}".format(tag))
            tag_blobs[tag] = tuple(tree[path] for path in paths)
            needed_oids.extend(tag_blobs[tag])

        blob_texts = git.texts(needed_oids)
        parse_fn = parse_mysql_words if engine == "mysql" else parse_mariadb_words

        parsed_cache = {}  # type: Dict[Tuple[str, str], Tuple[List[Dict[str, Any]], List[str]]]
        function_cache = {}  # type: Dict[Tuple[str, ...], Dict[str, FunctionBounds]]
        by_series = {}  # type: Dict[str, List[Dict[str, Any]]]
        for version, tag, series in tags:
            blobs = tag_blobs[tag]
            pair = blobs[:2]
            if pair not in parsed_cache:
                parsed_cache[pair] = parse_fn(
                    blob_texts[pair[0]], blob_texts[pair[1]]
                )
            keywords, grammar_functions = parsed_cache[pair]
            function_key = blobs[2:]
            if function_key not in function_cache:
                source = "\n".join(blob_texts[oid] for oid in function_key)
                function_parser = (parse_mysql_functions
                                   if engine == "mysql" and series != "5.7"
                                   else parse_legacy_functions)
                function_cache[function_key] = function_parser(source)
                if not function_cache[function_key]:
                    raise RuntimeError("Parsed zero native functions from {}".format(tag))
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
                    "functions": function_cache[function_key],
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


def _format_functions(functions: Dict[str, FunctionBounds]) -> str:
    # Group names with the same bounds. Unknown is '?', unbounded is '*'.
    groups = {}  # type: Dict[str, List[str]]
    for name, bounds in sorted(functions.items()):
        key = ",".join("?" if x is None else "*" if x == "unbounded" else str(x)
                       for x in bounds)
        groups.setdefault(key, []).append(name)
    if not groups:
        return "{}"
    fields = [json.dumps(key) + ": " + json.dumps(" ".join(groups[key]))
              for key in sorted(groups)]
    return "{ " + ", ".join(fields) + " }"


def _format_release_delta(delta: Dict[str, Any], function_ref: str = "") -> str:
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
    if delta.get("functionsRemoved"):
        fields.append("functionsRemoved: {}".format(_format_str_list(delta["functionsRemoved"])))
    fields.append("functions: " + (function_ref or _format_functions(delta.get("functions", {}))))
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
        '// Bounds key: "min,max"; ? means unknown, * means unbounded. Value: space-separated names.',
        "export type GeneratedFunctionEntry = Readonly<Record<string, string>>;",
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
        "  readonly functionsRemoved?: readonly string[];",
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

    ]

    function_sets = {}  # type: Dict[str, int]
    for catalog in (mysql_series, mariadb_series):
        for series in sorted(catalog, key=_version_sort_key):
            for rel in catalog[series]["releases"]:
                packed = _format_functions(rel.get("functions", {}))
                if packed != "{}" and packed not in function_sets:
                    function_sets[packed] = len(function_sets)
    lines.append("const FUNCTION_SETS: readonly GeneratedFunctionEntry[] = [")
    for packed in function_sets:
        lines.append("  " + packed + ",")
    lines.append("];\n")
    lines.append("export const ENGINE_WORDS_CATALOG: GeneratedEngineWordsCatalog = {")

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
                packed = _format_functions(rel.get("functions", {}))
                ref = "FUNCTION_SETS[{}]".format(function_sets[packed]) if packed != "{}" else "{}"
                lines.append("        {},".format(_format_release_delta(rel, ref)))
            lines.append("      ],")
            lines.append("    },")
        lines.append("  },")

    lines.append("};")
    lines.append("")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate SQL word and function catalogs from MySQL and MariaDB git checkouts."
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
