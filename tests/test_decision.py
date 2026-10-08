import copy
import math
import unittest
from unittest.mock import patch

from dao.decision import demo_payload, evaluate


def simple_payload():
    return {
        "scenarios": [{"name": "up", "probability": 0.5}, {"name": "down", "probability": 0.5}],
        "actions": [{"name": "invest", "payoffs": [10, -10], "reversibility": 0.5}],
    }


class DecisionTests(unittest.TestCase):
    def test_perfect_signal_has_known_option_value(self):
        payload = simple_payload()
        payload["signals"] = [{"name": "good", "likelihoods": [1, 0]},
                              {"name": "bad", "likelihoods": [0, 1]}]
        result = evaluate(payload)
        self.assertEqual(result["baseline_utility"], 0)
        self.assertEqual(result["expected_value_of_information"], 5)
        self.assertEqual(result["wait_utility"], 5)
        self.assertEqual(result["recommendation"], "wait")
        self.assertIsNone(result["selected_action"])
        self.assertEqual(result["signal_analysis"][0]["selected_action"], "invest")
        self.assertIsNone(result["signal_analysis"][1]["selected_action"])

    def test_bayes_posterior_and_information_value(self):
        payload = simple_payload()
        payload["signals"] = [{"name": "good", "likelihoods": [0.8, 0.2]},
                              {"name": "bad", "likelihoods": [0.2, 0.8]}]
        result = evaluate(payload)
        self.assertAlmostEqual(result["expected_value_of_information"], 3)
        self.assertAlmostEqual(result["signal_analysis"][0]["posterior"][0]["probability"], 0.8)

    def test_uninformative_signal_adds_no_option_value(self):
        payload = simple_payload()
        payload["signals"] = [{"name": "good", "likelihoods": [0.3, 0.3]},
                              {"name": "bad", "likelihoods": [0.7, 0.7]}]
        result = evaluate(payload)
        self.assertEqual(result["expected_value_of_information"], 0)
        self.assertEqual(result["recommendation"], "abstain")

    def test_no_signals_does_not_assume_improvement(self):
        payload = simple_payload()
        payload["actions"][0]["payoffs"] = [10, 2]
        payload["waiting_cost"] = 1
        payload["discount"] = 0.5
        result = evaluate(payload)
        self.assertEqual(result["expected_value_of_information"], 0)
        self.assertEqual(result["expected_utility_after_signal"], 6)
        self.assertEqual(result["wait_utility"], 2)
        self.assertEqual(result["recommendation"], "act")

    def test_cost_rollback_reversibility_and_variance(self):
        payload = simple_payload()
        payload["actions"][0].update(cost=2, rollback_cost=4)
        payload["irreversibility_penalty"] = 6
        payload["risk_aversion"] = 0.01
        score = evaluate(payload)["scores"][0]
        # Net outcomes [5, -19], mean -7, variance 144, risk penalty 1.44.
        self.assertEqual(score["expected_payoff"], 0)
        self.assertEqual(score["expected_rollback_cost"], 2)
        self.assertEqual(score["irreversibility_cost"], 3)
        self.assertEqual(score["expected_net_payoff"], -7)
        self.assertEqual(score["variance"], 144)
        self.assertAlmostEqual(score["utility"], -8.44)

    def test_equal_action_utilities_prefer_reversibility(self):
        payload = simple_payload()
        payload["actions"] = [{"name": "hard", "payoffs": [3, 3], "reversibility": 0},
                              {"name": "easy", "payoffs": [3, 3], "reversibility": 1}]
        self.assertEqual(evaluate(payload)["selected_action"], "easy")

    def test_unknown_reversibility_has_no_credit(self):
        payload = simple_payload()
        del payload["actions"][0]["reversibility"]
        payload["irreversibility_penalty"] = 6
        score = evaluate(payload)["scores"][0]
        self.assertEqual(score["reversibility"], 0)
        self.assertEqual(score["irreversibility_cost"], 6)

    def test_risk_penalty_recomputes_under_each_posterior(self):
        payload = simple_payload()
        payload["risk_aversion"] = 0.01
        payload["signals"] = [{"name": "good", "likelihoods": [0.8, 0.2]},
                              {"name": "bad", "likelihoods": [0.2, 0.8]}]
        result = evaluate(payload)
        # Prior utility is -1 and abstention sets baseline 0. Good posterior
        # has mean 6 and variance 64: utility 5.36, observed with probability .5.
        self.assertEqual(result["baseline_utility"], 0)
        self.assertAlmostEqual(result["expected_value_of_information"], 2.68)

    def test_margin_is_utility_buffer_and_wait_tie_keeps_action(self):
        payload = simple_payload()
        payload["actions"][0]["payoffs"] = [2, 2]
        payload["confidence_margin"] = 2
        self.assertEqual(evaluate(payload)["recommendation"], "abstain")
        payload["confidence_margin"] = 0
        self.assertEqual(evaluate(payload)["recommendation"], "act")

    def test_expensive_wait_loses_to_immediate_action(self):
        payload = demo_payload()
        payload["waiting_cost"] = 1000
        result = evaluate(payload)
        self.assertEqual(result["recommendation"], "act")
        self.assertEqual(result["selected_action"], "Try an alternate scene")

    def test_impossible_signal_is_reported_without_division(self):
        payload = simple_payload()
        payload["signals"] = [{"name": "certain", "likelihoods": [1, 1]},
                              {"name": "impossible", "likelihoods": [0, 0]}]
        result = evaluate(payload)
        self.assertIsNone(result["signal_analysis"][1]["posterior"])
        self.assertEqual(result["signal_analysis"][1]["probability"], 0)

    def test_demo_waits_and_does_not_mutate_input(self):
        payload = demo_payload()
        before = copy.deepcopy(payload)
        result = evaluate(payload)
        self.assertEqual(result["recommendation"], "wait")
        self.assertGreater(result["expected_value_of_information"], 0)
        self.assertEqual(payload, before)

    def test_validation_rejects_nonfinite_bool_and_bad_ranges(self):
        for value in (True, False, math.nan, math.inf, -math.inf, "0.5", 10 ** 1000):
            with self.subTest(value=str(value)[:30]):
                payload = simple_payload()
                payload["scenarios"][0]["probability"] = value
                with self.assertRaises(ValueError):
                    evaluate(payload)
        for field, value in (("discount", 1.1), ("discount", -0.1), ("waiting_cost", -1),
                             ("risk_aversion", -1), ("confidence_margin", -1), ("irreversibility_penalty", -1)):
            with self.subTest(field=field, value=value):
                payload = simple_payload()
                payload[field] = value
                with self.assertRaises(ValueError):
                    evaluate(payload)

    def test_validation_rejects_incomplete_distribution(self):
        payload = simple_payload()
        payload["scenarios"][0]["probability"] = 0.4
        with self.assertRaisesRegex(ValueError, "sum to 1"):
            evaluate(payload)
        payload = simple_payload()
        payload["signals"] = [{"name": "incomplete", "likelihoods": [0.5, 0.5]}]
        with self.assertRaisesRegex(ValueError, "sum to 1"):
            evaluate(payload)

    def test_validation_rejects_shape_duplicates_and_unknowns(self):
        changes = [lambda p: p.update(actions=[]),
                   lambda p: p["actions"][0].update(payoffs=[1]),
                   lambda p: p["actions"][0].update(reversibility=1.1),
                   lambda p: p["actions"].append(copy.deepcopy(p["actions"][0])),
                   lambda p: p["scenarios"][1].update(name="up"),
                   lambda p: p.update(unmodeled_information=True)]
        for change in changes:
            payload = simple_payload()
            change(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                evaluate(payload)

    def test_numerical_overflow_is_rejected(self):
        payload = simple_payload()
        payload["actions"][0]["payoffs"] = [1e308, -1e308]
        with self.assertRaises(ValueError):
            evaluate(payload)

    def test_each_input_dimension_is_bounded(self):
        for field in ("scenarios", "actions", "signals"):
            payload = simple_payload()
            payload[field] = [{}] * 65
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "at most 64 items"):
                evaluate(payload)

    def test_names_are_bounded_before_results_are_amplified(self):
        for field in ("scenarios", "actions", "signals"):
            payload = demo_payload()
            payload[field][0]["name"] = "x" * 201
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "at most 200 characters"):
                evaluate(payload)
        payload = simple_payload()
        payload["actions"][0]["name"] = "x" * 200
        self.assertEqual(evaluate(payload)["scores"][0]["name"], "x" * 200)

    def test_work_budget_rejects_valid_distributions_before_scoring(self):
        payload = {
            "scenarios": [{"name": f"s{i}", "probability": 1 / 64} for i in range(64)],
            "actions": [{"name": f"a{i}", "payoffs": [1] * 64} for i in range(64)],
            "signals": [{"name": f"y{i}", "likelihoods": [1 / 64] * 64} for i in range(64)],
        }
        with patch("dao.decision._score") as score:
            with self.assertRaisesRegex(ValueError, "workload exceeds"):
                evaluate(payload)
            score.assert_not_called()


if __name__ == "__main__":
    unittest.main()
