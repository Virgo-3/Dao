import re
import sqlite3
import tempfile
import unicodedata
import unittest
from contextlib import closing
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from dao.agent import Agent
from dao.memory import EXCERPT_CHARS, terms
from dao.store import Store


class UnicodeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        Agent(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def save_message(self, content):
        head = self.store.head("main")
        state = self.store.read(head)["state"]
        state["messages"].append({"role": "user", "content": content,
                                  "at": str(len(state["messages"]))})
        return self.store.commit("main", state, "Unicode memory", head)

    def assert_search_finds_source(self, content, query):
        source = self.save_message(content)
        hits = self.store.search(query, limit=50)["results"]
        hit = next((hit for hit in hits if hit["source_commit"] == source), None)
        self.assertIsNotNone(hit, f"{query!r} did not retrieve {content!r}")
        self.assertEqual(hit["excerpt"], content)
        return hit

    def database_rows(self, tables):
        with closing(sqlite3.connect(self.path)) as db:
            return {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in tables}

    def make_legacy_cache(self):
        source = self.save_message("cafe\u0301")
        self.store.branch("alternative", source)
        self.store.search("caf\u00e9")
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            # The original tokenizer split decomposed accents before normalizing.
            documents = db.execute("SELECT id, content FROM memory_documents").fetchall()
            db.execute("DELETE FROM memory_terms")
            for document_id, content in documents:
                counts = Counter(unicodedata.normalize("NFKC", word).casefold()
                                 for word in re.findall(r"[^\W_]+", content))
                db.execute("UPDATE memory_documents SET length = ? WHERE id = ?",
                           (sum(counts.values()), document_id))
                db.executemany("INSERT INTO memory_terms VALUES (?, ?, ?)",
                               [(word, document_id, count) for word, count in counts.items()])
            db.execute("DROP TABLE IF EXISTS memory_index_version")
        self.store = Store(self.path)
        return source

    def test_canonical_equivalence_in_both_source_and_query(self):
        pairs = [
            ("caf\u00e9", "cafe\u0301"),
            # Neither q plus accent has a precomposed Unicode character.
            ("q\u0301\u0323", "q\u0323\u0301"),
            ("\ud55c\uae00", unicodedata.normalize("NFD", "\ud55c\uae00")),
            ("\u03b1\u0345\u0301", unicodedata.normalize("NFC", "\u03b1\u0345\u0301")),
        ]
        for first, second in pairs:
            for content, query in ((first, second), (second, first)):
                with self.subTest(content=content, query=query):
                    self.assert_search_finds_source(content, query)

    def test_compatibility_and_casefold_equivalence(self):
        pairs = [
            ("\uff23\uff21\uff26\uff25\u0301", "caf\u00e9"),
            ("o\ufb03ce", "OFFICE"),
            ("Stra\u00dfe", "STRASSE"),
            ("\u2122", "tm"),
            ("\u2103", "c"),
        ]
        for first, second in pairs:
            for content, query in ((first, second), (second, first)):
                with self.subTest(content=content, query=query):
                    self.assert_search_finds_source(content, query)

    def test_noncomposable_accents_remain_part_of_the_word(self):
        accented = self.save_message("q\u0301")
        plain = self.save_message("q")
        self.assertEqual([hit["source_commit"] for hit in self.store.search("q\u0301")["results"]],
                         [accented])
        self.assertEqual([hit["source_commit"] for hit in self.store.search("q")["results"]],
                         [plain])

    def test_unattached_marks_do_not_obscure_words_or_become_search_terms(self):
        self.assertEqual(terms("\u0301caf\u00e9"), terms("caf\u00e9"))
        self.assertEqual(terms("\u0301\u0323"), [])
        # Greek ypogegrammeni case-folds into a letter, which must be preserved.
        self.assertEqual(terms("\u0345caf\u00e9"), ["\u03b9caf\u00e9"])
        self.assert_search_finds_source("caf\u00e9", "\u0301cafe\u0301")

    def test_excerpts_locate_matches_after_long_unbroken_source_spans(self):
        cases = [
            ("\u0301" * 1500 + "caf\u00e9" + " tail" * 300, "caf\u00e9"),
            ("x" * 15000 + "\u00bdcaf\u00e9" + " tail" * 300, "2caf\u00e9"),
        ]
        for content, query in cases:
            with self.subTest(query=query):
                source = self.save_message(content)
                hit = next(hit for hit in self.store.search(query, limit=50)["results"]
                           if hit["source_commit"] == source)
                self.assertIn("caf\u00e9", hit["excerpt"])
                self.assertGreater(hit["excerpt_start"], 1000)
                self.assertEqual(hit["excerpt"], content[hit["excerpt_start"]:hit["excerpt_end"]])
                self.assertLessEqual(len(hit["excerpt"]), EXCERPT_CHARS)

    def test_long_excerpts_keep_original_offsets_when_normalization_changes_length(self):
        cases = [
            ("o\ufb03ce " * 1800, "cafe\u0301", "caf\u00e9"),
            ("re\u0301sume\u0301 " * 1500, "caf\u00e9", "cafe\u0301"),
            ("background " * 1500, unicodedata.normalize("NFD", "\ud55c\uae00"), "\ud55c\uae00"),
            # Compatibility expansion contains a separator inside one source character.
            ("x" * 15000, "\u00bdcaf\u00e9", "2caf\u00e9"),
        ]
        for prefix, target, query in cases:
            with self.subTest(target=target, query=query):
                content = prefix + "The target is " + target + "." + " trailing context" * 150
                source = self.save_message(content)
                hits = self.store.search(query, limit=50)["results"]
                hit = next((hit for hit in hits if hit["source_commit"] == source), None)
                self.assertIsNotNone(hit)
                self.assertIn(target, hit["excerpt"])
                self.assertGreater(hit["excerpt_start"], 5000)
                self.assertEqual(hit["content_chars"], len(content))
                self.assertEqual(hit["excerpt"], content[hit["excerpt_start"]:hit["excerpt_end"]])
                self.assertEqual(hit["excerpt_end"] - hit["excerpt_start"], len(hit["excerpt"]))
                self.assertLessEqual(len(hit["excerpt"]), EXCERPT_CHARS)

    def test_legacy_cache_rebuilds_without_changing_snapshots_or_branches(self):
        source = self.make_legacy_cache()
        before = self.database_rows(("commits", "branches"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM memory_terms WHERE term = 'caf\u00e9'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_indexed").fetchone()[0], 2)
        result = self.store.search("caf\u00e9")
        self.assertEqual(result["results"][0]["source_commit"], source)
        self.assertEqual(result["results"][0]["source_branches"], ["alternative", "main"])
        self.assertEqual(result["indexed_checkpoints"], 2)
        self.assertEqual(before, self.database_rows(("commits", "branches")))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT version FROM memory_index_version WHERE id = 1").fetchone()[0], 2)

    def test_failed_legacy_rebuild_rolls_back_the_entire_cache(self):
        source = self.make_legacy_cache()
        tables = ("commits", "branches", "memory_terms", "memory_documents", "memory_indexed",
                  "memory_index_version")
        before = self.database_rows(tables)
        with patch("dao.memory.terms", side_effect=ValueError("cannot index this snapshot")):
            with self.assertRaisesRegex(ValueError, "cannot index"):
                self.store.search("caf\u00e9")
        self.assertEqual(before, self.database_rows(tables))
        self.assertEqual(self.store.search("caf\u00e9")["results"][0]["source_commit"], source)

    def test_version_one_cache_is_upgraded_but_future_version_is_preserved(self):
        source = self.make_legacy_cache()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO memory_index_version VALUES (1, 1)")
        self.assertEqual(self.store.search("caf\u00e9")["results"][0]["source_commit"], source)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE memory_index_version SET version = 999 WHERE id = 1")
        tables = ("commits", "branches", "memory_terms", "memory_documents", "memory_indexed",
                  "memory_index_version")
        before = self.database_rows(tables)
        with self.assertRaisesRegex(ValueError, "version"):
            self.store.search("caf\u00e9")
        self.assertEqual(before, self.database_rows(tables))

    def test_connection_opened_before_upgrade_can_add_searchable_memories(self):
        first = self.make_legacy_cache()
        with Store(self.path) as second:
            self.store.search("caf\u00e9")
            state = second.read("main")["state"]
            state["messages"].append({"role": "user", "content": "re\u0301sume\u0301", "at": "later"})
            latest = second.commit("main", state, "Another connection", first)
            result = second.search("r\u00e9sum\u00e9")
            self.assertEqual(result["indexed_checkpoints"], 3)
            self.assertEqual(result["results"][0]["source_commit"], latest)
        self.assertEqual(self.store.search("re\u0301sume\u0301")["results"][0]["source_commit"], latest)


if __name__ == "__main__":
    unittest.main()
