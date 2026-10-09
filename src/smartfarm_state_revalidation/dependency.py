from __future__ import annotations

from collections.abc import Iterable

from .contracts import AnswerUnitSpec, RelatedStateSpec, StateDependency, StateDiff


class DependencyRegistry:
    """Resolve only predeclared related-state profiles.

    The registry deliberately performs no model-output or keyword inference.
    A scenario or request manifest must name the profile before generation.
    """

    def __init__(self, profiles: Iterable[RelatedStateSpec] = ()) -> None:
        self._profiles: dict[str, RelatedStateSpec] = {}
        for profile in profiles:
            self.add(profile)

    def add(self, profile: RelatedStateSpec) -> None:
        if profile.profile_id in self._profiles:
            raise ValueError(f"duplicate related-state profile: {profile.profile_id}")
        self._profiles[profile.profile_id] = profile

    def resolve(self, profile_id: str) -> RelatedStateSpec:
        try:
            return self._profiles[profile_id]
        except KeyError as error:
            raise KeyError(f"unknown related-state profile: {profile_id}") from error


def affected_unit_ids(spec: RelatedStateSpec, diff: StateDiff) -> tuple[str, ...]:
    """관련 상태 변화의 영향을 받은 답변 단위를 선언 순서대로 돌려준다.

    판정은 사전 선언된 단위 의존관계와 이미 계산된 상태 차이만 사용하며
    생성된 답변 문자열을 보지 않는다.
    """
    changed = {change.key for change in diff.changes}
    return tuple(
        unit.unit_id
        for unit in spec.answer_units()
        if any(key in changed for key in unit.depends_on)
    )


def declare_answer_units(
    groups: Iterable[tuple[str, Iterable[str]]],
) -> tuple[AnswerUnitSpec, ...]:
    """(단위 이름, 의존 상태키) 목록을 단위 선언으로 바꾼다."""
    return tuple(
        AnswerUnitSpec(unit_id=unit_id, depends_on=tuple(keys))
        for unit_id, keys in groups
    )


def declare_related_state(
    profile_id: str,
    keys: Iterable[str],
    *,
    tolerances: dict[str, float] | None = None,
    boundaries: dict[str, tuple[float, ...]] | None = None,
    units: dict[str, str] | None = None,
    unit_specs: Iterable[AnswerUnitSpec] = (),
) -> RelatedStateSpec:
    tolerances = tolerances or {}
    boundaries = boundaries or {}
    units = units or {}
    return RelatedStateSpec(
        profile_id=profile_id,
        dependencies=tuple(
            StateDependency(
                key=key,
                tolerance=tolerances.get(key, 0.0),
                boundaries=boundaries.get(key, ()),
                unit=units.get(key),
            )
            for key in keys
        ),
        unit_specs=tuple(unit_specs),
    )
