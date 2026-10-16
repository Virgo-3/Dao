import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dao.config import Config
from dao.service import Dao
from dao.store import Store
from dao.terminal import Terminal


def head(identifier, *, activities=None):
    message = {"role": "assistant", "content": "Saved reply", "status": "completed"}
    if activities:
        message["activities"] = activities
    return {"id": identifier, "state": {"messages": [message]}}


class ConversationTerminalTests(unittest.TestCase):
    def test_assumption_request_recalls_real_context_in_the_terminal_without_a_provider(self):
        store = Store(":memory:")
        try:
            app = Dao(store, Config(provider="openai", api_key="test-only", token_budget=1))
            output = io.StringIO()
            terminal = Terminal(app, input_stream=io.StringIO(
                "We chose SQLite because this must work offline.\nShow the assumptions behind that decision\n/quit\n"),
                output_stream=output)
            with patch("dao.service.openai_stream") as provider:
                self.assertEqual(terminal.run(), 0)
                provider.assert_not_called()
            self.assertIn("The most recent recorded decision was: We chose SQLite", output.getvalue())
            self.assertIn("this must work offline", output.getvalue())
            self.assertEqual(len(store.head()["state"]["relationships"]["nodes"]), 2)
            self.assertEqual(store.usage()["entries"], [])
        finally:
            store.close()

    def run_terminal(self, lines, turn):
        output = io.StringIO()
        requests = []

        def chat(data):
            requests.append(data)
            yield from turn(data, output)

        app = SimpleNamespace(config=Config(),
                              store=SimpleNamespace(head=lambda branch: head("initial")),
                              chat=chat)
        terminal = Terminal(app, input_stream=io.StringIO(lines), output_stream=output)
        code = terminal.run()
        return code, output.getvalue(), terminal, requests

    def test_activity_is_quiet_and_waits_until_the_answer_is_saved(self):
        activity = {"id": "activity-secret-id", "kind": "remember", "label": "Decision remembered",
                    "summary": "SQLite was chosen for offline use.", "node_ids": ["sqlite-node"],
                    "citations": [{"message_id": "message-1", "start": 0, "end": 4, "quote": "test"}]}

        def turn(data, output):
            self.assertEqual(data["message"], "We chose SQLite because it works offline.")
            yield {"type": "start", "head": head("started")}
            yield {"type": "activity", "result": activity}
            self.assertNotIn("Decision remembered", output.getvalue())
            yield {"type": "delta", "text": "I'll remember the decision and its reason."}
            self.assertNotIn("Decision remembered", output.getvalue())
            yield {"type": "done", "head": head("finished", activities=[activity])}

        code, output, terminal, _ = self.run_terminal("We chose SQLite because it works offline.\n/quit\n", turn)
        self.assertEqual(code, 0)
        self.assertEqual(terminal.head["id"], "finished")
        self.assertIn("Dao: I'll remember the decision and its reason.\n  · Decision remembered\n", output)
        self.assertNotIn("activity-secret-id", output)
        self.assertNotIn("sqlite-node", output)
        self.assertNotIn('"citations"', output)

    def test_natural_branch_navigation_updates_the_next_turn_and_prompt(self):
        stages = iter([
            ("Let's explore another direction", "main", "initial", "alternate", "alternate-start", "Alternative explored"),
            ("Actually, we need concurrent writers.", "alternate", "alternate-start", None, "alternate-corrected", "Assumption changed"),
            ("Switch to main", "alternate", "alternate-corrected", "main", "main-return", "Reasoning recalled"),
            ("Why did we decide that?", "main", "main-return", None, "main-recalled", "Reasoning recalled"),
        ])

        def turn(data, _output):
            message, branch, expected, target, identifier, label = next(stages)
            self.assertEqual((data["message"], data["branch"], data["expected_head"]),
                             (message, branch, expected))
            yield {"type": "delta", "text": "Done."}
            yield {"type": "activity", "result": {"id": identifier, "label": label, "kind": "conversation"}}
            done = {"type": "done", "head": head(identifier)}
            if target:
                done["branch"] = target
            yield done

        code, output, terminal, requests = self.run_terminal(
            "Let's explore another direction\nActually, we need concurrent writers.\nSwitch to main\nWhy did we decide that?\n/quit\n", turn)
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 4)
        self.assertEqual(terminal.branch, "main")
        self.assertIn("alternate@alternate-st>", output)
        self.assertIn("alternate@alternate-co>", output)
        self.assertIn("main@main-return>", output)
        self.assertIn("  · Assumption changed", output)

    def test_failed_turn_does_not_claim_memory_or_show_transient_proposals(self):
        def turn(_data, output):
            yield {"type": "delta", "text": "A partial response."}
            yield {"type": "activity", "result": {"kind": "remember", "label": "Decision remembered"}}
            yield {"type": "proposal", "result": {"id": "transient-proposal", "status": "pending"}}
            self.assertNotIn("Decision remembered", output.getvalue())
            self.assertNotIn("pending review", output.getvalue())
            yield {"type": "error", "head": head("failed"), "error": "Provider interrupted"}

        code, output, terminal, _ = self.run_terminal("Remember that the pilot is reversible.\n/quit\n", turn)
        self.assertEqual(code, 0)
        self.assertEqual(terminal.head["id"], "failed")
        self.assertIn("Error: Provider interrupted", output)
        self.assertNotIn("Decision remembered", output)
        self.assertNotIn("pending review", output)
        self.assertNotIn("transient-proposal", output)


if __name__ == "__main__":
    unittest.main()
