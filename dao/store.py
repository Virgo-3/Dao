"""Durable, append-only state history and usage accounting for Dao.

SQLite transactions serialize journal writes and head changes across threads and
processes. Hash chains detect changes to persisted history; they are not signatures
and do not protect against an attacker who can replace the whole database.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
import uuid


class ConflictError(RuntimeError):
    """The branch changed or an idempotency key has conflicting parameters."""


class BudgetError(RuntimeError):
    """A reservation would exceed the global token budget."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Value must be finite JSON data") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _integer(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= (2 ** 63 - 1):
        raise ValueError(f"{name} must be a nonnegative 64-bit integer")
    return value


def _name(name: str) -> str:
    if (not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", name)
            or ".." in name or "//" in name or name.endswith(("/", "."))):
        raise ValueError("Invalid branch name")
    return name


def _text(value: str, name: str, limit: int = 1000) -> str:
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"Invalid {name}")
    return value


class Store:
    """A connection-per-operation store with atomic optimistic branch updates.

    Snapshots returned by this API are detached JSON values. Calling ``restore``
    appends a new commit, so old state, audit events, and provider costs remain.
    The token budget counts both finalized tokens and outstanding reservations
    globally, including requests on branches that are no longer active.
    """

    def __init__(self, path: str | Path):
        self._keeper: sqlite3.Connection | None = None
        self._closed = False
        self.path = str(path)
        self._uri = self.path == ":memory:"
        if self._uri:
            self.path = f"file:dao-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = self._connect()
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS commits (
                    id TEXT PRIMARY KEY, parent_id TEXT REFERENCES commits(id),
                    kind TEXT NOT NULL, label TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS branches (
                    name TEXT PRIMARY KEY, head TEXT NOT NULL REFERENCES commits(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, branch TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, previous_hash TEXT,
                    hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS usage_ledger (
                    request_id TEXT PRIMARY KEY, branch TEXT NOT NULL,
                    reserved_tokens INTEGER NOT NULL, token_budget INTEGER NOT NULL,
                    status TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL DEFAULT '',
                    cost_microusd INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, finalized_at TEXT
                );
                CREATE INDEX IF NOT EXISTS events_branch ON events(branch, id);
            """)
            # executescript ends an open transaction; reacquire before bootstrap.
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM branches WHERE name='main'").fetchone() is None:
                if any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                       for table in ("commits", "branches", "events", "usage_ledger")):
                    raise ValueError("Existing database has no main branch; integrity repair is required")
                created = _now()
                commit = self._insert_commit(conn, None, "bootstrap", "Initial state",
                                             {"messages": [], "memory": {},
                                              "decisions": [], "artifacts": {}}, created)
                conn.execute("INSERT INTO branches VALUES (?, ?, ?)",
                             ("main", commit["id"], created))
                self._event(conn, "main", "state.bootstrap", {"commit_id": commit["id"]})

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("Store is closed")
        conn = sqlite3.connect(self.path, timeout=30, uri=self._uri,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        if not self._uri:
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def close(self) -> None:
        """Release the keeper connection of an in-memory store."""
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None
        self._closed = True

    @contextmanager
    def _transaction(self, write: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _commit(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["state"] = json.loads(result["state"])
        return result

    @staticmethod
    def _usage_entry(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["estimated"] = bool(result["estimated"])
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
        return result

    @staticmethod
    def _lookup(conn: sqlite3.Connection, commit_id: str) -> dict:
        row = conn.execute("SELECT * FROM commits WHERE id=?", (commit_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown commit: {commit_id}")
        return Store._commit(row)

    @staticmethod
    def _head_id(conn: sqlite3.Connection, branch: str) -> str:
        row = conn.execute("SELECT head FROM branches WHERE name=?", (branch,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown branch: {branch}")
        return row["head"]

    @staticmethod
    def _insert_commit(conn: sqlite3.Connection, parent_id: str | None, kind: str,
                       label: str, state: dict, created_at: str) -> dict:
        record = {"parent_id": parent_id, "kind": kind, "label": label,
                  "state": state, "created_at": created_at}
        record["id"] = _hash(record)
        conn.execute("INSERT INTO commits VALUES (?, ?, ?, ?, ?, ?)",
                     (record["id"], parent_id, kind, label, _json(state), created_at))
        return record

    @staticmethod
    def _event(conn: sqlite3.Connection, branch: str, kind: str, payload: dict) -> dict:
        prior = conn.execute("SELECT id, hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
        record = {"id": prior["id"] + 1 if prior else 1,
                  "branch": branch, "kind": kind, "payload": payload,
                  "created_at": _now(), "previous_hash": prior["hash"] if prior else None}
        record["hash"] = _hash(record)
        conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (record["id"], branch, kind, _json(payload), record["created_at"],
                      record["previous_hash"], record["hash"]))
        return record

    def branches(self) -> list[dict]:
        with self._transaction(False) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM branches ORDER BY name")]

    def branch(self, name: str, from_commit: str) -> dict:
        _name(name)
        with self._transaction() as conn:
            self._lookup(conn, from_commit)
            if conn.execute("SELECT 1 FROM branches WHERE name=?", (name,)).fetchone():
                raise ConflictError(f"Branch already exists: {name}")
            record = {"name": name, "head": from_commit, "created_at": _now()}
            conn.execute("INSERT INTO branches VALUES (?, ?, ?)",
                         (name, from_commit, record["created_at"]))
            self._event(conn, name, "branch.created", record)
            return record

    def head(self, branch: str = "main") -> dict:
        _name(branch)
        with self._transaction(False) as conn:
            return self._lookup(conn, self._head_id(conn, branch))

    def get_commit(self, commit_id: str) -> dict:
        with self._transaction(False) as conn:
            return self._lookup(conn, commit_id)

    def _advance(self, conn: sqlite3.Connection, branch: str, state: dict,
                 kind: str, label: str, expected_head: str) -> dict:
        actual = self._head_id(conn, branch)
        if actual != expected_head:
            raise ConflictError(f"Branch {branch} changed; reload before retrying")
        record = self._insert_commit(conn, actual, kind, label, state, _now())
        changed = conn.execute("UPDATE branches SET head=? WHERE name=? AND head=?",
                               (record["id"], branch, expected_head)).rowcount
        if changed != 1:
            raise ConflictError(f"Branch {branch} changed; reload before retrying")
        self._event(conn, branch, "state.commit", {
            "commit_id": record["id"], "parent_id": actual, "kind": kind, "label": label})
        return record

    def commit(self, branch: str, state: dict, kind: str, label: str,
               expected_head: str) -> dict:
        _name(branch)
        _text(kind, "commit kind", 100)
        _text(label, "commit label")
        if not isinstance(state, dict):
            raise ValueError("State must be a JSON object")
        # Serialize before taking a lock, rejecting NaN and detaching caller data.
        state = json.loads(_json(state))
        with self._transaction() as conn:
            return self._advance(conn, branch, state, kind, label, expected_head)

    def history(self, branch: str = "main") -> list[dict]:
        _name(branch)
        with self._transaction(False) as conn:
            current = self._head_id(conn, branch)
            records, seen = [], set()
            while current is not None:
                if current in seen:
                    raise ValueError("Cycle in commit ancestry")
                seen.add(current)
                record = self._lookup(conn, current)
                records.append(record)
                current = record["parent_id"]
            return records

    def restore(self, branch: str, commit_id: str, expected_head: str) -> dict:
        _name(branch)
        with self._transaction() as conn:
            current = self._head_id(conn, branch)
            if current != expected_head:
                raise ConflictError(f"Branch {branch} changed; reload before retrying")
            seen = set()
            while current is not None and current != commit_id:
                if current in seen:
                    raise ValueError("Cycle in commit ancestry")
                seen.add(current)
                current = self._lookup(conn, current)["parent_id"]
            if current is None:
                raise ValueError("Restore target must be an ancestor of the branch head")
            target = self._lookup(conn, commit_id)
            record = self._advance(conn, branch, target["state"], "restore",
                                   f"Restore {commit_id[:12]}", expected_head)
            self._event(conn, branch, "state.restored",
                        {"commit_id": record["id"], "restored_from": commit_id})
            return record

    def append_event(self, branch: str, kind: str, payload: dict) -> dict:
        _name(branch)
        _text(kind, "event kind", 100)
        if kind.startswith(("state.", "branch.", "usage.")):
            raise ValueError("Reserved journal event kind")
        if not isinstance(payload, dict):
            raise ValueError("Event payload must be a JSON object")
        payload = json.loads(_json(payload))
        with self._transaction() as conn:
            self._head_id(conn, branch)
            return self._event(conn, branch, kind, payload)

    def events(self, branch: str | None = None) -> list[dict]:
        if branch is not None:
            _name(branch)
        with self._transaction(False) as conn:
            if branch is not None:
                self._head_id(conn, branch)
            rows = conn.execute("SELECT * FROM events WHERE branch=? ORDER BY id", (branch,)) \
                if branch is not None else conn.execute("SELECT * FROM events ORDER BY id")
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def reserve_usage(self, branch: str, request_id: str, reserved_tokens: int,
                      token_budget: int) -> None:
        _name(branch)
        _text(request_id, "request ID", 200)
        _integer(reserved_tokens, "reserved_tokens")
        _integer(token_budget, "token_budget")
        with self._transaction() as conn:
            self._head_id(conn, branch)
            existing = conn.execute("SELECT * FROM usage_ledger WHERE request_id=?",
                                    (request_id,)).fetchone()
            if existing:
                if (existing["branch"], existing["reserved_tokens"], existing["token_budget"]) \
                        != (branch, reserved_tokens, token_budget):
                    raise ConflictError("Request ID already has different reservation parameters")
                return
            charged = sum(row["reserved_tokens"] if row["status"] == "reserved"
                          else row["input_tokens"] + row["output_tokens"]
                          for row in conn.execute("SELECT * FROM usage_ledger"))
            if charged + reserved_tokens > token_budget:
                raise BudgetError("Global token budget exhausted")
            conn.execute("""INSERT INTO usage_ledger
                (request_id, branch, reserved_tokens, token_budget, status, created_at)
                VALUES (?, ?, ?, ?, 'reserved', ?)""",
                         (request_id, branch, reserved_tokens, token_budget, _now()))
            entry = self._usage_entry(conn.execute(
                "SELECT * FROM usage_ledger WHERE request_id=?", (request_id,)).fetchone())
            self._event(conn, branch, "usage.reserved", entry)

    def finalize_usage(self, request_id: str, input_tokens: int, output_tokens: int,
                       estimated: bool, status: str, model: str,
                       cost_microusd: int = 0) -> dict:
        _text(request_id, "request ID", 200)
        _integer(input_tokens, "input_tokens")
        _integer(output_tokens, "output_tokens")
        _integer(cost_microusd, "cost_microusd")
        _integer(input_tokens + output_tokens, "total_tokens")
        if type(estimated) is not bool:
            raise ValueError("estimated must be a boolean")
        if not isinstance(status, str) or status not in {
                "completed", "incomplete", "failed", "unknown", "cancelled", "succeeded", "success"}:
            raise ValueError("Invalid terminal usage status")
        if not isinstance(model, str) or len(model) > 200:
            raise ValueError("Invalid model")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM usage_ledger WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown usage request: {request_id}")
            if row["status"] != "reserved":
                return self._usage_entry(row)
            # Transport failures may occur after the provider consumed tokens.
            if status in {"incomplete", "failed", "unknown", "cancelled"} and estimated:
                output_tokens = max(output_tokens, row["reserved_tokens"] - input_tokens)
            conn.execute("""UPDATE usage_ledger SET input_tokens=?, output_tokens=?,
                estimated=?, status=?, model=?, cost_microusd=?, finalized_at=?
                WHERE request_id=? AND status='reserved'""",
                         (input_tokens, output_tokens, estimated, status, model,
                          cost_microusd, _now(), request_id))
            entry = self._usage_entry(conn.execute(
                "SELECT * FROM usage_ledger WHERE request_id=?", (request_id,)).fetchone())
            self._event(conn, row["branch"], "usage.finalized", entry)
            return entry

    def usage(self) -> dict:
        with self._transaction(False) as conn:
            entries = [self._usage_entry(row) for row in conn.execute(
                "SELECT * FROM usage_ledger ORDER BY created_at, request_id")]
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                  "cost_microusd": 0, "reserved_tokens": 0}
        for entry in entries:
            for key in ("input_tokens", "output_tokens", "total_tokens", "cost_microusd"):
                totals[key] += entry[key]
            if entry["status"] == "reserved":
                totals["reserved_tokens"] += entry["reserved_tokens"]
        return {"entries": entries, "totals": totals, **totals}

    def verify(self) -> dict:
        """Recompute hashes and replay branch pointers and usage journal records."""
        errors: list[str] = []
        with self._transaction(False) as conn:
            commits = {}
            for row in conn.execute("SELECT * FROM commits"):
                try:
                    record = self._commit(row)
                    digest = record.pop("id")
                    if _hash(record) != digest:
                        errors.append(f"Commit hash mismatch: {digest}")
                    commits[digest] = {"id": digest, **record}
                except (ValueError, TypeError):
                    errors.append(f"Invalid commit JSON: {row['id']}")
            for record in commits.values():
                if record["parent_id"] is not None and record["parent_id"] not in commits:
                    errors.append(f"Missing commit parent: {record['id']}")
            previous, event_count, heads, ledger, referenced = None, 0, {}, {}, set()
            created_branches = {}
            for row in conn.execute("SELECT * FROM events ORDER BY id"):
                event_count += 1
                try:
                    record = dict(row)
                    record["payload"] = json.loads(record["payload"])
                    digest = record.pop("hash")
                    if record["id"] != event_count or record["previous_hash"] != previous:
                        errors.append(f"Broken event chain: {record['id']}")
                    if _hash(record) != digest:
                        errors.append(f"Event hash mismatch: {record['id']}")
                    previous = digest
                    branch, kind, payload = record["branch"], record["kind"], record["payload"]
                    if kind == "state.bootstrap":
                        heads[branch] = payload["commit_id"]
                        referenced.add(payload["commit_id"])
                        created_branches[branch] = commits[payload["commit_id"]]["created_at"]
                    elif kind == "branch.created":
                        heads[branch] = payload["head"]
                        created_branches[branch] = payload["created_at"]
                    elif kind == "state.commit":
                        if heads.get(branch) != payload["parent_id"]:
                            errors.append(f"Invalid branch transition: {record['id']}")
                        stored = commits.get(payload["commit_id"], {})
                        if stored.get("parent_id") != payload["parent_id"]:
                            errors.append(f"Commit journal mismatch: {record['id']}")
                        heads[branch] = payload["commit_id"]
                        referenced.add(payload["commit_id"])
                    elif kind in {"usage.reserved", "usage.finalized"}:
                        ledger[payload["request_id"]] = payload
                except (ValueError, TypeError, KeyError):
                    errors.append(f"Invalid event payload: {row['id']}")
                    previous = row["hash"]
            branches = [dict(row) for row in conn.execute("SELECT * FROM branches")]
            actual_heads = {row["name"]: row["head"] for row in branches}
            if heads != actual_heads or any(head not in commits for head in heads.values()):
                errors.append("Branch heads differ from the journal")
            if created_branches != {row["name"]: row["created_at"] for row in branches}:
                errors.append("Branch metadata differs from the journal")
            if referenced != set(commits):
                errors.append("Commit inventory differs from the journal")
            actual_ledger = {row["request_id"]: self._usage_entry(row)
                             for row in conn.execute("SELECT * FROM usage_ledger")}
            if ledger != actual_ledger:
                errors.append("Usage ledger differs from the journal")
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                errors.append(f"SQLite integrity check: {check}")
        return {"ok": not errors, "errors": errors,
                "commits": len(commits), "events": event_count}
