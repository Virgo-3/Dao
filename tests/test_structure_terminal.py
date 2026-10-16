import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dao.config import Config
from dao.service import Dao
from dao.store import Store
from dao.terminal import Terminal


class StructureTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / "state.db")
        self.app = Dao(self.store, Config())
        with patch("dao.provider.time.sleep"):
            list(self.app.chat({"branch": "main", "expected_head": self.store.head()["id"],
                                "message": "🧭 It must work offline."}))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def proposal_file(self, name="proposal.json", **extra):
        message = self.app.message_records(self.store.head()["state"])[0]
        quote = "work offline"
        start = message["content"].index(quote)
        payload = {
            "rationale": "Preserve the user's offline requirement.",
            "operations": [{"operation": "node", "node": {
                "id": "offline", "label": "Must work offline", "kind": "claim", "importance": 1,
                "citations": [{"message_id": message["id"], "start": start,
                               "end": start + len(quote), "quote": quote}],
            }}],
            **extra,
        }
        path = self.directory / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def run_terminal(self, source):
        output = io.StringIO()
        terminal = Terminal(self.app, input_stream=source, output_stream=output)
        with patch("dao.provider.time.sleep"):
            code = terminal.run()
        return code, output.getvalue(), terminal

    def test_stage_inspect_accept_and_trace_exact_source(self):
        path = self.proposal_file("proposal with spaces.json")
        app, store = self.app, self.store
        observations = []

        class Input:
            def __init__(self):
                self.step = 0

            def readline(self):
                self.step += 1
                if self.step == 1:
                    return f'/propose "{path}"\n'
                if self.step == 2:
                    state = store.head()["state"]
                    observations.append((state["proposals"][0]["status"],
                                         "offline" in state.get("relationships", {}).get("nodes", {})))
                    return "/proposals\n"
                if self.step == 3:
                    return "/messages\n"
                if self.step == 4:
                    return f'/accept {store.head()["state"]["proposals"][0]["id"]}\n'
                return "/quit\n"

        code, output, _ = self.run_terminal(Input())
        self.assertEqual(code, 0)
        self.assertNotIn("Error:", output)
        self.assertEqual(observations, [("pending", False)])
        self.assertIn("pending review", output)
        self.assertIn("work offline", output)
        state = store.head()["state"]
        self.assertEqual(state["proposals"][0]["status"], "accepted")
        citation = state["relationships"]["nodes"]["offline"]["citations"][0]
        source = next(message for message in app.message_records(state)
                      if message["id"] == citation["message_id"])
        self.assertEqual(source["content"][citation["start"]:citation["end"]], citation["quote"])
        self.assertTrue(store.verify()["ok"])

    def test_reject_and_branch_views_keep_pending_knowledge_separate(self):
        path = self.proposal_file()
        store = self.store

        class Input:
            def __init__(self):
                self.step = 0

            def readline(self):
                self.step += 1
                if self.step == 1:
                    return f"/propose {path}\n"
                if self.step == 2:
                    return "/branch alternate\n"
                if self.step == 3:
                    return f'/reject {store.head("alternate")["state"]["proposals"][0]["id"]}\n'
                if self.step == 4:
                    return "/switch main\n"
                if self.step == 5:
                    return "/proposals\n"
                return "/quit\n"

        code, output, terminal = self.run_terminal(Input())
        self.assertEqual(code, 0)
        self.assertNotIn("Error:", output)
        self.assertEqual(terminal.branch, "main")
        self.assertEqual(store.head()["state"]["proposals"][0]["status"], "pending")
        self.assertEqual(store.head("alternate")["state"]["proposals"][0]["status"], "rejected")
        self.assertNotIn("offline", store.head("alternate")["state"].get("relationships", {}).get("nodes", {}))

    def test_import_cannot_override_branch_and_review_requires_id(self):
        initial = self.store.head()["id"]
        path = self.proposal_file(branch="alternate")
        code, output, _ = self.run_terminal(io.StringIO(f"/propose {path}\n/accept\n/reject two ids\n/quit\n"))
        self.assertEqual(code, 0)
        self.assertIn("cannot select a branch", output)
        self.assertIn("Use /accept ID", output)
        self.assertIn("Use /reject ID", output)
        self.assertEqual(self.store.head()["id"], initial)

    def test_streamed_model_proposal_announces_review_without_claiming_acceptance(self):
        head = self.store.head()
        proposal = {"id": "proposal-1", "status": "pending", "rationale": "Capture an assumption", "operations": []}
        events = [{"type": "start", "head": head},
                  {"type": "proposal", "name": "propose_structure", "result": proposal},
                  {"type": "done", "head": head}]
        with patch.object(self.app, "chat", return_value=iter(events)):
            code, output, _ = self.run_terminal(io.StringIO("Propose structure\n/quit\n"))
        self.assertEqual(code, 0)
        self.assertIn("Structure proposal pending review", output)
        self.assertIn("Keep that", output)
        self.assertNotIn('"id": "proposal-1"', output)
        self.assertNotIn("accepted at", output)


if __name__ == "__main__":
    unittest.main()
