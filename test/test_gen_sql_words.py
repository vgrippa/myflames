"""Unit tests for scripts/gen_sql_words.py."""

import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_sql_words.py"
_SPEC = importlib.util.spec_from_file_location("gen_sql_words", str(_SCRIPT))
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)


MYSQL_LEX_FIXTURE = """
/* MySQL sql/lex.h snippet */
static const SYMBOL symbols[] = {
  { SYM("&&",                       AND_AND_SYM)},
  { SYM("<=>",                      EQUAL_SYM)},
  { SYM("SELECT",                   SELECT_SYM)},
  { SYM("ACCOUNT",                  ACCOUNT_SYM)},
  { SYM_HK("BKA",                   BKA_HINT)},
  { SYM_H("MAX_EXECUTION_TIME",     MAX_EXECUTION_TIME_HINT)},
  { SYM("PARSE_GCOL_EXPR",          PARSE_GCOL_EXPR_SYM)},
  { SYM("ABORT",                    ABORT_SYM)},
  { SYM("GRAMMAR_SELECTOR_EXPR",    GRAMMAR_SELECTOR_EXPR)},
  /* { SYM("COMMENTED_WORD",        COMMENTED_SYM)}, */
  { SYM_FN("COUNT",                 COUNT_SYM)},
  { SYM_FN("CAST",                  CAST_SYM)}
};
"""

MYSQL_80_YACC_FIXTURE = """
%token<lexer.keyword_none> SELECT_SYM
%token<lexer.keyword> ACCOUNT_SYM
%left<lexer.keyword> BKA_HINT
%%
query:
  SELECT_SYM ACCOUNT_SYM
  ;
%%
"""

MYSQL_8011_YACC_FIXTURE = """
%token<keyword> ACCOUNT_SYM
%%
query:
  SELECT_SYM ACCOUNT_SYM
  ;
%%
"""

MYSQL_57_YACC_FIXTURE = """
%token SELECT_SYM
%token ACCOUNT_SYM
%token BKA_HINT
%%
keyword:
  ACCOUNT_SYM
  ;

keyword_sp:
  BKA_HINT
  ;
%%
"""

MARIADB_LEX_FIXTURE = """
/* MariaDB sql/lex.h snippet */
static SYMBOL symbols[] = {
  { "!=",              SYM(NE)},
  { "SELECT",          SYM(SELECT_SYM)},
  { "SEQUENCE",        SYM(SEQUENCE_SYM)},
  { "VECTOR",          SYM(VECTOR_SYM)},
  { "PARSE_GCOL_EXPR", SYM(PARSE_GCOL_EXPR_SYM)},
  // { "COMMENTED",    SYM(COMMENTED_SYM)},
};

static SYMBOL sql_functions[] = {
  { "->",              SYM(JSON_EXTRACT_SYM)},
  { "COUNT",           SYM(COUNT_SYM)},
  { "ADDDATE",         SYM(ADDDATE_SYM)},
};
"""

MARIADB_YACC_FIXTURE = """
%token SELECT_SYM
%token SEQUENCE_SYM
%token VECTOR_SYM
%%
keyword_ident:
    keyword_sp_var_and_label
  | keyword_data_type
  ;

keyword_sp_var_and_label:
    SEQUENCE_SYM
  ;

%ifdef MARIADB
keyword_data_type:
    VECTOR_SYM
  ;
%else
keyword_data_type:
    SELECT_SYM
  ;
%endif
%%
"""


class TestMySQLParser(unittest.TestCase):
    """Parser tests on small MySQL lex.h and sql_yacc.yy fixtures."""

    def test_mysql_lex_filters_hints_functions_operators_and_internal_tokens(self):
        kw_tokens, grammar_functions = gen.parse_mysql_lex(MYSQL_LEX_FIXTURE)
        # SYM and SYM_HK are kept as keywords; SYM_H (MAX_EXECUTION_TIME),
        # SYM_FN (COUNT, CAST), operators (&&, <=>), comments, and internal
        # tokens (PARSE_GCOL_EXPR, ABORT_SYM, GRAMMAR_SELECTOR_*) are excluded.
        self.assertEqual(
            kw_tokens,
            {
                "ACCOUNT": "ACCOUNT_SYM",
                "BKA": "BKA_HINT",
                "SELECT": "SELECT_SYM",
            },
        )
        self.assertEqual(grammar_functions, ["CAST", "COUNT"])
        self.assertNotIn("PARSE_GCOL_EXPR", kw_tokens)
        self.assertNotIn("MAX_EXECUTION_TIME", kw_tokens)

    def test_mysql_80_lexer_keyword_tags_distinguish_reserved_from_nonreserved(self):
        keywords, grammar_functions = gen.parse_mysql_words(
            MYSQL_LEX_FIXTURE, MYSQL_80_YACC_FIXTURE
        )
        self.assertEqual(
            keywords,
            [
                {"name": "ACCOUNT", "reserved": False},
                {"name": "BKA", "reserved": False},
                {"name": "SELECT", "reserved": True},
            ],
        )
        self.assertEqual(grammar_functions, ["CAST", "COUNT"])

    def test_mysql_8011_keyword_tag_and_57_grammar_rules(self):
        keywords_8011, _ = gen.parse_mysql_words(
            MYSQL_LEX_FIXTURE, MYSQL_8011_YACC_FIXTURE
        )
        self.assertEqual(
            keywords_8011,
            [
                {"name": "ACCOUNT", "reserved": False},
                {"name": "BKA", "reserved": True},
                {"name": "SELECT", "reserved": True},
            ],
        )

        keywords_57, _ = gen.parse_mysql_words(
            MYSQL_LEX_FIXTURE, MYSQL_57_YACC_FIXTURE
        )
        self.assertEqual(
            keywords_57,
            [
                {"name": "ACCOUNT", "reserved": False},
                {"name": "BKA", "reserved": False},
                {"name": "SELECT", "reserved": True},
            ],
        )


class TestMariaDBParser(unittest.TestCase):
    """Parser tests on small MariaDB lex.h and sql_yacc.yy fixtures."""

    def test_mariadb_lex_separates_symbols_and_sql_functions(self):
        kw_tokens, grammar_functions = gen.parse_mariadb_lex(MARIADB_LEX_FIXTURE)
        self.assertEqual(
            kw_tokens,
            {
                "SELECT": "SELECT_SYM",
                "SEQUENCE": "SEQUENCE_SYM",
                "VECTOR": "VECTOR_SYM",
            },
        )
        self.assertEqual(grammar_functions, ["ADDDATE", "COUNT"])
        self.assertNotIn("PARSE_GCOL_EXPR", kw_tokens)

    def test_mariadb_keyword_rules_and_ifdef_preprocessing(self):
        keywords, grammar_functions = gen.parse_mariadb_words(
            MARIADB_LEX_FIXTURE, MARIADB_YACC_FIXTURE
        )
        self.assertEqual(
            keywords,
            [
                {"name": "SELECT", "reserved": True},
                {"name": "SEQUENCE", "reserved": False},
                {"name": "VECTOR", "reserved": False},
            ],
        )
        self.assertEqual(grammar_functions, ["ADDDATE", "COUNT"])


class TestNativeFunctionBounds(unittest.TestCase):
    """Source snippets exercise each supported arity form without engine checkouts."""

    def test_mysql_macros_factories_aliases_and_unknowns(self):
        source = """
class Optional_instantiator {
  static constexpr int Min_argcount = 1;
  static constexpr int Max_argcount = 3;
};
class Variadic_instantiator {
  static constexpr int Min_argcount = 0;
  static constexpr auto Max_argcount = MAX_ARGLIST_SIZE;
};
class Opaque_instantiator {
  static constexpr int Min_argcount = COMPUTED_MIN;
  static constexpr int Max_argcount = 4;
};
using Alias = Optional_instantiator;
using Second_alias = Alias;
using Spatial_alias = Instantiator_with_functype<Item_spatial, 2>;
using Cycle_a = Cycle_b;
using Cycle_b = Cycle_a;
static const auto func_array[] = {
  {"FIXED", SQL_FN(Item_fixed, 2)},
  {"FIXED_ALIAS", SQL_FN(Item_fixed, 2)},
  {"LIST", SQL_FN_LIST(Item_list, 0)},
  {"VAR", SQL_FN_V(Item_var, 1, MAX_ARGLIST_SIZE)},
  {"VAR_LIST", SQL_FN_V_LIST(Item_list, 2, 4)},
  {"VAR_THD", SQL_FN_V_THD(Item_thd, 3, 5)},
  {"VAR_LIST_THD", SQL_FN_V_LIST_THD(Item_list_thd, 0, MAX_ARGLIST_SIZE)},
  {"ODD", SQL_FN_ODD(Item_odd, 3, MAX_ARGLIST_SIZE)},
  {"EVEN", SQL_FN_EVEN(Item_even, 0, MAX_ARGLIST_SIZE)},
  {"FACTORY", SQL_FACTORY(Optional_instantiator)},
  {"FACTORY_ALIAS", SQL_FACTORY(Second_alias)},
  {"FACTORY_VARIADIC", SQL_FACTORY(Variadic_instantiator)},
  {"SPATIAL", SQL_FACTORY(Spatial_alias)},
  {"OPAQUE", SQL_FACTORY(Opaque_instantiator)},
  {"CYCLE", SQL_FACTORY(Cycle_a)},
  {"MISSING", SQL_FACTORY(Missing_instantiator)},
  {"EXPRESSION", SQL_FN(Item_fixed, 1 + 1)},
  {"INTERNAL_HIDDEN", SQL_FN(Item_fixed, 2)},
  {"HIDDEN_BY_MACRO", SQL_FN_LIST_INTERNAL_V(Item_internal, 1, 3)},
  // {"COMMENTED", SQL_FN(Item_fixed, 9)},
};
"""
        self.assertEqual(gen.parse_mysql_functions(source), {
            "FIXED": (2, 2), "FIXED_ALIAS": (2, 2), "LIST": (0, 0),
            "VAR": (1, "unbounded"), "VAR_LIST": (2, 4),
            "VAR_THD": (3, 5), "VAR_LIST_THD": (0, "unbounded"),
            "ODD": (3, "unbounded"), "EVEN": (0, "unbounded"),
            "FACTORY": (1, 3), "FACTORY_ALIAS": (1, 3),
            "FACTORY_VARIADIC": (0, "unbounded"), "SPATIAL": (2, 2),
            "OPAQUE": (None, None), "CYCLE": (None, None),
            "MISSING": (None, None), "EXPRESSION": (None, None),
        })

    def test_legacy_fixed_inheritance_geometry_aliases_and_oracle_exclusion(self):
        source = """
class Create_pair : public Create_func_arg2 {};
class Create_pair_alias : public Create_pair {};
class Create_oracle : public Create_func_arg1 {};
const Native_func_registry func_array[] = {
  {{STRING_WITH_LEN("PAIR")}, BUILDER(Create_pair)},
  {{STRING_WITH_LEN("PAIR_ALIAS")}, BUILDER(Create_pair_alias)},
  {{STRING_WITH_LEN("INTERNAL_PAIR")}, BUILDER(Create_pair)},
  /* {{STRING_WITH_LEN("COMMENTED")}, BUILDER(Create_pair)}, */
};
const Native_func_registry geometry_array[] = {
  {{C_STRING_WITH_LEN("ST_PAIR")}, GEOM_BUILDER(Create_pair)},
};
const Native_func_registry oracle_func_array[] = {
  {{STRING_WITH_LEN("PAIR")}, BUILDER(Create_oracle)},
};
"""
        self.assertEqual(gen.parse_legacy_functions(source), {
            "PAIR": (2, 2), "PAIR_ALIAS": (2, 2), "ST_PAIR": (2, 2),
        })

    def test_legacy_native_guards_switches_and_conservative_unknowns(self):
        cases = [
            ("exact", "if (arg_count != 2) { ERROR; return NULL; }", (2, 2)),
            ("interval", "if (unlikely(arg_count < 2 || arg_count > 4)) { ERROR; return NULL; }", (2, 4)),
            ("separate_guards", "if (arg_count < 2) { ERROR; return NULL; } if (arg_count > 3) { ERROR; return NULL; }", (2, 3)),
            ("bare_guard", "if (arg_count < 2) ERROR;", (2, "unbounded")),
            ("accepted_branch", "if (arg_count == 2 || arg_count == 3) { return new Item(); } else { ERROR; return NULL; }", (2, 3)),
            ("conditional_default", "switch (arg_count) { case 2: return new Item(); default: if (oracle_mode) { ERROR; return NULL; } return new Item(); }", (None, None)),
            ("exclusive", "if (arg_count <= 1 || arg_count >= 5) { ERROR; return NULL; }", (2, 4)),
            ("variadic", "if (arg_count < 1) { ERROR; return NULL; }", (1, "unbounded")),
            ("switch", "switch (arg_count) { case 1: return new Item(); case 2: return new Item(); default: ERROR; return NULL; }", (1, 2)),
            ("guarded_dispatch", "if (arg_count < 1 || arg_count > 2) { ERROR; return NULL; } switch (arg_count) { case 1: func = new Item(); break; case 2: func = new Item(); break; } return func;", (1, 2)),
            ("unguarded_dispatch", "switch (arg_count) { case 1: return new Item(); case 2: return new Item(); }", (None, None)),
            ("incomplete_dispatch", "if (arg_count < 1 || arg_count > 3) { ERROR; return NULL; } switch (arg_count) { case 1: return new Item(); case 3: return new Item(); }", (None, None)),
            ("switch_tail", "switch (arg_count) { case 1: return new Item(); case 2: return new Item(); } ERROR; return NULL;", (1, 2)),
            ("gap", "switch (arg_count) { case 1: return new Item(); case 3: return new Item(); default: ERROR; return NULL; }", (None, None)),
            ("nested", "if (oracle_mode) { if (arg_count != 2) { ERROR; return NULL; } }", (None, None)),
            ("conditional_error", "if (arg_count != 2) { if (oracle_mode) { ERROR; return NULL; } }", (None, None)),
            ("parity", "if (arg_count < 2 || arg_count % 2 != 0) { ERROR; return NULL; }", (None, None)),
            ("symbolic", "if (arg_count > MAX_ARGS) { ERROR; return NULL; }", (None, None)),
            ("delegated", "return make_item(item_list);", (None, None)),
        ]
        for name, body, expected in cases:
            with self.subTest(name=name):
                # Real builders use this error to reject an invalid count.
                body = body.replace("ERROR", "my_error(ER_WRONG_PARAMCOUNT_TO_NATIVE_FCT, MYF(0), name)")
                source = """
class Create_test : public Create_native_func {};
Item *Create_test::create_native(THD *thd, List<Item> *item_list) {
%s
}
const Native_func_registry func_array[] = {
  {{STRING_WITH_LEN("TEST")}, BUILDER(Create_test)},
};
""" % body
                self.assertEqual(gen.parse_legacy_functions(source), {"TEST": expected})


class TestReleaseTags(unittest.TestCase):
    """Release-to-tag and major.minor series selection from VERSIONS tuples."""

    def test_release_tags_from_versions_tuples(self):
        sample_versions = (
            ("5.7.44", "mysql-5.7.44", "GA", "2023-10-18", 0, "image"),
            ("8.0.11", "mysql-8.0.11", "GA", "2018-04-08", 1, "image"),
            ("10.11.6", "mariadb-10.11.6", "stable", "2023-11-15", 0, "image"),
        )
        self.assertEqual(
            gen.release_tags(sample_versions),
            [
                ("5.7.44", "mysql-5.7.44", "5.7"),
                ("8.0.11", "mysql-8.0.11", "8.0"),
                ("10.11.6", "mariadb-10.11.6", "10.11"),
            ],
        )
        with self.assertRaises(ValueError):
            gen.series_of_version("invalid")

    def test_cataloged_mysql_and_mariadb_versions_have_valid_tags_and_series(self):
        mysql_tags = gen.release_tags(gen.MYSQL_VERSIONS)
        mariadb_tags = gen.release_tags(gen.MARIADB_VERSIONS)
        self.assertEqual(len(mysql_tags), 102)
        self.assertEqual(len(mariadb_tags), 113)
        self.assertEqual(mysql_tags[0], ("5.7.9", "mysql-5.7.9", "5.7"))
        self.assertEqual(mysql_tags[-1], ("26.10.0", "mysql-26.10.0", "26.10"))
        self.assertEqual(mariadb_tags[0], ("10.6.3", "mariadb-10.6.3", "10.6"))
        self.assertEqual(mariadb_tags[-1], ("13.0.2", "mariadb-13.0.2", "13.0"))


class TestDeltaEncodingRoundTrip(unittest.TestCase):
    """encode_series followed by decode_series reproduces the original releases."""

    def test_encode_then_decode_round_trip(self):
        releases = [
            {
                "release": "8.0.11",
                "tag": "mysql-8.0.11",
                "keywords": [
                    {"name": "ACCESSIBLE", "reserved": True},
                    {"name": "ADMIN", "reserved": True},
                    {"name": "ARRAY", "reserved": False},
                    {"name": "REMOTE", "reserved": False},
                    {"name": "SELECT", "reserved": True},
                ],
                "grammarFunctions": ["CAST", "COUNT"],
                "functions": {},
            },
            {
                "release": "8.0.14",
                "tag": "mysql-8.0.14",
                "keywords": [
                    {"name": "ACCESSIBLE", "reserved": True},
                    {"name": "ACTIVE", "reserved": False},
                    {"name": "ADMIN", "reserved": False},
                    {"name": "ARRAY", "reserved": True},
                    {"name": "LATERAL", "reserved": True},
                    {"name": "SELECT", "reserved": True},
                ],
                "grammarFunctions": ["CAST", "COUNT", "ST_COLLECT"],
                "functions": {},
            },
            {
                "release": "8.0.15",
                "tag": "mysql-8.0.15",
                "keywords": [
                    {"name": "ACCESSIBLE", "reserved": True},
                    {"name": "ACTIVE", "reserved": False},
                    {"name": "ADMIN", "reserved": False},
                    {"name": "ARRAY", "reserved": False},
                    {"name": "LATERAL", "reserved": True},
                    {"name": "SELECT", "reserved": True},
                ],
                "grammarFunctions": ["COUNT"],
                "functions": {},
            },
        ]
        encoded = gen.encode_series(releases)
        self.assertEqual(encoded["baseRelease"], "8.0.11")
        self.assertEqual(encoded["baseReserved"], ["ACCESSIBLE", "ADMIN", "SELECT"])
        self.assertEqual(encoded["baseNonReserved"], ["ARRAY", "REMOTE"])
        self.assertEqual(encoded["baseGrammarFunctions"], ["CAST", "COUNT"])

        decoded = gen.decode_series(encoded)
        self.assertEqual(decoded, releases)

    def test_function_deltas_preserve_changes_removals_and_unknown_bounds(self):
        releases = [
            {"release": "8.0.11", "tag": "mysql-8.0.11", "keywords": [],
             "grammarFunctions": ["COUNT"],
             "functions": {"CHANGED": (1, 1), "REMOVED": (2, 2), "UNKNOWN": (None, None)}},
            {"release": "8.0.12", "tag": "mysql-8.0.12", "keywords": [],
             "grammarFunctions": ["COUNT"],
             "functions": {"ADDED": (0, "unbounded"), "CHANGED": (1, 3), "UNKNOWN": (None, None)}},
            {"release": "8.0.13", "tag": "mysql-8.0.13", "keywords": [],
             "grammarFunctions": ["COUNT"],
             "functions": {"CHANGED": (None, None), "UNKNOWN": (2, 2)}},
        ]
        encoded = gen.encode_series(releases)
        self.assertEqual(encoded["releases"][1]["functions"], {
            "ADDED": (0, "unbounded"), "CHANGED": (1, 3),
        })
        self.assertEqual(encoded["releases"][1]["functionsRemoved"], ["REMOVED"])
        self.assertEqual(encoded["releases"][2]["functionsRemoved"], ["ADDED"])
        self.assertEqual(gen.decode_series(encoded), releases)
        # Earlier snapshots must not inherit a later release's mutations.
        self.assertEqual(gen.decode_series(encoded)[0]["functions"]["CHANGED"], (1, 1))

    def test_encode_empty_series_raises(self):
        with self.assertRaises(ValueError):
            gen.encode_series([])


class TestRenderDeterminism(unittest.TestCase):
    """Two renders of the same logical series data produce identical output."""

    def test_two_renders_are_identical(self):
        series_a = gen.encode_series(
            [
                {
                    "release": "8.0.11",
                    "tag": "mysql-8.0.11",
                    "keywords": [
                        {"name": "SELECT", "reserved": True},
                        {"name": "ACCOUNT", "reserved": False},
                    ],
                    "grammarFunctions": ["COUNT", "CAST"],
                    "functions": {},
                },
                {
                    "release": "8.0.14",
                    "tag": "mysql-8.0.14",
                    "keywords": [
                        {"name": "ACCOUNT", "reserved": False},
                        {"name": "LATERAL", "reserved": True},
                        {"name": "SELECT", "reserved": True},
                    ],
                    "grammarFunctions": ["CAST", "COUNT"],
                    "functions": {},
                },
            ]
        )
        series_b = gen.encode_series(
            [
                {
                    "release": "5.7.9",
                    "tag": "mysql-5.7.9",
                    "keywords": [{"name": "SELECT", "reserved": True}],
                    "grammarFunctions": ["COUNT"],
                    "functions": {},
                }
            ]
        )
        maria_106 = gen.encode_series(
            [
                {
                    "release": "10.6.3",
                    "tag": "mariadb-10.6.3",
                    "keywords": [
                        {"name": "SELECT", "reserved": True},
                        {"name": "SEQUENCE", "reserved": False},
                    ],
                    "grammarFunctions": ["COUNT"],
                    "functions": {},
                }
            ]
        )

        first = gen.render_generated_ts(
            {"8.0": series_a, "5.7": series_b},
            {"10.6": maria_106},
        )
        second = gen.render_generated_ts(
            {"5.7": series_b, "8.0": series_a},
            {"10.6": maria_106},
        )
        self.assertEqual(first, second)


class TestCheckExitCodes(unittest.TestCase):
    """--check exit codes: 0 when current, 1 when stale or missing."""

    def _fake_catalog(self, engine):
        if engine == "mysql":
            return {
                "8.0": gen.encode_series(
                    [
                        {
                            "release": "8.0.11",
                            "tag": "mysql-8.0.11",
                            "keywords": [{"name": "SELECT", "reserved": True}],
                            "grammarFunctions": ["COUNT"],
                            "functions": {},
                        }
                    ]
                )
            }
        return {
            "10.6": gen.encode_series(
                [
                    {
                        "release": "10.6.3",
                        "tag": "mariadb-10.6.3",
                        "keywords": [{"name": "SELECT", "reserved": True}],
                        "grammarFunctions": ["COUNT"],
                        "functions": {},
                    }
                ]
            )
        }

    def test_check_exit_codes_for_missing_current_and_stale_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mysql_dir = tmp_path / "mysql-server"
            mariadb_dir = tmp_path / "mariadb-server"
            mysql_dir.mkdir()
            mariadb_dir.mkdir()
            out_file = tmp_path / "engineWords.generated.ts"

            def side_effect(_repo, _versions, engine):
                return self._fake_catalog(engine)

            with mock.patch.object(
                gen, "collect_engine_catalog", side_effect=side_effect
            ):
                # 1. Missing output file -> exit 1
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = gen.main(
                        [
                            "--mysql-source",
                            str(mysql_dir),
                            "--mariadb-source",
                            str(mariadb_dir),
                            "--out",
                            str(out_file),
                            "--check",
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertIn("Missing generated file", err.getvalue())

                # 2. Generate output file -> exit 0, then --check -> exit 0
                code = gen.main(
                    [
                        "--mysql-source",
                        str(mysql_dir),
                        "--mariadb-source",
                        str(mariadb_dir),
                        "--out",
                        str(out_file),
                    ]
                )
                self.assertEqual(code, 0)

                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = gen.main(
                        [
                            "--mysql-source",
                            str(mysql_dir),
                            "--mariadb-source",
                            str(mariadb_dir),
                            "--out",
                            str(out_file),
                            "--check",
                        ]
                    )
                self.assertEqual(code, 0, err.getvalue())

                # 3. Stale output file -> exit 1
                out_file.write_text("// stale\n", encoding="utf-8")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = gen.main(
                        [
                            "--mysql-source",
                            str(mysql_dir),
                            "--mariadb-source",
                            str(mariadb_dir),
                            "--out",
                            str(out_file),
                            "--check",
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertIn("out of date", err.getvalue())

    def test_live_check_when_engine_checkouts_present(self):
        mysql_repo = Path(
            gen.os.environ.get(
                "MYSQL_SOURCE", "/Users/viniciusgrippa/Downloads/git/mysql-server"
            )
        )
        mariadb_repo = Path(
            gen.os.environ.get(
                "MARIADB_SOURCE", "/Users/viniciusgrippa/Downloads/git/mariadb-server"
            )
        )
        if not mysql_repo.is_dir() or not mariadb_repo.is_dir():
            self.skipTest("Engine git checkouts are not present on this machine.")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = gen.main(
                [
                    "--mysql-source",
                    str(mysql_repo),
                    "--mariadb-source",
                    str(mariadb_repo),
                    "--check",
                ]
            )
        self.assertEqual(code, 0, err.getvalue())


if __name__ == "__main__":
    unittest.main()
