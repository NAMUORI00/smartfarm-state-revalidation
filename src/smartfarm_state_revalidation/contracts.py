from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, TypeAlias


Scalar: TypeAlias = str | int | float | bool | None


class Condition(StrEnum):
    """비교 조건.

    B0/B1/B2 는 동결된 confirmatory-v1 실험이 사용한 식별자이므로 그대로 둔다.
    SR 은 v2 에서 추가한 선택적 수정 조건이며, 영향을 받은 답변 단위만 다시 만든다.
    """

    B0 = "B0"
    B1 = "B1"
    B2 = "B2"
    SR = "SR"


class StatePoint(StrEnum):
    S0 = "S0"
    S_RELEASE = "S_release"
    S_EXEC = "S_exec"


class Barrier(StrEnum):
    INITIAL_STATE_READ = "initial_state_read"
    GENERATION_STARTED = "generation_started"
    DURING_INITIAL_GENERATION = "during_initial_generation"
    GENERATION_COMPLETED = "generation_completed"
    POST_GENERATION_READ = "post_generation_read"
    REFRESH_GENERATION_STARTED = "refresh_generation_started"
    DURING_REFRESH_GENERATION = "during_refresh_generation"
    REFRESH_GENERATION_COMPLETED = "refresh_generation_completed"
    RELEASE_CHECK = "release_check"
    APPROVAL_EVENT = "approval_event"
    EXECUTION_CHECK = "execution_check"
    ACTION_APPLY = "action_apply"


class PredicateOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"


class FailureCode(StrEnum):
    NO_ACTION = "no_action"
    RELEASE_STATE_CHANGED = "release_state_changed"
    REFRESH_EXHAUSTED = "refresh_exhausted"
    PRECONDITION_FAILED = "precondition_failed"
    PRECONDITION_NOT_ENFORCED = "precondition_not_enforced"


def _immutable_mapping(values: Mapping[str, Scalar]) -> Mapping[str, Scalar]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class StateDependency:
    key: str
    tolerance: float = 0.0
    boundaries: tuple[float, ...] = ()
    unit: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("state dependency key must not be blank")
        if self.tolerance < 0:
            raise ValueError("state dependency tolerance must be non-negative")
        if tuple(sorted(set(self.boundaries))) != self.boundaries:
            raise ValueError("state dependency boundaries must be unique and sorted")
        if self.unit is not None and not self.unit.strip():
            raise ValueError("state dependency unit must not be blank")


@dataclass(frozen=True, slots=True)
class AnswerUnitSpec:
    """답변의 최소 수정 단위와 그 단위가 의존하는 상태키.

    의존관계는 생성 결과가 아니라 사전 선언된 프로파일에서만 나온다.
    프로토콜의 selection_rule.must_not_depend_on 제약을 코드에서 지키기 위한 것이다.
    """

    unit_id: str
    depends_on: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.unit_id.strip():
            raise ValueError("answer unit id must not be blank")
        if not self.depends_on:
            raise ValueError("answer unit must depend on at least one state key")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("answer unit dependencies must be unique")


@dataclass(frozen=True, slots=True)
class RelatedStateSpec:
    profile_id: str
    dependencies: tuple[StateDependency, ...]
    unit_specs: tuple[AnswerUnitSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("related-state profile id must not be blank")
        if not self.dependencies:
            raise ValueError("at least one related-state dependency is required")
        keys = tuple(item.key for item in self.dependencies)
        if len(keys) != len(set(keys)):
            raise ValueError("related-state dependency keys must be unique")
        if self.unit_specs:
            unit_ids = tuple(unit.unit_id for unit in self.unit_specs)
            if len(unit_ids) != len(set(unit_ids)):
                raise ValueError("answer unit ids must be unique")
            declared = set(keys)
            for unit in self.unit_specs:
                unknown = tuple(key for key in unit.depends_on if key not in declared)
                if unknown:
                    raise ValueError(
                        f"answer unit {unit.unit_id} depends on undeclared state keys: {unknown}"
                    )
            covered = {key for unit in self.unit_specs for key in unit.depends_on}
            missing = tuple(key for key in keys if key not in covered)
            if missing:
                raise ValueError(f"state keys not covered by any answer unit: {missing}")

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.dependencies)

    def answer_units(self) -> tuple[AnswerUnitSpec, ...]:
        """선언된 단위 구성을 돌려준다.

        선언이 없으면 상태키 하나가 곧 단위 하나인 기본 구성을 사용한다.
        v1 고정 입력은 단위를 선언하지 않으므로 이 기본값으로 기존 동작을 유지한다.
        """
        if self.unit_specs:
            return self.unit_specs
        return tuple(
            AnswerUnitSpec(unit_id=item.key, depends_on=(item.key,))
            for item in self.dependencies
        )


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    revision: int
    values: Mapping[str, Scalar]
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("state revision must be non-negative")
        observed_at = self.observed_at
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "observed_at", observed_at.astimezone(timezone.utc))
        object.__setattr__(self, "values", _immutable_mapping(self.values))


@dataclass(frozen=True, slots=True)
class StateCapture:
    point: StatePoint
    snapshot: StateSnapshot
    state_hash: str


@dataclass(frozen=True, slots=True)
class StateClaim:
    key: str
    value: Scalar
    unit: str | None = None
    observed_revision: int | None = None


@dataclass(frozen=True, slots=True)
class AnswerUnit:
    """생성된 답변의 한 조각과 그 조각이 근거로 삼은 상태 관측.

    revised 는 이번 실행에서 다시 생성됐는지를 나타낸다.
    보존된 단위는 최초 생성 시점의 source_revision 을 그대로 유지한다.
    """

    unit_id: str
    depends_on: tuple[str, ...]
    claims: tuple[StateClaim, ...]
    text: str
    source_revision: int
    revised: bool = False

    def __post_init__(self) -> None:
        if not self.unit_id.strip():
            raise ValueError("answer unit id must not be blank")
        if self.source_revision < 0:
            raise ValueError("answer unit source revision must be non-negative")


@dataclass(frozen=True, slots=True)
class ActionPrecondition:
    state_key: str
    operator: PredicateOperator
    expected: Scalar

    @property
    def key(self) -> str:
        return self.state_key


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    action: str
    target: str
    value: Scalar
    zone_id: str | None = None
    bound_state_revision: int | None = None
    preconditions: tuple[ActionPrecondition, ...] = ()

    def __post_init__(self) -> None:
        if not self.action.strip() or not self.target.strip():
            raise ValueError("action and target must not be blank")
        if self.bound_state_revision is not None and self.bound_state_revision < 0:
            raise ValueError("bound state revision must be non-negative")


@dataclass(frozen=True, slots=True)
class StructuredResponse:
    text: str
    state_claims: tuple[StateClaim, ...]
    action_candidate: ActionCandidate | None = None
    source_revision: int | None = None
    generation_id: str | None = None
    units: tuple[AnswerUnit, ...] = ()

    @property
    def revised_units(self) -> tuple[AnswerUnit, ...]:
        return tuple(unit for unit in self.units if unit.revised)

    @property
    def preserved_units(self) -> tuple[AnswerUnit, ...]:
        return tuple(unit for unit in self.units if not unit.revised)


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    question: str
    evidence: tuple[str, ...]
    related_state: RelatedStateSpec
    snapshot: StateSnapshot
    attempt: int
    revise_unit_ids: tuple[str, ...] = ()
    prior_units: tuple[AnswerUnit, ...] = ()

    def __post_init__(self) -> None:
        if self.revise_unit_ids and not self.prior_units:
            raise ValueError("selective revision requires the prior answer units")

    @property
    def is_selective(self) -> bool:
        return bool(self.revise_unit_ids)


@dataclass(frozen=True, slots=True)
class StatePatch:
    values: Mapping[str, Scalar] = field(default_factory=dict)
    remove_keys: tuple[str, ...] = ()
    revision_delta: int = 1
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.revision_delta < 0:
            raise ValueError("revision delta must be non-negative")
        object.__setattr__(self, "values", _immutable_mapping(self.values))


@dataclass(frozen=True, slots=True)
class ScheduledPatch:
    barrier: Barrier
    patch: StatePatch
    occurrence: int = 1
    order: int = 0

    def __post_init__(self) -> None:
        if self.occurrence < 1:
            raise ValueError("scheduled patch occurrence must be at least one")


@dataclass(frozen=True, slots=True)
class FieldChange:
    key: str
    before: Scalar
    after: Scalar
    reason: str


@dataclass(frozen=True, slots=True)
class StateDiff:
    before_hash: str
    after_hash: str
    changes: tuple[FieldChange, ...]

    @property
    def changed(self) -> bool:
        return bool(self.changes)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    dry_run: bool
    applied: bool
    valid: bool | None
    checked_revision: int
    failed_preconditions: tuple[ActionPrecondition, ...] = ()
    failure_code: FailureCode | None = None
