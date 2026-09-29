"""Durable idempotency records and bounded operation reconciliation."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from .identity import opaque_id
from .protocol import ProtocolError, canonical_digest, redact_text
from .storage import StateStore


TERMINAL_STATES = frozenset({"observed_return", "succeeded", "failed", "unknown"})
ACTIVE_STATES = frozenset({"accepted", "dispatched"})


def _now() -> float:
    return time.time()


def iso_timestamp(value: float | None = None) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(_now() if value is None else value, timezone.utc).isoformat().replace("+00:00", "Z")


class OperationConflict(ProtocolError):
    def __init__(self, message: str):
        super().__init__(message, 409, "request_id_conflict")


class OperationJournal:
    MAX_OPERATIONS = 256

    def __init__(self, store: StateStore, clock=_now):
        self.store = store
        self.clock = clock
        self.lock = threading.RLock()
        self._mark_incomplete_unknown()

    def _mark_incomplete_unknown(self) -> None:
        def mutate(state: dict[str, Any]) -> None:
            operations = state.setdefault("operations", {})
            for operation in operations.values():
                if operation.get("state") in ACTIVE_STATES:
                    operation["state"] = "unknown"
                    operation["reason"] = "host reloaded before the operation could be reconciled"
                    operation["updated_at"] = iso_timestamp(self.clock())

        self.store.mutate(mutate)

    def begin(self, client_id: str, request_id: str, kind: str, body: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        digest = canonical_digest(body)
        with self.lock:
            def mutate(state: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
                operations = state.setdefault("operations", {})
                for operation in operations.values():
                    if operation.get("client_id") == client_id and operation.get("request_id") == request_id:
                        if operation.get("body_digest") != digest:
                            raise OperationConflict("request_id was already used with a different body")
                        return False, self.public(operation)
                operation_id = opaque_id("op-")
                stamp = iso_timestamp(self.clock())
                operation = {
                    "id": operation_id,
                    "client_id": client_id,
                    "request_id": request_id,
                    "kind": kind,
                    "body_digest": digest,
                    "state": "accepted",
                    "created_at": stamp,
                    "updated_at": stamp,
                    "outcome": None,
                    "reason": None,
                }
                operations[operation_id] = operation
                self._prune_dict(operations)
                return True, self.public(operation)

            return self.store.mutate(mutate)

    def lookup(self, client_id: str, request_id: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """Return an existing request before target preflight can reject a retry."""
        digest = canonical_digest(body)
        for operation in self.store.get("operations", {}).values():
            if operation.get("client_id") == client_id and operation.get("request_id") == request_id:
                if operation.get("body_digest") != digest:
                    raise OperationConflict("request_id was already used with a different body")
                return self.public(operation)
        return None

    def internal(self, kind: str, body: dict[str, Any], owner: str = "host") -> dict[str, Any]:
        with self.lock:
            operation_id = opaque_id("op-")
            stamp = iso_timestamp(self.clock())
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            operation = {
                "id": operation_id,
                "client_id": owner,
                "request_id": opaque_id("internal-"),
                "kind": kind,
                "body_digest": digest,
                "state": "accepted",
                "created_at": stamp,
                "updated_at": stamp,
                "outcome": None,
                "reason": None,
            }

            def mutate(state: dict[str, Any]) -> None:
                operations = state.setdefault("operations", {})
                operations[operation_id] = operation
                self._prune_dict(operations)

            self.store.mutate(mutate)
            return self.public(operation)

    def update(self, operation_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            def mutate(state: dict[str, Any]) -> dict[str, Any]:
                operation = state.setdefault("operations", {}).get(operation_id)
                if operation is None:
                    raise KeyError(operation_id)
                allowed = {"state", "outcome", "reason", "preview_id", "restore_state", "restored", "resolved_by", "target"}
                for key, value in changes.items():
                    if key in allowed:
                        operation[key] = redact_text(value) if key in {"outcome", "reason", "resolved_by", "restore_state"} else value
                operation["updated_at"] = iso_timestamp(self.clock())
                return self.public(operation)

            return self.store.mutate(mutate)

    def get(self, operation_id: str, client_id: str | None = None) -> dict[str, Any] | None:
        operations = self.store.get("operations", {})
        operation = operations.get(operation_id)
        if not isinstance(operation, dict):
            return None
        if client_id is not None and operation.get("client_id") not in {client_id, "host"}:
            return None
        return self.public(operation)

    def cancel_for_client(self, client_id: str, kind_prefix: str, reason: str) -> list[str]:
        cancelled: list[str] = []

        def mutate(state: dict[str, Any]) -> None:
            for operation in state.setdefault("operations", {}).values():
                if operation.get("client_id") == client_id and operation.get("kind", "").startswith(kind_prefix) and operation.get("state") == "accepted":
                    operation["state"] = "failed"
                    operation["reason"] = reason[:256]
                    operation["updated_at"] = iso_timestamp(self.clock())
                    cancelled.append(operation["id"])

        self.store.mutate(mutate)
        return cancelled

    def cancel_kind(self, kind_prefix: str, reason: str) -> list[str]:
        cancelled: list[str] = []

        def mutate(state: dict[str, Any]) -> None:
            for operation in state.setdefault("operations", {}).values():
                if operation.get("kind", "").startswith(kind_prefix) and operation.get("state") == "accepted":
                    operation["state"] = "failed"
                    operation["reason"] = reason[:256]
                    operation["updated_at"] = iso_timestamp(self.clock())
                    cancelled.append(operation["id"])

        self.store.mutate(mutate)
        return cancelled

    def active(self, kind_prefix: str | None = None) -> list[dict[str, Any]]:
        operations = self.store.get("operations", {})
        return [
            self.public(operation)
            for operation in operations.values()
            if operation.get("state") in ACTIVE_STATES
            and (kind_prefix is None or operation.get("kind", "").startswith(kind_prefix))
        ]

    @staticmethod
    def public(operation: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: operation.get(key)
            for key in ("id", "request_id", "state", "kind", "created_at", "updated_at", "outcome", "reason", "preview_id", "restore_state", "restored", "resolved_by", "target")
            if key in operation
        }
        return result

    def _prune_dict(self, operations: dict[str, Any]) -> None:
        if len(operations) <= self.MAX_OPERATIONS:
            return
        terminal = sorted(
            (operation for operation in operations.values() if operation.get("state") in TERMINAL_STATES),
            key=lambda operation: operation.get("updated_at", ""),
        )
        for operation in terminal[: max(0, len(operations) - self.MAX_OPERATIONS)]:
            operations.pop(operation["id"], None)
