import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dao.__main__ import main, terminal_main
from dao.config import Config
from dao.store import Store


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def terminal(self, commands, args=None):
        output = io.StringIO()
        with patch("dao.__main__.Config.from_env", return_value=Config()), patch(
                "sys.stdin", io.StringIO(commands)), patch("sys.stdout", output), patch("dao.provider.time.sleep"):
            code = terminal_main(args if args is not None else ["--db", str(self.db)])
        self.assertEqual(code, 0)
        return output.getvalue()

    def test_terminal_and_browser_share_durable_state(self):
        self.terminal("/remember interface=terminal\n/quit\n")
        server = Mock()
        observed = {}

        def serve():
            app = make.call_args.args[0]
            observed.update(app.snapshot()["head"]["state"]["memory"])
            raise KeyboardInterrupt

        server.serve_forever.side_effect = serve
        with patch("dao.__main__.Config.from_env", return_value=Config()), patch(
                "dao.__main__.make_server", return_value=server) as make, patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["--web", "--db", str(self.db)]), 0)
        self.assertEqual(observed, {"interface": "terminal"})
        server.server_close.assert_called_once()
        self.terminal("/remember interface=both\n/quit\n")
        store = Store(self.db)
        try:
            self.assertEqual(store.head()["state"]["memory"], {"interface": "both"})
            self.assertTrue(store.verify()["ok"])
        finally:
            store.close()

    def test_frozen_default_survives_restart_outside_bundle(self):
        with patch("sys.frozen", True, create=True), patch("sys.platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": self.temp.name}):
            self.terminal("/remember portable=persistent\n/quit\n", [])
            self.terminal("/verify\n/quit\n", [])
        db = Path(self.temp.name) / "Dao" / "state.sqlite3"
        store = Store(db)
        try:
            self.assertEqual(store.head()["state"]["memory"], {"portable": "persistent"})
            self.assertTrue(store.verify()["ok"])
        finally:
            store.close()

    def test_unknown_initial_branch_fails_before_conversation(self):
        with patch("dao.__main__.Config.from_env", return_value=Config()), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                terminal_main(["--db", str(self.db), "--branch", "missing"])
        self.assertEqual(error.exception.code, 2)
        store = Store(self.db)
        try:
            self.assertEqual(store.head()["state"]["messages"], [])
            self.assertEqual([b["name"] for b in store.branches()], ["main"])
        finally:
            store.close()

    def test_incompatible_browser_option_fails_without_creating_database(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                terminal_main(["--db", str(self.db), "--open-browser"])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
