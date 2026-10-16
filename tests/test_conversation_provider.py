from copy import deepcopy
import io
import json
import unittest
from unittest.mock import Mock, patch

from dao.config import Config
from dao.provider import (MAX_CONTEXT_BYTES, ProviderError, build_input, demo_stream,
                          openai_stream)


class FakeResponse:
    def __init__(self, events):
        self.body = io.BytesIO(b"".join(("data: " + json.dumps(event) + "\n").encode()
                                      for event in events))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, size):
        return self.body.read1(size)


def completed(*calls):
    return {"type": "response.completed", "response": {"status": "completed", "output": list(calls),
            "usage": {"input_tokens": 20, "output_tokens": 3}}}


def remember_call(payload=None, *, call_id="remember1", arguments=None):
    return {"type": "function_call", "call_id": call_id, "name": "remember_conversation",
            "arguments": json.dumps({"context_json": json.dumps(payload)}) if arguments is None else arguments}


class ConversationProviderTests(unittest.TestCase):
    def state(self):
        return {"messages": [{"id": "msg-1", "role": "user", "content": "I am considering SQLite."}],
                "memory": {}}

    def payload(self):
        return {"rationale": "Keep a tentative option", "operations": [
            {"operation": "node", "node": {"id": "sqlite-option", "label": "SQLite", "kind": "claim",
             "importance": 1, "stance": "considering", "citations": [
                 {"message_id": "msg-1", "start": 0, "end": 24, "quote": "I am considering SQLite."}]}}]}

    def activity(self):
        return {"id": "activity-1", "kind": "remembered", "label": "SQLite is under consideration",
                "node_ids": ["sqlite-option"], "citations": [
                    {"message_id": "msg-1", "start": 0, "end": 24, "quote": "I am considering SQLite."}]}

    def test_routine_capture_dispatches_activity_and_result_for_followup(self):
        state = self.state()
        original = deepcopy(state)
        remember = Mock(return_value=self.activity())
        evaluate = Mock()
        second = [{"type": "response.output_text.delta", "delta": "What draws you to SQLite?"}, completed()]
        with patch("dao.provider.urlopen", side_effect=[FakeResponse([completed(remember_call(self.payload()))]),
                                                        FakeResponse(second)]) as request:
            events = list(openai_stream(state, Config(), evaluate, remember=remember))
        remember.assert_called_once_with(self.payload())
        evaluate.assert_not_called()
        self.assertEqual(state, original)
        self.assertEqual([event["type"] for event in events], ["usage", "activity", "next_round", "delta", "usage"])
        self.assertEqual(events[1], {"type": "activity", "name": "remember_conversation", "result": self.activity()})
        first = json.loads(request.call_args_list[0].args[0].data)
        tools = {tool["name"]: tool for tool in first["tools"]}
        self.assertEqual(set(tools), {"evaluate_decision", "remember_conversation"})
        self.assertEqual(tools["remember_conversation"]["parameters"], {
            "type": "object", "properties": {"context_json": {"type": "string"}},
            "required": ["context_json"], "additionalProperties": False})
        self.assertTrue(tools["remember_conversation"]["strict"])
        self.assertFalse(first["parallel_tool_calls"])
        self.assertIn("[message_id: msg-1]", first["input"][-1]["content"])
        followup = json.loads(request.call_args_list[1].args[0].data)
        self.assertEqual(json.loads(followup["input"][-1]["output"]), {"result": self.activity()})

    def test_disabled_routine_capture_is_unadvertised_and_rejected(self):
        with patch("dao.provider.urlopen", side_effect=[FakeResponse([completed(remember_call(self.payload()))]),
                                                        FakeResponse([completed()])]) as request:
            events = list(openai_stream(self.state(), Config(), Mock()))
        self.assertEqual(events[1], {"type": "tool_error", "name": "remember_conversation", "error": "Unknown tool"})
        first = json.loads(request.call_args_list[0].args[0].data)
        self.assertEqual([tool["name"] for tool in first["tools"]], ["evaluate_decision"])
        self.assertNotIn("Use remember_conversation", first["instructions"])

    def test_malformed_wrappers_do_not_reach_capture_callback(self):
        for arguments in ("invalid JSON", "[]", "{}", '{"context_json": {}}',
                          '{"context_json": "{}", "accept": true}', '{"context_json": "invalid JSON"}'):
            with self.subTest(arguments=arguments):
                remember = Mock()
                with patch("dao.provider.urlopen", side_effect=[
                    FakeResponse([completed(remember_call(arguments=arguments))]), FakeResponse([completed()])]):
                    events = list(openai_stream(self.state(), Config(), Mock(), remember=remember))
                remember.assert_not_called()
                self.assertEqual(events[1]["type"], "tool_error")

    def test_capture_rejections_return_diagnostics_without_success_activity(self):
        cases = [(Mock(side_effect=ValueError("Only latest-user citations are allowed")), "latest-user"),
                 (Mock(side_effect=ValueError("Routine relations cannot change action policy")), "action policy"),
                 (Mock(return_value={"error": "Tentative mention is not a decided statement"}), "Tentative mention"),
                 (Mock(return_value=None), "activity object")]
        for remember, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), patch("dao.provider.urlopen", side_effect=[
                FakeResponse([completed(remember_call(self.payload()))]), FakeResponse([completed()])]) as request:
                events = list(openai_stream(self.state(), Config(), Mock(), remember=remember))
                self.assertFalse(any(event["type"] == "activity" for event in events))
                self.assertEqual(events[1]["type"], "tool_error")
                self.assertIn(diagnostic, events[1]["error"])
                followup = json.loads(request.call_args_list[1].args[0].data)
                self.assertIn(diagnostic, json.loads(followup["input"][-1]["output"])["error"])

    def test_context_uses_latest_user_and_completed_activities_without_quote_duplication(self):
        state = self.state()
        state["messages"] = [
            {"id": "old-user", "role": "user", "content": "Earlier topic"},
            {"id": "old-assistant", "role": "assistant", "content": "Earlier answer", "status": "completed",
             "activities": [{"id": "old-activity", "kind": "remembered", "label": "Earlier option",
                             "node_ids": ["old-option"], "citations": [{"quote": "DO NOT DUPLICATE THIS PASSAGE"}]}]},
            {"id": "failed-assistant", "role": "assistant", "content": "Failed answer", "status": "failed",
             "activities": [{"id": "failed-activity", "label": "Do not reuse failed memory"}]},
            state["messages"][0]]
        state["_turn_activities"] = [self.activity()]
        original = deepcopy(state)
        instructions, messages = build_input(state, include_conversation=True)
        self.assertEqual(state, original)
        context = json.loads(messages[0]["content"].split("\n", 1)[1])
        self.assertEqual(context["latest_user_message_id"], "msg-1")
        self.assertEqual([a["id"] for a in context["recent_activities"]], ["old-activity", "activity-1"])
        self.assertNotIn("DO NOT DUPLICATE THIS PASSAGE", messages[0]["content"])
        self.assertNotIn("Failed answer", json.dumps(messages))
        self.assertNotIn("failed-activity", json.dumps(context))
        self.assertIn("mention or tentative idea is never a decision", instructions)
        self.assertIn("severe false and actions []", instructions)
        self.assertIn("Do not cite older user messages or assistant text", instructions)

    def test_context_recent_activity_and_decision_references_are_bounded(self):
        state = self.state()
        state["messages"].insert(0, {"id": "history", "role": "assistant", "content": "History", "activities": [
            {"id": f"activity-{i}", "label": "Remembered context", "citations": [{"quote": "duplicate"}]}
            for i in range(12)]})
        state["relationships"] = {"nodes": {
            f"node-{i:02d}": {"id": f"node-{i:02d}", "label": "Context", "kind": "decision",
                               "stance": "decided" if i != 11 else "mentioned", "created_at": f"2026-10-{i+1:02d}"}
            for i in range(12)}}
        # Context selection is independent of relationship validation, which the runtime
        # performs before provider admission; this isolates the reference selection here.
        with patch("dao.provider.summarize", return_value={}):
            _, messages = build_input(state, include_conversation=True)
        context = json.loads(messages[0]["content"].split("\n", 1)[1])
        self.assertEqual([a["id"] for a in context["recent_activities"]], [f"activity-{i}" for i in range(4, 12)])
        self.assertEqual(context["recent_decided_node_ids"], [f"node-{i:02d}" for i in range(3, 11)])
        self.assertEqual(context["accepted_structure"], state["relationships"])

    def test_all_tools_share_the_same_round_and_usage_limits(self):
        remember, propose = Mock(return_value=self.activity()), Mock(return_value={"id": "p-1", "status": "pending"})
        proposal = {"type": "function_call", "call_id": "proposal1", "name": "propose_structure",
                    "arguments": json.dumps({"proposal_json": json.dumps({"rationale": "Review", "operations": []})})}
        responses = [FakeResponse([completed(remember_call(self.payload()))]), FakeResponse([completed(proposal)]),
                     FakeResponse([completed(remember_call(self.payload(), call_id="remember2"))])]
        events = []
        with patch("dao.provider.urlopen", side_effect=responses) as request:
            with self.assertRaisesRegex(ProviderError, "bounded tool-round limit"):
                events.extend(openai_stream(self.state(), Config(), Mock(), propose, remember))
        remember.assert_called_once()
        propose.assert_called_once()
        self.assertEqual([e["round"] for e in events if e["type"] == "usage"], [0, 1, 2])
        self.assertEqual([e["type"] for e in events if e["type"] in {"activity", "proposal"}], ["activity", "proposal"])
        final = json.loads(request.call_args_list[2].args[0].data)
        self.assertEqual(final["tool_choice"], "none")
        self.assertEqual({tool["name"] for tool in final["tools"]},
                         {"evaluate_decision", "propose_structure", "remember_conversation"})

    def test_multiple_calls_fail_before_routine_capture(self):
        remember = Mock()
        with patch("dao.provider.urlopen", return_value=FakeResponse([
            completed(remember_call(self.payload()), remember_call(self.payload(), call_id="remember2"))])):
            with self.assertRaisesRegex(ProviderError, "at most one tool call"):
                list(openai_stream(self.state(), Config(), Mock(), remember=remember))
        remember.assert_not_called()

    def test_conversation_activity_results_cannot_exceed_context_limit(self):
        remember = Mock(return_value={"id": "activity-1", "label": "x" * MAX_CONTEXT_BYTES})
        with patch("dao.provider.urlopen", return_value=FakeResponse([completed(remember_call(self.payload()))])) as request:
            with self.assertRaisesRegex(ProviderError, "Context exceeds 1 MiB after a tool result"):
                list(openai_stream(self.state(), Config(), Mock(), remember=remember))
        self.assertEqual(request.call_count, 1)

    def test_demo_fallback_is_short_discloses_offline_and_does_not_invent_memory(self):
        state = self.state()
        original = deepcopy(state)
        with patch("dao.provider.time.sleep"):
            events = list(demo_stream(state))
        answer = "".join(event["text"] for event in events if event["type"] == "delta")
        self.assertIn("Dao’s offline demo", answer)
        self.assertIn("no AI model is connected", answer)
        self.assertLess(len(answer), 180)
        self.assertNotIn("SQLite", answer)
        self.assertNotIn("/remember", answer)
        self.assertNotIn("Saved memory", answer)
        self.assertEqual(state, original)
        self.assertEqual(events[-1]["type"], "usage")
        self.assertTrue(events[-1]["estimated"])
        state["messages"].extend([{"role": "assistant", "content": answer, "status": "completed"},
                                  {"role": "user", "content": "Thanks!"}])
        with patch("dao.provider.time.sleep"):
            followup = "".join(event["text"] for event in demo_stream(state) if event["type"] == "delta")
        self.assertEqual(followup, "You’re welcome.")


if __name__ == "__main__":
    unittest.main()
