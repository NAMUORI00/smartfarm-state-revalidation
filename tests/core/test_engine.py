from __future__ import annotations

from smartfarm_state_revalidation import (
    ActionCandidate,
    ActionPrecondition,
    B0_ONE_SHOT,
    B1_BLOCK_ONLY,
    B2_MAX_ONE_REFRESH,
    Barrier,
    BarrierPatchInjector,
    DryRunExecutor,
    ExperimentEngine,
    FailureCode,
    InMemoryStateStore,
    PredicateOperator,
    ScheduledPatch,
    SnapshotEchoGenerator,
    StatePatch,
    StateSnapshot,
    declare_related_state,
)


TEMPERATURE = "sensor.zone-a.temperature_c"
HUMIDITY = "sensor.zone-a.humidity_pct"
RELATED = declare_related_state(
    "temperature-control-v1",
    [TEMPERATURE],
    tolerances={TEMPERATURE: 0.1},
    boundaries={TEMPERATURE: (25.0,)},
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
                state_key=TEMPERATURE,
                operator=PredicateOperator.EQ,
                expected=request.snapshot.values[TEMPERATURE],
            ),
        ),
    )


def run_with(policy, *patches: ScheduledPatch):
    store = InMemoryStateStore(
        StateSnapshot(
            revision=10,
            values={TEMPERATURE: 24.0, HUMIDITY: 70.0},
        )
    )
    injector = BarrierPatchInjector(store, list(patches))
    generator = SnapshotEchoGenerator(action_for_snapshot)
    result = ExperimentEngine(
        store,
        generator,
        DryRunExecutor(),
        injector=injector,
    ).run(
        policy,
        question="현재 온도에 맞는 냉각 동작을 제안해줘.",
        related_state=RELATED,
        evidence=("frozen-evidence",),
    )
    return result, generator


def test_b0_one_shot_keeps_stale_claim_and_would_apply_invalid_action() -> None:
    result, generator = run_with(
        B0_ONE_SHOT,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={TEMPERATURE: 26.0}),
        ),
    )

    assert generator.call_count == 1
    assert result.refresh_count == 0
    assert result.s0.snapshot.values[TEMPERATURE] == 24.0
    assert result.s_release.snapshot.values[TEMPERATURE] == 26.0
    assert result.response.state_claims[0].value == 24.0
    assert result.response.action_candidate is not None
    assert result.execution.applied is True
    assert result.execution.valid is False
    assert result.failure_code is FailureCode.PRECONDITION_NOT_ENFORCED


def test_b1_blocks_action_but_does_not_refresh_stale_claim() -> None:
    result, generator = run_with(
        B1_BLOCK_ONLY,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={TEMPERATURE: 26.0}),
        ),
    )

    assert generator.call_count == 1
    assert result.refresh_count == 0
    assert result.response.state_claims[0].value == 24.0
    assert result.response.action_candidate is None
    assert result.action_blocked is True
    assert result.execution.applied is False
    assert result.failure_code is FailureCode.RELEASE_STATE_CHANGED


def test_b2_refreshes_once_and_ignores_unrelated_change_before_execution() -> None:
    result, generator = run_with(
        B2_MAX_ONE_REFRESH,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={TEMPERATURE: 26.0}),
        ),
        ScheduledPatch(
            barrier=Barrier.APPROVAL_EVENT,
            patch=StatePatch(values={HUMIDITY: 72.0}),
        ),
    )

    assert generator.call_count == 2
    assert result.refresh_count == 1
    assert result.s0.snapshot.revision == 10
    assert result.s_release.snapshot.revision == 11
    assert result.s_exec.snapshot.revision == 12
    assert result.response.state_claims[0].value == 26.0
    assert result.response.state_claims[0].observed_revision == 11
    assert result.response.action_candidate is not None
    assert result.response.action_candidate.bound_state_revision == 11
    assert result.execution.applied is True
    assert result.execution.valid is True
    assert result.failure_code is None
    assert result.s_release.state_hash == result.s_exec.state_hash

    barriers = [event.barrier for event in result.trace]
    assert barriers == [
        Barrier.INITIAL_STATE_READ,
        Barrier.GENERATION_STARTED,
        Barrier.DURING_INITIAL_GENERATION,
        Barrier.GENERATION_COMPLETED,
        Barrier.POST_GENERATION_READ,
        Barrier.RELEASE_CHECK,
        Barrier.REFRESH_GENERATION_STARTED,
        Barrier.DURING_REFRESH_GENERATION,
        Barrier.REFRESH_GENERATION_COMPLETED,
        Barrier.POST_GENERATION_READ,
        Barrier.RELEASE_CHECK,
        Barrier.APPROVAL_EVENT,
        Barrier.EXECUTION_CHECK,
        Barrier.ACTION_APPLY,
    ]


def test_b2_second_relevant_change_returns_latest_claims_and_blocks_action() -> None:
    result, generator = run_with(
        B2_MAX_ONE_REFRESH,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={TEMPERATURE: 26.0}),
        ),
        ScheduledPatch(
            barrier=Barrier.DURING_REFRESH_GENERATION,
            patch=StatePatch(values={TEMPERATURE: 23.0}),
        ),
    )

    assert generator.call_count == 2
    assert result.refresh_count == 1
    assert result.response.source_revision == 12
    assert result.response.state_claims[0].value == 23.0
    assert "23.0" in result.response.text
    assert result.response.action_candidate is None
    assert result.action_blocked is True
    assert result.execution.applied is False
    assert result.failure_code is FailureCode.REFRESH_EXHAUSTED


def test_b2_final_execution_check_rejects_state_change_after_release() -> None:
    result, generator = run_with(
        B2_MAX_ONE_REFRESH,
        ScheduledPatch(
            barrier=Barrier.APPROVAL_EVENT,
            patch=StatePatch(values={TEMPERATURE: 26.0}),
        ),
    )

    assert generator.call_count == 1
    assert result.refresh_count == 0
    assert result.s_release.snapshot.values[TEMPERATURE] == 24.0
    assert result.s_exec.snapshot.values[TEMPERATURE] == 26.0
    assert result.response.action_candidate is not None
    assert result.execution.applied is False
    assert result.execution.valid is False
    assert result.failure_code is FailureCode.PRECONDITION_FAILED


def test_b2_rechecks_an_action_apply_boundary_change() -> None:
    result, _generator = run_with(
        B2_MAX_ONE_REFRESH,
        ScheduledPatch(
            barrier=Barrier.ACTION_APPLY,
            patch=StatePatch(values={TEMPERATURE: 26.0}),
        ),
    )

    assert result.s_exec.snapshot.values[TEMPERATURE] == 26.0
    assert result.execution.applied is False
    assert result.execution.valid is False
    assert result.failure_code is FailureCode.PRECONDITION_FAILED
    apply_event = next(event for event in result.trace if event.barrier is Barrier.ACTION_APPLY)
    assert apply_event.details["state_changed_after_execution_check"] is True


def test_b0_marks_post_generation_and_execution_reads_as_observer_only() -> None:
    result, _generator = run_with(B0_ONE_SHOT)

    post_read = next(event for event in result.trace if event.barrier is Barrier.POST_GENERATION_READ)
    release = next(event for event in result.trace if event.barrier is Barrier.RELEASE_CHECK)
    execution = next(event for event in result.trace if event.barrier is Barrier.EXECUTION_CHECK)
    assert post_read.details["observer_only"] is True
    assert release.details["observer_only"] is True
    assert execution.details["observer_only"] is True


def test_b2_does_not_refresh_for_unrelated_state_revision() -> None:
    result, generator = run_with(
        B2_MAX_ONE_REFRESH,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            patch=StatePatch(values={HUMIDITY: 75.0}),
        ),
    )

    assert generator.call_count == 1
    assert result.refresh_count == 0
    assert result.s0.snapshot.revision == 10
    assert result.s_release.snapshot.revision == 11
    assert result.release_diff.changed is False
    assert result.s0.state_hash == result.s_release.state_hash


def test_ordered_barrier_patches_are_applied_without_sleep() -> None:
    result, _generator = run_with(
        B2_MAX_ONE_REFRESH,
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            order=2,
            patch=StatePatch(values={TEMPERATURE: 26.0}),
        ),
        ScheduledPatch(
            barrier=Barrier.DURING_INITIAL_GENERATION,
            order=1,
            patch=StatePatch(values={TEMPERATURE: 25.5}),
        ),
    )

    assert result.s_release.snapshot.values[TEMPERATURE] == 26.0
    during_event = next(
        event for event in result.trace if event.barrier is Barrier.DURING_INITIAL_GENERATION
    )
    assert during_event.details["injected_patch_count"] == 2
