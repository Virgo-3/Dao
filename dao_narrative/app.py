"""A writing runtime over Dao's durable drafts, versions, and usage accounting."""

from copy import deepcopy
import re

from dao.service import Dao, text

from .provider import demo_stream, live_stream, reserve


class NarrativeDao(Dao):
    def supports_endpoint(self, path):
        return path not in {"/api/decision", "/api/decision-example", "/api/audit",
                            "/api/relationships", "/api/verify"}

    def snapshot(self, branch="main"):
        return {"config": self.config.public(), "branches": self.store.branches(),
                "head": self.store.head(branch), "history": self.store.history(branch),
                "usage": self.store.usage()}

    def export(self, branch="main"):
        head = self.store.head(branch)
        state = head["state"]
        lines = [f"# {branch}", "", f"Saved version: {head['id']}", "", "## Story notes", ""]
        for key, value in state.get("memory", {}).items():
            lines.extend([f"### {key}", "", str(value), ""])
        lines.extend(["## Documents", ""])
        for name, document in state.get("artifacts", {}).items():
            content = document.get("content", "") if isinstance(document, dict) else document
            lines.extend([f"### {name}", "", str(content), ""])
        lines.extend(["## Conversation", ""])
        for message in state.get("messages", []):
            label = "You" if message["role"] == "user" else "Dao Narrative"
            if message.get("status") == "failed":
                label += " (incomplete reply)"
            lines.extend([f"### {label}", "", message["content"], ""])
        filename = re.sub(r"[^a-zA-Z0-9_-]", "_", branch)
        return {"name": f"story-{filename}.md", "content": "\n".join(lines)}

    def demo_response(self, state):
        return demo_stream(state)

    def model_reservation(self, state):
        return reserve(state, self.config)

    def live_response(self, state):
        return live_stream(state, self.config)

    def memory_message(self, key, value):
        return f"Saved story note {key}: {value}. This note stays with this draft."

    def chat(self, data):
        message = data.get("message", "")
        if (isinstance(message, str) and message.strip().startswith("/") and not data.get("literal", False)
                and not message.strip().startswith("/remember ")):
            raise ValueError("Unknown writing command. Use /remember key=value or send literal text with /say.")
        yield from super().chat(data)

    def mutate(self, route, data):
        if route in {"/api/branches", "/api/restore", "/api/memory"}:
            return super().mutate(route, data)
        if route != "/api/artifact":
            raise KeyError("Unknown writing endpoint")
        if set(data) - {"branch", "expected_head", "name", "content"}:
            raise ValueError("Document saving accepts only name and content with the current draft and version")
        name = text(data.get("name"), "document name", 100)
        content = data.get("content")
        if not isinstance(content, str) or len(content) > 32768:
            raise ValueError("Document content must be text up to 32768 characters")
        branch = data.get("branch", "main")
        with self.branch_lock(branch):
            head = self.checked_head(branch, data.get("expected_head"))
            state = deepcopy(head["state"])
            documents = state.setdefault("artifacts", {})
            if len(documents) >= 100 and name not in documents:
                raise ValueError("A draft can contain up to 100 documents")
            documents[name] = {"content": content}
            saved = self.store.commit(branch, state, "artifact", f"Save document {name}", head["id"])
            return {"head": saved, "result": None}
