"""Dao command line. Every write targets an explicit branch or proposal."""

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import Agent
from .audit import Audit
from .decision import evaluate
from .executors import ArtifactExecutor
from .ledger import Ledger
from .projection import project_state
from .providers import OfflineProvider, OpenAIProvider
from .runtime import chat_with_runtime
from .store import Store
from .work import Coordinator


def parser():
    p = argparse.ArgumentParser(description="Dao conversational agent and branchable state")
    p.add_argument("--db", default=os.environ.get("DAO_DB", ".dao/state.db"))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    run = sub.add_parser("run", help="Start and advance a durable objective")
    run.add_argument("objective")
    run.add_argument("--branch", default="main")
    run.add_argument("--work-id", help="Stable identity for retrying objective creation")
    run.add_argument("--success", type=Path, help="JSON memory keys and exact expected values")
    run.add_argument("--max-turns", type=int, default=20)
    run.add_argument("--expected-head")
    run.add_argument("--provider", choices=("offline", "openai"), default="offline")
    run.add_argument("--no-advance", action="store_true")
    run.add_argument("--json", action="store_true")
    work = sub.add_parser("work", help="Inspect and advance the objective lifecycle")
    operations = work.add_subparsers(dest="work_command", required=True)
    listing = operations.add_parser("list")
    listing.add_argument("--limit", type=int, default=50)
    for name in ("inspect", "history", "advance", "plan", "observe", "recover", "complete",
                 "reconcile", "adjudicate"):
        child = operations.add_parser(name)
        child.add_argument("work_id")
        if name not in ("inspect", "history"):
            child.add_argument("--expected-revision", type=int)
        if name == "advance":
            child.add_argument("--provider", choices=("offline", "openai"), default="offline")
            child.add_argument("--until-blocked", action="store_true")
        elif name == "plan":
            child.add_argument("problem", type=Path)
            child.add_argument("patch", type=Path, nargs="?")
            child.add_argument("--effect", type=Path)
            child.add_argument("--wait-for", type=Path)
            child.add_argument("--expected-head")
        elif name == "observe":
            child.add_argument("input_id")
            child.add_argument("topic")
            child.add_argument("source")
            child.add_argument("value", type=Path)
            child.add_argument("--actor", required=True)
        elif name == "adjudicate":
            child.add_argument("verdict", choices=("approved", "rejected", "deferred"))
            child.add_argument("--actor", required=True)
            child.add_argument("--reason", required=True)
            child.add_argument("--evidence", type=Path)
        elif name in ("recover", "complete", "reconcile"):
            child.add_argument("evidence", type=Path)
            child.add_argument("--actor", required=True)
            child.add_argument("--reason", required=True)
        if name in {"plan", "advance", "reconcile", "adjudicate"}:
            child.add_argument("--artifact-dir", type=Path,
                               help="Explicitly install the confined artifact executor")
    sub.add_parser("branches")
    for name in ("state", "log"):
        child = sub.add_parser(name)
        child.add_argument("--branch", default="main")
    project = sub.add_parser("project", help="Narrate recorded state at a branch head")
    project.add_argument("--branch", default="main")
    project.add_argument("--json", action="store_true", help="show the full sourced projection")
    fork = sub.add_parser("branch")
    fork.add_argument("name")
    fork.add_argument("--from-ref", default="main")
    diff = sub.add_parser("diff")
    diff.add_argument("a")
    diff.add_argument("b")
    for name, positional in (("merge", "source"), ("revert", "commit_id")):
        child = sub.add_parser(name)
        child.add_argument(positional)
        child.add_argument("--branch", default="main")
        child.add_argument("--expected-head", required=True)
    chat = sub.add_parser("chat")
    chat.add_argument("message", nargs="?")
    chat.add_argument("--branch", default="main")
    chat.add_argument("--provider", choices=("offline", "openai"), default="offline")
    chat.add_argument("--json", action="store_true", help="show the full structured chat result")
    chat.add_argument("--runtime", choices=("direct", "langgraph"), default="direct")
    chat.add_argument("--checkpoint-db", help="Persistent LangGraph checkpoints in a separate file")
    chat.add_argument("--run-id", help="Stable LangGraph turn ID for retrying a one-shot request")
    chat.add_argument("--expected-head", help="Previously observed branch head")
    decide = sub.add_parser("decide")
    decide.add_argument("problem", type=Path)
    propose = sub.add_parser("propose")
    propose.add_argument("problem", type=Path)
    propose.add_argument("patch", type=Path)
    propose.add_argument("--branch", default="main")
    propose.add_argument("--expected-head", required=True)
    sub.add_parser("proposals")
    workflow = sub.add_parser("workflow", help="Inspect a durable workflow operation receipt")
    workflow.add_argument("operation_id")
    ruling = sub.add_parser("adjudicate")
    ruling.add_argument("proposal_id")
    ruling.add_argument("verdict", choices=("approved", "rejected", "deferred"))
    ruling.add_argument("--actor", required=True)
    ruling.add_argument("--reason", required=True)
    ruling.add_argument("--evidence", type=Path)
    execute = sub.add_parser("execute")
    execute.add_argument("proposal_id")
    execute.add_argument("--expected-head", required=True)
    for name in ("usage", "events", "verify"):
        sub.add_parser(name)
    budget = sub.add_parser("budget")
    budget.add_argument("amount_usd")
    resolve = sub.add_parser("resolve-usage")
    resolve.add_argument("request_id")
    resolve.add_argument("--input-tokens", type=int, required=True)
    resolve.add_argument("--output-tokens", type=int, required=True)
    resolve.add_argument("--cost-usd", required=True)
    resolve.add_argument("--model", required=True)
    sub.add_parser("mcp")
    return p


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def print_chat_reply(reply):
    print("Dao: " + reply["text"].strip())
    proposal = reply.get("proposal")
    if proposal is not None:
        print("\nA proposal awaits review. Run dao proposals.")
    print()


def dispatch(args, store):
    cmd = args.command
    if cmd in {"run", "work"}:
        provider = OpenAIProvider() if getattr(args, "provider", "offline") == "openai" else None
        executors = {}
        if getattr(args, "artifact_dir", None) is not None:
            executors["artifact"] = ArtifactExecutor(args.artifact_dir)
        coordinator = Coordinator(store, provider, executors=executors)
        if cmd == "run":
            record = coordinator.create(
                args.objective, success=read_json(args.success) if args.success else None,
                branch=args.branch, work_id=args.work_id, expected_head=args.expected_head,
                max_turns=args.max_turns,
            )
            return record if args.no_advance else coordinator.run(record["id"])
        operation = args.work_command
        if operation == "list":
            return coordinator.list(args.limit)
        if operation == "inspect":
            return coordinator.get(args.work_id)
        if operation == "history":
            return coordinator.history(args.work_id)
        options = {"expected_revision": args.expected_revision}
        if operation == "advance":
            if args.until_blocked:
                if args.expected_revision is not None:
                    coordinator.advance(args.work_id, **options)
                return coordinator.run(args.work_id)
            return coordinator.advance(args.work_id, **options)
        if operation == "plan":
            return coordinator.plan(args.work_id, read_json(args.problem),
                                    read_json(args.patch) if args.patch else None,
                                    effect=read_json(args.effect) if args.effect else None,
                                    expected_head=args.expected_head,
                                    wait_for=read_json(args.wait_for) if args.wait_for else None,
                                    **options)
        if operation == "observe":
            return coordinator.observe(args.work_id, args.input_id, args.topic, args.source,
                                       read_json(args.value), args.actor, **options)
        if operation == "adjudicate":
            return coordinator.adjudicate(args.work_id, args.verdict, args.actor, args.reason,
                                           read_json(args.evidence) if args.evidence else None,
                                           **options)
        return getattr(coordinator, operation)(args.work_id, args.actor, args.reason,
                                              read_json(args.evidence), **options)
    if cmd == "init":
        Ledger(store)
        return {"head": store.head("main"), "db": str(Path(args.db).resolve())}
    if cmd == "state":
        with store.transaction():
            head = store.head(args.branch)
            return {"head": head, "state": store.snapshot(head)}
    if cmd == "project":
        return project_state(store, args.branch)
    if cmd == "branches":
        return store.branches()
    if cmd == "branch":
        return {"head": store.branch(args.name, args.from_ref)}
    if cmd == "log":
        return store.log(args.branch)
    if cmd == "diff":
        return store.diff(args.a, args.b)
    if cmd == "merge":
        return {"head": store.merge(args.branch, args.source, args.expected_head)}
    if cmd == "revert":
        return {"head": store.revert(args.branch, args.commit_id, args.expected_head)}
    if cmd == "decide":
        return evaluate(read_json(args.problem))
    if cmd == "propose":
        return Audit(store).propose(
            args.branch, read_json(args.problem), read_json(args.patch), args.expected_head
        )
    if cmd == "proposals":
        return Audit(store).proposals()
    if cmd == "workflow":
        from .workflow import WorkflowJournal

        return WorkflowJournal(store).get(args.operation_id)
    if cmd == "adjudicate":
        return Audit(store).adjudicate(
            args.proposal_id,
            args.verdict,
            args.actor,
            args.reason,
            read_json(args.evidence) if args.evidence else None,
        )
    if cmd == "execute":
        return Audit(store).execute(args.proposal_id, args.expected_head)
    if cmd == "usage":
        ledger = Ledger(store)
        return {
            "summary": ledger.summary(),
            "entries": ledger.entries(),
            "reservations": ledger.reservations(),
        }
    if cmd == "budget":
        return Ledger(store).set_budget(args.amount_usd)
    if cmd == "resolve-usage":
        return Ledger(store).resolve_unknown(
            args.request_id,
            args.input_tokens,
            args.output_tokens,
            args.cost_usd,
            args.model,
            {"source": "trusted reconciliation"},
        )
    if cmd == "events":
        return store.events()
    if cmd == "verify":
        if store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_revisions'"
        ).fetchone():
            return Coordinator(store).verify_integrity()
        result = Audit(store).verify_integrity()
        if store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_operations'"
        ).fetchone():
            from .workflow import WorkflowJournal

            workflow = WorkflowJournal(store).verify_integrity()
            issues = list(dict.fromkeys(result["issues"] + workflow["issues"]))
            result = {**result, "ok": not issues, "issues": issues}
        return result
    if cmd == "chat":
        if args.message is None and (args.run_id is not None or args.expected_head is not None):
            raise ValueError("--run-id and --expected-head require a one-shot message")
        provider = OpenAIProvider() if args.provider == "openai" else OfflineProvider()
        agent = Agent(store, provider)
        def converse(text):
            return chat_with_runtime(
                agent, text, args.branch, args.expected_head, runtime=args.runtime,
                checkpoint_db=args.checkpoint_db, run_id=args.run_id,
            )
        if args.message is not None:
            return converse(args.message)
        print("Dao. Type /help for commands or /quit to exit.\n")
        prompt = "You: " if sys.stdin.isatty() else ""
        while True:
            try:
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                break
            if line == "/quit":
                break
            try:
                reply = converse(line)
                if args.json:
                    print(json.dumps(reply, indent=2, allow_nan=False))
                else:
                    print_chat_reply(reply)
            except (ValueError, RuntimeError) as exc:
                print("error: " + str(exc), file=sys.stderr)
        return None
    raise ValueError("unknown command")


def main():
    args = parser().parse_args()
    if args.command == "mcp":
        from .mcp_server import serve

        serve(args.db)
        return
    store = Store(args.db)
    try:
        result = dispatch(args, store)
        if result is not None:
            if args.command == "run" and not args.json:
                print("Dao work " + result["id"] + ": " + result["status"])
                print(result["objective"])
                if result.get("reply"):
                    print("Dao: " + result["reply"]["text"])
                if result.get("proposal_id"):
                    print("Proposal: " + result["proposal_id"])
                print("Next: " + result["next"])
            elif args.command == "chat" and not args.json:
                print_chat_reply(result)
            elif args.command == "project" and not args.json:
                if hasattr(sys.stdout, "reconfigure"):
                    sys.stdout.reconfigure(encoding="utf-8")
                print(result["text"])
            else:
                print(json.dumps(result, indent=2, allow_nan=False))
        if args.command == "verify" and not result["ok"]:
            raise SystemExit(1)
    except (ValueError, RuntimeError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        store.close()


if __name__ == "__main__":
    main()
