"""Deterministic writing examples; live inference uses the shared provider."""

import json
import re
import time


def demo_stream(state):
    message = state["messages"][-1]["content"]
    memory_count = len(state.get("memory", {}))
    answer = ("This is Dao Narrative’s offline demo. Conversations, story notes, drafts, and usage are saved; "
              "this reply comes from a deterministic simulator.\n\n")
    if re.search(r"wait|uncertain|launch|risk|revers", message, re.I):
        answer += ("Keep an alternate draft open while you explore this choice. Separate established story details "
                   "from proposed ideas and open questions. Try a scene before committing to its consequences. "
                   "A reader response may help clarify what needs setup. Open Explore to compare explicit "
                   "assumptions, or enter /explore in the terminal. Those numbers do not score literary quality.\n\n")
    else:
        answer += (f"You wrote: {message}\n\nStart with what the character wants, what stands in the way, "
                   "and what changes by the end of the scene. Keep established details, proposed ideas, and open "
                   "questions visible. Use /remember key=value to save a story note, or create another draft "
                   "from a saved version to try a different direction.\n\n")
    answer += f"Saved story notes in this draft: {memory_count}."
    for chunk in re.findall(r"\S+\s*", answer):
        yield {"type": "delta", "text": chunk}
        time.sleep(0.008)
    yield {"type": "usage", "input_tokens": max(1, len(json.dumps(state["messages"])) // 4),
           "output_tokens": max(1, len(answer) // 4), "estimated": True, "status": "completed"}
