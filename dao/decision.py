"""A small, auditable decision model for acting and buying information.

All payoffs and costs must share the same utility units. An action's heuristic
score is expected utility, less an expected loss penalty, a commitment penalty,
and an expected rollback cost::

    E[payoff] - risk_aversion * E[max(-payoff, 0)]
    - irreversibility_aversion * (1 - reversibility) * commitment_cost
    - reversibility * rollback_cost

``reversibility`` is a user-supplied degree of recoverability, not a calibrated
probability. Its use as a rollback-cost weight is a modeling heuristic. This
engine evaluates stated assumptions; it does not estimate them or execute acts.

Waiting buys one observation followed by one action. Signal likelihoods are
P(signal | state), and must sum to one for each state. Scores under the resulting
posteriors retain the same preferences and action costs. EVSI is the value of
that information before waiting cost and discount; net option value includes
both. Waiting is available only when a signal model is supplied.
"""

from __future__ import annotations

import math
from typing import Any


MAX_ITEMS = 64
MAX_MAGNITUDE = 1_000_000_000.0
PROBABILITY_TOLERANCE = 1e-9
SCORE_ABSOLUTE_TOLERANCE = 1e-9
SCORE_RELATIVE_TOLERANCE = 1e-12


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} must have string keys")
    return value


def _fields(
    value: dict[str, Any], required: set[str], optional: set[str], path: str
) -> None:
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing:
        raise ValueError(f"{path} is missing required fields: {', '.join(sorted(missing))}")
    if extra:
        names = ", ".join(repr(key[:80]) for key in sorted(extra)[:10])
        raise ValueError(f"{path} has unknown fields: {names}")


def _text(value: Any, path: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{path} must be a nonempty string of at most {limit} characters")
    return value


def _identifier(value: Any, path: str) -> str:
    result = _text(value, path, 64)
    if result != result.strip() or any(ord(char) < 32 for char in result):
        raise ValueError(f"{path} must not contain control characters or surrounding whitespace")
    return result


def _number(
    value: Any, path: str, lower: float = -MAX_MAGNITUDE, upper: float = MAX_MAGNITUDE
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite number (booleans are not numbers)")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{path} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{path} must be a finite number")
    if not lower <= result <= upper:
        raise ValueError(f"{path} must be between {lower:g} and {upper:g}")
    return result


def _items(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ITEMS:
        raise ValueError(f"{path} must be a list containing 1 to {MAX_ITEMS} items")
    return value


def _unique(identifier: str, seen: set[str], path: str) -> None:
    if identifier in seen:
        raise ValueError(f"{path} contains duplicate id {identifier!r}")
    seen.add(identifier)


def _state_values(
    value: Any, state_ids: set[str], path: str, *, probability: bool = False
) -> dict[str, float]:
    source = _object(value, path)
    if source.keys() != state_ids:
        raise ValueError(f"{path} must contain exactly the declared state ids")
    return {
        key: _number(number, f"{path}.{key}", 0.0, 1.0)
        if probability
        else _number(number, f"{path}.{key}")
        for key, number in source.items()
    }


def validate_decision(problem: dict[str, Any]) -> dict[str, Any]:
    """Validate and return an independent, normalized JSON-compatible problem.

    Raises ``ValueError`` with a field path for invalid data. Unknown fields are
    rejected. There may be 1–64 states, actions, or signals; absolute numeric
    inputs are bounded at 1e9. Probability sums within 1e-9 of one are accepted
    and normalized to remove floating-point drift. Optional action costs and
    preference weights default to zero; an optional wait discount defaults to
    one. The caller's input is never mutated.
    """
    source = _object(problem, "problem")
    _fields(
        source,
        {"title", "states", "actions"},
        {"risk_aversion", "irreversibility_aversion", "wait"},
        "problem",
    )
    result: dict[str, Any] = {
        "title": _text(source["title"], "title"),
        "risk_aversion": _number(source.get("risk_aversion", 0), "risk_aversion", 0.0),
        "irreversibility_aversion": _number(
            source.get("irreversibility_aversion", 0), "irreversibility_aversion", 0.0
        ),
        "states": [],
        "actions": [],
    }
    state_ids: set[str] = set()
    for index, raw_state in enumerate(_items(source["states"], "states")):
        path = f"states[{index}]"
        state = _object(raw_state, path)
        _fields(state, {"id", "probability"}, set(), path)
        identifier = _identifier(state["id"], f"{path}.id")
        _unique(identifier, state_ids, "states")
        result["states"].append(
            {
                "id": identifier,
                "probability": _number(state["probability"], f"{path}.probability", 0.0, 1.0),
            }
        )
    probability_sum = math.fsum(state["probability"] for state in result["states"])
    if not math.isclose(probability_sum, 1.0, rel_tol=0.0, abs_tol=PROBABILITY_TOLERANCE):
        raise ValueError("states probabilities must sum to one")
    for state in result["states"]:
        state["probability"] /= probability_sum

    action_ids: set[str] = set()
    for index, raw_action in enumerate(_items(source["actions"], "actions")):
        path = f"actions[{index}]"
        action = _object(raw_action, path)
        _fields(
            action,
            {"id", "label", "payoffs", "reversibility"},
            {"rollback_cost", "commitment_cost"},
            path,
        )
        identifier = _identifier(action["id"], f"{path}.id")
        _unique(identifier, action_ids, "actions")
        result["actions"].append(
            {
                "id": identifier,
                "label": _text(action["label"], f"{path}.label"),
                "payoffs": _state_values(action["payoffs"], state_ids, f"{path}.payoffs"),
                "reversibility": _number(action["reversibility"], f"{path}.reversibility", 0.0, 1.0),
                "rollback_cost": _number(action.get("rollback_cost", 0), f"{path}.rollback_cost", 0.0),
                "commitment_cost": _number(
                    action.get("commitment_cost", 0), f"{path}.commitment_cost", 0.0
                ),
            }
        )

    if "wait" in source:
        wait_source = _object(source["wait"], "wait")
        _fields(wait_source, {"cost", "signals"}, {"discount"}, "wait")
        wait = {
            "cost": _number(wait_source["cost"], "wait.cost", 0.0),
            "discount": _number(wait_source.get("discount", 1), "wait.discount", 0.0, 1.0),
            "signals": [],
        }
        signal_ids: set[str] = set()
        for index, raw_signal in enumerate(_items(wait_source["signals"], "wait.signals")):
            path = f"wait.signals[{index}]"
            signal = _object(raw_signal, path)
            _fields(signal, {"id", "likelihoods"}, set(), path)
            identifier = _identifier(signal["id"], f"{path}.id")
            _unique(identifier, signal_ids, "wait.signals")
            wait["signals"].append(
                {
                    "id": identifier,
                    "likelihoods": _state_values(
                        signal["likelihoods"], state_ids, f"{path}.likelihoods", probability=True
                    ),
                }
            )
        for state_id in state_ids:
            total = math.fsum(signal["likelihoods"][state_id] for signal in wait["signals"])
            if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=PROBABILITY_TOLERANCE):
                raise ValueError(f"wait.signals likelihoods for state {state_id!r} must sum to one")
            for signal in wait["signals"]:
                signal["likelihoods"][state_id] /= total
        result["wait"] = wait
    return result


def _score_actions(problem: dict[str, Any], probabilities: dict[str, float]) -> list[dict[str, Any]]:
    scored = []
    for action in problem["actions"]:
        utility = math.fsum(probabilities[state] * payoff for state, payoff in action["payoffs"].items())
        downside = math.fsum(
            probabilities[state] * max(-payoff, 0.0) for state, payoff in action["payoffs"].items()
        )
        downside_penalty = problem["risk_aversion"] * downside
        irreversibility_penalty = (
            problem["irreversibility_aversion"] * (1.0 - action["reversibility"]) * action["commitment_cost"]
        )
        rollback_penalty = action["reversibility"] * action["rollback_cost"]
        scored.append(
            {
                "id": action["id"],
                "label": action["label"],
                "expected_utility": utility,
                "expected_downside": downside,
                "downside_penalty": downside_penalty,
                "irreversibility_penalty": irreversibility_penalty,
                "rollback_penalty": rollback_penalty,
                "reversibility": action["reversibility"],
                "score": math.fsum([utility, -downside_penalty, -irreversibility_penalty, -rollback_penalty]),
            }
        )
    return scored


def _tied(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=SCORE_RELATIVE_TOLERANCE, abs_tol=SCORE_ABSOLUTE_TOLERANCE)


def _best(actions: list[dict[str, Any]]) -> dict[str, Any]:
    # Compare every contender with the true maximum, avoiding order-dependent
    # chains of pairwise near-ties. IDs provide stable ordering of equal options.
    highest_score = max(action["score"] for action in actions)
    contenders = [action for action in actions if _tied(action["score"], highest_score)]
    return min(contenders, key=lambda action: (-action["reversibility"], action["id"]))


def evaluate(problem: dict[str, Any]) -> dict[str, Any]:
    """Return current scores, Bayesian observation policy, and a recommendation.

    Numerically tied actions favor greater reversibility, then lexical action
    ID. Waiting wins a numerical tie when a wait model exists. A wait model does
    not add a free do-nothing action: after its signal, one declared action must
    be selected. Explicitly include a hold action when abstention is feasible.
    The result is JSON serializable and contains no generated or executed acts.
    """
    model = validate_decision(problem)
    priors = {state["id"]: state["probability"] for state in model["states"]}
    actions = _score_actions(model, priors)
    best = _best(actions)
    result: dict[str, Any] = {
        "title": model["title"],
        "actions": actions,
        "best_action_id": best["id"],
        "best_action_score": best["score"],
        "wait": None,
        "recommendation": {
            "kind": "act",
            "action_id": best["id"],
            "reason": f"{best['label']} has the highest current score ({best['score']:.6g}); no observation model was supplied.",
        },
    }
    if "wait" not in model:
        return result

    wait = model["wait"]
    policy = []
    for signal in wait["signals"]:
        joint = {state: prior * signal["likelihoods"][state] for state, prior in priors.items()}
        mass = math.fsum(joint.values())
        if mass == 0.0:
            continue
        posterior = {state: probability / mass for state, probability in joint.items()}
        after_signal = _best(_score_actions(model, posterior))
        policy.append(
            {
                "signal_id": signal["id"],
                "probability": mass,
                "posterior": posterior,
                "action_id": after_signal["id"],
                "action_score": after_signal["score"],
            }
        )
    expected_future_score = math.fsum(item["probability"] * item["action_score"] for item in policy)
    evsi = expected_future_score - best["score"]
    if _tied(expected_future_score, best["score"]):
        evsi = 0.0
    wait_score = wait["discount"] * expected_future_score - wait["cost"]
    option_value = wait_score - best["score"]
    result["wait"] = {
        "score": wait_score,
        "evsi": evsi,
        "net_option_value": option_value,
        "expected_future_score": expected_future_score,
        "cost": wait["cost"],
        "discount": wait["discount"],
        "max_affordable_cost": wait["discount"] * expected_future_score - best["score"],
        "policy": policy,
    }
    if wait_score > best["score"] or _tied(wait_score, best["score"]):
        tied = _tied(wait_score, best["score"])
        result["recommendation"] = {
            "kind": "wait",
            "action_id": None,
            "reason": (
                "Waiting ties the best current score and preserves the choice until after the observation."
                if tied
                else f"Waiting scores {wait_score:.6g}, improving on acting now by {option_value:.6g} after cost and discount."
            ),
        }
    else:
        result["recommendation"]["reason"] = (
            f"{best['label']} scores {best['score']:.6g}; waiting scores {wait_score:.6g} after cost and discount."
        )
    return result
