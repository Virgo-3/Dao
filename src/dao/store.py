"""Transactional, content-addressed conversation state and an append-only event journal.

Branches are mutable references; commits are immutable snapshots. Every state write
requires the head observed by its caller, preventing lost updates between processes.
The journal supplies tamper evidence, not protection from a database administrator
who can rewrite the entire history and recompute its hashes.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator


class ConflictError(ValueError):
    """A stale write or incompatible merge must be adjudicated by the caller."""


class NotFoundError(ValueError):
    """A requested branch or commit does not exist."""


_MISSING = object()
_CHAIN_START = "0" * 64
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_REF_EVENT_KINDS = frozenset(
    {"branch.created", "state.committed", "state.merged", "state.restored"}
)


def canonical_json(value: Any) -> str:
    """Encode finite JSON values deterministically, rejecting ambiguous object keys."""

    def validate(item: Any) -> None:
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ValueError("Only JSON values can be stored")

    try:
        validate(value)
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Value must be finite, serializable JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _branch_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not _BRANCH_PATTERN.fullmatch(name)
        or ".." in name
        or "//" in name
        or name.endswith(("/", "."))
    ):
        raise ValueError("Branch name must be 1-128 letters, digits, '.', '_', '-', or '/'")
    return name


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10_000:
        raise ValueError("Limit must be an integer between 1 and 10000")
    return value


def _state(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("State must be a JSON object")
    # Serialize and parse to detach input, including all nested objects.
    result = json.loads(canonical_json(value))
    result.setdefault("messages", [])
    result.setdefault("memory", {})
    result.setdefault("decisions", {})
    if not isinstance(result["messages"], list):
        raise ValueError("State messages must be a list")
    if not isinstance(result["memory"], dict) or not isinstance(result["decisions"], dict):
        raise ValueError("State memory and decisions must be objects")
    return result


def _pointer(path: str, key: str) -> str:
    return path + "/" + key.replace("~", "~0").replace("/", "~1")


def _equal(a: Any, b: Any) -> bool:
    if a is _MISSING or b is _MISSING:
        return a is b
    # Python treats True == 1 and 1 == 1.0; JSON content hashes do not.
    return canonical_json(a) == canonical_json(b)


def _merge(base: Any, ours: Any, theirs: Any, path: str, conflicts: list[str]) -> Any:
    if _equal(ours, theirs):
        return ours
    if _equal(ours, base):
        return theirs
    if _equal(theirs, base):
        return ours
    if (
        isinstance(ours, dict)
        and isinstance(theirs, dict)
        and (isinstance(base, dict) or base is _MISSING)
    ):
        baseline = {} if base is _MISSING else base
        result = {}
        for key in sorted(set(baseline) | set(ours) | set(theirs)):
            value = _merge(
                baseline.get(key, _MISSING),
                ours.get(key, _MISSING),
                theirs.get(key, _MISSING),
                _pointer(path, key),
                conflicts,
            )
            if value is not _MISSING:
                result[key] = value
        return result
    conflicts.append(path or "/")
    return ours


class Store:
    """SQLite state store; transactions also compose atomically with usage and audit."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._savepoint = 0
        self.db = sqlite3.connect(
            self.path, timeout=30, isolation_level=None, check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 30000")
        self.db.execute("PRAGMA journal_mode = WAL")
        with self.transaction() as db:
            statements = (
                "CREATE TABLE IF NOT EXISTS commits (id TEXT PRIMARY KEY, "
                "parents TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL, "
                "created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS branches (name TEXT PRIMARY KEY, "
                "head TEXT NOT NULL REFERENCES commits(id))",
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, "
                "kind TEXT NOT NULL, payload TEXT NOT NULL, timestamp TEXT NOT NULL, "
                "previous_hash TEXT NOT NULL, hash TEXT NOT NULL UNIQUE)",
            )
            for statement in statements:
                db.execute(statement)
            for table in ("commits", "events"):
                for operation in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                        f"BEFORE {operation} ON {table} BEGIN "
                        f"SELECT RAISE(ABORT, '{table} are append-only'); END"
                    )
            if db.execute("SELECT 1 FROM branches WHERE name = 'main'").fetchone() is None:
                genesis = self._insert_commit(
                    [], _state({}), "Genesis", "1970-01-01T00:00:00.000000+00:00"
                )
                db.execute("INSERT INTO branches(name, head) VALUES ('main', ?)", (genesis,))
                self._append_event(
                    "branch.created",
                    {"branch": "main", "before": None, "head": genesis, "from_ref": None},
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Hold a process lock and a database write lock; nested calls use savepoints."""
        with self.lock:
            outer = not self.db.in_transaction
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
                savepoint = None
            else:
                self._savepoint += 1
                savepoint = f"dao_{self._savepoint}"
                self.db.execute(f"SAVEPOINT {savepoint}")
            try:
                yield self.db
            except BaseException:
                if outer:
                    self.db.rollback()
                else:
                    self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                try:
                    if outer:
                        self.db.commit()
                    else:
                        self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException:
                    if outer:
                        self.db.rollback()
                    raise

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def head(self, branch: str = "main") -> str:
        _branch_name(branch)
        with self.lock:
            row = self.db.execute("SELECT head FROM branches WHERE name = ?", (branch,)).fetchone()
            if row is None:
                raise NotFoundError(f"Branch not found: {branch}")
            return row["head"]

    def _resolve(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref:
            raise ValueError("Reference must be a nonempty string")
        row = self.db.execute("SELECT head FROM branches WHERE name = ?", (ref,)).fetchone()
        if row is not None:
            return row["head"]
        if self.db.execute("SELECT 1 FROM commits WHERE id = ?", (ref,)).fetchone() is not None:
            return ref
        raise NotFoundError(f"Reference not found: {ref}")

    def _read_commit(self, commit_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM commits WHERE id = ?", (commit_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Commit not found: {commit_id}")
        return {
            "id": row["id"],
            "parents": json.loads(row["parents"]),
            "state": json.loads(row["state"]),
            "message": row["message"],
            "created_at": row["created_at"],
        }

    def snapshot(self, ref: str = "main") -> dict[str, Any]:
        with self.lock:
            return self._read_commit(self._resolve(ref))["state"]

    def branches(self) -> dict[str, str]:
        with self.lock:
            return {
                row["name"]: row["head"]
                for row in self.db.execute("SELECT name, head FROM branches ORDER BY name")
            }

    def branch(self, name: str, from_ref: str = "main") -> str:
        _branch_name(name)
        with self.transaction() as db:
            commit_id = self._resolve(from_ref)
            if db.execute("SELECT 1 FROM branches WHERE name = ?", (name,)).fetchone():
                raise ConflictError(f"Branch already exists: {name}")
            db.execute("INSERT INTO branches(name, head) VALUES (?, ?)", (name, commit_id))
            self._append_event(
                "branch.created",
                {"branch": name, "before": None, "head": commit_id, "from_ref": from_ref},
            )
            return commit_id

    def _check_head(self, branch: str, expected_head: str) -> str:
        if not isinstance(expected_head, str) or not expected_head:
            raise ValueError("expected_head must be the previously observed commit id")
        actual = self.head(branch)
        if actual != expected_head:
            raise ConflictError(f"Stale branch {branch}: expected {expected_head}, found {actual}")
        return actual

    def _insert_commit(
        self, parents: list[str], state: dict[str, Any], message: str, created_at: str | None = None
    ) -> str:
        if not isinstance(message, str):
            raise ValueError("Commit message must be a string")
        payload = {
            "parents": parents,
            "state": state,
            "message": message,
            "created_at": created_at or _timestamp(),
        }
        commit_id = _digest(payload)
        self.db.execute(
            "INSERT OR IGNORE INTO commits VALUES (?, ?, ?, ?, ?)",
            (
                commit_id,
                canonical_json(parents),
                canonical_json(state),
                message,
                payload["created_at"],
            ),
        )
        return commit_id

    def _advance(self, branch: str, commit_id: str, expected_head: str) -> None:
        result = self.db.execute(
            "UPDATE branches SET head = ? WHERE name = ? AND head = ?",
            (commit_id, branch, expected_head),
        )
        if result.rowcount != 1:
            raise ConflictError(f"Branch changed while writing: {branch}")

    def commit(self, branch: str, state: dict[str, Any], message: str, expected_head: str) -> str:
        state = _state(state)
        with self.transaction():
            parent = self._check_head(branch, expected_head)
            commit_id = self._insert_commit([parent], state, message)
            self._advance(branch, commit_id, parent)
            self._append_event(
                "state.committed", {"branch": branch, "before": parent, "head": commit_id}
            )
            return commit_id

    def log(self, branch: str = "main", limit: int = 50) -> list[dict[str, Any]]:
        _limit(limit)
        with self.transaction():
            pending = [self._resolve(branch)]
            visited = set()
            result = []
            while pending and len(result) < limit:
                commit_id = pending.pop()
                if commit_id in visited:
                    continue
                visited.add(commit_id)
                entry = self._read_commit(commit_id)
                result.append(entry)
                pending.extend(reversed(entry["parents"]))
            return result

    def diff(self, a: str, b: str) -> list[dict[str, Any]]:
        with self.transaction():
            before, after = self.snapshot(a), self.snapshot(b)
        changes = []

        def walk(old: Any, new: Any, path: str) -> None:
            if _equal(old, new):
                return
            if isinstance(old, dict) and isinstance(new, dict):
                for key in sorted(set(old) | set(new)):
                    walk(old.get(key, _MISSING), new.get(key, _MISSING), _pointer(path, key))
            else:
                operation = "add" if old is _MISSING else "remove" if new is _MISSING else "replace"
                changes.append(
                    {
                        "path": path or "/",
                        "op": operation,
                        "before": None if old is _MISSING else old,
                        "after": None if new is _MISSING else new,
                    }
                )

        walk(before, after, "")
        return changes

    def _ancestors(self, commit_id: str) -> dict[str, int]:
        distances = {}
        pending = deque([(commit_id, 0)])
        while pending:
            current, distance = pending.popleft()
            if current in distances:
                continue
            distances[current] = distance
            pending.extend(
                (parent, distance + 1) for parent in self._read_commit(current)["parents"]
            )
        return distances

    def merge(self, branch: str, source: str, expected_head: str) -> str:
        with self.transaction():
            ours = self._check_head(branch, expected_head)
            theirs = self._resolve(source)
            ours_ancestors, theirs_ancestors = self._ancestors(ours), self._ancestors(theirs)
            common = set(ours_ancestors) & set(theirs_ancestors)
            if not common:
                raise ConflictError("Branches have no common ancestor")
            # Shortest distance alone can select an older ancestor when merge
            # parents introduce shortcuts. Keep only maximal common ancestors.
            bases = set(common)
            pending = deque(
                parent for item in common for parent in self._read_commit(item)["parents"]
            )
            visited = set()
            while pending:
                ancestor = pending.popleft()
                if ancestor in visited:
                    continue
                visited.add(ancestor)
                bases.discard(ancestor)
                pending.extend(self._read_commit(ancestor)["parents"])
            if len(bases) != 1:
                raise ConflictError("Multiple merge bases require explicit adjudication")
            base = bases.pop()
            conflicts: list[str] = []
            state = _merge(
                self.snapshot(base), self.snapshot(ours), self.snapshot(theirs), "", conflicts
            )
            if conflicts:
                raise ConflictError("Merge conflicts at " + ", ".join(conflicts))
            parents = list(dict.fromkeys([ours, theirs]))
            commit_id = self._insert_commit(parents, _state(state), f"Merge {source} into {branch}")
            self._advance(branch, commit_id, ours)
            self._append_event(
                "state.merged",
                {
                    "branch": branch,
                    "before": ours,
                    "head": commit_id,
                    "source": source,
                    "source_head": theirs,
                    "base": base,
                },
            )
            return commit_id

    def revert(self, branch: str, commit_id: str, expected_head: str) -> str:
        """Restore an old snapshot in a new commit, retaining intervening history."""
        with self.transaction():
            parent = self._check_head(branch, expected_head)
            restored = self._read_commit(commit_id)["state"]
            new_id = self._insert_commit([parent], restored, f"Restore snapshot {commit_id}")
            self._advance(branch, new_id, parent)
            self._append_event(
                "state.restored",
                {"branch": branch, "before": parent, "head": new_id, "restore_from": commit_id},
            )
            return new_id

    def append_event(self, kind: str, payload: Any) -> dict[str, Any]:
        """Append an application event; Store exclusively emits reference transitions."""
        if isinstance(kind, str) and kind in _REF_EVENT_KINDS:
            raise ValueError("Branch and state event kinds are reserved for Store mutations")
        return self._append_event(kind, payload)

    def _append_event(self, kind: str, payload: Any) -> dict[str, Any]:
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("Event kind must be a nonempty string")
        payload = json.loads(canonical_json(payload))
        with self.transaction() as db:
            previous = db.execute(
                "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            event = {
                "seq": 1 if previous is None else previous["seq"] + 1,
                "kind": kind,
                "payload": payload,
                "timestamp": _timestamp(),
                "previous_hash": _CHAIN_START if previous is None else previous["hash"],
            }
            event["hash"] = _digest(event)
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event["seq"],
                    kind,
                    canonical_json(payload),
                    event["timestamp"],
                    event["previous_hash"],
                    event["hash"],
                ),
            )
            return event

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the latest events, ordered chronologically within the requested window."""
        _limit(limit)
        with self.lock:
            rows = self.db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))
            result = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
            return list(reversed(result))

    def _replay_ref_event(
        self, kind: str, payload: Any, refs: dict[str, str], commits: dict[str, dict[str, Any]]
    ) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Reference transition payload must be an object")
        branch = _branch_name(payload["branch"])
        head, before = payload["head"], payload["before"]
        if not isinstance(head, str) or head not in commits:
            raise ValueError("Reference transition head does not identify a valid commit")
        entry = commits[head]

        def resolve(ref: Any) -> str:
            if not isinstance(ref, str):
                raise ValueError("Reference transition source must be a string")
            resolved = refs.get(ref, ref)
            if resolved not in commits:
                raise ValueError("Reference transition source does not exist")
            return resolved

        if kind == "branch.created":
            if before is not None or branch in refs:
                raise ValueError("Branch creation must start from an absent reference")
            source = payload["from_ref"]
            if source is None:
                if (
                    branch != "main"
                    or refs
                    or entry["parents"]
                    or not _equal(entry["state"], _state({}))
                ):
                    raise ValueError("Initial main reference must point to the genesis snapshot")
            elif resolve(source) != head:
                raise ValueError("Created branch does not match its recorded source")
        else:
            if not isinstance(before, str) or refs.get(branch) != before:
                raise ValueError("Reference transition predecessor does not match the journal")
            parents = [before]
            if kind == "state.merged":
                source = resolve(payload["source"])
                if source != payload["source_head"]:
                    raise ValueError("Merge source head does not match its recorded source")
                parents = list(dict.fromkeys([before, source]))
                base = payload["base"]
                if (
                    not isinstance(base, str)
                    or base not in self._ancestors(before)
                    or base not in self._ancestors(source)
                ):
                    raise ValueError("Merge base is not a common ancestor")
            if entry["parents"] != parents:
                raise ValueError("Reference transition does not match commit parents")
            if kind == "state.restored":
                restored = resolve(payload["restore_from"])
                if not _equal(entry["state"], commits[restored]["state"]):
                    raise ValueError("Restored commit does not match its recorded snapshot")
        refs[branch] = head

    def verify_integrity(self) -> dict[str, Any]:
        """Check hashes, ancestry, event chain, and refs against journaled transitions."""
        with self.transaction() as db:
            issues = []
            rows = list(db.execute("SELECT * FROM commits"))
            ids = {row["id"] for row in rows}
            commits = {}
            for row in rows:
                try:
                    entry = self._read_commit(row["id"])
                    commits[entry["id"]] = entry
                    payload = {
                        key: entry[key] for key in ("parents", "state", "message", "created_at")
                    }
                    if _digest(payload) != entry["id"]:
                        issues.append(f"Commit hash mismatch: {entry['id']}")
                    if not isinstance(entry["parents"], list) or not all(
                        isinstance(parent, str) for parent in entry["parents"]
                    ):
                        issues.append(f"Invalid commit parents: {entry['id']}")
                    else:
                        for parent in entry["parents"]:
                            if parent not in ids:
                                issues.append(f"Dangling parent: {entry['id']} -> {parent}")
                    if not _equal(_state(entry["state"]), entry["state"]):
                        issues.append(f"Invalid commit state: {entry['id']}")
                except (ValueError, TypeError, KeyError) as exc:
                    issues.append(f"Invalid commit {row['id']}: {exc}")
            actual_refs = {}
            for row in db.execute("SELECT name, head FROM branches"):
                actual_refs[row["name"]] = row["head"]
                if row["head"] not in ids:
                    issues.append(f"Dangling branch: {row['name']}")
            events = list(db.execute("SELECT * FROM events ORDER BY seq"))
            previous_hash = _CHAIN_START
            projected_refs: dict[str, str] = {}
            for expected_seq, row in enumerate(events, 1):
                if row["seq"] != expected_seq:
                    issues.append(f"Event sequence gap at {expected_seq}")
                if row["previous_hash"] != previous_hash:
                    issues.append(f"Event chain mismatch at {row['seq']}")
                try:
                    event = {**dict(row), "payload": json.loads(row["payload"])}
                    expected_hash = event.pop("hash")
                    if _digest(event) != expected_hash:
                        issues.append(f"Event hash mismatch at {row['seq']}")
                    if event["kind"] in _REF_EVENT_KINDS:
                        self._replay_ref_event(
                            event["kind"], event["payload"], projected_refs, commits
                        )
                except (ValueError, TypeError, KeyError) as exc:
                    issues.append(f"Invalid event {row['seq']}: {exc}")
                previous_hash = row["hash"]
            for branch in sorted(set(actual_refs) | set(projected_refs)):
                if actual_refs.get(branch) != projected_refs.get(branch):
                    issues.append(f"Branch reference mismatch: {branch}")
            trigger_names = {
                row[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            }
            for table in ("commits", "events"):
                for operation in ("update", "delete"):
                    if f"{table}_no_{operation}" not in trigger_names:
                        issues.append(f"Missing append-only trigger: {table}_no_{operation}")
            for row in db.execute("PRAGMA quick_check"):
                if row[0] != "ok":
                    issues.append(f"SQLite integrity: {row[0]}")
            return {"ok": not issues, "issues": issues, "commits": len(rows), "events": len(events)}
