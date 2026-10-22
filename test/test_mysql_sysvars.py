"""Generated MySQL facts: hand-map agreement, parser edge cases and fallback."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

from myflames import mysql_sysvars, variables
from test_variables import mysql_variable


_SPEC = importlib.util.spec_from_file_location(
    "gen_mysql_sysvars", str(Path(__file__).resolve().parents[1] / "scripts/gen_mysql_sysvars.py"))
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)


class TestGeneratedCatalog(unittest.TestCase):
    def test_all_handwritten_scopes_and_flags_agree(self):
        mismatches = []
        for name, (scope, _, flags) in variables.MYSQL_VARIABLES.items():
            generated = mysql_sysvars.SYSVARS.get(name)
            expected_flags = set(flags.split()) - {"sro"}
            if (generated is None or generated[0] != scope or generated[2] is None
                    or set(generated[2].split()) & {"ro", "np", "pro"} != expected_flags):
                mismatches.append("%s: hand=%r generated=%r" % (name, (scope, flags), generated))
        self.assertEqual(mismatches, [], "\n".join(mismatches))

    def test_spot_checks_and_size(self):
        self.assertGreaterEqual(len(mysql_sysvars.SYSVARS), 500)
        self.assertEqual(mysql_sysvars.SOURCE_TAGS[0], "mysql-9.7.0")
        self.assertRegex(mysql_sysvars.SOURCE_TAGS[1], r"^mysql-8\.4\.\d+$")
        for name, expected in {
                "max_connections": ("global", "integer", ""),
                "innodb_buffer_pool_instances": ("global", "integer", "ro"),
                "innodb_buffer_pool_size": ("global", "integer", "pro"),
                "core_file": ("global", "boolean", "ro np nc"),
                "log_bin": ("global", "boolean", "ro np nc"),
                "sort_buffer_size": ("both", "integer", ""),
                "sql_log_bin": ("session", "boolean", "nc"),
                "binlog_encryption": ("global", "boolean", "pro"),
                "gtid_executed": ("global", "string", "ro np nc"),
                "sql_require_primary_key": ("both", "boolean", ""),
                "restrict_fk_on_non_standard_key": ("both", "boolean", "np"),
                "innodb_native_foreign_keys": ("global", "boolean", "ro pro"),
                "enable_cascade_triggers": ("both", "boolean", ""),
                "innodb_version": ("global", "string", "ro np nc"),
                "proxy_user": ("session", "string", "ro"),
                "mysqlx_wait_timeout": ("both", "integer", ""),
                "validate_password.length": ("global", "integer", ""),
                "rpl_semi_sync_master_wait_for_slave_count": ("global", "integer", "")}.items():
            with self.subTest(name=name):
                self.assertEqual(mysql_sysvars.SYSVARS[name], expected)
        self.assertEqual(mysql_sysvars.VALUES["binlog_row_image"], ("MINIMAL", "NOBLOB", "FULL"))

    def test_conflicting_facts_are_unknown(self):
        self.assertIsNone(mysql_sysvars.SYSVARS["caching_sha2_password_digest_rounds"][2])
        self.assertIsNone(variables.static_dynamic("mysql", "caching_sha2_password_digest_rounds"))
        # large_pages has no command-line option on Windows and an optional
        # argument on other builds; the catalog must not choose a platform.
        self.assertIsNone(mysql_sysvars.SYSVARS["large_pages"][2])
        self.assertIsNone(variables.static_dynamic("mysql", "large_pages"))
        self.assertIsNone(mysql_sysvars.SYSVARS["innodb_log_writer_threads"][1])
        self.assertEqual(variables._flags("caching_sha2_password_digest_rounds"), set())

    def test_fallback_preserves_hand_map_and_engine_boundaries(self):
        variable = mysql_variable("mysqlx_port")
        self.assertEqual((variable["scope"], variable["type"], variable["dynamic"]),
                         ("global", "integer", False))
        self.assertEqual(variable["notes"], [])
        self.assertTrue(variables.static_dynamic("mysql", "mysqlx_wait_timeout"))
        self.assertIsNone(variables.static_dynamic("mariadb", "mysqlx_wait_timeout"))
        self.assertIsNone(variables.static_dynamic("unknown", "max_connections"))
        self.assertEqual(mysql_variable("key_buffer_size")["type"], "bytes")
        self.assertIsNone(mysql_variable("myflames_unknown")["dynamic"])
        self.assertIn("no source-verified record", mysql_variable("myflames_unknown")["notes"][0])
        # The generated catalog never overrides handwritten refinements.
        with mock.patch.dict(mysql_sysvars.SYSVARS, {"max_connections": ("session", "string", "ro np")}):
            self.assertTrue(variables.static_dynamic("mysql", "max_connections"))
            self.assertEqual(variables._flags("max_connections"), set())
            self.assertEqual(mysql_variable("max_connections")["scope"], "global")
            self.assertEqual(mysql_variable("max_connections")["type"], "integer")
        text = mysql_variable("mysqlx_socket")
        self.assertFalse(text["modes"]["persist_only"]["available"])
        no_bounds = mysql_variable("mysqlx_port", info=None)
        self.assertFalse(no_bounds["modes"]["persist_only"]["available"])

    def test_metadata_enum_keeps_source_values(self):
        metadata = {"scope": "SESSION", "type": "Enumeration"}
        result = mysql_variable("binlog_row_image", metadata=metadata)
        self.assertEqual(result["values"], list(mysql_sysvars.VALUES["binlog_row_image"]))


class TestGenerator(unittest.TestCase):
    def test_balanced_declarations_aliases_and_literal_lists(self):
        source = r'''
          // static Sys_var_bool Sys_fake("fake", "", READ_ONLY GLOBAL_VAR(x));
          static const char *choices[] = {"A", "B" "C", nullptr};
          static Sys_var_enum Sys_mode(
              "mode", "Doc: (, ) READ_ONLY // words", SESSION_VAR(mode),
              NO_CMD_LINE, choices, DEFAULT(0));
          static Sys_var_ulong Sys_limit("limit", "doc", READ_ONLY
              NON_PERSIST GLOBAL_VAR(limit), CMD_LINE(OPT_ARG), VALID_RANGE(0, f(1, 2)));
          static Sys_var_deprecated_alias Sys_old("old_mode", Sys_mode);
          static Sys_var_charptr Sys_secret("secret", "doc", SESSION_ONLY(secret));
        '''
        result = gen.parse_sources({"sql/sys_vars.cc": source})
        self.assertNotIn("fake", result)
        self.assertEqual(result["mode"], ("both", "enum", "nc", ("A", "BC")))
        self.assertEqual(result["old_mode"], result["mode"])
        self.assertEqual(result["limit"], ("global", "integer", "ro np", None))
        self.assertEqual(result["secret"][0], "session")

    def test_constructor_flags_and_plugin_prefixes(self):
        source = {
            "sql/sys_vars.h": '''class Sys_var_binlog_encryption {
              Sys_var_binlog_encryption(int flag_args) : Sys_var_bool(name, doc,
                flag_args | PERSIST_AS_READ_ONLY, off) {} };''',
            "sql/sys_vars.cc": '''static Sys_var_binlog_encryption Sys_enc(
                "binlog_encryption", "doc", GLOBAL_VAR(x), NO_CMD_LINE);''',
            "storage/innobase/handler/ha_innodb.cc": '''
                static const char *choices[] = {"ON", "OFF", nullptr};
                static TYPELIB opts = {2, "", choices, nullptr};
                MYSQL_SYSVAR_ENUM(mode, mode, PLUGIN_VAR_READONLY | PLUGIN_VAR_NOPERSIST,
                    "doc", nullptr, nullptr, 0, &opts);
                MYSQL_THDVAR_UINT(limit, PLUGIN_VAR_PERSIST_AS_READ_ONLY,
                    "doc", nullptr, nullptr, 0, 0, 100, 1);
                MYSQL_SYSVAR_BOOL(hidden, x, PLUGIN_VAR_NOSYSVAR, "doc", nullptr, nullptr, 0);
            '''}
        result = gen.parse_sources(source)
        self.assertEqual(result["binlog_encryption"][2], "pro nc")
        self.assertEqual(result["innodb_mode"], ("global", "enum", "ro np", ("ON", "OFF")))
        self.assertEqual(result["innodb_limit"], ("both", "integer", "pro", None))
        self.assertNotIn("innodb_hidden", result)

    def test_core_snapshot_declaration_count_and_initializer_shapes(self):
        # Small source snapshot preserving the four previously missed forms,
        # a conditional duplicate, a template, and an alias. Count declarations
        # before merging names so a duplicate cannot conceal a skipped shape.
        source = '''
            #define PRIMARY_KEY_NAME "sql_require_primary_key"
            static Sys_var_bool Sys_primary_key{
                PRIMARY_KEY_NAME, "doc with } and (", SESSION_VAR(x),
                CMD_LINE(OPT_ARG), DEFAULT(false), ON_CHECK(check({1, 2}))};
            Sys_var_bool Sys_restrict("restrict_fk_on_non_standard_key", "doc",
                NON_PERSIST SESSION_VAR(x), CMD_LINE(OPT_ARG));
            Sys_var_bool Sys_native("innodb_native_foreign_keys", "doc",
                PERSIST_AS_READONLY READ_ONLY GLOBAL_VAR(x), CMD_LINE(OPT_ARG));
            Sys_var_bool Sys_cascade{"enable_cascade_triggers", "doc",
                SESSION_VAR(x), CMD_LINE(OPT_ARG)};
            #ifdef OTHER_BUILD
            static Sys_var_bool Sys_primary_key_other(
                PRIMARY_KEY_NAME, "doc", SESSION_VAR(x), CMD_LINE(OPT_ARG));
            #endif
            static Sys_var_struct<Foo, Bar> Sys_charset{
                "charset", "doc", GLOBAL_VAR(x), NO_CMD_LINE};
            Sys_var_deprecated_alias Sys_old{"old_primary_key", Sys_primary_key};
        '''
        self.assertEqual(len(gen.core_declarations(gen.uncomment(source))), 7)
        result = gen.parse_sources({"sql/sys_vars.cc": source})
        self.assertEqual(len(result), 6)
        self.assertEqual(result["sql_require_primary_key"], ("both", "boolean", "", None))
        self.assertEqual(result["restrict_fk_on_non_standard_key"], ("both", "boolean", "np", None))
        self.assertEqual(result["innodb_native_foreign_keys"], ("global", "boolean", "ro pro", None))
        self.assertEqual(result["enable_cascade_triggers"], ("both", "boolean", "", None))
        self.assertEqual(result["old_primary_key"], result["sql_require_primary_key"])
        self.assertEqual(result["charset"], ("global", "string", "nc", None))
        # A shape outside the parser must fail instead of shrinking the catalog.
        with self.assertRaisesRegex(ValueError, "Unparsed core declarations: Sys_new"):
            gen.parse_sources({"sql/sys_vars.cc": source + '''
                Sys_var_bool Sys_new = {"new", "doc", GLOBAL_VAR(x)};
            '''})

    def test_nonliteral_nonstatic_names_are_skipped_but_static_names_fail(self):
        source = '''
            #define KNOWN_NAME "known"
            Sys_var_bool make_variable(const char *name, int flags);
            Sys_var_bool Sys_unknown(UNKNOWN_NAME, "doc", GLOBAL_VAR(x));
            Sys_var_bool Sys_known(KNOWN_NAME, "doc", GLOBAL_VAR(x));
        '''
        self.assertEqual(gen.parse_sources({"sql/sys_vars.cc": source}),
                         {"known": ("global", "boolean", "", None)})
        with self.assertRaisesRegex(ValueError, "Nonliteral core variable name: Sys_unknown"):
            gen.parse_sources({"sql/sys_vars.cc": source.replace(
                "Sys_var_bool Sys_unknown", "static Sys_var_bool Sys_unknown")})

    def test_constructor_no_command_line_is_derived_and_inherited(self):
        header = '''
            class Sys_var_have { Sys_var_have(int flag_args) :
                sys_var(list, name, doc, flag_args, off, getopt.id) {
                    assert(getopt.id == -1);
                } };
            class Sys_var_have_func { Sys_var_have_func(int ignored) :
                Sys_var_have(name, doc, READ_ONLY NON_PERSIST GLOBAL_VAR(x), NO_CMD_LINE) {} };
            class Sys_var_charptr_func { Sys_var_charptr_func(int flag_arg) :
                sys_var(list, name, doc, READ_ONLY NON_PERSIST flag_arg, 0,
                        NO_CMD_LINE.id, NO_CMD_LINE.arg_type) {} };
            class Sys_var_version { Sys_var_version(int ignored) :
                Sys_var_charptr_func(name, doc, GLOBAL) {} };
            class Sys_var_test_flag { Sys_var_test_flag(int ignored) :
                Sys_var_bool(name, doc, READ_ONLY NON_PERSIST GLOBAL_VAR(x), NO_CMD_LINE) {} };
        '''
        # These constructors forward the option but assert that it is absent.
        for kind, parent in (("plugin", "sys_var"), ("bit", "Sys_var_typelib"),
                             ("session_special", "Sys_var_ulonglong"),
                             ("session_special_double", "Sys_var_double")):
            forwarded = "list, name, doc, flag_args" if parent == "sys_var" else "name, doc, flag_args"
            header += '''class Sys_var_%s { Sys_var_%s(int flag_args) : %s(%s, off, getopt) {
                assert(getopt.id == -1);
            } };''' % (kind, kind, parent, forwarded)
        kinds = ("have", "have_func", "charptr_func", "version", "test_flag", "plugin", "bit",
                 "session_special", "session_special_double")
        declarations = '\n'.join('static Sys_var_%s Sys_%s("%s", "doc", GLOBAL_VAR(x));' %
                                 (kind, kind, kind) for kind in kinds)
        result = gen.parse_sources({"sql/sys_vars.h": header, "sql/sys_vars.cc": declarations})
        for kind in kinds:
            with self.subTest(kind=kind):
                self.assertIn("nc", result[kind][2].split())
        self.assertEqual(result["version"], ("global", "string", "ro np nc", None))

    def test_no_command_line_tokens_and_conflicts(self):
        result = gen.parse_sources({"sql/sys_vars.cc": '''
            static Sys_var_bool Sys_x("x", "doc", GLOBAL_VAR(x), NO_CMD_LINE);
            static Sys_var_bool Sys_x_other("x", "doc", GLOBAL_VAR(x), CMD_LINE(OPT_ARG));
            static Sys_var_bool Sys_y("y", "NO_CMD_LINE", GLOBAL_VAR(x), CMD_LINE(NO_ARG));
            static Sys_var_charptr Sys_z("z", "doc", GLOBAL_VAR(x), CMD_LINE(OPT_ARG),
                DEFAULT("NO_CMD_LINE")); // NO_CMD_LINE is not a declaration flag here
        ''', "storage/innobase/handler/ha_innodb.cc": '''
            MYSQL_SYSVAR_STR(version, v, PLUGIN_VAR_READONLY | PLUGIN_VAR_NOPERSIST | PLUGIN_VAR_NOCMDOPT,
                "doc", nullptr, nullptr, "1.0");
            MYSQL_THDVAR_BOOL(toggle, PLUGIN_VAR_NOCMDOPT, "doc", nullptr, nullptr, false);
            MYSQL_SYSVAR_BOOL(noarg, v, PLUGIN_VAR_NOCMDARG, "doc", nullptr, nullptr, false);
        '''})
        self.assertIsNone(result["x"][2])
        self.assertEqual(result["y"][2], "")
        self.assertEqual(result["z"][2], "")
        self.assertEqual(result["innodb_version"], ("global", "string", "ro np nc", None))
        self.assertEqual(result["innodb_toggle"], ("both", "boolean", "nc", None))
        self.assertEqual(result["innodb_noarg"][2], "")

    def test_merge_is_fieldwise_and_unknown_is_sticky(self):
        merged = {}
        gen.add(merged, "x", ("global", "enum", "ro"), ("A", "B"))
        gen.add(merged, "x", ("global", "boolean", ""), ("OFF", "ON"))
        self.assertEqual(merged["x"], ("global", None, None, None))
        gen.add(merged, "x", ("both", "boolean", ""))
        self.assertEqual(merged["x"], (None, None, None, None))

    def test_conditional_command_line_options_preserve_unknown_flags(self):
        cases = (
            ("IF_WIN(NO_CMD_LINE, CMD_LINE(OPT_ARG))", None),
            ("IF_WIN(CMD_LINE(OPT_ARG), NO_CMD_LINE)", None),
            ("IF_WIN(NO_CMD_LINE, NO_CMD_LINE)", "ro np nc"),
            ("IF_WIN(CMD_LINE(OPT_ARG), CMD_LINE(NO_ARG))", "ro np"),
        )
        for option, flags in cases:
            with self.subTest(option=option):
                source = '''
                    static Sys_var_bool Sys_conditional("conditional", "doc",
                        READ_ONLY NON_PERSIST GLOBAL_VAR(x), %s);
                ''' % option
                result = gen.parse_sources({"sql/sys_vars.cc": source})
                self.assertEqual(result["conditional"], ("global", "boolean", flags, None))

    def test_quoted_option_tokens_do_not_create_conditional_conflicts(self):
        source = r'''
            static Sys_var_charptr Sys_no_option("no_option", "CMD_LINE(OPT_ARG)",
                READ_ONLY GLOBAL_VAR(x), IF_WIN(NO_CMD_LINE, NO_CMD_LINE),
                DEFAULT("CMD_LINE(OPT_ARG)"));
            static Sys_var_charptr Sys_option("option", "NO_CMD_LINE",
                READ_ONLY GLOBAL_VAR(x), IF_WIN(CMD_LINE(OPT_ARG), CMD_LINE(NO_ARG)),
                DEFAULT("NO_CMD_LINE"));
        '''
        result = gen.parse_sources({"sql/sys_vars.cc": source})
        self.assertEqual(result["no_option"][2], "ro nc")
        self.assertEqual(result["option"][2], "ro")

    def test_conditional_values_are_not_unioned(self):
        source = '''const char *choices[] = {
            #ifdef WINDOWS
            "windows",
            #else
            "unix",
            #endif
            nullptr};
            static Sys_var_enum Sys_x("x", "", GLOBAL_VAR(x), NO_CMD_LINE, choices);
        '''
        self.assertIsNone(gen.parse_sources({"sql/sys_vars.cc": source})["x"][3])

    def test_help_texts_follow_the_declarations(self):
        source = {
            "sql/sys_vars.cc": r'''
              #define PORT_HELP "Port " STRINGIFY_ARG(MYSQL_PORT)
              static Sys_var_ulong Sys_limit("limit", "The most "
                  "clients\n  allowed", GLOBAL_VAR(limit), CMD_LINE(REQUIRED_ARG));
              static Sys_var_uint Sys_port("port", "Port " STRINGIFY_ARG(MYSQL_PORT), GLOBAL_VAR(port));
              static Sys_var_deprecated_alias Sys_old("old_limit", Sys_limit);
              static Sys_var_bool Sys_have("have_x", "have_x", READ_ONLY GLOBAL_VAR(have));
              #ifdef A
              static Sys_var_bool Sys_v1("variant", "One text", GLOBAL_VAR(v));
              #else
              static Sys_var_bool Sys_v2("variant", "Another text", GLOBAL_VAR(v));
              #endif
            ''',
            "storage/innobase/handler/ha_innodb.cc": '''
                MYSQL_SYSVAR_ULONG(size, size, PLUGIN_VAR_RQCMDARG, "Pool size.", nullptr, nullptr, 1, 1, 9, 0);
                MYSQL_THDVAR_UINT(wait, PLUGIN_VAR_OPCMDARG, "Wait seconds.", nullptr, nullptr, 0, 0, 100, 1);
                MYSQL_SYSVAR_BOOL(hidden, x, PLUGIN_VAR_NOSYSVAR, "Hidden", nullptr, nullptr, 0);
            ''',
            "components/validate_password/validate_password_imp.cc": '''
                mysql_service_component_sys_variable_register->register_variable(
                    "validate_password", "length", PLUGIN_VAR_INT, "Minimum length", nullptr, nullptr, &arg, &len);
            '''}
        result = gen.parse_help(source)
        self.assertEqual(result["limit"], "The most clients allowed", "joined, with a newline escape as a space")
        self.assertEqual(result["old_limit"], result["limit"])
        self.assertNotIn("port", result, "a text built with a macro is unknown")
        self.assertNotIn("have_x", result, "a text that repeats the name says nothing")
        self.assertNotIn("variant", result, "build variants that disagree leave the text out")
        self.assertEqual((result["innodb_size"], result["innodb_wait"]), ("Pool size.", "Wait seconds."))
        self.assertNotIn("innodb_hidden", result)
        self.assertEqual(result["validate_password.length"], "Minimum length")

    def test_generated_help_texts(self):
        from myflames import mysql_sysvar_help as helps
        self.assertRegex(helps.HELP_TAGS[0], r"^mysql-8\.4\.\d+$")
        self.assertRegex(helps.HELP_TAGS[1], r"^mysql-8\.0\.\d+$")
        self.assertGreaterEqual(len(helps.HELP), 700)
        self.assertEqual(helps.HELP["max_connections"], "The number of simultaneous clients allowed")
        self.assertEqual(helps.HELP["innodb_buffer_pool_size"],
                         "The size of the memory buffer InnoDB uses to cache data and indexes of its tables.")
        self.assertIn("expire_logs_days", helps.HELP_80)
        self.assertNotIn("expire_logs_days", helps.HELP, "removed in 8.4")
        for table in (helps.HELP, helps.HELP_80):
            for name, text in table.items():
                with self.subTest(name=name):
                    self.assertTrue(text and text == " ".join(text.split()) and text.lower() != name)
        self.assertFalse(set(helps.HELP_80.items()) & set(helps.HELP.items()), "HELP_80 holds only differences")

    def test_newest_stable_tag_is_numeric(self):
        with mock.patch.object(gen, "git", return_value="mysql-8.4.9\nmysql-8.4.10\nmysql-8.4.11-preview\n"):
            self.assertEqual(gen.source_tags("unused"), ("mysql-9.7.0", "mysql-8.4.10"))
