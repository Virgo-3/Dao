import json
from types import SimpleNamespace

import pytest

from dao.agent import Agent, memory_problem
from dao.audit import Audit
from dao.providers import OpenAIProvider, Reply
from dao.store import ConflictError, Store


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "state.db")
    yield result
    result.close()


def test_adjudication_executes_once_and_revert_preserves_audit(store):
    audit = Audit(store)
    p = audit.propose(
        "main", memory_problem("request"), {"set": {"goal": "learn"}}, store.head("main")
    )
    with pytest.raises(ConflictError):
        audit.execute(p["id"], p["base"])
    audit.adjudicate(p["id"], "approved", "operator", "Reviewed exact reversible patch")
    outcome = audit.execute(p["id"], p["base"])
    assert audit.execute(p["id"], p["base"]) == outcome
    assert store.snapshot()["memory"] == {"goal": "learn"}
    store.revert("main", p["base"], outcome["commit"])
    assert store.snapshot()["memory"] == {}
    assert any(e["kind"] == "action.executed" for e in store.events())
    assert store.verify_integrity()["ok"]


def test_changed_head_invalidates_approval(store):
    audit = Audit(store)
    p = audit.propose("main", memory_problem("request"), {"set": {"x": 1}}, store.head("main"))
    audit.adjudicate(p["id"], "approved", "operator", "reviewed")
    store.commit("main", store.snapshot(), "new evidence", p["base"])
    with pytest.raises(ConflictError):
        audit.execute(p["id"], p["base"])
    assert store.snapshot()["memory"] == {}


def test_wait_cannot_be_approved(store):
    problem = {
        "scenarios": [{"id": "good", "probability": 0.6}, {"id": "bad", "probability": 0.4}],
        "actions": [{"id": "ship", "utilities": {"good": 100, "bad": -120}}],
        "signals": [
            {"id": "positive", "likelihoods": {"good": 0.8, "bad": 0.2}},
            {"id": "negative", "likelihoods": {"good": 0.2, "bad": 0.8}},
        ],
        "wait_cost": 3,
    }
    audit = Audit(store)
    p = audit.propose("main", problem, {"set": {"ship": True}}, store.head("main"))
    assert p["evaluation"]["recommendation"]["kind"] == "wait"
    with pytest.raises(ConflictError):
        audit.adjudicate(p["id"], "approved", "operator", "approve")
    assert audit.adjudicate(p["id"], "deferred", "operator", "Need signal")["verdict"] == "deferred"


def test_latest_ruling_controls_execution(store):
    audit = Audit(store)
    p = audit.propose("main", memory_problem("request"), {"delete": ["x"]}, store.head("main"))
    audit.adjudicate(p["id"], "approved", "one", "reviewed")
    audit.adjudicate(p["id"], "rejected", "two", "reconsidered")
    with pytest.raises(ConflictError):
        audit.execute(p["id"], p["base"])


def test_offline_conversation_and_memory_proposal(store):
    agent = Agent(store)
    answer = agent.chat("hello")
    assert "Offline demo" in answer["text"]
    p = agent.chat('/remember preference "concise"')["proposal"]
    assert store.head("main") == p["base"]
    assert store.snapshot()["memory"] == {}
    agent.audit.adjudicate(p["id"], "approved", "operator", "User asked")
    agent.audit.execute(p["id"], p["base"])
    assert store.snapshot()["memory"]["preference"] == "concise"
    assert agent.ledger.summary()["spent_usd"] == "0.000000"


def test_projection_chat_uses_recorded_state_without_provider_call(store):
    class NeverCalled:
        reservation_usd = "0.01"

        def respond(self, messages, tools):
            raise AssertionError("projection must not call the provider")

    agent = Agent(store, NeverCalled())
    before_usage = agent.ledger.summary()
    reply = agent.chat("/projection")

    assert "main" in reply["text"]
    assert reply["projection"]["text"] == reply["text"]
    assert reply["projection"]["branch"] == "main"
    assert reply["projection"]["head"] != reply["head"]
    assert reply["projection"]["claims"]
    assert reply["projection"]["coverage"]["omissions"]
    assert store.snapshot()["messages"] == [
        {"role": "user", "content": "/projection"},
        {"role": "assistant", "content": reply["text"]},
    ]
    assert agent.ledger.summary() == before_usage


def test_timeout_keeps_reservation(store):
    class Failing:
        reservation_usd = ".1"

        def respond(self, messages, tools):
            raise TimeoutError("private data")

    agent = Agent(store, Failing())
    with pytest.raises(RuntimeError, match="retained usage reservation"):
        agent.chat("hello")
    assert agent.ledger.summary()["unknown_count"] == 1
    assert agent.ledger.summary()["reserved_usd"] == "0.100000"
    assert "private data" not in json.dumps(store.events())


def test_concurrent_state_change_keeps_billing_but_rejects_reply(store):
    class Racing:
        reservation_usd = ".1"

        def respond(self, messages, tools):
            store.commit("main", store.snapshot(), "Other writer", store.head("main"))
            return Reply("stale", 10, 5, ".01", "mock")

    agent = Agent(store, Racing())
    with pytest.raises(ConflictError):
        agent.chat("hello")
    assert agent.ledger.summary()["spent_usd"] == "0.010000"
    assert not any(m["content"] == "stale" for m in store.snapshot()["messages"])
    assert any(e["kind"] == "conversation.orphaned_reply" for e in store.events())


def test_model_can_propose_but_cannot_adjudicate(store):
    class ToolProvider:
        reservation_usd = "0"
        count = 0

        def respond(self, messages, tools):
            self.count += 1
            if self.count == 1:
                call = {
                    "id": "call_1",
                    "name": "propose_memory",
                    "arguments": json.dumps(
                        {"key": "goal", "value_json": '"learn"', "reason": "User request"}
                    ),
                }
                return Reply("", calls=[call])
            return Reply("Proposed, pending adjudication")

    agent = Agent(store, ToolProvider())
    answer = agent.chat("Remember goal learn")
    proposal = agent.audit.get(answer["proposal_ids"][0])
    assert store.head("main") == proposal["base"]
    assert store.snapshot()["memory"] == {}
    assert store.snapshot()["messages"][-1]["content"] == "Proposed, pending adjudication"
    with pytest.raises(ConflictError):
        agent.audit.execute(proposal["id"], proposal["base"])


def test_openai_adapter_counts_cached_tokens_and_disables_storage():
    captured = {}

    class Responses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_text="hello",
                output=[],
                id="response_1",
                usage=SimpleNamespace(
                    input_tokens=1000,
                    output_tokens=100,
                    input_tokens_details=SimpleNamespace(cached_tokens=500),
                ),
            )

    provider = OpenAIProvider(
        model="test-model",
        input_rate="2",
        output_rate="10",
        cached_rate="1",
        reservation_usd=".01",
        client=SimpleNamespace(responses=Responses()),
    )
    reply = provider.respond([{"role": "user", "content": "hi"}], [])
    assert reply.cost_usd == "0.0025"
    assert reply.input_tokens == 1000
    assert captured["store"] is False


def test_patch_overlap_rejected_without_commit(store):
    head = store.head("main")
    with pytest.raises(ValueError):
        Audit(store).propose("main", memory_problem("x"), {"set": {"x": 1}, "delete": ["x"]}, head)
    assert store.head("main") == head


def test_tampered_proposal_is_detected_and_cannot_execute(store):
    audit = Audit(store)
    p = audit.propose("main", memory_problem("x"), {"set": {"x": 1}}, store.head("main"))
    audit.adjudicate(p["id"], "approved", "operator", "Reviewed")
    altered = dict(p)
    altered["patch"] = {"set": {"x": "never reviewed"}, "delete": []}
    with store.transaction() as db:
        db.execute("DROP TRIGGER immutable_proposals_UPDATE")
        db.execute("UPDATE proposals SET payload=? WHERE id=?", (json.dumps(altered), p["id"]))
        db.execute("""CREATE TRIGGER immutable_proposals_UPDATE BEFORE UPDATE ON proposals
                      BEGIN SELECT RAISE(ABORT, 'immutable audit record'); END""")
    assert not audit.verify_integrity()["ok"]
    with pytest.raises(ConflictError, match="integrity"):
        audit.execute(p["id"], p["base"])
    assert store.snapshot()["memory"] == {}


def test_execution_retry_validates_audit_projection(store):
    audit = Audit(store)
    p = audit.propose("main", memory_problem("x"), {"set": {"x": 1}}, store.head("main"))
    audit.adjudicate(p["id"], "approved", "operator", "Reviewed")
    outcome = audit.execute(p["id"], p["base"])
    outcome["commit"] = "fake"
    with store.transaction() as db:
        db.execute("DROP TRIGGER immutable_executions_UPDATE")
        db.execute(
            "UPDATE executions SET payload=? WHERE proposal_id=?", (json.dumps(outcome), p["id"])
        )
    with pytest.raises(ConflictError, match="integrity"):
        audit.execute(p["id"], p["base"])


def test_ruling_reorder_cannot_revive_revoked_approval(store):
    audit = Audit(store)
    p = audit.propose("main", memory_problem("x"), {"set": {"x": 1}}, store.head("main"))
    audit.adjudicate(p["id"], "approved", "operator", "Reviewed")
    audit.adjudicate(p["id"], "rejected", "operator", "Revoked")
    with store.transaction() as db:
        db.execute("DROP TRIGGER immutable_rulings_UPDATE")
        db.execute("UPDATE rulings SET seq=99 WHERE verdict='approved'")
        db.execute("""CREATE TRIGGER immutable_rulings_UPDATE BEFORE UPDATE ON rulings
                      BEGIN SELECT RAISE(ABORT, 'immutable audit record'); END""")
    assert not audit.verify_integrity()["ok"]
    with pytest.raises(ConflictError, match="integrity"):
        audit.execute(p["id"], p["base"])
