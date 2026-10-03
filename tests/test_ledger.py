"""Accounting tests exercise crash ambiguity, concurrency, and immutable spending."""

from decimal import Decimal, Inexact, Overflow, Rounded, Underflow, localcontext
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from dao.ledger import BudgetExceeded, Ledger
from dao.store import Store


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "dao.sqlite3"
        self.store = Store(self.path)
        self.ledger = Ledger(self.store, "10")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_reservation_budget_and_idempotence(self):
        first = self.ledger.reserve("r1", "main", "6", {"turn": 1})
        events = len(self.store.events())
        self.assertEqual(first, self.ledger.reserve("r1", "main", 6, {"turn": 1}))
        self.assertEqual(len(self.store.events()), events)
        for branch, cost, metadata in [
            ("other", "6", {"turn": 1}),
            ("main", "5", {"turn": 1}),
            ("main", "6", {"turn": 2}),
        ]:
            with self.assertRaises(ValueError):
                self.ledger.reserve("r1", branch, cost, metadata)
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("r2", "main", "4.000001")
        self.assertEqual(self.ledger.summary()["reserved_usd"], "6.000000")
        self.assertEqual(self.ledger.summary()["remaining_usd"], "4.000000")
        self.assertEqual(len(self.store.events()), events)

    def test_settlement_is_append_only_and_preserves_actual_overrun(self):
        self.ledger.reserve("r1", "main", "1")
        entry = self.ledger.settle("r1", 14, 27, "12.0000001", "model", {"provider": "bill-1"})
        self.assertEqual(entry["cost_usd"], "12.000001")
        events = len(self.store.events())
        self.assertEqual(
            entry, self.ledger.settle("r1", 14, 27, "12.000001", "model", {"provider": "bill-1"})
        )
        self.assertEqual(len(self.store.events()), events)
        with self.assertRaises(ValueError):
            self.ledger.settle("r1", 15, 27, "12.000001", "model", {"provider": "bill-1"})
        summary = self.ledger.summary()
        self.assertEqual(summary["spent_usd"], "12.000001")
        self.assertEqual(summary["reserved_usd"], "0.000000")
        self.assertEqual(summary["remaining_usd"], "0.000000")
        self.assertEqual(summary["over_budget_usd"], "2.000001")
        self.assertTrue(summary["over_budget"])
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("blocked", "main", "0")
        for statement in ("UPDATE ledger_usage SET cost_micros = 0", "DELETE FROM ledger_usage"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(statement)
        self.assertTrue(self.store.verify_integrity()["ok"])

    def test_unknown_retains_reservation_until_audited_resolution(self):
        self.ledger.reserve("timeout", "main", "7")
        unknown = self.ledger.mark_unknown("timeout", "provider timed out after send")
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(
            unknown, self.ledger.mark_unknown("timeout", "provider timed out after send")
        )
        with self.assertRaises(ValueError):
            self.ledger.mark_unknown("timeout", "different")
        with self.assertRaises(ValueError):
            self.ledger.release("timeout", "assume unbilled")
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("another", "main", "4")
        self.assertEqual(self.ledger.summary()["unknown_count"], 1)
        result = self.ledger.resolve_unknown("timeout", 5, 6, "2", "model")
        self.assertEqual(result, self.ledger.resolve_unknown("timeout", 5, 6, "2", "model"))
        self.assertEqual(self.ledger.summary()["remaining_usd"], "8.000000")
        self.assertEqual(self.ledger.summary()["unknown_count"], 0)
        self.assertEqual(self.ledger.reservations()[0]["status"], "settled")
        kinds = [
            event["kind"] for event in self.store.events() if event["kind"].startswith("usage.")
        ]
        self.assertEqual(
            kinds, ["usage.budget_created", "usage.reserved", "usage.unknown", "usage.settled"]
        )

    def test_release_is_explicit_and_only_pending(self):
        self.ledger.reserve("not-sent", "main", "4")
        released = self.ledger.release("not-sent", "cancelled before provider call")
        self.assertEqual(
            released, self.ledger.release("not-sent", "cancelled before provider call")
        )
        self.assertEqual(self.ledger.summary()["remaining_usd"], "10.000000")
        with self.assertRaises(ValueError):
            self.ledger.release("not-sent", "another reason")
        with self.assertRaises(ValueError):
            self.ledger.settle("not-sent", 0, 0, "1", "model")
        with self.assertRaises(ValueError):
            self.ledger.mark_unknown("not-sent", "timeout")

    def test_branch_revert_and_reopen_cannot_erase_physical_spend(self):
        genesis = self.store.head()
        self.store.branch("experiment")
        head = self.store.commit("experiment", {"memory": {"trial": True}}, "trial", genesis)
        self.ledger.reserve("exp-call", "experiment", "3")
        self.ledger.settle("exp-call", 10, 5, "2", "model")
        self.store.revert("experiment", genesis, head)
        self.ledger.reserve("main-call", "main", "2")
        self.ledger.mark_unknown("main-call", "connection interrupted")
        self.assertEqual(self.ledger.summary()["remaining_usd"], "6.000000")
        with Store(self.path) as other:
            ledger = Ledger(other)
            self.assertEqual(ledger.summary(), self.ledger.summary())
            self.assertEqual(ledger.entries()[0]["branch"], "experiment")
            with self.assertRaises(ValueError):
                Ledger(other, "20")
            ledger.set_budget("11")
        self.assertEqual(self.ledger.summary()["budget_usd"], "11.000000")
        self.assertEqual(self.ledger.summary()["spent_usd"], "2.000000")
        self.assertEqual(self.ledger.summary()["reserved_usd"], "2.000000")
        self.assertTrue(self.store.verify_integrity()["ok"])

    def test_concurrent_connections_cannot_oversubscribe(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def attempt(number):
            try:
                with Store(self.path) as connection:
                    ledger = Ledger(connection)
                    barrier.wait(timeout=10)
                    try:
                        ledger.reserve(f"parallel-{number}", "main", "6")
                        outcome = "reserved"
                    except BudgetExceeded:
                        outcome = "blocked"
                with lock:
                    results.append(outcome)
            except BaseException as exc:
                with lock:
                    results.append(exc)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(results, ["reserved", "blocked"])
        self.assertEqual(self.ledger.summary()["reserved_usd"], "6.000000")
        self.assertEqual(self.ledger.summary()["pending_count"], 1)

    def test_decimal_accounting_rounds_up_independent_of_context(self):
        self.ledger.set_budget("0.000004")
        with localcontext() as context:
            context.prec = 2
            self.ledger.reserve("tiny", "main", Decimal("1e-10000000"))
            self.ledger.reserve(
                "boundary", "main", Decimal("0.000001000000000000000000000000000000000000001")
            )
        self.assertEqual(self.ledger.summary()["reserved_usd"], "0.000003")
        self.ledger.settle("tiny", 0, 0, "0.00000001", "model")
        self.assertEqual(self.ledger.summary()["spent_usd"], "0.000001")
        self.assertEqual(self.ledger.summary()["remaining_usd"], "0.000001")
        with localcontext() as context:
            context.prec = 2
            self.ledger.set_budget("9223372036854.775807")
        self.assertEqual(self.ledger.summary()["budget_usd"], "9223372036854.775807")

    def test_invalid_amounts_tokens_and_parameters_are_rejected(self):
        for amount in ("NaN", "Infinity", "-1", True, None, "invalid", "1e999"):
            with self.assertRaises(ValueError):
                self.ledger.reserve("bad", "main", amount)
        self.ledger.reserve("valid", "main", "1")
        for tokens in (True, -1, 1.5, "1", 1 << 63):
            with self.assertRaises(ValueError):
                self.ledger.settle("valid", tokens, 0, "0", "model")
        with self.assertRaises(ValueError):
            self.ledger.reserve("metadata", "main", "1", {"nan": float("nan")})
        with self.assertRaises(ValueError):
            self.ledger.reserve("metadata", "main", "1", {"nested": {1: "not a JSON key"}})
        for limit in (True, 0, -1, 10001, "1"):
            with self.assertRaises(ValueError):
                self.ledger.entries(limit)
        self.assertEqual(self.ledger.summary()["usage_count"], 0)

    def test_decimal_accounting_ignores_exponent_limits_and_rounding_traps(self):
        with localcontext() as context:
            context.prec = 2
            context.Emax = 5
            context.Emin = -5
            for signal in (Inexact, Rounded, Overflow, Underflow):
                context.traps[signal] = True
            self.ledger.set_budget("9223372036854.775807")
            self.ledger.reserve("whole", "main", "1")
            self.ledger.reserve("fraction", "main", "1.000000000000000000001")
            self.ledger.reserve("zero", "main", "0e10000000")
            self.assertEqual(self.ledger.summary()["reserved_usd"], "2.000001")
            self.ledger.settle("whole", 1, 0, "1e-10000000", "model")
            self.assertEqual(self.ledger.summary()["spent_usd"], "0.000001")
            self.assertEqual(self.ledger.summary()["reserved_usd"], "1.000001")

    def test_long_decimal_tail_and_maximum_rejection_are_atomic(self):
        self.ledger.reserve("long-tail", "main", "0.000001" + "0" * 5000 + "1")
        self.assertEqual(self.ledger.summary()["reserved_usd"], "0.000002")
        before = self.ledger.summary()
        with localcontext() as context:
            context.prec = 2
            context.Emax = 5
            for amount in ("9223372036854.775807000000000001", "-1e-10000000"):
                with self.assertRaises(ValueError):
                    self.ledger.reserve("invalid", "main", amount)
                with self.assertRaises(ValueError):
                    self.ledger.settle("long-tail", 1, 0, amount, "model")
                self.assertEqual(self.ledger.summary(), before)

    def test_audit_failure_rolls_back_money_transition(self):
        original = self.store.append_event

        def fail(*args, **kwargs):
            raise RuntimeError("journal unavailable")

        self.store.append_event = fail
        try:
            with self.assertRaises(RuntimeError):
                self.ledger.reserve("rollback", "main", "5")
        finally:
            self.store.append_event = original
        self.assertEqual(self.ledger.summary()["reserved_usd"], "0.000000")
        self.assertEqual(self.ledger.reservations(), [])
        self.ledger.reserve("rollback-settle", "main", "5")
        self.store.append_event = fail
        try:
            with self.assertRaises(RuntimeError):
                self.ledger.settle("rollback-settle", 1, 2, "3", "model")
        finally:
            self.store.append_event = original
        self.assertEqual(self.ledger.summary()["reserved_usd"], "5.000000")
        self.assertEqual(self.ledger.summary()["spent_usd"], "0.000000")
        self.assertEqual(self.ledger.entries(), [])


if __name__ == "__main__":
    unittest.main()
