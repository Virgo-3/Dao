import io
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from dao.config import Config
from dao.decision import demo_payload
from dao.provider import ProviderError
from dao.service import Dao, artifact_claim
from dao.store import Store
from dao.terminal import Terminal
from dao_narrative.app import NarrativeDao
from dao_narrative.terminal import NarrativeTerminal


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / "state.db")
        self.app = Dao(self.store, Config())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_terminal(self, commands, *, branch="main", input_stream=None, output_stream=None):
        source = input_stream if input_stream is not None else io.StringIO(commands)
        output = output_stream if output_stream is not None else io.StringIO()
        view = NarrativeTerminal if isinstance(self.app, NarrativeDao) else Terminal
        terminal = view(self.app, branch, source, output)
        with patch("dao.provider.time.sleep"):
            code = terminal.run()
        return code, output.getvalue() if hasattr(output, "getvalue") else "", terminal

    def file(self, name, payload):
        path = self.directory / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def audit_payload(self, *, contradict=False):
        evidence = [{"source": "Operator", "content": "Plan reviewed", "stance": "support", "reliability": 1},
                    {"source": "Checklist", "content": "Constraints satisfied", "stance": "support", "reliability": 1}]
        if contradict:
            evidence.append({"source": "New review", "content": "A flaw remains", "stance": "contradict", "reliability": 1})
        return {"claim": artifact_claim("plan", "Pilot first"), "evidence": evidence}

    def test_branch_restore_keeps_usage_and_isolates_memory(self):
        initial = self.store.head()["id"]
        commands = ("/remember strategy=Pilot\n/branch alternate\n/remember strategy=Launch\n"
                    "/switch main\nShould we wait?\n/history\n/restore " + initial[:12] + "\n/usage\n/verify\n/quit\n")
        code, output, terminal = self.run_terminal(commands)
        self.assertEqual(code, 0)
        self.assertEqual(terminal.branch, "main")
        self.assertEqual(self.store.head()["state"]["memory"], {})
        self.assertEqual(self.store.head()["state"]["messages"], [])
        self.assertEqual(self.store.head("alternate")["state"]["memory"]["strategy"], "Launch")
        self.assertEqual(len(self.store.usage()["entries"]), 1)
        self.assertGreater(self.store.usage()["total_tokens"], 0)
        self.assertEqual(self.store.head()["kind"], "restore")
        self.assertIn("Integrity: OK", output)
        self.assertIn("usage is retained", output)
        self.assertTrue(self.store.verify()["ok"])

    def test_decision_and_memory_runtime_commands_avoid_model_usage(self):
        problem = self.file("decision with spaces.json", demo_payload())
        code, output, _ = self.run_terminal(f'/decision "{problem}"\n/decide\n/remember next=Study\n/memory\n/quit\n')
        self.assertEqual(code, 0)
        self.assertEqual(len(self.store.head()["state"]["decisions"]), 2)
        self.assertEqual(self.store.usage()["entries"], [])
        self.assertIn('"recommendation": "wait"', output)
        self.assertIn("next = Study", output)

    def test_story_commands_share_drafts_versions_and_accounting(self):
        self.app = NarrativeDao(self.store, Config())
        initial = self.store.head()["id"]
        problem = self.file("story choice.json", demo_payload())
        connection = self.file("story connection.json", {
            "operation": "node", "node": {"id": "mara", "label": "Mara", "kind": "person", "importance": 1}})
        commands = ("/remember motive=Protect Ivo\n/draft alternate\n/remember motive=Expose Ivo\n"
                    f'/connect "{connection}"\n/connections\n/explore\n/explore "{problem}"\n'
                    "/notes\n/drafts\n/version\n/versions\n/activity\n/switch main\n"
                    f"/restore {initial[:12]}\n/verify\n/quit\n")
        code, output, _ = self.run_terminal(commands)
        self.assertEqual(code, 0)
        self.assertNotIn("Error:", output)
        self.assertIn("Dao Narrative writing room | draft main", output)
        self.assertIn("motive = Expose Ivo", output)
        self.assertIn("Try an alternate scene", output)
        self.assertIn("Story started", output)
        alternate = self.store.head("alternate")["state"]
        self.assertEqual(alternate["memory"], {"motive": "Expose Ivo"})
        self.assertEqual(len(alternate["decisions"]), 2)
        self.assertEqual(alternate["relationships"]["nodes"]["mara"]["label"], "Mara")
        self.assertEqual(self.store.head()["state"]["memory"], {})
        self.assertEqual(self.store.usage()["entries"], [])
        self.assertTrue(self.store.verify()["ok"])

    def test_story_review_and_document_aliases_preserve_the_save_gate(self):
        self.app = NarrativeDao(self.store, Config())
        audit = self.audit_payload()
        verdict = self.app.adjudicate_problem(self.store.head()["state"], audit)
        source = self.file("review.json", audit)
        document = self.file("manuscript.json", {
            "name": "plan", "content": "Pilot first", "verdict_id": verdict["verdict_id"]})
        override = self.file("review override.json", {**audit, "branch": "main"})
        code, output, _ = self.run_terminal(
            f"/manuscript {document}\n/review {override}\n/review {source}\n/manuscript {document}\n/quit\n")
        self.assertEqual(code, 0)
        self.assertEqual(output.count("Artifact needs an allowed adjudication"), 1)
        self.assertEqual(output.count("cannot select a branch"), 1)
        self.assertEqual(output.count("Document saved"), 1)
        self.assertEqual(self.store.head()["state"]["artifacts"]["plan"]["content"], "Pilot first")
        self.assertEqual(self.store.usage()["entries"], [])

    def test_imports_use_current_branch_and_latest_artifact_gate(self):
        audit = self.audit_payload()
        verdict = self.app.adjudicate_problem(self.store.head()["state"], audit)
        audit_file = self.file("audit.json", audit)
        contested_file = self.file("contested.json", self.audit_payload(contradict=True))
        artifact_file = self.file("artifact.json", {"name": "plan", "content": "Pilot first", "verdict_id": verdict["verdict_id"]})
        code, output, _ = self.run_terminal(f"/artifact {artifact_file}\n/audit {audit_file}\n/artifact {artifact_file}\n"
                                            f"/audit {contested_file}\n/artifact {artifact_file}\n/quit\n")
        self.assertEqual(code, 0)
        self.assertEqual(output.count("Artifact saved"), 1)
        self.assertEqual(output.count("Artifact needs an allowed adjudication"), 2)
        self.assertEqual(self.store.head()["state"]["artifacts"]["plan"]["content"], "Pilot first")
        self.assertEqual(self.store.head()["state"]["audits"][-1]["verdict"], "contested")
        self.assertTrue(self.store.verify()["ok"])

    def test_unknown_slash_is_rejected_and_say_escapes_runtime_commands(self):
        code, output, _ = self.run_terminal("/not-a-command\n/say /remember secret=literal\n/say /decide\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("Unknown command: /not-a-command", output)
        state = self.store.head()["state"]
        self.assertEqual(state["memory"], {})
        self.assertEqual(state["decisions"], [])
        self.assertEqual([m["content"] for m in state["messages"] if m["role"] == "user"],
                         ["/remember secret=literal", "/decide"])
        self.assertEqual(len(self.store.usage()["entries"]), 2)

    def test_file_bounds_invalid_json_and_branch_override_do_not_commit(self):
        initial = self.store.head()["id"]
        oversized = self.directory / "too-large.json"
        oversized.write_bytes(b" " * (128 * 1024 + 1))
        invalid = self.directory / "invalid.json"
        invalid.write_text("{broken", encoding="utf-8")
        nonfinite = self.directory / "nonfinite.json"
        nonfinite.write_text('{"value": NaN}', encoding="utf-8")
        override = self.file("override.json", {**self.audit_payload(), "branch": "main"})
        array = self.file("array.json", [])
        code, output, _ = self.run_terminal(f"/decision {oversized}\n/audit {invalid}\n/audit {nonfinite}\n"
                                            f"/audit {override}\n/audit {array}\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("at most 128 KiB", output)
        self.assertIn("Invalid JSON at line", output)
        self.assertIn("Non-finite JSON value", output)
        self.assertIn("cannot select a branch", output)
        self.assertIn("must contain an object", output)
        self.assertEqual(self.store.head()["id"], initial)

    def test_export_matches_browser_schema_and_does_not_overwrite(self):
        target = self.directory / "workspace export.json"
        code, output, _ = self.run_terminal(f"/remember next=Study\n/export {target}\n/export {target}\n/quit\n")
        self.assertEqual(code, 0)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "dao-export-v1")
        self.assertEqual(payload["head"]["state"]["memory"], {"next": "Study"})
        self.assertEqual(set(payload), {"schema", "config", "branches", "head", "history", "usage", "events", "relationships", "relationship_digest"})
        self.assertNotIn("api_key", payload["config"])
        self.assertEqual(output.count("Export created"), 1)
        self.assertIn("already exists", output)
        sentinel = self.directory / "existing.json"
        sentinel.write_text("keep this file", encoding="utf-8")
        self.run_terminal(f"/export {sentinel}\n/quit\n")
        self.assertEqual(sentinel.read_text(), "keep this file")

    def test_relationship_commands_preserve_branch_conflicts_and_usage(self):
        operations = [
            {"operation": "node", "node": {"id": "launch", "label": "Full launch", "kind": "action", "importance": 1}},
            {"operation": "node", "node": {"id": "trust", "label": "Customer trust", "kind": "goal", "importance": 20}},
            {"operation": "relation", "relation": {"id": "launch-trust", "source": "launch", "target": "trust", "kind": "effect", "weight": 20, "severe": True, "actions": ["Full launch"]}},
            {"operation": "assess", "relation_id": "launch-trust", "belief": {"positive": 0, "neutral": 0, "negative": 1}, "source": "Review", "content": "An unresolved customer impact"},
        ]
        files = [self.file(f"relation-{index}.json", operation) for index, operation in enumerate(operations)]
        unknown = self.file("unknown.json", {"operation": "assess", "relation_id": "launch-trust", "belief": None, "source": "Uncertainty review", "content": "The effect is now unknown"})
        commands = "/relationships\n" + "".join(f"/relate {path}\n" for path in files)
        commands += f"/conflicts\n/branch exploratory\n/relate {unknown}\n/relationships\n/switch main\n/relationships\n/quit\n"
        code, output, _ = self.run_terminal(commands)
        self.assertEqual(code, 0)
        self.assertNotIn("Error:", output)
        self.assertIn("Coherence: Not defined", output)
        self.assertIn('"relation_id": "launch-trust"', output)
        self.assertEqual(self.app.snapshot("main")["relationships"]["negative_weight"], 20)
        self.assertEqual(self.app.snapshot("exploratory")["relationships"]["unknown_weight"], 20)
        self.assertEqual(len(self.app.snapshot("exploratory")["relationships"]["unresolved_conflicts"]), 1)
        self.assertEqual(self.store.usage()["entries"], [])
        self.assertTrue(self.store.verify()["ok"])

    def test_relationship_import_cannot_override_branch_or_expected_head(self):
        initial = self.store.head()["id"]
        node = {"operation": "node", "node": {"id": "goal", "label": "Goal", "kind": "goal", "importance": 1}}
        branch_override = self.file("branch-override.json", {**node, "branch": "main"})
        head_override = self.file("head-override.json", {**node, "expected_head": initial})
        code, output, _ = self.run_terminal(f"/relate {branch_override}\n/relate {head_override}\n/conflicts\n/quit\n")
        self.assertEqual(code, 0)
        self.assertEqual(output.count("cannot select a branch"), 2)
        self.assertIn("No unresolved relationship conflicts", output)
        self.assertEqual(self.store.head()["id"], initial)

    def test_relationship_summary_uses_viewed_checkpoint_until_refreshed(self):
        store, app = self.store, self.app
        class ConcurrentInput:
            def __init__(self):
                self.lines = iter(["/relationships\n", "/head\n", "/relationships\n", "/quit\n"])
                self.first = True
            def readline(self):
                if self.first:
                    self.first = False
                    app.mutate("/api/relationships", {"expected_head": store.head()["id"], "operation": "node", "node": {"id": "goal", "label": "Goal", "kind": "goal", "importance": 1}})
                return next(self.lines, "")
        code, output, _ = self.run_terminal("", input_stream=ConcurrentInput())
        self.assertEqual(code, 0)
        self.assertIn('"node_count": 0', output)
        self.assertIn('"node_count": 1', output)
        self.assertEqual(len(self.store.history()), 2)

    def test_stale_prompt_rejects_change_until_head_refresh(self):
        store, app = self.store, self.app
        class ConcurrentInput:
            def __init__(self):
                self.lines = iter(["/remember local=blocked\n", "/head\n", "/remember local=accepted\n", "/quit\n"])
                self.first = True
            def readline(self):
                if self.first:
                    self.first = False
                    app.mutate("/api/memory", {"expected_head": store.head()["id"], "key": "external", "value": "kept"})
                return next(self.lines, "")
        code, output, _ = self.run_terminal("", input_stream=ConcurrentInput())
        self.assertEqual(code, 0)
        self.assertIn("branch changed", output.lower())
        self.assertEqual(self.store.head()["state"]["memory"], {"external": "kept", "local": "accepted"})
        users = [m["content"] for m in self.store.head()["state"]["messages"] if m["role"] == "user"]
        self.assertNotIn("/remember local=blocked", users)

    def test_terminal_controls_and_credentials_are_filtered_across_deltas(self):
        secret = "sk-private-credential"
        self.app = Dao(self.store, Config(provider="openai", api_key=secret))
        def streamed(*args):
            for delta in ["\x1b", "[31mVisible\x1b[0m ", "sk-private-", "credential",
                          "\x1b]52;hidden clipboard", "\x07 safe\r\b\x00\x9b2J\u202e\n\tend"]:
                yield {"type": "delta", "text": delta}
            yield {"type": "usage", "input_tokens": 20, "output_tokens": 10,
                   "estimated": False, "status": "completed"}
        with patch("dao.service.openai_stream", streamed):
            code, output, _ = self.run_terminal("Hello\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("Visible [redacted] safe\n\tend", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("hidden clipboard", output)
        self.assertFalse(any(c in output for c in ("\x1b", "\r", "\b", "\x00", "\x9b", "\u202e")))
        self.assertEqual(self.store.usage()["total_tokens"], 30)

    def test_stream_failure_keeps_partial_state_and_estimated_charge(self):
        secret = "sk-do-not-print"
        self.app = Dao(self.store, Config(provider="openai", api_key=secret))
        def failing(*args):
            yield {"type": "delta", "text": "Partial response"}
            raise ProviderError("Connection failed " + secret)
        with patch("dao.service.openai_stream", failing):
            code, output, _ = self.run_terminal("Hello\n/verify\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("Partial response", output)
        self.assertIn("Connection failed [redacted]", output)
        self.assertNotIn(secret, output)
        self.assertEqual(self.store.head()["state"]["messages"][-1]["status"], "failed")
        entry = self.store.usage()["entries"][0]
        self.assertEqual(entry["status"], "unknown")
        self.assertEqual(entry["total_tokens"], entry["reserved_tokens"])
        self.assertTrue(self.store.verify()["ok"])

    def test_ctrl_c_during_turn_drains_accounting_before_exit(self):
        original = signal.getsignal(signal.SIGINT)
        def stopped(*args):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            yield {"type": "delta", "text": "Finished reply"}
            yield {"type": "usage", "input_tokens": 20, "output_tokens": 10,
                   "estimated": False, "status": "completed"}
        with patch("dao.service.demo_stream", stopped):
            code, output, _ = self.run_terminal("Hello\n/remember should=not-run\n")
        self.assertEqual(code, 0)
        self.assertIn("State and usage recorded; exiting", output)
        self.assertEqual(signal.getsignal(signal.SIGINT), original)
        self.assertEqual(self.store.head()["state"]["messages"][-1]["status"], "completed")
        self.assertEqual(self.store.head()["state"]["memory"], {})
        self.assertEqual(self.store.usage()["entries"][0]["status"], "completed")
        self.assertEqual(self.store.usage()["reserved_tokens"], 0)

    def test_ctrl_c_at_prompt_and_eof_exit_without_changes(self):
        initial = self.store.head()["id"]
        class InterruptedInput:
            def readline(self):
                raise KeyboardInterrupt()
        code, output, _ = self.run_terminal("", input_stream=InterruptedInput())
        self.assertEqual(code, 0)
        self.assertIn("Exiting", output)
        self.assertEqual(self.run_terminal("")[0], 0)
        self.assertEqual(self.store.head()["id"], initial)
        self.assertEqual(self.store.usage()["entries"], [])

    @unittest.skipUnless(hasattr(signal, "SIGBREAK"), "Windows Ctrl+Break signal")
    def test_ctrl_break_settles_turn_and_is_restored_at_prompt_exit(self):
        original = signal.getsignal(signal.SIGBREAK)
        def stopped(*args):
            signal.getsignal(signal.SIGBREAK)(signal.SIGBREAK, None)
            yield {"type": "delta", "text": "Finished reply"}
            yield {"type": "usage", "input_tokens": 20, "output_tokens": 10,
                   "estimated": False, "status": "completed"}
        with patch("dao.service.demo_stream", stopped):
            code, output, _ = self.run_terminal("Hello\n/remember should=not-run\n")
        self.assertEqual(code, 0)
        self.assertIn("State and usage recorded; exiting", output)
        self.assertEqual(signal.getsignal(signal.SIGBREAK), original)
        self.assertEqual(self.store.usage()["reserved_tokens"], 0)
        self.assertEqual(self.store.usage()["entries"][0]["status"], "completed")
        class BreakInput:
            def readline(self):
                signal.getsignal(signal.SIGBREAK)(signal.SIGBREAK, None)
        self.assertEqual(self.run_terminal("", input_stream=BreakInput())[0], 0)
        self.assertEqual(signal.getsignal(signal.SIGBREAK), original)

    def test_unknown_branch_error_is_readable_and_does_not_switch(self):
        code, output, terminal = self.run_terminal("/switch absent\n/branches\n/restore deadbeef\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("Error: Unknown branch: absent\n", output)
        self.assertNotIn("'Unknown branch", output)
        self.assertEqual(terminal.branch, "main")
        self.assertIn("No reachable saved revision", output)
        startup, _, _ = self.run_terminal("", branch="absent")
        self.assertEqual(startup, 1)

    def test_closed_output_during_turn_does_not_abandon_usage(self):
        class BrokenOutput:
            def __init__(self):
                self.calls = 0
            def write(self, text):
                self.calls += 1
                if self.calls > 4:
                    raise BrokenPipeError("reader closed")
            def flush(self):
                pass
        code, _, _ = self.run_terminal("Hello\n", output_stream=BrokenOutput())
        self.assertEqual(code, 1)
        self.assertEqual(self.store.head()["state"]["messages"][-1]["status"], "completed")
        self.assertEqual(self.store.usage()["reserved_tokens"], 0)
        self.assertEqual(len(self.store.usage()["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
