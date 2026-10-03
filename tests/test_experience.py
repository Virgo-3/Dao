"""Relationship forecasts change per branch while observations remain durable."""

import math
import sqlite3
import tempfile
from pathlib import Path
import unittest

from dao.experience import Experience
from dao.store import ConflictError, Store


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "state.sqlite3")
        self.experience = Experience(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def register(self, prior=None):
        return self.experience.register_relationship(
            "main", "reminders", "agent", "user", "interruption", prior, self.store.head()
        )

    def observe(
        self,
        *,
        likelihoods=None,
        observed_at="2026-09-30T12:00:00+00:00",
        source_event_id=None,
    ):
        return self.experience.record_observation(
            "reminders",
            "user reported fewer interruptions",
            likelihoods or {"enhancing": 0.8, "neutral": 0.4, "degrading": 0.1},
            "user feedback",
            "user-claimed",
            observed_at=observed_at,
            note="Claimed feedback, not authenticated by the store",
            proposal_id="evidence-plan-1",
            source_event_id=source_event_id,
        )

    def test_unassessed_is_distinct_from_neutral_and_branches_diverge(self):
        initial = self.register()
        self.assertEqual(initial["relationship"]["basis"], "unassessed")
        self.assertAlmostEqual(initial["relationship"]["belief"]["neutral"], 1 / 3)
        self.store.branch("experiment")
        observation = self.observe()
        applied = self.experience.apply_observation(
            "experiment", observation["id"], self.store.head("experiment")
        )
        self.assertTrue(applied["applied"])
        self.assertEqual(self.store.head("main"), initial["head"])
        self.assertEqual(self.experience.relationship("main", "reminders"), initial["relationship"])
        self.assertEqual(applied["relationship"]["evidence_ids"], [observation["id"]])
        self.assertEqual(applied["relationship"]["basis"], "observation")
        self.assertGreater(applied["relationship"]["belief"]["enhancing"], 1 / 3)
        self.assertEqual(self.experience.get_observation(observation["id"]), observation)
        self.assertTrue(self.experience.verify_integrity()["ok"])

    def test_bayes_update_and_likelihoods_are_conditional_not_distribution(self):
        self.register({"enhancing": 0.6, "neutral": 0.3, "degrading": 0.1})
        observation = self.observe(
            likelihoods={"enhancing": 0.8, "neutral": 0.5, "degrading": 0.1}
        )
        belief = self.experience.apply_observation(
            "main", observation["id"], self.store.head()
        )["relationship"]["belief"]
        self.assertAlmostEqual(belief["enhancing"], 0.75)
        self.assertAlmostEqual(belief["neutral"], 0.234375)
        self.assertAlmostEqual(belief["degrading"], 0.015625)
        self.assertAlmostEqual(sum(belief.values()), 1.0)

    def test_positive_marginal_under_float_range_can_condition_and_replay(self):
        initial = self.register({"enhancing": 1e-300, "neutral": 1, "degrading": 0})
        observation = self.observe(
            likelihoods={"enhancing": 1e-30, "neutral": 0, "degrading": 0}
        )
        applied = self.experience.apply_observation("main", observation["id"], initial["head"])
        self.assertEqual(
            applied["relationship"]["belief"], {"enhancing": 1, "neutral": 0, "degrading": 0}
        )
        event_count = len(self.store.events())
        repeated = self.experience.apply_observation("main", observation["id"], initial["head"])
        self.assertFalse(repeated["applied"])
        self.assertEqual(repeated["head"], applied["head"])
        self.assertEqual(repeated["relationship"], applied["relationship"])
        self.assertEqual(len(self.store.events()), event_count)
        self.assertTrue(self.experience.verify_integrity()["ok"])

    def test_subnormal_uninformative_likelihood_preserves_prior(self):
        initial = self.register({"enhancing": 0.1, "neutral": 0.2, "degrading": 0.7})
        smallest = math.ulp(0.0)
        observation = self.observe(
            likelihoods={state: smallest for state in ("enhancing", "neutral", "degrading")}
        )
        applied = self.experience.apply_observation("main", observation["id"], initial["head"])
        self.assertEqual(applied["relationship"]["belief"], initial["relationship"]["belief"])
        self.assertEqual(applied["relationship"]["evidence_ids"], [observation["id"]])

    def test_subnormal_uninformative_likelihood_accepts_uniform_prior(self):
        initial = self.register()
        smallest = math.ulp(0.0)
        observation = self.observe(
            likelihoods={state: smallest for state in ("enhancing", "neutral", "degrading")}
        )
        applied = self.experience.apply_observation("main", observation["id"], initial["head"])
        self.assertEqual(applied["relationship"]["belief"], initial["relationship"]["belief"])

    def test_smallest_positive_prior_support_survives_registration(self):
        smallest = math.ulp(0.0)
        relationship = self.register({"enhancing": smallest, "neutral": 1, "degrading": 0})[
            "relationship"
        ]
        self.assertEqual(relationship["belief"]["enhancing"], smallest)
        self.assertEqual(relationship["belief"]["degrading"], 0)
        self.assertEqual(math.fsum(relationship["belief"].values()), 1)

    def test_unrepresentable_positive_posterior_is_rejected_atomically(self):
        initial = self.register({"enhancing": 1e-300, "neutral": 1, "degrading": 0})
        observation = self.observe(
            likelihoods={"enhancing": 1e-30, "neutral": 1, "degrading": 0}
        )
        event_count = len(self.store.events())
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "numeric range"):
                self.experience.apply_observation("main", observation["id"], initial["head"])
            self.assertEqual(self.store.head(), initial["head"])
            self.assertEqual(self.experience.relationship("main", "reminders"), initial["relationship"])
            self.assertEqual(self.experience.get_observation(observation["id"]), observation)
            self.assertEqual(len(self.store.events()), event_count)
        self.assertTrue(self.experience.verify_integrity()["ok"])

    def test_exact_record_retry_and_duplicate_apply_are_idempotent(self):
        head = self.register()["head"]
        first = self.observe()
        second = self.observe()
        self.assertEqual(first, second)
        self.assertEqual(self.experience.list_observations(), [first])
        self.assertEqual(
            [event["kind"] for event in self.store.events()].count("experience.observed"), 1
        )
        applied = self.experience.apply_observation("main", first["id"], head)
        event_count = len(self.store.events())
        repeated = self.experience.apply_observation("main", first["id"], head)
        self.assertFalse(repeated["applied"])
        self.assertEqual(repeated["head"], applied["head"])
        self.assertEqual(repeated["relationship"]["evidence_ids"], [first["id"]])
        self.assertEqual(len(self.store.events()), event_count)

    def test_source_event_id_deduplicates_retry_without_observed_time(self):
        self.register()
        first = self.observe(observed_at=None, source_event_id="survey-response-42")
        repeated = self.observe(observed_at=None, source_event_id="survey-response-42")
        self.assertEqual(repeated, first)
        self.assertEqual(
            self.experience.find_observation("user feedback", "survey-response-42"), first
        )
        self.assertIsNone(self.experience.find_observation("user feedback", "unknown"))
        self.assertEqual(len(self.experience.list_observations()), 1)
        with self.assertRaisesRegex(ConflictError, "different content"):
            self.observe(
                observed_at=None,
                source_event_id="survey-response-42",
                likelihoods={"enhancing": 0.9, "neutral": 0.4, "degrading": 0.1},
            )
        self.assertEqual(len(self.experience.list_observations()), 1)
        self.assertTrue(self.experience.verify_integrity()["ok"])
        with Store(self.store.path) as reopened:
            reports = Experience(reopened)
            self.assertEqual(
                reports.find_observation("user feedback", "survey-response-42"), first
            )
            self.assertEqual(reports.get_observation(first["id"]), first)
            self.assertTrue(reports.verify_integrity()["ok"])

    def test_impossible_observation_is_retained_without_changing_branch(self):
        initial = self.register({"enhancing": 1, "neutral": 0, "degrading": 0})
        observation = self.observe(
            likelihoods={"enhancing": 0, "neutral": 0.7, "degrading": 0.5}
        )
        with self.assertRaisesRegex(ValueError, "zero probability"):
            self.experience.apply_observation("main", observation["id"], initial["head"])
        self.assertEqual(self.store.head(), initial["head"])
        self.assertEqual(self.experience.get_observation(observation["id"]), observation)
        self.assertTrue(self.experience.verify_integrity()["ok"])

    def test_stale_head_preserves_observation_for_later_application(self):
        initial = self.register()
        observation = self.observe()
        state = self.store.snapshot()
        state["memory"]["unrelated"] = True
        new_head = self.store.commit("main", state, "Concurrent change", initial["head"])
        with self.assertRaises(ConflictError):
            self.experience.apply_observation("main", observation["id"], initial["head"])
        self.assertEqual(self.store.head(), new_head)
        self.assertEqual(self.experience.list_observations(), [observation])
        applied = self.experience.apply_observation("main", observation["id"], new_head)
        self.assertTrue(applied["applied"])
        self.assertTrue(self.experience.verify_integrity()["ok"])

    def test_rejects_invalid_probabilities_and_naive_observation_times(self):
        head = self.store.head()
        for invalid in (
            {"enhancing": 1, "neutral": 0},
            {"enhancing": float("nan"), "neutral": 0, "degrading": 0},
            {"enhancing": True, "neutral": 0, "degrading": 0},
            {"enhancing": 0.5, "neutral": 0.1, "degrading": 0.1},
        ):
            with self.assertRaises(ValueError):
                self.experience.register_relationship(
                    "main", "bad", "agent", "user", "interruption", invalid, head
                )
        self.assertEqual(self.store.head(), head)
        for invalid in (
            {"enhancing": 0, "neutral": 0, "degrading": 0},
            {"enhancing": -0.1, "neutral": 0.3, "degrading": 0.2},
        ):
            with self.assertRaises(ValueError):
                self.observe(likelihoods=invalid)
        with self.assertRaises(ValueError):
            self.observe(observed_at="2026-09-30T12:00:00")
        self.assertEqual(self.experience.list_observations(), [])

    def test_observation_records_are_append_only_and_integrity_checked(self):
        self.register()
        observation = self.observe()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute(
                "UPDATE observations SET payload = '{}' WHERE id = ?", (observation["id"],)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM observations WHERE id = ?", (observation["id"],))
        self.assertTrue(self.experience.verify_integrity()["ok"])
        self.store.db.execute("DROP TRIGGER immutable_observations_UPDATE")
        self.assertIn(
            "Missing observation append-only trigger: immutable_observations_UPDATE",
            self.experience.verify_integrity()["issues"],
        )
        self.store.db.execute(
            "UPDATE observations SET payload = '{}' WHERE id = ?", (observation["id"],)
        )
        self.assertIn(
            "Observation projection differs from event chain",
            self.experience.verify_integrity()["issues"],
        )


if __name__ == "__main__":
    unittest.main()
