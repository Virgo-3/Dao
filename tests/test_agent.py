import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from dao.agent import Agent
from dao.examples import launch_decision
from dao.provider import ProviderError
from dao.store import ConflictError, Store


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.agent = Agent(self.store)
        self.root = self.store.head("main")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_demo_chat_commits_user_and_assistant_together(self):
        head = self.agent.chat("main", "  Should we wait?  ", self.root)
        record = self.store.read(head)
        self.assertEqual(record["parents"], [self.root])
        self.assertEqual(len(self.store.history("main")), 2)
        self.assertEqual([m["role"] for m in record["state"]["messages"]], ["user", "assistant"])
        self.assertEqual(record["state"]["messages"][0]["content"], "Should we wait?")
        self.assertIn("Waiting", record["state"]["messages"][1]["content"])
        self.assertEqual(self.store.read(self.root)["state"]["messages"], [])
        self.assertEqual(self.agent.workspace()["mode"], "demo")

    def test_ai_provider_receives_branch_context_and_only_reply_is_committed(self):
        noted = self.agent.chat("main", "/remember budget=small", self.root)
        provider = Mock()
        captured = {}

        def reply(messages, context):
            captured["messages"] = json.loads(json.dumps(messages))
            captured["context"] = context
            return "Try the pilot first."

        provider.reply.side_effect = reply
        ai = Agent(self.store, provider)
        head = ai.chat("main", "What would you suggest?", noted)
        messages, context = captured["messages"], captured["context"]
        self.assertEqual(messages[-1]["content"], "What would you suggest?")
        self.assertEqual(context["notes"], {"budget": "small"})
        self.assertIn("analysis", context)
        self.assertEqual(self.store.read(head)["state"]["messages"][-1]["content"], "Try the pilot first.")
        self.assertEqual(ai.mode, "ai")
        self.assertEqual(len(self.store.history("main")), 3)

    def test_model_failure_creates_no_partial_conversation_commit(self):
        provider = Mock()
        provider.reply.side_effect = ProviderError("Unavailable")
        agent = Agent(self.store, provider)
        with self.assertRaises(ProviderError):
            agent.chat("main", "Tell me more", self.root)
        self.assertEqual(self.store.head("main"), self.root)
        self.assertEqual(self.store.read("main")["state"]["messages"], [])
        self.assertEqual(len(self.store.history("main")), 1)

    def test_branch_moving_during_model_call_rejects_stale_reply(self):
        entered, finish = threading.Event(), threading.Event()
        failures = []

        class BlockingProvider:
            def reply(self, messages, context):
                entered.set()
                if not finish.wait(timeout=5):
                    raise TimeoutError("Test release was not signaled")
                return "Outdated advice"

        ai = Agent(self.store, BlockingProvider())

        def chat():
            try:
                ai.chat("main", "Slow request", self.root)
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=chat)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=3))
            current = self.agent.chat("main", "/remember priority=reversibility", self.root)
        finally:
            finish.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ConflictError)
        self.assertEqual(self.store.head("main"), current)
        self.assertEqual(len(self.store.read("main")["state"]["messages"]), 2)
        self.assertNotIn("Outdated advice", str(self.store.read("main")["state"]))

    def test_notes_are_branch_local_and_forgetting_preserves_history(self):
        self.store.branch("alternative", self.root)
        noted = self.agent.chat("alternative", "/remember constraint=low cost", self.root)
        self.assertEqual(self.store.read("main")["state"]["notes"], {})
        self.assertEqual(self.store.read(noted)["state"]["notes"], {"constraint": "low cost"})
        forgotten = self.agent.chat("alternative", "/forget constraint", noted)
        self.assertEqual(self.store.read(forgotten)["state"]["notes"], {})
        self.assertEqual(self.store.read(noted)["state"]["notes"], {"constraint": "low cost"})

    def test_observation_updates_beliefs_records_cost_and_consumes_signal(self):
        decision = launch_decision()
        observed = self.agent.chat("main", "/observe positive", self.root)
        state = self.store.read(observed)["state"]
        self.assertNotIn("wait", state["decision"])
        for item in state["decision"]["states"]:
            self.assertAlmostEqual(item["probability"], {"demand": 0.8, "no_demand": 0.2}[item["id"]])
        self.assertEqual(state["observations"][0]["cost"], decision["wait"]["cost"])
        self.assertEqual(state["observations"][0]["decision_commit"], self.root)
        self.assertEqual(state["observations"][0]["signal_id"], "positive")
        self.assertIsNone(self.agent.workspace()["analysis"]["wait"])
        with self.assertRaises(ValueError):
            self.agent.chat("main", "/observe positive", observed)
        self.assertEqual(self.store.head("main"), observed)
        self.assertIn("wait", self.store.read(self.root)["state"]["decision"])

    def test_recorded_choice_is_intent_without_external_execution(self):
        provider = Mock()
        agent = Agent(self.store, provider)
        chosen = agent.choose("main", "launch", self.root)
        state = self.store.read(chosen)["state"]
        self.assertEqual(state["choices"][0]["action_id"], "launch")
        self.assertEqual(state["choices"][0]["kind"], "recorded_intent")
        self.assertIs(state["choices"][0]["external_effect"], False)
        self.assertEqual(state["choices"][0]["decision_commit"], self.root)
        self.assertIn("no external action", state["messages"][-1]["content"])
        self.assertEqual(state["decision"], launch_decision())
        provider.reply.assert_not_called()

    def test_invalid_commands_and_stale_mutations_leave_head_unchanged(self):
        for message in ("", " " * 3, "x" * 12001, None, "/remember broken",
                        "/forget nonexistent", "/observe impossible"):
            with self.subTest(message=str(message)[:30]):
                with self.assertRaises(ValueError):
                    self.agent.chat("main", message, self.root)
                self.assertEqual(self.store.head("main"), self.root)
        with self.assertRaises(ValueError):
            self.agent.choose("main", "not-an-action", self.root)
        advanced = self.agent.chat("main", "/decide", self.root)
        for action in (lambda: self.agent.chat("main", "/decide", self.root),
                       lambda: self.agent.choose("main", "launch", self.root),
                       lambda: self.agent.decision("main", launch_decision(), self.root)):
            with self.assertRaises(ConflictError):
                action()
        self.assertEqual(self.store.head("main"), advanced)

    def test_invalid_decision_cannot_replace_current_assumptions(self):
        invalid = launch_decision()
        invalid["states"][0]["probability"] = 2
        with self.assertRaises(ValueError):
            self.agent.decision("main", invalid, self.root)
        self.assertEqual(self.store.head("main"), self.root)

    def test_semantically_invalid_merge_of_valid_decisions_is_rejected(self):
        without_wait = launch_decision()
        del without_wait["wait"]
        base = self.agent.decision("main", without_wait, self.root)
        self.store.branch("alternative", base)
        renamed = launch_decision()
        del renamed["wait"]
        names = {"demand": "high", "no_demand": "low"}
        for state in renamed["states"]:
            state["id"] = names[state["id"]]
        for action in renamed["actions"]:
            action["payoffs"] = {names[key]: value for key, value in action["payoffs"].items()}
        left = self.agent.decision("main", renamed, base)
        right = self.agent.decision("alternative", launch_decision(), base)
        with self.assertRaisesRegex(ValueError, "state ids"):
            self.agent.merge("main", "alternative", left)
        self.assertEqual(self.store.branches(), {"main": left, "alternative": right})
        self.assertEqual(len(self.store.history("main")), 3)

    def test_ai_help_describes_current_mode_without_calling_model(self):
        provider = Mock()
        agent = Agent(self.store, provider)
        result = agent.chat("main", "/help", self.root)
        reply = self.store.read(result)["state"]["messages"][-1]["content"]
        self.assertNotIn("Demo mode", reply)
        provider.reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
