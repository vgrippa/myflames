#!/usr/bin/env bash
# Runs the Python unit/integration test suite.
#
# Tests run with a throwaway HOME and the file vault, so no test can read or
# write the developer's real ~/.myflames (workspace database, saved passwords)
# or the OS keychain, even if it forgets its own temporary HOME.
set -euo pipefail

test_home=$(mktemp -d)
trap 'rm -rf "$test_home"' EXIT

HOME="$test_home" MYFLAMES_SECRET_BACKEND=vault \
  python3 -m unittest discover -s test -p "test_*.py" -v
