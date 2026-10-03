"""Offline LangGraph chat, independent approval, and SQLite checkpoint restart."""

import json
import tempfile
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from dao.agent import Agent
from dao.audit import Audit
from dao.langgraph import compile_action_graph, compile_chat_graph
from dao.ledger import Ledger
from dao.store import Store
from dao.workflow import WorkflowJournal


def main():
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "state.db"
        checkpoint_path = str(Path(directory) / "checkpoints.db")
        chat_config = {"configurable": {"thread_id": "demo-conversation"}}
        action_config = {"configurable": {"thread_id": "demo-action"}}
        store = Store(state_path)
        try:
            agent = Agent(store)
            chat_request = {
                "run_id": "demo-chat-1",
                "text": "Explain how review protects a memory update.",
                "branch": "main",
                "expected_head": store.head("main"),
            }
            with SqliteSaver.from_conn_string(checkpoint_path) as checkpointer:
                chat = compile_chat_graph(agent, checkpointer=checkpointer)
                chat_result = chat.invoke(chat_request, config=chat_config, durability="sync")
                assert chat_result["reply"]["text"]
                usage_before = agent.ledger.summary()
                assert usage_before["usage_count"] == 1

                action = compile_action_graph(store, checkpointer=checkpointer)
                paused = action.invoke(
                    {
                        "run_id": "demo-action-1",
                        "problem": json.loads(
                            (Path(__file__).parent / "memory-decision.json").read_text()
                        ),
                        "patch": {"set": {"goal": "Preserve options while learning"}},
                        "branch": "main",
                        "expected_head": store.head("main"),
                        "rationale": "Operator requested this exact local memory update",
                    },
                    config=action_config,
                    durability="sync",
                )
                assert paused["__interrupt__"]
                proposal_id = paused["proposal_id"]
                assert "goal" not in store.snapshot("main")["memory"]

            # This is a trusted operator action, separate from graph resume data.
            proposal = Audit(store).get(proposal_id)
            Audit(store).adjudicate(
                proposal_id,
                "approved",
                "demo-operator",
                "Inspected the exact proposed memory patch and unchanged base",
            )
        finally:
            store.close()

        # Recreate the application from only its durable database files.
        store = Store(state_path)
        try:
            with SqliteSaver.from_conn_string(checkpoint_path) as checkpointer:
                action = compile_action_graph(store, checkpointer=checkpointer)
                completed = action.invoke(
                    Command(resume=True), config=action_config, durability="sync"
                )
                assert completed["status"] == "executed"
                assert completed["execution"]["proposal_id"] == proposal_id
                assert store.snapshot("main")["memory"]["goal"] == (
                    "Preserve options while learning"
                )

                # Replaying the same chat operation returns its saved receipt.
                # It does not generate another reply, write messages, or bill twice.
                head = store.head("main")
                chat = compile_chat_graph(Agent(store), checkpointer=checkpointer)
                replayed = chat.invoke(chat_request, config=chat_config, durability="sync")
                assert replayed["reply"] == chat_result["reply"]
                assert store.head("main") == head
                assert Ledger(store).summary() == usage_before
                assert Audit(store).execute(proposal_id, proposal["base"]) == (
                    completed["execution"]
                )
                integrity = Audit(store).verify_integrity()
                assert integrity["ok"]
                workflow_integrity = WorkflowJournal(store).verify_integrity()
                assert workflow_integrity["ok"]
                print(
                    json.dumps(
                        {
                            "status": completed["status"],
                            "proposal_id": proposal_id,
                            "execution": completed["execution"],
                            "memory": store.snapshot("main")["memory"],
                            "usage": Ledger(store).summary(),
                            "integrity": integrity,
                            "workflow_integrity": workflow_integrity,
                        },
                        indent=2,
                    )
                )
        finally:
            store.close()


if __name__ == "__main__":
    main()
