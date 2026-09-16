"""Durable, immutable conversation snapshots with branch-safe updates.

Restoring a snapshot changes recorded state only. It cannot undo effects in the
outside world, such as messages that were sent or money that was spent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import memory


MAX_STATE_BYTES = 1_048_576
MAX_STATE_DEPTH = 64
MAX_GRAPH_NODES = 10_000
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")
_COMMIT_ID = re.compile(r"[0-9a-f]{64}\Z")
_MISSING = object()


class StoreError(Exception):
    """Invalid input, unknown references, or an unusable state database."""


class ConflictError(StoreError):
    """The branch moved since the caller read its head."""


class MergeConflict(StoreError):
    """Both branches changed the same value; paths use JSON Pointer syntax."""

    def __init__(self, paths: list[str]):
        self.paths = sorted(set(paths))
        super().__init__("Merge conflicts at: " + ", ".join(self.paths))


def _validate_json(value: Any, depth: int = 0, active: set[int] | None = None) -> None:
    if depth > MAX_STATE_DEPTH:
        raise StoreError(f"State nesting exceeds {MAX_STATE_DEPTH} levels.")
    kind = type(value)
    if value is None or kind in (bool, int, str):
        return
    if kind is float:
        if not math.isfinite(value):
            raise StoreError("State must not contain NaN or infinity.")
        return
    if kind not in (list, dict):
        raise StoreError("State must contain only JSON values.")
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise StoreError("State must not contain cycles.")
    active.add(identity)
    try:
        if kind is dict:
            if any(type(key) is not str for key in value):
                raise StoreError("State object keys must be strings.")
            children = value.values()
        else:
            children = value
        for child in children:
            _validate_json(child, depth + 1, active)
    finally:
        active.remove(identity)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise StoreError("State cannot be serialized as JSON.") from exc


def _snapshot(state: Any) -> str:
    if type(state) is not dict:
        raise StoreError("A state snapshot must be a JSON object.")
    _validate_json(state)
    encoded = _canonical(state)
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise StoreError("State must contain valid Unicode text.") from exc
    if size > MAX_STATE_BYTES:
        raise StoreError(f"State exceeds the {MAX_STATE_BYTES}-byte limit.")
    return encoded


def _pointer(path: str, key: str) -> str:
    return path + "/" + key.replace("~", "~0").replace("/", "~1")


def _equal(left: Any, right: Any) -> bool:
    # Python considers True == 1; JSON treats booleans and numbers separately.
    if left is _MISSING or right is _MISSING:
        return left is right
    return _canonical(left) == _canonical(right)


def _merge_values(base: Any, target: Any, source: Any, path: str,
                  conflicts: list[str]) -> Any:
    if _equal(target, source):
        return target
    if _equal(target, base):
        return source
    if _equal(source, base):
        return target
    if type(target) is dict and type(source) is dict and (type(base) is dict or base is _MISSING):
        original = {} if base is _MISSING else base
        merged = {}
        for key in sorted(set(original) | set(target) | set(source)):
            value = _merge_values(original.get(key, _MISSING), target.get(key, _MISSING),
                                  source.get(key, _MISSING), _pointer(path, key), conflicts)
            if value is not _MISSING:
                merged[key] = value
        return merged
    conflicts.append(path or "/")
    return target


class Store:
    """A SQLite commit DAG. Each write checks the expected head atomically.

    References are branch names or full SHA-256 commit IDs. Lists (including
    conversation transcripts) merge atomically to avoid inventing event order.
    Every read returns freshly decoded data, so callers cannot mutate history.
    """

    def __init__(self, path: str | Path):
        if not isinstance(path, (str, Path)) or not str(path):
            raise StoreError("The state database path must be nonempty text or a Path.")
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._db = sqlite3.connect(str(path), timeout=10, isolation_level=None,
                                       check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                self._db.close()
                raise StoreError(f"Unsupported state database version: {version}.")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS commits (
                    id TEXT PRIMARY KEY,
                    parents TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS branches (
                    name TEXT PRIMARY KEY,
                    head TEXT NOT NULL REFERENCES commits(id)
                );
                CREATE TRIGGER IF NOT EXISTS commits_no_update
                    BEFORE UPDATE ON commits BEGIN
                    SELECT RAISE(ABORT, 'Commit snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS commits_no_delete
                    BEFORE DELETE ON commits BEGIN
                    SELECT RAISE(ABORT, 'Commit snapshots are immutable'); END;
                PRAGMA user_version = 1;
            """)
            memory.create_schema(self._db)
        except sqlite3.Error as exc:
            if hasattr(self, "_db"):
                self._db.close()
            raise StoreError("Could not open the state database.") from exc

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    @contextmanager
    def _transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise StoreError("The state store is closed.")
            try:
                self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield self._db
                self._db.execute("COMMIT")
            except BaseException as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise StoreError("State database operation failed.") from exc
                raise

    @staticmethod
    def _branch_name(name: Any) -> str:
        if (not isinstance(name, str) or not _BRANCH.fullmatch(name)
                or ".." in name or "//" in name or name.endswith(("/", "."))
                or _COMMIT_ID.fullmatch(name)):
            raise StoreError("Branch names must be 1–64 letters, digits, '.', '_', '-', or '/'; "
                             "start with a letter or digit and avoid '..', '//', and trailing '/' or '.'.")
        return name

    @staticmethod
    def _message(message: Any) -> str:
        if not isinstance(message, str) or not message.strip() or len(message) > 4096:
            raise StoreError("Commit messages must contain 1–4096 characters.")
        try:
            message.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise StoreError("Commit messages must contain valid Unicode text.") from exc
        return message

    @staticmethod
    def _resolve(db: sqlite3.Connection, ref: Any) -> str:
        if not isinstance(ref, str) or not ref or len(ref) > 64:
            raise StoreError("A reference must be a branch name or full commit ID.")
        if not _BRANCH.fullmatch(ref):
            raise StoreError("A reference must be a branch name or full commit ID.")
        branch = db.execute("SELECT head FROM branches WHERE name = ?", (ref,)).fetchone()
        if branch:
            return branch["head"]
        if _COMMIT_ID.fullmatch(ref):
            if db.execute("SELECT 1 FROM commits WHERE id = ?", (ref,)).fetchone():
                return ref
        raise StoreError(f"Unknown reference: {ref!r}.")

    @staticmethod
    def _branch_head(db: sqlite3.Connection, branch: str) -> str:
        Store._branch_name(branch)
        row = db.execute("SELECT head FROM branches WHERE name = ?", (branch,)).fetchone()
        if not row:
            raise StoreError(f"Unknown branch: {branch!r}.")
        return row["head"]

    @staticmethod
    def _check_head(db: sqlite3.Connection, branch: str, expected_head: Any) -> str:
        current = Store._branch_head(db, branch)
        if not isinstance(expected_head, str) or not _COMMIT_ID.fullmatch(expected_head):
            raise StoreError("expected_head must be the full commit ID previously read.")
        if current != expected_head:
            raise ConflictError(f"Branch {branch!r} has changed; reload it before retrying.")
        return current

    @staticmethod
    def _hash(parents: list[str], message: str, created_at: str, state: dict) -> str:
        envelope = {"version": 1, "parents": parents, "message": message,
                    "created_at": created_at, "state": state}
        return hashlib.sha256(_canonical(envelope).encode("utf-8")).hexdigest()

    @staticmethod
    def _get(db: sqlite3.Connection, commit_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM commits WHERE id = ?", (commit_id,)).fetchone()
        if not row:
            raise StoreError(f"Missing commit: {commit_id}.")
        try:
            parents = json.loads(row["parents"])
            state = json.loads(row["state"])
            if (not isinstance(parents, list) or len(parents) > 2
                    or any(not isinstance(p, str) or not _COMMIT_ID.fullmatch(p) for p in parents)):
                raise ValueError("Invalid parents")
            _snapshot(state)
            actual = Store._hash(parents, row["message"], row["created_at"], state)
            if actual != commit_id:
                raise ValueError("Content hash mismatch")
        except (TypeError, ValueError, UnicodeError, RecursionError, StoreError) as exc:
            raise StoreError(f"Corrupt commit: {commit_id}.") from exc
        return {"id": commit_id, "parents": parents, "message": row["message"],
                "created_at": row["created_at"], "state": state}

    @staticmethod
    def _insert(db: sqlite3.Connection, parents: list[str], state_json: str, message: str) -> str:
        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        commit_id = Store._hash(parents, message, created_at, json.loads(state_json))
        db.execute("INSERT INTO commits(id, parents, message, created_at, state) VALUES (?, ?, ?, ?, ?)",
                   (commit_id, _canonical(parents), message, created_at, state_json))
        return commit_id

    def initialize(self, initial_state: dict) -> str:
        state_json = _snapshot(initial_state)
        with self._transaction(write=True) as db:
            row = db.execute("SELECT head FROM branches WHERE name = 'main'").fetchone()
            if row:
                self._get(db, row["head"])
                return row["head"]
            commit_id = self._insert(db, [], state_json, "Initialize state")
            db.execute("INSERT INTO branches(name, head) VALUES ('main', ?)", (commit_id,))
            return commit_id

    def branches(self) -> dict[str, str]:
        with self._transaction() as db:
            return {row["name"]: row["head"] for row in
                    db.execute("SELECT name, head FROM branches ORDER BY name")}

    def head(self, branch: str) -> str:
        with self._transaction() as db:
            return self._branch_head(db, branch)

    def read(self, ref: str) -> dict[str, Any]:
        with self._transaction() as db:
            return self._get(db, self._resolve(db, ref))

    def search(self, query: str, ref: str = "main", *, limit: int = 8,
               max_chars: int = 16000) -> dict[str, Any]:
        """Rank all saved state, across every branch and both merge parents.

        The write transaction only updates the derived search cache. Branch
        heads, immutable snapshots, and checkpoint IDs are never changed.
        """
        with self._transaction(write=True) as db:
            current = self._get(db, self._resolve(db, ref))
            return memory.search(db, query, current, self._get, limit=limit, max_chars=max_chars)

    def commit(self, branch: str, state: dict, message: str, expected_head: str) -> str:
        state_json = _snapshot(state)
        self._message(message)
        with self._transaction(write=True) as db:
            parent = self._check_head(db, branch, expected_head)
            self._get(db, parent)
            commit_id = self._insert(db, [parent], state_json, message)
            db.execute("UPDATE branches SET head = ? WHERE name = ? AND head = ?",
                       (commit_id, branch, parent))
            return commit_id

    def branch(self, name: str, from_ref: str) -> str:
        self._branch_name(name)
        with self._transaction(write=True) as db:
            if db.execute("SELECT 1 FROM branches WHERE name = ?", (name,)).fetchone():
                raise ConflictError(f"Branch {name!r} already exists.")
            commit_id = self._resolve(db, from_ref)
            self._get(db, commit_id)
            db.execute("INSERT INTO branches(name, head) VALUES (?, ?)", (name, commit_id))
            return commit_id

    def history(self, ref: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return first-parent history, newest first; merge metadata names both parents."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise StoreError("History limit must be an integer from 1 to 1000.")
        with self._transaction() as db:
            current = self._resolve(db, ref)
            result = []
            seen = set()
            while current and len(result) < limit:
                if current in seen:
                    raise StoreError("Commit graph contains a cycle.")
                seen.add(current)
                record = self._get(db, current)
                result.append({key: value for key, value in record.items() if key != "state"})
                current = record["parents"][0] if record["parents"] else None
            return result

    def diff(self, left: str, right: str) -> list[dict[str, Any]]:
        """Diff dictionaries recursively and lists atomically; existence flags preserve nulls."""
        with self._transaction() as db:
            before = self._get(db, self._resolve(db, left))["state"]
            after = self._get(db, self._resolve(db, right))["state"]
        changes = []

        def walk(old: Any, new: Any, path: str) -> None:
            if _equal(old, new):
                return
            if type(old) is dict and type(new) is dict:
                for key in sorted(set(old) | set(new)):
                    walk(old.get(key, _MISSING), new.get(key, _MISSING), _pointer(path, key))
                return
            changes.append({"path": path or "/", "before": None if old is _MISSING else old,
                            "after": None if new is _MISSING else new,
                            "before_exists": old is not _MISSING, "after_exists": new is not _MISSING})

        walk(before, after, "")
        return changes

    def restore(self, branch: str, ref: str, expected_head: str) -> str:
        """Append an old snapshot. Previously recorded commits remain reachable."""
        with self._transaction(write=True) as db:
            parent = self._check_head(db, branch, expected_head)
            self._get(db, parent)
            source = self._resolve(db, ref)
            state = self._get(db, source)["state"]
            commit_id = self._insert(db, [parent], _snapshot(state), f"Restore state from {source}")
            db.execute("UPDATE branches SET head = ? WHERE name = ? AND head = ?",
                       (commit_id, branch, parent))
            return commit_id

    @staticmethod
    def _ancestry(db: sqlite3.Connection, start: str,
                  records: dict[str, dict[str, Any]]) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if current not in records:
                if len(records) >= MAX_GRAPH_NODES:
                    raise StoreError(f"Merge exceeds the {MAX_GRAPH_NODES}-commit traversal limit.")
                # Keep only graph metadata while walking. A long history must
                # not retain thousands of complete conversation snapshots.
                record = Store._get(db, current)
                records[current] = {"parents": record["parents"]}
            pending.extend(records[current]["parents"])
        return seen

    def merge(self, target: str, source: str, expected_head: str,
              validate: Callable[[dict], Any] | None = None) -> str:
        """Create a two-parent merge or fail without modifying either branch.

        Multiple best merge bases are rejected for explicit resolution. Lists
        conflict if both sides change them differently, preserving event order.
        An optional trusted validator can reject semantic conflicts by raising
        before anything is written. It must not mutate the candidate snapshot.
        """
        with self._transaction(write=True) as db:
            target_id = self._check_head(db, target, expected_head)
            source_id = self._resolve(db, source)
            records: dict[str, dict[str, Any]] = {}
            target_ancestors = self._ancestry(db, target_id, records)
            source_ancestors = self._ancestry(db, source_id, records)
            if target_id == source_id or source_id in target_ancestors:
                return target_id
            common = target_ancestors & source_ancestors
            if not common:
                raise StoreError("The snapshots have no common ancestor.")
            # Every ancestor below a common commit is also common. The best
            # bases are the common nodes that are not parents of common nodes.
            older = {parent for node in common for parent in records[node]["parents"]}
            bases = common - older
            if len(bases) != 1:
                raise MergeConflict(["/"])
            base_id = next(iter(bases))
            conflicts: list[str] = []
            merged = _merge_values(self._get(db, base_id)["state"],
                                   self._get(db, target_id)["state"],
                                   self._get(db, source_id)["state"], "", conflicts)
            if conflicts:
                raise MergeConflict(conflicts)
            if validate is not None:
                # Pass a separate snapshot so validation cannot silently edit
                # the result of the three-way merge.
                validate(json.loads(_snapshot(merged)))
            state_json = _snapshot(merged)
            commit_id = self._insert(db, [target_id, source_id], state_json,
                                     f"Merge {source_id} into {target}")
            db.execute("UPDATE branches SET head = ? WHERE name = ? AND head = ?",
                       (commit_id, target, target_id))
            return commit_id
