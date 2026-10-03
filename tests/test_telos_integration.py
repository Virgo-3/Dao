"""End-to-end evidence authorization and retrospective review."""

from datetime import datetime, timedelta, timezone

import pytest

from dao.audit import Audit
from dao.decision import evaluate
from dao.experience import Experience
from dao.relational import compile_relationship_problem
from dao.store import ConflictError, Store


def _problem(relationship):
    return compile_relationship_problem(
        relationship,
        [{
            "id": "engage",
            "utilities": {"enhancing": 8, "neutral": 0, "degrading": -12},
            "cost": 1,
        }],
        [
            {"id": "positive", "likelihoods": {
                "enhancing": 0.8, "neutral": 0.5, "degrading": 0.2,
            }},
            {"id": "negative", "likelihoods": {
                "enhancing": 0.2, "neutral": 0.5, "degrading": 0.8,
            }},
        ],
        wait_cost=1,
    )


def test_authorized_evidence_updates_belief_and_preserves_original_decision(tmp_path):
    with Store(tmp_path / "telos.db") as store:
        experiences = Experience(store)
        audit = Audit(store)
        start = store.head()
        registered = experiences.register_relationship(
            "main", "reminders", "user", "reminders", "interruption",
            {"enhancing": 0.6, "neutral": 0, "degrading": 0.4}, start,
        )
        relationship = registered["relationship"]
        problem = _problem(relationship)
        assert evaluate(problem)["recommendation"]["kind"] == "wait"
        plan = {
            "relationship_id": "reminders",
            "source": "user feedback",
            "question": "Would a reminder help?",
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "max_cost_usd": 0,
        }
        proposal = audit.propose_evidence(
            "main", problem, plan, registered["head"]
        )
        with pytest.raises(ConflictError):
            audit.execute(proposal["id"], proposal["base"])
        audit.adjudicate(proposal["id"], "approved", "operator", "small inquiry")
        execution = audit.execute(proposal["id"], proposal["base"])
        assert store.snapshot("main")["evidence_plans"][proposal["id"]]["status"] == "authorized"
        audit.check_evidence_plan(proposal["id"], "reminders", "user feedback")
        with pytest.raises(ConflictError):
            audit.check_evidence_plan(proposal["id"], "reminders", "other source")

        observation = experiences.record_observation(
            "reminders", "positive",
            {"enhancing": 0.8, "neutral": 0.5, "degrading": 0.2},
            "user feedback", "operator", proposal_id=proposal["id"],
        )
        applied = experiences.apply_observation(
            "main", observation["id"], execution["commit"]
        )
        assert applied["relationship"]["belief"]["enhancing"] == pytest.approx(6 / 7)
        assert audit.get(proposal["id"])["evaluation"]["recommendation"]["kind"] == "wait"
        with pytest.raises(ConflictError):
            audit.adjudicate(proposal["id"], "rejected", "operator", "already executed")


def test_retrospective_review_requires_execution_and_durable_evidence(tmp_path):
    with Store(tmp_path / "review.db") as store:
        experiences = Experience(store)
        audit = Audit(store)
        registered = experiences.register_relationship(
            "main", "task", "user", "task", "completion",
            {"enhancing": 1, "neutral": 0, "degrading": 0}, store.head(),
        )
        problem = compile_relationship_problem(
            registered["relationship"], [
                {"id": "remember", "utilities": {
                    "enhancing": 2, "neutral": 0, "degrading": -2,
                }},
            ],
        )
        proposal = audit.propose("main", problem, {"set": {"task": "remember"}},
                                 registered["head"])
        with pytest.raises(ConflictError):
            audit.review_outcome(
                proposal["id"], ["not yet observed"], "supported", "operator", "observed"
            )
        audit.adjudicate(proposal["id"], "approved", "operator", "reviewed")
        audit.execute(proposal["id"], proposal["base"])
        observation = experiences.record_observation(
            "task", "completed", {"enhancing": 0.9, "neutral": 0.5, "degrading": 0.1},
            "user feedback", "operator",
        )
        review = audit.review_outcome(
            proposal["id"], [observation["id"]], "supported", "operator", "observed"
        )
        assert audit.reviews(proposal["id"]) == [review]
        assert audit.get(proposal["id"])["evaluation"]["recommendation"]["kind"] == "act"
        assert audit.verify_integrity()["ok"]
        assert experiences.verify_integrity()["ok"]


def test_temporal_memory_action_predicts_next_state_before_new_evidence(tmp_path):
    with Store(tmp_path / "temporal.db") as store:
        experiences = Experience(store)
        audit = Audit(store)
        registered = experiences.register_relationship(
            "main", "workflow", "user", "workflow", "helpfulness",
            {"enhancing": 0.6, "neutral": 0, "degrading": 0.4}, store.head(),
        )
        states = ("enhancing", "neutral", "degrading")
        transitions = {
            "enhancing": {"enhancing": 1, "neutral": 0, "degrading": 0},
            "neutral": {"enhancing": 0, "neutral": 1, "degrading": 0},
            "degrading": {"enhancing": 0, "neutral": 1, "degrading": 0},
        }
        problem = {
            "states": list(states),
            "prior": registered["relationship"]["belief"],
            "horizon": 1,
            "budget": 0,
            "actions": [{
                "id": "remember", "kind": "act",
                "utilities": {"enhancing": 10, "neutral": 0, "degrading": 0},
                "cost": 0, "resource_cost": 0,
                "transitions": transitions,
                "observations": {state: {"unseen": 1} for state in states},
            }],
        }
        proposal = audit.propose_temporal_memory(
            "main", "workflow", problem, {"set": {"workflow": "trial"}},
            registered["head"],
        )
        assert proposal["evaluation"]["model_version"] == "dao-temporal/2"
        audit.adjudicate(proposal["id"], "approved", "operator", "reversible trial")
        execution = audit.execute(proposal["id"], proposal["base"])
        belief = experiences.relationship("main", "workflow")["belief"]
        assert belief == pytest.approx({
            "enhancing": 0.6, "neutral": 0.4, "degrading": 0,
        })
        observation = experiences.record_observation(
            "workflow", "positive",
            {"enhancing": 0.9, "neutral": 0.1, "degrading": 0.1},
            "user feedback", "operator", source_event_id="feedback-1",
        )
        result = experiences.apply_observation(
            "main", observation["id"], execution["commit"]
        )
        assert result["relationship"]["belief"]["enhancing"] == pytest.approx(27 / 29)
        assert audit.verify_integrity()["ok"]


def test_unassessed_uniform_prior_cannot_authorize_temporal_action(tmp_path):
    with Store(tmp_path / "unassessed.db") as store:
        experiences = Experience(store)
        audit = Audit(store)
        registered = experiences.register_relationship(
            "main", "unknown", "user", "task", "helpfulness", None, store.head()
        )
        states = ("enhancing", "neutral", "degrading")
        identity = {
            state: {next_state: int(state == next_state) for next_state in states}
            for state in states
        }
        problem = {
            "states": list(states),
            "prior": registered["relationship"]["belief"],
            "horizon": 1,
            "budget": 0,
            "actions": [{
                "id": "update", "kind": "act",
                "utilities": {state: 1 for state in states},
                "cost": 0, "resource_cost": 0,
                "transitions": identity,
                "observations": {state: {"unseen": 1} for state in states},
            }],
        }
        with pytest.raises(ValueError, match="assessed"):
            audit.propose_temporal_memory(
                "main", "unknown", problem, {"set": {"task": "done"}},
                registered["head"],
            )


def test_temporal_execution_rejects_unrepresentable_predicted_support_atomically(tmp_path):
    with Store(tmp_path / "prediction.db") as store:
        experiences = Experience(store)
        audit = Audit(store)
        registered = experiences.register_relationship(
            "main", "workflow", "user", "workflow", "helpfulness",
            {"enhancing": 1e-300, "neutral": 1, "degrading": 0}, store.head(),
        )
        states = ("enhancing", "neutral", "degrading")
        transitions = {
            "enhancing": {"enhancing": 0, "neutral": 1, "degrading": 1e-30},
            "neutral": {"enhancing": 0, "neutral": 1, "degrading": 0},
            "degrading": {"enhancing": 0, "neutral": 0, "degrading": 1},
        }
        problem = {
            "states": list(states), "prior": registered["relationship"]["belief"],
            "horizon": 1, "budget": 0,
            "actions": [{
                "id": "remember", "kind": "act", "utilities": {s: 1 for s in states},
                "cost": 0, "resource_cost": 0, "transitions": transitions,
                "observations": {s: {"unseen": 1} for s in states},
            }],
        }
        proposal = audit.propose_temporal_memory(
            "main", "workflow", problem, {"set": {"workflow": "trial"}}, registered["head"],
        )
        audit.adjudicate(proposal["id"], "approved", "operator", "reviewed")
        before = store.snapshot()
        with pytest.raises(ValueError, match="numeric range"):
            audit.execute(proposal["id"], proposal["base"])
        assert store.head() == proposal["base"]
        assert store.snapshot() == before
        assert audit.get(proposal["id"]) == proposal
        with store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        assert audit.verify_integrity()["ok"]
