from copy import deepcopy
import unittest
from unittest.mock import patch

from dao.config import Config
from dao.decision import demo_payload
from dao.provider import ProviderError
from dao.relationships import digest
from dao.service import Dao
from dao.store import ConflictError, Store
from dao.structure import citations_for, dependency_index, message_records


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.app = Dao(self.store, Config())
        self.chat("A sample passage about SQLite: this must work offline. 道😀 Concurrent writers may change that choice.")
        self.citation = self.quote("must work offline")

    def tearDown(self):
        self.store.close()

    def chat(self, message, branch="main"):
        with patch("dao.provider.time.sleep"):
            return list(self.app.chat({"branch": branch, "expected_head": self.store.head(branch)["id"], "message": message}))

    def mutate(self, route, branch="main", **data):
        return self.app.mutate(route, {"branch": branch, "expected_head": self.store.head(branch)["id"], **data})

    def quote(self, text):
        message = self.app.message_records(self.store.head()["state"])[0]
        start = message["content"].index(text)
        return {"message_id": message["id"], "start": start, "end": start + len(text), "quote": text}

    def node(self, identifier="offline", kind="requirement"):
        return {"operation": "node", "node": {"id": identifier, "label": identifier,
                "kind": kind, "importance": 1, "citations": [deepcopy(self.citation)]}}

    def relation(self, identifier, source, target, kind="depends_on"):
        return {"operation": "relation", "relation": {"id": identifier, "source": source,
                "target": target, "kind": kind, "weight": 1, "severe": False, "actions": [],
                "citations": [deepcopy(self.citation)]}}

    def proposal(self, operations=None):
        return self.mutate("/api/structure/propose", rationale="Preserve the stated requirement",
                           operations=[self.node()] if operations is None else operations)["result"]

    def test_messages_have_unique_ids_that_survive_branch_and_restore(self):
        old = self.store.head()
        ids = [m["id"] for m in old["state"]["messages"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.store.branch("alternative", old["id"])
        self.chat("Same follow-up")
        self.chat("Same follow-up", "alternative")
        main = self.store.head()["state"]["messages"]
        other = self.store.head("alternative")["state"]["messages"]
        self.assertEqual([m["id"] for m in main[:2]], ids)
        self.assertNotEqual(main[2]["id"], other[2]["id"])
        self.mutate("/api/restore", commit_id=old["id"])
        self.assertEqual([m["id"] for m in self.store.head()["state"]["messages"]], ids)
        self.assertTrue(self.store.verify()["ok"])

    def test_legacy_ids_are_read_only_then_materialized_without_rehashing_old_commits(self):
        state = deepcopy(self.store.head()["state"])
        for message in state["messages"]:
            del message["id"]
        legacy = self.store.commit("main", state, "legacy", "Legacy fixture", self.store.head()["id"])
        snapshot = self.app.snapshot()
        ids = [m["id"] for m in snapshot["messages"]]
        self.assertTrue(all(identifier.startswith("legacy-") for identifier in ids))
        self.assertEqual(self.store.get_commit(legacy["id"]), legacy)
        self.assertEqual(snapshot["head"], legacy)
        self.mutate("/api/memory", key="note", value="Next revision")
        self.assertEqual([m["id"] for m in self.store.head()["state"]["messages"]], ids)
        self.assertEqual(self.store.get_commit(legacy["id"]), legacy)
        self.assertTrue(self.store.verify()["ok"])

    def test_exact_unicode_passage_identity_and_detached_read_views(self):
        citation = self.quote("道😀 Concurrent")
        state = self.store.head()["state"]
        first = citations_for(state, [citation])[0]
        self.assertEqual(len(first["passage_id"]), 64)
        self.assertEqual(citations_for(state, [first])[0], first)
        detached = message_records(state)
        detached[0]["content"] = "Changed read view"
        self.assertNotEqual(detached, message_records(state))
        self.assertNotIn("passage_id", citation)

    def test_forged_missing_failed_and_mismatched_citations_are_rejected(self):
        state = self.store.head()["state"]
        for change in ({"quote": "invented"}, {"end": 99999}, {"message_id": "missing"},
                       {"passage_id": "0" * 64}, {"start": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                citations_for(state, [{**self.citation, **change}])
        state["messages"][0]["status"] = "failed"
        with self.assertRaisesRegex(ValueError, "nonfailed"):
            citations_for(state, [self.citation])

    def test_staging_does_not_change_graph_and_accept_is_atomic(self):
        before = digest(self.store.head()["state"].get("relationships"))
        proposal = self.proposal([self.node(), self.node("sqlite", "decision"), self.relation("choice", "sqlite", "offline")])
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(proposal["source"], "operator")
        self.assertEqual(digest(self.store.head()["state"].get("relationships")), before)
        self.assertIn("passage_id", proposal["operations"][0]["node"]["citations"][0])
        accepted = self.mutate("/api/structure/review", proposal_id=proposal["id"], decision="accept")["result"]
        graph = self.store.head()["state"]["relationships"]
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["accepted_digest"], digest(graph))
        self.assertEqual(self.app.snapshot()["dependencies"]["offline"], ["sqlite"])
        self.assertTrue(self.store.verify()["ok"])

    def test_invalid_later_operation_cannot_partially_stage_or_accept(self):
        before = self.store.head()
        with self.assertRaises(ValueError):
            self.proposal([self.node(), self.relation("broken", "missing", "offline")])
        self.assertEqual(self.store.head(), before)
        proposal = self.proposal()
        state = self.store.head()["state"]
        state["messages"][0]["content"] = "Citation source was changed"
        changed = self.store.commit("main", state, "fixture", "Changed source", self.store.head()["id"])
        with self.assertRaises(ValueError):
            self.mutate("/api/structure/review", proposal_id=proposal["id"], decision="accept")
        self.assertEqual(self.store.head(), changed)
        self.assertNotIn("relationships", changed["state"])

    def test_rejection_retains_proposal_without_accepting_structure(self):
        proposal = self.proposal()
        rejected = self.mutate("/api/structure/review", proposal_id=proposal["id"], decision="reject")["result"]
        self.assertEqual(rejected["status"], "rejected")
        self.assertNotIn("relationships", self.store.head()["state"])
        with self.assertRaisesRegex(ValueError, "Only pending"):
            self.mutate("/api/structure/review", proposal_id=proposal["id"], decision="accept")

    def test_graph_changes_require_fresh_proposal_and_head_check(self):
        proposal = self.proposal()
        old = self.store.head()["id"]
        self.mutate("/api/relationships", **self.node("another"))
        with self.assertRaises(ConflictError):
            self.app.mutate("/api/structure/review", {"proposal_id": proposal["id"], "decision": "accept", "expected_head": old})
        with self.assertRaisesRegex(ConflictError, "Relationships changed"):
            self.mutate("/api/structure/review", proposal_id=proposal["id"], decision="accept")
        self.mutate("/api/structure/review", proposal_id=proposal["id"], decision="reject")
        self.assertEqual(list(self.store.head()["state"]["relationships"]["nodes"]), ["another"])

    def test_proposals_survive_branching_and_restore_with_lifetime_usage(self):
        proposal = self.proposal()
        checkpoint = self.store.head()
        self.store.branch("alternative", checkpoint["id"])
        self.mutate("/api/structure/review", proposal_id=proposal["id"], decision="accept")
        self.assertNotIn("relationships", self.store.head("alternative")["state"])
        self.mutate("/api/structure/review", branch="alternative", proposal_id=proposal["id"], decision="reject")
        usage = self.store.usage()
        self.mutate("/api/restore", commit_id=checkpoint["id"])
        self.assertEqual(self.store.head()["state"]["proposals"][0]["status"], "pending")
        self.assertEqual(self.store.usage(), usage)
        self.assertTrue(self.store.verify()["ok"])

    def test_proposal_limits_and_privileged_operations_are_rejected(self):
        ungrounded = self.node()
        del ungrounded["node"]["citations"]
        cases = [[ungrounded], [], [self.node()] * 33,
                 [{"operation": "resolve", "relation_id": "risk", "citations": [self.citation]}]]
        for operations in cases:
            before = self.store.head()
            with self.subTest(operations=operations), self.assertRaises(ValueError):
                self.proposal(operations)
            self.assertEqual(self.store.head(), before)
        with self.assertRaises(ValueError):
            self.mutate("/api/structure/propose", rationale="x", operations=[self.node()], status="accepted")
        with self.assertRaises(ValueError):
            self.mutate("/api/structure/propose", rationale="x" * 4001, operations=[self.node()])

    def test_direct_operations_and_decisions_validate_source_references(self):
        bad = self.node()
        bad["node"]["citations"][0]["quote"] = "Fabricated requirement"
        before = self.store.head()
        with self.assertRaises(ValueError):
            self.mutate("/api/relationships", **bad)
        self.assertEqual(self.store.head(), before)
        self.mutate("/api/relationships", **self.node())
        self.mutate("/api/decision", problem=demo_payload(), dependency_ids=["offline"], citations=[self.citation])
        record = self.store.head()["state"]["decisions"][-1]
        self.assertEqual(record["dependency_ids"], ["offline"])
        self.assertEqual(record["citations"][0]["quote"], self.citation["quote"])
        self.assertEqual(record["relationship_digest"], digest(self.store.head()["state"]["relationships"]))
        for dependencies in (["missing"], ["offline", "offline"]):
            with self.assertRaises(ValueError):
                self.mutate("/api/decision", problem=demo_payload(), dependency_ids=dependencies)
        with self.assertRaises(ValueError):
            self.mutate("/api/decision", problem=demo_payload(), citations={})

    def test_model_proposal_waits_for_explicit_review(self):
        self.app = Dao(self.store, Config(provider="openai", api_key="test-only"))

        def stream(state, config, evaluate, propose, remember=None):
            proposal = propose({"rationale": "Grounded requirement", "operations": [self.node()]})
            yield {"type": "proposal", "name": "propose_structure", "result": proposal}
            yield {"type": "delta", "text": "The proposed requirement is ready for your review."}
            yield {"type": "usage", "input_tokens": 10, "output_tokens": 5, "estimated": False, "status": "completed"}

        with patch("dao.service.openai_stream", stream):
            events = self.chat("Keep track of that requirement")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(next(e for e in events if e["type"] == "proposal")["result"]["status"], "pending")
        self.assertNotIn("relationships", self.store.head()["state"])
        self.assertEqual(self.store.head()["state"]["proposals"][0]["source"], "model")

    def test_failed_turn_rolls_back_new_proposals_and_keeps_previous_candidates(self):
        original = self.proposal()
        self.app = Dao(self.store, Config(provider="openai", api_key="test-only"))

        def stream(state, config, evaluate, propose, remember=None):
            proposal = propose({"rationale": "Another requirement", "operations": [self.node("other")]})
            yield {"type": "proposal", "name": "propose_structure", "result": proposal}
            raise ProviderError("Interrupted after proposing")

        with patch("dao.service.openai_stream", stream):
            events = self.chat("Please preserve the context")
        self.assertEqual(events[-1]["type"], "error")
        state = self.store.head()["state"]
        self.assertEqual(state["proposals"], [original])
        self.assertNotIn("relationships", state)
        self.assertEqual(state["messages"][-1]["status"], "failed")
        self.assertEqual(self.store.usage()["entries"][-1]["status"], "unknown")
        self.assertTrue(self.store.verify()["ok"])

    def test_dependency_index_is_transitive_and_cycle_safe(self):
        graph = {"nodes": {key: {} for key in ("assumption", "analysis", "decision")},
                 "relations": {"a": {"kind": "depends_on", "source": "analysis", "target": "assumption"},
                               "b": {"kind": "depends_on", "source": "decision", "target": "analysis"},
                               "c": {"kind": "depends_on", "source": "assumption", "target": "decision"},
                               "d": {"kind": "supersedes", "source": "assumption", "target": "analysis"}}}
        self.assertEqual(dependency_index(graph)["assumption"], ["analysis", "decision"])
        self.assertEqual(dependency_index(None), {})


if __name__ == "__main__":
    unittest.main()
