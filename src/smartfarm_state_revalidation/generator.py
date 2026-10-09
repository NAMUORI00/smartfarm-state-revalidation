from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .contracts import (
    ActionCandidate,
    AnswerUnit,
    Barrier,
    GenerationRequest,
    StateClaim,
    StructuredResponse,
)


BarrierCallback = Callable[[Barrier], None]
ActionFactory = Callable[[GenerationRequest], ActionCandidate | None]


class ResponseGenerator(Protocol):
    def generate(
        self,
        request: GenerationRequest,
        reach_barrier: BarrierCallback,
    ) -> StructuredResponse: ...


def _unit_text(claims: tuple[StateClaim, ...]) -> str:
    return ", ".join(f"{claim.key}={claim.value}" for claim in claims)


class SnapshotEchoGenerator:
    """Deterministic structured generator for causal mechanism tests.

    답변은 사전 선언된 단위 구성에 따라 조각으로 나뉜다. 갱신 요청이 수정 대상
    단위를 지정하면 그 단위만 현재 스냅샷으로 다시 만들고 나머지는 최초 생성
    결과를 그대로 보존한다. 보존된 단위는 원래의 관측 revision 을 유지한다.
    """

    def __init__(self, action_factory: ActionFactory | None = None) -> None:
        self._action_factory = action_factory
        self.call_count = 0
        self.generated_unit_count = 0

    def generate(
        self,
        request: GenerationRequest,
        reach_barrier: BarrierCallback,
    ) -> StructuredResponse:
        self.call_count += 1
        barrier = (
            Barrier.DURING_INITIAL_GENERATION
            if request.attempt == 0
            else Barrier.DURING_REFRESH_GENERATION
        )
        reach_barrier(barrier)

        units_by_id = {unit.unit_id: unit for unit in request.prior_units}
        revise = set(request.revise_unit_ids)
        unit_units_by_key = {
            dependency.key: dependency for dependency in request.related_state.dependencies
        }

        units: list[AnswerUnit] = []
        for spec in request.related_state.answer_units():
            preserved = units_by_id.get(spec.unit_id)
            if request.is_selective and preserved is not None and spec.unit_id not in revise:
                units.append(preserved)
                continue
            claims = tuple(
                StateClaim(
                    key=key,
                    value=request.snapshot.values.get(key),
                    unit=unit_units_by_key[key].unit if key in unit_units_by_key else None,
                    observed_revision=request.snapshot.revision,
                )
                for key in spec.depends_on
            )
            self.generated_unit_count += 1
            units.append(
                AnswerUnit(
                    unit_id=spec.unit_id,
                    depends_on=spec.depends_on,
                    claims=claims,
                    text=_unit_text(claims),
                    source_revision=request.snapshot.revision,
                    revised=request.attempt > 0,
                )
            )

        claims = tuple(claim for unit in units for claim in unit.claims)
        return StructuredResponse(
            text=", ".join(unit.text for unit in units if unit.text),
            state_claims=claims,
            action_candidate=(
                self._action_factory(request) if self._action_factory is not None else None
            ),
            source_revision=request.snapshot.revision,
            generation_id=f"generation-{self.call_count}",
            units=tuple(units),
        )
