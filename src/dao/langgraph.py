"""Optional native LangGraph workflows backed by Dao's authority and receipts.

LangGraph owns scheduling and checkpoints. Dao owns branch heads, conversation
history, usage, proposals, adjudication, and execution. A resume value is only a
wakeup signal; it never grants approval or supplies an executable patch.
"""

import json
from typing import Annotated, Any, NotRequired, TypedDict

from .audit import Audit
from .store import ConflictError, canonical_json
from .workflow import WorkflowJournal


class ChatInput(TypedDict):
    run_id: str
    text: str
    expected_head: str
    branch: NotRequired[str]


class ActionInput(TypedDict):
    run_id: str
    expected_head: str
    problem: dict[str, Any]
    patch: dict[str, Any]
    branch: NotRequired[str]
    rationale: NotRequired[Any]


class WorkflowOutput(TypedDict):
    status: str
    request: dict[str, Any]
    head: str | None
    reply: dict[str, Any] | None
    proposal: dict[str, Any] | None
    proposal_id: str | None
    base: str | None
    execution: dict[str, Any] | None


def _dependencies(checkpointer):
    if checkpointer is None or isinstance(checkpointer, bool):
        raise ValueError("an explicit LangGraph checkpointer is required")
    try:
        from langchain_core.runnables import RunnableConfig
        from langgraph.channels import EphemeralValue
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt
    except ImportError as exc:
        raise ImportError("Install Dao's LangGraph extra: pip install 'dao-agent[langgraph]'") from exc
    return RunnableConfig, EphemeralValue, StateGraph, START, END, interrupt


def _schemas(inputs, ephemeral):
    # Pydantic's generated schemas require typing_extensions on Python 3.11.
    # Import it only after the optional LangGraph runtime was requested.
    from typing_extensions import TypedDict as LangGraphTypedDict

    # Input channels expire after prepare. Otherwise LangGraph merges a new
    # partial input with an old checkpoint's text, branch, or expected head.
    channels = {key: Annotated[value, ephemeral(value)] for key, value in inputs.items()}
    input_types = {key: NotRequired[value] if key in {"branch", "rationale"} else value
                   for key, value in inputs.items()}
    input_schema = LangGraphTypedDict("DaoWorkflowInput", input_types)
    output_schema = LangGraphTypedDict("DaoWorkflowOutput", WorkflowOutput.__annotations__)
    state_schema = LangGraphTypedDict(
        "DaoWorkflowState",
        {**channels, **WorkflowOutput.__annotations__, "_route": str},
        total=False,
    )
    return state_schema, input_schema, output_schema


def _prepare(state, config, kind):
    thread_id = config.get("configurable", {}).get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("config.configurable.thread_id must be a nonempty string")
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 200:
        raise ValueError("run_id must contain 1..200 characters")
    head = state.get("expected_head")
    if not isinstance(head, str) or not head.strip():
        raise ValueError("expected_head must be the previously observed Dao commit id")
    branch = state.get("branch", "main")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("branch must be a nonempty string")
    request = {"run_id": run_id, "branch": branch, "expected_head": head}
    if kind == "chat":
        text = state.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ValueError("text must contain 1..32000 characters")
        request["text"] = text
    else:
        if not isinstance(state.get("problem"), dict) or not isinstance(state.get("patch"), dict):
            raise ValueError("problem and patch must be JSON objects")
        request.update(problem=state["problem"], patch=state["patch"],
                       rationale=state.get("rationale"))
    request = json.loads(canonical_json(request))
    return {
        "request": request,
        "status": "pending",
        "head": None,
        "reply": None,
        "proposal": None,
        "proposal_id": None,
        "base": None,
        "execution": None,
        "_route": "",
    }


def _operation(request, kind, step):
    return "dao:" + kind + ":" + request["run_id"] + ":" + step


def compile_chat_graph(agent, *, checkpointer):
    """Compile a replay-protected chat graph with native invoke/stream APIs.

    Every new invocation requires run_id, text, and expected_head, plus a native
    configurable.thread_id. Reusing a run_id with different inputs conflicts.
    An uncertain provider call is never automatically retried; inspect Dao's
    journal and ledger before choosing a new run_id.
    """
    RunnableConfig, ephemeral, StateGraph, START, END, _ = _dependencies(checkpointer)
    journal = WorkflowJournal(agent.store)
    state_schema, input_schema, output_schema = _schemas(
        {"run_id": str, "text": str, "expected_head": str, "branch": str}, ephemeral
    )

    def prepare(state, config: RunnableConfig):
        return _prepare(state, config, "chat")

    def chat(state):
        request = state["request"]
        reply = journal.run_external(
            _operation(request, "chat", "reply"), request,
            lambda: agent.chat(request["text"], branch=request["branch"],
                               expected_head=request["expected_head"]),
        )
        proposal = reply.get("proposal")
        return {
            "status": "proposed" if proposal else "completed",
            "head": reply["head"],
            "reply": reply,
            "proposal": proposal,
            "proposal_id": proposal["id"] if proposal else None,
            "base": proposal["base"] if proposal else None,
        }

    builder = StateGraph(state_schema, input_schema=input_schema, output_schema=output_schema)
    builder.add_node("prepare", prepare)
    builder.add_node("chat", chat)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "chat")
    builder.add_edge("chat", END)
    return builder.compile(checkpointer=checkpointer)


def compile_action_graph(store, *, checkpointer):
    """Compile propose -> independent adjudication -> authorized execution.

    The graph pauses for an act recommendation. A trusted operator adjudicates
    through Dao's Audit/CLI, then wakes the graph with Command(resume=...). All
    approval, integrity, and stale-head checks remain enforced by Audit.execute.
    Wait and abstain recommendations end without executing their patches.
    """
    RunnableConfig, ephemeral, StateGraph, START, END, interrupt = _dependencies(checkpointer)
    audit, journal = Audit(store), WorkflowJournal(store)
    state_schema, input_schema, output_schema = _schemas(
        {"run_id": str, "expected_head": str, "branch": str, "problem": dict,
         "patch": dict, "rationale": Any}, ephemeral
    )

    def prepare(state, config: RunnableConfig):
        return _prepare(state, config, "action")

    def proposal_for(request):
        with store.transaction():
            receipt = journal.run_local(
                _operation(request, "action", "propose"), request,
                lambda: audit.propose(request["branch"], request["problem"], request["patch"],
                                      request["expected_head"], rationale=request["rationale"]),
            )
            if not audit.verify_integrity()["ok"]:
                raise ConflictError("state, event, or adjudication integrity failed")
            # Checkpoints can carry display data, but the Dao receipt chooses
            # the proposal and Dao's audit record supplies its contents.
            proposal = audit.get(receipt["id"])
            if canonical_json(proposal) != canonical_json(receipt):
                raise ConflictError("workflow receipt differs from the authoritative proposal")
            return proposal

    def execution_request(request, proposal):
        return {"request": request, "proposal_id": proposal["id"], "base": proposal["base"]}

    def execution_for(request, proposal):
        with store.transaction():
            if not audit.verify_integrity()["ok"]:
                raise ConflictError("state, event, or adjudication integrity failed")
            return journal.run_local(
                _operation(request, "action", "execute"), execution_request(request, proposal),
                lambda: audit.execute(proposal["id"], proposal["base"]),
            )

    def propose(state):
        proposal = proposal_for(state["request"])
        kind = proposal["evaluation"]["recommendation"]["kind"]
        return {
            "status": "proposed" if kind == "act" else kind,
            "head": proposal["base"],
            "proposal": proposal,
            "proposal_id": proposal["id"],
            "base": proposal["base"],
            "_route": "review" if kind == "act" else "end",
        }

    def review_snapshot(request, proposal):
        with store.transaction() as db:
            # A successful local execution may precede a failed checkpoint.
            # Recover its receipt before treating the advanced head as stale.
            executed = db.execute(
                "SELECT 1 FROM executions WHERE proposal_id=?", (proposal["id"],)
            ).fetchone()
            if executed:
                execution = execution_for(request, proposal)
                return {"status": "executed", "execution": execution,
                        "head": execution["commit"], "_route": "end"}
            if store.head(proposal["branch"]) != proposal["base"]:
                raise ConflictError("reviewed state changed; reassess and start a new run")
            row = db.execute(
                "SELECT payload FROM rulings WHERE proposal_id=? ORDER BY seq DESC LIMIT 1",
                (proposal["id"],),
            ).fetchone()
            verdict = json.loads(row[0])["verdict"] if row else None
            if verdict == "approved":
                return {"status": "proposed", "_route": "execute"}
            if verdict == "rejected":
                return {"status": "rejected", "_route": "end"}
            return {"status": "proposed", "_route": "review"}

    def review(state):
        request = state["request"]
        proposal = proposal_for(request)
        references = {"proposal": proposal, "proposal_id": proposal["id"],
                      "base": proposal["base"], "head": proposal["base"]}
        snapshot = review_snapshot(request, proposal)
        if snapshot["_route"] != "review":
            return {**references, **snapshot}
        interrupt({
            "kind": "dao.adjudication_required",
            "proposal_id": proposal["id"],
            "branch": proposal["branch"],
            "base": proposal["base"],
            "recommendation": proposal["evaluation"]["recommendation"],
            "evaluation": proposal["evaluation"],
            "patch": proposal["patch"],
            "rationale": proposal["rationale"],
            "instruction": "Adjudicate independently with Dao, then resume to recheck the ruling.",
        })
        # LangGraph restarts this node on resume. The payload above is a
        # notification; the supplied resume value is intentionally discarded.
        return {**references, **review_snapshot(request, proposal)}

    def execute(state):
        request = state["request"]
        proposal = proposal_for(request)
        result = execution_for(request, proposal)
        return {"status": "executed", "execution": result, "head": result["commit"],
                "proposal": proposal, "proposal_id": proposal["id"], "base": proposal["base"]}

    builder = StateGraph(state_schema, input_schema=input_schema, output_schema=output_schema)
    for name, node in (("prepare", prepare), ("propose", propose), ("review", review),
                       ("execute", execute)):
        builder.add_node(name, node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "propose")
    builder.add_conditional_edges("propose", lambda state: state["_route"],
                                  {"review": "review", "end": END})
    builder.add_conditional_edges("review", lambda state: state["_route"],
                                  {"review": "review", "execute": "execute", "end": END})
    builder.add_edge("execute", END)
    return builder.compile(checkpointer=checkpointer)
