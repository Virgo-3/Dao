from copy import deepcopy
from decimal import Decimal
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dao.config import Config
from dao.decision import demo_payload, evaluate
from dao.provider import ProviderError, build_input, openai_stream
from dao.service import Dao, artifact_claim
from dao.store import ConflictError, Store


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")
        self.app = Dao(self.store, Config())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def chat(self, message):
        return list(self.app.chat({"message": message, "branch": "main", "expected_head": self.store.head()["id"]}))

    def mutate(self, route, **kwargs):
        return self.app.mutate(route, {"branch": "main", "expected_head": self.store.head()["id"], **kwargs})

    def test_memory_command_and_newline_message_are_durable(self):
        events = self.chat("/remember preference=Keep options open")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.store.head()["state"]["memory"]["preference"], "Keep options open")
        self.chat("First line\nSecond line")
        self.assertEqual(self.store.head()["state"]["messages"][-2]["content"], "First line\nSecond line")
        self.assertTrue(self.store.verify()["ok"])

    def test_demo_stream_accounted_and_restore_retains_ledger(self):
        initial = self.store.head()["id"]
        with patch("dao.provider.time.sleep"):
            events = self.chat("Should we launch or wait?")
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(self.store.usage()["entries"][0]["estimated"])
        self.assertGreater(self.store.usage()["total_tokens"], 0)
        ledger = self.store.usage()
        self.mutate("/api/restore", commit_id=initial)
        self.assertEqual(self.store.usage(), ledger)
        self.assertEqual(self.store.head()["state"]["messages"], [])

    def test_partial_model_failure_keeps_output_and_charge(self):
        def failing(*args):
            yield {"type": "delta", "text": "Partial answer"}
            raise ProviderError("Stream interrupted")
        self.app = Dao(self.store, Config(provider="openai", api_key="test-only"))
        with patch("dao.service.openai_stream", failing):
            events = self.chat("Hello")
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.store.head()["state"]["messages"][-1]["status"], "failed")
        entry = self.store.usage()["entries"][0]
        self.assertEqual(entry["status"], "unknown")
        self.assertTrue(entry["estimated"])
        self.assertEqual(entry["total_tokens"], entry["reserved_tokens"])
        self.assertTrue(self.store.verify()["ok"])

    def test_budget_admission_failure_does_not_fabricate_cost(self):
        self.app = Dao(self.store, Config(token_budget=1))
        events = self.chat("Hello")
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(self.store.usage()["entries"], [])
        self.assertTrue(self.store.verify()["ok"])

    def test_stream_limit_failure_preserves_usage_and_releases_branch(self):
        self.app = Dao(self.store, Config(provider="openai", api_key="test-only"))
        response = FakeResponse([{"type": "response.output_text.delta", "delta": ""}] * 4)
        with patch("dao.provider.urlopen", return_value=response), patch("dao.provider.MAX_STREAM_LINES", 3):
            events = self.chat("Hello")
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("line limit", events[-1]["error"])
        entry = self.store.usage()["entries"][0]
        self.assertEqual(entry["total_tokens"], entry["reserved_tokens"])
        self.assertEqual(entry["status"], "unknown")
        self.mutate("/api/memory", key="after_failure", value="Branch is available")
        self.assertTrue(self.store.verify()["ok"])

    def test_multiple_tools_are_rejected_before_computation_and_usage_is_retained(self):
        self.app = Dao(self.store, Config(provider="openai", api_key="test-only"))
        call = {"type": "function_call", "call_id": "call1", "name": "evaluate_decision",
                "arguments": json.dumps({"problem_json": json.dumps(demo_payload())})}
        events = [{"type": "response.completed", "response": {"status": "completed",
                  "output": [call, {**call, "call_id": "call2"}],
                  "usage": {"input_tokens": 20, "output_tokens": 3}}}]
        with patch("dao.provider.urlopen", return_value=FakeResponse(events)), patch("dao.service.evaluate") as score:
            result = self.chat("Compare the options")
            score.assert_not_called()
        self.assertEqual(result[-1]["type"], "error")
        self.assertIn("at most one tool call", result[-1]["error"])
        usage = self.store.usage()["entries"][0]
        self.assertEqual(usage["total_tokens"], 23)
        self.assertEqual(usage["status"], "completed")
        self.assertFalse(usage["estimated"])
        self.assertEqual(self.store.head()["state"]["decisions"], [])
        self.mutate("/api/memory", key="after_rejection", value="Branch is available")
        self.assertTrue(self.store.verify()["ok"])

    def test_branch_busy_and_stale_head_rejected(self):
        old = self.store.head()["id"]
        self.mutate("/api/memory", key="x", value="y")
        with self.assertRaises(ConflictError):
            list(self.app.chat({"message": "Hello", "expected_head": old}))
        with self.app.branch_lock("main"):
            with self.assertRaises(ConflictError):
                self.mutate("/api/memory", key="z", value="t")

    def test_artifact_gate_binds_branch_and_exact_content(self):
        claim = artifact_claim("plan", "Pilot first")
        audit = self.mutate("/api/audit", claim=claim, evidence=[
            {"source": "Operator", "content": "Reviewed plan", "stance": "support", "reliability": 1},
            {"source": "Checklist", "content": "Plan meets constraints", "stance": "support", "reliability": 1},
        ])["result"]
        with self.assertRaises(ValueError):
            self.mutate("/api/artifact", name="plan", content="Changed plan", verdict_id=audit["verdict_id"])
        self.mutate("/api/artifact", name="plan", content="Pilot first", verdict_id=audit["verdict_id"])
        self.assertEqual(self.store.head()["state"]["artifacts"]["plan"]["content"], "Pilot first")
        initial = self.store.history()[-1]["id"]
        self.store.branch("other", initial)
        with self.assertRaises(ValueError):
            self.app.mutate("/api/artifact", {"branch": "other", "expected_head": initial,
                            "name": "plan", "content": "Pilot first", "verdict_id": audit["verdict_id"]})

    def test_decision_tool_command_is_recorded_without_model_usage(self):
        self.chat("/decide")
        self.assertEqual(self.store.head()["state"]["decisions"][-1]["result"]["recommendation"], "wait")
        self.assertEqual(self.store.usage()["entries"], [])

    def test_new_contradiction_revokes_prior_artifact_verdict(self):
        claim = artifact_claim("plan", "Pilot first")
        evidence = [{"source": str(i), "content": "Reviewed", "stance": "support", "reliability": 1} for i in range(2)]
        first = self.mutate("/api/audit", claim=claim, evidence=evidence)["result"]
        self.mutate("/api/audit", claim=claim, evidence=[*evidence,
                    {"source": "New review", "content": "Plan has a flaw", "stance": "contradict", "reliability": 1}])
        with self.assertRaises(ValueError):
            self.mutate("/api/artifact", name="plan", content="Pilot first", verdict_id=first["verdict_id"])

    def test_context_limit_and_failed_reply_exclusion(self):
        state = deepcopy(self.store.head()["state"])
        state["messages"] = [{"role": "assistant", "content": "Partial", "status": "failed"}]
        self.assertEqual(build_input(state)[1], [])
        state["messages"] = [{"role": "user", "content": "x" * 70000}]
        self.assertEqual(build_input(state)[1], state["messages"])
        state["messages"][0]["content"] = "x" * 1048576
        with self.assertRaisesRegex(ValueError, "Context exceeds 1 MiB"):
            build_input(state)

    def test_context_byte_boundary_includes_instructions_and_saved_memory(self):
        for prefix in ("", "道" * 1000):
            with self.subTest(multibyte=bool(prefix)):
                state = deepcopy(self.store.head()["state"])
                state["memory"] = {"note": "道" * 100}
                state["messages"] = [{"role": "user", "content": prefix}]
                instructions, messages = build_input(state)
                size = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + len(instructions.encode("utf-8"))
                state["messages"][0]["content"] += "x" * (1048576 - size)
                instructions, messages = build_input(state)
                self.assertEqual(len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
                                 + len(instructions.encode("utf-8")), 1048576)
                state["messages"][0]["content"] += "x"
                with self.assertRaisesRegex(ValueError, "Context exceeds 1 MiB"):
                    build_input(state)

    def test_prices_and_credentials_remain_server_side(self):
        config = Config(api_key="private-key", input_price=Decimal("0.5"), output_price=Decimal("2"), pricing_configured=True)
        self.assertNotIn("private-key", repr(config))
        self.assertNotIn("api_key", config.public())
        self.assertEqual(config.cost(3, 4), 10)


class FakeResponse:
    def __init__(self, events):
        self.lines = [("data: " + json.dumps(e) + "\n").encode() for e in events]
        self.body = io.BytesIO(b"".join(self.lines))
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def __iter__(self):
        return iter(self.lines)
    def read1(self, size):
        return self.body.read1(size)


class ProviderTests(unittest.TestCase):
    def state(self):
        return {"messages": [{"role": "user", "content": "Hello"}], "memory": {}}

    def test_real_adapter_parses_deltas_and_usage(self):
        events = [{"type": "response.output_text.delta", "delta": "Hello"},
                  {"type": "response.completed", "response": {"status": "completed", "output": [],
                   "usage": {"input_tokens": 20, "output_tokens": 3}}}]
        with patch("dao.provider.urlopen", return_value=FakeResponse(events)) as request:
            output = list(openai_stream(self.state(), Config(api_key="test"), lambda x: {}))
        self.assertEqual(output[0], {"type": "delta", "text": "Hello"})
        self.assertEqual(output[-1]["input_tokens"], 20)
        self.assertFalse(output[-1]["estimated"])
        payload = json.loads(request.call_args.args[0].data)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["tools"][0]["name"], "evaluate_decision")

    def test_missing_terminal_fails_even_after_text(self):
        with patch("dao.provider.urlopen", return_value=FakeResponse([{"type": "response.output_text.delta", "delta": "Partial"}])):
            with self.assertRaises(ProviderError):
                list(openai_stream(self.state(), Config(), lambda x: {}))

    def completed(self):
        return {"type": "response.completed", "response": {"status": "completed", "output": [],
                "usage": {"input_tokens": 20, "output_tokens": 3}}}

    def test_empty_deltas_are_skipped_and_terminal_stops_processing(self):
        events = [{"type": "response.output_text.delta", "delta": ""},
                  {"type": "response.output_text.delta"}, self.completed(),
                  {"type": "error", "message": "Must never be processed"}]
        with patch("dao.provider.urlopen", return_value=FakeResponse(events)):
            output = list(openai_stream(self.state(), Config(), lambda x: {}))
        self.assertEqual([event["type"] for event in output], ["usage"])

    def test_nontext_delta_is_rejected(self):
        with patch("dao.provider.urlopen", return_value=FakeResponse([{"type": "response.output_text.delta", "delta": []}])):
            with self.assertRaisesRegex(ProviderError, "must be a string"):
                list(openai_stream(self.state(), Config(), lambda x: {}))

    def test_aggregate_bytes_and_lines_are_bounded(self):
        for field, limit, message in [("MAX_STREAM_LINES", 3, "line limit"),
                                      ("MAX_STREAM_BYTES", 30, "byte limit")]:
            with self.subTest(field=field), patch("dao.provider." + field, limit), patch(
                    "dao.provider.urlopen", return_value=FakeResponse([{"type": "response.output_text.delta", "delta": ""}] * 4)):
                with self.assertRaisesRegex(ProviderError, message):
                    list(openai_stream(self.state(), Config(), lambda x: {}))

    def test_elapsed_deadline_is_checked_without_waiting_for_newline(self):
        with patch("dao.provider.urlopen", return_value=FakeResponse([self.completed()])), patch(
                "dao.provider.time.monotonic", side_effect=[0, 0, 91]):
            with self.assertRaisesRegex(ProviderError, "time limit"):
                list(openai_stream(self.state(), Config(), lambda x: {}))

    def test_unterminated_line_is_bounded(self):
        response = FakeResponse([])
        response.body = io.BytesIO(b"x" * 100)
        with patch("dao.provider.urlopen", return_value=response), patch("dao.provider.MAX_LINE_BYTES", 50):
            with self.assertRaisesRegex(ProviderError, "event exceeds"):
                list(openai_stream(self.state(), Config(), lambda x: {}))

    def test_tool_round_requires_reservation_event_and_followup(self):
        call = {"type": "function_call", "call_id": "call1", "name": "evaluate_decision",
                "arguments": json.dumps({"problem_json": json.dumps(demo_payload())})}
        first = [{"type": "response.completed", "response": {"status": "completed", "output": [call],
                  "usage": {"input_tokens": 20, "output_tokens": 10}}}]
        second = [{"type": "response.output_text.delta", "delta": "Wait for the signal."},
                  {"type": "response.completed", "response": {"status": "completed", "output": [],
                   "usage": {"input_tokens": 50, "output_tokens": 6}}}]
        with patch("dao.provider.urlopen", side_effect=[FakeResponse(first), FakeResponse(second)]):
            output = list(openai_stream(self.state(), Config(), lambda x: {"recommendation": "wait"}))
        self.assertEqual([e["type"] for e in output], ["usage", "tool", "next_round", "delta", "usage"])

    def test_model_tool_rejects_oversized_decision_and_returns_diagnostic(self):
        problem = demo_payload()
        problem["actions"] = [{}] * 65
        call = {"type": "function_call", "call_id": "call1", "name": "evaluate_decision",
                "arguments": json.dumps({"problem_json": json.dumps(problem)})}
        first = self.completed()
        first["response"]["output"] = [call]
        with patch("dao.provider.urlopen", side_effect=[FakeResponse([first]), FakeResponse([self.completed()])]) as request:
            output = list(openai_stream(self.state(), Config(), evaluate))
        self.assertEqual([e["type"] for e in output], ["usage", "tool_error", "next_round", "usage"])
        self.assertIn("at most 64", output[1]["error"])
        followup = json.loads(request.call_args_list[1].args[0].data)
        diagnostic = json.loads(followup["input"][-1]["output"])
        self.assertIn("at most 64", diagnostic["error"])

    def test_terminal_output_shape_is_validated(self):
        for malformed in (None, {}, ["not an output object"]):
            terminal = self.completed()
            terminal["response"]["output"] = malformed
            with self.subTest(output=malformed), patch("dao.provider.urlopen", return_value=FakeResponse([terminal])):
                with self.assertRaisesRegex(ProviderError, "Malformed provider output"):
                    list(openai_stream(self.state(), Config(), evaluate))


if __name__ == "__main__":
    unittest.main()
