"""Client XML robustness, using both clients' resultset format."""
import unittest

from myflames.perf_common import named_rows


class NamedRowsTests(unittest.TestCase):
    def fixture(self, value, engine='mysql'):
        # Both clients use resultset/row/field and xsi:nil. MariaDB indents
        # fields with tabs; whitespace between elements is immaterial.
        indent = '\t' if engine == 'mariadb' else '  '
        return ('<?xml version="1.0"?>\n<resultset statement="SHOW TABLES" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
                '<row>{0}<field name="TABLE_NAME">normal_before</field></row>\n'
                '<row>{0}<field name="TABLE_NAME">{1}</field></row>\n'
                '<row>{0}<field name="TABLE_NAME">normal_after</field></row>\n'
                '<row><field name="TABLE_NAME" xsi:nil="true" /></row>\n'
                '</resultset>').format(indent, value)

    def test_control_in_name_preserves_all_rows_for_both_clients(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                rows = named_rows(self.fixture('bad\x01table', engine))
                self.assertEqual(rows, [{'TABLE_NAME': 'normal_before'}, {'TABLE_NAME': 'bad\x01table'},
                                        {'TABLE_NAME': 'normal_after'}, {'TABLE_NAME': None}])

    def test_every_xml_forbidden_character_round_trips(self):
        # Names keep their exact identity: a SQL statement or a pagination
        # cursor built from one must address that object and no other.
        for point in list(range(9)) + [11, 12] + list(range(14, 32)) + [0xfffe, 0xffff]:
            with self.subTest(point=point):
                self.assertEqual(named_rows(self.fixture('a' + chr(point) + 'b'))[1]['TABLE_NAME'], 'a' + chr(point) + 'b')
        # Lone surrogates come from bytes that were not UTF-8: no object has them.
        for point in (0xd800, 0xdfff):
            with self.subTest(point=point):
                self.assertEqual(named_rows(self.fixture('a' + chr(point) + 'b'))[1]['TABLE_NAME'], 'a\ufffdb')

    def test_similar_names_stay_distinct_and_the_escape_mark_round_trips(self):
        names = ['a\x01b', 'a\ufffdb', 'a\U000f0000b', 'a\U000f00001;b', '\U000f00001;']
        values = [named_rows(self.fixture(name))[1]['TABLE_NAME'] for name in names]
        self.assertEqual(values, names)
        self.assertEqual(len(set(values)), len(names))

    def test_valid_whitespace_unicode_escapes_and_empty_values_survive(self):
        for raw, expected in (('a\tb\nc\rd', 'a\tb\nc\nd'), ('', ''),
                              ('\u0085\ud7ff\ue000\ufffd\U00010000\U0010ffff',
                               '\u0085\ud7ff\ue000\ufffd\U00010000\U0010ffff'),
                              ('a&amp;&lt;&gt;&quot;b', 'a&<>"b')):
            with self.subTest(raw=raw):
                self.assertEqual(named_rows(self.fixture(raw))[1]['TABLE_NAME'], expected)

    def test_invalid_column_name_round_trips(self):
        self.assertEqual(named_rows('<resultset><row><field name="a\x01b">ok</field></row></resultset>'),
                         [{'a\x01b': 'ok'}])

    def test_malformed_xml_and_entity_declarations_are_still_refused(self):
        for text in ('<resultset><row></resultset>', '<!DOCTYPE x><resultset/>',
                     '<!ENTITY x "bad"><resultset/>', '<resultset><row>&unknown;</row></resultset>'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    named_rows(text)

    def test_empty_output(self):
        for text in ('', '  \n', None):
            self.assertEqual(named_rows(text), [])
