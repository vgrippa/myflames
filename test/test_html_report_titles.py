"""Report titles and fallback copy use punctuation without phrase separators."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

from myflames.output_html_report import (
    _WHY_BY_CATEGORY, _render_all_clear_card, _render_myteach_section,
    _why_fallback, render_html_report,
)


class TitleReader(HTMLParser):
    """Read the document title without inspecting the inlined chart script."""

    def __init__(self):
        HTMLParser.__init__(self)
        self.title_found = False
        self.in_title = False
        self.title = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'title' and not self.title_found:
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == 'title':
            self.title_found = True
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


class TestHTMLReportTitles(unittest.TestCase):
    def test_title_uses_colon_and_escapes_plan_text_for_both_engines(self):
        fixtures = Path(__file__).resolve().parent
        for name in ('mysql-explain-hash-join.json', 'mariadb-explain-complex.json'):
            source = (fixtures / name).read_text(encoding='utf-8')
            for view in ('flamegraph', 'bargraph', 'tree', 'workbench'):
                for title in ('Query plan', 'A < B & "quoted"', ''):
                    with self.subTest(engine=name, view=view, title=title):
                        document = render_html_report(source, view_type=view, title=title)
                        reader = TitleReader()
                        reader.feed(document)
                        expected = (title + ': ' if title else '') + 'myflames report'
                        self.assertEqual(reader.title, expected)

    def test_generated_fallback_copy_has_no_phrase_separators(self):
        pieces = list(_WHY_BY_CATEGORY.values()) + [
            _render_all_clear_card(), _render_myteach_section({}),
            _why_fallback({'category': 'unknown'}),
        ]
        for piece in pieces:
            with self.subTest(copy=piece):
                self.assertNotIn(' — ', piece)
                self.assertNotIn(' - ', piece)


if __name__ == '__main__':
    unittest.main()
