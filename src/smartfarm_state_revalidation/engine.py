from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from typing import Protocol

from .contracts import (
    AnswerUnit,
    Barrier,
    ExecutionResult,
    FailureCode,
    GenerationRequest,
    RelatedStateSpec,
    ScheduledPatch,
    StateCapture,
    StateDiff,
    StatePatch,
    StatePoint,
    StateClaim,
    StateSnapshot,
    StructuredResponse,
)
from .dependency import affected_unit_ids
from .executor import ActionExecutor
from .fingerprint import compare_related, fingerprint
from .generator import ResponseGenerator
from .policies import RevalidationPolicy
from .trace import TraceEvent, TraceRecorder, TraceSink


class StateReader(Protocol):
    def read(self) -> StateSnapshot: ...


class StateMutator(Protocol):
    def apply_patch(self, patch: StatePatch) -> StateSnapshot: ...


class BarrierInjector(Protocol):
    def apply(self, barrier: Barrier) -> int: ...


class InMemoryStateStore:
    """Thread-safe state source used only by isolated experiments and tests."""

    def __init__(self, initial: StateSnapshot) -> None:
        self._state = initial
        self._lock = Lock()

    def read(self) -> StateSnapshot:
        with self._lock:
            return StateSnapshot(
                revision=self._state.revision,
                values=self._state.values,
                observed_at=self._state.observed_at,
            )

    def apply_patch(self, patch: StatePatch) -> StateSnapshot:
        with self._lock:
            values = dict(self._state.values)
            values.update(patch.values)
            for key in patch.remove_keys:
                values.pop(key, None)
            observed_at = patch.observed_at or datetime.now(timezone.utc)
            self._state = StateSnapshot(
                revision=self._state.revision + patch.revision_delta,
                values=values,
                observed_at=observed_at,
            )
            return self._state


class BarrierPatchInjector:
    def __init__(
        self,
        mutator: StateMutator,
        patches: tuple[ScheduledPatch, ...] | list[ScheduledPatch],
    ) -> None:
        self._mutator = mutator
        self._occurrences: dict[Barrier, int] = defaultdict(int)
        self._patches = tuple(
            sorted(patches, key=lambda item: (item.barrier.value, item.occurrence, item.order))
        )

    def apply(self, barrier: Barrier) -> int:
        self._occurrences[barrier] += 1
        occurrence = self._occurrences[barrier]
        selected = [
            item
            for item in self._patches
            if item.barrier is barrier and item.occurrence == occurrence
        ]
        for item in selected:
            self._mutator.apply_patch(item.patch)
        return len(selected)


def _latest_summary_units(
    related_state: RelatedStateSpec,
    snapshot: StateSnapshot,
) -> tuple[AnswerUnit, ...]:
    """선언된 단위 구성 그대로 최신 스냅샷 요약을 만든다."""
    unit_by_key = {item.key: item for item in related_state.dependencies}
    units: list[AnswerUnit] = []
    for spec in related_state.answer_units():
        claims = tuple(
            StateClaim(
                key=key,
                value=snapshot.values.get(key),
                unit=unit_by_key[key].unit if key in unit_by_key else None,
                observed_revision=snapshot.revision,
            )
            for key in spec.depends_on
        )
        units.append(
            AnswerUnit(
                unit_id=spec.unit_id,
                depends_on=spec.depends_on,
                claims=claims,
                text=", ".join(f"{claim.key}={claim.value}" for claim in claims),
                source_revision=snapshot.revision,
                revised=True,
            )
        )
    return tuple(units)


@dataclass(frozen=True, slots=True)
class RunResult:
    condition: str
    response: StructuredResponse
    s0: StateCapture
    s_release: StateCapture
    s_exec: StateCapture
    release_diff: StateDiff
    execution: ExecutionResult
    refresh_count: int
    action_blocked: bool
    failure_code: FailureCode | None
    trace: tuple[TraceEvent, ...]

    @property
    def applied(self) -> bool:
        return self.execution.applied


class ExperimentEngine:
    def __init__(
        self,
        state_reader: StateReader,
        generator: ResponseGenerator,
        executor: ActionExecutor,
        *,
        trace_sink: TraceSink | None = None,
        injector: BarrierInjector | None = None,
    ) -> None:
        self._state_reader = state_reader
        self._generator = generator
        self._executor = executor
        self._trace_sink = trace_sink or TraceRecorder()
        self._injector = injector

    def run(
        self,
        policy: RevalidationPolicy,
        *,
        question: str,
        related_state: RelatedStateSpec,
        evidence: tuple[str, ...] = (),
    ) -> RunResult:
        refresh_count = 0
        policy_observes_release = policy.max_refreshes > 0 or policy.block_action_on_release_change
        trace_start = (
            len(self._trace_sink.events)
            if isinstance(self._trace_sink, TraceRecorder)
            else 0
        )

        def reach(
            barrier: Barrier,
            *,
            capture: StateCapture | None = None,
            details: dict[str, object] | None = None,
        ) -> None:
            patch_count = self._injector.apply(barrier) if self._injector is not None else 0
            event_details = {"injected_patch_count": patch_count}
            if details:
                event_details.update(details)
            self._trace_sink.record(
                barrier,
                policy.condition,
                refresh_count,
                state_point=capture.point if capture is not None else None,
                state_revision=capture.snapshot.revision if capture is not None else None,
                state_hash=capture.state_hash if capture is not None else None,
                details=event_details,
            )

        initial_patch_count = self._injector.apply(Barrier.INITIAL_STATE_READ) if self._injector is not None else 0
        s0_snapshot = self._state_reader.read()
        s0 = StateCapture(StatePoint.S0, s0_snapshot, fingerprint(s0_snapshot, related_state))
        self._trace_sink.record(
            Barrier.INITIAL_STATE_READ,
            policy.condition,
            refresh_count,
            state_point=s0.point,
            state_revision=s0.snapshot.revision,
            state_hash=s0.state_hash,
            details={"injected_patch_count": initial_patch_count},
        )
        reach(Barrier.GENERATION_STARTED)
        response = self._generator.generate(
            GenerationRequest(
                question=question,
                evidence=evidence,
                related_state=related_state,
                snapshot=s0_snapshot,
                attempt=0,
            ),
            reach,
        )
        reach(Barrier.GENERATION_COMPLETED)

        reach(
            Barrier.POST_GENERATION_READ,
            details={"observer_only": not policy_observes_release},
        )
        release_snapshot = self._state_reader.read()
        release_patch_count = self._injector.apply(Barrier.RELEASE_CHECK) if self._injector is not None else 0
        if release_patch_count:
            release_snapshot = self._state_reader.read()
        release_diff = compare_related(s0_snapshot, release_snapshot, related_state)
        release_capture = StateCapture(
            StatePoint.S_RELEASE,
            release_snapshot,
            release_diff.after_hash,
        )
        self._trace_sink.record(
            Barrier.RELEASE_CHECK,
            policy.condition,
            refresh_count,
            state_point=release_capture.point,
            state_revision=release_capture.snapshot.revision,
            state_hash=release_capture.state_hash,
            details={
                "injected_patch_count": release_patch_count,
                "related_state_changed": release_diff.changed,
                "observer_only": not policy_observes_release,
            },
        )

        action_blocked = False
        failure_code: FailureCode | None = None
        if release_diff.changed and policy.max_refreshes > 0:
            refresh_count = 1
            revise_unit_ids: tuple[str, ...] = ()
            if policy.selective_revision:
                revise_unit_ids = affected_unit_ids(related_state, release_diff)
            reach(
                Barrier.REFRESH_GENERATION_STARTED,
                capture=release_capture,
                details={
                    "selective_revision": policy.selective_revision,
                    "revise_unit_ids": list(revise_unit_ids),
                    "declared_unit_count": len(related_state.answer_units()),
                },
            )
            response = self._generator.generate(
                GenerationRequest(
                    question=question,
                    evidence=evidence,
                    related_state=related_state,
                    snapshot=release_snapshot,
                    attempt=refresh_count,
                    revise_unit_ids=revise_unit_ids,
                    prior_units=response.units if policy.selective_revision else (),
                ),
                reach,
            )
            reach(
                Barrier.REFRESH_GENERATION_COMPLETED,
                details={
                    "revised_unit_count": len(response.revised_units),
                    "preserved_unit_count": len(response.preserved_units),
                },
            )
            reach(Barrier.POST_GENERATION_READ)
            refreshed_release_snapshot = self._state_reader.read()
            release_patch_count = self._injector.apply(Barrier.RELEASE_CHECK) if self._injector is not None else 0
            if release_patch_count:
                refreshed_release_snapshot = self._state_reader.read()
            second_diff = compare_related(
                release_snapshot,
                refreshed_release_snapshot,
                related_state,
            )
            release_snapshot = refreshed_release_snapshot
            release_diff = second_diff
            release_capture = StateCapture(
                StatePoint.S_RELEASE,
                release_snapshot,
                second_diff.after_hash,
            )
            self._trace_sink.record(
                Barrier.RELEASE_CHECK,
                policy.condition,
                refresh_count,
                state_point=release_capture.point,
                state_revision=release_capture.snapshot.revision,
                state_hash=release_capture.state_hash,
                details={
                    "injected_patch_count": release_patch_count,
                    "related_state_changed": second_diff.changed,
                },
            )
            if second_diff.changed:
                action_blocked = True
                failure_code = FailureCode.REFRESH_EXHAUSTED
                # 갱신 예산을 소진하면 조건과 무관하게 최신 상태 요약만 남기고
                # 행동 후보를 제거한다. 부분 보존은 여기서 적용하지 않는다.
                latest_units = _latest_summary_units(related_state, release_snapshot)
                latest_claims = tuple(
                    claim for unit in latest_units for claim in unit.claims
                )
                response = replace(
                    response,
                    text=", ".join(unit.text for unit in latest_units if unit.text),
                    state_claims=latest_claims,
                    action_candidate=None,
                    source_revision=release_snapshot.revision,
                    units=latest_units,
                )
        elif release_diff.changed and policy.block_action_on_release_change:
            action_blocked = True
            failure_code = FailureCode.RELEASE_STATE_CHANGED
            response = replace(response, action_candidate=None)

        reach(Barrier.APPROVAL_EVENT)
        execution_patch_count = self._injector.apply(Barrier.EXECUTION_CHECK) if self._injector is not None else 0
        checked_snapshot = self._state_reader.read()
        checked_capture = StateCapture(
            StatePoint.S_EXEC,
            checked_snapshot,
            fingerprint(checked_snapshot, related_state),
        )
        self._trace_sink.record(
            Barrier.EXECUTION_CHECK,
            policy.condition,
            refresh_count,
            state_point=checked_capture.point,
            state_revision=checked_capture.snapshot.revision,
            state_hash=checked_capture.state_hash,
            details={
                "injected_patch_count": execution_patch_count,
                "observer_only": not policy.enforce_execution_preconditions,
            },
        )
        candidate = None if action_blocked else response.action_candidate
        apply_patch_count = self._injector.apply(Barrier.ACTION_APPLY) if self._injector is not None else 0
        exec_snapshot = self._state_reader.read()
        s_exec = StateCapture(
            StatePoint.S_EXEC,
            exec_snapshot,
            fingerprint(exec_snapshot, related_state),
        )
        self._trace_sink.record(
            Barrier.ACTION_APPLY,
            policy.condition,
            refresh_count,
            state_point=s_exec.point,
            state_revision=s_exec.snapshot.revision,
            state_hash=s_exec.state_hash,
            details={
                "injected_patch_count": apply_patch_count,
                "action_blocked": action_blocked,
                "state_changed_after_execution_check": checked_capture.state_hash != s_exec.state_hash,
            },
        )
        execution = self._executor.execute(
            candidate,
            exec_snapshot,
            enforce_preconditions=policy.enforce_execution_preconditions,
        )
        if failure_code is None:
            failure_code = execution.failure_code

        trace = (
            self._trace_sink.events[trace_start:]
            if isinstance(self._trace_sink, TraceRecorder)
            else ()
        )
        return RunResult(
            condition=policy.condition.value,
            response=response,
            s0=s0,
            s_release=release_capture,
            s_exec=s_exec,
            release_diff=release_diff,
            execution=execution,
            refresh_count=refresh_count,
            action_blocked=action_blocked,
            failure_code=failure_code,
            trace=trace,
        )


RevalidationEngine = ExperimentEngine
