"""Predict how an ALTER TABLE runs on InnoDB: INSTANT, INPLACE or COPY.

``predict(alter_sql, create_table_sql, engine, version, facts=None)`` reads
the statement and the table's current SHOW CREATE TABLE and returns what the
server will most likely do, without running anything. It mirrors the
server's own decision, read from the source, never from documentation or by
trying the statement:

1. sql/sql_table.cc ``fill_alter_inplace_info`` compares the old and the new
   table and sets one handler flag per kind of change (a column type, its
   NULL attribute, its position, an index, a table option ...);
2. sql/sql_table.cc ``mysql_alter_table`` forces COPY for a few statements
   (ORDER BY, another ENGINE, an enforced CHECK, ...);
3. InnoDB's ``ha_innobase::check_if_supported_inplace_alter`` in
   storage/innobase/handler/handler0alter.cc maps the flags to INSTANT,
   INPLACE with or without a rebuild, or "not supported" (COPY), and says
   whether concurrent writes may continue (LOCK=NONE).

The model below follows those three steps per engine. Every rule carries its
source location: mysql-server 9.7 (trunk, ``git describe`` mysql-9.7.0) and
mariadb-server 13.0 (trunk, mariadb-12.3.1-167). The same functions were
compared with mysql-8.4.0 and mariadb-10.11.16 / 11.4.10; the differences
that matter are version-gated here (see ``_MYSQL_GATES``/``_MARIADB_GATES``).
Live checks against MySQL 8.4 and MariaDB 11.4 are recorded in
test/fixtures/alter_algorithm/ (scripts/record-alter-algorithm-fixtures.py).

Anything outside the verified rules yields a clause with ``algorithm: None``
and a reason, and the statement ``predicted: False``: partitioning,
system-versioned tables, other engines, unknown syntax, and conditions the
definition cannot show (MySQL's row-version budget, a hidden FTS_DOC_ID
column left by an old FULLTEXT index, another table's foreign keys) when the
caller could not supply them in ``facts``.

Stdlib only, Python 3.7.
"""
import copy
import re

from .server_info import server_version_tuple

__all__ = ["predict", "outcome_text", "FACT_NAMES"]

# ---------------------------------------------------------------------------
# Results


INSTANT, INPLACE, COPY = "INSTANT", "INPLACE", "COPY"

#: Facts a caller may pass. Server settings fall back to the documented
#: default (and say so in ``notes``); table facts have no default.
FACT_NAMES = (
    "foreign_key_checks",  # bool, session value
    "sql_mode",  # str, session value
    "innodb_page_size",  # int
    "innodb_strict_mode",  # bool (MariaDB instant row-size check)
    "old_alter_table",  # bool (MySQL)
    "alter_algorithm",  # str (MariaDB before 11.5)
    "old_mode",  # str (MariaDB)
    "innodb_instant_alter_column_allowed",  # str (MariaDB)
    "total_row_versions",  # int, information_schema.INNODB_TABLES (MySQL)
    "fts_doc_id_index",  # bool, the table has InnoDB's FTS_DOC_ID_INDEX
    "system_tablespace",  # bool, the table lives in the system tablespace (MariaDB)
    "innodb_file_per_table",  # bool (MariaDB)
    "default_collations",  # {charset: collation}, information_schema.CHARACTER_SETS (MariaDB)
    "referenced_columns",  # [str], this table's columns other tables' foreign keys reference
)


class Unpredictable(Exception):
    """A statement or definition outside the verified rules."""


def outcome_text(algorithm, rebuild, concurrent_dml, online_copy=False):
    """One plain sentence for a predicted outcome."""
    if algorithm == INSTANT:
        return "Changes only table metadata; no rows are copied."
    if algorithm == INPLACE:
        if rebuild:
            head = "Rebuilds the table in place"
        else:
            head = "Changes the table in place without rebuilding it; rows are not copied"
        return head + ("; concurrent writes continue." if concurrent_dml
                       else "; writes wait until it finishes (reads continue).")
    if algorithm == COPY:
        if concurrent_dml:
            return ("Copies the table; concurrent writes continue and are applied before the copy "
                    "replaces the table." if online_copy else "Copies the table; concurrent writes continue.")
        return "Copies the table; writes wait until it finishes (reads continue)."
    return "Not predicted."


def _summary(algorithm, rebuild):
    if algorithm == INSTANT:
        return "INSTANT, metadata only"
    if algorithm == INPLACE:
        return "INPLACE, rebuilds the table" if rebuild else "INPLACE, no rebuild"
    if algorithm == COPY:
        return "COPY, copies the table"
    return None


_RANK = {INSTANT: 0, INPLACE: 1, COPY: 3}


def _rank(outcome):
    if outcome.algorithm is None:
        return 9
    base = _RANK[outcome.algorithm] + (1 if outcome.algorithm == INPLACE and outcome.rebuild else 0)
    return base * 2 + (0 if outcome.concurrent_dml else 1)


class _Outcome(object):
    __slots__ = ("algorithm", "rebuild", "concurrent_dml", "rule", "details", "online_copy", "needs", "copy_why")

    def __init__(self, algorithm, rebuild, concurrent_dml, rule, details=None, online_copy=False, needs=None):
        self.algorithm = algorithm
        self.rebuild = rebuild
        self.concurrent_dml = concurrent_dml
        self.rule = rule
        self.details = list(details or [])
        self.online_copy = online_copy
        self.needs = list(needs or [])
        self.copy_why = None


def _unknown(rule, details=None, needs=None):
    return _Outcome(None, None, None, rule, details, needs=needs)


# ---------------------------------------------------------------------------
# Source evidence. M = mysql-server 9.7 trunk, Ma = mariadb-server 13.0 trunk.

_M_H = "mysql-server storage/innobase/handler/handler0alter.cc"
_M_T = "mysql-server sql/sql_table.cc"
_MA_H = "mariadb-server storage/innobase/handler/handler0alter.cc"
_MA_T = "mariadb-server sql/sql_table.cc"
_MA_I = "mariadb-server storage/innobase/handler/ha_innodb.cc"

#: rule id -> (reason, source). The reason is one sentence about why; the
#: outcome sentence (outcome_text) is added separately.
RULES = {
    # -- MySQL ------------------------------------------------------------------------
    "mysql.no_change": (
        "Nothing InnoDB stores changes; the server only rewrites the table definition.",
        _M_T + ":18423 (handler_flags == 0 is a no-op); " + _M_H + ":145-155 INNOBASE_INPLACE_IGNORE, "
        ":829 innobase_support_instant (INSTANT_NO_CHANGE)"),
    "mysql.rename_table": (
        "Renaming the table alone only renames it in the data dictionary.",
        _M_T + ":16759 is_simple_rename_or_index_change, :17876-17884"),
    "mysql.instant_rename_column": (
        "Renaming a column is a data dictionary change InnoDB does instantly.",
        _M_H + ":157-165 INNOBASE_INSTANT_ALLOWED, :829 innobase_support_instant (COLUMN_RENAME_ONLY)"),
    "mysql.rename_referenced_column": (
        "Another table's foreign key references this column, so InnoDB renames it in place instead "
        "of instantly.",
        _M_H + ":784-817 ok_to_rename_column (ER_ALTER_OPERATION_NOT_SUPPORTED_REASON_FK_RENAME)"),
    "mysql.instant_virtual": (
        "Adding or dropping a virtual column changes only metadata.",
        _M_H + ":829 innobase_support_instant (VIRTUAL_ADD_DROP_ONLY), :604 check_v_col_in_order"),
    "mysql.instant_add_drop": (
        "Adding or dropping a stored column is instant: existing rows keep their row version and are "
        "read with the new definition.",
        _M_H + ":829 innobase_support_instant (INSTANT_ADD/INSTANT_DROP), :1043-1105 "
        "check_if_supported_inplace_alter; storage/innobase/include/dict0dict.ic:1248 "
        "support_instant_add_drop. MySQL 8.0.29 added DROP and any position (INNOBASE_INSTANT_ALLOWED "
        "at mysql-8.0.28 vs mysql-8.0.29)"),
    "mysql.no_instant_compressed": (
        "ROW_FORMAT=COMPRESSED tables cannot add or drop columns instantly, so the table is rebuilt.",
        "mysql-server storage/innobase/include/dict0dict.ic:1248 support_instant_add_drop "
        "(DICT_TF_GET_ZIP_SSIZE)"),
    "mysql.no_instant_fulltext": (
        "A table with a FULLTEXT index (or the hidden FTS_DOC_ID column one leaves) cannot add or drop "
        "columns instantly.",
        "mysql-server storage/innobase/include/dict0dict.ic:1248 support_instant_add_drop "
        "(DICT_TF2_FTS, DICT_TF2_FTS_HAS_DOC_ID)"),
    "mysql.instant_requested_inplace": (
        "ALGORITHM=INPLACE asks for the in-place path, which rebuilds the table for added or dropped "
        "columns.",
        _M_H + ":1053-1056 (\"Still fall back to INPLACE\")"),
    "mysql.row_versions_exhausted": (
        "The table has used all its instant row versions, so the server rebuilds it.",
        _M_H + ":1073-1083 is_valid_row_version; MAX_ROW_VERSION is 64 through mysql-9.0.1 "
        "(storage/innobase/include/dict0mem.h:323 at mysql-8.4.0) and 255 from mysql-9.1.0 "
        "(storage/innobase/include/rem0types.h:46)"),
    "mysql.copy_column_type": (
        "The column's stored format changes, which InnoDB cannot do in place.",
        _M_T + ":13104-13160 fill_alter_inplace_info (ALTER_STORED_COLUMN_TYPE); " + _M_H +
        ":1006-1021 check_if_supported_inplace_alter"),
    "mysql.copy_generated": (
        "A stored generated column or a column with an expression DEFAULT is computed by the server "
        "for every row, which needs a copy.",
        _M_T + ":13270-13281 (ADD_STORED_GENERATED_COLUMN); " + _M_H + ":1006-1021"),
    "mysql.copy_flags": (
        "This change is not one InnoDB can do in place.",
        _M_H + ":1006-1021 check_if_supported_inplace_alter"),
    "mysql.copy_fk_checks": (
        "Adding a foreign key in place needs foreign_key_checks=0; with checks on, the server copies "
        "the table to validate every row.",
        _M_H + ":1023-1030 (ER_ALTER_OPERATION_NOT_SUPPORTED_REASON_FK_CHECK)"),
    "mysql.copy_not_null_nonstrict": (
        "NULL to NOT NULL needs a strict sql_mode to run in place.",
        _M_H + ":1110-1120"),
    "mysql.copy_drop_pk": (
        "Dropping the primary key without adding another one needs a copy.",
        _M_H + ":1122-1130 (ER_ALTER_OPERATION_NOT_SUPPORTED_REASON_NOPK)"),
    "mysql.copy_nullable_clustered": (
        "Making a column of the clustering unique key nullable leaves InnoDB without that clustered "
        "index, so the table is copied.",
        _M_H + ":1132-1149 (ER_PRIMARY_CANT_HAVE_NULL)"),
    "mysql.copy_virtual_mix": (
        "Adding or dropping a virtual column in place cannot be combined with these other changes.",
        _M_H + ":1151-1186 (ER_UNSUPPORTED_ALTER_INPLACE_ON_VIRTUAL_COLUMN)"),
    "mysql.copy_fulltext_rebuild": (
        "The table keeps a FULLTEXT index, and InnoDB refuses to rebuild such a table in place.",
        _M_H + ":1326-1347 (ER_INNODB_FT_LIMIT)"),
    "mysql.copy_server": (
        "The server copies the table for this statement before asking InnoDB.",
        _M_T + ":13855 is_inplace_alter_impossible, :17998-18020"),
    "mysql.copy_check": (
        "An enforced CHECK constraint is evaluated by the server for every row, which needs a copy.",
        _M_T + ":20736 is_any_check_constraints_evaluation_required, :13855 is_inplace_alter_impossible"),
    "mysql.copy_old_alter_table": (
        "old_alter_table=ON makes the server copy the table unless ALGORITHM asks otherwise.",
        _M_T + ":17998-18020"),
    "mysql.copy_requested": (
        "ALGORITHM=COPY asks for a copy.",
        _M_T + ":18375 (requested_algorithm == COPY)"),
    "mysql.inplace": (
        "InnoDB changes this in place without rebuilding the table.",
        _M_H + ":171-180 INNOBASE_ALTER_NOREBUILD, :930 innobase_need_rebuild"),
    "mysql.inplace_rebuild": (
        "InnoDB does this in place, but must rebuild the table (a new clustered index) to do it.",
        _M_H + ":128-138 INNOBASE_ALTER_REBUILD, :930 innobase_need_rebuild"),
    "mysql.inplace_options": (
        "A table option alone changes only metadata in place; combined with other changes, or for "
        "ROW_FORMAT and KEY_BLOCK_SIZE, it rebuilds the table.",
        _M_H + ":930-950 innobase_need_rebuild (CHANGE_CREATE_OPTION)"),
    "mysql.inplace_fulltext": (
        "Adding a FULLTEXT index needs a shared lock; the first one also adds the hidden FTS_DOC_ID "
        "column, which rebuilds the table.",
        _M_H + ":1349-1366 (FULLTEXT requires a lock), :5965-5980 (\"InnoDB rebuilding table to add "
        "column FTS_DOC_ID\")"),
    "mysql.lock_spatial": (
        "Building a SPATIAL index, or rebuilding a table that has one, needs a shared lock.",
        _M_H + ":1281-1284, :1326-1347"),
    "mysql.lock_autoinc": (
        "A new AUTO_INCREMENT column cannot get its values while other sessions write.",
        _M_H + ":1249-1256 (ER_ALTER_OPERATION_NOT_SUPPORTED_REASON_AUTOINC)"),
    "mysql.lock_virtual_index": (
        "Indexing a newly added virtual column needs a shared lock.",
        _M_H + ":1258-1272 (ER_UNSUPPORTED_ALTER_ONLINE_ON_VIRTUAL_COLUMN)"),
    # -- MariaDB ----------------------------------------------------------------------
    "mariadb.instant": (
        "InnoDB does this instantly, changing only metadata.",
        _MA_H + ":148-157 INNOBASE_ALTER_INSTANT, :2339-2353, :2724-2727 check_if_supported_inplace_alter"),
    "mariadb.rename_table": (
        "Renaming the table alone only renames it.",
        _MA_T + ":11330-11355 (simple_rename_or_index_change)"),
    "mariadb.instant_columns": (
        "Adding, dropping or reordering columns is instant (innodb_instant_alter_column_allowed="
        "add_drop_reorder); old rows are read with the new definition.",
        _MA_H + ":1693 instant_alter_column_possible, :2683-2727 check_if_supported_inplace_alter; "
        "instant ADD since MariaDB 10.3, DROP and reorder since 10.4"),
    "mariadb.instant_type_change": (
        "InnoDB can change this column type without touching rows (a longer VARCHAR, or utf8mb3 to "
        "utf8mb4 data that needs no conversion).",
        _MA_T + ":7155-7170 fill_alter_inplace_info (ALTER_COLUMN_TYPE_CHANGE_BY_ENGINE); " + _MA_I +
        ":20837-20906 supports_enlarging, can_convert_varstring"),
    "mariadb.instant_options": (
        "A table option that needs no rebuild (COMMENT, AUTO_INCREMENT, DEFAULT CHARSET, ...) "
        "changes only metadata.",
        _MA_H + ":1541 alter_options_need_rebuild, :2339-2353"),
    "mariadb.no_instant_fulltext": (
        "A table with a FULLTEXT index (or the hidden FTS_DOC_ID column one leaves) cannot add, drop "
        "or reorder columns instantly (MDEV-17459).",
        _MA_H + ":1705-1710 instant_alter_column_possible"),
    "mariadb.no_instant_indexed_virtual": (
        "A table with an index on a virtual column cannot add, drop or reorder columns instantly "
        "(MDEV-17468).",
        _MA_H + ":1711-1721 instant_alter_column_possible"),
    "mariadb.no_instant_compressed": (
        "ROW_FORMAT=COMPRESSED tables cannot change columns instantly.",
        _MA_H + ":1913-1915 instant_alter_column_possible; storage/innobase/include/dict0mem.h supports_instant"),
    "mariadb.no_instant_index": (
        "Adding an index together with instantly added or dropped columns is not instant; the "
        "columns are rebuilt into the table.",
        _MA_H + ":1917-1940 instant_alter_column_possible"),
    "mariadb.no_instant_nullable": (
        "Removing NOT NULL is instant only for ROW_FORMAT=REDUNDANT; other row formats rebuild.",
        _MA_H + ":1945-1955 instant_alter_column_possible"),
    "mariadb.instant_setting": (
        "innodb_instant_alter_column_allowed does not allow this change instantly, so the table is "
        "rebuilt.",
        _MA_H + ":2306-2337"),
    "mariadb.copy_column_type": (
        "The column's stored format changes, which InnoDB cannot do in place.",
        _MA_T + ":7155-7178 fill_alter_inplace_info (ALTER_STORED_COLUMN_TYPE); " + _MA_I +
        ":20800-20960 can_convert_*; " + _MA_H + ":2275-2290"),
    "mariadb.copy_flags": (
        "This change is not one InnoDB can do in place.",
        _MA_H + ":2275-2290 check_if_supported_inplace_alter"),
    "mariadb.copy_generated": (
        "A stored generated column is computed by the server for every row, which needs a copy.",
        _MA_T + ":7336-7349 (ALTER_ADD_STORED_GENERATED_COLUMN); " + _MA_H + ":2275-2290"),
    "mariadb.copy_default": (
        "The new column's DEFAULT differs per row (it reads another column or calls a "
        "non-deterministic function), so the server copies the table to compute it.",
        _MA_H + ":1999 is_non_const_value, :2661-2676"),
    "mariadb.copy_check": (
        "Adding a CHECK constraint is validated by the server for every row, which needs a copy.",
        _MA_H + ":2275-2290 (ALTER_ADD_CHECK_CONSTRAINT is not an in-place flag)"),
    "mariadb.copy_fk_checks": (
        "Adding a foreign key in place needs foreign_key_checks=0; with checks on, the server copies "
        "the table to validate every row.",
        _MA_H + ":2295-2302"),
    "mariadb.copy_drop_pk": (
        "Dropping the primary key without adding another one needs a copy.",
        _MA_H + ":2367-2375"),
    "mariadb.copy_nullable_clustered": (
        "Making a column of the clustering unique key nullable needs a copy.",
        _MA_H + ":2376-2393"),
    "mariadb.copy_virtual_mix": (
        "Adding, dropping or moving virtual columns in place cannot be combined with these other "
        "changes (a stored column added or dropped before a virtual one moves it too).",
        _MA_T + ":7286-7291 (ALTER_VIRTUAL_COLUMN_ORDER); " + _MA_H + ":2686-2717"),
    "mariadb.copy_vcol": (
        "A stored generated column or an indexed virtual column may depend on the changed column, so "
        "the server recomputes it with a copy.",
        _MA_T + ":7315-7328 (ALTER_COLUMN_VCOL); " + _MA_H + ":2275-2290"),
    "mariadb.copy_fulltext": (
        "Rebuilding a table with more than one FULLTEXT index, or adding two at once, needs a copy.",
        _MA_H + ":2729-2740, :2779-2786"),
    "mariadb.copy_ignore": (
        "ALTER IGNORE cannot add a unique or primary key in place.",
        _MA_H + ":2356-2365"),
    "mariadb.copy_timestamp_not_null": (
        "Making a TIMESTAMP column NOT NULL needs a copy on MariaDB.",
        _MA_H + ":2609-2648"),
    "mariadb.copy_server": (
        "The server copies the table for this statement before asking InnoDB.",
        _MA_T + ":7893 is_inplace_alter_impossible, :11516-11532"),
    "mariadb.copy_alter_algorithm": (
        "The session's alter_algorithm=COPY makes the server copy the table.",
        "mariadb-server sql/sql_alter.cc:257 Alter_info::algorithm (mariadb-11.4.10 reads "
        "thd->variables.alter_algorithm; ignored from mariadb-11.5.0)"),
    "mariadb.copy_lock_none": (
        "LOCK=NONE asks for concurrent writes, which this change allows only by copying the table online.",
        "mariadb-server sql/sql_alter.cc:183-236 Alter_info::supports_lock; " + _MA_T + ":10650"),
    "mariadb.copy_requested": (
        "ALGORITHM=COPY asks for a copy.",
        _MA_T + ":11664"),
    "mariadb.inplace": (
        "InnoDB changes this in place without rebuilding the table (ALGORITHM=NOCOPY).",
        _MA_H + ":143-146 INNOBASE_ALTER_NOREBUILD, :2849-2866"),
    "mariadb.inplace_rebuild": (
        "InnoDB does this in place, but must rebuild the table to do it.",
        _MA_H + ":88-103 INNOBASE_ALTER_REBUILD, :1579 innobase_need_rebuild, :2849-2856"),
    "mariadb.inplace_fulltext": (
        "Adding a FULLTEXT index needs a shared lock; the first one also adds the hidden FTS_DOC_ID "
        "column, which rebuilds the table.",
        _MA_H + ":2766-2828"),
    "mariadb.lock_spatial": (
        "Building a SPATIAL index, or rebuilding a table with SPATIAL, FULLTEXT or indexed virtual "
        "columns, needs a shared lock.",
        _MA_H + ":2729-2760, :2817-2826"),
    "mariadb.lock_autoinc": (
        "A new AUTO_INCREMENT column cannot get its values while other sessions write, and is "
        "rebuilt into the table.",
        _MA_H + ":2527-2540"),
    "mariadb.lock_virtual_index": (
        "Indexing a newly added virtual column needs a shared lock.",
        _MA_H + ":2542-2560"),
    # -- shared --------------------------------------------------------------------------
    "unknown.syntax": ("This clause is outside the rules myflames checked against the server source.", ""),
    "unknown.fact": ("The outcome depends on something the table definition does not show.", ""),
    "unknown.refused": ("The server refuses this statement as written; nothing would change.", ""),
}

# ---------------------------------------------------------------------------
# Character sets: maximum bytes per character, and MySQL's primary (default)
# collation. information_schema.CHARACTER_SETS on MySQL 8.4.11 and MariaDB
# 11.4.13 (MAXLEN identical on both); mysql-server strings/ctype-*.cc
# MY_CS_PRIMARY. MariaDB's default collation can be configured
# (character_set_collations, 11.2+), so it is not assumed.

_CHARSETS = {
    "armscii8": (1, "armscii8_general_ci"), "ascii": (1, "ascii_general_ci"), "big5": (2, "big5_chinese_ci"),
    "binary": (1, "binary"), "cp1250": (1, "cp1250_general_ci"), "cp1251": (1, "cp1251_general_ci"),
    "cp1256": (1, "cp1256_general_ci"), "cp1257": (1, "cp1257_general_ci"), "cp850": (1, "cp850_general_ci"),
    "cp852": (1, "cp852_general_ci"), "cp866": (1, "cp866_general_ci"), "cp932": (2, "cp932_japanese_ci"),
    "dec8": (1, "dec8_swedish_ci"), "eucjpms": (3, "eucjpms_japanese_ci"), "euckr": (2, "euckr_korean_ci"),
    "gb18030": (4, "gb18030_chinese_ci"), "gb2312": (2, "gb2312_chinese_ci"), "gbk": (2, "gbk_chinese_ci"),
    "geostd8": (1, "geostd8_general_ci"), "greek": (1, "greek_general_ci"), "hebrew": (1, "hebrew_general_ci"),
    "hp8": (1, "hp8_english_ci"), "keybcs2": (1, "keybcs2_general_ci"), "koi8r": (1, "koi8r_general_ci"),
    "koi8u": (1, "koi8u_general_ci"), "latin1": (1, "latin1_swedish_ci"), "latin2": (1, "latin2_general_ci"),
    "latin5": (1, "latin5_turkish_ci"), "latin7": (1, "latin7_general_ci"), "macce": (1, "macce_general_ci"),
    "macroman": (1, "macroman_general_ci"), "sjis": (2, "sjis_japanese_ci"), "swe7": (1, "swe7_swedish_ci"),
    "tis620": (1, "tis620_thai_ci"), "ucs2": (2, "ucs2_general_ci"), "ujis": (3, "ujis_japanese_ci"),
    "utf16": (4, "utf16_general_ci"), "utf16le": (4, "utf16le_general_ci"), "utf32": (4, "utf32_general_ci"),
    "utf8mb3": (3, "utf8mb3_general_ci"), "utf8mb4": (4, "utf8mb4_0900_ai_ci"),
}
# Fixed-width encodings (mbminlen == mbmaxlen > 1) matter to MariaDB's
# can_convert_string; ucs2/utf32 are fixed, utf16 is not.
_FIXED_WIDTH = {"ucs2", "utf32"}


def _charset_name(name):
    name = (name or "").lower()
    if name == "utf8":  # MySQL 8.4 and MariaDB (old_mode UTF8_IS_UTF8MB3, the default) read utf8 as utf8mb3
        return "utf8mb3"
    return name


def _collation_charset(collation):
    collation = (collation or "").lower()
    if collation == "binary":
        return "binary"
    for size in range(len(collation), 0, -1):
        head = collation[:size]
        if head.endswith("_") and _charset_name(head[:-1]) in _CHARSETS:
            return _charset_name(head[:-1])
    return None


def _normal_collation(collation):
    collation = (collation or "").lower()
    if collation.startswith("utf8_"):
        return "utf8mb3_" + collation[5:]
    return collation


# ---------------------------------------------------------------------------
# Tokens


class _Tok(object):
    __slots__ = ("kind", "text", "value", "start", "end")

    def __init__(self, kind, text, value, start, end):
        self.kind, self.text, self.value, self.start, self.end = kind, text, value, start, end

    def __repr__(self):
        return "<%s %r>" % (self.kind, self.text)


_WORD = re.compile(r"[0-9A-Za-z_$\u0080-\uffff]+")
_NUMBER = re.compile(r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)")
_EXEC_COMMENT = re.compile(r"/\*(M?!)\d*")
_TWO_CHAR_OPS = ("<=", ">=", "<>", "!=", ":=", "||", "&&", "->", "<<", ">>")


def _tokens(sql):
    """Tokens of *sql*: comments dropped, version comments (/*!80023 ... */,
    /*M!100301 ... */) read as code, the way both servers run them."""
    out, i, n, executable = [], 0, len(sql), 0
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if c == "#" or (sql.startswith("--", i) and (i + 2 >= n or sql[i + 2].isspace())):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            match = _EXEC_COMMENT.match(sql, i)
            if match:
                executable += 1
                i = match.end()
                continue
            j = sql.find("*/", i + 2)
            if j < 0:
                raise Unpredictable("A comment is not closed.")
            i = j + 2
            continue
        if executable and sql.startswith("*/", i):
            executable -= 1
            i += 2
            continue
        if c in "'\"`":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise Unpredictable("A quoted string or name is not closed.")
                ch = sql[j]
                if ch == "\\" and c != "`" and j + 1 < n:
                    buf.append(sql[j + 1])
                    j += 2
                    continue
                if ch == c:
                    if j + 1 < n and sql[j + 1] == c:
                        buf.append(c)
                        j += 2
                        continue
                    break
                buf.append(ch)
                j += 1
            out.append(_Tok("ident" if c == "`" else "str", sql[i:j + 1], "".join(buf), i, j + 1))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            match = _NUMBER.match(sql, i)
            word = _WORD.match(sql, i)
            if match and not (word and word.end() > match.end()):
                out.append(_Tok("num", match.group(0), match.group(0), i, match.end()))
                i = match.end()
                continue
        match = _WORD.match(sql, i)
        if match:
            text = match.group(0)
            out.append(_Tok("word", text, text.upper(), i, match.end()))
            i = match.end()
            continue
        two = sql[i:i + 2]
        if two in _TWO_CHAR_OPS:
            out.append(_Tok("op", two, two, i, i + 2))
            i += 2
            continue
        out.append(_Tok("op", c, c, i, i + 1))
        i += 1
    return out


class _Parser(object):
    """A cursor over tokens with the few helpers the grammar below needs."""

    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def at_end(self):
        return self.i >= len(self.toks)

    def is_word(self, *words, k=0):
        tok = self.peek(k)
        return tok is not None and tok.kind == "word" and (not words or tok.value in words)

    def word(self, *words):
        if self.is_word(*words):
            tok = self.toks[self.i]
            self.i += 1
            return tok.value
        return None

    def words(self, *seq):
        for k, word in enumerate(seq):
            if not self.is_word(word, k=k):
                return False
        self.i += len(seq)
        return True

    def is_op(self, op, k=0):
        tok = self.peek(k)
        return tok is not None and tok.kind == "op" and tok.value == op

    def op(self, op):
        if self.is_op(op):
            self.i += 1
            return True
        return False

    def name(self):
        """An identifier: `quoted` or a bare word (its written spelling)."""
        tok = self.peek()
        if tok is None or tok.kind not in ("ident", "word", "str"):
            raise Unpredictable("Expected a name.")
        if tok.kind == "str" and not tok.text.startswith('"'):
            raise Unpredictable("Expected a name.")
        self.i += 1
        return tok.value if tok.kind != "word" else tok.text

    def qualified_name(self):
        first = self.name()
        if self.op("."):
            return self.name()
        return first

    def group(self):
        """Tokens inside a balanced (...) at the cursor, which must be '('."""
        if not self.is_op("("):
            raise Unpredictable("Expected '('.")
        depth, start = 0, self.i
        while self.i < len(self.toks):
            tok = self.toks[self.i]
            if tok.kind == "op" and tok.value == "(":
                depth += 1
            elif tok.kind == "op" and tok.value == ")":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.toks[start + 1:self.i - 1]
            self.i += 1
        raise Unpredictable("A parenthesis is not closed.")

    def eq(self):
        self.op("=")

    def value(self):
        """A table or column option value: a word, number or string."""
        tok = self.peek()
        if tok is None or tok.kind == "op":
            raise Unpredictable("Expected a value.")
        self.i += 1
        return tok.value if tok.kind != "word" else tok.text


def _split_top(toks, sep=","):
    """Token lists separated by top-level *sep* (outside parentheses)."""
    parts, depth, current = [], 0, []
    for tok in toks:
        if tok.kind == "op" and tok.value == "(":
            depth += 1
        elif tok.kind == "op" and tok.value == ")":
            depth -= 1
        if depth == 0 and tok.kind == "op" and tok.value == sep:
            parts.append(current)
            current = []
            continue
        current.append(tok)
    parts.append(current)
    return parts


# ---------------------------------------------------------------------------
# Table model

_INT_TYPES = {"TINYINT": 1, "SMALLINT": 2, "MEDIUMINT": 3, "INT": 4, "BIGINT": 8}
_REAL_TYPES = {"FLOAT": 4, "DOUBLE": 8}
_BLOB_TYPES = {"TINYBLOB": (1, 255), "BLOB": (2, 65535), "MEDIUMBLOB": (3, 16777215), "LONGBLOB": (4, 4294967295)}
_TEXT_TO_BLOB = {"TINYTEXT": "TINYBLOB", "TEXT": "BLOB", "MEDIUMTEXT": "MEDIUMBLOB", "LONGTEXT": "LONGBLOB"}
_TEMPORAL = {"DATE", "TIME", "DATETIME", "TIMESTAMP"}
_GEOMETRY = {"GEOMETRY", "POINT", "LINESTRING", "POLYGON", "MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON",
             "GEOMETRYCOLLECTION"}
_SYNONYMS = {"INTEGER": "INT", "INT1": "TINYINT", "INT2": "SMALLINT", "INT3": "MEDIUMINT", "MIDDLEINT": "MEDIUMINT",
             "INT4": "INT", "INT8": "BIGINT", "DEC": "DECIMAL", "NUMERIC": "DECIMAL", "FIXED": "DECIMAL",
             "FLOAT4": "FLOAT", "FLOAT8": "DOUBLE", "REAL": "DOUBLE", "CHARACTER": "CHAR",
             "VARCHARACTER": "VARCHAR", "GEOMCOLLECTION": "GEOMETRYCOLLECTION"}


class _Column(object):
    def __init__(self, name):
        self.name = name
        self.origin = name  # the old column this one comes from; None when added
        self.type = None  # canonical type word
        self.display = None  # type as written, lowercased, for messages
        self.length = None  # characters for strings, digits/bits for numbers
        self.decimals = None
        self.members = None  # ENUM/SET values
        self.unsigned = False
        self.charset = None
        self.collation = None
        self.nullable = True
        self.default = None  # None, "null", "literal", "now", "expr"
        self.default_toks = None
        self.auto_increment = False
        self.generated = None  # None, "VIRTUAL", "STORED"
        self.gen_toks = None
        self.invisible = False
        self.srid = None
        self.compressed = False
        self.inline_unique = False
        self.inline_primary = False
        self.inline_check = None  # (enforced,)
        self.binary_attr = False
        self.null_given = False
        self.text = ""

    # -- sizes ---------------------------------------------------------------------
    def kind(self):
        t = self.type
        if t in _INT_TYPES:
            return "int"
        if t in _REAL_TYPES:
            return "real"
        if t in ("CHAR", "VARCHAR"):
            return t.lower()
        if t in _BLOB_TYPES:
            return "blob"
        if t in _TEMPORAL:
            return "temporal"
        if t in _GEOMETRY:
            return "geometry"
        return {"DECIMAL": "decimal", "BIT": "bit", "YEAR": "year", "ENUM": "enum", "SET": "set",
                "JSON": "json"}.get(t, "other")

    def is_string(self):
        return self.kind() in ("char", "varchar", "blob", "enum", "set")

    def maxlen(self):
        if self.charset is None:
            raise Unpredictable("The character set of column %s is not known." % self.name)
        if self.charset not in _CHARSETS:
            raise Unpredictable("Character set %s is outside the checked rules." % self.charset)
        return _CHARSETS[self.charset][0]

    def byte_length(self):
        """Maximum bytes of a CHAR/VARCHAR value (Field::field_length)."""
        return (self.length or 0) * self.maxlen()

    def pack_length(self):
        kind = self.kind()
        if kind == "int":
            return _INT_TYPES[self.type]
        if kind == "real":
            return _REAL_TYPES[self.type]
        if kind == "varchar":
            size = self.byte_length()
            return size + (1 if size < 256 else 2)
        if kind == "char":
            return self.byte_length()
        if kind == "blob":
            return _BLOB_TYPES[self.type][0] + 8
        if kind == "enum":
            return 1 if len(self.members) < 256 else 2
        if kind == "set":
            size = (len(self.members) + 7) // 8
            return 8 if size > 4 else size
        if kind == "decimal":
            return _decimal_bytes(self.length, self.decimals)
        if kind == "bit":
            return ((self.length or 1) + 7) // 8
        if kind == "year":
            return 1
        if kind == "temporal":
            fsp = ((self.decimals or 0) + 1) // 2
            return {"DATE": 3, "TIME": 3 + fsp, "DATETIME": 5 + fsp, "TIMESTAMP": 4 + fsp}[self.type]
        return None

    def key_bytes(self, prefix):
        """Key part length in bytes (KEY_PART_INFO::length). A FULLTEXT
        or SPATIAL part (no prefix on a BLOB or geometry) counts as 0."""
        if prefix:
            return prefix * (self.maxlen() if self.is_string() else 1)
        if self.kind() in ("char", "varchar"):
            return self.byte_length()
        if self.kind() in ("blob", "json", "geometry"):
            return 0
        return self.pack_length() or 0

    def describe(self):
        return self.display or (self.type or "").lower()


def _decimal_bytes(precision, scale):
    digits = (0, 1, 1, 2, 2, 3, 3, 4, 4, 4)
    precision = precision if precision is not None else 10
    scale = scale or 0
    whole = precision - scale
    return (whole // 9) * 4 + digits[whole % 9] + (scale // 9) * 4 + digits[scale % 9]


class _Index(object):
    def __init__(self, name, kind):
        self.name = name
        self.origin = name  # the old index this one comes from; None when added
        self.kind = kind  # PRIMARY, UNIQUE, KEY, FULLTEXT, SPATIAL
        self.parts = []  # [column name, prefix length or None, descending]
        self.comment = None
        self.visible = True
        self.ignored = False
        self.functional = False
        self.renamed = False  # by RENAME INDEX (MySQL matches renamed indexes by that list)
        self.implicit = False  # created by the server for a foreign key

    def unique(self):
        return self.kind in ("PRIMARY", "UNIQUE")


class _ForeignKey(object):
    def __init__(self, name, columns, ref_table, ref_columns, on_delete, on_update):
        self.name, self.columns, self.ref_table = name, columns, ref_table
        self.ref_columns, self.on_delete, self.on_update = ref_columns, on_delete, on_update


class _Table(object):
    def __init__(self):
        self.name = None
        self.temporary = False
        self.columns = []
        self.indexes = []
        self.foreign_keys = []
        self.checks = []  # [name, enforced, tokens]
        self.engine = None
        self.charset = None
        self.collation = None
        self.row_format = None
        self.key_block_size = None
        self.options = {}
        self.partitioned = False
        self.versioned = False
        self.unsupported = []

    def column(self, name):
        for col in self.columns:
            if col.name.lower() == (name or "").lower():
                return col
        return None

    def index(self, name):
        for index in self.indexes:
            if index.name.lower() == (name or "").lower():
                return index
        return None

    def primary(self):
        for index in self.indexes:
            if index.kind == "PRIMARY":
                return index
        return None

    def candidate(self, index):
        """A unique key InnoDB may cluster on: no prefix parts, no nullable
        part (sql/sql_table.cc is_candidate_key)."""
        if not index.unique() or index.functional:
            return False
        for name, prefix, _ in index.parts:
            col = self.column(name)
            if col is None or col.nullable or (prefix and col.key_bytes(None) != col.key_bytes(prefix)):
                return False
        return True

    def clustered(self):
        """The index InnoDB clusters the rows on, or None for its own row id."""
        primary = self.primary()
        if primary is not None:
            return primary
        for index in self.indexes:
            if index.kind == "UNIQUE" and self.candidate(index):
                return index
        return None

    def compressed(self):
        if (self.row_format or "").upper() == "COMPRESSED":
            return True
        return bool(self.key_block_size) and (self.row_format or "").upper() in ("", "DEFAULT")

    def fulltext(self):
        return [index for index in self.indexes if index.kind == "FULLTEXT"]

    def spatial(self):
        return [index for index in self.indexes if index.kind == "SPATIAL"]

    def stored(self):
        return [col for col in self.columns if col.generated != "VIRTUAL"]


# ---------------------------------------------------------------------------
# Column definitions (shared by SHOW CREATE TABLE and ALTER)

_NOW = {"CURRENT_TIMESTAMP", "NOW", "LOCALTIME", "LOCALTIMESTAMP"}


def _parse_type(p, col):
    word = p.word()
    if word is None:
        raise Unpredictable("Column %s has no data type." % col.name)
    t = _SYNONYMS.get(word, word)
    national = False
    if t == "NATIONAL":
        national = True
        t = p.word("CHAR", "CHARACTER", "VARCHAR") or "?"
        t = _SYNONYMS.get(t, t)
    if t in ("NCHAR", "NVARCHAR"):
        national = True
        t = "CHAR" if t == "NCHAR" else "VARCHAR"
    if t == "CHAR" and p.word("VARYING"):
        t = "VARCHAR"
    if t == "DOUBLE":
        p.word("PRECISION")
    if t == "LONG":
        if p.word("VARBINARY"):
            t = "MEDIUMBLOB"
        else:
            p.word("VARCHAR")
            t = "MEDIUMTEXT"
    if t == "SERIAL":
        t = "BIGINT"
        col.unsigned, col.nullable, col.auto_increment, col.inline_unique = True, False, True, True
    if t in ("BOOL", "BOOLEAN"):
        t, col.length = "TINYINT", 1
    col.display = word.lower()
    params = None
    if p.is_op("("):
        params = p.group()
        col.display += "(" + "".join(tok.text if tok.kind != "op" else tok.value for tok in params) + ")"
    binary_charset = False
    if t in ("BINARY", "VARBINARY"):
        binary_charset = True
        t = "CHAR" if t == "BINARY" else "VARCHAR"
    if t in _TEXT_TO_BLOB:
        t = _TEXT_TO_BLOB[t]
        col.charset = None  # filled from the attributes or the table
    elif t in _BLOB_TYPES:
        binary_charset = True
    if t in ("ENUM", "SET"):
        if not params:
            raise Unpredictable("%s without values." % t)
        col.members = [tok.value for tok in params if tok.kind == "str"]
        if len(col.members) != len([x for x in _split_top(params) if x]):
            raise Unpredictable("%s values are not plain strings." % t)
    elif params:
        numbers = [tok for tok in params if tok.kind == "num"]
        if len(numbers) != len([x for x in _split_top(params) if x]):
            raise Unpredictable("Column %s has unusual type arguments." % col.name)
        if numbers:
            col.length = int(float(numbers[0].value))
        if len(numbers) > 1:
            col.decimals = int(numbers[1].value)
    if t == "FLOAT" and col.length is not None and col.decimals is None:
        t = "DOUBLE" if col.length > 24 else "FLOAT"
        col.length = None
    if t == "DECIMAL":
        col.length = 10 if col.length is None else col.length
        col.decimals = 0 if col.decimals is None else col.decimals
    if t in ("CHAR",) and col.length is None:
        col.length = 1
    if t == "BIT" and col.length is None:
        col.length = 1
    if t in _TEMPORAL:
        col.decimals, col.length = (col.length or 0), None
    if t in _INT_TYPES:
        col.length = None  # display width does not change storage
    if t == "VARCHAR" and col.length is None:
        raise Unpredictable("VARCHAR without a length.")
    if t in ("VECTOR",):
        raise Unpredictable("VECTOR columns are outside the checked rules.")
    known = (set(_INT_TYPES) | set(_REAL_TYPES) | set(_BLOB_TYPES) | _TEMPORAL | _GEOMETRY |
             {"CHAR", "VARCHAR", "DECIMAL", "BIT", "YEAR", "ENUM", "SET", "JSON"})
    if t not in known:
        raise Unpredictable("Data type %s is outside the checked rules." % word.lower())
    col.type = t
    if binary_charset:
        col.charset, col.collation = "binary", "binary"
    if national:
        col.charset = "utf8mb3"
    return col


def _parse_default(p, col):
    tok = p.peek()
    if tok is None:
        raise Unpredictable("DEFAULT without a value.")
    start = p.i
    if p.is_op("("):
        p.group()
        col.default = "expr"
    elif tok.kind == "str":
        p.i += 1
        col.default = "literal"
    elif tok.kind == "num" or (tok.kind == "op" and tok.value in "-+"):
        p.i += 1
        if tok.kind == "op":
            if not (p.peek() and p.peek().kind == "num"):
                raise Unpredictable("Unusual DEFAULT value.")
            p.i += 1
        col.default = "literal"
    elif tok.kind == "word" and tok.value == "NULL":
        p.i += 1
        col.default = "null"
    elif tok.kind == "word" and tok.value in ("TRUE", "FALSE"):
        p.i += 1
        col.default = "literal"
    elif tok.kind == "word" and tok.value in _NOW:
        p.i += 1
        if p.is_op("("):
            p.group()
        col.default = "now"
    elif tok.kind == "word" and p.peek(1) is not None and p.peek(1).kind == "str" and \
            (tok.value in ("X", "B", "N") or tok.value.startswith("_")):
        p.i += 2
        col.default = "literal"
    elif tok.kind == "word" and p.is_op("(", k=1):
        p.i += 1
        p.group()
        col.default = "expr"
    else:
        raise Unpredictable("Unusual DEFAULT value.")
    col.default_toks = p.toks[start:p.i]


def _parse_column(p, name):
    """A column definition after its name, up to the end of *p* or FIRST/AFTER."""
    col = _Column(name)
    _parse_type(p, col)
    while not p.at_end():
        if p.is_word("FIRST", "AFTER"):
            break
        tok = p.peek()
        word = p.word()
        if word is None:
            raise Unpredictable("Unexpected %r in column %s." % (tok.text, name))
        if word == "UNSIGNED" or word == "ZEROFILL":
            col.unsigned = True
        elif word == "SIGNED":
            col.unsigned = False
        elif word in ("CHARACTER", "CHAR") and p.word("SET") or word == "CHARSET":
            p.eq()
            col.charset = _charset_name(p.value())
            col.collation = None
            col.binary_attr = False
        elif word == "COLLATE":
            p.eq()
            col.collation = _normal_collation(p.value())
        elif word == "BINARY":
            col.binary_attr = True
        elif word == "ASCII":
            col.charset = "latin1"
        elif word == "UNICODE":
            col.charset = "ucs2"
        elif word == "BYTE":
            col.charset, col.collation = "binary", "binary"
        elif word == "NULL":
            col.nullable = True
            col.null_given = True
        elif word == "NOT":
            if p.word("NULL"):
                col.nullable = False
                col.null_given = True
            elif p.word("SECONDARY"):
                pass
            else:
                raise Unpredictable("Unexpected NOT in column %s." % name)
        elif word == "DEFAULT":
            _parse_default(p, col)
        elif word == "ON":
            if not p.word("UPDATE"):
                raise Unpredictable("Unexpected ON in column %s." % name)
            if not p.word(*_NOW):
                raise Unpredictable("Unusual ON UPDATE in column %s." % name)
            if p.is_op("("):
                p.group()
        elif word == "AUTO_INCREMENT":
            col.auto_increment = True
        elif word == "UNIQUE":
            p.word("KEY")
            col.inline_unique = True
        elif word == "PRIMARY":
            p.word("KEY")
            col.inline_primary = True
        elif word == "KEY":
            col.inline_primary = True
        elif word == "COMMENT":
            p.value()
        elif word in ("COLUMN_FORMAT", "STORAGE"):
            p.value()
        elif word == "GENERATED":
            if not p.words("ALWAYS"):
                raise Unpredictable("Unusual GENERATED in column %s." % name)
            if not p.word("AS"):
                raise Unpredictable("Unusual GENERATED in column %s." % name)
            col.gen_toks = p.group()
            col.generated = "VIRTUAL"
        elif word == "AS":
            if p.word("ROW"):
                raise Unpredictable("System-versioning columns are outside the checked rules.")
            col.gen_toks = p.group()
            col.generated = "VIRTUAL"
        elif word == "VIRTUAL":
            col.generated = "VIRTUAL"
        elif word in ("STORED", "PERSISTENT"):
            col.generated = "STORED"
        elif word == "VISIBLE":
            col.invisible = False
        elif word == "INVISIBLE":
            col.invisible = True
        elif word == "SRID":
            col.srid = p.value()
        elif word == "REF_SYSTEM_ID":
            p.eq()
            col.srid = p.value()
        elif word == "CHECK":
            p.group()
            enforced = True
            if p.words("NOT", "ENFORCED"):
                enforced = False
            else:
                p.word("ENFORCED")
            col.inline_check = enforced
        elif word == "CONSTRAINT":
            if not p.is_word("CHECK"):
                p.name()
        elif word in ("ENGINE_ATTRIBUTE", "SECONDARY_ENGINE_ATTRIBUTE"):
            p.eq()
            p.value()
        elif word == "COMPRESSED":
            col.compressed = True
            if p.op("="):
                p.value()
        elif word == "REFERENCES":
            raise Unpredictable("An inline REFERENCES clause is outside the checked rules.")
        elif word in ("WITH", "WITHOUT"):
            raise Unpredictable("System-versioning columns are outside the checked rules.")
        else:
            raise Unpredictable("Column attribute %s is outside the checked rules." % word)
    if col.generated and col.gen_toks is None:
        raise Unpredictable("Generated column %s has no expression." % name)
    return col


def _resolve_charset(col, table_charset, table_collation, engine, defaults=None):
    """Effective character set and collation of a string column.

    MySQL gives a column with CHARACTER SET but no COLLATE the set's primary
    collation; with neither, the table default (sql/sql_table.cc:4346
    get_sql_field_charset). MariaDB can map a set to another default
    collation (character_set_collations), so its collation stays unknown
    then."""
    if not col.is_string() or col.charset == "binary":
        if col.is_string():
            col.collation = "binary"
        return
    binary_attr = getattr(col, "binary_attr", False)
    if col.collation and not col.charset:
        cs = _collation_charset(col.collation)
        if cs is None:
            # MariaDB 11.2+ context collations ("uca1400_ai_ci") apply to the set in use.
            base = table_charset
            if base is None:
                raise Unpredictable("The collation of column %s is not known." % col.name)
            col.charset, col.collation = base, base + "_" + col.collation
        else:
            col.charset = cs
    elif col.collation and col.charset and _collation_charset(col.collation) is None:
        col.collation = col.charset + "_" + col.collation
    if col.charset is None:
        col.charset = table_charset
        if col.collation is None:
            col.collation = table_collation
    elif col.collation is None:
        if binary_attr:
            col.collation = col.charset + "_bin"
        elif engine == "mysql":
            if col.charset not in _CHARSETS:
                raise Unpredictable("Character set %s is outside the checked rules." % col.charset)
            col.collation = _CHARSETS[col.charset][1]
        else:
            col.collation = _normal_collation((defaults or {}).get(col.charset)) or None
    if binary_attr and col.charset and col.collation and not col.collation.endswith("_bin"):
        col.collation = col.charset + "_bin"
    if col.charset is None:
        raise Unpredictable("The character set of column %s is not known." % col.name)
    if col.charset not in _CHARSETS:
        raise Unpredictable("Character set %s is outside the checked rules." % col.charset)


# ---------------------------------------------------------------------------
# Indexes and constraints (shared)


def _parse_parts(p):
    parts, functional = [], False
    for part in _split_top(p.group()):
        if not part:
            raise Unpredictable("An empty index part.")
        q = _Parser(part)
        if q.is_op("("):
            q.group()
            functional = True
            name, prefix = None, None
        else:
            name = q.name()
            prefix = None
            if q.is_op("("):
                inner = q.group()
                if len(inner) != 1 or inner[0].kind != "num":
                    raise Unpredictable("Unusual index prefix.")
                prefix = int(inner[0].value)
        desc = False
        if q.word("DESC"):
            desc = True
        else:
            q.word("ASC")
        if not q.at_end():
            raise Unpredictable("Unusual index part.")
        parts.append([name, prefix, desc])
    return parts, functional


def _parse_index_options(p, index):
    while not p.at_end():
        if p.is_word("FIRST", "AFTER"):
            break
        word = p.word()
        if word is None:
            raise Unpredictable("Unexpected %r in an index." % p.peek().text)
        if word == "USING":
            p.word("BTREE", "HASH", "RTREE")
        elif word == "KEY_BLOCK_SIZE":
            p.eq()
            p.value()
        elif word == "COMMENT":
            index.comment = p.value()
        elif word == "VISIBLE":
            index.visible = True
        elif word == "INVISIBLE":
            index.visible = False
        elif word == "IGNORED":
            index.ignored = True
        elif word == "NOT" and p.word("IGNORED"):
            index.ignored = False
        elif word == "WITH" and p.word("PARSER"):
            p.value()
        elif word in ("ENGINE_ATTRIBUTE", "SECONDARY_ENGINE_ATTRIBUTE"):
            p.eq()
            p.value()
        elif word == "CLUSTERING":
            raise Unpredictable("CLUSTERING indexes are outside the checked rules.")
        else:
            raise Unpredictable("Index option %s is outside the checked rules." % word)


_ACTIONS = ("RESTRICT", "CASCADE", "SET", "NO")


def _fk_action(p):
    word = p.word(*_ACTIONS)
    if word == "SET":
        return "SET " + (p.word("NULL", "DEFAULT") or "?")
    if word == "NO":
        p.word("ACTION")
        return "NO ACTION"
    if word is None:
        raise Unpredictable("Unusual foreign key action.")
    return word


def _parse_constraint(p, symbol=None):
    """An index, key or constraint definition; returns (kind, object)."""
    if p.word("CONSTRAINT"):
        if not p.is_word("PRIMARY", "UNIQUE", "FOREIGN", "CHECK"):
            symbol = p.name()
    word = p.word("PRIMARY", "UNIQUE", "KEY", "INDEX", "FULLTEXT", "SPATIAL", "FOREIGN", "CHECK", "VECTOR",
                  "PERIOD")
    if word in ("VECTOR", "PERIOD"):
        raise Unpredictable("%s definitions are outside the checked rules." % word)
    if word == "CHECK":
        toks = p.group()
        enforced = True
        if p.words("NOT", "ENFORCED"):
            enforced = False
        else:
            p.word("ENFORCED")
        return "check", [symbol, enforced, toks]
    if word == "FOREIGN":
        if not p.word("KEY"):
            raise Unpredictable("Expected FOREIGN KEY.")
        p.words("IF", "NOT", "EXISTS")
        if not p.is_op("("):
            p.name()
        parts, _ = _parse_parts(p)
        if not p.word("REFERENCES"):
            raise Unpredictable("Expected REFERENCES.")
        ref_table = p.qualified_name()
        ref_parts, _ = _parse_parts(p)
        on_delete = on_update = "RESTRICT"
        while not p.at_end():
            if p.word("MATCH"):
                p.word("FULL", "PARTIAL", "SIMPLE")
            elif p.words("ON", "DELETE"):
                on_delete = _fk_action(p)
            elif p.words("ON", "UPDATE"):
                on_update = _fk_action(p)
            else:
                raise Unpredictable("Unusual foreign key clause.")
        return "fk", _ForeignKey(symbol, [x[0] for x in parts], ref_table, [x[0] for x in ref_parts],
                                 on_delete, on_update)
    if word == "PRIMARY":
        if not p.word("KEY"):
            raise Unpredictable("Expected PRIMARY KEY.")
        kind, name = "PRIMARY", "PRIMARY"
    elif word == "UNIQUE":
        p.word("KEY", "INDEX")
        kind, name = "UNIQUE", None
    elif word in ("FULLTEXT", "SPATIAL"):
        p.word("KEY", "INDEX")
        kind, name = word, None
    elif word in ("KEY", "INDEX"):
        kind, name = "KEY", None
    else:
        raise Unpredictable("Unknown table element.")
    p.words("IF", "NOT", "EXISTS")
    if kind != "PRIMARY" and not p.is_op("(") and not p.is_word("USING"):
        name = p.name()
    if p.word("USING"):
        p.word("BTREE", "HASH", "RTREE")
    index = _Index(name, kind)
    index.symbol = symbol
    index.parts, index.functional = _parse_parts(p)
    _parse_index_options(p, index)
    return "index", index


# ---------------------------------------------------------------------------
# CREATE TABLE


_ELEMENT_WORDS = ("PRIMARY", "UNIQUE", "KEY", "INDEX", "FULLTEXT", "SPATIAL", "CONSTRAINT", "FOREIGN", "CHECK",
                  "PERIOD", "VECTOR")

# Table options whose change both engines treat like COMMENT (metadata only
# when alone): MySQL innobase_need_rebuild only looks at ROW_FORMAT,
# KEY_BLOCK_SIZE and TABLESPACE (handler0alter.cc:930-950); MariaDB
# alter_options_need_rebuild at ROW_FORMAT, KEY_BLOCK_SIZE and its
# page_compressed/encryption options (handler0alter.cc:1541-1570).
_PLAIN_OPTIONS = {"COMMENT", "AUTO_INCREMENT", "STATS_PERSISTENT", "STATS_AUTO_RECALC", "STATS_SAMPLE_PAGES",
                  "AVG_ROW_LENGTH", "MAX_ROWS", "MIN_ROWS", "CHECKSUM", "TABLE_CHECKSUM", "PACK_KEYS",
                  "DELAY_KEY_WRITE"}
_REBUILD_OPTIONS = {"ROW_FORMAT", "KEY_BLOCK_SIZE"}


def _parse_table_options(p, table, stop_at_partition=True):
    while not p.at_end():
        if p.op(","):
            continue
        if p.is_word("PARTITION"):
            table.partitioned = True
            return
        if p.words("WITH", "SYSTEM", "VERSIONING"):
            table.versioned = True
            continue
        default = bool(p.word("DEFAULT"))
        if p.word("CHARSET") or (p.is_word("CHARACTER", "CHAR") and p.is_word("SET", k=1) and p.word() and
                                 p.word("SET")):
            p.eq()
            table.charset = _charset_name(p.value())
            table.options["CHARSET"] = table.charset
            continue
        if p.word("COLLATE"):
            p.eq()
            table.collation = _normal_collation(p.value())
            table.options["COLLATE"] = table.collation
            continue
        if default:
            raise Unpredictable("Unusual DEFAULT table option.")
        if p.words("DATA", "DIRECTORY") or p.words("INDEX", "DIRECTORY"):
            p.eq()
            p.value()
            table.unsupported.append("DATA DIRECTORY")
            continue
        word = p.word()
        if word is None:
            raise Unpredictable("Unexpected %r in the table options." % p.peek().text)
        p.eq()
        if word == "UNION":
            p.group()
            table.unsupported.append(word)
            continue
        value = p.value()
        table.options[word] = value
        if word == "ENGINE" or word == "TYPE":
            table.engine = value
        elif word == "ROW_FORMAT":
            table.row_format = value.upper()
        elif word == "KEY_BLOCK_SIZE":
            table.key_block_size = int(value) if str(value).isdigit() else None


def _parse_create(sql, engine):
    p = _Parser(_tokens(sql))
    if not p.word("CREATE"):
        raise Unpredictable("The table definition does not start with CREATE TABLE.")
    p.words("OR", "REPLACE")
    table = _Table()
    if p.word("TEMPORARY"):
        table.temporary = True
    if not p.word("TABLE"):
        raise Unpredictable("The table definition does not start with CREATE TABLE.")
    p.words("IF", "NOT", "EXISTS")
    table.name = p.qualified_name()
    body = p.group()
    _parse_table_options(p, table)
    elements = []
    for element in _split_top(body):
        if not element:
            raise Unpredictable("An empty table element.")
        q = _Parser(element)
        tok = q.peek()
        if tok.kind == "word" and tok.value in _ELEMENT_WORDS:
            kind, obj = _parse_constraint(q)
            if not q.at_end():
                raise Unpredictable("Unexpected text after a table element.")
            elements.append((kind, obj))
        else:
            name = q.name()
            col = _parse_column(q, name)
            if not q.at_end():
                raise Unpredictable("Unexpected text after column %s." % name)
            col.text = " ".join(t.text for t in element)
            elements.append(("column", col))
    if table.charset is None and table.collation:
        table.charset = _collation_charset(table.collation)
    if table.charset and table.collation is None and engine == "mysql":
        table.collation = _CHARSETS.get(table.charset, (0, None))[1]
    for kind, obj in elements:
        if kind == "column":
            _resolve_charset(obj, table.charset, table.collation, engine)
            table.columns.append(obj)
            _inline_keys(table, obj)
        elif kind == "index":
            if obj.name is None:
                obj.name = _generated_index_name(table, obj)
            table.indexes.append(obj)
        elif kind == "fk":
            table.foreign_keys.append(obj)
        else:
            table.checks.append(obj)
    return table


def _inline_keys(table, col):
    if col.inline_primary and table.primary() is None:
        index = _Index("PRIMARY", "PRIMARY")
        index.parts = [[col.name, None, False]]
        table.indexes.insert(0, index)
        col.nullable = False
    if col.inline_unique:
        index = _Index(None, "UNIQUE")
        index.parts = [[col.name, None, False]]
        index.name = _generated_index_name(table, index)
        table.indexes.append(index)
    if col.inline_check is not None:
        table.checks.append([None, col.inline_check, []])


def _generated_index_name(table, index):
    """The server names an unnamed key after its first column, with _2, _3
    ... when taken (sql/sql_table.cc make_unique_key_name)."""
    base = index.parts[0][0] if index.parts and index.parts[0][0] else "functional_index"
    names = {i.name.lower() for i in table.indexes if i.name}
    if base.lower() not in names and base.upper() != "PRIMARY":
        return base
    n = 2
    while ("%s_%d" % (base, n)).lower() in names:
        n += 1
    return "%s_%d" % (base, n)


# ---------------------------------------------------------------------------
# ALTER TABLE


class _Clause(object):
    def __init__(self, kind, text, **data):
        self.kind = kind
        self.text = text
        self.data = data


_PARTITION_WORDS = ("PARTITION", "REMOVE", "COALESCE", "REORGANIZE", "EXCHANGE", "ANALYZE", "CHECK", "OPTIMIZE",
                    "REBUILD", "REPAIR", "TRUNCATE", "DISCARD", "IMPORT", "UPGRADE")


def _clause_text(sql, toks):
    return sql[toks[0].start:toks[-1].end] if toks else ""


def _parse_alter(sql):
    toks = _tokens(sql)
    while toks and toks[-1].kind == "op" and toks[-1].value == ";":
        toks.pop()
    p = _Parser(toks)
    if not p.word("ALTER"):
        raise Unpredictable("Not an ALTER TABLE statement.")
    head = {"online": False, "ignore": False}
    while p.is_word("ONLINE", "OFFLINE", "IGNORE"):
        word = p.word()
        if word == "OFFLINE":
            raise Unpredictable("ALTER OFFLINE TABLE is outside the checked rules.")
        head[word.lower()] = True
    if not p.word("TABLE"):
        raise Unpredictable("Not an ALTER TABLE statement.")
    p.words("IF", "EXISTS")
    name = p.qualified_name()
    if p.word("WAIT"):
        p.value()
    else:
        p.word("NOWAIT")
    clauses = []
    rest = toks[p.i:]
    if not rest:
        raise Unpredictable("The statement changes nothing.")
    for part in _split_top(rest):
        if not part:
            raise Unpredictable("An empty ALTER clause.")
        clauses.extend(_parse_clause(sql, part))
    return name, head, clauses


def _parse_clause(sql, toks):
    text = _clause_text(sql, toks)
    p = _Parser(toks)

    def done(clause):
        if not p.at_end():
            raise Unpredictable("Unexpected text in clause: %s" % text)
        return [clause]

    try:
        if p.is_word("PARTITION") or (p.is_word(*_PARTITION_WORDS) and p.is_word("PARTITION", "PARTITIONING",
                                                                                 "TABLESPACE", k=1)):
            raise Unpredictable("Partitioning and tablespace clauses are outside the checked rules.")
        if p.word("ADD"):
            if p.is_word("PARTITION", "PERIOD", "SYSTEM", "VECTOR") and not p.peek().kind == "ident":
                raise Unpredictable("%s is outside the checked rules." % p.peek().value)
            if p.is_word(*_ELEMENT_WORDS):
                kind, obj = _parse_constraint(p)
                return done(_Clause("add_" + kind, text, obj=obj))
            p.word("COLUMN")
            p.words("IF", "NOT", "EXISTS")
            if p.is_op("("):
                cols = []
                for part in _split_top(p.group()):
                    q = _Parser(part)
                    cname = q.name()
                    cols.append(_parse_column(q, cname))
                    if not q.at_end():
                        raise Unpredictable("Unexpected text in ADD COLUMN.")
                return done(_Clause("add_columns", text, columns=cols))
            cname = p.name()
            col = _parse_column(p, cname)
            position = _position(p)
            return done(_Clause("add_column", text, column=col, position=position))
        if p.word("DROP"):
            if p.is_word("PARTITION", "PERIOD", "SYSTEM"):
                raise Unpredictable("%s is outside the checked rules." % p.peek().value)
            if p.words("PRIMARY", "KEY"):
                return done(_Clause("drop_primary", text))
            if p.word("INDEX", "KEY"):
                p.words("IF", "EXISTS")
                return done(_Clause("drop_index", text, name=p.name()))
            if p.words("FOREIGN", "KEY"):
                p.words("IF", "EXISTS")
                return done(_Clause("drop_fk", text, name=p.name()))
            if p.word("CHECK"):
                return done(_Clause("drop_check", text, name=p.name()))
            if p.word("CONSTRAINT"):
                p.words("IF", "EXISTS")
                return done(_Clause("drop_constraint", text, name=p.name()))
            p.word("COLUMN")
            p.words("IF", "EXISTS")
            cname = p.name()
            p.word("RESTRICT", "CASCADE")
            return done(_Clause("drop_column", text, name=cname))
        if p.word("MODIFY"):
            p.word("COLUMN")
            p.words("IF", "EXISTS")
            cname = p.name()
            col = _parse_column(p, cname)
            return done(_Clause("modify_column", text, name=cname, column=col, position=_position(p)))
        if p.word("CHANGE"):
            p.word("COLUMN")
            p.words("IF", "EXISTS")
            old = p.name()
            new = p.name()
            col = _parse_column(p, new)
            return done(_Clause("modify_column", text, name=old, column=col, position=_position(p)))
        if p.word("RENAME"):
            if p.word("COLUMN"):
                old = p.name()
                if not p.word("TO"):
                    raise Unpredictable("Expected TO.")
                return done(_Clause("rename_column", text, name=old, new=p.name()))
            if p.word("INDEX", "KEY"):
                old = p.name()
                if not p.word("TO"):
                    raise Unpredictable("Expected TO.")
                return done(_Clause("rename_index", text, name=old, new=p.name()))
            p.word("TO", "AS")
            return done(_Clause("rename_table", text, new=p.qualified_name()))
        if p.word("ALTER"):
            if p.word("INDEX", "KEY"):
                iname = p.name()
                if p.word("VISIBLE"):
                    return done(_Clause("index_visibility", text, name=iname, visible=True))
                if p.word("INVISIBLE"):
                    return done(_Clause("index_visibility", text, name=iname, visible=False))
                if p.word("IGNORED"):
                    return done(_Clause("index_ignored", text, name=iname, ignored=True))
                if p.words("NOT", "IGNORED"):
                    return done(_Clause("index_ignored", text, name=iname, ignored=False))
                raise Unpredictable("Unusual ALTER INDEX.")
            if p.word("CHECK", "CONSTRAINT"):
                cname = p.name()
                if p.words("NOT", "ENFORCED"):
                    return done(_Clause("check_enforced", text, name=cname, enforced=False))
                if p.word("ENFORCED"):
                    return done(_Clause("check_enforced", text, name=cname, enforced=True))
                raise Unpredictable("Unusual ALTER CHECK.")
            p.word("COLUMN")
            p.words("IF", "EXISTS")
            cname = p.name()
            if p.words("SET", "DEFAULT"):
                tmp = _Column(cname)
                _parse_default(p, tmp)
                return done(_Clause("set_default", text, name=cname, default=tmp.default,
                                    default_toks=tmp.default_toks))
            if p.words("DROP", "DEFAULT"):
                return done(_Clause("drop_default", text, name=cname))
            if p.words("SET", "VISIBLE"):
                return done(_Clause("column_visibility", text, name=cname, invisible=False))
            if p.words("SET", "INVISIBLE"):
                return done(_Clause("column_visibility", text, name=cname, invisible=True))
            raise Unpredictable("Unusual ALTER COLUMN.")
        if p.words("CONVERT", "TO"):
            if not (p.word("CHARSET") or p.words("CHARACTER", "SET") or p.words("CHAR", "SET")):
                raise Unpredictable("Unusual CONVERT TO.")
            p.eq()
            if p.word("DEFAULT"):
                raise Unpredictable("CONVERT TO CHARACTER SET DEFAULT depends on the database default.")
            cs = _charset_name(p.value())
            coll = None
            if p.word("COLLATE"):
                p.eq()
                coll = _normal_collation(p.value())
            return done(_Clause("convert", text, charset=cs, collation=coll))
        if p.word("FORCE"):
            return done(_Clause("force", text))
        if p.words("ORDER", "BY"):
            return [_Clause("order_by", text)]
        if p.word("ENABLE", "DISABLE"):
            raise Unpredictable("ENABLE/DISABLE KEYS is outside the checked rules.")
        if p.word("ALGORITHM"):
            p.eq()
            return done(_Clause("algorithm", text, value=str(p.value()).upper()))
        if p.word("LOCK"):
            p.eq()
            return done(_Clause("lock", text, value=str(p.value()).upper()))
        if p.word("WITH", "WITHOUT"):
            raise Unpredictable("WITH/WITHOUT VALIDATION is outside the checked rules.")
        options = _Table()
        try:
            _parse_table_options(p, options)
        except Unpredictable:
            raise Unpredictable("The clause %s is outside the checked rules." % text)
        if options.partitioned or options.versioned or options.unsupported:
            raise Unpredictable("This table option is outside the checked rules.")
        if not p.at_end() or not options.options:
            raise Unpredictable("The clause %s is outside the checked rules." % text)
        for key in options.options:
            if key not in _PLAIN_OPTIONS and key not in _REBUILD_OPTIONS and \
                    key not in ("ENGINE", "TYPE", "CHARSET", "COLLATE"):
                raise Unpredictable("Table option %s is outside the checked rules." % key)
        return [_Clause("options", text, table=options)]
    except Unpredictable as exc:
        return [_Clause("unknown", text, reason=str(exc))]


def _position(p):
    if p.word("FIRST"):
        return ("FIRST", None)
    if p.word("AFTER"):
        return ("AFTER", p.name())
    return None


# ---------------------------------------------------------------------------
# Applying clauses to a copy of the table


class _Change(object):
    """What the statement changes, beyond the new table itself."""

    def __init__(self):
        self.kinds = set()  # parser-level clause kinds
        self.options = {}  # table options given
        self.order_by = False
        self.rename_table = False
        self.engine_change = False
        self.recreate = False
        self.check_add_enforced = False
        self.check_enforce = False
        self.add_fk = False
        self.drop_fk = False
        self.column_visibility = False
        self.renamed_indexes = []  # MySQL RENAME INDEX pairs (old, new)
        self.visibility_indexes = []
        self.ignored_indexes = []
        self.convert = None


def _norm(name):
    return (name or "").lower()


def _apply(table, clauses, engine, defaults=None):
    new = copy.deepcopy(table)
    change = _Change()
    # A new table default (DEFAULT CHARSET, COLLATE or CONVERT TO), which new
    # and modified columns without a character set take (mysql-server
    # sql/sql_table.cc:4346 get_sql_field_charset; sql/parse_tree_nodes.cc:3615
    # PT_alter_table_convert_to_charset).
    for clause in clauses:
        cs = coll = None
        if clause.kind == "options":
            cs, coll = clause.data["table"].charset, clause.data["table"].collation
        elif clause.kind == "convert":
            cs, coll = clause.data["charset"], clause.data["collation"]
        if cs is None and coll is None:
            continue
        if cs is None:
            cs = _collation_charset(coll)
        if coll is None:
            coll = _CHARSETS.get(cs, (0, None))[1] if engine == "mysql" else \
                _normal_collation((defaults or {}).get(cs)) or None
        new.charset, new.collation = cs, coll
    for clause in clauses:
        kind = clause.kind
        if kind not in ("algorithm", "lock"):
            change.kinds.add(kind)
        d = clause.data
        for col in ([d["column"]] if kind in ("add_column", "modify_column") else
                    d["columns"] if kind == "add_columns" else []):
            if col.inline_check is not None:
                if engine == "mariadb":
                    # A column CHECK sets no ALTER_ADD_CHECK_CONSTRAINT
                    # (sql/sql_yacc.yy:8110-8140); how it is validated is
                    # outside the checked rules.
                    raise Unpredictable("A column-level CHECK is outside the checked rules on MariaDB.")
                change.kinds.add("add_check")
                if col.inline_check:
                    change.check_add_enforced = True
        if kind == "add_column":
            _add_column(new, d["column"], d["position"], engine, defaults)
        elif kind == "add_columns":
            for col in d["columns"]:
                _add_column(new, col, None, engine, defaults)
        elif kind == "drop_column":
            _drop_column(new, d["name"], change)
        elif kind == "modify_column":
            _modify_column(new, d["name"], d["column"], d["position"], engine, defaults)
        elif kind == "rename_column":
            col = _need_column(new, d["name"])
            if new.column(d["new"]) is not None and _norm(d["new"]) != _norm(d["name"]):
                raise Unpredictable("Column %s already exists." % d["new"])
            _rename_in_indexes(new, col.name, d["new"])
            col.name = d["new"]
        elif kind in ("set_default", "drop_default"):
            col = _need_column(new, d["name"])
            col.default = d.get("default") if kind == "set_default" else None
        elif kind == "column_visibility":
            _need_column(new, d["name"]).invisible = d["invisible"]
            change.column_visibility = True
        elif kind == "add_index":
            index = d["obj"]
            index = copy.deepcopy(index)
            index.origin = None
            if index.kind == "PRIMARY":
                if new.primary() is not None:
                    raise Unpredictable("The table already has a primary key.")
                for part in index.parts:
                    col = new.column(part[0])
                    if col is not None:
                        col.nullable = False  # both servers make PRIMARY KEY columns NOT NULL
            if index.name is None:
                index.name = _generated_index_name(new, index)
            elif new.index(index.name) is not None:
                raise Unpredictable("Index %s already exists." % index.name)
            for part in index.parts:
                if part[0] is not None and new.column(part[0]) is None:
                    raise _Missing(part[0])
            if index.kind == "PRIMARY":
                new.indexes.insert(0, index)
            else:
                new.indexes.append(index)
        elif kind == "add_fk":
            fk = d["obj"]
            for name in fk.columns:
                if new.column(name) is None:
                    raise _Missing(name)
            new.foreign_keys.append(copy.deepcopy(fk))
            change.add_fk = True
            if not _covered(new, fk.columns):
                # The server creates an index when none starts with the
                # foreign key's columns (sql/sql_table.cc prepare_foreign_key).
                index = _Index(fk.name or fk.columns[0], "KEY")
                index.origin = None
                index.implicit = True
                index.parts = [[name, None, False] for name in fk.columns]
                if new.index(index.name) is not None:
                    index.name = _generated_index_name(new, index)
                new.indexes.append(index)
        elif kind == "add_check":
            new.checks.append(list(d["obj"]))
            if d["obj"][1]:
                change.check_add_enforced = True
        elif kind == "drop_primary":
            primary = new.primary()
            if primary is None:
                raise Unpredictable("The table has no primary key.")
            new.indexes.remove(primary)
        elif kind == "drop_index":
            index = new.index(d["name"])
            if index is None:
                raise Unpredictable("Index %s does not exist." % d["name"])
            new.indexes.remove(index)
        elif kind == "drop_fk":
            fk = _find_fk(new, d["name"])
            new.foreign_keys.remove(fk)
            change.drop_fk = True
        elif kind in ("drop_check", "drop_constraint"):
            _drop_constraint(new, d["name"], kind, change)
        elif kind == "check_enforced":
            found = [c for c in new.checks if _norm(c[0]) == _norm(d["name"])]
            if not found:
                raise Unpredictable("Check constraint %s does not exist." % d["name"])
            if d["enforced"] and not found[0][1]:
                change.check_enforce = True
            found[0][1] = d["enforced"]
        elif kind == "rename_index":
            index = new.index(d["name"])
            if index is None:
                raise Unpredictable("Index %s does not exist." % d["name"])
            if index.kind == "PRIMARY":
                raise Unpredictable("The primary key cannot be renamed.")
            index.name = d["new"]
            index.renamed = True
            change.renamed_indexes.append((d["name"], d["new"]))
        elif kind == "index_visibility":
            index = new.index(d["name"])
            if index is None:
                raise Unpredictable("Index %s does not exist." % d["name"])
            index.visible = d["visible"]
            change.visibility_indexes.append(d["name"])
        elif kind == "index_ignored":
            index = new.index(d["name"])
            if index is None:
                raise Unpredictable("Index %s does not exist." % d["name"])
            index.ignored = d["ignored"]
            change.ignored_indexes.append(d["name"])
        elif kind == "rename_table":
            change.rename_table = True
        elif kind == "convert":
            # Every character column takes the new set; binary ones keep it.
            change.convert = d
            for col in new.columns:
                if col.is_string() and col.charset != "binary":
                    if new.collation is None:
                        raise _Fact("collation")
                    col.charset, col.collation = new.charset, new.collation
            change.options["CONVERT"] = d["charset"]
        elif kind == "force":
            change.recreate = True
        elif kind == "order_by":
            change.order_by = True
        elif kind == "options":
            opts = d["table"]
            for key, value in opts.options.items():
                change.options[key] = value
            if opts.engine is not None:
                if _norm(opts.engine) == _norm(table.engine or "InnoDB"):
                    change.recreate = True
                else:
                    change.engine_change = True
            if opts.row_format is not None:
                new.row_format = opts.row_format
            if opts.key_block_size is not None or "KEY_BLOCK_SIZE" in opts.options:
                new.key_block_size = opts.key_block_size
        elif kind in ("algorithm", "lock"):
            pass
        else:
            raise Unpredictable("Clause kind %s is outside the checked rules." % kind)
    # The server refuses to leave a foreign key without an index on its
    # columns (ER_DROP_INDEX_FK).
    for fk in new.foreign_keys:
        if not _covered(new, fk.columns):
            raise Unpredictable("An index needed by foreign key %s would be dropped; the server refuses."
                                % (fk.name or ""))
    return new, change


class _Missing(Unpredictable):
    """A clause names a column another clause of the statement adds."""


class _Fact(Unpredictable):
    """The outcome depends on a fact the definition does not show."""


def _need_column(table, name):
    col = table.column(name)
    if col is None:
        raise _Missing(name)
    return col


def _covered(table, columns):
    want = [_norm(c) for c in columns]
    for index in table.indexes:
        names = [_norm(part[0]) for part in index.parts]
        if index.kind in ("FULLTEXT", "SPATIAL") or index.functional:
            continue
        if names[:len(want)] == want:
            return True
    return False


def _find_fk(table, name):
    for fk in table.foreign_keys:
        if _norm(fk.name) == _norm(name):
            return fk
    raise Unpredictable("Foreign key %s does not exist." % name)


def _drop_constraint(table, name, kind, change):
    for check in table.checks:
        if _norm(check[0]) == _norm(name):
            table.checks.remove(check)
            return
    if kind == "drop_constraint":
        for fk in table.foreign_keys:
            if _norm(fk.name) == _norm(name):
                table.foreign_keys.remove(fk)
                change.drop_fk = True
                return
        index = table.index(name)
        if index is not None and index.kind == "UNIQUE":
            table.indexes.remove(index)
            return
    raise Unpredictable("Constraint %s does not exist." % name)


def _place(table, col, position):
    if position is None:
        table.columns.append(col)
    elif position[0] == "FIRST":
        table.columns.insert(0, col)
    else:
        after = table.column(position[1])
        if after is None:
            raise _Missing(position[1])
        table.columns.insert(table.columns.index(after) + 1, col)


def _add_column(table, col, position, engine, defaults=None):
    if table.column(col.name) is not None:
        raise Unpredictable("Column %s already exists." % col.name)
    col = copy.deepcopy(col)
    col.origin = None
    _resolve_charset(col, table.charset, table.collation, engine, defaults)
    _place(table, col, position)
    _inline_keys(table, col)


def _modify_column(table, name, col, position, engine, defaults=None):
    old = table.column(name)
    if old is None:
        raise _Missing(name)
    if col.name != old.name and table.column(col.name) is not None and _norm(col.name) != _norm(old.name):
        raise Unpredictable("Column %s already exists." % col.name)
    col = copy.deepcopy(col)
    col.origin = old.origin
    _resolve_charset(col, table.charset, table.collation, engine, defaults)
    if col.is_string() and col.collation is None:
        raise _Fact("collation")
    at = table.columns.index(old)
    table.columns.remove(old)
    if position is None:
        table.columns.insert(at, col)
    else:
        _place(table, col, position)
    if col.name != old.name:
        _rename_in_indexes(table, old.name, col.name)
    for index in table.indexes:
        if index.kind == "PRIMARY" and any(_norm(part[0]) == _norm(col.name) for part in index.parts):
            col.nullable = False
    _inline_keys(table, col)


def _rename_in_indexes(table, old, new):
    for index in table.indexes:
        for part in index.parts:
            if _norm(part[0]) == _norm(old):
                part[0] = new
    for fk in table.foreign_keys:
        fk.columns = [new if _norm(c) == _norm(old) else c for c in fk.columns]


def _drop_column(table, name, change):
    col = table.column(name)
    if col is None:
        raise _Missing(name)
    for fk in table.foreign_keys:
        if _norm(name) in [_norm(c) for c in fk.columns]:
            raise Unpredictable("Column %s is used by foreign key %s; the server refuses to drop it."
                                % (name, fk.name or ""))
    table.columns.remove(col)
    # "If columns are dropped from a table, the columns are also removed from
    # any index of which they are a part" (mysql_prepare_alter_table in both
    # servers); an index left without parts is dropped.
    for index in list(table.indexes):
        index.parts = [part for part in index.parts if _norm(part[0]) != _norm(name)]
        if not index.parts:
            table.indexes.remove(index)


# ---------------------------------------------------------------------------
# Comparing the old and the new table (fill_alter_inplace_info)


class _Diff(object):
    def __init__(self):
        self.flags = set()
        self.details = {}  # flag -> [sentence]
        self.added = []  # new columns
        self.dropped = []  # old columns
        self.pairs = []  # (old, new) columns kept
        self.added_keys = []
        self.dropped_keys = []
        self.renamed_keys = []
        self.add_fts_doc_id = False

    def flag(self, name, detail=None):
        self.flags.add(name)
        if detail:
            self.details.setdefault(name, [])
            if detail not in self.details[name]:
                self.details[name].append(detail)


def _gcol_refs(col):
    names = set()
    toks = col.gen_toks or []
    for k, tok in enumerate(toks):
        if tok.kind == "ident":
            names.add(_norm(tok.value))
        elif tok.kind == "word" and not (k + 1 < len(toks) and toks[k + 1].kind == "op" and toks[k + 1].value == "("):
            names.add(_norm(tok.text))
    return names


def _column_pairs(old, new, diff):
    for col in old.columns:
        match = [c for c in new.columns if c.origin is not None and _norm(c.origin) == _norm(col.name)]
        if match:
            diff.pairs.append((col, match[0]))
        else:
            diff.dropped.append(col)
    diff.added = [c for c in new.columns if c.origin is None]
    # A generated column that reads a dropped or renamed column would need
    # the server to evaluate it again (VIRTUAL/STORED_GCOL_REEVAL in MySQL;
    # an error or ALTER_COLUMN_VCOL in MariaDB): outside the checked rules.
    changed = {_norm(c.name) for c in diff.dropped}
    changed |= {_norm(o.name) for o, n in diff.pairs if o.name != n.name}
    for o, n in diff.pairs:
        if (o.generated or n.generated) and _gcol_refs(n) & changed:
            raise Unpredictable("Generated column %s reads a renamed or dropped column." % n.name)


def _mysql_is_equal(o, n):
    """IS_EQUAL_YES / IS_EQUAL_NO / IS_EQUAL_PACK_LENGTH of Field::is_equal
    (mysql-server sql/field.cc) for the old column *o* and the new *n*;
    returns (verdict, why)."""
    ok, nk = o.kind(), n.kind()
    if o.generated or n.generated:
        if (o.generated, _toks_text(o.gen_toks), o.type, o.length, o.decimals, o.unsigned) != \
                (n.generated, _toks_text(n.gen_toks), n.type, n.length, n.decimals, n.unsigned):
            raise Unpredictable("Changing a generated column is outside the checked rules.")
        return "YES", None
    if ok in ("char", "varchar"):
        if nk != ok:
            return "NO", "%s becomes %s" % (o.describe(), n.describe())
        # sql/field.cc:157 length_prevents_inplace, :214 charset_prevents_inplace
        if n.pack_length() < o.pack_length():
            return "NO", "its maximum length shrinks from %d to %d bytes" % (o.byte_length(), n.byte_length())
        o_row = o.byte_length() if ok == "varchar" else o.pack_length()
        if n.byte_length() >= 256 and o_row < 256:
            return "NO", ("its maximum length grows from %d to %d bytes, past the 255 bytes a 1-byte length "
                          "prefix can hold" % (o.byte_length(), n.byte_length()))
        if not (o.charset == n.charset or n.charset == "binary" or (o.charset == "utf8mb3" and
                                                                    n.charset == "utf8mb4")):
            return "NO", "its character set changes from %s to %s" % (o.charset, n.charset)
        if ok == "char":
            if n.length != o.length:
                return "NO", "CHAR changes length from %d to %d characters" % (o.length, n.length)
            return ("YES", None) if n.collation == o.collation else ("PACK", "its collation changes")
        if n.collation == o.collation and n.pack_length() == o.pack_length():
            return "YES", None
        return "PACK", None
    if ok == "blob":
        if n.type != o.type:
            return "NO", "%s becomes %s" % (o.describe(), n.describe())
        if not (o.charset == n.charset or n.charset == "binary" or (o.charset == "utf8mb3" and
                                                                    n.charset == "utf8mb4")):
            return "NO", "its character set changes from %s to %s" % (o.charset, n.charset)
        return ("YES", None) if n.collation == o.collation else ("PACK", None)
    if ok in ("enum", "set"):
        # sql/field.cc:8570 Field_enum::is_equal
        if nk != ok:
            return "NO", "%s becomes %s" % (o.describe(), n.describe())
        if len(o.members) > len(n.members) or not _members_prefix(o, n):
            return "NO", "existing %s values are removed, reordered or renamed" % ok.upper()
        if n.pack_length() != o.pack_length():
            return "NO", "its storage grows from %d to %d bytes" % (o.pack_length(), n.pack_length())
        return "YES", None
    if ok in ("int", "real", "year"):
        # sql/field.cc:8685 Field_num::is_equal
        if n.type != o.type or n.unsigned != o.unsigned or n.auto_increment != o.auto_increment:
            return "NO", "%s becomes %s" % (_num_text(o), _num_text(n))
        return "YES", None
    if ok == "decimal":
        if (n.type, n.length, n.decimals, n.unsigned, n.auto_increment) != \
                (o.type, o.length, o.decimals, o.unsigned, o.auto_increment):
            return "NO", "%s becomes %s" % (_num_text(o), _num_text(n))
        return "YES", None
    if ok == "temporal":
        if n.type != o.type or (n.decimals or 0) != (o.decimals or 0):
            return "NO", "%s becomes %s" % (o.describe(), n.describe())
        return "YES", None
    if ok == "bit":
        return ("YES", None) if (n.type, n.length) == (o.type, o.length) else \
            ("NO", "%s becomes %s" % (o.describe(), n.describe()))
    if ok == "json":
        return ("YES", None) if n.type == o.type else ("NO", "JSON becomes %s" % n.describe())
    if ok == "geometry":
        return ("YES", None) if n.type == o.type else ("NO", "%s becomes %s" % (o.describe(), n.describe()))
    raise Unpredictable("Changing a %s column is outside the checked rules." % o.describe())


def _num_text(col):
    return col.describe() + (" unsigned" if col.unsigned else "") + (" auto_increment" if col.auto_increment else "")


def _toks_text(toks):
    return " ".join(t.text for t in toks or [])


def _members_prefix(o, n):
    ci = (n.collation or "").endswith("_ci")
    for a, b in zip(o.members, n.members):
        if (a.lower(), b.lower())[0] != (a.lower(), b.lower())[1] if ci else a != b:
            return False
    return True


def _mysql_diff(old, new, change, facts):
    """mysql-server sql/sql_table.cc:12985 fill_alter_inplace_info."""
    diff = _Diff()
    _column_pairs(old, new, diff)
    kinds = change.kinds
    if kinds & {"modify_column", "rename_column", "set_default", "drop_default"}:
        diff.flag("ALTER_COLUMN_DEFAULT")
    if change.add_fk:
        diff.flag("ADD_FOREIGN_KEY")
    if change.drop_fk:
        diff.flag("DROP_FOREIGN_KEY")
    if change.options or change.convert:
        names = sorted("CONVERT TO CHARACTER SET" if k == "CONVERT" else "DEFAULT CHARSET" if k == "CHARSET"
                       else k for k in change.options)
        diff.flag("CHANGE_CREATE_OPTION", "table option %s" % ", ".join(names))
    if change.rename_table:
        diff.flag("ALTER_RENAME")
    if change.recreate:
        diff.flag("RECREATE_TABLE")
    if "add_check" in kinds:
        diff.flag("ADD_CHECK_CONSTRAINT")
    if kinds & {"drop_check", "drop_constraint"}:
        diff.flag("DROP_CHECK_CONSTRAINT")
    if "check_enforced" in kinds:
        diff.flag("SUSPEND_CHECK_CONSTRAINT")
    if change.column_visibility:
        diff.flag("ALTER_COLUMN_VISIBILITY")
    new_keys = new.indexes
    old_stored = [c for c in old.columns if c.generated != "VIRTUAL"]
    new_stored = [c for c in new.columns if c.generated != "VIRTUAL"]
    old_virtual = [c for c in old.columns if c.generated == "VIRTUAL"]
    new_virtual = [c for c in new.columns if c.generated == "VIRTUAL"]
    for o, n in diff.pairs:
        verdict, why = _mysql_is_equal(o, n)
        if verdict == "NO":
            diff.flag("ALTER_VIRTUAL_COLUMN_TYPE" if o.generated == "VIRTUAL" else "ALTER_STORED_COLUMN_TYPE",
                      "column %s: %s" % (n.name, why or "%s becomes %s" % (o.describe(), n.describe())))
        elif verdict == "PACK":
            indexed = any(_norm(part[0]) == _norm(n.name) for index in new_keys for part in index.parts)
            if o.collation != n.collation and indexed:
                diff.flag("ALTER_STORED_COLUMN_TYPE",
                          "column %s: its collation changes (%s to %s) and an index uses it"
                          % (n.name, o.collation, n.collation))
            else:
                diff.flag("ALTER_COLUMN_EQUAL_PACK_LENGTH", _pack_detail(o, n))
        if o.name != n.name:
            diff.flag("ALTER_COLUMN_NAME", "column %s is renamed to %s" % (o.name, n.name))
        if o.nullable != n.nullable:
            diff.flag("ALTER_COLUMN_NULLABLE" if n.nullable else "ALTER_COLUMN_NOT_NULLABLE",
                      "column %s becomes %s" % (n.name, "NULL" if n.nullable else "NOT NULL"))
        if o.generated != "VIRTUAL":
            if old_stored.index(o) != new_stored.index(n):
                diff.flag("ALTER_STORED_COLUMN_ORDER", "column %s moves" % n.name)
        elif old_virtual.index(o) != new_virtual.index(n):
            diff.flag("ALTER_VIRTUAL_COLUMN_ORDER", "virtual column %s moves" % n.name)
        if o.srid != n.srid and n.srid is not None:
            change.srid = True
    for col in diff.dropped:
        if col.generated == "VIRTUAL":
            diff.flag("DROP_VIRTUAL_COLUMN", "virtual column %s is dropped" % col.name)
        else:
            diff.flag("DROP_STORED_COLUMN", "column %s is dropped" % col.name)
    for col in diff.added:
        if col.generated == "VIRTUAL":
            diff.flag("ADD_VIRTUAL_COLUMN", "virtual column %s is added" % col.name)
        elif col.generated or col.default == "expr":
            diff.flag("ADD_STORED_GENERATED_COLUMN",
                      "column %s is %s" % (col.name, "a stored generated column" if col.generated else
                                           "added with an expression DEFAULT"))
        else:
            diff.flag("ADD_STORED_BASE_COLUMN", "column %s is added" % col.name)
    _mysql_keys(old, new, change, diff)
    return diff


def _pack_detail(o, n):
    if o.kind() == "varchar" and o.byte_length() != n.byte_length():
        return ("column %s: %s to %s keeps the same length prefix (%d to %d bytes)"
                % (n.name, o.describe(), n.describe(), o.byte_length(), n.byte_length()))
    if o.charset != n.charset:
        return "column %s: %s data is valid %s, so only metadata changes" % (n.name, o.charset, n.charset)
    return "column %s: only its collation changes" % n.name


def _mysql_key_changed(old, new, okey, nkey, change, diff):
    """sql/sql_table.cc:12859 has_index_def_changed."""
    if okey.kind != nkey.kind or len(okey.parts) != len(nkey.parts) or okey.functional or nkey.functional:
        return True
    if (okey.comment or "") != (nkey.comment or ""):
        diff.flag("ALTER_INDEX_COMMENT")
    only_change_column = change.kinds <= {"modify_column", "rename_column"}
    for opart, npart in zip(okey.parts, nkey.parts):
        ocol = old.column(opart[0])
        ncol = new.column(npart[0])
        if ocol is None or ncol is None or ncol.origin is None or _norm(ncol.origin) != _norm(ocol.name):
            return True
        olen = ocol.key_bytes(opart[1])
        nlen = ncol.key_bytes(npart[1])
        if olen != nlen:
            verdict, _ = _mysql_is_equal(ocol, ncol)
            if only_change_column and verdict == "PACK":
                diff.flag("ALTER_COLUMN_INDEX_LENGTH")
            else:
                return True
        if opart[2] != npart[2]:
            return True
        if not ncol.kind() == "blob" and okey.kind != "FULLTEXT":
            old_prefix = bool(opart[1]) and ocol.key_bytes(None) != olen
            new_prefix = bool(npart[1]) and ncol.key_bytes(None) != nlen
            if old_prefix != new_prefix:
                return True
    return False


def _mysql_keys(old, new, change, diff):
    renamed_new = {_norm(b) for a, b in change.renamed_indexes}
    renamed_old = {_norm(a) for a, b in change.renamed_indexes}
    for a, b in change.renamed_indexes:
        okey, nkey = old.index(a), new.index(b)
        if okey is None or nkey is None:
            continue
        if not _mysql_key_changed(old, new, okey, nkey, change, diff):
            diff.flag("RENAME_INDEX", "index %s is renamed to %s" % (a, b))
        else:
            diff.dropped_keys.append(okey)
            diff.added_keys.append(nkey)
    for name in change.visibility_indexes:
        diff.flag("RENAME_INDEX", "index %s changes visibility" % name)
    for okey in old.indexes:
        if _norm(okey.name) in renamed_old:
            continue
        nkey = None
        for k in new.indexes:
            if k.name == okey.name:  # find_key_cs
                nkey = k
        if nkey is None:
            diff.dropped_keys.append(okey)
        elif _mysql_key_changed(old, new, okey, nkey, change, diff):
            diff.dropped_keys.append(okey)
            diff.added_keys.append(nkey)
    for nkey in new.indexes:
        if _norm(nkey.name) in renamed_new:
            continue
        if not any(k.name == nkey.name for k in old.indexes):
            diff.added_keys.append(nkey)
    old_primary = old.clustered()
    candidates = sum(1 for k in old.indexes if k is old_primary or old.candidate(k))
    for okey in diff.dropped_keys:
        if okey.unique():
            if okey is old_primary:
                diff.flag("DROP_PK_INDEX", "the primary key %s is dropped" % okey.name)
                candidates -= 1
            else:
                diff.flag("DROP_UNIQUE_INDEX", "unique index %s is dropped" % okey.name)
                if old.candidate(okey):
                    candidates -= 1
        else:
            diff.flag("DROP_INDEX", "index %s is dropped" % okey.name)
    for nkey in sorted(diff.added_keys, key=lambda k: new.indexes.index(k)):
        if nkey.unique():
            if new.candidate(nkey) or nkey.kind == "PRIMARY":
                if candidates == 0 or nkey.kind == "PRIMARY":
                    diff.flag("ADD_PK_INDEX", "index %s becomes the clustered (primary) key" % nkey.name)
                else:
                    diff.flag("ADD_UNIQUE_INDEX", "unique index %s is added" % nkey.name)
                candidates += 1
            else:
                diff.flag("ADD_UNIQUE_INDEX", "unique index %s is added" % nkey.name)
        elif nkey.kind == "SPATIAL":
            diff.flag("ADD_SPATIAL_INDEX", "SPATIAL index %s is added" % nkey.name)
        else:
            diff.flag("ADD_INDEX", "%sindex %s is added" % ("FULLTEXT " if nkey.kind == "FULLTEXT" else "",
                                                             nkey.name))


# MySQL handler flag sets (storage/innobase/handler/handler0alter.cc:123-180)
_M_ONLINE_CREATE = {"ADD_INDEX", "ADD_UNIQUE_INDEX", "ADD_SPATIAL_INDEX"}
_M_REBUILD = {"ADD_PK_INDEX", "DROP_PK_INDEX", "CHANGE_CREATE_OPTION", "ALTER_COLUMN_NULLABLE",
              "ALTER_COLUMN_NOT_NULLABLE", "ALTER_STORED_COLUMN_ORDER", "DROP_STORED_COLUMN",
              "ADD_STORED_BASE_COLUMN", "RECREATE_TABLE"}
_M_IGNORE = {"ALTER_COLUMN_DEFAULT", "ALTER_COLUMN_COLUMN_FORMAT", "ALTER_COLUMN_STORAGE_TYPE", "ALTER_RENAME",
             "CHANGE_INDEX_OPTION", "ADD_CHECK_CONSTRAINT", "DROP_CHECK_CONSTRAINT", "SUSPEND_CHECK_CONSTRAINT",
             "ALTER_COLUMN_VISIBILITY", "ALTER_COLUMN_MASKING"}
_M_INSTANT_ALLOWED = {"ALTER_COLUMN_NAME", "ADD_VIRTUAL_COLUMN", "DROP_VIRTUAL_COLUMN", "ALTER_VIRTUAL_COLUMN_ORDER",
                      "ADD_STORED_BASE_COLUMN", "ALTER_STORED_COLUMN_ORDER", "DROP_STORED_COLUMN"}
_M_FOREIGN = {"DROP_FOREIGN_KEY", "ADD_FOREIGN_KEY"}
_M_NOREBUILD = _M_ONLINE_CREATE | _M_FOREIGN | {
    "DROP_INDEX", "DROP_UNIQUE_INDEX", "RENAME_INDEX", "ALTER_COLUMN_NAME", "ALTER_COLUMN_EQUAL_PACK_LENGTH",
    "ALTER_INDEX_COMMENT", "ADD_VIRTUAL_COLUMN", "DROP_VIRTUAL_COLUMN", "ALTER_VIRTUAL_COLUMN_ORDER",
    "ALTER_COLUMN_INDEX_LENGTH"}


def _v_col_in_order(old, new, diff, engine):
    """check_v_col_in_order (both servers): a new virtual column comes after
    every existing one, and existing virtual columns keep their order."""
    flags = {f[6:] if f.startswith("ALTER_ADD_") or f.startswith("ALTER_DROP_") else f for f in diff.flags}
    if "ADD_VIRTUAL_COLUMN" in flags:
        seen_new = False
        for col in new.columns:
            if col.generated != "VIRTUAL":
                if engine == "mysql" and seen_new and col.auto_increment:
                    return False
                continue
            if col.origin is None:
                seen_new = True
            elif seen_new:
                return False
    if "ALTER_VIRTUAL_COLUMN_ORDER" not in flags:
        return True
    kept_old = [c.name for c in old.columns if c.generated == "VIRTUAL" and c not in diff.dropped]
    kept_new = [c.origin for c in new.columns if c.generated == "VIRTUAL" and c.origin is not None]
    return [_norm(x) for x in kept_old] == [_norm(x) for x in kept_new]


def _uses_added(new, diff):
    """Index parts on columns this statement adds: (autoinc, virtual)."""
    added = {_norm(c.name): c for c in diff.added}
    autoinc = virtual = False
    for key in new.indexes:
        for part in key.parts:
            col = added.get(_norm(part[0]))
            if col is None:
                continue
            if col.auto_increment:
                autoinc = True
            if col.generated == "VIRTUAL":
                virtual = True
    return autoinc, virtual


def _row_bytes(table):
    """An upper bound of the clustered index record size, following
    get_field_max_size (mysql-server storage/innobase/dict/dict0dict.cc:2111):
    columns longer than 255 bytes or BLOBs may live off-page (40 bytes with
    DYNAMIC/COMPRESSED, a 768-byte prefix plus 20 with COMPACT/REDUNDANT)."""
    dynamic = (table.row_format or "DYNAMIC").upper() in ("DYNAMIC", "COMPRESSED", "DEFAULT")
    clustered = table.clustered()
    key_names = {_norm(part[0]) for part in clustered.parts} if clustered else set()
    size = 5 + (len(table.columns) + 7) // 8 + 13 + (0 if clustered else 6)
    for col in table.columns:
        if col.generated == "VIRTUAL":
            continue
        kind = col.kind()
        if kind in ("char", "varchar"):
            longest = col.byte_length()
        elif kind in ("blob", "json", "geometry"):
            longest = 1 << 32
        else:
            longest = col.pack_length() or 0
        big = kind in ("blob", "json", "geometry") or longest > 255
        if _norm(col.name) in key_names:
            size += longest + 2
        elif big:
            size += (40 + 1) if dynamic else (768 + 20 + 2)
        else:
            size += longest + (1 if kind in ("varchar", "char") else 0)
    return size


def _page_rec_max(page_size):
    """page_get_free_space_of_empty(comp) / 2 (storage/innobase/include/
    page0page.ic:816; dict0dict.cc:2077 get_permissible_max_size)."""
    page = page_size or 16384
    if page >= 65536:
        return 16383
    return (page - 132) // 2


# Fraction of the record limit an instant ADD may reach in this model; the
# estimate is an upper bound but cannot see columns dropped earlier, which
# InnoDB still counts, so the model stays well below the limit.
_ROW_MARGIN = 0.75


def _row_size_unknown(table, facts, before=None):
    limit = _page_rec_max(facts.get("innodb_page_size"))
    size = max(_row_bytes(table), _row_bytes(before) if before is not None else 0)
    if size > limit * _ROW_MARGIN:
        return ("the widest possible row (about %d bytes) is close to InnoDB's %d-byte record limit, where "
                "the server stops adding columns instantly" % (size, limit))
    return None


def _mysql_gate(version):
    v = version
    return {
        # MAX_ROW_VERSION: 64 through mysql-9.0.1 (storage/innobase/include/
        # dict0mem.h:323 at mysql-8.4.0), 255 from mysql-9.1.0 (rem0types.h:46).
        "max_row_version": 255 if v >= (9, 1, 0) else 64,
        # handler0alter.cc:1058-1063 "No records: prefer INPLACE": present in
        # mysql-8.4.4 and mysql-9.2.0, absent in 8.4.3 and 9.1.0.
        "empty_table_inplace": v >= (9, 2, 0) or ((8, 4, 4) <= v < (9, 0, 0)),
    }


def _strict(facts):
    mode = facts.get("sql_mode")
    if mode is None:
        return True
    mode = mode.upper()
    return "STRICT_TRANS_TABLES" in mode or "STRICT_ALL_TABLES" in mode


def _mysql_decide(old, new, change, diff, facts, gate, requested):
    F = diff.flags
    d = diff.details
    # -- the server layer -----------------------------------------------------------
    simple_rename = change.kinds <= {"rename_table", "algorithm", "lock"} and "rename_table" in change.kinds
    if simple_rename and requested.get("algorithm") != COPY:
        return _Outcome(INSTANT, False, True, "mysql.rename_table")
    if requested.get("algorithm") == COPY:
        return _Outcome(COPY, True, False, "mysql.copy_requested")
    if change.order_by:
        return _Outcome(COPY, True, False, "mysql.copy_server", ["ORDER BY always copies the table"])
    if change.check_add_enforced or change.check_enforce:
        return _Outcome(COPY, True, False, "mysql.copy_check")
    if change.engine_change:
        return _Outcome(COPY, True, False, "mysql.copy_server", ["ENGINE changes the storage engine"])
    if getattr(change, "srid", False):
        return _Outcome(COPY, True, False, "mysql.copy_server", ["a geometry column changes its SRID"])
    if facts.get("old_alter_table") and requested.get("algorithm") not in (INPLACE, INSTANT):
        return _Outcome(COPY, True, False, "mysql.copy_old_alter_table")
    if not F:
        return _Outcome(INSTANT, False, True, "mysql.no_change")
    # -- InnoDB: check_if_supported_inplace_alter -----------------------------------------
    outside = F - _M_IGNORE - _M_NOREBUILD - _M_REBUILD
    if outside:
        if "ADD_STORED_GENERATED_COLUMN" in outside:
            return _Outcome(COPY, True, False, "mysql.copy_generated", d.get("ADD_STORED_GENERATED_COLUMN"))
        if "ALTER_STORED_COLUMN_TYPE" in outside or "ALTER_VIRTUAL_COLUMN_TYPE" in outside:
            return _Outcome(COPY, True, False, "mysql.copy_column_type",
                            d.get("ALTER_STORED_COLUMN_TYPE", []) + d.get("ALTER_VIRTUAL_COLUMN_TYPE", []))
        return _Outcome(COPY, True, False, "mysql.copy_flags", sorted(outside))
    if "ADD_FOREIGN_KEY" in F and facts.get("foreign_key_checks", True):
        return _Outcome(COPY, True, False, "mysql.copy_fk_checks")
    # innobase_support_instant (handler0alter.cc:829)
    active = F - _M_IGNORE
    old_fts = bool(old.fulltext()) or facts.get("fts_doc_id_index") is True
    instant = None
    instant_rule = None
    if not active:
        instant = "NO_CHANGE"
    elif not active - _M_INSTANT_ALLOWED:
        if active == {"ALTER_COLUMN_NAME"}:
            refs = facts.get("referenced_columns")
            renamed = [o.name for o, n in diff.pairs if o.name != n.name]
            if any(c.name.upper() == "FTS_DOC_ID" for c in old.columns if c.name in renamed) and old.fulltext():
                instant = None
            elif refs is None:
                return _unknown("unknown.fact", [
                    "Renaming a column is instant unless another table's foreign key references it; then "
                    "InnoDB renames it in place (no rebuild). myflames could not read which columns other "
                    "tables reference (information_schema.KEY_COLUMN_USAGE)."], needs=["referenced_columns"])
            elif {_norm(r) for r in refs} & {_norm(r) for r in renamed}:
                instant_rule = "mysql.rename_referenced_column"
            else:
                instant = "RENAME"
        elif not active - {"ADD_VIRTUAL_COLUMN", "DROP_VIRTUAL_COLUMN"}:
            if _v_col_in_order(old, new, diff, "mysql"):
                instant = "VIRTUAL"
        elif not active - {"ADD_VIRTUAL_COLUMN", "DROP_VIRTUAL_COLUMN", "ALTER_COLUMN_NAME"}:
            instant = None  # VIRTUAL_ADD_DROP_WITH_RENAME: not supported (handler0alter.cc:893-895)
        elif ("ADD_STORED_BASE_COLUMN" in active and "DROP_VIRTUAL_COLUMN" not in active) or \
                ("DROP_STORED_COLUMN" in active and _v_col_in_order(old, new, diff, "mysql")):
            if new.compressed() or old.compressed():
                instant_rule = "mysql.no_instant_compressed"
            elif old_fts:
                instant_rule = "mysql.no_instant_fulltext"
            elif requested.get("algorithm") == INPLACE:
                instant = "ADD_DROP"  # rebuilt in place whatever the hidden state (handler0alter.cc:1053-1056)
            else:
                missing = [name for name in ("fts_doc_id_index", "total_row_versions") if facts.get(name) is None]
                if any(c.name.upper() == "FTS_DOC_ID" for c in old.columns):
                    missing = [name for name in missing if name != "fts_doc_id_index"]
                if missing:
                    return _unknown("unknown.fact", [
                        "Adding or dropping a column is instant unless the table keeps a hidden FTS_DOC_ID column "
                        "from an earlier FULLTEXT index or has used all %d row versions; then MySQL rebuilds it "
                        "in place. myflames could not read information_schema.INNODB_TABLES and INNODB_INDEXES "
                        "(they need the PROCESS privilege)." % gate["max_row_version"]], needs=missing)
                instant = "ADD_DROP"
    if instant == "ADD_DROP":
        added = len([c for c in diff.added if c.generated != "VIRTUAL"])
        why = None
        if requested.get("algorithm") == INPLACE:
            instant, instant_rule = None, "mysql.instant_requested_inplace"
        elif len(old.columns) + added > 1000:
            return _unknown("unknown.fact", ["the table is close to InnoDB's column limit"])
        else:
            versions = facts.get("total_row_versions")
            if versions + 1 > gate["max_row_version"]:
                instant, instant_rule = None, "mysql.row_versions_exhausted"
                why = "the table already has %d row versions" % versions
            else:
                # dict0inst.cc:42-71 checks the current table, then the added columns.
                why = _row_size_unknown(new, facts, old)
                if why:
                    return _unknown("unknown.fact", [why])
        if instant == "ADD_DROP":
            return _Outcome(INSTANT, False, True, "mysql.instant_add_drop")
        if why:
            d.setdefault("_why", []).append(why)
    elif instant in ("NO_CHANGE", "VIRTUAL", "RENAME"):
        rule = {"NO_CHANGE": "mysql.no_change", "VIRTUAL": "mysql.instant_virtual",
                "RENAME": "mysql.instant_rename_column"}[instant]
        if requested.get("algorithm") == INPLACE and instant != "NO_CHANGE":
            pass  # the server runs an instant change even when INPLACE is requested (sql_table.cc:18495-18510)
        return _Outcome(INSTANT, False, True, rule)
    # -- in place -----------------------------------------------------------------------
    if "ALTER_COLUMN_NOT_NULLABLE" in F and not _strict(facts):
        return _Outcome(COPY, True, False, "mysql.copy_not_null_nonstrict")
    if "DROP_PK_INDEX" in F and "ADD_PK_INDEX" not in F:
        return _Outcome(COPY, True, False, "mysql.copy_drop_pk")
    if "ALTER_COLUMN_NULLABLE" in F and new.clustered() is None and old.clustered() is not None:
        return _Outcome(COPY, True, False, "mysql.copy_nullable_clustered")
    add_drop_v = bool(F & {"ADD_VIRTUAL_COLUMN", "DROP_VIRTUAL_COLUMN", "ALTER_VIRTUAL_COLUMN_ORDER"})
    if add_drop_v:
        rest = F - {"ADD_VIRTUAL_COLUMN", "DROP_VIRTUAL_COLUMN", "ALTER_VIRTUAL_COLUMN_ORDER", "ADD_INDEX",
                    "DROP_INDEX"}
        if rest or not _v_col_in_order(old, new, diff, "mysql"):
            return _Outcome(COPY, True, False, "mysql.copy_virtual_mix", sorted(rest))
    online, lock_rule = True, None
    autoinc, virtual = _uses_added(new, diff)
    if new.fulltext() and add_drop_v:
        # handler0alter.cc:1196-1203: only a hidden FTS_DOC_ID (DICT_TF2_FTS_HAS_DOC_ID,
        # dict0dd.cc) lets a FULLTEXT table add or drop virtual columns in place.
        visible = any(c.name.upper() == "FTS_DOC_ID" for c in old.columns)
        hidden = not visible and (bool(old.fulltext()) or facts.get("fts_doc_id_index") is True)
        if not hidden:
            if not visible and facts.get("fts_doc_id_index") is None:
                return _unknown("unknown.fact", ["whether the table keeps a hidden FTS_DOC_ID column"],
                                needs=["fts_doc_id_index"])
            return _Outcome(COPY, True, False, "mysql.copy_virtual_mix")
    if old.fulltext() and new.fulltext():
        # handler0alter.cc:1286-1316: FTS_DOC_ID_INDEX and the FTS_DOC_ID column stay.
        touched = {_norm(c.name) for c in diff.dropped} | {_norm(o.name) for o, n in diff.pairs if o.name != n.name}
        if "fts_doc_id" in touched or any(_norm(k.name) == "fts_doc_id_index" for k in diff.dropped_keys):
            return _Outcome(COPY, True, False, "mysql.copy_flags", ["FTS_DOC_ID is renamed or dropped"])
    if autoinc:
        online, lock_rule = False, "mysql.lock_autoinc"
    if virtual:
        if "DROP_VIRTUAL_COLUMN" in F:
            return _Outcome(COPY, True, False, "mysql.copy_virtual_mix")
        online, lock_rule = False, lock_rule or "mysql.lock_virtual_index"
    if "ADD_SPATIAL_INDEX" in F:
        online, lock_rule = False, lock_rule or "mysql.lock_spatial"
    rebuild = _mysql_need_rebuild(F, change)
    fulltext_added = [k for k in diff.added_keys if k.kind == "FULLTEXT"]
    if online:
        if ("ADD_PK_INDEX" in F or rebuild) and (new.fulltext() or new.spatial()):
            online = False
            if old.fulltext() or facts.get("fts_doc_id_index") is True:
                return _Outcome(COPY, True, False, "mysql.copy_fulltext_rebuild")
            if new.fulltext() and facts.get("fts_doc_id_index") is None:
                return _unknown("unknown.fact", [
                    "the table may keep a hidden FTS_DOC_ID column from an earlier FULLTEXT index, which makes "
                    "MySQL copy instead of rebuilding in place"], needs=["fts_doc_id_index"])
            lock_rule = "mysql.inplace_fulltext" if new.fulltext() else "mysql.lock_spatial"
        elif fulltext_added:
            online, lock_rule = False, "mysql.inplace_fulltext"
    if fulltext_added and not old.fulltext() and not any(c.name.upper() == "FTS_DOC_ID" for c in new.columns):
        hidden = facts.get("fts_doc_id_index")
        if hidden is None:
            return _unknown("unknown.fact", [
                "The first FULLTEXT index adds a hidden FTS_DOC_ID column and rebuilds the table, unless one "
                "is left from an earlier FULLTEXT index. myflames could not read information_schema."
                "INNODB_INDEXES (it needs the PROCESS privilege)."], needs=["fts_doc_id_index"])
        if not hidden:
            rebuild = True
            diff.add_fts_doc_id = True
    if "ADD_PK_INDEX" in F:
        rebuild = True
    if len(fulltext_added) > 1:
        # prepare_inplace_alter_table_dict (handler0alter.cc:4506-4509):
        # ER_INNODB_FT_LIMIT, "InnoDB presently supports one FULLTEXT index
        # creation at a time"; only ALGORITHM=COPY adds two at once.
        return _Outcome(None, None, None, "unknown.refused", [
            "MySQL adds one FULLTEXT index per statement in place and refuses two (error 1795); "
            "use separate statements or ALGORITHM=COPY"])
    rule = lock_rule if lock_rule else ("mysql.inplace_rebuild" if rebuild else "mysql.inplace")
    if instant_rule and not lock_rule:
        rule = instant_rule
    elif not lock_rule and "CHANGE_CREATE_OPTION" in F and \
            (not rebuild or not (F & (_M_REBUILD - {"CHANGE_CREATE_OPTION"}))):
        rule = "mysql.inplace_options"
    details = []
    for flag in sorted(F):
        details.extend(d.get(flag, []))
    details.extend(d.get("_why", []))
    return _Outcome(INPLACE, rebuild, online, rule, details)


def _mysql_need_rebuild(F, change):
    """handler0alter.cc:930 innobase_need_rebuild (after the instant check)."""
    active = F - _M_IGNORE
    if active == {"CHANGE_CREATE_OPTION"} and not set(change.options) & {"ROW_FORMAT", "KEY_BLOCK_SIZE",
                                                                         "TABLESPACE"}:
        return False
    return bool(F & _M_REBUILD)


# -- MariaDB ------------------------------------------------------------------------------


def _collation_suffix(collation, charset):
    """Charset::collation_specific_name (sql/sql_type.cc): the collation
    name without its character set prefix."""
    collation = collation or ""
    if charset and collation.startswith(charset):
        return collation[len(charset):]
    return None


def _reinterpret(o, n):
    """Charset::encoding_allows_reinterpret_as (sql/sql_type.cc:9775)."""
    return o.charset == n.charset or (o.charset == "utf8mb3" and n.charset == "utf8mb4")


def _in_primary(table, col):
    primary = table.clustered()
    return primary is not None and any(_norm(p[0]) == _norm(col.name) for p in primary.parts) and \
        primary.kind == "PRIMARY"


def _in_key_prefix(table, col):
    for index in table.indexes:
        for part in index.parts:
            if _norm(part[0]) == _norm(col.name) and part[1] and col.key_bytes(part[1]) != col.key_bytes(None):
                return True
    return False


def _in_any_key(table, col):
    return any(_norm(part[0]) == _norm(col.name) for index in table.indexes for part in index.parts)


def _mariadb_is_equal(o, n):
    """Field::is_equal (mariadb-server sql/field.cc) as a bool."""
    ok, nk = o.kind(), n.kind()
    if nk != ok:
        return False
    if ok == "char":
        return n.length == o.length and n.collation == o.collation and n.byte_length() == o.byte_length()
    if ok == "varchar":
        return n.byte_length() == o.byte_length() and n.length == o.length and n.collation == o.collation and \
            n.compressed == o.compressed
    if ok == "blob":
        return n.type == o.type and n.compressed == o.compressed and n.collation == o.collation
    if ok in ("enum", "set"):
        return n.collation == o.collation and n.pack_length() == o.pack_length() and \
            len(o.members) <= len(n.members) and _members_prefix(o, n)
    if ok in ("int", "real", "year"):
        return n.unsigned == o.unsigned and not (n.auto_increment and not o.auto_increment) and n.type == o.type
    if ok == "decimal":
        return (n.length, n.decimals, n.unsigned) == (o.length, o.decimals, o.unsigned) and \
            not (n.auto_increment and not o.auto_increment)
    if ok == "temporal":
        return n.type == o.type and (n.decimals or 0) == (o.decimals or 0)
    if ok == "bit":
        return n.length == o.length
    if ok == "geometry":
        return n.type == o.type or n.type == "GEOMETRY"  # sql/sql_type_geom.cc:951 supertype
    if ok == "json":
        return True
    raise Unpredictable("Changing a %s column is outside the checked rules." % o.describe())


def _mariadb_nocopy(old, o, n):
    """ha_innobase::can_convert_nocopy (ha_innodb.cc:20941); (ok, why)."""
    redundant = (old.row_format or "").upper() == "REDUNDANT"
    ok = o.kind()
    if ok == "char":
        # can_convert_string (ha_innodb.cc:20800)
        if n.kind() != "char" or n.length != o.length:
            return False, "CHAR changes length from %s to %s characters" % (o.length, n.length)
        if n.byte_length() != o.byte_length() and (not not_redundant(old) or o.charset in _FIXED_WIDTH):
            return False, "its byte length changes"
        if n.collation != o.collation:
            return _mariadb_collation_nocopy(old, o, n)
        return True, None
    if ok == "varchar":
        # can_convert_varstring (ha_innodb.cc:20844)
        if n.kind() != "varchar":
            return False, "%s becomes %s" % (o.describe(), n.describe())
        if n.byte_length() < o.byte_length() or n.length < o.length:
            return False, "its maximum length shrinks from %d to %d bytes" % (o.byte_length(), n.byte_length())
        if n.compressed != o.compressed:
            return False, "its compression changes"
        enlarge = o.byte_length() <= 127 or n.byte_length() <= 255 or o.byte_length() > 255 or redundant
        if n.collation != o.collation:
            if not enlarge:
                return False, _enlarge_why(o, n)
            return _mariadb_collation_nocopy(old, o, n)
        if n.byte_length() != o.byte_length():
            if not enlarge:
                return False, _enlarge_why(o, n)
            return True, ("column %s: %s to %s (%d to %d bytes) keeps InnoDB's length encoding"
                          % (n.name, o.describe(), n.describe(), o.byte_length(), n.byte_length()))
        return True, None
    if ok == "blob":
        if n.type != o.type or n.compressed != o.compressed:
            return False, "%s becomes %s" % (o.describe(), n.describe())
        if n.collation != o.collation:
            if not _reinterpret(o, n):
                return False, "its character set changes from %s to %s" % (o.charset, n.charset)
            if _collation_suffix(o.collation, o.charset) != _collation_suffix(n.collation, n.charset):
                return (not _in_any_key(old, o)), "its collation changes"
            if _in_key_prefix(old, o):
                return False, "a prefix index uses it"
        return True, None
    return False, "%s becomes %s" % (_num_text(o) if o.kind() in ("int", "decimal", "real") else o.describe(),
                                     _num_text(n) if n.kind() in ("int", "decimal", "real") else n.describe())


def not_redundant(table):
    return (table.row_format or "").upper() != "REDUNDANT"


def _enlarge_why(o, n):
    return ("its maximum length grows from %d to %d bytes; InnoDB stores lengths of 128-255 byte columns in "
            "one byte, so it must convert every row" % (o.byte_length(), n.byte_length()))


def _mariadb_collation_nocopy(old, o, n):
    if not _reinterpret(o, n):
        return False, "its character set changes from %s to %s" % (o.charset, n.charset)
    if _collation_suffix(o.collation, o.charset) != _collation_suffix(n.collation, n.charset):
        if _in_primary(old, o):
            return False, "its collation changes and it is part of the primary key"
        return True, "column %s: only its collation changes" % n.name
    if _in_key_prefix(old, o):
        return False, "its character set changes and a prefix index uses it"
    return True, "column %s: %s data is valid %s, so only metadata changes" % (n.name, o.charset, n.charset)


def _mariadb_diff(old, new, change, facts):
    """mariadb-server sql/sql_table.cc:7044 fill_alter_inplace_info."""
    diff = _Diff()
    _column_pairs(old, new, diff)
    kinds = change.kinds
    if "modify_column" in kinds or kinds & {"set_default", "drop_default"}:
        diff.flag("ALTER_COLUMN_DEFAULT")
    if change.add_fk:
        diff.flag("ALTER_ADD_FOREIGN_KEY")
    if change.drop_fk:
        diff.flag("ALTER_DROP_FOREIGN_KEY")
    # CONVERT TO sets only ALTER_CONVERT_TO (sql/sql_lex.h:4720
    # add_alter_list_item_convert_to_charset); table options set
    # ALTER_OPTIONS (sql/sql_yacc.yy:8278-8282).
    if set(change.options) - {"CONVERT"}:
        diff.flag("ALTER_OPTIONS")
    if change.convert:
        diff.flag("ALTER_CONVERT_TO")
    if change.rename_table:
        diff.flag("ALTER_RENAME")
    if change.recreate:
        diff.flag("ALTER_RECREATE")
    if "add_check" in kinds:
        diff.flag("ALTER_ADD_CHECK_CONSTRAINT")
    if kinds & {"drop_check", "drop_constraint"}:
        diff.flag("ALTER_DROP_CHECK_CONSTRAINT")
    if change.ignored_indexes:
        diff.flag("ALTER_INDEX_IGNORABILITY")
    if "check_enforced" in kinds or change.column_visibility or change.visibility_indexes:
        raise Unpredictable("This clause is MySQL syntax.")
    old_stored = [c for c in old.columns if c.generated != "VIRTUAL"]
    new_stored = [c for c in new.columns if c.generated != "VIRTUAL"]
    maybe_vcol = False
    for o, n in diff.pairs:
        if o.generated or n.generated:
            if (o.generated, _toks_text(o.gen_toks), o.type, o.length, o.decimals, o.unsigned) != \
                    (n.generated, _toks_text(n.gen_toks), n.type, n.length, n.decimals, n.unsigned):
                raise Unpredictable("Changing a generated column is outside the checked rules.")
            if o.generated == "STORED" or _in_any_key(old, o):
                maybe_vcol = True
        elif not _mariadb_is_equal(o, n):
            ok, why = _mariadb_nocopy(old, o, n)
            if ok:
                diff.flag("ALTER_COLUMN_TYPE_CHANGE_BY_ENGINE", why or "column %s changes type in place" % n.name)
            else:
                diff.flag("ALTER_STORED_COLUMN_TYPE", "column %s: %s" % (n.name, why))
        if o.name != n.name:
            diff.flag("ALTER_COLUMN_NAME", "column %s is renamed to %s" % (o.name, n.name))
        if o.nullable != n.nullable:
            diff.flag("ALTER_COLUMN_NULLABLE" if n.nullable else "ALTER_COLUMN_NOT_NULLABLE",
                      "column %s becomes %s" % (n.name, "NULL" if n.nullable else "NOT NULL"))
            if not n.nullable and n.type == "TIMESTAMP":
                # handler0alter.cc:2609-2648: the only way past the TIMESTAMP case
                # needs is_real_null(), never true of a NOT NULL field.
                diff.flag("_TIMESTAMP_NOT_NULL")
        if o.generated != "VIRTUAL":
            if old_stored.index(o) != new_stored.index(n):
                diff.flag("ALTER_STORED_COLUMN_ORDER", "column %s moves" % n.name)
        elif old.columns.index(o) != new.columns.index(n):
            # Virtual columns compare their position among all columns
            # (sql/sql_table.cc:7286-7291), so a stored column added,
            # dropped or moved before one moves it too.
            diff.flag("ALTER_VIRTUAL_COLUMN_ORDER", "virtual column %s moves" % n.name)
    for col in diff.dropped:
        diff.flag("ALTER_DROP_VIRTUAL_COLUMN" if col.generated == "VIRTUAL" else "ALTER_DROP_STORED_COLUMN",
                  "column %s is dropped" % col.name)
    if maybe_vcol and diff.flags & {"ALTER_STORED_COLUMN_TYPE", "ALTER_VIRTUAL_COLUMN_TYPE",
                                    "ALTER_COLUMN_NOT_NULLABLE"}:
        diff.flag("ALTER_COLUMN_VCOL")
    for col in diff.added:
        if col.generated == "STORED":
            diff.flag("ALTER_ADD_STORED_GENERATED_COLUMN", "column %s is a stored generated column" % col.name)
        elif col.generated == "VIRTUAL":
            diff.flag("ALTER_ADD_VIRTUAL_COLUMN", "virtual column %s is added" % col.name)
        else:
            diff.flag("ALTER_ADD_STORED_BASE_COLUMN", "column %s is added" % col.name)
    _mariadb_keys(old, new, change, diff)
    return diff


def _mariadb_part_compare(old, new, ocol, ncol, opart, npart):
    """ha_innobase::compare_key_parts (ha_innodb.cc:20963)."""
    if not _mariadb_is_equal(ocol, ncol):
        ok, _ = _mariadb_nocopy(old, ocol, ncol)
        if not ok:
            return "NotEqual"
        if _collation_suffix(ocol.collation, ocol.charset) != _collation_suffix(ncol.collation, ncol.charset):
            return "NotEqual"
    olen, nlen = ocol.key_bytes(opart[1]), ncol.key_bytes(npart[1])
    ochars = olen // (ocol.maxlen() if ocol.is_string() and ocol.kind() not in ("enum", "set") else 1)
    nchars = nlen // (ncol.maxlen() if ncol.is_string() and ncol.kind() not in ("enum", "set") else 1)
    if ochars != nchars:
        if olen != ocol.key_bytes(None):
            return "NotEqual"
        if olen >= nlen:
            return "NotEqual"
        if nlen != ncol.key_bytes(None):
            return "NotEqual"
        return "EqualButKeyPartLength"
    return "Equal"


def _mariadb_compare_keys(old, new, okey, nkey, old_pk, new_pk):
    """sql/sql_table.cc:6901 compare_keys_but_name."""
    if okey.kind != nkey.kind or len(okey.parts) != len(nkey.parts) or okey.functional or nkey.functional:
        return "NotEqual"
    if (nkey is new_pk) != (okey is old_pk):
        return "NotEqual"
    result = "Equal"
    for opart, npart in zip(okey.parts, nkey.parts):
        ocol, ncol = old.column(opart[0]), new.column(npart[0])
        if ocol is None or ncol is None or ncol.origin is None or _norm(ncol.origin) != _norm(ocol.name):
            return "NotEqual"
        if opart[2] != npart[2]:
            return "NotEqual"
        compare = _mariadb_part_compare(old, new, ocol, ncol, opart, npart)
        if compare == "NotEqual":
            return compare
        if compare != "Equal":
            result = compare
    if (okey.comment or "") != (nkey.comment or "") and result == "Equal":
        result = "EqualButComment"
    return result


def _mariadb_keys(old, new, change, diff):
    old_pk, new_pk = old.clustered(), new.clustered()
    dropped, added = [], []
    for okey in old.indexes:
        nkey = None
        for k in new.indexes:
            if k.name == okey.name:
                nkey = k
        if nkey is None:
            dropped.append(okey)
            continue
        compare = _mariadb_compare_keys(old, new, okey, nkey, old_pk, new_pk)
        if compare == "EqualButKeyPartLength":
            diff.flag("ALTER_COLUMN_INDEX_LENGTH")
        elif compare == "EqualButComment":
            diff.flag("ALTER_CHANGE_INDEX_COMMENT")
        elif compare == "NotEqual":
            dropped.append(okey)
            added.append(nkey)
    for nkey in new.indexes:
        if not any(k.name == nkey.name for k in old.indexes):
            added.append(nkey)
    # A dropped and an added index with the same definition is a rename
    # (sql/sql_table.cc:7455-7487).
    for nkey in list(added):
        for okey in list(dropped):
            if okey.name != nkey.name and \
                    _mariadb_compare_keys(old, new, okey, nkey, old_pk, new_pk) == "Equal":
                diff.flag("ALTER_RENAME_INDEX", "index %s is renamed to %s" % (okey.name, nkey.name))
                added.remove(nkey)
                dropped.remove(okey)
                break
    for name in change.ignored_indexes:
        diff.flag("ALTER_RENAME_INDEX", "index %s changes IGNORED" % name)
    for okey in dropped:
        if okey.unique():
            diff.flag("ALTER_DROP_PK_INDEX" if okey is old_pk else "ALTER_DROP_UNIQUE_INDEX",
                      "%s %s is dropped" % ("primary key" if okey is old_pk else "unique index", okey.name))
        else:
            diff.flag("ALTER_DROP_NON_UNIQUE_NON_PRIM_INDEX", "index %s is dropped" % okey.name)
    for nkey in added:
        if nkey.unique():
            diff.flag("ALTER_ADD_PK_INDEX" if nkey is new_pk else "ALTER_ADD_UNIQUE_INDEX",
                      "%s %s is added" % ("primary (clustered) key" if nkey is new_pk else "unique index",
                                          nkey.name))
        else:
            diff.flag("ALTER_ADD_NON_UNIQUE_NON_PRIM_INDEX", "%sindex %s is added"
                      % ({"FULLTEXT": "FULLTEXT ", "SPATIAL": "SPATIAL "}.get(nkey.kind, ""), nkey.name))
    diff.dropped_keys, diff.added_keys = dropped, added


# MariaDB handler flag sets (storage/innobase/handler/handler0alter.cc:72-157)
_MA_ONLINE_CREATE = {"ALTER_ADD_NON_UNIQUE_NON_PRIM_INDEX", "ALTER_ADD_UNIQUE_INDEX"}
_MA_DEFAULTS = {"ALTER_COLUMN_NOT_NULLABLE", "ALTER_ADD_STORED_BASE_COLUMN"}
_MA_REBUILD = {"ALTER_ADD_PK_INDEX", "ALTER_DROP_PK_INDEX", "ALTER_OPTIONS", "ALTER_COLUMN_NULLABLE",
               "ALTER_STORED_COLUMN_ORDER", "ALTER_DROP_STORED_COLUMN", "ALTER_RECREATE"} | _MA_DEFAULTS
_MA_IGNORE = {"ALTER_COLUMN_DEFAULT", "ALTER_PARTITIONED", "ALTER_COLUMN_COLUMN_FORMAT", "ALTER_COLUMN_STORAGE_TYPE",
              "ALTER_CONVERT_TO", "ALTER_VIRTUAL_GCOL_EXPR", "ALTER_DROP_CHECK_CONSTRAINT", "ALTER_RENAME",
              "ALTER_INDEX_ORDER", "ALTER_COLUMN_INDEX_LENGTH", "ALTER_CHANGE_INDEX_COMMENT",
              "ALTER_INDEX_IGNORABILITY"}
_MA_FOREIGN = {"ALTER_DROP_FOREIGN_KEY", "ALTER_ADD_FOREIGN_KEY"}
_MA_NOCREATE = {"ALTER_DROP_NON_UNIQUE_NON_PRIM_INDEX", "ALTER_DROP_UNIQUE_INDEX"}
_MA_NOREBUILD = _MA_ONLINE_CREATE | _MA_NOCREATE
_MA_INSTANT = {"ALTER_VIRTUAL_COLUMN_ORDER", "ALTER_COLUMN_NAME", "ALTER_ADD_VIRTUAL_COLUMN",
               "ALTER_COLUMN_TYPE_CHANGE_BY_ENGINE", "ALTER_COLUMN_UNVERSIONED", "ALTER_RENAME_INDEX",
               "ALTER_DROP_VIRTUAL_COLUMN"} | _MA_FOREIGN


def _mariadb_gate(version):
    return {
        # sql/sql_table.cc:10650 online_alter_check_supported (online
        # ALGORITHM=COPY, MDEV-16329): absent at mariadb-11.1.6, present at
        # mariadb-11.2.1.
        "online_copy": version >= (11, 2, 0),
        # sql/sql_alter.cc:257 Alter_info::algorithm reads alter_algorithm at
        # mariadb-11.4.10, ignores it at mariadb-11.5.0.
        "alter_algorithm": version < (11, 5, 0),
    }


def _options_need_rebuild(change):
    """handler0alter.cc:1541 alter_options_need_rebuild."""
    return bool(set(change.options) & {"ROW_FORMAT", "KEY_BLOCK_SIZE"})


def _mariadb_non_const(col):
    """Whether a new column's DEFAULT is non-constant (handler0alter.cc:1999
    is_non_const_value): it references a column or calls a function flagged
    VCOL_NON_DETERMINISTIC/NEXTVAL. Returns True, False or None (unknown)."""
    if col.default != "expr":
        return False
    toks = col.default_toks or []
    # Functions whose check_vcol_func_processor sets no flag (deterministic)
    # or only VCOL_SESSION_FUNC/VCOL_TIME_FUNC (sql/item_timefunc.h:968 NOW,
    # sql/item_func.h Item_func_rand); is_non_const_value ignores both.
    const_functions = {"RAND", "CURRENT_TIMESTAMP", "NOW", "LOCALTIME", "LOCALTIMESTAMP", "CURDATE",
                       "CURRENT_DATE", "CURTIME", "CURRENT_TIME", "FLOOR", "CEIL", "CEILING", "ROUND", "ABS",
                       "CONCAT", "CONCAT_WS", "LOWER", "UPPER", "LCASE", "UCASE", "LEFT", "RIGHT", "SUBSTRING",
                       "SUBSTR", "TRIM", "LTRIM", "RTRIM", "LPAD", "RPAD", "REPLACE", "REPEAT", "COALESCE",
                       "IFNULL", "NULLIF", "IF", "GREATEST", "LEAST", "DATE_ADD", "DATE_SUB", "ADDDATE",
                       "SUBDATE", "CAST", "CONVERT", "HEX", "UNHEX", "MD5", "SHA1", "SHA2", "JSON_OBJECT",
                       "JSON_ARRAY"}
    # VCOL_NON_DETERMINISTIC or VCOL_NEXTVAL (sql/item_func.h:4574 uuid_short,
    # sql/item_timefunc.h:1010 utc_timestamp and :1055 sysdate,
    # sql/item_strfunc.h:498 random_bytes, sql/item_strfunc.cc:5890
    # natural_sort_key, plugin/type_uuid/item_uuidfunc.h:48,64).
    non_det = {"UUID", "UUID_SHORT", "SYS_GUID", "UUID_V4", "UUID_V7", "RANDOM_BYTES", "UTC_TIMESTAMP",
               "SYSDATE", "NATURAL_SORT_KEY", "NEXTVAL", "LASTVAL", "SETVAL"}
    unknown = False
    for k, tok in enumerate(toks):
        call = k + 1 < len(toks) and toks[k + 1].kind == "op" and toks[k + 1].value == "("
        if tok.kind == "ident" or (tok.kind == "op" and tok.value == "@"):
            return True
        if tok.kind == "word":
            if call:
                if tok.value in non_det:
                    return True
                if tok.value not in const_functions:
                    unknown = True
            elif tok.value in ("NULL", "TRUE", "FALSE", "AND", "OR", "NOT", "DIV", "MOD", "INTERVAL", "DAY",
                               "HOUR", "MINUTE", "SECOND", "MONTH", "YEAR", "WEEK"):
                continue
            elif tok.value in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME", "LOCALTIME", "LOCALTIMESTAMP"):
                continue  # time functions without parentheses (VCOL_TIME_FUNC, sql/item_timefunc.h:935, :970)
            elif tok.value in ("UTC_TIMESTAMP", "UTC_DATE", "UTC_TIME"):
                return None if tok.value != "UTC_TIMESTAMP" else True
            elif (tok.value.startswith("_") or tok.value in ("X", "B", "N")) and k + 1 < len(toks) and \
                    toks[k + 1].kind == "str":
                continue  # a string introducer: _utf8mb4'x', X'..', N'..'
            else:
                return True  # a column reference (VCOL_FIELD_REF)
    return None if unknown else False


def _mariadb_instant_possible(old, new, change, diff, facts):
    """handler0alter.cc:1693 instant_alter_column_possible; (bool, rule, why)."""
    F = diff.flags
    if F & {"ALTER_STORED_COLUMN_ORDER", "ALTER_DROP_STORED_COLUMN", "ALTER_ADD_STORED_BASE_COLUMN"}:
        if old.fulltext() or new.fulltext() or facts.get("fts_doc_id_index") is True:
            return False, "mariadb.no_instant_fulltext", None
        if facts.get("fts_doc_id_index") is None and not any(c.name.upper() == "FTS_DOC_ID" for c in old.columns):
            raise _Fact("fts_doc_id_index")
        for index in old.indexes:
            for part in index.parts:
                col = old.column(part[0])
                if col is not None and col.generated == "VIRTUAL":
                    return False, "mariadb.no_instant_indexed_virtual", None
        why = _row_size_unknown(new, facts, old)
        if why:
            raise _Fact(why)
    avoid = {"ALTER_ADD_STORED_BASE_COLUMN", "ALTER_DROP_STORED_COLUMN", "ALTER_STORED_COLUMN_ORDER",
             "ALTER_COLUMN_NULLABLE"}
    if not F & avoid:
        if F & _MA_REBUILD:
            return False, None, None
        if "ALTER_OPTIONS" in F and _options_need_rebuild(change):
            return False, None, None
    elif new.compressed() or old.compressed():
        return False, "mariadb.no_instant_compressed", None
    blocking = (_MA_REBUILD | _MA_ONLINE_CREATE) - avoid - {"ALTER_OPTIONS"}
    if F & blocking:
        return False, ("mariadb.no_instant_index" if F & _MA_ONLINE_CREATE else None), None
    if "ALTER_OPTIONS" in F and _options_need_rebuild(change):
        return False, None, None
    if "ALTER_COLUMN_NULLABLE" in F:
        if not_redundant(old):
            return False, "mariadb.no_instant_nullable", None
        primary = old.clustered()
        for o, n in diff.pairs:
            if n.nullable and not o.nullable and primary is not None and \
                    any(_norm(p[0]) == _norm(o.name) for p in primary.parts):
                return False, "mariadb.no_instant_nullable", None
    return True, None, None


def _mariadb_decide(old, new, change, diff, facts, gate, requested, head):
    F = diff.flags
    d = diff.details
    algorithm = requested.get("algorithm")
    if algorithm is None and gate["alter_algorithm"]:
        session = (facts.get("alter_algorithm") or "DEFAULT").upper()
        if session == "COPY":
            # mariadb-11.4.10 sql/sql_table.cc:11127-11130 tests algorithm(thd),
            # so even a plain RENAME copies.
            return _Outcome(COPY, True, False, "mariadb.copy_alter_algorithm")
        if session != "DEFAULT":
            raise Unpredictable("The session's alter_algorithm=%s is outside the checked rules." % session)
    if change.kinds == {"rename_table"} and algorithm != COPY:
        return _Outcome(INSTANT, False, True, "mariadb.rename_table")
    if algorithm == COPY:
        return _Outcome(COPY, True, False, "mariadb.copy_requested")
    if change.order_by:
        return _Outcome(COPY, True, False, "mariadb.copy_server", ["ORDER BY always copies the table"])
    if change.engine_change:
        return _Outcome(COPY, True, False, "mariadb.copy_server", ["ENGINE changes the storage engine"])
    outside = F - _MA_IGNORE - _MA_INSTANT - _MA_NOREBUILD - _MA_REBUILD - {"_TIMESTAMP_NOT_NULL"}
    if outside:
        if "ALTER_ADD_STORED_GENERATED_COLUMN" in outside:
            return _Outcome(COPY, True, False, "mariadb.copy_generated", d.get("ALTER_ADD_STORED_GENERATED_COLUMN"))
        if "ALTER_STORED_COLUMN_TYPE" in outside:
            return _Outcome(COPY, True, False, "mariadb.copy_column_type", d.get("ALTER_STORED_COLUMN_TYPE"))
        if "ALTER_ADD_CHECK_CONSTRAINT" in outside:
            return _Outcome(COPY, True, False, "mariadb.copy_check")
        if "ALTER_COLUMN_VCOL" in outside:
            return _Outcome(COPY, True, False, "mariadb.copy_vcol")
        return _Outcome(COPY, True, False, "mariadb.copy_flags", sorted(outside))
    if "ALTER_ADD_FOREIGN_KEY" in F and facts.get("foreign_key_checks", True):
        return _Outcome(COPY, True, False, "mariadb.copy_fk_checks")
    need_rebuild, rebuild_rule = False, None
    setting = (facts.get("innodb_instant_alter_column_allowed") or "add_drop_reorder").lower()
    if setting == "never" and F & {"ALTER_ADD_STORED_BASE_COLUMN", "ALTER_STORED_COLUMN_ORDER",
                                   "ALTER_DROP_STORED_COLUMN"}:
        F = F | {"ALTER_RECREATE"}
        rebuild_rule = "mariadb.instant_setting"
    elif setting == "add_last" and F & {"ALTER_STORED_COLUMN_ORDER", "ALTER_DROP_STORED_COLUMN"}:
        F = F | {"ALTER_RECREATE"}
        rebuild_rule = "mariadb.instant_setting"
    elif setting != "add_drop_reorder":
        raise Unpredictable("innodb_instant_alter_column_allowed=%s on a table that may already have instant "
                            "changes is outside the checked rules." % setting)
    active = F - _MA_IGNORE
    if active == {"ALTER_OPTIONS"}:
        # handler0alter.cc:2339-2353: with innodb_file_per_table=ON a table
        # still in the system tablespace (space id 0) is moved, so rebuilt.
        per_table = facts.get("innodb_file_per_table", True)
        if per_table and facts.get("system_tablespace") is None and not _options_need_rebuild(change):
            raise _Fact("system_tablespace")
        if (per_table and facts.get("system_tablespace")) or _options_need_rebuild(change):
            need_rebuild = True
        else:
            return _Outcome(INSTANT, False, True, "mariadb.instant_options")
    elif not active:
        return _Outcome(INSTANT, False, True, "mariadb.instant")
    if head.get("ignore") and F & {"ALTER_ADD_PK_INDEX", "ALTER_ADD_UNIQUE_INDEX"}:
        return _Outcome(COPY, True, False, "mariadb.copy_ignore")
    if "ALTER_DROP_PK_INDEX" in F and "ALTER_ADD_PK_INDEX" not in F:
        return _Outcome(COPY, True, False, "mariadb.copy_drop_pk")
    if "ALTER_COLUMN_NULLABLE" in F and new.clustered() is None and old.clustered() is not None:
        return _Outcome(COPY, True, False, "mariadb.copy_nullable_clustered")
    add_drop_v = bool(F & {"ALTER_ADD_VIRTUAL_COLUMN", "ALTER_DROP_VIRTUAL_COLUMN", "ALTER_VIRTUAL_COLUMN_ORDER"})
    online, lock_rule = True, None
    has_doc_id = bool(old.fulltext()) or any(c.name.upper() == "FTS_DOC_ID" for c in old.columns) or \
        facts.get("fts_doc_id_index") is True
    # handler0alter.cc:2475-2484: DICT_TF2_FTS_HAS_DOC_ID marks a hidden FTS_DOC_ID only.
    visible_doc_id = any(c.name.upper() == "FTS_DOC_ID" for c in old.columns)
    if new.fulltext() and add_drop_v and (visible_doc_id or not has_doc_id):
        if not visible_doc_id and facts.get("fts_doc_id_index") is None:
            raise _Fact("fts_doc_id_index")
        return _Outcome(COPY, True, False, "mariadb.copy_virtual_mix")
    if old.fulltext() and new.fulltext():
        # handler0alter.cc:2571-2604: FTS_DOC_ID_INDEX and the FTS_DOC_ID column stay.
        touched = {_norm(c.name) for c in diff.dropped} | {_norm(o.name) for o, n in diff.pairs if o.name != n.name}
        if "fts_doc_id" in touched or any(_norm(k.name) == "fts_doc_id_index" for k in diff.dropped_keys):
            return _Outcome(COPY, True, False, "mariadb.copy_flags", ["FTS_DOC_ID is renamed or dropped"])
    autoinc, virtual = _uses_added(new, diff)
    if autoinc:
        online, need_rebuild, lock_rule = False, True, "mariadb.lock_autoinc"
    if virtual:
        if "ALTER_DROP_VIRTUAL_COLUMN" in F:
            return _Outcome(COPY, True, False, "mariadb.copy_virtual_mix")
        online, lock_rule = False, lock_rule or "mariadb.lock_virtual_index"
    need_rebuild = need_rebuild or _mariadb_need_rebuild(F, change)
    if "_TIMESTAMP_NOT_NULL" in F:
        return _Outcome(COPY, True, False, "mariadb.copy_timestamp_not_null")
    for col in diff.added:
        verdict = _mariadb_non_const(col)
        if verdict is None:
            raise Unpredictable("Whether the DEFAULT of column %s is constant is outside the checked rules."
                                % col.name)
        if verdict:
            return _Outcome(COPY, True, False, "mariadb.copy_default", ["column %s" % col.name])
    supports_instant, instant_rule, _ = _mariadb_instant_possible(old, new, change, diff, facts)
    if add_drop_v:
        rest = set(F) - {"ALTER_ADD_VIRTUAL_COLUMN", "ALTER_DROP_VIRTUAL_COLUMN", "ALTER_VIRTUAL_COLUMN_ORDER",
                         "ALTER_VIRTUAL_GCOL_EXPR", "ALTER_COLUMN_VCOL", "ALTER_ADD_NON_UNIQUE_NON_PRIM_INDEX",
                         "ALTER_DROP_NON_UNIQUE_NON_PRIM_INDEX", "ALTER_INDEX_ORDER"}
        if supports_instant:
            rest -= {"ALTER_DROP_STORED_COLUMN", "ALTER_STORED_COLUMN_ORDER"}
        if rest or not _v_col_in_order(old, new, diff, "mariadb"):
            return _Outcome(COPY, True, False, "mariadb.copy_virtual_mix",
                            d.get("ALTER_VIRTUAL_COLUMN_ORDER", []) + sorted(rest))
    if supports_instant and not F & _MA_NOREBUILD:
        rule = "mariadb.instant"
        if F & {"ALTER_ADD_STORED_BASE_COLUMN", "ALTER_DROP_STORED_COLUMN", "ALTER_STORED_COLUMN_ORDER"}:
            rule = "mariadb.instant_columns"
        elif "ALTER_COLUMN_TYPE_CHANGE_BY_ENGINE" in F:
            rule = "mariadb.instant_type_change"
        details = []
        for flag in sorted(F):
            details.extend(d.get(flag, []))
        return _Outcome(INSTANT, False, True, rule, details)
    fulltext_count = len(new.fulltext())
    if need_rebuild and (fulltext_count or new.spatial() or _indexed_virtual(new)):
        if fulltext_count > 1:
            return _Outcome(COPY, True, False, "mariadb.copy_fulltext")
        online = False
        lock_rule = lock_rule or ("mariadb.inplace_fulltext" if fulltext_count else "mariadb.lock_spatial")
    fts_need_rebuild = False
    added_fulltext = [k for k in diff.added_keys if k.kind == "FULLTEXT"]
    if len(added_fulltext) > 1:
        return _Outcome(COPY, True, False, "mariadb.copy_fulltext")
    if added_fulltext:
        online, lock_rule = False, "mariadb.inplace_fulltext"
        if not has_doc_id:
            if facts.get("fts_doc_id_index") is None:
                raise _Fact("fts_doc_id_index")
            fts_need_rebuild = True
    if any(k.kind == "SPATIAL" for k in diff.added_keys):
        online, lock_rule = False, lock_rule or "mariadb.lock_spatial"
    rebuild = (need_rebuild and not supports_instant) or fts_need_rebuild
    rule = lock_rule or rebuild_rule or ("mariadb.inplace_rebuild" if rebuild else "mariadb.inplace")
    if instant_rule and not lock_rule and not rebuild_rule:
        rule = instant_rule
    details = []
    for flag in sorted(F):
        details.extend(d.get(flag, []))
    return _Outcome(INPLACE, rebuild, online, rule, details)


def _indexed_virtual(table):
    for index in table.indexes:
        for part in index.parts:
            col = table.column(part[0])
            if col is not None and col.generated == "VIRTUAL":
                return True
    return False


def _mariadb_need_rebuild(F, change):
    """handler0alter.cc:1579 innobase_need_rebuild."""
    if F - _MA_IGNORE - _MA_NOREBUILD - _MA_INSTANT == {"ALTER_OPTIONS"}:
        return _options_need_rebuild(change)
    return bool(F & _MA_REBUILD)


def _mariadb_online_copy(old, new, change, diff, facts, gate, requested, head):
    """Whether MariaDB's ALGORITHM=COPY lets writes continue (online ALTER,
    MDEV-16329): (online, why not). sql/sql_table.cc:10828 starts with
    ``online= order == NULL``, :10905-10911 drops it for LOCK=SHARED or
    EXCLUSIVE, old_mode LOCK_ALTER_TABLE_COPY with LOCK=DEFAULT, or
    ALGORITHM=INPLACE/NOCOPY/INSTANT; :10650 online_alter_check_supported then
    names a reason (a refusal with LOCK=NONE)."""
    if not gate["online_copy"]:
        return False, "version"
    lock = requested.get("lock")
    if change.order_by:
        return False, "order"
    if lock in ("SHARED", "EXCLUSIVE") or requested.get("algorithm") in (INPLACE, "NOCOPY", INSTANT):
        return False, "lock"
    if lock is None and "LOCK_ALTER_TABLE_COPY" in (facts.get("old_mode") or "").upper():
        return False, "old_mode"
    if head.get("ignore"):
        return False, "check"
    if change.engine_change and _norm(change.options.get("ENGINE")) in ("s3", "connect"):
        return False, "check"
    for fk in old.foreign_keys:
        if fk.on_delete in ("CASCADE", "SET NULL", "SET DEFAULT") or \
                fk.on_update in ("CASCADE", "SET NULL", "SET DEFAULT"):
            return False, "check"
    for col in diff.added:
        if col.auto_increment:
            return False, "check"
        if _uses_nextval(col):
            return False, "check"  # :10706-10712, VCOL_NEXTVAL
    for o, n in diff.pairs:
        if o.nullable and not n.nullable and _uses_nextval(n):
            return False, "check"
    if not _autoinc_online(old, new, change, diff, facts):
        return False, "check"
    return True, None


def _uses_nextval(col):
    return any(tok.kind == "word" and tok.value in ("NEXTVAL", "NEXT") for tok in col.default_toks or [])


def _autoinc_online(old, new, change, diff, facts):
    """mariadb-server sql/sql_table.cc:10592 online_alter_check_autoinc: an
    online copy that adds AUTO_INCREMENT to an existing column needs
    NO_AUTO_VALUE_ON_ZERO or a NOT NULL unique key whose columns stay as
    they are."""
    if "NO_AUTO_VALUE_ON_ZERO" in (facts.get("sql_mode") or "").upper():
        return True
    if "modify_column" not in change.kinds:
        return True
    kept = {_norm(o.name): (o, n) for o, n in diff.pairs}
    for index in old.indexes:
        if not index.unique() or index.functional:
            continue
        if any(old.column(p[0]) is None or old.column(p[0]).nullable for p in index.parts):
            continue
        good = True
        for part in index.parts:
            pair = kept.get(_norm(part[0]))
            if pair is None or not _same_column(pair[0], pair[1]):
                good = False
                break
        if good:
            return True
    for col in new.columns:
        if col.auto_increment:
            if col.origin is not None:
                previous = old.column(col.origin)
                if previous is not None and not previous.auto_increment:
                    return False
            break
    return True


def _same_column(o, n):
    """Field::is_equal for the key-part test above (types, sizes, flags)."""
    try:
        return _mariadb_is_equal(o, n)
    except Unpredictable:
        return False


# ---------------------------------------------------------------------------
# Explicit ALGORITHM / LOCK


def _requested(clauses, engine, head):
    out = {}
    for clause in clauses:
        if clause.kind in ("algorithm", "lock"):
            out.pop(clause.kind, None)
            if clause.data["value"] != "DEFAULT":
                out[clause.kind] = clause.data["value"]
    if head.get("online"):
        out["lock"] = "NONE"
    valid_alg = (None, INSTANT, INPLACE, COPY) + (("NOCOPY",) if engine == "mariadb" else ())
    if out.get("algorithm") not in valid_alg or out.get("lock") not in (None, "NONE", "SHARED", "EXCLUSIVE"):
        raise Unpredictable("Unknown ALGORITHM or LOCK value.")
    return out


def _apply_request(outcome, requested, engine, change):
    """The server's answer to an explicit ALGORITHM/LOCK, given the default
    choice. MySQL sql/sql_table.cc:17360-17367, :18454-18530; MariaDB
    sql/sql_alter.cc:139 supports_algorithm, :183 supports_lock."""
    alg, lock = requested.get("algorithm"), requested.get("lock")
    if outcome.algorithm is None or (alg is None and lock is None):
        return outcome
    refused = None
    if engine == "mysql":
        if alg == INSTANT and lock is not None:
            refused = "ALGORITHM=INSTANT cannot be combined with LOCK (error 1221)"
        elif outcome.rule == "mysql.rename_table" and lock not in (None, "EXCLUSIVE"):
            refused = "renaming a table alone needs LOCK=EXCLUSIVE (error 1845)"
        elif alg == INSTANT and outcome.algorithm != INSTANT:
            refused = "ALGORITHM=INSTANT is not available for this change (error 1845 or 1846)"
        elif alg == INPLACE and outcome.algorithm == COPY:
            refused = "ALGORITHM=INPLACE is not available for this change (error 1845 or 1846)"
        elif lock == "NONE" and not outcome.concurrent_dml:
            refused = "LOCK=NONE is not available for this change (error 1845 or 1846)"
    else:
        if outcome.rule == "mariadb.rename_table" and lock not in (None, "EXCLUSIVE"):
            refused = "renaming a table alone needs LOCK=EXCLUSIVE (error 1845)"
        elif alg == INSTANT and outcome.algorithm != INSTANT:
            refused = "ALGORITHM=INSTANT is not available for this change (error 1845 or 1846)"
        elif alg == "NOCOPY" and (outcome.algorithm == COPY or outcome.rebuild):
            refused = "ALGORITHM=NOCOPY is not available for this change (error 1845 or 1846)"
        elif alg in (INPLACE, "NOCOPY") and outcome.algorithm == COPY:
            refused = "ALGORITHM=%s is not available for this change (error 1845 or 1846)" % alg
        elif lock == "NONE" and not outcome.concurrent_dml:
            if outcome.algorithm == COPY:
                # sql/sql_table.cc:11918-11930 refuses only when a planned
                # online copy fails its checks (mariadb-10.11: always,
                # "COPY algorithm requires a lock"); with ORDER BY no online
                # copy is planned and LOCK=NONE is accepted, writes blocked.
                if outcome.copy_why in ("version", "check"):
                    refused = "LOCK=NONE is not available for this change (error 1845 or 1846)"
            elif alg is None and outcome.copy_why is None:
                # sql/sql_alter.cc:183 supports_lock: an in-place change that
                # needs a lock falls back to an online copy.
                copied = _Outcome(COPY, True, True, "mariadb.copy_lock_none", online_copy=True)
                copied.copy_why = None
                return copied
            else:
                refused = "LOCK=NONE is not available for this change (error 1845 or 1846)"
    if refused:
        return _Outcome(None, None, None, "unknown.refused", [refused])
    if lock in ("SHARED", "EXCLUSIVE") and outcome.algorithm != INSTANT:
        return _Outcome(outcome.algorithm, outcome.rebuild, False, outcome.rule, outcome.details)
    return outcome


# ---------------------------------------------------------------------------
# predict()


def _version(version):
    text = re.sub(r"^\s*5\.5\.5-", "", str(version or ""))
    return server_version_tuple(text)


def _gate(engine, version):
    """None when this engine and version are covered; else the reason."""
    if engine == "mysql":
        if (5, 0, 0) <= version < (8, 0, 0):
            # mysql-5.7.44 sql/sql_alter.cc set_requested_algorithm accepts only
            # INPLACE, COPY and DEFAULT; ALTER_TABLE_ALGORITHM_INSTANT first
            # appears in mysql-8.0.12 sql/sql_alter.h.
            return ("MySQL %d.%d.%d is not predicted: it has no ALGORITHM=INSTANT (MySQL 8.0.12 added it), and the "
                    "rules were checked for MySQL 8.4 and 9.x" % version)
        if version < (8, 4, 0):
            return "older servers: not predicted (rules were checked for MySQL 8.4 and 9.x)"
        if version >= (10, 0, 0):
            return "this MySQL version is newer than the source the rules were checked against (9.7)"
        return None
    if engine == "mariadb":
        if version < (10, 11, 0):
            return "older servers: not predicted (rules were checked for MariaDB 10.11 to 13.0)"
        if version >= (13, 1, 0):
            return "this MariaDB version is newer than the source the rules were checked against (13.0)"
        return None
    return "only MySQL and MariaDB are predicted"


def _defaults_note(facts, engine):
    assumed = []
    if facts.get("foreign_key_checks") is None:
        assumed.append("foreign_key_checks=ON")
    if facts.get("sql_mode") is None:
        assumed.append("a strict sql_mode")
    if facts.get("innodb_page_size") is None:
        assumed.append("innodb_page_size=16384")
    if engine == "mysql" and facts.get("old_alter_table") is None:
        assumed.append("old_alter_table=OFF")
    if engine == "mariadb" and facts.get("innodb_instant_alter_column_allowed") is None:
        assumed.append("innodb_instant_alter_column_allowed=add_drop_reorder")
    if engine == "mariadb" and facts.get("old_mode") is None:
        assumed.append("old_mode without LOCK_ALTER_TABLE_COPY")
    if engine == "mariadb" and facts.get("innodb_file_per_table") is None:
        assumed.append("innodb_file_per_table=ON")
    if not assumed:
        return None
    return "Assumes the server defaults: %s." % ", ".join(assumed)


def _fk_copy_problem(table, new, facts):
    """The copy path checks foreign keys (mysql-server sql/sql_table.cc:16513
    fk_check_copy_alter_table, :16590-16600 and :16645-16652; mariadb-server
    sql/sql_table.cc:10006, :10098): renaming a column a foreign key uses is
    refused there, and changing its type is checked against the other table."""
    renamed, retyped = set(), set()
    for col in new.columns:
        if col.origin is None:
            continue
        old = table.column(col.origin)
        if old is None:
            continue
        if old.name != col.name:
            renamed.add(_norm(old.name))
        if (old.type, old.length, old.decimals, old.unsigned, old.charset) != \
                (col.type, col.length, col.decimals, col.unsigned, col.charset):
            retyped.add(_norm(old.name))
    if not renamed and not retyped:
        return None
    used = {_norm(c) for fk in table.foreign_keys for c in fk.columns}
    refs = facts.get("referenced_columns")
    if renamed & used or (refs is not None and renamed & {_norm(c) for c in refs}):
        return _Outcome(None, None, None, "unknown.refused", [
            "the copy algorithm refuses to rename a column a foreign key uses (error 1846); ALGORITHM=INPLACE "
            "renames it"])
    if refs is None:
        return _unknown("unknown.fact", [
            "a table copy checks other tables' foreign keys on renamed or retyped columns, and myflames could "
            "not read which columns they reference"], needs=["referenced_columns"])
    if retyped & (used | {_norm(c) for c in refs}):
        return _unknown("unknown.fact", [
            "a column a foreign key uses changes type; the server checks it against the other table and may "
            "refuse"])
    return None


def _evaluate(table, clauses, engine, gate, facts, requested, head):
    new, change = _apply(table, clauses, engine, facts.get("default_collations"))
    if engine == "mysql":
        _date_check(table, new, facts, engine)
        diff = _mysql_diff(table, new, change, facts)
        outcome = _mysql_decide(table, new, change, diff, facts, gate, requested)
    else:
        _date_check(table, new, facts, engine)
        diff = _mariadb_diff(table, new, change, facts)
        outcome = _mariadb_decide(table, new, change, diff, facts, gate, requested, head)
        online, why = _mariadb_online_copy(table, new, change, diff, facts, gate, requested, head)
        outcome.copy_why = why
        if outcome.algorithm == COPY:
            outcome.concurrent_dml = online
            outcome.online_copy = online
    outcome = _apply_request(outcome, requested, engine, change)
    if outcome.algorithm == COPY:
        problem = _fk_copy_problem(table, new, facts)
        if problem is not None:
            return problem
    return outcome


def _date_check(old, new, facts, engine):
    """A new DATE/DATETIME NOT NULL column without DEFAULT fails on a table
    with rows when sql_mode has NO_ZERO_DATE (MySQL sql/sql_table.cc:15840-
    15862, :14520-14560; MariaDB sql/sql_table.cc:9018-9031)."""
    mode = facts.get("sql_mode")
    no_zero = ("NO_ZERO_DATE" in mode.upper()) if mode is not None else engine == "mysql"
    for col in new.columns:
        if col.origin is None and col.type in ("DATE", "DATETIME") and not col.nullable and col.default is None \
                and not col.generated:
            if no_zero or engine == "mysql":
                raise Unpredictable("Adding DATE/DATETIME NOT NULL column %s without a DEFAULT fails on a table "
                                    "with rows under sql_mode NO_ZERO_DATE, and runs differently on an empty one."
                                    % col.name)


def _clause_entry(clause, outcome, reason_prefix=None):
    rule = RULES.get(outcome.rule, ("", ""))
    reason = rule[0]
    if outcome.rule.startswith("unknown.") and outcome.details:
        reason = "; ".join(outcome.details)
        reason = reason[0].upper() + reason[1:]
    elif outcome.details:
        reason = (reason + " " if reason else "") + "(" + "; ".join(outcome.details) + ")"
    if reason_prefix:
        reason = reason_prefix + " " + reason
    return {
        "text": clause.text,
        "algorithm": outcome.algorithm,
        "rebuild": outcome.rebuild,
        "concurrent_dml": outcome.concurrent_dml,
        "summary": _summary(outcome.algorithm, outcome.rebuild),
        "outcome": outcome_text(outcome.algorithm, outcome.rebuild, outcome.concurrent_dml, outcome.online_copy)
        if outcome.algorithm else None,
        "reason": reason.strip(),
        "source": rule[1],
    }


def predict(alter_sql, create_table_sql, engine, version, facts=None):
    """Predict the algorithm of one ALTER TABLE on the table *create_table_sql*
    (its SHOW CREATE TABLE) for *engine* ("mysql" or "mariadb"; Percona
    Server is "mysql") at *version*.

    Returns ``{predicted, algorithm, rebuild, concurrent_dml, summary, text,
    reason, clauses, notes, needs}``. ``algorithm`` is INSTANT, INPLACE, COPY
    or None; ``clauses`` has one entry per clause with its own outcome, reason
    and source; ``needs`` lists facts that would let an unpredicted clause be
    predicted. Never raises for SQL input.
    """
    facts = dict(facts or {})
    engine = (engine or "").lower()
    if engine == "percona":
        engine = "mysql"
    result = {"predicted": False, "algorithm": None, "rebuild": None, "concurrent_dml": None, "summary": None,
              "text": None, "reason": "", "source": "", "clauses": [], "notes": [], "needs": [], "engine": engine,
              "version": version}
    ver = _version(version)
    reason = _gate(engine, ver)
    if reason:
        result["reason"] = reason[0].upper() + reason[1:] + "."
        return result
    gate = _mysql_gate(ver) if engine == "mysql" else _mariadb_gate(ver)
    try:
        table = _parse_create(create_table_sql or "", engine)
    except Unpredictable as exc:
        result["reason"] = "The table definition could not be read: %s" % exc
        return result
    problem = _table_problem(table, engine)
    if problem:
        result["reason"] = problem
        return result
    try:
        name, head, clauses = _parse_alter(alter_sql or "")
    except Unpredictable as exc:
        result["reason"] = "The statement could not be read: %s" % exc
        return result
    if engine == "mysql" and head.get("ignore"):
        result["reason"] = "MySQL has no ALTER IGNORE TABLE; the server refuses this statement."
        return result
    if _norm(name) != _norm(table.name):
        result["reason"] = "The statement alters %s, not %s." % (name, table.name)
        return result
    try:
        requested = _requested(clauses, engine, head)
    except Unpredictable as exc:
        result["reason"] = str(exc)
        return result
    actions = [c for c in clauses if c.kind not in ("algorithm", "lock")]
    if not actions:
        result["reason"] = "The statement only sets ALGORITHM or LOCK; it is not predicted."
        return result
    unknown = [c for c in actions if c.kind == "unknown"]
    needs = []
    entries = []
    no_request = {}
    # Each clause on its own (with the clauses that add what it names).
    for index, clause in enumerate(actions):
        if clause.kind == "unknown":
            entries.append({"text": clause.text, "algorithm": None, "rebuild": None, "concurrent_dml": None,
                            "summary": None, "outcome": None, "reason": clause.data["reason"], "source": ""})
            continue
        outcome, helpers = _clause_outcome(table, actions, index, engine, gate, facts, no_request, head)
        needs.extend(outcome.needs)
        entries.append((clause, outcome, helpers))
    statement = None
    if not unknown:
        try:
            statement = _evaluate(table, actions + [c for c in clauses if c.kind in ("algorithm", "lock")],
                                  engine, gate, facts, requested, head)
        except _Fact as exc:
            statement = _fact_outcome(exc)
        except Unpredictable as exc:
            statement = _unknown("unknown.syntax", [str(exc)])
        needs.extend(statement.needs)
    # Per-clause entries; a clause the statement makes cheaper than it is
    # alone (DROP PRIMARY KEY with ADD PRIMARY KEY) takes the statement's
    # outcome, so no clause looks slower than the statement.
    out = []
    for item in entries:
        if isinstance(item, dict):
            out.append(item)
            continue
        clause, outcome, helpers = item
        prefix = None
        if helpers:
            prefix = "Together with %s, which it needs:" % "; ".join(helpers)
        if statement is not None and statement.algorithm is not None and outcome.algorithm is not None and \
                _rank(outcome) > _rank(statement) and not requested:
            prefix = "On its own this would be %s; with the other clauses of this statement:" % \
                     _summary(outcome.algorithm, outcome.rebuild)
            outcome = _Outcome(statement.algorithm, statement.rebuild, statement.concurrent_dml, statement.rule,
                               statement.details, statement.online_copy)
        out.append(_clause_entry(clause, outcome, prefix))
    result["clauses"] = out
    result["needs"] = sorted(set(needs))
    note = _defaults_note(facts, engine)
    if note:
        result["notes"].append(note)
    if requested:
        result["notes"].append("The statement sets %s; each clause shows what the server would choose without it."
                               % ", ".join("%s=%s" % (k.upper(), v) for k, v in sorted(requested.items())))
    if statement is None or statement.algorithm is None:
        if unknown:
            result["reason"] = "Not predicted: %s" % unknown[0].data["reason"]
        else:
            rule = RULES.get(statement.rule, ("", ""))
            detail = "; ".join(statement.details)
            result["reason"] = "Not predicted: " + (detail or rule[0])
            if statement.rule == "unknown.refused":
                result["reason"] = "Not predicted: the server refuses this statement as written (%s); " \
                                   "nothing would change." % detail
        return result
    rule = RULES.get(statement.rule, ("", ""))
    clause_ranks = [_rank(item[1]) for item in entries if not isinstance(item, dict) and not item[2]]
    if clause_ranks and _rank(statement) > max(clause_ranks):
        result["notes"].append("Together these clauses need more than any one of them alone: %s" % rule[0])
    result.update(predicted=True, algorithm=statement.algorithm, rebuild=statement.rebuild,
                  concurrent_dml=statement.concurrent_dml, summary=_summary(statement.algorithm, statement.rebuild),
                  text=outcome_text(statement.algorithm, statement.rebuild, statement.concurrent_dml,
                                    statement.online_copy),
                  reason=(rule[0] + (" (" + "; ".join(statement.details) + ")" if statement.details else "")).strip(),
                  source=rule[1])
    _extra_notes(result, table, actions, engine, gate, statement)
    return result


def _fact_outcome(exc):
    text = str(exc)
    names = {
        "fts_doc_id_index": ("whether the table keeps InnoDB's hidden FTS_DOC_ID column from an earlier FULLTEXT "
                             "index (information_schema.INNODB_INDEXES, which needs the PROCESS privilege)"),
        "system_tablespace": ("whether the table has its own tablespace file; a table in the system tablespace "
                              "is rebuilt (information_schema.INNODB_SYS_TABLES, which needs PROCESS)"),
        "collation": ("the column's new collation: with CHARACTER SET but no COLLATE, MariaDB uses "
                      "character_set_collations"),
    }
    if text in names:
        return _unknown("unknown.fact", ["the outcome depends on " + names[text]],
                        needs=["default_collations" if text == "collation" else text])
    return _unknown("unknown.fact", [text])


def _clause_outcome(table, actions, index, engine, gate, facts, requested, head):
    """(outcome, [texts of the clauses it was evaluated with]) for one clause:
    alone, or with the clauses that add the columns or indexes it names."""
    clause = actions[index]
    group = [clause]
    for _ in range(3):
        helpers = [c.text for c in group if c is not clause]
        try:
            return _evaluate(table, group, engine, gate, facts, requested, head), helpers
        except _Missing as exc:
            missing = str(exc)
            more = [c for c in actions[:index] + actions[index + 1:]
                    if c.kind in ("add_column", "add_columns", "modify_column", "rename_column", "add_index")
                    and c not in group]
            if not more:
                return _unknown("unknown.syntax", ["column or index %s does not exist" % missing]), []
            group = more + group
        except _Fact as exc:
            return _fact_outcome(exc), helpers
        except Unpredictable as exc:
            return _unknown("unknown.syntax", [str(exc)]), helpers
    return _unknown("unknown.syntax", ["the clause depends on others in a way outside the checked rules"]), []


def _table_problem(table, engine):
    if _norm(table.engine or "innodb") != "innodb":
        return "Only InnoDB tables are predicted; this table uses %s." % table.engine
    if table.partitioned:
        return "Partitioned tables are not predicted."
    if table.versioned:
        return "System-versioned tables are not predicted."
    if table.temporary:
        return "Temporary tables are not predicted."
    if table.unsupported:
        return "Tables with %s are not predicted." % ", ".join(sorted(set(table.unsupported)))
    if any(index.functional for index in table.indexes):
        return "Tables with functional index parts are not predicted."
    for col in table.columns:
        if col.compressed:
            return "Tables with compressed columns are not predicted."
    return None


def _extra_notes(result, table, actions, engine, gate, statement):
    if engine == "mysql" and statement.rule == "mysql.instant_add_drop" and gate["empty_table_inplace"]:
        result["notes"].append("If the table has no rows, MySQL rebuilds it in place instead (nothing to copy) "
                               "without using a row version (handler0alter.cc:1058-1063, MySQL 8.4.4+ and 9.2+).")
    if engine == "mysql" and statement.rule == "mysql.instant_add_drop":
        result["notes"].append("Each instant ADD or DROP COLUMN uses one of the table's %d row versions."
                               % gate["max_row_version"])
    if statement.algorithm in (INPLACE, COPY):
        result["notes"].append("The ALTER waits for a metadata lock at its start and end; transactions that use "
                               "the table must finish first, and new ones queue behind it.")
    if statement.algorithm == COPY or statement.rebuild:
        result["notes"].append("A rebuild or copy needs free disk space for a second copy of the table and "
                               "replicates as one long statement.")
