"""Project an evidence-backed relationship belief into a one-step decision problem.

The relationship is an uncertain description of the current situation.  It is
not itself a utility function: callers must supply the consequences of each
action for each possible relational state.  This compiler deliberately leaves
relationship metadata outside the decision input, whose schema is closed.
"""

from __future__ import annotations

import math
from typing import Any

from .decision import evaluate


RELATIONSHIP_STATES = ("enhancing", "neutral", "degrading")


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _validate_relationship(relationship: Any) -> dict[str, Any]:
    if not isinstance(relationship, dict):
        raise ValueError("relationship must be an object")
    for field in ("id", "subject", "object", "dimension", "updated_at"):
        _nonempty_string(relationship.get(field), f"relationship.{field}")
    evidence_ids = relationship.get("evidence_ids")
    if not isinstance(evidence_ids, list):
        raise ValueError("relationship.evidence_ids must be a list")
    seen: set[str] = set()
    for index, evidence_id in enumerate(evidence_ids):
        evidence_id = _nonempty_string(evidence_id, f"relationship.evidence_ids[{index}]")
        if evidence_id in seen:
            raise ValueError("relationship.evidence_ids must be unique")
        seen.add(evidence_id)
    belief = relationship.get("belief")
    if not isinstance(belief, dict) or set(belief) != set(RELATIONSHIP_STATES):
        raise ValueError("relationship.belief must contain exactly the three relationship states")
    return belief


def compile_relationship_problem(
    relationship: dict[str, Any],
    actions: list[dict[str, Any]],
    signals: list[dict[str, Any]] | None = None,
    wait_cost: float = 0,
    policy: dict[str, Any] | None = None,
    utility_unit: str = "modeled utility units",
) -> dict[str, Any]:
    """Return a canonical problem accepted by :func:`dao.decision.evaluate`.

    ``belief`` is a probability distribution over enhancing, neutral, and
    degrading states.  Action ``utilities`` describe outcomes *conditional on*
    those states; they must be supplied explicitly rather than inferred from
    their labels.  Optional signals describe a single exhaustive observation,
    which the decision evaluator uses to compare acting now with waiting for
    information.  Callers retain the relationship ID, evidence, and version
    separately when recording the resulting decision.

    This is a one-step projection.  It does not assume that merely observing a
    relationship improves it or that choosing to wait leaves the world fixed.
    """
    belief = _validate_relationship(relationship)
    problem = {
        "scenarios": [
            {"id": state, "probability": belief[state]} for state in RELATIONSHIP_STATES
        ],
        "actions": actions,
        "signals": [] if signals is None else signals,
        "wait_cost": wait_cost,
        "policy": {} if policy is None else policy,
        "utility_unit": utility_unit,
    }
    # The public evaluator is the sole authority for numeric ranges, finite
    # arithmetic, signal partitions, reserved IDs, and policy validation.
    # Returning its canonical inputs also ensures deterministic replay.
    return evaluate(problem)["inputs"]


def coherence_diagnostic(relationships: list[dict[str, Any]]) -> dict[str, Any]:
    """Report the ratio of expected enhancing to active relationship counts.

    This is a scoped diagnostic, not a utility or an expected ratio of actual
    outcomes. Unassessed relationships have no evidenced prior and are omitted
    from the numerator and denominator while remaining visible in coverage.
    """
    if not isinstance(relationships, list):
        raise ValueError("relationships must be a list")
    ids: set[str] = set()
    assessed = []
    unassessed = []
    for relationship in relationships:
        belief = _validate_relationship(relationship)
        identifier = relationship["id"]
        if identifier in ids:
            raise ValueError("relationship IDs must be unique")
        ids.add(identifier)
        evaluate({
            "scenarios": [
                {"id": state, "probability": belief[state]}
                for state in RELATIONSHIP_STATES
            ],
            "actions": [],
        })
        if relationship.get("basis") == "unassessed":
            unassessed.append(identifier)
        else:
            assessed.append(relationship)
    enhancing = math.fsum(item["belief"]["enhancing"] for item in assessed)
    degrading = math.fsum(item["belief"]["degrading"] for item in assessed)
    active = enhancing + degrading
    return {
        "scope_ids": sorted(ids),
        "assessed_count": len(assessed),
        "unassessed_ids": sorted(unassessed),
        "expected_enhancing_count": enhancing,
        "expected_degrading_count": degrading,
        "expected_active_count": active,
        "expected_count_ratio": None if active == 0 else enhancing / active,
        "meaning": "ratio of expected counts in the stated scope; diagnostic only",
    }
