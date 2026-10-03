"""Replay receipts must preserve Dao's atomic state and global usage history."""

import json

import pytest

from dao.ledger import Ledger
from dao.store import ConflictError, NotFoundError, Store
from dao.workflow import WorkflowJournal


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "dao.sqlite3") as value:
        yield value


def test_local_receipt_replays_exact_request_without_repeating_state_write(store):
    journal = WorkflowJournal(store)
    head = store.head()
    calls = []

    def write():
        calls.append(True)
        state = store.snapshot()
        state["memory"]["preference"] = "tea"
        return {"head": store.commit("main", state, "preference", head), "nested": [1]}

    first = journal.run_local("local-1", {"branch": "main", "head": head}, write)
    first["nested"].append(2)
    events = store.events()
    second = journal.run_local("local-1", {"head": head, "branch": "main"}, write)
    assert calls == [True]
    assert second["nested"] == [1]
    assert store.snapshot()["memory"] == {"preference": "tea"}
    assert store.events() == events
    receipt = journal.get("local-1")
    assert receipt["status"] == "completed"
    assert receipt["kind"] == "local"
    assert receipt["result"] == second
    assert journal.verify_integrity()["ok"]


def test_same_operation_id_cannot_change_request_or_execution_kind(store):
    journal = WorkflowJournal(store)
    journal.run_local("bound", {"patch": {"set": {"x": 1}}}, lambda: {"ok": True})

    def forbidden():
        pytest.fail("a conflicting request must never execute")

    with pytest.raises(ConflictError):
        journal.run_local("bound", {"patch": {"set": {"x": 2}}}, forbidden)
    with pytest.raises(ConflictError):
        journal.run_external("bound", {"patch": {"set": {"x": 1}}}, forbidden)
    assert journal.get("bound")["result"] == {"ok": True}


@pytest.mark.parametrize("bad_result", [{"x": float("nan")}, {"x": object()}])
def test_invalid_local_result_rolls_back_state_and_receipt(store, bad_result):
    journal = WorkflowJournal(store)
    before = store.head(), store.events()

    def write():
        state = store.snapshot()
        state["memory"]["unpublished"] = True
        store.commit("main", state, "will rollback", store.head())
        return bad_result

    with pytest.raises(ValueError):
        journal.run_local("invalid-result", {"purpose": "atomic"}, write)
    assert (store.head(), store.events()) == before
    with pytest.raises(NotFoundError):
        journal.get("invalid-result")
    assert journal.verify_integrity()["ok"]


def test_local_exception_rolls_back_mutation_and_allows_safe_retry(store):
    journal = WorkflowJournal(store)
    before = store.head(), store.events()

    def fail():
        state = store.snapshot()
        state["memory"]["unpublished"] = True
        store.commit("main", state, "will rollback", store.head())
        raise RuntimeError("local failure")

    with pytest.raises(RuntimeError, match="local failure"):
        journal.run_local("retry-safe", {"value": 1}, fail)
    assert (store.head(), store.events()) == before
    assert journal.run_local("retry-safe", {"value": 1}, lambda: {"ok": True}) == {"ok": True}


def test_external_call_runs_outside_transaction_and_replays_without_new_usage(store):
    journal = WorkflowJournal(store)
    ledger = Ledger(store)
    genesis = store.head()
    calls = []

    def bill():
        assert not store.db.in_transaction
        calls.append(True)
        ledger.reserve("provider-1", "main", ".1")
        ledger.settle("provider-1", 10, 5, ".03", "mock")
        return {"answer": "done"}

    result = journal.run_external("external-1", {"text": "hello"}, bill)
    before = ledger.summary(), store.events()
    assert journal.run_external("external-1", {"text": "hello"}, bill) == result
    assert calls == [True]
    assert (ledger.summary(), store.events()) == before
    state = store.snapshot()
    state["memory"]["temporary"] = True
    changed = store.commit("main", state, "temporary", store.head())
    store.revert("main", genesis, changed)
    assert ledger.summary()["spent_usd"] == "0.030000"
    assert journal.get("external-1")["result"] == result
    assert journal.verify_integrity()["ok"]


def test_external_failure_preserves_liability_and_blocks_automatic_retry(store):
    journal = WorkflowJournal(store)
    ledger = Ledger(store)
    calls = []

    def uncertain():
        calls.append(True)
        ledger.reserve("uncertain", "main", ".2")
        ledger.mark_unknown("uncertain", "TimeoutError")
        raise TimeoutError("secret provider request and credentials")

    with pytest.raises(TimeoutError):
        journal.run_external("external-failure", {"text": "hello"}, uncertain)
    receipt = journal.get("external-failure")
    assert receipt["status"] == "failed"
    assert receipt["error_type"] == "TimeoutError"
    with pytest.raises(ConflictError):
        journal.run_external("external-failure", {"text": "hello"}, uncertain)
    assert calls == [True]
    assert ledger.summary()["unknown_count"] == 1
    assert ledger.summary()["reserved_usd"] == "0.200000"
    assert "secret provider request" not in json.dumps(store.events())
    assert journal.verify_integrity()["ok"]


def test_interrupted_external_attempt_stays_claimed_across_reopen(tmp_path):
    path = tmp_path / "durable.sqlite3"
    with Store(path) as store:
        journal = WorkflowJournal(store)

        def interrupt_process():
            Ledger(store).reserve("interrupted", "main", ".1")
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            journal.run_external("interrupted", {"text": "hello"}, interrupt_process)
        assert journal.get("interrupted")["status"] == "started"

    with Store(path) as reopened:
        journal = WorkflowJournal(reopened)
        with pytest.raises(ConflictError):
            journal.run_external("interrupted", {"text": "hello"}, lambda: pytest.fail("replayed"))
        assert Ledger(reopened).summary()["reserved_usd"] == "0.100000"
        assert journal.verify_integrity()["ok"]


def test_external_execution_rejects_callers_transaction_before_any_effect(store):
    journal = WorkflowJournal(store)
    before = store.events()
    with store.transaction():
        with pytest.raises(ConflictError):
            journal.run_external("nested", {}, lambda: pytest.fail("remote call under transaction"))
    assert store.events() == before
    with pytest.raises(NotFoundError):
        journal.get("nested")


@pytest.mark.parametrize("operation_id", ["", " ", None, 1, "x" * 257])
def test_invalid_operation_identity_is_rejected_without_work(store, operation_id):
    journal = WorkflowJournal(store)
    before = store.events()
    with pytest.raises(ValueError):
        journal.run_local(operation_id, {}, lambda: pytest.fail("invalid identity executed"))
    assert store.events() == before


def test_nonfinite_request_is_rejected_before_external_claim(store):
    journal = WorkflowJournal(store)
    before = store.events()
    with pytest.raises(ValueError):
        journal.run_external("invalid", {"cost": float("inf")}, lambda: pytest.fail("executed"))
    assert store.events() == before


def test_other_connection_observes_claim_before_external_dispatch_finishes(tmp_path):
    path = tmp_path / "claim.sqlite3"
    with Store(path) as store:
        journal = WorkflowJournal(store)

        def external_work():
            with Store(path) as second:
                other = WorkflowJournal(second)
                assert other.get("single-dispatch")["status"] == "started"
                with pytest.raises(ConflictError):
                    other.run_external("single-dispatch", {"text": "hello"},
                                       lambda: pytest.fail("second dispatch"))
            return {"answer": "once"}

        assert journal.run_external("single-dispatch", {"text": "hello"}, external_work) == {
            "answer": "once",
        }
        assert journal.verify_integrity()["ok"]


def test_tampered_receipt_cannot_be_replayed_as_authoritative_result(store):
    journal = WorkflowJournal(store)
    journal.run_local("tamper-test", {}, lambda: {"reviewed": True})
    with store.transaction() as db:
        db.execute("DROP TRIGGER immutable_workflow_results_UPDATE")
        payload = json.loads(db.execute(
            "SELECT payload FROM workflow_results WHERE operation_id='tamper-test'"
        ).fetchone()[0])
        payload["result"] = {"reviewed": False}
        db.execute("UPDATE workflow_results SET payload=? WHERE operation_id='tamper-test'",
                   (json.dumps(payload),))
        db.execute("""CREATE TRIGGER immutable_workflow_results_UPDATE
            BEFORE UPDATE ON workflow_results BEGIN
            SELECT RAISE(ABORT, 'immutable workflow record'); END""")
    assert not journal.verify_integrity()["ok"]
    with pytest.raises(ConflictError, match="integrity"):
        journal.run_local("tamper-test", {}, lambda: pytest.fail("tampered receipt executed"))


def test_unserializable_external_result_preserves_usage_and_blocks_replay(store):
    journal = WorkflowJournal(store)
    ledger = Ledger(store)

    def external_work():
        ledger.reserve("invalid-output", "main", ".1")
        ledger.settle("invalid-output", 1, 1, ".01", "mock")
        return {"invalid": float("nan")}

    with pytest.raises(ValueError):
        journal.run_external("invalid-output", {}, external_work)
    assert journal.get("invalid-output")["status"] == "failed"
    with pytest.raises(ConflictError):
        journal.run_external("invalid-output", {}, lambda: pytest.fail("duplicate provider call"))
    assert ledger.summary()["spent_usd"] == "0.010000"
