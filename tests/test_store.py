import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from dao.store import ConflictError, MergeConflict, Store, StoreError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.initial = {"messages": [], "beliefs": {"rain": 0.4}, "present_null": None}
        self.root = self.store.initialize(self.initial)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_snapshots_are_immutable_and_persistent(self):
        self.initial["beliefs"]["rain"] = 0.8
        snapshot = self.store.read("main")
        self.assertEqual(snapshot["state"]["beliefs"]["rain"], 0.4)
        snapshot["state"]["beliefs"]["rain"] = 1
        self.assertEqual(self.store.read(self.root)["state"]["beliefs"]["rain"], 0.4)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.initialize({"ignored": True}), self.root)
        self.assertEqual(self.store.branches(), {"main": self.root})

    def test_commit_hash_covers_state_and_metadata(self):
        record = self.store.read(self.root)
        envelope = {key: record[key] for key in ("parents", "message", "created_at", "state")}
        envelope["version"] = 1
        content = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(hashlib.sha256(content.encode("utf-8")).hexdigest(), self.root)
        with closing(sqlite3.connect(self.path)) as db, db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE commits SET message = 'tampered'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM commits")

    def test_branch_isolation_and_append_only_restore(self):
        self.store.branch("experiment/wait", "main")
        changed = self.store.commit("experiment/wait", {"answer": 42}, "Try a branch", self.root)
        self.assertEqual(self.store.head("main"), self.root)
        restored = self.store.restore("experiment/wait", self.root, changed)
        self.assertNotEqual(restored, self.root)
        self.assertEqual(self.store.read(restored)["state"], self.initial)
        self.assertEqual([r["id"] for r in self.store.history("experiment/wait")],
                         [restored, changed, self.root])
        self.assertEqual(self.store.read(changed)["state"], {"answer": 42})

    def test_diff_handles_null_missing_json_types_and_pointer_escaping(self):
        state = {"messages": [], "beliefs": {"rain": 0.4}, "new_null": None,
                 "a/b~c": True}
        changed = self.store.commit("main", state, "Different keys", self.root)
        changes = {r["path"]: r for r in self.store.diff(self.root, changed)}
        self.assertFalse(changes["/new_null"]["before_exists"])
        self.assertTrue(changes["/new_null"]["after_exists"])
        self.assertTrue(changes["/present_null"]["before_exists"])
        self.assertFalse(changes["/present_null"]["after_exists"])
        self.assertIn("/a~1b~0c", changes)
        numeric = self.store.commit("main", {**state, "a/b~c": 1}, "Number not boolean", changed)
        self.assertEqual(self.store.diff(changed, numeric)[0]["path"], "/a~1b~0c")

    def test_three_way_merge_independent_nested_edits(self):
        self.store.branch("scenario", self.root)
        left = {**self.initial, "beliefs": {"rain": 0.7}, "new": {"left": 1}}
        right = {**self.initial, "messages": [{"role": "user", "content": "Wait?"}],
                 "new": {"right": 2}}
        left_id = self.store.commit("main", left, "Update belief", self.root)
        right_id = self.store.commit("scenario", right, "Record question", self.root)
        merged = self.store.merge("main", "scenario", left_id)
        record = self.store.read(merged)
        self.assertEqual(record["parents"], [left_id, right_id])
        self.assertEqual(record["state"]["beliefs"], {"rain": 0.7})
        self.assertEqual(record["state"]["messages"], right["messages"])
        self.assertEqual(record["state"]["new"], {"left": 1, "right": 2})
        self.assertEqual(self.store.head("scenario"), right_id)
        self.assertEqual(self.store.merge("main", "scenario", merged), merged)

    def test_fast_forward_merge_keeps_two_parents(self):
        self.store.branch("scenario", self.root)
        source = self.store.commit("scenario", {"x": 1}, "Advance", self.root)
        merged = self.store.merge("main", "scenario", self.root)
        self.assertEqual(self.store.read(merged)["parents"], [self.root, source])
        self.assertEqual(self.store.read(merged)["state"], {"x": 1})

    def test_ambiguous_criss_cross_merge_bases_require_resolution(self):
        self.store.branch("scenario", self.root)
        left = self.store.commit("main", {**self.initial, "a": 1}, "Left", self.root)
        right = self.store.commit("scenario", {**self.initial, "b": 1}, "Right", self.root)
        self.store.branch("left-before-merge", left)
        left_merged = self.store.merge("main", "scenario", left)
        right_merged = self.store.merge("scenario", "left-before-merge", right)
        with self.assertRaises(MergeConflict) as raised:
            self.store.merge("main", "scenario", left_merged)
        self.assertEqual(raised.exception.paths, ["/"])
        self.assertEqual(self.store.head("main"), left_merged)
        self.assertEqual(self.store.head("scenario"), right_merged)

    def test_merge_preserves_unilateral_delete_and_identical_edits(self):
        self.store.branch("scenario", self.root)
        left_state = {key: value for key, value in self.initial.items() if key != "present_null"}
        left_state["shared"] = [1, 2]
        left = self.store.commit("main", left_state, "Delete and add", self.root)
        self.store.commit("scenario", {**self.initial, "shared": [1, 2]}, "Same addition", self.root)
        merged = self.store.merge("main", "scenario", left)
        self.assertEqual(self.store.read(merged)["state"], left_state)

    def test_semantic_merge_validator_can_reject_without_partial_commit(self):
        self.store.branch("scenario", self.root)
        left = self.store.commit("main", {**self.initial, "min": 5}, "Minimum", self.root)
        right = self.store.commit("scenario", {**self.initial, "max": 3}, "Maximum", self.root)
        with closing(sqlite3.connect(self.path)) as db, db:
            count = db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]

        def validate(state):
            if state["min"] > state["max"]:
                raise ValueError("Minimum exceeds maximum.")

        with self.assertRaisesRegex(ValueError, "Minimum"):
            self.store.merge("main", "scenario", left, validate=validate)
        self.assertEqual(self.store.branches(), {"main": left, "scenario": right})
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM commits").fetchone()[0], count)

    def test_validator_cannot_modify_merged_snapshot(self):
        self.store.branch("scenario", self.root)
        self.store.commit("scenario", {**self.initial, "a": 1}, "Addition", self.root)
        merged = self.store.merge("main", "scenario", self.root,
                                  validate=lambda state: state.update({"injected": True}))
        self.assertNotIn("injected", self.store.read(merged)["state"])

    def test_conflicting_conversations_fail_atomically(self):
        self.store.branch("scenario", self.root)
        left = self.store.commit("main", {**self.initial, "messages": ["act"]}, "Act", self.root)
        right = self.store.commit("scenario", {**self.initial, "messages": ["wait"]}, "Wait", self.root)
        with closing(sqlite3.connect(self.path)) as db, db:
            count = db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        with self.assertRaises(MergeConflict) as raised:
            self.store.merge("main", "scenario", left)
        self.assertEqual(raised.exception.paths, ["/messages"])
        self.assertEqual(self.store.branches(), {"main": left, "scenario": right})
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM commits").fetchone()[0], count)

    def test_delete_versus_modify_conflict_and_null_are_distinct(self):
        self.store.branch("scenario", self.root)
        left_state = {key: value for key, value in self.initial.items() if key != "beliefs"}
        left = self.store.commit("main", left_state, "Delete", self.root)
        self.store.commit("scenario", {**self.initial, "beliefs": None}, "Null", self.root)
        with self.assertRaises(MergeConflict) as raised:
            self.store.merge("main", "scenario", left)
        self.assertEqual(raised.exception.paths, ["/beliefs"])

    def test_conflicting_nested_scalars_report_all_paths(self):
        self.store.branch("scenario", self.root)
        left = self.store.commit("main", {"beliefs": {"rain": 0.8}, "messages": [1]}, "A", self.root)
        self.store.commit("scenario", {"beliefs": {"rain": 0.9}, "messages": [2]}, "B", self.root)
        with self.assertRaises(MergeConflict) as raised:
            self.store.merge("main", "scenario", left)
        self.assertEqual(raised.exception.paths, ["/beliefs/rain", "/messages"])

    def test_stale_writes_restore_and_merge_are_rejected(self):
        self.store.branch("scenario", self.root)
        current = self.store.commit("main", {"x": 1}, "Advance", self.root)
        for action in (
            lambda: self.store.commit("main", {"x": 2}, "Stale", self.root),
            lambda: self.store.restore("main", self.root, self.root),
            lambda: self.store.merge("main", "scenario", self.root),
        ):
            with self.assertRaises(ConflictError):
                action()
        self.assertEqual(self.store.head("main"), current)

    def test_separate_connections_cannot_lose_an_update(self):
        second = Store(self.path)
        barrier = threading.Barrier(2)
        outcomes = []

        def writer(store, value):
            barrier.wait()
            try:
                outcomes.append(store.commit("main", {"value": value}, "Concurrent", self.root))
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=writer, args=(store, value))
                   for store, value in ((self.store, 1), (second, 2))]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes.count("conflict"), 1)
            self.assertEqual(len(self.store.history("main")), 2)
        finally:
            second.close()

    def test_invalid_inputs_do_not_modify_state(self):
        cycle = {}
        cycle["self"] = cycle
        for value in ([], {"x": float("nan")}, {"x": float("inf")}, {1: "key"},
                      {"x": object()}, {"x": (1, 2)}, cycle, {"x": "a" * 1_048_576},
                      {"x": "\ud800"}):
            with self.subTest(value_type=type(value)):
                with self.assertRaises(StoreError):
                    self.store.commit("main", value, "Invalid", self.root)
        for ref in (None, 5, [], "unknown", "' OR 1=1 --", "\ud800"):
            with self.assertRaises(StoreError):
                self.store.read(ref)
        for name in ("", "../escape", "a//b", "bad name", "x" * 65, "0" * 64,
                     "a;DROP TABLE branches", None):
            with self.assertRaises(StoreError):
                self.store.branch(name, self.root)
        self.assertEqual(self.store.head("main"), self.root)
        self.assertEqual(len(self.store.history("main")), 1)

    def test_depth_and_invalid_messages_are_bounded(self):
        state = {}
        for _ in range(66):
            state = {"deeper": state}
        with self.assertRaisesRegex(StoreError, "nesting"):
            self.store.commit("main", state, "Too deep", self.root)
        for message in (None, "", " ", "x" * 4097, "\ud800"):
            with self.assertRaises(StoreError):
                self.store.commit("main", {}, message, self.root)
        self.assertEqual(self.store.head("main"), self.root)

    def test_duplicate_branch_unknown_parent_and_bad_limits(self):
        with self.assertRaises(ConflictError):
            self.store.branch("main", self.root)
        with self.assertRaises(StoreError):
            self.store.branch("new", "f" * 64)
        for limit in (0, -1, 1001, True, 1.5):
            with self.assertRaises(StoreError):
                self.store.history("main", limit)
        self.assertEqual(self.store.branches(), {"main": self.root})

    def test_corrupt_commit_is_detected(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TRIGGER commits_no_update")
            db.execute("UPDATE commits SET state = ? WHERE id = ?", ('{"bad":true}', self.root))
        with self.assertRaisesRegex(StoreError, "Corrupt commit"):
            self.store.read("main")
        with self.assertRaisesRegex(StoreError, "Corrupt commit"):
            self.store.commit("main", {}, "Should not append", self.root)

    def test_in_memory_and_closed_store(self):
        with Store(":memory:") as memory:
            root = memory.initialize({})
            self.assertEqual(memory.read(root)["state"], {})
        with self.assertRaises(StoreError):
            memory.read(root)


if __name__ == "__main__":
    unittest.main()
