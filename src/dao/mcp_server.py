"""Local stdio MCP. Mutation and adjudication have separate capabilities."""

import argparse
import json
import os
from typing import Any

from .agent import Agent
from .audit import Audit
from .decision import evaluate
from .experience import Experience
from .ledger import Ledger
from .projection import project_state as render_projection
from .providers import OpenAIProvider
from .relational import coherence_diagnostic, compile_relationship_problem
from .runtime import chat_with_runtime
from .store import Store
from .temporal import plan_temporal
from .work import Coordinator


def create_server(db_path, allow_adjudication=False, provider=None, allow_observation=False,
                  *, runtime="direct", checkpoint_db=None):
    from mcp.server.fastmcp import FastMCP

    store = Store(db_path)
    agent = Agent(store, provider)
    audit = Audit(store)
    experiences = Experience(store)
    coordinator = Coordinator(store, provider)
    mcp = FastMCP(
        "Dao",
        instructions="Versioned local state and evidence-bound proposals. "
        "Inspect state and proposal before requesting adjudication. Waiting is a decision.",
    )

    @mcp.tool()
    def start_work(objective: str, success: dict | None = None, branch: str = "main",
                   work_id: str | None = None, expected_head: str | None = None,
                   max_turns: int = 20) -> dict[str, Any]:
        """Create a durable objective. Success maps memory keys to exact expected values."""
        return coordinator.create(objective, success=success, branch=branch, work_id=work_id,
                                  expected_head=expected_head, max_turns=max_turns)

    @mcp.tool()
    def read_work(work_id: str) -> dict[str, Any]:
        """Read objective progress, source references, and attributed usage."""
        return coordinator.get(work_id)

    @mcp.tool()
    def list_work(limit: int = 50) -> list[dict]:
        """List durable objectives, including stopped and completed work."""
        return coordinator.list(limit)

    @mcp.tool()
    def work_history(work_id: str) -> list[dict]:
        """Read the advancing work history; branch restore cannot rewind it."""
        return coordinator.history(work_id)

    @mcp.tool()
    def advance_work(work_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        """Advance one permitted step. Polling review or waiting does not call a provider."""
        return coordinator.advance(work_id, expected_revision=expected_revision)

    @mcp.tool()
    def run_work(work_id: str, max_steps: int = 100) -> dict[str, Any]:
        """Carry an objective forward until approval, evidence, input, or completion is needed."""
        return coordinator.run(work_id, max_steps=max_steps)

    @mcp.tool()
    def plan_work(work_id: str, problem: dict, patch: dict,
                  expected_revision: int | None = None, expected_head: str | None = None,
                  wait_for: dict | None = None) -> dict[str, Any]:
        """Supply explicit decision assumptions; a waiting decision needs source and topic."""
        return coordinator.plan(work_id, problem, patch, expected_revision=expected_revision,
                                expected_head=expected_head, wait_for=wait_for)

    @mcp.resource("dao://work/{work_id}")
    def work_resource(work_id: str) -> str:
        return json.dumps(coordinator.get(work_id))

    @mcp.tool()
    def read_state(branch: str = "main") -> dict:
        """Read a branch snapshot and its immutable commit id."""
        with store.transaction():
            head = store.head(branch)
            return {"head": head, "state": store.snapshot(head)}

    @mcp.tool()
    def project_state(branch: str = "main") -> dict[str, Any]:
        """Read a sourced natural-language projection of recorded state without changing it."""
        return render_projection(store, branch)

    @mcp.tool()
    def list_branches() -> dict:
        """List all branch heads."""
        return store.branches()

    @mcp.tool()
    def branch_state(name: str, from_ref: str = "main") -> dict:
        """Fork state from a branch or commit without duplicating physical spend."""
        return {"head": store.branch(name, from_ref)}

    @mcp.tool()
    def history(branch: str = "main", limit: int = 50) -> list[dict]:
        """Read version history."""
        return store.log(branch, limit)

    @mcp.tool()
    def diff_state(a: str, b: str) -> list[dict]:
        """Compare states at branches or commits."""
        return store.diff(a, b)

    @mcp.tool()
    def merge_state(branch: str, source: str, expected_head: str) -> dict:
        """Three-way merge, rejecting stale heads and unresolved conflicts."""
        return {"head": store.merge(branch, source, expected_head)}

    @mcp.tool()
    def revert_state(branch: str, commit_id: str, expected_head: str) -> dict:
        """Restore a snapshot as a new commit; audit and usage remain durable."""
        return {"head": store.revert(branch, commit_id, expected_head)}

    @mcp.tool()
    def decide(problem: dict) -> dict:
        """Compare actions, abstention, and Bayesian value of waiting for evidence."""
        return evaluate(problem)

    @mcp.tool()
    def register_relationship(
        branch: str, relationship_id: str, subject: str, object: str,
        dimension: str, prior: dict | None, expected_head: str,
    ) -> dict:
        """Create a branchable relationship belief with an explicit or unassessed prior."""
        return experiences.register_relationship(
            branch, relationship_id, subject, object, dimension, prior, expected_head
        )

    @mcp.tool()
    def relationship(branch: str, relationship_id: str) -> dict:
        """Read a branch's current relationship belief and its evidence references."""
        return experiences.relationship(branch, relationship_id)

    @mcp.tool()
    def relationship_coherence(
        branch: str = "main", relationship_ids: list[str] | None = None
    ) -> dict:
        """Read a scoped, belief-based coherence diagnostic; never a goal score."""
        with store.transaction():
            head = store.head(branch)
            state = store.snapshot(branch)
        known = state.get("relationships", {})
        if not isinstance(known, dict):
            raise ValueError("relationships state must be an object")
        ids = sorted(known) if relationship_ids is None else relationship_ids
        if (not isinstance(ids, list)
                or any(not isinstance(item, str) or not item for item in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("relationship_ids must be a list of unique IDs")
        rows = []
        for identifier in ids:
            if identifier not in known:
                raise ValueError("unknown relationship in coherence scope: " + identifier)
            rows.append(known[identifier])
        return {"head": head, **coherence_diagnostic(rows)}

    @mcp.tool()
    def evaluate_relationship(
        branch: str, relationship_id: str, actions: list[dict],
        signals: list[dict] | None = None, wait_cost: float = 0.0,
        policy: dict | None = None, utility_unit: str = "modeled utility units",
    ) -> dict:
        """Compile a relationship belief into the existing decision evaluator."""
        with store.transaction():
            head = store.head(branch)
            relation = experiences.relationship(branch, relationship_id)
        model = compile_relationship_problem(
            relation, actions, signals, wait_cost, policy, utility_unit,
        )
        return {
            **evaluate(model),
            "relationship_context": {
                "id": relationship_id,
                "belief_head": head,
                "basis": relation["basis"],
                "evidence_ids": relation["evidence_ids"],
            },
        }

    @mcp.tool()
    def plan_relationship(
        branch: str, relationship_id: str, actions: list[dict],
        horizon: int, budget: float, max_loss: float | None = None,
        utility_unit: str = "modeled utility units",
        resource_unit: str = "modeled resource units",
    ) -> dict:
        """Compare bounded action-dependent plans against a branch belief."""
        with store.transaction():
            head = store.head(branch)
            relation = experiences.relationship(branch, relationship_id)
        problem = {
            "states": ["enhancing", "neutral", "degrading"],
            "prior": relation["belief"],
            "actions": actions,
            "horizon": horizon,
            "budget": budget,
            "utility_unit": utility_unit,
            "resource_unit": resource_unit,
        }
        if max_loss is not None:
            problem["max_loss"] = max_loss
        result = plan_temporal(problem)
        return {
            **result,
            "relationship_id": relationship_id,
            "belief_head": head,
            "belief_basis": relation["basis"],
            "evidence_ids": relation["evidence_ids"],
        }

    @mcp.tool()
    def list_observations(limit: int = 50) -> list[dict]:
        """Read durable observations; source claims are not authenticated by the record."""
        return experiences.list_observations(limit)

    @mcp.tool()
    def apply_observation(branch: str, observation_id: str, expected_head: str) -> dict:
        """Apply durable evidence to a branch belief using an expected head."""
        return experiences.apply_observation(branch, observation_id, expected_head)

    @mcp.tool()
    def propose_action(branch: str, problem: dict, patch: dict, expected_head: str) -> dict:
        """Record a decision and local memory patch for separate adjudication."""
        return audit.propose(branch, problem, patch, expected_head)

    @mcp.tool()
    def propose_relationship_action(
        branch: str, relationship_id: str, actions: list[dict], patch: dict,
        expected_head: str, signals: list[dict] | None = None,
        wait_cost: float = 0.0, policy: dict | None = None,
        utility_unit: str = "modeled utility units",
    ) -> dict:
        """Bind a local action proposal to a relationship belief at the reviewed head."""
        with store.transaction():
            if store.head(branch) != expected_head:
                raise ValueError("relationship belief changed; reassess against current head")
            relation = experiences.relationship(branch, relationship_id)
            if relation.get("basis") == "unassessed":
                raise ValueError("relationship action requires an assessed prior")
            problem = compile_relationship_problem(
                relation, actions, signals, wait_cost, policy, utility_unit
            )
            rationale = {
                "relationship_id": relationship_id,
                "evidence_ids": relation["evidence_ids"],
                "belief_head": expected_head,
            }
            return audit.propose(branch, problem, patch, expected_head, rationale)

    @mcp.tool()
    def propose_temporal_action(
        branch: str, relationship_id: str, actions: list[dict], patch: dict,
        horizon: int, budget: float, expected_head: str,
        max_loss: float | None = None,
        utility_unit: str = "modeled utility units",
        resource_unit: str = "modeled resource units",
    ) -> dict:
        """Propose an approved local patch for a planner-selected first act."""
        with store.transaction():
            if store.head(branch) != expected_head:
                raise ValueError("relationship belief changed; reassess against current head")
            relation = experiences.relationship(branch, relationship_id)
            problem = {
                "states": ["enhancing", "neutral", "degrading"],
                "prior": relation["belief"],
                "actions": actions,
                "horizon": horizon,
                "budget": budget,
                "utility_unit": utility_unit,
                "resource_unit": resource_unit,
            }
            if max_loss is not None:
                problem["max_loss"] = max_loss
            rationale = {
                "relationship_id": relationship_id,
                "evidence_ids": relation["evidence_ids"],
                "belief_head": expected_head,
            }
            return audit.propose_temporal_memory(
                branch, relationship_id, problem, patch, expected_head, rationale
            )

    @mcp.tool()
    def propose_evidence(
        branch: str, problem: dict, evidence_plan: dict, expected_head: str,
    ) -> dict:
        """Propose a cost-bounded evidence plan for separate authorization."""
        return audit.propose_evidence(branch, problem, evidence_plan, expected_head)

    @mcp.tool()
    def list_proposals(limit: int = 50) -> list[dict]:
        """Read proposed effects and their exact decision evidence."""
        return audit.proposals(limit)

    @mcp.tool()
    def execute_action(proposal_id: str, expected_head: str) -> dict:
        """Execute once only after trusted approval at this exact head."""
        return audit.execute(proposal_id, expected_head)

    @mcp.tool()
    def chat(message: str, branch: str = "main", expected_head: str | None = None,
             run_id: str | None = None) -> dict:
        """Converse on a branch. Provider tools can propose but cannot adjudicate."""
        return chat_with_runtime(
            agent, message, branch, expected_head, runtime=runtime,
            checkpoint_db=checkpoint_db, run_id=run_id,
        )

    @mcp.tool()
    def workflow_operation(operation_id: str) -> dict:
        """Inspect a workflow receipt, including unfinished work that needs review."""
        from .workflow import WorkflowJournal

        return WorkflowJournal(store).get(operation_id)

    @mcp.tool()
    def usage() -> dict:
        """Read global physical usage and reservations across all branches."""
        ledger = Ledger(store)
        return {
            "summary": ledger.summary(),
            "entries": ledger.entries(),
            "reservations": ledger.reservations(),
        }

    @mcp.tool()
    def verify_integrity() -> dict:
        """Verify commit hashes, event hash chain, and reference integrity."""
        result = coordinator.verify_integrity()
        observation_result = experiences.verify_integrity()
        issues = result["issues"] + observation_result["issues"]
        if store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_operations'"
        ).fetchone():
            from .workflow import WorkflowJournal

            issues += WorkflowJournal(store).verify_integrity()["issues"]
        issues = list(dict.fromkeys(issues))
        return {**result, "ok": not issues, "issues": issues}

    @mcp.resource("dao://branches")
    def branches_resource() -> str:
        return json.dumps(store.branches())

    @mcp.resource("dao://state/{branch}")
    def state_resource(branch: str) -> str:
        return json.dumps(store.snapshot(branch))

    @mcp.resource("dao://projection/{branch}")
    def projection_resource(branch: str) -> str:
        return render_projection(store, branch)["text"]

    @mcp.resource("dao://audit")
    def audit_resource() -> str:
        return json.dumps(store.events())

    if allow_adjudication:

        @mcp.tool()
        def adjudicate_work(work_id: str, verdict: str, actor: str, reason: str,
                            evidence: dict | None = None,
                            expected_revision: int | None = None) -> dict[str, Any]:
            """Trusted review of the current work proposal, followed by its permitted transition."""
            return coordinator.adjudicate(work_id, verdict, actor, reason, evidence,
                                          expected_revision=expected_revision)

        @mcp.tool()
        def recover_work(work_id: str, actor: str, reason: str, evidence: dict,
                         expected_revision: int | None = None) -> dict[str, Any]:
            """Quiescent trusted operator only: recover after reconciling state and usage."""
            return coordinator.recover(work_id, actor, reason, evidence,
                                       expected_revision=expected_revision)

        @mcp.tool()
        def complete_work(work_id: str, actor: str, reason: str, evidence: dict,
                          expected_revision: int | None = None) -> dict[str, Any]:
            """Trusted outcome certification; required memory predicates must actually hold."""
            return coordinator.complete(work_id, actor, reason, evidence,
                                        expected_revision=expected_revision)

        @mcp.tool()
        def adjudicate_action(
            proposal_id: str, verdict: str, actor: str, reason: str, evidence: dict | None = None
        ) -> dict:
            """Trusted operators only: rule on the exact proposal and evidence."""
            return audit.adjudicate(proposal_id, verdict, actor, reason, evidence)

        @mcp.tool()
        def review_outcome(
            proposal_id: str, observation_ids: list[str],
            assessment: str, actor: str, reason: str,
        ) -> dict:
            """Append a trusted retrospective assessment without rewriting the ruling."""
            return audit.review_outcome(
                proposal_id, observation_ids, assessment, actor, reason
            )

        @mcp.tool()
        def list_outcome_reviews(proposal_id: str) -> list[dict]:
            """Read retrospective assessments for an executed proposal."""
            return audit.reviews(proposal_id)

    if allow_observation:

        @mcp.tool()
        def observe_work(work_id: str, input_id: str, topic: str, source: str, value: Any,
                         actor: str, expected_revision: int | None = None) -> dict[str, Any]:
            """Trusted evidence submission; matching evidence wakes reassessment, not execution."""
            return coordinator.observe(work_id, input_id, topic, source, value, actor,
                                       expected_revision=expected_revision)

        @mcp.tool()
        def record_observation(
            relationship_id: str, signal: str, likelihoods: dict,
            source: str, actor: str, source_event_id: str,
            observed_at: str | None = None,
            note: str | None = None, proposal_id: str | None = None,
        ) -> dict:
            """Record a trusted client's source claim as durable evidence."""
            existing = experiences.find_observation(source, source_event_id)
            if proposal_id is not None and existing is None:
                audit.check_evidence_plan(proposal_id, relationship_id, source, observed_at)
            return experiences.record_observation(
                relationship_id, signal, likelihoods, source, actor,
                observed_at, note, proposal_id, source_event_id,
            )

    return mcp


def serve(db_path, allow_adjudication=False, provider=None, allow_observation=False,
          *, runtime="direct", checkpoint_db=None):
    create_server(
        db_path, allow_adjudication, provider, allow_observation,
        runtime=runtime, checkpoint_db=checkpoint_db,
    ).run(transport="stdio")


def main():
    parser = argparse.ArgumentParser(description="Dao local stdio MCP")
    parser.add_argument("--db", default=os.environ.get("DAO_DB", ".dao/state.db"))
    parser.add_argument(
        "--allow-adjudication",
        action="store_true",
        help="Expose trusted adjudication capability to this MCP client",
    )
    parser.add_argument(
        "--allow-observation",
        action="store_true",
        help="Expose trusted evidence submission capability to this MCP client",
    )
    parser.add_argument("--provider", choices=("offline", "openai"), default="offline")
    parser.add_argument("--runtime", choices=("direct", "langgraph"), default="direct")
    parser.add_argument("--checkpoint-db", help="Persistent LangGraph checkpoints in a separate file")
    args = parser.parse_args()
    provider = OpenAIProvider() if args.provider == "openai" else None
    serve(args.db, args.allow_adjudication, provider, args.allow_observation,
          runtime=args.runtime, checkpoint_db=args.checkpoint_db)


if __name__ == "__main__":
    main()
