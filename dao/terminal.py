"""A streaming terminal view over Dao's existing runtime and durable store.

Local commands never execute a shell. Imported JSON is bounded, exports are
created exclusively, and displayed untrusted text cannot issue terminal control
sequences. Ctrl+C during a model turn requests exit after that admitted turn has
finished its accounting; Ctrl+C at the prompt exits immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import signal
import sys
import threading
import unicodedata


MAX_FILE_BYTES = 128 * 1024

HELP = """Chat by typing a message. Commands:
  /help                 Show these commands
  /quit                 Exit (EOF also exits)
  /branches             List branches and their heads
  /branch NAME          Fork the viewed checkpoint and switch to the new branch
  /switch NAME          Switch to an existing branch
  /head                 Refresh and show the current branch head
  /history              Show reachable revisions, newest first
  /restore ID_PREFIX    Restore a unique reachable checkpoint as a new revision
  /remember KEY=VALUE   Save memory in the current branch
  /memory               Show this checkpoint's saved memory
  /relationships        Show coherence, coverage, and reported observations
  /conflicts            Show unresolved relationship conflicts
  /relate PATH          Apply one relationship operation JSON (up to 128 KiB)
  /decide               Run the decision example
  /decision PATH        Evaluate a decision JSON file (up to 128 KiB)
  /audit PATH           Review a claim and evidence JSON file (up to 128 KiB)
  /artifact PATH        Save an artifact with its current matching audit verdict
  /usage                Show global usage, including prior branches and restores
  /verify               Verify the state and event journal
  /export PATH          Create a readable dao-export-v1 JSON file; never overwrite
  /say TEXT             Send literal text, including a slash-prefixed message
Paths may contain spaces; surrounding quotes are optional.
Ctrl+C at the prompt exits. During a turn, it exits after state and usage are recorded.
"""

COMMAND_ALIASES = {
    "/drafts": "/branches", "/draft": "/branch", "/version": "/head",
    "/versions": "/history", "/notes": "/memory", "/connections": "/relationships",
    "/connect": "/relate", "/review": "/audit", "/manuscript": "/artifact",
    "/activity": "/usage",
}


class _TextFilter:
    """Remove split ANSI sequences as well as Unicode terminal controls."""

    def __init__(self):
        self.mode = "text"

    def feed(self, value):
        visible = []
        for char in str(value):
            point = ord(char)
            if self.mode == "escape":
                if char == "[":
                    self.mode = "csi"
                elif char == "]":
                    self.mode = "string"
                elif char in "PX^_":
                    self.mode = "string"
                elif 0x30 <= point <= 0x7E:
                    self.mode = "text"
                continue
            if self.mode == "csi":
                if 0x40 <= point <= 0x7E:
                    self.mode = "text"
                elif char == "\x1b":
                    self.mode = "escape"
                continue
            if self.mode == "string":
                if char in {"\x07", "\x9c"}:
                    self.mode = "text"
                elif char == "\x1b":
                    self.mode = "string_escape"
                continue
            if self.mode == "string_escape":
                self.mode = "text" if char == "\\" else "string"
                continue
            if char == "\x1b":
                self.mode = "escape"
            elif char == "\x9b":
                self.mode = "csi"
            elif char in {"\x90", "\x98", "\x9d", "\x9e", "\x9f"}:
                self.mode = "string"
            elif char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}:
                visible.append(char)
        return "".join(visible)


class _Display:
    """Filter streamed text and redact credentials even across delta boundaries."""

    def __init__(self, secret=""):
        self.filter = _TextFilter()
        self.secret = secret
        self.pending = ""

    def feed(self, value, *, final=False):
        text = self.pending + self.filter.feed(value)
        if self.secret:
            text = text.replace(self.secret, "[redacted]")
        self.pending = ""
        if self.secret and not final:
            # Retain only a possible credential prefix, so normal deltas flush immediately.
            for size in range(min(len(self.secret) - 1, len(text)), 0, -1):
                if text.endswith(self.secret[:size]):
                    self.pending = text[-size:]
                    text = text[:-size]
                    break
        return text


class Terminal:
    app_name = "Dao"
    view_name = "terminal"
    branch_noun = "branch"
    revision_noun = "revision"
    branch_command = "/branch"
    artifact_noun = "Artifact"
    memory_empty = "No saved memory at this revision."
    conflicts_empty = "No unresolved relationship conflicts at this revision."
    introduction = "Type /help for commands."
    help_text = HELP

    def _tool_label(self, name):
        return f"Tool: {name}"

    def _history_label(self, commit):
        return "Workspace started" if commit["kind"] == "bootstrap" else commit["label"]

    def __init__(self, app, branch="main", input_stream=None, output_stream=None):
        self.app = app
        self.branch = branch
        self.input = input_stream if input_stream is not None else sys.stdin
        self.output = output_stream if output_stream is not None else sys.stdout
        self.head = None
        self._output_failed = False
        self._stop_requested = False
        self._secret = getattr(app.config, "api_key", "")

    def _emit(self, value):
        if self._output_failed:
            return
        try:
            self.output.write(value)
            self.output.flush()
        except (OSError, ValueError):
            # A closed pipe must not abandon an admitted request's accounting.
            self._output_failed = True

    def _write(self, value):
        self._emit(_Display(self._secret).feed(value, final=True))

    def _json(self, value):
        self._write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")

    def _error(self, exc):
        if isinstance(exc, KeyError):
            diagnostic = str(exc.args[0]) if exc.args else "Unknown item"
        elif isinstance(exc, (ValueError, RuntimeError, OSError)):
            diagnostic = str(exc)
        else:
            diagnostic = f"Operation failed ({type(exc).__name__})"
        self._write("Error: " + diagnostic + "\n")

    def _refresh(self):
        self.head = self.app.store.head(self.branch)

    def _request(self, **data):
        return {**data, "branch": self.branch, "expected_head": self.head["id"]}

    def _mutate(self, route, **data):
        result = self.app.mutate(route, self._request(**data))
        if "head" in result:
            self.head = result["head"]
        return result

    @staticmethod
    def _path(argument):
        argument = argument.strip()
        if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in "\"'":
            argument = argument[1:-1]
        if not argument:
            raise ValueError("A file path is required")
        return Path(argument).expanduser()

    def _load(self, argument):
        path = self._path(argument)
        with path.open("rb") as source:
            content = source.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("JSON files must be at most 128 KiB")
        try:
            def reject_constant(value):
                raise ValueError(f"Non-finite JSON value: {value}")
            result = json.loads(content.decode("utf-8-sig"), parse_constant=reject_constant)
        except UnicodeError as exc:
            raise ValueError("JSON file must use UTF-8 encoding") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
        except RecursionError as exc:
            raise ValueError("JSON nesting is too deep") from exc
        if not isinstance(result, dict):
            raise ValueError("JSON file must contain an object")
        return result

    def _chat(self, message, *, literal=False):
        display = _Display(self._secret)
        speaking = False
        original_handlers = {}
        announced_stop = False

        def request_stop(_signum, _frame):
            self._stop_requested = True

        # Real Ctrl+C is deferred rather than injected into Dao.chat's generator.
        if threading.current_thread() is threading.main_thread():
            for number in [signal.SIGINT] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else []):
                original_handlers[number] = signal.getsignal(number)
                signal.signal(number, request_stop)
        try:
            for event in self.app.chat(self._request(message=message, literal=literal)):
                if self._stop_requested and not announced_stop:
                    self._write("\nExit requested; finishing this turn's accounting.\n")
                    announced_stop = True
                kind = event["type"]
                if kind == "start":
                    self.head = event["head"]
                elif kind == "delta":
                    if not speaking:
                        self._write(self.app_name + ": ")
                        speaking = True
                    self._emit(display.feed(event["text"]))
                elif kind == "tool":
                    self._emit(display.feed("", final=True))
                    display = _Display(self._secret)
                    self._write("\n" + self._tool_label(event["name"]) + "\n")
                    self._json(event["result"])
                    speaking = False
                elif kind in {"done", "error"}:
                    self.head = event["head"]
                    self._emit(display.feed("", final=True))
                    display = _Display(self._secret)
                    if speaking:
                        self._write("\n")
                        speaking = False
                    if kind == "error":
                        self._write("Error: " + str(event["error"]) + "\n")
            self._emit(display.feed("", final=True))
            if speaking:
                self._write("\n")
        finally:
            for number, original_handler in original_handlers.items():
                signal.signal(number, original_handler)
        if self._stop_requested:
            self._write("State and usage recorded; exiting.\n")

    @staticmethod
    def _no_argument(command, argument):
        if argument:
            raise ValueError(f"{command} does not take an argument")

    def _command(self, line):
        parts = line.split(None, 1)
        requested_command = parts[0]
        argument = parts[1].strip() if len(parts) == 2 else ""
        command = COMMAND_ALIASES.get(requested_command, requested_command)
        if command == "/explore":
            command = "/decision" if argument else "/decide"
        if command == "/say":
            if not argument:
                raise ValueError("Use /say TEXT")
            self._chat(argument, literal=True)
        elif command == "/remember":
            if not argument:
                raise ValueError("Use /remember KEY=VALUE")
            self._chat(command + " " + argument)
        elif command == "/decide":
            self._no_argument(requested_command, argument)
            self._chat(command)
            decisions = self.head["state"].get("decisions", [])
            if decisions:
                self._json(decisions[-1]["result"])
        elif command in {"/help", "/quit", "/branches", "/head", "/history", "/memory", "/relationships", "/conflicts", "/usage", "/verify"}:
            self._no_argument(requested_command, argument)
            if command == "/quit":
                return False
            if command == "/help":
                self._write(self.help_text)
            elif command == "/branches":
                for branch in self.app.store.branches():
                    marker = "*" if branch["name"] == self.branch else " "
                    self._write(f"{marker} {branch['name']} {branch['head'][:12]}\n")
            elif command == "/head":
                self._refresh()
                self._write(f"{self.branch_noun.title()} {self.branch} | {self.revision_noun} {self.head['id']}\n")
            elif command == "/history":
                for commit in self.app.store.history(self.branch):
                    label = self._history_label(commit)
                    self._write(f"{commit['id'][:12]}  {label}\n")
            elif command == "/memory":
                memory = self.head["state"].get("memory", {})
                if not memory:
                    self._write(self.memory_empty + "\n")
                for key, value in memory.items():
                    self._write(f"{key} = {value}\n")
            elif command == "/usage":
                self._json(self.app.store.usage())
            elif command in {"/relationships", "/conflicts"}:
                # Read the viewed checkpoint, just like /memory. /head refreshes it.
                from .relationships import summarize
                summary = summarize(self.head["state"].get("relationships"))
                if command == "/conflicts":
                    conflicts = summary["unresolved_conflicts"]
                    if not conflicts:
                        self._write(self.conflicts_empty + "\n")
                    self._json(conflicts)
                else:
                    coherence = summary["coherence"]
                    label = "Not defined" if coherence is None else f"{coherence:.1%}"
                    self._write(f"Coherence: {label}; assessed coverage: {summary['coverage']:.1%}\n")
                    self._json(summary)
            elif command == "/verify":
                result = self.app.store.verify()
                if result["ok"]:
                    self._write(f"Integrity: OK ({result['commits']} {self.revision_noun}s, {result['events']} events)\n")
                else:
                    self._write("Integrity: FAILED\n")
                    for error in result["errors"]:
                        self._write(str(error) + "\n")
        elif command == "/branch":
            if not argument:
                raise ValueError(f"Use {self.branch_command} NAME")
            result = self.app.mutate("/api/branches", {"name": argument, "from_commit": self.head["id"]})
            self.branch = result["branch"]["name"]
            self._refresh()
            self._write(f"Created and switched to {self.branch_noun} {self.branch} at {self.revision_noun} {self.head['id'][:12]}.\n")
        elif command == "/switch":
            if not argument:
                raise ValueError("Use /switch NAME")
            head = self.app.store.head(argument)
            self.branch, self.head = argument, head
            self._write(f"Switched to {self.branch_noun} {self.branch} at {self.revision_noun} {self.head['id'][:12]}.\n")
        elif command == "/restore":
            if not re.fullmatch(r"[0-9a-fA-F]{1,64}", argument):
                raise ValueError(f"Use /restore ID_PREFIX with a hexadecimal {self.revision_noun} prefix")
            candidates = [commit for commit in self.app.store.history(self.branch)
                          if commit["id"].startswith(argument.lower())]
            if not candidates:
                raise ValueError(f"No reachable saved {self.revision_noun} matches that prefix")
            if len(candidates) != 1:
                raise ValueError(f"{self.revision_noun.title()} prefix is ambiguous; use more characters")
            result = self._mutate("/api/restore", commit_id=candidates[0]["id"])
            self._write(f"Restored as {self.revision_noun} {result['head']['id'][:12]}; usage is retained.\n")
        elif command in {"/decision", "/audit", "/artifact", "/relate"}:
            payload = self._load(argument)
            if command == "/decision":
                result = self._mutate("/api/decision", problem=payload)
            else:
                if {"branch", "expected_head"} & set(payload):
                    raise ValueError("Imported JSON cannot select a branch or override the expected head")
                if command == "/artifact" and set(payload) - {"name", "content", "verdict_id"}:
                    raise ValueError(f"{self.artifact_noun} JSON accepts only name, content, and verdict_id")
                route = "/api/relationships" if command == "/relate" else "/api" + command
                result = self._mutate(route, **payload)
            if result.get("result") is not None:
                self._json(result["result"])
            else:
                self._write(f"{self.artifact_noun} saved at {self.revision_noun} {self.head['id'][:12]}.\n")
        elif command == "/export":
            path = self._path(argument)
            payload = {"schema": "dao-export-v1", **self.app.snapshot(self.branch)}
            content = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
            try:
                with path.open("x", encoding="utf-8", newline="\n") as output:
                    output.write(content)
            except FileExistsError as exc:
                raise ValueError("Export path already exists; choose a new file") from exc
            self._write(f"Export created: {path}\n")
        else:
            raise ValueError(f"Unknown command: {command}. Use /help or /say TEXT to send literal text.")
        return True

    def run(self):
        # Windows Ctrl+Break should behave like Ctrl+C at the input prompt.
        previous_break = None
        manage_break = hasattr(signal, "SIGBREAK") and threading.current_thread() is threading.main_thread()
        if manage_break:
            previous_break = signal.getsignal(signal.SIGBREAK)
            signal.signal(signal.SIGBREAK, signal.default_int_handler)
        try:
            return self._run()
        finally:
            if manage_break:
                signal.signal(signal.SIGBREAK, previous_break)

    def _run(self):
        try:
            self._refresh()
        except Exception as exc:
            self._error(exc)
            return 1
        self._write(f"{self.app_name} {self.view_name} | {self.branch_noun} {self.branch} | {self.app.config.public()['provider']}\n")
        self._write(self.introduction + "\n")
        while not self._stop_requested and not self._output_failed:
            self._write(f"{self.branch}@{self.head['id'][:12]}> ")
            if self._output_failed:
                return 1
            try:
                line = self.input.readline()
            except KeyboardInterrupt:
                self._write("\nExiting.\n")
                return 0
            except Exception as exc:
                self._error(exc)
                return 1
            if line == "":
                self._write("\n")
                return 0
            line = line.strip()
            if not line:
                continue
            try:
                if line.startswith("/"):
                    if not self._command(line):
                        return 0
                else:
                    self._chat(line)
            except KeyboardInterrupt:
                # Programmatic KeyboardInterrupt is not a deferred OS SIGINT.
                # Its generator cannot be resumed, so do not claim accounting settled.
                self._write("\nTurn interrupted unexpectedly. Any pending usage reservation is retained.\n")
                return 130
            except Exception as exc:
                self._error(exc)
        return 1 if self._output_failed else 0
