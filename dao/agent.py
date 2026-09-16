"""Conversation orchestration. All mutations create a commit with a checked parent."""

from datetime import datetime, timezone

from .decision import evaluate, validate_decision
from .examples import initial_state
from .store import ConflictError


def now():
    return datetime.now(timezone.utc).isoformat()


class Agent:
    def __init__(self, store, provider=None):
        self.store, self.provider = store, provider
        self.store.initialize(initial_state())

    @property
    def mode(self):
        return "ai" if self.provider else "demo"

    def workspace(self, branch="main"):
        commit = self.store.read(branch)
        return {"branch": branch, "commit": commit, "branches": self.store.branches(),
                "history": self.store.history(commit["id"]), "mode": self.mode,
                "analysis": evaluate(commit["state"]["decision"])}

    def _snapshot(self, branch, expected_head):
        commit = self.store.read(branch)
        if not isinstance(expected_head, str) or expected_head != commit["id"]:
            raise ConflictError("The branch changed. Reload before trying again.")
        return commit["state"]

    def decision(self, branch, problem, expected_head):
        state = self._snapshot(branch, expected_head)
        state["decision"] = validate_decision(problem)
        return self.store.commit(branch, state, "Update decision assumptions", expected_head)

    def merge(self, branch, source, expected_head):
        return self.store.merge(branch, source, expected_head,
                                validate=lambda state: validate_decision(state["decision"]))

    def choose(self, branch, action_id, expected_head):
        state = self._snapshot(branch, expected_head)
        actions = {a["id"]: a for a in state["decision"]["actions"]}
        if action_id not in actions:
            raise ValueError("Choose an action in this decision.")
        state["choices"].append({"action_id": action_id, "at": now(), "decision_commit": expected_head,
                                  "kind": "recorded_intent", "external_effect": False})
        state["messages"].append({"role": "assistant", "content":
            f"Recorded your choice: {actions[action_id]['label']}. This records intent; no external action was performed.", "at": now()})
        return self.store.commit(branch, state, "Record choice: " + action_id, expected_head)

    def chat(self, branch, message, expected_head):
        if not isinstance(message, str) or not message.strip() or len(message) > 12000:
            raise ValueError("Enter a message between 1 and 12,000 characters.")
        message = message.strip()
        state = self._snapshot(branch, expected_head)
        state["messages"].append({"role": "user", "content": message, "at": now()})
        analysis = evaluate(state["decision"])
        if message.startswith("/remember "):
            key, sep, value = message[10:].partition("=")
            key, value = key.strip(), value.strip()
            if not sep or not key or len(key) > 80 or not value:
                raise ValueError("Use /remember key=value (key up to 80 characters).")
            state["notes"][key] = value
            reply = f"Remembered {key}: {value}. This note belongs to this branch."
        elif message.startswith("/forget "):
            key = message[8:].strip()
            if key not in state["notes"]:
                raise ValueError("That note does not exist on this branch.")
            del state["notes"][key]
            reply = f"Removed {key} from the current state. Earlier commits still retain it."
        elif message.startswith("/observe "):
            signal_id = message[9:].strip()
            policies = analysis["wait"]["policy"] if analysis["wait"] else []
            policy = next((p for p in policies if p["signal_id"] == signal_id), None)
            if not policy or policy["probability"] <= 0:
                raise ValueError("Select a possible signal in the current waiting model.")
            state["observations"].append({"signal_id": signal_id, "at": now(), "decision_commit": expected_head,
                                          "cost": state["decision"]["wait"]["cost"]})
            for s in state["decision"]["states"]:
                s["probability"] = policy["posterior"][s["id"]]
            state["decision"].pop("wait", None)
            reply = f"Recorded your observation '{signal_id}' and updated beliefs using Bayes' rule. The signal is consumed; add a new observation model to evaluate waiting again.\n\n" + self.explain(evaluate(state["decision"]))
        elif message in ("/decide", "/help") or not self.provider:
            reply = self.explain(analysis)
            if message != "/decide":
                reply += "\n\nUse /model to inspect assumptions, /branch name to explore, or /remember key=value, /forget key, /observe signal, and /decide. Type /help for terminal commands."
                if not self.provider:
                    reply += " Demo mode uses deterministic guidance. Enable AI mode for open-ended conversation."
        else:
            memories = self.store.search(message, ref=expected_head)
            reply = self.provider.reply(state["messages"], {
                "branch": branch, "notes": state["notes"], "decision": state["decision"],
                "analysis": analysis, "memory": memories})
        state["messages"].append({"role": "assistant", "content": reply, "at": now()})
        return self.store.commit(branch, state, "Conversation: " + message[:72], expected_head)

    @staticmethod
    def explain(analysis):
        best = next(a for a in analysis["actions"] if a["id"] == analysis["best_action_id"])
        reply = f"With these assumptions, the strongest immediate option is {best['label']} (score {best['score']:.2f})."
        wait = analysis["wait"]
        if wait:
            reply += f" Waiting for the specified signal scores {wait['score']:.2f}, a net option value of {wait['net_option_value']:+.2f} compared with acting now."
        reply += "\n\n" + analysis["recommendation"]["reason"]
        return reply + "\n\nThese scores depend on your probabilities, payoffs, costs, and risk preferences. Reversibility describes the action; restoring conversation state cannot undo effects in the world."
