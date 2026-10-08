import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dao.__main__ import DEFAULT_APPLICATION, default_database, main, terminal_main
from dao.config import Config
from dao.decision import demo_payload, evaluate
from dao.store import Store
from dao_narrative.__main__ import APPLICATION, main as narrative_main, terminal_main as narrative_terminal_main
from dao_narrative.app import NarrativeDao
from dao_narrative.examples import demo_payload as narrative_example
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
            for launch, commands, banner, help_command, data_folder, expected_example in (
                (terminal_main, "/remember purpose=general\n/decide\nHello\n/help\n/verify\n/quit\n",
                 "Dao terminal | branch main", "/branches", "Dao", "Reversible pilot"),
                (narrative_terminal_main, "/remember purpose=story\n/explore\nHello\n/help\n/verify\n/quit\n",
                 "Dao Narrative writing room | draft main", "/drafts", "DaoNarrative", "Try an alternate scene"),
            ):
                output = io.StringIO()
                with patch("sys.stdin", io.StringIO(commands)), patch("sys.stdout", output):
                    self.assertEqual(launch([]), 0)
                self.assertIn(banner, output.getvalue())
                self.assertIn(help_command, output.getvalue())
                self.assertIn(expected_example, output.getvalue())
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

    def test_story_example_changes_labels_and_preserves_engine_math(self):
        generic, narrative = evaluate(demo_payload()), evaluate(narrative_example())
        for key in ("recommendation", "baseline_utility", "wait_utility", "expected_value_of_information"):
            self.assertEqual(generic[key], narrative[key])
        self.assertEqual([score["utility"] for score in generic["scores"]], [score["utility"] for score in narrative["scores"]])


class NarrativeHttpTests(test_server.HttpTests):
    # Run the HTTP trust, validation, export, integrity, and asset checks for both apps.
    app_type = NarrativeDao
    static_dir = APPLICATION.static_dir

    def test_own_interface_and_example_are_served(self):
        page = self.request("GET", "/")[1]
        self.assertIn("Dao Narrative · Writing room".encode(), page)
        self.assertIn(b"Story claim to review", page)
        self.assertNotIn(b"data-interface", page)
        example = self.request("GET", "/api/decision-example")[1]
        self.assertEqual(example["actions"][1]["name"], "Try an alternate scene")


if __name__ == "__main__":
    unittest.main()
