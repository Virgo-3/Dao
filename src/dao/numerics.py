"""Exact probability arithmetic with explicit limits at the JSON float boundary."""

from fractions import Fraction
import math
from typing import Mapping


def exact_distribution(values: Mapping[str, float | Fraction]) -> dict[str, Fraction]:
    """Normalize already-validated weights without erasing positive support."""
    weights = {key: Fraction(value) for key, value in values.items()}
    total = sum(weights.values(), Fraction(0))
    if total <= 0:
        raise ValueError("probability weights must have a positive total")
    return {key: value / total for key, value in weights.items()}


def float_distribution(
    values: Mapping[str, float | Fraction], *, require_support: bool = False
) -> dict[str, float]:
    """Round normalized weights for JSON; optionally forbid lost positive support.

    Pure planners retain exact weights internally and expose support separately.
    Persisted beliefs must reject an unrepresentable posterior before committing,
    so later computations cannot confuse rounded zeros with impossible states.
    """
    exact = exact_distribution(values)
    result = {key: float(value) for key, value in exact.items()}
    if require_support and any(exact[key] > 0 and value == 0 for key, value in result.items()):
        raise ValueError("probability exceeds numeric range for preserving positive support")
    pivot = max(result, key=result.get)
    for _ in range(4):
        residual = 1.0 - math.fsum(result.values())
        if residual == 0:
            return result
        result[pivot] += residual
    raise ValueError("numerical instability in probability normalization")
