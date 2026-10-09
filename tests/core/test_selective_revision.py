from __future__ import annotations

import pytest

from smartfarm_state_revalidation import (
    ActionCandidate,
    ActionPrecondition,
    AnswerUnitSpec,
    B2_MAX_ONE_REFRESH,
    Barrier,
    BarrierPatchInjector,
    DryRunExecutor,
    ExperimentEngine,
    FailureCode,
    InMemoryStateStore,
    PredicateOperator,
    RelatedStateSpec,
    SR_SELECTIVE_REVISION,
    ScheduledPatch,
    SnapshotEchoGenerator,
    StatePatch,
    StateSnapshot,
    affected_unit_ids,
    compare_related,
    declare_answer_units,
    declare_related_state,
)


ROOT_TEMP = "sensor.zone-a.root_zone_temperature_c"
SUPPLY_EC = "sensor.zone-a.supply_ec_ds_m"
TANK_LEVEL = "sensor.zone-a.tank_level_pct"

# 근권 온도와 양액은 서로 다른 답변 단위에 속한다.
# 탱크 수위는 관수 단위에서 양액과 함께 쓰인다.
RELATED = declare_related_state(
    "wasabi-root-zone-v2",
    [ROOT_TEMP, SUPPLY_EC, TANK_LEVEL],
    tolerances={ROOT_TEMP: 0.1, SUPPLY_EC: 0.05, TANK_LEVEL: 1.0},
    unit_specs=declare_answer_units(
        [
            ("thermal", [ROOT_TEMP]),
            ("nutrient", [SUPPLY_EC, TANK_LEVEL]),
        ]
    ),
)


def action_for_snapshot(request) -> ActionCandidate:
    return ActionCandidate(
        action="set_cooling_enabled",
        target="cooler",
        value=True,
        zone_id="zone-a",
        bound_state_revision=request.snapshot.revision,
        preconditions=(
            ActionPrecondition(
                state_key=ROOT_TEMP,
                operator=PredicateOperator.EQ,
                expected=request.snapshot.values[ROOT_TEMP],
            ),
        ),
    )


def run_with(policy, *patches: ScheduledPatch):
    store = InMemoryStateStore(
        StateSnapshot(
            revision=10,
            values={ROOT_TEMP: 16.0, SUPPLY_EC: 1.2, TANK_LEVEL: 80.0},
        )
    )
    generator = SnapshotEchoGenerator(action_for_snapshot)
    result = ExperimentEngine(
        store,
        generator,
        DryRunExecutor(),
        injector=BarrierPatchInjector(store, list(patches)),
    ).run(
        policy,
        question="근권 온도와 양액 상태에 맞는 조치를 알려줘.",
        related_state=RELATED,
        evidence=("frozen-evidence",),
    )
    return result, generator


def unit(result, unit_id):
    return next(item for item in result.response.units if item.unit_id == unit_id)


# --- 단위 선언 ---------------------------------------------------------------


def test_units_default_to_one_unit_per_state_key() -> None:
    spec = declare_related_state("plain-v1", [ROOT_TEMP, SUPPLY_EC])

    units = spec.answer_units()

    assert [item.unit_id for item in units] == [ROOT_TEMP, SUPPLY_EC]
    assert [item.depends_on for item in units] == [(ROOT_TEMP,), (SUPPLY_EC,)]


def test_declared_units_may_group_several_state_keys() -> None:
    assert [item.depends_on for item in RELATED.answer_units()] == [
        (ROOT_TEMP,),
        (SUPPLY_EC, TANK_LEVEL),
    ]


def test_unit_declaration_rejects_undeclared_state_key() -> None:
    with pytest.raises(ValueError, match="undeclared state keys"):
        declare_related_state(
            "bad-v1",
            [ROOT_TEMP],
            unit_specs=[AnswerUnitSpec(unit_id="thermal", depends_on=(SUPPLY_EC,))],
        )


def test_unit_declaration_requires_every_state_key_to_be_covered() -> None:
    with pytest.raises(ValueError, match="not covered by any answer unit"):
        declare_related_state(
            "bad-v2",
            [ROOT_TEMP, SUPPLY_EC],
            unit_specs=[AnswerUnitSpec(unit_id="thermal", depends_on=(ROOT_TEMP,))],
        )


def test_unit_declaration_rejects_duplicate_unit_ids() -> None:
    with pytest.raises(ValueError, match="answer unit ids must be unique"):
        RelatedStateSpec(
            profile_id="bad-v3",
            dependencies=declare_related_state("x", [ROOT_TEMP, SUPPLY_EC]).dependencies,
            unit_specs=(
                AnswerUnitSpec(unit_id="same", depends_on=(ROOT_TEMP,)),
                AnswerUnitSpec(unit_id="same", depends_on=(SUPPLY_EC,)),
            ),
        )


# --- 영향 범위 판정 -----------------------------------------------------------


def test_affected_units_cover_only_units_depending_on_changed_keys() -> None:
    before = StateSnapshot(revision=1, values={ROOT_TEMP: 16.0, SUPPLY_EC: 1.2, TANK_LEVEL: 80.0})
    after = StateSnapshot(revision=2, values={ROOT_TEMP: 19.0, SUPPLY_EC: 1.2, TANK_LEVEL: 80.0})

    diff = compare_related(before, after, RELATED)

    assert affected_unit_ids(RELATED, diff) == ("thermal",)


def test_a_shared_state_key_marks_its_whole_unit_as_affected() -> None:
    before = StateSnapshot(revision=1, values={ROOT_TEMP: 16.0, SUPPLY_EC: 1.2, TANK_LEVEL: 80.0})
    after = StateSnapshot(revision=2, values={ROOT_TEMP: 16.0, SUPPLY_EC: 1.2, TANK_LEVEL: 40.0})

    diff = compare_related(before, after, RELATED)

    assert affected_unit_ids(RELATED, diff) == ("nutrient",)


def test_no_relevant_change_affects_no_unit() -> None:
    before = StateSnapshot(revision=1, values={ROOT_TEMP: 16.0, SUPPLY_EC: 1.2, TANK_LEVEL: 80.0})
    after = StateSnapshot(revision=2, values={ROOT_TEMP: 16.05, SUPPLY_EC: 1.2, TANK_LEVEL: 80.0})

    diff = compare_related(before, after, RELATED)

    assert affected_unit_ids(RELATED, diff) == ()


# --- 선택적 수정 동작 ---------------------------------------------------------


def test_selective_revision_rewrites_only_the_affected_unit() -> None:
    result, _ = run_with(
        SR_SELECTIVE_REVISION,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={ROOT_TEMP: 19.0}),
        ),
    )

    assert result.refresh_count == 1
    thermal = unit(result, "thermal")
    nutrient = unit(result, "nutrient")
    assert thermal.revised is True
    assert thermal.claims[0].value == 19.0
    assert nutrient.revised is False


def test_preserved_unit_keeps_its_original_observation_revision() -> None:
    result, _ = run_with(
        SR_SELECTIVE_REVISION,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={ROOT_TEMP: 19.0}),
        ),
    )

    thermal = unit(result, "thermal")
    nutrient = unit(result, "nutrient")
    assert nutrient.source_revision == result.s0.snapshot.revision
    assert thermal.source_revision == result.s_release.snapshot.revision


def test_selective_revision_matches_full_refresh_on_the_changed_claim() -> None:
    patch = ScheduledPatch(
        barrier=Barrier.DURING_INITIAL_GENERATION,
        patch=StatePatch(values={ROOT_TEMP: 19.0}),
    )

    selective, _ = run_with(SR_SELECTIVE_REVISION, patch)
    full, _ = run_with(B2_MAX_ONE_REFRESH, patch)

    def claim(result, key):
        return next(item for item in result.response.state_claims if item.key == key)

    assert claim(selective, ROOT_TEMP).value == claim(full, ROOT_TEMP).value == 19.0
    assert selective.execution.valid == full.execution.valid


def test_selective_revision_regenerates_fewer_units_than_full_refresh() -> None:
    patch = ScheduledPatch(
        barrier=Barrier.DURING_INITIAL_GENERATION,
        patch=StatePatch(values={ROOT_TEMP: 19.0}),
    )

    _, selective_generator = run_with(SR_SELECTIVE_REVISION, patch)
    _, full_generator = run_with(B2_MAX_ONE_REFRESH, patch)

    # 최초 생성 2단위 + 갱신 1단위 = 3, 전체 갱신은 2 + 2 = 4
    assert selective_generator.generated_unit_count == 3
    assert full_generator.generated_unit_count == 4


def test_full_refresh_rewrites_every_unit() -> None:
    result, _ = run_with(
        B2_MAX_ONE_REFRESH,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={ROOT_TEMP: 19.0}),
        ),
    )

    assert [item.revised for item in result.response.units] == [True, True]


def test_selective_revision_without_relevant_change_preserves_every_unit() -> None:
    result, generator = run_with(SR_SELECTIVE_REVISION)

    assert result.refresh_count == 0
    assert generator.generated_unit_count == 2
    assert [item.revised for item in result.response.units] == [False, False]
    assert result.execution.applied is True


def test_second_relevant_change_drops_the_action_and_reports_latest_state() -> None:
    result, _ = run_with(
        SR_SELECTIVE_REVISION,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={ROOT_TEMP: 19.0}),
        ),
        ScheduledPatch(
            barrier=Barrier.DURING_REFRESH_GENERATION,
            patch=StatePatch(values={ROOT_TEMP: 22.0}),
        ),
    )

    assert result.action_blocked is True
    assert result.failure_code is FailureCode.REFRESH_EXHAUSTED
    assert result.response.action_candidate is None
    # 예산 소진 시에는 부분 보존 없이 최신 상태 요약만 남는다.
    assert all(item.revised for item in result.response.units)
    assert unit(result, "thermal").claims[0].value == 22.0


def test_trace_records_the_revision_scope() -> None:
    result, _ = run_with(
        SR_SELECTIVE_REVISION,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={ROOT_TEMP: 19.0}),
        ),
    )

    started = next(
        event
        for event in result.trace
        if event.barrier is Barrier.REFRESH_GENERATION_STARTED
    )
    assert started.details["selective_revision"] is True
    assert started.details["revise_unit_ids"] == ["thermal"]
    assert started.details["declared_unit_count"] == 2
