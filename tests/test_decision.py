"""Decision model tests using hand-checkable utilities and Bayesian posteriors."""

import copy
import json
import math
from pathlib import Path
import unittest

from dao.decision import MODEL_VERSION, evaluate


def problem():
    return {
        "scenarios": [{"id": "ready", "probability": 0.6}, {"id": "not_ready", "probability": 0.4}],
        "actions": [
            {"id": "ship", "utilities": {"ready": 100, "not_ready": -120}},
            {"id": "idle", "utilities": {"ready": 0, "not_ready": 0}},
        ],
        "signals": [
            {"id": "positive", "likelihoods": {"ready": 0.8, "not_ready": 0.2}},
            {"id": "negative", "likelihoods": {"ready": 0.2, "not_ready": 0.8}},
        ],
        "wait_cost": 3,
    }


class DecisionTests(unittest.TestCase):
    def test_known_bayesian_option_value(self):
        result = evaluate(problem())
        self.assertEqual(result["model_version"], MODEL_VERSION)
        self.assertEqual(result["best_now"]["action_id"], "ship")
        self.assertAlmostEqual(result["best_now"]["score"], 12)
        signals = {item["signal_id"]: item for item in result["signal_evaluations"]}
        self.assertAlmostEqual(signals["positive"]["probability"], 0.56)
        self.assertAlmostEqual(signals["negative"]["probability"], 0.44)
        self.assertAlmostEqual(signals["positive"]["posterior"]["ready"], 6 / 7)
        self.assertAlmostEqual(signals["negative"]["posterior"]["ready"], 3 / 11)
        self.assertEqual(signals["positive"]["best"]["action_id"], "ship")
        self.assertEqual(signals["negative"]["best"]["kind"], "abstain")
        self.assertAlmostEqual(result["value_after_signals"], 38.4)
        self.assertAlmostEqual(result["expected_value_of_sample_information"], 26.4)
        self.assertAlmostEqual(result["wait_evaluation"]["score"], 35.4)
        self.assertEqual(result["recommendation"]["kind"], "wait")

    def test_example_matches_known_problem(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "waiting.json"
        result = evaluate(json.loads(path.read_text(encoding="utf-8")))
        self.assertAlmostEqual(result["recommendation"]["score"], 35.4)

    def test_uninformative_signal_has_no_wait_bonus(self):
        model = problem()
        model["signals"] = [
            {"id": "heads", "likelihoods": {"ready": 0.5, "not_ready": 0.5}},
            {"id": "tails", "likelihoods": {"ready": 0.5, "not_ready": 0.5}},
        ]
        model["wait_cost"] = 0
        result = evaluate(model)
        self.assertAlmostEqual(result["expected_value_of_sample_information"], 0)
        self.assertEqual(result["recommendation"]["kind"], "act")

    def test_wait_cost_can_outweigh_information(self):
        model = problem()
        model["wait_cost"] = 27
        result = evaluate(model)
        self.assertEqual(result["recommendation"]["action_id"], "ship")
        self.assertFalse(result["wait_evaluation"]["improves_best_now"])

    def test_missing_signals_cannot_recommend_wait(self):
        model = problem()
        del model["signals"]
        model["wait_cost"] = 0
        result = evaluate(model)
        self.assertEqual(result["expected_value_of_sample_information"], 0)
        self.assertFalse(result["wait_evaluation"]["available"])
        self.assertEqual(result["recommendation"]["kind"], "act")

    def test_abstention_is_real_baseline_even_with_negative_minimum(self):
        model = {
            "scenarios": [{"id": "only", "probability": 1}],
            "actions": [{"id": "lose", "utilities": {"only": -1}}],
            "policy": {"min_score": -10},
        }
        self.assertEqual(evaluate(model)["recommendation"]["kind"], "abstain")
        model["actions"][0]["utilities"]["only"] = 0
        self.assertEqual(evaluate(model)["recommendation"]["kind"], "abstain")

    def test_reversibility_penalties_and_tie_preference(self):
        model = {
            "scenarios": [{"id": "only", "probability": 1}],
            "actions": [
                {"id": "a_commit", "utilities": {"only": 10}, "reversibility": 0},
                {"id": "z_reversible", "utilities": {"only": 10}, "reversibility": 1},
            ],
        }
        self.assertEqual(evaluate(model)["recommendation"]["action_id"], "z_reversible")
        model["policy"] = {"irreversibility_weight": 2}
        model["actions"][0]["rollback_cost"] = 3
        model["actions"][0]["cost"] = 1
        result = evaluate(model)
        commit = result["action_evaluations"][0]
        self.assertAlmostEqual(commit["expected_net_utility"], 9)
        self.assertAlmostEqual(commit["irreversibility_penalty"], 2)
        self.assertAlmostEqual(commit["rollback_penalty"], 3)
        self.assertAlmostEqual(commit["score"], 4)

    def test_downside_and_loss_policy(self):
        model = problem()
        model["signals"] = []
        model["policy"] = {"downside_weight": 0.5}
        result = evaluate(model)
        ship = next(item for item in result["action_evaluations"] if item["action_id"] == "ship")
        self.assertAlmostEqual(ship["expected_downside"], 48)
        self.assertAlmostEqual(ship["score"], -12)
        self.assertEqual(result["recommendation"]["kind"], "abstain")
        model["policy"] = {"max_loss": 119}
        result = evaluate(model)
        ship = next(item for item in result["action_evaluations"] if item["action_id"] == "ship")
        self.assertFalse(ship["admissible"])
        self.assertEqual(result["recommendation"]["kind"], "abstain")
        model["policy"] = {"max_loss": 120}
        self.assertEqual(evaluate(model)["recommendation"]["kind"], "act")

    def test_perfect_information_removes_impossible_loss_scenarios(self):
        model = problem()
        model["policy"] = {"max_loss": 0}
        model["signals"] = [
            {"id": "yes", "likelihoods": {"ready": 1, "not_ready": 0}},
            {"id": "no", "likelihoods": {"ready": 0, "not_ready": 1}},
            {"id": "impossible", "likelihoods": {"ready": 0, "not_ready": 0}},
        ]
        result = evaluate(model)
        self.assertEqual(result["best_now"]["kind"], "abstain")
        self.assertEqual(result["recommendation"]["kind"], "wait")
        self.assertAlmostEqual(result["value_after_signals"], 60)
        impossible = next(
            item for item in result["signal_evaluations"] if item["signal_id"] == "impossible"
        )
        self.assertIsNone(impossible["posterior"])
        self.assertIsNone(impossible["best"])

    def test_minimum_score_gates_recommendations_not_information_optimization(self):
        model = {
            "scenarios": [{"id": "low", "probability": 0.5}, {"id": "high", "probability": 0.5}],
            "actions": [{"id": "act", "utilities": {"low": 0.99, "high": 1.01}}],
            "signals": [
                {"id": "low", "likelihoods": {"low": 1, "high": 0}},
                {"id": "high", "likelihoods": {"low": 0, "high": 1}},
            ],
            "policy": {"min_score": 1},
        }
        result = evaluate(model)
        self.assertAlmostEqual(result["expected_value_of_sample_information"], 0)
        self.assertEqual(result["recommendation"]["kind"], "act")
        low = next(item for item in result["signal_evaluations"] if item["signal_id"] == "low")
        self.assertEqual(low["best"]["kind"], "act")
        self.assertEqual(low["recommendation"]["kind"], "abstain")
        model["policy"]["min_score"] = 1.1
        self.assertEqual(evaluate(model)["recommendation"]["kind"], "abstain")

    def test_wait_also_must_meet_threshold(self):
        model = problem()
        model["policy"] = {"min_score": 36}
        result = evaluate(model)
        self.assertTrue(result["wait_evaluation"]["improves_best_now"])
        self.assertFalse(result["wait_evaluation"]["meets_min_score"])
        self.assertEqual(result["recommendation"]["kind"], "abstain")

    def test_wait_uses_the_thresholded_continuation_policy(self):
        model = {
            "scenarios": [{"id": "low", "probability": .5}, {"id": "high", "probability": .5}],
            "actions": [
                {"id": "a", "utilities": {"low": 99, "high": 101}},
                {"id": "b", "utilities": {"low": -100, "high": 105}},
            ],
            "signals": [
                {"id": "low", "likelihoods": {"low": 1, "high": 0}},
                {"id": "high", "likelihoods": {"low": 0, "high": 1}},
            ],
            "policy": {"min_score": 100}, "wait_cost": 1,
        }
        result = evaluate(model)
        self.assertEqual(result["best_now"]["score"], 100)
        self.assertEqual(result["value_after_signals"], 102)
        self.assertEqual(result["expected_value_of_sample_information"], 2)
        self.assertEqual(result["value_after_recommendations"], 52.5)
        self.assertEqual(result["wait_evaluation"]["score"], 51.5)
        self.assertEqual(result["recommendation"]["action_id"], "a")

    def test_near_ties_cannot_cross_the_abstention_baseline(self):
        model = {
            "scenarios": [{"id": "s", "probability": 1}],
            "actions": [{"id": str(i), "utilities": {"s": value}, "reversibility": i / 3}
                        for i, value in enumerate([2e-12, 1.1e-12, 2e-13, -7e-13])],
            "policy": {"min_score": -1},
        }
        result = evaluate(model)
        self.assertEqual(result["recommendation"]["action_id"], "1")
        self.assertGreater(result["recommendation"]["score"], 1e-12)

    def test_tie_preferences_are_anchored_to_the_global_maximum(self):
        model = {
            "scenarios": [{"id": "s", "probability": 1}],
            "actions": [{"id": f"a{i:02}", "utilities": {"s": 1e12 - .9 * i},
                         "reversibility": i / 9} for i in range(10)],
        }
        result = evaluate(model)
        self.assertEqual(result["recommendation"]["action_id"], "a01")
        self.assertTrue(math.isclose(result["best_now"]["score"], 1e12,
                                     rel_tol=1e-12, abs_tol=1e-12))

    def test_subnormal_signal_preserves_a_nonzero_loss_scenario(self):
        model = {
            "scenarios": [{"id": "good", "probability": 1},
                          {"id": "bad", "probability": 1e-300}],
            "actions": [{"id": "risky", "utilities": {"good": 10, "bad": -1}}],
            "signals": [
                {"id": "yes", "likelihoods": {"good": .1, "bad": 1e-100}},
                {"id": "no", "likelihoods": {"good": .9, "bad": 1}},
            ],
            "policy": {"max_loss": 0},
        }
        result = evaluate(model)
        signal = next(s for s in result["signal_evaluations"] if s["signal_id"] == "yes")
        self.assertEqual(signal["posterior"]["bad"], 0)
        self.assertIn("bad", signal["posterior_support"])
        self.assertFalse(signal["action_evaluations"][0]["admissible"])
        self.assertEqual(result["recommendation"]["kind"], "abstain")

    def test_positive_signal_is_not_impossible_when_its_float_probability_is_zero(self):
        model = {
            "scenarios": [{"id": "rare", "probability": 1e-300},
                          {"id": "common", "probability": 1}],
            "actions": [{"id": "choose", "utilities": {"rare": 1e300, "common": -1}}],
            "signals": [
                {"id": "rare", "likelihoods": {"rare": 1e-30, "common": 0}},
                {"id": "other", "likelihoods": {"rare": 1, "common": 1}},
            ],
        }
        result = evaluate(model)
        signal = next(s for s in result["signal_evaluations"] if s["signal_id"] == "rare")
        self.assertEqual(signal["probability"], 0)
        self.assertTrue(signal["possible"])
        self.assertEqual(signal["posterior"], {"common": 0, "rare": 1})
        self.assertGreater(result["value_after_signals"], 0)

    def test_loss_cap_uses_exact_net_loss_before_display_rounding(self):
        model = {
            "scenarios": [{"id": "s", "probability": 1}],
            "actions": [{"id": "lose", "utilities": {"s": -1e-17}, "cost": 1}],
            "policy": {"max_loss": 1},
        }
        self.assertFalse(evaluate(model)["action_evaluations"][0]["admissible"])

    def test_threshold_gate_does_not_hide_an_eligible_near_tie(self):
        model = {
            "scenarios": [{"id": "s", "probability": 1}],
            "actions": [
                {"id": "a", "utilities": {"s": 100 + 4e-11}, "reversibility": 0},
                {"id": "b", "utilities": {"s": 100 - 4e-11}, "reversibility": 1},
            ], "policy": {"min_score": 100},
        }
        result = evaluate(model)
        self.assertEqual(result["best_now"]["action_id"], "b")
        self.assertEqual(result["recommendation"]["action_id"], "a")

    def test_potential_evsi_uses_maxima_before_posterior_tie_preferences(self):
        model = {
            "scenarios": [{"id": "low", "probability": .5}, {"id": "high", "probability": .5}],
            "actions": [
                {"id": "a", "utilities": {"low": .1, "high": 1.9}, "reversibility": 0},
                {"id": "b", "utilities": {"low": .1 - .9e-12, "high": 0}},
                {"id": "c", "utilities": {"low": 0, "high": 1.9 - 1.7e-12}},
            ],
            "signals": [
                {"id": "low", "likelihoods": {"low": 1, "high": 0}},
                {"id": "high", "likelihoods": {"low": 0, "high": 1}},
            ],
        }
        result = evaluate(model)
        self.assertAlmostEqual(result["optimal_score_now"], 1)
        self.assertAlmostEqual(result["value_after_signals"], 1)
        self.assertEqual(result["expected_value_of_sample_information"], 0)
        self.assertLess(result["value_after_recommendations"], result["optimal_score_now"])
        self.assertEqual(result["recommendation"]["action_id"], "a")

    def test_canonical_replay_no_mutation_and_order_independence(self):
        model = problem()
        original = copy.deepcopy(model)
        result = evaluate(model)
        self.assertEqual(model, original)
        self.assertEqual(evaluate(result["inputs"]), result)
        model["scenarios"].reverse()
        model["actions"].reverse()
        model["signals"].reverse()
        self.assertEqual(evaluate(model), result)
        json.dumps(result, allow_nan=False)

    def test_empty_action_set_abstains(self):
        model = {"scenarios": [{"id": "only", "probability": 1}], "actions": []}
        self.assertEqual(evaluate(model)["recommendation"]["kind"], "abstain")

    def test_normalization_is_stable_under_exact_replay(self):
        probabilities = [
            0.33842037968744165,
            0.2724008867810352,
            0.08218781856256115,
            0.27129964099820797,
            0.03569127397075397,
        ]
        model = {
            "scenarios": [
                {"id": str(index), "probability": probability}
                for index, probability in enumerate(probabilities)
            ],
            "actions": [
                {
                    "id": "choose",
                    "utilities": {str(index): index * 10 for index in range(len(probabilities))},
                }
            ],
            "signals": [
                {
                    "id": str(index),
                    "likelihoods": {
                        str(scenario): probability for scenario in range(len(probabilities))
                    },
                }
                for index, probability in enumerate(probabilities)
            ],
        }
        result = evaluate(model)
        self.assertEqual(
            math.fsum(item["probability"] for item in result["inputs"]["scenarios"]), 1
        )
        self.assertEqual(evaluate(result["inputs"]), result)

    def test_invalid_models_are_rejected(self):
        invalid = []
        for value in (float("nan"), float("inf"), -float("inf"), True, "0.6"):
            model = problem()
            model["scenarios"][0]["probability"] = value
            invalid.append(model)
            model = problem()
            model["actions"][0]["utilities"]["ready"] = value
            invalid.append(model)
        for key, value in (
            ("cost", -1),
            ("rollback_cost", -1),
            ("reversibility", -0.1),
            ("reversibility", 1.1),
        ):
            model = problem()
            model["actions"][0][key] = value
            invalid.append(model)
        for policy in (
            {"max_loss": -1},
            {"downside_weight": -1},
            {"irreversibility_weight": -1},
            {"min_score": math.inf},
            {"downside_weigth": 1},
        ):
            model = problem()
            model["policy"] = policy
            invalid.append(model)
        for action_id in ("wait", "abstain", "", "idle"):
            model = problem()
            model["actions"][0]["id"] = action_id
            invalid.append(model)
        for modify in (
            lambda p: p["scenarios"].append(copy.deepcopy(p["scenarios"][0])),
            lambda p: p["scenarios"][0].update(probability=0.5),
            lambda p: p["signals"].append(copy.deepcopy(p["signals"][0])),
            lambda p: p["signals"][0]["likelihoods"].update(ready=0.5),
            lambda p: p["signals"][0]["likelihoods"].update(ready=1.1),
            lambda p: p["actions"][0]["utilities"].pop("ready"),
            lambda p: p["actions"][0]["utilities"].update(extra=0),
            lambda p: p.update(wait_cost=-1),
            lambda p: p.update(signals=None),
            lambda p: p.update(actions=None),
            lambda p: p.update(scenarios=[]),
            lambda p: p.update(wait_bonus=100),
        ):
            model = problem()
            modify(model)
            invalid.append(model)
        invalid.extend([None, [], {"scenarios": [], "actions": []}])
        for model in invalid:
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    evaluate(model)

    def test_finite_inputs_cannot_produce_infinite_output(self):
        model = {
            "scenarios": [{"id": "only", "probability": 1}],
            "actions": [{"id": "overflow", "utilities": {"only": -1e308}, "cost": 1e308}],
        }
        with self.assertRaises(ValueError):
            evaluate(model)
        model["actions"][0]["cost"] = 0
        model["policy"] = {"downside_weight": 1e308}
        with self.assertRaises(ValueError):
            evaluate(model)


if __name__ == "__main__":
    unittest.main()
