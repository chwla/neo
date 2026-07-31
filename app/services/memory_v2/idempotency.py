"""Stable, owner-scoped Phase 3 idempotency keys."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.core.identifiers import canonical_uuid
from app.services.memory_v2.contracts import MemoryOperationKind


def _key(owner_id: str, surface: str, material: dict[str, Any]) -> str:
    payload = {
        "owner_id": canonical_uuid(owner_id),
        "surface": surface,
        **material,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"phase3:{surface}:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class MemoryV2Idempotency:
    @staticmethod
    def http(owner_id: str, request_id: str, operation: MemoryOperationKind | str) -> str:
        return _key(owner_id, "http", {"request_id": request_id, "operation": str(operation)})

    @staticmethod
    def review(owner_id: str, candidate_id: str, revision: int, action: str) -> str:
        return _key(
            owner_id,
            "review",
            {"candidate_id": candidate_id, "revision": revision, "action": action},
        )

    @staticmethod
    def chat(
        owner_id: str,
        message_id: str,
        extraction_version: str,
        candidate_key: str,
    ) -> str:
        return _key(
            owner_id,
            "chat",
            {
                "message_id": message_id,
                "extraction_version": extraction_version,
                "candidate_key": candidate_key,
            },
        )

    @staticmethod
    def source_change(
        owner_id: str,
        message_id: str,
        revision: int,
        affected_id: str,
        action: str,
    ) -> str:
        return _key(
            owner_id,
            "source_change",
            {
                "message_id": message_id,
                "revision": revision,
                "affected_id": affected_id,
                "action": action,
            },
        )

    @staticmethod
    def imported(owner_id: str, batch_id: str, item_hash: str) -> str:
        return _key(owner_id, "import", {"batch_id": batch_id, "item_hash": item_hash})

    @staticmethod
    def agent(owner_id: str, tool_call_id: str) -> str:
        return _key(owner_id, "agent", {"tool_call_id": tool_call_id})

    @staticmethod
    def maintenance(owner_id: str, run_id: str, command_hash: str) -> str:
        return _key(owner_id, "maintenance", {"run_id": run_id, "command_hash": command_hash})

    @staticmethod
    def manual(owner_id: str, client_mutation_id: str) -> str:
        return _key(owner_id, "manual", {"client_mutation_id": client_mutation_id})
