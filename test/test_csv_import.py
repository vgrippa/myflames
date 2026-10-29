"""Import CSV, the pure part (myflames/csv_import.py): reading, detection,
inference, conversion of every column type, quoting and statement builders.

No server and no network. Server behaviour these rules rely on is cited in
the module docstring and checked live by scripts/lab-csv-import.py.
"""
import codecs
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from myflames import csv_import as ci  # noqa: E402


def fmt(**values):
    return ci.check_format(values)


def records(text, encoding='utf-8', **values):
    data = text if isinstance(text, bytes) else text.encode(encoding)
    return list(ci.Records(io.BytesIO(data), fmt(encoding=encoding if encoding != 'utf-16' else 'utf-16-le',
                                                 **values)))


def col(type_text, **values):
    column = ci.parse_type(type_text)
    column.name = values.pop('name', 'c')
    for key, value in values.items():
        setattr(column, key, value)
    return column


def info(**row):
    base = {'COLUMN_NAME': 'c', 'DATA_TYPE': 'varchar', 'COLUMN_TYPE': 'varchar(10)', 'IS_NULLABLE': 'YES',
            'COLUMN_DEFAULT': None, 'EXTRA': '', 'CHARACTER_SET_NAME': 'utf8mb4',
            'COLLATION_NAME': 'utf8mb4_0900_ai_ci', 'CHARACTER_MAXIMUM_LENGTH': None, 'NUMERIC_PRECISION': None,
            'NUMERIC_SCALE': None, 'DATETIME_PRECISION': None}
    base.update(row)
    return ci.from_information_schema(base)


class FormatTest(unittest.TestCase):
    def test_defaults_and_every_key(self):
        value = ci.check_format({})
        self.assertEqual(list(value), list(ci.FORMAT_KEYS))
        self.assertEqual(value['null_markers'], ['\\N'])

    def test_refusals(self):
        for bad in ({'encoding': 'utf-32'}, {'separator': ''}, {'separator': ';;'}, {'separator': '\n'},
                    {'quote': '`'}, {'escape': '/'}, {'header': 'yes'}, {'skip_lines': -1},
                    {'skip_lines': 1001}, {'skip_lines': True}, {'empty': 'blank'}, {'null_markers': ['nil']},
                    {'separator': '"'}, {'unknown': 1}, {'line_ending': '\n\n'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ci.check_format(bad)

    def test_custom_separator_and_none_values(self):
        value = ci.check_format({'separator': '^', 'quote': None, 'escape': None, 'null_markers': ['NULL', '\\N']})
        self.assertEqual((value['separator'], value['quote'], value['escape']), ('^', '', ''))
        self.assertEqual(value['null_markers'], ['\\N', 'NULL'])


class ReadingTest(unittest.TestCase):
    def test_quoted_separators_quotes_and_newlines_keep_line_numbers(self):
        text = 'a,b\n1,"x, y"\n2,"say ""hi"""\n3,"two\nlines"\n\n4,"crlf\r\ninside"\r\n5,end\n'
        got = records(text)
        self.assertEqual(got, [(1, ['a', 'b']), (2, ['1', 'x, y']), (3, ['2', 'say "hi"']),
                               (4, ['3', 'two\nlines']), (7, ['4', 'crlf\r\ninside']), (9, ['5', 'end'])])

    def test_tab_semicolon_pipe_and_custom_separators(self):
        for sep in ('\t', ';', '|', '^', '#'):
            with self.subTest(sep=sep):
                got = records('a%sb\n1%s"q%s"\n' % (sep, sep, sep), separator=sep)
                self.assertEqual(got[1], (2, ['1', 'q%s' % sep]))

    def test_skip_lines_counts_physical_lines(self):
        got = records('exported by tool\nsecond note\na,b\n1,2\n', skip_lines=2)
        self.assertEqual(got, [(3, ['a', 'b']), (4, ['1', '2'])])

    def test_backslash_escape_and_no_quote(self):
        self.assertEqual(records('a,b\n1,x\\,y\n', escape='\\')[1], (2, ['1', 'x,y']))
        self.assertEqual(records('a,b\n1,"x"\n', quote='')[1], (2, ['1', '"x"']))

    def test_text_after_closing_quote_is_a_problem_with_its_line(self):
        got = records('a,b\n1,ok\n2,"bad"x\n3,ok\n')
        self.assertEqual(got[2][0], 3)
        self.assertIsInstance(got[2][1], ci.CsvProblem)
        self.assertIn('closing quote', got[2][1].message)
        self.assertEqual(got[3], (4, ['3', 'ok']))

    def test_boms_and_utf16(self):
        self.assertEqual(records(b'\xef\xbb\xbfa,b\n1,2\n', encoding='utf-8-sig')[0], (1, ['a', 'b']))
        # A BOM read as plain UTF-8 is still dropped.
        self.assertEqual(records(b'\xef\xbb\xbfa,b\n', encoding='utf-8')[0], (1, ['a', 'b']))
        data = codecs.BOM_UTF16_LE + 'a\tb\n\u00e9\t\u20ac\n'.encode('utf-16-le')
        self.assertEqual(records(data, encoding='utf-16-le', separator='\t'), [(1, ['a', 'b']), (2, ['\u00e9', '\u20ac'])])
        data = codecs.BOM_UTF16_BE + 'a,b\n1,2\n'.encode('utf-16-be')
        self.assertEqual(records(data, encoding='utf-16-be')[1], (2, ['1', '2']))

    def test_cp1252_decodes_like_mysql_latin1(self):
        got = records(b'n\ncaf\xe9 \x80 \x81\n', encoding='cp1252')
        self.assertEqual(got[1], (2, ['caf\u00e9 \u20ac \x81']))
        self.assertIn('\x81', ci.LATIN1_CHARS)
        self.assertIn('\u20ac', ci.LATIN1_CHARS)
        self.assertNotIn('\u0100', ci.LATIN1_CHARS)

    def test_an_over_long_line_is_a_problem_not_a_memory_spike(self):
        data = b'a,b\n1,2\n' + b'x' * 5000 + b',3\n4,5\n'
        with mock.patch.object(ci, 'LINE_LIMIT', 1000):
            with self.assertRaises(ci.LongLine) as caught:
                list(ci.Records(io.BytesIO(data), fmt()))
        self.assertEqual(caught.exception.line, 3)
        self.assertIsInstance(caught.exception, ci.ReadProblem)
        with mock.patch.object(ci, 'LINE_LIMIT', 1000):
            # Exactly at the limit is fine, with or without a final newline.
            self.assertEqual(len(list(ci.Records(io.BytesIO(b'a\n' + b'y' * 999 + b'\n'), fmt()))), 2)
            self.assertEqual(len(list(ci.Records(io.BytesIO(b'a\n' + b'y' * 1000), fmt()))), 2)

    def test_header_rule(self):
        header = {}
        rows = list(ci.split_header(ci.Records(io.BytesIO(b'"bad"x,y\nname,city\na,b\n'), fmt()), fmt(), header))
        self.assertEqual((header['line'], header['fields']), (1, None))
        self.assertIn('closing quote', header['problem'])
        self.assertEqual(rows, [(2, ['name', 'city']), (3, ['a', 'b'])])
        header = {}
        rows = list(ci.split_header(ci.Records(io.BytesIO(b'n,c\n1,2\n'), fmt(header=False)), fmt(header=False),
                                    header))
        self.assertEqual((header, len(rows)), ({}, 2))

    def test_decode_error_names_the_exact_line(self):
        data = ('a,b\n' + 'x,1\n' * 20000 + 'y\xff,2\n').encode('latin-1')
        with self.assertRaises(ci.DecodeProblem) as caught:
            list(ci.Records(io.BytesIO(data), fmt()))
        self.assertEqual(caught.exception.line, 20002)
        data = b'a\r\nb\r\n\xff\r\n'
        self.assertEqual(ci.bad_line(io.BytesIO(data), 'utf-8'), 3)

    def test_hash_covers_the_whole_file(self):
        import hashlib
        data = b'a,b\n1,2\n' * 1000
        digest = hashlib.sha256()
        reader = ci.Records(io.BytesIO(data), fmt(), digest)
        list(reader)
        reader.drain()
        self.assertEqual(digest.hexdigest(), hashlib.sha256(data).hexdigest())
        self.assertEqual(reader.count, len(data))


class DetectionTest(unittest.TestCase):
    def test_separators(self):
        for sep in (',', ';', '\t', '|'):
            with self.subTest(sep=sep):
                data = ('id{0}name{0}price\n1{0}"a{0}b"{0}1.5\n2{0}c{0}2\n3{0}d{0}3\n').format(sep).encode()
                value, _ = ci.detect(data, True)
                self.assertEqual(value['separator'], sep)
                self.assertTrue(value['header'])

    def test_header_detection(self):
        self.assertFalse(ci.detect(b'1,2,3\n4,5,6\n', True)[0]['header'])
        self.assertTrue(ci.detect(b'name,city\nann,paris\nbob,rome\n', True)[0]['header'])
        self.assertFalse(ci.detect(b'2024-01-01,5\n2024-01-02,6\n', True)[0]['header'])
        self.assertFalse(ci.detect(b'a,a\n1,2\n', True)[0]['header'])

    def test_encodings_and_line_endings(self):
        self.assertEqual(ci.detect(b'\xef\xbb\xbfa,b\r\n1,2\r\n', True)[0]['encoding'], 'utf-8-sig')
        self.assertEqual(ci.detect(b'\xef\xbb\xbfa,b\r\n1,2\r\n', True)[0]['line_ending'], '\r\n')
        self.assertEqual(ci.detect(codecs.BOM_UTF16_LE + 'a,b\n'.encode('utf-16-le'), True)[0]['encoding'], 'utf-16-le')
        self.assertEqual(ci.detect(codecs.BOM_UTF16_BE + 'a,b\n'.encode('utf-16-be'), True)[0]['encoding'], 'utf-16-be')
        value, notes = ci.detect('a,b\n1,2\n'.encode('utf-16-le'), True)
        self.assertEqual(value['encoding'], 'utf-16-le')
        self.assertTrue(notes)
        value, notes = ci.detect('n\ncaf\xe9\n'.encode('cp1252'), True)
        self.assertEqual(value['encoding'], 'cp1252')
        self.assertIn('not valid UTF-8', notes[0])
        # A multi-byte character cut by the sample's end is still UTF-8.
        self.assertEqual(ci.detect('a\n\u00e9'.encode('utf-8')[:-1], False)[0]['encoding'], 'utf-8')
        self.assertEqual(ci.detect(b'a\rb\r', True)[0]['line_ending'], '\r')

    def test_null_word_suggests_the_marker(self):
        value, notes = ci.detect(b'id,v\n1,NULL\n2,x\n', True)
        self.assertEqual(value['null_markers'], ['\\N', 'NULL'])
        self.assertTrue(any('NULL' in n for n in notes))

    def test_sample_rows_uses_the_chosen_format(self):
        rows = ci.sample_rows(b'id^name\n1^a\n', fmt(separator='^'), True)
        self.assertTrue(ci.detect_header(rows))


class TypesTest(unittest.TestCase):
    def test_parse_and_render(self):
        for text, rendered in (('int', 'INT'), ('INTEGER unsigned', 'INT UNSIGNED'), ('bigint(20) unsigned', 'BIGINT UNSIGNED'),
                               ('tinyint(1)', 'TINYINT(1)'), ('boolean', 'TINYINT(1)'), ('decimal', 'DECIMAL(10,0)'),
                               ('numeric(12, 3)', 'DECIMAL(12,3)'), ('double', 'DOUBLE'), ('datetime(6)', 'DATETIME(6)'),
                               ('timestamp', 'TIMESTAMP'), ('time(3)', 'TIME(3)'), ('year', 'YEAR'),
                               ('varchar(255)', 'VARCHAR(255)'), ('char(0)', 'CHAR(0)'), ('longtext', 'LONGTEXT'),
                               ('varbinary(16)', 'VARBINARY(16)'), ('bit(64)', 'BIT(64)'), ('json', 'JSON'),
                               ("enum('a','it''s')", "ENUM('a','it''s')"), ("set('x','y')", "SET('x','y')")):
            with self.subTest(text=text):
                self.assertEqual(ci.render_type(ci.parse_type(text)), rendered)

    def test_parse_type_refuses_anything_else(self):
        for bad in ('geometry', 'int; DROP TABLE t', 'varchar', 'varchar(16384)', 'decimal(66,2)', 'decimal(5,6)',
                    'datetime(7)', 'bit(65)', "enum('a\\b')", "enum('a','A')", 'text(10)', 'double(5,2)',
                    'varchar(10) unsigned', 'int(10) zerofill', '', None, 'tinyint(1,2)', 'decimal(\u0661\u0660)',
                    'decimal(65,39)',
                    "enum('a') comment 'x'", 'vector(3)'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ci.parse_type(bad)

    def test_engine_rules(self):
        with self.assertRaisesRegex(ValueError, 'MySQL deprecates UNSIGNED'):
            ci.parse_type('decimal(5,2) unsigned', 'mysql')
        with self.assertRaisesRegex(ValueError, 'MySQL deprecates UNSIGNED'):
            ci.parse_type('double unsigned', 'mysql')
        self.assertEqual(ci.render_type(ci.parse_type('decimal(5,2) unsigned', 'mariadb')), 'DECIMAL(5,2) UNSIGNED')
        self.assertEqual(ci.render_type(ci.parse_type('double unsigned', 'mariadb')), 'DOUBLE UNSIGNED')
        self.assertEqual(ci.parse_type('decimal(40,38)', 'mariadb').scale, 38)
        with self.assertRaisesRegex(ValueError, 'scale 0 to 30 on MySQL'):
            ci.parse_type('decimal(40,31)', 'mysql')

    def test_members_from_the_server(self):
        self.assertEqual(ci.parse_members("'a','it''s','back\\\\slash','new\\nline',''"),
                         ['a', "it's", 'back\\slash', 'new\nline', ''])

    def test_information_schema_columns(self):
        c = info(DATA_TYPE='tinyint', COLUMN_TYPE='tinyint(1)', IS_NULLABLE='NO')
        self.assertTrue(c.boolean)
        self.assertTrue(c.required)
        self.assertFalse(info(DATA_TYPE='tinyint', COLUMN_TYPE='tinyint(1) unsigned').boolean)
        self.assertFalse(info(DATA_TYPE='tinyint', COLUMN_TYPE='tinyint').boolean)
        self.assertFalse(info(DATA_TYPE='int', COLUMN_TYPE='int', IS_NULLABLE='NO', EXTRA='auto_increment').required)
        self.assertFalse(info(DATA_TYPE='int', COLUMN_TYPE='int', IS_NULLABLE='NO', COLUMN_DEFAULT='0').required)
        generated = info(DATA_TYPE='int', COLUMN_TYPE='int', EXTRA='STORED GENERATED')
        self.assertIn('generated', generated.unsupported)
        self.assertIn('generated', info(DATA_TYPE='int', COLUMN_TYPE='int', EXTRA='VIRTUAL GENERATED').unsupported)
        # MariaDB shows its system-versioning ROW START/END columns as STORED GENERATED.
        self.assertIn('generated', info(DATA_TYPE='timestamp', COLUMN_TYPE='timestamp(6)',
                                        EXTRA='STORED GENERATED INVISIBLE').unsupported)
        # MySQL marks an expression default DEFAULT_GENERATED: an ordinary column.
        stamped = info(DATA_TYPE='timestamp', COLUMN_TYPE='timestamp', IS_NULLABLE='NO',
                       COLUMN_DEFAULT='CURRENT_TIMESTAMP', EXTRA='DEFAULT_GENERATED on update CURRENT_TIMESTAMP')
        self.assertEqual(stamped.unsupported, '')
        self.assertFalse(stamped.required)
        self.assertIn('GEOMETRY', info(DATA_TYPE='geometry', COLUMN_TYPE='geometry').unsupported)
        # MariaDB's JSON is LONGTEXT with CHECK (json_valid(`c`)).
        row = {'COLUMN_NAME': 'doc', 'DATA_TYPE': 'longtext', 'COLUMN_TYPE': 'longtext', 'IS_NULLABLE': 'YES'}
        self.assertTrue(ci.from_information_schema(row, {'doc'}).json)
        self.assertFalse(ci.from_information_schema(row).json)
        self.assertEqual(info(DATA_TYPE='decimal', COLUMN_TYPE='decimal(8,3)', NUMERIC_PRECISION='8',
                              NUMERIC_SCALE='3').scale, 3)
        self.assertEqual(info(DATA_TYPE='datetime', COLUMN_TYPE='datetime(3)', DATETIME_PRECISION='3').fsp, 3)
        self.assertEqual(info(DATA_TYPE='enum', COLUMN_TYPE="enum('a','b')").members, ['a', 'b'])


class ConvertTest(unittest.TestCase):
    def ok(self, value, column, expected, **values):
        self.assertEqual(ci.convert(value, column, fmt(**values)), expected)

    def bad(self, value, column, words, **values):
        with self.assertRaises(ValueError) as caught:
            ci.convert(value, column, fmt(**values))
        self.assertIn(words, str(caught.exception))

    def test_null_markers_and_empty(self):
        c = col('varchar(10)')
        self.ok('', c, ('null', None))
        self.ok('\\N', c, ('null', None))
        self.ok('NULL', c, ('text', 'NULL'))
        self.ok('NULL', c, ('null', None), null_markers=['NULL'])
        self.ok('', c, ('text', ''), empty='empty')
        self.bad('', col('varchar(10)', nullable=False), 'does not accept NULL')
        self.ok('', col('int', nullable=False, auto_increment=True), ('null', None))
        self.bad('', col('int'), 'is empty', empty='empty')
        self.ok('  ', col('int'), ('null', None))

    def test_integers(self):
        for kind, low, high in (('tinyint', -128, 127), ('smallint', -32768, 32767), ('mediumint', -8388608, 8388607),
                                ('int', -2147483648, 2147483647), ('bigint', -2 ** 63, 2 ** 63 - 1)):
            with self.subTest(kind=kind):
                self.ok(str(low), col(kind), ('num', str(low)))
                self.ok(' +%d ' % high, col(kind), ('num', str(high)))
                self.bad(str(high + 1), col(kind), 'outside the range')
                self.bad(str(low - 1), col(kind), 'outside the range')
                self.ok(str(2 * high + 1), col(kind + ' unsigned'), ('num', str(2 * high + 1)))
                self.bad('-1', col(kind + ' unsigned'), 'outside the range')
        self.ok('18446744073709551615', col('bigint unsigned'), ('num', '18446744073709551615'))
        self.bad('18446744073709551616', col('bigint unsigned'), 'outside the range')
        self.ok('007', col('int'), ('num', '7'))
        for value in ('1.0', '1e3', '0x10', '1,000', 'abc', '1_000', '12\n', '\uff11\uff12', '\u0661\u0662'):
            self.bad(value, col('int'), 'whole number')
        self.bad('true', col('tinyint'), 'whole number')

    def test_booleans(self):
        for value, number in (('true', '1'), ('FALSE', '0'), ('yes', '1'), ('No', '0'), ('on', '1'), ('off', '0'),
                              ('t', '1'), ('f', '0'), ('y', '1'), ('n', '0'), ('1', '1'), ('0', '0'), ('5', '5')):
            self.ok(value, col('tinyint(1)'), ('num', number))
        self.ok('true', col('bit(1)'), ('bit', 1))
        self.bad('true', col('bit(2)'), 'whole number')

    def test_decimal(self):
        c = col('decimal(5,2)')
        self.ok('123.45', c, ('num', '123.45'))
        self.ok('-0.5', c, ('num', '-0.5'))
        self.ok('.5', c, ('num', '0.5'))
        self.ok('1.230', c, ('num', '1.23'))
        self.ok('-0.00', c, ('num', '0'))
        self.ok('00012', c, ('num', '12'))
        self.bad('1234.5', c, 'does not fit')
        self.bad('1.234', c, 'round')
        for value in ('1e3', '1,5', '.', '-', 'abc', '1.2.3', '1.5\n', '\u0661\u0662\u0663'):
            self.bad(value, c, 'not a decimal')
        self.ok('42', col('decimal(65,0)'), ('num', '42'))
        self.ok('9' * 65, col('decimal(65,0)'), ('num', '9' * 65))
        unsigned = col('decimal(5,2)', unsigned=True)
        self.bad('-1', unsigned, 'negative')

    def test_float_and_double(self):
        self.ok('1e10', col('double'), ('num', '1e10'))
        self.ok('+1.5', col('double'), ('num', '1.5'))
        self.ok('-.25', col('float'), ('num', '-.25'))
        self.bad('1e39', col('float'), 'range')
        self.bad('1e309', col('double'), 'range')
        for value in ('nan', 'inf', 'Infinity', '1e', '--1', '0x1p3', '1.5\n', '1_0', '\u0661.\u0665'):
            self.bad(value, col('double'), 'not a number')

    def test_dates_and_times(self):
        self.ok('2024-02-29', col('date'), ('text', '2024-02-29'))
        self.bad('2023-02-29', col('date'), 'calendar')
        for value in ('2024/01/02', '02-01-2024', '2024-1-2', '20240102', '2024-01-02\n',
                      '\u0662\u0660\u0662\u0666-\u0660\u0661-\u0660\u0662'):
            self.bad(value, col('date'), 'YYYY-MM-DD')
        self.bad('12:00:00\n', col('time'), 'HH:MM:SS')
        self.bad('2024\n', col('year'), '1901 to 2155')
        self.ok('2024-01-02', col('datetime'), ('text', '2024-01-02 00:00:00'))
        self.ok('2024-01-02T03:04:05', col('datetime'), ('text', '2024-01-02 03:04:05'))
        self.ok('2024-01-02 03:04:05.120', col('datetime(2)'), ('text', '2024-01-02 03:04:05.12'))
        self.bad('2024-01-02 03:04:05.123', col('datetime(2)'), 'rounded')
        self.ok('2024-01-02 03:04:05.000', col('datetime'), ('text', '2024-01-02 03:04:05'))
        self.bad('2024-01-02 24:00:00', col('datetime'), 'time of day')
        self.bad('2024-01-02 03:04:05+01:00', col('datetime'), 'YYYY-MM-DD HH:MM:SS')
        self.ok('2038-01-19 03:14:07', col('timestamp'), ('text', '2038-01-19 03:14:07'))
        self.bad('2040-01-01 00:00:00', col('timestamp'), 'TIMESTAMP range')
        self.bad('1969-12-31 00:00:00', col('timestamp'), 'TIMESTAMP range')
        self.ok('2100-01-01 00:00:00', col('timestamp', timestamp_max_year=2106), ('text', '2100-01-01 00:00:00'))
        self.ok('838:59:59', col('time'), ('text', '838:59:59'))
        self.ok('-838:59:59', col('time'), ('text', '-838:59:59'))
        self.ok('12:00:00.5', col('time(1)'), ('text', '12:00:00.5'))
        self.bad('839:00:00', col('time'), 'TIME range')
        self.bad('838:59:59.5', col('time(1)'), 'TIME range')
        self.bad('10:60:00', col('time'), 'TIME range')
        self.ok('1901', col('year'), ('num', '1901'))
        self.ok('2155', col('year'), ('num', '2155'))
        for value in ('1900', '2156', '24', '0000'):
            self.bad(value, col('year'), '1901 to 2155')

    def test_text_lengths_and_character_sets(self):
        self.ok('\U0001F600' * 3, col('varchar(3)'), ('text', '\U0001F600' * 3))
        self.bad('abcd', col('varchar(3)'), '4 characters')
        self.ok('ab   ', col('char(3)'), ('text', 'ab '))
        self.ok(' keep ', col('varchar(10)'), ('text', ' keep '))
        self.bad('\U0001F600', col('varchar(3)', charset='utf8mb3'), 'utf8mb3')
        self.ok('caf\u00e9 \u20ac', col('varchar(10)', charset='latin1'), ('text', 'caf\u00e9 \u20ac'))
        self.bad('\u0100', col('varchar(10)', charset='latin1'), 'latin1')
        self.bad('\u00e9', col('varchar(10)', charset='ascii'), 'ascii')
        self.ok('\u00e9' * 127, col('tinytext'), ('text', '\u00e9' * 127))
        self.bad('\u00e9' * 128, col('tinytext'), '256 bytes')
        self.ok('x' * 255, col('tinytext', charset='latin1'), ('text', 'x' * 255))

    def test_binary(self):
        self.ok('abc', col('varbinary(3)'), ('bytes', b'abc'))
        self.bad('\u00e9\u00e9', col('varbinary(3)'), '4 bytes')
        self.ok('', col('blob'), ('bytes', b''), empty='empty')
        self.ok('0x41', col('binary(4)'), ('bytes', b'0x41'))

    def test_enum_and_set(self):
        c = info(COLUMN_NAME='e', DATA_TYPE='enum', COLUMN_TYPE="enum('red','Green')")
        self.ok('red', c, ('text', 'red'))
        self.ok('GREEN ', c, ('text', 'Green'))
        self.bad('blue', c, 'not one of')
        self.bad('1', c, 'not one of')
        binary = info(COLUMN_NAME='e', DATA_TYPE='enum', COLUMN_TYPE="enum('red')", COLLATION_NAME='utf8mb4_bin')
        self.bad('RED', binary, 'not one of')
        s = info(COLUMN_NAME='s', DATA_TYPE='set', COLUMN_TYPE="set('a','b','c')")
        self.ok('c,a', s, ('text', 'c,a'))
        self.ok('', s, ('text', ''), empty='empty')
        self.bad('a,d', s, 'not one of')

    def test_bit(self):
        self.ok('15', col('bit(4)'), ('bit', 15))
        self.bad('16', col('bit(4)'), 'does not fit')
        self.bad('-1', col('bit(4)'), 'whole number')

    def test_json(self):
        self.ok('{"a": [1, null]}', col('json'), ('text', '{"a": [1, null]}'))
        self.ok('"text"', col('json'), ('text', '"text"'))
        for value in ('{a: 1}', 'NaN', '[1, Infinity]', "{'a': 1}"):
            self.bad(value, col('json'), 'not valid JSON')
        self.bad('', col('json', nullable=False), 'not valid JSON', empty='empty')
        self.bad('', col('json', nullable=False), 'does not accept NULL')

    def test_unsupported_type_is_refused(self):
        self.bad('POINT(1 1)', info(DATA_TYPE='point', COLUMN_TYPE='point'), 'cannot be converted')


class SqlTextTest(unittest.TestCase):
    def test_literals_in_every_mode(self):
        self.assertEqual(ci.sql_text("it's"), "'it''s'")
        for mode in (None, True, False):
            self.assertEqual(ci.sql_text('plain', mode), "'plain'")
        self.assertEqual(ci.sql_text('a\\b', None), "CONVERT(X'615C62' USING utf8mb4)")
        self.assertEqual(ci.sql_text('a\\b', True), "'a\\\\b'")
        self.assertEqual(ci.sql_text('a\\b', False), "'a\\b'")
        self.assertEqual(ci.sql_text("x\n'y'\t\x00\x1a", True), "'x\\n''y''\\t\\0\\Z'")
        self.assertEqual(ci.sql_text('x\ny', False), "CONVERT(X'780A79' USING utf8mb4)")
        self.assertEqual(ci.sql_text('bell\x07', True), "CONVERT(X'62656C6C07' USING utf8mb4)")
        self.assertEqual(ci.sql_text('\u00e9\U0001F600'), "'\u00e9\U0001F600'")

    def test_values_rows_and_prefix(self):
        items = [('null', None), ('num', '-1.5'), ('bit', 5), ('bytes', b'\x00A'), ('text', "o'k"), ('bytes', b'')]
        self.assertEqual(ci.row_sql(items), "(NULL, -1.5, 5, X'0041', 'o''k', X'')")
        self.assertEqual(ci.insert_prefix('d`b', 't', ['a b', 'c`']),
                         "/*myflames:import_csv*/ INSERT INTO `d``b`.`t` (`a b`, `c```) VALUES ")


class BatchesTest(unittest.TestCase):
    def collect(self, batches, rows):
        out = []
        for i, row in enumerate(rows):
            out += batches.add(row, i + 2)
        return out + batches.finish()

    def test_row_limit_and_commit_boundaries(self):
        out = self.collect(ci.Batches('P ', max_rows=3, every=5), ['(%d)' % i for i in range(12)])
        self.assertEqual([b['rows'] for b in out], [3, 2, 3, 2, 2])
        self.assertEqual([b['commit'] for b in out], [False, True, False, True, True])
        self.assertEqual(out[0]['sql'], 'P (0), (1), (2)')
        self.assertEqual(out[1]['lines'], [5, 6])
        self.assertEqual(out[0]['values'], ['(0)', '(1)', '(2)'])

    def test_byte_budget_and_one_large_row(self):
        out = self.collect(ci.Batches('P ', max_rows=1000, max_bytes=20), ['(aaaa)', '(bbbb)', '(' + 'x' * 50 + ')',
                                                                          '(c)'])
        self.assertEqual([b['rows'] for b in out], [2, 1, 1])
        self.assertLessEqual(len(out[0]['sql'].encode()), 20)
        self.assertEqual(ci.Batches('P ').finish(), [])

    def test_all_or_nothing_has_one_final_commit(self):
        batches = ci.Batches('P ', max_rows=2)
        out = self.collect(batches, ['(1)', '(2)', '(3)'])
        self.assertEqual([b['commit'] for b in out], [False, True])
        self.assertEqual(batches.count, 2)


class LoadDataTest(unittest.TestCase):
    def test_statement_for_the_checked_copy(self):
        sql = ci.load_data_sql('/tmp/x/chunk-000001.tsv', 'db', 't', [('column', 'a'), ('variable', '@c2')],
                               [('b', 'cast_unsigned', '@c2')])
        self.assertEqual(sql, "/*myflames:import_csv*/ LOAD DATA LOCAL INFILE '/tmp/x/chunk-000001.tsv' INTO TABLE "
                              "`db`.`t` CHARACTER SET utf8mb4 FIELDS TERMINATED BY X'09' ENCLOSED BY '' ESCAPED BY "
                              "X'5C' LINES TERMINATED BY X'0A' (`a`, @c2) SET `b` = CAST(@c2 AS UNSIGNED)")

    def test_every_clause(self):
        sql = ci.load_data_sql("/d/it's.csv", 'db', 't', [('column', 'a'), ('variable', '@v')],
                               [('b', 'nullif_empty', '@v')], charset='latin1',
                               fields_terminated=';', enclosed='"', optionally=True, escaped='',
                               lines_terminated='\r\n', ignore_lines=1)
        self.assertIn("INFILE '/d/it''s.csv'", sql)
        self.assertIn("CHARACTER SET latin1 FIELDS TERMINATED BY X'3B' OPTIONALLY ENCLOSED BY X'22' ESCAPED BY '' "
                      "LINES TERMINATED BY X'0D0A' IGNORE 1 LINES (`a`, @v) SET `b` = NULLIF(@v, '')", sql)

    def test_refusals(self):
        for kwargs in ({'charset': 'utf16'}, {'targets': [('variable', '@1bad')]}, {'targets': ['@v']},
                       {'targets': [('expression', 'NOW()')]}, {'sets': [('b', 'raw sql', '@v')]},
                       {'sets': [('b', 'nullif_empty', '@other')]}, {'sets': [('b', 'nullif_empty', 'a')]},
                       {'path': 'x\ny'}, {'path': 'C:\\x.csv'},
                       {'enclosed': '""'}, {'ignore_lines': -1}, {'fields_terminated': ''}):
            args = {'path': '/tmp/x', 'schema': 'db', 'table': 't', 'targets': [('column', 'a'), ('variable', '@v')],
                    'sets': ()}
            args.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ci.load_data_sql(**args)
        self.assertIn("'C:\\\\x.csv'", ci.load_data_sql('C:\\x.csv', 'd', 't', [('column', 'a')],
                                                     backslash_escapes=True))
        self.assertIn("'C:\\x.csv'", ci.load_data_sql('C:\\x.csv', 'd', 't', [('column', 'a')],
                                                   backslash_escapes=False))

    def test_a_column_named_like_a_variable_stays_a_column(self):
        sql = ci.load_data_sql('/tmp/x', 'd', 't', [('column', '@note'), ('variable', '@c2')],
                               [('b', 'cast_unsigned', '@c2')])
        self.assertIn("(`@note`, @c2) SET `b` = CAST(@c2 AS UNSIGNED)", sql)
        self.assertEqual(ci.load_targets([col('int', name='@note')])[0], [('column', '@note')])

    def test_tsv_rows(self):
        self.assertEqual(ci.tsv_row([('null', None), ('text', 'a\tb\nc\\d\r\x00'), ('num', '1.5'), ('bit', 3),
                                     ('bytes', b'xy'), ('text', '\\N'), ('text', '')]),
                         '\\N\ta\\tb\\nc\\\\d\\r\\0\t1.5\t3\txy\t\\\\N\t\n')

    def test_targets_cast_bit_columns(self):
        columns = [col('int', name='a'), col('bit(3)', name='b')]
        self.assertEqual(ci.load_targets(columns), ([('column', 'a'), ('variable', '@c2')],
                                                    [('b', 'cast_unsigned', '@c2')]))


class InferenceTest(unittest.TestCase):
    def infer(self, values, **values_fmt):
        stats = ci.Infer()
        for value in values:
            stats.add(value, fmt(**values_fmt))
        return stats

    def test_types(self):
        cases = ((['1', '-5', '2147483647'], 'INT'), (['1', '2147483648'], 'BIGINT'),
                 (['18446744073709551615'], 'BIGINT UNSIGNED'), (['-1', '18446744073709551615'], 'DECIMAL(20,0)'),
                 (['1.5', '22.25'], 'DECIMAL(4,2)'), (['1e3', '2'], 'DOUBLE'), (['true', 'no'], 'TINYINT(1)'),
                 (['2024-01-02'], 'DATE'), (['2024-01-02', '2024-01-03 10:00:00.25'], 'DATETIME(2)'),
                 (['10:00:00', '-1:00:00'], 'TIME'), (['{"a":1}', '[]'], 'JSON'), (['007', '12'], 'VARCHAR(16)'),
                 (['x' * 40], 'VARCHAR(64)'), (['x' * 20000], 'TEXT'), ([], 'VARCHAR(255)'),
                 (['2023-02-29'], 'VARCHAR(16)'))
        for values, expected in cases:
            with self.subTest(values=values[:2]):
                self.assertEqual(self.infer(values).sql_type(), expected)

    def test_nullable_samples_and_partial(self):
        stats = self.infer(['', 'a', 'a', 'b', '\\N'])
        self.assertEqual(stats.public(False), {'sql_type': 'VARCHAR(16)', 'nullable': True, 'max_length': 1})
        self.assertEqual(stats.samples, ['a', 'b'])
        self.assertFalse(self.infer(['a']).public(False)['nullable'])
        self.assertTrue(self.infer(['a']).public(True)['nullable'])

    def test_wide_rows_fit_innodb(self):
        types = ci.fit_row(['VARCHAR(16383)'] * 3 + ['INT'])
        self.assertEqual(types.count('TEXT'), 3)
        self.assertEqual(ci.fit_row(['VARCHAR(255)', 'INT']), ['VARCHAR(255)', 'INT'])

    def test_column_names(self):
        used = set()
        self.assertEqual(ci.column_name('Order Id', 0, used), 'Order Id')
        self.assertEqual(ci.column_name('order id ', 1, used), 'order id_2')
        self.assertEqual(ci.column_name('', 2, used), 'column_3')
        self.assertEqual(len(ci.column_name('x' * 80, 3, used)), 64)
        self.assertEqual(ci.column_name('a\tb\U0001F600', 4, used), 'ab')

    def test_check_name(self):
        for bad in ('', 'x' * 65, 'trailing ', 'tab\there', '\U0001F600', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ci.check_name(bad)
        self.assertEqual(ci.check_name('ok name`'), 'ok name`')


class CreateTableTest(unittest.TestCase):
    def test_create_table(self):
        columns = [col('varchar(64)', name='name', nullable=False), col("enum('a','b')", name='kind')]
        sql = ci.create_table_sql('db', 'new`t', columns, add_id_key=True)
        self.assertEqual(sql, "/*myflames:import_csv*/ CREATE TABLE `db`.`new``t` (\n"
                              "  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
                              "  `name` VARCHAR(64) NOT NULL,\n"
                              "  `kind` ENUM('a','b') NULL,\n"
                              "  PRIMARY KEY (`id`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


if __name__ == '__main__':
    unittest.main()
