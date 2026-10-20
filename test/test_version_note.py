"""The CLI notes a server below the supported floor without an extra query."""
import io
import unittest
from contextlib import redirect_stderr

from myflames import workspace_cli


class _Conn(object):
    def __init__(self, version=None, comment=None):
        self._version_cache = version
        self._version_comment_cache = comment


class TestVersionNote(unittest.TestCase):
    def _note(self, conn, quiet=False):
        err = io.StringIO()
        with redirect_stderr(err):
            workspace_cli._version_note(conn, quiet)
        return err.getvalue()

    def test_old_mariadb_and_mysql_get_one_note(self):
        self.assertEqual(self._note(_Conn("10.6.21-MariaDB-ubu2204")),
                         "note: myflames supports MariaDB 10.11 or later; this server is MariaDB 10.6.21, "
                         "so some results may be missing or wrong.\n")
        self.assertIn("MySQL 8.4 or later; this server is MySQL 8.0.46", self._note(_Conn("8.0.46")))

    def test_supported_unknown_and_quiet_print_nothing(self):
        self.assertEqual(self._note(_Conn("8.4.11")), "")
        self.assertEqual(self._note(_Conn("11.8.3-MariaDB")), "")
        self.assertEqual(self._note(_Conn(None)), "")
        self.assertEqual(self._note(object()), "")
        self.assertEqual(self._note(_Conn("8.0.46"), quiet=True), "")


if __name__ == "__main__":
    unittest.main()
