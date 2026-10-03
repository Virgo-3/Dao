"""Durable, branch-attributed accounting for irreversible provider spending.

State revisions can be reverted. Money already spent and uncertain billable
attempts cannot, so this ledger belongs to the store rather than a state branch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any

from .store import canonical_json


_SCALE = 1_000_000
_MAX_MICROS = (1 << 63) - 1
_MAX_USD = Decimal("9223372036854.775807")


class BudgetExceeded(ValueError):
    """A new reservation would exceed the remaining global budget."""


def _micros(value: Any, name: str = "amount_usd") -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} must be a nonnegative, finite USD amount")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"{name} must be a nonnegative, finite USD amount")
        if amount > _MAX_USD:
            raise ValueError(f"{name} is too large")
        if not amount:
            return 0
        # Shift the finite decimal's point using integers. Decimal multiplication
        # inherits the caller's precision, exponent limits, and traps.
        _, digits, exponent = amount.as_tuple()
        whole_digits = len(digits) + exponent + 6
        if whole_digits <= 0:
            return 1
        result = 0
        for digit in digits[:whole_digits]:
            result = result * 10 + digit
        if whole_digits > len(digits):
            result *= 10 ** (whole_digits - len(digits))
        elif any(digits[whole_digits:]):
            result += 1
        if result > _MAX_MICROS:
            raise ValueError(f"{name} is too large")
        return result
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError(f"{name} must be a nonnegative, finite USD amount") from exc


def _usd(micros: int) -> str:
    # Integer formatting avoids dependence on the caller's Decimal context.
    sign = "-" if micros < 0 else ""
    whole, fractional = divmod(abs(micros), _SCALE)
    return f"{sign}{whole}.{fractional:06d}"


def _metadata(value: dict[str, Any] | None) -> str:
    if value is None:
        value = {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("metadata must be a JSON object with string keys")
    try:
        return canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must contain only finite JSON values") from exc


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _tokens(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_MICROS:
        raise ValueError(f"{name} must be a nonnegative integer within SQLite's range")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Account once globally, while retaining branch and request attribution.

    Amounts are converted to integer microUSD, rounding upward independently.
    ``budget_usd=None`` preserves an existing limit or initializes a $10 limit.
    Supplying a different limit on reopen is rejected; use ``set_budget`` to
    make an explicit, audited change. All request transitions are atomic.
    """

    def __init__(self, store: Any, budget_usd: Any = None) -> None:
        self.store = store
        requested_budget = None if budget_usd is None else _micros(budget_usd, "budget_usd")
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ledger_budget (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                amount_micros INTEGER NOT NULL CHECK (amount_micros >= 0)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS ledger_reservations (
                request_id TEXT PRIMARY KEY,
                branch TEXT NOT NULL,
                amount_micros INTEGER NOT NULL CHECK (amount_micros >= 0),
                metadata_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'settled', 'unknown', 'released')),
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS ledger_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE REFERENCES ledger_reservations(request_id),
                branch TEXT NOT NULL,
                input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
                output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
                cost_micros INTEGER NOT NULL CHECK (cost_micros >= 0),
                model TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS ledger_usage_no_update
                BEFORE UPDATE ON ledger_usage BEGIN
                SELECT RAISE(ABORT, 'usage entries are append-only'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS ledger_usage_no_delete
                BEFORE DELETE ON ledger_usage BEGIN
                SELECT RAISE(ABORT, 'usage entries are append-only'); END""")
            row = db.execute("SELECT amount_micros FROM ledger_budget WHERE id = 1").fetchone()
            if row is None:
                initial_budget = 10 * _SCALE if requested_budget is None else requested_budget
                db.execute(
                    "INSERT INTO ledger_budget (id, amount_micros) VALUES (1, ?)", (initial_budget,)
                )
                store.append_event("usage.budget_created", {"budget_usd": _usd(initial_budget)})
            elif requested_budget is not None and row["amount_micros"] != requested_budget:
                raise ValueError("budget already exists with a different limit; use set_budget")

    def _totals(self, db: Any) -> tuple[int, int, int]:
        budget = db.execute("SELECT amount_micros FROM ledger_budget WHERE id = 1").fetchone()[0]
        # Python integers keep genuine usage accountably representable even if
        # cumulative lifetime spend eventually exceeds SQLite's integer range.
        spent = sum(row[0] for row in db.execute("SELECT cost_micros FROM ledger_usage"))
        reserved = sum(
            row[0]
            for row in db.execute(
                "SELECT amount_micros FROM ledger_reservations WHERE status IN ('pending', 'unknown')"
            )
        )
        return budget, spent, reserved

    @staticmethod
    def _reservation(row: Any) -> dict[str, Any]:
        return {
            "request_id": row["request_id"],
            "branch": row["branch"],
            "amount_usd": _usd(row["amount_micros"]),
            "status": row["status"],
            "metadata": json.loads(row["metadata_json"]),
            "reason": row["reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _usage(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "request_id": row["request_id"],
            "branch": row["branch"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cost_usd": _usd(row["cost_micros"]),
            "model": row["model"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    def reserve(
        self, request_id: str, branch: str, amount_usd: Any, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Atomically reserve a maximum call cost before starting a provider call."""
        request_id = _text(request_id, "request_id")
        branch = _text(branch, "branch")
        amount = _micros(amount_usd)
        encoded = _metadata(metadata)
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM ledger_reservations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                if (row["branch"], row["amount_micros"], row["metadata_json"]) != (
                    branch,
                    amount,
                    encoded,
                ):
                    raise ValueError("request_id already reserved with different parameters")
                return self._reservation(row)
            budget, spent, reserved = self._totals(db)
            if spent + reserved + amount > budget:
                raise BudgetExceeded(
                    f"reservation exceeds budget: available {_usd(budget - spent - reserved)} USD"
                )
            timestamp = _now()
            db.execute(
                """INSERT INTO ledger_reservations
                (request_id, branch, amount_micros, metadata_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (request_id, branch, amount, encoded, timestamp, timestamp),
            )
            result = self._reservation(
                db.execute(
                    "SELECT * FROM ledger_reservations WHERE request_id = ?", (request_id,)
                ).fetchone()
            )
            self.store.append_event("usage.reserved", result)
            return result

    def settle(
        self,
        request_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Any,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record reported usage, including actual spend above a reservation.

        Unexpected cost must remain visible. It may put the ledger over budget,
        which prevents subsequent reservations until its budget is increased.
        """
        request_id = _text(request_id, "request_id")
        input_tokens = _tokens(input_tokens, "input_tokens")
        output_tokens = _tokens(output_tokens, "output_tokens")
        cost = _micros(cost_usd, "cost_usd")
        model = _text(model, "model")
        encoded = _metadata(metadata)
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT * FROM ledger_usage WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                prior = (
                    existing["input_tokens"],
                    existing["output_tokens"],
                    existing["cost_micros"],
                    existing["model"],
                    existing["metadata_json"],
                )
                if prior != (input_tokens, output_tokens, cost, model, encoded):
                    raise ValueError("request_id already settled with different usage")
                return self._usage(existing)
            reservation = db.execute(
                "SELECT * FROM ledger_reservations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if reservation is None:
                raise ValueError("request_id has no reservation")
            if reservation["status"] not in ("pending", "unknown"):
                raise ValueError("only pending or unknown requests can be settled")
            timestamp = _now()
            db.execute(
                """INSERT INTO ledger_usage
                (request_id, branch, input_tokens, output_tokens, cost_micros, model, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    reservation["branch"],
                    input_tokens,
                    output_tokens,
                    cost,
                    model,
                    encoded,
                    timestamp,
                ),
            )
            db.execute(
                "UPDATE ledger_reservations SET status = 'settled', updated_at = ? WHERE request_id = ?",
                (timestamp, request_id),
            )
            result = self._usage(
                db.execute(
                    "SELECT * FROM ledger_usage WHERE request_id = ?", (request_id,)
                ).fetchone()
            )
            self.store.append_event("usage.settled", result)
            return result

    def mark_unknown(self, request_id: str, reason: str) -> dict[str, Any]:
        """Retain the reservation when an attempted call may have been billed."""
        request_id = _text(request_id, "request_id")
        reason = _text(reason, "reason")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM ledger_reservations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError("request_id has no reservation")
            if row["status"] == "unknown":
                if row["reason"] != reason:
                    raise ValueError("request already marked unknown with a different reason")
                return self._reservation(row)
            if row["status"] != "pending":
                raise ValueError("only pending requests can be marked unknown")
            db.execute(
                "UPDATE ledger_reservations SET status = 'unknown', reason = ?, updated_at = ? WHERE request_id = ?",
                (reason, _now(), request_id),
            )
            result = self._reservation(
                db.execute(
                    "SELECT * FROM ledger_reservations WHERE request_id = ?", (request_id,)
                ).fetchone()
            )
            self.store.append_event("usage.unknown", result)
            return result

    def release(self, request_id: str, reason: str) -> dict[str, Any]:
        """Release only a pending request known not to have a billable attempt.

        Unknown attempts require a settlement (zero cost if confirmed unbilled).
        This method deliberately cannot erase their retained reservation.
        """
        request_id = _text(request_id, "request_id")
        reason = _text(reason, "reason")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM ledger_reservations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError("request_id has no reservation")
            if row["status"] == "released":
                if row["reason"] != reason:
                    raise ValueError("request already released with a different reason")
                return self._reservation(row)
            if row["status"] != "pending":
                raise ValueError("only pending, definitely unbilled requests can be released")
            db.execute(
                "UPDATE ledger_reservations SET status = 'released', reason = ?, updated_at = ? WHERE request_id = ?",
                (reason, _now(), request_id),
            )
            result = self._reservation(
                db.execute(
                    "SELECT * FROM ledger_reservations WHERE request_id = ?", (request_id,)
                ).fetchone()
            )
            self.store.append_event("usage.released", result)
            return result

    def resolve_unknown(
        self,
        request_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Any,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Settle an uncertain attempt after its provider usage is established."""
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT status FROM ledger_reservations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None or row["status"] not in ("unknown", "settled"):
                raise ValueError("request must be unknown or already settled")
            return self.settle(request_id, input_tokens, output_tokens, cost_usd, model, metadata)

    def set_budget(self, amount_usd: Any) -> dict[str, Any]:
        """Change the limit explicitly; existing physical usage is preserved."""
        amount = _micros(amount_usd, "budget_usd")
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT amount_micros FROM ledger_budget WHERE id = 1"
            ).fetchone()[0]
            if amount != previous:
                db.execute("UPDATE ledger_budget SET amount_micros = ? WHERE id = 1", (amount,))
                self.store.append_event(
                    "usage.budget_changed",
                    {
                        "previous_budget_usd": _usd(previous),
                        "budget_usd": _usd(amount),
                    },
                )
            return self.summary()

    def summary(self) -> dict[str, Any]:
        with self.store.transaction() as db:
            budget, spent, reserved = self._totals(db)
            counts = {
                row["status"]: row["count"]
                for row in db.execute(
                    "SELECT status, COUNT(*) AS count FROM ledger_reservations GROUP BY status"
                )
            }
            usage_count = db.execute("SELECT COUNT(*) FROM ledger_usage").fetchone()[0]
            balance = budget - spent - reserved
            return {
                "budget_usd": _usd(budget),
                "spent_usd": _usd(spent),
                "reserved_usd": _usd(reserved),
                "remaining_usd": _usd(max(balance, 0)),
                "over_budget": balance < 0,
                "over_budget_usd": _usd(max(-balance, 0)),
                "usage_count": usage_count,
                "reservation_count": sum(counts.values()),
                "pending_count": counts.get("pending", 0),
                "unknown_count": counts.get("unknown", 0),
                "settled_count": counts.get("settled", 0),
                "released_count": counts.get("released", 0),
            }

    @staticmethod
    def _limit(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000")
        return value

    def entries(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = self._limit(limit)
        with self.store.transaction() as db:
            return [
                self._usage(row)
                for row in db.execute(
                    "SELECT * FROM ledger_usage ORDER BY id DESC LIMIT ?", (limit,)
                )
            ]

    def reservations(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = self._limit(limit)
        with self.store.transaction() as db:
            return [
                self._reservation(row)
                for row in db.execute(
                    "SELECT * FROM ledger_reservations ORDER BY created_at DESC, request_id DESC LIMIT ?",
                    (limit,),
                )
            ]
