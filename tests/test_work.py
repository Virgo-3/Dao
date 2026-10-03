"""Objective continuity, independent authority, and recovery across persistence cuts."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from dao.agent import memory_problem
from dao.audit import Audit
from dao.executors import ArtifactExecutor
from dao.providers import Reply
from dao.store import ConflictError, Store
from dao.work import Coordinator
from dao.workflow import WorkflowJournal


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.db") as result:
        yield result


class MemoryProvider:
    reservation_usd = ".03"

    def __init__(self):
        self.calls = 0

    def respond(self, messages, tools):
        self.calls += 1
        assert {tool["name"] for tool in tools} == {"read_state", "propose_memory"}
        if self.calls == 1:
            return Reply("", 2, 2, ".01", "test", calls=[{
                "id": "memory", "name": "propose_memory",
                "arguments": json.dumps({"key": "goal", "value_json": '"done"',
                                         "reason": "Explicit objective"}),
            }])
        return Reply("Objective completed, I approve myself", 2, 2, ".01", "test")


def approve(coordinator, record):
    coordinator.audit.adjudicate(record["proposal_id"], "approved", "operator", "Reviewed effect")


def local_plan(coordinator, record, value="done"):
    return coordinator.plan(record["id"], memory_problem("explicit request"),
                            {"set": {"goal": value}}, expected_revision=record["revision"])


def test_objective_proposal_usage_and_completion_share_one_lifecycle(store):
    provider = MemoryProvider()
    work = Coordinator(store, provider)
    created = work.create("Remember the requested goal", success={"goal": "done"})
    proposed = work.advance(created["id"], expected_revision=1)
    assert proposed["status"] == "awaiting_approval"
    assert len(proposed["usage_request_ids"]) == 2
    assert proposed["global_usage"]["spent_usd"] == "0.020000"
    assert store.snapshot()["memory"] == {}
    assert work.advance(created["id"]) == proposed
    assert provider.calls == 2
    approve(work, proposed)
    complete = work.advance(created["id"])
    assert complete["status"] == "completed"
    assert complete["completion"]["head"] == store.head()
    assert work.advance(created["id"]) == complete
    assert work.verify_integrity()["ok"]


def test_restart_and_restore_preserve_work_and_physical_spend(store):
    work = Coordinator(store, MemoryProvider())
    genesis = store.head()
    record = work.advance(work.create("Remember goal", success={"goal": "done"})["id"])
    approve(work, record)
    record = work.advance(record["id"])
    with Store(store.path) as reopened:
        fresh = Coordinator(reopened)
        assert fresh.get(record["id"])["revision"] == record["revision"]
        assert fresh.get(record["id"])["status"] == "completed"
        reopened.revert("main", genesis, reopened.head())
        restored = fresh.get(record["id"])
        assert restored["status"] == "completed"
        assert not restored["success_currently_satisfied"]
        assert restored["global_usage"]["spent_usd"] == "0.020000"
        assert fresh.history(record["id"])[-1]["completion"]["head"] == record["head"]


def test_wait_requires_explicit_wakeup_and_matching_evidence(store):
    work = Coordinator(store)
    record = work.create("Wait for test results")
    problem = {
        "scenarios": [{"id": "good", "probability": .6}, {"id": "bad", "probability": .4}],
        "actions": [{"id": "ship", "utilities": {"good": 100, "bad": -120}}],
        "signals": [{"id": "positive", "likelihoods": {"good": .8, "bad": .2}},
                    {"id": "negative", "likelihoods": {"good": .2, "bad": .8}}],
        "wait_cost": 3,
    }
    head = store.head()
    with pytest.raises(ValueError, match="wait_for"):
        work.plan(record["id"], problem, {})
    assert store.head() == head
    assert work.get(record["id"])["revision"] == 1
    waiting = work.plan(record["id"], problem, {}, wait_for={"topic": "test", "source": "CI"})
    assert waiting["status"] == "waiting"
    assert work.advance(record["id"]) == waiting
    wrong = work.observe(record["id"], "one", "test", "untrusted", {"ok": True}, "operator")
    assert wrong["status"] == "waiting"
    ready = work.observe(record["id"], "two", "test", "CI", {"ok": True}, "operator")
    assert ready["status"] == "ready"
    assert not ready["proposal_id"]
    assert store.snapshot()["memory"] == {}
    assert work.observe(record["id"], "two", "test", "CI", {"ok": True}, "operator") == ready
    with pytest.raises(ConflictError, match="different evidence"):
        work.observe(record["id"], "two", "test", "CI", {"ok": False}, "operator")


def test_new_evidence_and_branch_writes_invalidate_pending_approval(store):
    work = Coordinator(store)
    record = local_plan(work, work.create("Record goal"))
    approve(work, record)
    work.observe(record["id"], "news", "review", "operator", "Changed context", "operator")
    with pytest.raises(ConflictError, match="state changed"):
        work.audit.execute(record["proposal_id"], record["head"])
    record = local_plan(work, work.get(record["id"]))
    approve(work, record)
    store.commit("main", store.snapshot(), "Other writer", store.head())
    assert work.advance(record["id"])["transition"] == "approval_stale"
    assert store.snapshot()["memory"] == {}


def test_ruling_rejection_and_deferral_never_dispatch(store):
    work = Coordinator(store)
    record = local_plan(work, work.create("Record goal"))
    work.audit.adjudicate(record["proposal_id"], "deferred", "operator", "Need evidence")
    assert work.advance(record["id"])["status"] == "awaiting_approval"
    work.audit.adjudicate(record["proposal_id"], "rejected", "operator", "Wrong effect")
    assert work.advance(record["id"])["status"] == "awaiting_input"
    assert store.snapshot()["memory"] == {}


def test_unknown_provider_usage_blocks_retry_until_trusted_recovery(store):
    class Failing:
        reservation_usd = ".1"
        calls = 0

        def respond(self, messages, tools):
            self.calls += 1
            raise TimeoutError("secret provider details")

    provider = Failing()
    work = Coordinator(store, provider)
    failed = work.advance(work.create("Do something")["id"])
    assert failed["status"] == "blocked"
    assert "secret provider details" not in json.dumps(work.history(failed["id"]))
    assert work.advance(failed["id"]) == failed
    evidence = {"dispatch_quiescent": True, "provider_receipt": "verified"}
    with pytest.raises(ConflictError, match="usage"):
        work.recover(failed["id"], "operator", "Inspected", evidence)
    request_id = failed["usage_request_ids"][0]
    work.agent.ledger.resolve_unknown(request_id, 1, 1, ".05", "test")
    ready = work.recover(failed["id"], "operator", "Verified provider billing", evidence)
    assert ready["status"] == "ready"
    assert ready["global_usage"]["spent_usd"] == "0.050000"
    assert provider.calls == 1
    assert work.journal.get(failed["operation_id"])["status"] == "failed"


def test_saved_reply_is_adopted_after_crash_without_repeating_provider(store, monkeypatch):
    provider = MemoryProvider()
    work = Coordinator(store, provider)
    record = work.create("Record goal")
    adopt = work._adopt
    monkeypatch.setattr(work, "_adopt", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        work.advance(record["id"])
    # Adoption and the saved reply normally commit together; emulate a separate
    # durable reply cut to exercise restart recovery explicitly.
    current = work._record(record["id"])
    assert current["status"] == "generating"
    operation = work.journal.get(current["operation_id"])
    assert operation["status"] == "started"
    proposal = work.audit.proposals()[0]
    with store.transaction():
        work.journal.finish_external(current["operation_id"], result={
            "text": "Saved reply", "head": proposal["base"], "proposal": proposal})
    monkeypatch.setattr(work, "_adopt", adopt)
    recovered = work.advance(record["id"])
    assert recovered["status"] == "awaiting_approval"
    assert provider.calls == 2
    assert len(work.audit.proposals()) == 1


def test_concurrent_advances_do_not_duplicate_inflight_generation(store):
    entered, release = threading.Event(), threading.Event()

    class Slow:
        reservation_usd = "0"
        calls = 0

        def respond(self, messages, tools):
            self.calls += 1
            entered.set()
            assert release.wait(10)
            return Reply("Need a concrete plan")

    provider = Slow()
    work = Coordinator(store, provider)
    record = work.create("Choose a plan")
    with ThreadPoolExecutor() as pool:
        first = pool.submit(work.advance, record["id"])
        assert entered.wait(10)
        try:
            with Store(store.path) as second_store:
                second = Coordinator(second_store, provider)
                assert second.advance(record["id"])["status"] == "generating"
                assert provider.calls == 1
        finally:
            release.set()
        assert first.result(timeout=10)["status"] == "awaiting_input"


def test_completion_needs_actual_predicates_or_trusted_evidence(store):
    work = Coordinator(store)
    record = work.create("Record integer", success={"x": 1})
    state = store.snapshot()
    state["memory"]["x"] = True
    store.commit("main", state, "Boolean differs from integer", store.head())
    with pytest.raises(ConflictError, match="predicates"):
        work.complete(record["id"], "operator", "Done", {"checked": True})
    with pytest.raises(ValueError, match="evidence"):
        work.complete(record["id"], "operator", "Done", {})
    manual = work.create("Verify external result")
    assert work.complete(manual["id"], "operator", "Verified", {"receipt": "source"})[
        "status"] == "completed"


def test_creation_and_mutation_guard_identity_and_revision(store):
    work = Coordinator(store)
    record = work.create("Record goal", success={"x": 1}, work_id="stable")
    assert work.create("Record goal", success={"x": 1}, work_id="stable") == record
    with pytest.raises(ConflictError, match="different objective"):
        work.create("Record goal", success={"x": True}, work_id="stable")
    planned = local_plan(work, record)
    with pytest.raises(ConflictError, match="revision"):
        work.advance(record["id"], expected_revision=record["revision"])
    assert work.get(record["id"]) == planned


def test_tampered_work_cannot_advance(store):
    work = Coordinator(store)
    record = work.create("Record goal")
    store.db.execute("DROP TRIGGER immutable_work_revisions_UPDATE")
    changed = {**work._record(record["id"]), "objective": "Unreviewed objective"}
    store.db.execute("UPDATE work_revisions SET payload=? WHERE work_id=?",
                     (json.dumps(changed), record["id"]))
    assert not work.verify_integrity()["ok"]
    with pytest.raises(ConflictError, match="integrity"):
        work.advance(record["id"])


def effect():
    return {"executor": "artifact", "arguments": {"content": {"result": "verified"}},
            "preconditions": {"absent": True}, "reservation_usd": ".1"}


def external_plan(work):
    created = work.create("Produce a verified artifact")
    record = work.plan(created["id"], memory_problem("explicit artifact"), effect=effect())
    approve(work, record)
    return record


def test_external_effect_has_independent_authority_and_preserves_accounting(store, tmp_path):
    artifact = ArtifactExecutor(tmp_path / "artifacts")
    work = Coordinator(store, executors={"artifact": artifact})
    record = external_plan(work)
    with pytest.raises(ConflictError, match="coordinator"):
        Audit(store).execute(record["proposal_id"], record["head"])
    outcome = work.advance(record["id"])
    assert outcome["outcome"]["receipt"]["status"] == "succeeded"
    assert len(list(artifact.directory.iterdir())) == 1
    assert outcome["global_usage"]["reserved_usd"] == "0.000000"
    assert len(outcome["usage_request_ids"]) == 1
    assert not store.db.in_transaction
    assert work.verify_integrity()["ok"]


def test_crash_after_external_effect_reconciles_without_redispatch(store, tmp_path):
    class CrashAfterEffect(ArtifactExecutor):
        calls = 0

        def execute(self, effect, key):
            self.calls += 1
            assert not store.db.in_transaction
            super().execute(effect, key)
            raise KeyboardInterrupt()

    artifact = CrashAfterEffect(tmp_path / "artifacts")
    work = Coordinator(store, executors={"artifact": artifact})
    record = external_plan(work)
    with pytest.raises(KeyboardInterrupt):
        work.advance(record["id"])
    pending = work.get(record["id"])
    assert pending["status"] == "executing"
    assert work.advance(record["id"]) == pending
    with pytest.raises(ConflictError, match="dispatch claimed"):
        work.audit.adjudicate(record["proposal_id"], "rejected", "operator", "Too late")
    # Changing Dao's logical state after dispatch cannot erase its physical outcome.
    store.revert("main", store.log()[-1]["id"], store.head())
    outcome = work.reconcile(record["id"], "operator", "Worker stopped", {"dispatch_quiescent": True})
    assert outcome["outcome"]["receipt"]["status"] == "succeeded"
    assert artifact.calls == 1
    assert len(list(artifact.directory.iterdir())) == 1
    assert outcome["global_usage"]["reserved_usd"] == "0.000000"
    assert work.verify_integrity()["ok"]


def test_uncertain_external_outcome_keeps_liability_and_never_retries(store):
    class Unknown:
        binding = {"service": "test-unknown", "version": "1"}
        calls = 0

        def execute(self, effect, key):
            self.calls += 1
            raise TimeoutError("sensitive executor error")

        def reconcile(self, effect, key):
            return {"status": "unknown", "evidence": {"provider_unavailable": True}, "cost_usd": "0"}

    executor = Unknown()
    work = Coordinator(store, executors={"artifact": executor})
    record = external_plan(work)
    outcome = work.advance(record["id"])
    assert outcome["status"] == "reconciling"
    assert outcome["global_usage"]["reserved_usd"] == "0.100000"
    assert "sensitive executor error" not in json.dumps(work.history(record["id"]))
    assert work.advance(record["id"]) == outcome
    assert executor.calls == 1
    with pytest.raises(ValueError, match="quiescent"):
        work.reconcile(record["id"], "operator", "Inspect", {"checked": True})
    assert work.reconcile(record["id"], "operator", "Stopped worker",
                          {"dispatch_quiescent": True})["status"] == "reconciling"
    assert executor.calls == 1


def test_absent_external_result_requires_new_plan_and_budget_failure_dispatches_nothing(store):
    class NeverExecuted:
        binding = {"service": "test-not-executed", "version": "1"}
        calls = 0

        def execute(self, effect, key):
            self.calls += 1
            return {"status": "not_executed", "evidence": {"precondition_failed": True}, "cost_usd": "0"}

        def reconcile(self, effect, key):
            raise AssertionError()

    executor = NeverExecuted()
    work = Coordinator(store, executors={"artifact": executor})
    record = external_plan(work)
    work.agent.ledger.set_budget("0")
    with pytest.raises(ValueError):
        work.advance(record["id"])
    assert executor.calls == 0
    assert not store.db.execute("SELECT 1 FROM external_claims").fetchone()
    work.agent.ledger.set_budget("1")
    assert work.advance(record["id"])["status"] == "awaiting_input"
    assert executor.calls == 1


def test_turn_limit_prevents_unbounded_provider_use(store):
    work = Coordinator(store)
    record = work.create("Need an explicit plan", max_turns=1)
    record = work.advance(record["id"])
    assert record["turns"] == 1
    work.observe(record["id"], "new", "update", "operator", "reconsider", "operator")
    record = work.advance(record["id"])
    assert record["transition"] == "turn_limit"
    assert record["turns"] == 1


def test_external_operation_with_null_result_is_replayed_without_dispatch(store):
    journal = WorkflowJournal(store)
    calls = []

    def dispatch():
        calls.append("effect")
        return None

    assert journal.run_external("null-effect", {"request": "same"}, dispatch) is None
    assert journal.run_external("null-effect", {"request": "same"}, dispatch) is None
    assert calls == ["effect"]


def test_bounded_run_stops_for_independent_review_and_adjudication_advances(store):
    work = Coordinator(store, MemoryProvider())
    record = work.create("Record goal", success={"goal": "done"})
    proposed = work.run(record["id"])
    assert proposed["status"] == "awaiting_approval"
    assert work.run(record["id"]) == proposed
    assert work.adjudicate(record["id"], "approved", "operator", "Reviewed exact proposal")[
        "status"] == "completed"


def test_abstention_is_a_durable_stop_and_cannot_be_approved(store):
    work = Coordinator(store)
    created = work.create("Preserve options")
    problem = {"scenarios": [{"id": "loss", "probability": 1}],
               "actions": [{"id": "harm", "utilities": {"loss": -10}}]}
    stopped = work.plan(created["id"], problem, {"set": {"harm": True}})
    assert stopped["status"] == "abstained"
    assert work.run(stopped["id"]) == stopped
    with pytest.raises(ConflictError):
        work.audit.adjudicate(stopped["proposal_id"], "approved", "operator", "Cannot act")
    assert store.snapshot()["memory"] == {}


def test_reviewed_external_target_cannot_be_redirected_after_approval(store, tmp_path):
    work = Coordinator(store, executors={"artifact": ArtifactExecutor(tmp_path / "reviewed")})
    record = external_plan(work)
    proposal = work.audit.get(record["proposal_id"])
    assert proposal["effect"]["binding"]["directory"] == str((tmp_path / "reviewed").resolve())
    work.executors["artifact"] = ArtifactExecutor(tmp_path / "unreviewed")
    with pytest.raises(ConflictError, match="configuration"):
        work.advance(record["id"])
    assert not list((tmp_path / "reviewed").iterdir())
    assert not list((tmp_path / "unreviewed").iterdir())
    assert not store.db.execute("SELECT 1 FROM external_claims").fetchone()
