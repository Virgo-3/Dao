"""Storage invariants: branch isolation, merge safety, CAS, and atomic history."""

import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from dao.store import ConflictError, NotFoundError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def write(self, branch="main", **memory):
        head = self.store.head(branch)
        state = self.store.snapshot(head)
        state["memory"].update(memory)
        return self.store.commit(branch, state, "change memory", expected_head=head)

    def test_branch_and_returned_snapshot_are_isolated(self):
        genesis = self.store.head()
        self.store.branch("experiment")
        branch_commit = self.write("experiment", preference="tea")
        self.assertEqual(self.store.head(), genesis)
        self.assertNotEqual(genesis, branch_commit)
        self.assertEqual(self.store.snapshot()["memory"], {})
        snapshot = self.store.snapshot("experiment")
        snapshot["memory"]["preference"] = "coffee"
        self.assertEqual(self.store.snapshot("experiment")["memory"]["preference"], "tea")

    def test_disjoint_divergent_merge_preserves_both_changes_and_parents(self):
        self.store.branch("experiment")
        ours = self.write(name="Alex")
        theirs = self.write("experiment", preference="tea")
        merged = self.store.merge("main", "experiment", expected_head=ours)
        self.assertEqual(self.store.snapshot()["memory"], {"name": "Alex", "preference": "tea"})
        self.assertEqual(self.store.log()[0]["parents"], [ours, theirs])
        self.assertEqual(self.store.head("experiment"), theirs)
        self.assertEqual(len(self.store.log()), 4)
        self.assertTrue(self.store.verify_integrity()["ok"])
        self.assertNotEqual(merged, ours)

    def test_same_field_conflict_leaves_branch_and_history_unchanged(self):
        self.store.branch("experiment")
        ours = self.write(preference="tea")
        self.write("experiment", preference="coffee")
        before = self.store.verify_integrity()["commits"]
        with self.assertRaisesRegex(ConflictError, "/memory/preference"):
            self.store.merge("main", "experiment", expected_head=ours)
        self.assertEqual(self.store.head(), ours)
        self.assertEqual(self.store.verify_integrity()["commits"], before)

    def test_delete_modify_conflict_and_json_scalar_type_conflict(self):
        original = self.write(value=True)
        self.store.branch("experiment")
        state = self.store.snapshot()
        del state["memory"]["value"]
        ours = self.store.commit("main", state, "delete", original)
        self.write("experiment", value=1)
        with self.assertRaises(ConflictError):
            self.store.merge("main", "experiment", ours)
        self.assertNotIn("value", self.store.snapshot()["memory"])

    def test_crisscross_history_requires_adjudication_of_multiple_merge_bases(self):
        self.store.branch("experiment")
        left = self.write(left=True)
        right = self.write("experiment", right=True)
        self.store.branch("left_old", left)
        self.store.branch("right_old", right)
        ours = self.store.merge("main", "right_old", left)
        self.store.merge("experiment", "left_old", right)
        with self.assertRaisesRegex(ConflictError, "Multiple merge bases"):
            self.store.merge("main", "experiment", ours)
        self.assertEqual(self.store.head(), ours)

    def test_revert_restores_snapshot_but_keeps_intervening_history(self):
        genesis = self.store.head()
        changed = self.write(preference="tea")
        restored = self.store.revert("main", genesis, expected_head=changed)
        self.assertEqual(self.store.snapshot(restored), self.store.snapshot(genesis))
        self.assertNotEqual(restored, genesis)
        self.assertEqual([entry["id"] for entry in self.store.log()], [restored, changed, genesis])

    def test_cross_connection_stale_writer_cannot_overwrite(self):
        with Store(self.path) as other:
            observed = other.head()
            updated = self.write(preference="tea")
            state = other.snapshot(observed)
            state["memory"]["preference"] = "coffee"
            with self.assertRaises(ConflictError):
                other.commit("main", state, "stale change", expected_head=observed)
            self.assertEqual(other.head(), updated)
            self.assertEqual(other.snapshot()["memory"]["preference"], "tea")

    def test_nested_transaction_rollback_undoes_commit_reference_and_event(self):
        original = self.store.head()
        original_events = self.store.events()
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.transaction():
                self.write(preference="tea")
                self.store.append_event("decision.executed", {"id": "d1"})
                raise RuntimeError("abort")
        self.assertEqual(self.store.head(), original)
        self.assertEqual(len(self.store.log()), 1)
        self.assertEqual(self.store.events(), original_events)
        self.assertTrue(self.store.verify_integrity()["ok"])

    def test_inner_failure_can_be_caught_without_losing_outer_work(self):
        with self.store.transaction():
            first = self.store.append_event("outer", {"step": 1})
            try:
                with self.store.transaction():
                    self.store.append_event("inner", {"step": 2})
                    raise RuntimeError("abort inner")
            except RuntimeError:
                pass
            last = self.store.append_event("outer", {"step": 3})
        self.assertEqual([event["seq"] for event in self.store.events()], [1, 2, 3])
        self.assertEqual(last["previous_hash"], first["hash"])
        self.assertTrue(self.store.verify_integrity()["ok"])

    def test_append_only_triggers_and_hash_tampering_detection(self):
        commit_id = self.write(preference="tea")
        self.store.append_event("proposal", {"id": "p1"})
        self.store.append_event("adjudication", {"approved": True})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM commits WHERE id = ?", (commit_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE events SET kind = 'changed' WHERE seq = 1")
        self.store.db.execute("DROP TRIGGER commits_no_update")
        self.store.db.execute("UPDATE commits SET message = 'tampered' WHERE id = ?", (commit_id,))
        self.store.db.execute("DROP TRIGGER events_no_update")
        self.store.db.execute("UPDATE events SET payload = '{}' WHERE seq = 1")
        report = self.store.verify_integrity()
        self.assertFalse(report["ok"])
        self.assertTrue(any("Commit hash mismatch" in issue for issue in report["issues"]))
        self.assertTrue(any("Event hash mismatch" in issue for issue in report["issues"]))

    def test_invalid_state_names_and_limits_do_not_write(self):
        original = self.store.head()
        for state in (
            {"memory": {"x": float("nan")}},
            {"memory": {"x": float("inf")}},
            {"memory": {1: "value"}},
            {"messages": {}},
            {"memory": []},
        ):
            with self.assertRaises(ValueError):
                self.store.commit("main", state, "invalid", original)
        for name in ("", "../main", "a//b", "/bad", "a.", "x" * 129):
            with self.assertRaises(ValueError):
                self.store.branch(name)
        for limit in (0, -1, True, 1.5, 10_001):
            with self.assertRaises(ValueError):
                self.store.events(limit)
            with self.assertRaises(ValueError):
                self.store.log(limit=limit)
        with self.assertRaises(NotFoundError):
            self.store.snapshot("unknown")
        self.assertEqual(self.store.head(), original)

    def test_diff_distinguishes_add_remove_and_escaped_paths(self):
        original = self.write(**{"a/b": "old", "delete": None})
        state = self.store.snapshot()
        state["memory"]["a/b"] = "new"
        del state["memory"]["delete"]
        state["memory"]["added"] = None
        changed = self.store.commit("main", state, "modify", original)
        changes = {item["path"]: item for item in self.store.diff(original, changed)}
        self.assertEqual(changes["/memory/a~1b"]["before"], "old")
        self.assertEqual(changes["/memory/a~1b"]["after"], "new")
        self.assertEqual(changes["/memory/delete"]["op"], "remove")
        self.assertEqual(changes["/memory/added"]["op"], "add")

    def test_genesis_reference_is_journaled_once_across_reopen(self):
        genesis = self.store.head()
        events = self.store.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "branch.created")
        self.assertEqual(
            events[0]["payload"],
            {"branch": "main", "before": None, "head": genesis, "from_ref": None},
        )
        with Store(self.path) as reopened:
            self.assertEqual(reopened.events(), events)
            self.assertTrue(reopened.verify_integrity()["ok"])

    def test_all_reference_operations_are_projected_from_their_events(self):
        genesis = self.store.head()
        self.store.branch("experiment")
        ours = self.write(name="Alex")
        theirs = self.write("experiment", preference="tea")
        merged = self.store.merge("main", "experiment", ours)
        restored = self.store.revert("main", genesis, merged)
        # Application events can mention a head without mutating its projected reference.
        self.store.append_event("action.adjudicated", {"branch": "main", "head": genesis})
        events = self.store.events()
        self.assertEqual(
            [event["kind"] for event in events],
            [
                "branch.created",
                "branch.created",
                "state.committed",
                "state.committed",
                "state.merged",
                "state.restored",
                "action.adjudicated",
            ],
        )
        projection = {}
        for event in events[:-1]:
            payload = event["payload"]
            self.assertEqual(projection.get(payload["branch"]), payload["before"])
            projection[payload["branch"]] = payload["head"]
        self.assertEqual(projection, self.store.branches())
        self.assertEqual(events[4]["payload"]["source_head"], theirs)
        self.assertEqual(events[4]["payload"]["head"], merged)
        self.assertEqual(events[5]["payload"]["restore_from"], genesis)
        self.assertEqual(events[5]["payload"]["head"], restored)
        self.assertTrue(self.store.verify_integrity()["ok"])

    def test_reference_rewind_to_valid_old_commit_fails_integrity(self):
        genesis = self.store.head()
        current = self.write(preference="tea")
        self.store.db.execute("UPDATE branches SET head = ? WHERE name = 'main'", (genesis,))
        report = self.store.verify_integrity()
        self.assertFalse(report["ok"])
        self.assertIn("Branch reference mismatch: main", report["issues"])
        self.store.db.execute("UPDATE branches SET head = ? WHERE name = 'main'", (current,))
        self.assertTrue(self.store.verify_integrity()["ok"])

    def test_deleted_and_unjournaled_refs_fail_integrity(self):
        head = self.store.branch("experiment")
        self.store.db.execute("DELETE FROM branches WHERE name = 'experiment'")
        self.assertIn(
            "Branch reference mismatch: experiment", self.store.verify_integrity()["issues"]
        )
        self.store.db.execute("INSERT INTO branches VALUES ('experiment', ?)", (head,))
        self.assertTrue(self.store.verify_integrity()["ok"])
        self.store.db.execute("INSERT INTO branches VALUES ('unjournaled', ?)", (head,))
        self.assertIn(
            "Branch reference mismatch: unjournaled", self.store.verify_integrity()["issues"]
        )

    def test_public_event_append_cannot_forge_reference_transitions(self):
        original_events = self.store.events()
        for kind in ("branch.created", "state.committed", "state.merged", "state.restored"):
            with self.assertRaisesRegex(ValueError, "reserved"):
                self.store.append_event(
                    kind, {"branch": "main", "before": self.store.head(), "head": self.store.head()}
                )
        self.assertEqual(self.store.events(), original_events)

    def test_semantically_invalid_transition_is_detected_even_with_valid_hash(self):
        genesis = self.store.head()
        current = self.write(preference="tea")
        # Simulate a forged but cryptographically consistent append, outside the public API.
        self.store._append_event(
            "state.committed", {"branch": "main", "before": current, "head": genesis}
        )
        report = self.store.verify_integrity()
        self.assertFalse(report["ok"])
        self.assertTrue(any("does not match commit parents" in issue for issue in report["issues"]))

    def test_journal_failure_rolls_back_each_reference_mutation(self):
        genesis = self.store.head()
        self.store.branch("experiment")
        ours = self.write(name="Alex")
        self.write("experiment", preference="tea")
        original_refs = self.store.branches()
        original_events = self.store.events()
        original_report = self.store.verify_integrity()
        state = self.store.snapshot()
        state["memory"]["extra"] = True
        operations = [
            lambda: self.store.branch("unpublished"),
            lambda: self.store.commit("main", state, "unpublished", ours),
            lambda: self.store.merge("main", "experiment", ours),
            lambda: self.store.revert("main", genesis, ours),
        ]
        for operation in operations:
            with patch.object(
                self.store, "_append_event", side_effect=RuntimeError("journal failure")
            ):
                with self.assertRaisesRegex(RuntimeError, "journal failure"):
                    operation()
            self.assertEqual(self.store.branches(), original_refs)
            self.assertEqual(self.store.events(), original_events)
            self.assertEqual(self.store.verify_integrity(), original_report)


if __name__ == "__main__":
    unittest.main()
