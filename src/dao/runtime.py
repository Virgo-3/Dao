"""Conversation runtime selection shared by CLI and MCP."""

from pathlib import Path
import uuid

from .store import ConflictError, NotFoundError


def chat_with_runtime(agent, text, branch="main", expected_head=None, *,
                      runtime="direct", checkpoint_db=None, run_id=None):
    """Keep Agent's reply interface while optionally driving a durable graph.

    A host can supply a stable run_id on transport retries. It names a turn, not
    a conversation: Dao's selected branch owns conversation continuity.
    """
    if runtime == "direct":
        if run_id is not None:
            raise ValueError("run_id requires the langgraph runtime")
        return agent.chat(text, branch, expected_head)
    if runtime != "langgraph":
        raise ValueError("runtime must be direct or langgraph")
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:
        raise RuntimeError('LangGraph runtime requires pip install "dao-agent[langgraph]"') from exc

    from .langgraph import compile_chat_graph
    from .workflow import WorkflowJournal

    if run_id is None:
        run_id = uuid.uuid4().hex
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 200:
        raise ValueError("run_id must contain 1..200 characters")
    operation_id = "dao:chat:" + run_id + ":reply"
    journal = WorkflowJournal(agent.store)
    if expected_head is None:
        try:
            expected_head = journal.get(operation_id)["request"]["expected_head"]
        except NotFoundError:
            expected_head = agent.store.head(branch)
    if checkpoint_db is None:
        if agent.store.path == ":memory:":
            raise ValueError("an in-memory Dao store requires an explicit checkpoint_db")
        checkpoint_db = agent.store.path + ".checkpoints.db"
    if str(checkpoint_db) == ":memory:":
        raise ValueError("checkpoint_db must name a persistent file")
    checkpoint_path = Path(checkpoint_db).resolve()
    if agent.store.path != ":memory:" and checkpoint_path == Path(agent.store.path).resolve():
        raise ValueError("checkpoint_db must differ from Dao's state database")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    thread_id = "dao:chat:" + run_id
    try:
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            graph = compile_chat_graph(agent, checkpointer=saver)
            output = graph.invoke(
                {"run_id": run_id, "branch": branch, "text": text, "expected_head": expected_head},
                {"configurable": {"thread_id": thread_id}},
                durability="sync",
            )
    except Exception as exc:
        # Reveal recovery identifiers even when there is no successful reply.
        # Unknown exception strings can contain provider secrets; retain those
        # only as the cause, not in the user-facing CLI/MCP error.
        known = type(exc) in (ValueError, ConflictError, NotFoundError, RuntimeError)
        reason = str(exc) if known else type(exc).__name__
        error_type = type(exc) if known else RuntimeError
        raise error_type(
            "workflow run " + run_id + " stopped (operation " + operation_id + "): " + reason
            + "; inspect this operation's receipt, state, and usage before retrying"
        ) from exc
    return {**output["reply"], "workflow": {"run_id": run_id, "thread_id": thread_id,
                                            "operation_id": operation_id}}
