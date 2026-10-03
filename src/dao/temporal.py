"""Pure, bounded planning with uncertain state and action-dependent observations.

The planner uses a small finite-horizon partially observable model.  It is a
simulation only: choosing an action here does not execute it, acquire evidence,
or charge the usage ledger.  See ``plan_temporal`` for timing assumptions.
"""

from __future__ import annotations

import copy
import math
from fractions import Fraction
from typing import Any

from .numerics import exact_distribution, float_distribution


MODEL_VERSION = "dao-temporal/2"
MAX_STATES = 6
MAX_ACTIONS = 8
MAX_OBSERVATIONS = 8
MAX_HORIZON = 4
MAX_EXPANSIONS = 20_000
_PROBABILITY_TOLERANCE = 1e-9
_SCORE_TOLERANCE = 1e-12


def _object(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - keys
    if unknown:
        raise ValueError(f"{name} has unsupported fields: {sorted(unknown, key=str)}")
    return value


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _finite(value: Any, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return result


def _sum(values: Any) -> float:
    try:
        total = math.fsum(values)
    except OverflowError as exc:
        raise ValueError("temporal arithmetic exceeded finite numeric range") from exc
    if not math.isfinite(total):
        raise ValueError("temporal arithmetic exceeded finite numeric range")
    return total


def _distribution(value: Any, expected: set[str], name: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} must contain exactly {sorted(expected)}")
    result = {key: _finite(value[key], f"{name}.{key}", 0.0) for key in sorted(expected)}
    if any(number > 1.0 for number in result.values()):
        raise ValueError(f"{name} probabilities must be at most 1")
    total = _sum(result.values())
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=_PROBABILITY_TOLERANCE):
        raise ValueError(f"{name} probabilities must sum to 1")
    if total != 1.0:
        result = {key: number / total for key, number in result.items()}
        pivot = max(result, key=result.get)
        result[pivot] += 1.0 - _sum(result.values())
    return result


def _utilities(value: Any, states: set[str], name: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != states:
        raise ValueError(f"{name} must contain exactly the state IDs")
    return {key: _finite(value[key], f"{name}.{key}") for key in sorted(states)}


def _validate(problem: Any) -> dict[str, Any]:
    raw = _object(
        problem,
        "problem",
        {
            "states", "prior", "actions", "horizon", "budget", "max_loss",
            "utility_unit", "resource_unit",
        },
    )
    states = raw.get("states")
    if not isinstance(states, list) or not states or len(states) > MAX_STATES:
        raise ValueError(f"states must be a nonempty list of at most {MAX_STATES}")
    state_ids = [_id(item, f"states[{index}]") for index, item in enumerate(states)]
    if len(set(state_ids)) != len(state_ids):
        raise ValueError("state IDs must be unique")
    state_ids.sort()
    state_set = set(state_ids)
    prior = _distribution(raw.get("prior"), state_set, "prior")

    horizon = raw.get("horizon")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= MAX_HORIZON:
        raise ValueError(f"horizon must be an integer from 1 to {MAX_HORIZON}")
    budget = _finite(raw.get("budget"), "budget", 0.0)
    max_loss = raw.get("max_loss")
    if max_loss is not None:
        max_loss = _finite(max_loss, "max_loss", 0.0)
    utility_unit = _id(raw.get("utility_unit", "modeled utility units"), "utility_unit")
    resource_unit = _id(raw.get("resource_unit", "modeled resource units"), "resource_unit")

    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions or len(raw_actions) > MAX_ACTIONS:
        raise ValueError(f"actions must be a nonempty list of at most {MAX_ACTIONS}")
    actions: list[dict[str, Any]] = []
    seen_actions: set[str] = set()
    max_observations = 0
    for index, item in enumerate(raw_actions):
        name = f"actions[{index}]"
        item = _object(
            item,
            name,
            {"id", "kind", "utilities", "cost", "resource_cost", "transitions", "observations"},
        )
        action_id = _id(item.get("id"), f"{name}.id")
        if action_id in seen_actions:
            raise ValueError("action IDs must be unique")
        seen_actions.add(action_id)
        kind = item.get("kind", action_id if action_id in {"hold", "probe", "act"} else "act")
        if not isinstance(kind, str) or kind not in {"hold", "probe", "act"}:
            raise ValueError(f"{name}.kind must be hold, probe, or act")
        utilities = _utilities(item.get("utilities"), state_set, f"{name}.utilities")
        cost = _finite(item.get("cost"), f"{name}.cost", 0.0)
        resource_cost = _finite(item.get("resource_cost"), f"{name}.resource_cost", 0.0)

        transitions_raw = item.get("transitions")
        if not isinstance(transitions_raw, dict) or set(transitions_raw) != state_set:
            raise ValueError(f"{name}.transitions must contain exactly the state IDs")
        transitions = {
            state: _distribution(
                transitions_raw[state], state_set, f"{name}.transitions.{state}"
            )
            for state in state_ids
        }

        observations_raw = item.get("observations")
        if not isinstance(observations_raw, dict) or set(observations_raw) != state_set:
            raise ValueError(f"{name}.observations must contain exactly the state IDs")
        first_row = observations_raw[state_ids[0]]
        if not isinstance(first_row, dict) or not first_row:
            raise ValueError(f"{name}.observations must have nonempty outcome rows")
        outcome_ids = {_id(outcome, f"{name}.observations outcome") for outcome in first_row}
        if len(outcome_ids) > MAX_OBSERVATIONS:
            raise ValueError(f"{name} has too many observations")
        observations = {
            state: _distribution(
                observations_raw[state], outcome_ids, f"{name}.observations.{state}"
            )
            for state in state_ids
        }
        max_observations = max(max_observations, len(outcome_ids))
        actions.append(
            {
                "id": action_id,
                "kind": kind,
                "utilities": utilities,
                "cost": cost,
                "resource_cost": resource_cost,
                "transitions": transitions,
                "observations": observations,
            }
        )
    actions.sort(key=lambda item: item["id"])
    width = len(actions) * max_observations
    expansion_bound = _sum(width**depth for depth in range(horizon))
    if expansion_bound > MAX_EXPANSIONS:
        raise ValueError("temporal model exceeds expansion limit")
    return {
        "states": state_ids,
        "prior": prior,
        "actions": actions,
        "horizon": horizon,
        "budget": budget,
        "max_loss": max_loss,
        "utility_unit": utility_unit,
        "resource_unit": resource_unit,
    }


def _choice_rank(action: dict[str, Any]) -> tuple[Any, ...]:
    return (
        0 if action["kind"] == "hold" else 1,
        action["resource_cost"],
        action["cost"],
        action["id"],
    )


def plan_temporal(problem: dict[str, Any]) -> dict[str, Any]:
    """Plan up to four decisions over uncertain, changing states.

    Immediate utility depends on the state *before* action.  Then the action's
    transition changes the state, an observation about the new state arrives,
    and the next decision uses the resulting Bayesian posterior.  The reward
    itself is not an additional observation unless modeled in ``observations``.
    Each action's cost is in utility units and deducted once; resource_cost is
    a separate budget debit, not automatically deducted from utility.  The
    optional max_loss conservatively bounds cumulative downside by reserving
    each step's worst possible loss across currently supported states.  The
    agent may stop early with value zero.  All hypothetical outcomes stay local.
    Probabilities and cap debits use exact fractions of the validated numeric
    inputs internally.  Reported probabilities are floats; posterior_support
    preserves reachability when a positive probability rounds to zero.
    """
    inputs = _validate(problem)
    states = inputs["states"]
    expansions = 0
    cache_hits = 0
    cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
    prior = exact_distribution(inputs["prior"])
    exact_actions = {
        action["id"]: {
            "resource_cost": Fraction(action["resource_cost"]),
            "cost": Fraction(action["cost"]),
            "utilities": {state: Fraction(action["utilities"][state]) for state in states},
            "transitions": {
                state: exact_distribution(action["transitions"][state]) for state in states
            },
            "observations": {
                state: exact_distribution(action["observations"][state]) for state in states
            },
        }
        for action in inputs["actions"]
    }

    def solve(
        belief: dict[str, Fraction], steps: int, budget: Fraction,
        loss_budget: Fraction | None,
    ) -> tuple[float, dict[str, Any]]:
        nonlocal expansions, cache_hits
        key = (tuple(belief[state] for state in states), steps, budget, loss_budget)
        cached = cache.get(key)
        if cached is not None:
            cache_hits += 1
            # Separate policy branches remain independent mutable JSON trees.
            # Copy on a hit, before attaching the policy under a new history.
            return cached[0], copy.deepcopy(cached[1])
        expansions += 1
        if expansions > MAX_EXPANSIONS:
            raise ValueError("temporal model exceeds expansion limit")
        stop_policy: dict[str, Any] = {
            "action_id": None,
            "kind": "stop",
            "expected_value": 0.0,
            "observations": [],
        }
        if steps == 0:
            cache[key] = (0.0, stop_policy)
            return 0.0, stop_policy
        candidates: list[tuple[float, tuple[Any, ...], dict[str, Any]]] = []
        support = [state for state in states if belief[state] > 0]
        for action in inputs["actions"]:
            exact_action = exact_actions[action["id"]]
            if exact_action["resource_cost"] > budget:
                continue
            # Keep the existing finite utility-arithmetic contract before
            # weighting, while reserving losses without rounding cap debits.
            net_utilities = {
                state: _sum((action["utilities"][state], -action["cost"]))
                for state in support
            }
            worst_step_loss = max(
                max(Fraction(0), exact_action["cost"] - exact_action["utilities"][state])
                for state in support
            )
            if loss_budget is not None and worst_step_loss > loss_budget:
                continue
            immediate = _sum(
                float(belief[state] * Fraction(net_utilities[state]))
                for state in support
            )
            predicted = {
                next_state: sum(
                    (belief[state] * exact_action["transitions"][state][next_state]
                     for state in support), Fraction(0),
                )
                for next_state in states
            }
            remaining_budget = budget - exact_action["resource_cost"]
            remaining_loss = (
                None if loss_budget is None else loss_budget - worst_step_loss
            )
            branches = []
            future_terms = []
            outcomes = sorted(action["observations"][states[0]])
            for outcome in outcomes:
                joints = {
                    state: predicted[state] * exact_action["observations"][state][outcome]
                    for state in states
                }
                probability = sum(joints.values(), Fraction(0))
                if probability == 0:
                    continue
                posterior = {state: joints[state] / probability for state in states}
                if steps > 1:
                    continuation_value, continuation = solve(
                        posterior, steps - 1, remaining_budget, remaining_loss
                    )
                else:
                    continuation_value = 0.0
                    continuation = {
                        "action_id": None,
                        "kind": "stop",
                        "expected_value": 0.0,
                        "observations": [],
                    }
                future_terms.append(float(probability * Fraction(continuation_value)))
                branches.append(
                    {
                        "id": outcome,
                        "probability": float(probability),
                        "possible": True,
                        "posterior": float_distribution(posterior),
                        "posterior_support": [state for state in states if posterior[state] > 0],
                        "continuation": continuation,
                    }
                )
            value = _sum((immediate, _sum(future_terms)))
            candidates.append(
                (value, _choice_rank(action), {
                    "action_id": action["id"],
                    "kind": action["kind"],
                    "expected_value": value,
                    "immediate_expected_utility": immediate,
                    "resource_cost": action["resource_cost"],
                    "worst_step_loss": float(worst_step_loss),
                    "observations": branches,
                })
            )
        maximum = max((item[0] for item in candidates), default=0.0)
        if maximum <= _SCORE_TOLERANCE:
            cache[key] = (0.0, stop_policy)
            return 0.0, stop_policy
        # A tolerance is always measured against the fixed maximum. Comparing
        # successive candidates would let a chain of near ties drift downward.
        eligible = (
            item for item in candidates
            if item[0] > _SCORE_TOLERANCE
            and math.isclose(item[0], maximum, rel_tol=0.0, abs_tol=_SCORE_TOLERANCE)
        )
        best_value, _, best_policy = min(eligible, key=lambda item: item[1])
        cache[key] = (best_value, best_policy)
        return best_value, best_policy

    expected_value, policy = solve(
        prior, inputs["horizon"], Fraction(inputs["budget"]),
        None if inputs["max_loss"] is None else Fraction(inputs["max_loss"]),
    )
    return {
        "model_version": MODEL_VERSION,
        "inputs": inputs,
        "prior_support": [state for state in states if prior[state] > 0],
        "first_action": policy["action_id"],
        "expected_value": expected_value,
        "policy": policy,
        "expansions": expansions,
        "cache_hits": cache_hits,
        "assumptions": {
            "reward_timing": "immediate utility depends on pre-action state",
            "state_timing": "transition occurs after reward and before observation",
            "observation_timing": "observation depends on post-transition state",
            "cost_timing": "cost deducted once per chosen action; resource_cost debits budget",
            "risk_bound": "max_loss bounds sum of worst possible step losses when supplied",
            "termination": "stop with zero future value is always available",
            "numeric_semantics": "exact probability support and cap debits; float utility scores and reported probabilities",
        },
    }
