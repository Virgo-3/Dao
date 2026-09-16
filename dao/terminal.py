"""Terminal conversation and explicit, local workspace commands."""

import json
from pathlib import Path

from .provider import ProviderError
from .store import MAX_STATE_BYTES, StoreError


HELP = """Conversation
  /decide                 Explain the current decision
  /remember key=value     Save context on this branch
  /forget key             Remove current context
  /observe signal         Record evidence and update beliefs
  /search words           Search relevant memories across the entire tree

Workspace
  /branches               List branches and their checkpoints
  /branch name            Fork here and switch to the new branch
  /switch name            Switch to an existing branch
  /history                Show the last 100 checkpoints (newest first)
  /state [ref]            Print a current or referenced snapshot as JSON
  /diff ref               Compare a branch or full commit ID with here
  /restore ref            Restore a snapshot as a new checkpoint
  /merge source           Merge another branch into this branch
  /model [file.json]      Print assumptions, or load an edited JSON file
  /choose action_id       Record intent; no external action is performed
  /export file.json       Save the current snapshot to a new file
  /help                   Show these commands
  /quit                   Exit

File paths may contain spaces; quotes around paths are optional.
Restoring state keeps history and cannot undo external effects."""


def terminal_text(value):
    """Render untrusted text without interpreting terminal control sequences."""
    return "".join(f"\\x{ord(char):02x}" if (ord(char) < 32 and char not in "\n\t")
                   or 127 <= ord(char) <= 159 else char for char in str(value))


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)


def _path(argument):
    if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in "\"'":
        argument = argument[1:-1]
    if not argument:
        raise ValueError("Enter a file path.")
    return Path(argument).expanduser()


class Terminal:
    def __init__(self, agent, branch="main"):
        self.agent = agent
        self.branch = branch
        self.agent.store.head(branch)

    def execute(self, text, expected_head):
        """Handle user input. Model replies never pass through this dispatcher."""
        text = text.strip()
        if not text:
            return ""
        parts = text.split(maxsplit=1)
        command, argument = parts[0], parts[1].strip() if len(parts) == 2 else ""
        store = self.agent.store
        no_argument = {"/help", "/branches", "/history"}
        requires_argument = {"/branch", "/switch", "/diff", "/restore", "/merge", "/choose", "/export", "/search"}
        if command in no_argument and argument:
            raise ValueError(f"Use {command} without arguments.")
        if command in requires_argument and not argument:
            raise ValueError(f"{command} needs an argument. Type /help for usage.")
        if command == "/help":
            return HELP
        if command == "/search":
            result = store.search(argument, ref=expected_head)
            lines = [f"Searched {result['indexed_checkpoints']} checkpoints across the entire tree."]
            for hit in result["results"]:
                branches = ", ".join(hit["source_branches"]) or "unattached checkpoint"
                if hit["source_branch_count"] > len(hit["source_branches"]):
                    branches += f" (+{hit['source_branch_count'] - len(hit['source_branches'])} more branches)"
                status = "present in current state" if hit["in_current_state"] else "historical or alternate state"
                lines.append(f"\n{hit['kind']} · {branches} · {status}\n"
                             f"{hit['source_commit']} {hit['source_path']}\n{hit['excerpt']}")
            if not result["results"]:
                lines.append("No matching memories. Try different keywords.")
            if result["results_truncated"]:
                lines.append("\nShowing the highest-ranked results within the context budget. Narrow the query for more detail.")
            if result["query_terms_truncated"]:
                lines.append("Only the first 64 distinct search terms were used.")
            return "\n".join(lines)
        if command == "/branches":
            return "\n".join(f"{'*' if name == self.branch else ' '} {name}  {head}"
                             for name, head in store.branches().items())
        if command == "/branch":
            self.agent._snapshot(self.branch, expected_head)
            store.branch(argument, expected_head)
            self.branch = argument
            return f"Switched to new branch {self.branch}."
        if command == "/switch":
            store.head(argument)
            self.branch = argument
            return f"Switched to {self.branch}."
        if command == "/history":
            return "\n\n".join(f"{commit['id']}\n{commit['created_at']}  {commit['message']}"
                               for commit in store.history(expected_head))
        if command == "/state":
            return _json(store.read(argument or expected_head))
        if command == "/diff":
            return _json(store.diff(argument, expected_head))
        if command == "/restore":
            head = store.restore(self.branch, argument, expected_head)
            return f"Restored state at {head}. Earlier history is preserved."
        if command == "/merge":
            head = self.agent.merge(self.branch, argument, expected_head)
            return f"Current checkpoint: {head}"
        if command == "/model":
            if not argument:
                return _json(store.read(expected_head)["state"]["decision"])
            with _path(argument).open("rb") as source:
                raw = source.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError(f"Decision file exceeds {MAX_STATE_BYTES} bytes.")
            # The decision validator rejects non-finite values and invalid schemas.
            decision = json.loads(raw.decode("utf-8-sig"))
            head = self.agent.decision(self.branch, decision, expected_head)
            return f"Saved decision assumptions at {head}."
        if command == "/choose":
            head = self.agent.choose(self.branch, argument, expected_head)
            return store.read(head)["state"]["messages"][-1]["content"]
        if command == "/export":
            snapshot = _json(store.read(expected_head)) + "\n"
            target = _path(argument)
            # Exclusive creation prevents replacing an existing file or database.
            with target.open("x", encoding="utf-8") as output:
                output.write(snapshot)
            return f"Exported snapshot to {target}. This is not a full history backup."
        if command.startswith("/") and command not in {"/decide", "/remember", "/forget", "/observe"}:
            raise ValueError("Unknown command. Type /help for available commands.")
        if command == "/decide" and argument:
            raise ValueError("Use /decide without arguments.")
        if command in {"/remember", "/forget", "/observe"} and not argument:
            raise ValueError(f"{command} needs an argument. Type /help for usage.")
        if command in {"/remember", "/forget", "/observe"}:
            text = command + " " + argument
        head = self.agent.chat(self.branch, text, expected_head)
        return store.read(head)["state"]["messages"][-1]["content"]

    def run(self):
        print(terminal_text(f"Dao ({self.agent.mode} mode), branch {self.branch}. Type /help for commands, /quit to exit."))
        while True:
            head = self.agent.store.head(self.branch)
            try:
                message = input(f"You [{self.branch}] > ")
                if message.strip() == "/quit":
                    return
                reply = self.execute(message, head)
                if reply:
                    print("Dao > " + terminal_text(reply))
            except (ValueError, StoreError, ProviderError, OSError, RecursionError) as exc:
                print("Dao > " + terminal_text(exc))
            except EOFError:
                return
