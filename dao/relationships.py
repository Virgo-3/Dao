"""Versioned, directed relationship beliefs and persistent conflict cases.

Coherence is a conditional diagnostic, not an objective or an authorization.
Definitions and weights cannot be edited or deleted; reassessment cannot erase
an adverse case. Observed transitions describe submitted observations and do
not establish causal action effects, calibrated probabilities, or truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from .audit import adjudicate

SCHEMA = "dao-relationships-v1"
STATES = ("positive", "neutral", "negative")
MAX_NODES = 100
MAX_RELATIONS = 256
MAX_OBSERVATIONS = 512
MAX_CONFLICT_HISTORY = 128
MAX_GRAPH_BYTES = 2_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")


def empty_graph() -> dict[str, Any]:
    return {"schema": SCHEMA, "nodes": {}, "relations": {}, "conflicts": {}, "observations": []}


def _object(value: Any, field: str, allowed: set[str], required: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be an object with string field names")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{field} contains unknown fields: {', '.join(sorted(unknown))}")
    missing = (allowed if required is None else required) - set(value)
    if missing:
        raise ValueError(f"{field} is missing fields: {', '.join(sorted(missing))}")
    return value


def _text(value: Any, field: str, limit: int = 2048, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a nonempty string of at most {limit} characters")
    text = value.strip()
    if any((ord(character) < 32 and not (multiline and character in "\r\n\t")) or ord(character) == 127
           for character in text):
        raise ValueError(f"{field} cannot contain control characters")
    return text


def _identifier(value: Any, field: str) -> str:
    text = _text(value, field, 64)
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"{field} must be an identifier using letters, numbers, _, ., :, or -")
    return text


def _number(value: Any, field: str, *, unit: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number) or (not 0 <= number <= 1 if unit else not 0 < number <= 1_000_000):
        raise ValueError(f"{field} must be between 0 and 1" if unit else f"{field} must be positive and at most 1000000")
    return number


def _choice(value: Any, field: str, choices: tuple[str, ...] | set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{field} must be one of {', '.join(sorted(choices))}")
    return value


def _belief(value: Any) -> dict[str, float] | None:
    if value is None:
        return None
    value = _object(value, "belief", set(STATES))
    result = {state: _number(value[state], f"belief.{state}", unit=True) for state in STATES}
    total = math.fsum(result.values())
    if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-9):
        raise ValueError("belief probabilities must sum to 1")
    return {state: probability / total for state, probability in result.items()}


def _actions(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("actions must be an array of at most 32 action names")
    result = [_text(action, "action name", 200) for action in value]
    if len(result) != len(set(result)):
        raise ValueError("action names must be distinct")
    return result


def _node(value: Any, *, stored: bool = False) -> dict[str, Any]:
    fields = {"id", "label", "kind", "importance"} | ({"created_at"} if stored else set())
    value = _object(value, "node", fields)
    result = {"id": _identifier(value["id"], "node.id"), "label": _text(value["label"], "node.label", 256),
              "kind": _choice(value["kind"], "node.kind", {"person", "goal", "claim", "action"}),
              "importance": _number(value["importance"], "node.importance")}
    if stored:
        result["created_at"] = _text(value["created_at"], "created_at", 64)
    return result


def _relation(value: Any, *, stored: bool = False) -> dict[str, Any]:
    fields = {"id", "source", "target", "kind", "weight", "severe", "actions"}
    if stored:
        fields |= {"created_at", "belief", "assessment"}
    value = _object(value, "relation", fields)
    if not isinstance(value["severe"], bool):
        raise ValueError("relation.severe must be a boolean")
    result = {"id": _identifier(value["id"], "relation.id"),
              "source": _identifier(value["source"], "relation.source"),
              "target": _identifier(value["target"], "relation.target"),
              "kind": _choice(value["kind"], "relation.kind", {"effect", "support", "compatibility", "resource"}),
              "weight": _number(value["weight"], "relation.weight"), "severe": value["severe"],
              "actions": _actions(value["actions"])}
    if stored:
        result["created_at"] = _text(value["created_at"], "created_at", 64)
        result["belief"] = _belief(value["belief"])
        result["assessment"] = value["assessment"]
        if value["assessment"] is not None:
            assessment = _object(value["assessment"], "assessment", {"source", "content", "at"})
            _text(assessment["source"], "assessment.source", 256)
            _text(assessment["content"], "assessment.content", multiline=True)
            _text(assessment["at"], "assessment.at", 64)
        elif result["belief"] is not None:
            raise ValueError("an assessed belief requires provenance")
    return result


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("graph must contain finite, bounded JSON values") from exc


def _checked_graph(graph: Any) -> dict[str, Any]:
    """Validate stored state and detach it before any mutation or returned view."""
    if graph is None:
        graph = empty_graph()
    _object(graph, "graph", {"schema", "nodes", "relations", "conflicts", "observations"})
    if graph["schema"] != SCHEMA:
        raise ValueError("unsupported relationship graph schema")
    for field, maximum in (("nodes", MAX_NODES), ("relations", MAX_RELATIONS), ("conflicts", MAX_RELATIONS)):
        if not isinstance(graph[field], dict) or len(graph[field]) > maximum:
            raise ValueError(f"{field} must be an object with at most {maximum} entries")
    if not isinstance(graph["observations"], list) or len(graph["observations"]) > MAX_OBSERVATIONS:
        raise ValueError(f"observations must be an array of at most {MAX_OBSERVATIONS} entries")
    canonical = _canonical(graph)
    if len(canonical.encode("utf-8")) > MAX_GRAPH_BYTES:
        raise ValueError("relationship graph exceeds its storage limit")
    result = json.loads(canonical)
    for identifier, value in result["nodes"].items():
        if _node(value, stored=True)["id"] != identifier:
            raise ValueError("node map key must match its id")
    for identifier, value in result["relations"].items():
        relation = _relation(value, stored=True)
        if relation["id"] != identifier:
            raise ValueError("relation map key must match its id")
        if relation["source"] not in result["nodes"] or relation["target"] not in result["nodes"]:
            raise ValueError("relation endpoints must name existing nodes")
    for identifier, conflict in result["conflicts"].items():
        _object(conflict, "conflict", {"relation_id", "status", "opened_at", "updated_at", "history"})
        if identifier not in result["relations"] or conflict["relation_id"] != identifier:
            raise ValueError("conflict must name an existing relation")
        _choice(conflict["status"], "conflict.status", {"unresolved", "resolved"})
        for field in ("opened_at", "updated_at"):
            _text(conflict[field], f"conflict.{field}", 64)
        if not isinstance(conflict["history"], list) or not 1 <= len(conflict["history"]) <= MAX_CONFLICT_HISTORY:
            raise ValueError("conflict history is outside its allowed bounds")
        previous = None
        for entry in conflict["history"]:
            _object(entry, "conflict history entry", {"at", "event", "source", "verdict_id"}, {"at", "event"})
            _text(entry["at"], "conflict history time", 64)
            event = _choice(entry["event"], "conflict history event", {"opened", "reopened", "resolved"})
            expected = "opened" if previous is None else "reopened" if previous == "resolved" else "resolved"
            if event != expected:
                raise ValueError("conflict history must alternate opening and adjudicated resolution")
            if event != "resolved":
                if "source" not in entry or "verdict_id" in entry:
                    raise ValueError("opening a conflict requires source provenance")
                _text(entry["source"], "conflict history source", 256)
            else:
                if "source" in entry or not isinstance(entry.get("verdict_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["verdict_id"]):
                    raise ValueError("resolved conflict history requires an adjudication digest")
            previous = event
        if (conflict["status"] == "resolved") != (previous == "resolved"):
            raise ValueError("conflict status must match its history")
        if conflict["opened_at"] != conflict["history"][0]["at"]:
            raise ValueError("conflict opening time must match its history")
    latest_observations = {}
    for observation in result["observations"]:
        _object(observation, "observation", {"relation_id", "action", "context", "before", "after", "source", "content", "at"})
        if observation["relation_id"] not in result["relations"]:
            raise ValueError("observation must name an existing relation")
        for field, limit in (("action", 200), ("context", 256), ("source", 256), ("content", 2048), ("at", 64)):
            _text(observation[field], f"observation.{field}", limit, multiline=field == "content")
        for field in ("before", "after"):
            _choice(observation[field], f"observation.{field}", STATES)
        if "negative" in (observation["before"], observation["after"]) and observation["relation_id"] not in result["conflicts"]:
            raise ValueError("adverse observations require a persistent conflict case")
        latest_observations[observation["relation_id"]] = observation
    for identifier, relation in result["relations"].items():
        adverse_belief = relation["belief"] is not None and relation["belief"]["negative"] > 0
        adverse_outcome = identifier in latest_observations and latest_observations[identifier]["after"] == "negative"
        if (adverse_belief or adverse_outcome) and (identifier not in result["conflicts"] or result["conflicts"][identifier]["status"] != "unresolved"):
            raise ValueError("current adverse assessments and outcomes require an unresolved conflict")
    return result


def digest(graph: Any) -> str:
    return hashlib.sha256(_canonical(_checked_graph(graph)).encode("utf-8")).hexdigest()


def resolution_claim(graph: Any, relation_id: str) -> str:
    graph = _checked_graph(graph)
    relation_id = _identifier(relation_id, "relation_id")
    if relation_id not in graph["relations"]:
        raise ValueError("unknown relation_id")
    return f"Resolve relationship conflict {relation_id} at graph {digest(graph)}"


def _conflict(graph: dict[str, Any], relation_id: str, source: str, at: str) -> None:
    conflict = graph["conflicts"].get(relation_id)
    if conflict is None:
        graph["conflicts"][relation_id] = {"relation_id": relation_id, "status": "unresolved", "opened_at": at,
                                            "updated_at": at, "history": [{"at": at, "event": "opened", "source": source}]}
    elif conflict["status"] == "resolved":
        if len(conflict["history"]) >= MAX_CONFLICT_HISTORY:
            raise ValueError("conflict history limit reached")
        conflict["status"] = "unresolved"
        conflict["updated_at"] = at
        conflict["history"].append({"at": at, "event": "reopened", "source": source})
    else:
        conflict["updated_at"] = at


def apply(graph: Any, payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one validated mutation without changing input state or payload.

    The service supplies resolve.verdict after adjudicating the exact current
    resolution_claim. Reassessment and observations are caller submissions.
    """
    graph = _checked_graph(graph)
    if not isinstance(payload, dict):
        raise ValueError("relationship operation must be an object")
    operation = _choice(payload.get("operation"), "operation", {"node", "relation", "assess", "observe", "resolve"})
    fields = {"node": {"node"}, "relation": {"relation"},
              "assess": {"relation_id", "belief", "source", "content"},
              "observe": {"relation_id", "action", "context", "before", "after", "source", "content"},
              "resolve": {"relation_id", "verdict"}}[operation] | {"operation"}
    payload = _object(payload, "relationship operation", fields)
    at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    event: dict[str, Any] = {"operation": operation, "at": at}
    if operation == "node":
        node = _node(payload["node"])
        if node["id"] in graph["nodes"]:
            raise ValueError("node definitions are immutable; id already exists")
        if len(graph["nodes"]) >= MAX_NODES:
            raise ValueError("node limit reached")
        node["created_at"] = at
        graph["nodes"][node["id"]] = node
        event["node_id"] = node["id"]
    elif operation == "relation":
        relation = _relation(payload["relation"])
        if relation["id"] in graph["relations"]:
            raise ValueError("relation definitions are immutable; id already exists")
        if relation["source"] not in graph["nodes"] or relation["target"] not in graph["nodes"]:
            raise ValueError("relation endpoints must name existing nodes")
        if len(graph["relations"]) >= MAX_RELATIONS:
            raise ValueError("relation limit reached")
        relation.update(created_at=at, belief=None, assessment=None)
        graph["relations"][relation["id"]] = relation
        event["relation_id"] = relation["id"]
    else:
        relation_id = _identifier(payload["relation_id"], "relation_id")
        if relation_id not in graph["relations"]:
            raise ValueError("unknown relation_id")
        relation = graph["relations"][relation_id]
        event["relation_id"] = relation_id
        if operation == "assess":
            belief = _belief(payload["belief"])
            source = _text(payload["source"], "source", 256)
            content = _text(payload["content"], "content", multiline=True)
            relation["belief"] = belief
            relation["assessment"] = {"source": source, "content": content, "at": at}
            event["belief"] = belief
            if belief is not None and belief["negative"] > 0:
                _conflict(graph, relation_id, source, at)
        elif operation == "observe":
            if len(graph["observations"]) >= MAX_OBSERVATIONS:
                raise ValueError("observation limit reached")
            observation = {"relation_id": relation_id, "at": at,
                           "action": _text(payload["action"], "action", 200),
                           "context": _text(payload["context"], "context", 256),
                           "before": _choice(payload["before"], "before", STATES),
                           "after": _choice(payload["after"], "after", STATES),
                           "source": _text(payload["source"], "source", 256),
                           "content": _text(payload["content"], "content", multiline=True)}
            graph["observations"].append(observation)
            event["observation_index"] = len(graph["observations"]) - 1
            if observation["before"] == "negative" or observation["after"] == "negative":
                _conflict(graph, relation_id, observation["source"], at)
        else:
            conflict = graph["conflicts"].get(relation_id)
            if conflict is None or conflict["status"] != "unresolved":
                raise ValueError("relation has no unresolved conflict")
            belief = relation["belief"]
            if belief is None or belief["negative"] != 0:
                raise ValueError("resolution requires a current assessed nonadverse belief")
            latest = next((item for item in reversed(graph["observations"]) if item["relation_id"] == relation_id), None)
            if latest is not None and latest["after"] == "negative":
                raise ValueError("latest observed outcome is adverse and must be resolved by a newer observation")
            verdict = payload["verdict"]
            if not isinstance(verdict, dict) or "normalized_payload" not in verdict:
                raise ValueError("resolution requires an evidence adjudication verdict")
            checked = adjudicate(verdict["normalized_payload"])
            if verdict != checked or checked["allowed"] is not True or checked["claim"] != resolution_claim(graph, relation_id):
                raise ValueError("resolution verdict must authorize this exact current graph and relation")
            if len(conflict["history"]) >= MAX_CONFLICT_HISTORY:
                raise ValueError("conflict history limit reached")
            conflict["status"] = "resolved"
            conflict["updated_at"] = at
            conflict["history"].append({"at": at, "event": "resolved", "verdict_id": checked["verdict_id"]})
            event["verdict_id"] = checked["verdict_id"]
    return _checked_graph(graph), json.loads(_canonical(event))


def _conflict_view(graph: dict[str, Any], conflict: dict[str, Any]) -> dict[str, Any]:
    relation = graph["relations"][conflict["relation_id"]]
    return {**conflict, "severe": relation["severe"], "actions": relation["actions"],
            "source": relation["source"], "target": relation["target"], "kind": relation["kind"]}


def blocking_conflicts(graph: Any, action: str | None = None) -> list[dict[str, Any]]:
    graph = _checked_graph(graph)
    if action is not None:
        action = _text(action, "action", 200)
    return [_conflict_view(graph, conflict) for identifier, conflict in graph["conflicts"].items()
            if conflict["status"] == "unresolved" and graph["relations"][identifier]["severe"]
            and (action is None or not graph["relations"][identifier]["actions"]
                 or action in graph["relations"][identifier]["actions"])]


def _transitions(graph: dict[str, Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for observation in graph["observations"]:
        key = observation["relation_id"], observation["action"], observation["context"]
        groups.setdefault(key, []).append(observation)
    models = []
    for (relation_id, action, context), observations in sorted(groups.items()):
        rows = {}
        for before in STATES:
            counts = {after: sum(item["before"] == before and item["after"] == after for item in observations)
                      for after in STATES}
            total = sum(counts.values()) + len(STATES)
            parameters = {state: count + 1 for state, count in counts.items()}
            rows[before] = {"counts": counts, "observation_count": sum(counts.values()),
                            "posterior_mean": {state: alpha / total for state, alpha in parameters.items()},
                            "posterior_variance": {state: alpha * (total - alpha) / (total * total * (total + 1))
                                                   for state, alpha in parameters.items()}}
        models.append({"relation_id": relation_id, "action": action, "context": context, "rows": rows,
                       "observation_count": len(observations), "prior": {state: 1 for state in STATES},
                       "assumptions": ["Dirichlet prior of one pseudo-count per destination state.",
                                       "Submitted observations are grouped by exact relation, action name, and context.",
                                       "These empirical associations do not establish causal effects or calibrated predictions."]})
    return models


def summarize(graph: Any) -> dict[str, Any]:
    graph = _checked_graph(graph)
    masses = {state: [] for state in (*STATES, "unknown")}
    for relation in graph["relations"].values():
        belief = relation["belief"]
        if belief is None:
            masses["unknown"].append(relation["weight"])
        else:
            for state in STATES:
                masses[state].append(relation["weight"] * belief[state])
    weights = {state: math.fsum(values) for state, values in masses.items()}
    total = math.fsum(relation["weight"] for relation in graph["relations"].values())
    assessed = math.fsum(relation["weight"] for relation in graph["relations"].values() if relation["belief"] is not None)
    active = weights["positive"] + weights["negative"]
    return {"schema": SCHEMA, "node_count": len(graph["nodes"]), "relation_count": len(graph["relations"]),
            "total_weight": total, **{f"{state}_weight": weight for state, weight in weights.items()},
            "coherence": weights["positive"] / active if active else None,
            "coverage": assessed / total if total else 0.0,
            "fractions": {state: weight / total if total else 0.0 for state, weight in weights.items()},
            "unresolved_conflicts": [_conflict_view(graph, conflict) for conflict in graph["conflicts"].values()
                                     if conflict["status"] == "unresolved"],
            "transition_models": _transitions(graph),
            "assumptions": ["Weights and relation definitions are fixed within this versioned reference scope.",
                            "Beliefs, evidence provenance, and observations are caller supplied, not verified world facts.",
                            "Coherence is conditional on assessed active relations and cannot authorize an action.",
                            "Assessment as neutral or unknown cannot close an unresolved conflict."]}
