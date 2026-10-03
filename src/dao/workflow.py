"""Durable workflow receipts outside Dao's branchable logical state.

Local effects and their receipts share one transaction. External work is claimed
before dispatch and never automatically retried when its outcome is uncertain.
This is replay protection, not an exactly-once guarantee for remote services.
"""

from datetime import datetime, timezone
import json

from .store import ConflictError, NotFoundError, canonical_json


def _now():
    return datetime.now(timezone.utc).isoformat()


def _operation_id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("operation_id must contain 1..256 characters")
    return value


class WorkflowJournal:
    """Append-only operation claims and outcomes in the authoritative Dao store."""

    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS workflow_operations (
                operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS workflow_results (
                operation_id TEXT PRIMARY KEY REFERENCES workflow_operations(operation_id),
                payload TEXT NOT NULL)""")
            for table in ("workflow_operations", "workflow_results"):
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation}
                        BEFORE {operation} ON {table}
                        BEGIN SELECT RAISE(ABORT, 'immutable workflow record'); END""")

    def get(self, operation_id):
        """Inspect a durable receipt; an unfinished claim needs operator inspection."""
        _operation_id(operation_id)
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT payload FROM workflow_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("unknown workflow operation")
            started = json.loads(row[0])
            result = db.execute(
                "SELECT payload FROM workflow_results WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return {**started, **(json.loads(result[0]) if result else {"status": "started"})}

    def verify_integrity(self):
        """Check receipts against Dao's event chain and append-only triggers."""
        with self.store.transaction() as db:
            result = self.store.verify_integrity()
            issues = list(result["issues"])
            try:
                events = {}
                for row in db.execute(
                    "SELECT kind, payload FROM events WHERE kind LIKE 'workflow.%' ORDER BY seq"
                ):
                    events.setdefault(row["kind"], []).append(canonical_json(json.loads(row["payload"])))
                for table, kinds in (
                    ("workflow_operations", ("workflow.started",)),
                    ("workflow_results", ("workflow.completed", "workflow.failed")),
                ):
                    rows = list(db.execute(f"SELECT operation_id, payload FROM {table}"))
                    payloads = [json.loads(row["payload"]) for row in rows]
                    expected = sorted(item for kind in kinds for item in events.get(kind, []))
                    if sorted(canonical_json(item) for item in payloads) != expected:
                        issues.append("Workflow projection differs from event chain: " + table)
                    if any(row["operation_id"] != item["operation_id"]
                           for row, item in zip(rows, payloads)):
                        issues.append("Workflow index differs from payload: " + table)
                    for operation in ("UPDATE", "DELETE"):
                        name = f"immutable_{table}_{operation}"
                        if not db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
                        ).fetchone():
                            issues.append("Missing workflow append-only trigger: " + name)
            except (ValueError, TypeError, KeyError) as exc:
                issues.append("Invalid workflow projection: " + type(exc).__name__)
            return {**result, "ok": not issues, "issues": issues}

    def _previous(self, operation_id, request, kind):
        if not self.verify_integrity()["ok"]:
            raise ConflictError("workflow or state integrity failed")
        try:
            previous = self.get(operation_id)
        except NotFoundError:
            return None
        if previous["kind"] != kind or canonical_json(previous["request"]) != request:
            raise ConflictError("workflow operation_id already binds different inputs")
        if previous["status"] != "completed":
            raise ConflictError(
                "workflow operation " + operation_id + " not completed; inspect state and usage "
                "before starting a new operation"
            )
        return previous

    def _start(self, operation_id, request, kind):
        payload = {"operation_id": operation_id, "kind": kind,
                   "request": json.loads(request), "created_at": _now()}
        self.store.db.execute(
            "INSERT INTO workflow_operations VALUES (?, ?)",
            (operation_id, canonical_json(payload)),
        )
        self.store.append_event("workflow.started", payload)

    def _finish(self, operation_id, status, **values):
        payload = {"operation_id": operation_id, "status": status,
                   "finished_at": _now(), **values}
        self.store.db.execute(
            "INSERT INTO workflow_results VALUES (?, ?)",
            (operation_id, canonical_json(payload)),
        )
        self.store.append_event("workflow." + status, payload)

    def run_local(self, operation_id, request, callback):
        """Execute a local mutation and cache its finite JSON result atomically."""
        _operation_id(operation_id)
        request = canonical_json(request)
        with self.store.transaction():
            previous = self._previous(operation_id, request, "local")
            if previous is not None:
                return previous["result"]
            self._start(operation_id, request, "local")
            result = json.loads(canonical_json(callback()))
            self._finish(operation_id, "completed", result=result)
            return result

    def run_external(self, operation_id, request, callback):
        """Claim before dispatch; completed work replays, uncertain work stops.

        The callback must not be wrapped in a caller's Store transaction. A
        crash or BaseException leaves a started claim; an Exception records its
        type, never potentially sensitive exception text. Neither is retried.
        """
        with self.store.lock:
            if self.store.db.in_transaction:
                raise ConflictError("external workflow work cannot run inside a Store transaction")
            previous = self.claim_external(operation_id, request)
        if previous is not None:
            return previous["result"]
        try:
            result = json.loads(canonical_json(callback()))
        except Exception as exc:
            self.finish_external(operation_id, error_type=type(exc).__name__)
            raise
        self.finish_external(operation_id, result=result)
        return result

    def claim_external(self, operation_id, request):
        """Claim dispatch, optionally in the transaction advancing its work record.

        Returns a completed receipt on replay. An unfinished claim raises; callers
        must never interpret it as permission to redispatch.
        """
        _operation_id(operation_id)
        request = canonical_json(request)
        with self.store.lock:
            with self.store.transaction():
                previous = self._previous(operation_id, request, "external")
                if previous is not None:
                    return previous
                self._start(operation_id, request, "external")
        return None

    def finish_external(self, operation_id, *, result=None, error_type=None):
        """Persist a claimed result without retrying or erasing its attempt."""
        with self.store.transaction():
            receipt = self.get(operation_id)
            if receipt["kind"] != "external" or receipt["status"] != "started":
                raise ConflictError("external operation is not an unfinished claim")
            if error_type is None:
                self._finish(operation_id, "completed", result=json.loads(canonical_json(result)))
            else:
                if not isinstance(error_type, str) or not error_type:
                    raise ValueError("error_type must be a nonempty string")
                self._finish(operation_id, "failed", error_type=error_type)
