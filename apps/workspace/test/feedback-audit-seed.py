"""Seed a temporary workspace database for test/feedback-audit.mjs.

Usage: python3 apps/workspace/test/feedback-audit-seed.py STORE_PATH

Two saved connections (a Development lab and a Production server, no
passwords) and one saved investigation with two plans from test/fixtures.
Run with HOME set to a temporary directory and MYFLAMES_SECRET_BACKEND=vault;
nothing reads ~/.myflames.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
sys.path.insert(0, ROOT)
from myflames.workspace_store import WorkspaceStore  # noqa: E402

store = WorkspaceStore(sys.argv[1])
store.save("profile", {"name": "Lab primary", "host": "127.0.0.1", "port": 3306, "user": "root",
                       "database": "", "ssl_mode": "", "type": "development"}, key="p-lab")
store.save("profile", {"name": "Billing replica", "host": "10.20.0.9", "port": 3306, "user": "app",
                       "database": "", "ssl_mode": "", "type": "production"}, key="p-other")


def raw(name):
    with open(os.path.join(ROOT, "test", "fixtures", name), encoding="utf-8") as f:
        return f.read()


store.save("investigation", {
    "schema_version": "investigation-1.0", "name": "Checkout slowdown", "notes": "Before and after the index.",
    "tags": ["orders"], "baseline_id": "plan-a",
    "plans": [{"id": "plan-a", "name": "Before index", "raw": raw("explain-001-table-scan-users-no-filter.json")},
              {"id": "plan-b", "name": "After index", "raw": raw("explain-008-index-scan-users-by-country.json")}],
}, key="inv-1")
print("seeded")
