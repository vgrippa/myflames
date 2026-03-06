#!/usr/bin/env bash
# Runs the full test suite: Python unit/integration tests + Perl regression tests.
#
# Usage:
#   ./run-tests.sh           # all tests
#   ./run-tests.sh python    # Python tests only
#   ./run-tests.sh perl      # Perl regression only
set -euo pipefail

MODE="${1:-all}"

run_python() {
  echo "=== Python tests ==="
  python3 -m unittest discover -s test -p "test_myflames.py" -v
}

run_perl() {
  echo "=== Perl regression tests (stackcollapse-perf.pl) ==="
  ./test.sh
}

case "$MODE" in
  python) run_python ;;
  perl)   run_perl ;;
  all)
    run_python
    echo ""
    run_perl
    echo ""
    echo "All tests passed."
    ;;
  *)
    echo "Usage: $0 [all|python|perl]"
    exit 1
    ;;
esac
