"""Bounded structured diagnostics with no raw memory text or model reasoning."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Protocol

from app.services.memory_v2.extraction_contracts import ExtractionDiagnostic


class ExtractionDiagnosticSink(Protocol):
    def record(self, diagnostic: ExtractionDiagnostic) -> None: ...


class InMemoryExtractionDiagnostics:
    def __init__(self, *, maximum_entries: int = 500) -> None:
        if not 1 <= maximum_entries <= 10_000:
            raise ValueError("diagnostic_limit_out_of_range")
        self._items: deque[ExtractionDiagnostic] = deque(maxlen=maximum_entries)

    def record(self, diagnostic: ExtractionDiagnostic) -> None:
        self._items.append(diagnostic)

    def snapshot(self) -> tuple[ExtractionDiagnostic, ...]:
        return tuple(self._items)


class StructuredExtractionDiagnosticSink:
    """Adapter for a structured event function; diagnostic contracts contain no raw text."""

    def __init__(self, emitter: Callable[[dict], None]) -> None:
        self._emitter = emitter

    def record(self, diagnostic: ExtractionDiagnostic) -> None:
        self._emitter(diagnostic.model_dump(mode="json"))
