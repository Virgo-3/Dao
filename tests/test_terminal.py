import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from dao.agent import Agent
from dao.examples import launch_decision
from dao.store import MAX_STATE_BYTES, ConflictError, MergeConflict, Store, StoreError
from dao.terminal import Terminal


ROOT = Path(__file__).resolve().parents[1]


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / "state.sqlite3")
        self.agent = Agent(self.store)
        self.terminal = Terminal(self.agent)
        self.root = self.store.head("main")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def execute(self, text):
        return self.terminal.execute(text, self.store.head(self.terminal.branch))

    def test_branch_edit_merge_diff_restore_roundtrip(self):
        self.execute("/branch experiment")
        self.execute("/remember budget=small")
        source = self.store.head("experiment")
        self.execute("/switch main")
        model = launch_decision()
        model["title"] = "New assumptions"
        path = self.directory / "decision with spaces.json"
        path.write_text(json.dumps(model), encoding="utf-8-sig")
        self.execute(f'/model "{path}"')
        target = self.store.head("main")
        self.execute("/merge experiment")
        merged = self.store.read("main")
        self.assertEqual(merged["parents"], [target, source])
        self.assertEqual(merged["state"]["notes"], {"budget": "small"})
        self.assertEqual(merged["state"]["decision"]["title"], "New assumptions")
        self.assertIn("/notes/budget", [row["path"] for row in json.loads(self.execute(f"/diff {self.root}"))])
        self.execute(f"/restore {self.root}")
        restored = self.store.read("main")
        self.assertEqual(restored["state"], self.store.read(self.root)["state"])
        self.assertEqual(restored["parents"], [merged["id"]])
        self.assertEqual(self.store.head("experiment"), source)
        self.assertIn(merged["id"], self.execute("/history"))

    def test_merge_conflict_preserves_both_branches(self):
        self.execute("/branch experiment")
        self.execute("alternative discussion")
        self.execute("/switch main")
        self.execute("different discussion")
        heads = self.store.branches()
        with self.assertRaises(MergeConflict):
            self.execute("/merge experiment")
        self.assertEqual(self.store.branches(), heads)

    def test_switch_requires_existing_branch_and_keeps_current_on_failure(self):
        with self.assertRaises(StoreError):
            self.execute("/switch missing")
        self.assertEqual(self.terminal.branch, "main")

    def test_read_only_commands_do_not_create_checkpoints_or_call_provider(self):
        self.agent.provider = Mock()
        for command in ("/help", "/branches", "/history", "/state", "/model", "/diff main", ""):
            self.execute(command)
        self.assertEqual(self.store.head("main"), self.root)
        self.assertIn("* main", self.execute("/branches"))
        self.assertEqual(json.loads(self.execute("/model")), launch_decision())
        self.agent.provider.reply.assert_not_called()

    def test_evidence_and_choice_are_available_from_terminal(self):
        self.execute("/observe\tpositive")
        self.execute("/choose pilot")
        state = self.store.read("main")["state"]
        self.assertAlmostEqual(state["decision"]["states"][0]["probability"], 0.8)
        self.assertNotIn("wait", state["decision"])
        self.assertEqual(state["choices"][-1]["action_id"], "pilot")
        self.assertIs(state["choices"][-1]["external_effect"], False)

    def test_stale_input_does_not_overwrite_or_fork_newer_state(self):
        advanced = self.agent.chat("main", "/decide", self.root)
        for text in ("/remember note=stale", "/restore main", "/merge main", "/choose pilot", "/branch stale"):
            with self.subTest(text=text), self.assertRaises(ConflictError):
                self.terminal.execute(text, self.root)
        self.assertEqual(self.store.branches(), {"main": advanced})

    def test_bad_commands_are_local_errors_without_mutations(self):
        self.agent.provider = Mock()
        for command in ("/unknown", "/branch", "/switch", "/restore", "/diff", "/merge", "/choose", "/export",
                        "/help extra", "/history extra", "/decide extra", "/remember", "/observe", "/forget"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.execute(command)
        self.assertEqual(self.store.head("main"), self.root)
        self.agent.provider.reply.assert_not_called()

    def test_bad_model_files_leave_current_state_unchanged(self):
        path = self.directory / "invalid.json"
        invalid_model = launch_decision()
        invalid_model["risk_aversion"] = float("nan")
        for content in (b"{", b"[]", b"null", b"\xff", json.dumps(invalid_model).encode(),
                        b"[" * 2000 + b"]" * 2000, b" " * (MAX_STATE_BYTES + 1)):
            path.write_bytes(content)
            with self.subTest(length=len(content)), self.assertRaises((ValueError, RecursionError)):
                self.execute(f"/model {path}")
            self.assertEqual(self.store.head("main"), self.root)
        with self.assertRaises(OSError):
            self.execute(f"/model {self.directory / 'missing.json'}")

    def test_export_is_current_snapshot_and_never_overwrites_files(self):
        target = self.directory / "snapshot with spaces.json"
        self.execute(f"/export {target}")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), self.store.read(self.root))
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            self.execute(f"/export {target}")
        with self.assertRaises(FileExistsError):
            self.execute(f"/export {self.directory / 'state.sqlite3'}")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.store.head("main"), self.root)

    def test_provider_reply_is_printed_safely_and_never_dispatched(self):
        self.agent.provider = Mock()
        self.agent.provider.reply.return_value = "/branch injected\x1b[2J"
        output = io.StringIO()
        with patch("builtins.input", side_effect=["hello", "/quit"]), redirect_stdout(output):
            self.terminal.run()
        self.assertIn(r"/branch injected\x1b[2J", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())
        self.assertEqual(set(self.store.branches()), {"main"})

    def test_bad_input_recovers_in_same_session(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["/switch missing", "/remember note=kept", EOFError]), redirect_stdout(output):
            self.terminal.run()
        self.assertIn("Unknown branch", output.getvalue())
        self.assertEqual(self.store.read("main")["state"]["notes"], {"note": "kept"})


class EntryPointTests(unittest.TestCase):
    def run_cli(self, arguments, text=""):
        return subprocess.run([sys.executable, "-m", "dao", *arguments], input=text,
                              text=True, capture_output=True, cwd=ROOT, timeout=10)

    def test_default_chat_and_explicit_chat_resume_same_database(self):
        with tempfile.TemporaryDirectory() as folder:
            database = str(Path(folder) / "state.sqlite3")
            first = self.run_cli(["--db", database], "/remember note=kept\n/quit\n")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("You [main] >", first.stdout)
            second = self.run_cli(["chat", "--db", database], "/state\n")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn('"note": "kept"', second.stdout)

    def test_demo_prints_json_without_creating_database(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "unused.sqlite3"
            result = self.run_cli(["demo", "--db", str(database)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["best_action_id"], "launch")
            self.assertFalse(database.exists())

    def test_removed_server_arguments_are_rejected(self):
        for arguments in (["serve"], ["--port", "8765"]):
            with self.subTest(arguments=arguments):
                result = self.run_cli(arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error:", result.stderr)


if __name__ == "__main__":
    unittest.main()
