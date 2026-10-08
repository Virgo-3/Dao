"""Illustrative story comparison, using the shared decision engine."""

from typing import Any


def demo_payload() -> dict[str, Any]:
    """Illustrative story choices; the numbers are assumptions, not literary scores."""
    return {
        "scenarios": [{"name": "The reveal feels earned", "probability": 0.5},
                      {"name": "The reveal needs setup", "probability": 0.5}],
        "actions": [{"name": "Commit to the ending", "payoffs": [120, -100], "cost": 5,
                     "reversibility": 0.1, "rollback_cost": 10},
                    {"name": "Try an alternate scene", "payoffs": [35, -10], "cost": 5,
                     "reversibility": 0.95, "rollback_cost": 2}],
        "signals": [{"name": "Reader finds the reveal earned", "likelihoods": [0.9, 0.1]},
                    {"name": "Reader asks for more setup", "likelihoods": [0.1, 0.9]}],
        "waiting_cost": 3,
        "discount": 0.95,
        "irreversibility_penalty": 8,
        "risk_aversion": 0.001,
        "confidence_margin": 1,
    }
