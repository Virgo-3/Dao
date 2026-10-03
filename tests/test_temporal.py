"""Small, hand-checkable temporal choices under partial observation."""

import copy
from fractions import Fraction
import math
import unittest

from dao.temporal import MODEL_VERSION, plan_temporal


def identity(states):
    return {
        state: {next_state: int(state == next_state) for next_state in states}
        for state in states
    }


def unobserved(states):
    return {state: {"none": 1} for state in states}


def action(action_id, utilities, *, kind="act", cost=0, resource_cost=0,
           transitions=None, observations=None):
    states = sorted(utilities)
    return {
        "id": action_id,
        "kind": kind,
        "utilities": utilities,
        "cost": cost,
        "resource_cost": resource_cost,
        "transitions": identity(states) if transitions is None else transitions,
        "observations": unobserved(states) if observations is None else observations,
    }


def simple_problem():
    return {
        "states": ["good", "bad"],
        "prior": {"good": 0.5, "bad": 0.5},
        "actions": [action("act", {"good": 1, "bad": 1})],
        "horizon": 1,
        "budget": 0,
    }


class TemporalPlannerTests(unittest.TestCase):
    def test_bayesian_update_follows_transition_then_observation(self):
        model = simple_problem()
        model["actions"] = [
            action(
                "probe",
                {"good": 1, "bad": 1},
                kind="probe",
                transitions={
                    "good": {"good": 0.9, "bad": 0.1},
                    "bad": {"good": 0.4, "bad": 0.6},
                },
                observations={
                    "good": {"green": 0.8, "red": 0.2},
                    "bad": {"green": 0.2, "red": 0.8},
                },
            )
        ]
        original = copy.deepcopy(model)
        result = plan_temporal(model)
        self.assertEqual(model, original)
        self.assertEqual(result["model_version"], MODEL_VERSION)
        self.assertEqual(result["first_action"], "probe")
        green = next(item for item in result["policy"]["observations"] if item["id"] == "green")
        self.assertAlmostEqual(green["probability"], 0.59)
        self.assertAlmostEqual(green["posterior"]["good"], 0.52 / 0.59)

    def test_informative_probe_beats_act_and_hold_without_double_counting(self):
        model = simple_problem()
        model["horizon"] = 2
        model["budget"] = 2
        model["actions"] = [
            action("act", {"good": 10, "bad": -10}, resource_cost=1),
            action("hold", {"good": 0, "bad": 0}, kind="hold", cost=0.2),
            action(
                "probe", {"good": 0, "bad": 0}, kind="probe", cost=1, resource_cost=1,
                observations={
                    "good": {"yes": 0.9, "no": 0.1},
                    "bad": {"yes": 0.1, "no": 0.9},
                },
            ),
        ]
        result = plan_temporal(model)
        self.assertEqual(result["first_action"], "probe")
        self.assertAlmostEqual(result["expected_value"], 3)
        branches = {item["id"]: item for item in result["policy"]["observations"]}
        self.assertEqual(branches["yes"]["continuation"]["action_id"], "act")
        self.assertIsNone(branches["no"]["continuation"]["action_id"])
        self.assertAlmostEqual(branches["yes"]["posterior"]["good"], 0.9)

    def test_hold_can_cost_resources_and_change_the_world(self):
        model = simple_problem()
        model["prior"] = {"good": 0, "bad": 1}
        model["horizon"] = 2
        model["budget"] = 1
        model["actions"] = [
            action("act", {"good": 10, "bad": -10}, resource_cost=1),
            action(
                "hold", {"good": 0, "bad": -1}, kind="hold", cost=0.5,
                transitions={
                    "good": {"good": 1, "bad": 0},
                    "bad": {"good": 1, "bad": 0},
                },
            ),
        ]
        result = plan_temporal(model)
        self.assertEqual(result["first_action"], "hold")
        self.assertAlmostEqual(result["expected_value"], 8.5)
        self.assertEqual(
            result["policy"]["observations"][0]["continuation"]["action_id"], "act"
        )

    def test_resource_budget_and_hard_loss_limit_gate_actions(self):
        model = simple_problem()
        model["actions"] = [
            action("expensive", {"good": 20, "bad": 20}, resource_cost=2),
            action("hold", {"good": 1, "bad": 1}, kind="hold", resource_cost=0),
        ]
        self.assertEqual(plan_temporal(model)["first_action"], "hold")
        model["actions"] = [
            action("risky", {"good": 10, "bad": -10}),
            action("safe", {"good": 2, "bad": 2}),
        ]
        model["max_loss"] = 5
        self.assertEqual(plan_temporal(model)["first_action"], "safe")

    def test_loss_limit_applies_across_steps_conservatively(self):
        model = simple_problem()
        model["prior"] = {"good": 0.9, "bad": 0.1}
        model["horizon"] = 2
        model["actions"] = [action("risky", {"good": 4, "bad": -3})]
        self.assertAlmostEqual(plan_temporal(model)["expected_value"], 6.6)
        model["max_loss"] = 5
        bounded = plan_temporal(model)
        self.assertAlmostEqual(bounded["expected_value"], 3.3)
        self.assertIsNone(bounded["policy"]["observations"][0]["continuation"]["action_id"])

    def test_tie_break_prefers_hold_among_positive_equal_choices(self):
        model = simple_problem()
        model["actions"] = [
            action("a", {"good": 1, "bad": 1}),
            action("z", {"good": 1, "bad": 1}, kind="hold"),
        ]
        self.assertEqual(plan_temporal(model)["first_action"], "z")

    def test_near_tie_chain_is_ranked_against_fixed_maximum(self):
        model = {
            "states": ["s"], "prior": {"s": 1}, "horizon": 1, "budget": 7,
            "actions": [
                action(f"a{index}", {"s": 1 + (7 - index) * 9e-13},
                       resource_cost=7 - index)
                for index in range(8)
            ],
        }
        result = plan_temporal(model)
        maximum = max(item["utilities"]["s"] for item in model["actions"])
        self.assertEqual(result["first_action"], "a1")
        self.assertLessEqual(maximum - result["expected_value"], 1e-12)

    def test_stop_guard_applies_independently_of_positive_near_ties(self):
        model = {
            "states": ["s"], "prior": {"s": 1}, "horizon": 1, "budget": 1,
            "actions": [
                action("positive", {"s": 1.1e-12}, resource_cost=1),
                action("hold", {"s": 2e-13}, kind="hold"),
            ],
        }
        self.assertEqual(plan_temporal(model)["first_action"], "positive")
        model["actions"] = [action("tiny", {"s": 5e-13})]
        self.assertIsNone(plan_temporal(model)["first_action"])

    def test_positive_support_survives_transition_product_underflow(self):
        states = ["start", "rare_start", "good", "bad"]
        transitions = {state: {next_state: 0 for next_state in states} for state in states}
        transitions["start"]["good"] = 1
        transitions["rare_start"].update(good=1, bad=1e-200)
        transitions["good"]["good"] = 1
        transitions["bad"]["bad"] = 1
        model = {
            "states": states,
            "prior": {"start": 1, "rare_start": 1e-200, "good": 0, "bad": 0},
            "horizon": 2, "budget": 0, "max_loss": 0,
            "actions": [
                action("prepare", {"start": 1, "rare_start": 1, "good": 0, "bad": 0},
                       transitions=transitions),
                action("risky", {"start": -100, "rare_start": -100,
                                 "good": 10, "bad": -1000000}),
            ],
        }
        result = plan_temporal(model)
        branch = result["policy"]["observations"][0]
        self.assertEqual(result["first_action"], "prepare")
        self.assertEqual(branch["posterior"]["bad"], 0)
        self.assertIn("bad", branch["posterior_support"])
        self.assertIsNone(branch["continuation"]["action_id"])

    def test_exact_normalized_support_survives_four_step_planning(self):
        states = [f"s{index}" for index in range(6)]
        outcomes = [f"o{index}" for index in range(6)]
        q = 2.0 ** -53
        weights = [q ** index - q ** (index + 1) for index in range(5)] + [q ** 5]
        self.assertEqual(sum(map(Fraction, weights), Fraction(0)), 1)
        prior = dict(zip(states, weights))
        observations = {state: dict(zip(outcomes, weights)) for state in states}
        transitions = {state: {next_state: int(next_state == "s0") for next_state in states}
                       for state in states}
        transitions["s5"] = prior.copy()
        model = {
            "states": states, "prior": prior, "horizon": 4, "budget": 0, "max_loss": 0,
            "actions": [
                action("prepare", {state: 1 for state in states},
                       transitions=transitions, observations=observations),
                action("risky", {state: (-1000000 if state == "s5" else 10)
                                 for state in states}),
            ],
        }
        result = plan_temporal(model)
        self.assertAlmostEqual(result["expected_value"], 4)

        def check(node):
            if node["action_id"] is None:
                return
            self.assertEqual(node["action_id"], "prepare")
            for branch in node["observations"]:
                self.assertTrue(branch["possible"])
                self.assertIn("s5", branch["posterior_support"])
                check(branch["continuation"])

        check(result["policy"])

    def test_positive_observation_branch_is_not_dropped_when_display_rounds_to_zero(self):
        model = simple_problem()
        model["prior"] = {"good": 1, "bad": 1e-200}
        model["actions"] = [action("probe", {"good": 1, "bad": 1}, observations={
            "good": {"rare": 0, "normal": 1},
            "bad": {"rare": 1e-200, "normal": 1},
        })]
        result = plan_temporal(model)
        rare = next(branch for branch in result["policy"]["observations"] if branch["id"] == "rare")
        self.assertEqual(rare["probability"], 0)
        self.assertTrue(rare["possible"])
        self.assertEqual(rare["posterior"], {"bad": 1.0, "good": 0.0})
        self.assertEqual(rare["posterior_support"], ["bad"])

    def test_small_resource_debit_cannot_round_remaining_budget_up(self):
        model = {
            "states": ["start", "good"], "prior": {"start": 1, "good": 0},
            "horizon": 2, "budget": 1,
            "actions": [
                action("prepare", {"start": 1, "good": 0}, resource_cost=1e-17,
                       transitions={"start": {"start": 0, "good": 1},
                                    "good": {"start": 0, "good": 1}}),
                action("buy", {"start": -100, "good": 10}, resource_cost=1),
            ],
        }
        result = plan_temporal(model)
        self.assertEqual(result["expected_value"], 1)
        self.assertIsNone(result["policy"]["observations"][0]["continuation"]["action_id"])

    def test_small_loss_debit_cannot_round_remaining_limit_up(self):
        states = ["start_good", "start_bad", "good", "bad"]
        model = {
            "states": states,
            "prior": {"start_good": 0.5, "start_bad": 0.5, "good": 0, "bad": 0},
            "horizon": 2, "budget": 0, "max_loss": 1,
            "actions": [
                action("prepare", {"start_good": 1, "start_bad": -1e-17,
                                   "good": 0, "bad": 0},
                       transitions={state: {"start_good": 0, "start_bad": 0,
                                            "good": 0.75, "bad": 0.25} for state in states}),
                action("risky", {"start_good": -100, "start_bad": -100,
                                 "good": 10, "bad": -1}),
            ],
        }
        result = plan_temporal(model)
        self.assertEqual(result["first_action"], "prepare")
        self.assertAlmostEqual(result["expected_value"], 0.5)
        self.assertIsNone(result["policy"]["observations"][0]["continuation"]["action_id"])

    def test_loss_reserve_includes_cost_that_rounds_away_in_net_utility(self):
        model = simple_problem()
        model["prior"] = {"good": 0.9, "bad": 0.1}
        model["max_loss"] = 1
        model["actions"] = [action("risky", {"good": 10, "bad": -1}, cost=1e-17)]
        self.assertIsNone(plan_temporal(model)["first_action"])

    def test_utility_arithmetic_overflow_is_rejected(self):
        model = simple_problem()
        model["actions"] = [action("overflow", {"good": -1e308, "bad": -1e308}, cost=1e308)]
        with self.assertRaisesRegex(ValueError, "finite numeric range"):
            plan_temporal(model)

    def test_repeated_exact_subproblems_are_cached_with_independent_output_branches(self):
        model = simple_problem()
        model["horizon"] = 4
        model["actions"] = [action("reward", {"good": 1, "bad": 1}, observations={
            "good": {"left": 0.5, "right": 0.5},
            "bad": {"left": 0.5, "right": 0.5},
        })]
        result = plan_temporal(model)
        self.assertEqual(result["expected_value"], 4)
        self.assertEqual(result["expansions"], 4)
        self.assertEqual(result["cache_hits"], 3)
        left, right = result["policy"]["observations"]
        self.assertIsNot(left["continuation"], right["continuation"])
        right_before = copy.deepcopy(right)
        left["continuation"]["action_id"] = "changed"
        left["continuation"]["observations"][0]["posterior"]["good"] = -1
        self.assertEqual(right, right_before)

    def test_invalid_probability_models_and_oversized_trees_are_rejected(self):
        invalid = []
        for probability in (-0.1, math.nan, math.inf, True):
            model = simple_problem()
            model["prior"]["good"] = probability
            invalid.append(model)
        for mutate in (
            lambda p: p["actions"][0]["transitions"]["good"].update(good=0.8),
            lambda p: p["actions"][0]["observations"]["bad"].update(none=0.8),
            lambda p: p["actions"][0].update(cost=-1),
            lambda p: p["actions"][0].update(resource_cost=-1),
            lambda p: p.update(horizon=5),
            lambda p: p.update(budget=-1),
            lambda p: p.update(max_loss=-1),
            lambda p: p.update(states=["good", "good"]),
        ):
            model = simple_problem()
            mutate(model)
            invalid.append(model)
        oversized = simple_problem()
        oversized["horizon"] = 4
        oversized["actions"] = [
            action(
                f"action_{index}", {"good": 1, "bad": 1},
                observations={state: {f"o{outcome}": 1 / 8 for outcome in range(8)}
                              for state in ("good", "bad")},
            )
            for index in range(8)
        ]
        invalid.append(oversized)
        for model in invalid:
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    plan_temporal(model)


if __name__ == "__main__":
    unittest.main()
