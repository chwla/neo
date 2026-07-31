"""Shared stable-identifier validation with no persistence dependencies."""

from __future__ import annotations

from uuid import UUID


def canonical_uuid(value: str | UUID) -> str:
    """Return the canonical lowercase UUID string or raise ``ValueError``."""

    try:
        return str(UUID(str(value).strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("canonical_uuid_required") from exc
