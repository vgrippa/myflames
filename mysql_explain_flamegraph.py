#!/usr/bin/env python3
"""Thin wrapper: flame graph from MySQL EXPLAIN JSON. Runs unified CLI with --type flamegraph."""
import sys
import os
# Allow running from repo root or with -m
sys.argv = [sys.argv[0], "--type", "flamegraph"] + sys.argv[1:]
if __name__ == "__main__":
    from myflames.cli import main
    main()
