import unittest
from unittest.mock import Mock, patch

from dao.__main__ import main, terminal_text
from dao.store import StoreError


class TerminalOutputTests(unittest.TestCase):
    def test_untrusted_output_cannot_emit_terminal_control_sequences(self):
        text = "Answer\x1b[2J\x1b]52;c;Y29weQ==\x07\x9b31m\rspoof\x08\x7f"
        rendered = terminal_text(text)
        self.assertIn(r"\x1b[2J", rendered)
        self.assertFalse(any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) <= 159 for c in rendered))

    def test_plain_unicode_and_formatting_are_preserved(self):
        text = "Dao 道 — options\n\tWait: +15"
        self.assertEqual(terminal_text(text), text)

    def test_unknown_branch_exits_instead_of_repeating_errors(self):
        agent = Mock()
        agent.store.head.side_effect = StoreError("Unknown branch")
        with patch("sys.argv", ["dao", "chat", "--branch", "missing"]), \
             patch("dao.__main__.Store"), patch("dao.__main__.Agent", return_value=agent), \
             patch("pathlib.Path.mkdir"), patch("builtins.input") as read_input, \
             patch("builtins.print"), patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 1)
        read_input.assert_not_called()
        agent.store.head.assert_called_once_with("missing")


if __name__ == "__main__":
    unittest.main()
