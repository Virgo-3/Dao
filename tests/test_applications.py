import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dao.__main__ import DEFAULT_APPLICATION, default_database, main, terminal_main
from dao.config import Config
from dao.store import Store
from dao_narrative.__main__ import APPLICATION, main as narrative_main, terminal_main as narrative_terminal_main
from dao_narrative.app import NarrativeDao
import test_server


class SeparateApplicationsTests(unittest.TestCase):
    def test_default_database_locations_are_distinct_on_each_platform(self):
        with patch("sys.frozen", False, create=True):
            self.assertEqual(default_database(), Path(".dao/state.sqlite3"))
            self.assertEqual(default_database(APPLICATION), Path(".dao-narrative/state.sqlite3"))
        with patch("sys.frozen", True, create=True), patch("sys.platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": str(Path("local-data").resolve())}):
            self.assertEqual(default_database().parent.name, "Dao")
            self.assertEqual(default_database(APPLICATION).parent.name, "DaoNarrative")
        with patch("sys.frozen", True, create=True), patch("sys.platform", "linux"), patch.dict(
                os.environ, {"XDG_DATA_HOME": str(Path("local-data").resolve())}):
            self.assertEqual(default_database().parent.name, "dao")
            self.assertEqual(default_database(APPLICATION).parent.name, "dao-narrative")

    def test_real_launchers_keep_default_state_usage_and_terminal_language_separate(self):
        with tempfile.TemporaryDirectory() as folder, patch("sys.frozen", True, create=True), patch(
                "sys.platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": folder}), patch(
                "dao.__main__.Config.from_env", return_value=Config()), patch("dao.provider.time.sleep"):
            for launch, commands, banner, help_command, data_folder, expected_reply in (
                (terminal_main, "/remember purpose=general\n/decide\nHello\n/help\n/verify\n/quit\n",
                 "Dao terminal | branch main", "/branches", "Dao", "Reversible pilot"),
                (narrative_terminal_main, "/remember purpose=story\nHello\n/help\n/documents\n/quit\n",
                 "Dao Narrative writing room | draft main", "/drafts", "DaoNarrative", "Start with what the character wants"),
            ):
                output = io.StringIO()
                with patch("sys.stdin", io.StringIO(commands)), patch("sys.stdout", output):
                    self.assertEqual(launch([]), 0)
                self.assertIn(banner, output.getvalue())
                self.assertIn(help_command, output.getvalue())
                self.assertIn(expected_reply, output.getvalue())
                self.assertNotIn("Error:", output.getvalue())
                store = Store(Path(folder) / data_folder / "state.sqlite3")
                try:
                    self.assertEqual(store.head()["state"]["memory"]["purpose"], "story" if data_folder == "DaoNarrative" else "general")
                    self.assertEqual(len(store.usage()["entries"]), 1)
                    self.assertTrue(store.verify()["ok"])
                finally:
                    store.close()

    def test_browser_launchers_select_fixed_assets_ports_and_services(self):
        with tempfile.TemporaryDirectory() as folder:
            for launch, application in ((main, DEFAULT_APPLICATION), (narrative_main, APPLICATION)):
                server = Mock()
                server.serve_forever.side_effect = KeyboardInterrupt
                with patch("dao.__main__.Config.from_env", return_value=Config()), patch(
                        "dao.__main__.make_server", return_value=server) as make, patch("sys.stdout", io.StringIO()):
                    self.assertEqual(launch(["--web", "--db", str(Path(folder) / f"{application.data_folder}.db")]), 0)
                app, port = make.call_args.args
                self.assertIs(type(app), application.service_type)
                self.assertEqual(port, application.port)
                self.assertEqual(make.call_args.kwargs["static_dir"], application.static_dir)
                server.server_close.assert_called_once()



class NarrativeHttpTests(test_server.HttpTests):
    # Run the HTTP trust, validation, export, integrity, and asset checks for both apps.
    app_type = NarrativeDao
    static_dir = APPLICATION.static_dir

    def test_own_writing_interface_is_served(self):
        page = self.request("GET", "/")[1]
        self.assertIn("Dao Narrative · Writing room".encode(), page)
        self.assertIn(b'aria-label="Writing conversation"', page)
        self.assertNotIn(b"data-interface", page)
        self.assertIn(b'id="document-save"', page)
        self.assertNotIn(b'id="audit-panel"', page)
        self.assertNotIn(b'id="decision-panel"', page)
        self.assertNotIn(b'id="relationships-panel"', page)
        self.assertNotIn(b'id="verify-button"', page)

    def test_csp_static_assets_and_export(self):
        status, page, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        exported = self.request("GET", "/api/export")[1]
        self.assertEqual(exported["name"], "story-main.md")
        self.assertIn("## Documents", exported["content"])
        self.assertEqual(set(exported), {"name", "content"})

    def test_oversized_decision_is_rejected_without_committing_state(self):
        self.test_removed_endpoints_are_unavailable()

    def test_removed_endpoints_are_unavailable(self):
        head = self.store.head()["id"]
        for method, route in (("GET", "/api/decision-example"), ("GET", "/api/verify"),
                              ("POST", "/api/decision"), ("POST", "/api/relationships"), ("POST", "/api/audit")):
            with self.subTest(route=route):
                self.assertEqual(self.request(method, route, {} if method == "POST" else None, self.auth())[0], 404)
        self.assertEqual(self.store.head()["id"], head)

    def test_document_saves_directly_and_rejects_stale_writes(self):
        body = {"branch": "main", "expected_head": self.store.head()["id"],
                "name": "scene.md", "content": "  The door opened.\n"}
        status, saved, _ = self.request("POST", "/api/artifact", body, self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(saved["head"]["state"]["artifacts"]["scene.md"]["content"], body["content"])
        self.assertEqual(self.request("POST", "/api/artifact", body, self.auth())[0], 409)
        self.assertIn(body["content"], self.request("GET", "/api/export")[1]["content"])
        self.assertTrue(self.store.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
