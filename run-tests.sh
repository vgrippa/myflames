#!/usr/bin/env bash
# Runs the Python suite in isolated parallel module processes (stdlib only).
# Set JOBS=1 for the original single-process order when debugging. Extra
# arguments go to scripts/run_tests_parallel.py (for example --jobs 4).
#
# Tests run with a throwaway HOME and the file vault, so no test can read or
# write the developer's real ~/.myflames (workspace database, saved passwords)
# or the OS keychain, even if it forgets its own temporary HOME.
set -euo pipefail

test_home=$(mktemp -d)
trap 'rm -rf "$test_home"' EXIT

HOME="$test_home" USERPROFILE="$test_home" MYFLAMES_SECRET_BACKEND=vault \
  python3 scripts/run_tests_parallel.py --skip-allowlist test/skip_allowlist.txt "$@"
