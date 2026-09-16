"""Examples, malformed-input cases, and invariants for the decision model."""

import copy
import itertools
import json
import math
import random
import unittest

from dao.decision import MAX_ITEMS, evaluate, validate_decision


def launch_problem():
    return {
        "title": "Launch decision",
        "states": [{"id": "good", "probability": 0.5}, {"id": "bad", "probability": 0.5}],
        "actions": [
            {"id": "launch", "label": "Launch", "payoffs": {"good": 100, "bad": -60}, "reversibility": 0.1},
            {"id": "pilot", "label": "Pilot", "payoffs": {"good": 20, "bad": 10}, "reversibility": 0.8},
            {"id": "hold", "label": "Hold", "payoffs": {"good": 0, "bad": 0}, "reversibility": 1},
        ],
        "wait": {
            "cost": 5,
            "signals": [
                {"id": "positive", "likelihoods": {"good": 0.8, "bad": 0.2}},
                {"id": "negative", "likelihoods": {"good": 0.2, "bad": 0.8}},
            ],
        },
    }


class DecisionExamples(unittest.TestCase):
    def test_noisy_observation_fixture(self):
        result = evaluate(launch_problem())
        self.assertEqual(result["best_action_id"], "launch")
        self.assertAlmostEqual(result["best_action_score"], 20)
        self.assertAlmostEqual(result["wait"]["expected_future_score"], 40)
        self.assertAlmostEqual(result["wait"]["evsi"], 20)
        self.assertAlmostEqual(result["wait"]["score"], 35)
        self.assertAlmostEqual(result["wait"]["net_option_value"], 15)
        self.assertAlmostEqual(result["wait"]["max_affordable_cost"], 20)
        self.assertEqual(result["recommendation"]["kind"], "wait")
        self.assertIsNone(result["recommendation"]["action_id"])
        positive, negative = result["wait"]["policy"]
        self.assertEqual(positive["action_id"], "launch")
        self.assertEqual(negative["action_id"], "pilot")
        self.assertAlmostEqual(positive["probability"], 0.5)
        self.assertEqual(positive["posterior"], {"good": 0.8, "bad": 0.2})
        self.assertAlmostEqual(positive["action_score"], 68)
        self.assertAlmostEqual(negative["action_score"], 12)

    def test_score_breakdown_and_reversibility(self):
        problem = launch_problem()
        problem["risk_aversion"] = 0.5
        problem["irreversibility_aversion"] = 2
        problem["actions"][0].update(reversibility=0.25, rollback_cost=8, commitment_cost=4)
        result = evaluate(problem)
        action = result["actions"][0]
        self.assertEqual(action["expected_utility"], 20)
        self.assertEqual(action["expected_downside"], 30)
        self.assertEqual(action["downside_penalty"], 15)
        self.assertEqual(action["irreversibility_penalty"], 6)
        self.assertEqual(action["rollback_penalty"], 2)
        self.assertEqual(action["score"], -3)
        self.assertEqual(result["best_action_id"], "pilot")

    def test_expensive_wait_recommends_current_action(self):
        problem = launch_problem()
        problem["wait"]["cost"] = 25
        result = evaluate(problem)
        self.assertEqual(result["wait"]["evsi"], 20)
        self.assertEqual(result["wait"]["net_option_value"], -5)
        self.assertEqual(result["recommendation"]["action_id"], "launch")

    def test_discount_applies_to_future_score_once(self):
        problem = launch_problem()
        problem["wait"]["discount"] = 0.5
        result = evaluate(problem)
        self.assertEqual(result["wait"]["expected_future_score"], 40)
        self.assertEqual(result["wait"]["evsi"], 20)
        self.assertEqual(result["wait"]["score"], 15)
        self.assertEqual(result["wait"]["net_option_value"], -5)
        self.assertEqual(result["recommendation"]["kind"], "act")

    def test_no_wait_model_means_no_wait_score(self):
        problem = launch_problem()
        del problem["wait"]
        result = evaluate(problem)
        self.assertIsNone(result["wait"])
        self.assertEqual(result["recommendation"]["kind"], "act")

    def test_uninformative_observation_has_no_information_value(self):
        problem = launch_problem()
        problem["wait"]["signals"] = [
            {"id": "heads", "likelihoods": {"good": 0.3, "bad": 0.3}},
            {"id": "tails", "likelihoods": {"good": 0.7, "bad": 0.7}},
        ]
        result = evaluate(problem)
        self.assertEqual(result["wait"]["evsi"], 0)
        self.assertAlmostEqual(result["wait"]["score"], 15)
        self.assertEqual(result["recommendation"]["kind"], "act")

    def test_perfect_information(self):
        problem = launch_problem()
        problem["wait"]["signals"] = [
            {"id": "good", "likelihoods": {"good": 1, "bad": 0}},
            {"id": "bad", "likelihoods": {"good": 0, "bad": 1}},
        ]
        result = evaluate(problem)
        self.assertEqual(result["wait"]["expected_future_score"], 55)
        self.assertEqual(result["wait"]["evsi"], 35)

    def test_zero_mass_signal_is_skipped(self):
        problem = launch_problem()
        problem["states"][0]["probability"] = 1
        problem["states"][1]["probability"] = 0
        problem["wait"]["signals"] = [
            {"id": "possible", "likelihoods": {"good": 1, "bad": 0}},
            {"id": "impossible", "likelihoods": {"good": 0, "bad": 1}},
        ]
        result = evaluate(problem)
        self.assertEqual(len(result["wait"]["policy"]), 1)
        self.assertEqual(result["wait"]["policy"][0]["signal_id"], "possible")
        self.assertEqual(result["wait"]["evsi"], 0)

    def test_wait_tie_preserves_choice(self):
        problem = launch_problem()
        problem["wait"]["cost"] = 20
        result = evaluate(problem)
        self.assertEqual(result["wait"]["net_option_value"], 0)
        self.assertEqual(result["recommendation"]["kind"], "wait")

    def test_action_ties_favor_reversibility_then_id_regardless_of_order(self):
        problem = launch_problem()
        del problem["wait"]
        for action in problem["actions"]:
            action["payoffs"] = {"good": 10, "bad": 10}
        for ordering in itertools.permutations(problem["actions"]):
            problem["actions"] = list(ordering)
            self.assertEqual(evaluate(problem)["best_action_id"], "hold")
        for action in problem["actions"]:
            action["reversibility"] = 1
        self.assertEqual(evaluate(problem)["best_action_id"], "hold")

    def test_negative_actions_do_not_invent_free_abstention(self):
        problem = launch_problem()
        problem["actions"] = [problem["actions"][0]]
        problem["actions"][0]["payoffs"] = {"good": -10, "bad": -20}
        result = evaluate(problem)
        self.assertEqual(result["best_action_score"], -15)
        self.assertAlmostEqual(result["wait"]["score"], -20)
        self.assertEqual(result["recommendation"]["action_id"], "launch")

    def test_validation_copies_and_adds_defaults(self):
        problem = launch_problem()
        original = copy.deepcopy(problem)
        normalized = validate_decision(problem)
        self.assertEqual(problem, original)
        self.assertEqual(normalized["risk_aversion"], 0)
        self.assertEqual(normalized["actions"][0]["rollback_cost"], 0)
        self.assertEqual(normalized["wait"]["discount"], 1)
        normalized["actions"][0]["payoffs"]["good"] = 999
        self.assertEqual(problem, original)
        evaluate(problem)
        self.assertEqual(problem, original)

    def test_tiny_probability_sum_drift_is_normalized(self):
        problem = launch_problem()
        problem["states"][0]["probability"] += 1e-10
        problem["wait"]["signals"][0]["likelihoods"]["good"] += 1e-10
        normalized = validate_decision(problem)
        self.assertAlmostEqual(math.fsum(item["probability"] for item in normalized["states"]), 1)
        self.assertAlmostEqual(math.fsum(item["likelihoods"]["good"] for item in normalized["wait"]["signals"]), 1)

    def test_extreme_accepted_inputs_produce_finite_json(self):
        problem = launch_problem()
        problem["risk_aversion"] = 1e9
        problem["irreversibility_aversion"] = 1e9
        for action in problem["actions"]:
            action.update(commitment_cost=1e9, rollback_cost=1e9)
            action["payoffs"] = {"good": 1e9, "bad": -1e9}
        serialized = json.dumps(evaluate(problem), allow_nan=False)
        self.assertIn('"recommendation"', serialized)


class DecisionValidation(unittest.TestCase):
    def test_invalid_top_level_and_collections(self):
        for value in (None, [], "text", 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                evaluate(value)
        for key in ("states", "actions"):
            for value in ([], {}, None, [None], [launch_problem()[key][0]] * (MAX_ITEMS + 1)):
                problem = launch_problem()
                problem[key] = value
                with self.subTest(key=key, value_type=type(value)), self.assertRaises(ValueError):
                    evaluate(problem)

    def test_required_and_unknown_fields(self):
        paths = ((), ("states", 0), ("actions", 0), ("wait",), ("wait", "signals", 0))
        for path in paths:
            problem = launch_problem()
            target = problem
            for key in path:
                target = target[key]
            target["typo"] = 1
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "unknown fields"):
                evaluate(problem)
        for key in ("title", "states", "actions"):
            problem = launch_problem()
            del problem[key]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "missing required"):
                evaluate(problem)
        for path in (("states", 0), ("actions", 0), ("wait", "signals", 0)):
            problem = launch_problem()
            target = problem
            for key in path:
                target = target[key]
            del target["id"]
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "missing required"):
                evaluate(problem)

    def test_duplicate_ids(self):
        for path in (("states",), ("actions",), ("wait", "signals")):
            problem = launch_problem()
            target = problem
            for key in path:
                target = target[key]
            target[1]["id"] = target[0]["id"]
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "duplicate"):
                evaluate(problem)

    def test_invalid_text(self):
        for bad in ("", "  ", 12, True, "x" * 201):
            problem = launch_problem()
            problem["title"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                evaluate(problem)
        for bad in ("", " leading", "trailing ", "has\nnewline", "x" * 65, None):
            problem = launch_problem()
            problem["actions"][0]["id"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                evaluate(problem)

    def test_every_numeric_field_rejects_bool_and_nonfinite(self):
        paths = [
            ("risk_aversion",), ("irreversibility_aversion",),
            ("states", 0, "probability"), ("actions", 0, "reversibility"),
            ("actions", 0, "payoffs", "good"), ("actions", 0, "rollback_cost"),
            ("actions", 0, "commitment_cost"), ("wait", "cost"), ("wait", "discount"),
            ("wait", "signals", 0, "likelihoods", "good"),
        ]
        for path, bad in itertools.product(paths, (True, False, float("nan"), float("inf"), -float("inf"), "1", None, 10 ** 1000)):
            problem = launch_problem()
            target = problem
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = bad
            with self.subTest(path=path, bad_type=type(bad)), self.assertRaises(ValueError):
                evaluate(problem)

    def test_numeric_bounds(self):
        edits = [
            (("risk_aversion",), -1), (("irreversibility_aversion",), -1),
            (("actions", 0, "reversibility"), -0.1), (("actions", 0, "reversibility"), 1.1),
            (("actions", 0, "commitment_cost"), -1), (("actions", 0, "rollback_cost"), -1),
            (("actions", 0, "payoffs", "good"), 1e10), (("wait", "cost"), -1),
            (("wait", "discount"), 1.1), (("wait", "discount"), -0.1),
            (("states", 0, "probability"), -0.1),
            (("wait", "signals", 0, "likelihoods", "good"), 1.1),
        ]
        for path, value in edits:
            problem = launch_problem()
            target = problem
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                evaluate(problem)

    def test_probability_mass_required_per_state_not_per_signal(self):
        problem = launch_problem()
        problem["states"][0]["probability"] = 0.2
        with self.assertRaisesRegex(ValueError, "sum to one"):
            evaluate(problem)
        problem = launch_problem()
        problem["wait"]["signals"] = [
            {"id": "first", "likelihoods": {"good": 0.8, "bad": 0.2}},
            {"id": "second", "likelihoods": {"good": 0.8, "bad": 0.2}},
        ]
        with self.assertRaisesRegex(ValueError, "sum to one"):
            evaluate(problem)

    def test_exact_payoff_and_likelihood_keys(self):
        for path in (("actions", 0, "payoffs"), ("wait", "signals", 0, "likelihoods")):
            for mode in ("missing", "extra", "nonstring"):
                problem = launch_problem()
                target = problem
                for key in path:
                    target = target[key]
                if mode == "missing":
                    del target["bad"]
                elif mode == "extra":
                    target["invented"] = 0
                else:
                    target[3] = 0
                with self.subTest(path=path, mode=mode), self.assertRaises(ValueError):
                    evaluate(problem)

    def test_wait_requires_explicit_nonempty_information_model(self):
        for wait in (None, {}, {"cost": 0}, {"cost": 0, "signals": []}, {"cost": 0, "signals": [None]}):
            problem = launch_problem()
            problem["wait"] = wait
            with self.subTest(wait=wait), self.assertRaises(ValueError):
                evaluate(problem)


class DecisionInvariants(unittest.TestCase):
    def test_information_value_and_bayes_plausibility_on_seeded_models(self):
        rng = random.Random(2026)
        for case in range(80):
            prior = rng.uniform(0.01, 0.99)
            good_signal, bad_signal = rng.random(), rng.random()
            problem = launch_problem()
            problem["states"][0]["probability"] = prior
            problem["states"][1]["probability"] = 1 - prior
            problem["risk_aversion"] = rng.random() * 2
            problem["irreversibility_aversion"] = rng.random() * 2
            for action in problem["actions"]:
                action["payoffs"] = {"good": rng.uniform(-100, 100), "bad": rng.uniform(-100, 100)}
                action.update(commitment_cost=rng.random() * 10, rollback_cost=rng.random() * 3)
            problem["wait"]["signals"] = [
                {"id": "yes", "likelihoods": {"good": good_signal, "bad": bad_signal}},
                {"id": "no", "likelihoods": {"good": 1-good_signal, "bad": 1-bad_signal}},
            ]
            result = evaluate(problem)
            with self.subTest(case=case):
                self.assertGreaterEqual(result["wait"]["evsi"], -1e-9)
                policy = result["wait"]["policy"]
                self.assertAlmostEqual(math.fsum(item["probability"] for item in policy), 1)
                for state in problem["states"]:
                    self.assertAlmostEqual(
                        math.fsum(item["probability"] * item["posterior"][state["id"]] for item in policy),
                        state["probability"],
                    )
                for item in policy:
                    self.assertAlmostEqual(math.fsum(item["posterior"].values()), 1)
                # Observing a noisy signal cannot beat observing the actual state.
                perfect = copy.deepcopy(problem)
                perfect["wait"]["signals"] = [
                    {"id": "good", "likelihoods": {"good": 1, "bad": 0}},
                    {"id": "bad", "likelihoods": {"good": 0, "bad": 1}},
                ]
                self.assertLessEqual(result["wait"]["evsi"], evaluate(perfect)["wait"]["evsi"] + 1e-9)

    def test_cost_threshold_switches_recommendation(self):
        problem = launch_problem()
        threshold = evaluate(problem)["wait"]["max_affordable_cost"]
        problem["wait"]["cost"] = threshold - 0.01
        self.assertEqual(evaluate(problem)["recommendation"]["kind"], "wait")
        problem["wait"]["cost"] = threshold + 0.01
        self.assertEqual(evaluate(problem)["recommendation"]["kind"], "act")


if __name__ == "__main__":
    unittest.main()
