"""The runtime owns state transitions, tools, and accounting, not the model."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import threading
import uuid

from .audit import adjudicate
from .decision import demo_payload, evaluate
from .provider import ProviderError, demo_stream, openai_stream, reservation
from .relationships import (apply as apply_relationships, blocking_conflicts,
                            digest as relationship_digest, resolution_claim, summarize)
from .store import ConflictError


def text(value, label, limit=8192):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be nonempty text up to {limit} characters")
    return value.strip()


class Dao:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self._locks, self._guard = {}, threading.Lock()

    @contextmanager
    def branch_lock(self, branch):
        branch = text(branch, "branch", 80)
        with self._guard:
            lock = self._locks.setdefault(branch, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ConflictError("This branch is busy. Wait for its current turn to finish.")
        try:
            yield
        finally:
            lock.release()

    def checked_head(self, branch, expected):
        head = self.store.head(branch)
        if head["id"] != expected:
            raise ConflictError("The branch changed. Refresh before trying again.")
        return head

    def snapshot(self, branch="main"):
        head = self.store.head(branch)
        graph = head["state"].get("relationships")
        summary = {**summarize(graph), "digest": relationship_digest(graph)}
        return {"config": self.config.public(), "branches": self.store.branches(),
                "head": head, "history": self.store.history(branch),
                "relationships": summary, "relationship_digest": summary["digest"],
                "usage": self.store.usage(), "events": self.store.events(branch)}

    def evaluate_problem(self, state, problem):
        graph = state.get("relationships")
        blocked = {}
        severe = blocking_conflicts(graph)
        actions = problem.get("actions", []) if isinstance(problem, dict) else []
        if isinstance(actions, list):
            for action in actions:
                name = action.get("name") if isinstance(action, dict) else None
                if isinstance(name, str):
                    name = name.strip()
                    conflicts = [conflict for conflict in severe
                                 if not conflict["actions"] or name in conflict["actions"]]
                    if conflicts:
                        blocked[name] = [conflict["relation_id"] for conflict in conflicts]
        result = evaluate(problem, blocked_actions=blocked)
        result["relationships"] = summarize(graph)
        result["relationship_digest"] = relationship_digest(graph)
        return result

    def adjudicate_problem(self, state, problem):
        problem = dict(problem)
        action = problem.pop("action", None)
        if action is not None:
            action = text(action, "action", 200)
        result = adjudicate(problem)
        graph = state.get("relationships")
        conflicts = blocking_conflicts(graph, action)
        result["action"] = action
        result["relationship_digest"] = relationship_digest(graph)
        result["blocked_conflicts"] = [conflict["relation_id"] for conflict in conflicts]
        if conflicts:
            result["allowed"] = False
            result["verdict"] = "blocked"
            result["reasons"].append("Unresolved severe relationship conflicts withhold permission under this policy.")
        # Permission binds evidence, action scope and relationship policy state.
        canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        result["verdict_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return result

    def mutate(self, route, data):
        if route == "/api/branches":
            return {"branch": self.store.branch(text(data.get("name"), "name", 80),
                                                text(data.get("from_commit"), "from_commit", 64))}
        branch = data.get("branch", "main")
        with self.branch_lock(branch):
            head = self.checked_head(branch, data.get("expected_head"))
            state = deepcopy(head["state"])
            result = None
            if route == "/api/restore":
                return {"head": self.store.restore(branch, data.get("commit_id"), head["id"])}
            if route == "/api/memory":
                key = text(data.get("key"), "key", 100)
                value = text(data.get("value"), "value", 4000)
                if len(state["memory"]) >= 100 and key not in state["memory"]:
                    raise ValueError("Memory is limited to 100 entries")
                state["memory"][key] = value
                kind, label = "memory", f"Remember {key}"
            elif route == "/api/decision":
                result = self.evaluate_problem(state, data.get("problem"))
                state["decisions"].append({"id": uuid.uuid4().hex, "problem": data["problem"], "result": result})
                kind, label = "decision", f"Decision: {result['recommendation']}"
            elif route == "/api/audit":
                problem = {k: v for k, v in data.items() if k not in {"branch", "expected_head"}}
                result = self.adjudicate_problem(state, problem)
                state.setdefault("audits", []).append(result)
                kind, label = "adjudication", f"Audit: {result['verdict']}"
            elif route == "/api/relationships":
                problem = {k: v for k, v in data.items() if k not in {"branch", "expected_head"}}
                if problem.get("operation") == "resolve":
                    if set(problem) - {"operation", "relation_id", "evidence", "threshold", "require_sources"}:
                        raise ValueError("Resolution accepts only relation_id, evidence, threshold, and require_sources")
                    claim = resolution_claim(state.get("relationships"), problem.get("relation_id"))
                    verdict = adjudicate({"claim": claim, **{k: v for k, v in problem.items()
                                         if k in {"evidence", "threshold", "require_sources"}}})
                    problem = {"operation": "resolve", "relation_id": problem.get("relation_id"), "verdict": verdict}
                graph, event = apply_relationships(state.get("relationships"), problem)
                state["relationships"] = graph
                if problem["operation"] == "resolve":
                    state.setdefault("audits", []).append(problem["verdict"])
                result = {**summarize(graph), "digest": relationship_digest(graph), "event": event}
                kind, label = "relationship." + problem["operation"], "Relationship " + problem["operation"]
            elif route == "/api/artifact":
                name = text(data.get("name"), "name", 100)
                content = text(data.get("content"), "content", 32768)
                claim = artifact_claim(name, content)
                verdict = next((a for a in reversed(state.get("audits", [])) if a["claim"] == claim), None)
                if not verdict or not verdict["allowed"] or verdict["verdict_id"] != data.get("verdict_id"):
                    raise ValueError("Artifact needs an allowed adjudication bound to its exact name and content digest")
                if verdict.get("action") is not None:
                    raise ValueError("Artifact needs an unscoped adjudication")
                current_digest = relationship_digest(state.get("relationships"))
                if verdict.get("relationship_digest") != current_digest:
                    raise ValueError("Relationship policy changed; adjudicate the artifact again")
                if blocking_conflicts(state.get("relationships")):
                    raise ValueError("Unresolved severe relationship conflicts withhold artifact permission")
                if len(state["artifacts"]) >= 100 and name not in state["artifacts"]:
                    raise ValueError("Artifacts are limited to 100 entries")
                state["artifacts"][name] = {"content": content, "verdict_id": verdict["verdict_id"]}
                kind, label = "artifact", f"Save artifact {name}"
            else:
                raise KeyError("Unknown endpoint")
            saved = self.store.commit(branch, state, kind, label, head["id"])
            return {"head": saved, "result": result}

    def chat(self, data):
        branch = data.get("branch", "main")
        message = text(data.get("message"), "message")
        literal = data.get("literal", False)
        if type(literal) is not bool:
            raise ValueError("literal must be a boolean")
        with self.branch_lock(branch):
            head = self.checked_head(branch, data.get("expected_head"))
            state = deepcopy(head["state"])
            state["messages"].append({"role": "user", "content": message})
            # Validate context before writing a user turn or reserving a model request.
            reserve = reservation(state, self.config)
            current = self.store.commit(branch, state, "user", " ".join(message.split())[:72], head["id"])
            yield {"type": "start", "head": current}
            chunks, active_id, charged = [], None, False
            output_characters = 0
            try:
                if not literal and message.startswith("/remember "):
                    key, separator, value = message[10:].partition("=")
                    if not separator:
                        raise ValueError("Use /remember key=value")
                    key, value = text(key, "key", 100), text(value, "value", 4000)
                    if len(state["memory"]) >= 100 and key not in state["memory"]:
                        raise ValueError("Memory is limited to 100 entries")
                    state["memory"][key] = value
                    stream = iter([{"type": "delta", "text": f"Saved {key}: {value}. This memory is part of this branch’s state."}])
                elif not literal and message == "/decide":
                    problem = demo_payload()
                    result = self.evaluate_problem(state, problem)
                    state["decisions"].append({"id": uuid.uuid4().hex, "problem": problem, "result": result})
                    stream = iter([{"type": "delta", "text": "Decision example: " + result["reason"] + "\n\nInspect the Decision panel for the inputs and utility calculation."}])
                else:
                    active_id = uuid.uuid4().hex
                    self.store.reserve_usage(branch, active_id, reserve, self.config.token_budget)
                    self.store.append_event(branch, "model_attempt", {"request_id": active_id, "provider": self.config.provider})
                    stream = (demo_stream(state) if self.config.provider == "demo" else
                              openai_stream(state, self.config, lambda problem: self.evaluate_problem(state, problem)))
                for event in stream:
                    if event["type"] == "delta":
                        delta = event["text"]
                        if not isinstance(delta, str):
                            raise ProviderError("Model text delta must be a string")
                        if not delta:
                            continue
                        output_characters += len(delta)
                        if output_characters > 2_000_000:
                            raise ProviderError("Model output exceeds the safety limit")
                        chunks.append(delta)
                        yield event
                    elif event["type"] == "usage":
                        self.store.finalize_usage(active_id, event["input_tokens"], event["output_tokens"],
                                                  event["estimated"], event["status"], self.config.public()["model"],
                                                  self.config.cost(event["input_tokens"], event["output_tokens"]) if self.config.provider == "openai" else 0)
                        charged = True
                    elif event["type"] == "next_round":
                        active_id, charged = uuid.uuid4().hex, False
                        reserve = event["input_bytes"] + 512 + self.config.max_output_tokens
                        self.store.reserve_usage(branch, active_id, reserve, self.config.token_budget)
                        self.store.append_event(branch, "model_attempt", {"request_id": active_id, "provider": self.config.provider, "round": event["round"]})
                    elif event["type"] == "tool":
                        state["decisions"].append({"id": uuid.uuid4().hex, "problem": event["problem"], "result": event["result"]})
                        self.store.append_event(branch, "tool_result", {"name": event["name"], "result": event["result"]})
                        yield {"type": "tool", "name": event["name"], "result": event["result"]}
                    elif event["type"] == "tool_error":
                        self.store.append_event(branch, "tool_rejected", {"name": event["name"], "error": event["error"]})
                        yield {"type": "tool", "name": event["name"], "result": {"error": event["error"]}}
                answer = "".join(chunks)
                if not answer:
                    raise ProviderError("Model completed without a conversational reply")
                state["messages"].append({"role": "assistant", "content": answer, "status": "completed"})
                saved = self.store.commit(branch, state, "assistant", "Dao replied", current["id"])
                yield {"type": "done", "head": saved, "usage": self.store.usage()}
            except Exception as exc:
                if active_id and not charged:
                    # A failed stream can still incur usage; conservatively retain its reservation.
                    try:
                        self.store.finalize_usage(active_id, reserve, 0, True, "unknown", self.config.public()["model"],
                                                  (self.config.cost(0, reserve) if self.config.output_price >= self.config.input_price
                                                   else self.config.cost(reserve, 0)) if self.config.provider == "openai" else 0)
                    except KeyError:
                        pass  # The next round may have failed admission before reservation existed.
                partial = "".join(chunks)
                state["messages"].append({"role": "assistant", "content": partial or "This turn did not complete.", "status": "failed"})
                saved = self.store.commit(branch, state, "failed", "Turn failed; usage retained", current["id"])
                self.store.append_event(branch, "turn_failed", {"error_type": type(exc).__name__, "request_id": active_id})
                # Known errors are useful; unexpected internals stay server-side.
                safe_error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "The turn failed. Inspect the server log."
                yield {"type": "error", "error": safe_error, "head": saved, "usage": self.store.usage()}


def artifact_claim(name, content):
    return "artifact:" + name + ":" + hashlib.sha256(content.encode("utf-8")).hexdigest()
