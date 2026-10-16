from copy import deepcopy
import unittest
import uuid

from dao.conversation import capture, prepare
from dao.relationships import apply, digest
from dao.structure import dependency_index, message_records, propose


def state(content="We chose SQLite because it must work offline."):
    return {"messages": [{"id": "user-1", "role": "user", "content": content}],
            "memory": {}, "decisions": [], "artifacts": {}}


def say(current, content, role="user"):
    current["messages"].append({"id": uuid.uuid4().hex, "role": role, "content": content})


def citation(current):
    message = message_records(current)[-1]
    return {"message_id": message["id"], "start": 0, "end": len(message["content"]), "quote": message["content"]}


def node(current, identifier="note", kind="claim", stance=None):
    record = {"id": identifier, "kind": kind, "label": identifier, "importance": 1,
              "citations": [citation(current)]}
    if stance is not None:
        record["stance"] = stance
    return {"operation": "node", "node": record}


def payload(*operations):
    return {"rationale": "Keep the user's stated context.", "operations": list(operations)}


class ConversationTests(unittest.TestCase):
    def test_affirmative_decision_and_reason_are_grounded_offline(self):
        current = state()
        original_message = deepcopy(current["messages"])
        result = prepare(current)
        self.assertIn("decision to use SQLite", result["reply"])
        self.assertEqual(result["activities"][0]["label"], "Decision remembered")
        self.assertIsNone(result["navigation"])
        graph = current["relationships"]
        self.assertEqual(len(graph["nodes"]), 2)
        decision = next(value for value in graph["nodes"].values() if value["kind"] == "decision")
        reason = next(value for value in graph["nodes"].values() if value["kind"] == "requirement")
        self.assertEqual(decision["stance"], "decided")
        self.assertEqual(reason["stance"], "believed")
        self.assertEqual(reason["content"], "it must work offline")
        link = next(iter(graph["relations"].values()))
        self.assertEqual((link["source"], link["target"], link["kind"]), (decision["id"], reason["id"], "depends_on"))
        self.assertIsNone(link["belief"])
        self.assertEqual(graph["conflicts"], {})
        self.assertEqual(current["messages"], original_message)
        self.assertNotIn("activities", current)
        for record in [decision, reason, link]:
            self.assertEqual(record["citations"][0]["quote"], original_message[0]["content"])
            self.assertEqual(len(record["citations"][0]["passage_id"]), 64)

    def test_using_phrase_and_uncertain_reason_preserve_real_decision(self):
        for text in ("We're using SQLite because it is convenient.",
                     "We’re using SQLite because it might work offline.",
                     "WE CHOSE SQLite BECAUSE it is convenient."):
            with self.subTest(text=text):
                current = state(text)
                result = prepare(current)
                self.assertEqual(result["activities"][0]["label"], "Decision remembered")
                decision = next(n for n in current["relationships"]["nodes"].values() if n["kind"] == "decision")
                self.assertNotIn("because", decision["content"].lower())
                if "might" in text:
                    reason = next(n for n in current["relationships"]["nodes"].values() if n["kind"] != "decision")
                    self.assertEqual(reason["stance"], "considering")

    def test_direct_affirmative_decisions_do_not_invent_rationale(self):
        for text in ("We chose SQLite.", "Let's use Postgres", "We’ve decided on SQLite",
                     "We've chosen SQLite", "I've decided to use SQLite", "We will use SQLite"):
            with self.subTest(text=text):
                current = state(text)
                result = prepare(current)
                self.assertEqual(result["activities"][0]["label"], "Decision remembered")
                graph = current["relationships"]
                self.assertEqual(len(graph["nodes"]), 1)
                self.assertEqual(graph["relations"], {})
                self.assertEqual(next(iter(graph["nodes"].values()))["stance"], "decided")
                say(current, "Why did we decide that?")
                self.assertIn("No supporting reason was recorded", prepare(current)["reply"])

    def test_same_message_changes_require_clarification_before_capture(self):
        for text in (
            "We chose SQLite because it works offline. Actually, we changed our minds and will use Postgres instead.",
            "We chose SQLite because it works offline; but we haven't decided yet.",
            "We chose SQLite because it works offline, but we will use Postgres.",
            "We chose SQLite because it works offline. On second thought, scratch that.",
            "We chose SQLite because it works offline. We won't use it.",
            "Remember that we chose SQLite because it works offline. Actually, scratch that.",
        ):
            with self.subTest(text=text):
                current = state(text)
                before = deepcopy(current)
                result = prepare(current)
                self.assertIn("Which decision", result["reply"])
                self.assertEqual(result["activities"], [])
                self.assertIsNone(result["navigation"])
                self.assertEqual(current, before)
                for kind, stance in (("decision", None), ("claim", "decided")):
                    submitted = node(current, kind=kind, stance=stance)
                    # Even an exact citation of just the original commitment
                    # cannot bypass the retraction elsewhere in the message.
                    quote = text[:text.index(" because")]
                    submitted["node"]["citations"] = [{**citation(current), "end": len(quote), "quote": quote}]
                    with self.assertRaisesRegex(ValueError, "affirmative decision"):
                        capture(current, payload(submitted))
                    self.assertEqual(current, before)

    def test_multiple_sentences_without_a_new_choice_keep_the_stated_decision(self):
        current = state("We chose SQLite because it works offline. That matters for our field team.")
        result = prepare(current)
        self.assertEqual(result["activities"][0]["label"], "Decision remembered")
        decision = next(n for n in current["relationships"]["nodes"].values() if n["kind"] == "decision")
        self.assertEqual(decision["label"], "SQLite")
        self.assertEqual(decision["citations"][0]["quote"], current["messages"][0]["content"])

    def test_actual_decision_correction_can_replace_only_an_explicitly_named_choice(self):
        current = state()
        prepare(current)
        previous = next(n for n in current["relationships"]["nodes"].values() if n["kind"] == "decision")
        say(current, "Actually, we're using Postgres instead of SQLite because we need concurrent writers.")
        result = prepare(current)
        self.assertEqual(result["activities"][0]["label"], "Decision remembered")
        graph = current["relationships"]
        self.assertEqual(graph["nodes"][previous["id"]], previous)
        replacement = next(link for link in graph["relations"].values() if link["kind"] == "supersedes")
        self.assertEqual(replacement["target"], previous["id"])
        self.assertEqual(graph["nodes"][replacement["source"]]["label"], "Postgres")
        say(current, "Why did we decide that?")
        self.assertIn("Postgres", prepare(current)["reply"])
        self.assertIn("concurrent writers", prepare(current)["reply"])

    def test_new_decision_without_explicit_replacement_preserves_old_active_record(self):
        current = state()
        prepare(current)
        say(current, "Actually, we're using Postgres because it supports our team.")
        prepare(current)
        graph = current["relationships"]
        self.assertEqual(len([n for n in graph["nodes"].values() if n["kind"] == "decision"]), 2)
        self.assertNotIn("supersedes", {link["kind"] for link in graph["relations"].values()})

    def test_tentative_choices_are_not_settled_decisions(self):
        for text in ("Maybe SQLite", "I'm considering Postgres.", "I’m considering SQLite?", "Maybe we chose SQLite because it works offline."):
            with self.subTest(text=text):
                current = state(text)
                result = prepare(current)
                notes = list(current["relationships"]["nodes"].values())
                self.assertEqual(len(notes), 1)
                self.assertEqual(notes[0]["stance"], "considering")
                self.assertNotEqual(notes[0]["kind"], "decision")
                self.assertEqual(result["activities"][0]["label"], "Option remembered")
                say(current, "Why did we decide that?")
                self.assertIn("don’t have a settled decision", prepare(current)["reply"])

    def test_explicit_memory_is_a_believed_claim_with_original_words(self):
        current = state("Remember that the field team works offline 🧠.")
        result = prepare(current)
        remembered = next(iter(current["relationships"]["nodes"].values()))
        self.assertEqual(remembered["kind"], "claim")
        self.assertEqual(remembered["stance"], "believed")
        self.assertEqual(remembered["content"], "the field team works offline 🧠.")
        self.assertEqual(result["activities"][0]["citations"][0]["quote"], current["messages"][0]["content"])

    def test_tentative_memory_remains_tentative(self):
        current = state("Remember that maybe SQLite would help.")
        prepare(current)
        self.assertEqual(next(iter(current["relationships"]["nodes"].values()))["stance"], "considering")

    def test_unknown_quoted_hypothetical_and_negated_intents_do_not_execute(self):
        messages = ["Tell me about SQLite", '"We chose SQLite because it works offline."',
                    "> Remember that we chose SQLite", "If we chose SQLite because it works offline, what happens?",
                    "Imagine we chose SQLite because it works offline.", "We didn't choose SQLite because it works offline.",
                    "We chose SQLite, hypothetically, because it works offline.",
                    '"Create a branch called idea"',
                    "Suppose I say keep that", "If needed, switch to main"]
        for text in messages:
            with self.subTest(text=text):
                current = state(text)
                before = deepcopy(current)
                self.assertEqual(prepare(current), {"reply": None, "activities": [], "navigation": None})
                self.assertEqual(current, before)

    def test_only_the_current_final_user_message_is_examined(self):
        current = state()
        say(current, "A completed response", role="assistant")
        before = deepcopy(current)
        self.assertIsNone(prepare(current)["reply"])
        self.assertEqual(current, before)
        say(current, "Unrelated follow-up")
        self.assertIsNone(prepare(current)["reply"])
        self.assertNotIn("relationships", current)

    def test_legacy_current_message_ids_are_consistent_without_rewriting_messages(self):
        current = state("Remember that we use SQLite.")
        del current["messages"][0]["id"]
        expected = message_records(current)[0]["id"]
        result = prepare(current)
        self.assertEqual(result["activities"][0]["citations"][0]["message_id"], expected)
        self.assertNotIn("id", current["messages"][0])

    def test_capture_requires_current_user_citations_and_assigns_stance(self):
        current = state("Maybe SQLite")
        submitted = payload(node(current))
        before = deepcopy(submitted)
        activity = capture(current, submitted)
        self.assertEqual(submitted, before)
        self.assertEqual(current["relationships"]["nodes"]["note"]["stance"], "considering")
        self.assertEqual(activity["node_ids"], ["note"])
        self.assertNotIn("activities", current)
        say(current, "Remember that it must work offline.")
        old_citation = deepcopy(submitted)
        old_citation["operations"][0]["node"]["id"] = "old-source"
        unchanged = deepcopy(current)
        with self.assertRaisesRegex(ValueError, "current user message"):
            capture(current, old_citation)
        self.assertEqual(current, unchanged)

    def test_capture_rejects_forged_or_assistant_source_citations(self):
        current = state("The actual user words.")
        invalid = payload(node(current))
        invalid["operations"][0]["node"]["citations"][0]["quote"] = "Invented"
        with self.assertRaisesRegex(ValueError, "exactly"):
            capture(current, invalid)
        say(current, "Assistant advice", role="assistant")
        with self.assertRaisesRegex(ValueError, "current user"):
            capture(current, payload(node(current)))
        self.assertNotIn("relationships", current)

    def test_capture_cannot_promote_tentative_quoted_hypothetical_or_negated_choices(self):
        for text in ("Maybe SQLite", '"We chose SQLite"', "If we chose SQLite", "We didn't choose SQLite",
                     "We chose SQLite if it works", "We chose SQLite, but we haven't decided."):
            for kind, stance in (("decision", None), ("claim", "decided")):
                with self.subTest(text=text, kind=kind, stance=stance):
                    current = state(text)
                    before = deepcopy(current)
                    with self.assertRaisesRegex(ValueError, "affirmative decision"):
                        capture(current, payload(node(current, kind=kind, stance=stance)))
                    self.assertEqual(current, before)

    def test_capture_keeps_mentions_distinct_from_beliefs(self):
        for text in ('"Maybe SQLite"', "Imagine SQLite were selected", "You said we chose SQLite", "The document says SQLite is selected"):
            current = state(text)
            capture(current, payload(node(current)))
            self.assertEqual(current["relationships"]["nodes"]["note"]["stance"], "mentioned")
            for stance in ("believed", "considering"):
                with self.assertRaisesRegex(ValueError, "only as mentioned"):
                    capture(current, payload(node(current, identifier=stance, stance=stance)))

    def test_capture_never_changes_conflicts_or_assesses_observations(self):
        current = state("Remember that this is a report.")
        capture(current, payload(node(current, "first"), node(current, "second")))
        relation = {"operation": "relation", "relation": {"id": "relation", "kind": "depends_on", "source": "first",
                    "target": "second", "weight": 1, "severe": False, "actions": [], "citations": [citation(current)]}}
        capture(current, payload(relation))
        for operation in ("assess", "observe", "resolve", "delete"):
            before = deepcopy(current)
            with self.assertRaises(ValueError):
                capture(current, payload({"operation": operation, "relation_id": "relation"}))
            self.assertEqual(current, before)
        for field, value in (("severe", True), ("severe", 0), ("actions", ["Publish"]), ("actions", None)):
            changed = deepcopy(relation)
            changed["relation"]["id"] = "new"
            changed["relation"][field] = value
            before = deepcopy(current)
            with self.assertRaisesRegex(ValueError, "severe false"):
                capture(current, payload(changed))
            self.assertEqual(current, before)
        self.assertEqual(current["relationships"]["conflicts"], {})
        self.assertEqual(current["relationships"]["observations"], [])

    def test_capture_batch_is_atomic_when_later_definition_is_invalid(self):
        current = state("Remember that field work is offline.")
        first, second = node(current, "first"), node(current, "second")
        second["node"]["unknown"] = True
        before = deepcopy(current)
        with self.assertRaises(ValueError):
            capture(current, payload(first, second))
        self.assertEqual(current, before)

    def test_capture_rejects_invalid_payloads_without_mutation(self):
        current = state("Remember that SQLite helps.")
        invalid = [None, {}, {**payload(node(current)), "accept": True}, payload(),
                   payload(*[node(current, str(index)) for index in range(33)]),
                   {"rationale": "", "operations": [node(current)]},
                   {"rationale": "x" * 4001, "operations": [node(current)]},
                   payload({"operation": []}), payload({"operation": "node", "node": {}})]
        missing = node(current)
        del missing["node"]["citations"]
        invalid.append(payload(missing))
        empty = node(current)
        empty["node"]["citations"] = []
        invalid.append(payload(empty))
        invalid_stance = node(current)
        invalid_stance["node"]["stance"] = None
        invalid.append(payload(invalid_stance))
        for submitted in invalid:
            before = deepcopy(current)
            with self.subTest(submitted=str(submitted)[:80]), self.assertRaises(ValueError):
                capture(current, submitted)
            self.assertEqual(current, before)

    def test_recall_returns_decision_reason_and_original_passage(self):
        current = state()
        prepare(current)
        original_graph = deepcopy(current["relationships"])
        say(current, "Why did we decide that?")
        result = prepare(current)
        self.assertIn("We chose SQLite", result["reply"])
        self.assertIn("it must work offline", result["reply"])
        self.assertEqual(result["activities"][0]["kind"], "recalled")
        self.assertEqual(result["activities"][0]["citations"][0]["message_id"], "user-1")
        self.assertEqual(current["relationships"], original_graph)

    def test_summary_distinguishes_mentioned_considering_believed_and_decided(self):
        current = state()
        prepare(current)
        say(current, "Maybe Postgres")
        prepare(current)
        say(current, "Remember that field work is remote.")
        prepare(current)
        say(current, "What do you remember?")
        before = digest(current["relationships"])
        result = prepare(current)
        self.assertIn("Decided: SQLite", result["reply"])
        self.assertIn("Considering: Postgres", result["reply"])
        self.assertIn("Believed:", result["reply"])
        self.assertGreaterEqual(len(result["activities"][0]["citations"]), 3)
        self.assertEqual(digest(current["relationships"]), before)

    def test_correction_preserves_old_reason_and_identifies_decision_to_revisit(self):
        current = state()
        prepare(current)
        original = deepcopy(current["relationships"])
        decision = next(n for n in original["nodes"].values() if n["kind"] == "decision")
        reason = next(n for n in original["nodes"].values() if n["kind"] == "requirement")
        say(current, "Actually, multiple people need to edit simultaneously")
        result = prepare(current)
        activity = result["activities"][0]
        self.assertEqual(activity["kind"], "assumption_changed")
        self.assertIn(decision["id"], activity["affected_node_ids"])
        self.assertIn(reason["id"], activity["previous_node_ids"])
        self.assertIn("worth revisiting", result["reply"])
        self.assertEqual(current["relationships"]["nodes"][reason["id"]], reason)
        self.assertEqual(current["relationships"]["relations"], original["relations"])
        self.assertNotIn("contradicts", {link["kind"] for link in current["relationships"]["relations"].values()})

    def test_explicit_replacement_can_link_old_assumption_without_deletion(self):
        current = state("We chose SQLite because one writer is enough.")
        prepare(current)
        old = next(n for n in current["relationships"]["nodes"].values() if n["kind"] == "assumption")
        say(current, "Actually, we need multiple writers instead of one writer is enough.")
        result = prepare(current)
        superseding = next(link for link in current["relationships"]["relations"].values() if link["kind"] == "supersedes")
        self.assertEqual(superseding["target"], old["id"])
        self.assertEqual(superseding["source"], result["activities"][0]["node_ids"][0])
        self.assertEqual(current["relationships"]["nodes"][old["id"]], old)
        self.assertEqual(current["relationships"]["conflicts"], {})

    def test_unrelated_correction_does_not_infer_decision_impact_from_recency(self):
        current = state()
        prepare(current)
        say(current, "Actually, the meeting is on Friday.")
        result = prepare(current)
        activity = result["activities"][0]
        self.assertEqual(activity["affected_node_ids"], [])
        self.assertEqual(activity["previous_node_ids"], [])
        self.assertEqual(len(activity["possible_node_ids"]), 1)
        self.assertIn("Possible context", activity["summary"])
        self.assertIn("Does that change the earlier decision about SQLite?", result["reply"])
        self.assertNotIn("worth revisiting", result["reply"])

    def test_correction_surfaces_transitive_analysis_dependents(self):
        current = state()
        prepare(current)
        graph = current["relationships"]
        decision = next(n for n in graph["nodes"].values() if n["kind"] == "decision")
        graph, _ = apply(graph, {"operation": "node", "node": {"id": "analysis", "label": "Plan", "kind": "analysis", "importance": 1}})
        graph, _ = apply(graph, {"operation": "relation", "relation": {"id": "planning", "source": "analysis", "target": decision["id"],
                              "kind": "depends_on", "weight": 1, "severe": False, "actions": []}})
        current["relationships"] = graph
        say(current, "Actually, the team needs simultaneous edits.")
        result = prepare(current)
        self.assertIn("analysis", result["activities"][0]["affected_node_ids"])

    def test_natural_navigation_returns_intents_without_touching_state(self):
        for text, expected in [("Let’s explore another direction", {"kind": "branch", "name": None}),
                               ("Let's try another direction.", {"kind": "branch", "name": None}),
                               ("Let's explore an alternative", {"kind": "branch", "name": None}),
                               ("Create a branch called prototype", {"kind": "branch", "name": "prototype"}),
                               ("Switch to prototype", {"kind": "switch", "name": "prototype"}),
                               ("Go back to main", {"kind": "switch", "name": "main"})]:
            with self.subTest(text=text):
                current = state(text)
                before = deepcopy(current)
                self.assertEqual(prepare(current)["navigation"], expected)
                self.assertEqual(current, before)
        for name in ("../escape", "unsafe name", "a//b", "a/", "a..", "x" * 81):
            current = state("Create a branch called " + name)
            self.assertIsNone(prepare(current)["navigation"])

    def test_unique_pending_proposal_can_be_kept_or_rejected_naturally(self):
        for words, status in (("Keep that", "accepted"), ("Accept that proposal", "accepted"),
                              ("Reject that proposal", "rejected")):
            current = state("Remember that the field team works offline.")
            proposal = propose(current, payload(node(current)), source="operator")
            say(current, words)
            result = prepare(current)
            self.assertEqual(current["proposals"][0]["status"], status)
            self.assertEqual(result["activities"][0]["proposal_id"], proposal["id"])
            self.assertEqual("relationships" in current, status == "accepted")

    def test_multiple_pending_proposals_are_clarified_without_ids_or_mutation(self):
        current = state("Remember that offline access matters.")
        first = propose(current, payload(node(current, "offline")), source="operator")
        second = propose(current, {"rationale": "Record the field-team context", "operations": [node(current, "team")]}, source="operator")
        say(current, "Keep that")
        before = deepcopy(current)
        result = prepare(current)
        self.assertIn("Which one", result["reply"])
        self.assertIn("field-team context", result["reply"])
        self.assertNotIn(first["id"], result["reply"])
        self.assertNotIn(second["id"], result["reply"])
        self.assertEqual(current, before)

    def test_stale_proposal_is_not_silently_accepted(self):
        current = state("Remember that offline access matters.")
        propose(current, payload(node(current, "offline")), source="operator")
        capture(current, payload(node(current, "other")))
        say(current, "Keep that")
        before = deepcopy(current)
        result = prepare(current)
        self.assertIn("fresh suggestion", result["reply"])
        self.assertEqual(current, before)

    def test_numbered_suggestion_selection_completes_the_clarification(self):
        for text, selected_status in (("Keep suggestion 2", "accepted"), ("Accept proposal 2", "accepted"),
                                      ("Reject suggestion 2", "rejected")):
            current = state("Remember that offline access matters.")
            propose(current, payload(node(current, "offline")), source="operator")
            second = propose(current, payload(node(current, "team")), source="operator")
            say(current, text)
            result = prepare(current)
            self.assertEqual(current["proposals"][0]["status"], "pending")
            self.assertEqual(current["proposals"][1]["status"], selected_status)
            self.assertEqual(result["activities"][0]["proposal_id"], second["id"])
        for selection in ("0", "3"):
            current = state("Remember that offline access matters.")
            propose(current, payload(node(current, "offline")), source="operator")
            propose(current, payload(node(current, "team")), source="operator")
            say(current, "Keep suggestion " + selection)
            before = deepcopy(current)
            self.assertIn("Choose a number from 1 to 2", prepare(current)["reply"])
            self.assertEqual(current, before)


if __name__ == "__main__":
    unittest.main()
