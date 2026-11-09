"""Import CSV: the pure part (no server, stdlib only, Python 3.7+).

Reading a file
--------------
A format is ``{encoding, separator, quote, escape, header, line_ending,
skip_lines, empty, null_markers}`` (:func:`check_format`). Encodings:
``utf-8``, ``utf-8-sig`` (UTF-8 with a byte order mark), ``utf-16-le``,
``utf-16-be`` and ``cp1252`` (Latin-1 / Windows-1252, decoded as MySQL's
``latin1``: the five bytes Windows-1252 leaves undefined become the C1
control characters, as in mysql-server strings/ctype-latin1.cc ``cs_to_uni``).
A leading U+FEFF is dropped in every encoding. :func:`detect` reads a sample:
a byte order mark, NUL bytes on alternate positions (UTF-16 without a mark),
UTF-8 validity, else ``cp1252``; the separator among comma, semicolon, tab
and pipe (or a custom one) by the most consistent field count; the header by
the first row's values.

:func:`records` parses with the csv module, streaming, strict quoting
(``"a"b`` is a problem), lines of any ending (``\\n``, ``\\r\\n``, ``\\r``)
and reports each record with the physical line it starts on. Empty lines
are skipped. A row with fewer fields than the header, or more fields that
are not all empty, is a problem with its line number.

Values
------
:class:`ColumnType` describes a target column (from information_schema, or
from a type the person chose for a new table, :func:`parse_type`).
:func:`convert` turns one CSV text into the value myflames sends, or raises
ValueError with the reason: integers by range (UNSIGNED included), DECIMAL by
precision and scale (never rounded), FLOAT and DOUBLE by range, DATE,
DATETIME, TIMESTAMP, TIME and YEAR in ISO forms (fractional digits beyond the
column's precision are refused, never rounded), CHAR and VARCHAR length in
characters, TEXT and BLOB sizes in bytes, the column character set (utf8mb3,
latin1, ascii), ENUM and SET members, BIT, JSON validity and booleans
(true/false, yes/no, on/off, t/f, y/n) as 1 and 0 for TINYINT(1) and BIT(1).
Numbers, dates and booleans ignore spaces around the value; text keeps every
character. NULL: an empty field when ``empty`` is ``"null"``, or a field
equal to one of ``null_markers`` (``\\N``, ``NULL``).

Statements
----------
Identifiers are backquoted with backquotes doubled (no backslash rules
apply). Text literals do not depend on NO_BACKSLASH_ESCAPES unless the mode
is known (:func:`sql_text`): plain quotes with ``'`` doubled when the value
has no backslash and no control character, escapes when the mode is known,
otherwise ``CONVERT(X'..' USING utf8mb4)``; binary values are ``X'..'``.
:class:`Batches` groups rows into multi-row INSERT statements of at most
1,000 rows and a byte budget below ``max_allowed_packet``, never across a
commit boundary. :func:`load_data_sql` builds LOAD DATA LOCAL INFILE with
FIELDS, LINES, IGNORE, CHARACTER SET, a column list with user variables and
SET clauses from a fixed set (NULLIF(@v, '') and CAST(@v AS UNSIGNED)),
every separator written as a hexadecimal literal so the text means the same
under any sql_mode (both grammars take a hex string there: mysql-server
sql/sql_yacc.yy ``text_string``, mariadb-server sql/sql_yacc.yy
``hex_or_bin_String``).
"""
import codecs
import csv
import datetime
import decimal
import hashlib
import io
import json
import math
import re

INSPECT_SCHEMA = "csv-inspect-1.0"
ENCODINGS = ("utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "cp1252")
SEPARATORS = (",", ";", "\t", "|")
QUOTES = ('"', "'", "")
ESCAPES = ("", "\\")
NULL_MARKERS = ("\\N", "NULL")
DEFAULT_NULL_MARKERS = ["\\N"]
LINE_ENDINGS = ("\n", "\r\n", "\r")
EMPTY = ("null", "empty")
FORMAT_KEYS = ("encoding", "separator", "quote", "escape", "header", "line_ending", "skip_lines", "empty",
               "null_markers")
DEFAULT_FORMAT = {"encoding": "utf-8", "separator": ",", "quote": '"', "escape": "", "header": True,
                  "line_ending": "\n", "skip_lines": 0, "empty": "null", "null_markers": list(DEFAULT_NULL_MARKERS)}
MAX_COLUMNS = 1024
MAX_SKIP_LINES = 1000
#: The largest single field the reader accepts (characters).
FIELD_LIMIT = 16 * 1024 * 1024
#: The longest physical line read at once (characters); a longer one is a
#: problem that stops reading, so one huge line cannot take the memory.
LINE_LIMIT = 16 * 1024 * 1024
DETECT_BYTES = 256 * 1024
INSPECT_BYTES = 64 * 1024 * 1024
PREVIEW_ROWS = 50
SAMPLES = 5
PROBLEM_LIMIT = 100
VALUE_PREVIEW = 80
MAX_ROWS_PER_INSERT = 1000
#: Bytes per INSERT statement. A single larger row goes alone, up to the
#: packet limit the caller computes from max_allowed_packet.
INSERT_BYTES = 1024 * 1024
TAG = "/*myflames:import_csv*/ "

# The csv module's limit is process wide; raise it to FIELD_LIMIT only.
if csv.field_size_limit() < FIELD_LIMIT:
    csv.field_size_limit(FIELD_LIMIT)

_C1 = frozenset((0x81, 0x8D, 0x8F, 0x90, 0x9D))


def _latin1_undefined(exc):
    """Decode the five bytes Windows-1252 leaves undefined as MySQL latin1
    does: U+0081, U+008D, U+008F, U+0090, U+009D."""
    if isinstance(exc, UnicodeDecodeError):
        data = exc.object[exc.start:exc.end]
        if data and all(b in _C1 for b in bytearray(data)):
            return "".join(chr(b) for b in bytearray(data)), exc.end
    raise exc


codecs.register_error("myflames-latin1", _latin1_undefined)
#: Every character a latin1 column can store.
LATIN1_CHARS = frozenset(bytes([b]).decode("cp1252", "myflames-latin1") for b in range(256))


def _errors(encoding):
    return "myflames-latin1" if encoding == "cp1252" else "strict"


# ---- format ------------------------------------------------------------------------------

def check_format(value, base=None):
    """The format *value* asks for, on top of *base* (the detected format or
    the defaults), validated. Raises ValueError naming the first wrong key."""
    fmt = dict(DEFAULT_FORMAT)
    if base:
        fmt.update({k: base[k] for k in FORMAT_KEYS if k in base})
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("The format must be an object.")
    unknown = sorted(k for k in value if k not in FORMAT_KEYS)
    if unknown:
        raise ValueError("Unknown format setting: %s." % unknown[0])
    fmt.update(value)
    if fmt["encoding"] not in ENCODINGS:
        raise ValueError("Choose an encoding: UTF-8, UTF-8 with BOM, UTF-16 LE, UTF-16 BE or Latin-1 / Windows-1252.")
    sep = fmt["separator"]
    if not isinstance(sep, str) or len(sep) != 1 or sep in "\r\n\x00":
        raise ValueError("The separator must be one character.")
    if fmt["quote"] is None:
        fmt["quote"] = ""
    if fmt["quote"] not in QUOTES:
        raise ValueError('The quote must be ", \' or none.')
    if fmt["escape"] is None:
        fmt["escape"] = ""
    if fmt["escape"] not in ESCAPES:
        raise ValueError("The escape character must be none or a backslash.")
    if sep in (fmt["quote"], fmt["escape"]) and sep:
        raise ValueError("The separator cannot also be the quote or the escape character.")
    if not isinstance(fmt["header"], bool):
        raise ValueError("Header row must be true or false.")
    if fmt["line_ending"] not in LINE_ENDINGS:
        raise ValueError("The line ending must be LF, CRLF or CR.")
    skip = fmt["skip_lines"]
    if isinstance(skip, bool) or not isinstance(skip, int) or not 0 <= skip <= MAX_SKIP_LINES:
        raise ValueError("Skip first lines must be a whole number from 0 to %d." % MAX_SKIP_LINES)
    if fmt["empty"] not in EMPTY:
        raise ValueError('Empty fields must be "null" or "empty".')
    markers = fmt["null_markers"]
    if not isinstance(markers, list) or any(m not in NULL_MARKERS for m in markers):
        raise ValueError("NULL markers can be \\N and NULL.")
    fmt["null_markers"] = [m for m in NULL_MARKERS if m in markers]
    return {k: fmt[k] for k in FORMAT_KEYS}


def dialect(fmt):
    """csv reader options for a checked format."""
    options = {"delimiter": fmt["separator"], "strict": True, "skipinitialspace": False}
    if fmt["quote"]:
        options.update(quotechar=fmt["quote"], quoting=csv.QUOTE_MINIMAL, doublequote=True)
    else:
        options.update(quoting=csv.QUOTE_NONE, quotechar=None)
    options["escapechar"] = fmt["escape"] or None
    return options


def is_null(raw, fmt):
    return (raw == "" and fmt["empty"] == "null") or raw in fmt["null_markers"]


# ---- reading ------------------------------------------------------------------------------

class _Counting(io.RawIOBase):
    """A raw reader that counts bytes and feeds a hash (the file's SHA-256)."""

    def __init__(self, raw, digest=None):
        io.RawIOBase.__init__(self)
        self._raw = raw
        self.digest = digest
        self.count = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        n = self._raw.readinto(buffer)
        if n:
            self.count += n
            if self.digest is not None:
                self.digest.update(memoryview(buffer)[:n])
        return n

    def drain(self):
        """Read (and hash) the rest of the file."""
        buffer = bytearray(1 << 20)
        while self.readinto(buffer):
            pass


class ReadProblem(ValueError):
    """A problem that stops reading the file, with the line it is on."""

    def __init__(self, line, message):
        ValueError.__init__(self, message)
        self.line = line


class DecodeProblem(ReadProblem):
    def __init__(self, line, encoding):
        ReadProblem.__init__(self, line, "Line %d is not valid %s text. Choose another encoding."
                             % (line, encoding_label(encoding)))


class LongLine(ReadProblem):
    def __init__(self, line):
        ReadProblem.__init__(self, line, "Line %d is longer than %d MiB. Import CSV reads lines of at most %d MiB; "
                                         "check the separator and the quoting." % (line, LINE_LIMIT // 1048576,
                                                                                  LINE_LIMIT // 1048576))


def encoding_label(encoding):
    return {"utf-8": "UTF-8", "utf-8-sig": "UTF-8", "utf-16-le": "UTF-16 LE", "utf-16-be": "UTF-16 BE",
            "cp1252": "Latin-1 / Windows-1252"}.get(encoding, encoding)


class Records(object):
    """Iterate a file's records: ``(line, fields)`` for every non-empty
    record after the skipped lines, the header included (the caller decides).
    :attr:`count` is the bytes read so far; :attr:`blank` the empty lines
    skipped. A wrong encoding raises :class:`DecodeProblem` with the exact
    line, a line longer than LINE_LIMIT :class:`LongLine` (both are
    :class:`ReadProblem`). A malformed record is yielded as ``(line,
    CsvProblem)``."""

    def __init__(self, raw, fmt, digest=None):
        self.fmt = fmt
        self.raw = raw
        self.counting = _Counting(raw, digest)
        self.text = io.TextIOWrapper(io.BufferedReader(self.counting, 1 << 16), encoding=fmt["encoding"],
                                     errors=_errors(fmt["encoding"]), newline="")
        self.blank = 0
        self.physical = 0

    @property
    def count(self):
        return self.counting.count

    def _source(self):
        first = True
        while True:
            try:
                line = self.text.readline(LINE_LIMIT + 1)
            except UnicodeDecodeError:
                raise DecodeProblem(bad_line(self.raw, self.fmt["encoding"]), self.fmt["encoding"])
            if not line:
                return
            if len(line) > LINE_LIMIT and not line.endswith(("\n", "\r")):
                raise LongLine(self.physical + 1)
            self.physical += 1
            if first:
                first = False
                if line.startswith("\ufeff"):
                    line = line[1:]
            yield line

    def __iter__(self):
        source = self._source()
        for _ in range(self.fmt["skip_lines"]):
            if next(source, None) is None:
                return
        skipped = self.fmt["skip_lines"]
        reader = csv.reader(source, **dialect(self.fmt))
        last = 0
        while True:
            try:
                fields = next(reader)
            except StopIteration:
                return
            except csv.Error as exc:
                start = skipped + last + 1
                last = reader.line_num
                yield start, CsvProblem(_csv_message(exc))
                continue
            start = skipped + last + 1
            last = reader.line_num
            if not fields:
                self.blank += 1
                continue
            yield start, fields

    def drain(self):
        """Read and hash the rest of the file (after an early stop)."""
        self.counting.drain()


def bad_line(raw, encoding):
    """The physical line (1-based) of the first byte sequence *encoding*
    cannot decode: the text reader decodes ahead in chunks, so its own
    position cannot say."""
    raw.seek(0)
    decoder = codecs.getincrementaldecoder(encoding)(_errors(encoding))
    line, carriage = 1, False
    while True:
        data = raw.read(1 << 16)
        try:
            text = decoder.decode(data, final=not data)
        except UnicodeDecodeError as exc:
            text = bytes(exc.object[:exc.start]).decode(encoding, "ignore")
            return line + _breaks(text, carriage)
        line += _breaks(text, carriage)
        carriage = text.endswith("\r") if text else carriage
        if not data:
            return line


def _breaks(text, carriage):
    count = text.count("\n") + text.count("\r") - text.count("\r\n")
    if carriage and text.startswith("\n"):
        count -= 1
    return count


class CsvProblem(object):
    def __init__(self, message):
        self.message = message


def split_header(records, fmt, header):
    """The data records ``(line, fields)``. With ``fmt["header"]`` the first
    record after the skipped lines is the header row whatever it holds: its
    line, its fields (None when it cannot be read) and the problem go into
    the *header* dict, and it is never taken as data. One rule for inspect,
    plan and apply."""
    pending = fmt["header"]
    for line, fields in records:
        if pending:
            pending = False
            header["line"] = line
            if isinstance(fields, CsvProblem):
                header["fields"], header["problem"] = None, fields.message
            else:
                header["fields"], header["problem"] = fields, None
            continue
        yield line, fields


def _csv_message(exc):
    text = str(exc)
    if "field larger than field limit" in text:
        return "A field is larger than 16 MiB, or a quote is never closed."
    if "expected after" in text:
        return "Text follows a closing quote. Check the quoting of this line."
    if "unexpected end of data" in text:
        return "A quoted field is never closed."
    if "line contains NUL" in text or "NUL" in text:
        return "The line contains a NUL byte. Check the encoding."
    return "This line cannot be read as CSV (%s)." % text


# ---- detection ----------------------------------------------------------------------------

def detect_encoding(sample):
    """(encoding, notes) for the first bytes of a file."""
    notes = []
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", notes
    if sample.startswith(b"\xff\xfe\x00\x00") or sample.startswith(b"\x00\x00\xfe\xff"):
        notes.append("The file looks like UTF-32, which myflames does not read. Save it as UTF-8.")
        return "utf-8", notes
    if sample.startswith(b"\xff\xfe"):
        return "utf-16-le", notes
    if sample.startswith(b"\xfe\xff"):
        return "utf-16-be", notes
    head = sample[:4096]
    if len(head) >= 4:
        odd = sum(1 for b in bytearray(head[1::2]) if b == 0)
        even = sum(1 for b in bytearray(head[0::2]) if b == 0)
        half = len(head) // 2
        if odd > half * 0.3 and even < half * 0.05:
            notes.append("The file has no byte order mark; its bytes look like UTF-16 LE.")
            return "utf-16-le", notes
        if even > half * 0.3 and odd < half * 0.05:
            notes.append("The file has no byte order mark; its bytes look like UTF-16 BE.")
            return "utf-16-be", notes
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        decoder.decode(sample, final=False)
        return "utf-8", notes
    except UnicodeDecodeError:
        notes.append("The file is not valid UTF-8, so Latin-1 / Windows-1252 was chosen. Check the preview.")
        return "cp1252", notes


def _decode_sample(sample, encoding, complete):
    if encoding in ("utf-16-le", "utf-16-be") and len(sample) % 2:
        sample = sample[:-1]
    decoder = codecs.getincrementaldecoder(encoding)(_errors(encoding))
    try:
        text = decoder.decode(sample, final=complete)
    except UnicodeDecodeError:
        text = sample.decode(encoding, "replace")
    if text.startswith("﻿"):
        text = text[1:]
    if not complete:
        # Keep whole lines only; the last one may be cut.
        cut = max(text.rfind("\n"), text.rfind("\r"))
        if cut > 0:
            text = text[:cut + 1]
    return text


def detect_line_ending(text):
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf:
        return "\r\n"
    if cr > lf:
        return "\r"
    return "\n"


def _parse_sample(text, fmt, limit=200):
    """The first records of *text*; a record the reader refuses counts as an
    empty list (it lowers that separator's consistency, never stops)."""
    rows = []
    reader = csv.reader(io.StringIO(text, newline=""), **dialect(fmt))
    while len(rows) < limit:
        try:
            fields = next(reader)
        except StopIteration:
            break
        except csv.Error:
            rows.append([])
            continue
        if fields:
            rows.append(fields)
    return rows


def detect_separator(text, quote='"', custom=None):
    """The separator whose rows have the most consistent field count above one."""
    candidates = list(SEPARATORS) + ([custom] if custom and custom not in SEPARATORS else [])
    best, best_score = ",", -1.0
    for sep in candidates:
        fmt = dict(DEFAULT_FORMAT, separator=sep, quote=quote)
        rows = _parse_sample(text, fmt)
        if not rows:
            continue
        counts = {}
        for row in rows:
            counts[len(row)] = counts.get(len(row), 0) + 1
        mode, hits = max(counts.items(), key=lambda item: (item[1], item[0]))
        if mode < 2:
            continue
        score = hits / float(len(rows)) + min(mode, 50) / 1000.0
        if score > best_score + 1e-9:
            best, best_score = sep, score
    return best


_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z", re.ASCII)
_DATEISH = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def detect_header(rows):
    """True when the first row reads as column names: all filled, distinct,
    none a number or a date, and either a later row has a number or a date
    in some column, or every first-row value is a short name."""
    if not rows:
        return False
    first = rows[0]
    values = [v.strip() for v in first]
    if any(not v for v in values) or len(set(v.lower() for v in values)) != len(values):
        return False
    if any(_NUMBER.match(v) or _DATEISH.match(v) for v in values):
        return False
    later = rows[1:]
    for index in range(len(first)):
        column = [r[index].strip() for r in later if index < len(r) and r[index].strip()]
        if column and all(_NUMBER.match(v) or _DATEISH.match(v) for v in column):
            return True
    return all(len(v) <= 64 and re.match(r"^[\w .()#%/-]+$", v, re.U) for v in values) and bool(later)


def sample_rows(sample, fmt, complete, limit=50):
    """The first records of a sample read with *fmt* (for header detection)."""
    text = _decode_sample(sample, fmt["encoding"], complete)
    lines = text.splitlines(True)[fmt["skip_lines"]:]
    return [r for r in _parse_sample("".join(lines), fmt, limit) if r]


def detect(sample, complete):
    """(format, notes) from the first bytes of a file (*complete* when the
    sample is the whole file)."""
    encoding, notes = detect_encoding(sample)
    text = _decode_sample(sample, encoding, complete)
    quote = '"'
    if '"' not in text and re.search(r"(^|[,;\t|])'[^']*'([,;\t|]|\r?$)", text, re.M):
        quote = "'"
    separator = detect_separator(text, quote)
    fmt = dict(DEFAULT_FORMAT, encoding=encoding, separator=separator, quote=quote,
               line_ending=detect_line_ending(text))
    rows = [r for r in _parse_sample(text, fmt, 50) if r]
    fmt["header"] = detect_header(rows)
    if any(field == "NULL" for row in rows[1 if fmt["header"] else 0:] for field in row):
        fmt["null_markers"] = ["\\N", "NULL"]
        notes.append("Fields that are exactly NULL are read as NULL. Turn this off to keep the word as text.")
    return check_format(fmt), notes


# ---- column names and types ------------------------------------------------------------------

def column_name(header, index, used):
    """A column name for a new table from a CSV header: control characters
    removed, at most 64 characters, no trailing space, unique ignoring case."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", header or "").strip()
    name = "".join(c for c in name if ord(c) <= 0xFFFF)
    name = name[:64].rstrip()
    if not name:
        name = "column_%d" % (index + 1)
    base, n = name, 2
    while name.lower() in used:
        suffix = "_%d" % n
        name = base[:64 - len(suffix)].rstrip() + suffix
        n += 1
    used.add(name.lower())
    return name


def check_name(value, what="name"):
    """A database, table or column name the server can have."""
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("A %s must have 1 to 64 characters." % what)
    if value != value.rstrip(" "):
        raise ValueError("A %s cannot end with a space." % what)
    if any(ord(c) < 32 or ord(c) == 127 or ord(c) > 0xFFFF for c in value):
        raise ValueError("A %s cannot contain control characters or characters outside the Basic Multilingual Plane."
                         % what)
    return value


def ident(name):
    return "`" + name.replace("`", "``") + "`"


INTEGER_BITS = {"tinyint": 8, "smallint": 16, "mediumint": 24, "int": 32, "integer": 32, "bigint": 64}
TEXT_BYTES = {"tinytext": 255, "text": 65535, "mediumtext": 16777215, "longtext": 4294967295}
BLOB_BYTES = {"tinyblob": 255, "blob": 65535, "mediumblob": 16777215, "longblob": 4294967295}
TEMPORAL = ("date", "datetime", "timestamp", "time", "year")
UNSUPPORTED = {"geometry", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon",
               "geometrycollection", "geomcollection", "vector", "inet4", "inet6", "uuid"}
TRANSACTIONAL_ENGINES = {"innodb", "rocksdb", "tokudb", "ndbcluster", "ndb"}
_BOOL_TRUE = ("true", "t", "yes", "y", "on")
_BOOL_FALSE = ("false", "f", "no", "n", "off")


def parse_members(text):
    """ENUM or SET members from the server's column type text, which writes
    a quote as '' and escapes \\0, \\n, \\r and \\\\ (mysql-server
    sql/table.cc append_unescaped; MariaDB prints the same)."""
    members, i, n = [], 0, len(text)
    while i < n:
        if text[i] in " ,":
            i += 1
            continue
        if text[i] != "'":
            raise ValueError("The ENUM or SET members could not be read.")
        i += 1
        out = []
        while True:
            if i >= n:
                raise ValueError("The ENUM or SET members could not be read.")
            c = text[i]
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                i += 1
                break
            if c == "\\" and i + 1 < n:
                out.append({"0": "\x00", "n": "\n", "r": "\r", "\\": "\\", "'": "'", "Z": "\x1a",
                            "t": "\t"}.get(text[i + 1], text[i + 1]))
                i += 2
                continue
            out.append(c)
            i += 1
        members.append("".join(out))
    return members


class ColumnType(object):
    """What one target column accepts."""

    def __init__(self, name, data_type, column_type="", nullable=True, default=None, extra="", charset=None,
                 collation=None, length=None, precision=None, scale=None, fsp=None, json_check=False):
        self.name = name
        self.data_type = (data_type or "").lower()
        self.column_type = column_type or self.data_type
        lower = self.column_type.lower()
        self.nullable = bool(nullable)
        self.default = default
        self.extra = extra or ""
        upper_extra = self.extra.upper()
        self.auto_increment = "AUTO_INCREMENT" in upper_extra
        # VIRTUAL GENERATED and STORED GENERATED columns cannot be filled.
        # MariaDB shows its system-versioning columns as STORED GENERATED too
        # (ROW START/END is in GENERATION_EXPRESSION). MySQL's DEFAULT_GENERATED
        # (an expression default) is an ordinary column.
        self.generated = bool(re.search(r"\b(?:VIRTUAL|STORED) GENERATED\b", upper_extra))
        self.charset = (charset or "").lower() or None
        self.collation = (collation or "").lower() or None
        self.unsigned = "unsigned" in lower
        self.json = self.data_type == "json" or bool(json_check)
        self.members = None
        if self.data_type in ("enum", "set"):
            inner = re.match(r"^\s*(?:enum|set)\s*\((.*)\)\s*$", self.column_type, re.I | re.S)
            self.members = parse_members(inner.group(1)) if inner else []
        sized = re.match(r"^\s*[a-z]+\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", lower)
        self.length = int(length) if length is not None else (int(sized.group(1)) if sized and self.data_type in (
            "char", "varchar", "binary", "varbinary", "bit") else None)
        self.precision = int(precision) if precision is not None else (
            int(sized.group(1)) if sized and self.data_type in ("decimal", "numeric") else (
                10 if self.data_type in ("decimal", "numeric") else None))
        self.scale = int(scale) if scale is not None else (
            int(sized.group(2)) if sized and sized.group(2) and self.data_type in ("decimal", "numeric") else (
                0 if self.data_type in ("decimal", "numeric") else None))
        if fsp is None and self.data_type in ("datetime", "timestamp", "time"):
            fsp = int(sized.group(1)) if sized else 0
        self.fsp = int(fsp) if fsp is not None else None
        if self.data_type == "bit" and self.length is None:
            self.length = 1
        # MySQL 8.0.19+ prints an integer display width only for TINYINT(1),
        # "since connectors use this to indicate BOOLEAN" (mysql-server
        # sql/field.cc Field_tiny::sql_type); MariaDB prints every width.
        self.tinyint1 = self.data_type == "tinyint" and re.match(r"^\s*tinyint\s*\(\s*1\s*\)", lower) is not None \
            and not self.unsigned
        self.boolean = self.tinyint1 or (self.data_type == "bit" and self.length == 1)
        #: TIMESTAMP's last year: 2038 on MySQL and MariaDB before 11.5, 2106 on
        #: MariaDB 11.5+ 64-bit builds (mariadb-server include/my_time.h
        #: TIMESTAMP_MAX_YEAR); the caller sets it from the server's version.
        self.timestamp_max_year = 2038

    @property
    def required(self):
        """No value given means an error in strict mode: NOT NULL without a default."""
        return not self.nullable and self.default is None and not self.auto_increment and not self.generated

    @property
    def unsupported(self):
        if self.generated:
            return "is generated by the server, so it cannot be filled"
        if self.data_type in UNSUPPORTED:
            return "has type %s, which Import CSV does not convert" % self.data_type.upper()
        known = set(INTEGER_BITS) | set(TEXT_BYTES) | set(BLOB_BYTES) | set(TEMPORAL) | {
            "decimal", "numeric", "float", "double", "real", "char", "varchar", "binary", "varbinary", "enum",
            "set", "bit", "json", "bool", "boolean"}
        if self.data_type not in known:
            return "has type %s, which Import CSV does not convert" % (self.data_type or "unknown").upper()
        return ""

    @property
    def textual(self):
        return self.data_type in ("char", "varchar", "enum", "set", "json") or self.data_type in TEXT_BYTES \
            or self.data_type in BLOB_BYTES or self.data_type in ("binary", "varbinary")

    @property
    def binary(self):
        return self.data_type in BLOB_BYTES or self.data_type in ("binary", "varbinary") or self.charset == "binary"

    def public(self):
        return {"name": self.name, "type": self.column_type, "nullable": self.nullable, "default": self.default,
                "extra": self.extra}

    def fingerprint(self):
        return [self.name, self.column_type, self.nullable, self.default, self.extra, self.charset, self.collation,
                self.json]


def from_information_schema(row, json_columns=()):
    """A ColumnType from an information_schema.COLUMNS row (names as keys)."""
    def num(key):
        value = row.get(key)
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    data_type = (row.get("DATA_TYPE") or "").lower()
    return ColumnType(row.get("COLUMN_NAME"), data_type, row.get("COLUMN_TYPE") or data_type,
                      nullable=row.get("IS_NULLABLE") == "YES", default=row.get("COLUMN_DEFAULT"),
                      extra=row.get("EXTRA") or "", charset=row.get("CHARACTER_SET_NAME"),
                      collation=row.get("COLLATION_NAME"),
                      length=num("CHARACTER_MAXIMUM_LENGTH") if data_type in ("char", "varchar", "binary",
                                                                                 "varbinary") else None,
                      precision=num("NUMERIC_PRECISION") if data_type in ("decimal", "numeric") else None,
                      scale=num("NUMERIC_SCALE") if data_type in ("decimal", "numeric") else None,
                      fsp=num("DATETIME_PRECISION") if data_type in ("datetime", "timestamp", "time") else None,
                      json_check=row.get("COLUMN_NAME") in json_columns)


_TYPE = re.compile(
    r"^\s*(?P<base>tinyint|smallint|mediumint|int|integer|bigint|decimal|numeric|float|double|date|datetime|"
    r"timestamp|time|year|char|varchar|tinytext|text|mediumtext|longtext|binary|varbinary|tinyblob|blob|"
    r"mediumblob|longblob|json|bit|bool|boolean|enum|set)\s*(?:\((?P<args>[^()]*)\))?\s*(?P<unsigned>unsigned)?\s*\Z",
    re.I | re.S)


#: DECIMAL's largest scale: 30 on MySQL, 38 on MariaDB (mariadb-server
#: include/my_decimal_limits.h DECIMAL_MAX_SCALE).
DECIMAL_SCALE = {"mysql": 30, "mariadb": 38}


def parse_type(text, engine=None):
    """A ColumnType from a type the person chose for a new table, validated
    against a fixed grammar (never sent as typed: :func:`render_type` writes
    the canonical text). *engine* (``mysql``, ``mariadb`` or None for "either
    engine") decides the engine's own rules: UNSIGNED DECIMAL, FLOAT and
    DOUBLE (deprecated in MySQL 8.0.17, accepted by MariaDB) and DECIMAL's
    largest scale. Raises ValueError."""
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError("Choose a column type.")
    match = _TYPE.match(text)
    if not match:
        raise ValueError("%s is not a type Import CSV can create." % (text.strip()[:40] or "An empty type"))
    base = match.group("base").lower()
    args = match.group("args")
    unsigned = bool(match.group("unsigned"))
    if base in ("bool", "boolean"):
        if args is not None or unsigned:
            raise ValueError("BOOLEAN takes no length.")
        return ColumnType("", "tinyint", "tinyint(1)")
    if base == "integer":
        base = "int"
    if base in ("enum", "set"):
        if not args or unsigned:
            raise ValueError("%s needs its members, like %s('a','b')." % (base.upper(), base.upper()))
        if "\\" in args:
            raise ValueError("%s members must be quoted text without backslashes or control characters." % base.upper())
        members = parse_members(args)
        if not members or any("\\" in m or any(ord(c) < 32 for c in m) for m in members):
            raise ValueError("%s members must be quoted text without backslashes or control characters." % base.upper())
        if len(members) > (65535 if base == "enum" else 64) or len(set(m.lower() for m in members)) != len(members):
            raise ValueError("%s members must be distinct (at most %s)." % (base.upper(),
                                                                          "65,535" if base == "enum" else "64"))
        quoted = ",".join("'%s'" % m.replace("'", "''") for m in members)
        return ColumnType("", base, "%s(%s)" % (base, quoted), charset="utf8mb4")
    numbers = []
    if args is not None:
        if not re.match(r"\s*[0-9]+\s*(,\s*[0-9]+\s*)?\Z", args, re.ASCII):
            raise ValueError("%s takes whole numbers in parentheses." % base.upper())
        numbers = [int(x) for x in args.split(",")]
    if unsigned and base not in INTEGER_BITS and base not in ("decimal", "numeric", "float", "double"):
        raise ValueError("UNSIGNED applies to numbers only.")
    if unsigned and base in ("decimal", "numeric", "float", "double") and engine == "mysql":
        raise ValueError("MySQL deprecates UNSIGNED for %s (8.0.17); leave it out, or add a CHECK in the Query "
                         "editor." % base.upper())
    suffix = " unsigned" if unsigned else ""
    if base in INTEGER_BITS:
        if numbers and (len(numbers) != 1 or not 1 <= numbers[0] <= 255):
            raise ValueError("%s takes at most one display width." % base.upper())
        if base == "tinyint" and numbers == [1] and not unsigned:
            return ColumnType("", "tinyint", "tinyint(1)")
        return ColumnType("", base, base + suffix)
    if base in ("decimal", "numeric"):
        precision = numbers[0] if numbers else 10
        scale = numbers[1] if len(numbers) > 1 else 0
        largest = DECIMAL_SCALE.get(engine, 38)
        if not 1 <= precision <= 65 or not 0 <= scale <= largest or scale > precision:
            raise ValueError("DECIMAL precision is 1 to 65 and scale 0 to %d%s, no larger than the precision."
                             % (largest, " on %s" % ("MySQL" if engine == "mysql" else "MariaDB") if engine else ""))
        return ColumnType("", "decimal", "decimal(%d,%d)%s" % (precision, scale, suffix), precision=precision,
                          scale=scale)
    if base in ("float", "double"):
        if numbers:
            raise ValueError("Write %s without a length." % base.upper())
        return ColumnType("", base, base + suffix)
    if base in ("date", "year", "json", "tinytext", "text", "mediumtext", "longtext", "tinyblob", "blob",
                "mediumblob", "longblob"):
        if numbers:
            raise ValueError("Write %s without a length." % base.upper())
        charset = "utf8mb4" if base in TEXT_BYTES else ("binary" if base in BLOB_BYTES else None)
        return ColumnType("", base, base, charset=charset)
    if base in ("datetime", "timestamp", "time"):
        fsp = numbers[0] if numbers else 0
        if len(numbers) > 1 or not 0 <= fsp <= 6:
            raise ValueError("%s takes a fractional precision from 0 to 6." % base.upper())
        return ColumnType("", base, "%s(%d)" % (base, fsp) if fsp else base, fsp=fsp)
    if base == "bit":
        width = numbers[0] if numbers else 1
        if len(numbers) > 1 or not 1 <= width <= 64:
            raise ValueError("BIT takes a width from 1 to 64.")
        return ColumnType("", "bit", "bit(%d)" % width, length=width)
    # char, varchar, binary, varbinary
    if len(numbers) != 1:
        raise ValueError("%s needs a length, like %s(64)." % (base.upper(), base.upper()))
    limit = {"char": 255, "binary": 255, "varchar": 16383, "varbinary": 65532}[base]
    if not (0 if base in ("char", "binary") else 1) <= numbers[0] <= limit:
        raise ValueError("%s length must be at most %d." % (base.upper(), limit))
    charset = "binary" if base in ("binary", "varbinary") else "utf8mb4"
    return ColumnType("", base, "%s(%d)" % (base, numbers[0]), length=numbers[0], charset=charset)


def render_type(column):
    """The canonical SQL type text of a ColumnType built by parse_type."""
    if column.data_type in ("enum", "set"):
        return column.data_type.upper() + column.column_type[len(column.data_type):]
    return column.column_type.upper()


# ---- converting values -----------------------------------------------------------------------

# ASCII digits only (\d would take other scripts' digits) and a true end of
# text (\Z: "$" also matches before a trailing newline). Values are rebuilt
# from these ASCII groups, never sent as typed.
_INT = re.compile(r"[+-]?[0-9]+\Z", re.ASCII)
_DEC = re.compile(r"([+-])?([0-9]*)(?:\.([0-9]*))?\Z", re.ASCII)
_FLOAT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z", re.ASCII)
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})\Z", re.ASCII)
_DATETIME = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[ T]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?)?\Z",
                       re.ASCII)
_TIME = re.compile(r"(-)?([0-9]{1,3}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?\Z", re.ASCII)
_DIGITS = re.compile(r"[0-9]+\Z", re.ASCII)
_YEAR = re.compile(r"[0-9]{4}\Z", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _fraction(digits, fsp, what):
    digits = digits or ""
    if len(digits) > fsp and digits[fsp:].strip("0"):
        raise ValueError("has more fractional second digits than %s(%d) keeps; it would be rounded." % (what, fsp))
    return digits[:fsp]


def _date(y, m, d):
    try:
        datetime.date(int(y), int(m), int(d))
    except ValueError:
        raise ValueError("is not a valid calendar date.")


def _encoded_length(text, charset):
    if charset in (None, "utf8mb4", "utf8mb3", "utf8"):
        return len(text.encode("utf-8"))
    if charset in ("latin1", "ascii", "binary"):
        return len(text) if charset != "binary" else len(text.encode("utf-8"))
    if charset in ("ucs2",):
        return 2 * len(text)
    if charset in ("utf16", "utf16le"):
        return len(text.encode("utf-16-le"))
    if charset == "utf32":
        return 4 * len(text)
    return None


def _charset_problem(text, charset):
    if charset in ("utf8mb3", "utf8") and any(ord(c) > 0xFFFF for c in text):
        return "has a character outside the Basic Multilingual Plane (an emoji, say), which a utf8mb3 column cannot store."
    if charset == "latin1" and not all(c in LATIN1_CHARS for c in text):
        return "has a character a latin1 column cannot store."
    if charset == "ascii" and any(ord(c) > 127 for c in text):
        return "has a character an ascii column cannot store."
    return ""


def _boolean(value):
    lower = value.lower()
    if lower in _BOOL_TRUE:
        return 1
    if lower in _BOOL_FALSE:
        return 0
    return None


def convert(raw, column, fmt):
    """The value to send for one CSV field: ``("null", None)``, ``("num",
    text)`` (written unquoted), ``("text", str)``, ``("bytes", bytes)`` or
    ``("bit", int)``. Raises ValueError with the reason (a sentence whose
    subject is "The value", left out)."""
    if is_null(raw, fmt):
        if not column.nullable and not column.auto_increment:
            raise ValueError("is empty or a NULL marker, but the column does not accept NULL.")
        return ("null", None)
    kind = column.data_type
    if kind in ("bool", "boolean"):
        kind = "tinyint"
    if kind in INTEGER_BITS or kind in ("decimal", "numeric", "float", "double", "real", "bit", "year") \
            or kind in TEMPORAL:
        value = raw.strip(" \t")
        if value == "":
            if column.nullable and fmt["empty"] == "null":
                return ("null", None)
            raise ValueError("is empty, which is not a %s." % _kind_word(column))
    else:
        value = raw
    if kind in INTEGER_BITS:
        if column.boolean:
            flag = _boolean(value)
            if flag is not None:
                return ("num", str(flag))
        if not _INT.match(value):
            raise ValueError("is not a whole number.")
        number = int(value)
        bits = INTEGER_BITS[kind]
        low, high = (0, 2 ** bits - 1) if column.unsigned else (-2 ** (bits - 1), 2 ** (bits - 1) - 1)
        if not low <= number <= high:
            raise ValueError("is outside the range of %s (%d to %d)." % (column.column_type.upper(), low, high))
        return ("num", str(number))
    if kind in ("decimal", "numeric"):
        match = _DEC.match(value)
        if not match or not (match.group(2) or match.group(3)):
            raise ValueError("is not a decimal number (write it without exponent or thousands separators).")
        sign, whole, frac = match.group(1) or "", (match.group(2) or "").lstrip("0"), match.group(3) or ""
        if len(frac) > column.scale and frac[column.scale:].strip("0"):
            raise ValueError("has more than %d decimal places; the server would round it." % column.scale)
        frac = frac[:column.scale].rstrip("0") if column.scale else ""
        if len(whole) > column.precision - column.scale:
            raise ValueError("does not fit DECIMAL(%d,%d)." % (column.precision, column.scale))
        if sign == "-" and column.unsigned and (whole or frac.strip("0")):
            raise ValueError("is negative, but the column is UNSIGNED.")
        text = (whole or "0") + ("." + frac if frac else "")
        if sign == "-" and (whole or frac.strip("0")):
            text = "-" + text
        return ("num", text)
    if kind in ("float", "double", "real"):
        if not _FLOAT.match(value):
            raise ValueError("is not a number.")
        number = float(value)
        limit = 3.402823466e38 if kind == "float" else 1.7976931348623157e308
        if math.isinf(number) or abs(number) > limit:
            raise ValueError("is outside the range of %s." % kind.upper())
        if column.unsigned and number < 0:
            raise ValueError("is negative, but the column is UNSIGNED.")
        return ("num", value.lstrip("+"))
    if kind == "bit":
        flag = _boolean(value) if column.length == 1 else None
        if flag is not None:
            return ("bit", flag)
        if not _DIGITS.match(value):
            raise ValueError("is not a whole number for BIT(%d)." % column.length)
        number = int(value)
        if number >= 2 ** column.length:
            raise ValueError("does not fit BIT(%d) (at most %d)." % (column.length, 2 ** column.length - 1))
        return ("bit", number)
    if kind == "year":
        if not _YEAR.match(value) or not 1901 <= int(value) <= 2155:
            raise ValueError("is not a year from 1901 to 2155.")
        return ("num", str(int(value)))
    if kind == "date":
        match = _DATE.match(value)
        if not match:
            raise ValueError("is not a date in the form YYYY-MM-DD.")
        _date(*match.groups())
        return ("text", value)
    if kind in ("datetime", "timestamp"):
        match = _DATETIME.match(value)
        if not match:
            raise ValueError("is not a date and time in the form YYYY-MM-DD HH:MM:SS.")
        y, mo, d, h, mi, s, frac = match.groups()
        _date(y, mo, d)
        if h is not None and (int(h) > 23 or int(mi) > 59 or int(s) > 59):
            raise ValueError("is not a valid time of day.")
        if kind == "timestamp" and not 1970 <= int(y) <= column.timestamp_max_year:
            raise ValueError("is outside the TIMESTAMP range (1970 to %d)." % column.timestamp_max_year)
        frac = _fraction(frac, column.fsp or 0, kind.upper())
        text = "%s-%s-%s %s:%s:%s" % (y, mo, d, h or "00", mi or "00", s or "00")
        return ("text", text + ("." + frac if frac else ""))
    if kind == "time":
        match = _TIME.match(value)
        if not match:
            raise ValueError("is not a time in the form HH:MM:SS.")
        sign, h, mi, s, frac = match.groups()
        if int(mi) > 59 or int(s) > 59 or int(h) > 838:
            raise ValueError("is outside the TIME range (-838:59:59 to 838:59:59).")
        frac = _fraction(frac, column.fsp or 0, "TIME")
        if int(h) == 838 and (int(mi), int(s)) == (59, 59) and frac.strip("0"):
            raise ValueError("is outside the TIME range (-838:59:59 to 838:59:59).")
        return ("text", "%s%s:%s:%s%s" % (sign or "", h, mi, s, "." + frac if frac else ""))
    if kind in ("enum", "set"):
        return ("text", _member(value, column))
    if column.json:
        try:
            json.loads(value, parse_constant=_no_constant)
        except ValueError:
            raise ValueError("is not valid JSON.")
        problem = _charset_problem(value, column.charset)
        if problem:
            raise ValueError(problem)
        return ("text", value)
    if column.binary:
        data = value.encode("utf-8")
        limit = column.length if kind in ("binary", "varbinary") else BLOB_BYTES.get(kind)
        if limit is not None and len(data) > limit:
            raise ValueError("is %d bytes long; %s holds at most %d." % (len(data), column.column_type.upper(), limit))
        return ("bytes", data)
    if kind in ("char", "varchar"):
        problem = _charset_problem(value, column.charset)
        if problem:
            raise ValueError(problem)
        if column.length is not None and len(value) > column.length:
            if len(value.rstrip(" ")) > column.length:
                raise ValueError("is %d characters long; %s holds at most %d." % (
                    len(value), column.column_type.upper(), column.length))
            value = value[:column.length]  # only trailing spaces go
        return ("text", value)
    if kind in TEXT_BYTES:
        problem = _charset_problem(value, column.charset)
        if problem:
            raise ValueError(problem)
        size = _encoded_length(value, column.charset)
        if size is not None and size > TEXT_BYTES[kind]:
            raise ValueError("is %d bytes long; %s holds at most %d." % (size, kind.upper(), TEXT_BYTES[kind]))
        return ("text", value)
    raise ValueError("cannot be converted to %s." % column.column_type.upper())


def _no_constant(name):
    raise ValueError(name)


def _kind_word(column):
    kind = column.data_type
    if kind in INTEGER_BITS or kind == "bit":
        return "whole number"
    if kind in ("decimal", "numeric", "float", "double", "real"):
        return "number"
    if kind == "year":
        return "year"
    return "date or time"


def _member(value, column):
    members = column.members or []
    parts = value.split(",") if column.data_type == "set" else [value]
    if column.data_type == "set" and value == "":
        return ""
    found = []
    folding = not (column.collation or "").endswith(("_bin", "_cs")) and column.collation != "binary"
    for part in parts:
        part = part.rstrip(" ")  # the server strips trailing spaces of ENUM and SET values
        match = next((m for m in members if m == part), None)
        if match is None and folding:
            match = next((m for m in members if m.lower() == part.lower()), None)
        if match is None:
            raise ValueError("is not one of the %s members (%s)." % (
                column.data_type.upper(), ", ".join(members[:8]) + (", ..." if len(members) > 8 else "")))
        found.append(match)
    return ",".join(found)


# ---- SQL text ------------------------------------------------------------------------------------

_ESCAPES = {"\\": "\\\\", "'": "''", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\x00": "\\0", "\x1a": "\\Z"}


def hex_text(value):
    return "CONVERT(X'%s' USING utf8mb4)" % value.encode("utf-8").hex().upper()


def sql_text(value, backslash_escapes=None):
    """A string literal for *value* whose meaning does not depend on
    NO_BACKSLASH_ESCAPES unless *backslash_escapes* says which it is (True:
    backslashes escape, False: NO_BACKSLASH_ESCAPES is on, None: unknown).
    Control characters other than the escapable ones go as hex."""
    has_control = bool(_CONTROL.search(value))
    if not has_control and "\\" not in value:
        return "'" + value.replace("'", "''") + "'"
    if backslash_escapes is True and not re.search(r"[\x01-\x08\x0b\x0c\x0e-\x19\x1b-\x1f\x7f]", value):
        return "'" + "".join(_ESCAPES.get(c, c) for c in value) + "'"
    if backslash_escapes is False and not has_control:
        return "'" + value.replace("'", "''") + "'"
    return hex_text(value)


def value_sql(item, backslash_escapes=None):
    kind, value = item
    if kind == "null":
        return "NULL"
    if kind == "num":
        return value
    if kind == "bit":
        return str(int(value))
    if kind == "bytes":
        return "X'%s'" % value.hex().upper()
    return sql_text(value, backslash_escapes)


def row_sql(items, backslash_escapes=None):
    return "(" + ", ".join(value_sql(item, backslash_escapes) for item in items) + ")"


def insert_prefix(schema, table, names):
    return "%sINSERT INTO %s.%s (%s) VALUES " % (TAG, ident(schema), ident(table), ", ".join(ident(n) for n in names))


class Batches(object):
    """Group rows into INSERT statements: at most *max_rows* rows and
    *max_bytes* bytes each (a larger single row goes alone), never across a
    commit after every *every* rows. :meth:`add` and :meth:`finish` return
    the statements that are complete: ``{"sql", "lines", "rows", "values",
    "commit"}`` (``values``: each row's ``(...)`` text; ``commit``: a commit
    follows this statement in "every" mode)."""

    def __init__(self, prefix, max_rows=MAX_ROWS_PER_INSERT, max_bytes=INSERT_BYTES, every=None):
        self.prefix = prefix
        self.prefix_bytes = len(prefix.encode("utf-8"))
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.every = every
        self.rows, self.lines = [], []
        self.size = self.prefix_bytes
        self.in_chunk = 0
        self.count = 0
        self.commits = 0

    def _close(self, commit):
        batch = {"sql": self.prefix + ", ".join(self.rows), "lines": self.lines, "rows": len(self.rows),
                 "values": self.rows, "commit": commit}
        self.rows, self.lines, self.size = [], [], self.prefix_bytes
        self.count += 1
        if commit:
            self.commits += 1
        return batch

    def add(self, row, line):
        out = []
        size = len(row.encode("utf-8")) + (2 if self.rows else 0)
        if self.rows and (len(self.rows) >= self.max_rows or self.size + size > self.max_bytes):
            out.append(self._close(False))
            size = len(row.encode("utf-8"))
        self.rows.append(row)
        self.lines.append(line)
        self.size += size
        self.in_chunk += 1
        if self.every and self.in_chunk >= self.every:
            out.append(self._close(True))
            self.in_chunk = 0
        return out

    def finish(self):
        self.in_chunk = 0
        return [self._close(True)] if self.rows else []


# ---- LOAD DATA ---------------------------------------------------------------------------------

#: CHARACTER SET names LOAD DATA may name here.
LOAD_CHARSETS = ("utf8mb4", "utf8mb3", "utf8", "latin1", "ascii", "binary")
_VARIABLE = re.compile(r"^@[A-Za-z_][A-Za-z0-9_]{0,63}$")
SET_KINDS = {"nullif_empty": "NULLIF(%s, '')", "cast_unsigned": "CAST(%s AS UNSIGNED)"}


def _hex(text):
    return "X'%s'" % text.encode("utf-8").hex().upper() if text else "''"


def load_data_sql(path, schema, table, targets, sets=(), charset="utf8mb4", fields_terminated="\t", enclosed="",
                  optionally=False, escaped="\\", lines_terminated="\n", ignore_lines=0, backslash_escapes=None):
    """The LOAD DATA LOCAL INFILE statement.

    *targets*: the column list in file order, each ``("column", name)`` (always
    backquoted, so a column named ``@x`` stays a column) or ``("variable",
    "@c1")``. *sets*: ``(column, kind, variable)`` with *kind* in
    SET_KINDS (``nullif_empty``: ``NULLIF(@v, '')``; ``cast_unsigned``:
    ``CAST(@v AS UNSIGNED)``, for BIT columns, whose text the server does
    not read as bits). Separators, quote and escape are hexadecimal
    literals; the file name is a plain string literal (the grammar wants
    text there) and is refused with a quote, a backslash under an unknown
    sql_mode, or a control character. CHARACTER SET is always named: without
    it the server reads the file in the database's character set
    (mysql-server sql/sql_load.cc uses collation_database).
    """
    if not isinstance(path, str) or not path or _CONTROL.search(path):
        raise ValueError("The file name for LOAD DATA is not usable.")
    if "\\" in path and backslash_escapes is None:
        raise ValueError("The file name has a backslash and the server's sql_mode is unknown.")
    if charset not in LOAD_CHARSETS:
        raise ValueError("LOAD DATA here reads utf8mb4, utf8mb3, latin1, ascii or binary files.")
    for text, what in ((fields_terminated, "field separator"), (lines_terminated, "line ending")):
        if not isinstance(text, str) or not 1 <= len(text) <= 8:
            raise ValueError("The %s must have 1 to 8 characters." % what)
    if not isinstance(enclosed, str) or len(enclosed) > 1 or not isinstance(escaped, str) or len(escaped) > 1:
        raise ValueError("The quote and the escape character are one character or none.")
    if isinstance(ignore_lines, bool) or not isinstance(ignore_lines, int) or ignore_lines < 0:
        raise ValueError("IGNORE takes a whole number of lines.")
    columns, variables = [], set()
    for target in targets:
        if not isinstance(target, (tuple, list)) or len(target) != 2 or target[0] not in ("column", "variable"):
            raise ValueError("A LOAD DATA target is a column or a user variable.")
        kind, value = target
        if kind == "variable":
            if not isinstance(value, str) or not _VARIABLE.match(value):
                raise ValueError("A user variable must look like @name.")
            columns.append(value)
            variables.add(value)
        else:
            columns.append(ident(check_name(value, "column name")))
    assignments = []
    for column, kind, variable in sets:
        if kind not in SET_KINDS or not _VARIABLE.match(variable) or variable not in variables:
            raise ValueError("A SET clause must use a listed variable and a known conversion.")
        assignments.append("%s = %s" % (ident(check_name(column, "column name")), SET_KINDS[kind] % variable))
    if "\\" in path and backslash_escapes:
        literal = "'" + path.replace("\\", "\\\\").replace("'", "''") + "'"
    else:
        literal = "'" + path.replace("'", "''") + "'"
    sql = ("%sLOAD DATA LOCAL INFILE %s INTO TABLE %s.%s CHARACTER SET %s FIELDS TERMINATED BY %s %sENCLOSED BY %s "
           "ESCAPED BY %s LINES TERMINATED BY %s" % (
               TAG, literal, ident(schema), ident(table), charset, _hex(fields_terminated),
               "OPTIONALLY " if optionally and enclosed else "", _hex(enclosed), _hex(escaped),
               _hex(lines_terminated)))
    if ignore_lines:
        sql += " IGNORE %d LINES" % ignore_lines
    sql += " (%s)" % ", ".join(columns)
    if assignments:
        sql += " SET " + ", ".join(assignments)
    return sql


_TSV_ESCAPES = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r", "\x00": "\\0"}


def tsv_value(item):
    """One field of the checked copy LOAD DATA reads: tab separated, no
    quotes, backslash escapes, ``\\N`` for NULL (the server's defaults:
    FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\', mysql-server
    sql/query_result.cc, mariadb-server sql/sql_class.cc)."""
    kind, value = item
    if kind == "null":
        return "\\N"
    if kind in ("num", "bit"):
        return str(value)
    if kind == "bytes":
        value = value.decode("utf-8")
    return "".join(_TSV_ESCAPES.get(c, c) for c in value)


def tsv_row(items):
    return "\t".join(tsv_value(item) for item in items) + "\n"


def load_targets(columns):
    """The LOAD DATA column list and SET clauses for the mapped columns, in
    order: a BIT column goes through a user variable and CAST(@v AS UNSIGNED)."""
    targets, sets = [], []
    for index, column in enumerate(columns):
        if column.data_type == "bit":
            variable = "@c%d" % (index + 1)
            targets.append(("variable", variable))
            sets.append((column.name, "cast_unsigned", variable))
        else:
            targets.append(("column", column.name))
    return targets, sets


# ---- inference -----------------------------------------------------------------------------------

class Infer(object):
    """What a column's values look like, for a new table's type."""

    def __init__(self):
        self.count = 0
        self.nulls = 0
        self.max_len = 0
        self.max_bytes = 0
        self.int_ok = self.dec_ok = self.float_ok = self.bool_ok = True
        self.date_ok = self.datetime_ok = self.time_ok = self.json_ok = True
        self.leading_zero = False
        self.int_min = self.int_max = 0
        self.int_digits = self.scale = 0
        self.fsp = 0
        self.has_time = False
        self.samples = []

    def add(self, raw, fmt):
        if is_null(raw, fmt):
            self.nulls += 1
            return
        if len(self.samples) < SAMPLES and raw not in self.samples:
            self.samples.append(raw[:VALUE_PREVIEW])
        self.count += 1
        self.max_len = max(self.max_len, len(raw))
        self.max_bytes = max(self.max_bytes, len(raw.encode("utf-8")))
        value = raw.strip(" \t")  # as convert() strips numbers, dates and booleans
        if self.bool_ok and _boolean(value) is None:
            self.bool_ok = False
        if self.int_ok:
            if _INT.match(value):
                number = int(value)
                digits = value.lstrip("+-")
                if len(digits) > 1 and digits.startswith("0"):
                    self.leading_zero = True
                self.int_min, self.int_max = (number, number) if self.count == 1 else (
                    min(self.int_min, number), max(self.int_max, number))
            else:
                self.int_ok = False
        if self.dec_ok:
            match = _DEC.match(value)
            if match and (match.group(2) or match.group(3)):
                whole = (match.group(2) or "").lstrip("0")
                if len(whole) > 1 and (match.group(2) or "").startswith("0"):
                    self.leading_zero = True
                self.int_digits = max(self.int_digits, len(whole))
                self.scale = max(self.scale, len(match.group(3) or ""))
            else:
                self.dec_ok = False
        if self.float_ok and not _FLOAT.match(value):
            self.float_ok = False
        if self.date_ok and not _valid(_DATE, value):
            self.date_ok = False
        if self.datetime_ok:
            match = _DATETIME.match(value)
            if match and _calendar(match):
                if match.group(4) is not None:
                    self.has_time = True
                    self.fsp = max(self.fsp, min(6, len(match.group(7) or "")))
            else:
                self.datetime_ok = False
        if self.time_ok:
            match = _TIME.match(value)
            if match and int(match.group(3)) <= 59 and int(match.group(4)) <= 59 and int(match.group(2)) <= 838:
                self.fsp = max(self.fsp, min(6, len(match.group(5) or "")))
            else:
                self.time_ok = False
        if self.json_ok:
            if value[:1] in ("{", "["):
                try:
                    json.loads(value, parse_constant=_no_constant)
                except ValueError:
                    self.json_ok = False
            else:
                self.json_ok = False

    def sql_type(self):
        if self.count == 0:
            return "VARCHAR(255)"
        if self.bool_ok:
            return "TINYINT(1)"
        if self.int_ok and not self.leading_zero:
            if -2 ** 31 <= self.int_min and self.int_max < 2 ** 31:
                return "INT"
            if -2 ** 63 <= self.int_min and self.int_max < 2 ** 63:
                return "BIGINT"
            if self.int_min >= 0 and self.int_max < 2 ** 64:
                return "BIGINT UNSIGNED"
            if max(len(str(abs(self.int_min))), len(str(self.int_max))) <= 65:
                return "DECIMAL(%d,0)" % max(len(str(abs(self.int_min))), len(str(self.int_max)))
        if self.dec_ok and not self.leading_zero:
            precision = max(1, self.int_digits + self.scale)
            if precision <= 65 and self.scale <= 30:
                return "DECIMAL(%d,%d)" % (precision, self.scale)
        if self.float_ok and not self.leading_zero:
            return "DOUBLE"
        if self.date_ok:
            return "DATE"
        if self.datetime_ok:
            return "DATETIME(%d)" % self.fsp if self.fsp else "DATETIME"
        if self.time_ok:
            return "TIME(%d)" % self.fsp if self.fsp else "TIME"
        if self.json_ok:
            return "JSON"
        return text_type(self.max_len, self.max_bytes)

    def public(self, partial):
        return {"sql_type": self.sql_type(), "nullable": bool(self.nulls) or partial or self.count == 0,
                "max_length": self.max_len}


def _valid(pattern, value):
    match = pattern.match(value)
    if not match:
        return False
    try:
        datetime.date(*(int(x) for x in match.groups()[:3]))
    except ValueError:
        return False
    return True


def _calendar(match):
    try:
        datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return False
    if match.group(4) is not None and (int(match.group(4)) > 23 or int(match.group(5)) > 59
                                       or int(match.group(6)) > 59):
        return False
    return True


_VARCHAR_STEPS = (16, 32, 64, 128, 255, 512, 1024, 2048, 4096, 8192, 16383)


def text_type(max_len, max_bytes):
    for step in _VARCHAR_STEPS:
        if max(max_len, 1) <= step:
            return "VARCHAR(%d)" % step
    for name, limit in (("TEXT", 65535), ("MEDIUMTEXT", 16777215)):
        if max_bytes <= limit:
            return name
    return "LONGTEXT"


#: The server's 65,535-byte row size limit (every engine) counts a VARCHAR at
#: 4 bytes per utf8mb4 character plus its length bytes; keep the inferred
#: columns well under it.
ROW_BYTES = 60000


def fit_row(types):
    """Turn the widest VARCHAR columns into TEXT until the row fits."""
    types = list(types)

    def width(text):
        match = re.match(r"^VARCHAR\((\d+)\)$", text)
        return int(match.group(1)) * 4 + 2 if match else 0

    while sum(width(t) for t in types) > ROW_BYTES:
        widest = max(range(len(types)), key=lambda i: width(types[i]))
        if not width(types[widest]):
            break
        types[widest] = "TEXT"
    return types


# ---- create table ----------------------------------------------------------------------------

def create_table_sql(schema, table, columns, add_id_key=False, id_name="id"):
    """CREATE TABLE for a new table: the columns (ColumnType with names, in
    order), an optional ``id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY``
    first, InnoDB, utf8mb4. CREATE TABLE commits implicitly (mysql-server
    sql/sql_parse.cc and mariadb-server sql/sql_parse.cc give SQLCOM_CREATE_TABLE
    CF_AUTO_COMMIT_TRANS)."""
    lines = []
    if add_id_key:
        lines.append("  %s BIGINT UNSIGNED NOT NULL AUTO_INCREMENT" % ident(id_name))
    for column in columns:
        lines.append("  %s %s %s" % (ident(column.name), render_type(column), "NULL" if column.nullable else "NOT NULL"))
    if add_id_key:
        lines.append("  PRIMARY KEY (%s)" % ident(id_name))
    return "%sCREATE TABLE %s.%s (\n%s\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" % (
        TAG, ident(schema), ident(table), ",\n".join(lines))


# ---- small helpers -----------------------------------------------------------------------------

def preview_value(value):
    if value is None:
        return None
    return value if len(value) <= VALUE_PREVIEW else value[:VALUE_PREVIEW - 1] + "…"


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            data = handle.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def number_text(value):
    return "{:,}".format(value)


def decimal_ok(text):
    try:
        return decimal.Decimal(text).is_finite()
    except decimal.InvalidOperation:
        return False
