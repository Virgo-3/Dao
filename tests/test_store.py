import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from dao.store import BudgetError, ConflictError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "dao.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def commit(self, branch="main", text="Hello"):
        head = self.store.head(branch)
        state = head["state"]
        state["messages"].append({"role": "user", "content": text})
        return self.store.commit(branch, state, "message", text, head["id"])

    def test_bootstrap_and_persistence(self):
        self.assertEqual(self.store.head()["state"], {
            "messages": [], "memory": {}, "decisions": [], "artifacts": {}})
        committed = self.commit()
        self.assertEqual(Store(self.path).head(), committed)
        self.assertTrue(self.store.verify()["ok"])

    def test_branch_isolation_and_detached_snapshots(self):
        initial = self.store.head()
        self.store.branch("what-if", initial["id"])
        divergent = self.commit("what-if", "Possibility")
        self.assertEqual(self.store.head()["id"], initial["id"])
        divergent["state"]["memory"]["changed"] = True
        self.assertEqual(self.store.head("what-if")["state"]["memory"], {})
        self.assertTrue(self.store.verify()["ok"])

    def test_compare_and_swap_with_real_concurrency(self):
        original = self.store.head()
        barrier = threading.Barrier(2)

        def attempt(index):
            second_store = Store(self.path)
            barrier.wait()
            try:
                return second_store.commit("main", original["state"], "message",
                                           f"Candidate {index}", original["id"])["id"]
            except ConflictError:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, range(2)))
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(len(self.store.history()), 2)
        self.assertTrue(self.store.verify()["ok"])

    def test_restore_appends_and_keeps_usage(self):
        initial = self.store.head()
        changed = self.commit()
        self.store.reserve_usage("main", "request-1", 200, 1000)
        self.store.finalize_usage("request-1", 90, 40, False, "completed", "example", 250)
        restored = self.store.restore("main", initial["id"], changed["id"])
        self.assertEqual(restored["state"], initial["state"])
        self.assertEqual(restored["parent_id"], changed["id"])
        self.assertEqual(len(self.store.history()), 3)
        self.assertEqual(self.store.usage()["total_tokens"], 130)
        self.assertEqual(self.store.usage()["cost_microusd"], 250)
        self.assertTrue(self.store.verify()["ok"])

    def test_restore_requires_reachable_ancestor(self):
        original = self.store.head()
        self.store.branch("other", original["id"])
        foreign = self.commit("other")
        with self.assertRaises(ValueError):
            self.store.restore("main", foreign["id"], original["id"])
        with self.assertRaises(ConflictError):
            self.store.restore("main", original["id"], "stale")

    def test_global_budget_and_terminal_idempotency(self):
        self.store.branch("other", self.store.head()["id"])
        self.store.reserve_usage("main", "one", 90, 100)
        self.store.reserve_usage("main", "one", 90, 100)
        with self.assertRaises(BudgetError):
            self.store.reserve_usage("other", "two", 11, 100)
        first = self.store.finalize_usage("one", 20, 30, False, "completed", "example", 7)
        again = self.store.finalize_usage("one", 90, 0, True, "failed", "other", 999)
        self.assertEqual(first, again)
        self.store.reserve_usage("other", "two", 50, 100)
        self.assertEqual(self.store.usage()["reserved_tokens"], 50)
        self.assertEqual(self.store.usage()["total_tokens"], 50)
        self.assertEqual(self.store.usage()["cost_microusd"], 7)
        with self.assertRaises(ConflictError):
            self.store.reserve_usage("main", "one", 1, 100)
        self.assertTrue(self.store.verify()["ok"])

    def test_atomic_reservations_under_concurrency(self):
        barrier = threading.Barrier(2)

        def attempt(index):
            barrier.wait()
            try:
                self.store.reserve_usage("main", str(index), 60, 100)
                return "reserved"
            except BudgetError:
                return "denied"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, range(2)))
        self.assertEqual(sorted(results), ["denied", "reserved"])
        self.assertEqual(self.store.usage()["reserved_tokens"], 60)

    def test_unknown_failure_retains_reservation(self):
        self.store.reserve_usage("main", "unknown", 100, 100)
        entry = self.store.finalize_usage("unknown", 0, 0, True, "unknown", "example")
        self.assertEqual(entry["total_tokens"], 100)
        self.assertTrue(entry["estimated"])
        self.assertEqual(self.store.usage()["reserved_tokens"], 0)
        with self.assertRaises(BudgetError):
            self.store.reserve_usage("main", "next", 1, 100)
        self.assertTrue(self.store.verify()["ok"])

    def test_incomplete_response_keeps_known_usage(self):
        self.store.reserve_usage("main", "partial", 100, 100)
        entry = self.store.finalize_usage("partial", 30, 10, False, "incomplete", "example")
        self.assertEqual(entry["total_tokens"], 40)
        self.assertFalse(entry["estimated"])
        self.assertEqual(entry["status"], "incomplete")
        self.assertTrue(self.store.verify()["ok"])

    def test_commit_hash_tampering_is_detected(self):
        committed = self.commit()
        with closing(sqlite3.connect(self.path)) as conn:
            with conn:
                conn.execute("UPDATE commits SET state=? WHERE id=?",
                             (json.dumps({"messages": ["tampered"]}), committed["id"]))
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("Commit hash mismatch" in error for error in result["errors"]))

    def test_journal_and_usage_tampering_is_detected(self):
        self.store.reserve_usage("main", "one", 30, 100)
        self.store.finalize_usage("one", 10, 5, False, "completed", "example")
        with closing(sqlite3.connect(self.path)) as conn:
            with conn:
                conn.execute("UPDATE usage_ledger SET input_tokens=0")
                conn.execute("UPDATE events SET payload='{}' WHERE id=1")
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertIn("Usage ledger differs from the journal", result["errors"])
        self.assertTrue(any("Event hash mismatch" in error for error in result["errors"]))

    def test_head_tampering_is_detected(self):
        initial = self.store.head()
        self.commit()
        with closing(sqlite3.connect(self.path)) as conn:
            with conn:
                conn.execute("UPDATE branches SET head=? WHERE name='main'", (initial["id"],))
        self.assertFalse(self.store.verify()["ok"])

    def test_reopen_does_not_hide_deleted_branch(self):
        with closing(sqlite3.connect(self.path)) as conn:
            with conn:
                conn.execute("DELETE FROM branches WHERE name='main'")
        with self.assertRaises(ValueError):
            Store(self.path)
        self.assertFalse(self.store.verify()["ok"])

    def test_branch_metadata_tampering_is_detected(self):
        with closing(sqlite3.connect(self.path)) as conn:
            with conn:
                conn.execute("UPDATE branches SET created_at='tampered'")
        self.assertIn("Branch metadata differs from the journal", self.store.verify()["errors"])

    def test_validation_rejects_nonfinite_and_invalid_inputs(self):
        initial = self.store.head()
        for name in ("../main", "bad name", "a//b", "a..b", ""):
            with self.assertRaises(ValueError):
                self.store.branch(name, initial["id"])
        with self.assertRaises(ValueError):
            self.store.commit("main", {"value": float("nan")}, "message", "Invalid", initial["id"])
        with self.assertRaises(ValueError):
            self.store.reserve_usage("main", "one", True, 100)
        with self.assertRaises(ValueError):
            self.store.append_event("main", "usage.finalized", {})
        with self.assertRaises(KeyError):
            self.store.head("missing")

    def test_global_journal_and_branch_filter(self):
        self.store.branch("other", self.store.head()["id"])
        event = self.store.append_event("other", "audit.verdict", {"verdict": "wait"})
        self.assertEqual(self.store.events("other")[-1], event)
        self.assertEqual(self.store.events()[-1], event)
        self.assertTrue(self.store.verify()["ok"])

    def test_memory_store_supports_per_operation_connections(self):
        store = Store(":memory:")
        head = store.head()
        store.commit("main", head["state"], "test", "Memory", head["id"])
        self.assertEqual(len(store.history()), 2)
        self.assertTrue(store.verify()["ok"])
        store.close()


if __name__ == "__main__":
    unittest.main()
