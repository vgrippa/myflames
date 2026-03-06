#!/usr/bin/env python3
"""
Unified command: flame graph, bar chart, or treemap from MySQL EXPLAIN ANALYZE JSON.
Usage:
  python mysql_explain.py [--type flamegraph|bargraph|treemap] [options] explain.json > output.svg
"""
from myflames.cli import main

if __name__ == "__main__":
    main()
