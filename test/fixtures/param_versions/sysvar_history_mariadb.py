# Test fixture in the format of the generated myflames/sysvar_history_mariadb.py
# (scripts/gen_sysvar_history.py). Written by hand. Lineage: 10.6.0 -> 10.11.0
# -> 11.4.0, each series' first release following the earlier series by date.
ENGINE = "mariadb"
GENERATED = {"on": "2026-10-28", "runtime": "fixture, no servers were run",
             "source": "https://github.com/MariaDB/server", "manual": {"10.6": "2026-10-28"}}
VERSIONS = (
    ("10.6.0", "mariadb-10.6.0", "stable", "2021-07-06", None, "image"),
    ("10.11.0", "mariadb-10.11.0", "stable", "2023-02-01", 0, "image"),
    ("11.4.0", "mariadb-11.4.0", "stable", "2024-03-29", 1, "image"),
)
SKIPPED = (
    ("10.7.1", "No image or package could be started: the container exited at startup."),
)
FIELDS = ("scope", "type", "dynamic", "default", "default_note", "min", "max", "values",
          "deprecated", "replacement", "alias_of", "plugin")
HISTORY = {
    # Shared with the MySQL fixture: history without an engine lists both.
    "always_on": (
        (0, ("global", "boolean", True, "ON", None, None, None, None, None, None, None, None)),
    ),
    "mariadb_only": (
        (0, ("global", "string", False, "strict", None, None, None, None, None, None, None, None)),
    ),
    # Default change at a series boundary (10.6.0 -> 10.11.0).
    "changed_var": (
        (0, ("global", "integer", True, "10", None, "1", "100", None, None, None, None, None)),
        (1, ("global", "integer", True, "20", None, "1", "100", None, None, None, None, None)),
    ),
}
