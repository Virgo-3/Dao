"""Explicit, operator-installed external effect contracts.

Executors enforce their service's preconditions at dispatch. Reconciliation is
read-only and must never send the effect again. Registry access is a trusted
local capability, not a tool granted to the conversational provider.
"""

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from .store import canonical_json


def _cost(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError("cost must be a finite nonnegative decimal")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("cost must be a finite nonnegative decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("cost must be a finite nonnegative decimal")
    return value


def validate_effect(effect):
    required = {"executor", "arguments", "preconditions", "reservation_usd"}
    if not isinstance(effect, dict) or not required <= set(effect) or set(effect) - required - {"binding"}:
        raise ValueError("effect requires executor, arguments, preconditions, reservation_usd")
    if not isinstance(effect["executor"], str) or not effect["executor"].strip():
        raise ValueError("executor must be a nonempty string")
    if not all(isinstance(effect[key], dict) for key in ("arguments", "preconditions")):
        raise ValueError("executor arguments and preconditions must be objects")
    _cost(effect["reservation_usd"])
    if "binding" in effect and (not isinstance(effect["binding"], dict) or not effect["binding"]):
        raise ValueError("executor binding must be a nonempty object")
    return json.loads(canonical_json(effect))


def validate_receipt(receipt):
    if not isinstance(receipt, dict) or set(receipt) != {"status", "evidence", "cost_usd"}:
        raise ValueError("receipt requires status, evidence, and cost_usd")
    if receipt["status"] not in {"succeeded", "not_executed", "unknown"}:
        raise ValueError("receipt status must be succeeded, not_executed, or unknown")
    if not isinstance(receipt["evidence"], dict) or not receipt["evidence"]:
        raise ValueError("receipt requires nonempty outcome evidence")
    _cost(receipt["cost_usd"])
    return json.loads(canonical_json(receipt))


class Executor(Protocol):
    binding: dict
    """Non-secret version, endpoint, account, and behavior-relevant configuration identifiers."""

    def execute(self, effect: dict, idempotency_key: str) -> dict:
        """Enforce reviewed preconditions and return a measured outcome receipt."""

    def reconcile(self, effect: dict, idempotency_key: str) -> dict:
        """Inspect an earlier dispatch without dispatching again."""


class ArtifactExecutor:
    """A zero-cost external filesystem example confined to an operator's directory.

    A new, key-named JSON artifact is the only permitted effect. Exclusive
    creation enforces target absence. A partial or conflicting file remains
    unknown and is never overwritten. No filesystem executor is installed by
    CLI or MCP; an operator must explicitly construct and register this one.
    """

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.binding = {"version": "dao-artifact/1", "directory": str(self.directory)}

    def _request(self, effect, key):
        validate_effect(effect)
        if canonical_json(effect.get("binding")) != canonical_json(self.binding):
            raise ValueError("artifact directory differs from the reviewed executor binding")
        if (set(effect["arguments"]) != {"content"}
                or set(effect["preconditions"]) != {"absent"}
                or effect["preconditions"]["absent"] is not True):
            raise ValueError("artifact requires content and the absent precondition")
        name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        path = self.directory / name
        body = canonical_json({"idempotency_key": key, "content": effect["arguments"]["content"]})
        return path, body

    def execute(self, effect, idempotency_key):
        path, body = self._request(effect, idempotency_key)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as output:
                output.write(body)
                output.flush()
                import os

                os.fsync(output.fileno())
        except FileExistsError:
            return self.reconcile(effect, idempotency_key)
        return self.reconcile(effect, idempotency_key)

    def reconcile(self, effect, idempotency_key):
        path, body = self._request(effect, idempotency_key)
        try:
            actual = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"status": "not_executed", "evidence": {"path": str(path), "absent": True},
                    "cost_usd": "0"}
        status = "succeeded" if actual == body else "unknown"
        return {"status": status, "evidence": {"path": str(path),
                "sha256": hashlib.sha256(actual.encode("utf-8")).hexdigest()}, "cost_usd": "0"}
