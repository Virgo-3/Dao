"""Branchable relationship beliefs backed by durable, claimed observations.

An observation is a report with a source and actor supplied by the caller. This
module records that provenance, but authentication and permission checks belong
at the API boundary. Applying a report updates a belief on one branch; the
report itself survives stale writes, restores, and divergent branch histories.
"""

from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
from typing import Any

from .numerics import float_distribution
from .store import ConflictError, NotFoundError, Store, canonical_json


RELATION_STATES = ("enhancing", "neutral", "degrading")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a nonempty string of at most {maximum} characters")
    return value.strip()


def _distribution(value: Any, label: str, *, normalize: bool) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(RELATION_STATES):
        raise ValueError(f"{label} must have enhancing, neutral, and degrading probabilities")
    result: dict[str, float] = {}
    for state in RELATION_STATES:
        number = value[state]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError(f"{label} probabilities must be finite numbers from 0 to 1")
        number = float(number)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError(f"{label} probabilities must be finite numbers from 0 to 1")
        result[state] = number
    total = sum(result.values())
    if normalize:
        if not math.isclose(total, 1.0, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f"{label} probabilities must sum to 1")
        return float_distribution(result, require_support=True)
    if total == 0:
        raise ValueError(f"{label} cannot assign zero likelihood to every state")
    return result


def _observed_time(value: str | None) -> str:
    if value is None:
        return _now()
    if not isinstance(value, str):
        raise ValueError("observed_at must be a timezone-aware ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be a timezone-aware ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must be a timezone-aware ISO 8601 timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class Experience:
    """Maintain relational forecasts and immutable reports of observations."""

    def __init__(self, store: Store):
        self.store = store
        with store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS observations ("
                "id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL, "
                "source TEXT NOT NULL, source_event_id TEXT, request_digest TEXT NOT NULL)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS unique_observations_source_event "
                "ON observations(source, source_event_id) WHERE source_event_id IS NOT NULL"
            )
            for operation in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS immutable_observations_{operation} "
                    f"BEFORE {operation} ON observations BEGIN "
                    "SELECT RAISE(ABORT, 'immutable observation'); END"
                )

    def register_relationship(
        self,
        branch: str,
        relationship_id: str,
        subject: str,
        object: str,
        dimension: str,
        prior: dict[str, float] | None,
        expected_head: str,
    ) -> dict[str, Any]:
        """Add a relationship to one branch, with uncertainty distinct from neutrality.

        A missing prior is explicitly unassessed. Uniform probabilities allow
        later Bayesian updates but are a modeling default, not observed fact.
        """
        relationship_id = _text(relationship_id, "relationship_id")
        subject = _text(subject, "subject")
        object = _text(object, "object")
        dimension = _text(dimension, "dimension")
        belief = (
            {state: 1 / len(RELATION_STATES) for state in RELATION_STATES}
            if prior is None
            else _distribution(prior, "prior", normalize=True)
        )
        timestamp = _now()
        relationship = {
            "id": relationship_id,
            "subject": subject,
            "object": object,
            "dimension": dimension,
            "belief": belief,
            "basis": "unassessed" if prior is None else "prior",
            "evidence_ids": [],
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        with self.store.transaction():
            state = self.store.snapshot(branch)
            relationships = state.setdefault("relationships", {})
            if not isinstance(relationships, dict):
                raise ValueError("State relationships must be an object")
            if relationship_id in relationships:
                raise ConflictError(f"Relationship already exists: {relationship_id}")
            relationships[relationship_id] = relationship
            head = self.store.commit(
                branch, state, f"Register relationship {relationship_id}", expected_head
            )
            self.store.append_event(
                "experience.relationship.registered",
                {"branch": branch, "relationship_id": relationship_id, "head": head},
            )
        return {"head": head, "relationship": relationship}

    def record_observation(
        self,
        relationship_id: str,
        signal: str,
        likelihoods: dict[str, float],
        source: str,
        actor: str,
        observed_at: str | None = None,
        note: str | None = None,
        proposal_id: str | None = None,
        source_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a report of a signal and P(signal | each relationship state).

        A source event ID makes transport retries idempotent even when the
        observed time is assigned here. Reusing it with changed input fails.
        Recording is independent of applying the report to a branch.
        """
        source = _text(source, "source", 512)
        supplied_time = None if observed_at is None else _observed_time(observed_at)
        request = {
            "relationship_id": _text(relationship_id, "relationship_id"),
            "signal": _text(signal, "signal"),
            "likelihoods": _distribution(likelihoods, "likelihoods", normalize=False),
            "source": source,
            "actor": _text(actor, "actor"),
            "observed_at": supplied_time,
            "note": None if note is None else _text(note, "note", 4096),
            "proposal_id": None if proposal_id is None else _text(proposal_id, "proposal_id"),
            "source_event_id": (
                None if source_event_id is None else _text(source_event_id, "source_event_id")
            ),
        }
        request_digest = _digest(request)
        with self.store.transaction() as db:
            if source_event_id is not None:
                existing = db.execute(
                    "SELECT payload, request_digest FROM observations "
                    "WHERE source = ? AND source_event_id = ?",
                    (source, request["source_event_id"]),
                ).fetchone()
                if existing is not None:
                    if existing["request_digest"] != request_digest:
                        raise ConflictError("source_event_id was already used with different content")
                    return json.loads(existing["payload"])
            body = {
                **request,
                "observed_at": supplied_time if supplied_time is not None else _now(),
                "request_digest": request_digest,
            }
            observation_id = _digest(body)
            row = db.execute(
                "SELECT payload FROM observations WHERE id = ?", (observation_id,)
            ).fetchone()
            if row is not None:
                return json.loads(row["payload"])
            record = {"id": observation_id, **body, "created_at": _now()}
            db.execute(
                "INSERT INTO observations "
                "(id, payload, created_at, source, source_event_id, request_digest) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    observation_id,
                    canonical_json(record),
                    record["created_at"],
                    source,
                    request["source_event_id"],
                    request_digest,
                ),
            )
            self.store.append_event("experience.observed", record)
            return record

    def get_observation(self, observation_id: str) -> dict[str, Any]:
        observation_id = _text(observation_id, "observation_id")
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT payload FROM observations WHERE id = ?", (observation_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Observation not found: {observation_id}")
        return json.loads(row["payload"])

    def find_observation(self, source: str, source_event_id: str) -> dict[str, Any] | None:
        """Find a prior report by the caller's stable retry key, if one exists."""
        source = _text(source, "source", 512)
        source_event_id = _text(source_event_id, "source_event_id")
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT payload FROM observations WHERE source = ? AND source_event_id = ?",
                (source, source_event_id),
            ).fetchone()
        return None if row is None else json.loads(row["payload"])

    def list_observations(self, limit: int = 50) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT payload FROM observations ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def relationship(self, branch: str, relationship_id: str) -> dict[str, Any]:
        relationship_id = _text(relationship_id, "relationship_id")
        state = self.store.snapshot(branch)
        relationships = state.get("relationships", {})
        if not isinstance(relationships, dict) or relationship_id not in relationships:
            raise NotFoundError(f"Relationship not found: {relationship_id}")
        return relationships[relationship_id]

    def apply_observation(
        self, branch: str, observation_id: str, expected_head: str
    ) -> dict[str, Any]:
        """Update one branch by Bayes' rule; duplicate application is a no-op.

        Replaying an already applied record returns the current head even if the
        supplied head is stale. A new application always requires the current
        head. The globally stored report survives a rejected new application.
        """
        if not isinstance(expected_head, str) or not expected_head:
            raise ValueError("expected_head must be the previously observed commit id")
        observation = self.get_observation(observation_id)
        with self.store.transaction():
            head = self.store.head(branch)
            state = self.store.snapshot(branch)
            relationships = state.get("relationships", {})
            relation = relationships.get(observation["relationship_id"])
            if relation is None:
                raise NotFoundError(
                    f"Relationship not found: {observation['relationship_id']}"
                )
            if observation_id in relation["evidence_ids"]:
                return {"head": head, "relationship": relation, "applied": False}
            if head != expected_head:
                raise ConflictError(f"Stale branch {branch}: expected {expected_head}, found {head}")
            prior = relation["belief"]
            # Conditioning can produce representable probabilities even when
            # every unnormalized product is smaller than a binary64 number.
            weights = {
                state_name: Fraction(prior[state_name])
                * Fraction(observation["likelihoods"][state_name])
                for state_name in RELATION_STATES
            }
            marginal = sum(weights.values())
            if marginal <= 0:
                raise ValueError("Observation has zero probability under the current belief")
            # A branch persists floats. Reject a positive posterior that cannot
            # be represented there before changing the belief or evidence IDs.
            posterior = float_distribution(weights, require_support=True)
            relation["belief"] = posterior
            relation["basis"] = "observation"
            relation["evidence_ids"].append(observation_id)
            relation["updated_at"] = _now()
            committed = self.store.commit(
                branch,
                state,
                f"Apply observation {observation_id} to {observation['relationship_id']}",
                expected_head,
            )
            self.store.append_event(
                "experience.applied",
                {
                    "branch": branch,
                    "relationship_id": observation["relationship_id"],
                    "observation_id": observation_id,
                    "before": head,
                    "head": committed,
                    "prior": prior,
                    "posterior": posterior,
                },
            )
        return {"head": committed, "relationship": relation, "applied": True}

    def verify_integrity(self) -> dict[str, Any]:
        """Check observation rows against their append-only events and triggers."""
        report = self.store.verify_integrity()
        issues = list(report["issues"])
        with self.store.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='unique_observations_source_event'"
            ).fetchone():
                issues.append("Missing observation source-event uniqueness index")
            for operation in ("UPDATE", "DELETE"):
                trigger = f"immutable_observations_{operation}"
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
                ).fetchone():
                    issues.append("Missing observation append-only trigger: " + trigger)
            try:
                events = [
                    json.loads(row["payload"])
                    for row in db.execute(
                        "SELECT payload FROM events WHERE kind='experience.observed' ORDER BY seq"
                    )
                ]
                rows = db.execute(
                    "SELECT id, payload, created_at, source, source_event_id, request_digest "
                    "FROM observations ORDER BY rowid"
                ).fetchall()
                records = [json.loads(row["payload"]) for row in rows]
            except (ValueError, TypeError):
                issues.append("Invalid observation projection")
                return {**report, "ok": False, "issues": issues}
            try:
                if sorted(canonical_json(item) for item in records) != sorted(
                    canonical_json(item) for item in events
                ):
                    issues.append("Observation projection differs from event chain")
                for row, record in zip(rows, records):
                    if not isinstance(record, dict):
                        issues.append("Invalid observation record: " + row["id"])
                        continue
                    if row["id"] != record.get("id") or row["created_at"] != record.get(
                        "created_at"
                    ):
                        issues.append("Observation index differs from payload: " + row["id"])
                    if (
                        row["source"] != record.get("source")
                        or row["source_event_id"] != record.get("source_event_id")
                        or row["request_digest"] != record.get("request_digest")
                    ):
                        issues.append("Observation retry index differs from payload: " + row["id"])
                    body = {
                        key: value
                        for key, value in record.items()
                        if key not in ("id", "created_at")
                    }
                    if record.get("id") != _digest(body):
                        issues.append("Observation digest mismatch: " + str(record.get("id")))
            except (ValueError, TypeError):
                issues.append("Invalid observation projection")
        return {**report, "ok": not issues, "issues": issues}
