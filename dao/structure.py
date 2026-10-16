"""Conversation provenance and reviewable proposals for the relationship graph.

Historical snapshots are never migrated in place. Legacy message identifiers are
derived in read views, then materialized only in a normal new revision. Proposed
records are validated against exact passages and a graph digest before review.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import uuid

from .relationships import apply, digest, normalize_citations
from .store import ConflictError

MAX_OPERATIONS = 32
MAX_PROPOSALS = 100
MAX_PROPOSAL_BYTES = 128 * 1024


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def message_records(state):
    """Return detached messages with stable IDs, including old saved dialogue."""
    records = deepcopy(state.get("messages", []))
    for index, message in enumerate(records):
        if "id" not in message:
            identity = {"index": index, "role": message["role"], "content": message["content"]}
            message["id"] = "legacy-" + hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
    return records


def identify_messages(state):
    """Materialize read-view identities in a new state, never an old commit."""
    state["messages"] = message_records(state)


def citations_for(state, citations):
    """Bind half-open Unicode codepoint offsets to exact nonfailed dialogue."""
    records = message_records(state)
    messages = {message["id"]: message for message in records}
    if len(messages) != len(records):
        raise ValueError("Conversation message identifiers must be unique")
    result = normalize_citations(citations)
    for citation in result:
        message = messages.get(citation["message_id"])
        if message is None or message.get("status") == "failed":
            raise ValueError("Citation must reference a nonfailed message in this checkpoint")
        content = message["content"]
        if citation["end"] > len(content) or content[citation["start"]:citation["end"]] != citation["quote"]:
            raise ValueError("Citation offsets and quote must match the original message exactly")
        identity = {key: citation[key] for key in ("message_id", "start", "end", "quote")}
        passage_id = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
        if "passage_id" in citation and citation["passage_id"] != passage_id:
            raise ValueError("Citation passage_id does not match its source passage")
        citation["passage_id"] = passage_id
    return result


def grounded_operation(state, operation, *, required=False):
    """Normalize provenance; relationship.apply validates the remaining schema."""
    if not isinstance(operation, dict):
        raise ValueError("Relationship operation must be an object")
    operation = deepcopy(operation)
    kind = operation.get("operation")
    if not isinstance(kind, str):
        raise ValueError("Relationship operation must name an operation")
    record = operation.get(kind) if kind in {"node", "relation"} else operation
    if not isinstance(record, dict):
        raise ValueError("Relationship operation needs a record")
    if "citations" in record:
        record["citations"] = citations_for(state, record["citations"])
    if required and not record.get("citations"):
        raise ValueError("Every proposed operation requires at least one conversation citation")
    return operation


def _operations(state, operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise ValueError(f"A proposal needs 1..{MAX_OPERATIONS} operations")
    graph = state.get("relationships")
    normalized = []
    for operation in operations:
        if (not isinstance(operation, dict) or not isinstance(operation.get("operation"), str)
                or operation["operation"] not in {"node", "relation", "assess", "observe"}):
            raise ValueError("Proposals accept only node, relation, assess, and observe operations")
        operation = grounded_operation(state, operation, required=True)
        graph, _ = apply(graph, operation)
        normalized.append(operation)
    return normalized, graph


def propose(state, payload, *, source):
    """Stage a detached candidate; accepted relationships remain unchanged."""
    if not isinstance(payload, dict) or set(payload) != {"rationale", "operations"}:
        raise ValueError("A structure proposal accepts only rationale and operations")
    rationale = payload["rationale"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000:
        raise ValueError("Proposal rationale must be nonempty text up to 4000 characters")
    if len(_canonical(payload).encode("utf-8")) > MAX_PROPOSAL_BYTES:
        raise ValueError("Proposal exceeds 128 KiB")
    if source not in {"operator", "model"}:
        raise ValueError("Unknown proposal source")
    if len(state.get("proposals", [])) >= MAX_PROPOSALS:
        raise ValueError("Proposals are limited to 100 per branch state")
    operations, _ = _operations(state, payload["operations"])
    proposal = {"id": uuid.uuid4().hex, "status": "pending", "source": source,
                "created_at": _now(), "rationale": rationale.strip(),
                "base_digest": digest(state.get("relationships")), "operations": operations}
    state.setdefault("proposals", []).append(proposal)
    return deepcopy(proposal)


def review(state, proposal_id, decision):
    """Accept a whole candidate atomically, or retain it as a rejected proposal."""
    if not isinstance(proposal_id, str) or not isinstance(decision, str) or decision not in {"accept", "reject"}:
        raise ValueError("Review requires proposal_id and decision accept or reject")
    proposal = next((item for item in state.get("proposals", []) if item["id"] == proposal_id), None)
    if proposal is None:
        raise ValueError("Unknown structure proposal in this checkpoint")
    if proposal["status"] != "pending":
        raise ValueError("Only pending proposals can be reviewed")
    if decision == "accept":
        if proposal["base_digest"] != digest(state.get("relationships")):
            raise ConflictError("Relationships changed since this proposal; reject it and stage a fresh proposal")
        operations, graph = _operations(state, proposal["operations"])
        state["relationships"] = graph
        proposal["operations"] = operations
        proposal["accepted_digest"] = digest(graph)
    proposal["status"] = "accepted" if decision == "accept" else "rejected"
    proposal["reviewed_at"] = _now()
    return deepcopy(proposal)


def dependency_index(graph):
    """Find transitive dependents of each node; declarations are not deductions."""
    graph = graph or {}
    reverse = {identifier: set() for identifier in graph.get("nodes", {})}
    for relation in graph.get("relations", {}).values():
        if relation["kind"] == "depends_on":
            reverse[relation["target"]].add(relation["source"])
    result = {}
    for identifier in reverse:
        seen, pending = {identifier}, list(reverse[identifier])
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(reverse[current] - seen)
        result[identifier] = sorted(seen - {identifier})
    return result
