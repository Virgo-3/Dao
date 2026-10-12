"""Simple writing commands over the shared durable terminal transport."""
from dao.terminal import Terminal


NARRATIVE_HELP = """Develop your story by typing a message. Commands:
  /help                 Show these commands
  /quit                 Exit (EOF also exits)
  /drafts               List drafts and their current versions
  /draft NAME           Create an alternate draft from the viewed version
  /switch NAME          Switch to an existing draft
  /version              Refresh and show the draft's current version
  /versions             Show saved versions, newest first
  /restore ID_PREFIX    Restore a saved version as a new version
  /remember KEY=VALUE   Save a story note in the current draft
  /notes                Show this version's story notes
  /manuscript PATH      Save a UTF-8 text file as a document in this draft
  /documents            List this version's saved documents
  /document NAME        Read a saved document
  /activity             Show usage across all drafts and restores
  /export PATH          Export this draft as readable Markdown; never overwrite
  /say TEXT             Send literal text, including a slash-prefixed message
State command aliases: /branches, /branch, /head, /history, /memory, /usage.
Documents can contain up to 32,768 characters. Saving needs no review.
Paths may contain spaces; surrounding quotes are optional.
Ctrl+C at the prompt exits. During a turn, it exits after state and usage are recorded.
"""


class NarrativeTerminal(Terminal):
    app_name = "Dao Narrative"
    view_name = "writing room"
    branch_noun = "draft"
    revision_noun = "version"
    branch_command = "/draft"
    artifact_noun = "Document"
    memory_empty = "No story notes at this version."
    introduction = "Bring a scene, character, or idea. Type /help for commands."
    help_text = NARRATIVE_HELP

    def _command(self, line):
        parts = line.split(None, 1)
        command, argument = parts[0], parts[1].strip() if len(parts) == 2 else ""
        if command == "/manuscript":
            path = self._path(argument)
            with path.open("rb") as source:
                raw = source.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                raise ValueError("Document files must be at most 128 KiB")
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeError as exc:
                raise ValueError("Document file must use UTF-8 encoding") from exc
            self._mutate("/api/artifact", name=path.name, content=content)
            self._write(f"Document {path.name} saved at version {self.head['id'][:12]}.\n")
        elif command in {"/documents", "/document"}:
            documents = self.head["state"].get("artifacts", {})
            if command == "/documents":
                self._no_argument(command, argument)
                self._write("\n".join(documents) + "\n" if documents else "No saved documents at this version.\n")
            else:
                if not argument or argument not in documents:
                    raise ValueError("Use /document NAME with a saved document name")
                document = documents[argument]
                content = document.get("content", "") if isinstance(document, dict) else document
                self._write(str(content) + "\n")
        elif command == "/export":
            path = self._path(argument)
            exported = self.app.export(self.branch)
            try:
                with path.open("x", encoding="utf-8", newline="\n") as output:
                    output.write(exported["content"])
            except FileExistsError as exc:
                raise ValueError("Export path already exists; choose a new file") from exc
            self._write(f"Draft exported: {path}\n")
        elif command == "/activity" or command == "/usage":
            self._no_argument(command, argument)
            usage = self.app.store.usage()
            self._write(f"Recorded tokens: {usage['total_tokens']} / {self.app.config.token_budget} across all drafts.\n")
            if self.app.config.provider == "demo":
                self._write("Offline demo estimates; no provider cost.\n")
        elif command in {"/help", "/quit", "/say", "/drafts", "/draft", "/switch", "/version",
                         "/versions", "/restore", "/remember", "/notes", "/branches", "/branch",
                         "/head", "/history", "/memory"}:
            return super()._command(line)
        else:
            raise ValueError(f"Unknown writing command: {command}. Use /help or /say TEXT.")
        return True

    def _history_label(self, commit):
        label = "Story started" if commit["kind"] == "bootstrap" else commit["label"]
        return label
