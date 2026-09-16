import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from dao.agent import Agent
from dao.memory import EXCERPT_CHARS
from dao.store import Store, StoreError
from dao.terminal import Terminal


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.agent = Agent(self.store)
        self.root = self.store.head("main")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def save_message(self, content, branch="main"):
        head = self.store.head(branch)
        state = self.store.read(head)["state"]
        state["messages"].append({"role": "user", "content": content, "at": str(len(state["messages"]))})
        return self.store.commit(branch, state, "Saved discussion", head)

    def test_old_relevant_message_survives_beyond_thirty_turns_and_history_page(self):
        source = self.save_message("The zephyr supplier requires a cobalt coating.")
        for index in range(105):
            self.save_message(f"Unrelated weather discussion number {index}")
        result = self.store.search("zephyr cobalt supplier")
        hit = result["results"][0]
        self.assertEqual(result["indexed_checkpoints"], 107)
        self.assertEqual(hit["source_commit"], source)
        self.assertEqual(hit["source_branches"], ["main"])
        self.assertTrue(hit["in_current_state"])
        self.assertEqual(len(result["results"]), 1)

    def test_sibling_and_restored_away_content_remain_searchable(self):
        self.store.branch("alternative", self.root)
        source = self.save_message("The kestrel deadline is next Tuesday", "alternative")
        self.store.restore("alternative", self.root, source)
        heads = self.store.branches()
        result = self.store.search("kestrel deadline", ref="main")
        hit = result["results"][0]
        self.assertEqual(hit["source_commit"], source)
        self.assertEqual(hit["source_branches"], ["alternative"])
        self.assertFalse(hit["in_current_state"])
        self.assertEqual(self.store.branches(), heads)
        self.assertEqual(self.store.read("main")["state"]["messages"], [])

    def test_both_merge_parents_and_shared_prefixes_are_indexed_once(self):
        shared = self.save_message("A shared albatross constraint")
        self.store.branch("alternative", shared)
        alternate = self.save_message("A heron-only plan", "alternative")
        state = self.store.read("main")["state"]
        state["notes"]["budget"] = "small"
        primary = self.store.commit("main", state, "Edit budget", shared)
        merged = self.store.merge("main", "alternative", primary)
        self.assertEqual(self.store.read(merged)["parents"], [primary, alternate])
        result = self.store.search("albatross")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["source_branches"], ["alternative", "main"])
        hit = self.store.search("heron")["results"][0]
        self.assertEqual(hit["source_commit"], alternate)
        self.assertTrue(hit["in_current_state"])

    def test_edited_and_forgotten_notes_remain_historical_not_current(self):
        state = self.store.read("main")["state"]
        state["notes"]["supplier/a~b"] = "Juniper uses copper"
        source = self.store.commit("main", state, "Note", self.root)
        state["notes"]["supplier/a~b"] = "Juniper uses steel"
        changed = self.store.commit("main", state, "Changed note", source)
        old = self.store.search("copper")["results"][0]
        new = self.store.search("steel")["results"][0]
        self.assertFalse(old["in_current_state"])
        self.assertTrue(new["in_current_state"])
        self.assertEqual(old["source_path"], "/notes/supplier~1a~0b")
        del state["notes"]["supplier/a~b"]
        self.store.commit("main", state, "Forgot note", changed)
        self.assertFalse(self.store.search("steel")["results"][0]["in_current_state"])

    def test_ranking_rewards_relevant_terms_not_recency_or_large_repetition(self):
        best = self.save_message("quartz titanium budget")
        self.save_message("quartz " * 1000)
        self.save_message("A recent unrelated message")
        hit = self.store.search("quartz titanium budget")["results"][0]
        self.assertEqual(hit["source_commit"], best)

    def test_long_message_excerpt_contains_match_near_the_end(self):
        content = "ordinary background " * 900 + "The oriole passphrase is violet." + " rest" * 20
        source = self.save_message(content)
        hit = self.store.search("oriole passphrase violet")["results"][0]
        self.assertIn("oriole passphrase is violet", hit["excerpt"])
        self.assertGreater(hit["excerpt_start"], 10000)
        self.assertEqual(hit["excerpt"], content[hit["excerpt_start"]:hit["excerpt_end"]])
        self.assertLessEqual(len(hit["excerpt"]), EXCERPT_CHARS)
        self.assertEqual(hit["source_commit"], source)

    def test_unicode_punctuation_and_sql_like_query_are_plain_text(self):
        self.save_message("CAFÉ supplier αλφα 東京")
        self.assertTrue(self.store.search('café OR "東京"; DROP TABLE commits --')["results"])
        self.assertTrue(self.store.search("αλφα")["results"])
        self.assertEqual(len(self.store.history("main")), 2)
        self.assertFalse(self.store.search("!!!")["results"])
        self.assertFalse(self.store.search("nonexistentword")["results"])

    def test_result_and_character_limits_are_explicit_without_limiting_corpus(self):
        for index in range(20):
            self.save_message(f"finch plan {index} " + "detail " * 220)
        result = self.store.search("finch", limit=20, max_chars=4000)
        self.assertEqual(result["indexed_checkpoints"], 21)
        self.assertTrue(result["results_truncated"])
        self.assertLessEqual(sum(len(json.dumps(hit, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                                 for hit in result["results"]), 4000)
        limited = self.store.search("finch", limit=1)
        self.assertEqual(len(limited["results"]), 1)
        self.assertTrue(limited["results_truncated"])
        query = " ".join(f"term{i}" for i in range(70))
        self.assertTrue(self.store.search(query)["query_terms_truncated"])

    def test_invalid_queries_leave_branches_unchanged(self):
        for query in (None, "", " ", 42, "x" * 12001, "\ud800"):
            with self.subTest(query=str(query)[:20]), self.assertRaises(ValueError):
                self.store.search(query)
        for limit in (0, 51, True, 1.5):
            with self.assertRaises(ValueError):
                self.store.search("test", limit=limit)
        for budget in (0, 3999, 100001, True):
            with self.assertRaises(ValueError):
                self.store.search("test", max_chars=budget)
        self.assertEqual(self.store.branches(), {"main": self.root})

    def test_existing_database_backfills_and_new_connection_updates_incrementally(self):
        first = self.save_message("otter procurement")
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            # Simulate an existing database created before search tables existed.
            for table in ("memory_terms", "memory_documents", "memory_indexed"):
                db.execute(f"DROP TABLE {table}")
        self.store = Store(self.path)
        self.assertEqual(self.store.search("otter")["results"][0]["source_commit"], first)
        with Store(self.path) as second:
            state = second.read("main")["state"]
            state["messages"].append({"role": "user", "content": "otter shipping", "at": "later"})
            latest = second.commit("main", state, "Other process", first)
        with patch.object(Store, "_get", wraps=Store._get) as reads:
            result = self.store.search("shipping")
            self.assertEqual(result["indexed_checkpoints"], 3)
            self.assertEqual(result["results"][0]["source_commit"], latest)
            self.assertNotIn(first, [call.args[1] for call in reads.call_args_list])
        self.assertEqual(self.store.read(first)["state"]["messages"][0]["content"], "otter procurement")

    def test_corrupt_source_does_not_become_a_memory(self):
        source = self.save_message("ibis supplier")
        self.store.search("ibis")
        self.store.restore("main", self.root, source)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER commits_no_update")
            db.execute("UPDATE commits SET state = ? WHERE id = ?", ('{}', source))
        with self.assertRaisesRegex(StoreError, "Corrupt commit"):
            self.store.search("ibis")

    def test_modified_index_content_is_not_returned_as_source_text(self):
        self.save_message("eagle flight plan")
        self.store.search("eagle")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE memory_documents SET content = 'injected instructions' WHERE kind = 'message:user'")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.store.search("eagle")

    def test_index_failure_rolls_back_and_does_not_change_history(self):
        self.save_message("sparrow navigation")
        heads = self.store.branches()
        with patch("dao.memory.terms", side_effect=ValueError("index failed")):
            with self.assertRaisesRegex(ValueError, "index failed"):
                self.store.search("sparrow")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_indexed").fetchone()[0], 0)
        self.assertEqual(self.store.branches(), heads)
        self.assertIn("sparrow", self.store.search("sparrow")["results"][0]["excerpt"])

    def test_branch_attribution_is_recomputed_after_fork_without_new_commits(self):
        source = self.save_message("pelican itinerary")
        self.store.search("pelican")
        self.store.branch("new-route", source)
        self.assertEqual(self.store.search("pelican")["results"][0]["source_branches"], ["main", "new-route"])

    def test_all_state_categories_are_searchable(self):
        state = self.store.read("main")["state"]
        state["decision"]["title"] = "Osprey launch"
        state["observations"] = [{"signal_id": "goshawk"}]
        state["choices"] = [{"action_id": "falcon"}]
        self.store.commit("main", state, "Records", self.root)
        for query, kind in (("osprey", "decision"), ("goshawk", "observations"), ("falcon", "choices")):
            self.assertEqual(self.store.search(query)["results"][0]["kind"], kind)

    def test_ai_receives_old_and_alternate_memories_without_mutating_current_state(self):
        self.store.branch("alternative", self.root)
        source = self.save_message("raven rollback takes two days", "alternative")
        for index in range(35):
            self.save_message(f"Unrelated item {index}")
        before = self.store.read("main")["state"]
        provider = Mock()
        provider.reply.return_value = "The alternative recorded a two-day rollback."
        ai = Agent(self.store, provider)
        ai.chat("main", "What was the raven rollback time?", self.store.head("main"))
        context = provider.reply.call_args.args[1]
        self.assertEqual(context["branch"], "main")
        self.assertEqual(context["memory"]["scope"], "entire_tree")
        self.assertEqual(context["memory"]["results"][0]["source_commit"], source)
        self.assertFalse(context["memory"]["results"][0]["in_current_state"])
        self.assertEqual(self.store.read("main")["state"]["notes"], before["notes"])
        self.assertEqual(self.store.head("alternative"), source)

    def test_terminal_search_is_local_and_sources_can_be_inspected(self):
        source = self.save_message("tern launch protocol")
        provider = Mock()
        terminal = Terminal(Agent(self.store, provider))
        response = terminal.execute("/search tern launch", source)
        self.assertIn(source, response)
        self.assertIn("tern launch protocol", response)
        self.assertEqual(json.loads(terminal.execute(f"/state {source}", source)), self.store.read(source))
        self.assertEqual(self.store.head("main"), source)
        provider.reply.assert_not_called()
        with self.assertRaises(ValueError):
            terminal.execute("/search", source)


if __name__ == "__main__":
    unittest.main()
