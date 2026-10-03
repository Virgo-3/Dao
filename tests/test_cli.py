"""Terminal chat presents conversation text while keeping structured output opt-in."""

import json
import subprocess
import sys

from dao.cli import print_chat_reply
from dao.store import Store


def run_chat(tmp_path, *args, input_text=None):
    result = subprocess.run(
        [sys.executable, "-m", "dao.cli", "--db", str(tmp_path / "state.db"), "chat", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def run_cli(tmp_path, *args):
    result = subprocess.run(
        [sys.executable, "-m", "dao.cli", "--db", str(tmp_path / "state.db"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def test_piped_chat_prints_reply_without_prompts_or_internal_metadata(tmp_path):
    output = run_chat(tmp_path, input_text="What is your purpose?\n/quit\n")
    assert "You: " not in output
    assert "Dao: Offline demo:" in output
    assert "Your message: What is your purpose?" in output
    assert '"head"' not in output
    assert '"text"' not in output


def test_one_shot_chat_is_plain_text_unless_json_requested(tmp_path):
    plain = run_chat(tmp_path, "hello")
    assert plain.startswith("Dao: Offline demo:")
    assert '"head"' not in plain

    structured = json.loads(run_chat(tmp_path, "--json", "hello"))
    assert structured["text"].startswith("Offline demo:")
    assert len(structured["head"]) == 64


def test_chat_keeps_proposal_review_action_visible_without_dumping_metadata(tmp_path):
    output = run_chat(tmp_path, '/remember preference "concise"')
    assert "Dao: Memory change proposed; adjudication required." in output
    assert "A proposal awaits review." in output
    assert "dao proposals" in output
    assert '"proposal"' not in output
    assert '"head"' not in output


def test_chat_reply_renders_unicode_and_paragraphs(capsys):
    print_chat_reply({"text": "I’m Dao.\n\nLet’s talk.", "head": "internal"})
    assert capsys.readouterr().out == "Dao: I’m Dao.\n\nLet’s talk.\n\n"


def test_projection_is_plain_text_by_default_and_does_not_mutate_state(tmp_path):
    run_cli(tmp_path, "branch", "experiment")
    store = Store(tmp_path / "state.db")
    try:
        before_head = store.head("experiment")
        before_events = store.events()
    finally:
        store.close()

    plain = run_cli(tmp_path, "project", "--branch", "experiment")
    structured = json.loads(run_cli(tmp_path, "project", "--branch", "experiment", "--json"))

    assert plain.strip() == structured["text"].strip()
    assert structured["branch"] == "experiment"
    assert structured["head"] == before_head
    assert "event_seq" in structured
    assert isinstance(structured["claims"], list)
    assert "coverage" in structured

    store = Store(tmp_path / "state.db")
    try:
        assert store.head("experiment") == before_head
        assert store.events() == before_events
    finally:
        store.close()
