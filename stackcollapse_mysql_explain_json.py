#!/usr/bin/env python3
"""
Convert MySQL EXPLAIN ANALYZE FORMAT=JSON output to folded stack format
for use with flamegraph tools.
Usage:
  python stackcollapse_mysql_explain_json.py [options] explain.json | flamegraph.pl > query.svg
  Or use: python -m myflames explain.json > query.svg  (unified Python tool)
"""
import sys
import argparse
import re
import json
from myflames.parser import load_explain_json, parse_node, build_flame_entries


def main():
    ap = argparse.ArgumentParser(description="Convert MySQL EXPLAIN JSON to folded stack format.")
    ap.add_argument("--use-total", action="store_true", help="Use total time instead of self time")
    ap.add_argument("--time-unit", choices=["ms", "us", "s"], default="ms", help="Time unit (default: ms)")
    ap.add_argument("input", nargs="?", default="-", help="Input JSON file or - for stdin")
    args = ap.parse_args()

    if args.input == "-":
        text = sys.stdin.read()
    else:
        with open(args.input, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

    text = text.strip()
    text = re.sub(r"^.*?EXPLAIN:\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"^\*+.*?\*+\s*", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"Failed to parse JSON: {e}\n")
        sys.exit(1)

    root = parse_node(data)
    if not root:
        sys.stderr.write("No valid EXPLAIN ANALYZE JSON data found.\n")
        sys.exit(1)

    multiplier = 1
    if args.time_unit == "us":
        multiplier = 1000
    elif args.time_unit == "s":
        multiplier = 0.001

    def node_at_path(path):
        node = root
        for label in path[1:]:  # path[0] is root's label
            found = None
            for c in node["children"]:
                if c["folded_label"] == label:
                    found = c
                    break
            if found is None:
                return None
            node = found
        return node

    max_time = 0
    lines = []
    for path, self_time in build_flame_entries(root):
        if args.use_total:
            n = node_at_path(path)
            t = n["total_time"] if n else self_time
        else:
            t = self_time
        t *= multiplier
        if t > 0.0001:
            max_time = max(max_time, t)
            lines.append((path, t))

    use_us = max_time > 0 and max_time < 1
    if use_us:
        for i, (path, t) in enumerate(lines):
            lines[i] = (path, int(t * 1000 + 0.5))
    else:
        for i, (path, t) in enumerate(lines):
            lines[i] = (path, int(t + 0.5))

    for path, t in lines:
        t = 1 if t == 0 and len(path) == 1 else t
        if t <= 0:
            continue
        sys.stdout.write(";".join(path) + " " + str(t) + "\n")


if __name__ == "__main__":
    main()
