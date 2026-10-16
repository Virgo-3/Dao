from copy import deepcopy
import unittest
from unittest.mock import patch

from dao.config import Config
from dao.provider import ProviderError
from dao.service import Dao
from dao.store import ConflictError, Store


class ConversationalServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.app = Dao(self.store, Config())

    def tearDown(self):
        self.store.close()

    def chat(self, message, branch="main", **data):
        with patch("dao.provider.time.sleep"):
            return list(self.app.chat({"message": message, "branch": branch,
                "expected_head": self.store.head(branch)["id"], **data}))

    def test_local_decision_recall_and_tentative_option_need_no_provider_or_review(self):
        self.app = Dao(self.store, Config(provider="openai", api_key="test-only", token_budget=1))
        with patch("dao.service.openai_stream") as provider:
            events = self.chat("We chose SQLite because this must work offline.")
            self.assertEqual(events[-1]["type"], "done")
            state = events[-1]["head"]["state"]
            decision = next(node for node in state["relationships"]["nodes"].values() if node["kind"] == "decision")
            self.assertEqual(decision["stance"], "decided")
            self.assertNotIn("proposals", state)
            self.assertNotIn("_turn_activities", state)
            self.assertEqual(state["messages"][-1]["activities"][0]["label"], "Decision remembered")
            self.assertLess(next(i for i, event in enumerate(events) if event["type"] == "activity"), len(events)-1)
            self.chat("Maybe Postgres")
            notes = self.store.head()["state"]["relationships"]["nodes"].values()
            option = next(node for node in notes if node["label"] == "Postgres")
            self.assertEqual(option["stance"], "considering")
            recalled = self.chat("Why did we decide that?")[-1]["head"]["state"]["messages"][-1]
            self.assertIn("SQLite", recalled["content"])
            self.assertIn("offline", recalled["content"])
            self.assertTrue(recalled["activities"][0]["citations"])
            provider.assert_not_called()
        self.assertEqual(self.store.usage()["entries"], [])
        self.assertTrue(self.store.verify()["ok"])

    def test_correction_keeps_original_reason_and_inspectable_source(self):
        self.chat("We’re using SQLite because this must work offline.")
        before = deepcopy(self.store.head()["state"]["relationships"])
        events = self.chat("Actually, multiple people need to edit simultaneously.")
        graph = events[-1]["head"]["state"]["relationships"]
        self.assertTrue(set(before["nodes"]).issubset(graph["nodes"]))
        activity = events[-1]["head"]["state"]["messages"][-1]["activities"][0]
        self.assertEqual(activity["kind"], "assumption_changed")
        self.assertTrue(activity["previous_node_ids"])
        self.assertTrue(activity["citations"])
        self.assertEqual(graph["conflicts"], {})

    def test_conversational_branch_and_switch_preserve_each_branch_context(self):
        self.chat("We chose SQLite because this must work offline.")
        source_graph = deepcopy(self.store.head()["state"]["relationships"])
        events = self.chat("Let’s explore another direction.")
        self.assertEqual(events[-1]["branch"], "alternative")
        self.assertEqual(self.store.head()["id"], self.store.head("alternative")["id"])
        self.chat("Remember that this alternative uses a hosted service.", "alternative")
        alternative = deepcopy(self.store.head("alternative")["state"]["relationships"])
        switched = self.chat("Go back to main", "alternative")
        self.assertEqual(switched[-1]["branch"], "main")
        main = switched[-1]["head"]["state"]
        self.assertEqual(main["relationships"], source_graph)
        self.assertEqual(main["messages"][-2]["content"], "Go back to main")
        self.assertEqual(self.store.head("alternative")["state"]["relationships"], alternative)
        self.assertGreater(len(alternative["nodes"]), len(source_graph["nodes"]))
        again = self.chat("Let's explore another direction")
        self.assertEqual(again[-1]["branch"], "alternative-2")
        self.assertTrue(self.store.verify()["ok"])

    def test_unknown_existing_and_busy_navigation_never_claims_a_new_alternative(self):
        unknown = self.chat("Switch to missing")
        self.assertEqual(unknown[-1]["branch"], "main")
        self.assertIn("couldn't find", unknown[-1]["head"]["state"]["messages"][-1]["content"])
        same = self.chat("Create a branch called main")
        self.assertEqual(same[-1]["branch"], "main")
        self.assertEqual(len(self.store.branches()), 1)
        self.store.branch("other", self.store.head()["id"])
        original = self.store.head("other")
        with self.app.branch_lock("other"):
            failed = self.chat("Switch to other")
        self.assertEqual(failed[-1]["type"], "error")
        self.assertEqual(self.store.head("other"), original)
        self.assertNotIn("activities", self.store.head()["state"]["messages"][-1])

    def test_quoted_hypothetical_and_literal_navigation_do_not_move_branches(self):
        for message, literal in [("What if we create a branch called experiment?", False),
                                 ('"Let’s explore another direction"', False),
                                 ("Let's explore another direction", True)]:
            with self.subTest(message=message):
                events = self.chat(message, literal=literal)
                self.assertEqual(events[-1]["branch"], "main")
        self.assertEqual(len(self.store.branches()), 1)

    def test_failed_model_capture_keeps_prior_graph_and_no_success_activity(self):
        self.chat("Remember that source passages matter.")
        original = deepcopy(self.store.head()["state"]["relationships"])
        self.app = Dao(self.store, Config(provider="openai", api_key="test-only"))

        def stream(state, config, evaluate, propose, remember):
            message = state["messages"][-1]
            citation = {"message_id": message["id"], "start": 0, "end": len(message["content"]), "quote": message["content"]}
            activity = remember({"rationale": "A mentioned topic", "operations": [{"operation": "node", "node": {
                "id": "new-note", "label": "A topic", "kind": "claim", "importance": 1,
                "stance": "mentioned", "citations": [citation]}}]})
            yield {"type": "activity", "result": activity}
            yield {"type": "delta", "text": "A partial response"}
            raise ProviderError("Interrupted")

        with patch("dao.service.openai_stream", stream):
            events = self.chat("Can we discuss this topic?")
        self.assertEqual(events[-1]["type"], "error")
        self.assertFalse(any(event["type"] == "activity" for event in events))
        state = self.store.head()["state"]
        self.assertEqual(state["relationships"], original)
        self.assertNotIn("_turn_activities", state)
        self.assertEqual(state["messages"][-1]["status"], "failed")
        self.assertNotIn("activities", state["messages"][-1])
        self.assertEqual(self.store.usage()["entries"][-1]["status"], "unknown")
        self.assertTrue(self.store.verify()["ok"])

    def test_commit_failure_rolls_back_capture_before_writing_failed_reply(self):
        with patch.object(self.store, "commit_turn", side_effect=ConflictError("Destination changed")):
            events = self.chat("We chose SQLite because this must work offline.")
        self.assertEqual(events[-1]["type"], "error")
        state = self.store.head()["state"]
        self.assertNotIn("relationships", state)
        self.assertEqual([message["role"] for message in state["messages"]], ["user", "assistant"])
        self.assertEqual(state["messages"][-1]["status"], "failed")
        self.assertNotIn("activities", state["messages"][-1])
        self.assertTrue(self.store.verify()["ok"])

    def test_local_recall_still_works_when_provider_context_is_full(self):
        head = self.store.head()
        state = deepcopy(head["state"])
        state["memory"]["large-legacy-note"] = "x" * 1_050_000
        self.store.commit("main", state, "fixture", "Large context", head["id"])
        self.app = Dao(self.store, Config(provider="openai", api_key="test-only", token_budget=1))
        with patch("dao.service.openai_stream") as provider:
            events = self.chat("What do you remember?")
            self.assertEqual(events[-1]["type"], "done")
            provider.assert_not_called()

    def test_natural_memory_summary_includes_legacy_saved_notes_with_bounded_text(self):
        self.chat("/remember preference=Keep alternatives open")
        events = self.chat("What do you remember?")
        answer = events[-1]["head"]["state"]["messages"][-1]
        self.assertIn("Keep alternatives open", answer["content"])
        self.assertEqual(answer["activities"][0]["memory_keys"], ["preference"])

    def test_concurrent_source_write_ends_stream_without_overwriting_newer_state(self):
        finish = self.store.commit_turn

        def competing_write(*args, **kwargs):
            head = self.store.head()
            external = deepcopy(head["state"])
            external["memory"]["external"] = "Keep this edit"
            self.store.commit("main", external, "memory", "Another process wrote", head["id"])
            return finish(*args, **kwargs)

        with patch.object(self.store, "commit_turn", competing_write):
            events = self.chat("We chose SQLite because this must work offline.")
        self.assertEqual(events[-1]["type"], "error")
        self.assertFalse(any(event["type"] == "activity" for event in events))
        self.assertEqual(events[-1]["head"], self.store.head())
        state = self.store.head()["state"]
        self.assertEqual(state["memory"]["external"], "Keep this edit")
        self.assertNotIn("relationships", state)
        self.assertEqual([message["role"] for message in state["messages"]], ["user"])
        self.assertTrue(self.store.verify()["ok"])


class NavigationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_duplicate_branch_name_rolls_back_source_reply_and_journal(self):
        original = self.store.head()
        self.store.branch("existing", original["id"])
        events = self.store.events()
        state = deepcopy(original["state"])
        state["messages"].append({"role": "assistant", "content": "Not yet saved"})
        with self.assertRaises(ConflictError):
            self.store.commit_turn("main", state, original["id"], new_branch="existing")
        self.assertEqual(self.store.head(), original)
        self.assertEqual(self.store.events(), events)

    def test_stale_destination_rolls_back_source_and_target_as_one_transaction(self):
        source = self.store.head()
        self.store.branch("other", source["id"])
        target_state = deepcopy(source["state"])
        target_state["memory"]["other"] = "Preserve this"
        target = self.store.commit("other", target_state, "memory", "Other context", source["id"])
        with self.assertRaises(ConflictError):
            self.store.commit_turn("main", source["state"], source["id"], switch_to="other",
                                   target_state=source["state"], target_expected=source["id"])
        self.assertEqual(self.store.head(), source)
        self.assertEqual(self.store.head("other"), target)
        self.assertTrue(self.store.verify()["ok"])

    def test_switch_uses_destination_state_and_preserves_global_usage(self):
        source = self.store.head()
        self.store.branch("other", source["id"])
        target_state = deepcopy(source["state"])
        target_state["memory"]["other"] = "Target context"
        self.store.reserve_usage("main", "already-paid", 100, 1000)
        self.store.finalize_usage("already-paid", 20, 10, False, "completed", "test", 0)
        usage = self.store.usage()
        result = self.store.commit_turn("main", source["state"], source["id"], switch_to="other",
                                        target_state=target_state, target_expected=source["id"])
        self.assertEqual(result["branch"], "other")
        self.assertEqual(result["head"]["state"]["memory"], {"other": "Target context"})
        self.assertEqual(self.store.head()["state"]["memory"], {})
        self.assertEqual(self.store.usage(), usage)
        self.assertTrue(self.store.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
