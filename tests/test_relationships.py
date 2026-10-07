import copy
import hashlib
import json
import math
import unittest

from dao.audit import adjudicate
from dao.relationships import (apply, blocking_conflicts, digest, empty_graph,
                               resolution_claim, summarize)


def node(identifier, kind="goal"):
    return {"id": identifier, "label": identifier, "kind": kind, "importance": 1}


def definition(identifier="effect", weight=1, severe=False, actions=None, source="action", target="person"):
    return {"id": identifier, "source": source, "target": target, "kind": "effect",
            "weight": weight, "severe": severe, "actions": [] if actions is None else actions}


def fixture(weight=1, severe=False, actions=None):
    graph = empty_graph()
    for identifier, kind in (("action", "action"), ("person", "person")):
        graph, _ = apply(graph, {"operation": "node", "node": node(identifier, kind)})
    return apply(graph, {"operation": "relation", "relation": definition(weight=weight, severe=severe, actions=actions)})[0]


def assess(graph, belief, identifier="effect"):
    return apply(graph, {"operation": "assess", "relation_id": identifier, "belief": belief,
                         "source": "submitted report", "content": "Observed impact\nwith supporting context."})[0]


def observation(graph, before="neutral", after="positive", action="Pilot", context="small cohort", identifier="effect"):
    return apply(graph, {"operation": "observe", "relation_id": identifier, "action": action, "context": context,
                         "before": before, "after": after, "source": "experiment log", "content": "Outcome recorded."})[0]


def resolution(graph, identifier="effect"):
    return adjudicate({"claim": resolution_claim(graph, identifier), "evidence": [
        {"source": "outcome log", "content": "The identified adverse outcome has been addressed.", "stance": "support", "reliability": 0.9},
        {"source": "review", "content": "The current nonadverse assessment has supporting evidence.", "stance": "support", "reliability": 0.8},
    ]})


POSITIVE = {"positive": 1, "neutral": 0, "negative": 0}
NEUTRAL = {"positive": 0, "neutral": 1, "negative": 0}
NEGATIVE = {"positive": 0, "neutral": 0, "negative": 1}


class RelationshipTests(unittest.TestCase):
    def test_absent_and_empty_graphs_are_backward_compatible(self):
        self.assertEqual(digest(None), digest(empty_graph()))
        self.assertEqual(summarize(None), summarize(empty_graph()))
        self.assertEqual(blocking_conflicts(None), [])
        summary = summarize(None)
        self.assertIsNone(summary["coherence"])
        self.assertEqual(summary["coverage"], 0)
        self.assertEqual(summary["total_weight"], 0)

    def test_mutations_and_views_are_detached(self):
        graph = fixture()
        before = copy.deepcopy(graph)
        payload = {"operation": "assess", "relation_id": "effect", "belief": copy.deepcopy(POSITIVE),
                   "source": "report", "content": "Benefit observed."}
        payload_before = copy.deepcopy(payload)
        updated, event = apply(graph, payload)
        self.assertEqual(graph, before)
        self.assertEqual(payload, payload_before)
        event["belief"]["positive"] = 0
        self.assertEqual(updated["relations"]["effect"]["belief"]["positive"], 1)
        self.assertIsNone(graph["relations"]["effect"]["belief"])
        self.assertNotEqual(digest(graph), digest(updated))

    def test_new_relations_start_unknown_with_runtime_provenance(self):
        graph = fixture()
        relation = graph["relations"]["effect"]
        self.assertIsNone(relation["belief"])
        self.assertIsNone(relation["assessment"])
        self.assertTrue(relation["created_at"].endswith("+00:00"))
        graph = assess(graph, POSITIVE)
        self.assertEqual(graph["relations"]["effect"]["assessment"]["source"], "submitted report")

    def test_hidden_adverse_relation_increases_coherence_but_reduces_coverage_and_keeps_case(self):
        graph = fixture()
        for index in range(9):
            graph, _ = apply(graph, {"operation": "relation", "relation": definition(f"benefit-{index}")})
            graph = assess(graph, POSITIVE, f"benefit-{index}")
        graph = assess(graph, NEGATIVE)
        initial = summarize(graph)
        self.assertEqual(initial["coherence"], 0.9)
        self.assertEqual(initial["coverage"], 1)
        hidden = assess(graph, None)
        summary = summarize(hidden)
        self.assertEqual(summary["coherence"], 1)
        self.assertEqual(summary["coverage"], 0.9)
        self.assertEqual(summary["unknown_weight"], 1)
        self.assertEqual([item["relation_id"] for item in summary["unresolved_conflicts"]], ["effect"])
        neutral = summarize(assess(hidden, NEUTRAL))
        self.assertEqual(neutral["coverage"], 1)
        self.assertEqual(len(neutral["unresolved_conflicts"]), 1)

    def test_weighted_severe_harm_outweighs_nine_unit_benefits(self):
        graph = assess(fixture(weight=20, severe=True), NEGATIVE)
        for index in range(9):
            graph, _ = apply(graph, {"operation": "relation", "relation": definition(f"benefit-{index}")})
            graph = assess(graph, POSITIVE, f"benefit-{index}")
        summary = summarize(graph)
        self.assertAlmostEqual(summary["coherence"], 9 / 29)
        self.assertEqual(summary["positive_weight"], 9)
        self.assertEqual(summary["negative_weight"], 20)
        self.assertEqual(len(blocking_conflicts(graph, "Publish")), 1)

    def test_all_unknown_and_all_neutral_coherence_are_undefined(self):
        graph = fixture(weight=3)
        unknown = summarize(graph)
        self.assertIsNone(unknown["coherence"])
        self.assertEqual(unknown["coverage"], 0)
        self.assertEqual(unknown["fractions"]["unknown"], 1)
        neutral = summarize(assess(graph, NEUTRAL))
        self.assertIsNone(neutral["coherence"])
        self.assertEqual(neutral["coverage"], 1)
        self.assertEqual(neutral["fractions"]["neutral"], 1)

    def test_directed_relations_represent_asymmetric_effects(self):
        graph = fixture()
        graph, _ = apply(graph, {"operation": "relation", "relation": definition("reverse", source="person", target="action")})
        graph = assess(graph, POSITIVE)
        graph = assess(graph, NEGATIVE, "reverse")
        self.assertEqual(summarize(graph)["coherence"], 0.5)
        self.assertEqual(graph["relations"]["effect"]["source"], "action")
        self.assertEqual(graph["relations"]["reverse"]["source"], "person")

    def test_fixed_definitions_prevent_weight_scope_and_deletion_gaming(self):
        graph = fixture(severe=True, actions=["Publish"])
        original = copy.deepcopy(graph)
        for changed in (definition(weight=100), definition(severe=False), definition(actions=["Wait"])):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "immutable"):
                apply(graph, {"operation": "relation", "relation": changed})
        with self.assertRaisesRegex(ValueError, "immutable"):
            apply(graph, {"operation": "node", "node": node("person")})
        for operation in ("delete", "update", "set_weight"):
            with self.assertRaises(ValueError):
                apply(graph, {"operation": operation, "relation_id": "effect"})
        self.assertEqual(graph, original)

    def test_any_negative_probability_opens_conflict(self):
        graph = assess(fixture(), {"positive": 0.99, "neutral": 0, "negative": 0.01})
        self.assertEqual(len(summarize(graph)["unresolved_conflicts"]), 1)
        self.assertEqual(blocking_conflicts(graph), [])

    def test_severe_conflicts_apply_to_exact_action_or_global_scope(self):
        graph = assess(fixture(severe=True, actions=["Publish"]), NEGATIVE)
        self.assertEqual(len(blocking_conflicts(graph)), 1)
        self.assertEqual(len(blocking_conflicts(graph, "Publish")), 1)
        self.assertEqual(blocking_conflicts(graph, "publish"), [])
        self.assertEqual(blocking_conflicts(graph, "Pilot"), [])
        result = blocking_conflicts(graph)
        result[0]["actions"].append("Pilot")
        self.assertEqual(blocking_conflicts(graph, "Pilot"), [])
        global_graph = assess(fixture(severe=True), NEGATIVE)
        self.assertEqual(len(blocking_conflicts(global_graph, "Pilot")), 1)

    def test_unknown_and_neutral_assessments_never_implicitly_resolve(self):
        graph = assess(fixture(severe=True), NEGATIVE)
        for belief in (None, NEUTRAL, POSITIVE):
            updated = assess(graph, belief)
            self.assertEqual(len(blocking_conflicts(updated)), 1)
            self.assertEqual(updated["conflicts"]["effect"]["status"], "unresolved")

    def test_explicit_resolution_and_reopening_preserve_history(self):
        graph = assess(fixture(severe=True), NEGATIVE)
        graph = assess(graph, POSITIVE)
        prior = copy.deepcopy(graph)
        graph, event = apply(graph, {"operation": "resolve", "relation_id": "effect", "verdict": resolution(graph)})
        self.assertEqual(event["operation"], "resolve")
        self.assertEqual(blocking_conflicts(graph), [])
        self.assertEqual([entry["event"] for entry in graph["conflicts"]["effect"]["history"]], ["opened", "resolved"])
        self.assertEqual(prior["conflicts"]["effect"]["status"], "unresolved")
        graph = assess(graph, NEGATIVE)
        self.assertEqual(len(blocking_conflicts(graph)), 1)
        self.assertEqual([entry["event"] for entry in graph["conflicts"]["effect"]["history"]], ["opened", "resolved", "reopened"])

    def test_resolution_requires_assessment_and_valid_state_bound_evidence(self):
        graph = assess(fixture(severe=True), NEGATIVE)
        for belief in (None, NEGATIVE):
            current = assess(graph, belief)
            with self.assertRaisesRegex(ValueError, "current assessed nonadverse"):
                apply(current, {"operation": "resolve", "relation_id": "effect", "verdict": resolution(current)})
        graph = assess(graph, POSITIVE)
        good = resolution(graph)
        stale_graph = assess(graph, NEUTRAL)
        insufficient = adjudicate({"claim": resolution_claim(graph, "effect"), "evidence": []})
        wrong_claim = adjudicate({"claim": "The release is ready.", "evidence": good["normalized_payload"]["evidence"]})
        forged = copy.deepcopy(good)
        forged["metrics"]["support_score"] = 1
        for verdict in ({"allowed": True}, insufficient, wrong_claim, forged):
            with self.subTest(verdict=verdict), self.assertRaises(ValueError):
                apply(graph, {"operation": "resolve", "relation_id": "effect", "verdict": verdict})
        with self.assertRaisesRegex(ValueError, "exact current graph"):
            apply(stale_graph, {"operation": "resolve", "relation_id": "effect", "verdict": good})

    def test_latest_adverse_observation_prevents_resolution_until_new_outcome(self):
        graph = observation(assess(fixture(severe=True), POSITIVE), after="negative")
        self.assertEqual(len(blocking_conflicts(graph)), 1)
        with self.assertRaisesRegex(ValueError, "latest observed outcome is adverse"):
            apply(graph, {"operation": "resolve", "relation_id": "effect", "verdict": resolution(graph)})
        graph = observation(graph, before="negative", after="positive")
        graph, _ = apply(graph, {"operation": "resolve", "relation_id": "effect", "verdict": resolution(graph)})
        self.assertEqual(blocking_conflicts(graph), [])
        graph = observation(graph, after="negative")
        self.assertEqual(len(blocking_conflicts(graph)), 1)

    def test_observed_negative_before_state_also_opens_a_case(self):
        graph = observation(fixture(), before="negative", after="positive")
        self.assertEqual(len(summarize(graph)["unresolved_conflicts"]), 1)
        self.assertIsNone(graph["relations"]["effect"]["belief"])

    def test_transition_posterior_matches_wolfram_exact_fixture(self):
        graph = fixture()
        for state, count in (("positive", 5), ("neutral", 1), ("negative", 0)):
            for _ in range(count):
                graph = observation(graph, before="neutral", after=state)
        model = summarize(graph)["transition_models"][0]
        row = model["rows"]["neutral"]
        self.assertEqual(row["counts"], {"positive": 5, "neutral": 1, "negative": 0})
        for state, mean, variance in (("positive", 2 / 3, 1 / 45), ("neutral", 2 / 9, 7 / 405), ("negative", 1 / 9, 4 / 405)):
            self.assertAlmostEqual(row["posterior_mean"][state], mean)
            self.assertAlmostEqual(row["posterior_variance"][state], variance)
        prior_row = model["rows"]["negative"]
        self.assertEqual(prior_row["observation_count"], 0)
        for state in ("positive", "neutral", "negative"):
            self.assertAlmostEqual(prior_row["posterior_mean"][state], 1 / 3)
            self.assertAlmostEqual(prior_row["posterior_variance"][state], 1 / 18)

    def test_transition_context_action_and_relation_are_separated(self):
        graph = fixture()
        graph, _ = apply(graph, {"operation": "relation", "relation": definition("other")})
        graph = observation(graph, action="Pilot", context="A")
        graph = observation(graph, action="Pilot", context="B", after="negative")
        graph = observation(graph, action="Wait", context="A", after="neutral")
        graph = observation(graph, action="Pilot", context="A", identifier="other")
        models = summarize(graph)["transition_models"]
        self.assertEqual(len(models), 4)
        self.assertEqual({(item["relation_id"], item["action"], item["context"]) for item in models},
                         {("effect", "Pilot", "A"), ("effect", "Pilot", "B"), ("effect", "Wait", "A"), ("other", "Pilot", "A")})
        self.assertTrue(all(item["observation_count"] == 1 for item in models))

    def test_digest_is_canonical_and_covers_provenance(self):
        graph = fixture()
        canonical = json.dumps(graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        self.assertEqual(digest(graph), hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        reordered = {key: graph[key] for key in reversed(graph)}
        self.assertEqual(digest(graph), digest(reordered))
        changed = copy.deepcopy(graph)
        changed["relations"]["effect"]["created_at"] = "2026-01-01T00:00:00+00:00"
        self.assertNotEqual(digest(graph), digest(changed))

    def test_rejects_nonfinite_bool_and_out_of_range_weights_and_importance(self):
        graph = fixture()
        for value in (True, False, math.nan, math.inf, -math.inf, 0, -1, "2", 1_000_001, 10 ** 1000):
            with self.subTest(value=str(value)[:30]):
                invalid = node("new")
                invalid["importance"] = value
                with self.assertRaises(ValueError):
                    apply(graph, {"operation": "node", "node": invalid})
                invalid_relation = definition("new")
                invalid_relation["weight"] = value
                with self.assertRaises(ValueError):
                    apply(graph, {"operation": "relation", "relation": invalid_relation})

    def test_rejects_invalid_probabilities_without_mutation(self):
        graph = fixture()
        before = copy.deepcopy(graph)
        invalid = [True, "positive", {}, {"positive": 0, "neutral": 0, "negative": 0},
                   {"positive": 0.8, "neutral": 0, "negative": 0.3}, {"positive": True, "neutral": 0, "negative": 0},
                   {"positive": math.nan, "neutral": 0, "negative": 0}, {"positive": 1, "neutral": 0, "negative": -0.1},
                   {**POSITIVE, "confidence": 1}]
        for belief in invalid:
            with self.subTest(belief=belief), self.assertRaises(ValueError):
                assess(graph, belief)
        self.assertEqual(graph, before)

    def test_rejects_missing_unknown_and_wrong_fields(self):
        graph = fixture()
        invalid = [{}, {"operation": "node"}, {"operation": "assess", "relation_id": "effect", "belief": None},
                   {"operation": "assess", "relation_id": "effect", "belief": None, "source": "report", "content": "x", "allowed": True},
                   {"operation": "observe", "relation_id": "effect", "action": "Pilot", "context": "A", "before": "unknown", "after": "positive", "source": "log", "content": "x"},
                   {"operation": "relation", "relation": {**definition("new"), "created_at": "supplied"}}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                apply(graph, payload)
        for field, value in (("source", "missing"), ("kind", "confluence"), ("severe", 1), ("actions", ["Pilot", "Pilot"]),
                             ("actions", "Pilot"), ("id", "unsafe/id")):
            relation = definition("new")
            relation[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                apply(graph, {"operation": "relation", "relation": relation})

    def test_rejects_unbounded_text_and_unknown_relation_ids(self):
        graph = fixture()
        for source, content in ((" ", "x"), ("x" * 257, "x"), ("report", "x" * 2049), ("report", "\x1b[31munsafe")):
            with self.assertRaises(ValueError):
                apply(graph, {"operation": "assess", "relation_id": "effect", "belief": None, "source": source, "content": content})
        with self.assertRaises(ValueError):
            assess(graph, POSITIVE, "missing")
        with self.assertRaises(ValueError):
            resolution_claim(graph, "missing")

    def test_caps_reject_additions_instead_of_discarding_history(self):
        graph = observation(fixture())
        graph["observations"] = [copy.deepcopy(graph["observations"][0]) for _ in range(512)]
        with self.assertRaisesRegex(ValueError, "observation limit"):
            observation(graph)
        self.assertEqual(len(graph["observations"]), 512)
        graph = fixture()
        sample = graph["nodes"]["person"]
        for index in range(98):
            graph["nodes"][f"n{index}"] = {**sample, "id": f"n{index}"}
        with self.assertRaisesRegex(ValueError, "node limit"):
            apply(graph, {"operation": "node", "node": node("overflow")})
        for index in range(255):
            graph["relations"][f"r{index}"] = {**graph["relations"]["effect"], "id": f"r{index}"}
        with self.assertRaisesRegex(ValueError, "relation limit"):
            apply(graph, {"operation": "relation", "relation": definition("overflow")})

    def test_malformed_saved_graph_is_rejected(self):
        for change in (lambda graph: graph.update(schema="v2"),
                       lambda graph: graph.update(observations={}),
                       lambda graph: graph["relations"]["effect"].update(weight=True),
                       lambda graph: graph["relations"]["effect"].update(target="missing"),
                       lambda graph: graph["relations"]["effect"].update(belief=POSITIVE),
                       lambda graph: graph.update(extra=True)):
            graph = fixture()
            change(graph)
            with self.assertRaises(ValueError):
                summarize(graph)

    def test_saved_graph_cannot_clear_conflicts_by_forging_status_or_deleting_cases(self):
        original = assess(fixture(severe=True), NEGATIVE)
        for change in (lambda graph: graph.update(conflicts={}),
                       lambda graph: graph["conflicts"]["effect"].update(status="resolved"),
                       lambda graph: graph["conflicts"]["effect"]["history"][0].update(event="resolved")):
            graph = copy.deepcopy(original)
            change(graph)
            with self.assertRaises(ValueError):
                blocking_conflicts(graph)
        observed = observation(fixture(), after="negative")
        observed["conflicts"] = {}
        with self.assertRaises(ValueError):
            summarize(observed)

    def test_conflict_history_cap_never_discards_a_prior_resolution(self):
        graph = assess(fixture(severe=True), NEGATIVE)
        graph = assess(graph, POSITIVE)
        graph, _ = apply(graph, {"operation": "resolve", "relation_id": "effect", "verdict": resolution(graph)})
        conflict = graph["conflicts"]["effect"]
        opened, resolved = copy.deepcopy(conflict["history"])
        conflict["history"] = [opened, resolved]
        for _ in range(63):
            conflict["history"].append({**opened, "event": "reopened"})
            conflict["history"].append(copy.deepcopy(resolved))
        self.assertEqual(len(conflict["history"]), 128)
        with self.assertRaisesRegex(ValueError, "conflict history limit"):
            assess(graph, NEGATIVE)
        self.assertEqual(len(graph["conflicts"]["effect"]["history"]), 128)
        self.assertEqual(graph["conflicts"]["effect"]["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
