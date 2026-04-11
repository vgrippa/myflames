#!/usr/bin/env bash
# Runs the Python unit/integration test suite.
set -euo pipefail

python3 -m unittest discover -s test -p "test_*.py" -v
