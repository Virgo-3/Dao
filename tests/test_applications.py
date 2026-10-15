import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dao.__main__ import DEFAULT_APPLICATION, default_database, main, terminal_main
from dao.config import Config
from dao.store import Store


class DaoApplicationTests(unittest.TestCase):
    def test_default_database_locations_on_each_platform(self):
        with patch("sys.frozen", False, create=True):
            self.assertEqual(default_database(), Path(".dao/state.sqlite3"))
        with patch("sys.frozen", True, create=True), patch("sys.platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": str(Path("local-data").resolve())}):
            self.assertEqual(default_database().parent.name, "Dao")
        with patch("sys.frozen", True, create=True), patch("sys.platform", "linux"), patch.dict(
                os.environ, {"XDG_DATA_HOME": str(Path("local-data").resolve())}):
            self.assertEqual(default_database().parent.name, "dao")

    def test_terminal_launcher_saves_default_state_and_usage(self):
        with tempfile.TemporaryDirectory() as folder, patch("sys.frozen", True, create=True), patch(
                "sys.platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": folder}), patch(
                "dao.__main__.Config.from_env", return_value=Config()), patch("dao.provider.time.sleep"):
            commands = "/remember purpose=general\n/decide\nHello\n/help\n/verify\n/quit\n"
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(commands)), patch("sys.stdout", output):
                self.assertEqual(terminal_main([]), 0)
            self.assertIn("Dao terminal | branch main", output.getvalue())
            self.assertIn("/branches", output.getvalue())
            self.assertIn("Reversible pilot", output.getvalue())
            self.assertNotIn("Error:", output.getvalue())
            store = Store(Path(folder) / "Dao" / "state.sqlite3")
            try:
                self.assertEqual(store.head()["state"]["memory"]["purpose"], "general")
                self.assertEqual(len(store.usage()["entries"]), 1)
                self.assertTrue(store.verify()["ok"])
            finally:
                store.close()

    def test_browser_launcher_uses_dao_assets_port_and_service(self):
        with tempfile.TemporaryDirectory() as folder:
            server = Mock()
            server.serve_forever.side_effect = KeyboardInterrupt
            with patch("dao.__main__.Config.from_env", return_value=Config()), patch(
                    "dao.__main__.make_server", return_value=server) as make, patch("sys.stdout", io.StringIO()):
                self.assertEqual(main(["--web", "--db", str(Path(folder) / "Dao.db")]), 0)
            app, port = make.call_args.args
            self.assertIs(type(app), DEFAULT_APPLICATION.service_type)
            self.assertEqual(port, DEFAULT_APPLICATION.port)
            self.assertEqual(make.call_args.kwargs["static_dir"], DEFAULT_APPLICATION.static_dir)
            server.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
