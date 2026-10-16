"""The statement splitter twins agree: myflames/dbops.py split_statements with
myflames/editor_session.py statement_at (what the server runs) and
apps/workspace/src/sqlSelection.ts (what the Query editor tints). Both run
the cases in apps/workspace/test/fixtures/sql-split.json; the Node half is
apps/workspace/test/sql-selection.mjs."""
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from myflames import dbops, editor_session  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "apps" / "workspace" / "test" / "fixtures" / "sql-split.json"
_LEAD = re.compile(r"\A(?:\s+|--(?=\s|\Z)[^\n]*|#[^\n]*|/\*(?![M]?!)(?:.|\n)*?\*/)")


def lead_stripped(text):
    """*text* without leading white space and leading comments (executable
    /*! and /*M! comments are code and stay)."""
    while True:
        match = _LEAD.match(text)
        if not match or not match.group():
            return text
        text = text[match.end():]


class SplitTwinTests(unittest.TestCase):
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_statement_texts(self):
        for case in self.cases["statements"]:
            with self.subTest(sql=case["sql"]):
                parts = dbops.split_statements(case["sql"], case.get("backslash", True))
                self.assertEqual([lead_stripped(p["text"]) for p in parts], case["texts"])

    def test_the_statement_at_the_cursor(self):
        for case in self.cases["cursors"]:
            with self.subTest(sql=case["sql"]):
                cursor = case["sql"].index("|")
                sql = case["sql"].replace("|", "", 1)
                parts = dbops.split_statements(sql, case.get("backslash", True))
                chosen = editor_session.statement_at(sql, parts, cursor)
                self.assertEqual(parts.index(chosen), case["index"])


if __name__ == "__main__":
    unittest.main()
