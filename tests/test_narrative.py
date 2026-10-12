import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dao.config import Config
from dao.provider import build_input
from dao.service import Dao
from dao.store import ConflictError, Store
from dao_narrative.app import NarrativeDao
from dao_narrative.provider import INPUT_OPTIONS, SYSTEM, reserve
from dao_narrative.terminal import NarrativeTerminal
from test_service import FakeResponse


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "writing.db"
        self.store = Store(self.path)
        self.app = NarrativeDao(self.store, Config())
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def save(self, content, branch="main"):
        return self.app.mutate("/api/artifact", {"branch": branch, "expected_head": self.store.head(branch)["id"],
                                               "name": "scene.md", "content": content})["head"]

    def test_prose_and_empty_drafts_save_without_review_and_survive_restart(self):
        prose = "  Indented opening.\n\nLast line.  \n"
        first = self.save(prose)
        self.assertEqual(first["state"]["artifacts"]["scene.md"], {"content": prose})
        self.assertFalse(first["state"].get("audits"))
        second = self.save("")
        self.app.mutate("/api/restore", {"branch": "main", "expected_head": second["id"], "commit_id": first["id"]})
        reopened = Store(self.path)
        try:
            self.assertEqual(reopened.head()["state"]["artifacts"]["scene.md"]["content"], prose)
            self.assertTrue(reopened.verify()["ok"])
        finally:
            reopened.close()

    def test_legacy_conflicts_and_audits_are_preserved_without_blocking_writing(self):
        generic = Dao(self.store, Config())
        operations = [
            {"operation": "node", "node": {"id": "choice", "label": "Choice", "kind": "action", "importance": 1}},
            {"operation": "node", "node": {"id": "goal", "label": "Goal", "kind": "goal", "importance": 1}},
            {"operation": "relation", "relation": {"id": "effect", "source": "choice", "target": "goal",
             "kind": "effect", "weight": 2, "severe": True, "actions": []}},
            {"operation": "assess", "relation_id": "effect", "belief": {"positive": 0, "neutral": 0, "negative": 1},
             "source": "Earlier work", "content": "An unresolved concern."},
        ]
        for operation in operations:
            generic.mutate("/api/relationships", {**operation, "branch": "main", "expected_head": self.store.head()["id"]})
        legacy_graph = self.store.head()["state"]["relationships"]
        saved = self.save("An intentionally unresolved ending.")
        self.assertEqual(saved["state"]["relationships"], legacy_graph)
        self.assertNotIn("relationships", self.app.snapshot())
        with self.assertRaisesRegex(ValueError, "adjudication"):
            generic.mutate("/api/artifact", {"branch": "main", "expected_head": saved["id"],
                                           "name": "general.md", "content": "Still gated in Dao."})

    def test_drafts_are_isolated_and_restoration_retains_usage(self):
        first = self.save("Original ending.")
        self.app.mutate("/api/branches", {"name": "alternate", "from_commit": first["id"]})
        self.save("Alternate ending.", "alternate")
        with patch("dao_narrative.provider.time.sleep"):
            events = list(self.app.chat({"branch": "alternate", "expected_head": self.store.head("alternate")["id"],
                                         "message": "Help revise this ending."}))
        self.assertEqual(events[-1]["type"], "done")
        usage = self.store.usage()
        self.app.mutate("/api/restore", {"branch": "alternate", "expected_head": self.store.head("alternate")["id"],
                                       "commit_id": first["id"]})
        self.assertEqual(self.store.usage(), usage)
        self.assertEqual(self.store.head()["state"]["artifacts"]["scene.md"]["content"], "Original ending.")
        self.assertTrue(self.store.verify()["ok"])

    def test_stale_save_cannot_replace_newer_document(self):
        head = self.store.head()["id"]
        self.save("Newer scene.")
        with self.assertRaises(ConflictError):
            self.app.mutate("/api/artifact", {"branch": "main", "expected_head": head,
                                           "name": "scene.md", "content": "Stale scene."})
        self.assertEqual(self.store.head()["state"]["artifacts"]["scene.md"]["content"], "Newer scene.")

    def test_removed_commands_and_mutations_cannot_create_records(self):
        head = self.store.head()["id"]
        for route in ("/api/decision", "/api/audit", "/api/relationships"):
            with self.assertRaises(KeyError):
                self.app.mutate(route, {"branch": "main", "expected_head": head})
        for command in ("/decide", "/explore", "/decision", "/audit", "/review", "/connect", "/relationships"):
            with self.assertRaisesRegex(ValueError, "Unknown writing command"):
                list(self.app.chat({"message": command, "expected_head": head}))
        self.assertEqual(self.store.head()["id"], head)
        self.assertEqual(self.store.usage()["entries"], [])

    def test_live_model_receives_writing_notes_and_documents_without_decision_tools(self):
        self.save("Mara hid the key in the garden.")
        self.app.mutate("/api/memory", {"expected_head": self.store.head()["id"],
                                       "key": "canon", "value": "Mara has never met Ivo."})
        self.app = NarrativeDao(self.store, Config(provider="openai", api_key="synthetic-test-key"))
        response = FakeResponse([
            {"type": "response.output_text.delta", "delta": "Try a scene in the garden."},
            {"type": "response.completed", "response": {"status": "completed", "output": [],
             "usage": {"input_tokens": 50, "output_tokens": 9}}},
        ])
        with patch("dao.provider.urlopen", return_value=response) as transport:
            events = list(self.app.chat({"message": "Continue the scene.", "expected_head": self.store.head()["id"]}))
        payload = json.loads(transport.call_args.args[0].data)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(payload["instructions"], SYSTEM)
        self.assertNotIn("tools", payload)
        self.assertFalse(payload["store"])
        context = payload["input"][0]
        self.assertEqual(context["role"], "user")
        self.assertIn("Mara hid the key", context["content"])
        self.assertIn("Mara has never met Ivo", context["content"])
        self.assertNotIn("relationship_summary", context["content"])
        self.assertEqual(self.store.usage()["total_tokens"], 59)
        self.assertEqual(self.store.head()["state"]["decisions"], [])

    def test_unrequested_model_tool_fails_without_evaluation_or_extra_request(self):
        self.app = NarrativeDao(self.store, Config(provider="openai", api_key="synthetic-test-key"))
        response = FakeResponse([{"type": "response.completed", "response": {"status": "completed",
            "output": [{"type": "function_call", "name": "evaluate_decision", "arguments": "{}", "call_id": "x"}],
            "usage": {"input_tokens": 10, "output_tokens": 2}}}])
        with patch("dao.provider.urlopen", return_value=response) as transport, patch.object(self.app, "evaluate_problem") as evaluate:
            events = list(self.app.chat({"message": "Write a scene.", "expected_head": self.store.head()["id"]}))
        self.assertEqual(events[-1]["type"], "error")
        evaluate.assert_not_called()
        transport.assert_called_once()
        self.assertEqual(self.store.usage()["total_tokens"], 12)
        self.assertEqual(self.store.head()["state"]["decisions"], [])

    def test_document_context_is_bounded_and_included_in_reservations(self):
        small = {"messages": [{"role": "user", "content": "Continue."}], "memory": {}, "artifacts": {}}
        large = {**small, "artifacts": {"scene.md": {"content": "a" * 10000}}}
        self.assertGreater(reserve(large, Config()), reserve(small, Config()) + 9999)
        with self.assertRaisesRegex(ValueError, "Context exceeds"):
            build_input({**small, "artifacts": {"novel.md": {"content": "a" * (1024 * 1024)}}}, **INPUT_OPTIONS)

    def test_terminal_imports_plain_text_and_exports_readable_draft(self):
        scene = Path(self.temp.name) / "first scene.md"
        scene.write_text("  Opening line.\n", encoding="utf-8")
        exported = Path(self.temp.name) / "story.md"
        commands = f"/remember canon=Mara is the narrator\n/manuscript {scene}\n/documents\n/document {scene.name}\n/export {exported}\n/quit\n"
        output = io.StringIO()
        terminal = NarrativeTerminal(self.app, input_stream=io.StringIO(commands), output_stream=output)
        self.assertEqual(terminal.run(), 0)
        self.assertNotIn("Error:", output.getvalue())
        self.assertIn("  Opening line.\n", exported.read_text(encoding="utf-8"))
        self.assertIn("Mara is the narrator", exported.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "already exists"):
            terminal._command(f"/export {exported}")

    def test_terminal_does_not_keep_formal_features_as_aliases(self):
        terminal = NarrativeTerminal(self.app, input_stream=io.StringIO(), output_stream=io.StringIO())
        terminal._refresh()
        for command in ("/explore", "/decide", "/decision", "/connections", "/relationships", "/connect",
                        "/relate", "/conflicts", "/review", "/audit", "/artifact", "/verify"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "Unknown writing command"):
                terminal._command(command)


if __name__ == "__main__":
    unittest.main()
