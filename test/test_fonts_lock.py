"""The bundled fonts (apps/workspace/src/fonts) match fonts.lock.json."""
import hashlib
import json
from pathlib import Path
import re
import unittest

FONTS_DIR = Path(__file__).resolve().parent.parent / 'apps' / 'workspace' / 'src' / 'fonts'
STYLE = Path(__file__).resolve().parent.parent / 'apps' / 'workspace' / 'src' / 'style.css'
HEX64 = re.compile(r'[0-9a-f]{64}\Z')


class FontsLockTest(unittest.TestCase):
    def test_lock_records_and_file_hashes(self):
        lock = json.loads((FONTS_DIR / 'fonts.lock.json').read_text(encoding='utf-8'))
        self.assertEqual(lock.get('schema_version'), 'fonts-lock-1.0')
        fonts = lock.get('fonts')
        self.assertIsInstance(fonts, list)
        self.assertEqual({font['name'] for font in fonts}, {'Inter', 'JetBrains Mono'})
        listed = []
        for font in fonts:
            with self.subTest(font=font['name']):
                self.assertTrue(font.get('version'))
                self.assertTrue(str(font.get('source', '')).startswith('https://'))
                self.assertTrue(HEX64.fullmatch(font.get('zip_sha256', '')))
                self.assertEqual(font.get('license'), 'SIL Open Font License 1.1')
                self.assertTrue(font.get('files'))
                for entry in font['files']:
                    path = entry.get('path', '')
                    self.assertTrue(HEX64.fullmatch(entry.get('sha256', '')))
                    self.assertNotIn('/', path)
                    self.assertNotIn('\\', path)
                    data = (FONTS_DIR / path).read_bytes()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'],
                                     '%s does not match fonts.lock.json' % path)
                    listed.append(path)
        # The folder holds the lock file, the listed files and nothing else.
        on_disk = sorted(p.name for p in FONTS_DIR.iterdir() if p.is_file())
        self.assertEqual(on_disk, sorted(listed + ['fonts.lock.json']))

    def test_every_vendored_font_is_wired_into_the_stylesheet(self):
        css = STYLE.read_text(encoding='utf-8')
        lock = json.loads((FONTS_DIR / 'fonts.lock.json').read_text(encoding='utf-8'))
        for font in lock['fonts']:
            for entry in font['files']:
                if entry['path'].endswith('.woff2'):
                    self.assertIn('./fonts/%s' % entry['path'], css)


if __name__ == '__main__':
    unittest.main()
