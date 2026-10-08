"""Writing presentation of the shared terminal command implementation."""

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
  /connections          Inspect connection assessments and observations
  /conflicts            Show unresolved connection conflicts
  /connect PATH         Apply one connection operation JSON (up to 128 KiB)
  /explore [PATH]       Compare the story example or a model JSON (up to 128 KiB)
  /review PATH          Review a claim against source JSON (up to 128 KiB)
  /manuscript PATH      Save a document with its current matching review
  /activity             Show usage across all drafts and restores
  /verify               Verify saved versions and the activity journal
  /export PATH          Create a readable dao-export-v1 JSON file; never overwrite
  /say TEXT             Send literal text, including a slash-prefixed message
Earlier commands remain aliases: /branches, /branch, /head, /history, /memory,
/relationships, /relate, /decide, /decision, /audit, /artifact, /usage.
Comparisons use supplied assumptions, not literary scores. Reviews do not decide canon.
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
    conflicts_empty = "No unresolved connection conflicts at this version."
    introduction = "Bring a scene, character, or idea. Type /help for commands."
    help_text = NARRATIVE_HELP

    def _tool_label(self, name):
        return {"remember": "Story note", "decision": "Story comparison",
                "evaluate_decision": "Story comparison"}.get(name, str(name)) + ":"

    def _history_label(self, commit):
        label = "Story started" if commit["kind"] == "bootstrap" else commit["label"]
        for kind, prefix, replacement in (
            ("decision", "Decision: ", "Comparison: "),
            ("adjudication", "Audit: ", "Review: "),
            ("relationship.", "Relationship ", "Connection "),
        ):
            if commit["kind"].startswith(kind) and label.startswith(prefix):
                return replacement + label[len(prefix):]
        return label
