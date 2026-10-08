"""Writing examples and wording; state transitions remain owned by Dao."""

from dao.service import Dao

from .examples import demo_payload
from .provider import demo_stream


class NarrativeDao(Dao):
    def example_problem(self):
        return demo_payload()

    def demo_response(self, state):
        return demo_stream(state)

    def memory_message(self, key, value):
        return f"Saved story note {key}: {value}. This note stays with this draft."

    def decision_message(self, result):
        return "Story comparison: " + result["reason"] + "\n\nInspect Explore for the assumptions and calculation."
