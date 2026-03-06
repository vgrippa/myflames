#!/usr/bin/env python3
"""Thin wrapper: bar chart from MySQL EXPLAIN JSON. Runs unified CLI with --type bargraph."""
import sys
sys.argv = [sys.argv[0], "--type", "bargraph"] + sys.argv[1:]
if __name__ == "__main__":
    from myflames.cli import main
    main()
