"""Adjudication is an executable authorization boundary, not a log label."""

import json
import hashlib
import math
import uuid
from datetime import datetime, timezone
from fractions import Fraction

from .decision import evaluate
from .numerics import exact_distribution, float_distribution
from .temporal import plan_temporal
from .store import ConflictError, NotFoundError, canonical_json


def canonical(value):
    return canonical_json(value)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def validate_patch(patch):
    if not isinstance(patch, dict) or set(patch) - {"set", "delete"}:
        raise ValueError("patch accepts only set and delete")
    changes, deletions = patch.get("set", {}), patch.get("delete", [])
    if not isinstance(changes, dict) or not isinstance(deletions, list):
        raise ValueError("set must be an object; delete must be a list")
    keys = list(changes) + deletions
    if any(not isinstance(k, str) or not k or len(k) > 256 for k in keys):
        raise ValueError("memory keys must be nonempty strings of at most 256 characters")
    if len(set(deletions)) != len(deletions) or set(changes) & set(deletions):
        raise ValueError("duplicate or overlapping patch keys")
    canonical(patch)
    return {"set": changes, "delete": deletions}


def validate_evidence_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {
        "relationship_id", "source", "question", "deadline", "max_cost_usd"
    }:
        raise ValueError(
            "evidence plan requires relationship_id, source, question, deadline, max_cost_usd"
        )
    if any(not isinstance(plan[key], str) or not plan[key].strip()
           for key in ("relationship_id", "source", "question", "deadline")):
        raise ValueError("evidence plan identity, source, question, and deadline must be strings")
    amount = plan["max_cost_usd"]
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError("max_cost_usd must be a finite nonnegative number")
    try:
        amount = float(amount)
    except (ValueError, OverflowError) as exc:
        raise ValueError("max_cost_usd must be a finite nonnegative number") from exc
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("max_cost_usd must be a finite nonnegative number")
    try:
        deadline = datetime.fromisoformat(plan["deadline"])
    except ValueError as exc:
        raise ValueError("deadline must be an ISO-8601 timestamp") from exc
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("deadline must include a timezone")
    if deadline <= datetime.now(timezone.utc):
        raise ValueError("evidence plan has expired")
    result = {**plan, "deadline": deadline.isoformat(), "max_cost_usd": amount}
    canonical(result)
    return result


def _plan_is_current(proposal):
    try:
        deadline = datetime.fromisoformat(proposal["evidence_plan"]["deadline"])
    except (KeyError, ValueError) as exc:
        raise ConflictError("invalid evidence plan deadline") from exc
    if deadline <= datetime.now(timezone.utc):
        raise ConflictError("evidence plan has expired; reassess and propose again")


class Audit:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, branch TEXT NOT NULL, base TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS rulings (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL,
                verdict TEXT NOT NULL, payload TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS executions (
                proposal_id TEXT PRIMARY KEY, payload TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS outcome_reviews (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL, payload TEXT NOT NULL)""")
            for table in ("external_claims", "external_results"):
                db.execute(f"CREATE TABLE IF NOT EXISTS {table} "
                           "(proposal_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            for table in ("proposals", "rulings", "executions", "outcome_reviews",
                          "external_claims", "external_results"):
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation}
                        BEFORE {operation} ON {table}
                        BEGIN SELECT RAISE(ABORT, 'immutable audit record'); END""")

    def propose(self, branch, problem, patch, expected_head, rationale=None):
        patch = validate_patch(patch)
        evaluation = evaluate(problem)
        proposal_id = uuid.uuid4().hex
        with self.store.transaction() as db:
            state = self.store.snapshot(branch)
            state["decisions"][proposal_id] = evaluation
            base = self.store.commit(branch, state, "Record decision " + proposal_id, expected_head)
            proposal = {
                "id": proposal_id,
                "branch": branch,
                "base": base,
                "evaluation": evaluation,
                "patch": patch,
                "rationale": rationale,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.execute(
                "INSERT INTO proposals VALUES (?, ?, ?, ?, ?)",
                (proposal_id, branch, base, canonical(proposal), proposal["created_at"]),
            )
            self.store.append_event("action.proposed", proposal)
            return proposal

    def propose_external(self, branch, problem, effect, expected_head, rationale=None):
        """Review a concrete executor request, including its external preconditions."""
        from .executors import validate_effect

        effect = validate_effect(effect)
        if "binding" not in effect:
            raise ValueError("external proposals require an explicit executor configuration binding")
        evaluation = evaluate(problem)
        if evaluation["recommendation"]["kind"] != "act":
            raise ValueError("external effects require an act recommendation")
        proposal_id = uuid.uuid4().hex
        with self.store.transaction() as db:
            state = self.store.snapshot(branch)
            state["decisions"][proposal_id] = evaluation
            base = self.store.commit(branch, state, "Record external decision " + proposal_id,
                                     expected_head)
            proposal = {"id": proposal_id, "kind": "external", "branch": branch, "base": base,
                        "evaluation": evaluation, "effect": effect, "rationale": rationale,
                        "created_at": datetime.now(timezone.utc).isoformat()}
            db.execute("INSERT INTO proposals VALUES (?, ?, ?, ?, ?)",
                       (proposal_id, branch, base, canonical(proposal), proposal["created_at"]))
            self.store.append_event("action.proposed", proposal)
            return proposal

    def check_authorization(self, proposal_id, expected_head):
        """Check live authorization inside the caller's dispatch transaction."""
        with self.store.transaction() as db:
            proposal = self.get(proposal_id)
            if expected_head != proposal["base"]:
                raise ConflictError("expected head must match the reviewed proposal base")
            if not self.verify_integrity()["ok"]:
                raise ConflictError("state, event, or adjudication integrity failed")
            row = db.execute("SELECT payload FROM rulings WHERE proposal_id=? "
                             "ORDER BY seq DESC LIMIT 1", (proposal_id,)).fetchone()
            if row is None:
                raise ConflictError("execution requires an approved adjudication")
            ruling = json.loads(row[0])
            if ruling["verdict"] != "approved":
                raise ConflictError("execution requires an approved adjudication")
            if ruling["head"] != proposal["base"] or ruling["proposal_hash"] != digest(proposal):
                raise ConflictError("ruling does not bind the current proposal")
            if self.store.head(proposal["branch"]) != proposal["base"]:
                raise ConflictError("approved state changed; reassess and propose again")
            return proposal

    def claim_external(self, proposal_id, expected_head, operation_id):
        """Freeze dispatch authorization. Execution happens after the transaction commits."""
        with self.store.transaction() as db:
            proposal = self.check_authorization(proposal_id, expected_head)
            if proposal.get("kind") != "external":
                raise ValueError("proposal is not an external effect")
            if db.execute("SELECT 1 FROM external_claims WHERE proposal_id=?",
                          (proposal_id,)).fetchone():
                raise ConflictError("external dispatch already claimed; reconcile it")
            claim = {"proposal_id": proposal_id, "operation_id": operation_id,
                     "proposal_hash": digest(proposal), "base": expected_head,
                     "effect": proposal["effect"]}
            db.execute("INSERT INTO external_claims VALUES (?, ?)", (proposal_id, canonical(claim)))
            self.store.append_event("action.claimed", claim)
            return claim

    def record_external(self, proposal_id, receipt):
        """Record a reconciled physical outcome without overwriting current branch state."""
        from .executors import validate_receipt

        receipt = validate_receipt(receipt)
        if receipt["status"] == "unknown":
            raise ValueError("unknown outcomes must remain unresolved")
        with self.store.transaction() as db:
            if not self.verify_integrity()["ok"]:
                raise ConflictError("state, event, or adjudication integrity failed")
            claim = db.execute("SELECT payload FROM external_claims WHERE proposal_id=?",
                               (proposal_id,)).fetchone()
            if claim is None:
                raise ConflictError("external outcome requires a dispatch claim")
            previous = db.execute("SELECT payload FROM external_results WHERE proposal_id=?",
                                  (proposal_id,)).fetchone()
            result = {"proposal_id": proposal_id, "receipt": receipt}
            if previous is not None:
                if canonical(json.loads(previous[0])) != canonical(result):
                    raise ConflictError("external outcome already has a different receipt")
                return json.loads(previous[0])
            db.execute("INSERT INTO external_results VALUES (?, ?)", (proposal_id, canonical(result)))
            self.store.append_event("action.reconciled", result)
            if receipt["status"] == "succeeded":
                proposal = self.get(proposal_id)
                execution = {"proposal_id": proposal_id, "branch": proposal["branch"],
                             "commit": proposal["base"], "external": True, "receipt": receipt}
                db.execute("INSERT INTO executions VALUES (?, ?)",
                           (proposal_id, canonical(execution)))
                self.store.append_event("action.executed", execution)
            return result

    def propose_evidence(self, branch, problem, evidence_plan, expected_head, rationale=None):
        plan = validate_evidence_plan(evidence_plan)
        evaluation = evaluate(problem)
        if evaluation["recommendation"]["kind"] != "wait":
            raise ValueError("an evidence plan requires a wait recommendation")
        proposal_id = uuid.uuid4().hex
        with self.store.transaction() as db:
            state = self.store.snapshot(branch)
            relation = state.get("relationships", {}).get(plan["relationship_id"])
            if not isinstance(relation, dict):
                raise ValueError("evidence plan relationship does not exist on this branch")
            belief = relation.get("belief", {})
            scenarios = {
                item["id"]: item["probability"]
                for item in evaluation["inputs"]["scenarios"]
            }
            if set(scenarios) != {"enhancing", "neutral", "degrading"} or any(
                not math.isclose(scenarios[key], belief.get(key, -1), abs_tol=1e-9)
                for key in scenarios
            ):
                raise ValueError("evidence plan probabilities must match current relationship belief")
            state["decisions"][proposal_id] = evaluation
            base = self.store.commit(branch, state, "Record evidence decision " + proposal_id,
                                     expected_head)
            proposal = {
                "id": proposal_id,
                "kind": "evidence",
                "branch": branch,
                "base": base,
                "evaluation": evaluation,
                "evidence_plan": plan,
                "rationale": rationale,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.execute("INSERT INTO proposals VALUES (?, ?, ?, ?, ?)",
                       (proposal_id, branch, base, canonical(proposal), proposal["created_at"]))
            self.store.append_event("action.proposed", proposal)
            return proposal

    def propose_temporal_memory(
        self, branch, relationship_id, problem, patch, expected_head, rationale=None
    ):
        """Propose only the first modeled memory action, bound to a branch belief."""
        patch = validate_patch(patch)
        plan = plan_temporal(problem)
        action_id = plan["first_action"]
        selected = next(
            (item for item in plan["inputs"]["actions"] if item["id"] == action_id), None
        )
        if selected is None or selected["kind"] != "act":
            raise ValueError("temporal memory proposal requires a selected act action")
        if not isinstance(relationship_id, str) or not relationship_id.strip():
            raise ValueError("relationship_id must be a nonempty string")
        evaluation = {
            **plan,
            "recommendation": {
                "kind": "act",
                "action_id": action_id,
                "score": plan["expected_value"],
                "reasons": ["highest admissible modeled finite-horizon value"],
            },
        }
        proposal_id = uuid.uuid4().hex
        with self.store.transaction() as db:
            state = self.store.snapshot(branch)
            relation = state.get("relationships", {}).get(relationship_id)
            if not isinstance(relation, dict):
                raise ValueError("temporal relationship does not exist on this branch")
            if relation.get("basis") == "unassessed":
                raise ValueError("temporal action requires an assessed relationship prior")
            belief = relation.get("belief", {})
            if set(plan["inputs"]["states"]) != {"enhancing", "neutral", "degrading"} or any(
                not math.isclose(plan["inputs"]["prior"][key], belief.get(key, -1),
                                 rel_tol=0.0, abs_tol=1e-9)
                for key in plan["inputs"]["states"]
            ):
                raise ValueError("temporal prior must match the current relationship belief")
            state["decisions"][proposal_id] = evaluation
            base = self.store.commit(branch, state,
                                     "Record temporal decision " + proposal_id, expected_head)
            proposal = {
                "id": proposal_id,
                "kind": "temporal_memory",
                "branch": branch,
                "base": base,
                "relationship_id": relationship_id,
                "evaluation": evaluation,
                "patch": patch,
                "rationale": rationale,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.execute("INSERT INTO proposals VALUES (?, ?, ?, ?, ?)",
                       (proposal_id, branch, base, canonical(proposal), proposal["created_at"]))
            self.store.append_event("action.proposed", proposal)
            return proposal

    def verify_integrity(self):
        result = self.store.verify_integrity()
        issues = list(result["issues"])
        with self.store.transaction() as db:
            events = {}
            try:
                for row in db.execute(
                    "SELECT kind, payload FROM events WHERE kind LIKE 'action.%' ORDER BY seq"
                ):
                    payload = json.loads(row["payload"])
                    events.setdefault(row["kind"], []).append(canonical(payload))
                for table, kind in (
                    ("proposals", "action.proposed"),
                    ("rulings", "action.adjudicated"),
                    ("executions", "action.executed"),
                    ("outcome_reviews", "action.reviewed"),
                    ("external_claims", "action.claimed"),
                    ("external_results", "action.reconciled"),
                ):
                    order = "seq" if table in ("rulings", "outcome_reviews") else "rowid"
                    projections = [
                        canonical(json.loads(r[0]))
                        for r in db.execute(f"SELECT payload FROM {table} ORDER BY {order}")
                    ]
                    chain = events.get(kind, [])
                    matches = (
                        projections == chain
                        if table in ("rulings", "outcome_reviews")
                        else sorted(projections) == sorted(chain)
                    )
                    if not matches:
                        issues.append("Audit projection differs from event chain: " + table)
                    for operation in ("UPDATE", "DELETE"):
                        name = f"immutable_{table}_{operation}"
                        if not db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
                        ).fetchone():
                            issues.append("Missing audit append-only trigger: " + name)
                for row in db.execute("SELECT id, branch, base, payload FROM proposals"):
                    p = json.loads(row["payload"])
                    if (p["id"], p["branch"], p["base"]) != (row["id"], row["branch"], row["base"]):
                        issues.append("Proposal index differs from payload")
                for row in db.execute("SELECT proposal_id, verdict, payload FROM rulings"):
                    ruling = json.loads(row["payload"])
                    p = self.get(row["proposal_id"])
                    if (ruling["proposal_id"], ruling["verdict"]) != (
                        row["proposal_id"],
                        row["verdict"],
                    ):
                        issues.append("Ruling index differs from payload")
                    if ruling["proposal_hash"] != digest(p):
                        issues.append("Ruling proposal digest mismatch")
                for row in db.execute("SELECT proposal_id, payload FROM executions"):
                    if row["proposal_id"] != json.loads(row["payload"])["proposal_id"]:
                        issues.append("Execution index differs from payload")
                for row in db.execute("SELECT proposal_id, payload FROM outcome_reviews"):
                    review = json.loads(row["payload"])
                    if row["proposal_id"] != review["proposal_id"]:
                        issues.append("Outcome review index differs from payload")
                    if review["proposal_hash"] != digest(self.get(row["proposal_id"])):
                        issues.append("Outcome review proposal digest mismatch")
                for row in db.execute("SELECT proposal_id, payload FROM external_claims"):
                    claim = json.loads(row["payload"])
                    proposal = self.get(row["proposal_id"])
                    if (claim["proposal_id"] != row["proposal_id"]
                            or claim["proposal_hash"] != digest(proposal)
                            or canonical(claim["effect"]) != canonical(proposal["effect"])):
                        issues.append("External claim differs from proposal")
                for row in db.execute("SELECT proposal_id, payload FROM external_results"):
                    if json.loads(row["payload"])["proposal_id"] != row["proposal_id"]:
                        issues.append("External result index differs from payload")
            except (ValueError, TypeError, KeyError) as exc:
                issues.append("Invalid audit projection: " + type(exc).__name__)
        return {**result, "ok": not issues, "issues": issues}

    def get(self, proposal_id):
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise NotFoundError("unknown proposal")
            return json.loads(row[0])

    def proposals(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self.store.transaction() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM proposals ORDER BY rowid DESC LIMIT ?", (limit,)
                )
            ]

    def adjudicate(self, proposal_id, verdict, actor, reason, evidence=None):
        if verdict not in ("approved", "rejected", "deferred"):
            raise ValueError("verdict must be approved, rejected, or deferred")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("an adjudicator identity is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a reason is required")
        canonical(evidence)
        with self.store.transaction() as db:
            proposal = self.get(proposal_id)
            if db.execute("SELECT 1 FROM external_claims WHERE proposal_id=?",
                          (proposal_id,)).fetchone():
                raise ConflictError("external dispatch claimed; reconcile its outcome")
            if db.execute(
                "SELECT 1 FROM executions WHERE proposal_id=?", (proposal_id,)
            ).fetchone():
                raise ConflictError("already executed; use a new proposal or revert")
            head = self.store.head(proposal["branch"])
            if verdict == "approved":
                expected_kind = (
                    "wait" if proposal.get("kind") == "evidence" else "act"
                )
                if proposal["evaluation"]["recommendation"]["kind"] != expected_kind:
                    raise ConflictError("a wait or abstain decision cannot authorize an action")
                if proposal.get("kind") == "evidence":
                    _plan_is_current(proposal)
                if head != proposal["base"]:
                    raise ConflictError("state changed; reassess and propose again")
                integrity = self.verify_integrity()
                if not integrity["ok"]:
                    raise ConflictError("state or event integrity failed")
            ruling = {
                "proposal_id": proposal_id,
                "verdict": verdict,
                "actor": actor,
                "reason": reason,
                "evidence": evidence,
                "head": head,
                "proposal_hash": digest(proposal),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.execute(
                "INSERT INTO rulings (proposal_id, verdict, payload) VALUES (?, ?, ?)",
                (proposal_id, verdict, canonical(ruling)),
            )
            self.store.append_event("action.adjudicated", ruling)
            return ruling

    def execute(self, proposal_id, expected_head):
        with self.store.transaction() as db:
            proposal = self.get(proposal_id)
            if proposal.get("kind") == "external":
                raise ConflictError("external effects must execute through the work coordinator")
            if expected_head != proposal["base"]:
                raise ConflictError("expected head must match the reviewed proposal base")
            if not self.verify_integrity()["ok"]:
                raise ConflictError("state, event, or adjudication integrity failed")
            previous = db.execute(
                "SELECT payload FROM executions WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if previous:
                return json.loads(previous[0])
            row = db.execute(
                "SELECT payload FROM rulings WHERE proposal_id=? ORDER BY seq DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
            if row is None or json.loads(row[0])["verdict"] != "approved":
                raise ConflictError("execution requires an approved adjudication")
            ruling = json.loads(row[0])
            if ruling["head"] != proposal["base"] or ruling["proposal_hash"] != digest(proposal):
                raise ConflictError("ruling does not bind the current proposal")
            if self.store.head(proposal["branch"]) != proposal["base"]:
                raise ConflictError("approved state changed; reassess and propose again")
            state = self.store.snapshot(proposal["branch"])
            if proposal.get("kind") == "evidence":
                _plan_is_current(proposal)
                state.setdefault("evidence_plans", {})[proposal_id] = {
                    **proposal["evidence_plan"], "status": "authorized"
                }
            else:
                state["memory"].update(proposal["patch"]["set"])
                for key in proposal["patch"]["delete"]:
                    state["memory"].pop(key, None)
                if proposal.get("kind") == "temporal_memory":
                    relationship_id = proposal["relationship_id"]
                    relation = state["relationships"][relationship_id]
                    inputs = proposal["evaluation"]["inputs"]
                    prior = relation["belief"]
                    if any(not math.isclose(prior[key], inputs["prior"][key],
                                            rel_tol=0.0, abs_tol=1e-9)
                           for key in inputs["states"]):
                        raise ConflictError("relationship belief differs from approved plan")
                    action_id = proposal["evaluation"]["recommendation"]["action_id"]
                    action = next(item for item in inputs["actions"] if item["id"] == action_id)
                    exact_prior = exact_distribution(prior)
                    transitions = {
                        current: exact_distribution(action["transitions"][current])
                        for current in inputs["states"]
                    }
                    predicted = {
                        next_state: sum(
                            (exact_prior[current] * transitions[current][next_state]
                             for current in inputs["states"]), Fraction(0)
                        )
                        for next_state in inputs["states"]
                    }
                    relation["belief"] = float_distribution(predicted, require_support=True)
                    relation["basis"] = "prediction"
                    relation["updated_at"] = datetime.now(timezone.utc).isoformat()
                    relation.setdefault("transition_proposal_ids", []).append(proposal_id)
            commit_id = self.store.commit(
                proposal["branch"],
                state,
                "Execute adjudicated action " + proposal_id,
                expected_head,
            )
            result = {
                "proposal_id": proposal_id,
                "commit": commit_id,
                "branch": proposal["branch"],
                "revert_to": proposal["base"],
            }
            db.execute("INSERT INTO executions VALUES (?, ?)", (proposal_id, canonical(result)))
            self.store.append_event("action.executed", result)
            return result

    def review_outcome(self, proposal_id, observation_ids, assessment, actor, reason):
        """Append a retrospective judgment without changing the original ruling."""
        if assessment not in ("supported", "contradicted", "inconclusive"):
            raise ValueError("assessment must be supported, contradicted, or inconclusive")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("a reviewer identity is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a review reason is required")
        if (not isinstance(observation_ids, list) or not observation_ids
                or any(not isinstance(item, str) or not item for item in observation_ids)
                or len(set(observation_ids)) != len(observation_ids)):
            raise ValueError("observation_ids must be a nonempty list of unique IDs")
        from .experience import Experience

        with self.store.transaction() as db:
            if not self.verify_integrity()["ok"]:
                raise ConflictError("audit history failed integrity verification")
            proposal = self.get(proposal_id)
            if not db.execute("SELECT 1 FROM executions WHERE proposal_id=?",
                              (proposal_id,)).fetchone():
                raise ConflictError("an outcome review requires an executed proposal")
            execution_event = db.execute(
                "SELECT timestamp FROM events WHERE kind='action.executed' "
                "AND json_extract(payload, '$.proposal_id')=? ORDER BY seq LIMIT 1",
                (proposal_id,),
            ).fetchone()
            if execution_event is None:
                raise ConflictError("execution event is missing")
            executed_at = datetime.fromisoformat(execution_event["timestamp"])
            experiences = Experience(self.store)
            if not experiences.verify_integrity()["ok"]:
                raise ConflictError("observation history failed integrity verification")
            for observation_id in observation_ids:
                observation = experiences.get_observation(observation_id)
                if (datetime.fromisoformat(observation["observed_at"]) < executed_at
                        or datetime.fromisoformat(observation["created_at"]) < executed_at):
                    raise ValueError("outcome observation predates execution")
            review = {
                "proposal_id": proposal_id,
                "proposal_hash": digest(proposal),
                "observation_ids": observation_ids,
                "assessment": assessment,
                "actor": actor,
                "reason": reason,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.execute("INSERT INTO outcome_reviews (proposal_id, payload) VALUES (?, ?)",
                       (proposal_id, canonical(review)))
            self.store.append_event("action.reviewed", review)
            return review

    def reviews(self, proposal_id):
        self.get(proposal_id)
        with self.store.transaction() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM outcome_reviews WHERE proposal_id=? ORDER BY seq",
                (proposal_id,),
            )]

    def check_evidence_plan(self, proposal_id, relationship_id, source, observed_at=None):
        """Check that a source observation names an executed evidence plan."""
        if not self.verify_integrity()["ok"]:
            raise ConflictError("audit history failed integrity verification")
        proposal = self.get(proposal_id)
        if proposal.get("kind") != "evidence":
            raise ConflictError("proposal does not authorize evidence acquisition")
        if source != proposal["evidence_plan"]["source"]:
            raise ConflictError("observation source differs from authorized source")
        if relationship_id != proposal["evidence_plan"]["relationship_id"]:
            raise ConflictError("observation relationship differs from authorized target")
        with self.store.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM executions WHERE proposal_id=?", (proposal_id,)
            ).fetchone():
                raise ConflictError("evidence plan has not been executed")
        try:
            timestamp = (
                datetime.fromisoformat(observed_at)
                if observed_at is not None
                else datetime.now(timezone.utc)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        deadline = datetime.fromisoformat(proposal["evidence_plan"]["deadline"])
        if timestamp > deadline or datetime.now(timezone.utc) > deadline:
            raise ConflictError("evidence plan deadline has passed")
        return proposal
