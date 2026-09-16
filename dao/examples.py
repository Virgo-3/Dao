"""Inspectable assumptions for the built-in launch scenario."""


def launch_decision():
    return {
        "title": "Launch now, run a pilot, or wait for evidence?",
        "states": [{"id": "demand", "probability": 0.5},
                   {"id": "no_demand", "probability": 0.5}],
        "actions": [
            {"id": "launch", "label": "Launch now", "payoffs": {"demand": 100, "no_demand": -60},
             "reversibility": 0.1, "rollback_cost": 0, "commitment_cost": 20},
            {"id": "pilot", "label": "Run a small pilot", "payoffs": {"demand": 20, "no_demand": 10},
             "reversibility": 0.9, "rollback_cost": 0, "commitment_cost": 2},
            {"id": "hold", "label": "Keep the current plan", "payoffs": {"demand": 0, "no_demand": 0},
             "reversibility": 1, "rollback_cost": 0, "commitment_cost": 0}],
        "risk_aversion": 0, "irreversibility_aversion": 0,
        "wait": {"cost": 5, "discount": 1, "signals": [
            {"id": "positive", "likelihoods": {"demand": 0.8, "no_demand": 0.2}},
            {"id": "negative", "likelihoods": {"demand": 0.2, "no_demand": 0.8}}]}}


def initial_state():
    return {"schema_version": 1, "messages": [], "notes": {},
            "decision": launch_decision(), "choices": [], "observations": []}
