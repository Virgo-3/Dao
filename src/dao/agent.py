"""Conversation orchestration and constrained tool use."""

import json
import uuid

from .audit import Audit
from .ledger import Ledger
from .projection import project_state
from .providers import OfflineProvider, TOOLS, parse_arguments
from .store import ConflictError


def memory_problem(reason):
    return {
        "scenarios": [{"id": "requested", "probability": 1.0}],
        "actions": [{"id": "update_memory", "utilities": {"requested": 1.0}, "reversibility": 1.0}],
    }


class Agent:
    def __init__(self, store, provider=None, budget_usd=None):
        self.store = store
        self.provider = provider or OfflineProvider()
        self.ledger = Ledger(store, budget_usd)
        self.audit = Audit(store)

    def propose_memory(self, branch, key, value, reason, expected_head):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a reason is required")
        return self.audit.propose(
            branch, memory_problem(reason), {"set": {key: value}}, expected_head, rationale=reason
        )

    def chat(self, text, branch="main", expected_head=None, *, usage_context=None):
        usage_context = {} if usage_context is None else dict(usage_context)
        if set(usage_context) - {"work_id", "operation_id"} or any(
            not isinstance(value, str) or not value for value in usage_context.values()
        ):
            raise ValueError("usage context accepts work_id and operation_id strings")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ValueError("message must contain 1..32000 characters")
        with self.store.transaction():
            head = self.store.head(branch)
            if expected_head is not None and head != expected_head:
                raise ConflictError("branch changed; retry against the new head")
            state = self.store.snapshot(branch)
            state["messages"].append({"role": "user", "content": text})
            self.store.commit(branch, state, "User message", head)
            context_head = self.store.head(branch)
            working = state["messages"][-40:]
        if text == "/state":
            with self.store.transaction():
                if self.store.head(branch) != context_head:
                    raise ConflictError("branch changed before state reply")
                return self._record_reply(branch, json.dumps(state["memory"]))
        if text == "/projection":
            with self.store.transaction():
                if self.store.head(branch) != context_head:
                    raise ConflictError("branch changed before projection reply")
                projection = project_state(self.store, branch)
                reply = self._record_reply(branch, projection["text"])
                reply["projection"] = projection
                return reply
        if text == "/help":
            with self.store.transaction():
                if self.store.head(branch) != context_head:
                    raise ConflictError("branch changed before help reply")
                return self._record_reply(
                    branch,
                    "/state; /projection; /remember KEY JSON; /help. Adjudicate proposals "
                    "with dao adjudicate, then dao execute.",
                )
        if text.startswith("/remember "):
            _, key, raw = text.split(" ", 2)
            with self.store.transaction():
                if self.store.head(branch) != context_head:
                    raise ConflictError("branch changed before proposal")
                return self._finish_reply(
                    branch,
                    "Memory change proposed; adjudication required.",
                    {"key": key, "value": json.loads(raw), "reason": "User requested memory"},
                )

        draft = None
        for _ in range(4):
            request_id = uuid.uuid4().hex
            with self.store.transaction():
                call_head = self.store.head(branch)
                if call_head != context_head:
                    raise ConflictError(
                        "branch changed before generation; retry with fresh context"
                    )
                self.ledger.reserve(
                    request_id, branch, self.provider.reservation_usd,
                    {"head": call_head, **usage_context}
                )
            try:
                reply = self.provider.respond(working, TOOLS)
                self.ledger.settle(
                    request_id,
                    reply.input_tokens,
                    reply.output_tokens,
                    reply.cost_usd,
                    reply.model,
                    {**reply.metadata, **usage_context},
                )
            except Exception as exc:
                # A request may have reached the provider. Preserve liability, not exception text
                # which can contain sensitive request data or authorization information.
                self.ledger.mark_unknown(request_id, type(exc).__name__)
                raise RuntimeError(
                    "provider attempt failed; retained usage reservation " + request_id
                ) from exc
            stale = False
            with self.store.transaction():
                stale = self.store.head(branch) != call_head
                if stale:
                    self.store.append_event(
                        "conversation.orphaned_reply", {"request_id": request_id, "head": call_head}
                    )
                elif not reply.calls:
                    return self._finish_reply(branch, reply.text, draft, request_id)
                elif len(reply.calls) > 8:
                    raise ValueError("provider requested too many tools")
                if not stale:
                    working.extend(reply.continuation)
                for call in [] if stale else reply.calls:
                    try:
                        args = parse_arguments(call["arguments"])
                        if call["name"] == "read_state" and not args:
                            result = self.store.snapshot(branch)["memory"]
                        elif call["name"] == "propose_memory" and set(args) == {
                            "key",
                            "value_json",
                            "reason",
                        }:
                            if draft is not None:
                                raise ValueError(
                                    "one memory proposal per turn; review it before continuing"
                                )
                            from .audit import validate_patch

                            value = json.loads(args["value_json"])
                            validate_patch({"set": {args["key"]: value}})
                            if not isinstance(args["reason"], str) or not args["reason"].strip():
                                raise ValueError("a reason is required")
                            draft = {"key": args["key"], "value": value, "reason": args["reason"]}
                            result = {
                                "status": "draft",
                                "patch": {"set": {args["key"]: value}},
                                "adjudication_required": True,
                            }
                        else:
                            raise ValueError("unsupported tool or arguments")
                    except (ValueError, TypeError) as exc:
                        result = {"error": str(exc)}
                    working.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["id"],
                            "output": json.dumps(result, allow_nan=False),
                        }
                    )
                    self.store.append_event(
                        "conversation.tool",
                        {
                            "branch": branch,
                            "name": call["name"],
                            "request_id": request_id,
                            "result": result,
                        },
                    )
            if stale:
                raise ConflictError("branch changed during generation; usage remains recorded")
        with self.store.transaction():
            if self.store.head(branch) != context_head:
                raise ConflictError("branch changed before final response")
            return self._finish_reply(
                branch, "Tool limit reached; review pending proposal before continuing.", draft
            )

    def _finish_reply(self, branch, text, draft=None, request_id=None):
        with self.store.transaction():
            answer = self._record_reply(branch, text, request_id)
            if draft is not None:
                proposal = self.propose_memory(
                    branch, draft["key"], draft["value"], draft["reason"], answer["head"]
                )
                answer.update(
                    proposal=proposal, proposal_ids=[proposal["id"]], head=proposal["base"]
                )
            return answer

    def _record_reply(self, branch, text, request_id=None):
        with self.store.transaction():
            head = self.store.head(branch)
            state = self.store.snapshot(branch)
            state["messages"].append({"role": "assistant", "content": text})
            new_head = self.store.commit(branch, state, "Assistant reply", head)
            self.store.append_event(
                "conversation.reply",
                {"branch": branch, "commit": new_head, "request_id": request_id},
            )
            return {"text": text, "head": new_head}
