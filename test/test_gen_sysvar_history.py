"""scripts/gen_sysvar_history.py: version scope, lineage, merge, evidence, output."""
import ast
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/gen_sysvar_history.py"
_SPEC = importlib.util.spec_from_file_location("gen_sysvar_history", str(_SCRIPT))
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)


def release(version, date, tag=None, maturity="GA"):
    return {"version": version, "tag": tag or "mysql-" + version, "maturity": maturity, "date": date}


def var(scope="global", default="1", dynamic=True, warning=None, plugin=None, values=None, **extra):
    data = {"scope": scope, "type": "integer", "default": default, "default_note": None, "min": None,
            "max": None, "values": values, "dynamic": dynamic, "dynamic_how": "set_probe",
            "deprecated_warning": warning, "plugin": plugin}
    data.update(extra)
    return data


def record(version, variables, engine="mysql", **extra):
    data = {"schema": "sysvar-runtime-1.0", "engine": engine, "version": version, "method": "image",
            "server_version": version, "plugins_loaded": [], "variables": variables, "notes": []}
    data.update(extra)
    return data


def fact(scope="global", ro=False, deprecated=False, substitute=None, alias_of=None, group="core", kind=""):
    return {"scope": scope, "ro": ro, "deprecated": deprecated, "substitute": substitute,
            "alias_of": alias_of, "group": group, "kind": kind}


def facts_at(catalog, name, version):
    """The facts of *name* at *version*, decoded from the run-length HISTORY."""
    position = [item[0] for item in catalog["versions"]].index(version)
    current = None
    for first, facts in catalog["history"].get(name, ()):
        if first <= position:
            current = facts
    return current


class TestVersionScope(unittest.TestCase):
    def test_mysql_maturity_suffix_respin_and_minimum(self):
        files = {
            "mysql-8.0.4": "MYSQL_VERSION_MAJOR=8\nMYSQL_VERSION_MINOR=0\nMYSQL_VERSION_PATCH=4\n"
                           "MYSQL_VERSION_EXTRA=-rc\n",
            "mysql-8.0.11": "MYSQL_VERSION_MAJOR=8\nMYSQL_VERSION_MINOR=0\nMYSQL_VERSION_PATCH=11\n"
                            "MYSQL_VERSION_EXTRA=\n",
            # An empty EXTRA must not swallow the next line (an old cluster suffix).
            "mysql-8.0.13": "MYSQL_VERSION_MAJOR=8\nMYSQL_VERSION_MINOR=0\nMYSQL_VERSION_PATCH=13\n"
                            "MYSQL_VERSION_EXTRA=\nMYSQL_CLUSTER_VERSION_EXTRA=-dmr\n",
            "mysql-8.4.0": "MYSQL_VERSION_MAJOR=8\nMYSQL_VERSION_MINOR=4\nMYSQL_VERSION_PATCH=0\n"
                           "MYSQL_VERSION_EXTRA=\nMYSQL_VERSION_MATURITY=\"LTS\"\n",
            "mysql-9.5.2": "MYSQL_VERSION_MAJOR=9\nMYSQL_VERSION_MINOR=5\nMYSQL_VERSION_PATCH=2\n"
                           "MYSQL_VERSION_EXTRA=\nMYSQL_VERSION_MATURITY=\"INNOVATION\"\n",
            "mysql-9.5.2-respin": "MYSQL_VERSION_MAJOR=9\nMYSQL_VERSION_MINOR=5\nMYSQL_VERSION_PATCH=2\n"
                                  "MYSQL_VERSION_EXTRA=\nMYSQL_VERSION_MATURITY=\"INNOVATION\"\n",
            "mysql-9.6.0": "MYSQL_VERSION_MAJOR=9\nMYSQL_VERSION_MINOR=6\nMYSQL_VERSION_PATCH=1\n",
            # No MYSQL_VERSION_MATURITY before 8.4.0, but Oracle published 8.1 to 8.3 as Innovation releases.
            "mysql-8.2.0": "MYSQL_VERSION_MAJOR=8\nMYSQL_VERSION_MINOR=2\nMYSQL_VERSION_PATCH=0\n"
                           "MYSQL_VERSION_EXTRA=\n",
        }
        dates = {tag: "2024-01-01T00:00:00Z" for tag in files}
        found = gen.releases_from("mysql", files, dates)
        self.assertEqual([(item["version"], item["tag"], item["maturity"]) for item in found], [
            ("8.0.11", "mysql-8.0.11", "GA"), ("8.0.13", "mysql-8.0.13", "GA"),
            ("8.2.0", "mysql-8.2.0", "INNOVATION"), ("8.4.0", "mysql-8.4.0", "LTS"),
            ("9.5.2", "mysql-9.5.2-respin", "INNOVATION")])

    def test_mariadb_stable_only_from_10_6(self):
        def text(version, maturity):
            major, minor, patch = version.split(".")
            return ("MYSQL_VERSION_MAJOR=%s\nMYSQL_VERSION_MINOR=%s\nMYSQL_VERSION_PATCH=%s\n"
                    "SERVER_MATURITY=%s\n" % (major, minor, patch, maturity))
        files = {"mariadb-10.5.20": text("10.5.20", "stable"), "mariadb-10.6.1": text("10.6.1", "beta"),
                 "mariadb-10.6.3": text("10.6.3", "stable"), "mariadb-11.4.11b": text("11.4.11", "stable"),
                 "mariadb-11.4.2": text("11.4.2", "stable")}
        found = gen.releases_from("mariadb", files, {tag: "2024-01-01T00:00:00+02:00" for tag in files})
        self.assertEqual([item["version"] for item in found], ["10.6.3", "11.4.2"])
        self.assertEqual({item["maturity"] for item in found}, {"stable"})


class TestLineage(unittest.TestCase):
    def lineage_of(self, releases):
        releases = sorted(releases, key=lambda item: gen.version_key(item["version"]))
        previous = gen.lineage(releases)
        return {item["version"]: (releases[index]["version"] if index is not None else None)
                for item, index in zip(releases, previous)}

    def test_mysql_real_tag_shapes(self):
        # Tag dates as on the real mysql-server tags (only their order matters).
        result = self.lineage_of([
            release("8.0.33", "2023-03-16T00:00:00+01:00"), release("8.0.34", "2023-06-22T09:34:44+02:00"),
            release("8.1.0", "2023-06-21T09:39:08+02:00"), release("8.3.0", "2023-12-14T15:49:18+01:00"),
            release("8.4.0", "2024-04-10T08:20:46+02:00"), release("8.0.40", "2024-09-10T00:00:00+02:00"),
            release("8.4.1", "2024-06-01T00:00:00+02:00"), release("9.0.0", "2024-06-07T09:15:03+02:00"),
            release("9.0.1", "2024-07-10T00:00:00+02:00"), release("9.1.0", "2024-09-24T13:34:54+02:00"),
            release("8.4.11", "2026-07-20T00:00:00+02:00"), release("9.7.1", "2026-07-01T00:00:00+02:00"),
            release("26.7.0", "2026-07-28T10:14:16+02:00"), release("8.4.13", "2026-09-30T11:31:22+05:30"),
            release("26.10.0", "2026-10-20T10:07:58+02:00")])
        self.assertIsNone(result["8.0.33"])
        self.assertEqual(result["8.0.34"], "8.0.33")
        self.assertEqual(result["8.1.0"], "8.0.33")      # 8.0.34 was tagged after 8.1.0
        self.assertEqual(result["8.4.0"], "8.3.0")
        self.assertEqual(result["9.0.0"], "8.4.1")
        self.assertEqual(result["9.1.0"], "9.0.1")       # not the later-dated 8.0.40
        self.assertEqual(result["26.7.0"], "9.7.1")      # not the later-dated 8.4.11
        self.assertEqual(result["26.10.0"], "26.7.0")    # not the later-dated 8.4.13
        self.assertEqual(result["8.4.13"], "8.4.11")

    def test_mariadb_real_tag_shapes_and_fallback_to_older_series(self):
        result = self.lineage_of([
            release("11.2.4", "2024-05-10T00:00:00Z", "mariadb-11.2.4", "stable"),
            release("11.3.2", "2024-02-14T00:00:00Z", "mariadb-11.3.2", "stable"),
            release("11.4.2", "2024-05-26T00:00:00Z", "mariadb-11.4.2", "stable"),
            release("11.4.7", "2025-05-01T00:00:00Z", "mariadb-11.4.7", "stable"),
            release("11.7.2", "2025-02-11T00:00:00Z", "mariadb-11.7.2", "stable"),
            release("11.8.2", "2025-05-21T00:00:00Z", "mariadb-11.8.2", "stable"),
            release("11.8.3", "2025-07-20T00:00:00Z", "mariadb-11.8.3", "stable"),
            release("12.0.2", "2025-07-31T00:00:00Z", "mariadb-12.0.2", "stable"),
            # 12.1 has only a release dated after 12.2.2, so 12.2.2 skips to 12.0.
            release("12.1.2", "2026-03-01T00:00:00Z", "mariadb-12.1.2", "stable"),
            release("12.2.2", "2026-02-06T00:00:00Z", "mariadb-12.2.2", "stable")])
        self.assertEqual(result["11.4.2"], "11.3.2")     # not the later-dated 11.2.4
        self.assertEqual(result["11.8.2"], "11.7.2")     # not the later-dated 11.4.7
        self.assertEqual(result["12.0.2"], "11.8.3")
        self.assertEqual(result["12.2.2"], "12.0.2")
        self.assertIsNone(result["11.2.4"])
        self.assertEqual(result["11.3.2"], None)         # only a later-dated 11.2 release exists


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.releases = [release("8.0.11", "2018-04-08T08:17:02+02:00"),
                         release("8.0.12", "2018-06-28T17:46:28+02:00"),
                         release("8.0.13", "2018-10-07T10:41:27+02:00"),
                         release("8.4.0", "2024-04-10T08:20:46+02:00", maturity="LTS"),
                         release("8.4.1", "2024-06-01T00:00:00+02:00", maturity="LTS")]

    def build(self, records, sources=None, manual=None, unusable=None, tag_dates=None):
        sources = sources or {}
        return gen.build("mysql", self.releases, records, unusable or {},
                         lambda tag: sources.get(tag, {}), manual or {}, {"8.0": "2026-10-28"}, tag_dates)

    def test_run_length_encoding_absent_and_readded(self):
        records = {
            "8.0.11": record("8.0.11", {"a": var(default="1"), "b": var(), "gone": var()}),
            "8.0.12": record("8.0.12", {"a": var(default="1"), "b": var(), "late": var()}),
            "8.0.13": record("8.0.13", {"a": var(default="2"), "b": var(), "late": var()}),
            "8.4.0": record("8.4.0", {"a": var(default="2"), "gone": var()}),
        }
        catalog = self.build(records, unusable={"8.4.1": "No image or package could be started: test"})
        self.assertIn("- No image or package could be started: test (1): 8.4.1",
                      "\n".join(gen.report_lines(catalog)))
        history = catalog["history"]
        self.assertEqual([item[0] for item in history["a"]], [0, 2])
        self.assertEqual(history["a"][1][1][3], "2")
        self.assertEqual(history["b"][0][0], 0)
        self.assertEqual(history["b"][1], (3, None))
        self.assertEqual([item[0] for item in history["gone"]], [0, 1, 3])
        self.assertIsNone(history["gone"][1][1])
        # A later name starts at the first version where it exists: no leading (0, None).
        self.assertEqual(history["late"][0][0], 1)
        self.assertEqual(history["late"][-1], (3, None))
        self.assertEqual(catalog["skipped"], (("8.4.1", "No image or package could be started: test", "2024-06-01"),))
        self.assertEqual([item[4] for item in catalog["versions"]], [None, 0, 1, 2])

    def test_absent_from_record_means_absent_even_if_source_declares_it(self):
        records = {"8.0.11": record("8.0.11", {"a": var()})}
        catalog = self.build(records, sources={"mysql-8.0.11": {"a": fact(), "plugin_x": fact(group="clone")}})
        self.assertNotIn("plugin_x", catalog["history"])
        self.assertEqual([item[0] for item in catalog["skipped"]], ["8.0.12", "8.0.13", "8.4.0", "8.4.1"])
        self.assertEqual({item[1] for item in catalog["skipped"]}, {gen.NOT_READ})

    def test_unusable_records_and_records_outside_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory, "mysql")
            folder.mkdir()
            Path(folder, "8.0.11.json").write_text(json.dumps(record("8.0.11", {"a": var()})))
            Path(folder, "8.0.12.json").write_text(json.dumps({"schema": "sysvar-runtime-1.0", "engine": "mysql",
                                                                "version": "8.0.12", "skipped": "Pull failed"}))
            Path(folder, "8.0.13.json").write_text(json.dumps(record("8.0.13", {})))
            Path(folder, "9.9.9.json").write_text(json.dumps(record("9.9.9", {"a": var()})))
            Path(folder, "versions-mysql.json").write_text("[]")
            records, unusable = gen.load_records(directory, "mysql")
        self.assertEqual(sorted(records), ["8.0.11", "9.9.9"])
        self.assertEqual(unusable, {"8.0.12": "Pull failed", "8.0.13": "The runtime record has no variables."})
        catalog = self.build(records, unusable=unusable)
        skipped = {item[0]: item[1:] for item in catalog["skipped"]}
        self.assertEqual(skipped["8.0.12"], ("Pull failed", "2018-06-28"))
        self.assertEqual(skipped["8.4.0"], (gen.NOT_READ, "2024-04-10"))
        self.assertEqual(catalog["outside"], ["9.9.9"])

    def test_deprecation_evidence_kinds_and_order(self):
        manual = {"old_server": [("8.0.12", None)], "late_manual": [("8.4.1", None)],
                  "dep80": [("8.0.13", None)]}
        warning = "'@@old_server' is deprecated and will be removed in a future release. Please use new_name instead."
        records = {version: record(version, {
            "old_server": var(warning=warning if version != "8.0.11" else None),
            "new_name": var(), "alias": var(), "late_manual": var(), "dep80": var(), "plain": var()})
            for version in ("8.0.11", "8.0.12", "8.0.13", "8.4.0", "8.4.1")}
        sources = {"mysql-8.0.13": {"old_server": fact(deprecated=True, substitute="''"),
                                    "alias": fact(deprecated=True, substitute="new_name", alias_of="new_name")}}
        catalog = self.build(records, sources=sources, manual=manual)
        self.assertIsNone(facts_at(catalog, "old_server", "8.0.11")[8])
        self.assertEqual(facts_at(catalog, "old_server", "8.0.12")[8], ("manual", "server"))
        self.assertEqual(facts_at(catalog, "old_server", "8.0.13")[8], ("manual", "server", "source"))
        # Replacement: source first (empty here), then manual (none), then the server's warning text.
        self.assertEqual(facts_at(catalog, "old_server", "8.0.13")[9], "new_name")
        alias = facts_at(catalog, "alias", "8.0.13")
        self.assertEqual((alias[8], alias[9], alias[10]), (("source",), "new_name", "new_name"))
        self.assertIsNone(facts_at(catalog, "alias", "8.4.0")[8])
        # Manual: not before its version in the series; not in an earlier series.
        self.assertIsNone(facts_at(catalog, "late_manual", "8.4.0")[8])
        self.assertEqual(facts_at(catalog, "late_manual", "8.4.1")[8], ("manual",))
        self.assertIsNone(facts_at(catalog, "late_manual", "8.0.13")[8])
        # A deprecation in an earlier series covers a later series while the name exists.
        self.assertEqual(facts_at(catalog, "dep80", "8.4.0")[8], ("manual",))
        self.assertIsNone(facts_at(catalog, "dep80", "8.0.12")[8])
        self.assertIsNone(facts_at(catalog, "plain", "8.4.1")[8])

    def test_server_warnings_count_only_when_they_deprecate_the_variable(self):
        kinds = gen.warning_kinds
        update = "Updating 'character_set_database' is deprecated. It will be made read-only in a future release."
        self.assertEqual(kinds("character_set_database", update)["runtime_change"], [update])
        self.assertEqual(kinds("character_set_database", update)["variable"], [])
        mariadb = "'@@tx_isolation' is deprecated and will be removed in a future release. " \
                  "Please use '@@transaction_isolation' instead"
        self.assertEqual(kinds("TX_ISOLATION", mariadb)["variable"], [mariadb])
        self.assertEqual(kinds("init_slave", "'init_slave' is deprecated and will be removed in a future release."
                               )["variable"], ["'init_slave' is deprecated and will be removed in a future release."])
        self.assertEqual(kinds("x", "The syntax '@@x' is deprecated and will be removed in MySQL 9.0.")["variable"],
                         ["The syntax '@@x' is deprecated and will be removed in MySQL 9.0."])
        # MySQL 5.7: InnoDB's "Using x is deprecated" and the option form '--secure-auth'.
        using = "Using innodb_large_prefix is deprecated and the parameter may be removed in future releases."
        self.assertEqual(kinds("innodb_large_prefix", using)["variable"], [using])
        stats = ("Using innodb_stats_sample_pages is deprecated and the variable may be removed in future releases. "
                 "Please use innodb_stats_transient_sample_pages instead.")
        self.assertEqual(kinds("innodb_stats_sample_pages", stats)["variable"], [stats])
        self.assertEqual(gen.warning_replacement(stats), "innodb_stats_transient_sample_pages")
        parameter = ("The parameter innodb_undo_logs is deprecated and may be removed in future releases. Please use "
                     "innodb_rollback_segments instead.")
        self.assertEqual(kinds("innodb_undo_logs", parameter)["variable"], [parameter])
        option = "'--secure-auth' is deprecated and will be removed in a future release."
        self.assertEqual(kinds("secure_auth", option)["variable"], [option])
        self.assertEqual(kinds("innodb_file_format", using)["other"], [using])
        # MariaDB's old_mode warns about a deprecated value, not the variable.
        value = "'NO_PROGRESS_INFO' is deprecated and will be removed in a future release"
        self.assertEqual(kinds("old_mode", value)["other"], [value])
        both = "'@@big_tables' is deprecated and will be removed in a future release\n" + value
        self.assertEqual((len(kinds("big_tables", both)["variable"]), len(kinds("big_tables", both)["other"])),
                         (1, 1))
        records = {version: record(version, {
            "character_set_database": var(warning=update), "old_mode": var(warning=value),
            "tx_isolation": var(warning=mariadb), "transaction_isolation": var()})
            for version in ("8.0.11", "8.0.12")}
        catalog = self.build(records)
        self.assertIsNone(facts_at(catalog, "character_set_database", "8.0.11")[8])
        self.assertIsNone(facts_at(catalog, "old_mode", "8.0.12")[8])
        self.assertEqual(facts_at(catalog, "tx_isolation", "8.0.12")[8:10], (("server",), "transaction_isolation"))
        report = "\n".join(gen.report_lines(catalog))
        self.assertIn("### Changing at runtime is deprecated", report)
        self.assertIn("- character_set_database: 8.0.11 to 8.0.12 (\"Updating 'character_set_database'", report)
        self.assertIn("- old_mode: 8.0.11 to 8.0.12 (\"'NO_PROGRESS_INFO' is deprecated", report)

    def test_manual_earlier_series_respects_tag_dates(self):
        later = {"version": "8.1.0", "date": "2023-06-21T09:39:08+02:00"}
        dates = {"8.0.34": "2023-06-22T09:34:44+02:00", "8.0.33": "2023-03-16T00:00:00+01:00"}
        self.assertFalse(gen.manual_applies("8.0.34", later, dates))
        self.assertTrue(gen.manual_applies("8.0.33", later, dates))
        self.assertTrue(gen.manual_applies("8.0.2", later, dates))      # no tag date known
        self.assertFalse(gen.manual_applies("8.4.0", later, dates))

    def test_replacement_order_source_then_manual_then_server(self):
        warning = "'@@x' is deprecated. Please use '@@from_server' instead."
        names = {"x": var(warning=warning), "from_source": var(), "from_manual": var(), "from_server": var()}
        records = {"8.0.11": record("8.0.11", dict(names))}
        manual = {"x": [("8.0.11", "from_manual")]}
        with_source = self.build(records, sources={"mysql-8.0.11": {"x": fact(deprecated=True,
                                                                           substitute="'@@from_source'")}},
                                 manual=manual)
        self.assertEqual(facts_at(with_source, "x", "8.0.11")[9], "from_source")
        self.assertEqual(facts_at(self.build(records, manual=manual), "x", "8.0.11")[9], "from_manual")
        self.assertEqual(facts_at(self.build(records), "x", "8.0.11")[9], "from_server")
        # A substitute that is not a variable name is not a replacement, and it is reported.
        odd = self.build(records, sources={"mysql-8.0.11": {"x": fact(
            deprecated=True, substitute="performance_schema.tls_channel_status table")}})
        self.assertEqual(facts_at(odd, "x", "8.0.11")[9], "from_server")
        self.assertIn(("x", "performance_schema.tls_channel_status table"), odd["bad_substitutes"])

    def test_dynamic_from_runtime_then_source_read_only_flag(self):
        records = {"8.0.11": record("8.0.11", {"probe": var(dynamic=False), "unknown": var(dynamic=None),
                                               "nosource": var(dynamic=None)})}
        catalog = self.build(records, sources={"mysql-8.0.11": {"probe": fact(ro=False),
                                                                 "unknown": fact(ro=True)}})
        self.assertIs(facts_at(catalog, "probe", "8.0.11")[2], False)
        self.assertIs(facts_at(catalog, "unknown", "8.0.11")[2], False)
        self.assertIsNone(facts_at(catalog, "nosource", "8.0.11")[2])

    def test_source_names_match_without_case(self):
        records = {"8.0.11": record("8.0.11", {"wsrep_slave_fk_checks": var(dynamic=None)})}
        catalog = self.build(records, sources={"mysql-8.0.11": {"wsrep_slave_FK_checks": fact(
            ro=True, deprecated=True, substitute="")}})
        facts = facts_at(catalog, "wsrep_slave_fk_checks", "8.0.11")
        self.assertEqual((facts[2], facts[8]), (False, ("source",)))
        report = "\n".join(gen.report_lines(catalog))
        self.assertIn("| 8.0.11 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |", report)

    def test_plugin_values_and_field_order(self):
        records = {"8.0.11": record("8.0.11", {"clone_x": var(plugin="clone", values=["OFF", "ON"], min="0",
                                                              max="9", default_note="host")})}
        facts = facts_at(self.build(records), "clone_x", "8.0.11")
        self.assertEqual(dict(zip(gen.FIELDS, facts)), {
            # MySQL before 9.0 reports no data types: the lab's guess is not kept.
            "scope": "global", "type": None, "dynamic": True, "default": "1", "default_note": "host",
            "min": "0", "max": "9", "values": ("OFF", "ON"), "deprecated": None, "replacement": None,
            "alias_of": None, "plugin": "clone"})

    def test_mysql_types_only_from_9_0(self):
        self.releases.append(release("9.0.0", "2024-07-01T00:00:00+02:00", maturity="INNOVATION"))
        records = {"8.4.1": record("8.4.1", {"x": var()}), "9.0.0": record("9.0.0", {"x": var()})}
        catalog = self.build(records)
        self.assertIsNone(facts_at(catalog, "x", "8.4.1")[1])
        self.assertEqual(facts_at(catalog, "x", "9.0.0")[1], "integer")

    def test_session_and_server_state_is_never_a_default(self):
        records = {"8.0.11": record("8.0.11", {"pseudo_thread_id": var(default="25"), "gtid_executed": var(default="")}),
                   "8.0.12": record("8.0.12", {"pseudo_thread_id": var(default="26"), "gtid_executed": var(default="")})}
        catalog = self.build(records)
        for version in ("8.0.11", "8.0.12"):
            self.assertEqual(facts_at(catalog, "pseudo_thread_id", version)[4], "state")
            self.assertEqual(facts_at(catalog, "gtid_executed", version)[4], "state")

    def test_short_skip_reason(self):
        raw = ("No image: public.ecr.aws/docker/library/mysql:9.1.1 (Error response from daemon: not found); "
               "docker.io/library/mysql:9.1.1 (not in the cached Docker Hub tag list). No package: none at "
               "https://cdn.mysql.com/Downloads/MySQL-9.1.1/ (checked 2026-10-28)")
        self.assertEqual(gen.short_reason(raw),
                         "No official image or package of this release could be found (checked 2026-10-28).")
        self.assertEqual(gen.short_reason("The runtime record has no variables."),
                         "The runtime record has no variables.")
        self.assertLessEqual(len(gen.short_reason("x" * 400)), 160)


class TestReport(unittest.TestCase):
    def test_disagreements_gaps_and_method_switch(self):
        releases = [release("8.0.11", "2018-04-08T00:00:00Z"), release("8.0.12", "2018-06-28T00:00:00Z"),
                    release("8.0.13", "2018-10-07T00:00:00Z")]
        records = {
            "8.0.11": record("8.0.11", {"a": var(), "flaky": var(), "basedir": var(default="/usr/")}),
            "8.0.12": record("8.0.12", {"a": var(dynamic=False), "basedir": var(default="/opt/mysql/"),
                                        "only_server": var()}, method="package"),
            "8.0.13": record("8.0.13", {"a": var(), "flaky": var(), "basedir": var(default="/opt/mysql/")},
                             method="package"),
        }
        sources = {
            "mysql-8.0.11": {"a": fact(), "flaky": fact(), "basedir": fact(),
                             "group_replication_x": fact(group="group_replication"),
                             "debug": fact(kind="debug"), "named_pipe": fact(kind="build"), "lost": fact(),
                             "performance_schema_instrument": fact(kind="hidden")},
            "mysql-8.0.12": {"a": fact(scope="both"), "basedir": fact(), "flaky": fact()},
            "mysql-8.0.13": {"a": fact(), "flaky": fact(), "basedir": fact()},
        }
        catalog = gen.build("mysql", releases, records, {}, lambda tag: sources[tag], {}, {}, {})
        text = "\n".join(gen.report_lines(catalog))
        self.assertIn("| 8.0.11 | 3 | 8 | 0 | 0 | 1 | 1 | 1 | 1 | 1 | 0 | 0 |", text)
        self.assertIn("| 8.0.12 | 3 | 3 | 0 | 1 | 0 | 0 | 0 | 0 | 1 | 1 | 1 |", text)
        self.assertIn("- lost: 8.0.11", text)
        self.assertIn("- only_server: 8.0.12", text)
        self.assertIn("- a (server global, source both): 8.0.12", text)
        self.assertIn("- a (server not dynamic, source not READ_ONLY): 8.0.12", text)
        self.assertIn("- flaky in 8.0: absent from 8.0.12", text)
        self.assertIn("8.0.11 (image) to 8.0.12 (package): 1", text)
        self.assertIn("Names: basedir (1).", text)


class TestHostAndRelease(unittest.TestCase):
    def mysql_catalog(self, sources=None):
        releases = [release("8.0.36", "2024-01-08T00:00:00Z"), release("8.4.0", "2024-04-10T00:00:00Z", maturity="LTS"),
                    release("9.6.0", "2025-12-23T00:00:00Z", maturity="INNOVATION")]
        names = lambda purge: {  # noqa: E731
            "innodb_purge_threads": var(default=purge), "temptable_max_ram": var(default="1073741824"),
            "innodb_parallel_read_threads": var(default="4"),
            "open_files_limit": var(default="1048576"), "version": var(default="x"),
            "thread_cache_size": var(default="9"), "server_uuid": var(default="u", default_note="host"),
            "lower_case_file_system": var(default=None, default_note="null"),
            "table_definition_cache": var(default="2000", default_note="unknown")}
        records = {"8.0.36": record("8.0.36", names("4")), "8.4.0": record("8.4.0", names("1")),
                   "9.6.0": record("9.6.0", names("1"))}
        for version in records:
            records[version]["variables"]["version"]["default"] = version
        return gen.build("mysql", releases, records, {}, (lambda tag: (sources or {}).get(tag, {})), {}, {}, {})

    def test_reviewed_host_defaults_follow_the_source_per_release(self):
        catalog = self.mysql_catalog()
        note = lambda name, version: facts_at(catalog, name, version)[4]  # noqa: E731
        # 8.4 computes innodb_purge_threads from the CPUs (1 up to 16, else 4); 8.0 declares 4.
        self.assertIsNone(note("innodb_purge_threads", "8.0.36"))
        self.assertEqual(note("innodb_purge_threads", "8.4.0"), "host")
        self.assertEqual(facts_at(catalog, "innodb_purge_threads", "8.4.0")[3], "1")    # the lab value is kept
        # innodb_parallel_read_threads: CPUs / 8, at least 4, from 8.4.0 (the lab read 4 on both hosts).
        self.assertIsNone(note("innodb_parallel_read_threads", "8.0.36"))
        self.assertEqual(note("innodb_parallel_read_threads", "9.6.0"), "host")
        # temptable_max_ram: 3% of memory in 8.4.0 to 9.5.2, a declared 1 GiB from 9.6.0.
        self.assertIsNone(note("temptable_max_ram", "8.0.36"))
        self.assertEqual(note("temptable_max_ram", "8.4.0"), "host")
        self.assertIsNone(note("temptable_max_ram", "9.6.0"))
        self.assertEqual(note("open_files_limit", "8.0.36"), "host")
        self.assertEqual(note("lower_case_file_system", "8.4.0"), "host")       # replaces "null"
        self.assertEqual(note("table_definition_cache", "8.4.0"), "unknown")    # unknown stays unknown
        self.assertIsNone(note("thread_cache_size", "8.4.0"))                   # follows max_connections
        self.assertEqual(note("server_uuid", "8.4.0"), "state")                 # server state, never a default
        # Release identity: "release", whatever the record said; default kept as read.
        self.assertEqual(note("version", "8.0.36"), "release")
        self.assertEqual(facts_at(catalog, "version", "9.6.0")[3], "9.6.0")
        self.assertEqual(note("lower_case_file_system", "8.0.36"), "host")
        marked = {name for name, how in catalog["host_marks"]}
        self.assertEqual(marked, {"innodb_purge_threads", "innodb_parallel_read_threads", "temptable_max_ram",
                                  "open_files_limit", "lower_case_file_system"})
        report = "\n".join(gen.report_lines(catalog))
        self.assertIn("- 8.0: lower_case_file_system, open_files_limit (2)", report)
        self.assertIn("- 8.4: innodb_parallel_read_threads, innodb_purge_threads, lower_case_file_system, "
                      "open_files_limit, temptable_max_ram (5)", report)
        self.assertIn("- not marked: temptable_max_ram, 9.6.0 on:", report)
        self.assertIn("### Release and build identity (default_note release)", report)
        self.assertIn("1: version.", report)

    def test_scan_against_review_in_report(self):
        sources = {"mysql-8.4.0": {"innodb_purge_threads": fact(), "temptable_max_ram": fact(),
                                   "open_files_limit": fact(), "thread_cache_size": fact(),
                                   "lower_case_file_system": fact()}}
        sources["mysql-8.4.0"]["innodb_purge_threads"]["host"] = ["ha_innodb.cc: declared default calls x"]
        sources["mysql-8.4.0"]["thread_cache_size"]["host"] = ["mysqld.cc: set at startup from y"]
        report = "\n".join(gen.report_lines(self.mysql_catalog(sources)))
        self.assertIn("scan hits with no reviewed entry: thread_cache_size (mysqld.cc: set at startup from y) at 8.4.0",
                      report)
        self.assertIn("marked entries without scan evidence: innodb_parallel_read_threads at 8.4.0; "
                      "lower_case_file_system at 8.4.0; open_files_limit at 8.4.0; temptable_max_ram at 8.4.0", report)
        # Lowered only when the OS grants too few open files: reviewed and not marked.
        self.assertIn("not marked: table_definition_cache, 5.7.9 on", report)

    def test_build_layout_paths_and_build_only_presence(self):
        releases = [release("8.0.28", "2022-01-01T00:00:00Z"), release("8.0.29", "2022-04-08T00:00:00Z")]
        records = {
            "8.0.28": record("8.0.28", {"socket": var(default="/var/run/mysqld/mysqld.sock"),
                                        "plugin_dir": var(default="/usr/lib/mysql/plugin/"),
                                        "datadir": var(default="/var/lib/mysql/", default_note="host"),
                                        "innodb_numa_interleave": var(default="OFF"), "port": var(default="3306")}),
            "8.0.29": record("8.0.29", {"socket": var(default="/var/lib/mysql/mysql.sock"),
                                        "plugin_dir": var(default="/usr/lib64/mysql/plugin/"),
                                        "datadir": var(default="/var/lib/mysql/"), "port": var(default="3306")}),
        }
        numa = {"innodb_numa_interleave": fact(kind="build"), "socket": fact(), "plugin_dir": fact(),
                "datadir": fact(), "port": fact()}
        catalog = gen.build("mysql", releases, records, {}, lambda tag: numa, {}, {}, {})
        for version in ("8.0.28", "8.0.29"):
            self.assertEqual(facts_at(catalog, "socket", version)[4], "build")
            self.assertEqual(facts_at(catalog, "datadir", version)[4], "build")
            self.assertIsNone(facts_at(catalog, "port", version)[4])
        self.assertEqual(facts_at(catalog, "plugin_dir", "8.0.29")[3], "/usr/lib64/mysql/plugin/")   # kept as read
        self.assertIsNone(facts_at(catalog, "innodb_numa_interleave", "8.0.29"))                     # HISTORY as read
        self.assertEqual(gen.build_only_candidates(catalog), {"innodb_numa_interleave": (["8.0.28"], ["8.0.29"])})
        report = "\n".join(gen.report_lines(catalog))
        self.assertIn("3: datadir, plugin_dir, socket.", report)
        self.assertIn("- Source and records: innodb_numa_interleave is in the records of 8.0.28 and missing from "
                      "8.0.29 while declared.", report)
        self.assertIn("Candidates not in BUILD_ONLY: none.", report)

    def test_mariadb_origin_auto(self):
        releases = [release("11.4.2", "2024-05-26T00:00:00Z", "mariadb-11.4.2", "stable")]
        variables = {"thread_pool_size": var(default="8", origin="AUTO"),
                     "thread_cache_size": var(default="256", origin="AUTO"),
                     "new_autosized": var(default="5", origin="AUTO"),
                     "max_connections": var(default="151", origin="COMPILE-TIME"),
                     "sort_buffer_size": var(default="2097152", origin="COMPILE-TIME"),
                     "version_source_revision": var(default=None, default_note="null", origin="COMPILE-TIME")}
        catalog = gen.build("mariadb", releases, {"11.4.2": record("11.4.2", variables, engine="mariadb")}, {},
                            None, {}, {}, {})
        note = lambda name: facts_at(catalog, name, "11.4.2")[4]  # noqa: E731
        self.assertEqual(note("thread_pool_size"), "host")          # reviewed
        self.assertEqual(note("new_autosized"), "host")             # origin AUTO, not reviewed
        self.assertIsNone(note("thread_cache_size"))                # AUTO reviewed as not host
        self.assertIsNone(note("max_connections"))                  # reviewed: only lowered when open files run short
        self.assertIsNone(note("sort_buffer_size"))
        self.assertEqual(note("version_source_revision"), "release")
        self.assertEqual(catalog["auto_excluded"], {"thread_cache_size": ["11.4.2"]})
        report = "\n".join(gen.report_lines(catalog))
        self.assertIn("Marked from origin AUTO and not in the reviewed list: new_autosized (11.4.2).", report)
        self.assertIn("Origin AUTO reviewed as not host: thread_cache_size (11.4.2).", report)
        self.assertIn("Origin in the records: AUTO 3, COMPILE-TIME 3.", report)

    def test_source_scan_finds_startup_defaults(self):
        sources = {
            "sql/sys_vars.cc": '''
                static Sys_var_ulonglong Sys_temptable_max_ram("temptable_max_ram", "doc",
                    GLOBAL_VAR(temptable_max_ram), CMD_LINE(REQUIRED_ARG), VALID_RANGE(2, ULLONG_MAX),
                    DEFAULT(std::clamp(ulonglong{3 * (my_physical_memory() / 100)}, 1ULL << 30, 1ULL << 32)),
                    BLOCK_SIZE(1));
                static Sys_var_ulonglong Sys_later("later_ram", "doc", GLOBAL_VAR(later_ram),
                    CMD_LINE(REQUIRED_ARG), VALID_RANGE(2, ULLONG_MAX), DEFAULT(1 << 30), BLOCK_SIZE(1));
                void update_later_ram_default() {
                  const ulonglong new_default =
                      std::clamp(ulonglong{3 * (my_physical_memory() / 100)}, 1ULL << 30, 1ULL << 32);
                  Sys_later.update_default(new_default);
                }
                static Sys_var_long Sys_pfs_digest_size("performance_schema_digests_size", "doc",
                    READ_ONLY GLOBAL_VAR(pfs_param.m_digest_sizing), CMD_LINE(REQUIRED_ARG),
                    VALID_RANGE(-1, 1024 * 1024), DEFAULT(PFS_AUTOSIZE_VALUE), BLOCK_SIZE(1));
                static Sys_var_bool Sys_lcfs("lower_case_file_system", "doc",
                    READ_ONLY GLOBAL_VAR(lower_case_file_system), NO_CMD_LINE, DEFAULT(false));
                static Sys_var_charptr Sys_log_bin_basename("log_bin_basename", "doc",
                    READ_ONLY GLOBAL_VAR(log_bin_basename), NO_CMD_LINE, DEFAULT(0));
                static Sys_var_ulong Sys_open_files("open_files_limit", "doc",
                    READ_ONLY GLOBAL_VAR(open_files_limit), CMD_LINE(REQUIRED_ARG), VALID_RANGE(0, 1),
                    DEFAULT(0), BLOCK_SIZE(1));
            ''',
            "sql/mysqld.cc": '''
                static void adjust_open_files_limit(ulong *requested_open_files) {
                  effective_open_files = my_set_max_open_files(request_open_files);
                  open_files_limit = effective_open_files;
                }
                static const char *help = "use --log-bin=name, sized by my_num_vcpus()";
                void init() {
                  lower_case_file_system = test_if_case_insensitive(mysql_real_data_home);
                  log_bin_basename = rpl_make_log_name("-bin", "name");
                }
            ''',
            "storage/innobase/handler/ha_innodb.cc": '''
                static MYSQL_SYSVAR_ULONG(purge_threads, srv_n_purge_threads, PLUGIN_VAR_OPCMDARG,
                    "doc", nullptr, nullptr, (std::thread::hardware_concurrency() <= 16 ? 1UL : 4UL), 1, 32, 0);
                static MYSQL_SYSVAR_ULONG(read_io_threads, srv_n_read_io_threads, PLUGIN_VAR_RQCMDARG,
                    "doc", nullptr, nullptr, 4, 1, 64, 0);
                static MYSQL_SYSVAR_ULONG(io_capacity_max, srv_max_io_capacity, PLUGIN_VAR_RQCMDARG,
                    "doc", nullptr, nullptr, 2000, 100, 100000, 0);
                inline void update_sysvars_default() {
                  auto num_vcpus = my_num_vcpus();
                  mysql_sysvar_io_capacity_max.def_val = std::clamp(ulong{2 * srv_io_capacity}, 100UL, 999UL);
                  if (!innodb_variable_is_set("innodb_read_io_threads")) {
                    srv_n_read_io_threads = std::clamp(num_vcpus / 2, 4U, 64U);
                  }
                }
            ''',
        }
        facts = gen.mysql_source_facts(sources)
        host = {name: fact.get("host") for name, fact in facts.items() if fact.get("host")}
        self.assertEqual(sorted(host), ["innodb_purge_threads", "innodb_read_io_threads", "later_ram",
                                        "lower_case_file_system", "open_files_limit",
                                        "performance_schema_digests_size", "temptable_max_ram"])
        self.assertIn("sql/sys_vars.cc: declared default calls my_physical_memory", host["temptable_max_ram"])
        self.assertIn("sql/sys_vars.cc: update_default() at startup from new_default", host["later_ram"])
        self.assertIn("sql/mysqld.cc: adjust_open_files_limit() at startup", host["open_files_limit"])
        self.assertIn("storage/innobase/handler/ha_innodb.cc: set at startup when not given, from num_vcpus",
                      host["innodb_read_io_threads"])

    def test_mariadb_source_scan_follows_autosize(self):
        sources = {
            "sql/sys_vars.cc": '''
                static Sys_var_ulong Sys_open_files_limit("open_files_limit", "doc",
                    AUTO_SET READ_ONLY GLOBAL_VAR(open_files_limit), CMD_LINE(REQUIRED_ARG),
                    VALID_RANGE(0, 1), DEFAULT(0), BLOCK_SIZE(1));
                static Sys_var_uint Sys_threadpool_size("thread_pool_size", "doc",
                    GLOBAL_VAR(threadpool_size), CMD_LINE(REQUIRED_ARG), VALID_RANGE(1, 100000),
                    DEFAULT(8), BLOCK_SIZE(1));
                static Sys_var_ulong Sys_thread_cache_size("thread_cache_size", "doc",
                    AUTO_SET GLOBAL_VAR(thread_cache_size), CMD_LINE(REQUIRED_ARG), VALID_RANGE(0, 16384),
                    DEFAULT(256), BLOCK_SIZE(1));
                static Sys_var_ulong Sys_log_tc_size("log_tc_size", "doc", READ_ONLY GLOBAL_VAR(opt_tc_log_size),
                    CMD_LINE(REQUIRED_ARG), VALID_RANGE(my_getpagesize() * 3, ULONG_MAX),
                    DEFAULT(my_getpagesize() * 6), BLOCK_SIZE(my_getpagesize()));
            ''',
            "storage/innobase/handler/ha_innodb.cc": '''
                static MYSQL_SYSVAR_ULONG(io_capacity_max, srv_max_io_capacity, PLUGIN_VAR_RQCMDARG, "doc",
                  NULL, innodb_io_capacity_max_update, SRV_MAX_IO_CAPACITY_DUMMY_DEFAULT, 100,
                  SRV_MAX_IO_CAPACITY_LIMIT, 0);
                static int innodb_init_params() {
                  if (srv_max_io_capacity == SRV_MAX_IO_CAPACITY_DUMMY_DEFAULT)
                    srv_max_io_capacity= std::max(2 * srv_io_capacity, 2000UL);
                }
            ''',
            "sql/mysqld.cc": '''
                static int init_common_variables() {
                  files= my_set_max_open_files(max_open_files);
                  SYSVAR_AUTOSIZE_IF_CHANGED(open_files_limit, files, ulong);
                  SYSVAR_AUTOSIZE(threadpool_size, my_getncpus());
                  SYSVAR_AUTOSIZE(thread_cache_size, max_connections);
                }
            ''',
        }
        facts = gen.mariadb_source_facts(sources)
        host = sorted(name for name, fact in facts.items() if fact.get("host"))
        self.assertEqual(host, ["innodb_io_capacity_max", "log_tc_size", "open_files_limit", "thread_pool_size"])
        self.assertIn("placeholder default SRV_MAX_IO_CAPACITY_DUMMY_DEFAULT replaced at startup",
                      facts["innodb_io_capacity_max"]["host"][0])

    def test_flip_flops_and_builds_sentence(self):
        releases = [release("10.11.11", "2025-01-01T00:00:00Z", "mariadb-10.11.11", "stable"),
                    release("10.11.12", "2025-02-01T00:00:00Z", "mariadb-10.11.12", "stable"),
                    release("10.11.13", "2025-03-01T00:00:00Z", "mariadb-10.11.13", "stable"),
                    release("11.4.2", "2025-04-01T00:00:00Z", "mariadb-11.4.2", "stable")]
        records = {"10.11.11": record("10.11.11", {"character_set_server": var(default="utf8mb4")}, "mariadb"),
                   "10.11.12": record("10.11.12", {"character_set_server": var(default="latin1")}, "mariadb",
                                      method="package"),
                   "10.11.13": record("10.11.13", {"character_set_server": var(default="utf8mb4")}, "mariadb"),
                   "11.4.2": record("11.4.2", {"character_set_server": var(default="latin1")}, "mariadb")}
        catalog = gen.build("mariadb", releases, records, {}, None, {}, {}, {})
        self.assertEqual(gen.flip_flops(catalog), [("character_set_server", "10.11.12", "utf8mb4", "latin1")])
        report = "\n".join(gen.report_lines(catalog))
        self.assertIn("- 10.11.12: character_set_server (utf8mb4, then latin1)", report)
        self.assertIn("Releases read from packages instead of images: 10.11.12; with a default that changes and "
                      "changes back: 10.11.12.", report)
        self.assertEqual(gen.builds_sentence("mariadb", catalog),
                         "Values come from MariaDB's own Ubuntu packages, as in the official MariaDB Docker image "
                         "(10.11.12 from MariaDB's generic Linux packages); other builds can compile other defaults.")
        records["10.11.12"]["package"] = "https://archive.mariadb.org/mariadb-10.11.12/repo/ubuntu jammy main"
        self.assertEqual(gen.builds_sentence("mariadb", gen.build("mariadb", releases, records, {}, None, {}, {}, {})),
                         "Values come from MariaDB's own Ubuntu packages, as in the official MariaDB Docker image; "
                         "other builds can compile other defaults.")
        self.assertEqual(gen.generated_info("mysql", "2026-10-28", {})["builds"],
                         "Values come from Oracle's official MySQL Docker images; other builds can compile other "
                         "defaults.")


class TestCheckerFixes(unittest.TestCase):
    def mysql(self, versions, variables, sources=None, manual=None):
        dates = {"8.0.29": "2022-04-08", "8.0.30": "2022-07-06", "8.0.36": "2024-01-08", "8.3.0": "2023-12-14",
                 "8.4.0": "2024-04-10", "8.4.13": "2026-09-30", "9.5.0": "2025-09-23", "9.6.0": "2025-12-23",
                 "26.7.0": "2026-07-28"}
        releases = [release(version, dates[version] + "T00:00:00Z") for version in versions]
        records = {version: record(version, {name: var(**dict(spec)) for name, spec in variables.items()})
                   for version in versions}
        return gen.build("mysql", releases, records, {}, (lambda tag: (sources or {}).get(tag, {})),
                         manual or {}, {}, {})

    def test_old_semisync_names_are_build_only(self):
        names = [name for name in gen.BUILD_ONLY["mysql"] if name.startswith("rpl_semi_sync_")]
        self.assertEqual(len(names), 8)
        self.assertEqual(len({gen.BUILD_ONLY["mysql"][name] for name in names}), 1)
        sentence = gen.BUILD_ONLY["mysql"]["rpl_semi_sync_master_enabled"]
        for text in ("8.0.26", "9.5.0", "rpl_semi_sync_source", "rpl_semi_sync_replica_*"):
            self.assertIn(text, sentence)

    def test_default_rules_follow_review_ranges_and_presence(self):
        catalog = self.mysql(["8.3.0", "8.4.0", "9.5.0", "9.6.0"],
                             {"temptable_max_ram": {"default": "1073741824"}, "innodb_flush_method": {},
                              "innodb_buffer_pool_instances": {}, "innodb_page_cleaners": {}})
        rules = catalog["default_rules"]
        self.assertEqual([period[:2] for period in rules["temptable_max_ram"]],
                         [("8.4.0", "9.5.0"), ("9.6.0", None)])
        self.assertIn("3% of physical memory", rules["temptable_max_ram"][0][2])
        self.assertEqual([period[:2] for period in rules["innodb_buffer_pool_instances"]],
                         [("8.3.0", "8.3.0"), ("8.4.0", None)])
        self.assertIn("8 when innodb_buffer_pool_size is 1 GiB or more", rules["innodb_buffer_pool_instances"][0][2])
        self.assertEqual(rules["innodb_flush_method"], (("8.4.0", None, gen.review_entry(
            "mysql", "innodb_flush_method", "8.4.0")[5]),))
        self.assertEqual(facts_at(catalog, "innodb_flush_method", "8.4.0")[4], "host")
        self.assertIsNone(facts_at(catalog, "innodb_flush_method", "8.3.0")[4])
        self.assertIsNone(facts_at(catalog, "innodb_buffer_pool_instances", "9.6.0")[4])    # reviewed, not marked
        self.assertTrue(gen.in_range("10.11.19", "10.11.17", "10.11"))
        self.assertFalse(gen.in_range("11.0.2", "10.11.17", "10.11"))
        self.assertFalse(gen.in_range("11.4.10", "11.4.11", "11.4"))
        report = "\n".join(gen.report_lines(catalog))
        self.assertIn("### How computed defaults are set (DEFAULT_RULES, 4 names)", report)

    def test_mariadb_rules_with_gaps_and_io_capacity_max(self):
        versions = ["10.11.8", "10.11.9", "11.0.2", "11.4.2", "11.4.3", "11.4.6", "11.4.11"]
        releases = [release(version, "2024-%02d-01T00:00:00Z" % (index + 1), "mariadb-" + version, "stable")
                    for index, version in enumerate(versions)]
        records = {}
        for version in versions:
            variables = {"innodb_io_capacity_max": var(default="4294967295")}
            if version not in ("10.11.8", "11.0.2", "11.4.2"):
                variables["innodb_log_write_ahead_size"] = var(default="512")
            if version in ("11.4.6", "11.4.11"):
                variables["innodb_buffer_pool_size_max"] = var(default="0")
            records[version] = record(version, variables, engine="mariadb")
        catalog = gen.build("mariadb", releases, records, {}, None, {}, {}, {})
        rules = catalog["default_rules"]
        self.assertEqual([period[:2] for period in rules["innodb_log_write_ahead_size"]],
                         [("10.11.9", "10.11.9"), ("11.4.3", None)])
        self.assertEqual([period[:2] for period in rules["innodb_buffer_pool_size_max"]],
                         [("11.4.6", "11.4.6"), ("11.4.11", "11.4.11")])    # the 11.4.11 entry ends with 11.4
        self.assertIn("Declared as 0", rules["innodb_buffer_pool_size_max"][0][2])
        self.assertIn("address-space limit", rules["innodb_buffer_pool_size_max"][1][2])
        for version in versions:
            self.assertEqual(facts_at(catalog, "innodb_io_capacity_max", version)[4], "host")
        self.assertIsNone(facts_at(catalog, "innodb_log_write_ahead_size", "11.4.3")[4])

    def test_reviewed_source_deprecation_and_rejected_manual_evidence(self):
        manual = {"innodb_log_file_size": [("8.0.30", None)],
                  "group_replication_communication_stack": [("8.4.11", None)]}
        catalog = self.mysql(["8.0.29", "8.0.30", "8.4.13", "26.7.0"],
                             {"innodb_log_file_size": {}, "innodb_redo_log_capacity": {},
                              "group_replication_communication_stack": {}}, manual=manual)
        self.assertIsNone(facts_at(catalog, "innodb_log_file_size", "8.0.29")[8])
        self.assertEqual(facts_at(catalog, "innodb_log_file_size", "8.0.30")[8:10],
                         (("manual", "source"), "innodb_redo_log_capacity"))
        self.assertIsNone(facts_at(catalog, "group_replication_communication_stack", "8.4.13")[8])
        self.assertEqual(facts_at(catalog, "group_replication_communication_stack", "26.7.0")[8], ("manual",))
        report = "\n".join(gen.report_lines(catalog))
        self.assertIn("Manual evidence rejected for group_replication_communication_stack at 8.4.13: Before 26.7.0",
                      report)
        self.assertIn("Reviewed source deprecation: innodb_log_file_size from 8.0.30, replacement "
                      "innodb_redo_log_capacity", report)
        self.assertIn("with no server or source evidence in any cataloged release (1): "
                      "group_replication_communication_stack (deprecated in 8.4.11).", report)    # not innodb_log_*

    def test_error_size_is_release_identity(self):
        catalog = self.mysql(["8.4.0"], {"performance_schema_error_size": {"default": "5533"}})
        self.assertEqual(facts_at(catalog, "performance_schema_error_size", "8.4.0")[3:5], ("5533", "release"))

    def test_mariadb_alias_help_texts(self):
        sources = {"sql/sys_vars.cc": '''
            static Sys_var_mybool Sys_slow_query_log("slow_query_log",
                   "Alias for log_slow_query. Log slow queries to a table or log file.",
                   SESSION_VAR(sql_log_slow), CMD_LINE(OPT_ARG), DEFAULT(FALSE));
            static Sys_var_double Sys_long_query_time("long_query_time",
                   "Alias for log_slow_query_time. Log all queries that have taken more than long_query_time",
                   SESSION_VAR(long_query_time_double), CMD_LINE(REQUIRED_ARG), VALID_RANGE(0, LONG_TIMEOUT),
                   DEFAULT(10));
            static Sys_var_session_special Sys_identity("identity", "Synonym for the last_insert_id variable",
                   NO_SET_STMT READ_ONLY sys_var::ONLY_SESSION, NO_CMD_LINE, VALID_RANGE(0, ULONGLONG_MAX),
                   BLOCK_SIZE(1), NO_MUTEX_GUARD, IN_BINLOG, ON_CHECK(0), ON_UPDATE(update_last_insert_id),
                   ON_READ(read_last_insert_id));
            static Sys_var_charptr Sys_debug_dbug("debug_dbug", "Built-in DBUG debugger. Alias for --debug",
                   SESSION_VAR(x), CMD_LINE(OPT_ARG), DEFAULT(0));
        '''}
        facts = gen.mariadb_source_facts(sources)
        self.assertEqual(facts["slow_query_log"]["alias_of"], "log_slow_query")
        self.assertEqual(facts["long_query_time"]["alias_of"], "log_slow_query_time")
        self.assertEqual(facts["identity"]["alias_of"], "last_insert_id")
        self.assertIsNone(facts["debug_dbug"]["alias_of"])
        releases = [release("11.4.2", "2024-05-26T00:00:00Z", "mariadb-11.4.2", "stable")]
        records = {"11.4.2": record("11.4.2", {"slow_query_log": var(), "log_slow_query": var(),
                                               "long_query_time": var()}, engine="mariadb")}
        catalog = gen.build("mariadb", releases, records, {}, lambda tag: facts, {}, {}, {})
        self.assertEqual(facts_at(catalog, "slow_query_log", "11.4.2")[10], "log_slow_query")
        self.assertIsNone(facts_at(catalog, "long_query_time", "11.4.2")[10])    # base not in this release

    def test_scan_sees_wrapped_flush_method_and_buffer_pool_instances(self):
        sources = {"storage/innobase/handler/ha_innodb.cc": '''
            static MYSQL_SYSVAR_ENUM(flush_method, innodb_flush_method, PLUGIN_VAR_RQCMDARG | PLUGIN_VAR_READONLY,
                "doc", nullptr, nullptr, 0, &innodb_flush_method_typelib);
            static MYSQL_SYSVAR_LONG(buffer_pool_instances, srv_buf_pool_instances,
                PLUGIN_VAR_RQCMDARG | PLUGIN_VAR_READONLY, "doc", nullptr, nullptr, 0, 0, 64, 0);
            static int innodb_init_params() {
              if (srv_buf_pool_size < BUF_POOL_SIZE_THRESHOLD) {
                srv_buf_pool_instances = 1;
              } else if (!innodb_buffer_pool_instances_is_set()) {
                ulong cpu_hint = ulong{std::thread::hardware_concurrency() / 4};
                srv_buf_pool_instances = std::clamp(std::min(bp_hint, cpu_hint), 1UL, 64UL);
              }
              if (!innodb_flush_method_is_set()) {
                innodb_flush_method = os_is_o_direct_supported() ? SRV_UNIX_O_DIRECT : SRV_UNIX_FSYNC;
              }
            }
        '''}
        facts = gen.mysql_source_facts(sources)
        self.assertIn("os_is_o_direct_supported", facts["innodb_flush_method"]["host"][0])
        self.assertTrue(facts["innodb_buffer_pool_instances"]["host"])


class TestMySQL57(unittest.TestCase):
    def test_scope_starts_at_the_first_57_ga(self):
        def text(numbers, extra=""):
            return ("MYSQL_VERSION_MAJOR=%d\nMYSQL_VERSION_MINOR=%d\nMYSQL_VERSION_PATCH=%d\n"
                    "MYSQL_VERSION_EXTRA=%s\n" % (numbers + (extra,)))
        files = {"mysql-5.6.51": text((5, 6, 51)), "mysql-5.7.8": text((5, 7, 8), "-rc"),
                 "mysql-5.7.9": text((5, 7, 9)), "mysql-5.7.44": text((5, 7, 44)),
                 "mysql-5.7.10-2": text((5, 7, 10)), "mysql-8.0.4": text((8, 0, 4), "-rc"),
                 "mysql-8.0.11": text((8, 0, 11))}
        found = gen.releases_from("mysql", files, {tag: "2020-01-01T00:00:00Z" for tag in files})
        self.assertEqual([(item["version"], item["maturity"]) for item in found],
                         [("5.7.9", "GA"), ("5.7.44", "GA"), ("8.0.11", "GA")])

    def test_80_follows_the_latest_57_dated_before_it(self):
        releases = [release("5.7.21", "2017-12-28T00:00:00Z"), release("5.7.22", "2018-03-04T00:00:00Z"),
                    release("5.7.23", "2018-06-08T00:00:00Z"), release("8.0.11", "2018-04-08T08:17:02+02:00"),
                    release("8.0.12", "2018-06-28T17:46:28+02:00")]
        previous = gen.lineage(releases)
        named = {item["version"]: (releases[index]["version"] if index is not None else None)
                 for item, index in zip(releases, previous)}
        self.assertEqual(named, {"5.7.21": None, "5.7.22": "5.7.21", "5.7.23": "5.7.22", "8.0.11": "5.7.22",
                                 "8.0.12": "8.0.11"})

    def test_manual_section_from_the_pdf(self):
        text = """Options and Variables Deprecated in MySQL 5.7
      The following system variables, status variables, and options have been deprecated in MySQL 5.7.

      \u2022 Qcache_hits: Number of query cache hits. Deprecated in MySQL 5.7.20.

      \u2022 innodb_undo_logs: Number of undo logs (rollback segments) used by InnoDB; alias for
        innodb_rollback_segments. Deprecated in MySQL
        5.7.19.


38
                       Options and Variables Deprecated in MySQL 5.7

\u2022 tx_isolation: Default transaction isolation level. Deprecated in MySQL 5.7.20.
                              Options and Variables Removed in MySQL 5.7
\u2022 query_cache_size: Memory allocated to store results from old queries. Deprecated in MySQL 5.7.20.

Options and Variables Removed in MySQL 5.7
      \u2022 Max_statement_time_exceeded: Number of statements that exceeded execution timeout value.
        Removed in MySQL 5.7.8.
"""
        self.assertEqual(gen.parse_manual_text(text), [
            ("Qcache_hits", "5.7.20", None), ("innodb_undo_logs", "5.7.19", None),
            ("tx_isolation", "5.7.20", None), ("query_cache_size", "5.7.20", None)])
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "added-deprecated-removed-5.7.txt").write_text(text, encoding="utf-8")
            Path(directory, "SAVED.json").write_text(json.dumps({"pages": {"5.7": {"saved_at": "2026-10-31T12:31:06Z"}}}))
            entries, saved = gen.read_manual(directory)
        self.assertEqual(saved, {"5.7": "2026-10-31"})
        self.assertEqual(entries["query_cache_size"], [("5.7.20", None)])

    def test_57_records_have_no_origin_by_design(self):
        records = {"5.7.44": record("5.7.44", {"a": var()}),
                   "8.0.11": record("8.0.11", {"a": var(origin="COMPILED")})}
        self.assertEqual(gen.mixed_origin(records), [])
        records["8.0.12"] = record("8.0.12", {"a": var()})
        self.assertEqual(gen.mixed_origin(records), ["8.0.12"])
        self.assertEqual(gen.mixed_origin({"8.0.12": records["8.0.12"]}), [])    # one run, no origin anywhere

    def test_57_scope_quirks_and_build_only_names(self):
        record57 = record("5.7.44", {"sql_log_bin": var(scope="session"), "gtid_executed": var(scope="global"),
                                     "sort_buffer_size": var(scope="global")})
        facts = {"sql_log_bin": fact(scope="both"), "gtid_executed": fact(scope="both"),
                 "sort_buffer_size": fact(scope="both")}
        result = gen.compare_release(record57, facts, "mysql", "5.7.44")
        self.assertEqual([item[0] for item in result["scope_explained"]], ["gtid_executed", "sql_log_bin"])
        self.assertEqual([item[0] for item in result["scope"]], ["sort_buffer_size"])
        later = gen.compare_release(record("8.0.11", {"sql_log_bin": var(scope="session")}),
                                    {"sql_log_bin": fact(scope="both")}, "mysql", "8.0.11")
        self.assertEqual([item[0] for item in later["scope"]], ["sql_log_bin"])
        for name in ("auto_generate_certs", "sha256_password_auto_generate_rsa_keys", "keyring_file_data"):
            self.assertIn(name, gen.BUILD_ONLY["mysql"])
        self.assertIn("5.7.28 dropped yaSSL", gen.BUILD_ONLY["mysql"]["auto_generate_certs"])
        self.assertIn("5.7.11", gen.BUILD_ONLY["mysql"]["keyring_file_data"])

    def test_57_plugin_files_and_reviewed_rules(self):
        sources = {
            "rapid/plugin/group_replication/src/plugin.cc": '''
                static MYSQL_SYSVAR_BOOL(start_on_boot, start_group_replication_at_boot_var,
                    PLUGIN_VAR_OPCMDARG | PLUGIN_VAR_PERSIST_AS_READ_ONLY, "doc", NULL, NULL, 1);
            ''',
            "plugin/password_validation/validate_password.cc": '''
                static MYSQL_SYSVAR_INT(length, validate_password_length, PLUGIN_VAR_RQCMDARG, "doc",
                    NULL, NULL, 8, 0, 0, 0);
            ''',
        }
        facts = gen.mysql_source_facts(sources)
        self.assertEqual(facts["group_replication_start_on_boot"]["group"], "group_replication")
        self.assertEqual(facts["validate_password_length"]["group"], "validate_password")
        for name in ("open_files_limit", "innodb_buffer_pool_instances", "innodb_page_cleaners",
                     "performance_schema_digests_size"):
            self.assertIsNotNone(gen.review_entry("mysql", name, "5.7.9"), name)
        self.assertIsNone(gen.review_entry("mysql", "innodb_flush_method", "5.7.44"))
        self.assertIsNotNone(gen.source_deprecation("mysql", "innodb_undo_tablespaces", "5.7.21"))
        self.assertIsNone(gen.source_deprecation("mysql", "innodb_undo_tablespaces", "5.7.20"))


class TestSourceFacts(unittest.TestCase):
    def test_mysql_markers_aliases_conditions_and_clone(self):
        sources = {
            "sql/sys_vars.cc": '''
                static Sys_var_ulong Sys_expire("expire_logs_days", "doc", GLOBAL_VAR(x),
                    CMD_LINE(REQUIRED_ARG), VALID_RANGE(0, 99), DEFAULT(0), BLOCK_SIZE(1), NO_MUTEX_GUARD,
                    NOT_IN_BINLOG, ON_CHECK(nullptr), ON_UPDATE(nullptr),
                    DEPRECATED_VAR("binlog_expire_logs_seconds"));
                static Sys_var_bool Sys_old("old_form", "doc", READ_ONLY GLOBAL_VAR(x),
                    CMD_LINE(OPT_ARG), DEFAULT(false), NO_MUTEX_GUARD, NOT_IN_BINLOG, ON_CHECK(0),
                    ON_UPDATE(0), DEPRECATED(""));
                static Sys_var_charptr Sys_init_replica("init_replica", "doc", GLOBAL_VAR(x),
                    CMD_LINE(REQUIRED_ARG));
                static Sys_var_deprecated_alias Sys_init_slave("init_slave", Sys_init_replica);
                #ifndef NDEBUG
                static Sys_var_charptr Sys_dbug("debug", "doc", SESSION_VAR(x), CMD_LINE(OPT_ARG));
                #endif
                #ifdef _WIN32
                static Sys_var_bool Sys_pipe("named_pipe", "doc", READ_ONLY GLOBAL_VAR(x), NO_CMD_LINE);
                #endif
                static Sys_var_bool Sys_pfs_consumer("performance_schema_consumer_x", "doc",
                    READ_ONLY NOT_VISIBLE GLOBAL_VAR(x), CMD_LINE(OPT_ARG), DEFAULT(false));
                #ifndef UNIV_HOTBACKUP
                static Sys_var_bool Sys_always("always_there", "doc", SESSION_VAR(x), CMD_LINE(OPT_ARG));
                #endif
            ''',
            "plugin/clone/src/clone_plugin.cc": '''
                static MYSQL_SYSVAR_UINT(buffer_size, clone_buffer_size, PLUGIN_VAR_RQCMDARG,
                    "doc", nullptr, nullptr, 4, 1, 256, 1);
                static MYSQL_THDVAR_BOOL(autotune, PLUGIN_VAR_READONLY, "doc", nullptr, nullptr, true);
            ''',
            "storage/innobase/handler/ha_innodb.cc": '''
                static MYSQL_SYSVAR_ULONG(log_checkpoint_every, x, PLUGIN_VAR_RQCMDARG | PLUGIN_VAR_EXPERIMENTAL,
                    "doc", NULL, NULL, 1000, 0, ULONG_MAX, 0);
                ATOMIC_SYSVAR_ULONG(autoinc_preallocate, PLUGIN_VAR_RQCMDARG | PLUGIN_VAR_READONLY,
                    "doc, with a comma", nullptr, after, 100);
            ''',
            "sql/ssl_init_callback.cc": '''
                static Sys_var_charptr Sys_ssl_ca("ssl_ca", "doc", PERSIST_AS_READONLY GLOBAL_VAR(opt_ssl_ca),
                    CMD_LINE(REQUIRED_ARG), IN_FS_CHARSET, DEFAULT(nullptr), &lock_ssl_ctx);
            ''',
            "storage/innobase/include/ut0atomic_sysvar.h": '''
                #define ATOMIC_SYSVAR_ULONG(sysvar_name_suffix, flags, description, validate,  \\
                                            after_change, default_value)                       \\
                  Atomic_sysvar<ulong> innodb_##sysvar_name_suffix(after_change);              \\
                  static MYSQL_SYSVAR_ULONG(                                                   \\
                      sysvar_name_suffix, innodb_##sysvar_name_suffix.registrable(), flags,    \\
                      description, validate, innodb_##sysvar_name_suffix.update, default_value)
            ''',
            "sql/auth/sql_authentication.cc": '''
                static MYSQL_SYSVAR_BOOL(auto_generate_rsa_keys, x, PLUGIN_VAR_READONLY | PLUGIN_VAR_OPCMDARG,
                    "doc", nullptr, nullptr, true);
            ''',
        }
        facts = gen.mysql_source_facts(sources)
        self.assertEqual((facts["expire_logs_days"]["deprecated"], facts["expire_logs_days"]["substitute"]),
                         (True, "binlog_expire_logs_seconds"))
        self.assertEqual((facts["old_form"]["deprecated"], facts["old_form"]["ro"]), (True, True))
        self.assertEqual((facts["init_slave"]["deprecated"], facts["init_slave"]["alias_of"]),
                         (True, "init_replica"))
        self.assertEqual(facts["init_slave"]["scope"], "global")
        self.assertFalse(facts["init_replica"]["deprecated"])
        self.assertEqual(facts["debug"]["kind"], "debug")
        self.assertEqual(facts["named_pipe"]["kind"], "build")
        self.assertEqual(facts["always_there"]["kind"], "")
        self.assertEqual(facts["performance_schema_consumer_x"]["kind"], "hidden")
        self.assertEqual((facts["clone_buffer_size"]["group"], facts["clone_buffer_size"]["scope"],
                          facts["clone_buffer_size"]["ro"]), ("clone", "global", False))
        self.assertEqual((facts["clone_autotune"]["scope"], facts["clone_autotune"]["ro"]), ("both", True))
        self.assertEqual(facts["innodb_log_checkpoint_every"]["kind"], "build")    # experimental
        self.assertEqual((facts["ssl_ca"]["scope"], facts["ssl_ca"]["ro"], facts["ssl_ca"]["group"]),
                         ("global", False, "core"))
        self.assertEqual((facts["innodb_autoinc_preallocate"]["scope"], facts["innodb_autoinc_preallocate"]["ro"]),
                         ("global", True))
        self.assertNotIn("innodb_sysvar_name_suffix", facts)
        self.assertEqual((facts["sha256_password_auto_generate_rsa_keys"]["group"],
                          facts["sha256_password_auto_generate_rsa_keys"]["ro"]), ("sha256_password", True))

    def test_mariadb_markers_and_plugin_variables(self):
        sources = {
            "sql/sys_vars.cc": '''
                static Sys_var_tx_isolation Sys_tx_isolation("tx_isolation", "doc", SESSION_VAR(x),
                    NO_CMD_LINE, tx_isolation_names, DEFAULT(0), NO_MUTEX_GUARD, NOT_IN_BINLOG,
                    ON_CHECK(0), ON_UPDATE(0), DEPRECATED(1101, "'@@transaction_isolation'"));
                static Sys_var_on_access_global<Sys_var_ulong, PRIV_SET_SYSTEM_GLOBAL_VAR_MAX_CONNECTIONS>
                Sys_max_connections("max_connections", "doc", GLOBAL_VAR(max_connections),
                    CMD_LINE(REQUIRED_ARG), VALID_RANGE(10, 100000), DEFAULT(151), BLOCK_SIZE(1));
                static Sys_var_charptr Sys_datadir("datadir", "doc", READ_ONLY GLOBAL_VAR(x),
                    CMD_LINE(REQUIRED_ARG), DEFAULT(0));
                static Sys_var_mybool Sys_keep_files_on_create("keep_files_on_create", "doc",
                    SESSION_VAR(x), CMD_LINE(OPT_ARG), DEFAULT(FALSE), NO_MUTEX_GUARD, NOT_IN_BINLOG,
                    ON_CHECK(0), ON_UPDATE(0), DEPRECATED_NO_REPLACEMENT(1008));
                static Sys_var_proxy_user Sys_proxy_user(
                       "proxy_user", "The proxy user account name used when logging in");
                static Sys_var_mybool Sys_old_10_6("big_tables", "doc", SESSION_VAR(x),
                    CMD_LINE(OPT_ARG), DEFAULT(FALSE), NO_MUTEX_GUARD, NOT_IN_BINLOG, ON_CHECK(0),
                    ON_UPDATE(0), DEPRECATED(""));
            ''',
            "storage/innobase/handler/ha_innodb.cc": '''
                static MYSQL_SYSVAR_BOOL(file_per_table, srv_file_per_table,
                    PLUGIN_VAR_NOCMDARG | PLUGIN_VAR_DEPRECATED, "doc", NULL, NULL, TRUE);
                static MYSQL_SYSVAR_UINT64_T(log_file_size, srv_log_file_size,
                    PLUGIN_VAR_RQCMDARG | PLUGIN_VAR_READONLY, "doc", NULL, NULL, 96 << 20, 1, ~0ULL, 0);
                #ifdef UNIV_DEBUG
                static MYSQL_SYSVAR_UINT(trx_rseg_n_slots_debug, x, PLUGIN_VAR_RQCMDARG, "doc",
                    NULL, NULL, 0, 0, 1024, 0);
                #endif
            ''',
            "storage/maria/ha_maria.cc": '''
                static MYSQL_SYSVAR_CONST_STR(log_dir_path, maria_data_root,
                    PLUGIN_VAR_RQCMDARG | PLUGIN_VAR_READONLY, "doc", NULL, NULL, "");
            ''',
            "plugin/server_audit/server_audit.c": '''
                static MYSQL_THDVAR_STR(loc_info, PLUGIN_VAR_NOSYSVAR | PLUGIN_VAR_NOCMDOPT, "doc",
                    NULL, NULL, "");
                static MYSQL_SYSVAR_ULONG(file_rotations, rotations, PLUGIN_VAR_RQCMDARG, "doc",
                    NULL, NULL, 9, 0, 999, 1);
            ''',
            "storage/maria/ha_s3.cc": '''
                static MYSQL_SYSVAR_ULONG(block_size, x, PLUGIN_VAR_RQCMDARG, "doc", 0, 0, 4, 4, 16, 1);
            ''',
        }
        facts = gen.mariadb_source_facts(sources)
        self.assertEqual((facts["tx_isolation"]["deprecated"], facts["tx_isolation"]["substitute"],
                          facts["tx_isolation"]["scope"]), (True, "'@@transaction_isolation'", "both"))
        self.assertEqual(gen.clean_name(facts["tx_isolation"]["substitute"]), "transaction_isolation")
        self.assertEqual((facts["max_connections"]["scope"], facts["max_connections"]["ro"]), ("global", False))
        self.assertTrue(facts["datadir"]["ro"])
        self.assertEqual((facts["big_tables"]["deprecated"], facts["big_tables"]["substitute"]), (True, ""))
        self.assertEqual((facts["innodb_file_per_table"]["deprecated"], facts["innodb_file_per_table"]["group"]),
                         (True, "innodb"))
        self.assertTrue(facts["innodb_log_file_size"]["ro"])
        self.assertEqual(facts["innodb_trx_rseg_n_slots_debug"]["kind"], "debug")
        self.assertIn("server_audit_file_rotations", facts)
        self.assertIn("proxy_user", facts)
        self.assertEqual((facts["keep_files_on_create"]["deprecated"], facts["keep_files_on_create"]["substitute"]),
                         (True, ""))
        self.assertEqual((facts["aria_log_dir_path"]["group"], facts["aria_log_dir_path"]["ro"]), ("aria", True))
        self.assertNotIn("server_audit_loc_info", facts)
        self.assertNotIn("s3_block_size", facts)      # S3 is not a plugin the lab loads

    def test_clean_name(self):
        self.assertEqual(gen.clean_name("'@@wsrep_sync_wait=1'"), "wsrep_sync_wait")
        self.assertEqual(gen.clean_name("--log_error_services"), "log_error_services")
        self.assertEqual(gen.clean_name("'@@old_mode'"), "old_mode")
        self.assertIsNone(gen.clean_name("performance_schema.tls_channel_status table"))
        self.assertIsNone(gen.clean_name(""))


class TestManual(unittest.TestCase):
    PAGE = '''<html><h3><a name="optvars-added"></a>Introduced</h3><ul>
      <li class="listitem"><p><code class="literal"><a href="x.html#sysvar_new_one">new_one</a></code>:
      New. Added in MySQL 8.0.30.</p></li></ul>
      <h3><a name="optvars-deprecated"></a>Deprecated</h3><ul>
      <li class="listitem"><p><code class="literal"><a href="x.html#sysvar_expire_logs_days">expire_logs_days</a></code>:
      Purge binary logs after this many days. Deprecated in MySQL
      8.0.3.</p></li>
      <li class="listitem"><p><code class="literal"><a href="x.html#sysvar_old_name">old_name</a></code>:
      Old thing; use new_name instead. Deprecated in MySQL 8.0.26.</p></li>
      <li class="listitem"><p><code class="literal"><a href="x.html#statvar_Compression">Compression</a></code>:
      Status. Deprecated in MySQL 8.0.18.</p></li></ul>
      <h3><a name="optvars-removed"></a>Removed</h3><ul>
      <li class="listitem"><p><code class="literal">gone</code>: Gone. Deprecated in MySQL 8.0.1.</p></li></ul>'''

    def test_parse_and_read_saved_pages(self):
        self.assertEqual(gen.parse_manual(self.PAGE), [("expire_logs_days", "8.0.3", None),
                                                       ("old_name", "8.0.26", "new_name"),
                                                       ("Compression", "8.0.18", None)])
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "added-deprecated-removed-8.0.html").write_text(self.PAGE)
            Path(directory, "SAVED.json").write_text(json.dumps(
                {"pages": {"8.0": {"saved_at": "2026-10-28T21:31:06Z"}}}))
            entries, saved = gen.read_manual(directory)
        self.assertEqual(saved, {"8.0": "2026-10-28"})
        self.assertEqual(entries["old_name"], [("8.0.26", "new_name")])

    def test_only_runtime_system_variables_are_kept(self):
        manual = {"Compression": [("8.0.11", None)], "x": [("8.0.11", None)]}
        records = {"8.0.11": record("8.0.11", {"x": var()})}
        catalog = gen.build("mysql", [release("8.0.11", "2018-04-08T00:00:00Z")], records, {},
                            None, manual, {}, {})
        self.assertEqual(set(catalog["history"]), {"x"})
        self.assertEqual(facts_at(catalog, "x", "8.0.11")[8], ("manual",))
        report = "\n".join(gen.report_lines(catalog))
        self.assertIn("Deprecated in the manual with no server or source evidence in any cataloged release (1): "
                      "x (deprecated in 8.0.11)", report)


class TestOutput(unittest.TestCase):
    def catalog(self, reverse=False):
        names = {"b": var(values=["OFF", "ON"]), "a": var(default="5"), "c": var(scope="both")}
        if reverse:
            names = dict(reversed(list(names.items())))
        records = {"8.0.11": record("8.0.11", names),
                   "8.0.12": record("8.0.12", {"a": var(default="6"), "b": var(values=["OFF", "ON"])})}
        releases = [release("8.0.11", "2018-04-08T08:17:02+02:00"), release("8.0.12", "2018-06-28T17:46:28+02:00"),
                    release("8.0.13", "2018-10-07T10:41:27+02:00")]
        return gen.build("mysql", releases, records, {}, None, {}, {}, {})

    def load(self, text):
        module = types.ModuleType("sysvar_history_fixture")
        exec(compile(text, "sysvar_history_fixture.py", "exec"), module.__dict__)
        return module

    def test_module_format_round_trip_and_determinism(self):
        generated = gen.generated_info("mysql", "2026-10-28", {"8.0": "2026-10-28"})
        text = gen.render_module(self.catalog(), generated)
        self.assertEqual(text, gen.render_module(self.catalog(reverse=True), generated))
        self.assertTrue(text.startswith("# Generated by scripts/gen_sysvar_history.py; do not edit.\n"))
        module = self.load(text)
        self.assertEqual(module.ENGINE, "mysql")
        self.assertEqual(module.FIELDS, gen.FIELDS)
        self.assertEqual(module.GENERATED["manual"], {"8.0": "2026-10-28"})
        self.assertEqual(module.VERSIONS[1], ("8.0.12", "mysql-8.0.12", "GA", "2018-06-28", 0, "image"))
        self.assertEqual(module.SKIPPED, (("8.0.13", gen.NOT_READ, "2018-10-07"),))
        self.assertEqual(module.HISTORY, self.catalog()["history"])
        self.assertEqual(module.HISTORY["c"][1], (1, None))
        self.assertEqual(sorted(name for name in vars(module) if not name.startswith("__")),
                         ["BUILD_ONLY", "DEFAULT_RULES", "ENGINE", "FIELDS", "GENERATED", "HISTORY", "SKIPPED",
                          "VERSIONS"])
        self.assertEqual(module.BUILD_ONLY, gen.BUILD_ONLY["mysql"])
        self.assertIn("HAVE_LIBNUMA", module.BUILD_ONLY["innodb_numa_interleave"])
        self.assertEqual(list(module.HISTORY), sorted(module.HISTORY))

    def test_python_37_syntax(self):
        if sys.version_info < (3, 8):
            self.skipTest("ast.parse feature_version needs Python 3.8")
        ast.parse(_SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 7))
        text = gen.render_module(self.catalog(), gen.generated_info("mariadb", "2026-10-28", {}))
        ast.parse(text, feature_version=(3, 7))


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestEndToEnd(unittest.TestCase):
    def git(self, repo, *args, env=None):
        subprocess.check_call(["git", "-C", repo] + list(args), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env)

    def commit(self, repo, files, tag, date):
        for path, text in files.items():
            target = Path(repo, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        self.git(repo, "add", "-A", env=env)
        self.git(repo, "commit", "-q", "-m", tag, env=env)
        self.git(repo, "tag", tag, env=env)

    def test_tiny_repository_records_manual_and_report(self):
        version_text = ("MYSQL_VERSION_MAJOR=8\nMYSQL_VERSION_MINOR=0\nMYSQL_VERSION_PATCH=%d\n"
                        "MYSQL_VERSION_EXTRA=\n")
        declaration = ('static Sys_var_ulong Sys_a("a", "doc", GLOBAL_VAR(x), CMD_LINE(REQUIRED_ARG), '
                       'VALID_RANGE(0, 9), DEFAULT(1), BLOCK_SIZE(1)%s);\n')
        with tempfile.TemporaryDirectory() as directory:
            repo = str(Path(directory, "repo"))
            os.makedirs(repo)
            self.git(repo, "init", "-q")
            self.commit(repo, {"MYSQL_VERSION": version_text % 11, "sql/sys_vars.cc": declaration % ""},
                        "mysql-8.0.11", "2018-04-08T08:17:02+02:00")
            self.commit(repo, {"MYSQL_VERSION": version_text % 12,
                               "sql/sys_vars.cc": declaration % (", NO_MUTEX_GUARD, NOT_IN_BINLOG, "
                                                                 "ON_CHECK(0), ON_UPDATE(0), DEPRECATED(\"\")") +
                               'static Sys_var_bool Sys_s("only_source", "doc", GLOBAL_VAR(y), NO_CMD_LINE);\n'},
                        "mysql-8.0.12", "2018-06-28T17:46:28+02:00")
            self.commit(repo, {"MYSQL_VERSION": version_text % 13}, "mysql-8.0.13", "2018-10-07T10:41:27+02:00")
            runtime = Path(directory, "runtime", "mysql")
            runtime.mkdir(parents=True)
            Path(runtime, "8.0.11.json").write_text(json.dumps(record("8.0.11", {"a": var(dynamic=None)})))
            Path(runtime, "8.0.12.json").write_text(json.dumps(record("8.0.12", {"a": var(dynamic=None)})))
            Path(runtime.parent, "versions-mysql.json").write_text(json.dumps(
                {"versions": [{"version": "8.0.11"}, {"version": "8.0.12"}, {"version": "8.0.13"}]}))
            manual = Path(directory, "manual")
            manual.mkdir()
            Path(manual, "added-deprecated-removed-8.0.html").write_text(TestManual.PAGE.replace(
                "expire_logs_days", "a").replace("8.0.3.", "8.0.12."))
            out = Path(directory, "out")
            out.mkdir()
            report = Path(directory, "VALIDATION.md")
            with contextlib.redirect_stdout(io.StringIO()):
                code = gen.main(["--runtime", str(runtime.parent), "--mysql-source", repo, "--engine", "mysql",
                                 "--manual", str(manual), "--output-dir", str(out), "--report", str(report),
                                 "--generated-on", "2026-10-28", "--cache", str(Path(directory, "cache"))])
            self.assertEqual(code, 0)
            text = Path(out, "sysvar_history_mysql.py").read_text()
            module = TestOutput().load(text)
            self.assertEqual([item[0] for item in module.VERSIONS], ["8.0.11", "8.0.12"])
            self.assertEqual(module.SKIPPED, (("8.0.13", gen.NOT_READ, "2018-10-07"),))
            history = module.HISTORY["a"]
            self.assertEqual(history[0][1][2], True)             # dynamic from the source flag
            self.assertEqual(history[1][0], 1)
            self.assertEqual(history[1][1][8], ("manual", "source"))
            self.assertNotIn("only_source", module.HISTORY)
            body = report.read_text()
            self.assertIn("The lab run's in-scope list matches the tag scan (3 releases).", body)
            self.assertIn("| 8.0.12 | 1 | 2 |", body)
            self.assertIn("only_source: 8.0.12", body)
            # Records of different lab runs (with and without origin) are refused unless allowed.
            mixed = record("8.0.12", {"a": var(dynamic=None, origin="COMPILED")})
            Path(runtime, "8.0.12.json").write_text(json.dumps(mixed))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                refused = gen.main(["--runtime", str(runtime.parent), "--mysql-source", repo, "--engine", "mysql",
                                    "--output-dir", str(out), "--report", str(report)])
            self.assertEqual(refused, 2)
            self.assertIn("1 of 2 records have no origin field (an earlier lab run): 8.0.11", err.getvalue())
            self.assertEqual(Path(out, "sysvar_history_mysql.py").read_text(), text)    # nothing written
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                allowed = gen.main(["--runtime", str(runtime.parent), "--mysql-source", repo, "--engine", "mysql",
                                    "--output-dir", str(out), "--report", str(report), "--allow-mixed-origin"])
            self.assertEqual(allowed, 0)
            self.assertIn("WARNING: generated from records of different lab runs", report.read_text())
            Path(runtime, "8.0.12.json").write_text(json.dumps(record("8.0.12", {"a": var(dynamic=None)})))
            # A second run reads the cached source facts and writes the same module.
            with contextlib.redirect_stdout(io.StringIO()):
                gen.main(["--runtime", str(runtime.parent), "--mysql-source", repo, "--engine", "mysql",
                          "--manual", str(manual), "--output-dir", str(out), "--generated-on", "2026-10-28",
                          "--cache", str(Path(directory, "cache"))])
            self.assertEqual(Path(out, "sysvar_history_mysql.py").read_text(), text)


if __name__ == "__main__":
    unittest.main()
