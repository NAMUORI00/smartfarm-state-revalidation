from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .contracts import Barrier, Condition, StatePoint


@dataclass(frozen=True, slots=True)
class TraceEvent:
    sequence: int
    barrier: Barrier
    condition: Condition
    refresh_count: int
    state_point: StatePoint | None = None
    state_revision: int | None = None
    state_hash: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "barrier": self.barrier.value,
            "condition": self.condition.value,
            "refresh_count": self.refresh_count,
            "state_point": self.state_point.value if self.state_point is not None else None,
            "state_revision": self.state_revision,
            "state_hash": self.state_hash,
            "details": dict(self.details),
        }


class TraceSink(Protocol):
    def record(
        self,
        barrier: Barrier,
        condition: Condition,
        refresh_count: int,
        *,
        state_point: StatePoint | None = None,
        state_revision: int | None = None,
        state_hash: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> TraceEvent: ...


class TraceRecorder:
    def __init__(self) -> None:
        self._events: list[TraceEvent] = []
        self._lock = Lock()

    def record(
        self,
        barrier: Barrier,
        condition: Condition,
        refresh_count: int,
        *,
        state_point: StatePoint | None = None,
        state_revision: int | None = None,
        state_hash: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> TraceEvent:
        with self._lock:
            event = TraceEvent(
                sequence=len(self._events) + 1,
                barrier=barrier,
                condition=condition,
                refresh_count=refresh_count,
                state_point=state_point,
                state_revision=state_revision,
                state_hash=state_hash,
                details=details or {},
            )
            self._events.append(event)
            return event

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        with self._lock:
            return tuple(self._events)

