"""Relational beliefs remain evidence, while utilities remain explicit assumptions."""

import copy
import math
import unittest

from dao.decision import evaluate
from dao.relational import compile_relationship_problem


def relationship():
    return {
        "id": "reminders",
        "subject": "user",
        "object": "reminders",
        "dimension": "interruption",
        "belief": {"enhancing": 0.6, "neutral": 0.2, "degrading": 0.2},
        "evidence_ids": ["feedback-001"],
        "updated_at": "2026-09-30T12:00:00Z",
    }


def actions():
    return [
        {
            "id": "try_reminders",
            "utilities": {"enhancing": 8, "neutral": 0, "degrading": -12},
            "cost": 1,
            "reversibility": 0.9,
            "rollback_cost": 2,
        }
    ]


def signals():
    return [
        {
            "id": "positive",
            "likelihoods": {"enhancing": 0.8, "neutral": 0.5, "degrading": 0.2},
        },
        {
            "id": "negative",
            "likelihoods": {"enhancing": 0.2, "neutral": 0.5, "degrading": 0.8},
        },
    ]


class RelationalCompilerTests(unittest.TestCase):
    def test_compiles_to_replayable_one_step_problem_without_mutation(self):
        rel, possible_actions, observations = relationship(), actions(), signals()
        original = copy.deepcopy((rel, possible_actions, observations))
        problem = compile_relationship_problem(
            rel,
            possible_actions,
            observations,
            wait_cost=1,
            policy={"downside_weight": 0.1},
            utility_unit="user-approved utility points",
        )
        self.assertEqual((rel, possible_actions, observations), original)
        self.assertEqual(
            {item["id"]: item["probability"] for item in problem["scenarios"]}, rel["belief"]
        )
        self.assertEqual(
            problem["actions"][0]["utilities"], possible_actions[0]["utilities"]
        )
        self.assertEqual(problem["policy"]["downside_weight"], 0.1)
        self.assertEqual(problem["utility_unit"], "user-approved utility points")
        self.assertEqual(evaluate(problem)["inputs"], problem)

    def test_evidence_can_have_option_value_without_mislabeling_neutrality(self):
        problem = compile_relationship_problem(relationship(), actions(), signals(), wait_cost=1)
        result = evaluate(problem)
        self.assertEqual(result["best_now"]["action_id"], "try_reminders")
        self.assertEqual(result["recommendation"]["kind"], "wait")
        self.assertGreater(result["expected_value_of_sample_information"], 1)
        positive = next(
            item for item in result["signal_evaluations"] if item["signal_id"] == "positive"
        )
        self.assertAlmostEqual(positive["posterior"]["neutral"], 0.1 / 0.62)

    def test_absent_evidence_model_does_not_invent_value_of_waiting(self):
        problem = compile_relationship_problem(relationship(), actions())
        result = evaluate(problem)
        self.assertEqual(problem["signals"], [])
        self.assertFalse(result["wait_evaluation"]["available"])
        self.assertEqual(result["expected_value_of_sample_information"], 0)

    def test_relationship_identity_and_evidence_are_required(self):
        invalid = []
        for field in ("id", "subject", "object", "dimension", "updated_at"):
            candidate = relationship()
            candidate[field] = ""
            invalid.append(candidate)
        for evidence in (None, "feedback-001", [""], ["duplicate", "duplicate"]):
            candidate = relationship()
            candidate["evidence_ids"] = evidence
            invalid.append(candidate)
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    compile_relationship_problem(candidate, actions())

    def test_invalid_beliefs_and_assumptions_are_rejected(self):
        invalid_beliefs = [
            {"enhancing": 1, "neutral": 0},
            {"enhancing": 0.4, "neutral": 0.4, "degrading": 0.4},
            {"enhancing": True, "neutral": 0, "degrading": 0},
            {"enhancing": math.nan, "neutral": 0, "degrading": 0},
            {"enhancing": -0.1, "neutral": 0.8, "degrading": 0.3},
        ]
        for belief in invalid_beliefs:
            candidate = relationship()
            candidate["belief"] = belief
            with self.subTest(belief=belief):
                with self.assertRaises(ValueError):
                    compile_relationship_problem(candidate, actions())
        bad_actions = actions()
        bad_actions[0]["utilities"] = {"enhancing": 8, "neutral": 0}
        with self.assertRaises(ValueError):
            compile_relationship_problem(relationship(), bad_actions)
        with self.assertRaises(ValueError):
            compile_relationship_problem(relationship(), actions(), signals(), wait_cost=-1)
        with self.assertRaises(ValueError):
            compile_relationship_problem(relationship(), actions(), signals()[:1])


if __name__ == "__main__":
    unittest.main()
