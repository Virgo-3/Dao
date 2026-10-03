"""Deterministic, one-step decision analysis in explicitly modeled utility units.

This module recommends; it never executes an action, acquires evidence, or charges
the usage ledger. See docs/decision-model.md for assumptions and the exact model.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

from .numerics import exact_distribution, float_distribution

MODEL_VERSION = "dao-decision/2"
_TOLERANCE = 1e-12
_PROBABILITY_TOLERANCE = 1e-9


def _object(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - keys
    if unknown:
        raise ValueError(f"{name} has unsupported fields: {sorted(unknown, key=str)}")
    return value


def _number(
    value: Any, name: str, minimum: float | None = None, maximum: float | None = None
) -> float:
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
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("decision arithmetic exceeded finite numeric range")
    return value


def _sum(values: Any) -> float:
    try:
        return _finite(math.fsum(values))
    except OverflowError as exc:
        raise ValueError("decision arithmetic exceeded finite numeric range") from exc


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE)


def _improves(left: float, right: float) -> bool:
    return left > right and not _close(left, right)


def _normalize(values: dict[str, float]) -> dict[str, float]:
    """Normalize once and stabilize the rounded sum for exact input replay."""
    total = _sum(values.values())
    if total == 1.0:
        return dict(values)
    normalized = {key: value / total for key, value in values.items()}
    pivot = max(normalized, key=normalized.get)
    for _ in range(4):
        residual = 1.0 - _sum(normalized.values())
        if residual == 0.0:
            return normalized
        normalized[pivot] += residual
    if _sum(normalized.values()) != 1.0:
        raise ValueError("numerical instability in probability normalization")
    return normalized


def _mapping(
    value: Any, scenario_ids: set[str], name: str, *, probability: bool = False
) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != scenario_ids:
        raise ValueError(f"{name} must contain exactly the scenario IDs")
    return {
        scenario: _number(
            value[scenario],
            f"{name}.{scenario}",
            0.0 if probability else None,
            1.0 if probability else None,
        )
        for scenario in sorted(scenario_ids)
    }


def _validate(problem: dict[str, Any]) -> dict[str, Any]:
    problem = _object(
        problem,
        "problem",
        {"scenarios", "actions", "signals", "policy", "wait_cost", "utility_unit"},
    )
    raw_scenarios = problem.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("scenarios must be a nonempty list")
    probabilities: dict[str, float] = {}
    for index, raw in enumerate(raw_scenarios):
        item = _object(raw, f"scenarios[{index}]", {"id", "probability"})
        identifier = _id(item.get("id"), f"scenarios[{index}].id")
        if identifier in probabilities:
            raise ValueError("scenario IDs must be unique")
        probabilities[identifier] = _number(
            item.get("probability"), f"scenarios[{index}].probability", 0.0, 1.0
        )
    total = _sum(probabilities.values())
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=_PROBABILITY_TOLERANCE):
        raise ValueError("scenario probabilities must sum to 1")
    probabilities = _normalize(dict(sorted(probabilities.items())))
    scenario_ids = set(probabilities)

    raw_actions = problem.get("actions")
    if not isinstance(raw_actions, list):
        raise ValueError("actions must be a list")
    actions = []
    seen_actions = set()
    for index, raw in enumerate(raw_actions):
        item = _object(
            raw, f"actions[{index}]", {"id", "utilities", "cost", "reversibility", "rollback_cost"}
        )
        identifier = _id(item.get("id"), f"actions[{index}].id")
        if identifier in {"abstain", "wait"}:
            raise ValueError("action IDs abstain and wait are reserved")
        if identifier in seen_actions:
            raise ValueError("action IDs must be unique")
        seen_actions.add(identifier)
        actions.append(
            {
                "id": identifier,
                "utilities": _mapping(
                    item.get("utilities"), scenario_ids, f"actions[{index}].utilities"
                ),
                "cost": _number(item.get("cost", 0.0), f"actions[{index}].cost", 0.0),
                "reversibility": _number(
                    item.get("reversibility", 1.0), f"actions[{index}].reversibility", 0.0, 1.0
                ),
                "rollback_cost": _number(
                    item.get("rollback_cost", 0.0), f"actions[{index}].rollback_cost", 0.0
                ),
            }
        )
    actions.sort(key=lambda item: item["id"])

    raw_policy = _object(
        problem.get("policy", {}),
        "policy",
        {"downside_weight", "irreversibility_weight", "max_loss", "min_score"},
    )
    policy = {
        "downside_weight": _number(
            raw_policy.get("downside_weight", 0.0), "policy.downside_weight", 0.0
        ),
        "irreversibility_weight": _number(
            raw_policy.get("irreversibility_weight", 0.0), "policy.irreversibility_weight", 0.0
        ),
        "min_score": _number(raw_policy.get("min_score", 0.0), "policy.min_score"),
        "max_loss": None,
    }
    if raw_policy.get("max_loss") is not None:
        policy["max_loss"] = _number(raw_policy["max_loss"], "policy.max_loss", 0.0)

    raw_signals = problem.get("signals", [])
    if not isinstance(raw_signals, list):
        raise ValueError("signals must be a list")
    signals = []
    seen_signals = set()
    for index, raw in enumerate(raw_signals):
        item = _object(raw, f"signals[{index}]", {"id", "likelihoods"})
        identifier = _id(item.get("id"), f"signals[{index}].id")
        if identifier in seen_signals:
            raise ValueError("signal IDs must be unique")
        seen_signals.add(identifier)
        signals.append(
            {
                "id": identifier,
                "likelihoods": _mapping(
                    item.get("likelihoods"),
                    scenario_ids,
                    f"signals[{index}].likelihoods",
                    probability=True,
                ),
            }
        )
    signals.sort(key=lambda item: item["id"])
    if signals:
        for scenario in probabilities:
            partition = _sum(item["likelihoods"][scenario] for item in signals)
            if not math.isclose(partition, 1.0, rel_tol=0.0, abs_tol=_PROBABILITY_TOLERANCE):
                raise ValueError(f"signal likelihoods for {scenario} must sum to 1")
            normalized = _normalize({item["id"]: item["likelihoods"][scenario] for item in signals})
            for item in signals:
                item["likelihoods"][scenario] = normalized[item["id"]]

    utility_unit = problem.get("utility_unit", "modeled utility units")
    if not isinstance(utility_unit, str) or not utility_unit.strip():
        raise ValueError("utility_unit must be a nonempty string")
    return {
        "scenarios": [{"id": key, "probability": value} for key, value in probabilities.items()],
        "actions": actions,
        "signals": signals,
        "policy": policy,
        "wait_cost": _number(problem.get("wait_cost", 0.0), "wait_cost", 0.0),
        "utility_unit": utility_unit,
    }


def _action_values(
    actions: list[dict[str, Any]], probabilities: dict[str, Fraction], policy: dict[str, Any]
) -> list[dict[str, Any]]:
    values = []
    for action in actions:
        exact_net = {
            scenario: Fraction(utility) - Fraction(action["cost"])
            for scenario, utility in action["utilities"].items()
        }
        net = {
            scenario: _finite(utility - action["cost"])
            for scenario, utility in action["utilities"].items()
        }
        expected = _sum(
            probabilities[scenario] * utility for scenario, utility in exact_net.items()
        )
        downside = _sum(
            probabilities[scenario] * max(Fraction(0), -utility)
            for scenario, utility in exact_net.items()
        )
        exact_worst_loss = max(
            max(Fraction(0), -exact_net[scenario])
            for scenario, probability in probabilities.items()
            if probability > 0.0
        )
        irreversibility = 1.0 - action["reversibility"]
        irreversible_penalty = _finite(policy["irreversibility_weight"] * irreversibility)
        rollback_penalty = _finite(action["rollback_cost"] * irreversibility)
        score = _finite(
            expected
            - _finite(policy["downside_weight"] * downside)
            - irreversible_penalty
            - rollback_penalty
        )
        admissible = (policy["max_loss"] is None
                      or exact_worst_loss <= Fraction(policy["max_loss"]))
        values.append(
            {
                "action_id": action["id"],
                "net_by_scenario": net,
                "expected_net_utility": expected,
                "expected_downside": downside,
                "worst_loss": float(exact_worst_loss),
                "reversibility": action["reversibility"],
                "irreversibility_penalty": irreversible_penalty,
                "rollback_penalty": rollback_penalty,
                "score": score,
                "admissible": admissible,
                "reason": "within loss policy" if admissible else "exceeds max_loss",
            }
        )
    return values


def _best(values: list[dict[str, Any]]) -> dict[str, Any]:
    abstain = {"kind": "abstain", "action_id": None, "score": 0.0, "reversibility": 1.0}
    eligible = [value for value in values if value["admissible"] and _improves(value["score"], 0)]
    if not eligible:
        return abstain
    maximum = max(value["score"] for value in eligible)
    tied = [value for value in eligible if _close(value["score"], maximum)]
    chosen = min(tied, key=lambda value: (-value["reversibility"], value["action_id"]))
    return {
        "kind": "act",
        "action_id": chosen["action_id"],
        "score": chosen["score"],
        "reversibility": chosen["reversibility"],
    }


def _optimal_score(values: list[dict[str, Any]]) -> float:
    """Potential information value uses maxima before approximate tie preferences."""
    return max((value["score"] for value in values if value["admissible"]), default=0.0,)


def _recommend(
    best: dict[str, Any], min_score: float, values: list[dict[str, Any]]
) -> dict[str, Any]:
    eligible = _best([value for value in values if value["score"] >= min_score])
    if eligible["kind"] == "act":
        return {
            "kind": "act",
            "action_id": eligible["action_id"],
            "score": eligible["score"],
            "reasons": ["highest admissible score above abstention", "meets min_score"],
        }
    return {
        "kind": "abstain",
        "action_id": None,
        "score": 0.0,
        "reasons": [
            "no admissible action improves on abstention"
            if best["kind"] == "abstain"
            else "best action is below min_score"
        ],
    }


def evaluate(problem: dict[str, Any]) -> dict[str, Any]:
    """Validate and evaluate a finite decision problem, without side effects.

    The returned inputs are canonicalized and include all defaults. A signal
    collection describes mutually exclusive, exhaustive outcomes of ONE evidence
    acquisition. Numerical ties prefer abstention, then higher reversibility,
    then lexicographically smaller action IDs. Invalid models raise ValueError.
    """
    inputs = _validate(problem)
    probabilities = exact_distribution(
        {item["id"]: item["probability"] for item in inputs["scenarios"]}
    )
    policy = inputs["policy"]
    action_values = _action_values(inputs["actions"], probabilities, policy)
    best_now = _best(action_values)
    optimal_score_now = max(0.0, _optimal_score(action_values))
    recommendation_now = _recommend(best_now, policy["min_score"], action_values)
    likelihoods = {
        scenario: exact_distribution(
            {signal["id"]: signal["likelihoods"][scenario] for signal in inputs["signals"]}
        )
        for scenario in probabilities
    } if inputs["signals"] else {}
    signal_values = []
    signal_probabilities = {}
    for signal in inputs["signals"]:
        joints = {
            scenario: probability * likelihoods[scenario][signal["id"]]
            for scenario, probability in probabilities.items()
        }
        signal_probability = sum(joints.values(), Fraction(0))
        signal_probabilities[signal["id"]] = signal_probability
        if signal_probability == 0:
            signal_values.append(
                {
                    "signal_id": signal["id"],
                    "probability": 0.0,
                    "possible": False,
                    "posterior": None,
                    "posterior_support": [],
                    "action_evaluations": [],
                    "best": None,
                    "optimal_score": None,
                    "recommendation": None,
                }
            )
            continue
        posterior = exact_distribution(joints)
        values = _action_values(inputs["actions"], posterior, policy)
        best = _best(values)
        signal_values.append(
            {
                "signal_id": signal["id"],
                "probability": float(signal_probability),
                "possible": True,
                "posterior": float_distribution(posterior),
                "posterior_support": [scenario for scenario, probability in posterior.items()
                                      if probability > 0],
                "action_evaluations": values,
                "best": best,
                "optimal_score": max(0.0, _optimal_score(values)),
                "recommendation": _recommend(best, policy["min_score"], values),
            }
        )
    value_after = (
        _sum(
            signal_probabilities[item["signal_id"]] * Fraction(item["optimal_score"])
            for item in signal_values
            if item["best"] is not None
        )
        if signal_values
        else optimal_score_now
    )
    evsi = _finite(value_after - optimal_score_now)
    if evsi < 0.0:
        if not _close(value_after, optimal_score_now):
            raise ValueError("numerical instability in expected value of information")
        evsi = 0.0
        value_after = optimal_score_now
    value_after_recommendations = (
        _sum(
            signal_probabilities[item["signal_id"]] * Fraction(item["recommendation"]["score"])
            for item in signal_values if item["recommendation"] is not None
        ) if signal_values else recommendation_now["score"]
    )
    wait_score = _finite(value_after_recommendations - inputs["wait_cost"])
    wait_available = bool(inputs["signals"])
    wait_improves = wait_available and _improves(wait_score, recommendation_now["score"])
    wait_meets_threshold = wait_score >= policy["min_score"]
    recommendation = recommendation_now
    if wait_improves and wait_meets_threshold:
        recommendation = {
            "kind": "wait",
            "action_id": None,
            "score": wait_score,
            "reasons": [
                "modeled evidence improves expected recommendation value",
                "thresholded continuation value exceeds immediate recommendation and wait_cost",
                "meets min_score",
            ],
        }
    wait_reasons = []
    if not wait_available:
        wait_reasons.append("no evidence signal model supplied")
    elif not wait_improves:
        wait_reasons.append("wait score does not strictly improve on recommendation_now")
    if not wait_meets_threshold:
        wait_reasons.append("wait score is below min_score")
    return {
        "model_version": MODEL_VERSION,
        "utility_unit": inputs["utility_unit"],
        "inputs": inputs,
        "numerical_tolerance": _TOLERANCE,
        "action_evaluations": action_values,
        "best_now": best_now,
        "optimal_score_now": optimal_score_now,
        "recommendation_now": recommendation_now,
        "signal_evaluations": signal_values,
        "value_after_signals": value_after,
        "value_after_recommendations": value_after_recommendations,
        "expected_value_of_sample_information": evsi,
        "wait_evaluation": {
            "available": wait_available,
            "wait_cost": inputs["wait_cost"],
            "score": wait_score,
            "improves_best_now": wait_available and _improves(wait_score, best_now["score"]),
            "improves_recommendation_now": wait_improves,
            "meets_min_score": wait_meets_threshold,
            "reasons": wait_reasons,
        },
        "recommendation": recommendation,
    }
