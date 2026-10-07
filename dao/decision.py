"""A finite, inspectable decision model with an explicit option to wait.

All payoffs and costs use the same caller-defined utility unit. An action's
scenario outcome is payoff minus cost, a rollback cost in losing scenarios,
and ``irreversibility_penalty * (1 - reversibility)``. The rollback trigger is
the sign of payoff minus cost before either penalty. Utility is the expected
outcome minus ``risk_aversion * variance(outcome)``; risk_aversion therefore
has units of inverse utility. This is a mean-variance preference model, not a
claim that a probability or payoff supplied by the caller is calibrated.

Signals are mutually exclusive, exhaustive observations. Their likelihoods
are P(signal | scenario). Waiting means obtaining one such observation and
then choosing the best action (or abstaining) under its Bayes posterior. It
never assumes that time alone supplies information. Abstaining has utility 0.
"""

from __future__ import annotations

import math
from typing import Any


_SUM_TOLERANCE = 1e-9
MAX_DECISION_ITEMS = 64
MAX_DECISION_WORK = 250_000
MAX_NAME_LENGTH = 200


def _number(value: Any, field: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return result


def _finite(value: float, field: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{field} exceeds the finite numerical range")
    return value


def _sum(values: Any, field: str) -> float:
    try:
        return _finite(math.fsum(values), field)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} exceeds the finite numerical range") from exc


def _object(value: Any, field: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} field names must be strings")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{field} contains unknown fields: {', '.join(sorted(unknown))}")
    return value


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    if len(value) > MAX_NAME_LENGTH:
        raise ValueError(f"{field} must be at most {MAX_NAME_LENGTH} characters")
    return value.strip()


def _array(value: Any, field: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        suffix = "nonempty " if nonempty else ""
        raise ValueError(f"{field} must be a {suffix}array")
    if len(value) > MAX_DECISION_ITEMS:
        raise ValueError(f"{field} must contain at most {MAX_DECISION_ITEMS} items")
    return value


def _unique(items: list[dict[str, Any]], field: str) -> None:
    names = [item["name"] for item in items]
    if len(names) != len(set(names)):
        raise ValueError(f"{field} names must be unique")


def _normalize(payload: Any) -> dict[str, Any]:
    payload = _object(payload, "decision", {
        "scenarios", "actions", "signals", "waiting_cost", "discount",
        "irreversibility_penalty", "risk_aversion", "confidence_margin",
        "observation_cost", "delay_cost", "reconsider_when",
    })
    scenarios = []
    for index, raw in enumerate(_array(payload.get("scenarios"), "scenarios", nonempty=True)):
        item = _object(raw, f"scenarios[{index}]", {"name", "probability"})
        scenarios.append({
            "name": _name(item.get("name"), f"scenarios[{index}].name"),
            "probability": _number(item.get("probability"), f"scenarios[{index}].probability",
                                   minimum=0, maximum=1),
        })
    _unique(scenarios, "scenario")
    probability_sum = _sum((item["probability"] for item in scenarios), "scenario probabilities")
    if not math.isclose(probability_sum, 1, rel_tol=0, abs_tol=_SUM_TOLERANCE):
        raise ValueError("scenario probabilities must sum to 1")
    # Normalize only tolerated rounding error, so every posterior uses a prior summing to one.
    for item in scenarios:
        item["probability"] /= probability_sum

    actions = []
    for index, raw in enumerate(_array(payload.get("actions"), "actions", nonempty=True)):
        item = _object(raw, f"actions[{index}]", {"name", "payoffs", "cost", "reversibility", "rollback_cost"})
        raw_payoffs = _array(item.get("payoffs"), f"actions[{index}].payoffs")
        if len(raw_payoffs) != len(scenarios):
            raise ValueError(f"actions[{index}].payoffs must match the number of scenarios")
        actions.append({
            "name": _name(item.get("name"), f"actions[{index}].name"),
            "payoffs": [_number(value, f"actions[{index}].payoffs[{position}]")
                        for position, value in enumerate(raw_payoffs)],
            "cost": _number(item.get("cost", 0), f"actions[{index}].cost", minimum=0),
            "reversibility": _number(item.get("reversibility", 0), f"actions[{index}].reversibility",
                                     minimum=0, maximum=1),
            "rollback_cost": _number(item.get("rollback_cost", 0), f"actions[{index}].rollback_cost", minimum=0),
        })
    _unique(actions, "action")

    signals = []
    for index, raw in enumerate(_array(payload.get("signals", []), "signals")):
        item = _object(raw, f"signals[{index}]", {"name", "likelihoods"})
        likelihoods = _array(item.get("likelihoods"), f"signals[{index}].likelihoods")
        if len(likelihoods) != len(scenarios):
            raise ValueError(f"signals[{index}].likelihoods must match the number of scenarios")
        signals.append({
            "name": _name(item.get("name"), f"signals[{index}].name"),
            "likelihoods": [_number(value, f"signals[{index}].likelihoods[{position}]", minimum=0, maximum=1)
                            for position, value in enumerate(likelihoods)],
        })
    _unique(signals, "signal")
    if signals:
        for scenario_index in range(len(scenarios)):
            total = _sum((signal["likelihoods"][scenario_index] for signal in signals), "signal likelihoods")
            if not math.isclose(total, 1, rel_tol=0, abs_tol=_SUM_TOLERANCE):
                raise ValueError("signal likelihoods must sum to 1 for each scenario")
            for signal in signals:
                signal["likelihoods"][scenario_index] /= total

    return {
        "scenarios": scenarios,
        "actions": actions,
        "signals": signals,
        "waiting_cost": _number(payload.get("waiting_cost", 0), "waiting_cost", minimum=0),
        "discount": _number(payload.get("discount", 1), "discount", minimum=0, maximum=1),
        "irreversibility_penalty": _number(payload.get("irreversibility_penalty", 0), "irreversibility_penalty", minimum=0),
        "risk_aversion": _number(payload.get("risk_aversion", 0), "risk_aversion", minimum=0),
        "confidence_margin": _number(payload.get("confidence_margin", 0), "confidence_margin", minimum=0),
        "observation_cost": _number(payload.get("observation_cost", 0), "observation_cost", minimum=0),
        "delay_cost": _number(payload.get("delay_cost", 0), "delay_cost", minimum=0),
        "reconsider_when": _name(payload["reconsider_when"], "reconsider_when") if "reconsider_when" in payload else None,
    }


def _score(action: dict[str, Any], probabilities: list[float], model: dict[str, Any]) -> dict[str, Any]:
    irreversibility_cost = _finite(model["irreversibility_penalty"] * (1 - action["reversibility"]), "irreversibility cost")
    outcomes = []
    losing = []
    for payoff in action["payoffs"]:
        before_penalties = _finite(payoff - action["cost"], "payoff minus cost")
        is_losing = before_penalties < 0
        losing.append(is_losing)
        outcomes.append(_finite(before_penalties - (action["rollback_cost"] if is_losing else 0)
                                - irreversibility_cost, "net outcome"))
    expected_payoff = _sum((p * value for p, value in zip(probabilities, action["payoffs"])), "expected payoff")
    expected_net = _sum((p * value for p, value in zip(probabilities, outcomes)), "expected net payoff")
    variance = _sum((p * _finite((value - expected_net) ** 2, "outcome variance")
                     for p, value in zip(probabilities, outcomes) if p > 0), "outcome variance")
    risk_penalty = _finite(model["risk_aversion"] * variance, "risk penalty")
    downside_probability = _sum((p for p, loss in zip(probabilities, losing) if loss), "downside probability")
    return {
        "name": action["name"],
        "expected_payoff": expected_payoff,
        "cost": action["cost"],
        "downside_probability": downside_probability,
        "expected_rollback_cost": _finite(downside_probability * action["rollback_cost"], "expected rollback cost"),
        "reversibility": action["reversibility"],
        "irreversibility_cost": irreversibility_cost,
        "expected_net_payoff": expected_net,
        "variance": variance,
        "risk_penalty": risk_penalty,
        "utility": _finite(expected_net - risk_penalty, "utility"),
    }


def _best(scores: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    """Prefer abstention at zero, then reversibility for numerical action ties."""
    maximum = max([0.0, *(score["utility"] for score in scores)])
    if maximum <= 0:
        return None, 0.0
    tied = [score for score in scores
            if math.isclose(score["utility"], maximum, rel_tol=1e-12, abs_tol=1e-12)]
    best = max(tied, key=lambda score: score["reversibility"])
    return best, maximum


def evaluate(payload: Any, *, blocked_actions: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Validate and score a decision; return JSON-compatible, deterministic evidence.

    ``confidence_margin`` is a utility buffer, not a statistical confidence
    interval. Waiting must beat the best immediate utility by this buffer;
    acting must beat abstention by it. Equal options keep the immediate choice.
    Action utility ties prefer higher reversibility, then caller input order.
    Numerical overflow is rejected instead of returning non-finite JSON values.
    """
    model = _normalize(payload)
    work = len(model["actions"]) * len(model["scenarios"]) * (len(model["signals"]) + 1)
    if work > MAX_DECISION_WORK:
        raise ValueError(f"Decision workload exceeds {MAX_DECISION_WORK} action-scenario evaluations")
    # The runtime supplies these constraints; they cannot be supplied by the model
    # in the decision JSON. Validate the whole problem before excluding actions.
    blocked_actions = blocked_actions or {}
    blocked = [{"name": action["name"], "conflicts": blocked_actions[action["name"]]}
               for action in model["actions"] if action["name"] in blocked_actions]
    model["actions"] = [action for action in model["actions"]
                        if action["name"] not in blocked_actions]
    prior = [scenario["probability"] for scenario in model["scenarios"]]
    try:
        scores = [_score(action, prior, model) for action in model["actions"]]
        best, baseline = _best(scores)
        signal_analysis = []
        if model["signals"]:
            optimized_terms = []
            for signal in model["signals"]:
                joints = [p * likelihood for p, likelihood in zip(prior, signal["likelihoods"])]
                probability = _sum(joints, "signal probability")
                if probability == 0:
                    signal_analysis.append({"name": signal["name"], "probability": 0.0,
                                            "posterior": None, "selected_action": None,
                                            "optimized_utility": 0.0})
                    continue
                posterior = [joint / probability for joint in joints]
                posterior_scores = [_score(action, posterior, model) for action in model["actions"]]
                signal_best, signal_utility = _best(posterior_scores)
                optimized_terms.append(probability * signal_utility)
                signal_analysis.append({
                    "name": signal["name"],
                    "probability": probability,
                    "posterior": [{"name": scenario["name"], "probability": value}
                                  for scenario, value in zip(model["scenarios"], posterior)],
                    "selected_action": signal_best["name"] if signal_best else None,
                    "optimized_utility": signal_utility,
                })
            after_signal = _sum(optimized_terms, "expected utility after signal")
        else:
            after_signal = baseline
        information_value = max(0.0, _finite(after_signal - baseline, "expected value of information"))
        total_waiting_cost = _sum((model["waiting_cost"], model["observation_cost"], model["delay_cost"]), "total waiting cost")
        wait_utility = _finite(model["discount"] * after_signal - total_waiting_cost, "wait utility")
    except OverflowError as exc:
        raise ValueError("decision calculations exceed the finite numerical range") from exc

    margin = model["confidence_margin"]
    if wait_utility > _finite(baseline + margin, "baseline plus confidence margin"):
        recommendation = "wait"
        selected_action = None
        reason = "The discounted value of observing the modeled signal exceeds the best immediate option plus the utility buffer."
    elif best is not None and baseline > margin:
        recommendation = "act"
        selected_action = best["name"]
        reason = "The best immediate action exceeds abstention plus the utility buffer; waiting does not improve enough to justify its cost."
    else:
        recommendation = "abstain"
        selected_action = None
        reason = "No immediate action or modeled wait option clears the utility buffer above abstention."
    return {
        "scores": scores,
        "blocked_actions": blocked,
        "recommendation": recommendation,
        "selected_action": selected_action,
        "best_immediate_action": best["name"] if best else None,
        "baseline_utility": baseline,
        "expected_utility_after_signal": after_signal,
        "expected_value_of_information": information_value,
        "wait_utility": wait_utility,
        "signal_analysis": signal_analysis,
        "waiting_plan": {"signals": [signal["name"] for signal in model["signals"]],
                         "reconsider_when": model["reconsider_when"] or (
                             "Reconsider when a modeled signal is observed; recheck current constraints and costs."
                             if model["signals"] else "Specify an informative observation before choosing to wait."),
                         "observation_cost": model["observation_cost"], "delay_cost": model["delay_cost"],
                         "other_waiting_cost": model["waiting_cost"], "total_waiting_cost": total_waiting_cost},
        "reason": reason,
        "assumptions": [
            "Probabilities, payoffs, reliability of signals, and costs are caller-supplied assumptions, not verified facts.",
            "Utility equals mean net outcome minus risk_aversion times its variance; risk_aversion has inverse-utility units.",
            "Rollback cost applies when payoff minus cost is negative; the irreversibility charge is penalty times (1 - reversibility).",
            "Omitted reversibility defaults to zero; unknown recovery receives no reversibility credit.",
            "Signals are mutually exclusive and exhaustive; posterior probabilities use Bayes' rule.",
            "Abstention has zero utility; action ties prefer reversibility and then input order.",
            "Confidence margin is a utility buffer, not a confidence interval.",
            "Without explicit signals, waiting has zero information value and never assumes uncertainty falls on its own.",
            "Observation and delay costs are additional utility costs; waiting_cost covers other costs and must not double-count them.",
            "Runtime relationship constraints exclude actions from both immediate and posterior choices; coherence does not replace utility.",
        ],
    }


def demo_payload() -> dict[str, Any]:
    """An informative study makes waiting preferable to a risky launch or pilot."""
    return {
        "scenarios": [{"name": "Demand holds", "probability": 0.5},
                      {"name": "Demand fades", "probability": 0.5}],
        "actions": [{"name": "Full launch", "payoffs": [120, -100], "cost": 5,
                     "reversibility": 0.1, "rollback_cost": 10},
                    {"name": "Reversible pilot", "payoffs": [35, -10], "cost": 5,
                     "reversibility": 0.95, "rollback_cost": 2}],
        "signals": [{"name": "Strong study", "likelihoods": [0.9, 0.1]},
                    {"name": "Weak study", "likelihoods": [0.1, 0.9]}],
        "waiting_cost": 3,
        "discount": 0.95,
        "irreversibility_penalty": 8,
        "risk_aversion": 0.001,
        "confidence_margin": 1,
    }
