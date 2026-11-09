# Test fixture in the format of the generated myflames/sysvar_history_mysql.py
# (scripts/gen_sysvar_history.py). Written by hand; small but covering every
# case test_param_versions.py exercises. Version lineage (previous index):
# 8.0.11 -> 8.0.30 -> 8.4.0 -> 8.4.3, and 9.0.0 follows 8.4.0 because the tag
# of 8.4.3 (2024-10-15) is dated after the tag of 9.0.0 (2024-07-01).
ENGINE = "mysql"
GENERATED = {"on": "2026-10-28", "runtime": "fixture, no servers were run",
             "source": "https://github.com/mysql/mysql-server", "manual": {"8.0": "2026-10-28"}}
VERSIONS = (
    ("8.0.11", "mysql-8.0.11", "GA", "2018-04-08", None, "image"),
    ("8.0.30", "mysql-8.0.30", "GA", "2022-07-26", 0, "image"),
    ("8.4.0", "mysql-8.4.0", "LTS", "2024-04-30", 1, "image"),
    ("8.4.3", "mysql-8.4.3", "LTS", "2024-10-15", 2, "image"),
    ("9.0.0", "mysql-9.0.0", "INNOVATION", "2024-07-01", 2, "image"),
)
SKIPPED = (
    # 8.4.1 sits between 8.4.0 and 8.4.3 on the 8.4 line (dated after 9.0.0, so
    # not on 9.0.0's step); changes seen at 8.4.3 may have come with it.
    ("8.4.1", "No official image or package of this release could be found.", "2024-07-23"),
    ("9.1.0", "No image or package could be started: the container exited at startup."),
)
FIELDS = ("scope", "type", "dynamic", "default", "default_note", "min", "max", "values",
          "deprecated", "replacement", "alias_of", "plugin")
HISTORY = {
    # Present from the first cataloged version, never changed.
    "always_on": (
        (0, ("global", "boolean", True, "ON", None, None, None, None, None, None, None, None)),
    ),
    # Added in 8.4.0.
    "added_later": (
        (0, None),
        (2, ("both", "integer", True, "10", None, "1", "100", None, None, None, None, None)),
    ),
    # Removed in 8.4.0.
    "removed_var": (
        (0, ("global", "string", False, "/tmp/log", None, None, None, None, None, None, None, None)),
        (2, None),
    ),
    # Removed in 8.4.0 and re-added in the 9.0 series.
    "readded_var": (
        (0, ("global", "boolean", True, "OFF", None, None, None, None, None, None, None, None)),
        (2, None),
        (4, ("global", "boolean", True, "ON", None, None, None, None, None, None, None, None)),
    ),
    # Deprecated alias of real_var, deprecated in every cataloged release.
    "old_alias": (
        (0, ("global", "integer", True, "100", None, "0", None, None,
             ("server", "source"), "real_var", "real_var", None)),
    ),
    "real_var": (
        (0, ("global", "integer", True, "100", None, "0", None, None, None, None, None, None)),
    ),
    # Default change inside the 8.0 series (8.0.11 -> 8.0.30).
    "buffer_size": (
        (0, ("global", "integer", True, "100", None, "0", "1024", None, None, None, None, None)),
        (1, ("global", "integer", True, "200", None, "0", "1024", None, None, None, None, None)),
    ),
    # Default change at a series boundary (8.0.30 -> 8.4.0).
    "cache_size": (
        (0, ("global", "integer", True, "400", None, "0", "4096", None, None, None, None, None)),
        (2, ("global", "integer", True, "800", None, "0", "4096", None, None, None, None, None)),
    ),
    # Default unknown from 8.4.0 on, and a dynamic change at the same time.
    "mystery_var": (
        (0, ("global", "integer", True, "5", None, "1", "100", None, None, None, None, None)),
        (2, ("global", "integer", False, None, "unknown", "1", "100", None, None, None, None, None)),
    ),
    # Host-dependent default, changed in 8.4.0.
    "host_sized": (
        (0, ("global", "integer", True, "128", None, "1", None, None, None, None, None, None)),
        (2, ("global", "integer", True, "256", "host", "1", None, None, None, None, None, None)),
    ),
    # Present only in builds with NUMA support: its disappearance is not a removal.
    "numa_only": (
        (0, ("global", "boolean", False, "OFF", None, None, None, None, None, None, None, None)),
        (2, None),
    ),
    # Follows the build's installation layout: never a default change.
    "plugin_dir": (
        (0, ("global", "string", False, "/usr/lib/mysql/plugin/", "build", None, None, None, None, None, None, None)),
        (2, ("global", "string", False, "/usr/lib64/mysql/plugin/", "build", None, None, None, None, None, None, None)),
    ),
    # Names the release itself: different every release, never a default change.
    "version": (
        (0, ("global", "string", False, "8.0.11", "release", None, None, None, None, None, None, None)),
        (2, ("global", "string", False, "8.4.0", "release", None, None, None, None, None, None, None)),
    ),
    # Sampled on the lab host on both sides: different numbers, no judged change.
    "host_both": (
        (0, ("global", "integer", True, "4", "host", "1", None, None, None, None, None, None)),
        (2, ("global", "integer", True, "8", "host", "1", None, None, None, None, None, None)),
    ),
    # A variable a plugin owns.
    "group_replication_var": (
        (0, ("global", "integer", True, "3", None, "1", "9", None, None, None, None, "group_replication")),
    ),
    # Deprecated in the 8.0 series only: undeprecated from 8.4.0.
    "legacy_var": (
        (0, ("global", "boolean", True, "OFF", None, None, None, None, ("source",), None, None, None)),
        (2, ("global", "boolean", True, "OFF", None, None, None, None, None, None, None, None)),
    ),
    # Allowed values change in 8.4.0.
    "mode_var": (
        (0, ("global", "enum", True, "A", None, None, None, ("A", "B"), None, None, None, None)),
        (2, ("global", "enum", True, "A", None, None, None, ("A", "B", "C"), None, None, None, None)),
    ),
    # Dynamic change in 8.4.0, range change in 9.0.0 (lineage from 8.4.0).
    "workers": (
        (0, ("global", "integer", True, "4", None, "1", "64", None, None, None, None, None)),
        (2, ("global", "integer", False, "4", None, "1", "64", None, None, None, None, None)),
        (4, ("global", "integer", False, "4", None, "1", "128", None, None, None, None, None)),
    ),
    # Scope change inside the 8.4 series (8.4.0 -> 8.4.3).
    # Session state: different every release, never a default change.
    "session_state": (
        (0, ("session", "integer", True, "25", "state", None, None, None, None, None, None, None)),
        (2, ("session", "integer", True, "26", "state", None, None, None, None, None, None, None)),
    ),
    # Default becomes an empty string in 8.4.0.
    "empty_default": (
        (0, ("global", "string", True, "x", None, None, None, None, None, None, None, None)),
        (2, ("global", "string", True, "", None, None, None, None, None, None, None, None)),
    ),
    # A name long enough that the full history summary passes 120 characters.
    "group_replication_a_very_long_parameter_name_for_summaries": (
        (1, ("global", "integer", True, "1", None, None, None, None, None, None, None, None)),
        (2, ("global", "integer", True, "2", None, None, None, None, None, None, None, None)),
        (3, ("global", "integer", True, "3", None, None, None, None, None, None, None, None)),
    ),
    # Arrives deprecated in 8.4.0.
    "born_deprecated": (
        (2, ("global", "integer", True, "1", None, None, None, None, ("source",), None, None, None)),
    ),
    # Deprecated in 8.0.30 for born_deprecated, which is itself deprecated from 8.4.0.
    "older_name": (
        (0, ("global", "integer", True, "1", None, None, None, None, None, None, None, None)),
        (1, ("global", "integer", True, "1", None, None, None, None, ("source",), "born_deprecated", None, None)),
    ),
    # Removed in 8.4.0; its replacement always_on still exists.
    "gone_var": (
        (0, ("global", "integer", True, "1", None, None, None, None, ("source",), "always_on", None, None)),
        (2, None),
    ),
    # Declared again as an enum of ON and OFF in 8.4.0: not a type change.
    "switch_var": (
        (0, ("global", "boolean", True, "ON", None, None, None, None, None, None, None, None)),
        (2, ("global", "enum", True, "ON", None, None, None, None, None, None, None, None)),
    ),
    # The maximum is reported rounded down to its block size from 8.4.0: not a range change.
    "rounded_var": (
        (0, ("global", "integer", True, "1", None, "0", "18446744073709551615", None, None, None, None, None)),
        (2, ("global", "integer", True, "1", None, "0", "18446744073709547520", None, None, None, None, None)),
    ),
    # utf8 printed under its full name from 8.4.0.
    "charset_var": (
        (0, ("global", "string", True, "utf8", None, None, None, None, None, None, None, None)),
        (2, ("global", "string", True, "utf8mb3", None, None, None, None, None, None, None, None)),
    ),
    # 9.0.0 (after 8.4.0) and the 8.4.3 patch both changed it.
    "twin_var": (
        (0, ("global", "string", True, "a", None, None, None, None, None, None, None, None)),
        (3, ("global", "string", True, "b", None, None, None, None, None, None, None, None)),
    ),
    # A boolean that becomes an enum with a third value in 8.4.0: a real type change.
    "enum_grown": (
        (0, ("global", "boolean", True, "OFF", None, None, None, None, None, None, None, None)),
        (2, ("global", "enum", True, "OFF", None, None, None, ("OFF", "ON", "FAST"), None, None, None, None)),
    ),
    # Removed in 8.0.30, back in 8.4.0 with another default.
    "comeback_var": (
        (0, ("global", "integer", True, "1", None, None, None, None, None, None, None, None)),
        (1, None),
        (2, ("global", "integer", True, "2", None, None, None, None, None, None, None, None)),
    ),
    # Changed by the 8.4.3 patch only: 9.0.0 (which follows 8.4.0) keeps "1".
    "branch_var": (
        (0, ("global", "integer", True, "1", None, None, None, None, None, None, None, None)),
        (3, ("global", "integer", True, "2", None, None, None, None, None, None, None, None)),
        (4, ("global", "integer", True, "1", None, None, None, None, None, None, None, None)),
    ),
    "log_scope": (
        (0, ("global", "string", True, "stderr", None, None, None, None, None, None, None, None)),
        (3, ("both", "string", True, "stderr", None, None, None, None, None, None, None, None)),
    ),
    # Type change in 8.4.0.
    "counter_var": (
        (0, ("session", "integer", True, "0", None, "0", None, None, None, None, None, None)),
        (2, ("session", "double", True, "0", None, "0", None, None, None, None, None, None)),
    ),
}

BUILD_ONLY = {"numa_only": "Only builds with NUMA support have it."}

DEFAULT_RULES = {"host_sized": (("8.4.0", None, "The server sets it at startup to twice the number of CPUs."),)}
