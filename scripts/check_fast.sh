#!/bin/sh
# Fast pre-handback check for a patch: one command before handing work back.
#
#   scripts/check_fast.sh [FILE ...]
#
# The files to check are the arguments (paths relative to the repository
# root). With no arguments, and git available inside a work tree, the files
# changed against `git merge-base HEAD origin/master` plus staged, unstaged
# and untracked files are checked. With neither, print usage and exit 2.
#
# Steps (each prints one PASS/FAIL line, details on the lines above it):
#   python-3.7-syntax   ast.parse(feature_version=(3, 7)), AST checks for
#                       newer syntax, plus regexes for
#                       str.removeprefix/removesuffix, functools.cache,
#                       f-string "=" and parenthesized `with (` on changed
#                       .py files under myflames/, scripts/, test/ and
#                       apps/desktop/scripts/.
#   python-tests        test/test_<m>.py for each changed myflames/<m>.py,
#                       and each changed test/test_*.py itself, each in its
#                       own throwaway HOME with MYFLAMES_SECRET_BACKEND=vault
#                       and MYFLAMES_LIVE_DOCKER=0, up to 4 at a time.
#   typescript          tsc --noEmit -p . in apps/workspace when anything
#                       under apps/workspace/src or apps/workspace/test
#                       changed (skipped with a message when node_modules
#                       is missing).
#   shell               sh -n on each changed .sh file.
#   workflow-yaml       ruby -ryaml parse of each changed .github/workflows
#                       file (skipped with a message when ruby is missing).
#
# Exit status: 0 when every step passes, 1 when any step fails, 2 on usage
# errors. python-3.7-syntax is a fast local approximation: the CI python37
# job remains the authority.

set -u

script_dir=${0%/*}
[ "$script_dir" = "$0" ] && script_dir=.
ROOT=$(CDPATH= cd "$script_dir/.." && pwd) || exit 2
cd "$ROOT" || exit 2

failed=0
failed_steps=""
mark_failed() {
  failed=1
  case " $failed_steps " in
    *" $1 "*) ;;
    *) failed_steps="$failed_steps $1" ;;
  esac
}

if [ "$#" -gt 0 ]; then
  files="$*"
else
  if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    base=$(git merge-base HEAD origin/master 2>/dev/null || true)
    files=$(
      {
        if [ -n "$base" ]; then git diff --name-only "$base" --; fi
        git diff --name-only --cached --
        git diff --name-only --
        git ls-files --others --exclude-standard
      } | sort -u
    )
    if [ -z "$files" ]; then
      echo "check_fast: no changed files against ${base:-origin/master}; nothing to check"
      exit 0
    fi
  else
    echo "usage: scripts/check_fast.sh [FILE ...]" >&2
    exit 2
  fi
fi

# Normalize: absolute paths under the root become relative; anything outside
# the root or no longer on disk is reported and skipped.
checked=""
for f in $files; do
  case "$f" in
    /*)
      case "$f" in
        "$ROOT"/*) f=${f#"$ROOT"/} ;;
        *) echo "note: $f is outside the repository root; skipped"; continue ;;
      esac ;;
  esac
  if [ -f "$f" ]; then
    checked="$checked $f"
  else
    echo "note: $f does not exist; skipped"
  fi
done
files=$checked

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# --- python-3.7-syntax ---------------------------------------------------
: > "$tmp/py-files"
for f in $files; do
  case "$f" in
    myflames/*.py|scripts/*.py|test/*.py|apps/desktop/scripts/*.py)
      printf '%s\n' "$f" >> "$tmp/py-files" ;;
  esac
done
if [ -s "$tmp/py-files" ]; then
  if command -v python3 >/dev/null 2>&1; then
    if python3 - "$tmp/py-files" <<'PYEOF'
import ast
import re
import sys

failures = 0
named_expr = getattr(ast, "NamedExpr", ())
match_statement = getattr(ast, "Match", ())

def report(path, lineno, reason):
    global failures
    failures += 1
    print("FAIL python-3.7-syntax %s:%d: %s" % (path, lineno, reason))

# Things ast.parse(feature_version=(3, 7)) cannot see on a newer host:
# newer stdlib calls, f-string "=", parenthesized with ( items.
stdlib_checks = [
    (re.compile(r"\.removeprefix\("), "str.removeprefix needs Python 3.9"),
    (re.compile(r"\.removesuffix\("), "str.removesuffix needs Python 3.9"),
    (re.compile(r"\bfunctools\.cache\b|from\s+functools\s+import[^\n]*\bcache\b"),
     "functools.cache needs Python 3.9"),
]
fstring = re.compile(r"(?i)(?:^|[^\w])[rub]{0,2}f(\"[^\"\n]*\"|'[^'\n]*')")
self_documenting = re.compile(r"=\s*[}:]")
# Only an "as" inside the first parentheses is the 3.10 form: a 3.7-valid
# parenthesized expression such as with (root / "a").open() as f stays legal.
with_paren = re.compile(r"^\s*(?:async\s+)?with\s*\((?:[^()]|\([^()]*\))*\bas\b")

with open(sys.argv[1], encoding="utf-8") as handle:
    paths = [line.strip() for line in handle if line.strip()]

for path in paths:
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        print("FAIL python-3.7-syntax %s: unreadable: %s" % (path, exc))
        failures += 1
        continue
    try:
        try:
            tree = ast.parse(source, filename=path, feature_version=(3, 7))
        except TypeError:
            # Python 3.7 has no feature_version: use its native parser.
            tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        report(path, exc.lineno or 0, "not Python 3.7 syntax: %s" % exc.msg)
        continue
    # feature_version is best-effort; some hosts still accept newer syntax.
    # The optional node classes and posonlyargs do not exist on Python 3.7.
    for node in ast.walk(tree):
        if isinstance(node, named_expr):
            report(path, node.lineno,
                   "not Python 3.7 syntax: assignment expressions need Python 3.8")
        elif isinstance(node, ast.arguments) and getattr(node, "posonlyargs", ()):
            # arguments has no lineno; its first positional-only argument does.
            report(path, node.posonlyargs[0].lineno,
                   "not Python 3.7 syntax: positional-only parameters need Python 3.8")
        elif isinstance(node, match_statement):
            report(path, node.lineno,
                   "not Python 3.7 syntax: match statements need Python 3.10")
    for lineno, line in enumerate(source.splitlines(), 1):
        for pattern, reason in stdlib_checks:
            if pattern.search(line):
                report(path, lineno, reason)
        for match in fstring.finditer(line):
            if self_documenting.search(match.group(1)):
                report(path, lineno, "self-documenting f-string \"=\" needs Python 3.8")
                break
        if with_paren.match(line):
            report(path, lineno,
                   "parenthesized with ( items need Python 3.10 "
                   "(a single parenthesized context manager is 3.7-legal: drop the parens)")

sys.exit(1 if failures else 0)
PYEOF
    then
      echo "PASS python-3.7-syntax"
    else
      echo "FAIL python-3.7-syntax"
      mark_failed python-3.7-syntax
    fi
    echo "note: python-3.7-syntax is a fast local approximation; the CI python37 job remains the authority."
  else
    echo "FAIL python-3.7-syntax (python3 not found)"
    mark_failed python-3.7-syntax
  fi
else
  echo "PASS python-3.7-syntax (no Python files to check)"
fi

# --- python-tests --------------------------------------------------------
: > "$tmp/test-files-raw"
for f in $files; do
  case "$f" in
    myflames/*.py)
      m=${f##*/}
      m=${m%.py}
      if [ -f "test/test_$m.py" ]; then printf '%s\n' "test/test_$m.py" >> "$tmp/test-files-raw"; fi ;;
    test/test_*.py)
      printf '%s\n' "$f" >> "$tmp/test-files-raw" ;;
  esac
done
sort -u "$tmp/test-files-raw" > "$tmp/test-files"
if [ -s "$tmp/test-files" ]; then
  if command -v python3 >/dev/null 2>&1; then
    : > "$tmp/test-results"
    n=0
    while IFS= read -r t; do
      (
        home=$(mktemp -d)
        HOME="$home" USERPROFILE="$home" MYFLAMES_SECRET_BACKEND=vault MYFLAMES_LIVE_DOCKER=0 \
          python3 -m unittest discover -s test -p "${t##*/}" > "$home/out" 2>&1
        code=$?
        printf '%s %s\n' "$code" "$t" >> "$tmp/test-results"
        if [ "$code" -ne 0 ]; then
          echo "--- python-test $t failed (exit $code); last lines:"
          tail -n 15 "$home/out"
        fi
        rm -rf "$home"
      ) &
      n=$((n + 1))
      [ $((n % 4)) -eq 0 ] && wait
    done < "$tmp/test-files"
    wait
    step_failed=0
    while read -r code t; do
      if [ "$code" -eq 0 ]; then
        echo "PASS python-test $t"
      else
        echo "FAIL python-test $t"
        step_failed=1
      fi
    done < "$tmp/test-results"
    if [ "$step_failed" -eq 0 ]; then
      echo "PASS python-tests ($(wc -l < "$tmp/test-files" | tr -d ' ') modules)"
    else
      echo "FAIL python-tests"
      mark_failed python-tests
    fi
  else
    echo "FAIL python-tests (python3 not found)"
    mark_failed python-tests
  fi
else
  echo "PASS python-tests (no test modules to run)"
fi

# --- typescript ----------------------------------------------------------
ts=0
for f in $files; do
  case "$f" in
    apps/workspace/src/*|apps/workspace/test/*) ts=1; break ;;
  esac
done
if [ "$ts" -eq 1 ]; then
  if [ ! -d apps/workspace/node_modules ]; then
    echo "SKIP typescript (apps/workspace/node_modules missing; run npm ci in apps/workspace)"
  elif [ ! -x apps/workspace/node_modules/.bin/tsc ]; then
    echo "SKIP typescript (tsc not installed in apps/workspace)"
  else
    if (cd apps/workspace && ./node_modules/.bin/tsc --noEmit -p .); then
      echo "PASS typescript"
    else
      echo "FAIL typescript"
      mark_failed typescript
    fi
  fi
else
  echo "PASS typescript (no workspace files changed)"
fi

# --- shell ---------------------------------------------------------------
sh_files=""
for f in $files; do
  case "$f" in
    *.sh) sh_files="$sh_files $f" ;;
  esac
done
if [ -n "$sh_files" ]; then
  step_failed=0
  for f in $sh_files; do
    if sh -n "$f" 2> "$tmp/sh-err"; then
      echo "PASS shell $f"
    else
      echo "FAIL shell $f: $(cat "$tmp/sh-err")"
      step_failed=1
    fi
  done
  if [ "$step_failed" -ne 0 ]; then
    echo "FAIL shell"
    mark_failed shell
  fi
else
  echo "PASS shell (no shell files changed)"
fi

# --- workflow-yaml -------------------------------------------------------
yaml_files=""
for f in $files; do
  case "$f" in
    .github/workflows/*.yml|.github/workflows/*.yaml) yaml_files="$yaml_files $f" ;;
  esac
done
if [ -n "$yaml_files" ]; then
  if command -v ruby >/dev/null 2>&1; then
    step_failed=0
    for f in $yaml_files; do
      if ruby -ryaml -e 'YAML.parse_file(ARGV[0])' "$f" 2> "$tmp/yaml-err"; then
        echo "PASS workflow-yaml $f"
      else
        echo "FAIL workflow-yaml $f: $(cat "$tmp/yaml-err")"
        step_failed=1
      fi
    done
    if [ "$step_failed" -ne 0 ]; then
      echo "FAIL workflow-yaml"
      mark_failed workflow-yaml
    fi
  else
    echo "SKIP workflow-yaml (ruby not found; workflows not parsed)"
  fi
fi

# --- summary -------------------------------------------------------------
echo
if [ "$failed" -eq 0 ]; then
  echo "check_fast: PASS"
  exit 0
fi
echo "check_fast: FAIL (failed steps:$failed_steps)"
exit 1
